"""Admin -> Cities: multi-country City Master (fields, popular order, duplicates, permissions, import/verify, public /cities).
(File name sorts after test_banner_media.py on purpose.)  Run:  python -m pytest test_cities_master_admin.py -q -W ignore"""
import json
import os
import sys
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import admin as ADM
from routers import admin_cities as ADMC
from routers import public as PUB

DB = sys.modules["database"].db
app = FastAPI()
app.include_router(ADMC.router, prefix="/admin")
app.include_router(ADM.router, prefix="/admin")
app.include_router(PUB.router)
web = TestClient(app)
REF = os.path.join(os.path.dirname(os.path.abspath(ADMC.__file__)), "..", "services", "city_reference")


def user(token, role_name="Custom", perms=None, system=False, country=None):
    rid = DB.dashboard_roles.insert_one({"role_name": role_name, "permissions": perms or {}, "is_system_role": system}).inserted_id
    d = {"token": token, "role_id": rid, "full_name": token, "status": "active", "mobile": "9000000001", "last_active_at": datetime.utcnow()}
    if country:
        d["country"] = country
    DB.dashboard_users.insert_one(d)
    return {"Authorization": "Bearer " + token}


def sa():
    return user("sa-token", "Super Admin", system=True)


def city(name, country, state="", order=0, **kw):
    d = {"name": name, "sort_order": order, "active": True, "status": "active", "image_url": "", "state": state}
    if country is not None:
        d["country"] = country
    d.update(kw)
    return str(DB.cities.insert_one(d).inserted_id)


@pytest.fixture(autouse=True)
def world():
    for c in ("cities", "dashboard_users", "dashboard_roles", "stores", "accounts"):
        getattr(DB, c).delete_many({})
    yield


def post(body, h=None):
    return web.post("/admin/cities", json=body, headers=h or sa())


def pub(country="", lang=""):
    q = "/cities?" + ("country=%s&" % country if country else "") + ("lang=%s" % lang if lang else "")
    return web.get(q).json()


