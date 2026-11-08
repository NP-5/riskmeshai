"""Financial identity model, validation and Decimal money handling.

Entity kinds are kept strictly apart (an order id is never a payer id; a capture id is never a payout id):

    payer (customer)  --approves-->  ORDER  --captured as-->  CAPTURE  --paid to-->  MERCHANT (payee)
    PAYOUT BATCH  --contains-->  PAYOUT ITEM  --pays-->  RECIPIENT / BENEFICIARY account

A relationship between a capture and a payout item exists only if verified evidence says so (see ``verified_links``).
Anything that cannot be established is the string ``UNKNOWN``; it is never guessed.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

UNKNOWN = "UNKNOWN"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{2,63}$")
CURRENCY_EXP = {  # ISO 4217 codes PayPal supports for payments/payouts, with minor-unit exponent
    "AUD": 2, "BRL": 2, "CAD": 2, "CHF": 2, "CZK": 2, "DKK": 2, "EUR": 2, "GBP": 2, "HKD": 2, "HUF": 2, "ILS": 2, "JPY": 0,
    "MXN": 2, "NOK": 2, "NZD": 2, "PHP": 2, "PLN": 2, "SEK": 2, "SGD": 2, "THB": 2, "TWD": 2, "USD": 2, "INR": 2,
}
MAX_AMOUNT = Decimal("10000000")


class ValidationError(ValueError):
    pass


def is_unknown(v: Any) -> bool:
    return v is None or v == UNKNOWN or v == ""


def valid_id(value: Any, what: str = "id") -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ValidationError(f"invalid {what}")
    return value


def valid_currency(code: Any) -> str:
    if not isinstance(code, str) or code.upper() not in CURRENCY_EXP:
        raise ValidationError("unsupported or missing currency")
    return code.upper()


def parse_money(value: Any, currency: str) -> Decimal:
    """Parse a money string to Decimal, rejecting floats, NaN/Infinity, negatives, bad precision and out-of-range values."""
    cur = valid_currency(currency)
    if isinstance(value, float) or isinstance(value, bool) or value is None:
        raise ValidationError("amount must be a decimal string, not a float")
    text = str(value).strip()
    if not re.fullmatch(r"-?\d{1,12}(\.\d{1,6})?", text):        # plain digits only: no exponents, signs other than '-', spaces, NaN/Infinity
        raise ValidationError("amount is not a plain decimal number")
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValidationError("amount is not a number") from None
    if not d.is_finite():
        raise ValidationError("amount must be finite")
    exp = Decimal(1).scaleb(-CURRENCY_EXP[cur])
    if d != d.quantize(exp, rounding=ROUND_HALF_UP):
        raise ValidationError(f"amount has more decimal places than {cur} allows")
    if d < 0 or d > MAX_AMOUNT:
        raise ValidationError("amount out of range")
    return d.quantize(exp)


def money_or_unknown(value: Any, currency: Any) -> str:
    try:
        return str(parse_money(value, currency))
    except ValidationError:
        return UNKNOWN


def parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def minutes(value: Any) -> Optional[int]:
    dt = parse_time(value)
    return int(dt.timestamp() // 60) if dt else None


def hash_identifier(value: str, pepper: str) -> str:
    """Stable pseudonymous id for a recipient (email/phone/PayPal id) so raw PII never needs to be logged or graphed."""
    key = (pepper or "riskmesh-dev-pepper-not-for-production").encode()
    return "rcpt_" + hmac.new(key, value.strip().lower().encode(), hashlib.sha256).hexdigest()[:16]


def mask_identifier(value: str) -> str:
    v = value.strip()
    if "@" in v:
        name, _, dom = v.partition("@")
        return (name[:1] + "***@" + dom) if name else "***@" + dom
    return v[:2] + "***" + v[-2:] if len(v) > 6 else "***"


def verified_links(captures: list, items: list, explicit: list) -> list:
    """Build capture -> payout-item relationships from *evidence only*.

    A link is verified when (a) an authorised analyst recorded it, or (b) the merchant's own correlation key matches:
    payout ``sender_item_id`` equals the capture's ``invoice_id`` / ``custom_id`` / ``capture_id``.
    Matching amounts, timing or recipients alone never create a link.
    """
    cap_ids = {c["capture_id"] for c in captures}
    item_ids = {i["item_id"] for i in items}
    links, seen = [], set()

    def add(cid, iid, basis):
        if (cid, iid) not in seen and cid in cap_ids and iid in item_ids:
            seen.add((cid, iid))
            links.append({"capture_id": cid, "item_id": iid, "basis": basis, "verified": True})

    for l in explicit:
        add(l["capture_id"], l["item_id"], l.get("basis") or "ANALYST_RECORDED")
    for it in items:
        key = it.get("sender_item_id")
        if is_unknown(key):
            continue
        for c in captures:
            for field, basis in (("invoice_id", "SENDER_ITEM_ID=INVOICE_ID"), ("custom_id", "SENDER_ITEM_ID=CUSTOM_ID"), ("capture_id", "SENDER_ITEM_ID=CAPTURE_ID")):
                if not is_unknown(c.get(field)) and c[field] == key:
                    add(c["capture_id"], it["item_id"], basis)
    return links
