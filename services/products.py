"""
Central Product service  (collection: merchant_products, orders: product_orders)

This module is the ONLY place that decides whether a product is FREE or PAID.
Merchant create, Admin create-for-merchant and Renewal all go through it.

Nothing in here hard-codes the free limit or the price. Both are read from the
Admin-editable settings document (db.pricing) on EVERY decision:

    free_product_limit        number of free qualifying products PER STORE (a merchant with
                              two stores gets that many free products in each store)
    product_price_per_day     price (before GST) of ONE day of a paid product;
                              a paid product costs  price_per_day x days
    product_max_validity_days longest allowed validity
    gst_percent               (existing setting) GST applied to the paid price

Days are INCLUSIVE: a product is live THROUGH its end date, so start 1 Nov and end
30 Nov is 30 days.

The numbers 10 / 10 / 365 appear only in SEED_DEFAULTS, which is used by
ensure_settings() to create a missing key once (startup / reset script). They
are never consulted when deciding free vs paid.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import time
import uuid

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from database import db

PRODUCTS = "merchant_products"
ORDERS = "product_orders"
LOCKS = "merchant_locks"

# Statuses that never occupy a slot. Active (approved) and pending count;
# deactivated (is_active False) products keep counting until they expire.
NON_QUALIFYING_STATUSES = ("deleted", "expired", "rejected")

# Seed data ONLY (see module docstring) — never read by business logic.
SEED_DEFAULTS = {
    "free_product_limit": 10,
    "product_price_per_day": 10,
    "product_max_validity_days": 365,
}

_LOCK_STALE_SECONDS = 30
_LOCK_WAIT_SECONDS = 8


class ProductError(Exception):
    """Raised for every business-rule failure. Routers map it to HTTPException."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def merchant_key(merchant: dict) -> str:
    """The id products are stored under. Same rule as merchant_app._mid():
    the legacy merchant id when present, otherwise the account _id."""
    if not merchant:
        return ""
    return str(merchant.get("merchant_id") or merchant.get("_id") or "")


# ── time helpers (IST business day; product valid THROUGH its end date) ────────
def ist_now() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).replace(tzinfo=None)


def ist_today() -> datetime:
    return ist_now().replace(hour=0, minute=0, second=0, microsecond=0)


def parse_date(val):
    """Accepts datetime, ISO ('2026-11-30[T..]') or '%d %b %Y'. Returns naive
    datetime at 00:00 or None. Never raises."""
    if val is None or val == "":
        return None
    if hasattr(val, "strftime"):
        return val.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    s = str(val).strip()
    for fmt, n in (("%Y-%m-%d", 10), ("%d %b %Y", 11)):
        try:
            return datetime.strptime(s[:n].strip(), fmt)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "")[:19]).replace(hour=0, minute=0, second=0, microsecond=0)
    except Exception:
        return None


def is_expired_by_date(doc) -> bool:
    end = parse_date(doc.get("end_date"))
    return bool(end and end < ist_today())


def inclusive_days(start: datetime, end: datetime) -> int:
    """Live days, counting both the start and the end date."""
    return (end - start).days + 1


def validate_period(from_raw, end_raw):
    """Merchant/Admin supplied validity -> (start, end, days). Start defaults to today and
    may not be in the past; end may not be before start; days may not exceed the
    Admin-configured maximum."""
    today = ist_today()
    start = parse_date(from_raw) if from_raw not in (None, "") else today
    if start is None:
        raise ProductError(400, "Please choose a valid start date for the product")
    if start < today:
        raise ProductError(400, "Start date cannot be in the past")
    end = parse_date(end_raw)
    if end is None:
        raise ProductError(400, "Please choose a valid end date for the product")
    if end < start:
        raise ProductError(400, "End date cannot be before the start date")
    days = inclusive_days(start, end)
    max_days = get_settings()["product_max_validity_days"]
    if days > max_days:
        raise ProductError(400, f"A product can run for at most {max_days} days")
    return start, end, days


def validate_end_date(raw) -> datetime:
    return validate_period(None, raw)[1]


