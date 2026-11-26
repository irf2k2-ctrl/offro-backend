"""NOTE: sorts AFTER test_banner_media.py on purpose (that module swaps the shared fake `database` at import).
Saudi QA round: (1) geocoded location language follows the app language, (3) Saudi mobile numbering rules, (4) exact gateway amounts."""
import sys
from datetime import timedelta

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import routers.merchant_app as MA
import routers.public as PUB
import server
from services import country_context as CC
from services import products as P
from utils import phone as PH

fake = sys.modules["database"].db
c = TestClient(server.app)


# ── Issue 1: language reaches the geocoders ──────────────────────────────────────────────────────────────────────────
class _Resp:
    def json(self):
        return {"address": {"state": "Riyadh Region", "city": "Riyadh", "suburb": "Al Munsiyah", "country_code": "sa"}, "display_name": "x"}


@pytest.mark.parametrize("given,sent", [("en", "en"), ("ar", "ar"), ("ar-SA", "ar"), ("EN_us", "en"), ("fr", "en"), ("", "en"), (None, "en")])
def test_normalize_geo_lang(given, sent):
    assert PUB.normalize_geo_lang(given) == sent


@pytest.mark.parametrize("q,sent", [("", "en"), ("&lang=en", "en"), ("&lang=ar", "ar"), ("&lang=zz", "en")])
def test_reverse_geocode_passes_accept_language(monkeypatch, q, sent):
    urls = []
    import requests
    monkeypatch.setattr(requests, "get", lambda url, **k: (urls.append(url), _Resp())[1])
    r = c.get("/reverse-geocode?lat=24.7&lng=46.7" + q).json()
    assert r["city"] == "Riyadh" and r["country"] == "SA"
    assert f"accept-language={sent}" in urls[0] and "lat=24.7&lon=46.7" in urls[0]        # coordinates untouched


def test_resolve_maps_link_passes_language_and_keeps_coordinates(monkeypatch):
    import routers.maps as M
    seen = []
    monkeypatch.setattr(M, "_get_google_page", lambda u: (_ for _ in ()).throw(M.requests.RequestException("no net")))
    monkeypatch.setattr(PUB, "_nominatim_reverse", lambda lat, lng, lang="en": (seen.append(lang), {"state": "S", "city": "C", "area": "A", "address": "D", "country": "SA"})[1])
    for q, want in (("", "en"), ("&lang=ar", "ar"), ("&lang=en", "en")):
        r = c.get("/resolve-maps-link?url=https://www.google.com/maps/@24.7136,46.6753,17z/data=!3d24.7136!4d46.6753" + q)
        assert r.status_code == 200, r.text
        j = r.json()
        assert (j["lat"], j["lng"], j["country"]) == (24.7136, 46.6753, "SA")
        assert seen[-1] == want


def test_places_details_request_has_language(monkeypatch):
    import routers.maps as M
    got = {}
    monkeypatch.setattr(M, "GOOGLE_MAPS_API_KEY", "k")

    class R:
        status_code = 404
        text = ""
    monkeypatch.setattr(M.requests, "get", lambda url, params=None, **k: (got.update(params or {}), R())[1])
    M._get_place_details("abc", "ar")
    assert got["languageCode"] == "ar"
    M._get_place_details("abc")
    assert got["languageCode"] == "en"


# ── Issue 3: Saudi mobile numbers (libphonenumber numbering plan) ────────────────────────────────────────────────────
@pytest.mark.parametrize("prefix", ["50", "53", "54", "55", "56", "57", "58", "59"])
def test_saudi_every_listed_prefix_is_accepted(prefix):
    for number in (prefix + "1234567", prefix + "7654321"):
        if prefix == "57":
            number = "571234567"                      # 57 is valid for 570-578 (5790-5798 too); see the numbering plan
        r = PH.parse("0" + number, "SA")
        assert r.ok and r.e164 == "+966" + number and r.country == "SA", (number, r.error)


@pytest.mark.parametrize("n", ["0501234567", "501234567", "966501234567", "+966501234567", "+966 50 123 4567", "00966501234567"])
def test_saudi_accepted_formats(n):
    assert PH.parse(n, "SA").e164 == "+966501234567"


@pytest.mark.parametrize("n", ["521234567", "5012345", "50123456789", "401234567", "579912345", "0000000000", "+966 11 123 4567"])
def test_saudi_numbering_plan_rejects_invalid(n):
    assert not PH.parse(n, "SA").ok


