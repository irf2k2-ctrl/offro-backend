"""NOTE: file name sorts AFTER test_banner_media.py on purpose (it swaps the shared fake `database` at import).
Rewards hardening: country-aware voucher fulfilment and withdrawal validation.
Run:  python -m pytest test_rewards_hardening.py -q -W ignore"""
import sys

import pytest
from fastapi.testclient import TestClient

import server

fake = sys.modules["database"].db
c = TestClient(server.app)
ADM = {"Authorization": "Bearer sa_tok"}
BASE = "/admin/pricing-config"


@pytest.fixture(autouse=True)
def _world():
    for col in ("dashboard_users", "dashboard_roles", "pricing", "pricing_countries", "country_prices", "pricing_history",
                "accounts", "withdraw_requests", "point_transactions"):
        fake[col].delete_many({})
    rid = fake.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "sa_tok", "status": "active", "full_name": "SA", "mobile": "9000000001",
                                     "role_id": rid, "assigned_cities": ["*"]})
    fake.pricing.insert_one({"gst_percent": 18, "plans": [], "conversion_rate": 0.10, "min_withdraw_points": 200})
    yield


def _user(tok, country=None, pts=500, phone="9876543210", pending_flag=False):
    d = {"token": tok, "phone": phone, "name": "U-" + tok, "visit_points": pts, "pool_points": 0}
    if country:
        d["phone_country"] = country
    if pending_flag:
        d["pending_withdraw"] = True
    r = fake.accounts.insert_one(d)
    return str(r.inserted_id), {"Authorization": "Bearer " + tok}


def _sa(partners="Noon, Jarir", configure=True):
    r = c.post(BASE + "/countries", json={"name": "Saudi Arabia", "code": "SA", "currency_name": "Saudi Riyal", "currency_code": "SAR",
                                          "currency_symbol": "SAR", "tax_label": "VAT", "tax_percent": 15}, headers=ADM)
    assert r.status_code in (200, 201), r.text
    if configure:
        r = c.put(BASE + "/countries/SA/prices/reward", json={"conversion_rate": 0.05, "min_withdraw_points": 100, "voucher_partners": partners}, headers=ADM)
        assert r.status_code == 200, r.text


def _req(uid, **kw):
    d = {"user_id": uid, "user_name": "U", "phone": "9", "points": 100, "status": "pending"}
    d.update(kw)
    return str(fake.withdraw_requests.insert_one(d).inserted_id)


def _sar_req(uid, **kw):
    base = dict(points=100, voucher_value=5.0, country="SA", currency_code="SAR", currency_symbol="SAR", conversion_rate=0.05,
                voucher_partners=["Noon", "Jarir"])
    base.update(kw)
    return _req(uid, **base)


def _fulfil(rid, **body):
    return c.post(f"/admin/withdraw-requests/{rid}/fulfill", json=body, headers=ADM)


def _tx(uid):
    return list(fake.point_transactions.find({"user_id": uid}))


# ───────────────────────────── 1. country-aware fulfilment ─────────────────────────────
def test_saudi_fulfilment_is_recorded_in_sar_never_rupees():
    uid, _ = _user("s1", "SA", pts=250)
    rid = _sar_req(uid)
    r = _fulfil(rid, voucher_code="NOON-123", voucher_type="Noon")
    assert r.status_code == 200, r.text
    t = _tx(uid)[0]
    assert t["description"] == "Gift Voucher Redeemed: Noon SAR 5" and "₹" not in t["description"]
    assert t["currency_code"] == "SAR" and t["voucher_value"] == 5.0 and t["points"] == 100
    assert fake.accounts.find_one({"_id": __import__("bson").ObjectId(uid)})["visit_points"] == 150
    d = fake.withdraw_requests.find_one({})
    assert d["status"] == "fulfilled" and d["voucher_type"] == "Noon" and d["voucher_code"] == "NOON-123"


