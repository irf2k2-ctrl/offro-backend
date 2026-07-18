from fastapi import APIRouter, Request as _Req
from database import db
from bson import ObjectId

router = APIRouter(tags=["Public"])

# =================== PUBLIC STORES LIST ===================
@router.get("/stores")
def get_stores(city: str = None, category: str = None):
    """Public endpoint - Flutter app fetches this"""
    query = {"status": "active"}
    if city:
        query["city"] = {"$regex": city, "$options": "i"}
    if category and category.strip() and category.strip() != "All":
        # Flexible case-insensitive contains match:
        # "Restaurant" matches "Restaurant", "Restaurants", "Indian Restaurant", "Restaurant / Bar"
        # Use re.escape so special chars in category names don't break the regex
        import re as _re
        _cat_pat = _re.escape(category.strip())
        query["category"] = {"$regex": _cat_pat, "$options": "i"}

    stores = list(db.stores.find(query, {
        "store_name":1,"category":1,"city":1,"area":1,"address":1,"phone":1,
        "image":1,"image_url":1,"image_thumb":1,"_thumb":1,"store_image":1,"store_image2":1,"images":1,"status":1,"points_per_scan":1,
        "lat":1,"lng":1,"rating":1,"admin_rating":1,"is_new_in_town":1,"is_trending":1,"is_popular":1,"badge":1,"merchant_id":1,
        "tags":1,"favorite_count":1,"favorites":1,"view_count":1,"views":1,
        "created_at":1,"late_night":1
    }))
    if not stores:
        return []

    # Pre-fetch ALL active deals in ONE query — no N+1
    store_ids = [str(s["_id"]) for s in stores]

    # ── SUBSCRIPTION FILTER: remove stores with expired/no subscription ──
    from datetime import datetime as _dt
    from bson import ObjectId as _OId
    _now = _dt.utcnow()
    # FIX: subscription store_id may be stored as ObjectId OR string — query both
    _oid_store_ids = []
    for _sid in store_ids:
        try: _oid_store_ids.append(_OId(_sid))
        except Exception: pass
    _sub_query = {"store_id": {"$in": store_ids + _oid_store_ids}}
    _active_subs = set()
    for _sub in db.subscriptions.find(_sub_query, {"store_id": 1, "end_date": 1, "status": 1}):
        _ed = _sub.get("end_date")
        _sid_str = str(_sub["store_id"])  # str(ObjectId) gives hex; str(str) is a no-op
        if _ed is None:
            # No end_date = perpetual/lifetime subscription — always active
            _active_subs.add(_sid_str)
            continue
        try:
            _ed_dt = _ed if isinstance(_ed, _dt) else _dt.fromisoformat(str(_ed).replace("Z",""))
            if _ed_dt >= _now:
                _active_subs.add(_sid_str)
        except Exception:
            pass
    # If a store has no subscription record at all it is NOT shown publicly
    stores = [s for s in stores if str(s["_id"]) in _active_subs]
    store_ids = [str(s["_id"]) for s in stores]
    if not stores:
        return []

    cols = db.list_collection_names()  # called ONCE, not per-store
    deals_by_store = {}
    if "deals" in cols:
        all_deals = list(db.deals.find(
            {"store_id": {"$in": store_ids}, "status": "active"},
            {"store_id":1,"title":1,"discount":1,"discount_percent":1}
        ))
        for d in all_deals:
            sid = d.get("store_id","")
            if sid not in deals_by_store:
                deals_by_store[sid] = []
            deals_by_store[sid].append(d)

    result = []
    for s in stores:
        store_id = str(s["_id"])
        deals = deals_by_store.get(store_id, [])
        deal_count = len(deals)
        deal_summary = None
        if deals:
            d = deals[0]
            disc = d.get("discount") or d.get("discount_percent")
            try:
                disc_val = int(float(str(disc))) if disc not in (None, "", "null") else None
            except (ValueError, TypeError):
                disc_val = None
            if disc_val and disc_val > 0:
                deal_summary = f"{disc_val}% off — {d.get('title','')}"
            elif d.get("title"):
                deal_summary = d.get("title","")

        admin_rating = s.get("admin_rating")
        raw_rating   = s.get("rating", 0) or 0
        display_rating = float(admin_rating) if admin_rating else float(raw_rating)
        result.append({
            "_id": store_id,
            "store_name": s.get("store_name"),
            "category": s.get("category", ""),
            "city": s.get("city", ""),
            "area": s.get("area", ""),
            "address": s.get("address", ""),
            "phone": s.get("phone", ""),
            "image":       (s.get("image_url") or s.get("image_thumb") or
                            s.get("image") or s.get("_thumb") or s.get("store_image") or
                            ((s.get("images") or [None])[0]) or None),
            "image_url":   (s.get("image_url") or s.get("image_thumb") or
                            s.get("image") or s.get("_thumb") or ""),
            "image_thumb": (s.get("image_thumb") or s.get("image_url") or
                            s.get("image") or s.get("_thumb") or ""),
            "image2": s.get("store_image2") or None,
            "images": s.get("images", []),
            "status": s.get("status", "active"),
            "visit_points": s.get("points_per_scan", 10),
            "points_per_scan": s.get("points_per_scan", 10),
            "latitude": s.get("lat") or None,
            "longitude": s.get("lng") or None,
            "rating": display_rating,
            "offer":      deal_summary,
            "deal_count":    deal_count,
            "is_new_in_town": s.get("is_new_in_town", False),
            "is_trending":    s.get("is_trending", False),
            "is_popular":     s.get("is_popular", False),
            "badge": s.get("badge", ""),
            "merchant_id": s.get("merchant_id", ""),
            "tags":           s.get("tags", []),
            "favorite_count": int(s.get("favorite_count") or s.get("favorites") or 0),
            "view_count":     int(s.get("view_count") or s.get("views") or 0),
            "created_at":     str(s.get("created_at", "") or ""),
            "late_night":     bool(s.get("late_night", False)),
        })
    return result

