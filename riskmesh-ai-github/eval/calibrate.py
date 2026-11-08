"""Choose rule thresholds on the CALIBRATION set only (eval/data/calibration.json) and write eval/calibration.json.

Constraints first, accuracy second:
  1. no real ring may be RELEASEd, and no non-ring case may be HOLDed (a wrong hold freezes someone's money)
  2. among the remaining settings, maximise decision accuracy; ties go to the setting closest to the defaults
The held-out test set is never read here. Thresholds are tuned on synthetic data and say nothing about real fraud rates.
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from riskmesh import rules                       # noqa: E402
from riskmesh.thresholds import DEFAULTS, TH      # noqa: E402

GRID = {"fan_in_min_payers": [4, 5, 6, 7, 8], "review_score": [15, 20, 25, 30, 40], "hold_score": [60, 70, 80],
        "shared_group_min": [2, 3], "payout_freq_min": [4, 5, 6, 8]}


def decide(bundle):
    st = bundle["subject"]["type"]
    findings = [f for f in (rules.evaluate(bundle, r[0]) for r in rules.RULES) if f]
    return rules.assess(findings, st, bool(bundle["links"]))["decision"]


def evaluate(cases):
    ok = dangerous = wrong_hold = 0
    for c in cases:
        d = decide(c["bundle"])
        ok += d == c["truth"]
        dangerous += d == "RELEASE" and c["truth"] == "HOLD"
        wrong_hold += d == "HOLD" and c["truth"] != "HOLD"
    return ok, dangerous, wrong_hold


def distance(t):
    return sum(abs(float(t[k]) - float(DEFAULTS[k])) / max(1.0, float(DEFAULTS[k])) for k in GRID)


def main():
    cases = json.loads((ROOT / "eval" / "data" / "calibration.json").read_text())["cases"]
    keys, best, tried = list(GRID), None, 0
    for combo in itertools.product(*GRID.values()):
        TH.update(DEFAULTS)
        TH.update(dict(zip(keys, combo)))
        ok, dang, wh = evaluate(cases)
        tried += 1
        if dang or wh:
            continue
        cand = (ok, -distance(TH), dict(zip(keys, combo)))
        if best is None or cand[:2] > best[:2]:
            best = cand
    if best is None:
        raise SystemExit("no threshold setting satisfies the safety constraints on the calibration set")
    TH.update(DEFAULTS); TH.update(best[2])
    base = dict(DEFAULTS)
    TH.update(base); d_ok, d_dang, d_wh = evaluate(cases)
    TH.update(best[2]); b_ok, _, _ = evaluate(cases)
    out = {"thresholds": best[2], "calibration_set": {"cases": len(cases), "accuracy_at_defaults": round(d_ok / len(cases), 3), "accuracy_calibrated": round(b_ok / len(cases), 3)},
           "settings_tried": tried, "note": "Chosen on eval/data/calibration.json only (synthetic). Not tuned on the held-out test set and not on real fraud data."}
    (ROOT / "eval" / "calibration.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
