"""PayPal REST client (sandbox by default). Standard library only, so it is easy to audit and to test with a fake transport.

Retry policy (this matters for money movement):
* GET and the signature-verification POST are retried on network errors, 429 and 5xx with exponential backoff.
* A POST that moves money is retried only if it carries a ``PayPal-Request-Id`` (PayPal de-duplicates on it).
  The payout-item *cancel* call has no idempotency key, so it is **never** retried automatically: after a timeout the
  outcome is UNCERTAIN and the caller must re-read the item before doing anything else.
* Nothing here ever fabricates a response. Network failures raise ``PayPalUnavailable``; HTTP errors raise ``PayPalError``
  carrying PayPal's ``debug_id``.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from base64 import b64encode
from typing import Callable, Optional
from urllib.parse import urlparse

from .config import Settings


class PayPalError(Exception):
    def __init__(self, status: int, issue: Optional[str], debug_id: Optional[str], body: dict):
        super().__init__(f"PayPal HTTP {status} {issue or ''}".strip())
        self.status, self.issue, self.debug_id, self.body = status, issue, debug_id, body

    def to_dict(self):
        return {"ok": False, "status": self.status, "issue": self.issue, "debug_id": self.debug_id}


class PayPalUnavailable(Exception):
    """Network failure/timeout: the request may or may not have reached PayPal."""


class PayPalNotConfigured(Exception):
    pass


Transport = Callable[[str, str, dict, Optional[bytes], float], tuple]


def urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes], timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        raise PayPalUnavailable(type(e).__name__) from e


CERT_HOST = re.compile(r"^[a-z0-9.-]+\.paypal\.com$")


class PayPalClient:
    def __init__(self, settings: Settings, transport: Transport = urllib_transport, sleep: Callable[[float], None] = time.sleep):
        self.s, self._t, self._sleep = settings, transport, sleep
        self._tok, self._tok_exp, self._lock = None, 0.0, threading.Lock()
        self.calls = []        # (method, path, http_status) log for the dashboard; contains no secrets or bodies

    # ---- plumbing -------------------------------------------------------------------------------------------------
    def _require(self):
        if not self.s.paypal_configured:
            raise PayPalNotConfigured("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set")

    def _send(self, method, path, *, json_body=None, form=None, headers=None, retry=False, auth=True, basic=None):
        url = self.s.paypal_base + path
        h = {"Accept": "application/json", **(headers or {})}
        body = None
        if json_body is not None:
            body, h["Content-Type"] = json.dumps(json_body).encode(), "application/json"
        elif form is not None:
            body, h["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
        if basic:
            h["Authorization"] = "Basic " + b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
        elif auth:
            h["Authorization"] = "Bearer " + self.token()
        attempts = 1 + (self.s.http_retries if retry else 0)
        last = None
        for n in range(attempts):
            try:
                status, _, raw = self._t(method, url, h, body, self.s.http_timeout)
            except PayPalUnavailable as e:
                last = e
                self.calls.append((method, path, "network-error"))
                if n + 1 < attempts:
                    self._sleep(min(2 ** n * 0.5, 4))
                    continue
                raise
            self.calls.append((method, path, status))
            if retry and (status == 429 or status >= 500) and n + 1 < attempts:
                self._sleep(min(2 ** n * 0.5, 4))
                continue
            try:
                data = json.loads(raw.decode() or "{}")
            except ValueError:
                data = {}
            if status >= 400:
                issue = ((data.get("details") or [{}])[0] or {}).get("issue") or data.get("name")
                raise PayPalError(status, issue, data.get("debug_id"), data)
            return status, data
        raise last or PayPalUnavailable("no attempt made")

    def token(self) -> str:
        self._require()
        with self._lock:
            if self._tok and time.time() < self._tok_exp - 30:
                return self._tok
            _, j = self._send("POST", "/v1/oauth2/token", form={"grant_type": "client_credentials"}, auth=False,
                              basic=(self.s.paypal_client_id, self.s.paypal_client_secret), retry=True)
            self._tok, self._tok_exp = j["access_token"], time.time() + int(j.get("expires_in", 300))
            return self._tok

    def _mock(self, mock: Optional[str]):
        """PayPal-Mock-Response is a sandbox-only negative-testing feature; refuse it elsewhere."""
        if not mock:
            return {}
        if not self.s.sandbox:
            raise ValueError("mock responses are only allowed against the PayPal sandbox")
        return {"PayPal-Mock-Response": json.dumps({"mock_application_codes": mock})}

    # ---- Orders / captures ----------------------------------------------------------------------------------------
    def create_order(self, amount: str, currency: str = "USD", invoice_id: str = "", custom_id: str = "", request_id: Optional[str] = None):
        unit = {"amount": {"currency_code": currency, "value": amount}}
        if invoice_id:
            unit["invoice_id"] = invoice_id
        if custom_id:
            unit["custom_id"] = custom_id
        _, j = self._send("POST", "/v2/checkout/orders", json_body={"intent": "CAPTURE", "purchase_units": [unit]},
                          headers={"PayPal-Request-Id": request_id or str(uuid.uuid4())}, retry=True)
        return j

    def get_order(self, order_id):
        return self._send("GET", f"/v2/checkout/orders/{urllib.parse.quote(order_id, safe='')}", retry=True)[1]

    def capture_order(self, order_id, request_id: Optional[str] = None, mock: Optional[str] = None):
        h = {"PayPal-Request-Id": request_id or str(uuid.uuid4()), "Content-Type": "application/json", **self._mock(mock)}
        return self._send("POST", f"/v2/checkout/orders/{urllib.parse.quote(order_id, safe='')}/capture", headers=h, retry=True)[1]

    def get_capture(self, capture_id):
        return self._send("GET", f"/v2/payments/captures/{urllib.parse.quote(capture_id, safe='')}", retry=True)[1]

    # ---- Webhooks -------------------------------------------------------------------------------------------------
    def verify_webhook(self, headers: dict, event: dict) -> bool:
        """PayPal's official verification API. True only for verification_status == SUCCESS."""
        self._require()
        if not self.s.paypal_webhook_id:
            raise PayPalNotConfigured("PAYPAL_WEBHOOK_ID is not set")
        h = {k.lower(): v for k, v in headers.items()}
        need = ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id", "paypal-transmission-sig", "paypal-transmission-time")
        if any(not h.get(k) for k in need):
            return False
        host = urlparse(h["paypal-cert-url"])
        if host.scheme != "https" or not CERT_HOST.match(host.hostname or ""):
            return False                                   # never make PayPal (or us) fetch a certificate from a foreign host
        body = {"auth_algo": h["paypal-auth-algo"], "cert_url": h["paypal-cert-url"], "transmission_id": h["paypal-transmission-id"],
                "transmission_sig": h["paypal-transmission-sig"], "transmission_time": h["paypal-transmission-time"],
                "webhook_id": self.s.paypal_webhook_id, "webhook_event": event}
        _, j = self._send("POST", "/v1/notifications/verify-webhook-signature", json_body=body, retry=True)
        return j.get("verification_status") == "SUCCESS"

    def list_webhooks(self):
        return self._send("GET", "/v1/notifications/webhooks", retry=True)[1]

    def register_webhook(self, url: str, event_types: list):
        return self._send("POST", "/v1/notifications/webhooks", json_body={"url": url, "event_types": [{"name": n} for n in event_types]}, retry=False)[1]

    # ---- Payouts --------------------------------------------------------------------------------------------------
    def create_payout_batch(self, sender_batch_id: str, items: list, currency: str = "USD", note: str = ""):
        """items: [{"receiver": email, "amount": "10.00", "sender_item_id": "..."}]. Sandbox use; moves money when live."""
        body = {"sender_batch_header": {"sender_batch_id": sender_batch_id, "email_subject": "Payout", **({"email_message": note} if note else {})},
                "items": [{"recipient_type": "EMAIL", "receiver": i["receiver"], "sender_item_id": i["sender_item_id"],
                           "amount": {"value": i["amount"], "currency": currency}} for i in items]}
        return self._send("POST", "/v1/payments/payouts", json_body=body, headers={"PayPal-Request-Id": sender_batch_id}, retry=True)[1]

    def get_payout_batch(self, batch_id):
        return self._send("GET", f"/v1/payments/payouts/{urllib.parse.quote(batch_id, safe='')}", retry=True)[1]

    def get_payout_item(self, item_id):
        return self._send("GET", f"/v1/payments/payouts-item/{urllib.parse.quote(item_id, safe='')}", retry=True)[1]

    def cancel_payout_item(self, item_id, mock: Optional[str] = None):
        """Cancels an UNCLAIMED payout item. Supported by PayPal only for unclaimed items. Never retried automatically."""
        return self._send("POST", f"/v1/payments/payouts-item/{urllib.parse.quote(item_id, safe='')}/cancel", headers=self._mock(mock), retry=False)

    def health(self) -> dict:
        if not self.s.paypal_configured:
            return {"status": "not_configured", "detail": "PAYPAL_CLIENT_ID/SECRET missing"}
        try:
            self.token()
            return {"status": "ok", "base": self.s.paypal_base, "sandbox": self.s.sandbox}
        except PayPalError as e:
            return {"status": "error", "http": e.status, "issue": e.issue}
        except PayPalUnavailable as e:
            return {"status": "unreachable", "detail": str(e)}
