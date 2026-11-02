from fastapi import APIRouter, Request as _Req
from database import db
from bson import ObjectId

router = APIRouter(tags=["Public"])

def _safe_end_date(p):
    """Return end_date string without corrupting 'DD Mon YYYY' format.
    The old [:10] truncation turned '12 Aug 2026' into '12 Aug 202' (year 202 AD).
    Handles: ISO datetime, ISO date, 'DD Mon YYYY', datetime objects, and empty."""
    from datetime import datetime as _dt
    raw = p.get("end_date", "") if isinstance(p, dict) else ""
    if not raw:
        return ""
    if isinstance(raw, _dt):
        return raw.strftime("%Y-%m-%d")
    s = str(raw).strip()
    if not s:
        return ""
    # ISO format: YYYY-MM-DD... -> first 10 chars is the date part
    if len(s) >= 10 and s[4] == '-' and s[7] == '-':
        return s[:10]
    # 'DD Mon YYYY' or other format — return as-is
    return s

# ── Shared date parsing + deal expiry ────────────────────────────────────
def _parse_flex_date_value(raw):
    """Parse a date that may be a datetime, an ISO string, or one of the
    human-entered formats admin/merchant forms write to end_date (e.g.
    "11 Aug 2026"). Returns a naive datetime, or None when empty/unparseable.
    Single shared implementation: the product-expiry code in
    get_public_products() delegates here, and deal expiry uses it too."""
    from datetime import datetime as _dt2
    if raw is None or raw == "":
        return None
    if hasattr(raw, "strftime"):
        return raw
    s = str(raw).strip()
    if not s or s in ("null", "None"):
        return None
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return _dt2.strptime(s[:19].replace("Z", ""), fmt)
        except ValueError:
            continue
    try:
        return _dt2.fromisoformat(s[:19].replace("Z", "").replace(" ", "T"))
    except ValueError:
        return None


def _ist_now():
    """Current time on the India business clock (IST, UTC+05:30) as a naive
    datetime. Offer/product validity is a business-day rule in India: an
    end date of 2026-10-14 runs through 2026-10-14 23:59:59 IST."""
    from datetime import datetime as _dt2, timedelta as _td2
    return _dt2.utcnow() + _td2(hours=5, minutes=30)


def _end_date_passed(raw, now=None):
    """True/False when [raw] is a parseable end date, None when it is empty or
    cannot be parsed. Valid THROUGH the end date: expired only once the end of
    that calendar day (23:59:59, IST) has passed. [now] is a naive IST
    datetime (defaults to _ist_now())."""
    dt = _parse_flex_date_value(raw)
    if dt is None:
        return None
    return dt.replace(hour=23, minute=59, second=59, microsecond=0) < (now or _ist_now())


def _is_deal_expired(deal, now=None):
    """True only when the deal has a parseable end date that is in the past.

    A deal is valid THROUGH its end date on the IST business day:
    end_date 2026-10-14 is live until 2026-10-14 23:59:59 IST and expired from
    2026-10-15 00:00 IST. No end date, or an end date that cannot be parsed,
    means NOT expired — a bad/missing date never hides a deal.
    [now] is a naive IST datetime (defaults to _ist_now())."""
    for k in ("end_date", "valid_until", "expiry"):
        raw = deal.get(k)
        if raw is None or raw == "":
            continue
        passed = _end_date_passed(raw, now)
        if passed is None:
            continue  # unparseable → try the next field, else treat as valid
        return passed
    return False


# ── Timestamp helpers (Round 4 — Bug 2 fix) ──────────────────────────────
# Root cause of "review shows 5h ago instead of just now": created_at was
# written as datetime.utcnow().isoformat() with NO timezone suffix. That IS
# genuinely UTC data, but the naked string is ambiguous to a client parser —
# Dart's DateTime.parse() treats a timezone-less ISO string as LOCAL time
# instead of UTC, silently shifting the parsed instant by the device's own
# UTC offset. For IST (UTC+5:30) a review created moments ago gets parsed
# as if it happened 5.5 hours ago — exactly the reported symptom. The fix
# is to always emit an explicit-UTC ('Z'-suffixed) timestamp from the API,
# for both new writes (_iso_utc_now) and old, already-stored naive values
# (_normalize_iso_utc, applied only at READ/serialization time — the
# database itself is never rewritten).
import re as _ts_re
_TZ_SUFFIX_RE = _ts_re.compile(r"(Z|[+-]\d{2}:?\d{2})$")