# ── settings (Admin-configured) ────────────────────────────────────────────────
def get_settings() -> dict:
    """Read the Admin-configured product settings. Raises a clear error if the
    free limit or the per-day price has not been configured — it never falls back to a
    hard-coded number."""
    doc = db.pricing.find_one({}) or {}
    ppd = doc.get("product_price_per_day")
    if ppd is None:
        ppd = doc.get("voucher_price_per_day")   # value the Admin already configured before the rename
    doc = dict(doc, product_price_per_day=ppd)
    missing = [k for k in SEED_DEFAULTS if doc.get(k) is None]
    if missing:
        raise ProductError(
            500,
            "Product settings are not configured (" + ", ".join(missing) +
            "). Set them in Admin → Settings → Pricing.",
        )
    try:
        out = {
            "free_product_limit": max(0, int(doc["free_product_limit"])),
            "product_price_per_day": max(0.0, float(doc["product_price_per_day"])),
            "product_max_validity_days": max(1, int(doc["product_max_validity_days"])),
            "gst_percent": max(0.0, float(doc.get("gst_percent", 18) or 0)),
        }
    except (TypeError, ValueError):
        raise ProductError(500, "Product settings in Admin → Pricing are invalid")
    return out


def ensure_settings() -> list:
    """Create any MISSING product setting. Existing values (including ones the
    Admin changed) are never overwritten. The free limit is seeded from the
    legacy Admin value `standard_product_limit` when that exists (same concept,
    already Admin-set); otherwise from SEED_DEFAULTS. Returns the keys created."""
    doc = db.pricing.find_one({})
    if doc is None:
        db.pricing.insert_one(dict(SEED_DEFAULTS))
        return list(SEED_DEFAULTS)
    add = {k: v for k, v in SEED_DEFAULTS.items() if doc.get(k) is None}
    if "free_product_limit" in add and doc.get("standard_product_limit") is not None:
        try:
            add["free_product_limit"] = int(doc["standard_product_limit"])
        except (TypeError, ValueError):
            pass
    if "product_price_per_day" in add and doc.get("voucher_price_per_day") is not None:
        try:
            add["product_price_per_day"] = float(doc["voucher_price_per_day"])
        except (TypeError, ValueError):
            pass
    if add:
        db.pricing.update_one({"_id": doc["_id"]}, {"$set": add})
    return list(add)


# ── qualifying count ───────────────────────────────────────────────────────────
def _merchant_clause(merchant_id):
    return {"merchant_id": str(merchant_id)}


def qualifying_query(store_id, exclude_id=None) -> dict:
    """Products that occupy one of a STORE's free slots (quota is per store, never
    per merchant - two stores of one merchant do not share it):
       - belongs to the store (admin-created ones included - they carry the store's id),
       - not OffrO-owned (pricing_tier "admin" never consumes a slot),
       - status not deleted / expired / rejected,
       - end date not yet passed (valid through the end date).
       Deactivated products (is_active False) are deliberately NOT excluded."""
    q = {
        "store_id": str(store_id),
        "pricing_tier": {"$ne": "admin"},
        "status": {"$nin": list(NON_QUALIFYING_STATUSES)},
        "end_date": {"$gte": ist_today()},
    }
    if exclude_id is not None:
        try:
            q["_id"] = {"$ne": ObjectId(str(exclude_id))}
        except Exception:
            q["_id"] = {"$ne": exclude_id}
    return q


def qualifying_count(store_id, exclude_id=None) -> int:
    return db[PRODUCTS].count_documents(qualifying_query(store_id, exclude_id))


