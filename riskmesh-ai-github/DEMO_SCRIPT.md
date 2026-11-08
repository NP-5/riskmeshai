# 2-minute demo script (live service in demo mode, or the static snapshot for steps 1-3)
**0:00** "Fraud rings hide in plain sight: each payment looks fine, the group does not. RiskMesh is a team of agents that finds the group, and a person decides."
**0:15** Dashboard → Investigations. Run the agents on the 7-item beneficiary. "Four specialists work in parallel, three reviewers check every claim against the records."
**0:35** Open the HOLD. "Verified facts, figures, inferences and assumptions are kept apart. The decision comes from rules over verified links: fan-in plus shared devices and IPs, not from model text." Show the rejected-claims and limits panels.
**0:55** Open the 6-item case with no session data. "Device and IP are missing, so those checks say UNKNOWN, never clear. It cannot be released."
**1:10** Approvals tab. "The agent only proposes. Analysts cannot approve." Approve one as a reviewer. "Demo mode labels it SIMULATED. Against the sandbox the service re-checks the evidence, cancels once, and stores what PayPal reports back." Click approve on the same item again → 409.
**1:30** Evaluation tab. "72 held-out synthetic cases: 60 right, no ring released, no wrong hold. It also misses two scenario types, and we list them." 
**1:45** Audit tab (admin) and `docs/architecture.md`. "Every decision, refusal and execution is logged."
**1:55** "RiskMesh: an agent team that acts, with a reviewer in control."
