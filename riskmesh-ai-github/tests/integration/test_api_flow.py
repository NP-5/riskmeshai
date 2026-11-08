import json
from asgi_client import request
from helpers import ADMIN, ANALYST, REVIEWER, VIEWER, FakePayPal, H, investigate, make_app, seeded
from riskmesh.paypal import PayPalUnavailable


def test_auth_and_roles():
    app, _, ids = seeded()
    assert request(app, "GET", "/api/config").status == 401
    assert request(app, "GET", "/api/config", headers={"Authorization": "Bearer wrong-token-value-xx"}).status == 401
    assert request(app, "GET", "/api/config", headers=H(VIEWER)).status == 200
    body = {"subject_type": "beneficiary", "subject_id": ids["ring"]}
    assert request(app, "POST", "/api/investigations", body, H(VIEWER)).status == 403
    assert request(app, "POST", "/api/investigations", body, H(ANALYST)).status == 200
    assert request(app, "GET", "/api/audit", headers=H(REVIEWER)).status == 403
    assert request(app, "GET", "/api/audit", headers=H(ADMIN)).status == 200
    assert request(app, "GET", "/healthz").status == 200 and request(app, "DELETE", "/api/config", headers=H(ADMIN)).status == 405
    assert request(app, "GET", "/nope").status == 404


def test_public_endpoints_leak_nothing_and_config_hides_secrets():
    app, _ = make_app(PAYPAL_CLIENT_SECRET="very-secret-1234", RISKMESH_LLM_KEY="sk-leak", RISKMESH_LLM_MODEL="m")
    cfg = request(app, "GET", "/api/config", headers=H(VIEWER)).text if hasattr(request, "text") else request(app, "GET", "/api/config", headers=H(VIEWER)).body.decode()
    assert "very-secret-1234" not in cfg and "sk-leak" not in cfg
    assert b"ring" not in request(app, "GET", "/readyz").body


def test_ring_gets_hold_with_pending_actions_and_traceable_claims():
    app, _, ids = seeded()
    inv = investigate(app, ids["ring"])
    assert inv["recommendation"] == "HOLD" and inv["ring"] == "VERIFIED_INDICATORS"
    assert len(inv["proposed_actions"]) == 7 and {a["status"] for a in inv["actions"]} == {"PENDING"}
    assert inv["report"]["traceability"]["all_traced"] is True
    st = {s["id"]: s for s in inv["report"]["statements"]}
    assert st and all(s["claim_ids"] for s in st.values())
    assert inv["report"]["risk"]["confidence_limitations"]
    trace = request(app, "GET", f"/api/investigations/{inv['id']}/trace", headers=H(VIEWER)).json()
    assert trace["plan"] and trace["rounds"] and trace["messages"] and any(t["agent"] == "evidence-auditor" for t in trace["tasks"])


def test_clean_is_released_and_missing_data_is_not():
    app, _, ids = seeded()
    clean, nos = investigate(app, ids["clean"]), investigate(app, ids["nosession"])
    assert clean["recommendation"] == "RELEASE" and clean["actions"] == []
    assert nos["recommendation"] == "REVIEW" and nos["ring"] == "UNKNOWN" and nos["actions"] == []
    assert request(app, "GET", "/api/actions?status=PENDING", headers=H(VIEWER)).json()["actions"] == []


def test_input_validation_on_investigations():
    app, _, ids = seeded()
    for body in ({"subject_type": "nonsense", "subject_id": ids["ring"]}, {"subject_type": "beneficiary", "subject_id": "bad id; drop"}, {}):
        assert request(app, "POST", "/api/investigations", body, H(ANALYST)).status == 422
    assert request(app, "POST", "/api/investigations", {"subject_type": "beneficiary", "subject_id": "rcpt_unknownunknown"}, H(ANALYST)).status == 404
    assert request(app, "POST", "/api/investigations", None, H(ANALYST), raw=b"{oops").status == 400
    assert request(app, "POST", "/api/investigations", None, H(ANALYST), raw=b"x" * 70000).status == 413