def test_fulfilment_uses_the_rate_stored_with_the_request_not_points_over_ten():
    uid, _ = _user("i1", "IN", pts=300)
    rid = _req(uid, points=200, voucher_value=30.0, country="IN", currency_code="INR", currency_symbol="₹", conversion_rate=0.15,
               voucher_partners=["Amazon", "Flipkart"])
    assert _fulfil(rid, voucher_code="A1", voucher_type="Flipkart").status_code == 200
    assert _tx(uid)[0]["description"] == "Gift Voucher Redeemed: Flipkart ₹30"       # not 200/10 = 20


def test_legacy_indian_request_is_unchanged():
    uid, _ = _user("l1", pts=300)
    rid = _req(uid, points=200)                                    # made before per-country rewards: no currency, no value, no partners
    row = c.get("/admin/withdraw-requests", headers=ADM).json()[0]
    assert row["currency_code"] == "INR" and row["voucher_value"] == 20.0 and row["voucher_partner_options"] == ["Amazon", "Flipkart"]
    assert _fulfil(rid, voucher_code="X1").status_code == 200       # old callers sent no voucher_type: Amazon, as before
    assert _tx(uid)[0]["description"] == "Gift Voucher Redeemed: Amazon ₹20"
    assert fake.withdraw_requests.find_one({})["voucher_type"] == "Amazon"


def test_old_dialog_field_names_are_still_accepted():
    uid, _ = _user("l2", pts=300)
    rid = _req(uid, points=200)
    assert _fulfil(rid, product_code="Z9", product_type="Flipkart").status_code == 200
    assert fake.withdraw_requests.find_one({})["voucher_type"] == "Flipkart"


def test_dialog_options_come_from_the_requests_own_partners():
    _sa()
    uid, _ = _user("o1", "SA")
    _sar_req(uid)
    _sar_req(uid, voucher_partners=[])
    rows = c.get("/admin/withdraw-requests", headers=ADM).json()
    opts = sorted([r["voucher_partner_options"] for r in rows])
    assert opts == [[], ["Noon", "Jarir"]]


def test_partner_must_be_one_of_the_requests_partners_and_nothing_is_deducted_on_rejection():
    uid, _ = _user("p1", "SA", pts=250)
    rid = _sar_req(uid)
    r = _fulfil(rid, voucher_code="C", voucher_type="Amazon")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_voucher_type"
    assert fake.withdraw_requests.find_one({})["status"] == "pending" and _tx(uid) == []
    assert _fulfil(rid, voucher_code="C", voucher_type="noon").status_code == 200       # case-insensitive, stored as configured
    assert fake.withdraw_requests.find_one({})["voucher_type"] == "Noon"


def test_country_without_partner_names_takes_a_typed_provider():
    uid, _ = _user("p2", "SA", pts=250)
    rid = _sar_req(uid, voucher_partners=[])
    assert _fulfil(rid, voucher_code="C").json()["detail"]["code"] == "voucher_type_required"
    assert _fulfil(rid, voucher_code="C", voucher_type="<b>x</b>").status_code == 400
    assert _fulfil(rid, voucher_code="C", voucher_type="A" * 41).status_code == 400
    assert _fulfil(rid, voucher_code="C", voucher_type="  Panda   Store ").status_code == 200
    assert _tx(uid)[0]["description"] == "Gift Voucher Redeemed: Panda Store SAR 5"


def test_fulfilment_errors_stay_clean():
    uid, _ = _user("e1", "SA")
    rid = _sar_req(uid)
    assert _fulfil(rid, voucher_type="Noon").status_code == 400                          # code required
    assert _fulfil("not-an-id", voucher_code="C", voucher_type="Noon").status_code == 404   # was a 500
    assert _fulfil(rid, voucher_code="C", voucher_type="Noon").status_code == 200
    assert _fulfil(rid, voucher_code="C", voucher_type="Noon").status_code == 400        # already fulfilled


def test_fractional_values_are_not_rounded_away():
    uid, _ = _user("f1", "SA", pts=250)
    rid = _sar_req(uid, voucher_value=7.5)
    _fulfil(rid, voucher_code="C", voucher_type="Noon")
    assert _tx(uid)[0]["description"].endswith("Noon SAR 7.5")


# ───────────────────────────── 2. withdrawal validation ─────────────────────────────
def _w(h, **body):
    return c.post("/user/wallet/withdraw", json=body, headers=h)


