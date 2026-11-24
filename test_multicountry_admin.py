"""NOTE: file name sorts AFTER test_banner_media.py on purpose (it swaps the shared fake `database` at import).
Multi-country admin: Store / Influencer country + city validation, country on the list rows, and Popup Campaign country targeting.
Run:  python -m pytest test_multicountry_admin.py -q -W ignore"""
import sys

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import server

fake = sys.modules["database"].db
c = TestClient(server.app)
ADM = {"Authorization": "Bearer sa_tok"}
PADM = {"x-admin-token": "sa_tok"}        # the popup router reads its own admin token header


def _city(name, country, state="", status="active", **kw):
    d = {"name": name, "name_key": name.lower(), "country": country, "state": state, "status": status, "active": status == "active"}
    d.update(kw)
    return fake.cities.insert_one(d).inserted_id


@pytest.fixture(autouse=True)
def _world():
    for col in ("dashboard_users", "dashboard_roles", "accounts", "stores", "influencers", "cities", "popup_campaigns", "subscriptions", "deals"):
        fake[col].delete_many({})
    rid = fake.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "sa_tok", "status": "active", "full_name": "SA", "mobile": "9000000001",
                                     "role_id": rid, "assigned_cities": ["*"]})
    for n, st in (("Mumbai", "Maharashtra"), ("Hyderabad", "Telangana"), ("Pune", "Maharashtra")):
        _city(n, "IN", st)
    for n in ("Riyadh", "Jeddah", "Madinah"):
        _city(n, "SA")
    for n in ("Amman", "Irbid"):
        _city(n, "JO")
    _city("Kathmandu", "NP")
    _city("Closed Town", "SA", status="inactive")
    yield


def _merchant(phone, name="M"):
    return str(fake.accounts.insert_one({"name": name, "phone": phone, "roles": ["merchant"], "status": "active"}).inserted_id)


def _store(mid, **kw):
    d = {"merchant_id": mid, "store_name": "Shop", "city": "Mumbai"}
    d.update(kw)
    return c.post("/admin/stores", json=d, headers=ADM)


def _stores():
    return c.get("/admin/stores", headers=ADM).json()


# ───────────────────────── Stores ─────────────────────────
def test_store_create_with_country_and_matching_city():
    mid = _merchant("+966501234567")
    r = _store(mid, country="SA", city="Riyadh")
    assert r.status_code == 200, r.text
    doc = fake.stores.find_one({"_id": ObjectId(r.json()["store_id"])})
    assert doc["country"] == "SA" and doc["city"] == "Riyadh"


def test_store_create_rejects_city_of_another_country():
    mid = _merchant("+966501234567")
    r = _store(mid, country="SA", city="Mumbai")
    assert r.status_code == 400 and "Mumbai" in r.json()["detail"] and "India" in r.json()["detail"]
    assert fake.stores.count_documents({}) == 0


def test_store_create_rejects_unknown_city_for_non_india_country():
    mid = _merchant("+962791234567")
    r = _store(mid, country="JO", city="Nowhereville")
    assert r.status_code == 400
    assert fake.stores.count_documents({}) == 0


def test_store_create_rejects_unsupported_country_and_owner_mismatch():
    mid = _merchant("+966501234567")
    assert _store(mid, country="ZZ", city="Riyadh").status_code == 400
    r = _store(mid, country="JO", city="Amman")               # owner is Saudi
    assert r.status_code == 400 and "merchant" in r.json()["detail"].lower()


def test_store_create_india_keeps_free_text_city_and_old_clients_work():
    mid = _merchant("9876543210")
    assert _store(mid, country="IN", city="Ballari").status_code == 200       # bundled India list is wider than the master
    # an older client sends no country: unchanged behaviour (country derived from the owner, stored nowhere)
    r = _store(mid, city="Pune")
    assert r.status_code == 200 and "country" not in fake.stores.find_one({"_id": ObjectId(r.json()["store_id"])})
    # ... but a city that clearly belongs to another country is refused even then
    assert _store(mid, city="Riyadh").status_code == 400


