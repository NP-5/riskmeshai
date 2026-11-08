"""Evidence bundles: the single, JSON-serialisable input every agent and rule works from.

A bundle is built from the database (or from a dataset file for evaluation). It contains normalised, typed records plus
the *verified* capture->payout-item links. Fields that are not known are the string "UNKNOWN".
"""
from __future__ import annotations

from .financial import UNKNOWN, is_unknown, minutes, verified_links


def _t(created):
    return minutes(created) if not is_unknown(created) else None


def make_bundle(subject_type, subject_id, captures, payout_batches, payout_items, disputes, explicit_links=(), meta=None) -> dict:
    caps = []
    for c in captures:
        caps.append({**c, "t": _t(c.get("created_at")), "device": c.get("device") or UNKNOWN, "ip": c.get("ip") or UNKNOWN})
    items = [{**i, "t": _t(i.get("created_at"))} for i in payout_items]
    disp = [{**d, "t": _t(d.get("created_at"))} for d in disputes]
    return {"subject": {"type": subject_type, "id": subject_id}, "captures": caps, "payout_batches": list(payout_batches), "payout_items": items,
            "disputes": disp, "links": verified_links(caps, items, list(explicit_links)), "meta": meta or {}}


def _captures(c, where="", params=()):
    rows = c.all(f"""SELECT c.capture_id, c.order_id,
        CASE WHEN c.payer_id <> 'UNKNOWN' THEN c.payer_id ELSE COALESCE(o.payer_id, 'UNKNOWN') END AS payer_id,
        CASE WHEN c.merchant_id <> 'UNKNOWN' THEN c.merchant_id ELSE COALESCE(o.merchant_id, 'UNKNOWN') END AS merchant_id,
        c.amount, c.currency, c.status, c.created_at, c.invoice_id, c.custom_id, c.source,
        COALESCE(s.device, 'UNKNOWN') AS device, COALESCE(s.ip, 'UNKNOWN') AS ip
        FROM captures c LEFT JOIN orders o ON o.order_id = c.order_id LEFT JOIN sessions s ON s.order_id = c.order_id {where}""", params)
    return rows


def build_bundle(db, subject_type: str, subject_id: str) -> dict:
    """subject_type: beneficiary (recipient_id) | payout (batch id or item id) | transaction (capture id) | merchant (merchant id)."""
    with db.tx() as c:
        if subject_type == "transaction":
            caps = _captures(c, "WHERE c.capture_id = ?", (subject_id,))
            if not caps:
                raise LookupError("capture not found")
            m = caps[0]["merchant_id"]
            if not is_unknown(m):                      # context: the same merchant's other captures, for outlier/frequency analysis
                caps = _captures(c, "WHERE (CASE WHEN c.merchant_id <> 'UNKNOWN' THEN c.merchant_id ELSE COALESCE(o.merchant_id,'UNKNOWN') END) = ?", (m,))
            items, batches = [], []
        elif subject_type == "merchant":
            caps = _captures(c, "WHERE (CASE WHEN c.merchant_id <> 'UNKNOWN' THEN c.merchant_id ELSE COALESCE(o.merchant_id,'UNKNOWN') END) = ?", (subject_id,))
            if not caps:
                raise LookupError("merchant has no captures")
            items, batches = [], []
        elif subject_type in ("beneficiary", "payout"):
            if subject_type == "beneficiary":
                items = c.all("SELECT * FROM payout_items WHERE recipient_id = ?", (subject_id,))
            else:
                items = c.all("SELECT * FROM payout_items WHERE batch_id = ? OR item_id = ?", (subject_id, subject_id))
            if not items:
                raise LookupError("no payout items found for subject")
            bids = sorted({i["batch_id"] for i in items})
            batches, all_items = [], list(items)
            for b in bids:
                row = c.one("SELECT * FROM payout_batches WHERE batch_id = ?", (b,))
                if row:
                    batches.append(row)
                    if subject_type == "payout":        # batch reconciliation needs every item of the batch
                        have = {i["item_id"] for i in all_items}
                        all_items += [i for i in c.all("SELECT * FROM payout_items WHERE batch_id = ?", (b,)) if i["item_id"] not in have]
            items = all_items
            explicit = c.all("SELECT capture_id, item_id, basis FROM links WHERE item_id IN (%s)" % ",".join("?" * len(items)), [i["item_id"] for i in items])
            keys = {i["sender_item_id"] for i in items if not is_unknown(i["sender_item_id"])}
            linked_ids = {l["capture_id"] for l in explicit}
            caps = []
            ids = linked_ids | keys
            if ids:
                caps = _captures(c, "WHERE c.capture_id IN (%s) OR c.invoice_id IN (%s) OR c.custom_id IN (%s)" % ((",".join("?" * len(ids)),) * 3), [*ids, *ids, *ids])
            disp = c.all("SELECT * FROM disputes WHERE capture_id IN (%s)" % ",".join("?" * max(1, len(caps))), [x["capture_id"] for x in caps] or ["-"])
            return make_bundle(subject_type, subject_id, caps, batches, items, disp, explicit)
        else:
            raise ValueError("unknown subject type")
        disp = c.all("SELECT * FROM disputes WHERE capture_id IN (%s)" % ",".join("?" * max(1, len(caps))), [x["capture_id"] for x in caps] or ["-"])
    return make_bundle(subject_type, subject_id, caps, batches, items, disp)
