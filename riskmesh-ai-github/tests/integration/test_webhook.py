import json
from asgi_client import request
from helpers import ADMIN, FakePayPal, VIEWER, H, make_app, webhook_body

SIG = {"paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/x", "paypal-transmission-id": "t1",
       "paypal-transmission-sig": "sig", "paypal-transmission-time": "2026-11-01T10:00:00Z"}


def post(app, body, headers=None, raw=None):
    return request(app, "POST", "/webhook", body if raw is None else None, headers or {}, raw=raw)


def test_demo_mode_accepts_unsigned_and_labels_it():
    app, _ = make_app()
    r = post(app, webhook_body())
    assert r.status == 200 and r.json()["verification"] == "DEMO_UNVERIFIED" and r.json()["entities"] == {"captures": 1}
    ev = request(app, "GET", "/api/events", headers=H(VIEWER)).json()["events"]
    assert ev[0]["verification"] == "DEMO_UNVERIFIED"


def test_duplicate_event_is_ignored_and_not_stored_twice():
    app, _ = make_app()
    assert post(app, webhook_body()).json()["status"] == "accepted"
    assert post(app, webhook_body()).json()["status"] == "duplicate"
    assert len(request(app, "GET", "/api/events", headers=H(VIEWER)).json()["events"]) == 1


def test_fails_closed_without_verification_config():
    app, _ = make_app(RISKMESH_DEMO_MODE="0")
    r = post(app, webhook_body())
    assert r.status == 503 and "refusing" in r.json()["error"]
    assert request(app, "GET", "/api/events", headers=H(VIEWER)).json()["events"] == []


def test_signed_event_in_demo_mode_without_verifier_is_refused():
    app, _ = make_app()
    assert post(app, webhook_body(), SIG).status == 503


def test_valid_signature_is_accepted_and_bad_signature_is_401():
    f = FakePayPal()
    app, _ = make_app(f, PAYPAL_CLIENT_ID="id", PAYPAL_CLIENT_SECRET="secret", PAYPAL_WEBHOOK_ID="WH", RISKMESH_DEMO_MODE="0")
    ok = post(app, webhook_body("WH-A"), SIG)
    assert ok.status == 200 and ok.json()["verification"] == "VERIFIED_BY_PAYPAL"
    f.verify_result = "FAILURE"
    bad = post(app, webhook_body("WH-B"), SIG)
    assert bad.status == 401
    assert [e["event_id"] for e in request(app, "GET", "/api/events", headers=H(VIEWER)).json()["events"]] == ["WH-A"]      # rejected event never stored


def test_verification_outage_returns_5xx_so_paypal_retries():
    from riskmesh.paypal import PayPalUnavailable
    f = FakePayPal(); f.on["verify"] = PayPalUnavailable("down")
    app, _ = make_app(f, PAYPAL_CLIENT_ID="id", PAYPAL_CLIENT_SECRET="secret", PAYPAL_WEBHOOK_ID="WH", RISKMESH_DEMO_MODE="0")
    assert post(app, webhook_body(), SIG).status == 503


def test_malformed_payloads_are_rejected():
    app, _ = make_app()
    assert post(app, None, raw=b"not json").status == 400
    assert post(app, None, raw=b"[1,2]").status == 400
    assert post(app, {"id": "EVT-1", "event_type": "SOMETHING.ELSE", "resource": {}}).status == 422
    assert post(app, {"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {}}).status == 400
    for i, bad in enumerate(("12.345", "NaN", "1e3", "-5")):          # a signed event with an unusable amount is kept, but the amount is UNKNOWN, never guessed
        assert post(app, webhook_body(eid=f"WH-BAD{i}", cap=f"CAP-BAD{i}", value=bad)).status == 200
        with app.db.tx() as c:
            assert c.one("SELECT amount FROM captures WHERE capture_id = ?", (f"CAP-BAD{i}",))["amount"] == "UNKNOWN"
    assert post(app, webhook_body(cap="bad id;")).status == 422
    assert post(app, None, raw=b"x" * 1_100_000).status == 413


def test_payout_item_event_pseudonymises_recipient():
    app, _ = make_app()
    ev = {"id": "WH-P1", "event_type": "PAYMENT.PAYOUTS-ITEM.UNCLAIMED", "create_time": "2026-11-01T10:00:00Z",
          "resource": {"payout_item_id": "ITEM-1", "payout_batch_id": "BATCH-1", "transaction_status": "UNCLAIMED", "time_processed": "2026-11-01T10:00:00Z",
                       "payout_item": {"receiver": "person@example.com", "amount": {"value": "10.00", "currency": "USD"}, "sender_item_id": "INV-1"}}}
    assert post(app, ev).status == 200
    with app.db.tx() as c:
        row = c.one("SELECT * FROM payout_items WHERE item_id = 'ITEM-1'")
    assert row["recipient_id"].startswith("rcpt_") and "person@example.com" not in json.dumps(row)
    with app.db.tx() as c:
        stored = c.one("SELECT payload_json FROM webhook_events WHERE event_id='WH-P1'")
    # the raw payload is kept for audit; make sure the audit trail and API outputs do not expose it
    assert "person@example.com" not in json.dumps(request(app, "GET", "/api/audit?limit=50", headers=H(ADMIN)).json())
    assert "person@example.com" not in json.dumps(request(app, "GET", "/api/events", headers=H(VIEWER)).json())