# ── THE decision ───────────────────────────────────────────────────────────────
def decide(store_id, exclude_id=None, days: int = 0) -> dict:
    """Free or paid? One function, three callers (create, admin-create, renew).
    The quota is PER STORE: `store_id` is the store the product belongs to.

    tier "admin"  – no store given (OffrO-owned product): no quota, no charge.
    tier "free"   – the store's qualifying count  <  free_product_limit
    tier "paid"   – the store's qualifying count  >= free_product_limit; price = price_per_day x days
    `days` is the number of billable days of THIS purchase (0 = just report the tier).
    """
    s = get_settings()
    ppd = round(s["product_price_per_day"], 4)
    base = {"price_per_day": ppd, "days": int(days or 0), "free_limit": s["free_product_limit"],
            "gst_percent": s["gst_percent"]}
    if not store_id:
        return {**base, "tier": "admin", "used": 0, "unit_price": 0.0, "gst_amount": 0.0, "total": 0.0}
    used = qualifying_count(store_id, exclude_id)
    if used < s["free_product_limit"]:
        tier, price = "free", 0.0
    else:
        tier, price = "paid", round(ppd * int(days or 0), 2)
    gst = round(price * s["gst_percent"] / 100.0, 2)
    return {**base, "tier": tier, "used": used, "unit_price": price,
            "gst_amount": gst, "total": round(price + gst, 2)}


def slots_summary(store_id, exclude_id=None, days: int = 0) -> dict:
    d = decide(store_id, exclude_id, days)
    return {"used": d["used"], "free_limit": d["free_limit"],
            "free_remaining": max(0, d["free_limit"] - d["used"]),
            "next_is_free": d["tier"] == "free", "tier": d["tier"],
            "price_per_day": d["price_per_day"], "days": d["days"],
            "unit_price": d["unit_price"], "gst_percent": d["gst_percent"],
            "gst_amount": d["gst_amount"], "total": d["total"]}


# ── per-store lock (count + insert must be atomic per store) ───────────────────
@contextmanager
def store_lock(store_id):
    key = str(store_id or "_admin_")
    token = uuid.uuid4().hex
    deadline = time.time() + _LOCK_WAIT_SECONDS
    while True:
        try:
            db[LOCKS].insert_one({"_id": key, "token": token, "at": time.time()})
            break
        except DuplicateKeyError:
            cur = db[LOCKS].find_one({"_id": key})
            if cur and time.time() - cur.get("at", 0) > _LOCK_STALE_SECONDS:
                db[LOCKS].delete_one({"_id": key, "token": cur.get("token")})
                continue
            if time.time() > deadline:
                raise ProductError(409, "Another product action is in progress. Please try again.")
            time.sleep(0.05)
    try:
        yield
    finally:
        db[LOCKS].delete_one({"_id": key, "token": token})


merchant_lock = store_lock      # backwards-compatible alias (the lock is per store now)


def store_key(fields: dict) -> str:
    return str((fields or {}).get("store_id") or "").strip()


def _need_store(fields: dict) -> str:
    sid = store_key(fields)
    if not sid:
        raise ProductError(400, "Please select the store this product is for "
                                "(free product slots are counted per store).")
    return sid


# ── create / renew / delete ────────────────────────────────────────────────────
def _paid_message(d: dict) -> str:
    return (f"All {d['free_limit']} free product slots are used. An additional product "
            f"costs ₹{d['price_per_day']:g}/day × {d['days']} days = ₹{d['unit_price']:g} "
            f"+ GST (₹{d['total']:g} total).")


def build_doc(*, merchant: dict, fields: dict, start_date: datetime, end_date: datetime,
              source: str, decision: dict, charged_amount: float = 0.0, payment: dict = None) -> dict:
    now = datetime.utcnow()
    approved = source == "admin"
    doc = {
        "merchant_id": merchant_key(merchant) or None,   # None = OffrO-owned (no merchant, no quota)
        "merchant_name": merchant.get("name") or merchant.get("merchant_name") or "",
        "merchant_phone": merchant.get("phone") or merchant.get("merchant_phone") or "",
        "store_id": str(fields.get("store_id") or ""),
        "store_name": fields.get("store_name") or "",
        "city": fields.get("city") or "",
        "title": (fields.get("title") or "").strip(),
        "description": fields.get("description") or "",
        "image_url": fields.get("image_url") or "",
        "image_thumb": fields.get("image_thumb") or "",
        "price": fields.get("price") or "",
        "original_price": fields.get("original_price") or "",
        "discount_label": fields.get("discount_label") or "",
        "start_date": start_date,
        "end_date": end_date,
        "duration_days": inclusive_days(start_date, end_date),
        "status": "approved" if approved else "pending",
        "is_active": True,
        "source": source,
        "pricing_tier": decision["tier"],
        "price_per_day": decision["price_per_day"] if decision["tier"] == "paid" else 0.0,
        "unit_price": decision["unit_price"],        # base price of THIS purchase (rate x days), fixed forever
        "charged_amount": round(charged_amount, 2),   # what was actually paid (incl. GST), fixed forever
        "gst_percent": decision["gst_percent"],
        "gst_amount": decision["gst_amount"] if charged_amount else 0.0,
        "rating": 0.0, "rating_count": 0,
        "created_at": now, "updated_at": now,
    }
    if payment:
        doc.update({k: payment[k] for k in payment})
    return doc


