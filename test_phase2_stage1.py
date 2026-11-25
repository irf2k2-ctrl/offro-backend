"""NOTE: file name sorts AFTER test_banner_media.py on purpose (it swaps the shared fake `database` at import).
Phase 2 / Stage 1: Task A (read-only country diagnostics + shared resolver) and Task B (Payments permission on payment endpoints).
Run:  python -m pytest test_phase2_stage1.py -q -W ignore"""
import copy
import sys

import pytest
from fastapi.testclient import TestClient

import server
from services import country_diagnostics as D
from utils import countries as C
from utils import phone as PH

fake = sys.modules["database"].db
c = TestClient(server.app)
VIEW = {"view": True}


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _user(tok, role, perms=None, country=None, cities=("*",)):
    rid = fake.dashboard_roles.insert_one({"role_name": role, "permissions": perms if perms is not None else {}}).inserted_id
    d = {"token": tok, "status": "active", "full_name": tok, "mobile": "9" + str(abs(hash(tok)))[:9], "role_id": rid,
         "assigned_cities": list(cities)}
    if country is not None:
        d["country"] = country
    fake.dashboard_users.insert_one(d)
    return _h(tok)


def _old_account_country(account):
    """The pre-Stage-1 implementation of utils.countries.account_country, copied verbatim, to prove the wrapper is identical."""
    acct = account or {}
    iso = str(acct.get("phone_country") or "").strip().upper()
    info = C.country_info(iso) if iso else None
    if info:
        return info
    phone = str(acct.get("phone") or "").strip()
    if not phone:
        return None
    try:
        derived = PH.phone_fields(phone).get("phone_country", "")
    except Exception:
        derived = ""
    return C.country_info(derived) if derived else None


SAMPLES = [
    {}, {"phone": ""}, {"phone": "9876543210"}, {"phone": "919876543210"}, {"phone": "09876543210"}, {"phone": "+919876543210"},
    {"phone": "+966501234567"}, {"phone": "00966501234567"}, {"phone": "+9779812345678"}, {"phone": "+639171234567"},
    {"phone": "+962791234567"}, {"phone": "+971501234567"}, {"phone": "+442071838750"}, {"phone": "5551234567"},
    {"phone": "501234567"}, {"phone": "0501234567"}, {"phone": "abc"}, {"phone": "+915551234567"},
    {"phone": "+966112345678"}, {"phone_country": "SA", "phone": "+966501234567"}, {"phone_country": "sa", "phone": "x"},
    {"phone_country": "XX", "phone": "9876543210"}, {"phone_country": "XX", "phone": "+966501234567"},
    {"phone_country": "XX"}, {"phone_country": "IN", "phone": "+966501234567"}, {"phone_country": None, "phone": "+639171234567"},
    {"phone": 9876543210}, {"phone": "  +966501234567  "},
]


@pytest.mark.parametrize("acct", SAMPLES)
def test_shared_resolver_keeps_account_country_exactly(acct):
    assert C.account_country(copy.deepcopy(acct)) == _old_account_country(copy.deepcopy(acct))


def test_resolver_returns_source():
    r = C.resolve_account_country
    assert r({"phone_country": "SA", "phone": "x"}) == {"iso": "SA", "source": "explicit", "reason": ""}
    assert r({"phone": "+639171234567"})["source"] == "parsed" and r({"phone": "+639171234567"})["iso"] == "PH"
    assert r({"phone": "9876543210"}) == {"iso": "IN", "source": "legacy_india", "reason": ""}
    assert r({"phone": "919876543210"})["source"] == "legacy_india"
    u = r({"phone": "+971501234567"})
    assert u["iso"] == "" and u["source"] == "unresolved" and u["reason"] == "unsupported_country"
    assert r({})["reason"] == "no_phone"
    assert r({"phone": "501234567"})["source"] == "unresolved"       # a bare non-Indian number is NOT taken as India


def test_classify_flags():
    assert D.classify({"phone_country": "IN", "phone": "+966501234567"})["issues"] == ["country_mismatch"]
    assert D.classify({"phone_country": "SA", "phone": "+966501234567"})["issues"] == []
    assert D.classify({"phone_country": "SA", "phone": "x", "phone_e164": "+639171234567"})["issues"] == ["country_mismatch"]
    assert D.classify({"phone_country": "SA", "phone": "+971501234567"})["issues"] == ["country_mismatch"]   # unsupported prefix
    assert D.classify({"phone_country": "XX", "phone": "+966501234567"})["issues"] == ["invalid_phone_country"]
    assert D.classify({"phone": "9876543210"})["issues"] == ["legacy_india"]
    assert D.classify({"phone": "501234567"})["issues"] == ["unresolved"]
    assert D.classify({"phone": "+919876543210"})["issues"] == []


def test_mask_phone():
    assert D.mask_phone("+966501234567") == "+96" + "•" * 8 + "67"
    assert D.mask_phone("") == "" and D.mask_phone("12345") == "•••••"
    assert "50123" not in D.mask_phone("+966501234567")


