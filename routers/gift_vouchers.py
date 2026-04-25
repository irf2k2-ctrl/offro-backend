"""
NEW FILE: routers/gift_vouchers.py
Add to server.py: from routers.gift_vouchers import router as vouchers_router
                  app.include_router(vouchers_router, prefix="")
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
    v["id"] = str(v["_id"]); del v["_id"]
    return v

class VoucherCreate(BaseModel):
    title: str
    text: str
    logo: Optional[str] = ""
    validity: Optional[str] = ""
    merchant_id: Optional[str] = ""
    is_active: Optional[bool] = True

@router.get("/gift-vouchers")
async def list_vouchers():
    """Public endpoint — returns active vouchers for home screen carousel"""
    vouchers = list(db.gift_vouchers.find({"is_active": True}))
    return [_fmt(v) for v in vouchers]

@router.get("/admin/gift-vouchers")
async def admin_list_vouchers(admin=Depends(get_admin)):
    vouchers = list(db.gift_vouchers.find())
    return [_fmt(v) for v in vouchers]

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