def _iso_utc_now():
    """Use for any NEW created_at/updated_at timestamp that will be read
    back and displayed to a user (review timestamps, etc.) — an explicit,
    unambiguous UTC ISO-8601 string."""
    from datetime import datetime as _dt
    return _dt.utcnow().isoformat() + "Z"

def _normalize_iso_utc(ts):
    """Make an ISO timestamp read back from the database unambiguous before
    it's returned in an API response — appends 'Z' only if the stored value
    has no timezone marker at all (historical naive-UTC data). A value that
    already carries an explicit offset (new data, or anything already
    correct) is returned unchanged."""
    if not ts:
        return ts
    s = str(ts)
    if _TZ_SUFFIX_RE.search(s):
        return s
    return s + "Z"

# =================== PUBLIC STORES LIST ===================
@router.get("/stores")
def get_stores(city: str = None, category: str = None):
    """Public endpoint - Flutter app fetches this"""
    query = {"status": "active"}
    if city:
        # FIX: escape the city string before using it in $regex — city comes
        # from live GPS reverse-geocoding on the client and is not curated
        # like a picked value, so it can contain regex-special characters
        # (parentheses, periods, etc.) that would otherwise break the query
        # or raise an unhandled error. Mirrors the same protection already
        # applied to `category` below.
        import re as _re
        query["city"] = {"$regex": _re.escape(city.strip()), "$options": "i"}
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
        "created_at":1,"late_night":1,
        # Needed by the Home Main Store Card's Open/Closed + hours row.
        "open_time":1,"close_time":1
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
            # Store hours — additive; empty string when not configured.
            "open_time":      s.get("open_time", "") or "",
            "close_time":     s.get("close_time", "") or "",
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
    # Merchant account was deleted — hide as if it never existed (deep-link/bookmark protection)
    if store.get("status") == "deleted" or store.get("is_deleted"):
        raise _HTTPEx(status_code=404, detail="Store not found")

    cols = db.list_collection_names()

    # Deals
    deals = list(db.deals.find({"store_id": store_id, "status": "active"})) \
        if "deals" in cols else []
    # Customer Store Detail must not show expired offers: `status` stays
    # "active" after end_date passes, so check the end date (valid through the
    # end of that day; missing/unparseable date → still shown).
    deals = [d for d in deals if not _is_deal_expired(d)]
    deals_list = [{
        "title":       d.get("title"),
        "discount":    d.get("discount"),
        "category":    d.get("category"),
        "description": d.get("description"),
        "start_date":  d.get("start_date"),
        "end_date":    d.get("end_date"),
        # Item 3: surface the deal's uploaded image so the store card can
        # show it (falls back to "" for older deals created before this
        # field existed — the store card treats it as optional).
        "image_url":   d.get("image_url", ""),
    } for d in deals]

    # Products for this store (merchant_products: approved, active, within dates)
    from routers.public_products import store_products as _store_products
    products_list = _store_products(store_id)

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
        # Store Logo: merchant app saves image2_url (CDN), admin saves store_image2.
        # image2_url first so a logo the merchant just uploaded is not hidden by an older store_image2.
        "image2":       store.get("image2_url") or store.get("store_image2") or store.get("image2") or None,
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


# =================== INFLUENCERS (Phase 2: public, read-only) ===================
# PRIVACY: `phone` is never included in this response shape — the admin-only
# shape (with phone) lives exclusively in routers/admin.py and is never
# reused here. Only active influencers are ever returned; inactive/deleted
# influencers and any city with no active influencers correctly return [],
# never another city's data (no fallback of any kind).

