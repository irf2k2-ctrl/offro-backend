"""NOTE: file name sorts AFTER test_banner_media.py on purpose (it swaps the shared fake `database` at import).
Phase 1 / Task 3: country targeting of admin banners and admin-created promo sliders (admin side + public `country` parameter).
Run:  python -m pytest test_phase1_banner_countries.py -q -W ignore"""
import sys

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import server

fake = sys.modules["database"].db
c = TestClient(server.app)
FULL = {"view": True, "add": True, "edit": True, "delete": True}


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _user(tok, role, perms=None, country=None):
    rid = fake.dashboard_roles.insert_one({"role_name": role, "permissions": perms if perms is not None else {}}).inserted_id
    d = {"token": tok, "status": "active", "full_name": tok, "mobile": "9" + str(abs(hash(tok)))[:9], "role_id": rid, "assigned_cities": ["*"]}
    if country is not None:
        d["country"] = country
    fake.dashboard_users.insert_one(d)
    return _h(tok)


@pytest.fixture(autouse=True)
def _world():
    for col in ("dashboard_users", "dashboard_roles", "admin_banners", "promo_sliders", "accounts", "stores"):
        fake[col].delete_many({})
    fake.accounts.insert_one({"merchant_id": "m_in", "phone": "9876543210", "roles": ["merchant"]})
    fake.accounts.insert_one({"merchant_id": "m_sa", "phone": "+966501234567", "phone_country": "SA", "roles": ["merchant"]})
    yield


def _titles(rows):
    return sorted(r["title"] for r in rows)


def _banner(title, countries=None, **kw):
    d = {"title": title, "image_url": "u", "is_active": True, "sort_order": 0}
    if countries is not None:
        d["countries"] = countries
    d.update(kw)
    return str(fake.admin_banners.insert_one(d).inserted_id)


# ── public /admin-banners ───────────────────────────────────────────────────────────────────────────────────────
def test_public_banners_without_country_param_are_unchanged():
    _banner("legacy"); _banner("all", []); _banner("sa", ["SA"]); _banner("in", ["IN"])
    assert _titles(c.get("/admin-banners").json()) == ["all", "in", "legacy", "sa"]


def test_public_banners_with_country_show_global_and_own_only():
    _banner("legacy"); _banner("all", []); _banner("sa", ["SA"]); _banner("in", ["IN"]); _banner("both", ["IN", "SA"])
    assert _titles(c.get("/admin-banners?country=SA").json()) == ["all", "both", "legacy", "sa"]
    assert _titles(c.get("/admin-banners?country=in").json()) == ["all", "both", "in", "legacy"]
    assert _titles(c.get("/admin-banners?country=ZZ").json()) == ["all", "legacy"]      # unsupported code: global ones only


# ── public /promo-sliders ───────────────────────────────────────────────────────────────────────────────────────
def test_public_sliders_follow_targeting_then_owner_country():
    fake.promo_sliders.insert_many([
        {"title": "admin-global", "is_active": True, "source": "admin"},
        {"title": "admin-sa", "is_active": True, "source": "admin", "countries": ["SA"]},
        {"title": "merchant-in", "is_active": True, "source_banner_id": "x1", "merchant_id": "m_in"},
        {"title": "merchant-sa", "is_active": True, "source_banner_id": "x2", "merchant_id": "m_sa"},
        {"title": "merchant-phone-sa", "is_active": True, "source_banner_id": "x3", "merchant_phone": "+966500000001"},
        {"title": "merchant-legacy", "is_active": True, "source_banner_id": "x4"},
    ])
    assert _titles(c.get("/promo-sliders").json()) == sorted(["admin-global", "admin-sa", "merchant-in", "merchant-sa", "merchant-phone-sa", "merchant-legacy"])
    assert _titles(c.get("/promo-sliders?country=SA").json()) == ["admin-global", "admin-sa", "merchant-phone-sa", "merchant-sa"]
    assert _titles(c.get("/promo-sliders?country=IN").json()) == ["admin-global", "merchant-in", "merchant-legacy"]
    assert _titles(c.get("/promo-sliders?country=ZZ").json()) == ["admin-global"]


# ── admin side: super admin ─────────────────────────────────────────────────────────────────────────────────────
def test_super_admin_creates_updates_and_lists_countries():
    h = _user("sa", "Super Admin")
    r = c.post("/admin/banners", json={"title": "B", "image_url": "u", "countries": ["sa", "JO", "SA"]}, headers=h)
    assert r.status_code == 200
    bid = r.json()["id"]
    assert fake.admin_banners.find_one({"_id": ObjectId(bid)})["countries"] == ["SA", "JO"]
    assert c.get("/admin/banners", headers=h).json()[0]["countries"] == ["SA", "JO"]
    assert c.put("/admin/banners/" + bid, json={"countries": []}, headers=h).status_code == 200      # explicit "all countries"
    assert fake.admin_banners.find_one({"_id": ObjectId(bid)})["countries"] == []
    assert c.put("/admin/banners/" + bid, json={"title": "T2"}, headers=h).status_code == 200        # not sent: unchanged
    assert fake.admin_banners.find_one({"_id": ObjectId(bid)})["countries"] == []
    assert c.post("/admin/banners", json={"title": "B", "image_url": "u", "countries": ["ZZ"]}, headers=h).status_code == 400


