"""services/pricing_config.py - country-wise platform pricing, tax and reward-conversion configuration (Pricing & Plans module).

Scope: OffrO's OWN platform charges only (store plans, influencer plan, banners, product day price and free-product window) plus the
reward-redemption conversion. Merchant product prices are never touched here, and nothing here creates a Razorpay order.

Storage
  India (IN)  reads and writes the EXISTING single `pricing` document, with the same field names and the same defaults the live
              app uses (plans, gst_percent, banner_price_per_day, product_price_per_day, free_product_limit, product_max_validity_days,
              influencer_subscription, conversion_rate, min_withdraw_points). Nothing is copied or migrated, so India behaves exactly as
              before; this module only edits those fields when an admin saves.
  other       `pricing_countries` (country master: code, name, currency, tax label / rate) and `country_prices` (one document per
              country holding its independent prices). A section that was never saved is "not configured": no other country's value is
              ever substituted, and no exchange rate is ever used.
  history     `pricing_history` (who changed what, old and new value).

Nothing here is read by checkout yet. Wiring checkout to these values is a separate, separately approved step.
"""
import math
import re
from datetime import datetime
from typing import Optional

from fastapi import HTTPException

from utils import iso_codes as _iso

INDIA = "IN"
PLAN_IDS = (("1month", "1 Month"), ("3months", "3 Months"), ("6months", "6 Months"), ("12months", "12 Months"))
INDIA_DEFAULT_PLANS = [{"id": "1month", "label": "1 Month", "price": 499}, {"id": "3months", "label": "3 Months", "price": 1299},
                       {"id": "6months", "label": "6 Months", "price": 2299}, {"id": "12months", "label": "12 Months", "price": 3999}]
SECTIONS = ("store_plans", "influencer", "banner", "product", "reward")
MAX_PRICE = 10_000_000
_CODE_RE = re.compile(r"^[A-Z]{2}$")
_CUR_RE = re.compile(r"^[A-Z]{3}$")


class _LiveDb:
    """The current `database.db`, resolved at call time (same reason as services.dashboard_country)."""
    def __getattr__(self, name):
        import database
        return getattr(database.db, name)


db = _LiveDb()


def _bad(msg: str, code: str = "invalid"):
    raise HTTPException(422, {"code": code, "message": msg})


# ── value validation ──────────────────────────────────────────────────────────────────────────────────────────────────
def money(v, field: str) -> float:
    if isinstance(v, bool) or v is None or (isinstance(v, str) and not v.strip()):
        _bad(f"{field} is required", "required")
    try:
        x = float(v)
    except (TypeError, ValueError):
        _bad(f"{field} must be a number")
    if not math.isfinite(x) or x < 0 or x > MAX_PRICE:
        _bad(f"{field} must be between 0 and {MAX_PRICE}")
    return round(x, 3)


def whole(v, field: str, minimum: int) -> int:
    if isinstance(v, bool) or v is None or (isinstance(v, str) and not v.strip()):
        _bad(f"{field} is required", "required")
    try:
        x = float(v)
    except (TypeError, ValueError):
        _bad(f"{field} must be a whole number")
    if not math.isfinite(x) or x != int(x):
        _bad(f"{field} must be a whole number")
    if int(x) < minimum or int(x) > 1_000_000:
        _bad(f"{field} must be at least {minimum}")
    return int(x)


def percent(v, field: str = "Tax rate") -> float:
    if isinstance(v, bool) or v is None or (isinstance(v, str) and not v.strip()):
        _bad(f"{field} is required", "required")
    try:
        x = float(v)
    except (TypeError, ValueError):
        _bad(f"{field} must be a number")
    if not math.isfinite(x) or x < 0 or x > 100:
        _bad(f"{field} must be between 0 and 100")
    return round(x, 4)


def text(v, field: str, lo: int, hi: int) -> str:
    s = v.strip() if isinstance(v, str) else ""
    if not s:
        _bad(f"{field} is required", "required")
    if len(s) < lo or len(s) > hi:
        _bad(f"{field} must be {lo} to {hi} characters")
    if any(ord(c) < 32 for c in s) or "<" in s or ">" in s:
        _bad(f"{field} contains characters that are not allowed")
    return s


