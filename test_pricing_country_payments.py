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


def test_unauthenticated_plans_list_does_not_expose_india_prices():
    # No verified country (no / invalid session): refused with a clear code - Indian prices are not shown. A signed-in India account is
    # unchanged (see test_india_plans_banner_and_subscribe_unchanged).
    for h in ({}, {"Authorization": "Bearer nope"}):
        r = c.get("/merchant/plans", headers=h)
        assert r.status_code == 401 and r.json()["detail"]["code"] == "country_context_required"
        assert "499" not in r.text and "588" not in r.text


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


# ══ Phase 4: admin-created products, admin store renewals, discount codes, plans endpoint ════════════════════════════
def _prev_sub(mid, plan="1month", price=499, gst=18):
    sid = _sid(mid)
    fake.subscriptions.insert_one({"store_id": sid, "merchant_id": mid, "plan": plan, "price": price, "gst_percent": gst, "status": "paid",
                                   "from_date": P.ist_today() - timedelta(days=60), "end_date": P.ist_today() - timedelta(days=30),
                                   "created_at": P.ist_today() - timedelta(days=60)})
    return sid


def _renew_body(**kw):
    b = {"plan": "1month", "from_date": (P.ist_today() + timedelta(days=1)).strftime("%Y-%m-%d"),
         "end_date": (P.ist_today() + timedelta(days=31)).strftime("%Y-%m-%d"), "pay_mode": "cash"}
    b.update(kw)
    return b


def test_admin_renewal_india_unchanged():
    sid = _prev_sub("m_in")
    i = c.get(f"/admin/stores/{sid}/renewal-info", headers=SA_ADMIN).json()
    assert i["amount"] == 499 and i["gst_percent"] == 18 and i["currency_code"] == "INR" and i["tax_label"] == "GST"
    r = c.post(f"/admin/stores/{sid}/renew-subscription", json=_renew_body(amount=499, gst_percent=18), headers=SA_ADMIN)
    assert r.status_code == 200, r.text
    sub = fake.subscriptions.find_one({"renewal": True})
    assert sub["total"] == 588.82 and sub["gst_percent"] == 18 and sub["currency_code"] == "INR" and sub["tax_label"] == "GST"
    assert "gst_percent" not in fake.invoices.find_one({"store_id": sid})                   # India's invoice document keeps its old shape


def test_admin_renewal_sa_uses_sa_plan_currency_and_vat_ignoring_form_gst():
    _full_sa()
    sid = _prev_sub("m_sa", price=499, gst=18)                                              # an old row must not leak India's price / GST
    i = c.get(f"/admin/stores/{sid}/renewal-info", headers=SA_ADMIN).json()
    assert i["amount"] == 100 and i["gst_percent"] == 15 and i["currency_code"] == "SAR" and i["tax_label"] == "VAT"
    r = c.post(f"/admin/stores/{sid}/renew-subscription", json=_renew_body(gst_percent=18), headers=SA_ADMIN)   # no amount typed, India GST sent
    assert r.status_code == 200, r.text
    sub = fake.subscriptions.find_one({"renewal": True})
    assert (sub["price"], sub["gst_percent"], sub["gst"], sub["total"]) == (100, 15, 15, 115)
    assert sub["currency_code"] == "SAR" and sub["tax_label"] == "VAT"
    inv = fake.invoices.find_one({"store_id": sid})
    assert inv["currency_code"] == "SAR" and inv["tax_label"] == "VAT" and inv["gst_percent"] == 15 and inv["total"] == 115


def test_admin_renewal_sa_plan_without_price_needs_an_amount_never_india():
    _full_sa()
    sid = _prev_sub("m_sa", plan="6months")
    i = c.get(f"/admin/stores/{sid}/renewal-info", headers=SA_ADMIN).json()
    assert i["amount"] is None and "6months" in i["price_note"]
    r = c.post(f"/admin/stores/{sid}/renew-subscription", json=_renew_body(plan="6months"), headers=SA_ADMIN)
    assert r.status_code == 409 and isinstance(r.json()["detail"], str) and "SAR" not in r.json()["detail"] and "499" not in r.json()["detail"]
    ok = c.post(f"/admin/stores/{sid}/renew-subscription", json=_renew_body(plan="6months", amount=500), headers=SA_ADMIN)
    assert ok.status_code == 200 and fake.subscriptions.find_one({"renewal": True})["gst"] == 75        # 15% VAT, not 18%


def test_admin_renewal_missing_tax_inactive_and_unknown_country_refused_with_text():
    _add_sa(tax_label="", tax_percent=None)
    _put("store_plans", {"plans": [{"id": "1month", "price": 100}]})
    sid = _prev_sub("m_sa")
    r = c.get(f"/admin/stores/{sid}/renewal-info", headers=SA_ADMIN)
    assert r.status_code == 409 and isinstance(r.json()["detail"], str) and "Tax" in r.json()["detail"]
    assert c.post(f"/admin/stores/{sid}/renew-subscription", json=_renew_body(amount=100), headers=SA_ADMIN).status_code == 409
    jsid = _prev_sub("m_jo")
    assert c.get(f"/admin/stores/{jsid}/renewal-info", headers=SA_ADMIN).status_code == 409
    assert fake.subscriptions.count_documents({"renewal": True}) == 0


