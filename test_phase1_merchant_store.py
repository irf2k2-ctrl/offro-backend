"""NOTE: file name sorts AFTER test_banner_media.py on purpose (it swaps the shared fake `database` at import).
Phase 1 / Task 4: merchant Add/Edit Store - the store's city must belong to the merchant ACCOUNT's country (City Master) and the store phone
follows that country's rules. Run:  python -m pytest test_phase1_merchant_store.py -q -W ignore"""
import sys

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import server
import routers.merchant_app as MA

MA._cloudinary_upload = lambda v, folder="": "http://img/x.png" if v else ""
MA._make_thumb_url = lambda u: u
fake = sys.modules["database"].db
c = TestClient(server.app)

SA_H = {"Authorization": "Bearer sa_m"}
IN_H = {"Authorization": "Bearer in_m"}
JO_H = {"Authorization": "Bearer jo_m"}


def _city(name, country, state=""):
    fake.cities.insert_one({"name": name, "name_key": name.lower(), "country": country, "state": state, "status": "active", "active": True})


@pytest.fixture(autouse=True)
def _world():
    for col in ("accounts", "stores", "cities"):
        fake[col].delete_many({})
    fake.accounts.insert_one({"token": "sa_m", "merchant_id": "m_sa", "name": "Saudi", "phone": "+966501234567", "phone_country": "SA", "roles": ["merchant"]})
    fake.accounts.insert_one({"token": "in_m", "merchant_id": "m_in", "name": "Indian", "phone": "9876543210", "roles": ["merchant"]})
    fake.accounts.insert_one({"token": "jo_m", "merchant_id": "m_jo", "name": "Jordan", "phone": "+962791234567", "phone_country": "JO", "roles": ["merchant"]})
    for n, st in (("Mumbai", "Maharashtra"), ("Pune", "Maharashtra")):
        _city(n, "IN", st)
    for n in ("Riyadh", "Jeddah"):
        _city(n, "SA")
    _city("Amman", "JO")
    yield


def _body(**kw):
    d = {"store_name": "Shop", "category": "Grocery", "state": "State", "city": "Riyadh", "lat": 24.7, "lng": 46.7, "phone": "501234567"}
    d.update(kw)
    return d


def _create(h, **kw):
    return c.post("/merchant/stores", json=_body(**kw), headers=h)


# ───────────── Saudi merchant (account country SA) ─────────────
def test_saudi_merchant_creates_store_in_a_saudi_city_with_a_saudi_phone():
    r = _create(SA_H, country="SA", city="Riyadh", phone="0501234567")
    assert r.status_code == 200, r.text
    d = fake.stores.find_one({"_id": ObjectId(r.json()["store_id"])})
    assert d["city"] == "Riyadh" and d["country"] == "SA" and d["phone"] == "+966501234567"


def test_saudi_merchant_cannot_save_an_indian_city():
    for kw in ({"country": "SA"}, {}):                       # new build and an older build that sends no country
        r = _create(SA_H, city="Mumbai", **kw)
        assert r.status_code == 400 and "Mumbai" in r.json()["detail"] and "India" in r.json()["detail"]
    assert fake.stores.count_documents({}) == 0


def test_city_unknown_to_the_master_is_refused_only_for_builds_that_send_country():
    r = _create(SA_H, country="SA", city="Al Khobar")
    assert r.status_code == 400 and "City Master" in r.json()["detail"]
    assert _create(SA_H, city="Al Khobar").status_code == 200          # older build: only a clearly cross-country city is refused
    assert fake.stores.count_documents({}) == 1


def test_country_in_the_request_must_be_the_accounts():
    r = _create(SA_H, country="IN", city="Mumbai")
    assert r.status_code == 400 and "account" in r.json()["detail"].lower()
    assert _create(SA_H, country="JO", city="Amman").status_code == 400
    assert _create(SA_H, country="ZZ", city="Riyadh").status_code == 400
    assert fake.stores.count_documents({}) == 0


def test_store_phone_follows_the_accounts_country():
    assert _create(SA_H, country="SA", phone="9876543210").status_code == 400           # an Indian number is not a Saudi mobile
    assert _create(SA_H, country="SA", phone="12345").status_code == 400
    assert _create(SA_H, country="SA", phone="+966501234567").status_code == 200
    assert _create(JO_H, country="JO", city="Amman", phone="0791234567").status_code == 200
    assert fake.stores.find_one({"city": "Amman"})["phone"] == "+962791234567"


