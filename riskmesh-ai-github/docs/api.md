# API
All `/api/*` routes need `Authorization: Bearer <token>`. Errors are `{"error": "...", "request_id": "..."}` (never a stack trace).
| Method & path | Role | Notes |
|---|---|---|
| GET `/healthz`, `/readyz` | public | readyz: 503 when the DB or webhook verification is not usable |
| POST `/webhook` | PayPal signature | 200 accepted/duplicate · 400/422 malformed · 401 bad signature · 413 > 1 MB · 503 verification unavailable (PayPal retries) |
| GET `/api/me`, `/api/config` | viewer | config never contains secret values |
| GET `/api/subjects` | viewer | beneficiaries as hashed id + masked display |
| GET/POST `/api/investigations` | viewer / analyst | POST `{subject_type: beneficiary\|payout\|transaction\|merchant, subject_id}`; HOLD creates PENDING actions |
| GET `/api/investigations/{id}` · `/trace` | viewer | report, claims, actions · agent plan/tasks/rounds/messages |
| GET `/api/actions?status=` | viewer | |
| POST `/api/actions/{id}/approve` · `/reject` | reviewer | `{reason}`; 409 unless PENDING; approve executes (see README) |
| POST `/api/sessions` `{order_id, device, ip}` · `/api/links` `{capture_id, item_id, basis}` | analyst | evidence input; links only between records we hold |
| GET `/api/events` · `/api/eval` | viewer | |
| GET `/api/audit` · `/api/paypal/health` | admin | |
| POST `/api/demo/seed` | admin | demo mode only |
| POST `/api/sandbox/order` `{amount, currency, invoice_id, custom_id}` | admin | sandbox base URL only; returns `order_id` + buyer `approve` link |
| POST `/api/sandbox/capture/{id}?mock=CODE` `{device, ip}` | admin | captures an approved order, **ingests PayPal's response as evidence** (`source=paypal-api`), optionally records the session; `{device}` without `{ip}` → 422 before any capture; returns `invoice_ids`, `unknown`, `payer_ref` |
| POST `/api/sandbox/payout` `{items:[{receiver, amount, sender_item_id\|invoice_id}], sender_batch_id?, currency?, note?}` | admin | creates a sandbox payout batch (1–25 items, strict validation), reads it back, ingests it; `unclaimed` counts cancellable items. `sender_item_id` must equal the capture's `invoice_id` to be linked |
| POST `/api/sandbox/payout/{batch_id}/sync` | admin | re-reads the batch and merges the latest item statuses |
Rate limit: `RISKMESH_RATE_LIMIT_PER_MIN` per identity (per IP when unauthenticated), per process; 429 with `Retry-After`.
