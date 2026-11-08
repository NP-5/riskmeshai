# Evaluation (held-out test set, 72 synthetic cases)

| Measure | Naive amount rule | Naive fan-in rule | RiskMesh |
|---|---|---|---|
| Correct decision (HOLD/REVIEW/RELEASE) | 15/72 | 24/72 | 60/72 |
| Problem cases flagged (not RELEASE) | 0/48 | 33/48 | 42/48 |
| Clean cases wrongly flagged | 9/24 | 12/24 | 6/24 |
| Rings correctly held | 0/12 | 12/12 | 12/12 |
| Wrong HOLDs (money held without a ring) | 9 | 33 | 0 |
| Dangerous RELEASEs of a real ring | 12 | 0 | 0 |

RiskMesh confusion (rows = ground truth, columns = decision):

| truth \ decision | HOLD | REVIEW | RELEASE |
|---|---|---|---|
| HOLD | 12 | 0 | 0 |
| REVIEW | 0 | 30 | 6 |
| RELEASE | 0 | 6 | 18 |

By scenario type (RiskMesh correct / cases):

- clean_single (truth RELEASE): 6/6
- clean_repeat (truth RELEASE): 6/6
- popular_seller (truth RELEASE): 0/6
- ring_full (truth HOLD): 6/6
- ring_injected (truth HOLD): 6/6
- ring_small_shared_device (truth REVIEW): 6/6
- fan_in_distinct_infra (truth REVIEW): 6/6
- ring_no_session_data (truth REVIEW): 6/6
- batch_mismatch (truth REVIEW): 6/6
- dispute_heavy (truth REVIEW): 6/6
- velocity_burst_merchant (truth REVIEW): 0/6
- clean_merchant (truth RELEASE): 6/6

RiskMesh errors:

- t003 popular_seller: expected RELEASE, got REVIEW (score 60)
- t011 velocity_burst_merchant: expected REVIEW, got RELEASE (score 20)
- t015 popular_seller: expected RELEASE, got REVIEW (score 60)
- t023 velocity_burst_merchant: expected REVIEW, got RELEASE (score 20)
- t027 popular_seller: expected RELEASE, got REVIEW (score 60)
- t035 velocity_burst_merchant: expected REVIEW, got RELEASE (score 20)
- t039 popular_seller: expected RELEASE, got REVIEW (score 60)
- t047 velocity_burst_merchant: expected REVIEW, got RELEASE (score 20)
- t051 popular_seller: expected RELEASE, got REVIEW (score 60)
- t059 velocity_burst_merchant: expected REVIEW, got RELEASE (score 20)
- t063 popular_seller: expected RELEASE, got REVIEW (score 60)
- t071 velocity_burst_merchant: expected REVIEW, got RELEASE (score 20)

Injected-memo cases: 0 of 6 decisions differed from ground truth. No model is in the loop here, so this shows only that no code path reads free text; it does not test an LLM's resistance.

**Read these honestly.** Cases are synthetic and their ground truth was fixed by the scenario design by the same author as the rules. Thresholds were chosen on a separate calibration set (eval/data/calibration.json), not on this one. The naive rules are straw men that cannot answer REVIEW. This shows the pipeline is consistent and what it trades off; it is not evidence of performance on real fraud or against PayPal's production risk systems.