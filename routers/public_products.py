"""
Customer-facing Product routes (no auth required unless noted).
Collection: merchant_products. Customers never see pricing tier / slots.

A product is visible to customers only when ALL hold:
  status == approved, is_active, started (start_date <= now), not past its end date.
"""
import re
from datetime import datetime

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Request as _Req

from database import db
from services import products as P
from routers.public import _get_user_optional

router = APIRouter(tags=["Products (public)"])
PRODUCTS = P.PRODUCTS


def _visible(extra: dict = None) -> dict:
    q = {"status": "approved", "is_active": {"$ne": False},
         "end_date": {"$gte": P.ist_today()}, "start_date": {"$lte": P.ist_now()}}
    if extra:
        q.update(extra)
    return q


def _store_clauses(store_id: str):
    c = [{"store_id": store_id}]
    try:
        c.append({"store_id": ObjectId(store_id)})
    except Exception:
        pass
    return {"$or": c}


def _store_info(sid: str) -> dict:
    try:
        s = db.stores.find_one({"_id": ObjectId(sid)}, {"store_name": 1, "city": 1, "rating": 1, "admin_rating": 1})
    except Exception:
        s = None
    return s or {}


def _dstr(d):
    return d.strftime("%d %b %Y") if hasattr(d, "strftime") else str(d or "")


def _card(p: dict) -> dict:
    return {"_id": str(p["_id"]), "title": p.get("title", ""), "offer_text": p.get("description", ""),
            "logo_url": p.get("image_url", ""), "price": str(p.get("price", "") or ""),
            "original_price": str(p.get("original_price", "") or ""),
            "rating": float(p.get("rating") or 0)}


# ── Discover Products (home) ───────────────────────────────────────────────────
@router.get("/products-public")
def products_public(city: str = ""):
    wanted = city.strip().lower()
    out = []
    for p in db[PRODUCTS].find(_visible()).sort("_id", -1):
        sid = str(p.get("store_id", ""))
        st = _store_info(sid) if sid else {}
        pcity = p.get("city") or st.get("city", "")
        if wanted:
            dc = (pcity or "").strip().lower()
            if not dc or not (re.search(re.escape(wanted), dc) or re.search(re.escape(dc), wanted)):
                continue
        img = p.get("image_url", "")
        out.append({
            "id": str(p["_id"]), "title": p.get("title", ""), "text": p.get("description", ""),
            "validity": _dstr(p.get("end_date")), "logo": img, "logo_url": img,
            "store_id": sid, "store_name": st.get("store_name") or p.get("store_name", ""), "city": pcity,
            "from_date": _dstr(p.get("start_date")), "end_date": _dstr(p.get("end_date")),
            "price": str(p.get("price", "") or ""), "original_price": str(p.get("original_price", "") or ""),
            "sale_price": str(p.get("price", "") or ""), "discount_label": p.get("discount_label", ""),
            "is_active": True, "source": p.get("source", "merchant"),
            "rating": float(st.get("rating") or st.get("admin_rating") or 0),
            "rating_count": int(p.get("rating_count") or 0),
        })
    return out


# ── Store detail / More from store ─────────────────────────────────────────────
def store_products(store_id: str, limit: int = 30) -> list:
    """Used by the store-detail bundle in public.py as well as /products/by-store."""
    rating = _store_info(store_id)
    r = float(rating.get("rating") or rating.get("admin_rating") or 0)
    rows = []
    for p in db[PRODUCTS].find(_visible(_store_clauses(store_id))).sort("created_at", -1).limit(limit):
        c = _card(p)
        c.update({"discount": p.get("discount_label", ""), "validity": _dstr(p.get("end_date")),
                  "end_date": _dstr(p.get("end_date")), "rating": r})
        rows.append(c)
    return rows


@router.get("/products/by-store/{store_id}")
def products_by_store(store_id: str, limit: int = 20):
    return store_products(store_id, limit)[:limit]


