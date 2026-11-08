"""Sandbox data path: PayPal API responses -> the same entity rows a webhook would produce.

Why this exists: webhooks cannot reach a laptop without a public URL, so a freshly created sandbox payment would never
become evidence. Here the data comes straight from PayPal's *authenticated* API responses to our own calls (capture,
payout-batch read). They are written with ``source = 'paypal-api'`` so they stay distinguishable from webhooks, and they use
exactly the same normalisation rules: a fact PayPal did not give us stays ``UNKNOWN`` and is never guessed.

PayPal webhooks (when you do expose one) merge into the very same rows, because ``_merge`` never lets UNKNOWN overwrite a
known value and keys are PayPal's own ids.

Linking rule (matches ``bundles.verified_links``): a payout item is tied to a payment only when its ``sender_item_id`` equals
that capture's ``invoice_id``. Create orders with an ``invoice_id`` and pay them out with the same value as ``sender_item_id``.
"""
from __future__ import annotations

from typing import Optional

from .config import Settings
from .db import Database, audit
from .financial import (UNKNOWN, ValidationError, hash_identifier, is_unknown, mask_identifier, money_or_unknown, parse_money,
                        valid_currency, valid_id)
from .ingest import _g, _merge

SOURCE = "paypal-api"
MAX_PAYOUT_ITEMS = 25


# ---- parsing -------------------------------------------------------------------------------------------------------------
def capture_rows(resp: dict, order: Optional[dict] = None) -> dict:
    """Rows from a capture-order response (``POST /v2/checkout/orders/{id}/capture``), optionally enriched by ``GET order``.

    payer_id comes from ``payer.payer_id`` and merchant_id from ``purchase_units[0].payee.merchant_id``; if neither response
    carries them they stay UNKNOWN (the caller may then read the order, as ``enrich_capture`` does for webhooks)."""
    order = order or {}
    oid = valid_id(_g(resp, "id", default=None), "order id")
    pu = _g(resp, "purchase_units", 0, default={}) or {}
    pu_o = _g(order, "purchase_units", 0, default={}) or {}
    payer = str(_g(resp, "payer", "payer_id", default=_g(order, "payer", "payer_id")))
    merchant = str(_g(pu, "payee", "merchant_id", default=_g(pu_o, "payee", "merchant_id")))
    caps = _g(pu, "payments", "captures", default=[]) or []
    out = {"orders": [], "captures": []}
    created = UNKNOWN
    for c in caps:
        cur = _g(c, "amount", "currency_code")
        created = str(_g(c, "create_time"))
        out["captures"].append({
            "capture_id": valid_id(_g(c, "id", default=None), "capture id"), "order_id": oid, "payer_id": payer, "merchant_id": merchant,
            "amount": money_or_unknown(_g(c, "amount", "value", default=None), cur if cur != UNKNOWN else None),
            "currency": valid_currency(cur) if cur != UNKNOWN else UNKNOWN, "status": str(_g(c, "status")), "created_at": created,
            # the capture carries its own invoice_id; fall back to the purchase unit's (PayPal puts it on both)
            "invoice_id": str(_g(c, "invoice_id", default=_g(pu, "invoice_id"))), "custom_id": str(_g(c, "custom_id", default=_g(pu, "custom_id"))),
            "source_event_id": f"api:{_g(c, 'id', default='?')}", "source": SOURCE})
    cur0 = _g(pu, "amount", "currency_code")
    out["orders"].append({
        "order_id": oid, "payer_id": payer, "merchant_id": merchant,
        "amount": money_or_unknown(_g(pu, "amount", "value", default=None), cur0 if cur0 != UNKNOWN else None),
        "currency": valid_currency(cur0) if cur0 != UNKNOWN else UNKNOWN, "status": str(_g(resp, "status")), "created_at": created, "source": SOURCE})
    return out


