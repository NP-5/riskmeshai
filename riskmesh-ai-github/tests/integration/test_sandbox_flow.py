"""Sandbox data path: orders (invoice_id) -> buyer approval -> capture (+device/IP) -> payout to an unclaimed recipient -> ring -> HOLD.

Uses a stateful fake of PayPal's sandbox, so it needs no network or credentials. It proves RiskMesh's side of the flow; it does NOT
prove PayPal's real response shapes (run scripts/sandbox_seed_ring.py against a real sandbox for that)."""
import argparse
import importlib.util
import json
from pathlib import Path

from asgi_client import request
from helpers import ADMIN, ANALYST, REVIEWER, VIEWER, H, make_app, raises
from riskmesh import sandbox_flow
from riskmesh.financial import UNKNOWN, ValidationError

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("seed_ring", ROOT / "scripts" / "sandbox_seed_ring.py")
seed_ring = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_ring)


class SandboxFake:
    """Just enough of PayPal's sandbox: orders, approval by a buyer, capture, payouts, unclaimed items, item read, cancel."""

    def __init__(self):
        self.orders, self.batches, self.items, self.calls = {}, {}, {}, []
        self.payer_of, self.registered, self.mock_seen = {}, set(), []
        self.pending_first, self.omit_payer_in_capture, self.reads = False, False, 0

    def approve(self, order_id, payer):
        self.payer_of[order_id] = payer

    def _r(self, status, body):
        return status, {}, json.dumps(body).encode()

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("paypal.com", 1)[1]
        self.calls.append((method, path))
        data = json.loads(body) if body and path != "/v1/oauth2/token" else {}
        if path == "/v1/oauth2/token":
            return self._r(200, {"access_token": "tok", "expires_in": 3000})
        if method == "POST" and path == "/v2/checkout/orders":
            oid = f"ORDER-{len(self.orders) + 1}"
            self.orders[oid] = data["purchase_units"][0]
            return self._r(201, {"id": oid, "status": "CREATED", "links": [{"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={oid}"}]})
        if path.startswith("/v2/checkout/orders/"):
            oid = path.split("/")[4]
            pu = self.orders[oid]
            payee = {"merchant_id": "MERCHANT-1"}
            if path.endswith("/capture"):
                mock = headers.get("PayPal-Mock-Response")
                if mock:
                    self.mock_seen.append(mock)
                    return self._r(422, {"name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg-1", "details": [{"issue": json.loads(mock)["mock_application_codes"]}]})
                if oid not in self.payer_of:
                    return self._r(422, {"name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg-2", "details": [{"issue": "ORDER_NOT_APPROVED"}]})
                cap = {"id": f"CAP-{oid}", "status": "COMPLETED", "amount": pu["amount"], "create_time": "2026-11-06T10:00:00Z", "invoice_id": pu.get("invoice_id", "")}
                resp = {"id": oid, "status": "COMPLETED", "purchase_units": [{"payee": payee, "payments": {"captures": [cap]}}]}
                if not self.omit_payer_in_capture:
                    resp["payer"] = {"payer_id": self.payer_of[oid]}
                else:
                    del resp["purchase_units"][0]["payee"]
                return self._r(201, resp)
            return self._r(200, {"id": oid, "payer": {"payer_id": self.payer_of.get(oid, "")}, "purchase_units": [{"payee": payee}]})
        if method == "POST" and path == "/v1/payments/payouts":
            bid = f"BATCH-{len(self.batches) + 1}"
            self.batches[bid] = data
            for it in data["items"]:
                iid = f"ITEM-{len(self.items) + 1}"
                self.items[iid] = {"item": it, "batch": bid, "status": "UNCLAIMED" if it["receiver"] not in self.registered else "SUCCESS"}
            return self._r(201, {"batch_header": {"payout_batch_id": bid, "batch_status": "PENDING", "sender_batch_header": data["sender_batch_header"]}})
        if method == "GET" and path.startswith("/v1/payments/payouts/"):
            bid = path.rsplit("/", 1)[1]
            self.reads += 1
            pending = self.pending_first and self.reads == 1
            items = [{"payout_item_id": k, "transaction_status": "PENDING" if pending else v["status"], "payout_batch_id": bid, "time_processed": "2026-11-06T10:05:00Z",
                      "payout_item": {"recipient_type": "EMAIL", "receiver": v["item"]["receiver"], "sender_item_id": v["item"]["sender_item_id"], "amount": v["item"]["amount"]}}
                     for k, v in self.items.items() if v["batch"] == bid]
            total = sum(float(i["payout_item"]["amount"]["value"]) for i in items)
            return self._r(200, {"batch_header": {"payout_batch_id": bid, "batch_status": "PENDING" if pending else "SUCCESS", "time_created": "2026-11-06T10:04:00Z",
                                                   "amount": {"currency": "USD", "value": f"{total:.2f}"}, "sender_batch_header": self.batches[bid]["sender_batch_header"]}, "items": items})
        if path.startswith("/v1/payments/payouts-item/"):
            iid = path.split("/")[4]
            if path.endswith("/cancel"):
                self.items[iid]["status"] = "CANCELED"
                return self._r(200, {})
            return self._r(200, {"transaction_status": self.items[iid]["status"]})
        return self._r(404, {})

    def count(self, suffix):
        return sum(1 for m, p in self.calls if p.endswith(suffix))