# =================== SINGLE STORE ===================
@router.get("/stores/{store_id}")
def get_store(store_id: str):
    from fastapi import HTTPException as _HTTPEx
    try:
        store = db.stores.find_one({"_id": ObjectId(store_id)})
    except Exception:
        raise _HTTPEx(status_code=400, detail="Invalid store_id")
    if not store:
        raise _HTTPEx(status_code=404, detail="Store not found")

    cols = db.list_collection_names()

    # Deals
    deals = list(db.deals.find({"store_id": store_id, "status": "active"})) \
        if "deals" in cols else []
    deals_list = [{
        "title":       d.get("title"),
        "discount":    d.get("discount"),
        "category":    d.get("category"),
        "description": d.get("description"),
        "start_date":  d.get("start_date"),
        "end_date":    d.get("end_date"),
    } for d in deals]

    # Products for this store — from merchant_vouchers + gift_vouchers (active/non-expired only)
    from datetime import datetime as _dt
    _pnow = _dt.utcnow()
    products_list = []
    seen_product_ids = set()

    def _prod_expired(p):
        """Return True if the product's end_date has passed."""
        ed = p.get("end_date") or p.get("validity_end") or ""
        if not ed:
            return False
        try:
            ed_dt = ed if isinstance(ed, _dt) else _dt.fromisoformat(str(ed)[:19].replace(" ","T"))
            return ed_dt < _pnow
        except Exception:
            return False

    def _prod_img(p):
        for k in ["logo_url","logo_thumb","image_url","logo","image"]:
            v = str(p.get(k,"") or "")
            if v.startswith("http"): return v
        return ""

    # 1. merchant_vouchers — approved, not expired
    if "merchant_vouchers" in cols:
        from bson import ObjectId as _OId2
        merchant_id = store.get("merchant_id", "")
        merchant_phone = str(store.get("phone", ""))
        q = {"status": "approved"}
        if merchant_id:
            # FIX: merchant_id may be ObjectId or string — match both
            _mid_oid = None
            try: _mid_oid = _OId2(merchant_id)
            except Exception: pass
            _or_clauses = [{"merchant_id": merchant_id}, {"merchant_phone": merchant_phone}]
            if _mid_oid: _or_clauses.append({"merchant_id": _mid_oid})
            q = {"$or": _or_clauses, "status": "approved"}
        for p in db.merchant_vouchers.find(q).sort("created_at", -1).limit(30):
            if _prod_expired(p): continue
            pid = str(p["_id"])
            if pid in seen_product_ids: continue
            seen_product_ids.add(pid)
            products_list.append({
                "_id":            pid,
                "title":          p.get("title", ""),
                "offer_text":     p.get("offer_text", ""),
                "logo_url":       _prod_img(p),
                "price":          str(p.get("price","") or ""),
                "original_price": str(p.get("original_price","") or ""),
                "discount":       str(p.get("discount_label","") or ""),
                "validity":       (str(p.get("end_date","") or "")[:10]) or (p.get("validity","") or ""),
                "end_date":       str(p.get("end_date","") or ""),
                # FIX: this endpoint never returned 'rating', so the Store
                # Detail product cards (which read this list, not
                # /products/by-store/) always showed no rating overlay even
                # after a review was submitted and averaged elsewhere.
                "rating":         float(p.get("rating") or p.get("avg_rating") or 0),
            })

    # 2. gift_vouchers — linked to this store_id, active
    if "gift_vouchers" in cols:
        from bson import ObjectId as _OId3
        # FIX: gift_vouchers.store_id may be ObjectId or string — match both
        _gv_clauses = [{"store_id": store_id}]
        try: _gv_clauses.append({"store_id": _OId3(store_id)})
        except Exception: pass
        _gv_q = {"$or": _gv_clauses}
        for p in db.gift_vouchers.find(_gv_q).sort("_id", -1).limit(30):
            if p.get("is_active", True) in (False, "false", "0", 0): continue
            if _prod_expired(p): continue
            pid = str(p["_id"])
            if pid in seen_product_ids: continue
            seen_product_ids.add(pid)
            products_list.append({
                "_id":            pid,
                "title":          p.get("title",""),
                "offer_text":     p.get("text","") or p.get("offer_text",""),
                "logo_url":       _prod_img(p),
                "price":          str(p.get("price","") or ""),
                "original_price": str(p.get("original_price","") or ""),
                "discount":       "",
                "validity":       (str(p.get("end_date","") or "")[:10]) or (p.get("validity","") or ""),
                "end_date":       str(p.get("end_date","") or ""),
                # FIX: this endpoint never returned 'rating' (see merchant_vouchers block above)
                "rating":         float(p.get("rating") or p.get("avg_rating") or 0),
            })

    # Rating count
    rating_count = db.ratings.count_documents({"store_id": store_id}) \
        if "ratings" in cols else 0

    # Review count
    review_count = db.reviews.count_documents({"store_id": store_id}) \
        if "reviews" in cols else 0

    # Image resolution
    image_url   = (store.get("image_url") or store.get("image_thumb") or
                   store.get("image") or store.get("_thumb") or "")
    image_thumb = (store.get("image_thumb") or store.get("image_url") or
                   store.get("image") or "")

    return {
        "_id":          str(store["_id"]),
        "store_name":   store.get("store_name"),
        "category":     store.get("category", ""),
        "city":         store.get("city", ""),
        "area":         store.get("area", ""),
        "address":      store.get("address", ""),
        "phone":        store.get("phone", ""),
        "image":        store.get("image") or None,
        "image_url":    image_url,
        "image_thumb":  image_thumb,
        "image2":       store.get("store_image2") or store.get("image2") or None,
        "images":       store.get("images", []),
        "latitude":     store.get("lat") or None,
        "longitude":    store.get("lng") or None,
        "visit_points": store.get("points_per_scan", 10),
        "rating":       float(store.get("admin_rating") or store.get("rating") or 0),
        "rating_count": rating_count,
        "review_count": review_count,
        "about":        store.get("about") or store.get("description") or "",
        "tags":         store.get("tags", []),
        "open_time":    store.get("open_time", ""),
        "close_time":   store.get("close_time", ""),
        "cost_for_two": store.get("cost_for_two", ""),
        "dine_in":      store.get("dine_in", False),
        "is_trending":  store.get("is_trending", False),
        "is_new_in_town": store.get("is_new_in_town", False),
        "is_popular":   store.get("is_popular", False),
        "badge":        store.get("badge", ""),
        "deals":        deals_list,
        "products":     products_list,
    }


# =================== STORE REVIEWS ===================

@router.get("/stores/{store_id}/reviews")
def get_store_reviews(store_id: str, limit: int = 10, skip: int = 0):
    """Public: fetch paginated reviews for a store."""
    from fastapi import HTTPException as _HTTPEx
    cols = db.list_collection_names()
    if "reviews" not in cols:
        return {"reviews": [], "total": 0}
    total = db.reviews.count_documents({"store_id": store_id})
    reviews = list(
        db.reviews.find({"store_id": store_id})
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )
    result = []
    for r in reviews:
        result.append({
            "_id":        str(r["_id"]),
            "store_id":   r.get("store_id", ""),
            "user_name":  r.get("user_name", "Anonymous"),
            "rating":     float(r.get("rating", 0)),
            "text":       r.get("text", ""),
            "created_at": str(r.get("created_at", "")),
        })
    return {"reviews": result, "total": total}


@router.post("/stores/{store_id}/review")
def submit_store_review(store_id: str, data: dict, request: _Req):
    """Authenticated user submits a review (rating + text)."""
    from fastapi import HTTPException as _HTTPEx
    try:
        store = db.stores.find_one({"_id": ObjectId(store_id)})
    except Exception:
        raise _HTTPEx(400, "Invalid store_id")
    if not store:
        raise _HTTPEx(404, "Store not found")

    rating = float(data.get("rating", 0))
    text   = (data.get("text", "") or "").strip()
    if not (1 <= rating <= 5):
        raise _HTTPEx(400, "Rating must be 1–5")
    if len(text) < 3:
        raise _HTTPEx(400, "Review text too short (min 3 chars)")

    # Optional auth — if token provided, use real name; else use provided name
    user     = _get_user_optional(request)
    user_id  = str(user["_id"]) if user else None
    user_name = (user.get("name") or user.get("full_name") or "").strip() if user else ""
    if not user_name:
        user_name = (data.get("user_name", "") or "").strip() or "Anonymous"

    # One review per user per store (upsert by user_id if known, else always insert)
    from datetime import datetime as _dt
    if user_id:
        db.reviews.update_one(
            {"store_id": store_id, "user_id": user_id},
            {"$set": {
                "store_id":   store_id,
                "user_id":    user_id,
                "user_name":  user_name,
                "rating":     rating,
                "text":       text,
                "updated_at": _dt.utcnow().isoformat(),
            }, "$setOnInsert": {"created_at": _dt.utcnow().isoformat()}},
            upsert=True
        )
    else:
        db.reviews.insert_one({
            "store_id":   store_id,
            "user_id":    None,
            "user_name":  user_name,
            "rating":     rating,
            "text":       text,
            "created_at": _dt.utcnow().isoformat(),
        })

    # Update store avg rating (includes reviews)
    all_ratings = list(db.ratings.find({"store_id": store_id}, {"rating": 1}))
    all_reviews = list(db.reviews.find({"store_id": store_id}, {"rating": 1}))
    combined = [r["rating"] for r in all_ratings] + [r["rating"] for r in all_reviews]
    avg = round(sum(combined) / len(combined), 1) if combined else rating
    if not store.get("admin_rating"):
        db.stores.update_one(
            {"_id": ObjectId(store_id)},
            {"$set": {"rating": avg}}
        )
    return {"ok": True, "message": "Review submitted!", "avg_rating": avg}


