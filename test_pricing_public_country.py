"""NOTE: sorts AFTER test_banner_media.py on purpose (that module swaps the shared fake `database` at import).
GET /country-pricing/{code}: read-only country pricing for the apps. Never falls back to India; payment only for India today."""
import copy
import sys

import pytest
from fastapi.testclient import TestClient

import server

fake = sys.modules["database"].db
c = TestClient(server.app)
SA_H = {"Authorization": "Bearer sa_tok"}
ADM = "/admin/pricing-config"
INDIA_DOC = {"gst_percent": 18, "plans": [{"id": "1month", "label": "1 Month", "price": 499}, {"id": "3months", "label": "3 Months", "price": 1299},
                                           {"id": "6months", "label": "6 Months", "price": 2299}, {"id": "12months", "label": "12 Months", "price": 3999}],
             "banner_price_per_day": 15, "product_price_per_day": 10, "free_product_limit": 10, "product_max_validity_days": 365,
             "influencer_subscription": {"fee": 999.0, "gst_percent": 18.0, "enabled": True}, "conversion_rate": 0.10, "min_withdraw_points": 200}


@pytest.fixture(autouse=True)
def _world():
    for col in ("dashboard_users", "dashboard_roles", "pricing", "pricing_countries", "country_prices", "pricing_history"):
        fake[col].delete_many({})
    rid = fake.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "sa_tok", "status": "active", "full_name": "SA", "mobile": "9000000000", "role_id": rid, "assigned_cities": ["*"]})
    fake.pricing.insert_one(copy.deepcopy(INDIA_DOC))
    yield


def _sa(**kw):
    b = {"name": "Saudi Arabia", "code": "SA", "currency_name": "Saudi Riyal", "currency_code": "SAR", "currency_symbol": "SAR", "tax_label": "VAT", "tax_percent": 15}
    b.update(kw)
    assert c.post(ADM + "/countries", json=b, headers=SA_H).status_code == 200


def test_no_login_needed_and_india_matches_existing_values():
    r = c.get("/country-pricing/in")
    assert r.status_code == 200
    d = r.json()
    assert d["country"]["currency_code"] == "INR" and d["tax"] == {"label": "GST", "percent": 18.0, "tax_free": False, "configured": True, "message": None}
    p = {x["id"]: x for x in d["store_plans"]}
    assert p["1month"]["price"] == 499 and p["1month"]["total"] == 588.82 and p["12months"]["price"] == 3999
    assert d["banner"]["price"] == 15 and d["product"]["free_product_limit"] == 10 and d["product"]["product_max_validity_days"] == 365
    assert d["influencer"]["price"] == 999.0 and d["influencer"]["total"] == 1178.82
    assert d["payment"] == {"supported": True, "mode": "razorpay", "currency": "INR", "reason": None}


def test_other_country_before_any_price_is_not_configured_and_never_india():
    _sa()
    d = c.get("/country-pricing/SA").json()
    assert d["tax"]["configured"] and d["tax"]["percent"] == 15.0
    assert all(x["configured"] is False and "price" not in x for x in d["store_plans"])
    assert d["banner"] == {"configured": False, "active": False} and d["influencer"]["configured"] is False
    assert d["product"] == {"configured": False, "active": False}
    assert "499" not in c.get("/country-pricing/SA").text and "18.0" not in c.get("/country-pricing/SA").text


def test_configured_country_uses_only_its_own_price_and_tax():
    _sa()
    h = SA_H
    assert c.put(ADM + "/countries/SA/prices/store_plans", json={"plans": [{"id": "1month", "price": 29}]}, headers=h).status_code == 200
    assert c.put(ADM + "/countries/SA/prices/banner", json={"price_per_day": 2}, headers=h).status_code == 200
    assert c.put(ADM + "/countries/SA/prices/product", json={"price_per_day": 1, "free_product_limit": 3, "product_max_validity_days": 60}, headers=h).status_code == 200
    d = c.get("/country-pricing/SA").json()
    one = next(x for x in d["store_plans"] if x["id"] == "1month")
    assert one["price"] == 29 and one["tax_amount"] == 4.35 and one["total"] == 33.35
    assert d["banner"]["total"] == 2.3 and d["product"]["free_product_limit"] == 3 and d["product"]["product_max_validity_days"] == 60
    assert next(x for x in d["store_plans"] if x["id"] == "3months")["configured"] is False
    assert d["payment"] == {"supported": False, "mode": "manual", "currency": "SAR", "reason": "currency_not_enabled"}


def test_deactivated_plan_is_not_offered():
    _sa()
    c.put(ADM + "/countries/SA/prices/banner", json={"price_per_day": 2}, headers=SA_H)
    c.put(ADM + "/countries/SA/prices/banner", json={"active": False}, headers=SA_H)
    assert c.get("/country-pricing/SA").json()["banner"] == {"configured": True, "active": False}


def test_missing_tax_is_reported_and_no_total_is_computed():
    _sa(tax_label="", tax_percent=None)
    c.put(ADM + "/countries/SA/prices/banner", json={"price_per_day": 2}, headers=SA_H)
    d = c.get("/country-pricing/SA").json()
    assert d["tax"]["configured"] is False and d["tax"]["message"] and d["banner"]["total"] is None


def test_unknown_inactive_and_invalid_country_are_refused_without_fallback():
    assert c.get("/country-pricing/JO").status_code == 404
    assert c.get("/country-pricing/JO").json()["detail"]["code"] == "pricing_not_configured"
    _sa()
    c.post(ADM + "/countries/SA/status", json={"status": "inactive"}, headers=SA_H)
    assert c.get("/country-pricing/SA").json()["detail"]["code"] == "country_inactive"
    for bad in ("X", "123", "SAU"):
        assert c.get("/country-pricing/" + bad).status_code == 422


def test_existing_merchant_endpoints_unchanged():
    assert c.get("/merchant/plans").status_code == 401        # no verified country -> India prices are not exposed (Phase 4)
