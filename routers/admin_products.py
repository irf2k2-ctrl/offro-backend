"""
Admin Product routes  —  /admin/products/...   (collection: merchant_products)

Admin creating a product FOR a merchant uses the very same service decision as
the merchant app (services/products.py): it consumes that merchant's quota and
is free or paid by the same rule. A product with no merchant is OffrO-owned and
consumes no quota.
"""
from datetime import datetime, timedelta

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from database import db
from routers.admin import get_current_admin, _check_perm, _city_filter
from routers.merchant_app import _cloudinary_upload, _make_thumb_url
from services import products as P

router = APIRouter(prefix="/admin", tags=["Admin Products"])
PRODUCTS, ORDERS = P.PRODUCTS, P.ORDERS


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except P.ProductError as e:
        raise HTTPException(e.status_code, e.detail)


def _oid(v):
    try:
        return ObjectId(str(v))
    except Exception:
        raise HTTPException(400, "Invalid product ID")


def _ymd(d):
    return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d or "")


def _row(p: dict) -> dict:
    expired = p.get("status") == "expired" or P.is_expired_by_date(p)
    status = "expired" if expired else p.get("status", "pending")
    logo = p.get("image_url", "")
    frm, to = _ymd(p.get("start_date")), _ymd(p.get("end_date"))
    return {
        "id": str(p["_id"]), "_id": str(p["_id"]), "logo": logo, "logo_url": logo,
        "title": p.get("title", ""), "text": p.get("description", ""), "offer_text": p.get("description", ""),
        "price": p.get("price", 0), "sale_price": p.get("price", 0), "original_price": p.get("original_price", ""),
        "from_date": frm, "end_date": to, "validity": f"{frm} → {to}" if frm and to else to,
        "is_active": bool(p.get("is_active", True)),
        "approval_status": status, "status": status,
        "source": p.get("source", "merchant"), "pricing_tier": p.get("pricing_tier", ""),
        "charged_amount": p.get("charged_amount", 0), "unit_price": p.get("unit_price", 0),
        "merchant_id": p.get("merchant_id", ""), "merchant_name": p.get("merchant_name", ""),
        "merchant_phone": p.get("merchant_phone", ""), "store_id": p.get("store_id", ""),
        "store_name": p.get("store_name", ""), "city": p.get("city", ""),
        "duration_days": max(0, ((P.parse_date(p.get("end_date")) or P.ist_today())
                                 - (P.parse_date(p.get("start_date")) or P.ist_today())).days),
        "payment_mode": p.get("payment_mode", ""), "payment_ref": p.get("payment_ref", ""),
        "created_at": str(p.get("created_at", ""))[:19],
    }


def _resolve_merchant(data: dict):
    """Merchant that owns the product: explicit merchant_id, else the store's
    merchant. Returns None for an OffrO-owned (no merchant) product."""
    mid = str(data.get("merchant_id") or "").strip()
    store = None
    sid = str(data.get("store_id") or "").strip()
    if sid:
        try:
            store = db.stores.find_one({"_id": ObjectId(sid)})
        except Exception:
            raise HTTPException(400, "Invalid store ID")
        if not store:
            raise HTTPException(404, "Store not found")
        smid = str(store.get("merchant_id") or "")
        if mid and smid and mid != smid:
            raise HTTPException(400, "The selected store belongs to a different merchant")
        if not mid:
            mid = smid
    if not mid:
        return None, store
    acct = None
    acct = db.accounts.find_one({"merchant_id": mid})
    if not acct:
        try:
            acct = db.merchants.find_one({"_id": ObjectId(mid)})
        except Exception:
            acct = None
    return {"_id": mid, "merchant_id": mid,
            "name": (acct or {}).get("name") or data.get("merchant_name", ""),
            "phone": str((acct or {}).get("phone") or data.get("merchant_phone", ""))}, store