# ─── PRODUCT REVIEWS ─────────────────────────────────────────────────────────

@router.get("/products/{pid}/reviews")
def get_product_reviews(pid: str, limit: int = 10, skip: int = 0):
    """Public: fetch paginated reviews for a product."""
    try:
        ObjectId(pid)
    except Exception:
        return {"reviews": [], "total": 0}
    total   = db.product_reviews.count_documents({"product_id": pid})
    reviews = list(
        db.product_reviews.find({"product_id": pid})
        .sort("created_at", -1).skip(skip).limit(limit)
    )
    for r in reviews:
        r["_id"] = str(r["_id"])
    return {"reviews": reviews, "total": total}


@router.post("/products/{pid}/review")
def submit_product_review(pid: str, data: dict, request: _Req):
    """Authenticated: submit or update a product review (one per user)."""
    from fastapi import HTTPException as _HTTPEx
    try:
        oid = ObjectId(pid)
    except Exception:
        raise _HTTPEx(400, "Invalid product_id")
    product = (
        db.products.find_one({"_id": oid}) or
        db.gift_vouchers.find_one({"_id": oid}) or
        db.merchant_vouchers.find_one({"_id": oid})
    )
    if not product:
        raise _HTTPEx(404, "Product not found")

    rating = float(data.get("rating", 0))
    text   = (data.get("text", "") or "").strip()
    if not (1 <= rating <= 5):
        raise _HTTPEx(400, "Rating must be 1–5")
    if len(text) < 3:
        raise _HTTPEx(400, "Review text too short (min 3 chars)")

    # FIX: this used to fall back to user_id=None ("Anonymous") and still
    # return {"ok": True} whenever the token was missing/expired — so a
    # review with an expired session looked identical to a successful save,
    # but was never attached to the user's account. get_my_product_review
    # (also user-scoped) could then never find it again, making the review
    # appear to silently vanish. Now an invalid/expired session is rejected
    # the same way toggle_product_favorite already rejects it, so the app can
    # show "Session expired" and the user knows to log in again.
    user = _get_user_optional(request)
    if not user:
        raise _HTTPEx(401, "Session expired")
    user_id   = str(user["_id"])
    user_name = (user.get("name") or user.get("full_name") or "").strip()
    if not user_name:
        user_name = (data.get("user_name", "") or "").strip() or "Anonymous"

    from datetime import datetime as _dt
    db.product_reviews.update_one(
        {"product_id": pid, "user_id": user_id},
        {"$set": {
            "product_id": pid,
            "user_id":    user_id,
            "user_name":  user_name,
            "rating":     rating,
            "text":       text,
            "updated_at": _dt.utcnow().isoformat(),
        }, "$setOnInsert": {"created_at": _dt.utcnow().isoformat()}},
        upsert=True,
    )

    all_revs = list(db.product_reviews.find({"product_id": pid}, {"rating": 1}))
    avg      = round(sum(r["rating"] for r in all_revs) / len(all_revs), 1) if all_revs else rating
    _rating_upd = {"$set": {"rating": avg, "rating_count": len(all_revs)}}
    db.products.update_one({"_id": oid}, _rating_upd)
    db.gift_vouchers.update_one({"_id": oid}, _rating_upd)
    db.merchant_vouchers.update_one({"_id": oid}, _rating_upd)
    return {"ok": True, "message": "Review submitted!", "avg_rating": avg}


@router.get("/products/{pid}/my-review")
def get_my_product_review(pid: str, request: _Req):
    """Authenticated: return the current user's review for a product (if any)."""
    user = _get_user_optional(request)
    if not user:
        return {}
    user_id = str(user["_id"])
    try:
        ObjectId(pid)
    except Exception:
        return {}
    rev = db.product_reviews.find_one({"product_id": pid, "user_id": user_id})
    if not rev:
        return {}
    rev["_id"] = str(rev["_id"])
    return rev


# =================== PUBLIC CATEGORIES ===================
# ── City → Areas mapping (predefined) ──────────────────────────────
CITY_AREAS = {
    "ballari": [
        "Cowl Bazaar", "Gandhi Nagar", "Cantonment", "Bellary Fort", "M.G. Road",
        "Hosapete Road", "Civil Station", "Sanganakal Road", "Shivappa Nayaka Circle",
        "Humnabad Road", "Kudligi Road", "Raichur Road", "Navalagunda Road",
        "Kottur", "Hirekerur", "Kampli", "Siruguppa"
    ],
    "bengaluru": [
        "Indiranagar", "Koramangala", "Jayanagar", "Whitefield", "HSR Layout",
        "Marathahalli", "BTM Layout", "Electronic City", "Bannerghatta Road",
        "JP Nagar", "Rajajinagar", "Malleshwaram", "Yelahanka", "Hebbal",
        "Domlur", "Bellandur", "Sarjapur", "Kadubeesanahalli", "Bellandur",
        "Majestic", "KR Market", "Shivajinagar", "Frazer Town"
    ],
    "hyderabad": [
        "Banjara Hills", "Jubilee Hills", "Gachibowli", "Hitech City",
        "Madhapur", "Ameerpet", "Begumpet", "Secunderabad", "Dilsukhnagar",
        "LB Nagar", "Kukatpally", "Miyapur", "Kondapur", "Manikonda",
        "Tolichowki", "Mehdipatnam", "Abids", "Nampally", "Koti"
    ],
    "hubli": [
        "Old Hubli", "Vidyanagar", "Deshpande Nagar", "Keshwapur",
        "Gokul Road", "Navanagar", "Unkal", "Akkipet", "Kalidas Nagar"
    ],
    "dharwad": [
        "PB Road", "Saraswathipuram", "Sadashivnagar", "Shirur Park",
        "Shivaji Nagar", "Hubli Road", "Saptapur", "Gabhur"
    ],
    "mysuru": [
        "Jayalakshmipuram", "Vijayanagar", "Kuvempunagar", "Gokulam",
        "Saraswathipuram", "Nazarabad", "Chamrajpura", "Krishnamurthypuram"
    ],
}


