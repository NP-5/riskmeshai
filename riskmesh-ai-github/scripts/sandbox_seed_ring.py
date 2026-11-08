"""Build a REAL PayPal-sandbox payout ring and investigate it with RiskMesh.

Prerequisites (see docs/sandbox_runbook.md):
  * RiskMesh running with PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET for a SANDBOX app and an admin API token
  * N distinct PERSONAL sandbox buyer accounts (default 5). Each order must be approved by a DIFFERENT buyer, because the
    ring rule counts distinct payers.

    python scripts/sandbox_seed_ring.py --api http://localhost:8000 --token $RISKMESH_ADMIN_TOKEN

What it does, in order (every PayPal call goes through RiskMesh's own sandbox-only endpoints):
  1. create N orders, each with a unique invoice_id          POST /api/sandbox/order
  2. you approve each as a different Personal buyer          (browser, www.sandbox.paypal.com)
  3. capture it and record the session (device + IP)         POST /api/sandbox/capture/{order}   <- PayPal webhooks carry neither
  4. pay everything out to ONE recipient with no PayPal account, sender_item_id == invoice_id     POST /api/sandbox/payout
     (a payout item links to its payment ONLY when sender_item_id == the capture's invoice_id;
      an email with no PayPal account leaves the item UNCLAIMED, the only status PayPal's cancel call works on)
  5. investigate the recipient                               POST /api/investigations

Nothing here talks to a live PayPal host: the server refuses these endpoints unless its base URL is the sandbox.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
import uuid


def make_plan(n: int, run_id: str, shared_device: int, shared_ip: bool, control: bool) -> list:
    """Who pays, from which device/IP. The first ``shared_device`` payers share one device; all share one IP unless disabled."""
    plan = []
    for i in range(1, n + 1):
        plan.append({"label": f"payer {i}", "invoice_id": f"RM-{run_id}-{i:02d}", "ring": True,
                     "device": "DEV-RING-A" if i <= shared_device else f"DEV-OWN-{run_id}-{i:02d}",
                     "ip": "198.51.100.7" if shared_ip else f"203.0.113.{i}"})     # RFC 5737 documentation addresses
    if control:                                              # an unrelated buyer paying a different recipient: should NOT be in the ring
        plan.append({"label": "control buyer", "invoice_id": f"RM-{run_id}-C01", "ring": False,
                     "device": f"DEV-CTRL-{run_id}", "ip": "203.0.113.200"})
    return plan


def http_call(base: str, token: str):
    def call(method: str, path: str, body=None):
        req = urllib.request.Request(base.rstrip("/") + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
    return call


class Abort(Exception):
    pass


def _ok(status, body, what):
    if status != 200:
        raise Abort(f"{what} failed (HTTP {status}): {body.get('error') or body}")
    return body


def run(a, call, ask=input, out=print, sleep=time.sleep) -> dict:
    run_id = a.run_id or uuid.uuid4().hex[:6]
    if a.payers < 5:
        out(f"NOTE: the ring rule needs >= 5 distinct payers into one recipient; {a.payers} will not form a ring.")
    plan = make_plan(a.payers, run_id, a.shared_device, not a.no_shared_ip, a.control)
    out(f"Run {run_id}: {len(plan)} orders, amount {a.amount} {a.currency} each, ring recipient {a.recipient}")
    captured, seen_payers = [], {}
    for n, p in enumerate(plan, 1):
        o = _ok(*call("POST", "/api/sandbox/order", {"amount": a.amount, "currency": a.currency, "invoice_id": p["invoice_id"], "custom_id": p["invoice_id"]}), "create order")
        out(f"\n[{n}/{len(plan)}] {p['label']}: open this link in a PRIVATE window and approve as a DIFFERENT Personal sandbox buyer:\n    {o.get('approve')}")
        ask("    Press Enter after approving... ")
        body = None if a.no_sessions else {"device": p["device"], "ip": p["ip"]}
        c = _ok(*call("POST", f"/api/sandbox/capture/{o['order_id']}", body), "capture")
        if not c.get("ingested"):
            raise Abort(f"capture succeeded but could not be ingested: {c.get('ingest_error')}")
        ref = c.get("payer_ref")
        if ref and ref in seen_payers:
            out(f"    WARNING: this is the SAME PayPal account as {seen_payers[ref]}. Fan-in counts distinct payers, so use a different buyer.")
        seen_payers.setdefault(ref, p["label"])
        if c.get("unknown"):
            out(f"    PayPal did not give us: {', '.join(c['unknown'])} (stays UNKNOWN; never guessed)")
        out(f"    captured {c['capture_ids']} status={c['status']} session={'recorded' if c['session_recorded'] else 'NOT recorded'}")
        captured.append({**p, "order_id": o["order_id"], "capture_ids": c["capture_ids"], "payer_ref": ref})
    items = [{"receiver": a.recipient if p["ring"] else a.control_recipient, "amount": a.amount, "sender_item_id": p["invoice_id"]} for p in plan]
    out(f"\nCreating payout batch ({len(items)} items; sender_item_id == invoice_id so each item links to its payment)...")
    pb = _ok(*call("POST", "/api/sandbox/payout", {"sender_batch_id": f"RMB-{run_id}", "currency": a.currency, "items": items}), "payout batch")
    for _ in range(a.sync_tries):
        if pb["items"] and all(i["status"] not in ("PENDING", "UNKNOWN") for i in pb["items"]):
            break
        sleep(a.sync_wait)
        pb = _ok(*call("POST", f"/api/sandbox/payout/{pb['batch_id']}/sync"), "payout sync")
    statuses = sorted({i["status"] for i in pb["items"]})
    out(f"    batch {pb['batch_id']} {pb['status']}; item statuses: {statuses}; unclaimed: {pb['unclaimed']}/{len(pb['items'])}")
    if pb["unclaimed"] < sum(1 for p in plan if p["ring"]):
        out("    WARNING: not all ring items are UNCLAIMED, so they cannot be cancelled (HOLD). Use a recipient email that has NO PayPal account.")
    result = {"run_id": run_id, "captured": captured, "payout": pb}
    if not a.investigate:
        return result
    subs = _ok(*call("GET", "/api/subjects"), "subjects")["subjects"]
    ring_sub = max(subs, key=lambda x: x["items"], default=None)
    if not ring_sub:
        raise Abort("no payout recipients are known yet")
    inv = _ok(*call("POST", "/api/investigations", {"subject_type": "beneficiary", "subject_id": ring_sub["subject_id"]}), "investigation")
    out(f"\nINVESTIGATION of {ring_sub['display']} ({ring_sub['items']} items): recommendation={inv.get('recommendation')} ring={inv.get('ring')} "
        f"proposed_actions={len(inv.get('proposed_actions', []))}")
    if inv.get("recommendation") != "HOLD":
        why = "no device/IP session was recorded" if a.no_sessions else "fewer than 5 distinct payers, no shared device/IP, or items not linked/UNCLAIMED"
        out(f"    Not HOLD. Likely reason: {why}. Open the investigation in the dashboard for the exact rule trace.")
    else:
        out("    HOLD proposed. Nothing is cancelled until a human reviewer approves each action in the dashboard (/api/actions).")
    result["investigation"] = inv
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://localhost:8000"); ap.add_argument("--token", required=True, help="admin API token")
    ap.add_argument("--payers", type=int, default=5); ap.add_argument("--amount", default="25.00"); ap.add_argument("--currency", default="USD")
    ap.add_argument("--recipient", default=None, help="ring recipient email with NO PayPal account (default: generated example.com address)")
    ap.add_argument("--control-recipient", default=None); ap.add_argument("--run-id", default=None)
    ap.add_argument("--shared-device", type=int, default=3, help="how many of the payers share one device")
    ap.add_argument("--no-shared-ip", action="store_true"); ap.add_argument("--no-sessions", action="store_true", help="skip device/IP (shows live data stuck at REVIEW)")
    ap.add_argument("--control", action="store_true", help="add one unrelated buyer paying a different recipient")
    ap.add_argument("--no-investigate", dest="investigate", action="store_false"); ap.add_argument("--sync-tries", type=int, default=4); ap.add_argument("--sync-wait", type=float, default=3.0)
    a = ap.parse_args(argv)
    rid = a.run_id or uuid.uuid4().hex[:6]
    a.run_id, a.recipient = rid, a.recipient or f"ring-collector-{rid}@example.com"
    a.control_recipient = a.control_recipient or f"control-{rid}@example.com"
    try:
        run(a, http_call(a.api, a.token))
    except Abort as e:
        raise SystemExit(f"STOPPED: {e}")


if __name__ == "__main__":
    main()
