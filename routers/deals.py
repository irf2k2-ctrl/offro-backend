from fastapi import APIRouter, Depends, HTTPException
from database import db
from datetime import datetime
from bson import ObjectId
from utils.auth import get_current_merchant

router = APIRouter()


# ---------------- EXPIRY CHECK ---------------- #
def check_expiry(deal):

    if deal.get("end_date"):
        try:
            end = datetime.strptime(str(deal["end_date"]), "%Y-%m-%d")

            if datetime.now() > end and deal.get("status") != "expired":
                db.deals.update_one(
                    {"_id": deal["_id"]},
                    {"$set": {"status": "expired"}}
                )
                deal["status"] = "expired"
        except:
            pass

    return deal


# ---------------- VALIDATE STORE ---------------- #
def validate_store(store_id, merchant_id):

    store = db.stores.find_one({"_id": ObjectId(store_id)})

    if not store:
        raise HTTPException(404, "Store not found")

    if str(store.get("merchant_id")) != str(merchant_id):
        raise HTTPException(403, "Unauthorized store")

    return store


# ---------------- CREATE DEAL ---------------- #
@router.post("/merchant/deal")
def create_deal(data: dict, merchant=Depends(get_current_merchant)):

    merchant_id = str(merchant["_id"])
    store_id = data.get("store_id")

    validate_store(store_id, merchant_id)

    deal = {
        "merchant_id": merchant_id,
        "store_id": store_id,
        "discount": data.get("discount"),
        "category": data.get("category_id"),
        "start_date": str(data.get("start_date")) if data.get("start_date") else None,
        "end_date": str(data.get("end_date")) if data.get("end_date") else None,
        "status": data.get("status", "active"),
        "created_at": datetime.now()
    }

    result = db.deals.insert_one(deal)

    return {
        "message": "Deal created",
        "deal_id": str(result.inserted_id)
    }


# ---------------- GET SINGLE DEAL ---------------- #
@router.get("/merchant/deal/{deal_id}")
def get_deal(deal_id: str, merchant=Depends(get_current_merchant)):

    deal = db.deals.find_one({"_id": ObjectId(deal_id)})

    if not deal:
        raise HTTPException(404, "Deal not found")

    if deal.get("merchant_id") != str(merchant["_id"]):
        raise HTTPException(403, "Unauthorized")

    deal = check_expiry(deal)

    return {
        "_id": str(deal["_id"]),
        "store_id": deal.get("store_id"),
        "discount": deal.get("discount"),
        "category_id": deal.get("category"),
        "start_date": deal.get("start_date") or "",
        "end_date": deal.get("end_date") or "",
        "status": deal.get("status")
    }


# ---------------- UPDATE DEAL ---------------- #
@router.put("/merchant/deal/{deal_id}")
def update_deal(deal_id: str, data: dict, merchant=Depends(get_current_merchant)):

    deal = db.deals.find_one({"_id": ObjectId(deal_id)})

    if not deal:
        raise HTTPException(404, "Deal not found")

    if deal.get("merchant_id") != str(merchant["_id"]):
        raise HTTPException(403, "Unauthorized")

    validate_store(data.get("store_id") or deal["store_id"], str(merchant["_id"]))

    update_fields = {}

    if "discount" in data:
        update_fields["discount"] = data.get("discount")

    if "category_id" in data:
        update_fields["category"] = data.get("category_id")

    if "start_date" in data:
        update_fields["start_date"] = str(data.get("start_date")) if data.get("start_date") else None

    if "end_date" in data:
        update_fields["end_date"] = str(data.get("end_date")) if data.get("end_date") else None

    if "status" in data:
        update_fields["status"] = data.get("status")

    if update_fields:
        db.deals.update_one(
            {"_id": ObjectId(deal_id)},
            {"$set": update_fields}
        )

    return {"message": "Deal updated"}


# ---------------- STATUS TOGGLE ---------------- #
@router.put("/merchant/deal/{deal_id}/status")
def update_status(deal_id: str, data: dict, merchant=Depends(get_current_merchant)):

    deal = db.deals.find_one({"_id": ObjectId(deal_id)})

    if not deal:
        raise HTTPException(404, "Deal not found")

    if deal.get("merchant_id") != str(merchant["_id"]):
        raise HTTPException(403, "Unauthorized")

    status = data.get("status")

    if status not in ["active", "paused"]:
        raise HTTPException(400, "Invalid status")

    db.deals.update_one(
        {"_id": ObjectId(deal_id)},
        {"$set": {"status": status}}
    )

    return {"message": "Status updated"}


# ---------------- STORE DEALS ---------------- #
@router.get("/store/{store_id}/deals")
def get_store_deals(store_id: str):

    deals = list(db.deals.find({"store_id": store_id}))

    result = []

    for deal in deals:
        deal = check_expiry(deal)

        result.append({
            "_id": str(deal["_id"]),
            "store_id": deal.get("store_id"),
            "discount": deal.get("discount"),
            "category": deal.get("category"),
            "start_date": deal.get("start_date") or "",
            "end_date": deal.get("end_date") or "",
            "status": deal.get("status")
        })

    return result