@router.get("/deals/all")
def get_all_active_deals(city: str = ""):
    """Return only active store deals (offers) for the Hot Deals screen.
    Products are excluded. Each deal includes store image, name, area, validity.
    """
    from datetime import datetime as _dt
    _now = _dt.utcnow()

    # Step 1: Active subscription store IDs
    _active_store_ids = set()
    for _sub in db.subscriptions.find({}, {"store_id": 1, "end_date": 1}):
        _ed = _sub.get("end_date")
        if _ed is None:
            # No end_date = perpetual/lifetime subscription — always active
            _active_store_ids.add(str(_sub["store_id"]))
            continue
        try:
            _ed_dt = _ed if isinstance(_ed, _dt) else _dt.fromisoformat(str(_ed).replace("Z",""))
            if _ed_dt >= _now:
                _active_store_ids.add(str(_sub["store_id"]))
        except Exception:
            pass

    # Step 2: Active stores in city
    store_q = {"status": "active"}
    if city:
        store_q["city"] = {"$regex": city, "$options": "i"}

    stores_raw = list(db.stores.find(store_q, {
        "store_name": 1, "category": 1, "city": 1, "area": 1, "address": 1, "phone": 1,
        "image_url": 1, "image_thumb": 1, "_thumb": 1, "image": 1, "images": 1,
    }))
    stores_map = {
        str(s["_id"]): s for s in stores_raw
        if str(s["_id"]) in _active_store_ids
    }

    # Step 3: Active deals only (no products)
    result = []
    deal_q = {"status": "active"}
    if stores_map:
        deal_q["store_id"] = {"$in": list(stores_map.keys())}

    for d in db.deals.find(deal_q).sort("created_at", -1).limit(200):
        # Skip truly empty deal records — must have NO title AND NO discount AND NO description
        _t    = (d.get("title") or d.get("deal_name") or "").strip()
        _disc = str(d.get("discount") or d.get("discount_percent") or d.get("offer_percent") or "").strip()
        _desc = (d.get("description") or "").strip()
        if not _t and not _disc and not _desc:
            continue

        sid   = str(d.get("store_id", ""))
        store = stores_map.get(sid)
        if not store:
            continue

        # Validity check — skip expired deals
        end_date = d.get("end_date") or d.get("valid_until") or d.get("expiry") or ""
        if end_date:
            try:
                end_dt = end_date if isinstance(end_date, _dt) else _dt.fromisoformat(str(end_date).replace("Z",""))
                if end_dt < _now:
                    continue
            except Exception:
                pass

        # Resolve store image
        def _store_img(s):
            for k in ("image_url","image_thumb","_thumb","image"):
                v = s.get(k,"") or ""
                if v: return v
            imgs = s.get("images") or []
            return imgs[0] if imgs else ""

        # Format end_date as ISO string and as human-readable validity
        _end_iso = end_date.isoformat() if isinstance(end_date, _dt) else (str(end_date)[:19].replace(" ","T") if end_date else "")
        _validity = ""
        if end_date:
            try:
                _vdt = end_date if isinstance(end_date, _dt) else _dt.fromisoformat(str(end_date)[:19].replace(" ","T"))
                _months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
                _validity = f"{_vdt.day} {_months[_vdt.month - 1]} {_vdt.year}"
            except Exception:
                pass
        result.append({
            "type":        "deal",
            "_id":         str(d["_id"]),
            "title":       d.get("title", d.get("deal_name", "")),
            "discount":    d.get("discount", d.get("offer_percent", "")),
            "description": d.get("description", ""),
            "end_date":    _end_iso,
            "validity":    _validity,
            "store_id":    sid,
            "store_name":  store.get("store_name",""),
            "store_area":  store.get("area",""),
            "store_city":  store.get("city",""),
            "store_address": store.get("address",""),
            "store_phone": store.get("phone",""),
            "image_url":   _store_img(store),
            "category":    d.get("category","") or store.get("category",""),
        })

    return result


# =================== CITIES ===================
@router.get("/cities")
def get_cities():
    """Return list of cities that OFFRO is active in, with optional background image.
    Used by the Flutter home screen hero section to show city imagery.
    Falls back to a default image if no image is configured for the city.
    """
    DEFAULT_CITY_IMAGE = "https://images.unsplash.com/photo-1477959858617-67f85cf4f1df?w=800&q=80"
    # Query supports both old ("active" bool) and new ("status" string) field formats
    docs = list(db.cities.find({
        "$or": [
            {"status": {"$ne": "inactive"}},
            {"active": True, "status": {"$exists": False}},
        ]
    }).sort("sort_order", 1))
    if docs:
        result = []
        for d in docs:
            raw_img = d.get("image_url", "") or d.get("image", "") or ""
            if not (raw_img.startswith("http://") or raw_img.startswith("https://") or raw_img.startswith("data:image")):
                raw_img = DEFAULT_CITY_IMAGE
            result.append({
                "name":       d.get("name", ""),
                "slug":       d.get("slug", d.get("name", "").lower()),
                "image_url":  raw_img,
                "sort_order": d.get("sort_order", 0),
            })
        return result
    # Fallback: derive cities from active stores
    store_cities = db.stores.distinct("city", {"status": "active"})
    result = []
    for c in store_cities:
        if c and str(c).strip():
            result.append({
                "name":      str(c).strip(),
                "slug":      str(c).strip().lower(),
                "image_url": DEFAULT_CITY_IMAGE,
                "sort_order": 0,
            })
    return result

@router.get("/areas")
def get_areas(city: str = ""):
    """Return predefined area list for a given city (case-insensitive)."""
    city_key = city.strip().lower() if city else ""
    # Try exact match first
    if city_key in CITY_AREAS:
        return {"city": city, "areas": CITY_AREAS[city_key]}
    # Try partial match
    for key, areas in CITY_AREAS.items():
        if city_key in key or key in city_key:
            return {"city": city, "areas": areas}
    # Fallback: return areas from DB for this city
    if city_key:
        db_areas = db.stores.distinct("area", {"city": {"$regex": city_key, "$options": "i"}, "area": {"$exists": True, "$ne": ""}})
        if db_areas:
            return {"city": city, "areas": sorted([a for a in db_areas if a])}
    return {"city": city, "areas": []}

@router.get("/categories")
def get_categories():
    """Return ALL admin-created categories for the Flutter app.
    All active categories are returned regardless of store subscription status.
    The /stores endpoint handles its own subscription filtering separately.
    Handles two MongoDB schemas:
      1. Rich: multiple docs each with {name, image_url, icon, subtitle, sort_order}
      2. Legacy: single doc with {categories: ["Grocery","Restaurant",...]}
    """
    # Schema 1: try rich per-document format first
    rich_cats = list(db.categories.find({"name": {"$exists": True}, "status": {"$ne": "deleted"}}, {"_id":0}).sort("sort_order",1))
    if rich_cats:
        result = []
        for cat in rich_cats:
            cat_name = cat.get("name", "")
            if not cat_name:
                continue
            raw_img = cat.get("image_url", "") or ""
            # Pass http URLs AND base64 data URIs — Flutter decodes both
            if raw_img.startswith("http://") or raw_img.startswith("https://") or raw_img.startswith("data:image"):
                safe_img = raw_img
            else:
                safe_img = ""
            result.append({
                "name":       cat_name,
                "subtitle":   cat.get("subtitle", ""),
                "icon":       cat.get("icon", "🏪"),
                "image_url":  safe_img,
                "sort_order": cat.get("sort_order", 0),
            })
        return result
    # Schema 2: legacy single-doc with categories array
    doc = db.categories.find_one({})
    if doc:
        cats_raw = doc.get("categories", [])
        result = []
        for i, item in enumerate(cats_raw):
            if isinstance(item, dict) and "name" in item:
                raw_img = item.get("image_url", "") or ""
                if raw_img.startswith("http://") or raw_img.startswith("https://") or raw_img.startswith("data:image"):
                    safe_img = raw_img
                else:
                    safe_img = ""
                result.append({
                    "name":       item.get("name", ""),
                    "subtitle":   item.get("subtitle", ""),
                    "icon":       item.get("icon", "🏪"),
                    "image_url":  safe_img,
                    "sort_order": item.get("sort_order", i+1),
                })
            elif isinstance(item, str):
                result.append({"name": item, "subtitle": "", "icon": "🏪", "image_url": "", "sort_order": i+1})
        if result:
            return result
    # Ultimate fallback
    fallback = ["Grocery","Restaurant","Pharmacy","Electronics","Clothing","Bakery","Salon","Other"]
    return [{"name":n,"subtitle":"","icon":"🏪","image_url":"","sort_order":i+1} for i,n in enumerate(fallback)]

