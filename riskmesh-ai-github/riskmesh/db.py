"""Persistent storage: SQLite for local development, PostgreSQL for deployment.

* Every write goes through ``Database.tx()``: one transaction, committed on success and rolled back on any exception.
* SQLite uses ``BEGIN IMMEDIATE`` (one writer at a time, WAL for readers); PostgreSQL uses its normal MVCC transactions.
* Concurrency-sensitive state changes (webhook de-duplication, action approval/execution) are single conditional
  statements (``INSERT ... ON CONFLICT DO NOTHING`` / ``UPDATE ... WHERE status = ?``) whose row count decides the winner.
* SQL is written once with ``?`` placeholders and translated for PostgreSQL. PostgreSQL support needs ``psycopg``
  (``pip install "psycopg[binary]"``) and has not been exercised in this repository's automated tests.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS webhook_events (event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, received_at TEXT NOT NULL,
        verification TEXT NOT NULL, payload_json TEXT NOT NULL, summary_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, payer_id TEXT NOT NULL, merchant_id TEXT NOT NULL, amount TEXT NOT NULL,
        currency TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, source TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS captures (capture_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, payer_id TEXT NOT NULL, merchant_id TEXT NOT NULL,
        amount TEXT NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, invoice_id TEXT NOT NULL,
        custom_id TEXT NOT NULL, source_event_id TEXT, source TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sessions (order_id TEXT PRIMARY KEY, device TEXT NOT NULL, ip TEXT NOT NULL, source TEXT NOT NULL, recorded_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS payout_batches (batch_id TEXT PRIMARY KEY, status TEXT NOT NULL, header_amount TEXT NOT NULL, currency TEXT NOT NULL,
        created_at TEXT NOT NULL, sender_batch_id TEXT NOT NULL, source TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS payout_items (item_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, recipient_id TEXT NOT NULL, recipient_display TEXT NOT NULL,
        amount TEXT NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL, sender_item_id TEXT NOT NULL, created_at TEXT NOT NULL, source TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS disputes (dispute_id TEXT PRIMARY KEY, capture_id TEXT NOT NULL, reason TEXT NOT NULL, amount TEXT NOT NULL,
        currency TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, source TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS links (id INTEGER PRIMARY KEY AUTOINCREMENT, capture_id TEXT NOT NULL, item_id TEXT NOT NULL, basis TEXT NOT NULL,
        created_by TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (capture_id, item_id))""",
    """CREATE TABLE IF NOT EXISTS investigations (id TEXT PRIMARY KEY, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, status TEXT NOT NULL,
        created_by TEXT NOT NULL, created_at TEXT NOT NULL, risk_level TEXT NOT NULL, risk_score INTEGER NOT NULL, recommendation TEXT NOT NULL,
        bundle_json TEXT NOT NULL, report_json TEXT NOT NULL, claims_json TEXT NOT NULL, trace_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL, action_type TEXT NOT NULL, target_id TEXT NOT NULL,
        status TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, requested_by TEXT NOT NULL, created_at TEXT NOT NULL, decided_by TEXT,
        decided_role TEXT, decided_at TEXT, decision_reason TEXT, executed_at TEXT, mode TEXT, paypal_http_status INTEGER, paypal_response_json TEXT,
        verification_json TEXT)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_actions_open_target ON actions (action_type, target_id) WHERE status IN ('PENDING','APPROVED','EXECUTING','EXECUTED','SIMULATED')""",
    """CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL,
        action TEXT NOT NULL, target TEXT NOT NULL, outcome TEXT NOT NULL, detail_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS eval_runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, results_json TEXT NOT NULL)""",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Conn:
    """Thin wrapper so callers use one SQL dialect and get dict rows."""

    def __init__(self, raw, pg: bool):
        self.raw, self.pg = raw, pg

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg else sql

    def execute(self, sql: str, params=()):
        cur = self.raw.cursor() if self.pg else self.raw
        cur = cur.execute(self._sql(sql), tuple(params))
        return cur

    def one(self, sql, params=()):
        cur = self.execute(sql, params)
        row = cur.fetchone()
        return self._row(cur, row) if row is not None else None

    def all(self, sql, params=()):
        cur = self.execute(sql, params)
        return [self._row(cur, r) for r in cur.fetchall()]

    @staticmethod
    def _row(cur, row):
        if isinstance(row, sqlite3.Row):
            return dict(row)
        if isinstance(row, dict):
            return row
        return {d[0]: v for d, v in zip(cur.description, row)}


class Database:
    def __init__(self, url: str):
        self.url = url
        self.pg = url.startswith("postgresql://")
        self._lock = threading.RLock()
        self._mem = None
        if self.pg:
            try:
                import psycopg  # noqa: F401
            except ImportError as exc:
                raise RuntimeError('PostgreSQL requires psycopg: pip install "psycopg[binary]"') from exc
        elif url.startswith("sqlite:///"):
            self.path = url[len("sqlite:///"):] or "riskmesh.db"
            if self.path == ":memory:":
                self._mem = self._connect_sqlite(":memory:")
        else:
            raise ValueError("DATABASE_URL must start with sqlite:/// or postgresql://")
        self.init_schema()

    def _connect_sqlite(self, path):
        c = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        if path != ":memory:":
            c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _schema_sql(self, stmt):
        if self.pg:
            stmt = stmt.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        return stmt

    def init_schema(self):
        with self.tx() as c:
            for stmt in SCHEMA:
                c.execute(self._schema_sql(stmt))

    @contextmanager
    def tx(self):
        """One atomic transaction. Serialised across threads for SQLite; MVCC for PostgreSQL."""
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            raw = psycopg.connect(self.url, row_factory=dict_row)
            try:
                yield Conn(raw, True)
                raw.commit()
            except BaseException:
                raw.rollback()
                raise
            finally:
                raw.close()
            return
        with self._lock:
            raw = self._mem or self._connect_sqlite(self.path)
            raw.execute("BEGIN IMMEDIATE")
            try:
                yield Conn(raw, False)
                raw.execute("COMMIT")
            except BaseException:
                raw.execute("ROLLBACK")
                raise
            finally:
                if raw is not self._mem:
                    raw.close()

    def ping(self) -> bool:
        try:
            with self.tx() as c:
                c.one("SELECT 1 AS ok")
            return True
        except Exception:
            return False


SENSITIVE_KEYS = re.compile(r"secret|token|authorization|password|api[_-]?key|cert|signature|email|receiver|phone", re.I)


def redact(obj, depth=0):
    """Remove credentials and personal payment identifiers from anything that is about to be logged."""
    if depth > 6:
        return "…"
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if SENSITIVE_KEYS.search(str(k)) else redact(v, depth + 1)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v, depth + 1) for v in obj[:50]]
    if isinstance(obj, str):
        return re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", obj)[:500]
    return obj


def audit(conn: Conn, actor: str, role: str, action: str, target: str, outcome: str, detail=None):
    """Append-only audit record. Call inside the same transaction as the change it describes."""
    conn.execute("INSERT INTO audit_log (ts, actor, role, action, target, outcome, detail_json) VALUES (?,?,?,?,?,?,?)",
                 (utcnow(), actor, role, action, target, outcome, json.dumps(redact(detail or {}), default=str)))
