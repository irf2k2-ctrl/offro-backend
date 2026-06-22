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
        city = _normalize_city(city)  # FIX: map GPS alternate spellings (Bellary→Ballari etc.)
        query["city"] = {"$regex": city, "$options": "i"}
    if category and category.strip() and category.strip() != "All":
        # Case-insensitive match so "Restaurant" matches "restaurant", "RESTAURANT" etc.
        query["category"] = {"$regex": f"^{category.strip()}$", "$options": "i"}


    stores = list(db.stores.find(query, {
        "store_name":1,"category":1,"city":1,"area":1,"address":1,"phone":1,
        "image":1,"image_url":1,"image_thumb":1,"_thumb":1,"store_image":1,"store_image2":1,"images":1,"status":1,"points_per_scan":1,
        "lat":1,"lng":1,"rating":1,"admin_rating":1,"is_new_in_town":1,"is_trending":1,"is_popular":1,"badge":1,"merchant_id":1,
        "tags":1,"favorite_count":1,"favorites":1,"view_count":1,"views":1,
        "created_at":1,"late_night":1,"open_time":1,"close_time":1,"logo_url":1,"logo_thumb":1,"logo":1,"logo_image":1
    }))
    if not stores:
        return []

    # Pre-fetch ALL active deals in ONE query — no N+1
    store_ids = [str(s["_id"]) for s in stores]

    # ── SUBSCRIPTION FILTER: remove stores with expired/no subscription ──
    from datetime import datetime as _dt
    _now = _dt.utcnow()
    _active_subs = set()
    for _sub in db.subscriptions.find(
            {"store_id": {"$in": store_ids}},
            {"store_id": 1, "end_date": 1, "status": 1}):
        _ed = _sub.get("end_date")
        if _ed is None:
            continue
        try:
            _ed_dt = _ed if isinstance(_ed, _dt) else _dt.fromisoformat(str(_ed).replace("Z",""))
            if _ed_dt >= _now:
                _active_subs.add(str(_sub["store_id"]))
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
            "open_time":      s.get("open_time",  "") or "",
            "close_time":     s.get("close_time", "") or "",
            "logo_url":       s.get("logo_url", "") or s.get("logo_thumb", "") or s.get("logo", "") or s.get("logo_image", "") or "", # ITEM3
            "lat":            s.get("lat") or None,
            "lng":            s.get("lng") or None,
        })
    return result

# =================== SINGLE STORE ===================


# ── City name normalization (handles GPS alternate spellings) ──────────────────
_CITY_ALIASES: dict = {
    # Ballari variants (GPS returns multiple spellings)
    "bellary":       "Ballari",
    "ballary":       "Ballari",
    "vijayanagara":  "Ballari",   # new district name (2021) that geocoders return
    "vijayanagar":   "Ballari",
    # Other Karnataka dual-name cities
    "bijapur":       "Vijayapura",
    "gulbarga":      "Kalaburagi",
    "shimoga":       "Shivamogga",
    "hospet":        "Hosapete",
    "tumkur":        "Tumakuru",
    "mysore":        "Mysuru",
    "belgaum":       "Belagavi",
    "mangalore":     "Mangaluru",
    "hubli":         "Hubballi",
    "dharwad":       "Hubballi",
    "davangere":     "Davanagere",
    "udupi":         "Udupi",
}

