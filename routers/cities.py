from fastapi import APIRouter
from database import db

router = APIRouter(prefix="/cities", tags=["Cities"])


@router.get("/")
def get_cities():

    cities = list(db.cities.find())

    for city in cities:
        city["_id"] = str(city["_id"])

    return cities