def test_store_update_to_a_different_country_needs_matching_merchant_and_city():
    in_m, sa_m = _merchant("9876543210"), _merchant("+966501234567")
    sid = _store(in_m, country="IN", city="Mumbai").json()["store_id"]
    # same merchant (Indian): moving the store to Saudi Arabia is refused
    r = c.put("/admin/stores/" + sid, json={"country": "SA", "city": "Riyadh"}, headers=ADM)
    assert r.status_code == 400
    # merchant of the new country + a city of it: accepted
    r = c.put("/admin/stores/" + sid, json={"country": "SA", "city": "Riyadh", "merchant_id": sa_m}, headers=ADM)
    assert r.status_code == 200, r.text
    d = fake.stores.find_one({"_id": ObjectId(sid)})
    assert d["country"] == "SA" and d["city"] == "Riyadh" and d["merchant_id"] == sa_m
    # keeping the new country but sending an Indian city is refused and nothing changes
    r = c.put("/admin/stores/" + sid, json={"country": "SA", "city": "Pune", "merchant_id": sa_m}, headers=ADM)
    assert r.status_code == 400 and fake.stores.find_one({"_id": ObjectId(sid)})["city"] == "Riyadh"


def test_store_update_country_change_with_the_old_city_is_refused():
    sa_m = _merchant("+966501234567")
    sid = _store(sa_m, country="SA", city="Riyadh").json()["store_id"]
    jo_m = _merchant("+962791234567")
    r = c.put("/admin/stores/" + sid, json={"country": "JO", "merchant_id": jo_m}, headers=ADM)   # city Riyadh is not Jordanian
    assert r.status_code == 400 and fake.stores.find_one({"_id": ObjectId(sid)})["city"] == "Riyadh"


def test_existing_store_edits_are_not_blocked_by_the_new_rules():
    mid = _merchant("+966501234567")
    sid = str(fake.stores.insert_one({"merchant_id": mid, "store_name": "Old", "city": "Some Legacy Town", "status": "active"}).inserted_id)
    assert c.put("/admin/stores/" + sid, json={"store_name": "Renamed"}, headers=ADM).status_code == 200
    assert c.put("/admin/stores/" + sid, json={"status": "inactive"}, headers=ADM).status_code == 200
    d = fake.stores.find_one({"_id": ObjectId(sid)})
    assert d["city"] == "Some Legacy Town" and "country" not in d


def test_store_list_shows_stored_country_else_the_owners_and_never_infers_from_city():
    sa_m, in_m = _merchant("+966501234567"), _merchant("9876543210")
    _store(sa_m, country="SA", city="Riyadh")
    fake.stores.insert_one({"merchant_id": in_m, "store_name": "Legacy", "city": "Riyadh", "status": "active"})   # odd city, no country
    rows = {r["store_name"]: r for r in _stores()}
    assert rows["Shop"]["country_iso"] == "SA" and rows["Shop"]["country_name"] == "Saudi Arabia" and rows["Shop"]["country_flag"]
    assert rows["Legacy"]["country_iso"] == "IN" and rows["Legacy"]["country_name"] == "India"    # owner's country, not Riyadh's


def test_country_options_and_country_cities_endpoints():
    r = c.get("/admin/country-options", headers=ADM)
    isos = [x["iso"] for x in r.json()]
    assert r.status_code == 200 and {"IN", "SA", "JO", "NP"} <= set(isos)
    r = c.get("/admin/country-cities?country=SA", headers=ADM)
    assert [x["name"] for x in r.json()] == ["Jeddah", "Madinah", "Riyadh"]            # active only, SA only, alphabetical
    assert c.get("/admin/country-cities?country=ZZ", headers=ADM).status_code == 400
    assert c.get("/admin/country-cities", headers=ADM).status_code == 400


# ───────────────────────── Influencers ─────────────────────────
def _inf(**kw):
    d = {"name": "Priya", "city": "Mumbai", "phone": "9876543210"}
    d.update(kw)
    return c.post("/admin/influencers", json=d, headers=ADM)


