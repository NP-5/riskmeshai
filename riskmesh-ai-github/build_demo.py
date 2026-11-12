"""Seed data for local demos and tests. Everything inserted here has ``source = 'demo'`` and is synthetic."""
from __future__ import annotations

import os
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
    # Optional: show your own address (masked in the UI, hashed in the DB) as the ring recipient. Set RISKMESH_DEMO_RECIPIENT on the server.
    ring_recipient = (os.environ.get("RISKMESH_DEMO_RECIPIENT") or "").strip() or "ring-beneficiary@example.test"
    ring = build_scenario("RING", ring_recipient, [f"PAYER-R{i}" for i in range(1, 8)], pepper,
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


# ---- showcase scenarios: a wider mix of outcomes for the live demo (used by the demo seed endpoint only) ----
def showcase_specs(ring_recipient: str) -> list:
    return [
        ("ring_b", "exam-fee collector", dict(name="RINGB", recipient="exam-fees-desk@example.test", payers=[f"PAYER-B{i}" for i in range(1, 6)],
            amounts=["40.00", "35.00", "42.50", "38.00", "41.00"], devices=["dev-X", "dev-X", "dev-Y", "dev-Y", "dev-Z"],
            ips=["192.0.2.21", "192.0.2.21", "192.0.2.22", "192.0.2.23", "192.0.2.23"], minutes_apart=400, start=1200, item_gap=400)),
        ("marketplace", "busy marketplace seller", dict(name="MKT", recipient="handmade-goods-shop@example.test", payers=[f"PAYER-M{i}" for i in range(1, 13)],
            amounts=["18.00", "42.50", "27.00", "65.00", "12.75", "33.00", "88.00", "21.50", "54.00", "16.00", "47.25", "29.90"], minutes_apart=420, start=7000, item_gap=420)),
        ("split", "split payments, one device", dict(name="SPLIT", recipient="quick-cash-agent@example.test", payers=[f"PAYER-S{i}" for i in range(1, 5)],
            amounts=["49.00", "49.00", "49.00", "49.00"], devices=["dev-S", "dev-S", "dev-S", "dev-S"], ips=["198.18.0.5"] * 4, minutes_apart=6, start=3800)),
        ("freelancer", "regular freelancer", dict(name="FREE", recipient="design-freelancer@example.test", payers=["PAYER-F1", "PAYER-F2", "PAYER-F3"],
            amounts=["300.00", "450.00", "275.00"], minutes_apart=600, start=4200)),
    ]


def build_showcase(kw: dict, pepper: str = "") -> dict:
    kw = dict(kw)
    gap = kw.pop("item_gap", None)
    name, recipient, payers = kw.pop("name"), kw.pop("recipient"), kw.pop("payers")
    sc = build_scenario(name, recipient, payers, pepper, **kw)
    if gap:
        n, base = len(payers), kw.get("start", 0) + len(payers) * kw.get("minutes_apart", 3) + 5
        for i, it in enumerate(sc["payout_items"]):
            it["created_at"] = iso(base + i * gap)
    return sc


def seed_showcase(db, pepper: str = "") -> dict:
    ids = {}
    with db.tx() as c:
        for key, _label, kw in showcase_specs(""):
            sc = build_showcase(kw, pepper)
            for table in ("orders", "captures", "sessions", "payout_batches", "payout_items", "disputes"):
                insert(c, table, sc[table])
            ids[key] = sc["payout_items"][0]["recipient_id"]
    return ids