def clean_code(v, iso: bool = False) -> str:
    """Normalise a country code. iso=True (adding a country) also requires a real ISO 3166-1 alpha-2 code; lookups only need the
    format so that an unknown code answers 404, not a validation error."""
    c = v.strip().upper() if isinstance(v, str) else ""
    if not c:
        _bad("Country code is required", "required")
    if not _CODE_RE.match(c):
        _bad("Country code must be a two-letter ISO code such as IN, SA or JO", "invalid_country_code")
    if iso and not _iso.is_country_code(c):
        _bad(f"{c} is not a valid ISO 3166-1 country code", "invalid_country_code")
    return c


def clean_currency_code(v) -> str:
    c = v.strip().upper() if isinstance(v, str) else ""
    if not c:
        _bad("Currency code is required", "required")
    if not _CUR_RE.match(c):
        _bad("Currency code must be three letters such as INR, SAR or JOD", "invalid_currency_code")
    if not _iso.is_currency_code(c):
        _bad(f"{c} is not a valid ISO 4217 currency code", "invalid_currency_code")
    return c


# ── country master ────────────────────────────────────────────────────────────────────────────────────────────────────
def _pricing_doc() -> dict:
    return db.pricing.find_one({}) or {}


def india_country() -> dict:
    doc = _pricing_doc()
    gst = doc.get("gst_percent", 18)
    return {"code": INDIA, "name": "India", "currency_name": "Indian Rupee", "currency_code": "INR", "currency_symbol": "₹",
            "tax_label": "GST", "tax_percent": float(gst) if gst is not None else None, "tax_free": False, "status": "active",
            "built_in": True}


def _public_country(d: dict) -> dict:
    return {"code": d["code"], "name": d["name"], "currency_name": d["currency_name"], "currency_code": d["currency_code"],
            "currency_symbol": d["currency_symbol"], "tax_label": d.get("tax_label") or "",
            "tax_percent": d.get("tax_percent"), "tax_free": bool(d.get("tax_free")),
            "status": d.get("status", "active"), "built_in": False}


def get_country(code: str) -> Optional[dict]:
    code = (code or "").strip().upper()
    if code == INDIA:
        return india_country()
    d = db.pricing_countries.find_one({"code": code})
    return _public_country(d) if d else None


def list_countries() -> list:
    out = [india_country()]
    for d in db.pricing_countries.find({}).sort("name", 1):
        if d.get("code") != INDIA:
            out.append(_public_country(d))
    return out


def tax_state(country: dict) -> dict:
    """{label, percent, tax_free, configured, error}. A country with neither a rate nor an explicit tax-free setting is NOT
    configured: callers show the error and never substitute another country's rate."""
    label = country.get("tax_label") or ""
    pct = country.get("tax_percent")
    free = bool(country.get("tax_free"))
    if free:
        return {"label": label or "No tax", "percent": 0.0, "tax_free": True, "configured": True, "error": None}
    if pct is None or not label:
        return {"label": label, "percent": None, "tax_free": False, "configured": False,
                "error": f"Tax is not configured for {country.get('name', 'this country')}. Set a tax label and rate, or mark the country tax-free."}
    return {"label": label, "percent": float(pct), "tax_free": False, "configured": True, "error": None}


def _country_fields(data: dict, partial_from: Optional[dict] = None) -> dict:
    """Validated country fields. Tax label / rate are optional at creation (the country then shows 'tax not configured')."""
    base = partial_from or {}
    get = lambda k: data[k] if k in data else base.get(k)
    out = {
        "name": text(get("name"), "Country name", 2, 60),
        "currency_name": text(get("currency_name"), "Currency name", 2, 40),
        "currency_code": clean_currency_code(get("currency_code")),
        "currency_symbol": text(get("currency_symbol"), "Currency symbol", 1, 8),
    }
    free = bool(get("tax_free"))
    label = get("tax_label")
    pct = get("tax_percent")
    out["tax_free"] = free
    if free:
        out["tax_label"] = text(label, "Tax label", 1, 30) if (isinstance(label, str) and label.strip()) else ""
        out["tax_percent"] = 0.0
    else:
        has_label = isinstance(label, str) and label.strip() != ""
        has_pct = pct is not None and not (isinstance(pct, str) and not pct.strip())
        if has_label != has_pct:
            _bad("Enter both the tax label and the tax rate, or leave both empty", "tax_incomplete")
        out["tax_label"] = text(label, "Tax label", 1, 30) if has_label else ""
        out["tax_percent"] = percent(pct) if has_pct else None
    return out


