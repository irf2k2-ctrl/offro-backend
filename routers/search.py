from fastapi import APIRouter
from database import db

router = APIRouter(prefix="/search", tags=["Search"])


@router.get("/")
def search_store(query: str):

    stores = list(
        db.stores.find(
            {"name": {"$regex": query, "$options": "i"}}
        )
    )

    for store in stores:
        store["_id"] = str(store["_id"])

    return stores