def create_free(*, merchant: dict, fields: dict, end_raw, from_raw=None, source: str = "merchant") -> dict:
    """Free creation. Re-decides under the STORE lock so two simultaneous
    requests cannot both take the store's last free slot."""
    sid = _need_store(fields)
    start, end, days = validate_period(from_raw, end_raw)
    with store_lock(sid):
        d = decide(sid, days=days)
        if d["tier"] != "free":
            raise ProductError(409, _paid_message(d))
        doc = build_doc(merchant=merchant, fields=fields, start_date=start, end_date=end,
                        source=source, decision=d)
        doc["_id"] = db[PRODUCTS].insert_one(doc).inserted_id
    return doc


def create_paid(*, merchant: dict, fields: dict, end_raw, from_raw=None, source: str, paid_total: float,
                payment: dict, price: dict) -> dict:
    """Insert a paid product after the merchant's payment was verified. `price` is what the
    ORDER quoted (unit_price, price_per_day, gst_percent, gst_amount) - the merchant is
    recorded at the price they were shown and paid, even if the Admin changed the rate since."""
    sid = _need_store(fields)
    start, end, days = validate_period(from_raw, end_raw)
    with store_lock(sid):
        d = {"tier": "paid", "price_per_day": price["price_per_day"], "unit_price": price["unit_price"],
             "gst_percent": price["gst_percent"], "gst_amount": price["gst_amount"]}
        doc = build_doc(merchant=merchant, fields=fields, start_date=start, end_date=end,
                        source=source, decision=d, charged_amount=paid_total, payment=payment)
        doc["_id"] = db[PRODUCTS].insert_one(doc).inserted_id
    return doc


def admin_create(*, merchant: dict, fields: dict, end_raw, from_raw=None, payment: dict = None) -> dict:
    """Admin creates a product FOR a merchant's store. Same rule as the merchant path:
    it consumes THAT STORE's quota. Free if the store has a slot left; otherwise the
    Admin must record the manual payment (mode + amount >= price x days + GST).
    With no merchant it is OffrO-owned: no quota, no charge (source stays "admin")."""
    mid = merchant_key(merchant)
    start, end, days = validate_period(from_raw, end_raw)
    if not mid:
        # OffrO-owned product: no merchant, no quota, no charge.
        d = decide("", days=days)
        doc = build_doc(merchant={}, fields=fields, start_date=start, end_date=end, source="admin", decision=d)
        doc["_id"] = db[PRODUCTS].insert_one(doc).inserted_id
        return doc
    sid = _need_store(fields)
    with store_lock(sid):
        d = decide(sid, days=days)
        if d["tier"] == "free":
            doc = build_doc(merchant=merchant, fields=fields, start_date=start, end_date=end,
                            source="admin", decision=d)
        else:
            expected = d["total"]                          # price_per_day x days + GST
            amt = round(float((payment or {}).get("amount") or 0), 2)
            mode = ((payment or {}).get("payment_mode") or "").strip()
            if not payment or not mode or amt <= 0:
                raise ProductError(409, _paid_message(d) + " Enter the payment mode and amount paid to create it.")
            if amt + 0.005 < expected:                      # >= expected is accepted, lower is not
                raise ProductError(400, f"Amount paid (₹{amt:g}) is less than the expected ₹{expected:g} "
                                        f"(₹{d['price_per_day']:g}/day × {days} days + GST). "
                                        f"Enter at least ₹{expected:g}.")
            doc = build_doc(merchant=merchant, fields=fields, start_date=start, end_date=end,
                            source="admin", decision=d, charged_amount=amt,
                            payment={"expected_amount": expected, "payment_mode": mode,
                                     "payment_ref": (payment.get("payment_ref") or "").strip(),
                                     "payment_status": "paid", "recorded_by_admin": True})
        doc["_id"] = db[PRODUCTS].insert_one(doc).inserted_id
        if doc["pricing_tier"] == "paid":
            _record_admin_paid_order(merchant, doc, d)
    return doc


