"""Generates postman/riskmesh.postman_collection.json from the routes in docs/api.md. Run: python postman/build_collection.py"""
import json, pathlib

def req(name, method, path, role, body=None, desc="", query=None):
    r = {"method": method,
         "header": [{"key": "Content-Type", "value": "application/json"}],
         "url": {"raw": "{{base_url}}" + path, "host": ["{{base_url}}"], "path": [p for p in path.strip("/").split("/") if p]},
         "description": f"Role: {role}. {desc}".strip()}
    if role != "public":
        r["auth"] = {"type": "bearer", "bearer": [{"key": "token", "value": "{{api_token}}", "type": "string"}]}
    else:
        r["auth"] = {"type": "noauth"}
    if query:
        r["url"]["query"] = [{"key": k, "value": v} for k, v in query.items()]
    if body is not None:
        r["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2), "options": {"raw": {"language": "json"}}}
    return {"name": name, "request": r}

folders = {
 "Health": [
  req("healthz", "GET", "/healthz", "public"),
  req("readyz", "GET", "/readyz", "public", desc="503 when DB or webhook verification is unusable."),
 ],
 "Session": [
  req("me", "GET", "/api/me", "viewer"),
  req("config (no secrets)", "GET", "/api/config", "viewer"),
 ],
 "Investigations": [
  req("list subjects", "GET", "/api/subjects", "viewer"),
  req("list investigations", "GET", "/api/investigations", "viewer"),
  req("investigate beneficiary", "POST", "/api/investigations", "analyst",
      {"subject_type": "beneficiary", "subject_id": "{{subject_id}}"}, "HOLD only creates PENDING requests for a human."),
  req("get investigation", "GET", "/api/investigations/{{investigation_id}}", "viewer"),
  req("agent trace", "GET", "/api/investigations/{{investigation_id}}/trace", "viewer"),
 ],
 "Actions (human approval)": [
  req("list pending actions", "GET", "/api/actions", "viewer", query={"status": "PENDING"}),
  req("approve action", "POST", "/api/actions/{{action_id}}/approve", "reviewer", {"reason": "Evidence reviewed"},
      "Executes the PayPal sandbox cancel after the evidence re-check. Reviewer role only."),
  req("reject action", "POST", "/api/actions/{{action_id}}/reject", "reviewer", {"reason": "False positive"}),
 ],
 "Evidence": [
  req("record session", "POST", "/api/sessions", "analyst", {"order_id": "{{order_id}}", "device": "dev-1", "ip": "203.0.113.5"}),
  req("link capture to item", "POST", "/api/links", "analyst", {"capture_id": "{{capture_id}}", "item_id": "{{item_id}}", "basis": "invoice_id match"}),
  req("events", "GET", "/api/events", "viewer"),
  req("evaluation results", "GET", "/api/eval", "viewer"),
  req("AI provider test", "POST", "/api/ai/test", "admin", desc="Checks the configured Gemini/OpenAI key; see README."),
 ],
 "Admin / sandbox": [
  req("audit log", "GET", "/api/audit", "admin"),
  req("PayPal health", "GET", "/api/paypal/health", "admin"),
  req("seed demo data", "POST", "/api/demo/seed", "admin", desc="Demo mode only."),
  req("sandbox: create order", "POST", "/api/sandbox/order", "admin",
      {"amount": "10.00", "currency": "USD", "invoice_id": "INV-1001", "custom_id": "buyer-1"}),
  req("sandbox: capture order", "POST", "/api/sandbox/capture/{{order_id}}", "admin",
      {"device": "dev-1", "ip": "203.0.113.5"}, "Optional ?mock=CODE sets the PayPal mock-response header.", query={"mock": ""}),
  req("sandbox: create payout", "POST", "/api/sandbox/payout", "admin",
      {"items": [{"receiver": "sb-recipient@example.com", "amount": "10.00", "sender_item_id": "INV-1001"}], "currency": "USD", "note": "RiskMesh sandbox"}),
  req("sandbox: sync payout", "POST", "/api/sandbox/payout/{{batch_id}}/sync", "admin"),
 ],
}

coll = {
 "info": {"name": "RiskMesh AI API", "description": "RiskMesh AI: multi-agent payout-ring detection with human approval. Set base_url and api_token (a token from RISKMESH_API_TOKENS).",
          "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
 "variable": [{"key": k, "value": v} for k, v in {
    "base_url": "http://localhost:8000", "api_token": "", "subject_id": "", "investigation_id": "",
    "action_id": "", "order_id": "", "capture_id": "", "item_id": "", "batch_id": ""}.items()],
 "item": [{"name": n, "item": items} for n, items in folders.items()],
}
out = pathlib.Path(__file__).with_name("riskmesh.postman_collection.json")
out.write_text(json.dumps(coll, indent=2))
print("wrote", out, sum(len(i) for i in folders.values()), "requests")
