# Devpost submission text

**Name:** RiskMesh AI
**Tagline:** A team of agents that finds payout rings before the money leaves, with a human in control.
**Category:** AI Agents or Automation

## Inspiration
Fraud rings rarely look suspicious one payment at a time. Ten small payments from ten accounts look normal; all ten funding one beneficiary through the same devices does not. Review teams see alerts one by one and connect them too late.

## What it does
RiskMesh AI ingests PayPal webhook events (signature-verified, de-duplicated) into typed records, then runs a multi-agent investigation: a lead orchestrator plans the work, transaction, network, payout and dispute specialists analyse their slice in parallel, and three independent reviewers (evidence auditor, numeric verifier, consistency reviewer) check every claim against the records, with at most two revision rounds. Claims are kept as facts, figures, inferences, assumptions or UNKNOWN. The report is built from verified claims only and the decision (HOLD / REVIEW / RELEASE) comes from deterministic rules, so text in a memo or a model draft cannot talk it into releasing money. Missing data (PayPal webhooks carry no device or IP) is UNKNOWN, never clear. A HOLD only creates a request: a reviewer approves, the service re-checks the evidence, cancels the unclaimed Payouts item once, reads it back from PayPal and records the result; refusals keep PayPal's debug ID and timeouts are marked UNCERTAIN. An MCP server exposes investigation and read tools, deliberately without any approve tool.

## How we built it
Python standard library for the engine (Decimal money, SQLite/PostgreSQL layer with atomic state transitions, a small ASGI API with roles, rate limiting and an audit log), an in-process JSON message bus for the agents, a static dashboard, and an evaluation harness with separate calibration and held-out sets. 88 tests cover webhook verification, concurrency (6 simultaneous approvals execute one hold), failure paths and prompt-injection cases against a fake PayPal transport.

## Challenges
Making failure safe: a crashed or hung agent turns its domain into UNKNOWN instead of approving; a stale decision cannot execute because evidence is rebuilt before the PayPal call; the cancel call has no idempotency key, so it is never retried and its outcome is verified by reading the item back. Tests also caught real bugs along the way (scientific-notation amounts accepted, an idempotency-key collision on re-proposal).

## Accomplishments
On 72 held-out synthetic cases RiskMesh made 60 correct decisions (naive amount rule 15, naive fan-in rule 24), held all 12 rings, and made no wrong HOLD and no dangerous RELEASE. It is not perfect: it over-flags busy legitimate sellers and under-flags a lone payment burst, and the README lists both. Synthetic data, same-author ground truth.

## What we learned
Agents earn trust when every step is checkable and reversible: separate verifiers, explicit UNKNOWN, and a human on the one action that moves money.

## What's next
Run the full sandbox loop with real credentials, join real session data for device/IP, live dispute signals from the PayPal Agent Toolkit, SSO instead of static tokens, graph search over large event volumes.

## AI tools used and how
Google Gemini API (`generateContent`; model: fill in the exact one you used, default `gemini-2.5-flash`). (1) Each specialist agent may ask Gemini for up to three inferences about its slice of the data; three reviewer agents verify them against the records and reject what cannot be backed. (2) Gemini writes a short plain-English case brief from verified claims only; code checks every sentence's citations and numbers before it is shown. The model never decides: HOLD / REVIEW / RELEASE, scores and payout actions come from rules and a human approval. Device/IP/payer identifiers are tokenised before leaving the process.

## PayPal technologies used
Orders and Captures, Payouts (create batch, read, cancel an unclaimed item), Webhooks with signature verification, and the sandbox mock-response header for failure testing.

## Built with
Python, ASGI, SQLite/PostgreSQL, MCP, PayPal REST API (Orders, Payouts, Webhooks), PayPal sandbox, Render, JavaScript

## Links to add
Repo: https://github.com/NP-5/riskmesh-ai · Demo: https://huggingface.co/spaces/nareshpavan/riskmesh-ai (static snapshot) · Video: (paste after recording)

## Checklist before you submit
- [ ] Run `scripts/sandbox_seed_ring.py` against the real PayPal sandbox (see docs/sandbox_runbook.md; nothing here has called PayPal yet) and record the result honestly
- [ ] The reliability layer's claim-ledger / evaluation pattern is adapted from your ClaimCheck project: confirm it is your own work and disclose it if the rules ask for pre-existing code
- [ ] `python eval/generate_cases.py && python eval/calibrate.py && python eval/run_eval.py && python build_demo.py`, then upload `index.html` to the Space
- [ ] Repo public, README renders, no `.env`, `*.db` or secrets committed
- [ ] Render service live with `RISKMESH_API_TOKENS` set; webhook URL registered in the sandbox
- [ ] Demo video recorded and public
- [ ] Devpost track and required fields match the hackathon page (not read in full)
