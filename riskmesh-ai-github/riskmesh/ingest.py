"""Webhook ingestion: validate -> verify signature -> de-duplicate -> persist, all before anything is analysed.

Verification modes recorded on every stored event:
  VERIFIED_BY_PAYPAL   PayPal's verify-webhook-signature API returned SUCCESS
  DEMO_UNVERIFIED      local demo mode only (RISKMESH_DEMO_MODE=1, never in production); clearly separated in the UI
"""
from __future__ import annotations

import json
from typing import Optional

from .config import Settings
from .db import Database, audit, utcnow
from .financial import UNKNOWN, ValidationError, hash_identifier, mask_identifier, money_or_unknown, valid_currency, valid_id
from .paypal import PayPalClient, PayPalError, PayPalNotConfigured, PayPalUnavailable

MAX_BODY = 1_000_000
EVENT_TYPES = {
    "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.PENDING", "PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED",
    "CHECKOUT.ORDER.APPROVED", "CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED",
    "PAYMENT.PAYOUTSBATCH.PROCESSING", "PAYMENT.PAYOUTSBATCH.SUCCESS", "PAYMENT.PAYOUTSBATCH.DENIED",
    "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", "PAYMENT.PAYOUTS-ITEM.FAILED", "PAYMENT.PAYOUTS-ITEM.BLOCKED", "PAYMENT.PAYOUTS-ITEM.CANCELED",
    "PAYMENT.PAYOUTS-ITEM.DENIED", "PAYMENT.PAYOUTS-ITEM.HELD", "PAYMENT.PAYOUTS-ITEM.REFUNDED", "PAYMENT.PAYOUTS-ITEM.RETURNED",
    "PAYMENT.PAYOUTS-ITEM.UNCLAIMED",
}


class WebhookRejected(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _g(d, *path, default=UNKNOWN):
    for p in path:
        if isinstance(d, list):
            d = d[p] if isinstance(p, int) and len(d) > p else None
        elif isinstance(d, dict):
            d = d.get(p)
        else:
            return default
        if d is None:
            return default
    return d


def _merge(conn, table, key, row):
    """Insert, or update an existing row without letting UNKNOWN overwrite a known value."""
    keycol = key[0]
    cur = conn.one(f"SELECT * FROM {table} WHERE {keycol} = ?", (row[keycol],))
    if cur is None:
        cols = list(row)
        conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])
        return "inserted"
    upd = {k: v for k, v in row.items() if k != keycol and v != UNKNOWN and v != cur.get(k)}
    if upd:
        conn.execute(f"UPDATE {table} SET {','.join(k + '=?' for k in upd)} WHERE {keycol} = ?", [*upd.values(), row[keycol]])
    return "updated" if upd else "unchanged"


