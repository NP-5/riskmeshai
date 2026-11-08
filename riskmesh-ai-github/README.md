# RiskMesh AI
**Track: AI Agents or Automation.** A team of specialist agents investigates PayPal payout activity for fraud rings (many payers funding one beneficiary through shared devices and IPs). Reviewer agents check every claim against the records. A person approves before the one write action (cancelling an unclaimed Payouts item). **Plain code makes the decision; a model, if enabled, can only propose inferences that the reviewers must verify.**

## What is in the box
| Part | Where |
|---|---|
| Webhook ingestion: validate → PayPal signature verification (fails closed) → de-duplicate → persist | `riskmesh/ingest.py` |
| Typed evidence, `Decimal` money, strict ID/currency validation, `UNKNOWN` for anything missing | `riskmesh/financial.py`, `bundles.py` |
| 17 deterministic fraud rules in 4 domains with thresholds | `riskmesh/rules.py`, `metrics.py`, `thresholds.py` |
| Multi-agent runtime: lead orchestrator, 4 specialists, 3 reviewers, synthesizer, JSON message bus, timeouts, retries, ≤2 revision rounds, full trace | `riskmesh/agents/` |
| Claim ledger (FACT / FIGURE / INFERENCE / ASSUMPTION / UNKNOWN) with status PROPOSED → VERIFIED / REJECTED | `riskmesh/ledger.py`, `verify.py` |
| Investigations, approval workflow, idempotent hold execution, audit log | `riskmesh/service.py`, `db.py` |
| HTTP API (dependency-free ASGI), tokens + roles, rate limit, body limits, security headers | `app.py` |
| Dashboard (static page; live mode with an API token, read-only snapshot otherwise) | `web/index.html`, `index.html` |
| MCP server (investigate and read only; **no approve tool**) | `riskmesh/mcp_server.py` |
| Synthetic evaluation, calibration on a separate set, held-out test | `eval/` |
| 88 tests | `tests/` |

## For judges: run it in 2 minutes (no PayPal or Google account needed)
```bash
git clone https://github.com/NP-5/riskmesh-ai && cd riskmesh-ai
python scripts/quickstart.py            # Python 3.10+, standard library only
python tests/run_tests.py               # 88 tests, no network
```
`quickstart.py` seeds synthetic data, runs the agent team on three beneficiaries (HOLD / REVIEW / RELEASE), and approves one hold as a reviewer (labelled SIMULATED, PayPal is not called).
To see the **AI** part, add a Gemini key: `GEMINI_API_KEY=<key> python scripts/quickstart.py`.
To see the **PayPal** part against the real sandbox, follow `docs/sandbox_runbook.md` (needs sandbox credentials). A hosted URL, if provided, is listed in the Devpost submission.