def make(fake=None):
    fake = fake or SandboxFake()
    app, s = make_app(fake, PAYPAL_CLIENT_ID="id", PAYPAL_CLIENT_SECRET="secret", PAYPAL_WEBHOOK_ID="WH", RISKMESH_DEMO_MODE="0")
    return app, fake


def args(**kw):
    d = dict(payers=5, amount="25.00", currency="USD", recipient="ring-collector@example.com", control_recipient="control@example.com", run_id="T1", shared_device=3,
             no_shared_ip=False, no_sessions=False, control=False, investigate=True, sync_tries=3, sync_wait=0)
    d.update(kw)
    return argparse.Namespace(**d)


def drive(app, fake, a, same_buyer=False):
    """Run the real script logic in-process. 'ask' plays the buyer: approve the newest order with a DIFFERENT Personal account each time."""
    def call(method, path, body=None):
        r = request(app, method, path, body, H(ADMIN))
        return r.status, r.json()
    n = [0]
    def buyer(_):
        n[0] += 1
        fake.approve(list(fake.orders)[-1], "BUYER-1" if same_buyer else f"BUYER-{n[0]}")
    out = []
    return seed_ring.run(a, call, ask=buyer, out=out.append, sleep=lambda x: None), out


# ---- the five steps from the sandbox guide --------------------------------------------------------------------------------
def test_full_ring_with_sessions_reaches_hold_and_the_hold_is_executable():
    app, f = make()
    res, _ = drive(app, f, args())
    inv = res["investigation"]
    assert inv["recommendation"] == "HOLD" and inv["ring"] == "VERIFIED_INDICATORS", inv
    assert res["payout"]["unclaimed"] == 5 and all(i["status"] == "UNCLAIMED" for i in res["payout"]["items"])
    assert inv["actions"] and {a["status"] for a in inv["actions"]} == {"PENDING"}
    assert f.count("/cancel") == 0                                              # proposing a hold cancels nothing
    r = request(app, "POST", f"/api/actions/{inv['actions'][0]['id']}/approve", {"reason": "ring confirmed"}, H(REVIEWER)).json()
    assert r["status"] == "EXECUTED" and r["verification"]["confirmed_cancelled"] is True and f.count("/cancel") == 1


def test_without_device_and_ip_live_data_never_reaches_hold():
    app, f = make()
    res, out = drive(app, f, args(no_sessions=True))
    assert res["investigation"]["recommendation"] != "HOLD" and res["investigation"]["actions"] == []
    assert any("no device/IP" in line for line in out)


def test_invoice_id_is_the_only_link_between_a_payment_and_a_payout_item():
    app, f = make()
    # capture with invoice RM-X-01 but pay out with an UNRELATED sender_item_id -> not linked, so no fan-in, no ring
    for i in range(5):
        o = request(app, "POST", "/api/sandbox/order", {"amount": "25.00", "invoice_id": f"RM-X-{i}"}, H(ADMIN)).json()
        f.approve(o["order_id"], f"B{i}")
        assert request(app, "POST", f"/api/sandbox/capture/{o['order_id']}", {"device": "DEV-1", "ip": "198.51.100.7"}, H(ADMIN)).status == 200
    pay = request(app, "POST", "/api/sandbox/payout", {"items": [{"receiver": "x@example.com", "amount": "25.00", "sender_item_id": f"OTHER-{i}"} for i in range(5)]}, H(ADMIN)).json()
    sid = request(app, "GET", "/api/subjects", headers=H(VIEWER)).json()["subjects"][0]["subject_id"]
    inv = request(app, "POST", "/api/investigations", {"subject_type": "beneficiary", "subject_id": sid}, H(ANALYST)).json()
    assert pay["unclaimed"] == 5 and inv["ring"] != "VERIFIED_INDICATORS" and inv["recommendation"] != "HOLD"


