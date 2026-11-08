"""RiskMesh AI HTTP API: a small dependency-free ASGI application (run it with ``uvicorn app:app``).

Why no framework: the whole surface is ~25 routes, and keeping it to the standard library makes the security-relevant
code (auth, roles, rate limit, body limits, error handling) short enough to read in one sitting and testable without
installing anything. Only an ASGI server (uvicorn) is needed to serve it.

Auth: ``Authorization: Bearer <token>``; tokens come from RISKMESH_API_TOKENS (token:identity:role). /webhook is
authenticated by PayPal's signature instead. /healthz and /readyz are public and reveal no configuration.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import logging
import re
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from riskmesh import config as cfg
from riskmesh import sandbox_flow
from riskmesh.financial import ValidationError, hash_identifier, is_unknown, valid_id
from riskmesh.db import Database
from riskmesh.ingest import WebhookRejected, enrich_capture, process_webhook
from riskmesh.llm import get_provider
from riskmesh.paypal import PayPalClient, PayPalError, PayPalNotConfigured, PayPalUnavailable
from riskmesh.service import ROLE_RANK, Service, ServiceError, need

log = logging.getLogger("riskmesh")
ROOT = Path(__file__).resolve().parent
MAX_JSON = 64_000
def cors_headers(origin: str) -> list:
    """Allow-list CORS for a frontend hosted elsewhere (e.g. a Hugging Face Space). RISKMESH_CORS_ORIGINS = comma-separated exact origins.
    Auth is a bearer token in a header (no cookies), and no origin is allowed unless listed. '*' is not honoured."""
    allowed = {o.strip().rstrip("/") for o in os.environ.get("RISKMESH_CORS_ORIGINS", "").split(",") if o.strip() and o.strip() != "*"}
    if not origin or origin.rstrip("/") not in allowed:
        return []
    return [(b"access-control-allow-origin", origin.encode("latin1")), (b"vary", b"Origin"),
            (b"access-control-allow-methods", b"GET, POST, OPTIONS"), (b"access-control-allow-headers", b"Authorization, Content-Type"),
            (b"access-control-max-age", b"600")]


SECURITY_HEADERS = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer"), (b"cache-control", b"no-store"),
                    (b"content-security-policy", b"default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")]


class HTTPError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


class RateLimiter:
    """Sliding one-minute window per key. In-memory: per process, so behind several workers the limit is per worker."""

    def __init__(self, per_min: int, clock=time.monotonic):
        self.per_min, self.clock, self.hits = per_min, clock, defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, q = self.clock(), self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_min:
            return False
        q.append(now)
        return True


class App:
    def __init__(self, settings: Optional[cfg.Settings] = None, db: Optional[Database] = None, client: Optional[PayPalClient] = None, provider=None):
        self.s = settings or cfg.load_settings()
        errors, self.warnings = cfg.validate(self.s)
        if errors:
            raise RuntimeError("invalid configuration: " + "; ".join(errors))
        self.db = db or Database(self.s.db_url)
        self.client = client or PayPalClient(self.s)
        self.provider = provider          # None => a fresh provider from settings per investigation
        self.svc = Service(self.s, self.db, self.client, self.provider)
        self.limiter = RateLimiter(self.s.rate_limit_per_min)
        self.routes = self._routes()
        for w in self.warnings:
            log.warning("config: %s", w)
        if self.s.generated_token:
            log.warning("development admin token (not persisted): %s", self.s.generated_token)

    # ---- routing ----------------------------------------------------------------------------------------------------
    def _routes(self):
        R = []

        def add(method, pattern, role, fn):
            R.append((method, re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"), role, fn))

        add("GET", "/healthz", None, lambda r: {"status": "ok"})
        add("GET", "/readyz", None, self.readyz)
        add("POST", "/webhook", None, self.webhook)
        add("GET", "/api/me", "viewer", lambda r: {"identity": r["who"].identity, "role": r["who"].role})
        add("GET", "/api/config", "viewer", lambda r: self.s.public_view())
        add("GET", "/api/subjects", "viewer", lambda r: {"subjects": self.svc.subjects()})
        add("GET", "/api/investigations", "viewer", lambda r: {"investigations": self.svc.list_investigations(int(r["query"].get("limit", 50)))})
        add("POST", "/api/investigations", "analyst", self.investigate)
        add("GET", "/api/investigations/{id}", "viewer", lambda r: self.svc.get_investigation(r["params"]["id"]))
        add("GET", "/api/investigations/{id}/trace", "viewer", lambda r: self.svc.get_investigation(r["params"]["id"], trace=True)["trace"])
        add("GET", "/api/actions", "viewer", lambda r: {"actions": self.svc.list_actions(r["query"].get("status"))})
        add("POST", "/api/actions/{id}/approve", "reviewer", lambda r: self.svc.approve_action(r["params"]["id"], r["who"].identity, r["who"].role, str(r["body"].get("reason", ""))))
        add("POST", "/api/actions/{id}/reject", "reviewer", lambda r: self.svc.reject_action(r["params"]["id"], r["who"].identity, r["who"].role, str(r["body"].get("reason", ""))))
        add("POST", "/api/sessions", "analyst", lambda r: self.svc.record_session(str(r["body"].get("order_id", "")), str(r["body"].get("device", "")), str(r["body"].get("ip", "")), r["who"].identity, r["who"].role))
        add("POST", "/api/links", "analyst", lambda r: self.svc.record_link(str(r["body"].get("capture_id", "")), str(r["body"].get("item_id", "")), str(r["body"].get("basis", "")), r["who"].identity, r["who"].role))
        add("GET", "/api/events", "viewer", lambda r: {"events": self.svc.events()})
        add("GET", "/api/eval", "viewer", self.eval_results)
        add("GET", "/api/audit", "admin", lambda r: {"audit": self.svc.audit_log(int(r["query"].get("limit", 100)))})
        add("POST", "/api/demo/seed", "admin", lambda r: self.svc.seed_demo(r["who"].identity, r["who"].role))
        add("GET", "/api/paypal/health", "admin", lambda r: self.client.health())
        add("POST", "/api/ai/test", "admin", self.ai_test)
        add("POST", "/api/sandbox/order", "admin", self.sandbox_order)
        add("POST", "/api/sandbox/capture/{id}", "admin", self.sandbox_capture)
        add("POST", "/api/sandbox/payout", "admin", self.sandbox_payout)
        add("POST", "/api/sandbox/payout/{id}/sync", "admin", self.sandbox_payout_sync)
        return R

    # ---- handlers ---------------------------------------------------------------------------------------------------
    def readyz(self, r):
        checks = {"database": self.db.ping(), "paypal_verification_configured": bool(self.s.paypal_configured and self.s.paypal_webhook_id) or (self.s.demo_mode and not self.s.production)}
        if not all(checks.values()):
            raise HTTPError(503, "not ready", checks=checks)
        return {"status": "ready", "checks": checks}

    def webhook(self, r):
        try:
            out = process_webhook(self.db, self.s, self.client, r["headers"], r["raw"])
        except WebhookRejected as e:
            raise HTTPError(e.status, e.message) from None
        if out["status"] == "accepted" and out.get("entities", {}).get("captures") and self.s.paypal_configured:
            try:                                   # best-effort: payer/merchant come from the Orders API, never guessed
                ev = json.loads(r["raw"])
                enrich_capture(self.db, self.client, ev["resource"]["id"])
            except Exception:
                log.exception("capture enrichment failed")
        return out

    def investigate(self, r):
        b = r["body"]
        return self.svc.investigate(str(b.get("subject_type", "")), str(b.get("subject_id", "")), r["who"].identity, r["who"].role, str(b.get("request", ""))[:500])

    def eval_results(self, r):
        p = ROOT / "eval" / "results.json"
        if not p.exists():
            raise HTTPError(404, "no evaluation results yet: run python eval/run_eval.py")
        return json.loads(p.read_text())

    def ai_test(self, r):
        """One tiny model call so you can confirm the key, model name and endpoint work before relying on them."""
        from riskmesh.llm import LLMError, get_provider
        prov = self.provider if self.provider is not None else get_provider(self.s)
        if not prov.enabled:
            raise HTTPError(503, "no AI model configured: set RISKMESH_LLM_PROVIDER, RISKMESH_LLM_MODEL and RISKMESH_LLM_KEY")
        t0 = time.perf_counter()
        try:
            out = prov.complete('Reply with only this JSON object: {"ok": true}', "ping")
        except LLMError as e:
            raise HTTPError(502, f"the model call failed: {e}")
        return {"ok": True, "provider": prov.name, "model": prov.model, "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens_in": prov.tokens_in, "tokens_out": prov.tokens_out, "reply_chars": len(out)}

    def _sandbox_only(self):
        if not self.s.paypal_configured:
            raise HTTPError(503, "PayPal credentials are not configured")
        if not self.s.sandbox:
            raise HTTPError(403, "sandbox helper endpoints are disabled against a live PayPal base URL")

    def sandbox_order(self, r):
        self._sandbox_only()
        b = r["body"]
        try:
            o = self.client.create_order(str(b.get("amount", "25.00")), str(b.get("currency", "USD")), str(b.get("invoice_id", "")), str(b.get("custom_id", "")))
        except PayPalError as e:
            raise HTTPException_from(e)
        return {"order_id": o["id"], "approve": next((l["href"] for l in o.get("links", []) if l["rel"] == "payer-action"), None)}

    def sandbox_capture(self, r):
        """Capture an order the BUYER has approved, then turn PayPal's own response into evidence (no webhook needed).

        Optional body ``{"device": "...", "ip": "..."}`` records the session for this order. PayPal webhooks carry no device or IP,
        so without it live data can reach REVIEW but never HOLD."""
        self._sandbox_only()
        oid, b = r["params"]["id"], (r["body"] if isinstance(r["body"], dict) else {})
        try:                                              # validate BEFORE moving money: a typo must not leave a capture without a session
            valid_id(oid, "order id")
            if ("device" in b) != ("ip" in b):
                raise ValidationError("send both device and ip, or neither")
            if "device" in b:
                valid_id(str(b["device"]), "device id"); valid_id(str(b["ip"]).replace(":", "_"), "ip")
        except ValidationError as e:
            raise HTTPError(422, str(e))
        try:
            o = self.client.capture_order(oid, mock=r["query"].get("mock") or None)
        except PayPalError as e:
            raise HTTPException_from(e)
        except ValueError as e:
            raise HTTPError(400, str(e))
        except PayPalUnavailable:
            raise HTTPError(503, "PayPal unreachable; the capture may or may not have happened. Re-read the order before retrying.")
        out = {"status": o.get("status"), "order_id": oid, "ingested": False, "session_recorded": False}
        try:
            rows = sandbox_flow.capture_rows(o)
            if sandbox_flow.unknown_facts(rows) and any(x in sandbox_flow.unknown_facts(rows) for x in ("payer_id", "merchant_id")):
                try:                                      # the capture response may omit payer/merchant: read the order, never guess
                    rows = sandbox_flow.capture_rows(o, self.client.get_order(oid))
                except (PayPalError, PayPalUnavailable):
                    pass
            who = r["who"]
            out["captures"] = sandbox_flow.ingest_rows(self.db, rows, who.identity, who.role, f"order:{oid}").get("captures", 0)
            out.update(ingested=True, invoice_ids=[c["invoice_id"] for c in rows["captures"]], capture_ids=[c["capture_id"] for c in rows["captures"]],
                       unknown=sandbox_flow.unknown_facts(rows))
            payer = rows["captures"][0]["payer_id"] if rows["captures"] else None
            # a stable pseudonym lets the caller see when two "different" buyers are really the same PayPal account (fan-in counts distinct payers)
            out["payer_ref"] = None if is_unknown(payer) else hash_identifier(str(payer), self.s.pepper)
            if "device" in b:
                self.svc.record_session(oid, str(b["device"]), str(b["ip"]), who.identity, who.role)
                out["session_recorded"] = True
        except ValidationError as e:
            out["ingest_error"] = f"PayPal's response could not be turned into evidence: {e}"
        return out

    def _payout_view(self, batch_id: str, full: dict, rows: dict) -> dict:
        items = rows["payout_items"]
        return {"batch_id": batch_id, "status": rows["payout_batches"][0]["status"],
                "items": [{"item_id": i["item_id"], "sender_item_id": i["sender_item_id"], "status": i["status"], "receiver": i["recipient_display"]} for i in items],
                "unclaimed": sum(1 for i in items if i["status"] == "UNCLAIMED"),
                "note": None if items else "PayPal returned no items yet; call POST /api/sandbox/payout/{batch_id}/sync in a moment.",
                "hold_note": "Only UNCLAIMED items can be cancelled (the HOLD action). Pay a recipient email that has no PayPal account to get UNCLAIMED items."}

    def sandbox_payout(self, r):
        """Create a sandbox payout batch. sender_item_id MUST equal the capture's invoice_id for the item to be linked to that payment."""
        self._sandbox_only()
        try:
            req = sandbox_flow.validate_payout_request(r["body"] if isinstance(r["body"], dict) else {})
        except ValidationError as e:
            raise HTTPError(422, str(e))
        sbid = req["sender_batch_id"] or "RM-" + uuid.uuid4().hex[:12]
        try:
            created = self.client.create_payout_batch(sbid, req["items"], req["currency"], req["note"])
            pid = ((created.get("batch_header") or {}).get("payout_batch_id"))
            if not pid:
                raise HTTPError(502, "PayPal did not return a payout batch id")
            full = self.client.get_payout_batch(pid)
        except PayPalError as e:
            raise HTTPException_from(e)
        except PayPalUnavailable:
            raise HTTPError(503, f"PayPal unreachable; the batch may exist. Retry with the same sender_batch_id ({sbid}); PayPal de-duplicates on it.")
        try:
            rows = sandbox_flow.payout_rows(full, self.s)
        except ValidationError as e:
            raise HTTPError(502, f"PayPal's payout response could not be turned into evidence: {e}")
        who = r["who"]
        sandbox_flow.ingest_rows(self.db, rows, who.identity, who.role, f"payout:{pid}")
        return {"sender_batch_id": sbid, **self._payout_view(pid, full, rows)}

    def sandbox_payout_sync(self, r):
        """Re-read a payout batch from PayPal and merge the latest item statuses (PENDING -> UNCLAIMED/SUCCESS...)."""
        self._sandbox_only()
        try:
            full = self.client.get_payout_batch(valid_id(r["params"]["id"], "payout batch id"))
            rows = sandbox_flow.payout_rows(full, self.s)
        except ValidationError as e:
            raise HTTPError(422, str(e))
        except PayPalError as e:
            raise HTTPException_from(e)
        except PayPalUnavailable:
            raise HTTPError(503, "PayPal unreachable")
        who = r["who"]
        sandbox_flow.ingest_rows(self.db, rows, who.identity, who.role, f"payout:{r['params']['id']}")
        return self._payout_view(r["params"]["id"], full, rows)

    # ---- ASGI plumbing ----------------------------------------------------------------------------------------------
    def authenticate(self, headers: dict):
        h = headers.get("authorization", "")
        if not h.lower().startswith("bearer "):
            return None
        tok = h[7:].strip()
        found = None
        for t in self.s.tokens:                    # compare against every token so timing does not reveal which one matched
            if hmac.compare_digest(t.token.encode(), tok.encode()):
                found = t
        return found

    async def dispatch(self, method: str, path: str, query: dict, headers: dict, raw: bytes, ip: str):
        if method == "GET" and path in ("/", "/index.html"):
            f = ROOT / "web" / "index.html"
            return 200, "text/html; charset=utf-8", f.read_bytes() if f.exists() else b"<h1>RiskMesh AI</h1><p>Dashboard not built.</p>"
        match, allowed = None, set()
        for m, rx, role, fn in self.routes:
            mm = rx.match(path)
            if mm:
                allowed.add(m)
                if m == method:
                    match = (role, fn, mm.groupdict())
        if not match:
            raise HTTPError(405 if allowed else 404, "method not allowed" if allowed else "not found")
        role, fn, params = match
        who = None
        if role:
            who = self.authenticate(headers)
            if who is None:
                raise HTTPError(401, "missing or invalid bearer token")
            try:
                need(who.role, role)
            except ServiceError as e:
                raise HTTPError(403, e.message)
        if path not in ("/healthz", "/readyz") and not self.limiter.allow(f"{who.identity if who else 'ip:' + ip}"):
            raise HTTPError(429, "rate limit exceeded", retry_after=60)
        body = {}
        if method == "POST" and path != "/webhook" and raw:
            try:
                body = json.loads(raw)
            except ValueError:
                raise HTTPError(400, "body must be valid JSON")
            if not isinstance(body, dict):
                raise HTTPError(400, "body must be a JSON object")
        req = {"who": who, "params": params, "query": query, "body": body, "headers": headers, "raw": raw}
        try:
            result = await asyncio.to_thread(fn, req)
        except ServiceError as e:
            raise HTTPError(e.status, e.message, **e.extra)
        return 200, "application/json", json.dumps(result, default=str).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                m = await receive()
                if m["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif m["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        rid = uuid.uuid4().hex[:12]
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]}
        query = {k: v[-1] for k, v in __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(scope.get("query_string", b"").decode()).items()}
        ip = (scope.get("client") or ("unknown", 0))[0]
        extra = cors_headers(headers.get("origin", ""))
        if scope["method"] == "OPTIONS":   # CORS preflight: answered without auth; allowed origins only get the allow-headers
            await send({"type": "http.response.start", "status": 204, "headers": [(b"x-request-id", rid.encode()), *SECURITY_HEADERS, *extra]})
            await send({"type": "http.response.body", "body": b""})
            return
        try:
            raw, over = b"", False
            limit = 1_000_000 if scope["path"] == "/webhook" else MAX_JSON
            while True:
                m = await receive()
                raw += m.get("body", b"")
                if len(raw) > limit:
                    over = True
                    break
                if not m.get("more_body"):
                    break
            if over:
                raise HTTPError(413, "request body too large")
            status, ctype, payload = await self.dispatch(scope["method"], scope["path"], query, headers, raw, ip)
        except HTTPError as e:
            status, ctype, payload = e.status, "application/json", json.dumps({"error": e.message, "request_id": rid, **e.extra}).encode()
            if e.status == 429:
                extra.append((b"retry-after", b"60"))
        except Exception:
            log.exception("unhandled error request_id=%s", rid)
            status, ctype, payload = 500, "application/json", json.dumps({"error": "internal error", "request_id": rid}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", ctype.encode()), (b"x-request-id", rid.encode()), *SECURITY_HEADERS, *extra]})
        await send({"type": "http.response.body", "body": payload})


def HTTPException_from(e: PayPalError) -> HTTPError:
    return HTTPError(502, "PayPal refused the request", paypal_issue=e.issue, debug_id=e.debug_id, paypal_status=e.status)


build_app = App
_instance: Optional[App] = None


async def app(scope, receive, send):
    """Lazy ASGI entry point for ``uvicorn app:app``: configuration is validated on the first request / startup."""
    global _instance
    if _instance is None:
        _instance = App()
    await _instance(scope, receive, send)