def _record_admin_paid_order(merchant: dict, doc: dict, d: dict) -> None:
    """Admin-recorded manual payment = a real paid Product transaction:
    product_orders(kind=create, status=paid, pay_mode=admin_recorded) + the normal
    Product invoice. No Razorpay order is created. Revenue reads product_orders.total,
    which here is the ACTUAL amount paid."""
    now = datetime.utcnow()
    mid = doc["merchant_id"]
    order = {
        "kind": "create", "merchant_id": mid, "merchant_name": doc["merchant_name"],
        "merchant_phone": doc["merchant_phone"], "product_id": str(doc["_id"]),
        "pricing_tier": "paid", "unit_price": d["unit_price"], "base_price": d["unit_price"],
        "price_per_day": d["price_per_day"], "days": doc["duration_days"],
        "gst_percent": d["gst_percent"], "gst_amount": d["gst_amount"],
        "expected_amount": doc["expected_amount"], "total": doc["charged_amount"],
        "amount_paid": doc["charged_amount"], "final_amount": d["unit_price"],
        "from_date": doc["start_date"], "end_date": doc["end_date"],
        "discount_code": None, "discount_amount": 0,
        "pay_mode": "admin_recorded", "payment_mode": doc["payment_mode"],
        "payment_ref": doc["payment_ref"], "razorpay_order_id": None, "amount_paise": 0,
        "recorded_by_admin": True, "status": "paid", "created_at": now, "paid_at": now,
    }
    oid = db[ORDERS].insert_one(order).inserted_id
    inv_no = f"PRD-{now.strftime('%Y%m%d')}-{str(doc['_id'])[-6:].upper()}"
    db.invoices.insert_one({
        "invoice_no": inv_no, "type": "product", "merchant_id": mid,
        "merchant_name": doc["merchant_name"], "merchant_phone": doc["merchant_phone"],
        "item_label": f"Discover Product – {doc['duration_days']} Days", "store_name": doc["title"],
        "plan": f"{doc['start_date']:%d %b %Y} \u2192 {doc['end_date']:%d %b %Y}",
        "base_price": d["unit_price"], "original_amount": d["unit_price"],
        "discount_code": "", "discount_amount": 0, "final_amount": d["unit_price"],
        "gst": d["gst_amount"], "gst_percent": d["gst_percent"],
        "expected_amount": doc["expected_amount"], "total": doc["charged_amount"],
        "payment_mode": doc["payment_mode"], "payment_ref": doc["payment_ref"],
        "payment_status": "paid", "recorded_by_admin": True, "created_at": now,
    })
    db[PRODUCTS].update_one({"_id": doc["_id"]}, {"$set": {"order_id": str(oid), "invoice_no": inv_no}})
    doc["order_id"], doc["invoice_no"] = str(oid), inv_no


def ensure_editable(doc: dict):
    if not doc:
        raise ProductError(404, "Product not found")
    if doc.get("status") == "deleted":
        raise ProductError(400, "A deleted product cannot be changed")


def soft_delete(doc: dict, by: str = "merchant") -> None:
    """status=deleted, no refund. Deleted products no longer qualify, so the
    slot is freed automatically by the count — nothing else to adjust."""
    if not doc:
        raise ProductError(404, "Product not found")
    if doc.get("status") == "deleted":
        return
    now = datetime.utcnow()
    db[PRODUCTS].update_one({"_id": doc["_id"]}, {"$set": {
        "status": "deleted", "is_active": False, "deleted_at": now,
        "deleted_by": by, "updated_at": now}})


