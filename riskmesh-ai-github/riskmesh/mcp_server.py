"""RiskMesh MCP server (stdio). Run: ``python -m riskmesh.mcp_server``  (needs ``pip install "mcp>=1.2,<2"``).

Deliberately limited: tools can look at subjects, run an investigation and read results. **There is no tool to approve or
execute a payout hold.** Approval stays a human action in the dashboard/API, so an MCP client cannot move or freeze money.
The tool bodies are plain functions in ``TOOLS`` so they are unit-tested without the MCP SDK.
"""
from __future__ import annotations

from .config import load_settings
from .db import Database
from .paypal import PayPalClient
from .service import Service, ServiceError

IDENTITY, ROLE = "mcp-client", "analyst"
_svc = None


def svc() -> Service:
    global _svc
    if _svc is None:
        s = load_settings()
        _svc = Service(s, Database(s.db_url), PayPalClient(s))
    return _svc


def _guard(fn):
    def run(*a, **k):
        try:
            return fn(*a, **k)
        except ServiceError as e:
            return {"error": e.message, "status": e.status}
    run.__doc__, run.__name__ = fn.__doc__, fn.__name__
    return run


@_guard
def list_subjects() -> dict:
    """List payout beneficiaries (hashed ids with masked display) that can be investigated."""
    return {"subjects": svc().subjects()}


@_guard
def investigate(subject_type: str, subject_id: str) -> dict:
    """Run the multi-agent investigation for a beneficiary, payout batch/item, transaction or merchant. Returns the risk report
    (verified facts, figures, inferences, assumptions, UNKNOWN, decision from rule results only). A HOLD only creates PENDING requests for a human."""
    r = svc().investigate(subject_type, subject_id, IDENTITY, ROLE, "via MCP")
    return {k: r[k] for k in ("id", "recommendation", "risk_score", "risk_level", "ring", "summary", "proposed_actions")} | {"report": r["report"]}


@_guard
def get_investigation(investigation_id: str) -> dict:
    """Read a stored investigation, its claims (with evidence and status) and its action requests."""
    return svc().get_investigation(investigation_id)


@_guard
def get_trace(investigation_id: str) -> dict:
    """The agent execution trace: plan, per-task status, review rounds, messages."""
    return svc().get_investigation(investigation_id, trace=True)["trace"]


@_guard
def list_pending_actions() -> dict:
    """Hold requests waiting for a human reviewer. Read-only: this server cannot approve them."""
    return {"actions": svc().list_actions("PENDING")}


TOOLS = {f.__name__: f for f in (list_subjects, investigate, get_investigation, get_trace, list_pending_actions)}


def main():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit('Install the MCP SDK first:  pip install "mcp>=1.2,<2"') from exc
    m = FastMCP("riskmesh")
    for f in TOOLS.values():
        m.tool()(f)
    m.run()


if __name__ == "__main__":
    main()