def _normalize_city(city: str) -> str:
    """Map GPS/old city spellings to the canonical name used in the database."""
    if not city:
        return city
    lower = city.strip().lower()
    # Exact alias match first
    if lower in _CITY_ALIASES:
        return _CITY_ALIASES[lower]
    # Substring match — e.g. "Ballari District" or "Bellary Urban" → "Ballari"
    for alias, canonical in _CITY_ALIASES.items():
        if alias in lower:
            return canonical
    return city.strip()


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

    # Approved products (from merchant_vouchers collection) for this store
    products_list = []
    if "merchant_vouchers" in cols:
        merchant_id = store.get("merchant_id", "")
        merchant_phone = str(store.get("phone", ""))
        q = {"status": "approved"}
        if merchant_id:
            q = {"$or": [{"merchant_id": merchant_id}, {"merchant_phone": merchant_phone}],
                 "status": "approved"}
        prods = list(db.merchant_vouchers.find(q).sort("created_at", -1).limit(20))
        for p in prods:
            products_list.append({
                "_id":            str(p["_id"]),
                "title":          p.get("title", ""),
                "offer_text":     p.get("offer_text", ""),
                "logo_url":       p.get("logo_url", "") or p.get("logo_thumb", ""),
                "price":          p.get("price", "") or "",
                "original_price": p.get("original_price", "") or "",
                "discount":       p.get("discount_label", "") or "",
                "validity":       p.get("validity", ""),
                "end_date":       p.get("end_date", ""),
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



@router.get("/stores/{store_id}/user-review")
def get_user_review(store_id: str, request: _Req):
    """Get the current user's own review for a store."""
    user = _get_user_optional(request)
    if not user:
        return {"review": None}
    user_id = str(user["_id"])
    r = db.reviews.find_one({"store_id": store_id, "user_id": user_id})
    if not r:
        return {"review": None}
    r["_id"] = str(r["_id"])
    r.setdefault("created_at", "")
    r.setdefault("updated_at", "")
    return {"review": r}

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

    # Step 1: All active stores (no subscription gating — show all stores with deals)
    store_q = {"status": "active"}
    if city:
        _norm = normalize_city(city)
        store_q["city"] = {"$regex": _norm, "$options": "i"}

    stores_raw = list(db.stores.find(store_q, {
        "store_name": 1, "category": 1, "city": 1, "area": 1, "address": 1, "phone": 1,
        "image_url": 1, "image_thumb": 1, "_thumb": 1, "image": 1, "images": 1,
    }))
    stores_map = {str(s["_id"]): s for s in stores_raw}

    # Step 2: Active deals only (no products)
    result = []
    deal_q = {"status": "active"}
    if stores_map:
        deal_q["store_id"] = {"$in": list(stores_map.keys())}

    for d in db.deals.find(deal_q).sort("created_at", -1).limit(200):
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

        result.append({
            "type":        "deal",
            "_id":         str(d["_id"]),
            "title":       d.get("title", d.get("deal_name", "")),
            "discount":    d.get("discount", d.get("offer_percent", "")),
            "description": d.get("description", ""),
            "end_date":    str(end_date) if end_date else "",
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
    """Return rich category objects with image_url, icon, subtitle for the Flutter app.
    Only categories that have at least 1 active subscribed store are returned.
    Handles two MongoDB schemas:
      1. Rich: multiple docs each with {name, image_url, icon, subtitle, sort_order}
      2. Legacy: single doc with {categories: ["Grocery","Restaurant",...]}
    """
    from datetime import datetime as _dt
    _now = _dt.utcnow()

    # Build set of active subscription store IDs
    _active_sub_ids = set()
    for _sub in db.subscriptions.find({}, {"store_id": 1, "end_date": 1}):
        _ed = _sub.get("end_date")
        if _ed is None:
            continue
        try:
            _ed_dt = _ed if isinstance(_ed, _dt) else _dt.fromisoformat(str(_ed).replace("Z",""))
            if _ed_dt >= _now:
                _active_sub_ids.add(str(_sub["store_id"]))
        except Exception:
            pass

    # Build set of category names that have at least 1 active store with active subscription
    _categories_with_stores = set()
    for _store in db.stores.find({"status": "active"}, {"category": 1}):
        if str(_store["_id"]) in _active_sub_ids:
            _cat = (_store.get("category") or "").strip()
            if _cat:
                _categories_with_stores.add(_cat.lower())

    # Schema 1: try rich per-document format first
    rich_cats = list(db.categories.find({"name": {"$exists": True}, "status": {"$ne": "deleted"}}, {"_id":0}).sort("sort_order",1))
    if rich_cats:
        result = []
        for cat in rich_cats:
            cat_name = cat.get("name", "")
            # Skip categories with no active stores
            if _categories_with_stores and cat_name.lower() not in _categories_with_stores:
                continue
            raw_img = cat.get("image_url", "") or ""
            # Pass http URLs AND base64 data URIs — Flutter decodes both
            if raw_img.startswith("http://") or raw_img.startswith("https://") or raw_img.startswith("data:image"):
                safe_img = raw_img
            else:
                safe_img = ""
                if raw_img:
                    print(f"[OFFRO /categories] WARN: unusable image_url for '{cat_name}' — stripped")
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
    return db.users.find_one({"token": token})

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
def get_promo_sliders_public(city: str = None):
    """Returns active promo slider banners for the app home screen, optionally filtered by city."""
    base_query = {"is_active": {"$ne": False}}
    docs = []
    if city and city.strip():
        city = _normalize_city(city)  # FIX: map GPS alternate spellings (Bellary→Ballari etc.)
        city_re = {"$regex": city.strip(), "$options": "i"}
        city_query = {"$and": [base_query, {"$or": [{"city": city_re}, {"store_city": city_re}]}]}
        docs = list(db.promo_sliders.find(city_query).sort("sort_order", 1))
        # Fallback: no city-specific sliders — return ALL active sliders
        # (better than a placeholder; user sees real content even if GPS city name mismatches)
        if not docs:
            docs = list(db.promo_sliders.find(base_query).sort("sort_order", 1))
    else:
        docs = list(db.promo_sliders.find(base_query).sort("sort_order", 1))
    result = []
    for d in docs:
        img = d.get("image_url", "") or d.get("image", "")
        result.append({
            "id": str(d["_id"]),
            "title": d.get("title", ""),
            "subtitle": d.get("subtitle", "") or d.get("text", ""),
            "image": img,
            "image_url": img,
            "link_url": d.get("link_url", ""),
            "bg_color": d.get("bg_color", ""),
            "sort_order": d.get("sort_order", 0),
            "city": d.get("city", ""),
        })
    return result

# =================== GIFT VOUCHERS (public - for Flutter app home screen) ===================
@router.get("/products-public")
def get_public_products():
    """Returns active products shown on the app home screen."""
    # Fetch all, then filter in Python to handle bool/string/missing is_active variants
    docs = list(db.products.find({}).sort("_id", -1))
    docs = [d for d in docs if d.get("is_active", True) not in (False, "false", "0", 0)]
    result = []
    for d in docs:
        vid = str(d.pop("_id"))
        # Normalise store field
        store = d.get("store", {})
        if isinstance(store, dict):
            sid = store.get("_id") or store.get("id")
            if sid:
                store["id"] = str(sid)
                store.pop("_id", None)
        # If store_id exists, try to pull store image for display
        store_id = d.get("store_id", "")
        if store_id and not d.get("logo"):
            try:
                from bson import ObjectId as OId
                s = db.stores.find_one({"_id": OId(store_id)}, {"store_image2":1,"image":1,"store_name":1})
                if s:
                    d["logo"] = s.get("store_image2") or s.get("image") or ""
                    if not d.get("title") and s.get("store_name"):
                        d["title"] = s["store_name"]
            except: pass
        result.append({"id": vid, **d})
    return result


# =================== PRODUCTS (public — Discover Products on home screen) ===================
@router.get("/products")
def get_products_public(category: str = None, city: str = None, limit: int = 60, skip: int = 0):
    """Returns active products for the Flutter app Discover Products section."""
    query = {"status": {"$nin": ["deleted", "inactive"]}}
    if category and category != "All":
        query["category"] = category
    # SIMPLIFIED: show all active products regardless of city
    # City filtering removed — products are global discovery content
    docs = list(db.products.find(query).sort("_id", -1).skip(skip).limit(limit))
    result = []
    for d in docs:
        pid = str(d.pop("_id", ""))
        # Attach store name if missing
        store_id = d.get("store_id", "")
        merchant_id = d.get("merchant_id", "")
        store_name = d.get("store_name", "")
        # ITEM7: resolve store_name via store_id first, then merchant_id fallback
        if not store_name:
            try:
                s = None
                if store_id:
                    s = db.stores.find_one({"_id": ObjectId(store_id)}, {"store_name": 1, "_id": 1})
                if not s and merchant_id:
                    s = db.stores.find_one({"merchant_id": merchant_id}, {"store_name": 1, "_id": 1})
                if s:
                    d["store_name"] = s.get("store_name", "")
                    if not store_id:
                        d["store_id"] = str(s["_id"])
            except Exception:
                pass
        # FIX3: normalise pricing fields so Flutter always finds them
        if d.get("offer_price") and not d.get("sale_price"):
            d["sale_price"] = d["offer_price"]
        if not d.get("price") and d.get("sale_price"):
            d["price"] = d["sale_price"]
        # Ensure original_price is always set from mrp if missing
        if not d.get("original_price") and d.get("mrp"):
            d["original_price"] = d["mrp"]
        # Ensure sale_price < original_price (otherwise hide strikethrough)
        if d.get("original_price") and d.get("sale_price"):
            try:
                if float(d["original_price"]) <= float(d["sale_price"]):
                    d["original_price"] = None
            except: pass
        result.append({"id": pid, **d})
    return result


# ─── Default Images (fallback for stores/products/offers/cities) ──────────────
@router.get("/default-images")
def get_default_images():
    """Return default/fallback images configured by admin.
    Returns lists of HTTP/HTTPS URLs for each type — strips base64.
    Flutter picks a random one client-side and rotates every 2 minutes.
    """
    doc = db.settings.find_one({"_type": "default_images"}) or {}

    def _safe_list(val) -> list:
        """Normalise legacy single-string or list → list of valid http(s) URLs."""
        if isinstance(val, list):
            return [v for v in val if isinstance(v, str) and v.startswith("http")]
        if isinstance(val, str) and val.startswith("http"):
            return [val]
        return []

    return {
        "store":           _safe_list(doc.get("store",           doc.get("store_images",   []))),
        "product":         _safe_list(doc.get("product",         doc.get("product_images", []))),
        "offer":           _safe_list(doc.get("offer",           doc.get("offer_images",   []))),
        "city":            _safe_list(doc.get("city",            doc.get("city_images",    []))),
        "merchant_banner": _safe_list(doc.get("merchant_banner", [])),
    }

# ─── Admin Banners (public - for Flutter app home screen) ─────────────────────
@router.get("/admin-banners")
def get_admin_banners_public(city: str = None):
    """Return active admin banners for the app home screen — shown globally regardless of city."""
    # FIX: return ALL banners where is_active is not explicitly False
    # Also handles docs where is_active field doesn't exist
    docs = list(db.banners.find(
        {"$or": [{"is_active": True}, {"is_active": {"$exists": False}}]}
    ).sort("sort_order", 1))
    print(f"[OFFRO] /admin-banners — total docs in collection: {db.banners.count_documents({})}, active: {len(docs)}")
    result = []
    for d in docs:
        img = d.get("image_url", "") or d.get("image", "") or ""
        # Normalise base64 — ensure header is present
        if img and not img.startswith("http") and not img.startswith("data:"):
            img = "data:image/jpeg;base64," + img
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


# ─── Product Reviews ──────────────────────────────────────────────────────────
@router.get("/products/{product_id}/reviews")
def get_product_reviews(product_id: str, limit: int = 20, skip: int = 0):
    """Get reviews for a product."""
    reviews = list(db.product_reviews.find(
        {"product_id": product_id}
    ).sort("created_at", -1).limit(limit).skip(skip))
    result = []
    for r in reviews:
        result.append({
            "id":         str(r["_id"]),
            "product_id": r.get("product_id", ""),
            "user_id":    r.get("user_id", ""),
            "user_name":  r.get("user_name", "Anonymous"),
            "rating":     r.get("rating", 0),
            "text":       r.get("text", ""),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
        })
    return result


@router.post("/products/{product_id}/review")
def submit_product_review(product_id: str, data: dict, request: _Req):
    """Submit a product review."""
    from datetime import datetime
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    user_id   = ""
    user_name = data.get("user_name", "Anonymous")
    if token:
        try:
            user = db.users.find_one({"token": token})
            if user:
                user_id   = str(user["_id"])
                user_name = user.get("name", user.get("full_name", user_name))
        except Exception:
            pass
    rating = float(data.get("rating", 0))
    text   = str(data.get("text", "")).strip()
    if not rating or not text:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="rating and text are required")
    now = datetime.utcnow()
    review_doc = {
        "product_id": product_id,
        "user_id":    user_id,
        "user_name":  user_name,
        "rating":     rating,
        "text":       text,
        "created_at": now,
    }
    result = db.product_reviews.insert_one(review_doc)
    # Recalculate product avg rating
    all_reviews = list(db.product_reviews.find({"product_id": product_id}, {"rating": 1}))
    if all_reviews:
        avg = sum(r["rating"] for r in all_reviews) / len(all_reviews)
        db.products.update_one(
            {"_id": ObjectId(product_id)},
            {"$set": {"rating": round(avg, 2), "rating_count": len(all_reviews)}}
        )
    return {"success": True, "id": str(result.inserted_id)}


# ─── Product Favorites ────────────────────────────────────────────────────────

@router.get("/user/product-favorites")
def get_user_favorites(request: _Req):
    """Get all product IDs favorited by the current user."""
    from fastapi import HTTPException as _HTTPEx
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        return {"product_ids": []}
    acct = db.accounts.find_one({"token": token})
    if not acct:
        return {"product_ids": []}
    user_id = str(acct["_id"])
    favs = list(db.product_favorites.find({"user_id": user_id}, {"product_id": 1}))
    return {"product_ids": [f["product_id"] for f in favs]}

@router.post("/user/product-favorites/{product_id}")
def toggle_product_favorite(product_id: str, request: _Req):
    """Toggle product favorite for authenticated user."""
    from fastapi import HTTPException
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user = db.users.find_one({"token": token})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    user_id  = str(user["_id"])
    existing = db.product_favorites.find_one({"user_id": user_id, "product_id": product_id})
    if existing:
        db.product_favorites.delete_one({"_id": existing["_id"]})
        return {"is_favorite": False}
    else:
        db.product_favorites.insert_one({"user_id": user_id, "product_id": product_id})
        return {"is_favorite": True}


@router.get("/user/product-favorites/{product_id}/check")
def check_product_favorite(product_id: str, request: _Req):
    """Check if a product is favorited by the current user."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        return {"is_favorite": False}
    user = db.users.find_one({"token": token})
    if not user:
        return {"is_favorite": False}
    user_id = str(user["_id"])
    exists  = db.product_favorites.find_one({"user_id": user_id, "product_id": product_id}) is not None
    return {"is_favorite": exists}