def _public_influencer_row(d):
    from routers.admin import _derive_influencer_categories
    return {
        "_id":          str(d["_id"]),
        "name":         d.get("name", ""),
        "city":         d.get("city", ""),
        "category":     d.get("category", ""),
        "categories":   _derive_influencer_categories(d),
        "photo_url":    d.get("photo_url", ""),
        "social":       d.get("social", {}) or {},
        "bio":          d.get("bio", ""),
        "rating":       d.get("rating", 0),
        "review_count": d.get("review_count", 0),
    }

def _influencer_public_visibility_filter() -> dict:
    """Publish/payment gate for the public directory (Influencer
    Subscription Fee + Payment + Publish feature). Deliberately additive to
    the existing `status` field, never a replacement for it:
      - `status` stays exactly what it always was — admin
        moderation/visibility (untouched by this feature).
      - `publish_status` is the NEW, separate payment-driven gate. A
        document that never went through this feature at all has no
        publish_status field — per the approved backward-compatibility
        rule, that means "treat as already published", so it must match
        here too (missing OR "published").
      - `is_active` is the self-service enable/disable toggle, independent
        of both of the above; missing OR True means visible.
    All three must hold for a profile to appear/resolve publicly."""
    return {
        "$and": [
            {"$or": [{"publish_status": {"$exists": False}}, {"publish_status": "published"}]},
            {"$or": [{"is_active": {"$exists": False}}, {"is_active": True}]},
        ]
    }

@router.get("/influencers")
def get_influencers_public(city: str = None):
    """Public endpoint — Home Screen 'City Influencers' section.
    Same city-filter convention as /stores above (escaped regex, case-
    insensitive). Only status=active influencers are ever returned."""
    query = {"status": "active", **_influencer_public_visibility_filter()}
    if city and city.strip():
        import re as _re
        query["city"] = {"$regex": _re.escape(city.strip()), "$options": "i"}
    docs = list(db.influencers.find(query).sort("created_at", -1))
    return [_public_influencer_row(d) for d in docs]

@router.get("/influencers/{influencer_id}")
def get_influencer_public(influencer_id: str):
    """Public influencer profile. Returns 404 for a missing, inactive,
    unpublished (payment not yet completed), or self-disabled influencer —
    none of these are publicly viewable at all, not merely hidden from the
    list."""
    from fastapi import HTTPException
    try:
        oid = ObjectId(influencer_id)
    except Exception:
        raise HTTPException(404, "Influencer not found")
    query = {"_id": oid, "status": "active", **_influencer_public_visibility_filter()}
    d = db.influencers.find_one(query)
    if not d:
        raise HTTPException(404, "Influencer not found")
    # Real "Profile Views" counter for the owner's own home/profile screen
    # stats section — incremented on every genuine public single-profile
    # view (mirrors the existing store view_count concept). Best-effort:
    # a failed increment never blocks the actual profile response.
    try:
        db.influencers.update_one({"_id": oid}, {"$inc": {"view_count": 1}})
    except Exception:
        pass
    return _public_influencer_row(d)


# =================== INFLUENCER REVIEWS (Item 4: real persistence) ===================
# Follows the exact same pattern as submit_product_review/get_my_product_review
# above — one review per (influencer_id, user_id) via upsert, aggregate
# recomputed from db.influencer_reviews and written onto the influencer doc.
# Uses the SAME _get_user_optional() helper (already fixed to check both
# db.accounts and the legacy db.users collection) — reusing it means this
# never regresses into the exact "review disappears on reopen" bug that
# helper's own fix comment describes for products, since an accounts-based
# login (the primary login path today) is already correctly recognized.

