from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from database import db
from bson import ObjectId
import uuid

router = APIRouter(tags=["Merchant"])

def create_token():
    return str(uuid.uuid4())

def get_current_merchant(request: Request):
    token = request.cookies.get("token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and "Bearer " in auth_header:
            token = auth_header.split(" ")[1]
    if not token:
        raise HTTPException(status_code=401, detail="No token found")
    merchant = db.merchants.find_one({"token": token})
    if not merchant:
        raise HTTPException(status_code=403, detail="Invalid session")
    return merchant

# ---------------- REGISTER ---------------- #
@router.post("/register")
def register_merchant(data: dict):
    phone = str(data.get("phone", "")).strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Phone is required")
    existing = db.merchants.find_one({"phone": {"$in": [phone, int(phone) if phone.isdigit() else phone]}})
    if existing:
        raise HTTPException(status_code=400, detail="Merchant already registered")
    merchant = {
        "name": data.get("name", "").strip(),
        "phone": phone,
        "area": data.get("area", "").strip(),
        "city": data.get("city", "").strip(),
        "status": "active",
        "token": None
    }
    db.merchants.insert_one(merchant)
    return {"message": "Registered successfully"}

# ---------------- LOGIN ---------------- #
@router.post("/login")
def merchant_login(data: dict):
    phone = str(data.get("phone", "")).strip()
    merchant = db.merchants.find_one({"phone": {"$in": [phone, int(phone) if phone.isdigit() else phone]}})
    if not merchant:
        raise HTTPException(status_code=401, detail="Phone not registered")
    if merchant.get("status") != "active":
        raise HTTPException(status_code=403, detail="Account inactive. Contact admin.")
    token = create_token()
    db.merchants.update_one({"_id": merchant["_id"]}, {"$set": {"token": token}})
    response = JSONResponse(content={
        "merchant_id": str(merchant["_id"]),
        "name": merchant.get("name"),
        "token": token
    })
    response.set_cookie(key="token", value=token, httponly=True,
                        samesite="Lax", secure=False, max_age=3600)
    return response

# ---------------- LOGOUT ---------------- #
@router.post("/logout")
def logout():
    res = JSONResponse(content={"message": "Logged out"})
    res.delete_cookie("token")
    return res

# ---------------- OVERVIEW ---------------- #
@router.get("/dashboard/overview")
def merchant_overview(merchant=Depends(get_current_merchant)):
    merchant_id = str(merchant["_id"])
    stores = list(db.stores.find({"merchant_id": merchant_id}))
    store_ids = [str(s["_id"]) for s in stores]
    collections = db.list_collection_names()
    return {
        "total_stores": len(stores),
        "total_deals": db.deals.count_documents({"store_id": {"$in": store_ids}}) if "deals" in collections else 0,
        "total_redemptions": db.redemptions.count_documents({"store_id": {"$in": store_ids}}) if "redemptions" in collections else 0,
        "total_customers": db.users.count_documents({}) if "users" in collections else 0,
    }

# ---------------- STORES (merchant view) ---------------- #
@router.get("/dashboard/stores")
def merchant_stores(merchant=Depends(get_current_merchant)):
    merchant_id = str(merchant["_id"])
    stores = list(db.stores.find({"merchant_id": merchant_id}))
    result = []
    for s in stores:
        store_id = str(s["_id"])
        collections = db.list_collection_names()
        deal_count = db.deals.count_documents({"store_id": store_id}) if "deals" in collections else 0
        result.append({
            "_id": store_id,
            "store_name": s.get("store_name"),
            "category": s.get("category"),
            "city": s.get("city"),
            "area": s.get("area"),
            "address": s.get("address"),
            "phone": s.get("phone"),
            "status": s.get("status", "active"),
            "qr_code": s.get("qr_code", ""),
            "points_per_scan": s.get("points_per_scan", 0),
            "visit_points": s.get("visit_points", 0),
            "image": s.get("image") or "",
            "deal_count": deal_count
        })
    return result

# ---------------- DEALS (merchant view) ---------------- #
@router.get("/dashboard/deals")
def merchant_deals(merchant=Depends(get_current_merchant)):
    merchant_id = str(merchant["_id"])
    collections = db.list_collection_names()
    if "deals" not in collections:
        return []
    deals = list(db.deals.find({"merchant_id": merchant_id}))
    result = []
    for d in deals:
        store = db.stores.find_one({"_id": ObjectId(d.get("store_id", ""))}) if d.get("store_id") else None
        result.append({
            "_id": str(d["_id"]),
            "store_name": store.get("store_name") if store else "Unknown",
            "store_id": d.get("store_id"),
            "title": d.get("title"),
            "discount": d.get("discount"),
            "category": d.get("category"),
            "description": d.get("description"),
            "start_date": d.get("start_date"),
            "end_date": d.get("end_date"),
            "status": d.get("status", "active")
        })
    return result

# ---------------- ADD DEAL ---------------- #
@router.post("/dashboard/deals")
def add_deal(data: dict, merchant=Depends(get_current_merchant)):
    merchant_id = str(merchant["_id"])
    store_id = data.get("store_id")
    if not store_id:
        raise HTTPException(status_code=400, detail="store_id required")
    # Verify store belongs to merchant
    store = db.stores.find_one({"_id": ObjectId(store_id), "merchant_id": merchant_id})
    if not store:
        raise HTTPException(status_code=403, detail="Store not found or not yours")
    deal = {
        "merchant_id": merchant_id,
        "store_id": store_id,
        "title": data.get("title", ""),
        "discount": data.get("discount", 0),
        "category": data.get("category", ""),
        "description": data.get("description", ""),
        "start_date": data.get("start_date", ""),
        "end_date": data.get("end_date", ""),
        "status": "active"
    }
    result = db.deals.insert_one(deal)
    return {"message": "Deal added", "deal_id": str(result.inserted_id)}

# ---------------- DELETE DEAL ---------------- #
@router.delete("/dashboard/deals/{deal_id}")
def delete_deal(deal_id: str, merchant=Depends(get_current_merchant)):
    merchant_id = str(merchant["_id"])
    db.deals.delete_one({"_id": ObjectId(deal_id), "merchant_id": merchant_id})
    return {"message": "Deal deleted"}

# ---------------- PROFILE ---------------- #
@router.get("/profile/me")
def merchant_me(merchant=Depends(get_current_merchant)):
    return {
        "id": str(merchant["_id"]),
        "name": merchant.get("name"),
        "phone": merchant.get("phone"),
        "city": merchant.get("city"),
        "area": merchant.get("area")
    }
