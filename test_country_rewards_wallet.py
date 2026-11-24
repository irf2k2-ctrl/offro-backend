"""NOTE: file name sorts AFTER test_banner_media.py on purpose (it swaps the shared fake `database` at import).
Per-country reward redemption: /user/wallet `reward` block, /user/wallet/withdraw, admin withdraw list, voucher partners.
Run:  python -m pytest test_country_rewards_wallet.py -q -W ignore"""
import sys

import pytest
from fastapi.testclient import TestClient

import server

fake = sys.modules["database"].db
c = TestClient(server.app)
SA_H = {"Authorization": "Bearer sa_tok"}
BASE = "/admin/pricing-config"


def _headers(tok):
    return {"Authorization": "Bearer " + tok}


@pytest.fixture(autouse=True)
def _world():
    for col in ("dashboard_users", "dashboard_roles", "pricing", "pricing_countries", "country_prices", "pricing_history",
                "accounts", "withdraw_requests"):
        fake[col].delete_many({})
    rid = fake.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "sa_tok", "status": "active", "full_name": "SA", "mobile": "9000000001",
                                     "role_id": rid, "assigned_cities": ["*"]})
    fake.pricing.insert_one({"gst_percent": 18, "plans": [], "conversion_rate": 0.10, "min_withdraw_points": 200})
    yield


def _acct(tok, country=None, pts=500, phone="9876543210"):
    d = {"token": tok, "phone": phone, "name": "U", "visit_points": pts, "pool_points": 0}
    if country:
        d["phone_country"] = country
    fake.accounts.insert_one(d)
    return _headers(tok)


def _sa_country():
    r = c.post(BASE + "/countries", json={"name": "Saudi Arabia", "code": "SA", "currency_name": "Saudi Riyal", "currency_code": "SAR",
                                          "currency_symbol": "SAR", "tax_label": "VAT", "tax_percent": 15}, headers=SA_H)
    assert r.status_code in (200, 201), r.text


def _sa_reward(**kw):
    body = {"conversion_rate": 0.05, "min_withdraw_points": 100}
    body.update(kw)
    r = c.put(BASE + "/countries/SA/prices/reward", json=body, headers=SA_H)
    assert r.status_code == 200, r.text
    return r.json()


def test_india_unchanged_200_points_is_20_rupees():
    h = _acct("in1", "IN")
    w = c.get("/user/wallet", headers=h).json()
    r = w["reward"]
    assert r["redeemable"] and r["currency_code"] == "INR" and r["currency_symbol"] == "₹"
    assert r["voucher_points"] == 200 and r["voucher_value"] == 20.0
    assert r["voucher_partners"] == ["Amazon", "Flipkart"]
    assert w["conversion_rate"] == 0.10 and w["min_withdraw_points"] == 200 and w["value_in_rupees"] == 50.0


def test_account_without_country_is_treated_as_india():
    h = _acct("in2")                      # legacy 10 digit Indian number, no phone_country
    assert c.get("/user/wallet", headers=h).json()["reward"]["currency_code"] == "INR"


def test_india_withdraw_stores_inr_value_and_rate():
    h = _acct("in3", "IN", pts=300)
    r = c.post("/user/wallet/withdraw", json={}, headers=h)
    assert r.status_code == 200 and "Amazon/Flipkart" in r.json()["message"]
    d = fake.withdraw_requests.find_one({})
    assert d["points"] == 200 and d["voucher_value"] == 20.0 and d["currency_code"] == "INR" and d["country"] == "IN"
    assert d["conversion_rate"] == 0.10


def test_saudi_not_configured_has_no_amount_and_cannot_redeem():
    _sa_country()
    h = _acct("sa1", "SA", phone="+966501234567")
    r = c.get("/user/wallet", headers=h).json()["reward"]
    assert r["configured"] is False and r["redeemable"] is False
    assert r["voucher_points"] is None and r["voucher_value"] is None and r["currency_code"] == "SAR"
    w = c.post("/user/wallet/withdraw", json={}, headers=h)
    assert w.status_code == 409 and w.json()["detail"]["code"] == "reward_not_configured"
    assert fake.withdraw_requests.count_documents({}) == 0
    assert c.get("/user/wallet", headers=h).json()["value_in_rupees"] == 0.0     # no INR value for a Saudi account


