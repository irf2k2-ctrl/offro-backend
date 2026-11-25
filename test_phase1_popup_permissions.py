"""NOTE: file name sorts AFTER test_banner_media.py on purpose (it swaps the shared fake `database` at import).
Phase 1 / Task 5: popup-campaign admin API enforces the "Popup Campaigns" permission, the dashboard user's country and assigned cities.
Run:  python -m pytest test_phase1_popup_permissions.py -q -W ignore"""
import sys

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import server

fake = sys.modules["database"].db
c = TestClient(server.app)

FULL = {"view": True, "add": True, "edit": True, "delete": True}


def _h(tok):
    return {"x-admin-token": tok}


def _user(tok, role, perms=None, country=None, cities=("*",)):
    rid = fake.dashboard_roles.insert_one({"role_name": role, "permissions": perms if perms is not None else {}}).inserted_id
    d = {"token": tok, "status": "active", "full_name": tok, "mobile": "9" + str(abs(hash(tok)))[:9], "role_id": rid,
         "assigned_cities": list(cities)}
    if country is not None:
        d["country"] = country
    fake.dashboard_users.insert_one(d)
    return _h(tok)


def _city(name, country):
    fake.cities.insert_one({"name": name, "name_key": name.lower(), "country": country, "state": "", "status": "active", "active": True})


@pytest.fixture(autouse=True)
def _world():
    for col in ("dashboard_users", "dashboard_roles", "cities", "popup_campaigns", "accounts"):
        fake[col].delete_many({})
    for n in ("Mumbai", "Pune"):
        _city(n, "IN")
    for n in ("Riyadh", "Jeddah", "Madinah"):
        _city(n, "SA")
    _city("Amman", "JO")
    yield


BASE = {"name": "C", "start_dt": "2026-01-01T00:00", "end_dt": "2027-01-01T00:00"}


def _post(h, **kw):
    return c.post("/admin/popup-campaigns", json={**BASE, **kw}, headers=h)


def _names(h):
    r = c.get("/admin/popup-campaigns", headers=h)
    assert r.status_code == 200, r.text
    return {x["name"] for x in r.json()}


def _cid(name):
    return str(fake.popup_campaigns.find_one({"name": name})["_id"])


# ───────────── permissions ─────────────
def test_unauthenticated_and_unknown_token_are_401():
    assert c.get("/admin/popup-campaigns").status_code == 401
    assert c.get("/admin/popup-campaigns", headers=_h("nope")).status_code == 401


def test_each_action_needs_its_own_permission():
    sa = _user("sa", "Super Admin")
    cid = _post(sa, name="Global", target="all").json()["id"]
    none = _user("none", "Nobody", {})
    for call in (lambda: c.get("/admin/popup-campaigns", headers=none),
                 lambda: _post(none, target="all"),
                 lambda: c.put("/admin/popup-campaigns/" + cid, json={**BASE, "target": "all"}, headers=none),
                 lambda: c.patch("/admin/popup-campaigns/%s/toggle" % cid, json={"is_active": False}, headers=none),
                 lambda: c.delete("/admin/popup-campaigns/" + cid, headers=none)):
        assert call().status_code == 403
    assert fake.popup_campaigns.count_documents({}) == 1
    viewer = _user("viewer", "Viewer", {"Popup Campaigns": {"view": True}})
    assert c.get("/admin/popup-campaigns", headers=viewer).status_code == 200
    assert _post(viewer, target="all").status_code == 403
    assert c.delete("/admin/popup-campaigns/" + cid, headers=viewer).status_code == 403
    editor = _user("editor", "Editor", {"Popup Campaigns": {"view": True, "add": True, "edit": True}})
    assert _post(editor, name="By editor", target="all").status_code == 200
    assert c.put("/admin/popup-campaigns/" + cid, json={**BASE, "name": "Renamed", "target": "all"}, headers=editor).status_code == 200
    assert c.patch("/admin/popup-campaigns/%s/toggle" % cid, json={"is_active": False}, headers=editor).status_code == 200
    assert c.delete("/admin/popup-campaigns/" + cid, headers=editor).status_code == 403          # no delete permission
    deleter = _user("deleter", "Deleter", {"Popup Campaigns": {"delete": True}})
    assert c.delete("/admin/popup-campaigns/" + cid, headers=deleter).status_code == 200
    assert fake.popup_campaigns.find_one({"_id": ObjectId(cid)}) is None


def test_other_modules_permissions_do_not_grant_popup_access():
    h = _user("banners", "Banner mgr", {"Banners": dict(FULL), "Admin Banners": dict(FULL)})
    assert c.get("/admin/popup-campaigns", headers=h).status_code == 403


def test_user_with_missing_role_is_denied():
    fake.dashboard_users.insert_one({"token": "norole", "status": "active", "assigned_cities": ["*"]})
    assert c.get("/admin/popup-campaigns", headers=_h("norole")).status_code == 403