def _admin_fields(data: dict, store):
    img = data.get("logo") or data.get("logo_url") or ""
    if img:
        img = _cloudinary_upload(img, folder="offro/products")
    title = (data.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Product title is required")
    price, orig = str(data.get("price", "") or ""), str(data.get("original_price", "") or "")
    return {
        "store_id": str(data.get("store_id") or ""),
        "store_name": data.get("store_name") or (store or {}).get("store_name", ""),
        "city": data.get("city") or (store or {}).get("city", ""),
        "title": title, "description": data.get("text") or data.get("offer_text") or "",
        "image_url": img, "image_thumb": _make_thumb_url(img) if img else "",
        "price": price, "original_price": orig,
    }


# ── list ───────────────────────────────────────────────────────────────────────
@router.get("/products")
def list_products(a=Depends(get_current_admin)):
    P.expire_due()
    flt = {"status": {"$ne": "deleted"}, **_city_filter(a)}
    return [_row(p) for p in db[PRODUCTS].find(flt).sort("_id", -1)]


# ── create (same rule as merchant) ─────────────────────────────────────────────
@router.post("/products")
def create_product(data: dict, a=Depends(get_current_admin)):
    _check_perm(a, "Products", "add")
    merchant, store = _resolve_merchant(data)
    fields = _admin_fields(data, store)
    payment = None
    if data.get("pay_mode") or data.get("pay_amount"):
        try:
            _amt = float(data.get("pay_amount") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "Amount paid must be a number")
        payment = {"payment_mode": data.get("pay_mode", ""), "payment_ref": data.get("pay_ref", ""),
                   "amount": _amt}
    prod = _guard(P.admin_create, merchant=merchant, fields=fields, end_raw=data.get("end_date"), payment=payment)
    if data.get("is_active") is False:
        db[PRODUCTS].update_one({"_id": prod["_id"]}, {"$set": {"is_active": False}})
    return {"ok": True, "id": str(prod["_id"]), "pricing_tier": prod["pricing_tier"],
            "charged_amount": prod["charged_amount"],
            "expected_amount": prod.get("expected_amount", 0), "invoice_no": prod.get("invoice_no", ""),
            "order_id": prod.get("order_id", "")}


# ── edit / lifecycle ───────────────────────────────────────────────────────────
def _get(vid, allow_deleted=False):
    doc = db[PRODUCTS].find_one({"_id": _oid(vid)})
    if not doc:
        raise HTTPException(404, "Product not found")
    if doc.get("status") == "deleted" and not allow_deleted:
        raise HTTPException(410, "This product was deleted")
    return doc


@router.put("/products/{vid}")
def update_product(vid: str, data: dict, a=Depends(get_current_admin)):
    _check_perm(a, "Products", "edit")
    doc = _get(vid)
    upd = {}
    for src, dst in (("title", "title"), ("text", "description"), ("offer_text", "description"),
                     ("price", "price"), ("original_price", "original_price"),
                     ("store_name", "store_name"), ("city", "city")):
        if src in data:
            upd[dst] = data[src]
    if data.get("logo") or data.get("logo_url"):
        img = _cloudinary_upload(data.get("logo") or data.get("logo_url"), folder="offro/products")
        upd["image_url"], upd["image_thumb"] = img, _make_thumb_url(img)
    if data.get("end_date"):
        new_end = P.parse_date(data["end_date"])
        if new_end is None:
            raise HTTPException(400, "Invalid end date")
        if new_end != P.parse_date(doc.get("end_date")):       # only validate a CHANGED date
            upd["end_date"] = _guard(P.validate_end_date, data["end_date"])
    if "is_active" in data:
        upd["is_active"] = bool(data["is_active"])
    if upd:
        upd["updated_at"] = datetime.utcnow()
        db[PRODUCTS].update_one({"_id": doc["_id"]}, {"$set": upd})
    return {"ok": True}


@router.put("/products/{vid}/approve")
def approve_product(vid: str, a=Depends(get_current_admin)):
    _check_perm(a, "Products", "approve")
    doc = _get(vid)
    expired = P.is_expired_by_date(doc)
    db[PRODUCTS].update_one({"_id": doc["_id"]}, {"$set": {
        "status": "expired" if expired else "approved", "is_active": not expired,
        "approved_at": datetime.utcnow(), "updated_at": datetime.utcnow()}})
    return {"ok": True, "message": "Product approved."}


@router.put("/products/{vid}/reject")
def reject_product(vid: str, body: dict = {}, a=Depends(get_current_admin)):
    _check_perm(a, "Products", "approve")
    doc = _get(vid)
    db[PRODUCTS].update_one({"_id": doc["_id"]}, {"$set": {
        "status": "rejected", "is_active": False, "rejection_reason": (body or {}).get("reason", ""),
        "rejected_at": datetime.utcnow(), "updated_at": datetime.utcnow()}})
    return {"ok": True}


@router.delete("/products/{vid}")
def delete_product(vid: str, a=Depends(get_current_admin)):
    """Soft delete — same terminal state as a merchant delete (frees the slot)."""
    _check_perm(a, "Products", "delete")
    _guard(P.soft_delete, _get(vid, allow_deleted=True), "admin")
    return {"ok": True}


@router.put("/products/{vid}/activate")
def activate_product(vid: str, a=Depends(get_current_admin)):
    _check_perm(a, "Products", "edit")
    doc = _get(vid)
    if doc.get("status") == "expired" or P.is_expired_by_date(doc):
        raise HTTPException(400, "This product has expired. Extend it first.")
    db[PRODUCTS].update_one({"_id": doc["_id"]}, {"$set": {"is_active": True, "updated_at": datetime.utcnow()}})
    return {"ok": True}


@router.put("/products/{vid}/deactivate")
def deactivate_product(vid: str, a=Depends(get_current_admin)):
    """Deactivated products stay approved and keep occupying the merchant's slot until they expire."""
    _check_perm(a, "Products", "edit")
    doc = _get(vid)
    db[PRODUCTS].update_one({"_id": doc["_id"]}, {"$set": {"is_active": False, "updated_at": datetime.utcnow()}})
    return {"ok": True}


@router.put("/products/{vid}/extend")
def extend_product(vid: str, data: dict, a=Depends(get_current_admin)):
    """Admin goodwill extension by N days (no charge)."""
    _check_perm(a, "Products", "edit")
    doc = _get(vid)
    try:
        days = max(1, int(data.get("days", 7)))
    except Exception:
        raise HTTPException(400, "days must be a number")
    end = P.parse_date(doc.get("end_date")) or P.ist_today()
    new_end = max(end, P.ist_today()) + timedelta(days=days)
    db[PRODUCTS].update_one({"_id": doc["_id"]}, {"$set": {
        "end_date": new_end, "status": "approved", "is_active": True,
        "admin_extended": True, "extended_at": datetime.utcnow(), "updated_at": datetime.utcnow()}})
    return {"ok": True, "new_end_date": new_end.isoformat(), "extended_days": days}


# ── reporting ──────────────────────────────────────────────────────────────────
@router.get("/products/summary")
def summary(a=Depends(get_current_admin)):
    today = P.ist_today()
    base = {"status": {"$ne": "deleted"}}
    c = lambda q: db[PRODUCTS].count_documents({**base, **q})
    return {
        "total": c({}),
        "live": c({"status": "approved", "end_date": {"$gte": today}}),
        "expired": c({"$or": [{"status": "expired"}, {"end_date": {"$lt": today}}]}),
        "pending": c({"status": "pending"}),
        "free": c({"pricing_tier": "free"}), "paid": c({"pricing_tier": "paid"}),
        "free_product_limit": P.get_settings()["free_product_limit"],
    }


@router.get("/products/revenue")
def revenue(a=Depends(get_current_admin)):
    """Money collected through product orders (create + renew) that were paid."""
    now = datetime.utcnow()
    today, month, epoch = datetime(now.year, now.month, now.day), datetime(now.year, now.month, 1), datetime(2020, 1, 1)

    def _sum(since):
        r = list(db[ORDERS].aggregate([
            {"$match": {"status": "paid", "total": {"$gt": 0}, "created_at": {"$gte": since}}},
            {"$group": {"_id": None, "t": {"$sum": "$total"}}}]))
        return r[0]["t"] if r else 0
    return {"today": _sum(today), "this_month": _sum(month), "lifetime": _sum(epoch)}


@router.get("/products/expiring")
def expiring(days: int = 7, a=Depends(get_current_admin)):
    today = P.ist_today()
    docs = db[PRODUCTS].find({"status": "approved", "end_date": {"$gte": today, "$lte": today + timedelta(days=days)}}
                             ).sort("end_date", 1).limit(100)
    return [{"id": str(d["_id"]), "title": d.get("title", ""), "merchant_name": d.get("merchant_name", ""),
             "merchant_phone": d.get("merchant_phone", ""), "store_name": d.get("store_name", ""),
             "end_date": _ymd(d["end_date"]), "days_left": max(0, (d["end_date"] - today).days)} for d in docs]


@router.get("/products/top-merchants")
def top_merchants(limit: int = 10, a=Depends(get_current_admin)):
    rows = db[PRODUCTS].aggregate([
        {"$match": {"status": "approved", "merchant_id": {"$ne": ""}}},
        {"$group": {"_id": "$merchant_id", "merchant_name": {"$first": "$merchant_name"},
                    "merchant_phone": {"$first": "$merchant_phone"}, "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}, {"$limit": limit}])
    return [{"merchant_id": str(r["_id"]), "merchant_name": r.get("merchant_name", ""),
             "merchant_phone": r.get("merchant_phone", ""), "product_count": r["count"]} for r in rows]


# ── reviews (moderation lives in admin.py; this is the listing that resolves titles) ──
@router.get("/product-reviews")
def list_reviews(a=Depends(get_current_admin)):
    reviews = list(db.product_reviews.find().sort("created_at", -1))
    oids = []
    for pid in {r.get("product_id") for r in reviews if r.get("product_id")}:
        try:
            oids.append(ObjectId(pid))
        except Exception:
            pass
    info = {str(p["_id"]): p for p in db[PRODUCTS].find({"_id": {"$in": oids}}, {"title": 1, "store_name": 1})} if oids else {}
    for r in reviews:
        r["_id"] = str(r["_id"])
        p = info.get(r.get("product_id", ""), {})
        r["product_title"] = p.get("title", "")
        r["store_name"] = p.get("store_name", "")
        r["visible"] = r.get("visible", True)
    return reviews
