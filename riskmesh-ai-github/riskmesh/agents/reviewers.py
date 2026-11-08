"""Independent reviewers. They never author findings; they only accept a claim or object to it with a reason.

* EvidenceAuditor     - resolves every evidence reference against the records and re-runs the rule on a freshly loaded bundle.
* NumericVerifier     - recomputes amounts, totals, percentages and currencies with separate Decimal code.
* ConsistencyReviewer - finds contradictions between claims (including between different agents).
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal as D

from .. import rules
from ..financial import is_unknown
from ..metrics import compute
from ..verify import recompute_figure, resolve_refs, same_number
from .core import Agent


def obj(cid, text):
    return {"claim_id": cid, "text": text}


class EvidenceAuditor(Agent):
    role = "evidence-auditor"

    def handle(self, kind, payload):
        b, claims, targets = payload["bundle"], {c["id"]: c for c in payload["claims"]}, set(payload["targets"])
        out = []
        for cid in targets:
            c = claims[cid]
            t = c["type"]
            miss = resolve_refs(b, c["evidence"])
            if miss:
                out.append(obj(cid, f"evidence not found in the records: {[m.get('kind', '?') + ':' + str(m.get('ref')) for m in miss][:5]}"))
            a = c.get("assertion") or {}
            if t in ("FACT", "UNKNOWN") and "metric" in a:
                if c.get("rule_id") in rules.RULE:
                    f = rules.evaluate(b, c["rule_id"])
                    if f is None:
                        out.append(obj(cid, "rule does not apply to this subject"))
                    elif f["outcome"] != a["expected_outcome"]:
                        out.append(obj(cid, f"recomputation gives {f['outcome']} (value {f['value']}), claim says {a['expected_outcome']}"))
                    elif t == "FACT" and not same_number(f["value"], a.get("expected_value")):
                        out.append(obj(cid, f"recomputed value {f['value']} differs from claimed {a.get('expected_value')}"))
                else:
                    m = compute(b, a["metric"], a.get("args"))
                    if a["expected_outcome"] == "CANNOT" and not m["unknown"]:
                        out.append(obj(cid, "claimed UNKNOWN but the data needed is available"))
                if t == "FACT" and a.get("expected_outcome") == "FAIL" and not c["evidence"]:
                    out.append(obj(cid, "a FAIL indicator must cite at least one record"))
            elif t == "FACT":
                out.append(obj(cid, "FACT has no verifiable assertion"))
            elif t == "UNKNOWN" and "figure" in a:
                val, _ = recompute_figure(b, a)
                if val is not None:
                    out.append(obj(cid, "claimed UNKNOWN but the figure can be computed"))
            elif t == "INFERENCE":
                deps = [claims.get(d) for d in c["dependencies"]]
                if not deps or any(d is None for d in deps):
                    out.append(obj(cid, "inference must depend on existing claims"))
                elif not any(d["type"] in ("FACT", "FIGURE") for d in deps):
                    out.append(obj(cid, "inference rests on no FACT or FIGURE claim"))
                elif any(d["type"] == "FACT" and (d.get("assertion") or {}).get("expected_outcome") == "PASS" for d in deps):
                    out.append(obj(cid, "inference depends on a claim that found no signal"))
                if not c["evidence"]:
                    out.append(obj(cid, "inference cites no evidence record"))
            elif t == "ASSUMPTION" and not (c.get("data") or {}).get("basis"):
                out.append(obj(cid, "assumption states no basis"))
        return {"objections": out}


class NumericVerifier(Agent):
    role = "numeric-verifier"

    def _indep(self, b, rule_id):
        """Separate Decimal recomputation of the quantities behind percentage/total rules. Returns str or None (not computable)."""
        if rule_id == "dispute_ratio":
            caps = ([c for c in b["captures"] if c["status"] == "COMPLETED" and c["capture_id"] in {l["capture_id"] for l in b["links"]}]
                    if b["subject"]["type"] in ("beneficiary", "payout") else [c for c in b["captures"] if c["status"] == "COMPLETED"])
            ids = {c["capture_id"] for c in caps}
            n = sum(1 for d in b["disputes"] if d["capture_id"] in ids)
            return str((D(n) / D(len(caps))).quantize(D("0.0001"))) if len(caps) >= 4 else "n/a"
        if rule_id == "amount_uniform":
            v = [D(c["amount"]) for c in b["captures"] if c["status"] == "COMPLETED" and not is_unknown(c["amount"])]
            return str(((max(v) - min(v)) / max(v)).quantize(D("0.0001"))) if len(v) >= 4 and max(v) else "n/a"
        if rule_id == "batch_reconciliation" and b["subject"]["type"] == "payout":
            tot = D(0)
            for bt in b["payout_batches"]:
                its = [i for i in b["payout_items"] if i["batch_id"] == bt["batch_id"]]
                if is_unknown(bt["header_amount"]) or any(is_unknown(i["amount"]) for i in its):
                    return None
                d = abs(D(bt["header_amount"]) - sum((D(i["amount"]) for i in its), D(0)))
                tot += d + (D("0.01") if any(i["currency"] != bt["currency"] for i in its) else 0)
            return str(tot)
        return None

    def handle(self, kind, payload):
        b, claims, targets = payload["bundle"], {c["id"]: c for c in payload["claims"]}, set(payload["targets"])
        out = []
        for cid in targets:
            c = claims[cid]
            a = c.get("assertion") or {}
            if c["type"] == "FIGURE" and "figure" in a:
                val, note = recompute_figure(b, a)
                if val is None:
                    out.append(obj(cid, f"figure cannot be recomputed: {note}"))
                elif not same_number(val, a.get("expected")):
                    out.append(obj(cid, f"recomputed {a['figure']} over {a['scope']} is {val}, claim says {a.get('expected')}"))
            elif c["type"] == "FACT" and c.get("rule_id") and (c.get("data") or {}).get("finding"):
                ind = self._indep(b, c["rule_id"])
                claimed = c["data"]["finding"]["value"]
                if ind is not None and not same_number(ind, claimed) and ind != "n/a":
                    out.append(obj(cid, f"independent calculation of {c['rule_id']} gives {ind}, claim says {claimed}"))
        return {"objections": out}


class ConsistencyReviewer(Agent):
    role = "consistency-reviewer"

    def handle(self, kind, payload):
        claims, targets = {c["id"]: c for c in payload["claims"]}, set(payload["targets"])
        out = []
        by_rule, by_fig = defaultdict(list), defaultdict(list)
        for c in claims.values():
            if c["status"] == "REJECTED":
                continue
            a = c.get("assertion") or {}
            if c["type"] == "FACT" and c.get("rule_id"):
                by_rule[c["rule_id"]].append(c)
            if c["type"] == "FIGURE" and "figure" in a:
                by_fig[(a["figure"], a["scope"], a.get("num"), a.get("den"))].append(c)
        for rid, lst in by_rule.items():
            outs = {(c["assertion"]["expected_outcome"], str(c["assertion"].get("expected_value"))) for c in lst}
            if len(outs) > 1:
                for c in lst:
                    if c["id"] in targets:
                        out.append(obj(c["id"], f"contradicts another claim about {rid} by {sorted({x['author'] for x in lst if x is not c})}"))
        for key, lst in by_fig.items():
            if len({str(c["assertion"]["expected"]) for c in lst}) > 1:
                for c in lst:
                    if c["id"] in targets:
                        out.append(obj(c["id"], f"different agents report different values for {key[0]} over {key[1]}"))
        # logical impossibility: more distinct payers than linked captures
        n = next((c for c in claims.values() if (c.get("assertion") or {}).get("scope") == "linked_captures" and c["assertion"].get("figure") == "count" and c["status"] != "REJECTED"), None)
        p = next((c for c in claims.values() if (c.get("assertion") or {}).get("figure") == "distinct_payers" and c["status"] != "REJECTED"), None)
        if n and p:
            try:
                if D(str(p["assertion"]["expected"])) > D(str(n["assertion"]["expected"])) and p["id"] in targets:
                    out.append(obj(p["id"], "more distinct payers than linked captures is impossible"))
            except Exception:
                pass
        for c in claims.values():
            if c["id"] in targets and c["type"] == "INFERENCE":
                if any(claims[d]["type"] == "UNKNOWN" for d in c["dependencies"] if d in claims):
                    out.append(obj(c["id"], "inference asserts a conclusion while depending on an UNKNOWN claim"))
        return {"objections": out}
