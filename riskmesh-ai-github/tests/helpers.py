import json
from asgi_client import request
from app import App
from riskmesh import demo_data
from riskmesh.config import load_settings
from riskmesh.db import Database
from riskmesh.paypal import PayPalClient, PayPalUnavailable

ADMIN, REVIEWER, ANALYST, VIEWER = ("a" * 20, "r" * 20, "n" * 20, "v" * 20)
TOKENS = f"{ADMIN}:admin1:admin,{REVIEWER}:rev1:reviewer,{ANALYST}:ana1:analyst,{VIEWER}:view1:viewer"


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def settings(**env):
    e = {"RISKMESH_ENV": "development", "RISKMESH_DEMO_MODE": "1", "DATABASE_URL": "sqlite:///:memory:", "RISKMESH_API_TOKENS": TOKENS, "RISKMESH_PEPPER": "p" * 20}
    e.update({k: str(v) for k, v in env.items()})
    return load_settings({k: v for k, v in e.items() if v != "None"})


def raises(fn, exc=Exception, contains=""):
    try:
        fn()
    except exc as e:
        assert contains in str(e), f"{contains!r} not in {e!r}"
        return e
    raise AssertionError(f"{exc.__name__} not raised")


class FakePayPal:
    """Records every call; behaviour per path can be overridden: fake.on['cancel'] = (status, body) or an exception."""

    def __init__(self):
        self.calls, self.item_status, self.on = [], "UNCLAIMED", {}
        self.verify_result = "SUCCESS"

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("paypal.com", 1)[1]
        self.calls.append((method, path))
        key = ("cancel" if path.endswith("/cancel") else "verify" if "verify-webhook" in path else "token" if "oauth2" in path else "item" if "payouts-item" in path else "other")
        if key in self.on:
            r = self.on[key]
            if isinstance(r, Exception):
                raise r
            if callable(r):
                r = r()
            return r[0], {}, json.dumps(r[1]).encode()
        if key == "token":
            return 200, {}, json.dumps({"access_token": "tok", "expires_in": 3000}).encode()
        if key == "verify":
            return 200, {}, json.dumps({"verification_status": self.verify_result}).encode()
        if key == "cancel":
            self.item_status = "CANCELED"
            return 200, {}, b"{}"
        if key == "item":
            return 200, {}, json.dumps({"transaction_status": self.item_status}).encode()
        return 404, {}, b"{}"

    def count(self, suffix):
        return sum(1 for m, p in self.calls if p.endswith(suffix))


def make_app(fake=None, provider=None, **env):
    s = settings(**env)
    client = PayPalClient(s, transport=fake, sleep=lambda x: None) if fake else None
    return App(s, Database(s.db_url), client, provider=provider), s


def seeded(fake=None, **env):
    app, s = make_app(fake, **env)
    demo_data.seed(app.db, s.pepper)
    subs = request(app, "GET", "/api/subjects", headers=H(VIEWER)).json()["subjects"]
    by = {x["items"]: x["subject_id"] for x in subs}
    return app, s, {"ring": by[7], "nosession": by[6], "clean": by[2]}


def investigate(app, sid, tok=ANALYST):
    r = request(app, "POST", "/api/investigations", {"subject_type": "beneficiary", "subject_id": sid}, H(tok))
    assert r.status == 200, r.body
    return r.json()


def webhook_body(eid="WH-1", etype="PAYMENT.CAPTURE.COMPLETED", cap="CAPTURE-1", order="ORDER-1", value="25.00"):
    return {"id": eid, "event_type": etype, "create_time": "2026-11-01T10:00:00Z",
            "resource": {"id": cap, "status": "COMPLETED", "amount": {"currency_code": "USD", "value": value}, "invoice_id": "INV-1", "create_time": "2026-11-01T10:00:00Z",
                         "supplementary_data": {"related_ids": {"order_id": order}}}}
