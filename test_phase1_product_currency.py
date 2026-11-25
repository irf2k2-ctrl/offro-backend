"""NOTE: file name sorts AFTER test_banner_media.py on purpose (it swaps the shared fake `database` at import).
Phase 1 / Task 1: customer-facing product / store payloads carry the owner's country and DISPLAY currency (no conversion, stored prices untouched).
Run:  python -m pytest test_phase1_product_currency.py -q -W ignore"""
import sys
from datetime import timedelta

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import server
from services import products as P

fake = sys.modules["database"].db
c = TestClient(server.app)


@pytest.fixture(autouse=True)
def _world():
    for col in ("accounts", "stores", "merchant_products", "pricing_countries", "pricing", "deals", "subscriptions"):
        fake[col].delete_many({})
    fake.pricing.insert_one({"gst_percent": 18})
    fake.pricing_countries.insert_one({"code": "SA", "name": "Saudi Arabia", "currency_name": "Saudi Riyal", "currency_code": "SAR",
                                       "currency_symbol": "SAR", "tax_label": "VAT", "tax_percent": 15, "status": "active"})
    fake.pricing_countries.insert_one({"code": "JO", "name": "Jordan", "currency_name": "Jordanian Dinar", "currency_code": "JOD",
                                       "currency_symbol": "JOD", "tax_label": "GST", "tax_percent": 16, "status": "active"})
    fake.accounts.insert_one({"merchant_id": "m_in", "name": "Indian", "phone": "9876543210", "roles": ["merchant"]})
    fake.accounts.insert_one({"merchant_id": "m_sa", "name": "Saudi", "phone": "+966501234567", "phone_country": "SA", "roles": ["merchant"]})
    fake.accounts.insert_one({"merchant_id": "m_np", "name": "Nepal", "phone": "+9779812345678", "phone_country": "NP", "roles": ["merchant"]})
    fake.accounts.insert_one({"merchant_id": "m_legacy", "name": "Legacy", "phone": "garbled", "roles": ["merchant"]})
    yield


def _store(mid, city, name="Shop", **kw):
    d = {"merchant_id": mid, "store_name": name, "city": city, "status": "active"}
    d.update(kw)
    return str(fake.stores.insert_one(d).inserted_id)


def _product(mid, sid, city, title="P", price="499", **kw):
    d = {"merchant_id": mid, "store_id": sid, "city": city, "title": title, "price": price, "original_price": "699", "status": "approved",
         "is_active": True, "start_date": P.ist_today(), "end_date": P.ist_today() + timedelta(days=20), "description": "d", "image_url": "u"}
    d.update(kw)
    return str(fake.merchant_products.insert_one(d).inserted_id)


def _by_title(rows):
    return {r["title"]: r for r in rows}


def test_discover_products_carry_each_owners_currency():
    s1 = _store("m_in", "Mumbai")
    s2 = _store("m_sa", "Riyadh")
    _product("m_in", s1, "Mumbai", "IN item", "499")
    _product("m_sa", s2, "Riyadh", "SA item", "115")
    rows = _by_title(c.get("/products-public").json())
    assert rows["IN item"]["currency_code"] == "INR" and rows["IN item"]["currency_symbol"] == "₹" and rows["IN item"]["country"] == "IN"
    assert rows["SA item"]["currency_code"] == "SAR" and rows["SA item"]["currency_symbol"] == "SAR" and rows["SA item"]["country"] == "SA"


def test_stored_prices_are_never_changed_or_converted():
    s2 = _store("m_sa", "Riyadh")
    pid = _product("m_sa", s2, "Riyadh", "SA item", "115")
    rows = _by_title(c.get("/products-public").json())
    assert rows["SA item"]["price"] == "115" and rows["SA item"]["original_price"] == "699" and rows["SA item"]["sale_price"] == "115"
    assert fake.merchant_products.find_one({"_id": ObjectId(pid)})["price"] == "115"


def test_store_detail_and_products_by_store_carry_the_currency_and_cost_for_two_currency():
    s2 = _store("m_sa", "Riyadh", cost_for_two="150")
    _product("m_sa", s2, "Riyadh", "SA item", "115")
    d = c.get("/stores/" + s2).json()
    assert d["cost_for_two"] == "150" and d["currency_code"] == "SAR" and d["country"] == "SA"
    assert d["products"][0]["currency_code"] == "SAR" and d["products"][0]["country"] == "SA"
    assert c.get("/products/by-store/" + s2).json()[0]["currency_code"] == "SAR"
    s1 = _store("m_in", "Mumbai", cost_for_two="400")
    d = c.get("/stores/" + s1).json()
    assert d["currency_code"] == "INR" and d["currency_symbol"] == "₹" and d["country"] == "IN"


def test_similar_products_carry_the_currency():
    s2 = _store("m_sa", "Riyadh")
    a = _product("m_sa", s2, "Riyadh", "A", "10")
    _product("m_sa", s2, "Riyadh", "B", "20")
    rows = c.get("/products/%s/similar" % a).json()
    assert [r["title"] for r in rows] == ["B"] and rows[0]["currency_code"] == "SAR"


def test_legacy_and_unknown_owners_stay_india():
    s = _store("m_legacy", "Pune")
    _product("m_legacy", s, "Pune", "Legacy item")
    s_missing = _store("no_such_merchant", "Pune")
    _product("no_such_merchant", s_missing, "Pune", "Orphan item")
    s_admin = _store("m_in", "Pune")
    _product(None, s_admin, "Pune", "OffrO-owned item")                       # no merchant: the store's owner decides
    rows = _by_title(c.get("/products-public").json())
    for t in ("Legacy item", "Orphan item", "OffrO-owned item"):
        assert rows[t]["currency_code"] == "INR" and rows[t]["currency_symbol"] == "₹" and rows[t]["country"] == "IN", t


def test_offro_owned_product_takes_the_stores_owner_country():
    s2 = _store("m_sa", "Riyadh")
    _product(None, s2, "Riyadh", "OffrO item for a Saudi store")
    rows = _by_title(c.get("/products-public").json())
    assert rows["OffrO item for a Saudi store"]["currency_code"] == "SAR"


def test_country_without_a_configured_currency_gets_an_empty_currency_never_rupees():
    s = _store("m_np", "Kathmandu")
    _product("m_np", s, "Kathmandu", "NP item", "300")                     # Nepal is supported but has no Pricing & Plans country record here
    row = _by_title(c.get("/products-public").json())["NP item"]
    assert row["country"] == "NP" and row["currency_code"] == "" and row["currency_symbol"] == ""
    assert row["price"] == "300"


def test_existing_fields_are_all_still_present():
    s1 = _store("m_in", "Mumbai")
    _product("m_in", s1, "Mumbai", "IN item")
    row = c.get("/products-public").json()[0]
    for k in ("id", "title", "text", "validity", "logo", "logo_url", "store_id", "store_name", "city", "from_date", "end_date", "price",
              "original_price", "sale_price", "discount_label", "is_active", "source", "rating", "rating_count"):
        assert k in row, k
