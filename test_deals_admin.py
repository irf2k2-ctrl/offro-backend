"""NOTE: file names sort AFTER test_banner_media.py on purpose - that module swaps the shared fake `database` at import, so tests importing `server` must be collected after it.
Admin Deals module (shared `deals` collection).
Run:  python -m pytest test_deals_admin.py -q -W ignore"""
import sys
from datetime import datetime, timedelta
import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
import server
import routers.merchant_app as MA
import routers.admin_deals as AD
from routers import public

MA._cloudinary_upload = lambda v, folder="": ("http://img/new.png" if v and not v.startswith("http") else v)
AD._cloudinary_upload = MA._cloudinary_upload

fake = sys.modules["database"].db
c = TestClient(server.app)
A = {"Authorization": "Bearer at"}
TODAY = datetime.utcnow() + timedelta(hours=5, minutes=30)
FUT = (TODAY + timedelta(days=20)).strftime("%Y-%m-%d")
PAST = (TODAY - timedelta(days=20)).strftime("%Y-%m-%d")
SOON = (TODAY + timedelta(days=1)).strftime("%Y-%m-%d")


def _role(name, perms=None, token="at"):
    rid = fake.dashboard_roles.insert_one({"role_name": name, "permissions": perms or {}}).inserted_id
    fake.dashboard_users.insert_one({"token": token, "status": "active", "full_name": name, "mobile": "9" + token.ljust(9, "0"),
                                     "role_id": rid, "assigned_cities": ["*"]})
    return rid


@pytest.fixture(autouse=True)
def _world():
    _role("Super Admin")
    m1 = fake.accounts.insert_one({"name": "Ravi Merchant", "phone": "9111111111", "roles": ["merchant"]}).inserted_id
    m2 = fake.accounts.insert_one({"name": "Sita Merchant", "phone": "9222222222", "roles": ["merchant"]}).inserted_id
    s1 = fake.stores.insert_one({"store_name": "Ravi Cafe", "city": "Pune", "status": "active", "merchant_id": str(m1)}).inserted_id
    s2 = fake.stores.insert_one({"store_name": "Sita Salon", "city": "Mumbai", "status": "active", "merchant_id": str(m2)}).inserted_id
    global M1, M2, S1, S2
    M1, M2, S1, S2 = str(m1), str(m2), str(s1), str(s2)
    yield


def _deal(store, merchant, title="Deal", **kw):
    d = {"merchant_id": merchant, "store_id": store, "title": title, "discount": 20, "category": "Food",
         "description": "desc " + title, "start_date": PAST, "end_date": FUT, "image_url": "http://img/a.png",
         "status": "active", "created_at": datetime.utcnow()}
    d.update(kw)
    return str(fake.deals.insert_one(d).inserted_id)


# ── list / display ──────────────────────────────────────────────────────────
def test_list_shows_every_required_field_with_merchant_and_store_names():
    i = _deal(S1, M1, "Coffee 20")
    r = c.get("/admin/deals", headers=A).json()
    assert r["total"] == 1
    row = r["items"][0]
    assert row["id"] == i and row["title"] == "Coffee 20" and row["description"] == "desc Coffee 20"
    assert row["merchant_name"] == "Ravi Merchant" and row["store_name"] == "Ravi Cafe" and row["city"] == "Pune"
    assert row["image_url"] == "http://img/a.png" and row["discount"] == 20
    assert row["start_date"] == PAST and row["end_date"] == FUT and row["status"] == "active"
    assert row["created_at_iso"].endswith("Z") and row["created_at"]
    assert r["merchants"] == [{"id": M1, "name": "Ravi Merchant"}]


def test_status_derivation_active_inactive_expired():
    _deal(S1, M1, "A")
    _deal(S1, M1, "P", status="paused")
    _deal(S1, M1, "E1", status="expired")
    _deal(S1, M1, "E2", end_date=PAST)
    by = {r["title"]: r["status"] for r in c.get("/admin/deals", headers=A).json()["items"]}
    assert by == {"A": "active", "P": "inactive", "E1": "expired", "E2": "expired"}