def normalise(event: dict, s: Settings) -> dict:
    """Map a validated webhook event onto financial entities. Missing facts stay UNKNOWN."""
    et, r, src = event["event_type"], event["resource"], event["id"]
    out = {"orders": [], "captures": [], "disputes": [], "payout_batches": [], "payout_items": []}
    if et.startswith("PAYMENT.CAPTURE."):
        cur = _g(r, "amount", "currency_code")
        cid = valid_id(_g(r, "id", default=None), "capture id")
        out["captures"].append({
            "capture_id": cid, "order_id": _g(r, "supplementary_data", "related_ids", "order_id"), "payer_id": UNKNOWN, "merchant_id": UNKNOWN,
            "amount": money_or_unknown(_g(r, "amount", "value", default=None), cur if cur != UNKNOWN else None),
            "currency": valid_currency(cur) if cur != UNKNOWN else UNKNOWN, "status": str(_g(r, "status")),
            "created_at": str(_g(r, "create_time", default=event.get("create_time") or UNKNOWN)),
            "invoice_id": str(_g(r, "invoice_id")), "custom_id": str(_g(r, "custom_id")), "source_event_id": src, "source": "webhook"})
    elif et == "CHECKOUT.ORDER.APPROVED":
        pu = _g(r, "purchase_units", 0, default={})
        cur = _g(pu, "amount", "currency_code")
        out["orders"].append({
            "order_id": valid_id(_g(r, "id", default=None), "order id"), "payer_id": str(_g(r, "payer", "payer_id")),
            "merchant_id": str(_g(pu, "payee", "merchant_id")), "amount": money_or_unknown(_g(pu, "amount", "value", default=None), cur if cur != UNKNOWN else None),
            "currency": valid_currency(cur) if cur != UNKNOWN else UNKNOWN, "status": str(_g(r, "status")),
            "created_at": str(_g(r, "create_time", default=event.get("create_time") or UNKNOWN)), "source": "webhook"})
    elif et.startswith("CUSTOMER.DISPUTE."):
        cur = _g(r, "dispute_amount", "currency_code")
        out["disputes"].append({
            "dispute_id": valid_id(_g(r, "dispute_id", default=None), "dispute id"), "capture_id": str(_g(r, "disputed_transactions", 0, "seller_transaction_id")),
            "reason": str(_g(r, "reason")), "amount": money_or_unknown(_g(r, "dispute_amount", "value", default=None), cur if cur != UNKNOWN else None),
            "currency": valid_currency(cur) if cur != UNKNOWN else UNKNOWN, "status": str(_g(r, "status")),
            "created_at": str(_g(r, "create_time", default=event.get("create_time") or UNKNOWN)), "source": "webhook"})
    elif et.startswith("PAYMENT.PAYOUTSBATCH."):
        h = _g(r, "batch_header", default={})
        cur = _g(h, "amount", "currency")
        out["payout_batches"].append({
            "batch_id": valid_id(_g(h, "payout_batch_id", default=None), "payout batch id"), "status": str(_g(h, "batch_status")),
            "header_amount": money_or_unknown(_g(h, "amount", "value", default=None), cur if cur != UNKNOWN else None),
            "currency": valid_currency(cur) if cur != UNKNOWN else UNKNOWN, "created_at": str(_g(h, "time_created", default=event.get("create_time") or UNKNOWN)),
            "sender_batch_id": str(_g(h, "sender_batch_header", "sender_batch_id")), "source": "webhook"})
    elif et.startswith("PAYMENT.PAYOUTS-ITEM."):
        pi = _g(r, "payout_item", default={})
        cur = _g(pi, "amount", "currency")
        recv = _g(pi, "receiver", default=None)
        out["payout_items"].append({
            "item_id": valid_id(_g(r, "payout_item_id", default=None), "payout item id"), "batch_id": valid_id(_g(r, "payout_batch_id", default=None), "payout batch id"),
            "recipient_id": hash_identifier(str(recv), s.pepper) if recv else UNKNOWN, "recipient_display": mask_identifier(str(recv)) if recv else UNKNOWN,
            "amount": money_or_unknown(_g(pi, "amount", "value", default=None), cur if cur != UNKNOWN else None),
            "currency": valid_currency(cur) if cur != UNKNOWN else UNKNOWN, "status": str(_g(r, "transaction_status")),
            "sender_item_id": str(_g(pi, "sender_item_id")), "created_at": str(_g(r, "time_processed", default=event.get("create_time") or UNKNOWN)), "source": "webhook"})
    return out


def parse_event(raw: bytes) -> dict:
    if len(raw) > MAX_BODY:
        raise WebhookRejected(413, "payload too large")
    try:
        ev = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WebhookRejected(400, "body is not valid JSON") from None
    if not isinstance(ev, dict):
        raise WebhookRejected(400, "event must be a JSON object")
    try:
        valid_id(ev.get("id"), "event id")
    except ValidationError:
        raise WebhookRejected(400, "missing or invalid event id") from None
    if ev.get("event_type") not in EVENT_TYPES:
        raise WebhookRejected(422, "unsupported event_type")
    if not isinstance(ev.get("resource"), dict):
        raise WebhookRejected(400, "missing resource object")
    return ev


