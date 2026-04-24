from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from database import db
from bson import ObjectId
import uuid, qrcode, io, base64
from datetime import datetime, timedelta

router = APIRouter(tags=["Users"])

def _qr(store_id: str) -> str:
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(f"offro://redeem?store_id={store_id}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="#3E5F55", back_color="white")
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode()

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
        "name": name, "phone": phone,
        "city": data.get("city", ""),
        "visit_points": 0, "pool_points": 0,
        "token": None, "favorites": []
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
        "total_points": user.get("visit_points", 0) + user.get("pool_points", 0),
        "profile_image": user.get("profile_image")
    }

@router.put("/city")
def update_city(data: dict, user=Depends(get_current_user)):
    city = data.get("city", "").strip()
    db.users.update_one({"_id": user["_id"]}, {"$set": {"city": city}})
    return {"message": "City updated"}

@router.put("/profile")
def update_profile(data: dict, user=Depends(get_current_user)):
    allowed = ["name", "city", "profile_image"]
    upd = {k: data[k] for k in allowed if k in data}
    if not upd:
        raise HTTPException(400, "Nothing to update")
    db.users.update_one({"_id": user["_id"]}, {"$set": upd})
    return {"message": "Profile updated"}

# =================== WALLET ===================
@router.get("/wallet")
def get_wallet(user=Depends(get_current_user)):
    return {
        "user_id": str(user["_id"]),
        "visit_points": user.get("visit_points", 0),
        "pool_points": user.get("pool_points", 0),
        "total_points": user.get("visit_points", 0)
    }

@router.post("/wallet/withdraw")
def withdraw(data: dict, user=Depends(get_current_user)):
    amount = int(data.get("amount", 0))
    pts = user.get("visit_points", 0)
    if amount < 200:
        raise HTTPException(400, "Minimum withdrawal is 200 points")
    if pts < amount:
        raise HTTPException(400, f"Insufficient points. You have {pts} points.")
    db.users.update_one({"_id": user["_id"]}, {"$inc": {"visit_points": -amount}})
    db.withdraw_requests.insert_one({
        "user_id": str(user["_id"]),
        "user_name": user.get("name"),
        "user_phone": user.get("phone"),
        "points": amount,
        "inr_value": amount / 10,
        "status": "pending",
        "created_at": datetime.utcnow()
    })
    return {"message": f"Withdrawal of {amount} points (₹{amount/10:.0f}) requested. Gift voucher delivered within 3–5 business days."}

# =================== REDEEM QR ===================
@router.post("/redeem")
def redeem_qr(data: dict, request: Request):
    """
    Called when user scans a store QR code.
    After successful redemption, the store QR is regenerated for security.
    """
    store_id = data.get("store_id")
    user_token = data.get("user_token") or request.cookies.get("user_token")

    if not store_id:
        raise HTTPException(status_code=400, detail="store_id required")
    if not user_token:
        raise HTTPException(status_code=401, detail="User not authenticated")

    user = db.users.find_one({"token": user_token})
    if not user:
        raise HTTPException(status_code=403, detail="Invalid user token")

    try:
        store = db.stores.find_one({"_id": ObjectId(store_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid store_id")

    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    if store.get("status") != "active":
        raise HTTPException(status_code=403, detail="Store is not active")

    # 24-hour cooldown per store per user
    since = datetime.utcnow() - timedelta(hours=24)
    recent = db.redemptions.find_one({
        "user_id": str(user["_id"]),
        "store_id": store_id,
        "created_at": {"$gte": since}
    })
    if recent:
        raise HTTPException(status_code=429, detail="Already redeemed from this store today. Try again tomorrow.")

    pts = store.get("points_per_scan", 10)
    db.users.update_one({"_id": user["_id"]}, {"$inc": {"visit_points": pts}})
    db.redemptions.insert_one({
        "user_id": str(user["_id"]),
        "user_name": user.get("name"),
        "user_phone": user.get("phone"),
        "store_id": store_id,
        "store_name": store.get("store_name"),
        "points": pts,
        "created_at": datetime.utcnow()
    })

    # 🔐 Regenerate QR code for security after successful scan
    new_qr = _qr(store_id)
    db.stores.update_one({"_id": ObjectId(store_id)}, {"$set": {"qr_code": new_qr}})

    return {"message": f"✅ {pts} points added! Keep earning more.", "points": pts, "qr_regenerated": True}

# =================== REDEMPTION HISTORY ===================
@router.get("/redemptions")
def redemption_history(user=Depends(get_current_user)):
    user_id = str(user["_id"])
    redemptions = list(db.redemptions.find({"user_id": user_id}).sort("created_at", -1).limit(50))
    result = []
    for r in redemptions:
        r["_id"] = str(r["_id"])
        created = r.get("created_at")
        r["date"] = created.isoformat() if created else ""
        # Fetch store image for the scan history card
        store_img = None
        store_id = r.get("store_id")
        if store_id:
            try:
                store_doc = db.stores.find_one({"_id": ObjectId(store_id)}, {"image": 1})
                if store_doc:
                    store_img = store_doc.get("image")
            except Exception:
                pass
        r["store_image"] = store_img
        result.append(r)
    return result

# =================== FAVORITES ===================
@router.get("/favorites")
def get_favorites(user=Depends(get_current_user)):
    fav_ids = user.get("favorites", [])
    if not fav_ids:
        return []
    stores = []
    for sid in fav_ids:
        try:
            s = db.stores.find_one({"_id": ObjectId(sid), "status": "active"})
            if s:
                stores.append({
                    "_id": str(s["_id"]),
                    "store_name": s.get("store_name"),
                    "category": s.get("category", ""),
                    "city": s.get("city", ""),
                    "area": s.get("area", ""),
                    "image": s.get("image"),
                    "images": s.get("images", []),
                    "visit_points": s.get("points_per_scan", 10),
                    "rating": s.get("rating", 0),
                })
        except Exception:
            pass
    return stores

@router.get("/favorites/{store_id}/check")
def check_favorite(store_id: str, user=Depends(get_current_user)):
    fav_ids = user.get("favorites", [])
    return {"is_favorite": store_id in fav_ids}

@router.post("/favorites/{store_id}")
def toggle_favorite(store_id: str, user=Depends(get_current_user)):
    fav_ids = user.get("favorites", [])
    if store_id in fav_ids:
        fav_ids.remove(store_id)
        action = "removed"
    else:
        fav_ids.append(store_id)
        action = "added"
    db.users.update_one({"_id": user["_id"]}, {"$set": {"favorites": fav_ids}})
    return {"message": f"Favourite {action}", "is_favorite": action == "added"}
