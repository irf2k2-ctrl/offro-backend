"""routers/public_pricing.py - read-only, country-specific platform prices for the mobile apps: GET /country-pricing/{code}.

Reads the SAME data the Pricing & Plans dashboard writes (services/pricing_config.read_prices) and returns what a client may show.
It is additive: no existing endpoint, checkout or Razorpay call changes, and nothing here creates an order.

Rules
  * The country in the URL is the only country used. A country that is unknown, inactive or has no tax configured is answered with a
    clear error / `configured: false`; India's prices or GST are NEVER substituted.
  * A section is listed only when it is configured and active; otherwise it is `{"configured": false}` (not zero, not India's value).
  * `payment.supported` tells the app whether OffrO can charge by Razorpay in this country's currency. India: always. Another country: only
    when its currency is listed in RAZORPAY_INTERNATIONAL_CURRENCIES; otherwise `mode` is "manual" (admin-confirmed activation) and `reason`
    says why. Nothing assumes a currency is supported.
"""
from fastapi import APIRouter, HTTPException

from services import country_context as CC
from services import pricing_config as P

router = APIRouter(tags=["Pricing (public)"])


def _err(status: int, code: str, message: str):
    raise HTTPException(status, {"code": code, "message": message})


def _row(r: dict, keys: tuple) -> dict:
    if not r.get("configured") or not r.get("active"):
        return {"configured": bool(r.get("configured")), "active": bool(r.get("active"))}
    return {"configured": True, "active": True, **{k: r.get(k) for k in keys}}


@router.get("/country-pricing/{code}")
def country_pricing(code: str):
    iso = (code or "").strip().upper()
    if len(iso) != 2 or not iso.isalpha():
        _err(422, "invalid_country", "Country must be a 2-letter ISO code")
    c = P.get_country(iso)
    if not c:
        _err(404, "pricing_not_configured", "Pricing is not available for this country")
    if c.get("status") != "active":
        _err(404, "country_inactive", "Pricing is not available for this country")
    r = P.read_prices(iso)
    tax = r["tax"]
    money = ("price", "tax_amount", "total")
    plans = [{"id": p["id"], "label": p["label"], **_row(p, money)} for p in r["store_plans"]]
    prod = _row(r["product"], money + ("free_product_limit", "product_max_validity_days"))
    inf = _row(r["influencer"], money)
    if iso == P.INDIA:
        pay = {"supported": True, "mode": "razorpay", "currency": "INR", "reason": None}
    else:
        route = CC.payment_route(c["currency_code"], True)
        pay = {"supported": route["mode"] == "razorpay", "mode": route["mode"], "currency": c["currency_code"],
               "reason": route["manual_reason"]}
    return {
        "country": {k: c[k] for k in ("code", "name", "currency_code", "currency_symbol")},
        "tax": {"label": tax["label"], "percent": tax["percent"], "tax_free": tax["tax_free"], "configured": tax["configured"],
                "message": tax["error"]},
        "store_plans": plans,
        "influencer": inf,
        "banner": _row(r["banner"], money),
        "product": prod,
        "payment": pay,
    }
