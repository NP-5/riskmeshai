import json
from pathlib import Path
from helpers import raises, settings
from riskmesh.db import Database, audit, redact

ROOT = Path(__file__).resolve().parent.parent.parent


def test_eval_datasets_are_disjoint_and_have_ground_truth():
    cal = json.loads((ROOT / "eval/data/calibration.json").read_text())["cases"]
    test = json.loads((ROOT / "eval/data/test.json").read_text())["cases"]
    assert not ({c["id"] for c in cal} & {c["id"] for c in test})
    assert all(c["truth"] in ("HOLD", "REVIEW", "RELEASE") for c in cal + test)
    assert {c["type"] for c in test} == {c["type"] for c in cal} and len({c["type"] for c in test}) >= 12
    assert any(c["injected"] for c in test)


def test_eval_results_match_a_fresh_run_of_the_held_out_set():
    import sys; sys.path.insert(0, str(ROOT / "eval"))
    import run_eval
    cases = json.loads((ROOT / "eval/data/test.json").read_text())["cases"][:24]
    got = run_eval.riskmesh(cases)
    saved = json.loads((ROOT / "eval/results.json").read_text())
    assert saved["cases"] == 72 and saved["systems"]["riskmesh"]["dangerous_releases"] == 0
    sc = run_eval.score(cases, {k: v["decision"] for k, v in got.items()})
    assert sc["dangerous_releases"] == 0 and sc["wrong_holds"] == 0


def test_transaction_rolls_back_on_error():
    db = Database("sqlite:///:memory:")
    def boom():
        with db.tx() as c:
            audit(c, "a", "r", "x", "t", "o")
            raise RuntimeError("fail")
    raises(boom, RuntimeError)
    with db.tx() as c:
        assert c.one("SELECT COUNT(*) AS n FROM audit_log")["n"] == 0


def test_conflicting_open_action_for_same_target_is_blocked_by_the_database():
    db = Database("sqlite:///:memory:")
    ins = "INSERT INTO actions (id, investigation_id, action_type, target_id, status, idempotency_key, requested_by, created_at) VALUES (?,?,?,?,?,?,?,?)"
    with db.tx() as c:
        c.execute(ins, ("A1", "I", "HOLD_PAYOUT_ITEM", "ITEM-1", "PENDING", "k1", "x", "t"))
    def dup():
        with db.tx() as c:
            c.execute(ins, ("A2", "I", "HOLD_PAYOUT_ITEM", "ITEM-1", "PENDING", "k2", "x", "t"))
    raises(dup)
    with db.tx() as c:
        c.execute("UPDATE actions SET status = 'REJECTED' WHERE id = 'A1'")
        c.execute(ins, ("A3", "I", "HOLD_PAYOUT_ITEM", "ITEM-1", "PENDING", "k3", "x", "t"))      # allowed again once the first is closed


def test_redaction_removes_credentials_and_email_addresses():
    r = redact({"client_secret": "abc", "Authorization": "Bearer x", "note": "pay bob@example.com now", "nested": {"api_key": "k", "ok": 1}, "paypal_cert_url": "u"})
    assert r["client_secret"] == r["Authorization"] == r["paypal_cert_url"] == "[REDACTED]" and r["nested"] == {"api_key": "[REDACTED]", "ok": 1}
    assert "bob@example.com" not in json.dumps(r) and "[email]" in r["note"]


def test_postgres_url_is_normalised_and_unknown_scheme_rejected():
    from riskmesh.config import load_settings
    assert load_settings({"DATABASE_URL": "postgres://u:p@h/db"}).db_url.startswith("postgresql://")
    raises(lambda: Database("mysql://x"), ValueError)
