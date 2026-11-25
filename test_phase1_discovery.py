"""NOTE: file name sorts AFTER test_banner_media.py on purpose (it swaps the shared fake `database` at import).
Phase 1 / Task 2: country-isolated discovery - the optional `country` parameter of /stores, /influencers, /deals/all, /products-public.
Run:  python -m pytest test_phase1_discovery.py -q -W ignore"""
import sys
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import server
from services import products as P

fake = sys.modules["database"].db
c = TestClient(server.app)


@pytest.fixture(autouse=True)
def _world():
    for col in ("accounts", "stores", "merchant_products", "deals", "subscriptions", "influencers"):
        fake[col].delete_many({})
    fake.accounts.insert_one({"merchant_id": "m_in", "name": "Indian", "phone": "9876543210", "roles": ["merchant"]})
    fake.accounts.insert_one({"merchant_id": "m_sa", "name": "Saudi", "phone": "+966501234567", "phone_country": "SA", "roles": ["merchant"]})
    fake.accounts.insert_one({"merchant_id": "m_legacy", "name": "Legacy", "phone": "garbled", "roles": ["merchant"]})
    yield


def _store(mid, city, name):
    sid = str(fake.stores.insert_one({"merchant_id": mid, "store_name": name, "city": city, "status": "active"}).inserted_id)
    fake.subscriptions.insert_one({"store_id": sid, "status": "active", "end_date": None})
    return sid


def _deal(sid, title):
    fake.deals.insert_one({"store_id": sid, "title": title, "status": "active", "discount": "10"})


def _product(mid, sid, city, title):
    fake.merchant_products.insert_one({"merchant_id": mid, "store_id": sid, "city": city, "title": title, "price": "10", "status": "approved",
                                       "is_active": True, "start_date": P.ist_today(), "end_date": P.ist_today() + timedelta(days=20),
                                       "description": "d", "image_url": "u"})


def _names(rows, key="store_name"):
    return sorted(r.get(key) or r.get("name") or r.get("title") for r in rows)


def _two_countries():
    """Same city name 'Springfield' in India and Saudi Arabia."""
    s_in = _store("m_in", "Springfield", "IN shop")
    s_sa = _store("m_sa", "Springfield", "SA shop")
    s_lg = _store("m_legacy", "Springfield", "Legacy shop")
    return s_in, s_sa, s_lg


def test_stores_without_country_are_unfiltered_as_before():
    _two_countries()
    assert _names(c.get("/stores?city=Springfield").json()) == ["IN shop", "Legacy shop", "SA shop"]


def test_stores_country_plus_city_never_mix():
    _two_countries()
    assert _names(c.get("/stores?city=Springfield&country=SA").json()) == ["SA shop"]
    assert _names(c.get("/stores?city=Springfield&country=IN").json()) == ["IN shop", "Legacy shop"]   # legacy owner = India
    assert _names(c.get("/stores?country=sa").json()) == ["SA shop"]                                    # code is case-insensitive


def test_stores_unsupported_country_is_empty_never_everything():
    _two_countries()
    assert c.get("/stores?country=ZZ").json() == []
    assert c.get("/stores?country=Saudi").json() == []


def test_deals_country_filter_and_city_regex_is_escaped():
    s_in, s_sa, _ = _two_countries()
    _deal(s_in, "IN deal")
    _deal(s_sa, "SA deal")
    assert _names(c.get("/deals/all?city=Springfield").json(), "title") == ["IN deal", "SA deal"]
    assert _names(c.get("/deals/all?city=Springfield&country=SA").json(), "title") == ["SA deal"]
    assert _names(c.get("/deals/all?country=IN").json(), "title") == ["IN deal"]
    assert c.get("/deals/all?country=ZZ").json() == []
    r = c.get("/deals/all", params={"city": "Spring(field"})        # unescaped, this raised a regex error
    assert r.status_code == 200 and r.json() == []


def test_products_country_filter():
    s_in, s_sa, _ = _two_countries()
    _product("m_in", s_in, "Springfield", "IN item")
    _product("m_sa", s_sa, "Springfield", "SA item")
    assert _names(c.get("/products-public?city=Springfield").json(), "title") == ["IN item", "SA item"]
    assert _names(c.get("/products-public?city=Springfield&country=SA").json(), "title") == ["SA item"]
    assert _names(c.get("/products-public?country=IN").json(), "title") == ["IN item"]
    assert c.get("/products-public?country=ZZ").json() == []


def test_influencers_country_filter_uses_own_country_then_account_then_phone():
    fake.accounts.insert_one({"_id": __import__("bson").ObjectId(), "name": "inf acct", "phone": "+966500000001", "phone_country": "SA"})
    acct = fake.accounts.find_one({"phone": "+966500000001"})
    base = {"status": "active", "city": "Springfield", "categories": [], "social": {}}
    fake.influencers.insert_one({**base, "name": "By field", "country": "SA", "phone": "9876543210"})
    fake.influencers.insert_one({**base, "name": "By account", "account_id": str(acct["_id"])})
    fake.influencers.insert_one({**base, "name": "By phone", "phone": "+966500000009"})
    fake.influencers.insert_one({**base, "name": "Legacy india", "phone": "9000000000"})
    fake.influencers.insert_one({**base, "name": "No data"})
    names = lambda q: sorted(r["name"] for r in c.get("/influencers" + q).json())
    assert names("?city=Springfield") == sorted(["By field", "By account", "By phone", "Legacy india", "No data"])
    assert names("?city=Springfield&country=SA") == ["By account", "By field", "By phone"]
    assert names("?city=Springfield&country=IN") == ["Legacy india", "No data"]
    assert c.get("/influencers?country=ZZ").json() == []
