"""City Master: country filtering on the server + display names per app language (additive; old behaviour unchanged).
(File name sorts after test_banner_media.py on purpose.)  Run:  python -m pytest test_cities_country_language.py -q -W ignore"""
import sys
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import admin as ADM
from routers import admin_cities as ADMC
from routers import public as PUB
from services import cities as C

DB = sys.modules["database"].db
app = FastAPI()
app.include_router(ADMC.router, prefix="/admin")   # city routes moved to routers/admin_cities.py (registered before admin, as in server.py)
app.include_router(ADM.router, prefix="/admin")
app.include_router(PUB.router)
web = TestClient(app)


def user(token, role_name="Custom", perms=None, system=False):
    rid = DB.dashboard_roles.insert_one({"role_name": role_name, "permissions": perms or {}, "is_system_role": system}).inserted_id
    DB.dashboard_users.insert_one({"token": token, "role_id": rid, "full_name": token, "status": "active", "mobile": "9000000001",
                                   "last_active_at": datetime.utcnow()})
    return {"Authorization": "Bearer " + token}


def sa():
    return user("sa-token", "Super Admin", system=True)


def city(name, country=None, names=None, order=0, **kw):
    d = {"name": name, "sort_order": order, "active": True, "status": "active", "image_url": ""}
    if country is not None:
        d["country"] = country
    if names is not None:
        d["names"] = names
    d.update(kw)
    return str(DB.cities.insert_one(d).inserted_id)


@pytest.fixture(autouse=True)
def world():
    DB.cities.delete_many({}); DB.dashboard_users.delete_many({}); DB.dashboard_roles.delete_many({})
    ids = {
        "blr": city("Bengaluru", "IN", {"kn": "ಬೆಂಗಳೂರು", "ar": "بنغالور"}, 1),
        "mys": city("Mysuru", "IN", None, 2),
        "ruh": city("Riyadh", "SA", {"ar": "الرياض"}, 3),
        "jed": city("Jeddah", "SA", {"ar": "جدة", "en": "Jeddah"}, 4),
        "old": city("Ballari", None, None, 5),                         # a legacy city: no country yet
        "off": city("Dammam", "SA", {"ar": "الدمام"}, 6, status="inactive", active=False),
    }
    yield ids


def names(rows, key="name"):
    return [r[key] for r in rows]


# ── backward compatibility ──────────────────────────────────────────────────────────────────────────────
def test_without_parameters_the_list_is_what_it_always_was_plus_additive_fields():
    r = web.get("/cities").json()
    assert names(r) == ["Bengaluru", "Mysuru", "Riyadh", "Jeddah", "Ballari"]                 # all active, same order, inactive left out
    assert all({"name", "slug", "image_url", "sort_order"} <= set(x) for x in r)               # the keys older apps read are still there
    assert all(x["display_name"] == x["name"] for x in r)                                      # no lang requested -> English


def test_blank_country_means_no_filter_like_an_older_app():
    assert names(web.get("/cities?country=").json()) == names(web.get("/cities").json())


# ── country filtering (server side) ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("iso,expected", [("IN", ["Bengaluru", "Mysuru"]), ("SA", ["Riyadh", "Jeddah"]), ("sa", ["Riyadh", "Jeddah"]), (" in ", ["Bengaluru", "Mysuru"])])
def test_a_country_gets_only_its_own_active_cities(iso, expected):
    assert names(web.get("/cities", params={"country": iso}).json()) == expected


def test_no_other_country_and_no_unassigned_city_leaks_into_a_country_request():
    for iso in ("IN", "SA"):
        got = names(web.get("/cities", params={"country": iso}).json())
        assert "Ballari" not in got and "Dammam" not in got
    assert not set(names(web.get("/cities?country=IN").json())) & set(names(web.get("/cities?country=SA").json()))


@pytest.mark.parametrize("bad", ["XX", "ZZZ", "India", "9", "'; drop", "JO"])
def test_an_unknown_or_unused_country_returns_nothing_never_another_countrys_cities(bad):
    assert web.get("/cities", params={"country": bad}).json() == []                           # JO is supported but has no city yet


def test_country_request_never_falls_back_to_store_cities():
    DB.cities.delete_many({})
    DB.stores.insert_one({"city": "Pune", "status": "active"})
    assert web.get("/cities?country=IN").json() == []
    assert names(web.get("/cities").json()) == ["Pune"]                                        # the legacy fallback is untouched


