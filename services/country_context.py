"""services/country_context.py - which country's OffrO platform prices apply to a signed-in account, and how it may be charged.

Used by the merchant (store plans, banners, products) and influencer (subscription) payment flows.

Country
  * Always worked out ON THE SERVER from the authenticated account (utils.countries.account_country: `phone_country`, else the stored
    phone). A country sent by the app is never used to choose prices.
  * India (IN) returns None: every caller then runs its existing code, byte for byte, on the existing `pricing` document.
  * Any other country is priced ONLY from that country's own Pricing & Plans configuration (services.pricing_config). A country that is
    unknown/inactive, or whose tax or price is not configured, is refused with a clear 409 - India's prices or GST are never used instead.
  * An account with no phone_country whose phone is not an international number is a legacy India-era account (international accounts are
    always created with a validated number and phone_country), so it keeps the existing India flow.

Payment
  * Razorpay is used for a non-India order only when its currency is listed in RAZORPAY_INTERNATIONAL_CURRENCIES (comma separated ISO 4217
    codes, empty by default = none) AND the Razorpay keys exist. Otherwise the order is created in the existing "manual" mode (admin
    confirms the payment) with `manual_reason` saying why. Nothing here assumes a currency is supported.
"""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

from fastapi import HTTPException

from services import pricing_config as P
from utils import countries as _countries

# Currencies whose smallest Razorpay unit is not 1/100. Everything else is 1/100.
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF", "XPF"}
_THREE_DECIMAL = {"BHD", "JOD", "KWD", "OMR", "TND"}      # amounts are sent in 1/1000 and must end in 0

_current: ContextVar = ContextVar("offro_country_ctx", default=None)


def _err(code: str, message: str, status: int = 409):
    raise HTTPException(status, {"code": code, "message": message})


def international_currencies() -> set:
    raw = os.environ.get("RAZORPAY_INTERNATIONAL_CURRENCIES", "")
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


def to_minor(amount: float, currency: str = "INR") -> int:
    """Amount in the currency's smallest Razorpay unit. INR keeps the existing int(round(total*100))."""
    cur = (currency or "INR").upper()
    if cur in _ZERO_DECIMAL:
        return int(round(amount))
    if cur in _THREE_DECIMAL:
        return int(round(amount * 100)) * 10
    return int(round(amount * 100))


def payment_route(currency: str, keys_present: bool = True) -> dict:
    """{'mode': 'razorpay'|'manual', 'manual_reason': None|str} for a NON-India currency."""
    cur = (currency or "").upper()
    if cur in international_currencies() and keys_present:
        return {"mode": "razorpay", "manual_reason": None}
    reason = "currency_not_enabled" if cur not in international_currencies() else "payment_gateway_not_configured"
    return {"mode": "manual", "manual_reason": reason}


def resolve(account: dict) -> Optional[dict]:
    """None for India. For another country a dict:
       {iso, name, currency_code, currency_symbol, tax_label, tax_percent, tax_free, prices (read_prices output)}.
    Raises 409 with {code, message} when pricing cannot be used."""
    info = _countries.account_country(account)
    if not info:
        # No phone_country and a phone that does not parse as an international number. International accounts are only ever created with
        # a validated number (which also stores phone_country), so an account like this is a LEGACY (India-only era) account: India's
        # existing flow applies, exactly as before. This is not a fallback for a missing country configuration - that is refused below.
        return None
    iso = info["iso"]
    if iso == P.INDIA:
        return None
    c = P.get_country(iso)
    if not c:
        _err("pricing_not_configured", f"Pricing is not available for {info['name']} yet.")
    if c.get("status") != "active":
        _err("country_inactive", f"Pricing is not available for {info['name']} right now.")
    r = P.read_prices(iso)
    tax = r["tax"]
    if not tax["configured"]:
        _err("tax_not_configured", f"Tax is not set up for {info['name']} yet, so a total cannot be shown.")
    return {"iso": iso, "name": c["name"], "currency_code": c["currency_code"], "currency_symbol": c["currency_symbol"],
            "tax_label": tax["label"], "tax_percent": float(tax["percent"] or 0.0), "tax_free": bool(tax["tax_free"]), "prices": r}


def resolve_for_admin(account: Optional[dict]) -> Optional[dict]:
    """resolve() for the ADMIN dashboard: same rules, but the 409 carries a plain-text detail (the dashboard shows `detail` as text)."""
    if not account:
        return None
    try:
        return resolve(account)
    except HTTPException as e:
        d = e.detail
        raise HTTPException(e.status_code, d.get("message") if isinstance(d, dict) else d)


def require_section(ctx: dict, section: str, label: str) -> dict:
    sec = ctx["prices"][section]
    if not sec.get("configured") or not sec.get("active"):
        _err("price_not_configured", f"{label} pricing is not set up for {ctx['name']} yet.")
    return sec


def tax_amount(base: float, ctx: dict) -> float:
    return round(base * ctx["tax_percent"] / 100, 2)


def labels(ctx: Optional[dict]) -> dict:
    """Currency/tax fields stored on orders, subscriptions, invoices and returned to the app. India's defaults are the old values."""
    if not ctx:
        return {"currency_code": "INR", "currency_symbol": "₹", "tax_label": "GST"}
    return {"currency_code": ctx["currency_code"], "currency_symbol": ctx["currency_symbol"], "tax_label": ctx["tax_label"]}


# ── request-scoped context for code that is shared with the India path (the product service) ───────────────────────────────────────────
def current() -> Optional[dict]:
    return _current.get()


@contextmanager
def activated(ctx: Optional[dict]):
    tok = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(tok)


def product_settings(ctx: dict) -> dict:
    """Same shape as services.products.get_settings(), from the country's own product configuration."""
    p = require_section(ctx, "product", "Product")
    if p.get("free_product_limit") is None or p.get("product_max_validity_days") is None or p.get("price") is None:
        _err("price_not_configured", f"Product settings are not complete for {ctx['name']}.")
    return {"free_product_limit": int(p["free_product_limit"]), "product_price_per_day": float(p["price"]),
            "product_max_validity_days": int(p["product_max_validity_days"]), "gst_percent": ctx["tax_percent"]}


def stamp(src: Optional[dict]) -> dict:
    """currency / tax label recorded on an order, subscription or invoice. A record without them is an India record (the old values)."""
    s = src or {}
    return {"currency_code": s.get("currency_code") or "INR", "currency_symbol": s.get("currency_symbol") or "₹",
            "tax_label": s.get("tax_label") or "GST"}