## Where AI (Google Gemini) is used, and where it is deliberately not
Get a key in Google AI Studio, then `export GEMINI_API_KEY=<your key>` (that alone selects Gemini; `RISKMESH_LLM_PROVIDER=gemini` does the same explicitly). The model defaults to `gemini-2.5-flash` (the example model in Google's own docs); set `RISKMESH_LLM_MODEL` to use another one your key can access. `POST /api/ai/test` (admin) makes one tiny call to confirm the key and model work. The key is sent only in the `x-goog-api-key` header, never in a URL. (OpenAI and Anthropic providers also exist as options; Gemini is the one this submission uses.)
1. **Specialist inferences.** Each of the four specialists may ask Gemini for up to 3 *inferences* about its slice of the data. They become INFERENCE claims that the three reviewer agents must verify (cited evidence has to resolve to real records); unverifiable ones are REJECTED and shown as such. Device, IP and payer identifiers are replaced by random-salted tokens before text leaves the process, so the model sees "these two payments share one device" but never the real values.
2. **AI case brief.** Gemini writes 3 to 7 plain-English sentences for the reviewer from the VERIFIED claims only. Code then checks each sentence: every cited claim id must exist and be verified, every number must appear in the cited claims, and instruction-like text, links and addresses are dropped. If fewer than 3 sentences survive, no brief is shown and the report says why.
3. **Not the model:** the decision (HOLD / REVIEW / RELEASE), the risk score, the hold requests and the pre-execution re-check are computed by rules and never use model output. With no key everything still works; the AI parts are simply absent and the report says "no AI model configured".

**What is verified about this AI path:** the Gemini request format (checked against Google's published REST examples), key-in-header, thinking-part and blocked-answer handling, key redaction, pseudonymisation, verification of inferences and briefs, and that a lying or failing model cannot change a decision (tests use a fake Gemini endpoint). **What is not:** no real Gemini call has been made from this repository, so output quality, quota behaviour and the exact behaviour of your chosen model are unmeasured. `eval/` runs without a model and does not measure the AI parts.

## What existed before, and what is new for this hackathon
*Fill in the dates for your Devpost form: this repository's history shows only my part.* Before: an earlier RiskMesh prototype (single FastAPI file, in-memory events, seven deterministic checks, no authentication, a static demo page) and the claim-label / evaluation pattern from the author's ClaimCheck project. New: the signature-verifying webhook ingest and database, evidence bundles with verified links, 17 rules, the multi-agent runtime with independent reviewers, the approval workflow with idempotent PayPal hold execution and read-back, roles/audit/rate limiting, the PayPal sandbox data path (orders, captures, payout batches), the Gemini-backed inferences and brief, a held-out evaluation, and the test suite.

## Tools used
- **PayPal** (sandbox): Orders v2 and Captures (create/capture/read), Payouts (create batch, read item, cancel unclaimed item), Webhooks (verify-webhook-signature, events), `PayPal-Mock-Response` for negative tests.
- **Google Gemini API** (`generateContent`, model chosen by `RISKMESH_LLM_MODEL`, default `gemini-2.5-flash`): specialist inferences and the reviewer brief, as described above. *Write the exact model name you used in your Devpost "tools" field.*
- Python standard library (ASGI app, SQLite, `Decimal`), `uvicorn` to serve, PostgreSQL on Render for deployment, optional MCP SDK.

## Quick start (local server)
```bash
pip install -r requirements-dev.txt          # pytest; the app itself needs only an ASGI server: pip install "uvicorn[standard]"
python tests/run_tests.py                    # or: pytest   (88 tests, ~3 s, no network)
RISKMESH_DEMO_MODE=1 RISKMESH_API_TOKENS="$(python -c 'import secrets;print(secrets.token_urlsafe(24))'):me:admin" uvicorn app:app
# open http://127.0.0.1:8000, paste the token, then:  POST /api/demo/seed  →  Investigate → Approve a hold (SIMULATED, labelled as such)
```
`RISKMESH_DEMO_MODE=1` accepts **unsigned** webhooks and **simulates** holds. It is refused when `RISKMESH_ENV=production`. See `.env.example`.

## How a case flows
1. Events arrive at `/webhook`. In production, an event is stored only if PayPal's verify-webhook-signature API says `SUCCESS`; if verification is unavailable the endpoint answers 503 so PayPal retries. Repeated events are ignored by a unique key.
2. An analyst starts an investigation of a beneficiary (hashed recipient), payout batch/item, transaction or merchant. The orchestrator plans the work, skips specialists whose data does not exist, and runs transaction, network, payout and dispute specialists concurrently.
3. Each specialist emits claims with evidence refs. The evidence auditor, numeric verifier and consistency reviewer check them against freshly loaded records. Objections trigger at most two revision rounds; unresolved claims are rejected, and claims resting on rejected claims are rejected too. A crashed or hung agent makes its domain `UNKNOWN`; it can never silently approve.
4. The synthesizer builds the report from VERIFIED claims only, keeping facts, figures, inferences, assumptions and UNKNOWN apart, with confidence limits and missing data. The decision (HOLD / REVIEW / RELEASE) is computed from verified rule results. HOLD needs a verified fan-in **and** shared device/IP/cluster evidence over verified capture→payout links.
5. HOLD creates PENDING requests per cancellable payout item. A reviewer approves or rejects. On approval the service re-builds the evidence and re-runs the investigation; it refuses if the decision is no longer HOLD or the item is no longer cancellable. It calls PayPal's cancel **once** (never retried), re-reads the item from PayPal, and stores that read-back as verification. A PayPal refusal is stored with its `debug_id`; a timeout is `UNCERTAIN`, which blocks a blind retry.

## Evaluation (held-out, synthetic: read this honestly)
`python eval/generate_cases.py && python eval/calibrate.py && python eval/run_eval.py`. 72 test cases from 12 scenario types, ground truth fixed by scenario design; thresholds were searched on a **separate** 48-case calibration set under the constraint "no ring released, no wrong HOLD".

| Measure | Naive amount rule | Naive fan-in rule | RiskMesh |
|---|---|---|---|
| Correct decision (HOLD/REVIEW/RELEASE) | 15/72 | 24/72 | **60/72** |
| Rings correctly held | 0/12 | 12/12 | 12/12 |
| Wrong HOLDs (frozen without a ring) | 9 | 33 | 0 |
| Dangerous RELEASEs of a real ring | 12 | 0 | 0 |
| Clean cases wrongly flagged | 9/24 | 12/24 | 6/24 |

RiskMesh is **not** perfect here, and the errors are structural, listed in `eval/results.md`: (a) busy but legitimate sellers (6–9 unrelated payers in a day) get REVIEW (6/6 popular-seller cases), a deliberate hard negative where the fan-in rule is conservative; (b) a payment burst on a merchant with nothing else wrong scores 20, below the REVIEW threshold of 25, so it is RELEASEd (6/6 burst cases). Calibration did not find a safe setting that fixes either without breaking the "no wrong HOLD" constraint. Caveats: synthetic data, same author wrote scenarios and rules, naive rules are straw men that cannot answer REVIEW, no LLM is in the loop (so the injected-memo result only shows that no code path reads free text). None of this says anything about real fraud rates or about PayPal's own risk systems.

## Security model (details: `docs/security.md`)
Bearer tokens with roles `viewer < analyst < reviewer < admin`; `/webhook` is authenticated by PayPal's signature, `/healthz` `/readyz` leak nothing. Production refuses to start without tokens, a ≥16-char pepper, a non-memory database and with demo mode on. Recipient identifiers are peppered-hashed and masked; logs and the audit trail are redacted. Rate limit is per identity/IP **per process**. Dashboard shows configuration without secret values.

## Limits and what has NOT been tested
- **PayPal sandbox was never called.** The client, retry rules, verification, the new capture/payout ingestion and the cancel/read-back flow are tested against a fake PayPal only (73 tests). The path to real sandbox data is built (`docs/sandbox_runbook.md`); the first real run is still to do, and PayPal's actual response shapes are assumed from its docs.
- The app was served by a real **uvicorn** once, against a fake PayPal, to run `scripts/sandbox_seed_ring.py` over real HTTP; everything else is exercised in-process through the ASGI interface. **PostgreSQL** path is written but untested. **MCP SDK** not installed here: tool bodies are unit-tested, the MCP wiring is not. **Dashboard** passes a JS syntax check and its snapshot data shape is checked, but it was not opened in a browser.
- **Gemini / OpenAI / Anthropic** HTTP calls were tested only against fake endpoints; no real model call has been made (see the AI section).
- PayPal webhooks carry no device or IP and no payer on captures. Device/IP must come from your own session data (`POST /api/sessions`); payer/merchant are fetched from the Orders API when credentials exist. Until then those checks are `UNKNOWN`, never "clear".
- Thresholds are starting points tuned on synthetic data. A FAIL is an indicator, never confirmed fraud.
- The payout hold needs an UNCLAIMED Payouts item (pay an email with no PayPal account). `POST /api/sandbox/payout` creates one; whether the real sandbox returns UNCLAIMED for your chosen address is part of the first real run.
- `agent_toolkit.py` (read-only PayPal Agent Toolkit helper) is carried over unchanged and untested; it is not wired into the API.

## Sandbox setup
1. Developer Dashboard → Sandbox: note the Business (seller) and Personal (buyer) accounts. Apps & Credentials → copy Client ID/Secret into `.env`.
2. (Optional) add a webhook to `https://<host>/webhook`; put its ID in `PAYPAL_WEBHOOK_ID`. Not needed for the sandbox ring path above.
3. `python scripts/sandbox_smoke_test.py` checks one order → approve → capture. To build a whole **ring on real sandbox data** (5+ buyers, shared device/IP, unclaimed payouts, HOLD) run `python scripts/sandbox_seed_ring.py --api <url> --token <admin>`; full steps and the checklist are in `docs/sandbox_runbook.md`. The same steps are API calls: `POST /api/sandbox/order`, `/capture/{id}` (body `{device, ip}`), `/payout`, `/payout/{id}/sync` (admin, sandbox only). Webhooks are optional on this path.
4. Deploy: `render.yaml` (Render web service + Postgres). Set `RISKMESH_API_TOKENS`; the pepper is generated.

`pip install "mcp>=1.2,<2"` then `python -m riskmesh.mcp_server`; `mcp.config.example.json` shows PayPal's own `@paypal/mcp` for a sandbox client. The hold is a direct Payouts REST call because PayPal's MCP documents no payout-cancel tool.

## Built with reference to
[PayPal developer docs](https://developer.paypal.com/) · [Webhooks](https://developer.paypal.com/api/webhooks/overview) · [Payouts](https://developer.paypal.com/docs/payouts/) · [Disputes](https://developer.paypal.com/platforms/disputes/overview) · [Sandbox guide](https://github.com/paypaldev/getting-started-with-paypal-sandbox) · [Agent Toolkit](https://docs.paypal.ai/developer/tools/ai/agent-toolkit-quickstart)