def test_registration_helper_uses_the_same_rules():
    assert PH.register_phone("0551234567", "SA").phone == "+966551234567"
    assert PH.register_phone("0521234567", "SA").error
    assert PH.register_phone("9876543210", "IN").phone == "+919876543210"        # India stays lenient exactly as before
    assert PH.parse("+971501234567", "SA").error                                   # another country's number is not Saudi


# ── Issue 4: exact gateway amount ────────────────────────────────────────────────────────────────────────────────────
def test_to_minor_sar_is_exact():
    assert CC.to_minor(0.14, "SAR") == 14 and CC.to_minor(1, "SAR") == 100 and CC.to_minor(0.138, "SAR") == 14 and CC.to_minor(0.145, "SAR") in (14, 15)


def test_gateway_order_matches():
    assert CC.gateway_order_matches({"amount": 14, "currency": "SAR"}, 14, "SAR")
    assert not CC.gateway_order_matches({"amount": 100, "currency": "SAR"}, 14, "SAR")   # SAR 0.14 must never become SAR 1
    assert not CC.gateway_order_matches({"amount": 14, "currency": "INR"}, 14, "SAR")
    assert not CC.gateway_order_matches({}, 14, "SAR")


def test_minimum_is_configuration_and_never_raises_the_amount(monkeypatch):
    monkeypatch.setenv("RAZORPAY_INTERNATIONAL_CURRENCIES", "SAR")
    assert CC.payment_route("SAR", True, 14)["mode"] == "razorpay"                       # no configured minimum -> sent as calculated
    monkeypatch.setenv("RAZORPAY_MIN_ORDER_MINOR", "SAR:100")
    assert CC.payment_route("SAR", True, 14) == {"mode": "manual", "manual_reason": "below_gateway_minimum"}
    assert CC.payment_route("SAR", True, 100)["mode"] == "razorpay"
    assert CC.payment_route("SAR", True, 0)["mode"] == "razorpay"
    assert CC.payment_route("AED", True, 14)["manual_reason"] == "currency_not_enabled"