def _status(v) -> str:
    if v not in ("active", "inactive"):
        _bad("Status must be active or inactive")
    return v


def create_country(data: dict, admin_ref: str) -> dict:
    code = clean_code(data.get("code"), iso=True)
    if code == INDIA or db.pricing_countries.find_one({"code": code}):
        raise HTTPException(409, {"code": "duplicate_country", "message": f"Country code {code} already exists"})
    f = _country_fields(data)
    if db.pricing_countries.find_one({"name_key": f["name"].casefold()}):
        raise HTTPException(409, {"code": "duplicate_country", "message": f"A country named {f['name']} already exists"})
    now = datetime.utcnow()
    doc = {"code": code, **f, "name_key": f["name"].casefold(), "status": _status(data.get("status", "active")),
           "created_at": now, "updated_at": now}
    try:
        db.pricing_countries.insert_one(doc)
    except Exception as e:                      # unique index race
        if "duplicate" in str(e).lower() or "E11000" in str(e):
            raise HTTPException(409, {"code": "duplicate_country", "message": f"Country code {code} already exists"})
        raise
    log(code, "country", "create", None, _public_country(doc), admin_ref)
    return _public_country(doc)


def update_country(code: str, data: dict, admin_ref: str) -> dict:
    code = clean_code(code)
    if code == INDIA:
        # India's currency, label and calculation are fixed. Its single GST percentage is the existing `gst_percent` field.
        if "tax_percent" in data:
            pct = percent(data.get("tax_percent"), "GST percentage")
            old = india_country()["tax_percent"]
            _upsert_pricing({"gst_percent": pct})
            log(code, "tax", "update", {"tax_percent": old}, {"tax_percent": pct}, admin_ref)
        return india_country()
    cur = db.pricing_countries.find_one({"code": code})
    if not cur:
        raise HTTPException(404, {"code": "not_found", "message": "Country not found"})
    f = _country_fields(data, partial_from=cur)
    new_name_key = f["name"].casefold()
    if new_name_key != cur.get("name_key") and db.pricing_countries.find_one({"name_key": new_name_key, "code": {"$ne": code}}):
        raise HTTPException(409, {"code": "duplicate_country", "message": f"A country named {f['name']} already exists"})
    upd = {**f, "name_key": new_name_key, "updated_at": datetime.utcnow()}
    if "status" in data:
        upd["status"] = _status(data["status"])
    db.pricing_countries.update_one({"code": code}, {"$set": upd})
    new = db.pricing_countries.find_one({"code": code})
    log(code, "country", "update", _public_country(cur), _public_country(new), admin_ref)
    return _public_country(new)


def set_country_status(code: str, status: str, admin_ref: str) -> dict:
    code = clean_code(code)
    status = _status(status)
    if code == INDIA:
        raise HTTPException(409, {"code": "built_in", "message": "India is the built-in default country and cannot be deactivated"})
    cur = db.pricing_countries.find_one({"code": code})
    if not cur:
        raise HTTPException(404, {"code": "not_found", "message": "Country not found"})
    db.pricing_countries.update_one({"code": code}, {"$set": {"status": status, "updated_at": datetime.utcnow()}})
    log(code, "country", "status", {"status": cur.get("status")}, {"status": status}, admin_ref)
    return _public_country(db.pricing_countries.find_one({"code": code}))


# ── prices ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _upsert_pricing(fields: dict) -> None:
    doc = db.pricing.find_one({})
    if doc:
        db.pricing.update_one({"_id": doc["_id"]}, {"$set": fields})
    else:
        db.pricing.insert_one(dict(fields))


def _cp(code: str) -> dict:
    return db.country_prices.find_one({"country_code": code}) or {}


def _row_money(price, tax: dict) -> dict:
    if price is None:
        return {"price": None, "tax_amount": None, "total": None}
    if not tax["configured"]:
        return {"price": price, "tax_amount": None, "total": None}
    amt = round(price * tax["percent"] / 100, 2)
    return {"price": price, "tax_amount": amt, "total": round(price + amt, 2)}


