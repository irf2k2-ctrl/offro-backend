"""NOTE: sorts AFTER test_banner_media.py on purpose (that module swaps the shared fake `database` at import).
Country-based OffrO platform charges: merchant store plans, banners, products and the influencer subscription.
India must behave exactly as before; Saudi Arabia (SA) uses only its own configured prices / VAT / currency; missing pricing, missing tax, an
inactive country or a currency Razorpay is not enabled for never fall back to India - they refuse, or use the manual activation path."""
import copy
import sys
from datetime import timedelta

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import routers.merchant_app as MA
import routers.merchant_products as MP
import server
from services import country_context as CC
from services import products as P

fake = sys.modules["database"].db
c = TestClient(server.app)
ADM = "/admin/pricing-config"
SA_ADMIN = {"Authorization": "Bearer sa_tok"}
IN_H = {"Authorization": "Bearer in_tok"}
SAU_H = {"Authorization": "Bearer sau_tok"}
JO_H = {"Authorization": "Bearer jo_tok"}
INDIA_DOC = {"gst_percent": 18, "plans": [{"id": "1month", "label": "1 Month", "price": 499}, {"id": "3months", "label": "3 Months", "price": 1299},
                                           {"id": "6months", "label": "6 Months", "price": 2299}, {"id": "12months", "label": "12 Months", "price": 3999}],
             "banner_price_per_day": 15, "product_price_per_day": 10, "free_product_limit": 10, "product_max_validity_days": 365,
             "influencer_subscription": {"fee": 999.0, "gst_percent": 18.0, "enabled": True}, "conversion_rate": 0.10, "min_withdraw_points": 200}
END = ""


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    global END
    END = (P.ist_today() + timedelta(days=29)).strftime("%Y-%m-%d")
    for col in ("dashboard_users", "dashboard_roles", "pricing", "pricing_countries", "country_prices", "pricing_history", "accounts", "stores",
                "subscriptions", "invoices", "banner_orders", "merchant_banners", "merchant_products", "product_orders", "influencers", "merchant_locks"):
        fake[col].delete_many({})
    rid = fake.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "sa_tok", "status": "active", "full_name": "SA", "mobile": "9000000000", "role_id": rid, "assigned_cities": ["*"]})
    fake.pricing.insert_one(copy.deepcopy(INDIA_DOC))
    # India merchant (legacy shape: no phone_country), Saudi merchant, Jordan merchant (country never added to Pricing & Plans)
    fake.accounts.insert_one({"token": "in_tok", "merchant_id": "m_in", "name": "Ind", "phone": "9876543210", "roles": ["merchant", "user"]})
    fake.accounts.insert_one({"token": "sau_tok", "merchant_id": "m_sa", "name": "Sau", "phone": "+966501234567", "phone_country": "SA", "roles": ["merchant", "user"]})
    fake.accounts.insert_one({"token": "jo_tok", "merchant_id": "m_jo", "name": "Jor", "phone": "+962791234567", "phone_country": "JO", "roles": ["merchant", "user"]})
    for mid in ("m_in", "m_sa", "m_jo"):
        fake.stores.insert_one({"_id": ObjectId(), "store_name": "S-" + mid, "city": "X", "status": "active", "merchant_id": mid})
    monkeypatch.delenv("RAZORPAY_INTERNATIONAL_CURRENCIES", raising=False)
    calls = []

    def fake_rzp(method, path, auth, json_data, timeout=8):
        calls.append(json_data)

        class R:
            def json(self):
                return {"id": "order_TEST%d" % len(calls)}
        return R()
    for mod in (MA, MP):
        monkeypatch.setattr(mod, "_razorpay_request", fake_rzp, raising=False)
        monkeypatch.setattr(mod, "RAZORPAY_KEY_ID", "rzp_test_x", raising=False)
        monkeypatch.setattr(mod, "RAZORPAY_KEY_SECRET", "secret_x", raising=False)
    yield calls


def _sid(mid):
    return str(fake.stores.find_one({"merchant_id": mid})["_id"])


def _add_sa(**kw):
    b = {"name": "Saudi Arabia", "code": "SA", "currency_name": "Saudi Riyal", "currency_code": "SAR", "currency_symbol": "SAR", "tax_label": "VAT", "tax_percent": 15}
    b.update(kw)
    assert c.post(ADM + "/countries", json=b, headers=SA_ADMIN).status_code == 200