def test_influencer_create_with_country_and_matching_city():
    r = _inf(name="Sara", country="SA", city="Jeddah", phone="+966501234567")
    assert r.status_code == 200, r.text
    d = fake.influencers.find_one({"_id": ObjectId(r.json()["_id"])})
    assert d["country"] == "SA" and d["city"] == "Jeddah"


def test_influencer_create_rejects_city_of_another_country():
    r = _inf(country="SA", city="Mumbai", phone="+966501234567")
    assert r.status_code == 400 and "India" in r.json()["detail"]
    assert fake.influencers.count_documents({}) == 0
    assert _inf(country="NP", city="Riyadh", phone="+9779812345678").status_code == 400


def test_influencer_create_without_country_keeps_old_behaviour():
    assert _inf(city="Ballari").status_code == 200                # India, legacy free text, phone gives India
    assert _inf(name="X", city="Riyadh").status_code == 400       # phone is Indian -> a Saudi city is refused


def test_influencer_update_changes_country_only_with_a_matching_city():
    iid = _inf(country="IN", city="Mumbai").json()["_id"]
    r = c.put("/admin/influencers/" + iid, json={"country": "JO"}, headers=ADM)           # Mumbai is not Jordanian
    assert r.status_code == 400 and fake.influencers.find_one({"_id": ObjectId(iid)})["city"] == "Mumbai"
    r = c.put("/admin/influencers/" + iid, json={"country": "JO", "city": "Amman", "phone": ""}, headers=ADM)
    assert r.status_code == 200, r.text
    d = fake.influencers.find_one({"_id": ObjectId(iid)})
    assert d["country"] == "JO" and d["city"] == "Amman"
    assert c.put("/admin/influencers/" + iid, json={"country": "JO", "city": "Pune"}, headers=ADM).status_code == 400


def test_influencer_update_phone_country_must_match_a_calling_code_phone():
    iid = _inf(country="SA", city="Riyadh", phone="+966501234567").json()["_id"]
    r = c.put("/admin/influencers/" + iid, json={"country": "SA", "phone": "+962791234567"}, headers=ADM)
    assert r.status_code == 400


def test_existing_influencer_edits_are_not_blocked():
    iid = str(fake.influencers.insert_one({"name": "Old", "city": "Legacy Town", "phone": "9876543210", "status": "active"}).inserted_id)
    assert c.put("/admin/influencers/" + iid, json={"status": "inactive"}, headers=ADM).status_code == 200
    assert c.put("/admin/influencers/" + iid, json={"name": "Older"}, headers=ADM).status_code == 200
    assert fake.influencers.find_one({"_id": ObjectId(iid)})["city"] == "Legacy Town"


def test_influencer_list_country_comes_from_the_record_or_its_phone():
    _inf(name="A", country="SA", city="Jeddah", phone="+966501234567")
    fake.influencers.insert_one({"name": "Legacy", "city": "Riyadh", "phone": "9876543210", "created_at": None})
    rows = {r["name"]: r for r in c.get("/admin/influencers", headers=ADM).json()}
    assert rows["A"]["country_iso"] == "SA" and rows["A"]["country_name"] == "Saudi Arabia"
    assert rows["Legacy"]["country_iso"] == "IN"                  # from the phone, not from the city


# ───────────────────────── Influencer self-service (Flutter) ─────────────────────────
def _own(phone, tok="t1"):
    fake.accounts.insert_one({"token": tok, "phone": phone, "name": "Me", "roles": ["customer"]})
    return {"Authorization": "Bearer " + tok}


def test_self_service_profile_city_must_belong_to_the_account_country():
    h = _own("+966501234567")
    r = c.post("/user/influencer-profile", json={"name": "Me", "city": "Mumbai", "country": "SA"}, headers=h)
    assert r.status_code == 400 and "India" in r.json()["detail"]
    r = c.post("/user/influencer-profile", json={"name": "Me", "city": "Riyadh", "country": "JO"}, headers=h)
    assert r.status_code == 400                                    # not the account's country
    r = c.post("/user/influencer-profile", json={"name": "Me", "city": "Riyadh", "country": "SA"}, headers=h)
    assert r.status_code == 200, r.text
    d = fake.influencers.find_one({})
    assert d["country"] == "SA" and d["city"] == "Riyadh"