def test_filters_search_merchant_status_date():
    _deal(S1, M1, "Coffee"); _deal(S2, M2, "Haircut", status="paused")
    _deal(S2, M2, "Old", created_at=datetime(2020, 1, 5))
    g = lambda **q: sorted(r["title"] for r in c.get("/admin/deals", params=q, headers=A).json()["items"])
    assert g(search="coffee") == ["Coffee"]
    assert g(search="sita") == ["Haircut", "Old"]            # merchant name
    assert g(search="Ravi Cafe") == ["Coffee"]               # store name
    assert g(merchant_id=M2) == ["Haircut", "Old"]
    assert g(status="inactive") == ["Haircut"]
    assert g(status="active") == ["Coffee", "Old"]
    assert g(date_from="2020-01-01", date_to="2020-12-31") == ["Old"]
    assert g(date_from="2021-01-01") == ["Coffee", "Haircut"]
    assert g(merchant_id=M2, status="active") == ["Old"]


def test_bad_filter_values_rejected():
    assert c.get("/admin/deals", params={"status": "weird"}, headers=A).status_code == 400
    assert c.get("/admin/deals", params={"date_from": "yesterday"}, headers=A).status_code == 400


def test_pagination_and_newest_first():
    for n in range(7):
        _deal(S1, M1, f"d{n}", created_at=datetime(2026, 1, 1 + n))
    p1 = c.get("/admin/deals", params={"page": 1, "page_size": 3}, headers=A).json()
    p3 = c.get("/admin/deals", params={"page": 3, "page_size": 3}, headers=A).json()
    assert [r["title"] for r in p1["items"]] == ["d6", "d5", "d4"] and p1["total"] == 7 and p1["pages"] == 3
    assert [r["title"] for r in p3["items"]] == ["d0"]


def test_orphan_and_legacy_deals_do_not_break_listing():
    fake.deals.insert_one({"title": "bare", "status": "active"})                       # no store, no merchant, no dates
    _deal("not-an-object-id", "nope", "weird", created_at="garbage", end_date="soon")
    r = c.get("/admin/deals", headers=A)
    assert r.status_code == 200 and r.json()["total"] == 2


# ── detail ──────────────────────────────────────────────────────────────────
def test_detail_and_invalid_or_missing_id():
    i = _deal(S1, M1)
    assert c.get(f"/admin/deals/{i}", headers=A).json()["store_name"] == "Ravi Cafe"
    assert c.get("/admin/deals/zzz", headers=A).status_code == 400
    assert c.get(f"/admin/deals/{ObjectId()}", headers=A).status_code == 404


# ── edit ────────────────────────────────────────────────────────────────────
def test_edit_changes_the_shared_deal_and_the_customer_view():
    i = _deal(S1, M1, "Old title")
    r = c.put(f"/admin/deals/{i}", json={"title": " New title ", "discount": "35", "description": "d2",
                                          "end_date": SOON, "image_url": "data:image/png;base64,AAAA"}, headers=A)
    assert r.status_code == 200, r.text
    d = fake.deals.find_one({"_id": ObjectId(i)})
    assert d["title"] == "New title" and d["discount"] == 35 and d["end_date"] == SOON
    assert d["image_url"] == "http://img/new.png" and d["merchant_id"] == M1 and d["store_id"] == S1   # ownership untouched
    assert d["updated_by"] and fake.deals.count_documents({}) == 1                                       # no duplicate record
    assert fake.stores.find_one({"_id": ObjectId(S1)})["discount_percent"] == 35
    # the merchant app lists the very same document
    fake.accounts.insert_one({"token": "mt", "merchant_id": M1, "name": "Ravi", "phone": "9111111111", "roles": ["merchant"]})
    mine = c.get("/merchant/deals", headers={"Authorization": "Bearer mt"}).json()
    assert [x["title"] for x in mine] == ["New title"]


def test_edit_is_partial_and_keeps_image_when_not_sent():
    i = _deal(S1, M1, "T")
    assert c.put(f"/admin/deals/{i}", json={"description": "only this"}, headers=A).status_code == 200
    d = fake.deals.find_one({"_id": ObjectId(i)})
    assert d["description"] == "only this" and d["title"] == "T" and d["image_url"] == "http://img/a.png"


@pytest.mark.parametrize("body", [
    {"title": "   "}, {"title": "x" * 121}, {"discount": "abc"}, {"discount": 101}, {"discount": -1},
    {"discount": True}, {"start_date": "05/06/2026"}, {"end_date": "tomorrow"},
    {"start_date": FUT, "end_date": PAST}, {"description": "x" * 2001}, {}, {"image_url": "A" * 12_000_001},
])
def test_edit_validation_rejects_bad_input_and_changes_nothing(body):
    i = _deal(S1, M1, "T")
    before = fake.deals.find_one({"_id": ObjectId(i)})
    assert c.put(f"/admin/deals/{i}", json=body, headers=A).status_code == 400
    assert fake.deals.find_one({"_id": ObjectId(i)}) == before