def test_saudi_configured_uses_its_own_rate_currency_and_partners():
    _sa_country()
    _sa_reward(voucher_partners="Noon, Jarir")
    h = _acct("sa2", "SA", pts=250, phone="+966501234567")
    w = c.get("/user/wallet", headers=h).json()
    r = w["reward"]
    assert r["redeemable"] and r["currency_code"] == "SAR" and r["voucher_points"] == 100 and r["voucher_value"] == 5.0
    assert r["voucher_partners"] == ["Noon", "Jarir"] and r["total_value"] == 12.5
    ok = c.post("/user/wallet/withdraw", json={}, headers=h)
    assert ok.status_code == 200 and "Amazon" not in ok.json()["message"]
    d = fake.withdraw_requests.find_one({})
    assert d["currency_code"] == "SAR" and d["voucher_value"] == 5.0 and d["country"] == "SA" and d["voucher_partners"] == ["Noon", "Jarir"]


def test_saudi_below_minimum_points_is_rejected():
    _sa_country()
    _sa_reward()
    h = _acct("sa3", "SA", pts=40, phone="+966501234567")
    assert c.post("/user/wallet/withdraw", json={}, headers=h).status_code == 400


def test_inactive_country_reward_blocks_redemption():
    _sa_country()
    _sa_reward()
    c.put(BASE + "/countries/SA/prices/reward", json={"active": False}, headers=SA_H)
    h = _acct("sa4", "SA", phone="+966501234567")
    r = c.get("/user/wallet", headers=h).json()["reward"]
    assert r["active"] is False and r["redeemable"] is False and r["voucher_value"] is None
    assert c.post("/user/wallet/withdraw", json={}, headers=h).status_code == 409


def test_india_settings_do_not_leak_into_saudi():
    _sa_country()
    fake.pricing.update_one({}, {"$set": {"conversion_rate": 0.5, "min_withdraw_points": 10}})
    h = _acct("sa5", "SA", phone="+966501234567")
    assert c.get("/user/wallet", headers=h).json()["reward"]["voucher_value"] is None


def test_admin_list_shows_currency_and_treats_legacy_rows_as_inr():
    fake.withdraw_requests.insert_one({"user_id": "1", "user_name": "Old", "phone": "9", "points": 200, "status": "pending"})
    fake.withdraw_requests.insert_one({"user_id": "2", "user_name": "New", "phone": "9", "points": 100, "voucher_value": 5.0,
                                       "currency_code": "SAR", "currency_symbol": "SAR", "country": "SA", "status": "pending"})
    rows = {r["user_name"]: r for r in c.get("/admin/withdraw-requests", headers=SA_H).json()}
    assert rows["Old"]["currency_code"] == "INR" and rows["Old"]["voucher_value"] == 20.0 and rows["Old"]["country"] == "IN"
    assert rows["New"]["currency_code"] == "SAR" and rows["New"]["voucher_value"] == 5.0


def test_partner_names_are_validated():
    _sa_country()
    for bad in ["<b>x</b>", "a" * 31, "a,b,c,d,e,f,gg"]:
        r = c.put(BASE + "/countries/SA/prices/reward", json={"conversion_rate": 0.05, "min_withdraw_points": 100, "voucher_partners": bad}, headers=SA_H)
        assert r.status_code == 422, bad
    _sa_reward(voucher_partners="")
    assert c.get(BASE + "/countries/SA/prices", headers=SA_H).json()["reward"]["voucher_partners"] == []


def test_india_partners_can_be_changed_and_default_returns_when_unset():
    r = c.put(BASE + "/countries/IN/prices/reward", json={"conversion_rate": 0.1, "min_withdraw_points": 200, "voucher_partners": "Amazon"}, headers=SA_H)
    assert r.status_code == 200, r.text
    h = _acct("in5", "IN")
    assert c.get("/user/wallet", headers=h).json()["reward"]["voucher_partners"] == ["Amazon"]
