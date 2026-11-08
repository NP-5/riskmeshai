"""One command, no accounts: seed synthetic data, run the agent team, approve one simulated hold.

    python scripts/quickstart.py                      # fully local, deterministic (no AI model)
    GEMINI_API_KEY=<your key> python scripts/quickstart.py
                                                      # adds the Gemini-written, code-checked case brief

Runs in demo mode in memory: PayPal is not called and the hold is labelled SIMULATED. For the real sandbox see docs/sandbox_runbook.md.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(RISKMESH_ENV="development", RISKMESH_DEMO_MODE="1", DATABASE_URL="sqlite:///:memory:", RISKMESH_PEPPER=os.environ.get("RISKMESH_PEPPER", "quickstart-pepper-0001"))
from riskmesh import demo_data                        # noqa: E402
from riskmesh.config import load_settings, validate   # noqa: E402
from riskmesh.db import Database                      # noqa: E402
from riskmesh.paypal import PayPalClient              # noqa: E402
from riskmesh.service import Service                  # noqa: E402


def main():
    s = load_settings()
    errors, _ = validate(s)
    if errors:
        raise SystemExit("config problem: " + "; ".join(errors))
    svc = Service(s, Database(s.db_url), PayPalClient(s))
    ids = demo_data.seed(svc.db, s.pepper)
    print(f"AI model: {s.llm_provider} {s.llm_model}".rstrip() if s.llm_provider != "none" else "AI model: none configured (deterministic rules only)")
    last = None
    for name in ("ring", "nosession", "clean"):
        inv = svc.investigate("beneficiary", ids[name], "quickstart", "admin")
        r = inv["report"]["risk"]
        print(f"\n[{name}] decision={inv['recommendation']} score={r['score']} ring={r['ring']} claims={len(inv['claims'])} pending_holds={len(inv['actions'])}")
        print("  next:", r["recommended_next_action"])
        b = inv["report"]["ai_brief"]
        print("  AI brief:", "\n    - " + "\n    - ".join(x["text"] for x in b["sentences"]) if b["available"] else b["reason"])
        if inv["actions"] and last is None:
            last = inv["actions"][0]["id"]
    if last:
        a = svc.approve_action(last, "quickstart-reviewer", "reviewer", "demo")
        print(f"\napproved {last}: status={a['status']} mode={a['mode']} -> {a['verification']}")


if __name__ == "__main__":
    main()
