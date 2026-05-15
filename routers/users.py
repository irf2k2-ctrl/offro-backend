from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from database import db
from bson import ObjectId
import uuid

# ── Phone normalisation helper ─────────────────────────────────
def _phone_variants(raw: str) -> list:
    p = str(raw).strip().replace(" ", "").replace("-", "")
    d = p[1:] if p.startswith("+") else p
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    last10 = d[-10:] if len(d) >= 10 else d
    return list({p, f"+91{last10}", f"91{last10}", last10, f"0{last10}"})

def _normalise_phone(raw: str) -> str:
    p = str(raw).strip().replace(" ", "").replace("-", "")
    d = p[1:] if p.startswith("+") else p
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    last10 = d[-10:] if len(d) >= 10 else d
    return f"+91{last10}" if len(last10) == 10 else p


router = APIRouter(tags=["Users"])

def get_current_user(request: Request):
    token = request.cookies.get("user_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if "Bearer " in auth:
            token = auth.split(" ")[1]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.users.find_one({"token": token})
    if not user:
        raise HTTPException(status_code=403, detail="Invalid session")
    return user

# ══════════════════════════════════════════════════════════════════════════════
# OTP — SEND
# POST /user/send-otp
# Body: { "phone": "+91XXXXXXXXXX" }
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/send-otp")
def send_otp_endpoint(data: dict):
    """
    DEPRECATED — OTP is now handled by MSG91 Widget SDK on the Flutter side.
    Kept for backward compatibility only. Not called by current app version.
    """
    return {"message": "OTP is now handled by MSG91 Widget SDK.", "deprecated": True}


# ══════════════════════════════════════════════════════════════════════════════
# OTP — VERIFY  (replaces client-side 1234 check)
# POST /user/verify-otp
# Body: { "phone": "+91XXXXXXXXXX", "otp": "XXXX" }
# Returns session token on success — same shape as old /login response
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/verify-otp")
def verify_otp_endpoint(data: dict):
    """
    DEPRECATED — OTP verification is now handled by MSG91 Widget SDK on the Flutter side.
    Kept for backward compatibility only. Not called by current app version.
    """
    return {"message": "OTP verification is now handled by MSG91 Widget SDK.", "deprecated": True}
# ══════════════════════════════════════════════════════════════════════════════
# REGISTER  (unchanged — register first, then OTP flow)
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/register")
def register_user(data: dict):
    raw_phone = str(data.get("phone", "")).strip()
    name = data.get("name", "").strip()
    if not raw_phone or not name:
        raise HTTPException(status_code=400, detail="Name and phone are required")
    phone = _normalise_phone(raw_phone)
    if db.users.find_one({"phone": {"$in": _phone_variants(raw_phone)}}):
        raise HTTPException(status_code=400, detail="Phone already registered")
    user = {
        "name":         name,
        "phone":        phone,
        "city":         data.get("city", ""),
        "visit_points": 0,
        "pool_points":  0,
        "token":        None,
    }
    result = db.users.insert_one(user)
    return {"message": "Registered successfully", "user_id": str(result.inserted_id)}


# ══════════════════════════════════════════════════════════════════════════════
# LOGIN  (kept for backward compat — now triggers OTP send, no direct token)
# POST /user/login
# Flutter calls this to initiate login → then shows OTP screen → calls verify-otp
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/login")
def login_user(data: dict):
    """
    MSG91 Widget flow — OTP is verified entirely on the Flutter side via MSG91 SDK.
    This endpoint is called AFTER MSG91 verifyOTP succeeds on device.
    Validates phone exists → issues OFFRO session token.
    """
    raw_phone = str(data.get("phone", "")).strip()
    user = db.users.find_one({"phone": {"$in": _phone_variants(raw_phone)}})
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Phone not registered. Please register first."
        )

    token = str(uuid.uuid4())
    db.users.update_one({"_id": user["_id"]}, {"$set": {"token": token}})
    print(f"[LOGIN] ✅ Session issued for {raw_phone} — user_id={str(user['_id'])}")

    response = JSONResponse(content={
        "user_id":      str(user["_id"]),
        "name":         user.get("name", ""),
        "phone":        user.get("phone", raw_phone),
        "token":        token,
        "visit_points": user.get("visit_points", 0),
        "pool_points":  user.get("pool_points", 0),
    })
    response.set_cookie(
        key="user_token", value=token, httponly=True,
        samesite="Lax", secure=False, max_age=3600 * 24 * 30,
    )
    return response


