"""Read-only PayPal Agent Toolkit for RiskMesh AI (disputes and order lookups, sandbox only).
Follows the Python setup in PayPal's agent toolkit quickstart: pip install paypal-agent-toolkit.
Writes (the payout hold) stay in app.py behind reviewer approval."""
import os

def build_toolkit():
    from paypal_agent_toolkit.openai.toolkit import PayPalToolkit
    from paypal_agent_toolkit.shared.configuration import Configuration, Context
    cfg = Configuration(actions={"disputes": {"list": True, "get": True}, "orders": {"get": True}}, context=Context(sandbox=True))
    return PayPalToolkit(client_id=os.environ["PAYPAL_CLIENT_ID"], secret=os.environ["PAYPAL_CLIENT_SECRET"], configuration=cfg)

SYSTEM_PROMPT = ("You assist a payment-risk reviewer. Use only read tools. Summarise dispute evidence for the beneficiary in question, "
                 "never request, approve or perform a payout action, and say when data is missing.")

def tool_names():
    try: return [getattr(t, "name", str(t)) for t in build_toolkit().get_tools()]
    except Exception as e: return {"available": False, "reason": type(e).__name__}
