"""Phase 5 / Issues 3+5 - category names are translated server-side (admin names, built-in glossary, raw fallback)."""
import sys
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import public as PUB
from services import category_names as CN

DB = sys.modules["database"].db
app = FastAPI()
app.include_router(PUB.router)
web = TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    DB.categories.delete_many({})
    yield


def _by_name(lang=""):
    r = web.get("/categories", params={"lang": lang} if lang else {})
    assert r.status_code == 200
    return {c["name"]: c for c in r.json()}


def test_builtin_glossary_translates_existing_categories_without_admin_work():
    for n in ("Electronics", "Salon", "Other", "Restaurant"):
        DB.categories.insert_one({"name": n, "status": "active", "sort_order": 1})
    got = _by_name("ar")
    assert got["Electronics"]["display_name"] == "إلكترونيات" and got["Salon"]["display_name"] == "صالون"
    assert got["Other"]["display_name"] == "أخرى"
    assert got["Electronics"]["name"] == "Electronics"                    # the stored/API value never changes
    assert got["Electronics"]["names"]["ar"] == "إلكترونيات"


def test_english_stays_english_and_no_lang_is_unchanged():
    DB.categories.insert_one({"name": "Electronics", "status": "active"})
    assert _by_name("en")["Electronics"]["display_name"] == "Electronics"
    assert _by_name()["Electronics"]["display_name"] == "Electronics"


def test_new_category_uses_admin_translation_and_admin_beats_glossary():
    DB.categories.insert_one({"name": "Pet Care", "status": "active", "names": {"ar": "رعاية الحيوانات"}})
    DB.categories.insert_one({"name": "Bakery", "status": "active", "names": {"ar": "مخبوزات"}})
    got = _by_name("ar")
    assert got["Pet Care"]["display_name"] == "رعاية الحيوانات"
    assert got["Bakery"]["display_name"] == "مخبوزات"


def test_unknown_category_without_translation_shows_as_typed():
    DB.categories.insert_one({"name": "Zorb Rentals", "status": "active"})
    assert _by_name("ar")["Zorb Rentals"]["display_name"] == "Zorb Rentals"


def test_clean_rejects_bad_names():
    with pytest.raises(ValueError):
        CN.clean({"ar": 5})
    assert CN.clean({"AR": " مطعم  "}) == {"ar": "مطعم"}