def test_five_orders_from_one_buyer_do_not_form_a_ring():
    app, f = make()
    res, out = drive(app, f, args(), same_buyer=True)
    assert res["investigation"]["recommendation"] != "HOLD"                      # fan-in counts DISTINCT payers
    assert sum("SAME PayPal account" in line for line in out) == 4              # the script warns the operator


def test_control_buyer_to_another_recipient_is_not_in_the_ring():
    app, f = make()
    res, _ = drive(app, f, args(control=True))
    subs = request(app, "GET", "/api/subjects", headers=H(VIEWER)).json()["subjects"]
    assert [s["items"] for s in subs] == [5, 1] and res["investigation"]["recommendation"] == "HOLD"


def test_a_recipient_with_a_paypal_account_is_not_cancellable_and_the_script_says_so():
    app, f = make(); f.registered.add("ring-collector@example.com")
    res, out = drive(app, f, args())
    assert res["payout"]["unclaimed"] == 0 and any("cannot be cancelled" in line for line in out)
    assert res["investigation"]["actions"] == []                                  # nothing to hold: items are not cancellable


def test_pending_payout_items_are_resolved_by_sync():
    app, f = make(); f.pending_first = True
    res, _ = drive(app, f, args())
    assert res["payout"]["unclaimed"] == 5 and f.reads >= 2


# ---- the endpoints ---------------------------------------------------------------------------------------------------------
def test_capture_records_payer_invoice_session_and_audits_it():
    app, f = make()
    o = request(app, "POST", "/api/sandbox/order", {"amount": "25.00", "invoice_id": "INV-9", "custom_id": "C9"}, H(ADMIN)).json()
    f.approve(o["order_id"], "PAYER-9")
    c = request(app, "POST", f"/api/sandbox/capture/{o['order_id']}", {"device": "DEV-1", "ip": "198.51.100.7"}, H(ADMIN)).json()
    assert c["ingested"] and c["session_recorded"] and c["invoice_ids"] == ["INV-9"] and c["unknown"] == [] and c["payer_ref"].startswith("rcpt_")
    row = app.db.tx().__enter__().one("SELECT * FROM captures WHERE capture_id = ?", (c["capture_ids"][0],))
    assert row["payer_id"] == "PAYER-9" and row["invoice_id"] == "INV-9" and row["source"] == "paypal-api" and row["amount"] == "25.00"
    assert "sandbox.ingest" in [a["action"] for a in request(app, "GET", "/api/audit", headers=H(ADMIN)).json()["audit"]]


def test_capture_falls_back_to_reading_the_order_and_never_guesses():
    app, f = make(); f.omit_payer_in_capture = True
    o = request(app, "POST", "/api/sandbox/order", {"amount": "25.00", "invoice_id": "INV-1"}, H(ADMIN)).json()
    f.approve(o["order_id"], "PAYER-1")
    c = request(app, "POST", f"/api/sandbox/capture/{o['order_id']}", {}, H(ADMIN)).json()
    assert c["ingested"] and c["unknown"] == [] and ("GET", f"/v2/checkout/orders/{o['order_id']}") in f.calls
    app2, f2 = make(); f2.omit_payer_in_capture = True
    f2.__class__ = type("NoOrderPayer", (SandboxFake,), {})
    o2 = request(app2, "POST", "/api/sandbox/order", {"amount": "25.00", "invoice_id": "INV-2"}, H(ADMIN)).json()
    f2.approve(o2["order_id"], "")                                   # PayPal gives no payer id anywhere
    c2 = request(app2, "POST", f"/api/sandbox/capture/{o2['order_id']}", {}, H(ADMIN)).json()
    assert "payer_id" in c2["unknown"] and c2["payer_ref"] is None


def test_half_a_session_is_refused_before_any_money_moves():
    app, f = make()
    o = request(app, "POST", "/api/sandbox/order", {"amount": "25.00", "invoice_id": "INV-3"}, H(ADMIN)).json()
    f.approve(o["order_id"], "P")
    assert request(app, "POST", f"/api/sandbox/capture/{o['order_id']}", {"device": "DEV-1"}, H(ADMIN)).status == 422
    assert request(app, "POST", f"/api/sandbox/capture/{o['order_id']}", {"device": "bad id;", "ip": "1.2.3.4"}, H(ADMIN)).status == 422
    assert f.count("/capture") == 0