# =================== PUBLIC CATEGORIES ALIASES ===================
# Flutter's fetchCategories() tries /public/categories then /public-categories
# before falling back to hardcoded defaults. These aliases delegate to /categories.
@router.get("/public/categories")
def get_public_categories_alias():
    return get_categories()

@router.get("/public-categories")
def get_public_categories_hyphen_alias():
    return get_categories()

# =================== PUBLIC TERMS ===================
@router.get("/terms/{type}")
def get_terms_public(type: str):
    if type not in ("merchant", "user"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="type must be merchant or user")
    doc = db.terms.find_one({"type": type}) or {}
    return {"type": type, "content": doc.get("content", "")}


# =================== TERMS ===================
# /terms/{type} handled above

def _nominatim_reverse(lat: float, lng: float) -> dict:
    """Call Nominatim reverse geocoding and return state/city/area/address."""
    import requests as _req, urllib.parse
    try:
        r = _req.get(
            f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lng}&format=json&addressdetails=1",
            headers={"User-Agent": "OffrO/2.0 (location-based deals app)"},
            timeout=8,
        )
        data = r.json()
        addr = data.get("address", {})
        state   = addr.get("state", "")
        city    = (addr.get("city") or addr.get("town") or addr.get("county") or addr.get("village") or "")
        area    = (addr.get("suburb") or addr.get("neighbourhood") or addr.get("quarter") or addr.get("locality") or "")
        address = data.get("display_name", "")[:250]
        return {"state": state, "city": city, "area": area, "address": address}
    except Exception:
        return {}


@router.get("/reverse-geocode")
def reverse_geocode_endpoint(lat: float, lng: float):
    """Reverse geocode lat/lng → state, city, area, address via Nominatim."""
    result = _nominatim_reverse(lat, lng)
    if not result:
        return {"error": "Could not resolve location details. Check coordinates and try again."}
    return result


@router.get("/resolve-maps-link")
def resolve_maps_link(url: str):
    import re, urllib.parse
    import requests as _req

    raw_url = url.strip()
    final_url = raw_url

    # ── Step 1: Follow redirects (handles maps.app.goo.gl short links) ──
    try:
        resp = _req.get(
            raw_url,
            timeout=10,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Mobile Safari/537.36"
                )
            },
        )
        final_url = resp.url
    except Exception:
        pass

    lat, lng = None, None

    # ── Step 2: Extract coords from expanded URL ──
    for pat in [
        r'[!;]3d(-?\d+\.\d+)[!;]4d(-?\d+\.\d+)',
        r'@(-?\d+\.\d+),(-?\d+\.\d+)',
        r'[?&](?:q|query)=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)',
        r'[?&]ll=(-?\d+\.\d+),(-?\d+\.\d+)',
        r'/maps/place/[^/@?]+/@?(-?\d+\.\d+),(-?\d+\.\d+)',
    ]:
        m = re.search(pat, final_url)
        if m:
            try:
                la, ln = float(m.group(1)), float(m.group(2))
                if la != 0.0 and ln != 0.0 and abs(la) <= 90 and abs(ln) <= 180:
                    lat, lng = la, ln
                    break
            except Exception:
                pass

    place_name = ""
    m = re.search(r'/maps/place/([^/@?]+)', final_url)
    if m:
        raw = m.group(1).replace('+', ' ')
        place_name = urllib.parse.unquote(raw).strip()

    if lat is None or lng is None:
        return {
            "error": (
                "Could not extract coordinates from this link. "
                "In Google Maps, tap Share → Copy link, then paste the full link here."
            )
        }

    # ── Step 3: Nominatim reverse geocode for address auto-fill ──
    geo = _nominatim_reverse(lat, lng)

    return {
        "lat":        round(lat, 6),
        "lng":        round(lng, 6),
        "place_name": place_name,
        "maps_url":   final_url,
        "state":      geo.get("state", ""),
        "city":       geo.get("city", ""),
        "area":       geo.get("area", ""),
        "address":    geo.get("address", ""),
    }


@router.get("/policy/{policy_type}")
def get_policy(policy_type: str):
    doc = db.policies.find_one({"type": policy_type}) or {}
    content = doc.get("content", "")
    if not content:
        defaults = {"privacy": _default_privacy(), "refund": _default_refund(), "kyc": _default_kyc()}
        content = defaults.get(policy_type, "")
    return {"content": content}

# =================== SOCIAL LINKS (public) ===================
@router.get("/social")
def get_social_public():
    doc = db.settings.find_one({"key": "social_links"}) or {}
    return {
        "whatsapp":  doc.get("whatsapp", ""),
        "facebook":  doc.get("facebook", ""),
        "instagram": doc.get("instagram", ""),
        "youtube":   doc.get("youtube", ""),
    }

# =================== CATEGORIES ===================
# /categories handled above

def _default_user_terms():
    return """# Terms & Conditions

## 1. Acceptance of Terms
By using LocalSaver, you agree to these terms. If you do not agree, please do not use the app.

## 2. Eligibility
You must be 18 years or older to use this service. By registering, you confirm you meet this requirement.

## 3. User Account
- You are responsible for maintaining the confidentiality of your account.
- Provide accurate information during registration.
- One account per person. Multiple accounts will be terminated.

## 4. Points & Rewards
- Points are earned by visiting registered stores and scanning their QR code.
- Points cannot be transferred between accounts.
- 2 Points = Rs.1 | Minimum withdrawal: 200 points (Rs.100).

## 5. QR Code Usage
- Each store QR can be scanned once per visit (cooldown applies).
- Attempting to spoof or duplicate scans will result in account suspension.

## 6. Prohibited Activities
- Creating fake accounts or using bots.
- Attempting to manipulate the points system.

## 7. Termination
LocalSaver may suspend accounts that violate these terms without prior notice.

## 8. Contact
For queries: support@localsaver.in"""

def _default_merchant_terms():
    return """# Merchant Terms & Conditions

## 1. Agreement
By registering as a merchant on LocalSaver, you agree to these terms.

## 2. Listing Requirements
- Stores must operate from a fixed physical location.
- All store details must be accurate.
- You must have the legal right to operate the listed business.

## 3. Subscription & Fees
- Store listing requires an active subscription.
- Subscription fees include GST as per Indian law.
- See Refund Policy for cancellation terms.

## 4. Store Approval
- After payment, stores enter Waiting Approval status.
- Admin reviews and approves within 24-48 hours.

## 5. QR Code Obligations
- The QR code must be displayed prominently at your store.
- Misuse of QR codes will result in immediate termination.

## 6. Contact
Merchant support: merchants@localsaver.in"""