@pytest.mark.parametrize("bad", [0, -5, -200, "abc", "", " ", "12abc", 1.5, True, False, [], {}, "1e3", "٣٠٠"])
def test_invalid_amounts_are_rejected_with_a_validation_error_not_a_500(bad):
    _, h = _user("v1", "IN", pts=900)
    r = _w(h, amount=bad)
    if bad in ("", " "):            # blank = not supplied = the minimum (unchanged behaviour)
        assert r.status_code == 200
        return
    assert r.status_code == 422, (bad, r.status_code, r.text)
    assert r.json()["detail"]["code"] == "invalid_amount"
    assert fake.withdraw_requests.count_documents({}) == 0
    assert not fake.accounts.find_one({"token": "v1"}).get("pending_withdraw")


def test_amount_below_the_countrys_minimum_is_rejected():
    _, h = _user("v2", "IN", pts=900)
    r = _w(h, amount=50)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "amount_below_minimum" and "200" in r.json()["detail"]["message"]
    assert fake.withdraw_requests.count_documents({}) == 0


def test_saudi_minimum_is_the_saudi_minimum():
    _sa()
    _, h = _user("v3", "SA", pts=900, phone="+966501234567")
    assert _w(h, amount=99).status_code == 422
    assert _w(h, amount=100).status_code == 200
    d = fake.withdraw_requests.find_one({})
    assert d["points"] == 100 and d["currency_code"] == "SAR" and d["voucher_value"] == 5.0


def test_valid_forms_of_a_whole_number_are_accepted():
    for i, v in enumerate([250, 250.0, "250", " 250 "]):
        _, h = _user("w%d" % i, "IN", pts=900)
        r = _w(h, amount=v)
        assert r.status_code == 200, (v, r.text)
    assert {d["points"] for d in fake.withdraw_requests.find({})} == {250}


def test_no_amount_means_the_minimum_and_null_too():
    _, h = _user("n1", "IN", pts=900)
    assert _w(h).status_code == 200
    assert fake.withdraw_requests.find_one({})["points"] == 200
    _, h2 = _user("n2", "IN", pts=900)
    assert _w(h2, amount=None).status_code == 200


def test_more_than_the_balance_is_still_rejected():
    _, h = _user("m1", "IN", pts=300)
    r = _w(h, amount=400)
    assert r.status_code == 400 and "Not enough points" in r.json()["detail"]


def test_a_second_pending_request_is_rejected_until_the_first_is_fulfilled():
    uid, h = _user("d1", "IN", pts=900)
    assert _w(h, amount=200).status_code == 200
    r = _w(h, amount=200)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "withdraw_pending"
    assert fake.withdraw_requests.count_documents({}) == 1
    rid = str(fake.withdraw_requests.find_one({})["_id"])
    assert _fulfil(rid, voucher_code="C", voucher_type="Amazon").status_code == 200
    assert _w(h, amount=200).status_code == 200                       # the flag was cleared by the fulfilment
    assert fake.withdraw_requests.count_documents({}) == 2


def test_a_stale_pending_flag_without_a_pending_request_does_not_block():
    _, h = _user("d2", "IN", pts=900, pending_flag=True)
    assert _w(h, amount=200).status_code == 200


def test_pending_requests_of_other_users_do_not_block():
    _, h1 = _user("d3", "IN", pts=900)
    _, h2 = _user("d4", "IN", pts=900, phone="9876543211")
    assert _w(h1).status_code == 200 and _w(h2).status_code == 200


def test_country_is_not_configured_still_wins_with_409():
    _sa(configure=False)
    _, h = _user("c1", "SA", pts=900, phone="+966501234567")
    for body in ({}, {"amount": 0}, {"amount": "abc"}, {"amount": 100}):
        r = _w(h, **body)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "reward_not_configured", body
    assert fake.withdraw_requests.count_documents({}) == 0


def test_india_default_flow_is_unchanged():
    _, h = _user("u1", "IN", pts=300)
    r = _w(h)
    assert r.status_code == 200 and "Amazon/Flipkart" in r.json()["message"]
    d = fake.withdraw_requests.find_one({})
    assert d["points"] == 200 and d["voucher_value"] == 20.0 and d["currency_code"] == "INR"
