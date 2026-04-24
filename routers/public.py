from fastapi import APIRouter, HTTPException, Depends, Request
from database import db
from bson import ObjectId
from datetime import datetime
import math

router = APIRouter(tags=["Public"])

def _dist(lat1, lon1, lat2, lon2):
    """Haversine distance in km"""
    try:
        R = 6371
        dlat = math.radians(float(lat2) - float(lat1))
        dlon = math.radians(float(lon2) - float(lon1))
        a = math.sin(dlat/2)**2 + math.cos(math.radians(float(lat1)))*math.cos(math.radians(float(lat2)))*math.sin(dlon/2)**2
        return round(R * 2 * math.asin(math.sqrt(a)), 2)
    except Exception:
        return None

def get_current_user_optional(request: Request):
    token = request.cookies.get("user_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if "Bearer " in auth:
            token = auth.split(" ")[1]
    if not token:
        return None
    return db.users.find_one({"token": token})

# =================== PUBLIC STORES LIST ===================
@router.get("/stores")
def get_stores(city: str = None, category: str = None,
               lat: float = None, lng: float = None):
    query = {"status": "active"}
    if city:
        query["city"] = {"$regex": city, "$options": "i"}
    if category and category != "All":
        query["category"] = category

    stores = list(db.stores.find(query))
    result = []
    for s in stores:
        store_id = str(s["_id"])
        deals = list(db.deals.find({"store_id": store_id, "status": "active"}).limit(3)) \
            if "deals" in db.list_collection_names() else []
        deal_summary = None
        deal_count = len(deals)
        if deals:
            d = deals[0]
            deal_summary = f"{d.get('discount','')}% off — {d.get('title','')}"

        # Calculate distance
        distance_km = None
        if lat is not None and lng is not None:
            distance_km = _dist(lat, lng, s.get("lat", ""), s.get("lng", ""))

        # Aggregate rating
        rating = s.get("rating", 0)

        result.append({
            "_id": store_id,
            "store_name": s.get("store_name"),
            "category": s.get("category", ""),
            "city": s.get("city", ""),
            "area": s.get("area", ""),
            "address": s.get("address", ""),
            "phone": s.get("phone", ""),
            "image": s.get("image") or None,
            "images": s.get("images", []),
            "about": s.get("about", ""),
            "logo": s.get("logo") or None,
            "state": s.get("state", ""),
            "status": s.get("status", "active"),
            "visit_points": s.get("points_per_scan", 10),
            "points_per_scan": s.get("points_per_scan", 10),
            "latitude": s.get("lat") or None,
            "longitude": s.get("lng") or None,
            "offer": deal_summary,
            "deal_count": deal_count,
            "rating": rating,
            "rating_count": s.get("rating_count", 0),
            "distance_km": distance_km,
            "is_new_in_town": s.get("is_new_in_town", False),
            "merchant_id": s.get("merchant_id", "")
        })

    # Sort by distance if available
    if lat is not None and lng is not None:
        result.sort(key=lambda x: (x["distance_km"] or 9999))

    return result

# =================== SINGLE STORE ===================
@router.get("/stores/{store_id}")
def get_store(store_id: str, lat: float = None, lng: float = None):
    try:
        store = db.stores.find_one({"_id": ObjectId(store_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid store_id")
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    deals = list(db.deals.find({"store_id": store_id, "status": "active"})) \
        if "deals" in db.list_collection_names() else []
    deals_list = [{
        "title": d.get("title"),
        "discount": d.get("discount"),
        "category": d.get("category"),
        "description": d.get("description"),
        "start_date": d.get("start_date"),
        "end_date": d.get("end_date")
    } for d in deals]

    distance_km = None
    if lat is not None and lng is not None:
        distance_km = _dist(lat, lng, store.get("lat",""), store.get("lng",""))

    return {
        "_id": str(store["_id"]),
        "store_name": store.get("store_name"),
        "category": store.get("category", ""),
        "city": store.get("city", ""),
        "area": store.get("area", ""),
        "address": store.get("address", ""),
        "phone": store.get("phone", ""),
        "image": store.get("image") or None,
        "images": store.get("images", []),           # multiple images
        "about": store.get("about", ""),
        "logo": store.get("logo") or None,
        "state": store.get("state", ""),
        "latitude": store.get("lat") or None,
        "longitude": store.get("lng") or None,
        "visit_points": store.get("points_per_scan", 10),
        "open_time": store.get("open_time", ""),
        "close_time": store.get("close_time", ""),
        "cost_for_two": store.get("cost_for_two", ""),
        "dine_in": store.get("dine_in", False),
        "tags": store.get("tags", []),
        "description": store.get("description", ""),
        "rating": store.get("rating", 0),
        "rating_count": store.get("rating_count", 0),
        "distance_km": distance_km,
        "deals": deals_list
    }

# =================== STORE RATING ===================
@router.post("/stores/{store_id}/rate")
def rate_store(store_id: str, data: dict, request: Request):
    """Rate a store — one rating per user per store."""
    token = request.cookies.get("user_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if "Bearer " in auth:
            token = auth.split(" ")[1]
    if not token:
        raise HTTPException(401, "Not authenticated")
    user = db.users.find_one({"token": token})
    if not user:
        raise HTTPException(403, "Invalid session")

    rating_val = float(data.get("rating", 0))
    if rating_val < 1 or rating_val > 5:
        raise HTTPException(400, "Rating must be between 1 and 5")

    user_id = str(user["_id"])

    # Check existing rating
    existing = db.store_ratings.find_one({"store_id": store_id, "user_id": user_id})
    if existing:
        raise HTTPException(400, "You have already rated this store")

    db.store_ratings.insert_one({
        "store_id": store_id,
        "user_id": user_id,
        "rating": rating_val,
        "created_at": datetime.utcnow()
    })

    # Recalculate average rating
    all_ratings = list(db.store_ratings.find({"store_id": store_id}))
    avg = sum(r["rating"] for r in all_ratings) / len(all_ratings)
    avg = round(avg, 1)
    db.stores.update_one({"_id": ObjectId(store_id)},
        {"$set": {"rating": avg, "rating_count": len(all_ratings)}})

    return {"message": "Thank you for your rating!", "rating": rating_val, "avg_rating": avg}

@router.get("/stores/{store_id}/my-rating")
def get_my_rating(store_id: str, request: Request):
    token = request.cookies.get("user_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if "Bearer " in auth:
            token = auth.split(" ")[1]
    if not token:
        raise HTTPException(401, "Not authenticated")
    user = db.users.find_one({"token": token})
    if not user:
        raise HTTPException(403, "Invalid session")
    r = db.store_ratings.find_one({"store_id": store_id, "user_id": str(user["_id"])})
    if not r:
        raise HTTPException(404, "Not rated yet")
    return {"rating": r["rating"]}

# =================== PUBLIC CATEGORIES ===================
@router.get("/categories")
def get_categories():
    doc = db.categories.find_one({})
    if doc and doc.get("categories"):
        return doc["categories"]
    cats = db.stores.distinct("category", {"status": "active"})
    return [c for c in cats if c] or ["Grocery","Restaurant","Pharmacy","Electronics","Clothing","Bakery","Salon"]

# =================== TERMS / POLICIES ===================
@router.get("/terms/{type}")
def get_terms_public(type: str):
    doc = db.terms.find_one({"type": type}) or {}
    return {"type": type, "content": doc.get("content", "")}

@router.get("/policy/{policy_type}")
def get_policy(policy_type: str):
    doc = db.policies.find_one({"type": policy_type}) or {}
    return {"content": doc.get("content", "")}

@router.get("/about")
def get_about():
    doc = db.about.find_one({}) or {}
    return {"content": doc.get("content", "Offro connects local stores with customers through deals and loyalty points.")}

@router.get("/social")
def get_social():
    doc = db.social.find_one({}) or {}
    doc.pop("_id", None)
    return doc

@router.post("/discount/validate")
def validate_discount(data: dict):
    code = data.get("code", "").strip().upper()
    if not code:
        raise HTTPException(400, "Code required")
    doc = db.discount_codes.find_one({"code": code, "active": True})
    if not doc:
        raise HTTPException(404, "Invalid or expired discount code")
    return {"code": code, "discount": doc.get("discount", 0), "description": doc.get("description", "")}