def _default_privacy():
    return """# Privacy Policy

## 1. Information We Collect
- Account Information: Name, phone number, city, area.
- Location Data: City-level only, for showing nearby deals.
- Usage Data: Store visits, QR scans, points transactions.

## 2. How We Use Your Information
- To provide and improve the LocalSaver service.
- To show relevant stores and deals in your city.
- To track your points and transaction history.

## 3. Information Sharing
We do not sell your personal information. We may share data:
- With merchants: only aggregate visit counts, not personal details.
- With law enforcement if required by law.
- With service providers under strict confidentiality.

## 4. Data Security
- All data is stored on secured servers with encryption.
- Tokens are hashed and never stored in plain text.
- We use HTTPS for all data transmission.

## 5. Your Rights
- Access: Request a copy of your personal data.
- Correction: Update inaccurate information.
- Deletion: Request account deletion via support.

## 6. Contact
Privacy queries: privacy@localsaver.in"""

def _default_refund():
    return """# Refund Policy

## Subscription Refunds

### Eligible for Refund
- Store NOT approved within 5 business days of payment.
- Duplicate payments made accidentally.
- LocalSaver terminates listing due to our error.

### NOT Eligible for Refund
- Subscriptions where the store has already been approved and activated.
- Requests made after 7 days of payment.
- Stores removed due to policy violations or fraud.
- Change of mind after store approval.

## Refund Process
1. Email refunds@localsaver.in with your Invoice Number and phone.
2. Our team will review within 3-5 business days.
3. Approved refunds credited to original payment method within 7-10 business days.

## Points & Rewards
- User reward points are non-refundable and non-transferable.

## Contact
refunds@localsaver.in"""

def _default_kyc():
    return """# KYC (Know Your Customer) Policy

## Why KYC?
KYC verification helps us prevent fraud, comply with Indian regulations, and protect merchants and users.

## Who Needs KYC?
- Merchants requesting refunds above Rs.10,000.
- Merchants flagged for suspicious activity.
- High-volume subscription accounts.

## Documents Required

### Individual Merchants
- Identity Proof: Aadhaar Card / PAN Card / Voter ID.
- Address Proof: Utility bill or Aadhaar.
- Business Proof: GST certificate or Shop Registration (if applicable).

### Business Entities
- Certificate of Incorporation or Partnership Deed.
- PAN of the business.
- Authorized signatory ID proof.

## KYC Process
1. Email documents to kyc@localsaver.in with subject: KYC - [Phone Number].
2. Verification within 3-5 business days.
3. You will be notified of approval or discrepancies.

## Non-Compliance
Failure to complete KYC may result in:
- Withholding of payouts.
- Temporary suspension of merchant account.

## Contact
kyc@localsaver.in"""


# =================== DISCOUNT VALIDATION (public) ===================

# =================== USER RATINGS ===================

def _get_user_optional(request: _Req):
    token = request.cookies.get("user_token") or request.headers.get("Authorization","").replace("Bearer ","").strip()
    if not token: return None
    # FIX: this only ever checked the legacy 'users' collection, so any user
    # logged in via the unified 'accounts' collection (the primary path today)
    # was never found here — submit_product_review then saved their review
    # as user_id=None (anonymous), and get_my_product_review always returned {}.
    # That's why a submitted rating always "disappeared" on refresh/return.
    return db.accounts.find_one({"token": token}) or db.users.find_one({"token": token})

@router.post("/stores/{store_id}/rate")
def rate_store(store_id: str, data: dict, request: _Req):
    """User submits a rating (1-5) for a store. Computes running average."""
    try:
        from bson import ObjectId as ObjId
        store = db.stores.find_one({"_id": ObjId(store_id)})
    except Exception:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid store id")
    if not store:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Store not found")

    user = _get_user_optional(request)
    user_id = str(user["_id"]) if user else None
    new_r = float(data.get("rating", 0))
    if not (1 <= new_r <= 5):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Rating must be 1-5")

    # Store individual rating in ratings collection
    if user_id:
        db.ratings.update_one(
            {"store_id": store_id, "user_id": user_id},
            {"$set": {"store_id": store_id, "user_id": user_id, "rating": new_r}},
            upsert=True
        )

    # Recompute average from ratings collection (skip if no documents)
    all_ratings = list(db.ratings.find({"store_id": store_id}, {"rating": 1}))
    if all_ratings:
        avg = sum(r["rating"] for r in all_ratings) / len(all_ratings)
        avg = round(avg, 1)
    else:
        avg = new_r

    # Only update store rating if no admin_rating override
    if not store.get("admin_rating"):
        try:
            from bson import ObjectId as ObjId2
            db.stores.update_one({"_id": ObjId2(store_id)}, {"$set": {"rating": avg, "user_rating": avg}})
        except Exception:
            pass

    return {"message": "Rating submitted", "avg_rating": avg, "rating": avg}


@router.get("/stores/{store_id}/my-rating")
def my_rating(store_id: str, request: _Req):
    """Get the logged-in user's own rating for a store."""
    user = _get_user_optional(request)
    if not user:
        return {"rating": None}
    user_id = str(user["_id"])
    doc = db.ratings.find_one({"store_id": store_id, "user_id": user_id})
    return {"rating": doc["rating"] if doc else None}

@router.post("/discount/validate")
def validate_discount(body: dict):
    from fastapi import HTTPException
    from datetime import datetime
    code = (body.get("code","")).strip().upper()
    if not code:
        raise HTTPException(400, "Code required")
    doc = db.discounts.find_one({"code": code})
    if not doc:
        raise HTTPException(404, "Invalid discount code")
    if not doc.get("active", True):
        raise HTTPException(400, "This code is no longer active")
    if doc.get("expiry_date") and datetime.utcnow() > doc["expiry_date"]:
        raise HTTPException(400, "This code has expired")
    if doc.get("max_uses", 0) > 0 and doc.get("used_count", 0) >= doc["max_uses"]:
        raise HTTPException(400, "This code has reached its usage limit")
    return {
        "ok": True,
        "code": code,
        "value": doc.get("value", 0),
        "discount_id": str(doc["_id"])
    }

# =================== ABOUT US (public) ===================
@router.get("/about")
def get_about_public():
    doc = db.settings.find_one({"key": "about_us"}) or {}
    return {"content": doc.get("content", "")}