def read_prices(code: str) -> dict:
    """Every section for one country, with a `configured` flag per section and tax / totals computed with THAT country's tax."""
    c = get_country(code)
    if not c:
        raise HTTPException(404, {"code": "not_found", "message": "Country not found"})
    tax = tax_state(c)
    india = c["code"] == INDIA
    out = {"country": c, "tax": tax, "is_india": india}
    if india:
        d = _pricing_doc()
        plans = d.get("plans") or INDIA_DEFAULT_PLANS
        out["store_plans"] = [{"id": p.get("id"), "label": p.get("label"), "configured": True, "active": True, **_row_money(p.get("price"), tax)}
                              for p in plans]
        isub = d.get("influencer_subscription", {}) or {}
        itax = {"label": "GST", "percent": float(isub.get("gst_percent", d.get("gst_percent", 18)) or 0), "configured": True,
                "tax_free": False, "error": None}
        fee = float(isub.get("fee", 0) or 0)
        out["influencer"] = {"configured": True, "active": bool(isub.get("enabled", False)), "tax": itax, **_row_money(fee, itax)}
        out["banner"] = {"configured": True, "active": True, **_row_money(float(d.get("banner_price_per_day", 15)), tax)}
        out["product"] = {"configured": True, "active": True,
                          **{k: v for k, v in _row_money(float(d.get("product_price_per_day", d.get("voucher_price_per_day", 10))), tax).items()},
                          "free_product_limit": int(d.get("free_product_limit", 10)),
                          "product_max_validity_days": int(d.get("product_max_validity_days", 365))}
        out["reward"] = {"configured": True, "active": True, "conversion_rate": float(d.get("conversion_rate", 0.10)),
                         "min_withdraw_points": int(d.get("min_withdraw_points", 200))}
        return out
    cp = _cp(c["code"])
    saved = cp.get("store_plans") or {}
    rows = []
    for pid, label in PLAN_IDS:
        s = saved.get(pid)
        if s and s.get("price") is not None:
            rows.append({"id": pid, "label": label, "configured": True, "active": bool(s.get("active", True)), **_row_money(float(s["price"]), tax)})
        else:
            rows.append({"id": pid, "label": label, "configured": False, "active": False, **_row_money(None, tax)})
    out["store_plans"] = rows
    inf = cp.get("influencer")
    out["influencer"] = ({"configured": True, "active": bool(inf.get("enabled", False)), "tax": tax, **_row_money(float(inf["fee"]), tax)}
                         if inf and inf.get("fee") is not None else {"configured": False, "active": False, "tax": tax, **_row_money(None, tax)})
    b = cp.get("banner")
    out["banner"] = ({"configured": True, "active": bool(b.get("active", True)), **_row_money(float(b["price_per_day"]), tax)}
                     if b and b.get("price_per_day") is not None else {"configured": False, "active": False, **_row_money(None, tax)})
    p = cp.get("product")
    if p and p.get("price_per_day") is not None:
        out["product"] = {"configured": True, "active": bool(p.get("active", True)), **_row_money(float(p["price_per_day"]), tax),
                          "free_product_limit": p.get("free_product_limit"), "product_max_validity_days": p.get("max_validity_days")}
    else:
        out["product"] = {"configured": False, "active": False, **_row_money(None, tax), "free_product_limit": None, "product_max_validity_days": None}
    r = cp.get("reward")
    out["reward"] = ({"configured": True, "active": bool(r.get("active", True)), "conversion_rate": float(r["conversion_rate"]),
                      "min_withdraw_points": int(r["min_withdraw_points"])}
                     if r and r.get("conversion_rate") is not None else {"configured": False, "active": False, "conversion_rate": None, "min_withdraw_points": None})
    return out


def _require_active_target(code: str) -> dict:
    c = get_country(code)
    if not c:
        raise HTTPException(404, {"code": "not_found", "message": "Country not found"})
    return c


def save_section(code: str, section: str, data: dict, admin_ref: str) -> dict:
    """Save one section for one country. India writes the existing `pricing` fields; others write `country_prices`."""
    code = clean_code(code)
    if section not in SECTIONS:
        raise HTTPException(404, {"code": "not_found", "message": "Unknown section"})
    _require_active_target(code)
    if not any(k in data for k in _KEYS[section]):
        _bad("Nothing to save", "empty")
    before = read_prices(code)[section]
    if code == INDIA:
        _save_india(section, data)
    else:
        _save_other(code, section, data)
    after = read_prices(code)[section]
    log(code, section, "update", before, after, admin_ref)
    return after