def renewal_period(doc: dict, end_raw):
    """New end date -> (end, billable_days) for a renewal. You only pay for the days you ADD:
    from the day after the current end date while the product is still live, or from today
    once it has expired."""
    ensure_editable(doc)
    end = parse_date(end_raw)
    if end is None:
        raise ProductError(400, "Please choose a valid end date for the product")
    cur = parse_date(doc.get("end_date"))
    base = max(cur, ist_today() - timedelta(days=1)) if cur else ist_today() - timedelta(days=1)
    days = (end - base).days
    if days < 1:
        raise ProductError(400, "The new end date must be after the current end date")
    max_days = get_settings()["product_max_validity_days"]
    if (end - ist_today()).days + 1 > max_days:           # remaining live time from today
        raise ProductError(400, f"A product can run for at most {max_days} days from today")
    return end, days


def renew_decision(doc: dict, days: int = 0) -> dict:
    """Free/paid for renewing `doc`. The product itself is excluded from the
    count so renewing an expired product is judged like a fresh slot and a
    live one is not counted twice."""
    ensure_editable(doc)
    sid = str(doc.get("store_id") or "").strip()
    if not sid and doc.get("merchant_id") and doc.get("pricing_tier") != "admin":
        # Quota is per store - never guess one (that would renew for free). Fail closed.
        raise ProductError(409, "This product is not linked to a store, so it cannot be renewed. "
                                "Please contact OffrO support.")
    return decide(sid, exclude_id=doc["_id"], days=days)


def apply_renewal(doc: dict, end_raw, *, charged_amount: float = 0.0, payment: dict = None,
                  price: dict = None) -> dict:
    """Extend/reactivate. Free renewals re-check under the lock; paid renewals
    are applied after the caller verified payment. `price` = what the renewal ORDER quoted."""
    end, days = renewal_period(doc, end_raw)
    with store_lock(doc.get("store_id")):
        d = renew_decision(doc, days)
        if charged_amount <= 0 and d["tier"] == "paid":
            raise ProductError(409, _paid_message(d))
        # Purchase history is permanent: pricing_tier / unit_price / charged_amount of the
        # ORIGINAL purchase are never rewritten. Each renewal is appended instead.
        now = datetime.utcnow()
        start = parse_date(doc.get("start_date")) or ist_today()
        upd = {"end_date": end, "is_active": True, "updated_at": now, "renewed_at": now,
               "duration_days": inclusive_days(min(start, end), end)}
        if doc.get("status") == "expired":
            upd["status"] = "approved"
        if payment:
            upd.update(payment)
        q = price or d
        entry = {"at": now, "new_end_date": end, "added_days": days,
                 "pricing_tier": "paid" if charged_amount > 0 else d["tier"],
                 "price_per_day": q.get("price_per_day", d["price_per_day"]),
                 "unit_price": q.get("unit_price", d["unit_price"]),
                 "charged_amount": round(float(charged_amount or 0), 2),
                 "gst_percent": q.get("gst_percent", d["gst_percent"])}
        db[PRODUCTS].update_one({"_id": doc["_id"]}, {"$set": upd, "$push": {"renewals": entry}})
    return db[PRODUCTS].find_one({"_id": doc["_id"]})


def expire_due(merchant_id=None) -> int:
    """Mark approved/pending products whose end date has passed as expired
    (idempotent; run on list calls and by the expiry check)."""
    q = {"status": {"$in": ["approved", "pending"]}, "end_date": {"$lt": ist_today()}}
    if merchant_id:
        q.update(_merchant_clause(merchant_id))
    r = db[PRODUCTS].update_many(q, {"$set": {"status": "expired", "is_active": False,
                                              "updated_at": datetime.utcnow()}})
    return r.modified_count


def ensure_indexes():
    db[PRODUCTS].create_index([("merchant_id", 1), ("status", 1), ("end_date", 1)])
    db[PRODUCTS].create_index([("store_id", 1), ("status", 1), ("end_date", 1)])
    db[ORDERS].create_index([("merchant_id", 1), ("created_at", -1)])
    db[ORDERS].create_index("razorpay_order_id")
