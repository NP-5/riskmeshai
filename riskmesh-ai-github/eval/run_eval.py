"""Held-out evaluation: RiskMesh vs two naive baselines on eval/data/test.json (never used for calibration).

    python eval/run_eval.py            writes eval/results.json and eval/results.md
Honest limits are written into the report: synthetic data, scenario-defined ground truth, same author, no LLM involved.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from riskmesh.agents.orchestrator import Orchestrator   # noqa: E402
from riskmesh.config import load_settings                # noqa: E402
from riskmesh.thresholds import TH                       # noqa: E402

DATA = ROOT / "eval" / "data"
DECISIONS = ("HOLD", "REVIEW", "RELEASE")


def naive_amount(b):
    """HOLD when the total captured amount is >= $1,000, else RELEASE."""
    return "HOLD" if sum((D(c["amount"]) for c in b["captures"] if c["amount"] != "UNKNOWN"), D(0)) >= 1000 else "RELEASE"


def naive_fan_in(b):
    """HOLD when five or more distinct payers fund one beneficiary, else RELEASE."""
    return "HOLD" if len({c["payer_id"] for c in b["captures"]}) >= 5 else "RELEASE"


def riskmesh(cases, settings=None):
    s = settings or load_settings({"RISKMESH_ENV": "development"})
    out = {}
    for c in cases:
        b = c["bundle"]
        r = asyncio.run(Orchestrator(s, None).investigate(c["subject_type"], b["subject"]["id"], b, lambda b=b: b))
        out[c["id"]] = {"decision": r["report"]["risk"]["decision"], "score": r["report"]["risk"]["score"], "ring": r["report"]["risk"]["ring"],
                        "rejected": len(r["report"]["rejected_claims"]), "traced": r["report"]["traceability"]["all_traced"]}
    return out


def score(cases, pred):
    n = len(cases)
    ok = sum(pred[c["id"]] == c["truth"] for c in cases)
    problem = [c for c in cases if c["truth"] != "RELEASE"]
    clean = [c for c in cases if c["truth"] == "RELEASE"]
    holds = [c for c in cases if c["truth"] == "HOLD"]
    return {"correct": ok, "n": n, "accuracy": round(ok / n, 3),
            "problem_flagged": sum(pred[c["id"]] != "RELEASE" for c in problem), "problem_n": len(problem),
            "clean_wrongly_flagged": sum(pred[c["id"]] != "RELEASE" for c in clean), "clean_n": len(clean),
            "ring_holds_found": sum(pred[c["id"]] == "HOLD" for c in holds), "ring_holds_n": len(holds),
            "wrong_holds": sum(pred[c["id"]] == "HOLD" and c["truth"] != "HOLD" for c in cases),
            "dangerous_releases": sum(pred[c["id"]] == "RELEASE" and c["truth"] == "HOLD" for c in cases)}


def main():
    cases = json.loads((DATA / "test.json").read_text())["cases"]
    t0 = time.time()
    rm = riskmesh(cases)
    preds = {"naive_amount": {c["id"]: naive_amount(c["bundle"]) for c in cases}, "naive_fan_in": {c["id"]: naive_fan_in(c["bundle"]) for c in cases},
             "riskmesh": {k: v["decision"] for k, v in rm.items()}}
    res = {"dataset": "eval/data/test.json (held out)", "cases": len(cases), "thresholds": {k: TH[k] for k in TH}, "systems": {k: score(cases, p) for k, p in preds.items()}}
    by = defaultdict(lambda: {"n": 0, "riskmesh_correct": 0, "truth": ""})
    for c in cases:
        r = by[c["type"]]; r["n"] += 1; r["truth"] = c["truth"]; r["riskmesh_correct"] += preds["riskmesh"][c["id"]] == c["truth"]
    res["by_type"] = dict(by)
    conf = Counter((c["truth"], preds["riskmesh"][c["id"]]) for c in cases)
    res["confusion"] = {t: {p: conf[(t, p)] for p in DECISIONS} for t in DECISIONS}
    inj = [c for c in cases if c["injected"]]
    res["injection"] = {"cases": len(inj), "decision_changed": sum(preds["riskmesh"][c["id"]] != c["truth"] for c in inj), "note": "No model is in the loop here, so this shows only that no code path reads free text; it does not test an LLM's resistance."}
    res["integrity"] = {"all_statements_traced": all(v["traced"] for v in rm.values()), "claims_rejected_by_reviewers": sum(v["rejected"] for v in rm.values())}
    res["runtime_s"] = round(time.time() - t0, 1)
    res["errors"] = [{"id": c["id"], "type": c["type"], "truth": c["truth"], "got": preds["riskmesh"][c["id"]], "score": rm[c["id"]]["score"]} for c in cases if preds["riskmesh"][c["id"]] != c["truth"]]
    (ROOT / "eval" / "results.json").write_text(json.dumps(res, indent=1))
    (ROOT / "eval" / "results.md").write_text(markdown(res))
    print(markdown(res))


def markdown(r):
    s = r["systems"]
    row = lambda label, f: f"| {label} | " + " | ".join(f(s[k]) for k in ("naive_amount", "naive_fan_in", "riskmesh")) + " |"
    L = [f"# Evaluation (held-out test set, {r['cases']} synthetic cases)", "", "| Measure | Naive amount rule | Naive fan-in rule | RiskMesh |", "|---|---|---|---|",
         row("Correct decision (HOLD/REVIEW/RELEASE)", lambda x: f"{x['correct']}/{x['n']}"),
         row("Problem cases flagged (not RELEASE)", lambda x: f"{x['problem_flagged']}/{x['problem_n']}"),
         row("Clean cases wrongly flagged", lambda x: f"{x['clean_wrongly_flagged']}/{x['clean_n']}"),
         row("Rings correctly held", lambda x: f"{x['ring_holds_found']}/{x['ring_holds_n']}"),
         row("Wrong HOLDs (money held without a ring)", lambda x: str(x["wrong_holds"])),
         row("Dangerous RELEASEs of a real ring", lambda x: str(x["dangerous_releases"])), "",
         "RiskMesh confusion (rows = ground truth, columns = decision):", "", "| truth \\ decision | HOLD | REVIEW | RELEASE |", "|---|---|---|---|"]
    for t, d in r["confusion"].items():
        L.append(f"| {t} | {d['HOLD']} | {d['REVIEW']} | {d['RELEASE']} |")
    L += ["", "By scenario type (RiskMesh correct / cases):", ""]
    L += [f"- {t} (truth {v['truth']}): {v['riskmesh_correct']}/{v['n']}" for t, v in r["by_type"].items()]
    if r["errors"]:
        L += ["", "RiskMesh errors:", ""] + [f"- {e['id']} {e['type']}: expected {e['truth']}, got {e['got']} (score {e['score']})" for e in r["errors"]]
    L += ["", f"Injected-memo cases: {r['injection']['decision_changed']} of {r['injection']['cases']} decisions differed from ground truth. {r['injection']['note']}", "",
          "**Read these honestly.** Cases are synthetic and their ground truth was fixed by the scenario design by the same author as the rules. "
          "Thresholds were chosen on a separate calibration set (eval/data/calibration.json), not on this one. The naive rules are straw men that cannot answer REVIEW. "
          "This shows the pipeline is consistent and what it trades off; it is not evidence of performance on real fraud or against PayPal's production risk systems."]
    return "\n".join(L)


if __name__ == "__main__":
    main()
