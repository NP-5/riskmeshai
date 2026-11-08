"""Lead orchestrator: decomposes an investigation, dispatches specialists concurrently, runs the review/revision loop
(at most two revision rounds) and asks the synthesizer for the final report. Records a full execution trace."""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Callable, Optional

from ..config import Settings
from ..ledger import Ledger
from .core import Agent, Bus
from .reviewers import ConsistencyReviewer, EvidenceAuditor, NumericVerifier
from .specialists import SPECIALISTS
from .synth import Synthesizer

LEAD = "lead-orchestrator"


class Orchestrator:
    def __init__(self, settings: Settings, provider=None, agents: Optional[dict] = None):
        self.s, self.provider = settings, provider
        self.agents: dict = agents or self.default_agents()

    def default_agents(self) -> dict:
        ag = {aid: cls(aid, self.provider) for aid, cls in SPECIALISTS.items()}
        ag.update({"evidence-auditor": EvidenceAuditor("evidence-auditor"), "numeric-verifier": NumericVerifier("numeric-verifier"),
                   "consistency-reviewer": ConsistencyReviewer("consistency-reviewer"), "final-synthesizer": Synthesizer("final-synthesizer")})
        return ag

    # ---- messaging with timeout + retry --------------------------------------------------------------------------------
    async def call(self, bus: Bus, task_id: str, to: str, kind: str, payload: dict, tasks_log: list) -> Optional[dict]:
        attempts = 1 + max(0, self.s.agent_retries)
        for n in range(1, attempts + 1):
            m = bus.message(task_id, LEAD, to, kind, payload)
            fut = asyncio.get_running_loop().create_future()
            bus.waiting[m.msg_id] = fut
            t0 = time.perf_counter()
            await bus.send(m)
            try:
                reply = await asyncio.wait_for(fut, self.s.agent_timeout)
                ok = reply.kind == "RESULT"
                tasks_log.append({"task_id": task_id, "agent": to, "kind": kind, "attempt": n, "status": "ok" if ok else "error", "error": None if ok else reply.payload.get("error"),
                                  "duration_ms": int((time.perf_counter() - t0) * 1000)})
                if ok:
                    return reply.payload
            except asyncio.TimeoutError:
                bus.waiting.pop(m.msg_id, None)
                tasks_log.append({"task_id": task_id, "agent": to, "kind": kind, "attempt": n, "status": "timeout", "error": f"no reply in {self.s.agent_timeout}s", "duration_ms": int((time.perf_counter() - t0) * 1000)})
        return None

    # ---- planning ------------------------------------------------------------------------------------------------------
    @staticmethod
    def plan(subject_type: str, bundle: dict) -> list:
        """Decompose the request into subtasks with dependencies; skip specialists whose data does not exist."""
        tasks = []
        def add(tid, agent, deps, why, skip=None):
            tasks.append({"task_id": tid, "agent": agent, "depends_on": deps, "purpose": why, "skipped": skip})
        has_c, has_p = bool(bundle["captures"]), bool(bundle["payout_items"])
        add("T1", "transaction-specialist", [], "amounts, frequency, failures, timing of captures", None if has_c else "no captures in evidence")
        add("T2", "network-specialist", [], "verified payer/beneficiary relationships and shared device/IP", None if subject_type in ("beneficiary", "payout") else "not a payout subject")
        add("T3", "payout-specialist", [], "payout batches, items, recipients, status", None if has_p else "no payout items in evidence")
        add("T4", "dispute-specialist", [], "dispute history and recurring reasons", None if has_c else "no captures to attach disputes to")
        add("T5", "evidence-auditor", ["T1", "T2", "T3", "T4"], "verify every claim against the records")
        add("T6", "numeric-verifier", ["T1", "T2", "T3", "T4"], "recompute figures, totals, percentages, currencies")
        add("T7", "consistency-reviewer", ["T1", "T2", "T3", "T4"], "find contradictions between claims and agents")
        add("T8", "final-synthesizer", ["T5", "T6", "T7"], "write the report from verified claims only")
        return tasks

    # ---- main entry ----------------------------------------------------------------------------------------------------
    async def investigate(self, subject_type: str, subject_id: str, bundle: dict, fresh_loader: Callable[[], dict], request: str = "") -> dict:
        inv_id = "INV-" + uuid.uuid4().hex[:10].upper()
        t0 = time.perf_counter()
        bus, ledger, log = Bus(), Ledger(), []
        bus.register(LEAD)
        servers = []
        for aid, ag in self.agents.items():
            bus.register(aid)
            servers.append(asyncio.create_task(ag.serve(bus)))
        plan = self.plan(subject_type, bundle)
        rounds = []
        try:
            # 1. specialists run concurrently
            runnable = [t for t in plan if t["task_id"] in ("T1", "T2", "T3", "T4") and not t["skipped"]]
            results = await asyncio.gather(*(self.call(bus, t["task_id"], t["agent"], "ANALYZE", {"bundle": bundle}, log) for t in runnable))
            for t, res in zip(runnable, results):
                t["status"] = "done" if res else "failed"
                if res is None:
                    c = ledger.new(f"{t['agent']} failed or timed out: its domain is UNKNOWN and was not assessed.", "UNKNOWN", LEAD, source="orchestrator", data={"failed_agent": t["agent"]})
                    ledger.verify(c.id)
                    continue
                idmap = {}
                for cd in res["claims"]:
                    c = ledger.new(cd["text"], cd["type"], cd["author"], evidence=cd.get("evidence", []), source=cd.get("source", ""), assertion=cd.get("assertion"),
                                   rule_id=cd.get("rule_id"), data=cd.get("data", {}))
                    idmap[cd["local_id"]] = c.id
                    c.dependencies = [cd2 for cd2 in cd.get("dependencies", [])]
                for c in ledger.claims.values():                      # remap local dependency ids for this agent's claims
                    if c.author == t["agent"]:
                        c.dependencies = [idmap[d] for d in c.dependencies if d in idmap] + [d for d in c.dependencies if d not in idmap and d.startswith("CLM-")]
                        if any(d not in idmap and not d.startswith("CLM-") for d in c.dependencies):
                            c.dependencies.append("UNRESOLVED")
            for t in plan:
                if t["skipped"]:
                    t["status"] = "skipped"
            # 2. review / revise loop (initial review + at most max_revision_rounds revision rounds)
            r = 0
            while True:
                targets = [c.id for c in ledger.claims.values() if c.status == "PROPOSED"]
                if not targets:
                    break
                fresh = fresh_loader()
                payload = {"bundle": fresh, "claims": ledger.to_list(), "targets": targets}
                outs = await asyncio.gather(*(self.call(bus, f"T{5 + i}-r{r}", aid, "REVIEW", payload, log) for i, aid in enumerate(("evidence-auditor", "numeric-verifier", "consistency-reviewer"))))
                flagged = {}
                for aid, o in zip(("evidence-auditor", "numeric-verifier", "consistency-reviewer"), outs):
                    if o is None:
                        for cid in targets:                          # a reviewer that fails cannot silently approve
                            flagged.setdefault(cid, []).append({"by": aid, "text": f"reviewer {aid} unavailable; claim could not be verified"})
                        continue
                    for ob in o["objections"]:
                        ledger.object(ob["claim_id"], aid, r, ob["text"])
                        flagged.setdefault(ob["claim_id"], []).append({"by": aid, "text": ob["text"]})
                for cid in targets:
                    if cid not in flagged and "UNRESOLVED" not in ledger.claims[cid].dependencies:
                        ledger.verify(cid)
                    elif cid not in flagged:
                        flagged[cid] = [{"by": "evidence-auditor", "text": "dependency could not be resolved"}]
                        ledger.object(cid, "evidence-auditor", r, "dependency could not be resolved")
                rounds.append({"round": r, "reviewed": len(targets), "objected": len(flagged), "claims": sorted(flagged)})
                if not flagged:
                    break
                if r >= self.s.max_revision_rounds:
                    for cid in flagged:
                        ledger.reject(cid)
                    break
                r += 1
                revisions = await asyncio.gather(*(self.revise(bus, ledger, cid, objs, fresh, r, log) for cid, objs in flagged.items()))
                rounds[-1]["revised"] = sum(1 for x in revisions if x)
            # reject anything that rests on a rejected claim (fixpoint)
            changed = True
            while changed:
                changed = False
                for c in ledger.claims.values():
                    if c.status == "VERIFIED" and any(d in ledger.claims and ledger.claims[d].status == "REJECTED" for d in c.dependencies):
                        ledger.object(c.id, LEAD, r, "a claim this one depends on was rejected")
                        ledger.reject(c.id)
                        changed = True
            # 3. synthesis from verified claims only
            report = await self.call(bus, "T8", "final-synthesizer", "SYNTHESIZE", {"claims": ledger.to_list(), "subject": {"type": subject_type, "id": subject_id}, "has_links": bool(bundle["links"])}, log)
            if report is None:
                from .synth import synthesize
                report = synthesize(ledger.to_list(), {"type": subject_type, "id": subject_id}, bool(bundle["links"]))
                report["summary"] += " (synthesizer agent failed; report built by the orchestrator fallback)"
        finally:
            for aid in self.agents:
                await bus.send(bus.message("STOP", LEAD, aid, "STOP", {}))
            await asyncio.gather(*servers, return_exceptions=True)
        usage = {"provider": getattr(self.provider, "name", "none"), "llm_calls": getattr(self.provider, "calls", 0), "tokens_in": getattr(self.provider, "tokens_in", 0),
                 "tokens_out": getattr(self.provider, "tokens_out", 0), "cost_usd": self.provider.cost_usd() if self.provider else None, "latency_ms": int((time.perf_counter() - t0) * 1000)}
        trace = {"investigation_id": inv_id, "request": request, "plan": plan, "tasks": log, "rounds": rounds, "messages": bus.trace(),
                 "agents": [{"agent_id": a.id, "role": a.role, "executions": a.executions} for a in self.agents.values()], "usage": usage}
        return {"id": inv_id, "report": report, "claims": ledger.to_list(), "trace": trace, "usage": usage}

    async def revise(self, bus, ledger, cid, objs, fresh, r, log):
        c = ledger.claims[cid]
        if c.author not in self.agents or not hasattr(self.agents[c.author], "revise"):
            ledger.reject(cid)
            return False
        res = await self.call(bus, f"{cid}-rev{r}", c.author, "REVISE", {"claim": c.to_dict(), "objections": objs, "bundle": fresh}, log)
        if not res or not res.get("claim"):
            c.revisions.append({"round": r, "by": c.author, "text": c.text, "change": (res or {}).get("change", "author unavailable; claim withdrawn")})
            ledger.reject(cid)
            return False
        nc = res["claim"]
        c.revisions.append({"round": r, "by": c.author, "text": c.text, "change": res.get("change", "")})
        c.text, c.evidence, c.assertion, c.data, c.source = nc["text"], nc.get("evidence", c.evidence), nc.get("assertion", c.assertion), nc.get("data", c.data), nc.get("source", c.source)
        c.status = "PROPOSED"
        return True