def test_create_without_countries_is_all_countries_and_legacy_rows_list_as_all():
    h = _user("sa", "Super Admin")
    bid = c.post("/admin/banners", json={"title": "B", "image_url": "u"}, headers=h).json()["id"]
    assert fake.admin_banners.find_one({"_id": ObjectId(bid)})["countries"] == []
    _banner("legacy")
    rows = {r["title"]: r for r in c.get("/admin/banners", headers=h).json()}
    assert rows["legacy"]["countries"] == []


# ── admin side: country-restricted dashboard user ───────────────────────────────────────────────────────────────
def test_restricted_admin_only_sees_and_changes_own_country_banners():
    _banner("global"); _banner("all", []); _banner("sa", ["SA"]); _banner("in", ["IN"]); _banner("both", ["IN", "SA"])
    h = _user("jo", "Banner Mgr", {"Banners": FULL}, country="SA")
    assert _titles(c.get("/admin/banners", headers=h).json()) == ["sa"]
    ids = {d["title"]: str(d["_id"]) for d in fake.admin_banners.find()}
    for t in ("global", "all", "in", "both"):
        assert c.put("/admin/banners/" + ids[t], json={"title": "x"}, headers=h).status_code == 404
        assert c.delete("/admin/banners/" + ids[t], headers=h).status_code == 404
        assert fake.admin_banners.find_one({"_id": ObjectId(ids[t])}) is not None
    assert c.put("/admin/banners/" + ids["sa"], json={"title": "mine"}, headers=h).status_code == 200
    assert c.put("/admin/banners/" + ids["sa"], json={"countries": ["IN"]}, headers=h).status_code == 403
    assert c.put("/admin/banners/" + ids["sa"], json={"countries": []}, headers=h).status_code == 200   # restricted: stays own country
    assert fake.admin_banners.find_one({"_id": ObjectId(ids["sa"])})["countries"] == ["SA"]
    assert c.delete("/admin/banners/" + ids["sa"], headers=h).status_code == 200


def test_restricted_admin_create_is_forced_to_own_country():
    h = _user("jo", "Banner Mgr", {"Banners": FULL}, country="SA")
    bid = c.post("/admin/banners", json={"title": "B", "image_url": "u"}, headers=h).json()["id"]
    assert fake.admin_banners.find_one({"_id": ObjectId(bid)})["countries"] == ["SA"]
    assert c.post("/admin/banners", json={"title": "B", "image_url": "u", "countries": ["SA", "IN"]}, headers=h).status_code == 403


def test_banners_permission_is_still_required():
    h = _user("noperm", "Viewer", {"Banners": {"view": True}}, country="SA")
    assert c.post("/admin/banners", json={"title": "B", "image_url": "u"}, headers=h).status_code == 403


# ── admin promo sliders ─────────────────────────────────────────────────────────────────────────────────────────
def test_admin_promo_slider_countries_and_restricted_scope(monkeypatch):
    import routers.admin as adm
    monkeypatch.setattr(adm, "_cloudinary_upload", lambda v, folder="": v)
    sup = _user("sup", "Super Admin")
    sid = c.post("/admin/promo-sliders", json={"title": "S", "image_url": "u", "countries": ["SA"]}, headers=sup).json()["id"]
    assert fake.promo_sliders.find_one({"_id": ObjectId(sid)})["countries"] == ["SA"]
    fake.promo_sliders.insert_one({"title": "legacy", "image_url": "u", "source": "admin"})
    fake.promo_sliders.insert_one({"title": "m-in", "image_url": "u", "source_banner_id": "z", "merchant_id": "m_in"})
    fake.promo_sliders.insert_one({"title": "m-sa", "image_url": "u", "source_banner_id": "z2", "merchant_id": "m_sa"})
    assert _titles(c.get("/admin/promo-sliders", headers=sup).json()) == ["S", "legacy", "m-in", "m-sa"]
    h = _user("jo", "Banner Mgr", {"Banners": FULL}, country="SA")
    assert _titles(c.get("/admin/promo-sliders", headers=h).json()) == ["S", "m-sa"]
    other = str(fake.promo_sliders.find_one({"title": "m-in"})["_id"])
    assert c.put("/admin/promo-sliders/" + other, json={"title": "x"}, headers=h).status_code == 404
    assert c.delete("/admin/promo-sliders/" + other, headers=h).status_code == 404
    assert fake.promo_sliders.find_one({"_id": ObjectId(other)}) is not None
    mine = c.post("/admin/promo-sliders", json={"title": "M", "image_url": "u"}, headers=h).json()["id"]
    assert fake.promo_sliders.find_one({"_id": ObjectId(mine)})["countries"] == ["SA"]
