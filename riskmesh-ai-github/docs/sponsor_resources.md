# PayPal AI Hackathon: sponsor resources and how they fit RiskMesh AI

Submission deadline per the Devpost page: **Nov 12, 2026, 12:00 pm PST**. Use only what you actually run; say so honestly in the Devpost text.

| Resource | What it offers | Fit | Status here |
|---|---|---|---|
| PayPal REST API (Orders, Payouts, Webhooks) | Core integration | Essential | Built; never run against the real sandbox yet |
| PayPal Agent Toolkit (`paypal-agent-toolkit`) | Read tools for disputes/orders for agent frameworks (OpenAI Agents SDK, LangChain, CrewAI, MCP, etc.) | High: gives the dispute specialist live dispute data | `agent_toolkit.py` exists, not wired in or tested |
| PayPal MCP server (`@paypal/mcp`) | Natural-language PayPal tools for MCP clients | Medium: `mcp.config.example.json` already lists it; RiskMesh's own MCP server deliberately has no approve tool | Config only |
| PayPal AI Toolkit plugin (Claude Code/Codex/Cursor) | `/paypal:doctor`, `/paypal:sandbox`, `/paypal:test-accounts`, `/paypal:explain-error`; sandbox MCP | High for the remaining work: sandbox setup and webhook debugging | Use during development |
| Getting Started with PayPal sandbox (GitHub) | Sandbox account setup | Required for the sandbox run | Follow `docs/sandbox_runbook.md` |
| Render | $50 credits; Render Workflows for coordinating agents and background jobs | High: `render.yaml` already deploys the API and Postgres | Deploy and register the webhook URL |
| Postman | API collection tooling, PayPal collection | Medium: judges can try the API in minutes | `postman/riskmesh.postman_collection.json` added |
| Elastic | Elasticsearch, vector search, Agent Builder, MCP | Low now; fits "graph/search over large event volumes" as future work | Not used |
| Zapier MCP | Actions across 9,000+ apps including PayPal | Low: could notify a reviewer when a HOLD is proposed | Not used |
| AG Grid (AG Studio) | Embedded dashboards, agent framework; "Best Use of AG Grid" prize | Medium but costly: needs a React dashboard rebuild; a fraud-alert dashboard is one of their suggested ideas | Not used |
| Bryntum | Gantt/Scheduler/Calendar; "Best Use of Bryntum" prize; suggests a dispute triage board | Medium but costly: needs a new UI | Not used |
| APIMatic | Context Plugin that gives coding agents PayPal API context; 1 month Basic free if used | Low cost: use it while finishing the PayPal calls, then say so | Not used |
| KERNEL | Cloud browsers for agents | Low | Not used |
| Channel3 | Product-search API (must be called to qualify for its prize) | None | Not used |
| Astropods | Agent runtime and deployment | Low: Render already covers hosting | Not used |

## Suggested order before the deadline
1. Run the real sandbox loop (`scripts/sandbox_seed_ring.py`, see `docs/sandbox_runbook.md`) and record the real results; fix any response-shape surprises.
2. Deploy to Render, set `RISKMESH_API_TOKENS` and the PayPal variables, register the webhook URL.
3. Wire `agent_toolkit.py` into the dispute specialist as an optional, read-only source (UNKNOWN when unavailable) and test it with the sandbox.
4. Record the demo video, fill the Devpost fields, and tick the checklist in `SUBMISSION.md`.