def test_negative_testing_header_is_passed_and_failed_capture_creates_no_evidence():
    app, f = make()
    o = request(app, "POST", "/api/sandbox/order", {"amount": "25.00", "invoice_id": "INV-4"}, H(ADMIN)).json()
    f.approve(o["order_id"], "P")
    r = request(app, "POST", f"/api/sandbox/capture/{o['order_id']}?mock=DUPLICATE_INVOICE_ID", {}, H(ADMIN))
    assert r.status == 502 and r.json()["paypal_issue"] == "DUPLICATE_INVOICE_ID" and r.json()["debug_id"] == "dbg-1"   # project convention: PayPal refusals surface as 502 + debug_id
    assert f.mock_seen == ['{"mock_application_codes": "DUPLICATE_INVOICE_ID"}']
    assert app.db.tx().__enter__().one("SELECT COUNT(*) AS n FROM captures")["n"] == 0


def test_payout_endpoint_validates_strictly_and_needs_admin():
    app, f = make()
    ok = {"items": [{"receiver": "a@example.com", "amount": "10.00", "sender_item_id": "SID-1"}]}
    assert request(app, "POST", "/api/sandbox/payout", ok, H(ANALYST)).status == 403
    for bad in ({}, {"items": []}, {"items": [{"receiver": "no-at-sign", "amount": "1.00", "sender_item_id": "SID-1"}]},
                {"items": [{"receiver": "a@b.co", "amount": "1e3", "sender_item_id": "SID-1"}]},
                {"items": [{"receiver": "a@b.co", "amount": "-5.00", "sender_item_id": "SID-1"}]},
                {"items": [{"receiver": "a@b.co", "amount": "5.00", "sender_item_id": "bad id"}]},
                {"items": [{"receiver": "a@b.co", "amount": "5.00", "sender_item_id": "SID-1"}, {"receiver": "c@b.co", "amount": "5.00", "sender_item_id": "SID-1"}]},
                {"currency": "XXX", "items": [{"receiver": "a@b.co", "amount": "5.00", "sender_item_id": "SID-1"}]},
                {"items": [{"receiver": "a@b.co", "amount": "5.00", "sender_item_id": "SID-%d" % i} for i in range(26)]}):
        assert request(app, "POST", "/api/sandbox/payout", bad, H(ADMIN)).status == 422, bad
    assert f.count("/payouts") == 0                                              # nothing reached PayPal
    good = request(app, "POST", "/api/sandbox/payout", ok, H(ADMIN))
    assert good.status == 200 and good.json()["items"][0]["receiver"] == "a***@example.com"   # masked, never the raw email


def test_payout_and_capture_helpers_are_disabled_against_a_live_base_url():
    app, f = make()
    app.s = app.svc.s = __import__("dataclasses").replace(app.s, paypal_base="https://api-m.paypal.com")
    for path in ("/api/sandbox/payout", "/api/sandbox/payout/BATCH-1/sync", "/api/sandbox/capture/ORDER-1"):
        assert request(app, "POST", path, {"items": []}, H(ADMIN)).status == 403
    assert f.calls == []


# ---- parsing is conservative ------------------------------------------------------------------------------------------------
def test_missing_facts_stay_unknown_and_bad_ids_are_rejected():
    rows = sandbox_flow.capture_rows({"id": "ORDER-1", "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"id": "CAP-1", "status": "COMPLETED"}]}}]})
    c = rows["captures"][0]
    assert c["payer_id"] == UNKNOWN and c["merchant_id"] == UNKNOWN and c["invoice_id"] == UNKNOWN and c["amount"] == UNKNOWN
    assert sandbox_flow.unknown_facts(rows) == ["amount", "invoice_id", "merchant_id", "payer_id"]
    raises(lambda: sandbox_flow.capture_rows({"id": "bad id!", "purchase_units": []}), ValidationError)
    raises(lambda: sandbox_flow.validate_payout_request({"items": [{"receiver": "a b@c.d", "amount": "1.00", "sender_item_id": "SID-1"}]}), ValidationError)


def test_a_late_webhook_merges_into_the_same_rows_without_losing_known_facts():
    app, f = make()
    o = request(app, "POST", "/api/sandbox/order", {"amount": "25.00", "invoice_id": "INV-W"}, H(ADMIN)).json()
    f.approve(o["order_id"], "PAYER-W")
    cap = request(app, "POST", f"/api/sandbox/capture/{o['order_id']}", {}, H(ADMIN)).json()["capture_ids"][0]
    from riskmesh.ingest import _merge
    with app.db.tx() as c:                                                        # what a webhook that lacks payer/merchant would try to write
        _merge(c, "captures", ("capture_id",), {"capture_id": cap, "payer_id": UNKNOWN, "merchant_id": UNKNOWN, "status": "COMPLETED"})
        row = c.one("SELECT * FROM captures WHERE capture_id = ?", (cap,))
    assert row["payer_id"] == "PAYER-W" and row["merchant_id"] == "MERCHANT-1"
