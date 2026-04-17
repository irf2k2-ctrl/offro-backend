from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timedelta
from database import db
from utils.security import verify_qr_token, get_current_merchant

router = APIRouter(
    prefix="/rewards",
    tags=["Rewards"]
)


@router.post("/scan")
def scan_reward(
    data: dict,
    merchant_id: str = Depends(get_current_merchant)
):
    # 🔹 Get data
    token = data.get("qr_token")
    store_id = data.get("store_id")

    if not token or not store_id:
        raise HTTPException(status_code=400, detail="Missing required fields")

    # 🔐 Verify QR
    user_id = verify_qr_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired QR")

    # 🔹 Get store
    store = db.stores.find_one({"store_id": store_id})
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    reward_points = store.get("reward_points", 0)
    pool_points = store.get("pool_points", 0)

    if reward_points <= 0:
        raise HTTPException(status_code=400, detail="Rewards not configured")

    if pool_points < reward_points:
        raise HTTPException(status_code=400, detail="Reward pool exhausted")

    # ✅ Use datetime (MongoDB compatible)
    now = datetime.utcnow()

    # 🔒 Daily limit (start + end of day)
    start_day = datetime(now.year, now.month, now.day)
    end_day = start_day + timedelta(days=1)

    existing = db.reward_transactions.find_one({
        "user_id": user_id,
        "store_id": store_id,
        "created_at": {
            "$gte": start_day,
            "$lt": end_day
        }
    })

    if existing:
        raise HTTPException(status_code=400, detail="Already rewarded today")

    # 🔹 Wallet update (safe)
    wallet = db.wallet.find_one({"user_id": user_id})

    if wallet:
        new_points = wallet.get("points", 0) + reward_points
        db.wallet.update_one(
            {"user_id": user_id},
            {"$set": {"points": new_points}}
        )
    else:
        new_points = reward_points
        db.wallet.insert_one({
            "user_id": user_id,
            "points": reward_points
        })

    # 🔹 Deduct store pool (FIXED: use store_id)
    db.stores.update_one(
        {"store_id": store_id},
        {"$inc": {"pool_points": -reward_points}}
    )

    # 🔹 Save transaction (NO date.today())
    db.reward_transactions.insert_one({
        "user_id": user_id,
        "store_id": store_id,
        "merchant_id": merchant_id,
        "points": reward_points,
        "created_at": now
    })

    return {
        "message": "Reward credited successfully",
        "points_earned": reward_points,
        "wallet_total": new_points
    }