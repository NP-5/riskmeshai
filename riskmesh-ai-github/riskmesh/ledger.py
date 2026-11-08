"""Evidence-based claim ledger.

Every conclusion is a Claim with an id, type, evidence, source, author, dependencies, status, reviewer objections and a
revision history. Only claims whose status is VERIFIED may be presented as established; the type says *what kind* of
statement it is (FACT, FIGURE, INFERENCE, ASSUMPTION, UNKNOWN) and is never silently upgraded.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

CLAIM_TYPES = ("FACT", "FIGURE", "INFERENCE", "ASSUMPTION", "UNKNOWN")
STATUSES = ("PROPOSED", "VERIFIED", "REJECTED")


@dataclass
class Claim:
    id: str
    text: str
    type: str
    author: str
    evidence: list = field(default_factory=list)          # [{"kind": "capture", "ref": "CAP1", "source": "payout bundle"}]
    source: str = ""                                      # supporting source in words (metric / API record)
    dependencies: list = field(default_factory=list)      # claim ids this claim builds on
    status: str = "PROPOSED"
    objections: list = field(default_factory=list)        # [{"by","round","text"}]
    revisions: list = field(default_factory=list)         # [{"round","by","text","change"}]
    assertion: Optional[dict] = None                      # machine-checkable form (see verify.py)
    rule_id: Optional[str] = None
    data: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


class Ledger:
    def __init__(self):
        self.claims: dict = {}
        self._n = 0

    def new(self, text, type_, author, **kw) -> Claim:
        if type_ not in CLAIM_TYPES:
            raise ValueError(f"claim type must be one of {CLAIM_TYPES}")
        self._n += 1
        c = Claim(id=f"CLM-{self._n:03d}", text=text, type=type_, author=author, **kw)
        self.claims[c.id] = c
        return c

    def add_from_dict(self, d: dict) -> Claim:
        """Re-create an agent-produced claim under a ledger-assigned id; dependency ids are remapped by the caller."""
        d = dict(d)
        d.pop("id", None)
        return self.new(d.pop("text"), d.pop("type"), d.pop("author"), **{k: v for k, v in d.items() if k in Claim.__dataclass_fields__})

    def object(self, cid, by, round_, text):
        self.claims[cid].objections.append({"by": by, "round": round_, "text": text})

    def verify(self, cid):
        self.claims[cid].status = "VERIFIED"

    def reject(self, cid):
        self.claims[cid].status = "REJECTED"

    def open_objections(self, cid, round_):
        return [o for o in self.claims[cid].objections if o["round"] == round_]

    def by_status(self, status):
        return [c for c in self.claims.values() if c.status == status]

    def to_list(self):
        return [c.to_dict() for c in self.claims.values()]

    def dumps(self):
        return json.dumps(self.to_list())
