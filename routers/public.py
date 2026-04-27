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
            disc = d.get("discount") or d.get("discount_percent")
            try:
                disc_val = int(float(str(disc))) if disc not in (None, "", "null") else None
            except (ValueError, TypeError):
                disc_val = None
            if disc_val and disc_val > 0:
                deal_summary = f"{disc_val}% off — {d.get('title','')}"
            elif d.get("title"):
                deal_summary = d.get("title","")

        # Use admin_rating if set, else raw rating
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
            "image": s.get("image") or None,
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
        "image2": store.get("store_image2") or None,
        "images": store.get("images", []),
        "latitude": store.get("lat") or None,
        "longitude": store.get("lng") or None,
        "visit_points": store.get("points_per_scan", 10),
        "rating": float(store.get("admin_rating") or store.get("rating") or 0),
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
from fastapi import Request as _Req

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