# ───────────── India keeps today's experience ─────────────
def test_india_merchant_is_unchanged_free_text_cities_and_ten_digit_phone():
    r = _create(IN_H, city="Ballari", state="Karnataka", phone="9876543210")             # an older build: nothing sent but the old fields
    assert r.status_code == 200, r.text
    d = fake.stores.find_one({"_id": ObjectId(r.json()["store_id"])})
    assert d["phone"] == "9876543210" and "country" not in d
    r = _create(IN_H, country="IN", city="Ballari", state="Karnataka", phone="9876543210")  # a new build: India stays lenient on city and phone
    assert r.status_code == 200 and fake.stores.find_one({"_id": ObjectId(r.json()["store_id"])})["phone"] == "9876543210"
    assert _create(IN_H, country="IN", city="Pune", state="Maharashtra").status_code == 200


def test_india_merchant_cannot_save_a_saudi_city():
    r = _create(IN_H, city="Riyadh")
    assert r.status_code == 400 and "Saudi Arabia" in r.json()["detail"]
    assert _create(IN_H, country="IN", city="Jeddah").status_code == 400
    assert fake.stores.count_documents({}) == 0


def test_store_without_a_phone_still_defaults_to_the_account_phone():
    r = c.post("/merchant/stores", json={k: v for k, v in _body(country="SA").items() if k != "phone"}, headers=SA_H)
    assert r.status_code == 200
    assert fake.stores.find_one({"_id": ObjectId(r.json()["store_id"])})["phone"] == "+966501234567"


# ───────────── editing ─────────────
def _store(mid, **kw):
    d = {"merchant_id": mid, "store_name": "Old", "city": "Riyadh", "state": "S", "status": "draft", "phone": "+966501234567"}
    d.update(kw)
    return str(fake.stores.insert_one(d).inserted_id)


def test_edit_changing_the_city_is_validated_and_other_edits_never_block():
    sid = _store("m_sa")
    assert c.put("/merchant/stores/" + sid, json={"store_name": "New", "country": "SA", "city": "Mumbai"}, headers=SA_H).status_code == 400
    assert fake.stores.find_one({"_id": ObjectId(sid)})["city"] == "Riyadh"
    assert c.put("/merchant/stores/" + sid, json={"city": "Jeddah", "country": "SA"}, headers=SA_H).status_code == 200
    assert fake.stores.find_one({"_id": ObjectId(sid)})["city"] == "Jeddah"
    assert c.put("/merchant/stores/" + sid, json={"store_name": "Renamed"}, headers=SA_H).status_code == 200


def test_legacy_store_with_a_foreign_city_can_still_be_edited_when_the_city_is_unchanged():
    sid = _store("m_sa", city="Mumbai")                                                  # a bad legacy record: not touched, not blocked
    r = c.put("/merchant/stores/" + sid, json={"store_name": "Renamed", "city": "Mumbai", "country": "SA"}, headers=SA_H)
    assert r.status_code == 200
    assert fake.stores.find_one({"_id": ObjectId(sid)})["city"] == "Mumbai"
    assert c.put("/merchant/stores/" + sid, json={"city": "Pune", "country": "SA"}, headers=SA_H).status_code == 400   # changing it IS validated


def test_edit_phone_is_validated_for_the_country_and_india_is_untouched():
    sid = _store("m_sa")
    assert c.put("/merchant/stores/" + sid, json={"phone": "9876543210", "country": "SA"}, headers=SA_H).status_code == 400
    assert c.put("/merchant/stores/" + sid, json={"phone": "0509999999", "country": "SA"}, headers=SA_H).status_code == 200
    assert fake.stores.find_one({"_id": ObjectId(sid)})["phone"] == "+966509999999"
    sid_in = _store("m_in", city="Ballari", phone="9876543210")
    assert c.put("/merchant/stores/" + sid_in, json={"phone": "9123456780"}, headers=IN_H).status_code == 200
    assert fake.stores.find_one({"_id": ObjectId(sid_in)})["phone"] == "9123456780"


def test_a_merchant_cannot_edit_another_merchants_store():
    sid = _store("m_in", city="Pune")
    assert c.put("/merchant/stores/" + sid, json={"store_name": "X"}, headers=SA_H).status_code == 404


# ───────────── reverse geocode reports the place's country (so the app can refuse a location of another country) ─────────────
def test_reverse_geocode_adds_the_country_code(monkeypatch):
    import requests

    class _R:
        def json(self):
            return {"display_name": "Somewhere", "address": {"state": "Riyadh Province", "city": "Riyadh", "suburb": "Olaya", "country_code": "sa"}}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    r = c.get("/reverse-geocode", params={"lat": 24.7, "lng": 46.7})
    assert r.status_code == 200
    assert r.json() == {"state": "Riyadh Province", "city": "Riyadh", "area": "Olaya", "address": "Somewhere", "country": "SA"}


def test_reverse_geocode_without_a_country_code_gives_an_empty_country(monkeypatch):
    import requests

    class _R:
        def json(self):
            return {"display_name": "X", "address": {"city": "Town"}}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    assert c.get("/reverse-geocode", params={"lat": 1, "lng": 2}).json()["country"] == ""
