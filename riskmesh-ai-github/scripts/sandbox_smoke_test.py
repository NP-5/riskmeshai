"""Sandbox loop smoke test using RiskMesh's own PayPal client (no third-party packages).

    PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... python scripts/sandbox_smoke_test.py

Creates an order, prints the approve link for your Personal (buyer) sandbox account, waits for Enter, then captures.
Refuses to run against a non-sandbox base URL. NOT run in this repository's automated tests (needs credentials and network).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from riskmesh.config import load_settings
from riskmesh.paypal import PayPalClient, PayPalError

s = load_settings()
if not s.paypal_configured or not s.sandbox:
    raise SystemExit("Set PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET and use the sandbox PAYPAL_BASE")
c = PayPalClient(s)
print("health:", c.health())
try:
    o = c.create_order("25.00", invoice_id="SMOKE-1")
    print("Order", o["id"], "\nApprove as the BUYER at:", next(l["href"] for l in o["links"] if l["rel"] == "payer-action"))
    input("Press Enter after approving...")
    print("capture status:", c.capture_order(o["id"]).get("status"))
except PayPalError as e:
    print("PayPal refused:", e.to_dict())
