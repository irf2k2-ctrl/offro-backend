from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime
from database import db
from routers.dependencies import user_required

router = APIRouter(
    prefix="/wallet",
    tags=["Wallet"]
)


# 🔹 1. Get Wallet Balance (SECURE)
@router.get("/")
def get_wallet(user=Depends(user_required)):

    user_id = user["user_id"]

    wallet = db.wallet.find_one({"user_id": user_id})

    if not wallet:
        return {
            "user_id": user_id,
            "points": 0,
            "wallet_value": 0
        }

    points = wallet.get("points", 0)

    return {
        "user_id": user_id,
        "points": points,
        "wallet_value": points * 0.25
    }


# 🔹 2. Request Withdraw (SECURE - NO deduction)
@router.post("/withdraw")
def request_withdraw(user=Depends(user_required)):

    user_id = user["user_id"]

    wallet = db.wallet.find_one({"user_id": user_id})

    if not wallet or wallet.get("points", 0) < 100:
        raise HTTPException(
            status_code=400,
            detail="Minimum 100 points required"
        )

    points = wallet["points"]
    amount = points * 0.25

    # 🔹 Save withdraw request ONLY
    db.withdraw_requests.insert_one({
        "user_id": user_id,
        "points": points,
        "amount": amount,
        "status": "pending",
        "created_at": datetime.utcnow()
    })

    return {
        "message": "Withdraw request submitted",
        "points": points,
        "amount": amount
    }