@router.get("/influencers/{influencer_id}/reviews")
def get_influencer_reviews(influencer_id: str, limit: int = 10, skip: int = 0):
    """Public: paginated reviews for an influencer.

    FIX (items 1 & 2): this used to (a) return the raw MongoDB review
    document — including user_id, a private/internal field never meant to
    be public — and (b) never checked whether the influencer itself exists
    or is active, so reviews for an inactive/deleted influencer were still
    publicly fetchable even though the influencer's own profile (404 via
    get_influencer_public above) was correctly hidden. Both are fixed here:
    the influencer is validated the exact same way get_influencer_public
    does (same 404 on invalid/missing/inactive), and only an explicit,
    public-safe field list is ever returned per review.
    """
    from fastapi import HTTPException
    try:
        oid = ObjectId(influencer_id)
    except Exception:
        raise HTTPException(404, "Influencer not found")
    influencer = db.influencers.find_one({"_id": oid, "status": "active"})
    if not influencer:
        raise HTTPException(404, "Influencer not found")

    total = db.influencer_reviews.count_documents({"influencer_id": influencer_id})
    cursor = (
        db.influencer_reviews.find({"influencer_id": influencer_id})
        .sort("created_at", -1).skip(skip).limit(limit)
    )
    reviews = [
        {
            "_id":           str(r["_id"]),
            "influencer_id": r.get("influencer_id", influencer_id),
            "user_name":     r.get("user_name", ""),
            "rating":        r.get("rating", 0),
            "text":          r.get("text", ""),
            "created_at":    _normalize_iso_utc(r.get("created_at", "")),
        }
        for r in cursor
    ]
    return {"reviews": reviews, "total": total}

@router.post("/influencers/{influencer_id}/review")
def submit_influencer_review(influencer_id: str, data: dict, request: _Req):
    """Authenticated: submit or update an influencer review (one per user)."""
    from fastapi import HTTPException as _HTTPEx
    try:
        oid = ObjectId(influencer_id)
    except Exception:
        raise _HTTPEx(400, "Invalid influencer id")
    influencer = db.influencers.find_one({"_id": oid, "status": "active"})
    if not influencer:
        raise _HTTPEx(404, "Influencer not found")

    rating = float(data.get("rating", 0))
    text   = (data.get("text", "") or "").strip()
    if not (1 <= rating <= 5):
        raise _HTTPEx(400, "Rating must be 1–5")
    if len(text) < 3:
        raise _HTTPEx(400, "Review text too short (min 3 chars)")

    user = _get_user_optional(request)
    if not user:
        raise _HTTPEx(401, "Session expired")
    user_id   = str(user["_id"])
    user_name = (user.get("name") or user.get("full_name") or "").strip() or "Anonymous"

    db.influencer_reviews.update_one(
        {"influencer_id": influencer_id, "user_id": user_id},
        {"$set": {
            "influencer_id": influencer_id,
            "user_id":       user_id,
            "user_name":     user_name,
            "rating":        rating,
            "text":          text,
            "updated_at":    _iso_utc_now(),
        }, "$setOnInsert": {"created_at": _iso_utc_now()}},
        upsert=True,
    )

    all_revs = list(db.influencer_reviews.find({"influencer_id": influencer_id}, {"rating": 1}))
    avg = round(sum(r["rating"] for r in all_revs) / len(all_revs), 1) if all_revs else rating
    db.influencers.update_one({"_id": oid}, {"$set": {"rating": avg, "review_count": len(all_revs)}})
    return {"ok": True, "message": "Review submitted!", "avg_rating": avg, "review_count": len(all_revs)}

