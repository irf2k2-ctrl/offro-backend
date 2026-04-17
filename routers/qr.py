from fastapi import APIRouter, HTTPException
from bson import ObjectId
from datetime import datetime

from database import redemptions_collection

router = APIRouter(prefix="/qr", tags=["QR System"])


# ------------------ GENERATE QR DATA ------------------ #
@router.get("/generate/{redemption_id}")
def generate_qr(redemption_id: str):
    redemption = redemptions_collection.find_one({"_id": ObjectId(redemption_id)})

    if not redemption:
        raise HTTPException(status_code=404, detail="Redemption not found")

    if redemption["status"] != "pending":
        raise HTTPException(status_code=400, detail="QR only for pending redemption")

    # QR payload (simple version)
    qr_data = {
        "redemption_id": str(redemption["_id"]),
        "user_id": redemption["user_id"],
        "points": redemption["points"]
    }

    return {
        "message": "QR generated",
        "qr_data": qr_data
    }


# ------------------ SCAN QR ------------------ #
@router.post("/scan")
def scan_qr(data: dict):
    redemption_id = data.get("redemption_id")

    if not redemption_id:
        raise HTTPException(status_code=400, detail="Missing redemption_id")

    redemption = redemptions_collection.find_one({"_id": ObjectId(redemption_id)})

    if not redemption:
        raise HTTPException(status_code=404, detail="Invalid QR")

    if redemption["status"] == "approved":
        raise HTTPException(status_code=400, detail="Already used")

    return {
        "message": "QR valid",
        "redemption_id": redemption_id,
        "points": redemption["points"],
        "status": redemption["status"]
    }