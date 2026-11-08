# Architecture
```
PayPal ──webhook──▶ /webhook ─▶ ingest: validate → verify signature → dedupe → persist (orders, captures, disputes, payout batches/items)
merchant sessions ─▶ /api/sessions (device, ip per order)        analyst links ─▶ /api/links
                                   │
                         bundles.build_bundle  ──▶  evidence bundle (typed records + verified capture→payout links; missing = "UNKNOWN")
                                   │
   Orchestrator (lead) ── plan ──▶ T1 transaction ─┐
                                   T2 network     ─┤ concurrent, JSON messages on an in-process bus (task id, agent id, timeout, retry)
                                   T3 payout      ─┤      each emits claims {type, text, evidence refs, rule id, assertion}
                                   T4 dispute     ─┘
                          review ◀ evidence-auditor · numeric-verifier · consistency-reviewer   (fresh bundle each round)
                          objections ─▶ author revises (max 2 rounds) ─▶ else REJECTED; dependants of rejected claims rejected
                                   │
                          synthesizer (VERIFIED claims only) ─▶ report + decision (rules.assess)  ── never model text
                                   │
                investigations table ─▶ HOLD ⇒ PENDING hold requests ─▶ reviewer approve ─▶ re-check ─▶ cancel once ─▶ read back ─▶ verification
```
**Agent roles.** Specialists differ in data slice and prompt (the network agent never sees amounts it does not need; the dispute agent sees only disputes). Reviewers are independent code paths: the numeric verifier recomputes figures with its own implementation, the auditor resolves every evidence ref against the records, the consistency reviewer looks for contradictions between agents.
**Failure behaviour.** Agent error/timeout → one retry → its domain becomes a VERIFIED UNKNOWN claim and is excluded from the score; a failed reviewer flags every claim, so it cannot approve by failing. The synthesizer failing falls back to the orchestrator's own synthesis.
**State machine for an action.** `PENDING → APPROVED → EXECUTING → EXECUTED | FAILED | UNCERTAIN | SIMULATED`, or `PENDING → REJECTED`. Every transition is a conditional UPDATE; one open action per payout item is enforced by a partial unique index.
**Where a model may appear.** Only `llm_inferences` in each specialist: ≤3 INFERENCE claims, treated as untrusted, cited evidence must resolve, and they cannot alter the rule-based score or decision.
