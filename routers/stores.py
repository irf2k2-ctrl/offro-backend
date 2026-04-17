from fastapi import APIRouter, Query, Depends
from database import db
from utils.auth import get_current_merchant
from bson import ObjectId

router = APIRouter()


# ----------------------------
# SAFE OBJECTID
# ----------------------------
def safe_object_id(val):
    try:
        return ObjectId(val)
    except:
        return val


# ----------------------------
# PUBLIC STORES (USER APP)
# ----------------------------
@router.get("/stores")
def get_stores(city: str = Query(None)):

    query = {"status": "active"}

    if city:
        query["city"] = city

    stores = list(db.stores.find(query))

    result = []

    for s in stores:

        try:
            merchant = db.merchants.find_one({
                "_id": safe_object_id(s.get("merchant_id"))
            })
        except:
            merchant = None

        # ✅ skip inactive merchants
        if merchant and merchant.get("status") != "active":
            continue

        s["_id"] = str(s["_id"])
        result.append(s)

    return result


# ----------------------------
# ADMIN CREATE STORE
# ----------------------------
@router.post("/admin/store")
def create_store(data: dict):

    store = {
        "merchant_id": str(data.get("merchant_id")),
        "store_name": data.get("store_name"),
        "category": data.get("category"),
        "city": data.get("city"),
        "address": data.get("address"),
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "visit_points": data.get("visit_points", 0),
        "pool_points": data.get("pool_points", 0),
        "status": "active"
    }

    result = db.stores.insert_one(store)

    return {
        "message": "Store created",
        "store_id": str(result.inserted_id)
    }


# ----------------------------
# MERCHANT CREATE STORE
# ----------------------------
@router.post("/merchant/store")
def merchant_create_store(data: dict, merchant=Depends(get_current_merchant)):

    store = {
        "merchant_id": str(merchant["_id"]),
        "store_name": data.get("store_name"),
        "city": data.get("city"),
        "area": data.get("area"),
        "visit_points": data.get("visit_points", 0),
        "pool_points": data.get("pool_points", 0),
        "status": "active"
    }

    result = db.stores.insert_one(store)

    return {
        "message": "Store created",
        "store_id": str(result.inserted_id)
    }


# ----------------------------
# MERCHANT STORES (DASHBOARD) ✅ FIXED
# ----------------------------
@router.get("/merchant/stores")
def get_merchant_stores(merchant=Depends(get_current_merchant)):

    merchant_id = str(merchant["_id"])

    stores = list(db.stores.find({
        "merchant_id": merchant_id
    }))

    result = []

    for s in stores:
        result.append({
            "_id": str(s["_id"]),
            "store_name": s.get("store_name"),
            "city": s.get("city"),
            "area": s.get("area"),
            "status": s.get("status", "active")
        })

    return result


# ----------------------------
# ADMIN STORES
# ----------------------------
@router.get("/admin/stores")
def admin_get_stores(city: str = Query(None)):

    query = {}

    if city:
        query["city"] = city

    stores = list(db.stores.find(query))

    result = []

    for s in stores:

        merchant = db.merchants.find_one({
            "_id": safe_object_id(s.get("merchant_id"))
        })

        deal = db.deals.find_one({
            "store_id": str(s["_id"])
        })

        result.append({
            "_id": str(s["_id"]),
            "store_name": s.get("store_name"),
            "merchant_name": merchant.get("name") if merchant else "N/A",
            "city": s.get("city"),
            "status": s.get("status", "active"),
            "start_date": deal.get("start_date") if deal else "",
            "end_date": deal.get("end_date") if deal else ""
        })

    return result