def _save_india(section: str, data: dict) -> None:
    d = _pricing_doc()
    if section == "store_plans":
        if "plans" not in data or not isinstance(data["plans"], list):
            _bad("plans is required")
        cur = list(d.get("plans") or INDIA_DEFAULT_PLANS)
        newp = {str(p.get("id")): p for p in data["plans"] if isinstance(p, dict)}
        out = []
        for p in cur:
            n = newp.get(str(p.get("id")))
            if n is not None and "price" in n:
                price = money(n["price"], f"{p.get('label')} price")
                if price < 1:
                    _bad(f"{p.get('label')} price must be at least 1")
                out.append({**p, "price": int(price) if price == int(price) else price})
            else:
                out.append(p)
        _upsert_pricing({"plans": out})
    elif section == "influencer":
        isub = dict(d.get("influencer_subscription", {}) or {})
        upd = {"fee": float(isub.get("fee", 0) or 0), "gst_percent": float(isub.get("gst_percent", d.get("gst_percent", 18)) or 0),
               "enabled": bool(isub.get("enabled", False))}
        if "fee" in data: upd["fee"] = money(data["fee"], "Fee")
        if "gst_percent" in data: upd["gst_percent"] = percent(data["gst_percent"], "GST percentage")
        if "enabled" in data: upd["enabled"] = bool(data["enabled"])
        _upsert_pricing({"influencer_subscription": upd})
    elif section == "banner":
        _upsert_pricing({"banner_price_per_day": money(data.get("price_per_day"), "Banner price per day")})
    elif section == "product":
        upd = {}
        if "price_per_day" in data: upd["product_price_per_day"] = money(data["price_per_day"], "Product day price")
        if "free_product_limit" in data: upd["free_product_limit"] = whole(data["free_product_limit"], "Free product window", 0)
        if "product_max_validity_days" in data: upd["product_max_validity_days"] = whole(data["product_max_validity_days"], "Maximum validity", 1)
        if not upd:
            _bad("Nothing to save")
        _upsert_pricing(upd)
    elif section == "reward":
        rate = money(data.get("conversion_rate"), "Conversion rate")
        if rate <= 0:
            _bad("Conversion rate must be greater than 0")
        _upsert_pricing({"conversion_rate": rate, "min_withdraw_points": whole(data.get("min_withdraw_points"), "Minimum points", 1)})


_KEYS = {"store_plans": ("plans",), "influencer": ("fee", "enabled"), "banner": ("price_per_day", "active"),
         "product": ("price_per_day", "free_product_limit", "product_max_validity_days", "active"),
         "reward": ("conversion_rate", "min_withdraw_points", "active")}