def _put(sec, body, code="SA"):
    r = c.put(f"{ADM}/countries/{code}/prices/{sec}", json=body, headers=SA_ADMIN)
    assert r.status_code == 200, r.text


def _full_sa():
    _add_sa()
    _put("store_plans", {"plans": [{"id": "1month", "price": 100}, {"id": "3months", "price": 250}]})
    _put("banner", {"price_per_day": 2})
    _put("product", {"price_per_day": 1, "free_product_limit": 3, "product_max_validity_days": 60})
    _put("influencer", {"fee": 200, "enabled": True})


# ── India: unchanged ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_india_plans_banner_and_subscribe_unchanged(_world):
    p = c.get("/merchant/plans", headers=IN_H).json()
    assert p[0]["price"] == 499 and p[0]["total"] == 588.82 and p[0]["gst_percent"] == 18
    assert c.get("/merchant/banners/pricing", headers=IN_H).json()["price_per_day"] == 15
    r = c.post("/merchant/subscribe", json={"store_id": _sid("m_in"), "plan": "1month", "from_date": "2026-12-01"}, headers=IN_H).json()
    assert r["amount"] == 58882 and r["amount_display"] == 588.82 and r["gst_percent"] == 18 and r["pay_mode"] == "razorpay"
    assert _world[-1]["currency"] == "INR" and _world[-1]["amount"] == 58882
    assert (r["currency_code"], r["tax_label"]) == ("INR", "GST")


def test_india_banner_order_and_product_unchanged(_world):
    o = c.post("/merchant/banners/order", json={"days": 10, "from_date": "2026-12-01"}, headers=IN_H).json()
    assert o["amount_display"] == 177.0 and o["amount_paise"] == 17700 and o["gst_percent"] == 18 and o["pay_mode"] == "razorpay"
    assert _world[-1]["currency"] == "INR"
    s = c.get("/merchant/products/slots", params={"store_id": _sid("m_in")}, headers=IN_H).json()
    assert s["free_limit"] == 10 and s["price_per_day"] == 10 and s["gst_percent"] == 18


def test_unauthenticated_plans_list_is_still_india():
    assert c.get("/merchant/plans").json()[0]["price"] == 499


# ── Saudi Arabia ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_sa_plans_use_only_sa_prices_vat_and_currency():
    _full_sa()
    p = c.get("/merchant/plans", headers=SAU_H).json()
    assert [x["id"] for x in p] == ["1month", "3months"]                       # only configured plans; no India plan appears
    assert p[0]["price"] == 100 and p[0]["gst_percent"] == 15 and p[0]["gst_amount"] == 15 and p[0]["total"] == 115
    assert (p[0]["currency_code"], p[0]["currency_symbol"], p[0]["tax_label"]) == ("SAR", "SAR", "VAT")
    assert "499" not in str(p) and "588" not in str(p)


def test_sa_subscribe_is_manual_with_reason_when_currency_not_enabled(_world):
    _full_sa()
    r = c.post("/merchant/subscribe", json={"store_id": _sid("m_sa"), "plan": "1month", "from_date": "2026-12-01"}, headers=SAU_H).json()
    assert r["pay_mode"] == "manual" and r["manual_reason"] == "currency_not_enabled" and r["razorpay_order_id"] is None
    assert r["amount_display"] == 115 and r["gst_percent"] == 15 and r["currency_code"] == "SAR" and r["tax_label"] == "VAT"
    assert _world == []                                                          # Razorpay was not called
    sub = fake.subscriptions.find_one({"store_id": _sid("m_sa")})
    assert sub["currency_code"] == "SAR" and sub["tax_label"] == "VAT" and sub["total"] == 115


def test_sa_subscribe_uses_razorpay_in_sar_when_enabled(_world, monkeypatch):
    monkeypatch.setenv("RAZORPAY_INTERNATIONAL_CURRENCIES", "sar, aed")
    _full_sa()
    r = c.post("/merchant/subscribe", json={"store_id": _sid("m_sa"), "plan": "3months", "from_date": "2026-12-01"}, headers=SAU_H).json()
    assert r["pay_mode"] == "razorpay" and r["manual_reason"] is None and r["amount"] == 28750
    assert _world[-1]["currency"] == "SAR" and _world[-1]["amount"] == 28750


def test_sa_unconfigured_plan_is_refused_not_priced_from_india():
    _full_sa()
    r = c.post("/merchant/subscribe", json={"store_id": _sid("m_sa"), "plan": "6months", "from_date": "2026-12-01"}, headers=SAU_H)
    assert r.status_code == 400 and fake.subscriptions.count_documents({}) == 0