def test_disabled_user_is_403():
    h = _user("dis", "Super Admin")
    fake.dashboard_users.update_one({"token": "dis"}, {"$set": {"status": "disabled"}})
    assert c.get("/admin/popup-campaigns", headers=h).status_code == 403


# ───────────── Super Admin / unrestricted keep global access ─────────────
def test_super_admin_is_global_even_with_a_country_on_the_user():
    sa = _user("sa", "Super Admin", country="SA")
    _post(sa, name="G", target="all")
    _post(sa, name="IN", target="selected_city", countries=["IN"])
    _post(sa, name="SA", target="selected_city", countries=["SA"])
    assert _names(sa) == {"G", "IN", "SA"}


def test_unrestricted_role_user_without_country_and_with_star_sees_everything():
    h = _user("ops", "Ops", {"Popup Campaigns": dict(FULL)})
    _post(h, name="G", target="all")
    _post(h, name="IN", target="selected_city", countries=["IN"])
    assert _names(h) == {"G", "IN"}


# ───────────── country restriction ─────────────
def _world_campaigns():
    sa = _user("sa", "Super Admin")
    _post(sa, name="Global", target="all")
    _post(sa, name="India", target="selected_city", countries=["IN"])
    _post(sa, name="Saudi", target="selected_city", countries=["SA"])
    _post(sa, name="Saudi+Jordan", target="selected_city", countries=["SA", "JO"])
    _post(sa, name="Riyadh only", target="selected_city", city_targets=[{"country": "SA", "city": "Riyadh"}])
    fake.popup_campaigns.insert_one({"name": "Legacy cities", "target": "selected_city", "cities": ["Riyadh"], "is_active": True, "image_url": ""})
    fake.popup_campaigns.insert_one({"name": "Legacy all", "target": "all", "cities": [], "is_active": True, "image_url": ""})
    return sa


def test_country_restricted_admin_sees_only_campaigns_wholly_inside_their_country():
    _world_campaigns()
    h = _user("saadm", "Country admin", {"Popup Campaigns": dict(FULL)}, country="SA")
    assert _names(h) == {"Saudi", "Riyadh only"}                 # not Global, India, Saudi+Jordan, nor the legacy ones


def test_country_restricted_admin_cannot_change_global_foreign_or_legacy_campaigns():
    _world_campaigns()
    h = _user("saadm", "Country admin", {"Popup Campaigns": dict(FULL)}, country="SA")
    before = fake.popup_campaigns.count_documents({})
    for name in ("Global", "India", "Saudi+Jordan", "Legacy cities", "Legacy all"):
        cid = _cid(name)
        assert c.put("/admin/popup-campaigns/" + cid, json={**BASE, "name": "X", "target": "selected_city", "countries": ["SA"]}, headers=h).status_code == 403, name
        assert c.patch("/admin/popup-campaigns/%s/toggle" % cid, json={"is_active": False}, headers=h).status_code == 403, name
        assert c.delete("/admin/popup-campaigns/" + cid, headers=h).status_code == 403, name
        assert fake.popup_campaigns.find_one({"name": name}) is not None
    assert fake.popup_campaigns.count_documents({}) == before


def test_country_restricted_admin_can_manage_their_own_country_campaigns():
    _world_campaigns()
    h = _user("saadm", "Country admin", {"Popup Campaigns": dict(FULL)}, country="SA")
    assert _post(h, name="Mine", target="selected_city", countries=["SA"]).status_code == 200
    assert _post(h, name="Mine city", target="selected_city", city_targets=[{"country": "SA", "city": "Jeddah"}]).status_code == 200
    cid = _cid("Mine")
    assert c.put("/admin/popup-campaigns/" + cid, json={**BASE, "name": "Mine2", "target": "selected_city", "countries": ["SA"]}, headers=h).status_code == 200
    assert c.patch("/admin/popup-campaigns/%s/toggle" % cid, json={"is_active": False}, headers=h).status_code == 200
    assert c.delete("/admin/popup-campaigns/" + cid, headers=h).status_code == 200


def test_country_restricted_admin_cannot_create_or_widen_to_other_audiences():
    _world_campaigns()
    h = _user("saadm", "Country admin", {"Popup Campaigns": dict(FULL)}, country="SA")
    n = fake.popup_campaigns.count_documents({})
    assert _post(h, name="g", target="all").status_code == 403                                           # All Users
    assert _post(h, name="g2").status_code == 403                                                        # target defaults to "all"
    assert _post(h, name="in", target="selected_city", countries=["IN"]).status_code == 403
    assert _post(h, name="mix", target="selected_city", countries=["SA", "JO"]).status_code == 403
    assert _post(h, name="inc", target="selected_city", city_targets=[{"country": "IN", "city": "Mumbai"}]).status_code == 403
    assert _post(h, name="old", target="selected_city", cities=["Riyadh"]).status_code == 403            # older payload shape = no country
    assert _post(h, name="empty", target="selected_city", countries=[]).status_code == 403
    assert fake.popup_campaigns.count_documents({}) == n
    cid = _cid("Saudi")
    for body in ({"target": "all"}, {"target": "selected_city", "countries": ["SA", "IN"]}, {"target": "selected_city", "countries": ["JO"]}):
        assert c.put("/admin/popup-campaigns/" + cid, json={**BASE, **body}, headers=h).status_code == 403
    d = fake.popup_campaigns.find_one({"_id": ObjectId(cid)})
    assert d["countries"] == ["SA"] and d["target"] == "selected_city"                                    # nothing was widened