# =================== PROMO SLIDERS (public - for Flutter app) ===================
@router.get("/promo-sliders")
def get_promo_sliders_public():
    """Returns active, non-expired merchant banners (promo sliders) for the home screen."""
    from datetime import datetime as _dt
    _now = _dt.utcnow()
    docs = list(db.promo_sliders.find({"is_active": True}).sort("sort_order", 1))
    result = []
    for d in docs:
        # Expiry check — approved merchant banners store expiry in "expires_at";
        # admin-created promo sliders use "end_date". Check both.
        _end = d.get("end_date") or d.get("expires_at")
        if _end:
            try:
                if isinstance(_end, _dt):
                    _end_dt = _end
                else:
                    _end_str = str(_end).strip()
                    try:
                        _end_dt = _dt.fromisoformat(_end_str.replace("Z", ""))
                    except Exception:
                        # Merchant banners store end_date as "%d %b %Y" (e.g. "27 Jul 2026")
                        import re as _re
                        _mon_map = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                                    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
                        _m = _re.match(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", _end_str)
                        if _m:
                            _mo = _mon_map.get(_m.group(2).lower()[:3])
                            if _mo:
                                _end_dt = _dt(int(_m.group(3)), _mo, int(_m.group(1)))
                            else:
                                _end_dt = None
                        else:
                            _end_dt = None
                if _end_dt and _end_dt < _now:
                    continue
            except Exception:
                pass
        img = d.get("image_url", "") or d.get("image", "")
        result.append({
            "id":         str(d["_id"]),
            "title":      d.get("title", ""),
            "subtitle":   d.get("subtitle", "") or d.get("text", ""),
            "image":      img,
            "image_url":  img,
            "link_url":   d.get("link_url", ""),
            "bg_color":   d.get("bg_color", ""),
            "sort_order": d.get("sort_order", 0),
            "city":       d.get("city", ""),
        })
    return result

@router.get("/admin-banners")
def get_admin_banners_public():
    """Returns active admin banners from the banners collection (Admin Dashboard → Banners module).
    This is the Home Screen banner section — completely separate from promo_sliders."""
    docs = list(db.admin_banners.find({"is_active": True}).sort("sort_order", 1))
    result = []
    for d in docs:
        img = d.get("image_url", "") or d.get("image", "")
        result.append({
            "id":         str(d["_id"]),
            "title":      d.get("title", ""),
            "subtitle":   d.get("subtitle", ""),
            "image":      img,
            "image_url":  img,
            "link_url":   d.get("link_url", ""),
            "sort_order": d.get("sort_order", 0),
            "is_active":  d.get("is_active", True),
        })
    return result

# =================== GIFT VOUCHERS (public - for Flutter app home screen) ===================
@router.get("/gift-vouchers-public")
def get_gift_vouchers_public(city: str = ""):
    """Returns active products for Discover Products section (Admin Dashboard → Products module).
    Reads from gift_vouchers (primary) and products (secondary) — same as admin /admin/products.
    Filters by city when city param is provided (case-insensitive)."""
    from bson import ObjectId as OId
    import re as _re
    result = []
    seen_ids = set()
    city_filter = city.strip().lower() if city else ""

    def _city_matches(doc_city: str) -> bool:
        if not city_filter:
            return True
        dc = (doc_city or "").strip().lower()
        if not dc:
            return False  # FIX: city filter active — exclude products with no city assigned
        return bool(_re.search(_re.escape(city_filter), dc) or _re.search(_re.escape(dc), city_filter))

    def _is_active(doc):
        return doc.get("is_active", True) not in (False, "false", "0", 0)

    def _is_expired(doc) -> bool:
        from datetime import datetime as _dt2
        _now2 = _dt2.utcnow()
        for k in ("end_date", "validity_end", "expiry", "valid_till"):
            v = doc.get(k)
            if not v:
                continue
            try:
                if isinstance(v, _dt2):
                    if v < _now2:
                        return True
                else:
                    vs = str(v).strip()
                    if vs and vs not in ("", "null", "None"):
                        vdt = _dt2.fromisoformat(vs[:19].replace(" ", "T"))
                        if vdt < _now2:
                            return True
            except Exception:
                pass
        return False

    def _resolve_img(doc):
        for k in ["logo", "logo_url", "image_url", "image", "thumbnail"]:
            v = str(doc.get(k, "") or "")
            if v.startswith("http"): return v
        store_id = doc.get("store_id", "")
        if store_id:
            try:
                s = db.stores.find_one({"_id": OId(store_id)},
                                       {"store_image2": 1, "image": 1, "image2": 1})
                if s:
                    return s.get("store_image2") or s.get("image2") or s.get("image") or ""
            except: pass
        return ""

    def _get_store_name(sid: str) -> str:
        if not sid: return ""
        try:
            s = db.stores.find_one({"_id": OId(sid)}, {"store_name": 1})
            return s.get("store_name", "") if s else ""
        except Exception:
            return ""

    def _get_store_name_by_merchant(mid: str) -> str:
        """Look up a merchant's primary active store name — matches admin dashboard behaviour."""
        if not mid: return ""
        try:
            clauses = [{"merchant_id": mid}]
            try: clauses.append({"merchant_id": OId(mid)})
            except Exception: pass
            s = db.stores.find_one({"$or": clauses, "status": "active"}, {"store_name": 1})
            if s: return s.get("store_name", "")
            s = db.stores.find_one({"$or": clauses}, {"store_name": 1})
            if s: return s.get("store_name", "")
        except Exception:
            pass
        return ""

    def _get_store_city(sid: str) -> str:
        if not sid: return ""
        try:
            s = db.stores.find_one({"_id": OId(sid)}, {"city": 1})
            return s.get("city", "") if s else ""
        except Exception:
            return ""

    # ── 1. gift_vouchers collection (Premium only — Standard are subscription-linked, shown in Store Detail) ──
    for d in db.gift_vouchers.find({"product_type": {"$ne": "standard"}}).sort("_id", -1):
        if not _is_active(d): continue
        if _is_expired(d): continue
        vid = str(d["_id"])
        if vid in seen_ids: continue
        # City filter: check product's own city OR its store's city
        doc_city = d.get("city", "") or _get_store_city(str(d.get("store_id", "")))
        if not _city_matches(doc_city): continue
        seen_ids.add(vid)
        sid = d.get("store_id", "")
        mid = str(d.get("merchant_id", "") or "")
        # Store name: prefer merchant_id live lookup (matches admin dashboard) → store_id lookup → cached field
        resolved_store_name = (
            _get_store_name_by_merchant(mid)
            or _get_store_name(sid)
            or d.get("store_name", "")
            or d.get("merchant_name", "")
        )
        result.append({
            "id":           vid,
            "title":        d.get("title", ""),
            "text":         d.get("text", "") or d.get("offer_text", ""),
            "validity":     (str(d.get("end_date","") or "")[:10]) or (d.get("validity","") or ""),
            "logo":         _resolve_img(d),
            "logo_url":     _resolve_img(d),
            "store_id":     sid,
            "store_name":   resolved_store_name,
            "city":         doc_city,
            "from_date":    d.get("from_date", ""),
            "end_date":     d.get("end_date", ""),
            "is_active":    True,
            "source":       "gift_vouchers",
            "rating":       float(d.get("rating") or d.get("avg_rating") or 0),
            "rating_count": int(d.get("rating_count") or d.get("review_count") or 0),
        })

    # ── 2. products collection ───────────────────────────────────────────────
    for p in db.products.find({}).sort("_id", -1):
        if not _is_active(p): continue
        if _is_expired(p): continue
        pid = str(p["_id"])
        if pid in seen_ids: continue
        # City filter: check product's own city OR its store's city
        pdoc_city = p.get("city", "") or _get_store_city(str(p.get("store_id", "")))
        if not _city_matches(pdoc_city): continue
        seen_ids.add(pid)
        price    = p.get("price", "")
        discount = p.get("discount", "")
        text_parts = []
        if discount: text_parts.append(f"{discount}% OFF")
        if price:    text_parts.append(f"₹{price}")
        offer_text = p.get("offer_text") or p.get("text") or (", ".join(text_parts) if text_parts else "")
        psid = str(p.get("store_id", ""))
        pmid = str(p.get("merchant_id", "") or "")
        # Store name: prefer merchant_id live lookup (matches admin dashboard) → store_id lookup → cached field
        resolved_p_store = (
            _get_store_name_by_merchant(pmid)
            or _get_store_name(psid)
            or p.get("store_name", "")
            or p.get("merchant_name", "")
        )
        result.append({
            "id":           pid,
            "title":        p.get("name") or p.get("title") or "",
            "text":         offer_text,
            "validity":     p.get("validity") or p.get("valid_till") or "",
            "logo":         _resolve_img(p),
            "logo_url":     _resolve_img(p),
            "store_id":     psid,
            "store_name":   resolved_p_store,
            "city":         pdoc_city,
            "from_date":    p.get("from_date") or p.get("start_date") or "",
            "end_date":     p.get("end_date") or p.get("expiry") or "",
            "is_active":    True,
            "source":       "products",
            "rating":       float(p.get("rating") or p.get("avg_rating") or 0),
            "rating_count": int(p.get("rating_count") or p.get("review_count") or 0),
        })

    return result


# ─── Default Images (fallback for stores/products/offers/cities) ──────────────
@router.get("/default-images")
def get_default_images():
    """Return default/fallback images configured by admin.
    Supports both legacy string and new array format per field."""
    doc = db.settings.find_one({"_type": "default_images"})
    if not doc:
        return {"store": "", "product": "", "offer": "", "city": "",
                "merchant_banner": "", "no_service_url": "",
                "no_service_title": "", "no_service_message": ""}

    def _first(val):
        """Return first HTTP URL from string or list."""
        if isinstance(val, list):
            for v in val:
                if isinstance(v, str) and v.startswith("http"): return v
            return ""
        return val if isinstance(val, str) else ""

    def _all_urls(val):
        """Return ALL URLs as a list (http AND data: URIs — used for banner/city rotation)."""
        def _ok(v):
            if not isinstance(v, str): return False
            return v.startswith("http") or v.startswith("data:image") or v.startswith("data:video")
        if isinstance(val, list):
            return [v for v in val if _ok(v)]
        if isinstance(val, str) and _ok(val):
            return [val]
        return []

    # no_service_url may be an HTTP URL or base64 — return raw so app can render both
    _ns_raw = doc.get("no_service_url", doc.get("no_service", ""))
    _ns_val = _ns_raw if isinstance(_ns_raw, str) else ""

    return {
        "store":            _first(doc.get("store", "")),
        "product":          _first(doc.get("product", "")),
        "offer":            _first(doc.get("offer", "")),
        "city":             _all_urls(doc.get("city", "")),
        "merchant_banner":  _all_urls(doc.get("merchant_banner", "")),  # array — same pattern as city
        "no_service_url":   _ns_val,
        "no_service_title": str(doc.get("no_service_title", "") or ""),
        "no_service_message": str(doc.get("no_service_message", "") or ""),
    }


# ═══════════════════════════════════════════════════════════
# PHASE 2 — PUBLIC PRODUCT ENDPOINTS
# ═══════════════════════════════════════════════════════════

@router.post("/products/{pid}/track")
def track_product_event(pid: str, data: dict):
    """Track a product event (view, share, open) from the Flutter app."""
    from bson import ObjectId as _OId4
    from datetime import datetime as _dtt
    event = data.get("event", "view")
    if event not in ("view", "share", "open"):
        event = "view"
    merchant_id = data.get("merchant_id", "")
    db.product_events.insert_one({
        "product_id": pid, "merchant_id": merchant_id,
        "event": event, "created_at": _dtt.utcnow(),
    })
    for col in [db.merchant_vouchers, db.gift_vouchers]:
        try:
            col.update_one({"_id": _OId4(pid)}, {"$inc": {f"{event}_count": 1}})
        except Exception:
            pass
    return {"ok": True}


@router.get("/products/{pid}/similar")
def get_similar_products(pid: str, limit: int = 6):
    """Return similar products (same category/city) for the detail page."""
    from bson import ObjectId as _OId5
    from datetime import datetime as _dt2
    _now2 = _dt2.utcnow()
    src = None
    try:
        _oid = _OId5(pid)
        src = db.merchant_vouchers.find_one({"_id": _oid}) or db.gift_vouchers.find_one({"_id": _oid})
    except Exception:
        pass
    if not src:
        return []
    category = src.get("category", "")
    city     = src.get("city", "")
    results  = []
    seen     = {pid}
    cat_q    = {"$regex": category, "$options": "i"} if category else {"$exists": True}
    city_q   = {"$regex": city,     "$options": "i"} if city     else {"$exists": True}
    for p in db.merchant_vouchers.find(
        {"status": "approved", "approval_status": "approved", "category": cat_q, "city": city_q}
    ).limit(limit + 5):
        sid = str(p["_id"])
        if sid in seen: continue
        ed = p.get("end_date")
        if ed and isinstance(ed, _dt2) and ed < _now2: continue
        seen.add(sid)
        results.append({
            "_id": sid, "title": p.get("title",""),
            "offer_text": p.get("offer_text",""),
            "logo_url": (p.get("logo_url") or p.get("image_url") or ""),
            "price": str(p.get("price","") or ""),
        })
        if len(results) >= limit: break
    return results


@router.get("/products/by-store/{store_id}")
def get_products_by_store(store_id: str, limit: int = 20):
    """Return all visible products for a given store (MoreFromStore widget)."""
    from datetime import datetime as _dt3
    from bson import ObjectId as _OId6
    _now3 = _dt3.utcnow()
    results = []
    seen    = set()
    _oid_clauses = [{"store_id": store_id}]
    try: _oid_clauses.append({"store_id": _OId6(store_id)})
    except Exception: pass
    q_oid = {"$or": _oid_clauses}
    for p in db.merchant_vouchers.find(
        {**q_oid, "status": "approved", "approval_status": "approved"}
    ).sort("created_at", -1).limit(limit):
        ed = p.get("end_date")
        if ed and isinstance(ed, _dt3) and ed < _now3: continue
        pid = str(p["_id"])
        if pid in seen: continue
        seen.add(pid)
        results.append({"_id": pid, "title": p.get("title",""),
                        "offer_text": p.get("offer_text",""),
                        "logo_url": (p.get("logo_url") or p.get("image_url") or ""),
                        "price": str(p.get("price","") or ""),
                        "original_price": str(p.get("original_price","") or ""),
                        "rating": float(p.get("rating") or p.get("avg_rating") or 0),
                        "product_type": "premium"})
    for p in db.gift_vouchers.find(q_oid).sort("_id", -1).limit(limit):
        if p.get("is_active", True) in (False, "false", "0", 0): continue
        pid = str(p["_id"])
        if pid in seen: continue
        seen.add(pid)
        results.append({"_id": pid, "title": p.get("title",""),
                        "offer_text": p.get("offer_text","") or p.get("text",""),
                        "logo_url": (p.get("logo_url") or p.get("image_url") or ""),
                        "price": str(p.get("price","") or ""),
                        "original_price": str(p.get("original_price","") or ""),
                        "rating": float(p.get("rating") or p.get("avg_rating") or 0),
                        "product_type": "standard"})
    return results[:limit]