# ══════════════════════════════════════════════════════════════════════════════
# LOGOUT
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/logout")
def logout_user():
    res = JSONResponse(content={"message": "Logged out"})
    res.delete_cookie("user_token")
    return res


# ══════════════════════════════════════════════════════════════════════════════
# PROFILE
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/me")
def get_profile(user=Depends(get_current_user)):
    return {
        "user_id":      str(user["_id"]),
        "_id":          str(user["_id"]),
        "name":         user.get("name", ""),
        "phone":        user.get("phone", ""),
        "city":         user.get("city", ""),
        "visit_points": user.get("visit_points", 0),
        "pool_points":  user.get("pool_points", 0),
        "total_points": user.get("visit_points", 0) + user.get("pool_points", 0),
    }


# ══════════════════════════════════════════════════════════════════════════════
# WALLET
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/wallet")
def get_wallet(user=Depends(get_current_user)):
    visit   = user.get("visit_points", 0)
    pool    = user.get("pool_points", 0)
    pricing = db.pricing.find_one({}) or {}
    rate    = float(pricing.get("conversion_rate", 0.10))
    min_w   = int(pricing.get("min_withdraw_points", 200))
    total   = visit + pool
    return {
        "visit_points":       visit,
        "pool_points":        pool,
        "total_points":       total,
        "conversion_rate":    rate,
        "min_withdraw_points": min_w,
        "value_in_rupees":    round(total * rate, 2),
        "profile_image":      user.get("profile_image", None),
    }

@router.post("/wallet/withdraw")
def withdraw(data: dict, user=Depends(get_current_user)):
    pricing    = db.pricing.find_one({}) or {}
    min_withdraw = int(pricing.get("min_withdraw_points", 200))
    visit      = user.get("visit_points", 0)
    pool       = user.get("pool_points", 0)
    total      = visit + pool
    amount     = int(data.get("amount", min_withdraw))
    if total < min_withdraw:
        raise HTTPException(status_code=400, detail=f"Minimum {min_withdraw} points required. You have {total}.")
    if total < amount:
        raise HTTPException(status_code=400, detail=f"Not enough points. You have {total}.")
    db.users.update_one({"_id": user["_id"]}, {"$set": {"pending_withdraw": True}})
    from datetime import datetime
    db.withdraw_requests.insert_one({
        "user_id":      str(user["_id"]),
        "user_name":    user.get("name"),
        "phone":        user.get("phone"),
        "email":        user.get("email", ""),
        "points":       amount,
        "voucher_value": round(amount / 10, 2),
        "status":       "pending",
        "created_at":   datetime.utcnow(),
    })
    return {
        "message":         "Gift Voucher request submitted! You will receive your Amazon/Flipkart voucher within 3-5 business days.",
        "remaining_points": total,
    }