def test_only_a_reviewer_can_approve_and_double_approval_is_409():
    app, _, ids = seeded()
    inv = investigate(app, ids["ring"]); aid = inv["proposed_actions"][0]
    assert request(app, "POST", f"/api/actions/{aid}/approve", {}, H(ANALYST)).status == 403
    assert request(app, "POST", f"/api/actions/{aid}/approve", {}, H(VIEWER)).status == 403
    ok = request(app, "POST", f"/api/actions/{aid}/approve", {"reason": "ring confirmed"}, H(REVIEWER))
    assert ok.status == 200 and ok.json()["status"] == "SIMULATED" and ok.json()["mode"] == "demo" and ok.json()["decided_by"] == "rev1"
    again = request(app, "POST", f"/api/actions/{aid}/approve", {}, H(REVIEWER))
    assert again.status == 409
    assert request(app, "POST", f"/api/actions/{aid}/reject", {}, H(REVIEWER)).status == 409
    assert request(app, "POST", "/api/actions/ACT-NOPE/approve", {}, H(REVIEWER)).status == 404
    log = request(app, "GET", "/api/audit?limit=500", headers=H(ADMIN)).json()["audit"]
    assert any(l["action"] == "action.approve" and l["outcome"] == "conflict" for l in log)       # refusals are audited too
    assert any(l["action"] == "action.approve" and l["outcome"] == "APPROVED" and l["actor"] == "rev1" for l in log)


def test_reject_closes_the_request_and_reinvestigation_does_not_duplicate_open_ones():
    app, _, ids = seeded()
    inv = investigate(app, ids["ring"]); aid = inv["proposed_actions"][0]
    assert request(app, "POST", f"/api/actions/{aid}/reject", {"reason": "false positive"}, H(REVIEWER)).json()["status"] == "REJECTED"
    inv2 = investigate(app, ids["ring"])
    pending = request(app, "GET", "/api/actions?status=PENDING", headers=H(VIEWER)).json()["actions"]
    assert len(pending) == 7 and len({a["target_id"] for a in pending}) == 7          # one open request per payout item, rejected one re-proposed once


def test_execution_rechecks_evidence_and_refuses_when_item_is_no_longer_cancellable():
    app, _, ids = seeded()
    inv = investigate(app, ids["ring"]); act = inv["actions"][0]
    with app.db.tx() as c:
        c.execute("UPDATE payout_items SET status = 'SUCCEEDED' WHERE item_id = ?", (act["target_id"],))
    r = request(app, "POST", f"/api/actions/{act['id']}/approve", {}, H(REVIEWER)).json()
    assert r["status"] == "FAILED" and "not cancellable" in r["verification"]["blocked"]


def test_execution_refuses_when_decision_is_no_longer_hold():
    app, _, ids = seeded()
    inv = investigate(app, ids["ring"]); act = inv["actions"][0]
    with app.db.tx() as c:
        c.execute("DELETE FROM sessions")            # device/IP evidence disappears: ring can no longer be verified
        c.execute("DELETE FROM disputes")
    r = request(app, "POST", f"/api/actions/{act['id']}/approve", {}, H(REVIEWER)).json()
    assert r["status"] == "FAILED" and "no longer HOLD" in r["verification"]["blocked"]


def test_unconfigured_paypal_outside_demo_mode_never_simulates():
    app, _, ids = seeded(RISKMESH_DEMO_MODE="1")
    inv = investigate(app, ids["ring"]); act = inv["actions"][0]
    app.s = app.svc.s = __import__("dataclasses").replace(app.s, demo_mode=False)
    r = request(app, "POST", f"/api/actions/{act['id']}/approve", {}, H(REVIEWER)).json()
    assert r["status"] == "FAILED" and "not configured" in r["verification"]["error"]


def _live(**env):
    f = FakePayPal()
    app, s, ids = seeded(f, PAYPAL_CLIENT_ID="id", PAYPAL_CLIENT_SECRET="secret", PAYPAL_WEBHOOK_ID="WH", RISKMESH_DEMO_MODE="0", **env)
    return app, f, ids


def test_live_execution_calls_cancel_once_and_stores_paypal_readback():
    app, f, ids = _live()
    inv = investigate(app, ids["ring"]); act = inv["actions"][0]
    r = request(app, "POST", f"/api/actions/{act['id']}/approve", {"reason": "x"}, H(REVIEWER)).json()
    assert r["status"] == "EXECUTED" and r["mode"] == "sandbox" and r["paypal_http_status"] == 200
    assert r["verification"] == {"call_outcome": "ok", "paypal_item_status": "CANCELED", "confirmed_cancelled": True}
    assert f.count("/cancel") == 1


