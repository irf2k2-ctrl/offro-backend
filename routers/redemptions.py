from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime
from bson import ObjectId
from fastapi.responses import StreamingResponse
import qrcode
import io

from database import (
    deals_collection,
    redemptions_collection,
    users_collection,
    wallet_collection   # ✅ NEW
)

router = APIRouter(prefix="/redemptions", tags=["Redemptions"])


# ------------------ REQUEST MODEL ------------------ #
class RedeemRequest(BaseModel):
    deal_id: str
    user_id: str


# ------------------ CREATE REDEMPTION ------------------ #
@router.post("/redeem")
def redeem_offer(data: RedeemRequest):

    user = users_collection.find_one({"_id": ObjectId(data.user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        deal = deals_collection.find_one({"_id": ObjectId(data.deal_id)})
    except:
        raise HTTPException(status_code=400, detail="Invalid deal ID")

    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    redemption = {
        "user_id": str(user["_id"]),
        "deal_id": data.deal_id,
        "store_id": str(deal.get("store_id")),
        "merchant_id": str(deal.get("merchant_id")),
        "points": deal.get("points", 0),
        "status": "pending",
        "created_at": datetime.utcnow()
    }

    result = redemptions_collection.insert_one(redemption)

    return {
        "message": "Redemption request created",
        "redemption_id": str(result.inserted_id),
        "status": "pending"
    }


# ------------------ GENERATE QR ------------------ #
@router.get("/qr/{redemption_id}")
def generate_qr(redemption_id: str):

    qr_data = f"REDEEM:{redemption_id}"

    qr = qrcode.make(qr_data)

    buffer = io.BytesIO()
    qr.save(buffer, format="PNG")
    buffer.seek(0)

    return StreamingResponse(buffer, media_type="image/png")


# ------------------ SCAN QR & APPROVE ------------------ #
@router.post("/scan")
def scan_qr(data: dict):

    qr_value = data.get("qr")

    if not qr_value or not qr_value.startswith("REDEEM:"):
        raise HTTPException(status_code=400, detail="Invalid QR")

    redemption_id = qr_value.split(":")[1]

    # 🔍 Get redemption
    redemption = redemptions_collection.find_one({
        "_id": ObjectId(redemption_id)
    })

    if not redemption:
        raise HTTPException(status_code=404, detail="Redemption not found")

    if redemption["status"] != "pending":
        return {"message": "Already processed"}

    # 🔍 Get user
    user = users_collection.find_one({
        "_id": ObjectId(redemption["user_id"])
    })

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.get("wallet", 0) < redemption["points"]:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    # ✅ Deduct wallet
    users_collection.update_one(
        {"_id": ObjectId(user["_id"])},
        {"$inc": {"wallet": -redemption["points"]}}
    )

    # ✅ SAVE WALLET TRANSACTION (NEW)
    wallet_collection.insert_one({
        "user_id": str(user["_id"]),
        "redemption_id": str(redemption["_id"]),
        "points": redemption["points"],
        "type": "debit",
        "description": "Redeemed deal",
        "created_at": datetime.utcnow()
    })

    # ✅ Approve redemption
    redemptions_collection.update_one(
        {"_id": ObjectId(redemption["_id"])},
        {"$set": {"status": "approved"}}
    )

    return {
        "message": "QR scanned & redemption approved",
        "status": "approved"
    }


# ------------------ GET USER REDEMPTIONS ------------------ #
@router.get("/my/{user_id}")
def get_user_redemptions(user_id: str):

    redemptions = list(
        redemptions_collection.find({"user_id": user_id})
    )

    for r in redemptions:
        r["_id"] = str(r["_id"])

    return redemptions


# ------------------ WALLET HISTORY ------------------ #
@router.get("/wallet/{user_id}")
def get_wallet_history(user_id: str):

    transactions = list(
        wallet_collection.find({"user_id": user_id})
    )

    for t in transactions:
        t["_id"] = str(t["_id"])

    return transactions