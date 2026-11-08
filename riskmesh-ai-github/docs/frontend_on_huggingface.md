# Frontend on Hugging Face, backend on Render

The frontend is the original RiskMesh AI design (Tailwind admin style, splash screen, ring gauges, entity graph, chat assistant) from https://github.com/NP-5/riskmesh-ai, connected to the PayPal RiskMesh backend through `web/legacy/adapter.js`.

- Without a backend it shows a read-only snapshot of real engine output over synthetic demo data (approve/reject only change the page).
- With a backend URL and token (bottom-left "Static demo" pill -> Connect) every screen uses live data and approvals run on the backend.

## Set up
1. Deploy the backend on Render from `render.yaml`. Environment: `RISKMESH_API_TOKENS`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `GEMINI_API_KEY`, and `RISKMESH_CORS_ORIGINS=https://nareshpavan-riskmesh-ai.hf.space` (already in `render.yaml`).
2. Upload this repo's `index.html` to the Space (Files -> Add file -> Upload; replace the old one).
3. Open https://nareshpavan-riskmesh-ai.hf.space, click the pill, paste the Render URL and a token, press Connect. The pill changes to "Live API - <role>".
4. Optional: share `https://nareshpavan-riskmesh-ai.hf.space/?api=https://<your-app>.onrender.com` so visitors only type a token.

## Roles in the UI
- viewer: dashboard, investigations, evidence, graph
- analyst: also "Investigate" a recipient
- reviewer/admin: Approve / Reject actions; admin also sees the audit trail
For judges, add a view-only token such as `judgetoken-123456:judge:viewer` to `RISKMESH_API_TOKENS`. Never put an admin token in the page or the URL.

## How the old screens map to the new data
| Old screen | Now shows |
|---|---|
| Case | Investigation (`INV-...`) |
| Structural risk / 6-hour escalation | Risk score / indicators fired |
| Entity graph | Captures, payout items, disputes, shared device/IP -> recipient |
| Evidence & rules | Rule results and verified figures, each tied to a claim ID |
| Critic review | The three reviewers' verdict and rejected claims |
| Intervention simulator | Proposed actions (HOLD_PAYOUT_ITEM) with per-action Approve/Reject |
| Audit trail | `/api/audit` rows for the investigation (admin token) |
| Held-out evaluation | `/api/eval` results |

Rebuild after changing the UI: `python web/build_ui.py && python build_demo.py`.

## Notes
- The token stays in the browser tab (sessionStorage) and is never written to the page.
- CORS is an allow-list; origins not listed get no CORS headers, and `*` is ignored.
- Render's free plan sleeps when idle; the first request can take about a minute.