def payout_rows(batch: dict, s: Settings) -> dict:
    """Rows from a payout batch response (create or ``GET /v1/payments/payouts/{id}``). Items are present on the read."""
    h = _g(batch, "batch_header", default={}) or {}
    bid = valid_id(_g(h, "payout_batch_id", default=None), "payout batch id")
    cur = _g(h, "amount", "currency")
    out = {"payout_batches": [{
        "batch_id": bid, "status": str(_g(h, "batch_status")),
        "header_amount": money_or_unknown(_g(h, "amount", "value", default=None), cur if cur != UNKNOWN else None),
        "currency": valid_currency(cur) if cur != UNKNOWN else UNKNOWN, "created_at": str(_g(h, "time_created")),
        "sender_batch_id": str(_g(h, "sender_batch_header", "sender_batch_id")), "source": SOURCE}], "payout_items": []}
    for it in batch.get("items") or []:
        pi = _g(it, "payout_item", default={}) or {}
        icur = _g(pi, "amount", "currency")
        recv = _g(pi, "receiver", default=None)
        out["payout_items"].append({
            "item_id": valid_id(_g(it, "payout_item_id", default=None), "payout item id"), "batch_id": bid,
            "recipient_id": hash_identifier(str(recv), s.pepper) if recv else UNKNOWN,
            "recipient_display": mask_identifier(str(recv)) if recv else UNKNOWN,
            "amount": money_or_unknown(_g(pi, "amount", "value", default=None), icur if icur != UNKNOWN else None),
            "currency": valid_currency(icur) if icur != UNKNOWN else UNKNOWN, "status": str(_g(it, "transaction_status")),
            "sender_item_id": str(_g(pi, "sender_item_id")), "created_at": str(_g(it, "time_processed", default=_g(h, "time_created"))), "source": SOURCE})
    return out


# ---- persistence ---------------------------------------------------------------------------------------------------------
def ingest_rows(db: Database, rows: dict, actor: str, role: str, what: str) -> dict:
    """Merge rows (never letting UNKNOWN overwrite a known value) and audit it. Returns counts per table."""
    keys = {"orders": "order_id", "captures": "capture_id", "payout_batches": "batch_id", "payout_items": "item_id"}
    counts = {}
    with db.tx() as c:
        for table in ("orders", "captures", "payout_batches", "payout_items"):    # order matters only for readability
            for row in rows.get(table, []):
                _merge(c, table, (keys[table],), row)
            if rows.get(table):
                counts[table] = len(rows[table])
        audit(c, actor, role, "sandbox.ingest", what, "ok", counts)
    return counts


def unknown_facts(rows: dict) -> list:
    """Which facts PayPal did not give us (so callers/UI can say so instead of pretending)."""
    miss = set()
    for cap in rows.get("captures", []):
        for k in ("payer_id", "merchant_id", "invoice_id", "amount"):
            if is_unknown(cap.get(k)):
                miss.add(k)
    return sorted(miss)


# ---- request validation for the payout helper ----------------------------------------------------------------------------
def validate_payout_request(body: dict) -> dict:
    """Strict validation of ``POST /api/sandbox/payout``. Raises ValidationError with a safe message."""
    items = body.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_PAYOUT_ITEMS:
        raise ValidationError(f"items must be a list of 1..{MAX_PAYOUT_ITEMS} payout items")
    cur = valid_currency(body.get("currency", "USD"))
    clean = []
    seen = set()
    for n, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise ValidationError(f"item {n} must be an object")
        recv = it.get("receiver")
        if not isinstance(recv, str) or "@" not in recv or len(recv) > 127 or any(ch.isspace() for ch in recv):
            raise ValidationError(f"item {n}: receiver must be an email address")
        # the link to a payment is sender_item_id == capture.invoice_id, so accept either name for the same value
        sid = valid_id(it.get("sender_item_id") or it.get("invoice_id"), f"item {n} sender_item_id")
        if sid in seen:
            raise ValidationError(f"item {n}: duplicate sender_item_id {sid!r} in one batch")
        seen.add(sid)
        clean.append({"receiver": recv.strip(), "sender_item_id": sid, "amount": str(parse_money(it.get("amount"), cur))})
    bid = body.get("sender_batch_id")
    return {"items": clean, "currency": cur, "note": str(body.get("note", ""))[:200],
            "sender_batch_id": valid_id(bid, "sender_batch_id") if bid else None}