def test_admin_renewal_inactive_country_refused():
    _full_sa()
    sid = _prev_sub("m_sa")
    assert c.post(ADM + "/countries/SA/status", json={"status": "inactive"}, headers=SA_ADMIN).status_code == 200
    r = c.post(f"/admin/stores/{sid}/renew-subscription", json=_renew_body(amount=100), headers=SA_ADMIN)
    assert r.status_code == 409 and isinstance(r.json()["detail"], str)


def _admin_product(sid, **kw):
    b = {"store_id": sid, "title": "Tea", "logo": "http://img/x.png", "end_date": END, "price": "10"}
    b.update(kw)
    return c.post("/admin/products", json=b, headers=SA_ADMIN)


@pytest.fixture
def _noimg(monkeypatch):
    import routers.admin_products as AP
    monkeypatch.setattr(AP, "_cloudinary_upload", lambda img, folder="": img, raising=False)
    monkeypatch.setattr(AP, "_make_thumb_url", lambda u: u, raising=False)


def test_admin_product_india_unchanged(_noimg):
    r = _admin_product(_sid("m_in"))
    assert r.status_code == 200 and r.json()["pricing_tier"] == "free" and r.json()["currency_code"] == "INR"


def test_admin_product_sa_uses_sa_free_window_price_and_vat(_noimg):
    _full_sa()
    sid = _sid("m_sa")
    for _ in range(3):                                                                         # SA free window is 3 (India's is 10)
        assert _admin_product(sid).json()["pricing_tier"] == "free"
    r = _admin_product(sid)                                                                    # 4th is paid in SA - would still be free in India
    assert r.status_code == 409 and isinstance(r.json()["detail"], str)
    msg = r.json()["detail"]
    assert "SAR" in msg and "VAT" in msg and "₹" not in msg and "GST" not in msg
    low = _admin_product(sid, pay_mode="cash", pay_amount=10)
    assert low.status_code == 400 and "SAR" in low.json()["detail"] and "₹" not in low.json()["detail"]
    ok = _admin_product(sid, pay_mode="cash", pay_amount=34.5)
    assert ok.status_code == 200, ok.text
    assert ok.json()["pricing_tier"] == "paid" and ok.json()["currency_code"] == "SAR" and ok.json()["tax_label"] == "VAT"
    o = fake.product_orders.find_one({"recorded_by_admin": True})
    assert o["gst_percent"] == 15 and o["currency_code"] == "SAR" and o["tax_label"] == "VAT"
    inv = fake.invoices.find_one({"type": "product"})
    assert inv["gst_percent"] == 15 and inv["currency_code"] == "SAR" and inv["tax_label"] == "VAT"


def test_admin_product_sa_without_product_pricing_is_refused_not_india(_noimg):
    _add_sa()
    _put("store_plans", {"plans": [{"id": "1month", "price": 100}]})
    r = _admin_product(_sid("m_sa"))
    assert r.status_code == 409 and isinstance(r.json()["detail"], str) and fake.merchant_products.count_documents({}) == 0


def test_admin_product_inactive_country_refused(_noimg):
    _full_sa()
    c.post(ADM + "/countries/SA/status", json={"status": "inactive"}, headers=SA_ADMIN)
    assert _admin_product(_sid("m_sa")).status_code == 409 and fake.merchant_products.count_documents({}) == 0


def test_admin_product_extend_uses_the_owners_country_max_days(_noimg):
    _full_sa()                                                                                 # SA max validity 60, India 365
    pid = _admin_product(_sid("m_sa")).json()["id"]
    assert c.put(f"/admin/products/{pid}/extend", json={"days": 100}, headers=SA_ADMIN).status_code == 400
    assert c.put(f"/admin/products/{pid}/extend", json={"days": 5}, headers=SA_ADMIN).status_code == 200


def _code(code, typ, value, **kw):
    fake.discounts.delete_many({"code": code})
    fake.discounts.insert_one({"code": code, "type": typ, "value": value, "active": True, "applies_to": "ALL", "max_uses": 0, "used_count": 0, **kw})


def test_fixed_amount_codes_are_never_applied_to_another_currency():
    _full_sa()
    _code("FLAT50", "VALUE", 50)
    _code("TEN", "PERCENTAGE", 10)
    body = {"store_id": _sid("m_sa"), "plan": "1month", "from_date": "2026-12-01"}
    r = c.post("/merchant/subscribe", json={**body, "discount_code": "FLAT50"}, headers=SAU_H)
    assert r.status_code == 400 and "SAR" in r.json()["detail"] and fake.subscriptions.count_documents({}) == 0
    ok = c.post("/merchant/subscribe", json={**body, "discount_code": "TEN"}, headers=SAU_H).json()
    assert ok["amount_display"] == 103.5                                                       # (100 - 10) + 15% VAT
    # preview endpoint agrees
    assert c.post("/merchant/discounts/validate", json={"code": "FLAT50"}, headers=SAU_H).status_code == 400
    assert c.post("/merchant/discounts/validate", json={"code": "TEN"}, headers=SAU_H).status_code == 200
    # India keeps its fixed-amount codes
    ind = c.post("/merchant/subscribe", json={"store_id": _sid("m_in"), "plan": "1month", "from_date": "2026-12-01", "discount_code": "FLAT50"}, headers=IN_H).json()
    assert ind["amount_display"] == round((499 - 50) * 1.18, 2)
    assert c.post("/merchant/discounts/validate", json={"code": "FLAT50"}, headers=IN_H).status_code == 200