@router.get("/influencers/{influencer_id}/my-review")
def get_my_influencer_review(influencer_id: str, request: _Req):
    """Authenticated: the current user's own review for this influencer, if any."""
    user = _get_user_optional(request)
    if not user:
        return {}
    user_id = str(user["_id"])
    try:
        ObjectId(influencer_id)
    except Exception:
        return {}
    rev = db.influencer_reviews.find_one({"influencer_id": influencer_id, "user_id": user_id})
    if not rev:
        return {}
    rev["_id"] = str(rev["_id"])
    if "created_at" in rev:
        rev["created_at"] = _normalize_iso_utc(rev["created_at"])
    if "updated_at" in rev:
        rev["updated_at"] = _normalize_iso_utc(rev["updated_at"])
    return rev


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

    # BUG FIX (Round 6 — Issue 3, "deal not appearing in Hot Deals"):
    # Root cause — this endpoint used to require the store to ALSO have a
    # fresh (non-expired) row in db.subscriptions before any of its deals
    # would show, on top of stores.status == "active". That is a DIFFERENT,
    # independent eligibility check from the one Today's Offers (get_store,
    # below) and create_deal() (routers/merchant_app.py) both use, which is
    # stores.status == "active" alone — the single authoritative field this
    # codebase treats as the source of truth for "is this store live"
    # (routers/admin.py's approve_store() sets it; Round 5's
    # _require_active_store() gates Banner/Product creation on it the same
    # way). A store approved directly by an admin (approve_store) — which is
    # exactly how a manually-created test store, or any store an admin
    # approves without routing it through the paid-subscribe flow, reaches
    # "active" — has status=="active" but may have NO db.subscriptions
    # document at all, which this endpoint's old check treated as "not
    # active" and silently dropped every one of its deals. create_deal()
    # itself already refuses to create a deal for a store whose status
    # isn't "active" (see its own check), so gating read-time visibility on
    # that same field is consistent, not a loosening of the rule — it's the
    # same rule Today's Offers and deal creation already enforce. The
    # subscriptions collection here was being used exactly the way the
    # codebase's own _is_store_subscription_active() docstring warns against
    # ("used for display purposes elsewhere, not as an authorization gate").
    # Expired/inactive deals and non-active stores remain excluded exactly
    # as before via deal_q's status=="active" and store_q's
    # status=="active" below — only the redundant, inconsistent
    # subscription-freshness pre-filter was removed.
    store_q = {"status": "active"}
    if city:
        store_q["city"] = {"$regex": city, "$options": "i"}

    stores_raw = list(db.stores.find(store_q, {
        "store_name": 1, "category": 1, "city": 1, "area": 1, "address": 1, "phone": 1,
        "image_url": 1, "image_thumb": 1, "_thumb": 1, "image": 1, "images": 1,
    }))
    stores_map = {str(s["_id"]): s for s in stores_raw}

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
        # Shared rule (same helper as Store Detail): valid through the end of
        # the end date, IST; missing/unparseable date → kept.
        end_date = d.get("end_date") or d.get("valid_until") or d.get("expiry") or ""
        if _is_deal_expired(d):
            continue

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
            # Item 3 (Round 5) added the deal's own uploaded image; prefer it
            # here and only fall back to the store's image when the deal has
            # none (matches "use fallback if no deal image exists").
            "image_url":   d.get("image_url") or _store_img(store),
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
    # Optional: caller may pass the checkout scope it intends to use this
    # code for (STORE / BANNERS / PRODUCTS) so the preview can reject a
    # code that's valid but mapped to a different checkout type. This is
    # a preview-time convenience only — _resolve_discount() in
    # merchant_app.py remains the sole authority at order-creation time.
    checkout_scope = (body.get("checkout_scope") or body.get("scope") or "").strip().upper()
    doc = db.discounts.find_one({"code": code})
    if not doc:
        raise HTTPException(404, "Invalid discount code")
    if not doc.get("active", True):
        raise HTTPException(400, "This code is no longer active")
    if doc.get("expiry_date") and datetime.utcnow() > doc["expiry_date"]:
        raise HTTPException(400, "This code has expired")
    if doc.get("max_uses", 0) > 0 and doc.get("used_count", 0) >= doc["max_uses"]:
        raise HTTPException(400, "This code has reached its usage limit")
    code_scope = str(doc.get("applies_to") or "ALL").upper()
    if checkout_scope and checkout_scope in {"STORE", "BANNERS", "PRODUCTS"} and code_scope != "ALL" and code_scope != checkout_scope:  # real checkout types only — "ALL" is a code-scope value, not a checkout type
        raise HTTPException(400, f"Code '{code}' is valid but can only be used for {code_scope.title()} checkout, not {checkout_scope.title()}.")
    return {
        "ok": True,
        "code": code,
        "type": doc.get("type", "VALUE"),
        "value": doc.get("value", 0),
        "applies_to": code_scope,
        "discount_id": str(doc["_id"])
    }