def test_self_service_old_app_without_country_only_blocks_clear_cross_country():
    h = _own("+962791234567")
    assert c.post("/user/influencer-profile", json={"name": "Me", "city": "Mumbai"}, headers=h).status_code == 400
    assert c.post("/user/influencer-profile", json={"name": "Me", "city": "Amman"}, headers=h).status_code == 200


def test_self_service_update_validates_only_a_changed_city():
    h = _own("+966501234567")
    assert c.post("/user/influencer-profile", json={"name": "Me", "city": "Riyadh", "country": "SA"}, headers=h).status_code == 200
    r = c.put("/user/influencer-profile", json={"city": "Pune", "country": "SA"}, headers=h)
    assert r.status_code == 400 and fake.influencers.find_one({})["city"] == "Riyadh"
    assert c.put("/user/influencer-profile", json={"city": "Jeddah", "country": "SA"}, headers=h).status_code == 200
    assert c.put("/user/influencer-profile", json={"name": "New Name"}, headers=h).status_code == 200


# ───────────────────────── Popup campaigns ─────────────────────────
def _camp(**kw):
    d = {"name": "C", "start_dt": "2026-01-01T00:00", "end_dt": "2027-01-01T00:00", "target": "selected_city"}
    d.update(kw)
    r = c.post("/admin/popup-campaigns", json=d, headers=PADM)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _shown(city="", country=""):
    r = c.get("/public/popup-campaigns", params={"city": city, "country": country})
    assert r.status_code == 200
    return {x["name"] for x in r.json()}


def test_one_country_includes_all_its_active_cities_and_never_indian_ones():
    cid = _camp(name="SA only", countries=["SA"])
    d = fake.popup_campaigns.find_one({"_id": ObjectId(cid)})
    assert d["countries"] == ["SA"] and d["audience_version"] == 2
    assert sorted(d["cities"]) == ["Jeddah", "Madinah", "Riyadh"]          # active cities only (Closed Town is inactive)
    assert _shown("Riyadh", "SA") == {"SA only"}
    assert _shown("Mumbai", "IN") == set()
    assert _shown("Hyderabad", "IN") == set()


def test_same_city_name_in_two_countries_is_decided_by_country_code():
    _city("Dupe", "IN")
    _city("Dupe", "SA")
    _camp(name="SA", countries=["SA"])
    assert "SA" in _shown("Dupe", "SA")
    assert _shown("Dupe", "IN") == set()


def test_multiple_countries_and_no_duplicate_cities():
    cid = _camp(name="Multi", countries=["SA", "jo", "SA"],
                city_targets=[{"country": "SA", "city": "Riyadh"}, {"country": "IN", "city": "Mumbai"}, {"country": "IN", "city": "mumbai"}])
    d = fake.popup_campaigns.find_one({"_id": ObjectId(cid)})
    assert d["countries"] == ["SA", "JO"]
    assert d["city_targets"] == [{"country": "IN", "city": "Mumbai"}]       # Riyadh is covered by SA; the Mumbai twin is dropped
    assert sorted(d["cities"]) == ["Amman", "Irbid", "Jeddah", "Madinah", "Mumbai", "Riyadh"]
    assert len(d["cities"]) == len(set(x.lower() for x in d["cities"]))
    assert _shown("Amman", "JO") == {"Multi"} and _shown("Mumbai", "IN") == {"Multi"}
    assert _shown("Pune", "IN") == set() and _shown("Kathmandu", "NP") == set()


def test_individual_city_exclusions_under_a_country():
    cid = _camp(name="NoMadinah", countries=["SA"], excluded_cities=[{"country": "SA", "city": "Madinah"}, {"country": "JO", "city": "Amman"}])
    d = fake.popup_campaigns.find_one({"_id": ObjectId(cid)})
    assert d["excluded_cities"] == [{"country": "SA", "city": "Madinah"}]   # an exclusion outside a targeted country is dropped
    assert "Madinah" not in d["cities"] and "Riyadh" in d["cities"]
    assert _shown("Riyadh", "SA") == {"NoMadinah"} and _shown("Madinah", "SA") == set()