def test_sa_banner_pricing_and_order(_world):
    _full_sa()
    b = c.get("/merchant/banners/pricing", headers=SAU_H).json()
    assert b["price_per_day"] == 2 and b["gst_pct"] == 15 and b["currency_code"] == "SAR" and b["tax_label"] == "VAT"
    o = c.post("/merchant/banners/order", json={"days": 10, "from_date": "2026-12-01"}, headers=SAU_H).json()
    assert o["amount_display"] == 23.0 and o["pay_mode"] == "manual" and o["manual_reason"] == "currency_not_enabled" and _world == []
    assert fake.banner_orders.find_one({})["currency_code"] == "SAR"


def test_sa_product_order_uses_sa_free_window_price_and_vat(_world, monkeypatch):
    monkeypatch.setenv("RAZORPAY_INTERNATIONAL_CURRENCIES", "SAR")
    _full_sa()
    sid = _sid("m_sa")
    s = c.get("/merchant/products/slots", params={"store_id": sid}, headers=SAU_H).json()
    assert s["free_limit"] == 3 and s["price_per_day"] == 1 and s["gst_percent"] == 15 and s["currency_code"] == "SAR"
    for _ in range(3):
        fake.merchant_products.insert_one({"merchant_id": "m_sa", "store_id": sid, "status": "approved", "is_active": True,
                                           "end_date": P.ist_today() + timedelta(days=20), "start_date": P.ist_today(), "title": "s"})
    o = c.post("/merchant/products/order", json={"store_id": sid, "end_date": END}, headers=SAU_H).json()
    assert o["pricing_tier"] == "paid" and o["base_price"] == 30 and o["amount_display"] == 34.5 and o["gst_percent"] == 15
    assert o["pay_mode"] == "razorpay" and _world[-1]["currency"] == "SAR" and _world[-1]["amount"] == 3450
    # India's quota is separate: the same store count would still be free for India (10 free)
    sin = _sid("m_in")
    assert c.get("/merchant/products/slots", params={"store_id": sin}, headers=IN_H).json()["free_limit"] == 10


