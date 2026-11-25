"""Phase 2 / Task 4: country-based notifications (target 'country'), audience preview, counts, restrictions.
(name sorts after test_banner_media.py on purpose.)  Run: python -m pytest test_phase2_task4_country_notifications.py -q -W ignore"""
import json
import re

import pytest

from services import notification_audience as AUD
from test_phase2_stage1_cde import (NH, NOTIF, _city, _cust, _send, _user, _world, c, fake, fcm)  # noqa: F401  (fixtures)


@pytest.fixture
def pop():
    _city("Riyadh", "SA"); _city("Jeddah", "SA"); _city("Ballari", "IN"); _city("Kathmandu", "NP")
    _cust("t_sa_user", "+966501234567", "Riyadh", phone_country="SA")
    _cust("t_sa_user2", "+966501234568", "Jeddah", phone_country="SA")
    _cust("t_sa_merchant", "+966501234569", "Riyadh", roles=("merchant",), phone_country="SA")
    _cust(None, "+966501234560", "Riyadh", phone_country="SA")                                   # no token
    _cust("t_sa_user", "+966501234561", "Riyadh", phone_country="SA")                            # duplicate token
    _cust("t_sa_parsed", "+966501234562", "Riyadh")                                               # country parsed from the prefix
    _cust("t_in", "9876543210", "Ballari")
    _cust("t_np", "+9779812345678", "Kathmandu", phone_country="NP")
    _cust("t_ghost", "501234567", "Riyadh")                                                       # country cannot be resolved
    fake.users.insert_one({"phone": "9876543299", "fcm_token": "t_legacy_user"})
    return None


def test_country_target_reaches_only_that_country(fcm, pop):
    r = _send(NH(), target="country", target_country="SA")
    assert r.status_code == 200, r.text
    assert sorted(fcm["tokens"]) == ["t_sa_merchant", "t_sa_parsed", "t_sa_user", "t_sa_user2"]
    j = r.json()
    assert (j["eligible_records"], j["sent_count"], j["failed_count"], j["skipped_no_token"], j["skipped_duplicate"]) == (6, 4, 0, 1, 1)
    assert j["skipped_count"] == 2 and j["country"] == "SA" and j["status"] == "sent"
    assert "t_in" not in fcm["tokens"] and "t_np" not in fcm["tokens"] and "t_ghost" not in fcm["tokens"] and "t_legacy_user" not in fcm["tokens"]


def test_india_country_target_is_india_only(fcm, pop):
    assert _send(NH(), target="country", target_country="IN").status_code == 200
    assert fcm["tokens"] == ["t_in"]


@pytest.mark.parametrize("iso,tok", [("SA", "t_sa_user"), ("NP", "t_np"), ("IN", "t_in")])
def test_each_selected_country(fcm, pop, iso, tok):
    j = _send(NH(), target="country", target_country=iso).json()
    assert tok in fcm["tokens"] and j["country"] == iso


@pytest.mark.parametrize("bad", ["", "  ", "ZZ", "India", None])
def test_country_is_required_and_never_defaulted(fcm, pop, bad):
    r = _send(NH(), target="country", target_country=bad)
    assert r.status_code == 400 and fcm["tokens"] == [] and fake.notifications.count_documents({}) == 0


def test_country_target_without_the_field_is_refused(fcm, pop):
    assert _send(NH(), target="country").status_code == 400 and fcm["tokens"] == []


def test_preview_count_is_exact_and_exposes_nothing(fcm, pop):
    r = _send(NH(), target="country", target_country="SA", dry_run=True)
    j = r.json()
    assert j["dry_run"] is True and j["eligible_records"] == 6 and j["unique_tokens"] == 4 and j["skipped_no_token"] == 1 and j["skipped_duplicate"] == 1
    txt = json.dumps(j)
    assert "t_sa_user" not in txt and "tokens" not in j and not re.search(r"\+?966\d{6,}", txt)
    assert fcm["tokens"] == [] and fake.notifications.count_documents({}) == 0 and fake.security_audit.count_documents({}) == 0
    sent = _send(NH(), target="country", target_country="SA").json()
    assert (sent["eligible_records"], sent["unique_tokens"]) == (j["eligible_records"], j["unique_tokens"])        # preview == real send