@pytest.fixture
def _sa(monkeypatch):
    for col in ("accounts", "stores", "subscriptions", "pricing", "pricing_countries", "country_prices"):
        fake[col].delete_many({})
    fake.accounts.insert_one({"token": "t", "merchant_id": "m_sa", "name": "Sau", "phone": "+966501234567", "phone_country": "SA", "roles": ["merchant", "user"]})
    sid = fake.stores.insert_one({"store_name": "S", "city": "Riyadh", "status": "active", "merchant_id": "m_sa"}).inserted_id
    fake.pricing.insert_one({"gst_percent": 18, "plans": []})
    import services.pricing_config as PC
    ctx = {"currency_code": "SAR", "currency_symbol": "SAR", "tax_label": "VAT", "tax_percent": 15, "iso": "SA", "name": "Saudi Arabia",
           "prices": {"store_plans": [{"id": "1month", "label": "1 Month", "price": 0.12, "configured": True, "active": True}]}}
    monkeypatch.setattr(CC, "resolve", lambda acct: ctx)
    monkeypatch.setenv("RAZORPAY_INTERNATIONAL_CURRENCIES", "SAR")
    monkeypatch.delenv("RAZORPAY_MIN_ORDER_MINOR", raising=False)
    monkeypatch.setattr(MA, "RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setattr(MA, "RAZORPAY_KEY_SECRET", "s")
    return str(sid)


def _subscribe(sid):
    return c.post("/merchant/subscribe", json={"store_id": sid, "plan": "1month", "from_date": "2026-12-01"}, headers={"Authorization": "Bearer t"}).json()


def test_sar_014_reaches_razorpay_and_the_app_as_14(_sa, monkeypatch):
    sent = []

    def rzp(method, path, auth, json_data, timeout=8):
        sent.append(json_data)

        class R:
            def json(self):
                return {"id": "order_1", "amount": json_data["amount"], "currency": json_data["currency"]}
        return R()
    monkeypatch.setattr(MA, "_razorpay_request", rzp)
    r = _subscribe(_sa)
    assert r["pay_mode"] == "razorpay" and r["amount_display"] == 0.14 and r["amount"] == 14
    assert sent[0]["amount"] == 14 and sent[0]["currency"] == "SAR"
    sub = fake.subscriptions.find_one({"razorpay_order_id": "order_1"})
    assert sub["total"] == 0.14 and CC.to_minor(sub["total"], "SAR") == sent[0]["amount"] == r["amount"]


def test_gateway_returning_other_amount_is_not_used(_sa, monkeypatch):
    def rzp(method, path, auth, json_data, timeout=8):
        class R:
            def json(self):
                return {"id": "order_x", "amount": 100, "currency": "SAR"}
        return R()
    monkeypatch.setattr(MA, "_razorpay_request", rzp)
    r = _subscribe(_sa)
    assert r["pay_mode"] == "manual" and r["razorpay_order_id"] is None and r["manual_reason"] == "gateway_amount_mismatch"
    assert r["amount"] == 14


def test_below_configured_minimum_is_manual_not_rounded_up(_sa, monkeypatch):
    monkeypatch.setenv("RAZORPAY_MIN_ORDER_MINOR", "SAR:100")
    called = []
    monkeypatch.setattr(MA, "_razorpay_request", lambda *a, **k: called.append(1))
    r = _subscribe(_sa)
    assert not called and r["pay_mode"] == "manual" and r["manual_reason"] == "below_gateway_minimum" and r["amount"] == 14


# ── minimum payable after a discount (non-India) ─────────────────────────────────────────────────────────────────────
def _code(code, typ="PERCENTAGE", value=50):
    fake.discounts.delete_many({"code": code})
    fake.discounts.insert_one({"code": code, "type": typ, "value": value, "active": True, "applies_to": "ALL", "max_uses": 0, "used_count": 0})


def test_check_min_payable_rules():
    ctx = {"currency_code": "SAR", "tax_percent": 15}
    CC.check_min_payable(None, 0.14)                       # India: untouched
    CC.check_min_payable(ctx, 0.0)                         # 100% discount: free path, allowed
    CC.check_min_payable(ctx, 1.0)
    CC.check_min_payable(ctx, 115.0)
    CC.check_min_payable(ctx, 0.14, discounted=False)      # no code: the plan's own price is not a discount decision
    with pytest.raises(Exception) as e:
        CC.check_min_payable(ctx, 0.99)
    assert e.value.status_code == 400 and "SAR 1.00" in e.value.detail["message"]


def test_subscribe_with_discount_leaving_under_one_sar_is_stopped_before_any_gateway_call(_sa, monkeypatch):
    called = []
    monkeypatch.setattr(MA, "_razorpay_request", lambda *a, **k: called.append(1))
    _code("HALF")
    r = c.post("/merchant/subscribe", json={"store_id": _sa, "plan": "1month", "from_date": "2026-12-01", "discount_code": "HALF"}, headers={"Authorization": "Bearer t"})
    assert r.status_code == 400 and "minimum" in str(r.json()).lower()
    assert not called and fake.subscriptions.count_documents({}) == 0


def test_hundred_percent_code_is_free_and_never_reaches_the_gateway(_sa, monkeypatch):
    called = []
    monkeypatch.setattr(MA, "_razorpay_request", lambda *a, **k: called.append(1))
    _code("FREE100", value=100)
    r = c.post("/merchant/subscribe", json={"store_id": _sa, "plan": "1month", "from_date": "2026-12-01", "discount_code": "FREE100"}, headers={"Authorization": "Bearer t"})
    assert r.status_code == 200, r.text
    assert not called and r.json().get("amount", 0) == 0 and not r.json().get("razorpay_order_id")


def test_apply_preview_rejects_low_payable_and_accepts_ok_or_free(_sa):
    h = {"Authorization": "Bearer t"}
    _code("HALF")
    _code("FREE100", value=100)
    r = c.post("/merchant/discounts/validate", json={"code": "HALF", "scope": "STORE", "pre_tax_amount": 0.12}, headers=h)
    assert r.status_code == 400 and "SAR 1.00" in str(r.json())
    assert c.post("/merchant/discounts/validate", json={"code": "HALF", "scope": "STORE", "pre_tax_amount": 100}, headers=h).status_code == 200
    assert c.post("/merchant/discounts/validate", json={"code": "FREE100", "scope": "STORE", "pre_tax_amount": 0.12}, headers=h).status_code == 200
    assert c.post("/merchant/discounts/validate", json={"code": "HALF", "scope": "STORE"}, headers=h).status_code == 200      # no amount sent: as before