def test_sa_product_settings_missing_is_refused():
    _add_sa()
    _put("store_plans", {"plans": [{"id": "1month", "price": 100}]})
    r = c.get("/merchant/products/slots", params={"store_id": _sid("m_sa")}, headers=SAU_H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "price_not_configured"


# ── missing pricing / tax / inactive / not added ──────────────────────────────────────────────────────────────────────
def test_country_not_in_pricing_plans_is_refused_everywhere():
    for call in (lambda: c.get("/merchant/plans", headers=JO_H), lambda: c.get("/merchant/banners/pricing", headers=JO_H),
                 lambda: c.post("/merchant/banners/order", json={"days": 5, "from_date": "2026-12-01"}, headers=JO_H),
                 lambda: c.post("/merchant/subscribe", json={"store_id": _sid("m_jo"), "plan": "1month", "from_date": "2026-12-01"}, headers=JO_H),
                 lambda: c.get("/merchant/products/slots", params={"store_id": _sid("m_jo")}, headers=JO_H)):
        r = call()
        assert r.status_code == 409 and r.json()["detail"]["code"] == "pricing_not_configured", r.text
    assert fake.subscriptions.count_documents({}) == 0 and fake.banner_orders.count_documents({}) == 0


def test_missing_tax_blocks_a_total():
    _add_sa(tax_label="", tax_percent=None)
    _put("store_plans", {"plans": [{"id": "1month", "price": 100}]})
    r = c.get("/merchant/plans", headers=SAU_H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "tax_not_configured"


def test_inactive_country_is_refused():
    _full_sa()
    assert c.post(ADM + "/countries/SA/status", json={"status": "inactive"}, headers=SA_ADMIN).status_code == 200
    r = c.get("/merchant/banners/pricing", headers=SAU_H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "country_inactive"


def test_tax_free_country_charges_no_tax():
    _add_sa(tax_label="", tax_percent=None, tax_free=True)
    _put("store_plans", {"plans": [{"id": "1month", "price": 100}]})
    p = c.get("/merchant/plans", headers=SAU_H).json()
    assert p[0]["gst_amount"] == 0 and p[0]["total"] == 100


def test_client_supplied_country_is_ignored():
    _full_sa()
    r = c.post("/merchant/subscribe", json={"store_id": _sid("m_in"), "plan": "1month", "from_date": "2026-12-01", "country": "SA"}, headers=IN_H).json()
    assert r["amount_display"] == 588.82 and r["currency_code"] == "INR"


# ── influencer ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _influencer(token, aid):
    inf = fake.influencers.insert_one({"name": "Inf", "account_id": str(aid), "photo_url": "http://x/y.png", "publish_status": "draft",
                                       "payment_status": "UNPAID"}).inserted_id
    fake.accounts.update_one({"_id": aid}, {"$set": {"influencer_id": str(inf)}})
    return inf


def test_influencer_india_pricing_and_publish_unchanged(_world):
    aid = fake.accounts.find_one({"token": "in_tok"})["_id"]
    _influencer("in_tok", aid)
    d = c.get("/user/influencer-profile/subscription-pricing", headers=IN_H).json()
    assert d["fee"] == 999.0 and d["gst_percent"] == 18.0 and d["total"] == 1178.82 and d["currency"] == "INR" and d["tax_label"] == "GST"
    r = c.post("/user/influencer-profile/publish", json={}, headers=IN_H).json()
    assert r["amount"] == 117882 and r["currency"] == "INR" and r["pay_mode"] == "razorpay" and _world[-1]["currency"] == "INR"


def test_influencer_sa_pays_its_own_fee_vat_and_currency(_world):
    _full_sa()
    aid = fake.accounts.find_one({"token": "sau_tok"})["_id"]
    _influencer("sau_tok", aid)
    d = c.get("/user/influencer-profile/subscription-pricing", headers=SAU_H).json()
    assert d["fee"] == 200 and d["gst_percent"] == 15 and d["total"] == 230 and d["currency"] == "SAR" and d["tax_label"] == "VAT"
    r = c.post("/user/influencer-profile/publish", json={}, headers=SAU_H).json()
    assert r["pay_mode"] == "manual" and r["manual_reason"] == "currency_not_enabled" and r["total"] == 230 and r["currency"] == "SAR" and _world == []


def test_influencer_sa_not_configured_is_refused_not_free_and_not_india():
    _add_sa()
    aid = fake.accounts.find_one({"token": "sau_tok"})["_id"]
    inf = _influencer("sau_tok", aid)
    assert c.get("/user/influencer-profile/subscription-pricing", headers=SAU_H).status_code == 409
    r = c.post("/user/influencer-profile/publish", json={}, headers=SAU_H)
    assert r.status_code == 409 and fake.influencers.find_one({"_id": inf})["publish_status"] == "draft"


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("amount,cur,minor", [(588.82, "INR", 58882), (115, "SAR", 11500), (10, "JPY", 10), (1.234, "JOD", 1230), (12.5, "KWD", 12500)])
def test_minor_units(amount, cur, minor):
    assert CC.to_minor(amount, cur) == minor


def test_payment_route_never_assumes_a_currency(monkeypatch):
    assert CC.payment_route("SAR", True)["manual_reason"] == "currency_not_enabled"
    monkeypatch.setenv("RAZORPAY_INTERNATIONAL_CURRENCIES", "SAR")
    assert CC.payment_route("SAR", True)["mode"] == "razorpay"
    assert CC.payment_route("SAR", False)["manual_reason"] == "payment_gateway_not_configured"
    assert CC.payment_route("JOD", True)["mode"] == "manual"


def test_invoices_carry_currency_and_tax_label(_world):
    _full_sa()
    c.post("/merchant/subscribe", json={"store_id": _sid("m_sa"), "plan": "1month", "from_date": "2026-12-01"}, headers=SAU_H)
    sub = fake.subscriptions.find_one({})
    fake.invoices.insert_one({"invoice_no": "X1", "merchant_id": "m_sa", "store_id": _sid("m_sa"), "total": 115, "gst": 15, "gst_percent": 15,
                              "base_price": 100, "created_at": __import__("datetime").datetime.utcnow(), **CC.stamp(sub)})
    inv = c.get("/merchant/invoices/full", headers=SAU_H).json()[0]
    assert (inv["currency_code"], inv["tax_label"]) == ("SAR", "VAT")
    fake.invoices.insert_one({"invoice_no": "X2", "merchant_id": "m_in", "total": 118, "gst": 18, "created_at": __import__("datetime").datetime.utcnow()})
    old = c.get("/merchant/invoices/full", headers=IN_H).json()[0]
    assert (old["currency_code"], old["currency_symbol"], old["tax_label"]) == ("INR", "₹", "GST")
