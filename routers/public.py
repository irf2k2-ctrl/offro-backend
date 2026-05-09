from fastapi import APIRouter, Query
from database import db
from bson import ObjectId

router = APIRouter(tags=["Public"])

# ─────────────────────────────────────────────────────────────
# HELPER — strip base64 from image fields, keep URLs only
# Base64 images are served from /store-image/<id> endpoint
# ─────────────────────────────────────────────────────────────
def _img_url(store_id: str, field: str, val: str | None) -> str | None:
    """Return URL if already a URL, or a lazy endpoint if base64, or None."""
    if not val:
        return None
    if val.startswith("http"):
        return val
    if val.startswith("data:image"):
        # Serve base64 via a lightweight proxy endpoint — clients cache it
        return f"/store-image/{store_id}/{field}"
    return None


# =================== PUBLIC STORES LIST (PAGINATED) ===================
@router.get("/stores")
def get_stores(
    city:     str  = None,
    category: str  = None,
    limit:    int  = Query(default=50, ge=1, le=200),
    skip:     int  = Query(default=0, ge=0),
):
    """
    Public endpoint — Flutter app fetches this.
    Supports pagination: /stores?limit=20&skip=0
    Images: base64 stripped from list — use /store-image/<id>/<field> to load individually.
    """
    query = {"status": "active"}
    if city:
        query["city"] = {"$regex": f"^{city.strip()}$", "$options": "i"}
    if category and category != "All":
        query["category"] = category

    # Only lightweight fields — NO image/base64 in list response
    stores = list(db.stores.find(query, {
        "store_name":1,"category":1,"city":1,"area":1,"address":1,"phone":1,
        "image":1,"store_image2":1,"status":1,"points_per_scan":1,
        "lat":1,"lng":1,"rating":1,"admin_rating":1,"is_new_in_town":1,"badge":1,
        "merchant_id":1,
    }).skip(skip).limit(limit))

    total_count = db.stores.count_documents(query)

    if not stores:
        return {"stores": [], "total": 0, "limit": limit, "skip": skip, "has_more": False}

    # Pre-fetch ALL active deals in ONE query — no N+1
    store_ids = [str(s["_id"]) for s in stores]
    deals_by_store: dict = {}
    try:
        all_deals = list(db.deals.find(
            {"store_id": {"$in": store_ids}, "status": "active"},
            {"store_id":1,"title":1,"discount":1,"discount_percent":1}
        ))
        for d in all_deals:
            sid = d.get("store_id","")
            deals_by_store.setdefault(sid, []).append(d)
    except Exception:
        pass

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

        # Image: return URL if hosted, or lazy endpoint if base64, or None
        raw_img  = s.get("image") or ""
        raw_img2 = s.get("store_image2") or ""
        img_out  = _img_url(store_id, "image",  raw_img)
        img2_out = _img_url(store_id, "image2", raw_img2)

        result.append({
            "_id":          store_id,
            "store_name":   s.get("store_name"),
            "category":     s.get("category", ""),
            "city":         s.get("city", ""),
            "area":         s.get("area", ""),
            "address":      s.get("address", ""),
            "phone":        s.get("phone", ""),
            "image":        img_out,
            "image2":       img2_out,
            "status":       s.get("status", "active"),
            "visit_points": s.get("points_per_scan", 10),
            "points_per_scan": s.get("points_per_scan", 10),
            "latitude":     s.get("lat") or None,
            "longitude":    s.get("lng") or None,
            "rating":       display_rating,
            "offer":        deal_summary,
            "deal_count":   deal_count,
            "is_new_in_town": s.get("is_new_in_town", False),
            "badge":        s.get("badge", ""),
            "merchant_id":  s.get("merchant_id", ""),
        })

    return {
        "stores":   result,
        "total":    total_count,
        "limit":    limit,
        "skip":     skip,
        "has_more": (skip + limit) < total_count,
    }


