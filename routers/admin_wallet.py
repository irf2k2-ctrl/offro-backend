from fastapi import APIRouter, HTTPException
from datetime import datetime
from database import db

router = APIRouter(
    prefix="/admin/wallet",
    tags=["Admin Wallet"]
)


# 🔹 1. View all withdraw requests
@router.get("/withdraw-requests")
def get_withdraw_requests():
    requests = list(db.withdraw_requests.find({}, {"_id": 0}))
    return {
        "total_requests": len(requests),
        "data": requests
    }


# 🔹 2. Approve withdraw (DEDUCT WALLET HERE)
@router.post("/approve")
def approve_withdraw(data: dict):
    user_id = data.get("user_id")

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    request = db.withdraw_requests.find_one({
        "user_id": user_id,
        "status": "pending"
    })

    if not request:
        raise HTTPException(status_code=404, detail="No pending request found")

    points = request["points"]

    # 🔒 Check wallet balance before deduct
    wallet = db.wallet.find_one({"user_id": user_id})

    if not wallet or wallet.get("points", 0) < points:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")

    # 🔥 Deduct wallet NOW
    db.wallet.update_one(
        {"user_id": user_id},
        {"$inc": {"points": -points}}
    )

    # 🔹 Update request status
    db.withdraw_requests.update_one(
        {"_id": request["_id"]},
        {
            "$set": {
                "status": "approved",
                "approved_at": datetime.utcnow()
            }
        }
    )

    return {"message": "Withdraw approved & wallet deducted"}


# 🔹 3. Reject withdraw (NO wallet change)
@router.post("/reject")
def reject_withdraw(data: dict):
    user_id = data.get("user_id")

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    request = db.withdraw_requests.find_one({
        "user_id": user_id,
        "status": "pending"
    })

    if not request:
        raise HTTPException(status_code=404, detail="No pending request found")

    db.withdraw_requests.update_one(
        {"_id": request["_id"]},
        {
            "$set": {
                "status": "rejected",
                "rejected_at": datetime.utcnow()
            }
        }
    )

    return {"message": "Withdraw rejected"}


# 🔹 4. Mark as paid
@router.post("/mark-paid")
def mark_paid(data: dict):
    user_id = data.get("user_id")

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    request = db.withdraw_requests.find_one({
        "user_id": user_id,
        "status": "approved"
    })

    if not request:
        raise HTTPException(status_code=404, detail="No approved request found")

    db.withdraw_requests.update_one(
        {"_id": request["_id"]},
        {
            "$set": {
                "status": "paid",
                "paid_at": datetime.utcnow()
            }
        }
    )

    return {"message": "Payment marked as completed"}