"""Phase 2 / Task 2: country + currency on the dashboard Payments lists, country filter, read-only historical report.
(name sorts after test_banner_media.py on purpose.)  Run: python -m pytest test_phase2_task2_payment_country.py -q -W ignore"""
import copy
import sys
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import server
from test_dashboard_country_access import ALL_PERMS, H, mk_user

DB = sys.modules["database"].db
web = TestClient(server.app)
NOW = datetime.utcnow()


@pytest.fixture
def pay():
    for c in ("accounts", "invoices", "subscriptions", "merchant_banners", "dashboard_users", "dashboard_roles"):
        DB[c].delete_many({})
    ids = {}
    for k, ph, extra in (("in", "9876543210", {}), ("sa", "+966501234567", {"phone_country": "SA"}), ("np", "+9779812345678", {}),
                         ("x", "", {})):
        ids[k] = str(DB.accounts.insert_one({"name": k, "phone": ph, "roles": ["merchant"], "status": "active", **extra}).inserted_id)
    base = {"store_name": "S", "plan": "1month", "created_at": NOW}
    DB.invoices.insert_many([
        {**base, "invoice_no": "I-IN-OLD", "merchant_id": ids["in"], "merchant_phone": "9876543210", "total": 118.0},                  # no stamp
        {**base, "invoice_no": "I-SA", "merchant_id": ids["sa"], "merchant_phone": "+966501234567", "total": 57.5,
         "currency_code": "SAR", "currency_symbol": "SR"},
        {**base, "invoice_no": "I-NP-NOSTAMP", "merchant_id": ids["np"], "merchant_phone": "+9779812345678", "total": 400.0},       # intl, no stamp
        {**base, "invoice_no": "I-REC", "merchant_id": ids["in"], "merchant_phone": "9876543210", "total": 10, "country": "JO",
         "currency_code": "JOD", "currency_symbol": "JD"},                                                                         # own country wins
        {**base, "invoice_no": "I-ORPHAN", "merchant_id": "000000000000000000000000", "merchant_phone": "", "total": 5},          # owner unknown
    ])
    DB.subscriptions.insert_one({**base, "merchant_id": ids["sa"], "status": "pending", "total": 99, "currency_code": "SAR"})
    DB.subscriptions.insert_one({**base, "merchant_id": ids["in"], "status": "paid", "total": 1})
    DB.merchant_banners.insert_one({"merchant_id": ids["np"], "merchant_phone": "+9779812345678", "payment_status": "paid", "total": 7,
                                    "invoice_no": "B-NP", "created_at": NOW, "currency_code": "NPR", "currency_symbol": "Rs"})
    mk_user("sup", role="Super Admin")
    return ids


def rows(path, tok="sup", **params):
    r = web.get(path, headers=H(tok), params=params)
    assert r.status_code == 200, r.text
    return {x.get("invoice_no") or x.get("store_name"): x for x in r.json()} if path.endswith("full") else r.json()


def test_country_and_currency_shown_with_original_amount(pay):
    r = rows("/admin/invoices/full")
    assert (r["I-SA"]["country"], r["I-SA"]["currency_code"], r["I-SA"]["total"]) == ("SA", "SAR", 57.5)
    assert r["I-SA"]["country_name"] and r["I-SA"]["currency_source"] == "recorded"
    assert (r["B-NP"]["country"], r["B-NP"]["currency_code"], r["B-NP"]["total"]) == ("NP", "NPR", 7.0)


def test_legacy_india_record_without_stamp_is_inr(pay):
    r = rows("/admin/invoices/full")["I-IN-OLD"]
    assert (r["country"], r["currency_code"], r["currency_source"], r["total"]) == ("IN", "INR", "legacy_default_inr", 118.0)


def test_international_record_without_stamp_never_gets_inr(pay):
    r = rows("/admin/invoices/full")["I-NP-NOSTAMP"]
    assert r["country"] == "NP" and r["currency_code"] == "" and r["currency_source"] == "not_recorded" and r["total"] == 400.0


def test_unknown_owner_is_unresolved_not_india(pay):
    r = rows("/admin/invoices/full")["I-ORPHAN"]
    assert r["country"] == "" and r["country_source"] == "unresolved" and r["currency_code"] == ""


def test_record_own_country_wins_over_owner(pay):
    r = rows("/admin/invoices/full")["I-REC"]
    assert (r["country"], r["country_source"], r["currency_code"]) == ("JO", "record", "JOD")


def test_no_conversion_amounts_unchanged(pay):
    r = rows("/admin/invoices/full")
    assert {k: v["total"] for k, v in r.items()} == {"I-IN-OLD": 118.0, "I-SA": 57.5, "I-NP-NOSTAMP": 400.0, "I-REC": 10.0,
                                                     "I-ORPHAN": 5.0, "B-NP": 7.0}


@pytest.mark.parametrize("path", ["/admin/invoices/full", "/admin/merchant-transactions", "/admin/subscriptions"])
def test_country_filter_on_each_list(pay, path):
    allr = web.get(path, headers=H("sup")).json()
    sa = web.get(path, headers=H("sup"), params={"country": "SA"}).json()
    assert sa and all(x["country"] == "SA" for x in sa) and len(sa) < len(allr)
    none = web.get(path, headers=H("sup"), params={"country": "NONE"}).json()
    assert all(x["country"] == "" for x in none)


def test_filter_lower_case_and_invalid(pay):
    assert all(x["country"] == "SA" for x in web.get("/admin/invoices/full", headers=H("sup"), params={"country": "sa"}).json())
    assert web.get("/admin/invoices/full", headers=H("sup"), params={"country": "ZZ"}).status_code == 400


def test_pending_subscription_has_currency(pay):
    pend = [x for x in web.get("/admin/subscriptions", headers=H("sup")).json() if x["status"] == "pending"][0]
    assert pend["country"] == "SA" and pend["currency_code"] == "SAR" and pend["total"] == 99


def test_requires_payments_permission(pay):
    perms = copy.deepcopy(ALL_PERMS); perms["Payments"]["view"] = False
    mk_user("nop", role="NoPay", perms=perms)
    for p in ("/admin/invoices/full", "/admin/merchant-transactions", "/admin/subscriptions", "/admin/payments/country-report"):
        assert web.get(p, headers=H("nop")).status_code == 403


def test_country_restricted_admin_sees_only_own_country_and_filter_cannot_widen(pay):
    mk_user("sa-admin", country="SA")
    for p in ("/admin/invoices/full", "/admin/merchant-transactions"):
        got = web.get(p, headers=H("sa-admin")).json()
        assert got and all(x["country"] == "SA" for x in got)
        assert web.get(p, headers=H("sa-admin"), params={"country": "IN"}).json() == []
        assert web.get(p, headers=H("sa-admin"), params={"country": "NONE"}).json() == []


def test_report_is_read_only_and_counts_gaps(pay):
    before = {c: list(DB[c].find({})) for c in ("invoices", "subscriptions", "merchant_banners")}
    r = web.get("/admin/payments/country-report", headers=H("sup")).json()
    inv = r["collections"]["invoices"]
    assert inv["records"] == 5 and inv["country_unresolved"] == 1 and inv["country_from_record"] == 1
    assert inv["currency_not_recorded"] == 2 and inv["currency_legacy_default_inr"] == 1 and inv["currency_recorded"] == 2
    assert r["read_only"] is True and "+9779" not in str(r) and "9876543210" not in str(r)
    assert {c: list(DB[c].find({})) for c in before} == before
