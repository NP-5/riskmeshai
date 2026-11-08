"""Minimal in-process ASGI test client (no httpx/starlette needed)."""
import asyncio
import json
from urllib.parse import urlsplit


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body or b"{}")


def request(app, method, url, body=None, headers=None, raw=None, client=("127.0.0.1", 5000)):
    u = urlsplit(url)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": u.path, "query_string": u.query.encode(), "headers": hdrs, "client": client}
    out = {"body": b""}

    async def run():
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": data, "more_body": False}

        async def send(m):
            if m["type"] == "http.response.start":
                out["status"], out["headers"] = m["status"], {k.decode(): v.decode() for k, v in m["headers"]}
            elif m["type"] == "http.response.body":
                out["body"] += m.get("body", b"")
        await app(scope, receive, send)
    asyncio.run(run())
    return Resp(out["status"], out["headers"], out["body"])
