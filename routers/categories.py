from fastapi import APIRouter, Depends
from database import db
from auth import get_current_admin

router = APIRouter()


# ---------------- ADMIN (PROTECTED)
@router.get("/admin/categories")
def get_categories():
    categories = list(db.categories.find())

    return [{
        "_id": str(c["_id"]),
        "name": c.get("name"),
        "status": c.get("status", "active")
    } for c in categories]


# ---------------- PUBLIC (NO AUTH) ✅
@router.get("/categories")
def get_public_categories():

    categories = list(db.categories.find({"status": "active"}))

    return [{
        "name": c.get("name")
    } for c in categories]