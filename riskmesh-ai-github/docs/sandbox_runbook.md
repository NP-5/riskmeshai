# Sandbox runbook: from zero to a real HOLD on live PayPal-sandbox data

Reference: PayPal's [Getting started with the PayPal sandbox](https://github.com/paypaldev/getting-started-with-paypal-sandbox).
**Status: this path is tested against a fake PayPal only (`tests/integration/test_sandbox_flow.py`). It has not yet been run against the
real sandbox. Do the checklist below once and record what differs.**

## Why each step exists

| Your gap | What the project now does |
|---|---|
| Real sandbox data | `scripts/sandbox_seed_ring.py` (or the API below) creates, captures and pays out real sandbox objects. PayPal's own responses become evidence (`source = paypal-api`), so **no public webhook URL is needed**. |
| `invoice_id` on orders | The script gives every order a unique `invoice_id` (`RM-<run>-NN`); `POST /api/sandbox/order` accepts it. |
| Payout item ↔ payment link | A payout item is linked to a capture **only when `sender_item_id == capture.invoice_id`**. The payout endpoint accepts `invoice_id` as an alias for `sender_item_id` so the pairing is explicit. |
| No endpoint to create a payout batch | **`POST /api/sandbox/payout`** (admin, sandbox-only) wraps `PayPalClient.create_payout_batch`, validates strictly, reads the batch back and ingests it. `POST /api/sandbox/payout/{batch_id}/sync` refreshes statuses. |
| Unclaimed items | Pay a recipient email that has **no PayPal account**. The response reports `unclaimed`, and the script warns if any ring item is not UNCLAIMED, because PayPal's cancel call only works on unclaimed items. |
| No device/IP in webhooks | `POST /api/sandbox/capture/{order_id}` takes `{"device","ip"}` and records the session in the same call (or use `POST /api/sessions` later). Half a pair is refused **before** money moves. |
| 5+ payers, shared device/IP | The script plans 5 payers (3 share a device, all share an IP) and warns if two approvals were the same PayPal account (fan-in counts *distinct* payers). |

## One-time setup (developer.paypal.com, Sandbox toggle)
1. **Sandbox Accounts → Create Account**: you need **5 Personal (buyer)** accounts (the two defaults are not enough) and the default Business account. Note each buyer's email/password; they only work on `www.sandbox.paypal.com`.
2. **Apps & Credentials → Sandbox**: copy Client ID and Secret into `.env` (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`; keep `PAYPAL_BASE=https://api-m.sandbox.paypal.com`).
3. Make sure the sandbox app/Business account is allowed to use **Payouts** and has a balance (Sandbox Accounts → View/Edit). If PayPal answers `AUTHORIZATION_ERROR` or insufficient funds, fix it there.
4. Run RiskMesh with an admin token (token >= 16 chars). Keep demo mode **off** so approvals really call the sandbox instead of simulating:
   ```bash
   export PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=...
   RISKMESH_API_TOKENS="<token>:you:admin" uvicorn app:app      # http://127.0.0.1:8000
   ```
   Without `PAYPAL_WEBHOOK_ID` the server reports not-ready on `/readyz` outside demo mode; that does not affect the sandbox endpoints used here.

## Run it
```bash
python scripts/sandbox_seed_ring.py --api http://localhost:8000 --token <admin token> --control
```
For each of the 6 orders the script prints an approve link. Open it in a **private window**, sign in as a **different** Personal buyer, approve, and press Enter. Expected end state:
`unclaimed: 6/6` and `INVESTIGATION ... recommendation=HOLD ring=VERIFIED_INDICATORS proposed_actions=5`. The control buyer is not in the ring. Nothing is cancelled until a **reviewer** approves each action (`/api/actions`).

Useful variants: `--no-sessions` (proves live data without device/IP stays at REVIEW, never HOLD), `--payers 4` (too few payers: no ring), `--recipient you@yourrealpaypal.test` (an account that exists: items go SUCCESS and cannot be held).

## Negative testing (from the sandbox guide)
`POST /api/sandbox/capture/{order}?mock=DUPLICATE_INVOICE_ID` (or `INSTRUMENT_DECLINED`) sends the `PayPal-Mock-Response` header. A refused capture creates **no evidence** and is reported as 502 with PayPal's `debug_id`. The header is refused on any non-sandbox base URL.

## Optional: webhooks
Webhooks are not required for this path. If you expose `/webhook` (Render URL or a tunnel), register it in the dashboard and set `PAYPAL_WEBHOOK_ID`; signed events then merge into the same rows (known facts are never overwritten by UNKNOWN).

## What to record after your first real run (and tell us if reality differs)
- [ ] capture response contained `payer.payer_id` and `captures[].invoice_id` (else the capture reply lists them under `unknown` and the order is read back)
- [ ] payout to a no-account email returned `UNCLAIMED` (else the hold cannot be exercised; try another address)
- [ ] approving one HOLD action cancelled the item and the read-back showed `CANCELED`
- [ ] anything PayPal returned that these parsers marked `UNKNOWN`