def test_individual_cities_without_the_whole_country():
    _camp(name="Two", city_targets=[{"country": "IN", "city": "Mumbai"}, {"country": "SA", "city": "Jeddah"}])
    assert _shown("Mumbai", "IN") == {"Two"} and _shown("Jeddah", "SA") == {"Two"}
    assert _shown("Riyadh", "SA") == set() and _shown("Pune", "IN") == set()


def test_country_only_user_without_a_city_and_unknown_location():
    _camp(name="SA", countries=["SA"])
    assert _shown("", "SA") == {"SA"} and _shown("", "IN") == set()
    assert _shown("", "") == {"SA"}                                          # location unknown: shown, as before


def test_old_app_sending_only_the_city_still_matches_through_the_city_master():
    _camp(name="SA", countries=["SA"])
    assert _shown("Riyadh") == {"SA"} and _shown("riyadh ") == {"SA"} and _shown("Mumbai") == set()


def test_all_users_unchanged_and_switching_clears_country_fields():
    cid = _camp(name="Everyone", target="all")
    assert _shown("Riyadh", "SA") == {"Everyone"} and _shown("Mumbai", "IN") == {"Everyone"} and _shown() == {"Everyone"}
    cid2 = _camp(name="Was SA", countries=["SA"])
    r = c.put("/admin/popup-campaigns/" + cid2, json={"name": "Was SA", "target": "all", "start_dt": "a", "end_dt": "b"}, headers=PADM)
    assert r.status_code == 200
    d = fake.popup_campaigns.find_one({"_id": ObjectId(cid2)})
    assert d["target"] == "all" and "countries" not in d and "audience_version" not in d and "excluded_cities" not in d


def test_existing_campaigns_keep_working_untouched():
    old_sel = fake.popup_campaigns.insert_one({"name": "Old city", "target": "selected_city", "cities": ["Mumbai", "pune"],
                                               "is_active": True, "image_url": "", "frequency": "once_per_day"}).inserted_id
    fake.popup_campaigns.insert_one({"name": "Old all", "target": "all", "cities": [], "is_active": True, "image_url": ""})
    before = dict(fake.popup_campaigns.find_one({"_id": old_sel}))
    assert _shown("Mumbai") == {"Old city", "Old all"} and _shown("PUNE", "IN") == {"Old city", "Old all"}
    assert _shown("Riyadh", "SA") == {"Old all"}
    assert _shown() == {"Old city", "Old all"}                                # the old "no city -> show" behaviour
    assert dict(fake.popup_campaigns.find_one({"_id": old_sel})) == before    # reading never changes a record
    # the older form payload (no countries) is stored exactly as before
    cid = _camp(name="Legacy payload", cities=["Mumbai"])
    d = fake.popup_campaigns.find_one({"_id": ObjectId(cid)})
    assert d["cities"] == ["Mumbai"] and "audience_version" not in d and "countries" not in d
    r = c.get("/admin/popup-campaigns", headers=PADM)
    assert r.status_code == 200 and {x["name"] for x in r.json()} >= {"Old city", "Old all", "Legacy payload"}


def test_campaign_validation_rejects_bad_countries_and_cross_country_cities():
    base = {"name": "Bad", "start_dt": "a", "end_dt": "b", "target": "selected_city"}
    assert c.post("/admin/popup-campaigns", json={**base, "countries": ["ZZ"]}, headers=PADM).status_code == 400
    r = c.post("/admin/popup-campaigns", json={**base, "city_targets": [{"country": "SA", "city": "Mumbai"}]}, headers=PADM)
    assert r.status_code == 400 and "India" in r.json()["detail"]
    assert c.post("/admin/popup-campaigns", json={**base, "city_targets": ["Mumbai"]}, headers=PADM).status_code == 400
    assert fake.popup_campaigns.count_documents({}) == 0