# ══════════════════════════════════════════════════════════════════════════════
# QR REDEEM  (unchanged)
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/redeem")
def redeem_qr(data: dict, request: Request):
    store_id   = data.get("store_id")
    user_token = data.get("user_token") or request.cookies.get("user_token")
    if not store_id:
        raise HTTPException(status_code=400, detail="store_id required")
    if not user_token:
        raise HTTPException(status_code=401, detail="User not authenticated")
    user = db.users.find_one({"token": user_token})
    if not user:
        raise HTTPException(status_code=403, detail="Invalid user session")
    try:
        store = db.stores.find_one({"_id": ObjectId(store_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid store_id")
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    if store.get("status") != "active":
        raise HTTPException(status_code=400, detail="Store is not active")

    points_to_add = int(store.get("points_per_scan", 10))
    user_id       = str(user["_id"])

    from datetime import datetime, timedelta
    recent = db.redemptions.find_one({
        "user_id":  user_id,
        "store_id": store_id,
        "created_at": {"$gte": datetime.utcnow() - timedelta(hours=24)},
    })
    if recent:
        raise HTTPException(status_code=429, detail="Already redeemed from this store today. Try again tomorrow.")

    db.users.update_one({"_id": user["_id"]}, {"$inc": {"visit_points": points_to_add}})
    db.redemptions.insert_one({
        "user_id":     user_id,
        "store_id":    store_id,
        "store_name":  store.get("store_name"),
        "merchant_id": store.get("merchant_id"),
        "points":      points_to_add,
        "created_at":  datetime.utcnow(),
    })
    updated = db.users.find_one({"_id": user["_id"]})
    return {
        "message":      f"✅ {points_to_add} points added!",
        "store_name":   store.get("store_name"),
        "points_earned": points_to_add,
        "total_points":  updated.get("visit_points", 0) + updated.get("pool_points", 0),
    }


# ══════════════════════════════════════════════════════════════════════════════
# CITY / PROFILE / HISTORY / FAVOURITES / FCM  (all unchanged from previous)
# ══════════════════════════════════════════════════════════════════════════════
@router.put("/city")
def update_city(data: dict, user=Depends(get_current_user)):
    city = data.get("city", "").strip()
    if city:
        db.users.update_one({"_id": user["_id"]}, {"$set": {"city": city}})
    return {"message": "City updated", "city": city}

@router.get("/redemptions")
def redemption_history(user=Depends(get_current_user)):
    user_id    = str(user["_id"])
    redemptions = list(db.redemptions.find({"user_id": user_id}).sort("created_at", -1).limit(50))
    result = []
    for r in redemptions:
        result.append({
            "store_name": r.get("store_name"),
            "points":     r.get("points"),
            "date": (
                (lambda dt: dt.strftime("%d %b %Y %H:%M IST")
                 if hasattr(dt, "strftime") else str(dt)[:16].replace("T", " ")
                )(r["created_at"])
            ) if r.get("created_at") else "",
        })
    return result

@router.get("/wallet/history")
def wallet_transaction_history(user=Depends(get_current_user)):
    user_id = str(user["_id"])
    txns    = list(db.point_transactions.find({"user_id": user_id}).sort("created_at", -1).limit(100))
    result  = []
    for t in txns:
        result.append({
            "type":   t.get("type", "credit"),
            "points": t.get("points", 0),
            "note":   t.get("note", ""),
            "date": (
                (lambda dt: dt.strftime("%d %b %Y %H:%M")
                 if hasattr(dt, "strftime") else str(dt)[:16].replace("T", " ")
                )(t["created_at"])
            ) if t.get("created_at") else "",
        })
    return result

@router.get("/favorites")
def list_favorites(user=Depends(get_current_user)):
    fav_ids = user.get("favorite_store_ids", [])
    from bson import ObjectId as OId
    valid_ids = []
    for fid in fav_ids:
        try: valid_ids.append(OId(str(fid)))
        except: pass
    stores = list(db.stores.find({"_id": {"$in": valid_ids}}))
    result = []
    for s in stores:
        img = s.get("image") or (s.get("images") or [None])[0] or ""
        result.append({
            "_id":        str(s["_id"]),
            "store_name": s.get("store_name", ""),
            "category":   s.get("category", ""),
            "area":       s.get("area", ""),
            "city":       s.get("city", ""),
            "rating":     float(s.get("admin_rating") or s.get("rating") or 0),
            "image":      img,
        })
    return result

@router.post("/favorites/{store_id}")
def toggle_favorite(store_id: str, user=Depends(get_current_user)):
    user_id = user["_id"]
    fav_ids = [str(f) for f in user.get("favorite_store_ids", [])]
    if store_id in fav_ids:
        db.users.update_one({"_id": user_id}, {"$pull":     {"favorite_store_ids": store_id}})
        return {"is_favorite": False}
    else:
        db.users.update_one({"_id": user_id}, {"$addToSet": {"favorite_store_ids": store_id}})
        return {"is_favorite": True}

@router.get("/favorites/{store_id}/check")
def check_favorite(store_id: str, user=Depends(get_current_user)):
    fav_ids = [str(f) for f in user.get("favorite_store_ids", [])]
    return {"is_favorite": store_id in fav_ids}

@router.put("/profile")
def update_user_profile(data: dict, user=Depends(get_current_user)):
    allowed = ["profile_image", "name"]
    update  = {k: v for k, v in data.items() if k in allowed}
    if not update:
        raise HTTPException(400, "Nothing to update")
    db.users.update_one({"_id": user["_id"]}, {"$set": update})
    return {"ok": True}

@router.post("/fcm-token")
def save_fcm_token(data: dict, user=Depends(get_current_user)):
    fcm_token = data.get("fcm_token", "").strip()
    if fcm_token:
        db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"fcm_token": fcm_token, "fcm_updated_at": __import__("datetime").datetime.utcnow()}}
        )
    return {"ok": True}