def test_edit_cannot_reassign_merchant_store_status_or_created():
    i = _deal(S1, M1)
    c.put(f"/admin/deals/{i}", json={"title": "x", "merchant_id": M2, "store_id": S2, "status": "expired",
                                      "created_at": "1999-01-01"}, headers=A)
    d = fake.deals.find_one({"_id": ObjectId(i)})
    assert d["merchant_id"] == M1 and d["store_id"] == S1 and d["status"] == "active" and d["created_at"].year > 2000


# ── activate / deactivate ───────────────────────────────────────────────────
def test_deactivate_hides_from_customers_and_activate_restores():
    i = _deal(S1, M1, "Live")
    assert fake.deals.count_documents({"store_id": S1, "status": "active"}) == 1
    r = c.post(f"/admin/deals/{i}/status", json={"active": False}, headers=A)
    assert r.status_code == 200 and r.json()["deal"]["status"] == "inactive"
    assert fake.deals.count_documents({"store_id": S1, "status": "active"}) == 0     # customer queries filter on status == active
    r = c.post(f"/admin/deals/{i}/status", json={"active": True}, headers=A)
    assert r.json()["deal"]["status"] == "active" and fake.deals.find_one({"_id": ObjectId(i)})["status"] == "active"


def test_cannot_activate_an_expired_deal_and_bad_body_rejected():
    i = _deal(S1, M1, end_date=PAST)
    assert c.post(f"/admin/deals/{i}/status", json={"active": True}, headers=A).status_code == 400
    for bad in ({}, {"active": "yes"}, {"active": 1}):
        assert c.post(f"/admin/deals/{i}/status", json=bad, headers=A).status_code == 400
    assert c.post(f"/admin/deals/{i}/status", json={"active": False}, headers=A).status_code == 200


# ── delete ──────────────────────────────────────────────────────────────────
def test_delete_removes_only_that_deal():
    a, b = _deal(S1, M1, "a"), _deal(S1, M1, "b")
    assert c.delete(f"/admin/deals/{a}", headers=A).status_code == 200
    assert [d["title"] for d in fake.deals.find()] == ["b"]
    assert c.delete(f"/admin/deals/{a}", headers=A).status_code == 404
    assert c.delete("/admin/deals/xyz", headers=A).status_code == 400


# ── permissions ─────────────────────────────────────────────────────────────
def test_unauthenticated_requests_are_refused():
    i = _deal(S1, M1)
    for call in (lambda: c.get("/admin/deals"), lambda: c.get(f"/admin/deals/{i}"),
                 lambda: c.put(f"/admin/deals/{i}", json={"title": "x"}),
                 lambda: c.post(f"/admin/deals/{i}/status", json={"active": False}),
                 lambda: c.delete(f"/admin/deals/{i}")):
        assert call().status_code in (401, 403)
    assert fake.deals.count_documents({}) == 1


def test_role_without_the_deals_module_is_denied_everything():
    _role("Support", {"Stores": {"view": True, "edit": True, "delete": True}}, token="sup")
    H = {"Authorization": "Bearer sup"}
    i = _deal(S1, M1)
    assert c.get("/admin/deals", headers=H).status_code == 403
    assert c.put(f"/admin/deals/{i}", json={"title": "x"}, headers=H).status_code == 403
    assert c.delete(f"/admin/deals/{i}", headers=H).status_code == 403
    assert fake.deals.count_documents({}) == 1


def test_action_level_permissions_are_enforced_separately():
    _role("Viewer", {"Deals": {"view": True}}, token="vw")
    _role("Editor", {"Deals": {"view": True, "edit": True}}, token="ed")
    i = _deal(S1, M1)
    V, E = {"Authorization": "Bearer vw"}, {"Authorization": "Bearer ed"}
    assert c.get("/admin/deals", headers=V).status_code == 200
    assert c.put(f"/admin/deals/{i}", json={"title": "x"}, headers=V).status_code == 403
    assert c.post(f"/admin/deals/{i}/status", json={"active": False}, headers=V).status_code == 403
    assert c.put(f"/admin/deals/{i}", json={"title": "x"}, headers=E).status_code == 200
    assert c.delete(f"/admin/deals/{i}", headers=E).status_code == 403
    assert fake.deals.count_documents({}) == 1