def test_city_target_still_filters_by_city_and_country(fcm, pop):
    _city("Riyadh", "IN")                                                                          # same name in another country
    _cust("t_in_riyadh", "9876543211", "Riyadh")
    assert _send(NH(), target="city", target_city="Riyadh").status_code == 400                     # ambiguous: must choose
    assert _send(NH(), target="city", target_city="Riyadh", target_country="SA").status_code == 200
    assert sorted(fcm["tokens"]) == ["t_sa_parsed", "t_sa_user"]                                   # customers only, SA only, no India Riyadh
    fcm["tokens"].clear()
    assert _send(NH(), target="city", target_city="Riyadh", target_country="IN").status_code == 200
    assert fcm["tokens"] == ["t_in_riyadh"]


def test_all_users_unchanged_for_unrestricted_admin(fcm, pop):
    assert _send(NH(), target="all_users").status_code == 200
    assert {"t_sa_user", "t_in", "t_np", "t_ghost", "t_legacy_user"} <= set(fcm["tokens"])         # exactly the previous behaviour


def test_country_restricted_admin_only_own_country(fcm, pop):
    sa = _user("sa_admin", "Marketing", NOTIF, country="SA")
    assert _send(sa, target="country", target_country="IN").status_code == 403
    assert _send(sa, target="country", target_country="NP", dry_run=True).status_code == 403
    assert fcm["tokens"] == []
    assert _send(sa, target="country", target_country="SA").status_code == 200
    assert sorted(fcm["tokens"]) == ["t_sa_merchant", "t_sa_parsed", "t_sa_user", "t_sa_user2"]


def test_corrupted_admin_country_fails_closed(fcm, pop):
    bad = _user("bad_admin", "Broken", NOTIF, country="XX")
    for ctry in ("SA", "IN"):
        assert _send(bad, target="country", target_country=ctry).status_code == 403
    assert fcm["tokens"] == []


def test_assigned_city_restriction_applies_to_country_target(fcm, pop):
    adm = _user("city_adm", "CityMarketing", NOTIF, cities=("Jeddah",))
    j = _send(adm, target="country", target_country="SA").json()
    assert fcm["tokens"] == ["t_sa_user2"] and j["eligible_records"] == 1
    none = _user("no_city", "NoCity", NOTIF, cities=())
    assert _send(none, target="country", target_country="SA").json()["eligible_records"] == 0


def test_permissions_required(fcm, pop):
    assert _send(_user("viewonly", "V", {"Notifications": {"view": True}}), target="country", target_country="SA").status_code == 403
    assert _send(_user("none", "N", {}), target="country", target_country="SA", dry_run=True).status_code == 403
    assert c.post("/admin/notifications/send", json={"title": "T", "body": "B", "target": "country", "target_country": "SA"}).status_code in (401, 403)


def test_no_recipient_cap_and_duplicates_once(fcm):
    for i in range(3500):
        fake.accounts.insert_one({"phone": f"+96650{i:07d}", "phone_country": "SA", "roles": ["user"], "city": "Riyadh", "fcm_token": f"tok{i % 3400}"})
    j = _send(NH(), target="country", target_country="SA").json()
    assert j["eligible_records"] == 3500 and j["unique_tokens"] == 3400 and j["skipped_duplicate"] == 100 and j["sent_count"] == 3400
    assert len(fcm["tokens"]) == len(set(fcm["tokens"])) == 3400


def test_failures_are_reported(fcm, pop):
    fcm["fail"].add("t_sa_user2")
    j = _send(NH(), target="country", target_country="SA").json()
    assert (j["sent_count"], j["failed_count"], j["status"]) == (3, 1, "partial") and j["skipped_count"] == 2


def test_history_and_audit_hold_counts_only(fcm, pop):
    _send(NH(), target="country", target_country="SA")
    doc = fake.notifications.find_one({})
    assert doc["target"] == "country" and doc["country"] == "SA"
    blob = json.dumps(list(fake.notifications.find({})), default=str) + json.dumps(list(fake.security_audit.find({})), default=str)
    assert "t_sa_user" not in blob and "+96650" not in blob
    a = fake.security_audit.find_one({"action": "notification.send"})
    assert a["details"]["target"] == "country" and a["details"]["eligible_records"] == 6
    rows = c.get("/admin/notifications", headers=NH("sup2")).json()
    assert "t_sa_user" not in json.dumps(rows, default=str)


def test_country_target_never_writes_accounts(fcm, pop):
    before = list(fake.accounts.find({}))
    _send(NH(), target="country", target_country="SA")
    assert list(fake.accounts.find({})) == before


def test_offers_still_refused_and_specific_unchanged(fcm, pop):
    assert _send(NH(), target="offers").status_code == 400
    assert _send(NH(), target="specific", target_phone="9876543210").status_code == 200 and fcm["tokens"] == ["t_in"]