# =================== LAZY IMAGE ENDPOINT ===================
@router.get("/store-image/{store_id}/{field}")
def get_store_image(store_id: str, field: str):
    """
    Serve base64 image as proper HTTP image response.
    Flutter's CachedNetworkImage will cache this by URL — loads once, cached forever.
    field: 'image' or 'image2'
    """
    from fastapi import HTTPException
    from fastapi.responses import Response
    import base64, re

    if field not in ("image", "image2"):
        raise HTTPException(400, "field must be image or image2")

    db_field = "image" if field == "image" else "store_image2"
    try:
        store = db.stores.find_one(
            {"_id": ObjectId(store_id)},
            {db_field: 1}
        )
    except Exception:
        raise HTTPException(400, "Invalid store_id")

    if not store:
        raise HTTPException(404, "Store not found")

    raw = store.get(db_field, "") or ""
    if not raw.startswith("data:image"):
        raise HTTPException(404, "No image")

    # Parse data URL
    match = re.match(r"data:([^;]+);base64,(.+)", raw, re.DOTALL)
    if not match:
        raise HTTPException(400, "Bad image data")

    mime    = match.group(1)
    b64data = match.group(2)
    try:
        img_bytes = base64.b64decode(b64data)
    except Exception:
        raise HTTPException(500, "Image decode error")

    return Response(
        content    = img_bytes,
        media_type = mime,
        headers    = {
            "Cache-Control": "public, max-age=86400",  # cache 24h on client
            "Content-Length": str(len(img_bytes)),
        }
    )


# =================== SINGLE STORE ===================
@router.get("/stores/{store_id}")
def get_store(store_id: str):
    from fastapi import HTTPException
    try:
        store = db.stores.find_one({"_id": ObjectId(store_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid store_id")
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    sid = str(store["_id"])
    deals = []
    try:
        deals = list(db.deals.find({"store_id": sid, "status": "active"}))
    except Exception:
        pass

    deals_list = [{
        "title":       d.get("title"),
        "discount":    d.get("discount"),
        "category":    d.get("category"),
        "description": d.get("description"),
        "start_date":  d.get("start_date"),
        "end_date":    d.get("end_date")
    } for d in deals]

    # Full image for detail page — serve proxy URL or hosted URL
    raw_img  = store.get("image")  or ""
    raw_img2 = store.get("store_image2") or store.get("image2") or ""
    img_out  = _img_url(sid, "image",  raw_img)
    img2_out = _img_url(sid, "image2", raw_img2)

    return {
        "_id":          sid,
        "store_name":   store.get("store_name"),
        "category":     store.get("category", ""),
        "city":         store.get("city", ""),
        "area":         store.get("area", ""),
        "address":      store.get("address", ""),
        "phone":        store.get("phone", ""),
        "image":        img_out,
        "image2":       img2_out,
        "latitude":     store.get("lat") or None,
        "longitude":    store.get("lng") or None,
        "visit_points": store.get("points_per_scan", 10),
        "rating":       float(store.get("admin_rating") or store.get("rating") or 0),
        "about":        store.get("about") or store.get("description") or "",
        "deals":        deals_list,
    }


# =================== PROMO SLIDERS ===================
@router.get("/promo-sliders")
def get_promo_sliders_public():
    docs = list(db.promo_sliders.find({"is_active": True}).sort("sort_order", 1))
    return [{"id":str(p["_id"]),"title":p.get("title",""),"image_url":p.get("image_url",""),
             "link":p.get("link",""),"order":p.get("sort_order",1)} for p in docs]


# =================== PUBLIC CATEGORIES ===================
@router.get("/categories")
def get_categories():
    doc = db.categories.find_one({})
    return doc.get("categories", ["Grocery","Restaurant","Pharmacy","Electronics","Clothing","Bakery","Salon","Other"]) if doc else []


# =================== PUBLIC TERMS ===================
@router.get("/terms/{type}")
def get_terms_public(type: str):
    from fastapi import HTTPException
    if type not in ("merchant", "user"):
        raise HTTPException(status_code=400, detail="type must be merchant or user")
    doc = db.terms.find_one({"type": type}) or {}
    return {"type": type, "content": doc.get("content", "")}


# =================== POLICY ===================
@router.get("/policy/{policy_type}")
def get_policy(policy_type: str):
    doc = db.policies.find_one({"type": policy_type}) or {}
    content = doc.get("content", "")
    if not content:
        defaults = {"privacy": _default_privacy(), "refund": _default_refund(), "kyc": _default_kyc()}
        content = defaults.get(policy_type, "")
    return {"content": content}


# =================== SOCIAL LINKS ===================
@router.get("/social")
def get_social_public():
    doc = db.settings.find_one({"key": "social_links"}) or {}
    return {
        "whatsapp":  doc.get("whatsapp", ""),
        "facebook":  doc.get("facebook", ""),
        "instagram": doc.get("instagram", ""),
        "youtube":   doc.get("youtube", ""),
    }


# =================== ABOUT US ===================
@router.get("/about")
def get_about_public():
    doc = db.settings.find_one({"key": "about_us"}) or {}
    return {"content": doc.get("content", "")}


def _default_privacy():
    return "Privacy Policy — Contact support@offro.app"

def _default_refund():
    return "Refund Policy — Contact support@offro.app"

def _default_kyc():
    return "KYC Policy — Contact support@offro.app"
