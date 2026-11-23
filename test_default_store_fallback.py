"""Default Store Image fallback (backend): the store images the app rotates through, country mapping, and the city-status check that tells
 'a city we serve that has no stores' (-> Default Store Image) apart from 'outside the service area' (-> No Service Area image)."""
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import public as PUB
from services import default_images as DI

DB = sys.modules["database"].db
app = FastAPI()
app.include_router(PUB.router)
web = TestClient(app)


def img(n):
    return f"https://cdn.example/{n}.jpg"


@pytest.fixture(autouse=True)
def _clean():
    for col in ("settings", "cities", "stores", "subscriptions", DI.MAPPINGS, DI.VARIANTS):
        DB[col].delete_many({})
    yield


def _seed(**kw):
    DB.settings.insert_one({"_type": "default_images", **kw})


def _public(country=None):
    r = web.get("/default-images", params={"country": country} if country is not None else None)
    assert r.status_code == 200, r.text
    return r.json()


def test_store_images_lists_every_configured_image_and_store_stays_the_first():
    _seed(store=[img("a"), img("b"), img("c")], no_service_url=img("ns"))
    d = _public()
    assert d["store_images"] == [img("a"), img("b"), img("c")] and d["store"] == img("a")       # `store` unchanged for older apps
    assert d["no_service_url"] == img("ns")


def test_store_images_follow_the_country_mapping():
    _seed(store=[img("a"), img("b"), img("c")])
    DB[DI.MAPPINGS].insert_one({"category": "store", "url_key": DI.url_key(img("c")), "countries": ["SA"]})
    assert _public("SA")["store_images"] == [img("c")] and _public("SA")["store"] == img("c")          # mapped to SA: only that
    assert _public("IN")["store_images"] == [img("a"), img("b")]                                       # SA's image never reaches India
    assert _public()["store_images"] == [img("a"), img("b")]                                           # unknown country: unmapped only


def test_store_images_ignore_junk_and_video():
    _seed(store=["", "junk", "data:video/mp4;base64,AAAA", img("a"), "data:image/png;base64,AAAA"])
    assert _public()["store_images"] == [img("a"), "data:image/png;base64,AAAA"]


def test_store_images_empty_when_not_configured():
    assert _public()["store_images"] == []
    _seed(store=[])
    assert _public()["store_images"] == [] and _public()["store"] == ""


def _city(name, country, status="active", **kw):
    DB.cities.insert_one({"name": name, "slug": name.lower(), "country": country, "status": status, **kw})


def _status(city, country=None):
    p = {"city": city}
    if country is not None:
        p["country"] = country
    r = web.get("/city-status", params=p)
    assert r.status_code == 200, r.text
    return r.json()["supported"]


def test_city_status_supported_city_even_with_no_stores():
    _city("Pune", "IN")
    assert DB.stores.count_documents({}) == 0 and web.get("/stores", params={"city": "Pune"}).json() == []   # no stores ...
    assert _status("Pune", "IN") is True                                                                     # ... but inside the service area
    assert _status("pune", "IN") is True and _status("  PUNE ", "IN") is True


def test_city_status_outside_the_service_area():
    _city("Pune", "IN")
    assert _status("Atlantis", "IN") is False
    assert _status("", "IN") is False


def test_city_status_is_per_country_and_ignores_inactive_cities():
    _city("Pune", "IN")
    _city("Riyadh", "SA")
    _city("Jeddah", "SA", status="inactive")
    assert _status("Riyadh", "SA") is True and _status("Riyadh", "IN") is False                              # another country's city is not ours
    assert _status("Jeddah", "SA") is False                                                                  # inactive city
    assert _status("Riyadh") is True                                                                         # no country given: any active city
    assert _status("Riyadh", "XX") is False                                                                  # not a supported country


def test_city_status_matches_translations_and_gps_style_names():
    _city("Riyadh", "SA", names={"ar": "الرياض"})
    assert _status("الرياض", "SA") is True
    assert _status("Riyadh Province", "SA") is True                                                          # whole word inside a GPS value
    assert _status("Riyadhville", "SA") is False                                                             # not a whole word


def test_city_status_without_a_city_master_uses_active_store_cities():
    DB.stores.insert_one({"store_name": "s", "city": "Mysore", "status": "active"})
    assert _status("Mysore") is True and _status("Nowhere") is False