def test_restricted_admin_gets_404_for_unknown_campaign_and_400_for_bad_id():
    h = _user("saadm", "Country admin", {"Popup Campaigns": dict(FULL)}, country="SA")
    assert c.delete("/admin/popup-campaigns/" + str(ObjectId()), headers=h).status_code == 404
    assert c.delete("/admin/popup-campaigns/not-an-id", headers=h).status_code == 400


def test_corrupted_user_country_fails_closed():
    _world_campaigns()
    h = _user("bad", "Country admin", {"Popup Campaigns": dict(FULL)}, country="ZZ")
    assert _names(h) == set()
    assert _post(h, name="x", target="selected_city", countries=["SA"]).status_code == 403


# ───────────── assigned-city restriction ─────────────
def test_city_restricted_admin_may_only_target_assigned_cities():
    _world_campaigns()
    h = _user("city", "City admin", {"Popup Campaigns": dict(FULL)}, country="SA", cities=["Riyadh"])
    assert _names(h) == {"Riyadh only"}                                                   # "Saudi" targets the whole country
    assert _post(h, name="ok", target="selected_city", city_targets=[{"country": "SA", "city": "Riyadh"}]).status_code == 200
    assert _post(h, name="other city", target="selected_city", city_targets=[{"country": "SA", "city": "Jeddah"}]).status_code == 403
    assert _post(h, name="whole country", target="selected_city", countries=["SA"]).status_code == 403
    assert _post(h, name="two", target="selected_city",
                 city_targets=[{"country": "SA", "city": "Riyadh"}, {"country": "SA", "city": "Jeddah"}]).status_code == 403
    assert c.delete("/admin/popup-campaigns/" + _cid("Saudi"), headers=h).status_code == 403
    assert c.delete("/admin/popup-campaigns/" + _cid("Riyadh only"), headers=h).status_code == 200


def test_city_only_restricted_admin_without_country_handles_legacy_city_campaigns_inside_their_cities():
    fake.popup_campaigns.insert_one({"name": "Legacy mp", "target": "selected_city", "cities": ["Mumbai", "pune"], "is_active": True, "image_url": ""})
    fake.popup_campaigns.insert_one({"name": "Legacy mp+delhi", "target": "selected_city", "cities": ["Mumbai", "Delhi"], "is_active": True, "image_url": ""})
    fake.popup_campaigns.insert_one({"name": "Legacy all", "target": "all", "cities": [], "is_active": True, "image_url": ""})
    h = _user("mp", "City admin", {"Popup Campaigns": dict(FULL)}, cities=["Mumbai", "Pune"])
    assert _names(h) == {"Legacy mp"}
    assert c.patch("/admin/popup-campaigns/%s/toggle" % _cid("Legacy mp"), json={"is_active": False}, headers=h).status_code == 200
    assert c.patch("/admin/popup-campaigns/%s/toggle" % _cid("Legacy all"), json={"is_active": False}, headers=h).status_code == 403
    assert c.patch("/admin/popup-campaigns/%s/toggle" % _cid("Legacy mp+delhi"), json={"is_active": False}, headers=h).status_code == 403


def test_user_with_no_assigned_city_has_no_popup_access_to_records():
    _world_campaigns()
    h = _user("nocity", "City admin", {"Popup Campaigns": dict(FULL)}, cities=[])
    assert _names(h) == set()
    assert _post(h, name="x", target="selected_city", countries=["SA"]).status_code == 403


# ───────────── nothing changes for the public API / existing records ─────────────
def test_public_api_is_unchanged_and_needs_no_login():
    sa = _user("sa", "Super Admin")
    _post(sa, name="SA only", target="selected_city", countries=["SA"])
    _post(sa, name="Everyone", target="all")
    r = c.get("/public/popup-campaigns", params={"city": "Riyadh", "country": "SA"})
    assert r.status_code == 200 and {x["name"] for x in r.json()} == {"SA only", "Everyone"}
    r = c.get("/public/popup-campaigns", params={"city": "Mumbai", "country": "IN"})
    assert {x["name"] for x in r.json()} == {"Everyone"}
    assert {x["name"] for x in c.get("/public/popup-campaigns").json()} == {"SA only", "Everyone"}      # an older build: no params
