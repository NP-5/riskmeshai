import json
from helpers import FakePayPal, raises, settings
from riskmesh import demo_data, rules
from riskmesh.bundles import make_bundle
from riskmesh.paypal import PayPalClient, PayPalError, PayPalUnavailable
from riskmesh.thresholds import TH


def bundle_for(name, n, **kw):
    sc = demo_data.build_scenario(name, "r@x.test", [f"P{name}{i}" for i in range(n)], **kw)
    sess = {s["order_id"]: s for s in sc["sessions"]}
    caps = [{**c, "device": sess.get(c["order_id"], {}).get("device"), "ip": sess.get(c["order_id"], {}).get("ip")} for c in sc["captures"]]
    return make_bundle("beneficiary", "R", caps, sc["payout_batches"], sc["payout_items"], sc["disputes"])


def decide(b):
    f = [x for x in (rules.evaluate(b, r[0]) for r in rules.RULES) if x]
    return rules.assess(f, b["subject"]["type"], bool(b["links"])), {x["rule_id"]: x["outcome"] for x in f}


def test_ring_is_held_only_with_fan_in_and_shared_infrastructure():
    a, o = decide(bundle_for("A", 7, devices=["d1"] * 4 + ["d2"] * 3, ips=["1.1.1.1"] * 7))
    assert a["decision"] == "HOLD" and a["ring"] == "VERIFIED_INDICATORS" and o["fan_in_verified"] == "FAIL" and o["shared_device"] == "FAIL"


def test_missing_device_ip_data_is_unknown_not_clear():
    a, o = decide(bundle_for("B", 7, with_sessions=False))
    assert o["shared_device"] == "CANNOT" and o["shared_ip"] == "CANNOT"
    assert a["ring"] == "UNKNOWN" and a["decision"] != "RELEASE"


def test_fan_in_threshold_boundary():
    four = bundle_for("C", 4, devices=["d"] * 4)
    five = bundle_for("D", 5, devices=["d"] * 5)
    assert decide(four)[1]["fan_in_verified"] == "PASS" and decide(five)[1]["fan_in_verified"] == "FAIL"
    assert TH["fan_in_min_payers"] == 5


def test_no_verified_link_means_fan_in_cannot_be_assessed():
    b = bundle_for("E", 6)
    for i in b["payout_items"]:
        i["sender_item_id"] = "UNKNOWN"
    b = make_bundle("beneficiary", "R", b["captures"], b["payout_batches"], b["payout_items"], [])
    assert b["links"] == [] and decide(b)[1]["fan_in_verified"] == "CANNOT"


def test_batch_reconciliation_detects_header_mismatch():
    sc = demo_data.build_scenario("F", "r@x.test", ["P1", "P2"], header_delta="12.50", minutes_apart=300)
    b = make_bundle("payout", "BATCH-F", [], sc["payout_batches"], sc["payout_items"], [])
    assert decide(b)[1]["batch_reconciliation"] == "FAIL"


# ---- PayPal client --------------------------------------------------------------------------------------------------
def client(fake, **env):
    return PayPalClient(settings(PAYPAL_CLIENT_ID="id", PAYPAL_CLIENT_SECRET="secret", PAYPAL_WEBHOOK_ID="wh", PAYPAL_BASE="https://api-m.sandbox.paypal.com", **env), transport=fake, sleep=lambda x: None)


def test_cancel_is_never_retried_even_on_5xx_or_timeout():
    f = FakePayPal(); f.on["cancel"] = (503, {"name": "INTERNAL"})
    raises(lambda: client(f).cancel_payout_item("ITEM-1"), PayPalError)
    assert f.count("/cancel") == 1
    f2 = FakePayPal(); f2.on["cancel"] = PayPalUnavailable("timeout")
    raises(lambda: client(f2).cancel_payout_item("ITEM-1"), PayPalUnavailable)
    assert f2.count("/cancel") == 1


def test_reads_are_retried_and_token_is_cached():
    f = FakePayPal(); seq = iter([(503, {}), (200, {"transaction_status": "UNCLAIMED"})]); f.on["item"] = lambda: next(seq)
    c = client(f)
    assert c.get_payout_item("ITEM-1")["transaction_status"] == "UNCLAIMED"
    assert f.count("ITEM-1") == 2 and f.count("/v1/oauth2/token") == 1


def test_paypal_error_carries_issue_and_debug_id():
    f = FakePayPal(); f.on["cancel"] = (422, {"name": "UNPROCESSABLE", "debug_id": "dbg123", "details": [{"issue": "ITEM_ALREADY_CLAIMED"}]})
    e = raises(lambda: client(f).cancel_payout_item("ITEM-1"), PayPalError)
    assert e.issue == "ITEM_ALREADY_CLAIMED" and e.debug_id == "dbg123" and e.status == 422


def test_webhook_verification_rejects_foreign_cert_host_and_missing_headers():
    f = FakePayPal(); c = client(f)
    good = {"paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/x", "paypal-transmission-id": "t",
            "paypal-transmission-sig": "s", "paypal-transmission-time": "2026-11-01T10:00:00Z"}
    assert c.verify_webhook(good, {"id": "E"}) is True
    assert c.verify_webhook({**good, "paypal-cert-url": "https://evil.example.com/cert"}, {"id": "E"}) is False
    assert c.verify_webhook({**good, "paypal-cert-url": "http://api.paypal.com/cert"}, {"id": "E"}) is False
    assert c.verify_webhook({k: v for k, v in good.items() if k != "paypal-transmission-sig"}, {"id": "E"}) is False
    assert f.count("verify-webhook-signature") == 1          # only the good one reached PayPal
    f.verify_result = "FAILURE"
    assert c.verify_webhook(good, {"id": "E"}) is False


def test_mock_header_is_sandbox_only():
    f = FakePayPal()
    live = PayPalClient(settings(PAYPAL_CLIENT_ID="id", PAYPAL_CLIENT_SECRET="s", PAYPAL_BASE="https://api-m.paypal.com"), transport=f, sleep=lambda x: None)
    raises(lambda: live.cancel_payout_item("ITEM-1", mock="INSTRUMENT_DECLINED"), ValueError, "sandbox")