@pytest.fixture(autouse=True)
def _world():
    for col in ("dashboard_users", "dashboard_roles", "accounts", "invoices", "subscriptions", "merchant_banners",
                "product_orders"):
        fake[col].delete_many({})
    fake.accounts.insert_many([
        {"name": "Asha", "phone": "9876543210", "roles": ["user"], "city": "Pune"},                                   # legacy India
        {"name": "Ravi", "phone": "+919812345678", "phone_country": "IN", "roles": ["merchant"], "city": "Pune"},      # explicit India
        {"name": "Sara", "phone": "+966501234567", "phone_country": "SA", "phone_e164": "+966501234567", "roles": ["user", "merchant"]},
        {"name": "Bad", "phone": "+966509999999", "phone_country": "IN", "roles": ["user"]},                           # mismatch (IN vs +966)
        {"name": "Nep", "phone": "+9779812345678", "roles": ["influencer"]},                                          # parsed
        {"name": "Ghost", "phone": "501234567", "roles": ["user"]},                                                   # unresolved
        {"name": "Uae", "phone": "+971501234567", "roles": ["merchant"]},                                             # unresolved (unsupported)
    ])
    yield


def _get(h, **q):
    return c.get("/admin/country-diagnostics", params=q, headers=h)


def _snapshot():
    return {n: sorted(map(str, fake[n].find({}))) for n in ("accounts", "invoices", "subscriptions", "activity_logs")}   # not dashboard_users: the existing session check only stamps last_active_at


# ── Task A ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_super_admin_sees_summary_and_full_numbers():
    h = _user("sa1", "Super Admin")
    r = _get(h); assert r.status_code == 200
    j = r.json(); s = j["summary"]
    assert j["masked"] is False
    assert s["total"] == 7
    assert s["by_source"] == {"explicit": 3, "parsed": 1, "legacy_india": 1, "unresolved": 2}
    assert s["by_issue"] == {"unresolved": 2, "country_mismatch": 1, "invalid_phone_country": 0, "legacy_india": 1}
    assert s["by_role"]["customer"]["total"] == 4 and s["by_role"]["merchant"]["total"] == 3 and s["by_role"]["influencer"]["total"] == 1
    assert s["by_role"]["customer"]["by_source"]["unresolved"] == 1
    assert s["by_country"] == {"IN": 3, "SA": 1, "NP": 1, "unresolved": 2}
    assert "9876543210" in [i["phone"] for i in j["items"]]


def test_filters():
    h = _user("sa1", "Super Admin")
    names = lambda **q: sorted(i["name"] for i in _get(h, **q).json()["items"])
    assert names(issue="unresolved") == ["Ghost", "Uae"]
    assert names(issue="country_mismatch") == ["Bad"]
    assert names(issue="legacy_india") == ["Asha"]
    assert names(source="parsed") == ["Nep"]
    assert names(country="unresolved") == ["Ghost", "Uae"]
    assert names(country="sa") == ["Sara"]
    assert names(role="influencer") == ["Nep"]
    assert names(role="merchant", source="unresolved") == ["Uae"]
    # the summary does not move with the list filters
    assert _get(h, issue="unresolved").json()["summary"]["total"] == 7
    assert _get(h, issue="unresolved").json()["total"] == 2
    for bad in ({"country": "ZZ"}, {"source": "x"}, {"issue": "x"}, {"role": "x"}):
        assert _get(h, **bad).status_code == 422


def test_paging():
    h = _user("sa1", "Super Admin")
    j = _get(h, page_size=3, page=3).json()
    assert j["pages"] == 3 and len(j["items"]) == 1 and j["total"] == 7


def test_needs_accounts_view_permission():
    assert _get(_user("none", "Nobody", {})).status_code == 403
    assert _get(_user("fin", "Finance", {"Payments": VIEW})).status_code == 403
    assert _get(_user("acc", "Accounts", {"Accounts": VIEW})).status_code == 200
    assert c.get("/admin/country-diagnostics").status_code in (401, 403)


def test_unrestricted_non_super_admin_sees_full_numbers_restricted_sees_masked():
    full = _get(_user("acc", "Accounts", {"Accounts": VIEW})).json()
    assert full["masked"] is False
    city = _get(_user("cty", "Accounts", {"Accounts": VIEW}, cities=("Pune",))).json()
    assert city["masked"] is True and {i["name"] for i in city["items"]} == {"Asha", "Ravi"}
    assert all("•" in i["phone"] for i in city["items"])


def test_country_restricted_admin_scope_and_masking():
    h = _user("sa_admin", "Accounts", {"Accounts": VIEW}, country="SA")
    j = _get(h).json()
    assert j["masked"] is True and j["scope"] == "SA"
    assert [i["name"] for i in j["items"]] == ["Sara"]
    assert j["summary"]["total"] == 1 and j["summary"]["by_source"]["unresolved"] == 0       # unresolved accounts are hidden
    assert "+966501234567" not in str(j) and "501234567" not in str(j)
    jin = _get(_user("in_admin", "Accounts", {"Accounts": VIEW}, country="IN")).json()
    assert sorted(i["name"] for i in jin["items"]) == ["Asha", "Bad", "Ravi"]     # explicit IN (incl. the mismatched one) + legacy India
    assert _get(_user("in2", "Accounts", {"Accounts": VIEW}, country="IN"), country="SA").json()["items"] == []
    assert _get(_user("bad", "Accounts", {"Accounts": VIEW}, country="XX")).json()["items"] == []   # corrupt scope fails closed


