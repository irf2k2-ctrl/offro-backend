from fastapi import APIRouter, HTTPException, Depends
from database import db
from utils.auth import get_current_merchant  # ✅ Ensure correct import
from fastapi.requests import Request  # ✅ Proper Request import

router = APIRouter(prefix="/merchant/dashboard", tags=["Merchant Dashboard"])

# ---------------- Dashboard Overview ---------------- #
@router.get("/overview")
def dashboard_overview(request: Request, user=Depends(get_current_merchant)):  # ✅ Correct use of Request and dependency
    merchant_id = user["user_id"]

    total_stores = db.stores.count_documents({"merchant_id": merchant_id})
    total_deals = db.deals.count_documents({"merchant_id": merchant_id})
    total_redemptions = db.redemptions.count_documents({"merchant_id": merchant_id})
    total_customers = len(db.redemptions.distinct("user_id", {"merchant_id": merchant_id}))

    return {
        "total_stores": total_stores,
        "total_deals": total_deals,
        "total_redemptions": total_redemptions,
        "total_customers": total_customers
    }

# ---------------- Get All Stores ---------------- #
@router.get("/stores")
def get_stores(request: Request, user=Depends(get_current_merchant)):  # ✅ Proper dependency and Request use
    merchant_id = user["user_id"]

    stores = list(db.stores.find({"merchant_id": merchant_id}))

    for s in stores:
        s["_id"] = str(s["_id"])

    return stores

# ---------------- Get All Deals ---------------- #
@router.get("/deals")
def get_deals(request: Request, user=Depends(get_current_merchant)):  # ✅ Same here for all requests
    merchant_id = user["user_id"]

    deals = list(db.deals.find({"merchant_id": merchant_id}))

    result = []

    for d in deals:
        result.append({
            "_id": str(d["_id"]),
            "store_id": d.get("store_id"),
            "discount": d.get("discount"),
            "category": d.get("category"),
            "start_date": d.get("start_date"),
            "end_date": d.get("end_date"),
            "status": d.get("status", "active")
        })

    return result