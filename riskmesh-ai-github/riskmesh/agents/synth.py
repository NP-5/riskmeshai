"""Final synthesizer: builds the report from VERIFIED claims only and keeps facts, figures, inferences, assumptions and UNKNOWN apart."""
from __future__ import annotations

from .. import rules
from .core import Agent


def synthesize(claims: list, subject: dict, has_links: bool) -> dict:
    ver = [c for c in claims if c["status"] == "VERIFIED"]
    rej = [c for c in claims if c["status"] == "REJECTED"]
    st, stmts = subject["type"], []

    def stmt(kind, c):
        stmts.append({"id": f"S{len(stmts) + 1}", "kind": kind, "text": c["text"], "claim_ids": [c["id"]], "evidence": [{"kind": e["kind"], "ref": e["ref"]} for e in c["evidence"]]})

    findings = []
    for c in ver:
        f = (c.get("data") or {}).get("finding")
        if f and c["type"] in ("FACT", "UNKNOWN"):
            findings.append(f)
    verified_rules = {f["rule_id"] for f in findings}
    applicable = [r[0] for r in rules.RULES if st in r[2]]
    unverified = sorted(set(applicable) - verified_rules)
    a = rules.assess(findings, st, has_links)
    limits = []
    if unverified:
        limits.append("These rules produced no verified result and were excluded from the score: " + ", ".join(unverified) + ".")
        if a["decision"] == "RELEASE":
            a["decision"] = "REVIEW"
            limits.append("Decision raised to REVIEW because part of the analysis could not be verified.")
    if a["ring"] == "UNKNOWN":
        limits.append("Whether a payout ring exists is UNKNOWN: no verified relationship or no shared device/IP data. Webhooks carry no device or IP; it must come from merchant session data.")
    limits.append("Thresholds are calibrated on synthetic data only; a FAIL is an indicator, never confirmed fraud.")

    sections = {"verified_facts": [], "verified_figures": [], "inferences": [], "assumptions": [], "unknown": []}
    key = {"FACT": "verified_facts", "FIGURE": "verified_figures", "INFERENCE": "inferences", "ASSUMPTION": "assumptions", "UNKNOWN": "unknown"}
    for c in ver:
        sections[key[c["type"]]].append(c["id"])
        stmt(c["type"].lower(), c)
    actions = [f["action"] for f in findings if f["outcome"] == "FAIL" and f.get("action")]
    if a["decision"] == "HOLD":
        nxt = "Submit for authorised human approval: cancel the UNCLAIMED payout item(s) in this case. No action is taken until a reviewer approves."
    elif a["decision"] == "REVIEW":
        nxt = "Human review recommended. " + " ".join(dict.fromkeys(actions)) if actions else "Human review recommended: supply the missing data listed below."
    else:
        nxt = "No payout action recommended. Indicators, if any, are listed for the record."
    missing = [{"rule_id": f["rule_id"], "why": f["why"]} for f in findings if f["outcome"] == "CANNOT"]
    summary = (f"{len(a['indicators'])} fraud indicator(s) verified; {len(missing)} check(s) could not run; {len(rej)} claim(s) rejected by reviewers. "
               f"Decision {a['decision']} is computed from verified rule results only, never from model text.")
    return {"subject": subject, "summary": summary,
            "risk": {**a, "confidence_limitations": limits, "recommended_next_action": nxt, "missing_information": missing},
            "sections": sections, "statements": stmts, "rejected_claims": [{"claim_id": c["id"], "text": c["text"], "objections": c["objections"]} for c in rej],
            "traceability": {"statements": len(stmts), "all_traced": all(s["claim_ids"] and all(next(c for c in claims if c["id"] == i)["status"] == "VERIFIED" for i in s["claim_ids"]) for s in stmts)},
            "findings": findings}


class Synthesizer(Agent):
    role = "final-synthesizer"

    def handle(self, kind, payload):
        return synthesize(payload["claims"], payload["subject"], payload["has_links"])