def test_country_filter_applies_to_customer_merchant_and_influencer_accounts_alike():
    # The country is the account's registered phone country; the role of the account never enters the request.
    from utils import countries as UC
    for phone, role, iso in [("+919876543210", "customer", "IN"), ("+966512345678", "merchant", "SA"), ("+919812345678", "influencer", "IN")]:
        assert UC.account_country({"phone": phone, "roles": [role]})["iso"] == iso
        assert set(names(web.get("/cities", params={"country": iso}).json())) == {c for c in
            {"IN": ["Bengaluru", "Mysuru"], "SA": ["Riyadh", "Jeddah"]}[iso]}


def test_changing_the_account_country_changes_the_list_on_the_next_request():
    assert names(web.get("/cities?country=IN").json())[0] == "Bengaluru"
    assert names(web.get("/cities?country=SA").json())[0] == "Riyadh"


# ── names per language ─────────────────────────────────────────────────────────────────────────────────────
def test_names_follow_the_selected_language_with_english_fallback():
    ar = {x["name"]: x["display_name"] for x in web.get("/cities?country=SA&lang=ar").json()}
    assert ar == {"Riyadh": "الرياض", "Jeddah": "جدة"}
    kn = {x["name"]: x["display_name"] for x in web.get("/cities?country=IN&lang=kn").json()}
    assert kn == {"Bengaluru": "ಬೆಂಗಳೂರು", "Mysuru": "Mysuru"}                                   # Mysuru has no Kannada name -> English
    hi = {x["name"]: x["display_name"] for x in web.get("/cities?country=IN&lang=hi").json()}
    assert hi == {"Bengaluru": "Bengaluru", "Mysuru": "Mysuru"}                               # a language with no translations -> English


def test_a_regional_language_code_uses_its_base_language_and_garbage_falls_back():
    assert {x["name"]: x["display_name"] for x in web.get("/cities?country=SA&lang=ar-SA").json()}["Riyadh"] == "الرياض"
    assert {x["name"]: x["display_name"] for x in web.get("/cities?country=SA&lang=AR_sa").json()}["Riyadh"] == "الرياض"
    assert {x["name"]: x["display_name"] for x in web.get("/cities?country=SA&lang=%3Cx%3E").json()}["Riyadh"] == "Riyadh"


def test_blank_translation_falls_back_to_english():
    DB.cities.update_one({"name": "Riyadh"}, {"$set": {"names": {"ar": "   "}}})
    assert {x["name"]: x["display_name"] for x in web.get("/cities?country=SA&lang=ar").json()}["Riyadh"] == "Riyadh"


def test_translating_never_changes_the_id_the_english_name_or_the_country(world):
    before = {x["name"]: (x["id"], x["country"]) for x in web.get("/cities").json()}
    web.put("/admin/cities/" + world["mys"], json={"names": {"kn": "ಮೈಸೂರು", "ar": "ميسور"}}, headers=sa())
    after = {x["name"]: (x["id"], x["country"]) for x in web.get("/cities").json()}
    assert after == before
    assert DB.cities.count_documents({}) == 6                                                   # no duplicate record per language
    assert {x["name"]: x["display_name"] for x in web.get("/cities?country=IN&lang=kn").json()}["Mysuru"] == "ಮೈಸೂರು"


def test_selection_uses_the_canonical_name_and_id_whatever_the_display_language():
    for lang in ("", "ar", "kn"):
        row = next(x for x in web.get("/cities", params={"country": "SA", "lang": lang}).json() if x["name"] == "Riyadh")
        assert row["name"] == "Riyadh" and row["id"] == str(DB.cities.find_one({"name": "Riyadh"})["_id"])


# ── helpers ────────────────────────────────────────────────────────────────────────────────────────────────
def test_clean_names_validation():
    assert C.clean_names({"AR": " الرياض ", "kn": "", "hi": None}) == {"ar": "الرياض"}
    assert C.clean_names(None) == {} and C.clean_names("") == {}
    for bad in ["x", [], {"a": "x"}, {"ar1234": "x"}, {"ar": 5}, {"ar": "x" * 81}, {"<b>": "x"}, {str(i) + "a": "x" for i in range(60)}]:
        with pytest.raises(ValueError):
            C.clean_names(bad)