def test_paypal_refusal_is_reported_with_debug_id_and_nothing_is_claimed():
    app, f, ids = _live()
    f.on["cancel"] = (422, {"name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg-777", "details": [{"issue": "ITEM_ALREADY_CLAIMED"}]})
    inv = investigate(app, ids["ring"]); act = inv["actions"][0]
    r = request(app, "POST", f"/api/actions/{act['id']}/approve", {}, H(REVIEWER)).json()
    assert r["status"] == "FAILED" and r["paypal_http_status"] == 422 and r["verification"]["confirmed_cancelled"] is False
    assert r["paypal_response"]["debug_id"] == "dbg-777" and r["paypal_response"]["issue"] == "ITEM_ALREADY_CLAIMED" if "issue" in r["paypal_response"] else r["paypal_response"]["debug_id"] == "dbg-777"
    assert f.count("/cancel") == 1


def test_timeout_is_uncertain_not_retried_and_not_reported_as_success():
    app, f, ids = _live()
    f.on["cancel"] = PayPalUnavailable("timeout")
    inv = investigate(app, ids["ring"]); act = inv["actions"][0]
    r = request(app, "POST", f"/api/actions/{act['id']}/approve", {}, H(REVIEWER)).json()
    assert r["status"] == "UNCERTAIN" and f.count("/cancel") == 1 and "Re-read" in r["verification"]["next_step"]
    # an UNCERTAIN action blocks a blind second attempt
    assert request(app, "POST", f"/api/actions/{act['id']}/approve", {}, H(REVIEWER)).status == 409


def test_rate_limit_and_security_headers():
    app, _ = make_app(RISKMESH_RATE_LIMIT_PER_MIN="3")
    codes = [request(app, "GET", "/api/config", headers=H(VIEWER)).status for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = request(app, "GET", "/api/config", headers=H(ADMIN))          # another identity has its own window
    assert r.status == 200 and r.headers["x-content-type-options"] == "nosniff" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert request(app, "GET", "/healthz").status == 200            # health checks are never throttled


def test_readyz_reports_not_ready_when_unverifiable():
    app, _ = make_app(RISKMESH_DEMO_MODE="0")
    r = request(app, "GET", "/readyz")
    assert r.status == 503 and r.json()["checks"]["paypal_verification_configured"] is False


def test_demo_seed_is_refused_outside_demo_mode_and_sessions_links_are_validated():
    app, _ = make_app(RISKMESH_DEMO_MODE="0")
    assert request(app, "POST", "/api/demo/seed", {}, H(ADMIN)).status == 403
    app, _, ids = seeded()
    assert request(app, "POST", "/api/sessions", {"order_id": "ORD-RING-01", "device": "dev new", "ip": "1.2.3.4"}, H(ANALYST)).status == 422
    assert request(app, "POST", "/api/sessions", {"order_id": "ORD-RING-01", "device": "dev-new", "ip": "203.0.113.5"}, H(ANALYST)).status == 200
    assert request(app, "POST", "/api/links", {"capture_id": "CAP-NOPE", "item_id": "ITEM-RING-01"}, H(ANALYST)).status == 404
    assert request(app, "POST", "/api/links", {"capture_id": "CAP-RING-01", "item_id": "ITEM-RING-01", "basis": "analyst"}, H(ANALYST)).status == 200


def test_sandbox_helpers_are_blocked_against_live_base_url():
    f = FakePayPal()
    app, _ = make_app(f, PAYPAL_CLIENT_ID="id", PAYPAL_CLIENT_SECRET="s", PAYPAL_BASE="https://api-m.paypal.com", PAYPAL_WEBHOOK_ID="w", RISKMESH_DEMO_MODE="0")
    assert request(app, "POST", "/api/sandbox/order", {}, H(ADMIN)).status == 403
    assert request(app, "POST", "/api/sandbox/capture/ORDER-1?mock=INSTRUMENT_DECLINED", {}, H(ADMIN)).status == 403