def test_diagnostics_never_write():
    h = _user("sa1", "Super Admin")
    before = _snapshot()
    for q in ({}, {"issue": "unresolved"}, {"country": "IN", "source": "legacy_india"}):
        assert _get(h, **q).status_code == 200
    assert _snapshot() == before
    for m in (c.post, c.put, c.delete, c.patch):
        assert m("/admin/country-diagnostics", headers=h).status_code == 405


# ── Task B ────────────────────────────────────────────────────────────────────────────────────────────────────────
PAY_URLS = ["/admin/subscriptions", "/admin/merchant-transactions", "/admin/invoices/full", "/admin/products/revenue"]


@pytest.fixture
def _money():
    fake.accounts.delete_many({})
    fake.accounts.insert_many([
        {"merchant_id": "m_in", "phone": "9876543210", "roles": ["merchant"]},
        {"merchant_id": "m_sa", "phone": "+966501234567", "phone_country": "SA", "roles": ["merchant"]},
    ])
    fake.invoices.insert_many([
        {"invoice_no": "IN1", "merchant_id": "m_in", "merchant_phone": "9876543210", "total": 118, "base_price": 100, "gst": 18},
        {"invoice_no": "SA1", "merchant_id": "m_sa", "merchant_phone": "+966501234567", "total": 115, "base_price": 100, "gst": 15},
    ])
    fake.subscriptions.insert_many([
        {"merchant_id": "m_in", "merchant_phone": "9876543210", "plan": "p", "total": 118, "status": "paid"},
        {"merchant_id": "m_sa", "merchant_phone": "+966501234567", "plan": "p", "total": 115, "status": "paid"},
    ])
    fake.product_orders.insert_many([
        {"merchant_id": "m_in", "status": "paid", "total": 50, "created_at": __import__("datetime").datetime.utcnow()},
        {"merchant_id": "m_sa", "status": "paid", "total": 70, "created_at": __import__("datetime").datetime.utcnow()},
    ])


@pytest.mark.parametrize("url", PAY_URLS)
def test_payment_endpoints_need_payments_view(url, _money):
    assert c.get(url).status_code in (401, 403)
    assert c.get(url, headers=_user("n1", "Nobody", {})).status_code == 403
    assert c.get(url, headers=_user("acc1", "Accounts", {"Accounts": VIEW, "Products": VIEW, "Stores": VIEW})).status_code == 403
    assert c.get(url, headers=_user("pe", "EditOnly", {"Payments": {"edit": True}})).status_code == 403
    assert c.get(url, headers=_user("fin1", "Finance", {"Payments": VIEW})).status_code == 200
    assert c.get(url, headers=_user("sup1", "Super Admin")).status_code == 200


def test_super_admin_gets_everything_unchanged(_money):
    h = _user("sup1", "Super Admin")
    assert sorted(i["invoice_no"] for i in c.get("/admin/invoices/full", headers=h).json()) == ["IN1", "SA1"]
    assert sorted(i["invoice_no"] for i in c.get("/admin/merchant-transactions", headers=h).json()) == ["IN1", "SA1"]
    assert len(c.get("/admin/subscriptions", headers=h).json()) == 2
    j = [i for i in c.get("/admin/invoices/full", headers=h).json() if i["invoice_no"] == "SA1"][0]
    assert j["total"] == 115 and j["gst"] == 15                                            # amounts untouched


def test_country_restricted_payment_viewer_sees_only_own_country(_money):
    for country, only in (("SA", "SA1"), ("IN", "IN1")):
        h = _user("fin_" + country, "Finance", {"Payments": VIEW}, country=country)
        assert [i["invoice_no"] for i in c.get("/admin/invoices/full", headers=h).json()] == [only]
        assert [i["invoice_no"] for i in c.get("/admin/merchant-transactions", headers=h).json()] == [only]
        assert len(c.get("/admin/subscriptions", headers=h).json()) == 1
    rev = lambda h: c.get("/admin/products/revenue", headers=h).json()["lifetime"]
    assert rev(_user("rv_sa", "Finance", {"Payments": VIEW}, country="SA")) == 70
    assert rev(_user("rv_in", "Finance", {"Payments": VIEW}, country="IN")) == 50
    assert rev(_user("rv_all", "Finance", {"Payments": VIEW})) == 120
    bad = _user("fin_bad", "Finance", {"Payments": VIEW}, country="XX")                   # corrupt scope fails closed
    assert c.get("/admin/invoices/full", headers=bad).json() == []
    assert c.get("/admin/products/revenue", headers=bad).json()["lifetime"] == 0