# ── create / validation ────────────────────────────────────────────────────────────────────────────────────
def test_create_requires_a_country_and_validates_it():
    h = sa()
    assert post({"name": "Riyadh"}, h).status_code == 400
    r = post({"name": "Riyadh", "country": "XX"}, h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "country_unsupported"


def test_create_stores_state_arabic_popular_and_audit_fields():
    h = sa()
    r = post({"country": "sa", "name": "Khobar", "name_ar": "الخبر", "state": "Eastern Province", "sort_order": 40, "is_popular": True,
              "popular_order": 6, "active": True}, h)
    assert r.status_code == 200
    d = DB.cities.find_one({"name": "Khobar"})
    assert d["country"] == "SA" and d["state"] == "Eastern Province" and d["names"] == {"ar": "الخبر"}
    assert d["is_popular"] is True and d["popular_order"] == 6 and d["name_key"] == "khobar" and d["source"] == "admin"
    assert d["status"] == "active" and d["updated_by"] == "sa-token"


@pytest.mark.parametrize("body,code", [
    ({"name": ""}, "name_required"), ({"name": "X"}, "name_length"), ({"name": "Riy@dh"}, "name_chars"),
    ({"name": "Yanbu", "name_ar": "ينبع123"}, "arabic_digits"), ({"name": "Yanbu", "name_ar": "Yanbu"}, "arabic_invalid"),
    ({"name": "Yanbu", "state": "x" * 61}, "state_length"), ({"name": "Yanbu", "sort_order": -1}, "sort_order_invalid"),
    ({"name": "Yanbu", "is_popular": True, "popular_order": 0}, "popular_order_invalid"),
])
def test_validation_codes(body, code):
    r = post({"country": "SA", **body})
    assert r.status_code == 400 and r.json()["detail"]["code"] == code
    assert DB.cities.count_documents({}) == 0


def test_arabic_name_is_optional_and_other_languages_still_work():
    assert post({"country": "SA", "name": "Duba"}).status_code == 200
    assert "names" not in DB.cities.find_one({"name": "Duba"})
    assert post({"country": "IN", "name": "Mysuru", "names": {"kn": "ಮೈಸೂರು"}}).status_code == 200
    assert DB.cities.find_one({"name": "Mysuru"})["names"] == {"kn": "ಮೈಸೂರು"}


# ── duplicates ─────────────────────────────────────────────────────────────────────────────────────────────
def test_duplicate_same_country_name_and_state_is_refused_case_insensitively():
    h = sa()
    assert post({"country": "SA", "name": "Khobar", "state": "Eastern Province"}, h).status_code == 200
    r = post({"country": "SA", "name": "khobar", "state": "Eastern Province"}, h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "duplicate_city"
    assert "Khobar already exists in Saudi Arabia (Eastern Province)" in r.json()["detail"]["message"]
    assert DB.cities.count_documents({}) == 1


def test_same_name_in_two_states_is_allowed():
    h = sa()
    assert post({"country": "IN", "name": "Udaipur", "state": "Rajasthan"}, h).status_code == 200
    assert post({"country": "IN", "name": "Udaipur", "state": "Tripura"}, h).status_code == 200
    assert post({"country": "IN", "name": "Udaipur", "state": "Tripura"}, h).status_code == 409


def test_a_city_without_a_state_matches_the_same_name_in_that_country():
    city("Ballari", "IN", "")
    assert post({"country": "IN", "name": "Ballari", "state": "Karnataka"}).status_code == 409


def test_same_name_in_another_country_is_allowed():
    city("Riyadh", "SA", "Riyadh Province")
    assert post({"country": "JO", "name": "Riyadh", "state": "Amman Governorate"}).status_code == 200


def test_an_unassigned_legacy_city_blocks_a_twin_and_says_to_assign_it():
    city("Ballari", None, "")
    r = post({"country": "IN", "name": "Ballari"})
    assert r.status_code == 409 and "Assign it to India" in r.json()["detail"]["message"]


# ── popular ordering ───────────────────────────────────────────────────────────────────────────────────────
def test_popular_without_order_goes_to_the_end_and_a_taken_order_shifts_the_others_down():
    h = sa()
    post({"country": "SA", "name": "Riyadh", "is_popular": True}, h)
    post({"country": "SA", "name": "Jeddah", "is_popular": True}, h)
    post({"country": "SA", "name": "Makkah", "is_popular": True, "popular_order": 1}, h)
    order = {d["name"]: d["popular_order"] for d in DB.cities.find({"is_popular": True})}
    assert order == {"Makkah": 1, "Riyadh": 2, "Jeddah": 3}


def test_unflagging_renumbers_and_clears_the_order():
    h = sa()
    ids = [post({"country": "SA", "name": n, "is_popular": True}, h).json()["id"] for n in ("Riyadh", "Jeddah", "Makkah")]
    assert web.put("/admin/cities/" + ids[0], json={"is_popular": False}, headers=h).status_code == 200
    assert "popular_order" not in DB.cities.find_one({"name": "Riyadh"}) and DB.cities.find_one({"name": "Riyadh"})["is_popular"] is False
    assert {d["name"]: d["popular_order"] for d in DB.cities.find({"is_popular": True})} == {"Jeddah": 1, "Makkah": 2}


def test_popular_order_endpoint_is_exact_and_unflags_the_rest():
    h = sa()
    ids = {n: post({"country": "SA", "name": n, "is_popular": True}, h).json()["id"] for n in ("Riyadh", "Jeddah", "Makkah")}
    other = city("Mumbai", "IN", "Maharashtra", is_popular=True, popular_order=1)
    r = web.put("/admin/cities/popular-order", json={"country": "SA", "ids": [ids["Makkah"], ids["Riyadh"]]}, headers=h)
    assert r.status_code == 200 and r.json()["popular"] == 2 and r.json()["unflagged"] == 1
    assert {d["name"]: d.get("popular_order") for d in DB.cities.find({"country": "SA"})} == {"Makkah": 1, "Riyadh": 2, "Jeddah": None}
    assert DB.cities.find_one({"name": "Jeddah"})["is_popular"] is False
    assert DB.cities.find_one({"name": "Mumbai"})["is_popular"] is True              # another country is untouched
    bad = web.put("/admin/cities/popular-order", json={"country": "SA", "ids": [other]}, headers=h)
    assert bad.status_code == 400 and DB.cities.find_one({"name": "Makkah"})["popular_order"] == 1     # nothing half-applied


# ── public /cities ─────────────────────────────────────────────────────────────────────────────────────────
def test_public_list_is_filtered_by_country_for_in_sa_np_jo_and_never_crosses():
    for n, c in (("Mumbai", "IN"), ("Riyadh", "SA"), ("Kathmandu", "NP"), ("Amman", "JO")):
        city(n, c)
    city("Ballari", None)
    for c, n in (("IN", "Mumbai"), ("SA", "Riyadh"), ("NP", "Kathmandu"), ("JO", "Amman")):
        assert [x["name"] for x in pub(c)] == [n]
    assert pub("XX") == []


def test_empty_country_is_an_empty_list_with_http_200_not_an_error():
    city("Mumbai", "IN")
    r = web.get("/cities?country=NP")
    assert r.status_code == 200 and r.json() == []


def test_public_list_carries_state_and_popular_fields_and_popular_comes_first():
    city("Dhahran", "SA", "Eastern Province", 1)
    city("Jeddah", "SA", "Makkah Province", 2, is_popular=True, popular_order=2)
    city("Riyadh", "SA", "Riyadh Province", 3, is_popular=True, popular_order=1)
    r = pub("SA", "ar")
    assert [x["name"] for x in r] == ["Riyadh", "Jeddah", "Dhahran"]
    assert r[0]["is_popular"] is True and r[0]["popular_order"] == 1 and r[0]["state"] == "Riyadh Province"
    assert r[2]["is_popular"] is False and r[2]["popular_order"] is None


def test_without_any_flag_the_public_order_is_exactly_the_old_one():
    for i, n in enumerate(("Mysuru", "Ballari", "Hubli")):
        city(n, "IN", "", order=[2, 1, 2][i])
    assert [x["name"] for x in pub("IN")] == ["Ballari", "Mysuru", "Hubli"]          # sort_order, ties keep their stored order


def test_inactive_cities_are_hidden_from_the_app_but_listed_for_the_admin():
    city("Riyadh", "SA"); city("Jeddah", "SA", status="inactive", active=False)
    assert [x["name"] for x in pub("SA")] == ["Riyadh"]
    rows = web.get("/admin/cities?country=SA", headers=sa()).json()
    assert [r["name"] for r in rows] == ["Jeddah", "Riyadh"]                              # alphabetical, includes inactive
    assert web.get("/admin/cities?country=SA&status=inactive", headers=sa()).json()[0]["name"] == "Jeddah"


def test_arabic_name_falls_back_to_english_in_the_public_list():
    city("Riyadh", "SA", names={"ar": "الرياض"}); city("Duba", "SA")
    got = {x["name"]: x["display_name"] for x in pub("SA", "ar")}
    assert got == {"Riyadh": "الرياض", "Duba": "Duba"}


# ── admin list, status, edit, delete ───────────────────────────────────────────────────────────────────────
def test_admin_list_filters_and_summary():
    h = sa()
    city("Riyadh", "SA", "Riyadh Province", names={"ar": "الرياض"}, is_popular=True, popular_order=1)
    city("Jeddah", "SA", "Makkah Province"); city("Mumbai", "IN", "Maharashtra"); city("Ballari", None)
    assert [r["name"] for r in web.get("/admin/cities?country=SA&q=الرياض", headers=h).json()] == ["Riyadh"]
    assert [r["name"] for r in web.get("/admin/cities?country=SA&q=makkah", headers=h).json()] == ["Jeddah"]
    assert [r["name"] for r in web.get("/admin/cities?country=SA&popular=true", headers=h).json()] == ["Riyadh"]
    assert [r["name"] for r in web.get("/admin/cities?country=unassigned", headers=h).json()] == ["Ballari"]
    s = web.get("/admin/cities/summary", headers=h).json()
    by = {c["code"]: c for c in s["countries"]}
    assert by["SA"]["total"] == 2 and by["SA"]["popular"] == 1 and by["IN"]["total"] == 1 and s["unassigned"] == 1
    assert {"IN", "SA", "NP", "JO"} <= set(by)


def test_edit_changes_only_what_is_sent_and_never_touches_the_image():
    h = sa()
    cid = city("Mumbai", "IN", "Maharashtra", 3, image_url="data:image/png;base64,AAAA", names={"ar": "مومباي"})
    r = web.put("/admin/cities/" + cid, json={"state": "Maharashtra State"}, headers=h)
    assert r.status_code == 200
    d = DB.cities.find_one({"name": "Mumbai"})
    assert d["image_url"] == "data:image/png;base64,AAAA" and d["names"] == {"ar": "مومباي"} and d["sort_order"] == 3 and d["state"] == "Maharashtra State"
    assert web.put("/admin/cities/" + cid, json={}, headers=h).status_code == 400
    assert web.put("/admin/cities/" + cid, json={"name_ar": ""}, headers=h).status_code == 200
    assert "ar" not in DB.cities.find_one({"name": "Mumbai"}).get("names", {})


def test_edit_to_a_duplicate_is_refused_and_activate_deactivate_works():
    h = sa()
    a = city("Riyadh", "SA", "Riyadh Province"); b = city("Jeddah", "SA", "Makkah Province")
    assert web.put("/admin/cities/" + b, json={"name": "Riyadh", "state": "Riyadh Province"}, headers=h).status_code == 409
    r = web.post("/admin/cities/" + a + "/status", json={"active": False}, headers=h)
    assert r.status_code == 200 and DB.cities.find_one({"name": "Riyadh"})["status"] == "inactive"
    assert web.post("/admin/cities/" + a + "/status", json={"active": True}, headers=h).status_code == 200
    assert DB.cities.find_one({"name": "Riyadh"})["status"] == "active"


def test_delete_is_blocked_while_a_store_or_account_uses_the_city():
    h = sa()
    a = city("Riyadh", "SA"); b = city("Duba", "SA")
    DB.stores.insert_one({"store_name": "S", "city": "riyadh"})
    r = web.delete("/admin/cities/" + a, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "city_in_use" and "Deactivate it instead" in r.json()["detail"]["message"]
    assert web.delete("/admin/cities/" + b, headers=h).status_code == 200 and DB.cities.count_documents({}) == 1


# ── permissions and country restriction ────────────────────────────────────────────────────────────────────
def test_permissions_per_action_and_unauthenticated():
    assert web.get("/admin/cities").status_code in (401, 403)
    h = user("v", perms={"Cities": {"view": True}})
    assert web.get("/admin/cities", headers=h).status_code == 200
    assert post({"country": "SA", "name": "Riyadh"}, h).status_code == 403
    cid = city("Riyadh", "SA")
    assert web.put("/admin/cities/" + cid, json={"state": "x"}, headers=h).status_code == 403
    assert web.put("/admin/cities/popular-order", json={"country": "SA", "ids": []}, headers=h).status_code == 403
    assert web.delete("/admin/cities/" + cid, headers=h).status_code == 403
    assert web.post("/admin/cities/import/preview", json={"country": "SA", "rows": []}, headers=h).status_code == 403
    h2 = user("n", perms={})
    assert web.get("/admin/cities", headers=h2).status_code == 403 and web.get("/admin/cities/verify?country=IN", headers=h2).status_code == 403


def test_a_country_restricted_dashboard_user_only_sees_and_changes_their_country():
    city("Riyadh", "SA"); mum = city("Mumbai", "IN"); city("Ballari", None)
    h = user("sa-user", perms={"Cities": {"view": True, "add": True, "edit": True, "delete": True}}, country="SA")
    assert [r["name"] for r in web.get("/admin/cities", headers=h).json()] == ["Riyadh"]
    assert web.get("/admin/cities?country=unassigned", headers=h).json() == []
    assert [c["code"] for c in web.get("/admin/cities/summary", headers=h).json()["countries"]] == ["SA"]
    assert post({"country": "IN", "name": "Pune"}, h).status_code == 403
    assert post({"country": "SA", "name": "Duba"}, h).status_code == 200
    assert web.put("/admin/cities/" + mum, json={"state": "x"}, headers=h).status_code == 403
    assert web.delete("/admin/cities/" + mum, headers=h).status_code == 403
    assert web.get("/admin/cities/verify?country=IN", headers=h).status_code == 403


# ── import ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_preview_writes_nothing_and_reports_every_row():
    h = sa()
    city("Tabuk", "SA", "Tabuk Province")
    rows = [{"name": "Riyadh", "name_ar": "الرياض", "state": "Riyadh Province", "is_popular": True, "popular_order": 1},
            {"name": "Tabuk", "state": "Tabuk Province"}, {"name": "Yanbu", "name_ar": "ينبع123"}, {"name": "Riyadh", "state": "Riyadh Province"}]
    r = web.post("/admin/cities/import/preview", json={"country": "SA", "rows": rows}, headers=h).json()
    assert (r["read"], r["new"], r["exists"], r["errors"], r["written"]) == (4, 1, 1, 2, 0)
    codes = [x.get("code") for x in r["rows"]]
    assert codes == [None, None, "arabic_digits", "duplicate_in_file"] and DB.cities.count_documents({}) == 1


def test_apply_needs_confirm_inserts_only_new_valid_rows_and_is_idempotent():
    h = sa()
    rows = [{"name": "Riyadh", "state": "Riyadh Province"}, {"name": "Jeddah", "state": "Makkah Province"}, {"name": "Bad1", "name_ar": "x1"}]
    assert web.post("/admin/cities/import/apply", json={"country": "SA", "rows": rows}, headers=h).status_code == 400
    r = web.post("/admin/cities/import/apply", json={"country": "SA", "rows": rows, "confirm": True}, headers=h).json()
    assert r["added"] == 2 and r["errors"] == 1 and r["batch"].startswith("imp-")
    assert DB.cities.count_documents({}) == 2 and all(d["import_batch"] == r["batch"] and d["country"] == "SA" for d in DB.cities.find({}))
    again = web.post("/admin/cities/import/apply", json={"country": "SA", "rows": rows, "confirm": True}, headers=h).json()
    assert again["added"] == 0 and again["skipped"] == 2 and DB.cities.count_documents({}) == 2


def test_import_never_edits_an_existing_city_and_fill_state_is_opt_in():
    h = sa()
    cid = city("Ballari", "IN", "", 7, image_url="data:image/png;base64,AAAA", names={"ar": "بالاري"})
    before = DB.cities.find_one({"_id": __import__("bson").ObjectId(cid)})
    body = {"country": "IN", "rows": [{"name": "Ballari", "state": "Karnataka", "sort_order": 99, "name_ar": "غيره"}], "confirm": True}
    web.post("/admin/cities/import/apply", json=body, headers=h)
    assert DB.cities.find_one({"_id": before["_id"]}) == before
    web.post("/admin/cities/import/apply", json={**body, "fill_empty_state": True}, headers=h)
    after = DB.cities.find_one({"_id": before["_id"]})
    assert after["state"] == "Karnataka" and after["image_url"] == before["image_url"] and after["names"] == before["names"] and after["sort_order"] == 7


def test_the_country_comes_from_the_selector_never_the_file():
    h = sa()
    r = web.post("/admin/cities/import/preview", json={"country": "SA", "rows": [{"name": "Pune", "country": "IN"}]}, headers=h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "country_mismatch"
    assert web.post("/admin/cities/import/preview", json={"country": "IN", "source": "reference:sa-v1"}, headers=h).status_code == 400
    assert web.post("/admin/cities/import/preview", json={"country": "SA"}, headers=h).status_code == 400


def test_csv_upload_is_parsed_and_validated():
    h = sa()
    csv_text = "name,name_ar,state,is_popular,popular_order\nAmman,عمّان,Amman Governorate,1,1\nIrbid,إربد,Irbid Governorate,,\n"
    r = web.post("/admin/cities/import/apply", json={"country": "JO", "csv": csv_text, "confirm": True}, headers=h).json()
    assert r["added"] == 2
    d = DB.cities.find_one({"name": "Amman"})
    assert d["is_popular"] is True and d["popular_order"] == 1 and d["names"] == {"ar": "عمّان"} and d["status"] == "active"
    assert web.post("/admin/cities/import/preview", json={"country": "JO", "csv": "city\nx"}, headers=h).status_code == 400


def test_saudi_reference_list_has_38_unique_cities_six_popular_and_imports_into_an_empty_country():
    h = sa()
    ref = json.load(open(os.path.join(REF, "sa.json"), encoding="utf-8"))
    assert ref["country"] == "SA" and len(ref["cities"]) == 38 and len({c["name"] for c in ref["cities"]}) == 38
    assert sorted(c["popular_order"] for c in ref["cities"] if c.get("is_popular")) == [1, 2, 3, 4, 5, 6]
    p = web.post("/admin/cities/import/preview", json={"country": "SA", "source": "reference:sa-v1"}, headers=h).json()
    assert (p["new"], p["errors"], p["active_default"]) == (38, 0, True) and DB.cities.count_documents({}) == 0
    web.post("/admin/cities/import/apply", json={"country": "SA", "source": "reference:sa-v1", "confirm": True}, headers=h)
    r = pub("SA", "ar")
    assert len(r) == 38 and [x["name"] for x in r][:6] == ["Riyadh", "Jeddah", "Makkah", "Madinah", "Dammam", "Khobar"]
    assert next(x for x in r if x["name"] == "Riyadh")["display_name"] == "الرياض"


def test_india_reference_matches_the_bundled_list_and_imports_inactive_so_the_service_area_is_unchanged():
    h = sa()
    ref = json.load(open(os.path.join(REF, "in.json"), encoding="utf-8"))
    keys = [(c["name"].lower(), c["state"].lower()) for c in ref["cities"]]
    assert ref["country"] == "IN" and ref["default_active"] is False and len(keys) == len(set(keys)) == 244
    assert {"Udaipur"} == {c["name"] for c in ref["cities"] if c["name"] == "Udaipur"} and sum(1 for c in ref["cities"] if c["name"] == "Udaipur") == 2
    city("Ballari", "IN", "", 1); city("Bengaluru", "IN", "Karnataka", 2)
    before = [x["name"] for x in pub("IN")]
    r = web.post("/admin/cities/import/apply", json={"country": "IN", "source": "reference:in-v1", "confirm": True}, headers=h).json()
    assert r["added"] == 242 and r["skipped"] == 2                              # the two existing cities are recognised, not duplicated
    assert [x["name"] for x in pub("IN")] == before                              # nothing new is served: the legacy Popular row and "supported" set are unchanged
    assert DB.cities.count_documents({"country": "IN", "status": "inactive"}) == 242
    assert all(d["sort_order"] > 2 for d in DB.cities.find({"import_batch": r["batch"]}))
    again = web.post("/admin/cities/import/apply", json={"country": "IN", "source": "reference:in-v1", "confirm": True}, headers=h).json()
    assert again["added"] == 0 and DB.cities.count_documents({"country": "IN"}) == 244


def test_undo_removes_only_unused_cities_of_that_batch():
    h = sa()
    old = city("Tabuk", "SA", "Tabuk Province")
    r = web.post("/admin/cities/import/apply", json={"country": "SA", "rows": [{"name": "Riyadh"}, {"name": "Jeddah"}], "confirm": True}, headers=h).json()
    DB.stores.insert_one({"store_name": "S", "city": "Jeddah"})
    u = web.post("/admin/cities/import/%s/undo" % r["batch"], headers=h).json()
    assert u["removed"] == 1 and u["kept_in_use"] == ["Jeddah"]
    assert {d["name"] for d in DB.cities.find({})} == {"Tabuk", "Jeddah"} and DB.cities.find_one({"name": "Tabuk"})["_id"] is not None
    assert web.post("/admin/cities/import/nope/undo", headers=h).status_code == 400


# ── verify / assign preview ────────────────────────────────────────────────────────────────────────────────
def test_verify_report_is_read_only_and_lists_unassigned_missing_and_duplicates():
    h = sa()
    city("Ballari", None); city("Mumbai", "IN", "Maharashtra"); city("Atlantis", "IN", "Nowhere")
    before = DB.cities.count_documents({})
    v = web.get("/admin/cities/verify?country=IN", headers=h).json()
    assert DB.cities.count_documents({}) == before
    assert v["unassigned_count"] == 1 and v["unassigned"][0]["name"] == "Ballari" and v["unassigned_matching_reference"] == 1
    assert v["missing_count"] == 243 and v["not_in_reference_count"] == 1 and v["not_in_reference"][0]["name"] == "Atlantis"
    city("Mumbai", "IN", "Maharashtra")
    assert web.get("/admin/cities/verify?country=IN", headers=h).json()["possible_duplicate_count"] == 1


def test_assign_country_preview_writes_nothing_and_assign_still_works():
    h = sa()
    city("Ballari", None); city("Hubli", None)
    p = web.post("/admin/cities/assign-country", json={"country": "IN", "preview": True}, headers=h).json()
    assert p["preview"] is True and p["would_update"] == 2 and DB.cities.count_documents({"country": "IN"}) == 0
    assert web.post("/admin/cities/assign-country", json={"country": "IN"}, headers=h).json()["updated"] == 2
