from database import db
from bson import ObjectId

# FIX STORES
stores = list(db.stores.find())
for s in stores:
    if isinstance(s.get("merchant_id"), ObjectId):
        db.stores.update_one(
            {"_id": s["_id"]},
            {"$set": {"merchant_id": str(s["merchant_id"])}}
        )

# FIX DEALS
deals = list(db.deals.find())
for d in deals:
    updates = {}

    if isinstance(d.get("merchant_id"), ObjectId):
        updates["merchant_id"] = str(d["merchant_id"])

    if isinstance(d.get("store_id"), ObjectId):
        updates["store_id"] = str(d["store_id"])

    if updates:
        db.deals.update_one(
            {"_id": d["_id"]},
            {"$set": updates}
        )

# FIX REDEMPTIONS
reds = list(db.redemptions.find())
for r in reds:
    updates = {}

    if isinstance(r.get("merchant_id"), ObjectId):
        updates["merchant_id"] = str(r["merchant_id"])

    if isinstance(r.get("store_id"), ObjectId):
        updates["store_id"] = str(r["store_id"])

    if updates:
        db.redemptions.update_one(
            {"_id": r["_id"]},
            {"$set": updates}
        )

print("✅ DB FIXED: All IDs converted to STRING")