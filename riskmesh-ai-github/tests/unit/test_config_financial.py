from decimal import Decimal
from helpers import raises, settings
from riskmesh import config as cfg
from riskmesh import financial as fin
from riskmesh.bundles import make_bundle


def test_parse_tokens_and_validation():
    t = cfg.parse_tokens("abcdefghijklmnop1:alice:reviewer, abcdefghijklmnop2:bob:viewer")
    assert [(x.identity, x.role) for x in t] == [("alice", "reviewer"), ("bob", "viewer")]
    raises(lambda: cfg.parse_tokens("short:a:admin"), ValueError, "16")
    raises(lambda: cfg.parse_tokens("abcdefghijklmnop1:a:root"), ValueError, "unknown role")
    raises(lambda: cfg.parse_tokens("abcdefghijklmnop1:a"), ValueError, "token:identity:role")


def test_production_refuses_unsafe_settings():
    s = cfg.load_settings({"RISKMESH_ENV": "production", "RISKMESH_DEMO_MODE": "1", "DATABASE_URL": "sqlite:///:memory:"})
    errors, _ = cfg.validate(s)
    text = " ".join(errors)
    assert "DEMO_MODE" in text and "API_TOKENS" in text and "PEPPER" in text and "in-memory" in text
    assert not s.tokens            # no generated dev token in production


def test_dev_generates_token_and_public_view_hides_secrets():
    s = cfg.load_settings({"PAYPAL_CLIENT_SECRET": "super-secret-value", "RISKMESH_LLM_KEY": "sk-secret", "RISKMESH_LLM_MODEL": "m"})
    assert s.generated_token and s.tokens[0].role == "admin"
    blob = str(s.public_view())
    assert "super-secret-value" not in blob and "sk-secret" not in blob and s.generated_token not in blob


def test_llm_requires_model():
    s = cfg.load_settings({"RISKMESH_LLM_PROVIDER": "openai", "RISKMESH_LLM_KEY": "k"})
    assert any("MODEL" in e for e in cfg.validate(s)[0])


def test_money_is_decimal_and_strict():
    assert fin.parse_money("10.10", "USD") + fin.parse_money("0.20", "USD") == Decimal("10.30")   # no float drift
    raises(lambda: fin.parse_money("10.123", "USD"), fin.ValidationError)
    raises(lambda: fin.parse_money("-5", "USD"), fin.ValidationError)
    raises(lambda: fin.parse_money("abc", "USD"), fin.ValidationError)
    raises(lambda: fin.parse_money("1e3", "USD"), fin.ValidationError)
    assert fin.money_or_unknown(None, "USD") == fin.UNKNOWN
    assert fin.parse_money("100", "JPY") == Decimal("100")
    raises(lambda: fin.parse_money("100.5", "JPY"), fin.ValidationError)      # JPY has no minor unit


def test_ids_and_currency_validation():
    assert fin.valid_id("CAPTURE-1", "x") == "CAPTURE-1"
    for bad in ("", "a b", "x;DROP", "../etc", None, "a" * 80):
        raises(lambda b=bad: fin.valid_id(b, "x"), fin.ValidationError)
    assert fin.valid_currency("usd") == "USD"
    raises(lambda: fin.valid_currency("XXX"), fin.ValidationError)


def test_identifier_hash_is_peppered_and_masked():
    a, b = fin.hash_identifier("x@example.com", "pepper-1"), fin.hash_identifier("x@example.com", "pepper-2")
    assert a != b and a == fin.hash_identifier("x@example.com", "pepper-1") and "example" not in a
    assert "x@example.com" not in fin.mask_identifier("x@example.com")


def _cap(i, inv="UNKNOWN", amt="10.00"):
    return {"capture_id": f"C{i:02d}", "order_id": f"O{i:02d}", "payer_id": f"P{i}", "merchant_id": "M", "amount": amt, "currency": "USD", "status": "COMPLETED",
            "created_at": "2026-11-01T10:00:00Z", "invoice_id": inv, "custom_id": "UNKNOWN"}


def _item(i, key="UNKNOWN", amt="10.00"):
    return {"item_id": f"I{i:02d}", "batch_id": "B1", "recipient_id": "R1", "recipient_display": "r***", "amount": amt, "currency": "USD", "status": "UNCLAIMED",
            "sender_item_id": key, "created_at": "2026-11-01T10:05:00Z"}


def test_links_need_a_shared_key_never_amounts_or_timing():
    caps, items = [_cap(1), _cap(2)], [_item(1), _item(2)]            # identical amounts and times, no key
    assert fin.verified_links(caps, items, []) == []
    caps = [_cap(1, inv="INV-1"), _cap(2)]
    links = fin.verified_links(caps, [_item(1, key="INV-1"), _item(2)], [])
    assert [(l["capture_id"], l["item_id"]) for l in links] == [("C01", "I01")] and links[0]["basis"] == "SENDER_ITEM_ID=INVOICE_ID"


def test_explicit_links_only_between_known_records():
    b = make_bundle("beneficiary", "R1", [_cap(1)], [], [_item(1)], [], explicit_links=[{"capture_id": "C01", "item_id": "I01", "basis": "ANALYST"}, {"capture_id": "NOPE", "item_id": "I01"}])
    assert len(b["links"]) == 1