def verify(s: Settings, client: PayPalClient, headers: dict, event: dict) -> str:
    """Return the verification mode, or raise WebhookRejected. Fails closed in production."""
    has_sig = any(k.lower().startswith("paypal-transmission") for k in headers)
    if s.paypal_configured and s.paypal_webhook_id:
        try:
            ok = client.verify_webhook(headers, event)
        except (PayPalUnavailable, PayPalError):
            raise WebhookRejected(503, "signature verification is temporarily unavailable") from None   # 5xx => PayPal retries later
        if not ok:
            raise WebhookRejected(401, "webhook signature verification failed")
        return "VERIFIED_BY_PAYPAL"
    if s.demo_mode and not s.production:
        if has_sig:
            raise WebhookRejected(503, "signed event received but PayPal verification is not configured")
        return "DEMO_UNVERIFIED"
    raise WebhookRejected(503, "webhook verification is not configured; refusing to process events")


def process_webhook(db: Database, s: Settings, client: PayPalClient, headers: dict, raw: bytes) -> dict:
    ev = parse_event(raw)
    mode = verify(s, client, headers, ev)
    try:
        ent = normalise(ev, s)
    except ValidationError as exc:
        with db.tx() as c:
            audit(c, "paypal-webhook", "system", "webhook.rejected", ev["id"], "invalid", {"reason": str(exc), "type": ev["event_type"]})
        raise WebhookRejected(422, f"invalid payload: {exc}") from None
    with db.tx() as c:
        cur = c.execute("INSERT INTO webhook_events (event_id, event_type, received_at, verification, payload_json, summary_json) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT (event_id) DO NOTHING",
                        (ev["id"], ev["event_type"], utcnow(), mode, json.dumps(ev), json.dumps({k: len(v) for k, v in ent.items() if v})))
        if cur.rowcount == 0:
            audit(c, "paypal-webhook", "system", "webhook.duplicate", ev["id"], "ignored", {"type": ev["event_type"]})
            return {"status": "duplicate", "event_id": ev["id"]}
        for table, key in (("orders", "order_id"), ("captures", "capture_id"), ("disputes", "dispute_id"), ("payout_batches", "batch_id"), ("payout_items", "item_id")):
            for row in ent[table]:
                _merge(c, table, (key,), row)
        audit(c, "paypal-webhook", "system", "webhook.accepted", ev["id"], mode, {"type": ev["event_type"]})
    return {"status": "accepted", "event_id": ev["id"], "verification": mode, "entities": {k: len(v) for k, v in ent.items() if v}}


def enrich_capture(db: Database, client: PayPalClient, capture_id: str) -> dict:
    """Fill payer/merchant for a capture from PayPal's Orders API (webhooks do not carry them). Never guesses."""
    with db.tx() as c:
        cap = c.one("SELECT * FROM captures WHERE capture_id = ?", (capture_id,))
    if not cap or cap["order_id"] == UNKNOWN:
        return {"enriched": False, "reason": "capture or order id unknown"}
    try:
        o = client.get_order(cap["order_id"])
    except (PayPalError, PayPalUnavailable, PayPalNotConfigured) as exc:
        with db.tx() as c:
            audit(c, "system", "system", "capture.enrich", capture_id, "failed", {"error": type(exc).__name__})
        return {"enriched": False, "reason": type(exc).__name__}
    pu = (o.get("purchase_units") or [{}])[0]
    payer = (o.get("payer") or {}).get("payer_id") or UNKNOWN
    merchant = (pu.get("payee") or {}).get("merchant_id") or UNKNOWN
    with db.tx() as c:
        _merge(c, "captures", ("capture_id",), {"capture_id": capture_id, "payer_id": payer, "merchant_id": merchant})
        audit(c, "system", "system", "capture.enrich", capture_id, "ok", {"payer_known": payer != UNKNOWN, "merchant_known": merchant != UNKNOWN})
    return {"enriched": True, "payer_known": payer != UNKNOWN, "merchant_known": merchant != UNKNOWN}
