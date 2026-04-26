from fastapi import APIRouter
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
    if category and category != "All":
        query["category"] = category

    stores = list(db.stores.find(query))
    result = []
    for s in stores:
        # Get active deals for this store
        store_id = str(s["_id"])
        cols = db.list_collection_names()
        deals = list(db.deals.find({"store_id": store_id, "status": "active"})) \
            if "deals" in cols else []
        deal_count = len(deals)
        deal_summary = None
        if deals:
            d = deals[0]
            deal_summary = f"{d.get('discount','')}% off — {d.get('title','')}"

        result.append({
            "_id": store_id,
            "store_name": s.get("store_name"),
            "category": s.get("category", ""),
            "city": s.get("city", ""),
            "area": s.get("area", ""),
            "address": s.get("address", ""),
            "phone": s.get("phone", ""),
            "image": s.get("image") or None,
            "image_thumb": s.get("image_thumb") or s.get("image") or None,
            "status": s.get("status", "active"),
            "visit_points": s.get("points_per_scan", 10),
            "points_per_scan": s.get("points_per_scan", 10),
            "latitude": s.get("lat") or None,
            "longitude": s.get("lng") or None,
            "offer":      deal_summary,
            "deal_count":    deal_count,
            "is_new_in_town": s.get("is_new_in_town", False),
            "merchant_id": s.get("merchant_id", "")
        })
    return result

# =================== SINGLE STORE ===================
@router.get("/stores/{store_id}")
def get_store(store_id: str):
    try:
        store = db.stores.find_one({"_id": ObjectId(store_id)})
    except Exception:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid store_id")
    if not store:
        from fastapi import HTTPException
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
    return {
        "_id": str(store["_id"]),
        "store_name": store.get("store_name"),
        "category": store.get("category", ""),
        "city": store.get("city", ""),
        "area": store.get("area", ""),
        "address": store.get("address", ""),
        "phone": store.get("phone", ""),
        "image": store.get("image") or None,
        "image2": store.get("image2") or None,
        "image_thumb": store.get("image_thumb") or store.get("image") or None,
        "images": store.get("images", []),
        "latitude": store.get("lat") or None,
        "longitude": store.get("lng") or None,
        "visit_points": store.get("points_per_scan", 10),
        "points_per_scan": store.get("points_per_scan", 10),
        "about": store.get("about") or store.get("description") or "",
        "description": store.get("description") or "",
        "open_time": store.get("open_time", ""),
        "close_time": store.get("close_time", ""),
        "cost_for_two": store.get("cost_for_two", ""),
        "dine_in": store.get("dine_in", False),
        "tags": store.get("tags", []),
        "rating": store.get("rating", 0.0),
        "rating_count": store.get("rating_count", 0),
        "is_new_in_town": store.get("is_new_in_town", False),
        "merchant_id": store.get("merchant_id", ""),
        "deals": deals_list
    }

# =================== PUBLIC CATEGORIES ===================
@router.get("/categories")
def get_categories():
    doc = db.categories.find_one({})
    return doc.get("categories", ["Grocery","Restaurant","Pharmacy","Electronics","Clothing","Bakery","Salon","Other"]) if doc else []

# =================== PUBLIC TERMS ===================
@router.get("/terms/{type}")
def get_terms_public(type: str):
    if type not in ("merchant", "user"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="type must be merchant or user")
    doc = db.terms.find_one({"type": type}) or {}
    return {"type": type, "content": doc.get("content", "")}


# =================== TERMS ===================
@router.get("/terms/{doc_type}")
def get_terms(doc_type: str):
    doc = db.terms.find_one({"type": doc_type}) or {}
    content = doc.get("content", "")
    if not content:
        defaults = {"user": _default_user_terms(), "merchant": _default_merchant_terms()}
        content = defaults.get(doc_type, "")
    return {"content": content}

# =================== POLICIES ===================
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
@router.get("/categories")
def get_categories():
    cats = db.stores.distinct("category", {"status": "active"})
    return [c for c in cats if c]

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
# =================== STORE RATING ===================
@router.post("/stores/{store_id}/rate")
def rate_store(store_id: str, data: dict, request: Request):
    """User rates a store (1-5 stars). Requires user token in Authorization header."""
    from fastapi import Request as Req
    from datetime import datetime
    # Authenticate user
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.users.find_one({"token": token})
    if not user:
        raise HTTPException(status_code=403, detail="Invalid session")

    rating = float(data.get("rating", 0))
    if not (1 <= rating <= 5):
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5")
    try:
        sid = ObjectId(store_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid store_id")

    store = db.stores.find_one({"_id": sid})
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    user_id = str(user["_id"])
    # One rating per user per store
    existing = db.ratings.find_one({"store_id": store_id, "user_id": user_id})
    if existing:
        raise HTTPException(status_code=400, detail="You have already rated this store")

    db.ratings.insert_one({
        "store_id": store_id,
        "user_id": user_id,
        "rating": rating,
        "created_at": datetime.utcnow()
    })

    # Recalculate average rating for store
    agg = list(db.ratings.aggregate([
        {"$match": {"store_id": store_id}},
        {"$group": {"_id": None, "avg": {"$avg": "$rating"}, "count": {"$sum": 1}}}
    ]))
    avg_rating = round(agg[0]["avg"], 1) if agg else rating
    count = agg[0]["count"] if agg else 1

    db.stores.update_one({"_id": sid}, {"$set": {"rating": avg_rating, "rating_count": count}})
    return {"ok": True, "your_rating": rating, "store_rating": avg_rating, "total_ratings": count}


@router.get("/stores/{store_id}/my-rating")
def get_my_rating(store_id: str, request: Request):
    """Get current user's rating for a store."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        return {"rated": False, "rating": None}
    user = db.users.find_one({"token": token})
    if not user:
        return {"rated": False, "rating": None}
    user_id = str(user["_id"])
    existing = db.ratings.find_one({"store_id": store_id, "user_id": user_id})
    if not existing:
        return {"rated": False, "rating": None}
    return {"rated": True, "rating": existing.get("rating")}

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
