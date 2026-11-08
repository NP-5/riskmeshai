# Status after the completion pass
Verified here: `python tests/run_tests.py` → 58 passed (no network, no third-party packages). `python eval/run_eval.py` → results in `eval/results.md`.

## Completed in this pass
API (`app.py`, dependency-free ASGI), auth + roles, rate limiting, approval/action service with idempotent, evidence-rechecked execution and PayPal read-back (`service.py`), new demo data, MCP server (no approve tool), evaluation dataset + calibration + 3-way comparison, 58 tests, dashboard, `render.yaml`, requirements, `.env.example`, README/SUBMISSION/DEMO_SCRIPT/docs. Old FastAPI app and ClaimCheck-era modules (`checks.py`, `pipeline.py`, `store.py`, `models.py`, `cases.py`) removed.

## Bugs found and fixed by the tests
Amounts like `1e3` were accepted; re-proposing a hold after a rejection hit a UNIQUE idempotency key (500); audit rows for refused approvals were rolled back with the transaction.

## Not done / not verified (rework candidates)
1. PayPal sandbox never called (no credentials/network). 2. No real uvicorn run; PostgreSQL untested; MCP SDK wiring untested; dashboard never opened in a browser; real LLM providers untested.
3. Known evaluation misses: popular legitimate sellers → REVIEW; lone velocity burst scores 20 < review threshold 25 → RELEASE. Consider changing rule weights rather than only thresholds.
4. `payout_concentration` is tautological when a beneficiary receives the whole batch; a batch-level view would be better.
5. `agent_toolkit.py` not integrated. No token expiry/rotation; rate limiter is per process.

## Sandbox data path pass
Added `riskmesh/sandbox_flow.py`, `POST /api/sandbox/payout` (+ `/sync`), capture-time ingestion and session recording, `payer_ref`, `scripts/sandbox_seed_ring.py`, `docs/sandbox_runbook.md`, `tests/integration/test_sandbox_flow.py`. `python tests/run_tests.py` → 73 passed (58 existing + 15 new). The seed script was also run over real HTTP under uvicorn against a fake PayPal: 5 buyers + 1 control → ring recipient HOLD / VERIFIED_INDICATORS, control not in the ring.
**Still not verified:** any call to the real PayPal sandbox; PayPal's real response shapes (capture `payer`/`invoice_id`, payout-batch `items[]`, UNCLAIMED for a no-account email); Payouts permission on the sandbox app; dashboard in a browser; PostgreSQL; MCP wiring.

## OpenAI + hackathon-requirements pass
Added: OpenAI-first provider hardening (JSON mode on the official endpoint, temperature retry, safe errors), per-investigation provider usage, identifier pseudonymisation before model calls, `riskmesh/brief.py` (AI case brief with code-checked sentences), `POST /api/ai/test`, dashboard AI panel, `scripts/quickstart.py` (judge 2-minute run), README sections for judges / AI / tools / before-vs-new, `.env.example`. The pre-execution re-check is deterministic only. `python tests/run_tests.py` -> 82 passed.
**Not verified:** any real OpenAI call (key and model unavailable here); real PayPal sandbox; hosted deployment; browser; video; the before/after dates for the Devpost form.

## Gemini switch
Gemini (`generateContent`, key in `x-goog-api-key`) is now the AI provider for this submission: `GeminiGenerate` in `riskmesh/llm.py`, `GEMINI_API_KEY` auto-selects it, default model `gemini-2.5-flash`, tests in `tests/integration/test_ai_gemini.py` (fake endpoint). OpenAI/Anthropic remain as options. `python tests/run_tests.py` -> 88 passed. **Not verified:** any real Gemini call; the default model name for your key/quota.
