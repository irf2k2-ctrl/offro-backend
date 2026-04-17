from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta
from bson import ObjectId

router = APIRouter()

# -------- REQUEST MODEL --------
class QRScanRequest(BaseModel):
    user_id: str
    store_id: str
    qr_code: str


# -------- QR SCAN API --------
@router.post("/scan-qr")
def scan_qr(data: QRScanRequest, db=Depends(lambda: None)):

    # Fetch user and store using db connection
    user = db.users.find_one({"_id": ObjectId(data.user_id)})
    store = db.stores.find_one({"_id": ObjectId(data.store_id)})

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    # Simple QR validation (match the store ID)
    if data.qr_code != str(store["_id"]):
        raise HTTPException(status_code=400, detail="Invalid QR code")

    # Prevent duplicate scan in a short time period (e.g., 10 minutes)
    last_scan = db.transactions.find_one({
        "user_id": data.user_id,
        "store_id": data.store_id,
        "type": "visit"
    }, sort=[("created_at", -1)])

    if last_scan:
        time_diff = datetime.utcnow() - last_scan["created_at"]
        if time_diff < timedelta(minutes=10):  # You can adjust the time limit
            raise HTTPException(
                status_code=400,
                detail="Already scanned recently. Please wait a few minutes."
            )

    # Points logic (use store's visit points or default to 10)
    points_earned = store.get("visit_points", 10)

    # Update user's wallet with earned points
    db.users.update_one(
        {"_id": ObjectId(data.user_id)},
        {"$inc": {"visit_points": points_earned}}
    )

    # Add transaction log (for auditing)
    db.transactions.insert_one({
        "user_id": data.user_id,
        "store_id": data.store_id,
        "points_added": points_earned,
        "type": "visit",
        "created_at": datetime.utcnow()
    })

    # Fetch updated user
    updated_user = db.users.find_one({"_id": ObjectId(data.user_id)})

    return {
        "status": "success",
        "points_added": points_earned,
        "new_balance": updated_user.get("visit_points", 0)
    }