def test_normalize_country_uses_the_one_country_master():
    assert C.normalize_country("sa") == "SA" and C.normalize_country("IN") == "IN"
    assert C.normalize_country("XX") is None and C.normalize_country("") is None and C.normalize_country(None) is None and C.normalize_country("966") is None


# ── admin API ──────────────────────────────────────────────────────────────────────────────────────────────
def test_admin_can_create_a_city_with_country_and_names_and_it_appears_in_that_country_only():
    h = sa()
    r = web.post("/admin/cities", json={"name": "Madinah", "country": "sa", "names": {"ar": "المدينة"}}, headers=h)
    assert r.status_code == 200
    assert "Madinah" in names(web.get("/cities?country=SA").json()) and "Madinah" not in names(web.get("/cities?country=IN").json())
    listed = next(x for x in web.get("/admin/cities", headers=h).json() if x["name"] == "Madinah")
    assert listed["country"] == "SA" and listed["names"] == {"ar": "المدينة"}


def test_admin_rejects_an_unsupported_country_or_bad_names():
    h = sa()
    assert web.post("/admin/cities", json={"name": "Nowhere", "country": "XX"}, headers=h).status_code == 400
    assert web.post("/admin/cities", json={"name": "Nowhere", "names": {"ar": 5}}, headers=h).status_code == 400
    assert DB.cities.count_documents({"name": "Nowhere"}) == 0
    cid = str(DB.cities.find_one({"name": "Mysuru"})["_id"])
    assert web.put("/admin/cities/" + cid, json={"country": "XX"}, headers=h).status_code == 400
    assert web.put("/admin/cities/" + cid, json={"names": "x"}, headers=h).status_code == 400
    assert DB.cities.find_one({"name": "Mysuru"}).get("country") == "IN"


def test_admin_update_can_change_and_clear_the_country():
    h = sa(); cid = str(DB.cities.find_one({"name": "Mysuru"})["_id"])
    assert web.put("/admin/cities/" + cid, json={"country": ""}, headers=h).status_code == 200
    assert "country" not in DB.cities.find_one({"name": "Mysuru"}) and "Mysuru" not in names(web.get("/cities?country=IN").json())
    web.put("/admin/cities/" + cid, json={"country": "in"}, headers=h)
    assert DB.cities.find_one({"name": "Mysuru"})["country"] == "IN"


def test_updating_other_fields_still_works_and_leaves_country_and_names_alone():
    h = sa(); cid = str(DB.cities.find_one({"name": "Bengaluru"})["_id"])
    assert web.put("/admin/cities/" + cid, json={"sort_order": 9}, headers=h).status_code == 200
    d = DB.cities.find_one({"name": "Bengaluru"})
    assert d["sort_order"] == 9 and d["country"] == "IN" and d["names"]["kn"] == "ಬೆಂಗಳೂರು"


def test_assign_country_only_touches_cities_that_have_none():
    h = sa()
    r = web.post("/admin/cities/assign-country", json={"country": "IN"}, headers=h)
    assert r.status_code == 200 and r.json()["updated"] == 1
    assert DB.cities.find_one({"name": "Ballari"})["country"] == "IN"
    assert DB.cities.find_one({"name": "Riyadh"})["country"] == "SA"                            # never reassigned
    assert "Ballari" in names(web.get("/cities?country=IN").json())
    assert web.post("/admin/cities/assign-country", json={"country": "IN"}, headers=h).json()["updated"] == 0
    assert web.post("/admin/cities/assign-country", json={"country": "ZZ"}, headers=h).status_code == 400
    assert web.post("/admin/cities/assign-country", json={}, headers=h).status_code == 400


def test_admin_city_endpoints_still_need_login_and_the_cities_permission():
    assert web.post("/admin/cities/assign-country", json={"country": "IN"}).status_code in (401, 403)
    h = user("vw", "Viewer", {"Cities": {"view": True}})
    assert web.post("/admin/cities/assign-country", json={"country": "IN"}, headers=h).status_code == 403
    assert web.post("/admin/cities", json={"name": "X1"}, headers=h).status_code == 403
    assert DB.cities.find_one({"name": "Ballari"}).get("country") is None


def test_existing_admin_list_keeps_its_old_fields():
    r = web.get("/admin/cities", headers=sa()).json()
    assert all({"id", "name", "image_url", "sort_order", "active", "created_at"} <= set(x) for x in r) and len(r) == 6
