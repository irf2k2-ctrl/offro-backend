"""Temporary read-only store-status diagnostic + the three-interface consistency it reports.
NOTE: sorts after test_banner_media.py on purpose (that module swaps the shared fake `database` at import)."""
import sys
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient
import server

fake = sys.modules["database"].db
c = TestClient(server.app)
A = {"Authorization": "Bearer diag-super"}
URL = "/admin/diagnostics/store-status"


@pytest.fixture(autouse=True)
def _world():
    for col in ("stores", "subscriptions", "dashboard_users", "dashboard_roles", "accounts"):
        fake[col].delete_many({})
    rid = fake.dashboard_roles.insert_one({"role_name": "Super Admin", "is_system_role": True, "permissions": {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "diag-super", "status": "active", "full_name": "Root", "mobile": "9000000001", "role_id": rid})
    rid2 = fake.dashboard_roles.insert_one({"role_name": "Support", "permissions": {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "diag-support", "status": "active", "full_name": "Sup", "mobile": "9000000002", "role_id": rid2})
    yield


def _store(end, status="active", name="test store stage"):
    return fake.stores.insert_one({"store_name": name, "status": status, "subscription_end": end, "merchant_id": "m1", "city": "Pune"}).inserted_id


def _sub(sid, end, status="paid", as_oid=False, **extra):
    d = {"store_id": sid if as_oid else str(sid), "status": status, "plan": "Monthly", "end_date": end, "created_at": datetime.utcnow(),
         "razorpay_payment_id": "pay_SECRET", "razorpay_signature": "sig_SECRET", "customer_phone": "9999999999"}
    d.update(extra)
    return fake.subscriptions.insert_one(d).inserted_id


def test_requires_authentication_and_super_admin():
    assert c.get(URL, params={"name": "x"}).status_code == 401
    assert c.get(URL, params={"name": "x"}, headers={"Authorization": "Bearer diag-support"}).status_code == 403
    assert c.get(URL, headers=A).status_code == 400


def test_never_writes_and_never_leaks_payment_or_customer_fields():
    sid = _store(datetime.utcnow() - timedelta(days=40))
    _sub(sid, datetime.utcnow() + timedelta(days=10))
    before = (fake.stores.count_documents({}), fake.subscriptions.count_documents({}), fake.stores.find_one({"_id": sid}))
    r = c.get(URL, params={"name": "Test Store Stage"}, headers=A)
    assert r.status_code == 200 and r.json()["read_only"] is True
    after = (fake.stores.count_documents({}), fake.subscriptions.count_documents({}), fake.stores.find_one({"_id": sid}))
    assert before == after
    txt = r.text
    for secret in ("pay_SECRET", "sig_SECRET", "9999999999"):
        assert secret not in txt
    row = r.json()["stores"][0]["subscriptions"][0]
    assert "razorpay_payment_id" in row["other_field_names"]        # names only, never values


def test_stale_store_copy_is_reported_and_all_three_current_rules_agree():
    sid = _store(datetime.utcnow() - timedelta(days=40))
    _sub(sid, datetime.utcnow() + timedelta(days=10))
    d = c.get(URL, params={"store_id": str(sid)}, headers=A).json()
    s = d["stores"][0]
    assert s["final_status"] == {"admin_dashboard": "active", "customer_app": "active", "merchant_app": "active", "all_three_agree": True}
    assert s["rule_detail"]["merchant_app_pre_phase5_rule"]["status"] == "expired"      # what an un-redeployed backend would show
    assert any("pre-Phase-5" in h or "STALE" in h or "OLDER" in h for h in s["hints"])
    assert d["merchant_endpoint_uses_shared_rule"] is True
    assert s["store"]["merchant_id"] == "m1" and s["subscriptions"][0]["valid_now"] is True


def test_expired_everywhere_and_abandoned_pending_renewal():
    sid = _store(datetime.utcnow() - timedelta(days=40))
    _sub(sid, datetime.utcnow() - timedelta(days=5))
    _sub(sid, datetime.utcnow() + timedelta(days=300), status="pending")      # abandoned renewal must not extend anything
    s = c.get(URL, params={"name": "test store stage"}, headers=A).json()["stores"][0]
    assert s["final_status"]["admin_dashboard"] == "expired"
    assert s["final_status"]["merchant_app"] == "expired"
    assert s["final_status"]["customer_app"] == "hidden" and s["final_status"]["all_three_agree"] is True
    pend = [r for r in s["subscriptions"] if r["status"] == "pending"][0]
    assert pend["status_counts_as_paid"] is False and pend["valid_now"] is False


def test_store_id_stored_as_object_id_is_found():
    sid = _store(datetime.utcnow() + timedelta(days=3))
    _sub(sid, datetime.utcnow() + timedelta(days=3), as_oid=True)
    s = c.get(URL, params={"store_id": str(sid)}, headers=A).json()["stores"][0]
    assert s["subscriptions"][0]["store_id_type"] == "ObjectId" and s["subscriptions"][0]["store_id_matches"] is True
    assert s["final_status"]["all_three_agree"] is True and s["final_status"]["merchant_app"] == "active"


def test_unknown_store_returns_no_match():
    d = c.get(URL, params={"name": "nope"}, headers=A).json()
    assert d["matches"] == 0 and d["stores"] == []
