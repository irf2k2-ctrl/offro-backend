from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from database import db
from bson import ObjectId
import uuid

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

# =================== REGISTER ===================
@router.post("/register")
def register_user(data: dict):
    phone = str(data.get("phone", "")).strip()
    name = data.get("name", "").strip()
    if not phone or not name:
        raise HTTPException(status_code=400, detail="Name and phone are required")
    if db.users.find_one({"phone": phone}):
        raise HTTPException(status_code=400, detail="Phone already registered")
    user = {
        "name": name,
        "phone": phone,
        "city": data.get("city", ""),
        "visit_points": 0,
        "pool_points": 0,
        "token": None
    }
    result = db.users.insert_one(user)
    return {"message": "Registered successfully", "user_id": str(result.inserted_id)}

# =================== LOGIN ===================
@router.post("/login")
def login_user(data: dict):
    phone = str(data.get("phone", "")).strip()
    user = db.users.find_one({"phone": phone})
    if not user:
        raise HTTPException(status_code=401, detail="Phone not registered")
    token = str(uuid.uuid4())
    db.users.update_one({"_id": user["_id"]}, {"$set": {"token": token}})
    response = JSONResponse(content={
        "user_id": str(user["_id"]),
        "name": user.get("name"),
        "phone": user.get("phone"),
        "token": token,
        "visit_points": user.get("visit_points", 0),
        "pool_points": user.get("pool_points", 0)
    })
    response.set_cookie(key="user_token", value=token, httponly=True,
                        samesite="Lax", secure=False, max_age=3600 * 24 * 30)
    return response

# =================== LOGOUT ===================
@router.post("/logout")
def logout_user():
    res = JSONResponse(content={"message": "Logged out"})
    res.delete_cookie("user_token")
    return res

# =================== PROFILE ===================
@router.get("/me")
def get_profile(user=Depends(get_current_user)):
    return {
        "user_id": str(user["_id"]),
        "name": user.get("name"),
        "phone": user.get("phone"),
        "city": user.get("city", ""),
        "visit_points": user.get("visit_points", 0),
        "pool_points": user.get("pool_points", 0),
        "total_points": user.get("visit_points", 0) + user.get("pool_points", 0)
    }

# =================== WALLET ===================
@router.get("/wallet")
def get_wallet(user=Depends(get_current_user)):
    visit = user.get("visit_points", 0)
    pool = user.get("pool_points", 0)
    pricing = db.pricing.find_one({}) or {}
    rate = float(pricing.get("conversion_rate", 0.10))
    min_w = int(pricing.get("min_withdraw_points", 200))
    total = visit + pool
    return {
        "visit_points": visit,
        "pool_points": pool,
        "total_points": total,
        "conversion_rate": rate,
        "min_withdraw_points": min_w,
        "value_in_rupees": round(total * rate, 2),
        "profile_image":  user.get("profile_image", None),
    }

@router.post("/wallet/withdraw")
def withdraw(data: dict, user=Depends(get_current_user)):
    pricing = db.pricing.find_one({}) or {}
    min_withdraw = int(pricing.get("min_withdraw_points", 200))
    visit = user.get("visit_points", 0)
    pool = user.get("pool_points", 0)
    total = visit + pool
    amount = int(data.get("amount", min_withdraw))
    if total < min_withdraw:
        raise HTTPException(status_code=400, detail=f"Minimum {min_withdraw} points required to withdraw. You have {total}.")
    if total < amount:
        raise HTTPException(status_code=400, detail=f"Not enough points. You have {total}.")
    # Mark pending_withdraw on user (don't deduct yet — deduct when voucher is sent)
    db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"pending_withdraw": True}}
    )
    from datetime import datetime
    db.withdraw_requests.insert_one({
        "user_id": str(user["_id"]),
        "user_name": user.get("name"),
        "phone": user.get("phone"),
        "email": user.get("email",""),
        "points": amount,
        "voucher_value": round(amount / 10, 2),
        "status": "pending",
        "created_at": datetime.utcnow()
    })
    return {"message": "Gift Voucher request submitted! You will receive your Amazon/Flipkart voucher within 3-5 business days.", "remaining_points": total}

# =================== QR REDEEM ===================
@router.post("/redeem")
def redeem_qr(data: dict, request: Request):
    """
    Called when user scans a store QR code.
    Payload: { store_id, user_token or user_id }
    """
    store_id = data.get("store_id")
    user_token = data.get("user_token") or request.cookies.get("user_token")

    if not store_id:
        raise HTTPException(status_code=400, detail="store_id required")
    if not user_token:
        raise HTTPException(status_code=401, detail="User not authenticated")

    user = db.users.find_one({"token": user_token})
    if not user:
        raise HTTPException(status_code=403, detail="Invalid user session")

    # Find store
    try:
        store = db.stores.find_one({"_id": ObjectId(store_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid store_id")

    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    if store.get("status") != "active":
        raise HTTPException(status_code=400, detail="Store is not active")

    points_to_add = int(store.get("points_per_scan", 10))
    user_id = str(user["_id"])

    # Prevent duplicate scan within 24 hours
    from datetime import datetime, timedelta
    recent = db.redemptions.find_one({
        "user_id": user_id,
        "store_id": store_id,
        "created_at": {"$gte": datetime.utcnow() - timedelta(hours=24)}
    })
    if recent:
        raise HTTPException(status_code=429, detail="Already redeemed from this store today. Try again tomorrow.")

    # Add points
    db.users.update_one(
        {"_id": user["_id"]},
        {"$inc": {"visit_points": points_to_add}}
    )
    db.redemptions.insert_one({
        "user_id": user_id,
        "store_id": store_id,
        "store_name": store.get("store_name"),
        "merchant_id": store.get("merchant_id"),
        "points": points_to_add,
        "created_at": datetime.utcnow()
    })

    updated_user = db.users.find_one({"_id": user["_id"]})
    return {
        "message": f"✅ {points_to_add} points added!",
        "store_name": store.get("store_name"),
        "points_earned": points_to_add,
        "total_points": updated_user.get("visit_points", 0) + updated_user.get("pool_points", 0)
    }


# =================== UPDATE CITY ===================
@router.put("/city")
def update_city(data: dict, user=Depends(get_current_user)):
    city = data.get("city", "").strip()
    if city:
        db.users.update_one({"_id": user["_id"]}, {"$set": {"city": city}})
    return {"message": "City updated", "city": city}

# =================== REDEMPTION HISTORY ===================
@router.get("/redemptions")
def redemption_history(user=Depends(get_current_user)):
    user_id = str(user["_id"])
    redemptions = list(db.redemptions.find({"user_id": user_id}).sort("created_at", -1).limit(50))
    result = []
    for r in redemptions:
        result.append({
            "store_name": r.get("store_name"),
            "points": r.get("points"),
            "date": r.get("created_at").strftime("%d %b %Y %H:%M") if r.get("created_at") else ""
        })
    return result


# =================== UPDATE USER PROFILE (image etc.) ===================
@router.put("/profile")
def update_user_profile(data: dict, user=Depends(get_current_user)):
    allowed = ["profile_image", "name"]
    update = {k: v for k, v in data.items() if k in allowed}
    if not update:
        from fastapi import HTTPException
        raise HTTPException(400, "Nothing to update")
    db.users.update_one({"_id": user["_id"]}, {"$set": update})
    return {"ok": True}