@router.get("/products/{pid}/similar")
def similar_products(pid: str, limit: int = 6):
    try:
        src = db[PRODUCTS].find_one({"_id": ObjectId(pid)})
    except Exception:
        src = None
    if not src:
        return []
    extra = {"_id": {"$ne": src["_id"]}}
    if src.get("city"):
        extra["city"] = {"$regex": f"^{re.escape(src['city'])}$", "$options": "i"}
    return [_card(p) for p in db[PRODUCTS].find(_visible(extra)).sort("_id", -1).limit(limit)]


# ── Events ─────────────────────────────────────────────────────────────────────
@router.post("/products/{pid}/track")
def track_product_event(pid: str, data: dict):
    """Owner is resolved server-side from the product; client merchant ids are ignored."""
    event = data.get("event", "view")
    if event not in ("view", "share", "open"):
        event = "view"
    try:
        oid = ObjectId(pid)
    except Exception:
        return {"ok": True}
    doc = db[PRODUCTS].find_one({"_id": oid}, {"merchant_id": 1})
    if not doc:
        return {"ok": True}
    db[PRODUCTS].update_one({"_id": oid}, {"$inc": {f"{event}_count": 1}})
    db.product_events.insert_one({"product_id": pid, "merchant_id": str(doc.get("merchant_id") or ""),
                                  "event": event, "created_at": datetime.utcnow()})
    return {"ok": True}


# ── Reviews ────────────────────────────────────────────────────────────────────
@router.get("/products/{pid}/reviews")
def get_product_reviews(pid: str, limit: int = 10, skip: int = 0):
    try:
        ObjectId(pid)
    except Exception:
        return {"reviews": [], "total": 0}
    total = db.product_reviews.count_documents({"product_id": pid})
    reviews = list(db.product_reviews.find({"product_id": pid}).sort("created_at", -1).skip(skip).limit(limit))
    for r in reviews:
        r["_id"] = str(r["_id"])
    return {"reviews": reviews, "total": total}


@router.post("/products/{pid}/review")
def submit_product_review(pid: str, data: dict, request: _Req):
    try:
        oid = ObjectId(pid)
    except Exception:
        raise HTTPException(400, "Invalid product_id")
    if not db[PRODUCTS].find_one({"_id": oid}, {"_id": 1}):
        raise HTTPException(404, "Product not found")
    rating = float(data.get("rating", 0))
    text = (data.get("text", "") or "").strip()
    if not (1 <= rating <= 5):
        raise HTTPException(400, "Rating must be 1–5")
    if len(text) < 3:
        raise HTTPException(400, "Review text too short (min 3 chars)")
    user = _get_user_optional(request)
    if not user:
        raise HTTPException(401, "Session expired")
    user_id = str(user["_id"])
    user_name = (user.get("name") or user.get("full_name") or "").strip() \
        or (data.get("user_name", "") or "").strip() or "Anonymous"
    now = datetime.utcnow().isoformat()
    db.product_reviews.update_one(
        {"product_id": pid, "user_id": user_id},
        {"$set": {"product_id": pid, "user_id": user_id, "user_name": user_name,
                  "rating": rating, "text": text, "updated_at": now},
         "$setOnInsert": {"created_at": now}}, upsert=True)
    revs = list(db.product_reviews.find({"product_id": pid}, {"rating": 1}))
    avg = round(sum(r["rating"] for r in revs) / len(revs), 1) if revs else rating
    db[PRODUCTS].update_one({"_id": oid}, {"$set": {"rating": avg, "rating_count": len(revs)}})
    return {"ok": True, "message": "Review submitted!", "avg_rating": avg}


@router.get("/products/{pid}/my-review")
def my_product_review(pid: str, request: _Req):
    user = _get_user_optional(request)
    if not user:
        return {}
    try:
        ObjectId(pid)
    except Exception:
        return {}
    rev = db.product_reviews.find_one({"product_id": pid, "user_id": str(user["_id"])})
    if not rev:
        return {}
    rev["_id"] = str(rev["_id"])
    return rev