def _save_other(code: str, section: str, data: dict) -> None:
    if not any(k in data for k in _KEYS[section]):
        _bad("Nothing to save", "empty")
    cp = _cp(code)
    now = datetime.utcnow()
    set_ = {"updated_at": now}
    if section == "store_plans":
        if "plans" not in data or not isinstance(data["plans"], list):
            _bad("plans is required")
        saved = dict(cp.get("store_plans") or {})
        valid = {pid for pid, _ in PLAN_IDS}
        for p in data["plans"]:
            if not isinstance(p, dict) or p.get("id") not in valid:
                _bad("Unknown plan")
            cur = dict(saved.get(p["id"]) or {})
            if "price" in p:
                cur["price"] = money(p["price"], "Plan price")
                if cur["price"] <= 0:
                    _bad("Plan price must be greater than 0")
            if "active" in p:
                cur["active"] = bool(p["active"])
            if cur.get("price") is None:
                _bad("Set a price before activating a plan")
            cur.setdefault("active", True)
            saved[p["id"]] = cur
        set_["store_plans"] = saved
    elif section == "influencer":
        cur = dict(cp.get("influencer") or {})
        if "fee" in data: cur["fee"] = money(data["fee"], "Fee")
        if "enabled" in data: cur["enabled"] = bool(data["enabled"])
        if cur.get("fee") is None:
            _bad("Set a fee before enabling the plan")
        cur.setdefault("enabled", False)
        set_["influencer"] = cur
    elif section == "banner":
        cur = dict(cp.get("banner") or {})
        if "price_per_day" in data: cur["price_per_day"] = money(data["price_per_day"], "Banner price per day")
        if "active" in data: cur["active"] = bool(data["active"])
        if cur.get("price_per_day") is None:
            _bad("Set a price before activating")
        cur.setdefault("active", True)
        set_["banner"] = cur
    elif section == "product":
        cur = dict(cp.get("product") or {})
        if "price_per_day" in data: cur["price_per_day"] = money(data["price_per_day"], "Product day price")
        if "free_product_limit" in data: cur["free_product_limit"] = whole(data["free_product_limit"], "Free product window", 0)
        if "product_max_validity_days" in data: cur["max_validity_days"] = whole(data["product_max_validity_days"], "Maximum validity", 1)
        if "active" in data: cur["active"] = bool(data["active"])
        missing = [k for k in ("price_per_day", "free_product_limit", "max_validity_days") if cur.get(k) is None]
        if missing:
            _bad("Set the product day price, free product window and maximum validity together")
        cur.setdefault("active", True)
        set_["product"] = cur
    elif section == "reward":
        cur = dict(cp.get("reward") or {})
        if "conversion_rate" in data:
            cur["conversion_rate"] = money(data["conversion_rate"], "Conversion rate")
            if cur["conversion_rate"] <= 0:
                _bad("Conversion rate must be greater than 0")
        if "min_withdraw_points" in data: cur["min_withdraw_points"] = whole(data["min_withdraw_points"], "Minimum points", 1)
        if "active" in data: cur["active"] = bool(data["active"])
        if cur.get("conversion_rate") is None or cur.get("min_withdraw_points") is None:
            _bad("Set both the conversion rate and the minimum points")
        cur.setdefault("active", True)
        set_["reward"] = cur
    db.country_prices.update_one({"country_code": code}, {"$set": set_, "$setOnInsert": {"country_code": code, "created_at": now}}, upsert=True)


# ── history ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _plain(v):
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items() if k != "_id"}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, datetime):
        return v.isoformat()
    return v


_COMPUTED = {"tax_amount", "total", "tax", "built_in", "label"}


def _flat(v, prefix=""):
    """Flatten a section / country snapshot to {path: value}, keyed by plan id inside lists, without computed fields."""
    out = {}
    if isinstance(v, dict):
        for k, x in v.items():
            if k in _COMPUTED:
                continue
            out.update(_flat(x, f"{prefix}.{k}" if prefix else k))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            key = x.get("id", i) if isinstance(x, dict) else i
            out.update(_flat(x, f"{prefix}.{key}" if prefix else str(key)))
    else:
        out[prefix] = v
    return out


def log(code: str, section: str, action: str, old, new, admin_ref: str) -> None:
    """Record only what changed (old and new value per field), never the whole document."""
    try:
        fo, fn = _flat(_plain(old)) if old is not None else {}, _flat(_plain(new)) if new is not None else {}
        if old is not None and new is not None:
            keys = [k for k in dict.fromkeys(list(fo) + list(fn)) if fo.get(k) != fn.get(k)]
            fo, fn = {k: fo.get(k) for k in keys}, {k: fn.get(k) for k in keys}
        db.pricing_history.insert_one({"country_code": code, "section": section, "action": action, "old": fo or None, "new": fn or None,
                                       "by": admin_ref, "at": datetime.utcnow()})
    except Exception as e:                              # history is informational; never block the change
        print(f"[pricing-config] history write failed: {e}")


def history(code: Optional[str], limit: int = 50) -> list:
    q = {"country_code": code} if code else {}
    out = []
    for h in db.pricing_history.find(q).sort("at", -1).limit(max(1, min(int(limit), 200))):
        out.append({"country_code": h.get("country_code"), "section": h.get("section"), "action": h.get("action"),
                    "old": h.get("old"), "new": h.get("new"), "by": h.get("by"), "at": h["at"].isoformat() + "Z"})
    return out


def ensure_indexes() -> None:
    db.pricing_countries.create_index("code", unique=True, name="pricing_countries_code", background=True)
    db.pricing_countries.create_index("name_key", unique=True, name="pricing_countries_name", background=True, sparse=True)
    db.country_prices.create_index("country_code", unique=True, name="country_prices_code", background=True)
    db.pricing_history.create_index([("country_code", 1), ("at", -1)], name="pricing_history_country_at", background=True)