# =================== ABOUT US (public) ===================
@router.get("/about")
def get_about_public():
    doc = db.settings.find_one({"key": "about_us"}) or {}
    return {"content": doc.get("content", "")}

# =================== PROMO SLIDERS (public - for Flutter app) ===================
def _public_banner_cta(d, store_id):
    """Final (cta_type, cta_id, cta_store_id) for a live banner. A Product/Deal
    destination that no longer exists / is inactive falls back to the Store,
    so the customer app never receives a dead destination."""
    ct = str(d.get("cta_type") or "store").strip().lower()
    ci = str(d.get("cta_id") or "").strip()
    try:
        if ct == "deal" and ci:
            deal = db.deals.find_one({"_id": ObjectId(ci), "status": "active"}, {"store_id": 1})
            if deal:
                return ("deal", ci, str(deal.get("store_id", "") or store_id))
        elif ct == "product" and ci:
            oid = ObjectId(ci)
            prod = db.merchant_products.find_one(
                {"_id": oid, "status": "approved", "is_active": {"$ne": False}}, {"store_id": 1})
            if prod:
                return ("product", ci, str(prod.get("store_id", "") or store_id))
    except Exception:
        pass
    return ("store", store_id, store_id)

@router.get("/promo-sliders")
def get_promo_sliders_public():
    """Returns active, non-expired merchant banners (promo sliders) for the home screen."""
    from datetime import datetime as _dt
    _now = _dt.utcnow()
    docs = list(db.promo_sliders.find({"is_active": True}).sort("sort_order", 1))
    # Store names for the Home hero strip (one query for all slides).
    from bson import ObjectId as _OID
    _oids = []
    for d in docs:
        try:
            if d.get("store_id"):
                _oids.append(_OID(str(d["store_id"])))
        except Exception:
            pass
    _store_names = {str(s["_id"]): s.get("store_name", "") for s in
                    db.stores.find({"_id": {"$in": _oids}}, {"store_name": 1})} if _oids else {}
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
        _sid = str(d.get("store_id", "") or "")
        _ct, _ci, _cs = _public_banner_cta(d, _sid)
        result.append({
            "id":         str(d["_id"]),
            "store_id":   _sid,
            "store_name": d.get("store_name") or _store_names.get(_sid, ""),
            "cta_type":   _ct,
            "cta_id":     _ci,
            "cta_store_id": _cs,
            "title":      d.get("title", ""),
            "subtitle":   d.get("subtitle", "") or d.get("text", ""),
            "image":      img,
            "image_url":  img,
            # "image" for every banner created before video support existed
            "media_type": d.get("media_type") or ("video" if ("/video/upload/" in str(img) or str(img).startswith("data:video") or str(img).lower().split("?")[0].endswith(".mp4")) else "image"),
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

# ─── Default Images (fallback for stores/products/offers/cities) ──────────────
@router.get("/default-images")
def get_default_images():
    """Return default/fallback images configured by admin.
    Supports both legacy string and new array format per field."""
    doc = db.settings.find_one({"_type": "default_images"})
    if not doc:
        return {"store": "", "product": "", "offer": "", "city": "",
                "merchant_banner": "", "merchant_banner_explore_url": "", "no_service_url": "",
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
        "product":          _all_urls(doc.get("product", "")),
        "offer":            _first(doc.get("offer", "")),
        "city":             _all_urls(doc.get("city", "")),
        "merchant_banner":  _all_urls(doc.get("merchant_banner", "")),  # array — same pattern as city
        # The single default banner media the admin marked "Explore <City>" ("" = none).
        "merchant_banner_explore_url": doc.get("merchant_banner_explore_url", "") or "",
        "no_service_url":   _ns_val,
        "no_service_title": str(doc.get("no_service_title", "") or ""),
        "no_service_message": str(doc.get("no_service_message", "") or ""),
    }
