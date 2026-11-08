"""Domain specialists. Each runs *different* analysis over a *different* slice of the bundle with a *different* prompt;
they are not renamed copies of one agent.

Every deterministic finding becomes a claim with a machine-checkable assertion, so reviewers can recompute it.
Optional LLM inferences are proposed as INFERENCE claims that must cite evidence and dependencies, and are verified like any other.
"""
from __future__ import annotations

import json
from decimal import Decimal as D

from .. import rules
from ..financial import UNKNOWN, is_unknown
from ..llm import LLMError, extract_json
from ..verify import recompute_figure
from .core import Agent
from .prompts import PROMPTS


PSEUDO_KEYS = ("device", "ip", "payer_id")


def pseudonymise(obj, salt=None):
    """Replace device, IP and payer identifiers with short random-salted tokens before text leaves the process.
    Equality is preserved (same device -> same token) so a model can still see 'these share one device'; the real values never leave."""
    import hashlib, uuid
    salt = salt or uuid.uuid4().hex

    def tok(kind, v):
        return v if v == UNKNOWN else f"{kind}#{hashlib.sha256((salt + str(v)).encode()).hexdigest()[:8]}"

    def walk(o):
        if isinstance(o, dict):
            return {k: (tok(k, v) if k in PSEUDO_KEYS and isinstance(v, str) else walk(v)) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    return walk(obj)


def _claim(local_id, text, type_, author, **kw):
    return {"local_id": local_id, "text": text, "type": type_, "author": author, **kw}


class Specialist(Agent):
    domain = ""

    def __init__(self, agent_id, provider=None):
        super().__init__(agent_id)
        self.provider = provider
        self.prompt = PROMPTS[agent_id]

    # -- slice of the evidence this agent is allowed to see (also what its LLM prompt receives) -------------------------
    def view(self, bundle: dict) -> dict:
        raise NotImplementedError

    def figures(self, bundle: dict) -> list:
        return []

    def assumptions(self, bundle: dict) -> list:
        return []

    # -- shared mechanics ---------------------------------------------------------------------------------------------
    def finding_claim(self, n, f):
        ev = [{**r, "source": f"metric:{f['metric']}"} for r in f["refs"]]
        data = {"finding": f}
        if f["outcome"] == "CANNOT":
            return _claim(f"F{n}", f"{f['rule_id']}: UNKNOWN - {f['why']}", "UNKNOWN", self.id, evidence=ev, source=f"metric:{f['metric']}", rule_id=f["rule_id"], data=data,
                          assertion={"metric": f["metric"], "args": f["args"], "expected_outcome": "CANNOT"})
        return _claim(f"F{n}", f"{f['rule_id']}: {f['outcome']} - {f['statement']}", "FACT", self.id, evidence=ev, source=f"metric:{f['metric']}", rule_id=f["rule_id"], data=data,
                      assertion={"metric": f["metric"], "args": f["args"], "expected_value": f["value"], "expected_outcome": f["outcome"]})

    def analyze(self, payload):
        bundle = payload["bundle"]
        claims, n = [], 0
        findings = rules.evaluate_domain(bundle, self.domain)
        for f in findings:
            n += 1
            claims.append(self.finding_claim(n, f))
        for text, a in self.figures(bundle):
            val, note = recompute_figure(bundle, a)
            n += 1
            if val is None:
                claims.append(_claim(f"G{n}", f"{text}: UNKNOWN - {note}", "UNKNOWN", self.id, source="figure", assertion={**a, "expected": None}))
            else:
                claims.append(_claim(f"G{n}", f"{text}: {val}", "FIGURE", self.id, source=f"figure:{a['figure']}", assertion={**a, "expected": val}, data={"value": val, "currency": a.get("currency")}))
        for text, basis in self.assumptions(bundle):
            n += 1
            claims.append(_claim(f"A{n}", text, "ASSUMPTION", self.id, source=basis, data={"basis": basis}))
        claims += self.llm_inferences(bundle, [c["local_id"] for c in claims if c["type"] in ("FACT", "FIGURE")])
        return {"claims": claims, "findings": findings, "agent": self.id, "llm": self.provider.name if self.provider and self.provider.enabled else "none"}

    def llm_inferences(self, bundle, usable_ids):
        if not (self.provider and self.provider.enabled):
            return []
        data = json.dumps({"claim_ids": usable_ids, "data": pseudonymise(self.view(bundle))})[:24000]
        try:
            j = extract_json(self.provider.complete(self.prompt, data))
        except (LLMError, ValueError):
            return []                      # an unusable model answer adds nothing; it never degrades the deterministic findings
        out = []
        for k, inf in enumerate((j.get("inferences") or [])[:3], 1):
            if isinstance(inf, dict) and isinstance(inf.get("text"), str):
                out.append(_claim(f"L{k}", inf["text"][:500], "INFERENCE", self.id, source=f"llm:{self.provider.name}",
                                  evidence=[{**e, "source": "llm-cited"} for e in (inf.get("evidence") or []) if isinstance(e, dict)],
                                  dependencies=[d for d in (inf.get("depends_on") or []) if isinstance(d, str)]))
        return out

    def revise(self, payload):
        """Re-derive a challenged claim from the data. Returns a corrected claim, or None to withdraw it."""
        c, bundle = payload["claim"], payload["bundle"]
        if c["rule_id"] and c["rule_id"] in rules.RULE:
            f = rules.evaluate(bundle, c["rule_id"])
            if f:
                nc = self.finding_claim(0, f)
                nc.update(local_id=c["id"])
                return {"claim": nc, "change": f"re-evaluated {c['rule_id']} from the evidence: {f['outcome']} ({f['value']})"}
        a = c.get("assertion") or {}
        if "figure" in a:
            val, note = recompute_figure(bundle, a)
            if val is not None:
                return {"claim": {**c, "text": c["text"].split(":")[0] + f": {val}", "assertion": {**a, "expected": val}, "data": {**c["data"], "value": val}}, "change": f"recomputed figure = {val}"}
        return {"claim": None, "change": "no supported correction exists; claim withdrawn"}

    def handle(self, kind, payload):
        if kind == "ANALYZE":
            return self.analyze(payload)
        if kind == "REVISE":
            return self.revise(payload)
        raise ValueError(f"unsupported task {kind}")


class TransactionSpecialist(Specialist):
    domain = "transaction"

    def view(self, b):
        return {"captures": [{k: c[k] for k in ("capture_id", "amount", "currency", "status", "created_at")} for c in b["captures"]]}

    def figures(self, b):
        cur = {c["currency"] for c in b["captures"] if c["status"] == "COMPLETED"}
        return [("Number of completed captures", {"figure": "count", "scope": "completed_captures"}),
                ("Total completed capture value", {"figure": "sum_amount", "scope": "completed_captures", "currency": next(iter(cur)) if len(cur) == 1 else None})] if b["captures"] else []

    def assumptions(self, b):
        return [("Capture timestamps are in UTC as supplied by PayPal; local-time patterns are not assessed.", "PayPal timestamps are ISO-8601 with an explicit offset")] if b["captures"] else []


class NetworkSpecialist(Specialist):
    domain = "network"

    def view(self, b):
        ids = {l["capture_id"] for l in b["links"]}
        return {"links": b["links"], "linked_captures": [{k: c[k] for k in ("capture_id", "payer_id", "device", "ip")} for c in b["captures"] if c["capture_id"] in ids]}

    def figures(self, b):
        return [("Verified capture-to-payout links", {"figure": "count", "scope": "linked_captures"}),
                ("Distinct known payers among linked captures", {"figure": "distinct_payers", "scope": "linked_captures"})]

    def assumptions(self, b):
        out = []
        if any(l["basis"].startswith("SENDER_ITEM_ID") for l in b["links"]):
            out.append(("A matching sender_item_id means the merchant intended the payout for that sale.", "merchant-supplied correlation key; PayPal does not verify it"))
        return out

    def analyze(self, payload):
        res = super().analyze(payload)
        claims = res["claims"]
        by_rule = {c.get("rule_id"): c for c in claims}
        deps = [by_rule[r]["local_id"] for r in ("fan_in_verified", "shared_device", "shared_ip", "account_cluster") if r in by_rule and by_rule[r]["type"] == "FACT" and by_rule[r]["assertion"]["expected_outcome"] == "FAIL"]
        if "fan_in_verified" in deps and len(deps) >= 2:
            claims.append(_claim("I1", "Payout-ring indicators are present: many verified payers fund one beneficiary and share infrastructure. This is an indicator, not confirmed fraud.", "INFERENCE",
                                 self.id, source="derived from the dependent FACT claims", dependencies=deps, evidence=by_rule["fan_in_verified"]["evidence"]))
        elif payload["bundle"]["subject"]["type"] in ("beneficiary", "payout") and not payload["bundle"]["links"]:
            claims.append(_claim("U1", "The relationship between captures and this payout is UNKNOWN: no verified link (sender_item_id/invoice match or analyst-recorded) exists, so no payout ring can be asserted.", "UNKNOWN",
                                 self.id, source="links table", assertion={"metric": "linked_payers", "args": {}, "expected_outcome": "CANNOT"}))
        return res


class PayoutSpecialist(Specialist):
    domain = "payout"

    def view(self, b):
        return {"payout_batches": b["payout_batches"], "payout_items": [{k: i[k] for k in ("item_id", "batch_id", "recipient_id", "amount", "currency", "status", "sender_item_id", "created_at")} for i in b["payout_items"]]}

    def figures(self, b):
        out = []
        if b["payout_items"]:
            cur = {i["currency"] for i in b["payout_items"]}
            out.append(("Total payout item value", {"figure": "sum_amount", "scope": "payout_items", "currency": next(iter(cur)) if len(cur) == 1 else None}))
            out.append(("Number of payout items", {"figure": "count", "scope": "payout_items"}))
        for bt in b["payout_batches"]:
            if b["subject"]["type"] == "payout":
                out.append((f"Header amount of batch {bt['batch_id']}", {"figure": "sum_amount", "scope": "batch_headers", "currency": bt["currency"]}))
        return out[:6]

    def assumptions(self, b):
        return [("Payout item status is as last reported by PayPal webhooks; it is re-read from PayPal before any action.", "action-time verification step")] if b["payout_items"] else []


class DisputeSpecialist(Specialist):
    domain = "dispute"

    def view(self, b):
        return {"disputes": [{k: d[k] for k in ("dispute_id", "capture_id", "reason", "status", "amount", "currency")} for d in b["disputes"]]}

    def figures(self, b):
        return [("Number of disputes", {"figure": "count", "scope": "disputes"})] if b["captures"] else []


SPECIALISTS = {"transaction-specialist": TransactionSpecialist, "network-specialist": NetworkSpecialist, "payout-specialist": PayoutSpecialist, "dispute-specialist": DisputeSpecialist}
