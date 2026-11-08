"""Rule thresholds. Defaults are conservative; ``eval/calibration.json`` (produced by ``eval/calibrate.py`` on a
*separate validation dataset*) overrides them. They are tuned on synthetic data only, never on real fraud."""
from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {
    "fan_in_min_payers": 5,            # distinct verified payers linked to one beneficiary
    "shared_group_min": 2,             # accounts sharing one device/IP
    "cluster_min_accounts": 3,         # accounts in a connected device/IP cluster
    "burst_min_captures": 4, "burst_window_min": 60,
    "uniform_min_captures": 4, "uniform_max_spread": "0.10",
    "outlier_min_captures": 6, "outlier_mad_z": "5.0",
    "failed_repeat_min": 3, "failed_window_min": 1440,
    "night_min_captures": 6, "night_share": "0.70",
    "concentration_share": "0.60", "concentration_min_items": 3,
    "payout_freq_min": 5, "payout_freq_window_min": 1440,
    "duplicate_window_min": 15,
    "dispute_ratio": "0.15", "dispute_ratio_min_captures": 4, "dispute_repeat_reason_min": 2,
    "hold_score": 70, "review_score": 25,
}
_CAL = Path(__file__).resolve().parent.parent / "eval" / "calibration.json"


def load() -> dict:
    t = dict(DEFAULTS)
    try:
        t.update(json.loads(_CAL.read_text()).get("thresholds", {}))
    except (OSError, ValueError):
        pass
    return t


TH = load()