def test_city_scoped_admin_sees_and_touches_only_their_cities():
    rid = fake.dashboard_roles.insert_one({"role_name": "CM", "permissions": {"Deals": {"view": True, "edit": True, "delete": True}}}).inserted_id
    fake.dashboard_users.insert_one({"token": "cm", "status": "active", "full_name": "CM", "mobile": "9000000009",
                                     "role_id": rid, "assigned_cities": ["Pune"]})
    H = {"Authorization": "Bearer cm"}
    mine, other = _deal(S1, M1, "pune"), _deal(S2, M2, "mumbai")
    assert [r["title"] for r in c.get("/admin/deals", headers=H).json()["items"]] == ["pune"]
    assert c.get(f"/admin/deals/{other}", headers=H).status_code == 403
    assert c.put(f"/admin/deals/{other}", json={"title": "x"}, headers=H).status_code == 403
    assert c.delete(f"/admin/deals/{other}", headers=H).status_code == 403
    assert fake.deals.count_documents({}) == 2
    assert c.put(f"/admin/deals/{mine}", json={"title": "ok"}, headers=H).status_code == 200


def test_deals_is_a_registered_rbac_module_and_wired_in_the_dashboard():
    from routers import dashboard_auth as DA
    assert "Deals" in DA.ALL_MODULES
    html = open("templates/admin_dashboard.html", encoding="utf-8").read()
    assert 'id="section-deals"' in html and "showSection('deals',this)" in html
    assert '"Deals":' in html and html.count('"Deals"') >= 2     # RBAC_SECTION_MAP + RBAC_MODULES


# ── store.discount_percent must never go stale ──────────────────────────────
def _disc(store=None):
    return fake.stores.find_one({"_id": ObjectId(store or S1)}).get("discount_percent", "ABSENT")


def test_deactivate_recalculates_or_removes_store_discount():
    a = _deal(S1, M1, "big", discount=50)
    b = _deal(S1, M1, "small", discount=10)
    fake.stores.update_one({"_id": ObjectId(S1)}, {"$set": {"discount_percent": 50}})
    c.post(f"/admin/deals/{a}/status", json={"active": False}, headers=A)
    assert _disc() == 10                                   # falls back to the remaining visible deal
    c.post(f"/admin/deals/{b}/status", json={"active": False}, headers=A)
    assert _disc() == "ABSENT"                             # nothing visible -> nothing left over
    c.post(f"/admin/deals/{a}/status", json={"active": True}, headers=A)
    assert _disc() == 50


def test_delete_recalculates_or_removes_store_discount():
    a = _deal(S1, M1, "big", discount=50)
    b = _deal(S1, M1, "small", discount=10)
    fake.stores.update_one({"_id": ObjectId(S1)}, {"$set": {"discount_percent": 50}})
    c.delete(f"/admin/deals/{a}", headers=A)
    assert _disc() == 10
    c.delete(f"/admin/deals/{b}", headers=A)
    assert _disc() == "ABSENT"


def test_expired_or_other_store_deals_do_not_keep_a_discount_alive():
    a = _deal(S1, M1, "live", discount=30)
    _deal(S1, M1, "gone", discount=90, end_date=PAST)      # expired by date
    other = _deal(S2, M2, "other", discount=70)
    fake.stores.update_one({"_id": ObjectId(S1)}, {"$set": {"discount_percent": 90}})
    c.put(f"/admin/deals/{a}", json={"discount": 30}, headers=A)
    assert _disc() == 30 and _disc(S2) == "ABSENT"         # S2 untouched by S1 edits
    c.delete(f"/admin/deals/{a}", headers=A)
    assert _disc() == "ABSENT"
    assert fake.deals.count_documents({"store_id": S2}) == 1 and other


def test_edit_that_lowers_or_expires_a_deal_updates_store_discount():
    a = _deal(S1, M1, "x", discount=60)
    c.put(f"/admin/deals/{a}", json={"discount": 15}, headers=A)
    assert _disc() == 15
    c.put(f"/admin/deals/{a}", json={"start_date": PAST, "end_date": PAST}, headers=A)
    assert _disc() == "ABSENT"
