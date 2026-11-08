"""Agent runtime: structured JSON messages over an in-process async bus, with task ids, agent ids, timeouts and retries."""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import asdict, dataclass, field

_seq = itertools.count(1)


@dataclass
class Message:
    msg_id: str
    task_id: str
    sender: str
    recipient: str
    kind: str                     # TASK | RESULT | ERROR | REVIEW | REVISE | STOP
    payload: dict
    ts: float = field(default_factory=time.time)
    in_reply_to: str = ""


class Bus:
    def __init__(self):
        self.queues: dict = {}
        self.log: list = []
        self.waiting: dict = {}

    def register(self, agent_id: str):
        self.queues[agent_id] = asyncio.Queue()

    def message(self, task_id, sender, recipient, kind, payload, in_reply_to="") -> Message:
        json.dumps(payload)        # messages must be structured JSON: raises on anything else
        return Message(f"M{next(_seq):05d}", task_id, sender, recipient, kind, payload, in_reply_to=in_reply_to)

    async def send(self, m: Message):
        self.log.append(m)
        if m.in_reply_to and m.in_reply_to in self.waiting:
            fut = self.waiting.pop(m.in_reply_to)
            if not fut.done():
                fut.set_result(m)
        elif m.recipient in self.queues:
            await self.queues[m.recipient].put(m)

    def trace(self, max_payload=400):
        out = []
        for m in self.log:
            d = asdict(m)
            s = json.dumps(m.payload)
            d["payload"] = m.payload if len(s) <= max_payload else {"_truncated": True, "bytes": len(s), "keys": list(m.payload)[:8]}
            out.append(d)
        return out


class Agent:
    """Base worker. Subclasses implement ``handle(kind, payload) -> dict`` (blocking; run in a thread)."""
    role = "agent"
    prompt = ""

    def __init__(self, agent_id: str):
        self.id = agent_id
        self.executions: list = []

    def handle(self, kind: str, payload: dict) -> dict:
        raise NotImplementedError

    async def serve(self, bus: Bus):
        q = bus.queues[self.id]
        while True:
            m = await q.get()
            if m.kind == "STOP":
                return
            t0 = time.perf_counter()
            rec = {"task_id": m.task_id, "msg_id": m.msg_id, "kind": m.kind, "started": time.time()}
            try:
                out = await asyncio.to_thread(self.handle, m.kind, m.payload)
                rec["status"] = "ok"
                reply = bus.message(m.task_id, self.id, m.sender, "RESULT", out, in_reply_to=m.msg_id)
            except Exception as exc:                       # an agent failure becomes an ERROR message, never a crash
                rec["status"], rec["error"] = "error", f"{type(exc).__name__}: {exc}"[:200]
                reply = bus.message(m.task_id, self.id, m.sender, "ERROR", {"error": rec["error"]}, in_reply_to=m.msg_id)
            rec["duration_ms"] = int((time.perf_counter() - t0) * 1000)
            self.executions.append(rec)
            await bus.send(reply)
