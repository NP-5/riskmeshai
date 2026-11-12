"""Application service: investigations, human-approved payout holds, evidence input.

Safety rules enforced here (not in the UI):
* A model never triggers an action. Only a PENDING action approved by a reviewer/admin can run.
* Approval and execution are conditional single-statement state changes (PENDING->APPROVED->EXECUTING->final), so a double
  click, a retry or two reviewers cannot execute the same hold twice. ``idempotency_key`` is UNIQUE.
* Just before the PayPal call the evidence is rebuilt from the database and the decision must still be HOLD.
* The cancel call is never retried. After success (or an uncertain timeout) the payout item is re-read from PayPal and
  the result is stored as verification. If PayPal refuses, the action is FAILED with the ``debug_id`` and nothing is claimed.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Optional

from . import demo_data
from .agents.orchestrator import Orchestrator
from .brief import make_brief
from .bundles import build_bundle
from .config import Settings
from .db import Database, audit, redact, utcnow
from .llm import get_provider
from .financial import UNKNOWN, ValidationError, valid_id
from .paypal import PayPalClient, PayPalError, PayPalNotConfigured, PayPalUnavailable

SUBJECT_TYPES = ("beneficiary", "payout", "transaction", "merchant")
CANCELLABLE = {"UNCLAIMED", "PENDING", "ONHOLD", "HELD"}
ROLE_RANK = {"viewer": 0, "analyst": 1, "reviewer": 2, "admin": 3}


class ServiceError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def need(role: str, minimum: str):
    if ROLE_RANK.get(role, -1) < ROLE_RANK[minimum]:
        raise ServiceError(403, f"role '{role}' cannot do this; '{minimum}' or higher is required")


def _j(v):
    return json.dumps(v, default=str)


class Service:
    def __init__(self, settings: Settings, db: Database, client: PayPalClient, provider=None):
        self.s, self.db, self.client, self.provider = settings, db, client, provider

    # ---- investigations ---------------------------------------------------------------------------------------------
    def _provider(self):
        """A fresh provider per investigation (so token/call usage is per case) unless one was injected (tests)."""
        return self.provider if self.provider is not None else get_provider(self.s)

    def _orchestrator(self, provider="default") -> Orchestrator:
        return Orchestrator(self.s, self._provider() if provider == "default" else provider)

    def investigate(self, subject_type: str, subject_id: str, actor: str, role: str, request: str = "") -> dict:
        need(role, "analyst")
        if subject_type not in SUBJECT_TYPES:
            raise ServiceError(422, f"subject_type must be one of {', '.join(SUBJECT_TYPES)}")
        try:
            subject_id = valid_id(subject_id, "subject id")
        except ValidationError as exc:
            raise ServiceError(422, str(exc)) from None
        try:
            bundle = build_bundle(self.db, subject_type, subject_id)
        except LookupError as exc:
            raise ServiceError(404, str(exc)) from None
        prov = self._provider()
        res = asyncio.run(self._orchestrator(prov).investigate(subject_type, subject_id, bundle, lambda: build_bundle(self.db, subject_type, subject_id), request))
        try:
            res["report"]["ai_brief"] = make_brief(prov, res["report"], res["claims"])
        except Exception as exc:                                  # the brief is an extra: it can never block or change an investigation
            res["report"]["ai_brief"] = {"available": False, "reason": f"brief step failed ({type(exc).__name__})"}
        res["usage"].update(llm_calls=getattr(prov, "calls", 0), tokens_in=getattr(prov, "tokens_in", 0), tokens_out=getattr(prov, "tokens_out", 0))
        res["trace"]["usage"] = res["usage"]
        risk = res["report"]["risk"]
        res["report"]["risk"]["recommendation"] = risk["decision"]
        with self.db.tx() as c:
            c.execute("INSERT INTO investigations (id, subject_type, subject_id, status, created_by, created_at, risk_level, risk_score, recommendation, "
                      "bundle_json, report_json, claims_json, trace_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (res["id"], subject_type, subject_id, "COMPLETE", actor, utcnow(), risk["level"], int(risk["score"]), risk["decision"],
                       _j(bundle), _j(res["report"]), _j(res["claims"]), _j(res["trace"])))
            audit(c, actor, role, "investigation.create", res["id"], risk["decision"], {"subject": f"{subject_type}:{subject_id}", "score": risk["score"]})
            proposed = self._propose_holds(c, res["id"], risk["decision"], subject_type, bundle)
        return {**self.get_investigation(res["id"]), "proposed_actions": proposed}

    def _propose_holds(self, c, inv_id, decision, subject_type, bundle) -> list:
        """HOLD on a payout subject creates PENDING hold requests for cancellable items. Nothing is executed."""
        out = []
        if decision != "HOLD" or subject_type not in ("beneficiary", "payout"):
            return out
        for it in bundle["payout_items"]:
            if str(it["status"]).upper() not in CANCELLABLE:
                continue
            existing = c.one("SELECT id FROM actions WHERE action_type='HOLD_PAYOUT_ITEM' AND target_id=? AND status IN ('PENDING','APPROVED','EXECUTING','EXECUTED','SIMULATED')", (it["item_id"],))
            if existing:
                out.append(existing["id"])
                continue
            aid = "ACT-" + uuid.uuid4().hex[:10].upper()
            c.execute("INSERT INTO actions (id, investigation_id, action_type, target_id, status, idempotency_key, requested_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (aid, inv_id, "HOLD_PAYOUT_ITEM", it["item_id"], "PENDING", f"hold:{it['item_id']}:{inv_id}", "riskmesh-agent", utcnow()))
            audit(c, "riskmesh-agent", "system", "action.propose", aid, "PENDING", {"item": it["item_id"], "investigation": inv_id})
            out.append(aid)
        return out

    @staticmethod
    def _inv_row(r, full=False):
        rep = json.loads(r["report_json"])
        base = {"id": r["id"], "subject_type": r["subject_type"], "subject_id": r["subject_id"], "status": r["status"], "created_by": r["created_by"], "created_at": r["created_at"],
                "risk_level": r["risk_level"], "risk_score": r["risk_score"], "recommendation": r["recommendation"], "summary": rep.get("summary"), "ring": rep["risk"].get("ring")}
        if full:
            base.update(report=rep, claims=json.loads(r["claims_json"]), bundle_counts={k: len(v) for k, v in json.loads(r["bundle_json"]).items() if isinstance(v, list)})
        return base

    def get_investigation(self, inv_id: str, trace: bool = False) -> dict:
        with self.db.tx() as c:
            r = c.one("SELECT * FROM investigations WHERE id = ?", (inv_id,))
            acts = c.all("SELECT * FROM actions WHERE investigation_id = ? ORDER BY created_at", (inv_id,)) if r else []
        if not r:
            raise ServiceError(404, "investigation not found")
        out = self._inv_row(r, full=True)
        out["actions"] = [self._act_row(a) for a in acts]
        if trace:
            out["trace"] = json.loads(r["trace_json"])
        return out

    def list_investigations(self, limit: int = 50) -> list:
        with self.db.tx() as c:
            rows = c.all("SELECT * FROM investigations ORDER BY created_at DESC, id LIMIT ?", (max(1, min(limit, 200)),))
        return [self._inv_row(r) for r in rows]

    def subjects(self) -> list:
        """Beneficiaries (hashed recipient ids) available to investigate, with masked display and item counts."""
        with self.db.tx() as c:
            return c.all("SELECT recipient_id AS subject_id, MIN(recipient_display) AS display, COUNT(*) AS items, MIN(source) AS source FROM payout_items "
                         "WHERE recipient_id <> 'UNKNOWN' GROUP BY recipient_id ORDER BY items DESC LIMIT 200")

    # ---- actions ----------------------------------------------------------------------------------------------------
    @staticmethod
    def _act_row(a: dict) -> dict:
        d = dict(a)
        for k in ("paypal_response_json", "verification_json"):
            d[k[:-5]] = json.loads(d.pop(k)) if d.get(k) else None
        return d

    def list_actions(self, status: Optional[str] = None) -> list:
        with self.db.tx() as c:
            rows = c.all("SELECT * FROM actions WHERE (? IS NULL OR status = ?) ORDER BY created_at DESC LIMIT 200", (status, status))
        return [self._act_row(a) for a in rows]

    def reject_action(self, action_id: str, actor: str, role: str, reason: str) -> dict:
        need(role, "reviewer")
        with self.db.tx() as c:
            cur = c.execute("UPDATE actions SET status='REJECTED', decided_by=?, decided_role=?, decided_at=?, decision_reason=? WHERE id=? AND status='PENDING'",
                            (actor, role, utcnow(), (reason or "")[:500], action_id))
            row = c.one("SELECT status FROM actions WHERE id=?", (action_id,)) if cur.rowcount == 0 else None
            if cur.rowcount == 0:
                audit(c, actor, role, "action.reject", action_id, "conflict", {"status": row and row["status"]})
            else:
                audit(c, actor, role, "action.reject", action_id, "REJECTED", {"reason": reason})
        if cur.rowcount == 0:        # raised after the transaction so the audit record of the refusal is kept
            raise ServiceError(404 if not row else 409, "action not found" if not row else f"action is {row['status']}, not PENDING")
        return self.get_action(action_id)

    def get_action(self, action_id: str) -> dict:
        with self.db.tx() as c:
            a = c.one("SELECT * FROM actions WHERE id=?", (action_id,))
        if not a:
            raise ServiceError(404, "action not found")
        return self._act_row(a)

    def approve_action(self, action_id: str, actor: str, role: str, reason: str = "") -> dict:
        """Reviewer approval, then execution. Safe to call twice: the second call sees a non-PENDING row and returns 409."""
        need(role, "reviewer")
        with self.db.tx() as c:
            a = c.one("SELECT * FROM actions WHERE id=?", (action_id,))
            cur = None
            if a:
                cur = c.execute("UPDATE actions SET status='APPROVED', decided_by=?, decided_role=?, decided_at=?, decision_reason=? WHERE id=? AND status='PENDING'",
                                (actor, role, utcnow(), (reason or "")[:500], action_id))
                audit(c, actor, role, "action.approve", action_id, "APPROVED" if cur.rowcount else "conflict", {"reason": reason, "status": a["status"]})
        if not a:
            raise ServiceError(404, "action not found")
        if cur.rowcount == 0:
            raise ServiceError(409, f"action is {a['status']}; it can only be approved while PENDING", current_status=a["status"])
        return self._execute(a, actor, role)

    def _finish(self, action_id, status, actor, role, **cols):
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.db.tx() as c:
            c.execute(f"UPDATE actions SET status=?, executed_at=?, {sets} WHERE id=?", (status, utcnow(), *cols.values(), action_id))
            audit(c, actor, role, "action.execute", action_id, status, {k: v for k, v in cols.items() if k != "paypal_response_json"})
        return self.get_action(action_id)

    def _execute(self, a: dict, actor: str, role: str) -> dict:
        aid, item = a["id"], a["target_id"]
        with self.db.tx() as c:
            cur = c.execute("UPDATE actions SET status='EXECUTING' WHERE id=? AND status='APPROVED'", (aid,))
            inv = c.one("SELECT subject_type, subject_id FROM investigations WHERE id=?", (a["investigation_id"],))
        if cur.rowcount == 0:
            raise ServiceError(409, "action is already being executed")
        # 1. fresh evidence: the decision must still be HOLD and the item must still be cancellable
        try:
            fresh = build_bundle(self.db, inv["subject_type"], inv["subject_id"])
            rep = asyncio.run(self._orchestrator(None).investigate(inv["subject_type"], inv["subject_id"], fresh, lambda: build_bundle(self.db, inv["subject_type"], inv["subject_id"]), "pre-action re-check"))
            decision = rep["report"]["risk"]["decision"]
            row = next((i for i in fresh["payout_items"] if i["item_id"] == item), None)
        except Exception as exc:
            return self._finish(aid, "FAILED", actor, role, mode="none", verification_json=_j({"error": f"re-check failed: {type(exc).__name__}"}))
        if decision != "HOLD" or row is None or str(row["status"]).upper() not in CANCELLABLE:
            why = "decision is no longer HOLD" if decision != "HOLD" else "payout item is not cancellable (status " + (str(row["status"]) if row else "missing") + ")"
            return self._finish(aid, "FAILED", actor, role, mode="none", verification_json=_j({"blocked": why, "recheck_decision": decision}))
        # 2. demo mode never calls PayPal and says so
        if not self.s.paypal_configured:
            if self.s.demo_mode and not self.s.production:
                return self._finish(aid, "SIMULATED", actor, role, mode="demo", verification_json=_j({"note": "demo mode: PayPal was not called; no money moved or was held"}))
            return self._finish(aid, "FAILED", actor, role, mode="none", verification_json=_j({"error": "PayPal credentials are not configured; refusing to simulate in this mode"}))
        # 3. the real call, never retried
        http_status, resp, uncertain = None, {}, False
        try:
            http_status, resp = self.client.cancel_payout_item(item)
            outcome = "ok"
        except PayPalError as e:
            http_status, resp, outcome = e.status, redact(e.to_dict()), "refused"
        except PayPalUnavailable:
            outcome, uncertain = "uncertain", True
        except PayPalNotConfigured:
            return self._finish(aid, "FAILED", actor, role, mode="none", verification_json=_j({"error": "PayPal not configured"}))
        # 4. read the item back; the stored verification is what PayPal says now, not what we hoped
        ver = {"call_outcome": outcome}
        try:
            got = self.client.get_payout_item(item)
            st = str(got.get("transaction_status") or "UNKNOWN").upper()
            ver.update(paypal_item_status=st, confirmed_cancelled=st in ("CANCELED", "CANCELLED", "RETURNED"))
        except (PayPalError, PayPalUnavailable) as exc:
            ver.update(paypal_item_status="UNKNOWN", confirmed_cancelled=False, readback_error=type(exc).__name__)
        mode = "sandbox" if self.s.sandbox else "live"
        if outcome == "ok" and ver["confirmed_cancelled"]:
            return self._finish(aid, "EXECUTED", actor, role, mode=mode, paypal_http_status=http_status, paypal_response_json=_j(redact(resp)), verification_json=_j(ver))
        status = "UNCERTAIN" if (uncertain or outcome == "ok") else "FAILED"
        if status == "UNCERTAIN":
            ver["next_step"] = "Re-read the payout item in PayPal before taking any further action; do not repeat the cancel blindly."
        return self._finish(aid, status, actor, role, mode=mode, paypal_http_status=http_status, paypal_response_json=_j(resp), verification_json=_j(ver))

    # ---- evidence input -----------------------------------------------------------------------------------------------
    def record_session(self, order_id: str, device: str, ip: str, actor: str, role: str) -> dict:
        need(role, "analyst")
        try:
            order_id = valid_id(order_id, "order id")
            device, ip = valid_id(device, "device id"), valid_id(ip.replace(":", "_"), "ip") and ip
        except ValidationError as exc:
            raise ServiceError(422, str(exc)) from None
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO sessions (order_id, device, ip, source, recorded_at) VALUES (?,?,?,?,?)", (order_id, device, ip, f"analyst:{actor}", utcnow()))
            audit(c, actor, role, "session.record", order_id, "ok", {})
        return {"order_id": order_id, "recorded": True}

    def record_link(self, capture_id: str, item_id: str, basis: str, actor: str, role: str) -> dict:
        need(role, "analyst")
        try:
            capture_id, item_id = valid_id(capture_id, "capture id"), valid_id(item_id, "payout item id")
        except ValidationError as exc:
            raise ServiceError(422, str(exc)) from None
        with self.db.tx() as c:
            if not c.one("SELECT 1 AS x FROM captures WHERE capture_id=?", (capture_id,)) or not c.one("SELECT 1 AS x FROM payout_items WHERE item_id=?", (item_id,)):
                raise ServiceError(404, "capture or payout item not found; links are only recorded between records we hold")
            c.execute("INSERT INTO links (capture_id, item_id, basis, created_by, created_at) VALUES (?,?,?,?,?) ON CONFLICT (capture_id, item_id) DO NOTHING",
                      (capture_id, item_id, (basis or "ANALYST_RECORDED")[:80], actor, utcnow()))
            audit(c, actor, role, "link.record", f"{capture_id}->{item_id}", "ok", {"basis": basis})
        return {"capture_id": capture_id, "item_id": item_id, "recorded": True}

    def seed_demo(self, actor: str, role: str) -> dict:
        need(role, "admin")
        if not self.s.demo_mode or self.s.production:
            raise ServiceError(403, "demo data can only be loaded when RISKMESH_DEMO_MODE=1 outside production")
        ids = demo_data.seed(self.db, self.s.pepper)
        ids.update(demo_data.seed_showcase(self.db, self.s.pepper))
        with self.db.tx() as c:                                   # demo only: start from a clean case list so repeated seeding never piles up duplicates
            c.execute("DELETE FROM actions")
            c.execute("DELETE FROM investigations")
            audit(c, actor, role, "demo.seed", "demo", "ok", {})
        done = 0
        for sid in ids.values():                                  # one investigation per synthetic recipient, so the dashboard is populated straight away
            try:
                self.investigate("beneficiary", sid, actor, role)
                done += 1
            except Exception:
                pass
        return {"seeded": True, "subjects": ids, "investigations": done}

    def audit_log(self, limit: int = 100) -> list:
        with self.db.tx() as c:
            rows = c.all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))
        return [{**r, "detail": json.loads(r.pop("detail_json"))} for r in rows]

    def events(self, limit: int = 50) -> list:
        with self.db.tx() as c:
            rows = c.all("SELECT event_id, event_type, received_at, verification, summary_json FROM webhook_events ORDER BY received_at DESC LIMIT ?", (max(1, min(limit, 200)),))
        return [{**r, "summary": json.loads(r.pop("summary_json"))} for r in rows]
