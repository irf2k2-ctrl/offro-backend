"""
UPDATED FILE: routers/gift_vouchers.py
Replaces old backend_gift_vouchers.py
Changes:
- Removed logo field; vouchers now use image2 from linked store
- Added store_id field to VoucherCreate
- Public endpoint enriches voucher with store's image2
- Admin endpoint same enrichment
"""
from fastapi import APIRouter, Depends, HTTPException
from database import db
from auth import get_admin
from pydantic import BaseModel
from typing import Optional
from bson import ObjectId
from datetime import datetime

router = APIRouter()

def _fmt(v):
    v = dict(v)
    v["id"] = str(v["_id"]); del v["_id"]
    return v

def _enrich_with_store_image(v):
    """Attach store's image2 to the voucher doc so Flutter can display it."""
    store_id = v.get("store_id", "")
    if store_id:
        try:
            store = db.stores.find_one({"_id": ObjectId(store_id)}, {"image2": 1, "image": 1})
            if store:
                v["image2"] = store.get("image2") or store.get("image") or ""
        except Exception:
            pass
    return v

class VoucherCreate(BaseModel):
    title: str
    text: str
    store_id: Optional[str] = ""        # linked store (provides image2)
    validity: Optional[str] = ""
    is_active: Optional[bool] = True

@router.get("/gift-vouchers")
async def list_vouchers():
    """Public endpoint — returns active vouchers with store image2 for home screen."""
    vouchers = list(db.gift_vouchers.find({"is_active": True}))
    result = []
    for v in vouchers:
        v = _fmt(v)
        v = _enrich_with_store_image(v)
        result.append(v)
    return result

@router.get("/admin/gift-vouchers")
async def admin_list_vouchers(admin=Depends(get_admin)):
    vouchers = list(db.gift_vouchers.find())
    result = []
    for v in vouchers:
        v = _fmt(v)
        v = _enrich_with_store_image(v)
        result.append(v)
    return result

@router.post("/admin/gift-vouchers")
async def create_voucher(data: VoucherCreate, admin=Depends(get_admin)):
    doc = {
        **data.dict(),
        "created_at": datetime.utcnow().isoformat()
    }
    res = db.gift_vouchers.insert_one(doc)
    return {"id": str(res.inserted_id), "message": "Voucher created"}

@router.put("/admin/gift-vouchers/{vid}")
async def update_voucher(vid: str, data: dict, admin=Depends(get_admin)):
    data.pop("id", None); data.pop("_id", None)
    db.gift_vouchers.update_one({"_id": ObjectId(vid)}, {"$set": data})
    return {"message": "Updated"}

@router.delete("/admin/gift-vouchers/{vid}")
async def delete_voucher(vid: str, admin=Depends(get_admin)):
    db.gift_vouchers.delete_one({"_id": ObjectId(vid)})
    return {"message": "Deleted"}
