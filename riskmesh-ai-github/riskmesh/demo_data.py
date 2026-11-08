"""Seed data for local demos and tests. Everything inserted here has ``source = 'demo'`` and is synthetic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from .financial import hash_identifier, mask_identifier

BASE_T = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def iso(minutes: int) -> str:
    return (BASE_T + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def insert(conn, table: str, rows: list) -> None:
    for r in rows:
        cols = list(r)
        conn.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [r[c] for c in cols])


def build_scenario(name, recipient, payers, pepper="", amounts=None, devices=None, ips=None, minutes_apart=3, item_status="UNCLAIMED",
                   with_sessions=True, header_delta="0.00", disputes=0, memo_note=None, start=0, merchant=None):
    """Return table rows for one scenario: N payers -> one recipient, with verified capture->payout links."""
    n = len(payers)
    amounts = amounts or ["25.00"] * n
    rows = {"orders": [], "captures": [], "sessions": [], "payout_batches": [], "payout_items": [], "disputes": []}
    merchant = merchant or f"MERCH-{name}"
    for i, p in enumerate(payers):
        oid, cid, inv = f"ORD-{name}-{i + 1:02d}", f"CAP-{name}-{i + 1:02d}", f"INV-{name}-{i + 1:02d}"
        t = iso(start + i * minutes_apart)
        rows["orders"].append({"order_id": oid, "payer_id": p, "merchant_id": merchant, "amount": amounts[i], "currency": "USD", "status": "COMPLETED", "created_at": t, "source": "demo"})
        rows["captures"].append({"capture_id": cid, "order_id": oid, "payer_id": p, "merchant_id": merchant, "amount": amounts[i], "currency": "USD", "status": "COMPLETED",
                                 "created_at": t, "invoice_id": inv, "custom_id": "UNKNOWN", "source_event_id": None, "source": "demo"})
        if with_sessions:
            rows["sessions"].append({"order_id": oid, "device": (devices or [f"dev-{name}-{i}"] * n)[i] if devices else f"dev-{name}-{i}",
                                     "ip": (ips[i] if ips else f"203.0.113.{10 + i}"), "source": "demo", "recorded_at": t})
        rows["payout_items"].append({"item_id": f"ITEM-{name}-{i + 1:02d}", "batch_id": f"BATCH-{name}", "recipient_id": hash_identifier(recipient, pepper),
                                     "recipient_display": mask_identifier(recipient), "amount": amounts[i], "currency": "USD", "status": item_status,
                                     "sender_item_id": inv, "created_at": iso(start + n * minutes_apart + 5 + i), "source": "demo"})
    total = sum((D(a) for a in amounts), D(0)) + D(header_delta)
    rows["payout_batches"].append({"batch_id": f"BATCH-{name}", "status": "PROCESSING", "header_amount": f"{total:.2f}", "currency": "USD",
                                   "created_at": iso(start + n * minutes_apart + 4), "sender_batch_id": f"SB-{name}", "source": "demo"})
    for k in range(disputes):
        rows["disputes"].append({"dispute_id": f"DSP-{name}-{k + 1:02d}", "capture_id": f"CAP-{name}-{k + 1:02d}", "reason": "UNAUTHORISED", "amount": amounts[k],
                                 "currency": "USD", "status": "OPEN", "created_at": iso(start + n * minutes_apart + 30), "source": "demo"})
    return rows


def seed(db, pepper="") -> dict:
    """Insert three demo subjects and return {name: recipient_id (hashed)}."""
    ring = build_scenario("RING", "ring-beneficiary@example.test", [f"PAYER-R{i}" for i in range(1, 8)], pepper,
                          devices=["dev-A", "dev-A", "dev-A", "dev-B", "dev-B", "dev-C", "dev-D"],
                          ips=["198.51.100.7", "198.51.100.7", "198.51.100.8", "198.51.100.9", "198.51.100.9", "198.51.100.10", "198.51.100.11"], disputes=2)
    clean = build_scenario("CLEAN", "clean-seller@example.test", ["PAYER-C1", "PAYER-C2"], pepper, amounts=["120.00", "75.50"], minutes_apart=240, start=500)
    nosess = build_scenario("NOSESS", "nosession@example.test", [f"PAYER-N{i}" for i in range(1, 7)], pepper, with_sessions=False, start=900)
    ids = {}
    with db.tx() as c:
        for key, sc in (("ring", ring), ("clean", clean), ("nosession", nosess)):
            for table in ("orders", "captures", "sessions", "payout_batches", "payout_items", "disputes"):
                insert(c, table, sc[table])
            ids[key] = sc["payout_items"][0]["recipient_id"]
    return ids
