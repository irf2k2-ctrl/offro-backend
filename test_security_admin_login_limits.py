"""Patch C: admin sign-in limits count FAILURES, never successful logins, logouts or refreshes. MSG91 is mocked.
Run:  python -m pytest test_security_admin_login_limits.py -q -W ignore"""
import sys
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import server
from routers import dashboard_auth as DA
from services import admin_login_limits as LIM
from services import admin_login_otp as OTP
from services import admin_trusted_devices as TD

DB = sys.modules["database"].db
MOBILE, OTHER, PIN = "9000000100", "9000000200", "1234"
SHARED = ("dashboard_users", "dashboard_roles", "activity_logs", "trusted_devices")
OWN = ("admin_login_challenges", "admin_auth_throttle", TD.COLLECTION)
VARS = ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_PUBLIC_DOMAIN", "ADMIN_OTP_STAGING_TEST_MODE", "ADMIN_OTP_TEST_MOBILES")
IP = "x-forwarded-for"


class SMS(list):
    def __call__(self, mobile, otp, tpl):
        self.append(otp)
        return {"ok": True}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    saved = {c: list(DB[c].find({})) for c in SHARED}
    for c in SHARED + OWN:
        DB[c].delete_many({})
    for k in VARS:
        monkeypatch.delenv(k, raising=False)
    sms = SMS()
    monkeypatch.setattr(OTP, "_send_sms", sms)
    monkeypatch.setattr(DA, "db", DB)
    yield sms
    for c in SHARED + OWN:
        DB[c].delete_many({})
    for c, docs in saved.items():
        if docs:
            DB[c].insert_many(docs)


@pytest.fixture
def sms(env):
    return env


def mk_user(mobile=MOBILE, role_name="Sales Executive"):
    r = DB.dashboard_roles.find_one({"role_name": role_name})
    rid = r["_id"] if r else DB.dashboard_roles.insert_one({"role_name": role_name, "permissions": {}}).inserted_id
    return DB.dashboard_users.insert_one({"mobile": mobile, "full_name": "U", "pin": DA.hash_pin(PIN), "status": "active", "role_id": rid}).inserted_id


def client():
    return TestClient(server.app, raise_server_exceptions=False)


def login(c, mobile=MOBILE, pin=PIN, ip="5.5.5.5", **body):
    return c.post("/admin/auth/login", json=dict(mobile=mobile, pin=pin, **body), headers={IP: ip})


def verify(c, cid, otp, ip="5.5.5.5"):
    return c.post("/admin/auth/verify-otp", json={"challenge_id": cid, "otp": otp}, headers={IP: ip})


def cycle(c, sms, mobile=MOBILE, ip="5.5.5.5"):
    """One full sign-in (PIN, SMS code) followed by a logout."""
    r = login(c, mobile, ip=ip)
    assert r.status_code == 200, r.text
    v = verify(c, r.json()["challenge_id"], sms[-1], ip)
    assert v.status_code == 200, v.text
    assert c.post("/admin/auth/logout", json={}).status_code == 200


def age_failures(seconds):
    """Pretend `seconds` have passed for every recorded failure."""
    DB.admin_auth_throttle.update_many({}, [{"$set": {"at": {"$subtract": ["$at", seconds * 1000]}}}]) if False else None
    for d in list(DB.admin_auth_throttle.find({})):
        DB.admin_auth_throttle.update_one({"_id": d["_id"]}, {"$set": {"at": d["at"] - timedelta(seconds=seconds)}})


def throttle_total():
    return DB.admin_auth_throttle.count_documents({})


# ── the reported problem: normal repeated sign-ins ─────────────────────────────────────────────────

def test_many_login_logout_cycles_from_one_mobile_and_one_ip_never_lock_out(sms):
    mk_user()
    c = client()
    for _ in range(25):
        cycle(c, sms)                                                                     # was: locked out after the 5th sign-in within an hour
    assert len(sms) == 25


def test_many_login_logout_cycles_in_staging_test_mode_never_lock_out(sms, monkeypatch):
    from services import admin_otp_test_mode as TM
    for k, v in {"RAILWAY_PROJECT_ID": TM.STAGING_PROJECT_ID, "RAILWAY_ENVIRONMENT_ID": TM.STAGING_ENVIRONMENT_ID, "RAILWAY_SERVICE_ID": TM.STAGING_SERVICE_ID,
                 "RAILWAY_PUBLIC_DOMAIN": TM.STAGING_PUBLIC_DOMAIN, "MONGO_URL": "mongodb://u:p@mongodb.railway.internal:27017",
                 "ADMIN_OTP_STAGING_TEST_MODE": "1", "ADMIN_OTP_TEST_MOBILES": MOBILE}.items():
        monkeypatch.setenv(k, v)
    mk_user()
    c = client()
    for _ in range(25):
        r = login(c)
        assert r.status_code == 200
        assert verify(c, r.json()["challenge_id"], "1234").status_code == 200
        c.post("/admin/auth/logout", json={})
    assert sms == []


def test_completed_sign_ins_give_their_sms_budget_back(sms):
    mk_user()
    c = client()
    for _ in range(6):
        cycle(c, sms)
    assert DB.admin_auth_throttle.count_documents({"kind": {"$in": ["otp_send_acct", "otp_send_ip"]}}) == 0


def test_signins_that_never_complete_still_use_the_sms_budget(sms):
    mk_user()
    codes = [login(client(), ip="6.6.6.%d" % i).status_code for i in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]                          # abandoned sends are what the budget is for
    r = login(client(), ip="6.6.6.99")
    assert "Too many attempts" in r.json()["detail"]


def test_logout_refresh_and_session_checks_do_not_touch_any_counter(sms):
    mk_user()
    c = client()
    r = login(c)
    verify(c, r.json()["challenge_id"], sms[-1])
    before = throttle_total()
    for _ in range(10):
        assert c.get("/admin/auth/me").status_code == 200
        assert c.get("/admin/dashboard", follow_redirects=False).status_code == 200
        assert c.get("/admin/auth/device-status").status_code == 200
    assert c.post("/admin/auth/logout", json={}).status_code == 200
    assert throttle_total() == before


def test_a_successful_login_adds_no_failure_counter(sms):
    mk_user()
    c = client()
    cycle(c, sms)
    assert DB.admin_auth_throttle.count_documents({"kind": {"$regex": "fail"}}) == 0
    assert DB.dashboard_users.find_one({"mobile": MOBILE}).get("login_attempts", 0) == 0


# ── wrong PIN: progressive delays ──────────────────────────────────────────────────────────────────

def test_the_first_four_wrong_pins_get_a_normal_error_and_the_fifth_a_clear_cooldown(sms):
    mk_user()
    c = client()
    for _ in range(4):
        r = login(c, pin="0000")
        assert r.status_code == 401 and r.json()["detail"] == DA._GENERIC_CREDENTIALS
    r = login(c, pin="0000")
    assert r.status_code == 429 and r.json()["detail"] == "Too many incorrect attempts. Please try again in about 60 seconds."
    assert login(c).status_code == 429 and sms == []                                      # correct PIN is refused while cooling down


def test_delays_grow_with_repeated_failures_and_end_by_themselves(sms):
    mk_user()
    c = client()
    for _ in range(5):
        login(c, pin="0000")
    assert LIM.pin_cooldown(MOBILE, "5.5.5.5") in range(58, 61)
    age_failures(61)
    assert login(c, pin="0000").status_code == 429                                        # 6th failure: 2 minutes
    assert 118 <= LIM.pin_cooldown(MOBILE, "5.5.5.5") <= 120
    age_failures(121)
    assert login(c, pin="0000").status_code == 429                                        # 7th: 5 minutes
    assert 298 <= LIM.pin_cooldown(MOBILE, "5.5.5.5") <= 300
    age_failures(301)
    assert login(c).status_code == 200                                                    # waiting is enough; the correct PIN works again


def test_the_delay_is_capped_at_thirty_minutes(sms):
    mk_user()
    for i in range(14):
        DB.admin_auth_throttle.insert_one({"kind": "pin_fail_pair", "key": LIM._pair_key(MOBILE, "5.5.5.5"), "at": datetime.utcnow(), "purge_at": datetime.utcnow() + timedelta(hours=2)})
    assert 1795 <= LIM.pin_cooldown(MOBILE, "5.5.5.5") <= 1800


def test_a_correct_pin_forgives_typos_from_that_place(sms):
    mk_user()
    c = client()
    for _ in range(3):
        assert login(c, pin="0000").status_code == 401
    assert login(c).status_code == 200                                                    # PIN ok
    for _ in range(3):
        assert login(c, pin="0000").status_code == 401                                    # still below the limit: the earlier typos were forgiven
    assert LIM.pin_cooldown(MOBILE, "5.5.5.5") == 0


def test_a_completed_sign_in_clears_the_account_counters(sms):
    mk_user()
    for i in range(3):
        login(client(), pin="0000", ip="7.7.7.%d" % i)
    assert DB.admin_auth_throttle.count_documents({"kind": "pin_fail_acct"}) == 3
    cycle(client(), sms)
    assert DB.admin_auth_throttle.count_documents({"kind": {"$in": ["pin_fail_acct", "pin_fail_pair", "otp_fail_acct"]}}) == 0


# ── counters are not shared more than necessary ────────────────────────────────────────────────────

def test_one_device_being_locked_does_not_lock_the_same_admin_on_another_device(sms):
    mk_user()
    for _ in range(5):
        login(client(), pin="0000", ip="1.1.1.1")
    assert login(client(), ip="1.1.1.1").status_code == 429                               # the guessing place is cooling down
    r = login(client(), ip="2.2.2.2")
    assert r.status_code == 200 and r.json()["otp_required"] is True                      # the admin's own device is unaffected


def test_one_admin_being_locked_does_not_affect_another_admin_on_the_same_ip(sms):
    mk_user()
    mk_user(OTHER)
    for _ in range(5):
        login(client(), MOBILE, pin="0000", ip="1.1.1.1")
    assert login(client(), MOBILE, ip="1.1.1.1").status_code == 429
    assert login(client(), OTHER, ip="1.1.1.1").status_code == 200


def test_guessing_spread_over_many_ips_is_stopped_by_the_account_ceiling(sms):
    mk_user()
    for i in range(14):
        assert login(client(), pin="0000", ip="9.9.%d.1" % i).status_code == 401           # each place stays under its own limit
    assert login(client(), pin="0000", ip="9.9.99.1").status_code == 429                   # the 15th failure on this account, from anywhere
    assert login(client(), ip="3.3.3.3").status_code == 429                                # even a new place waits: 5 minutes
    assert 290 <= LIM.pin_cooldown(MOBILE, "3.3.3.3") <= 300


def test_one_ip_trying_many_accounts_is_stopped_by_the_ip_ceiling(sms):
    codes = [login(client(), "90000009%02d" % i, pin="0000", ip="8.8.8.8").status_code for i in range(32)]
    assert codes[0] == 401 and codes[29] == 429 and codes[-1] == 429
    assert login(client(), "9000000555", ip="4.4.4.4", pin="0000").status_code == 401       # other IPs are fine


def test_unknown_numbers_behave_exactly_like_real_ones(sms):
    mk_user()
    real = [login(client(), MOBILE, pin="0000", ip="1.1.1.1") for _ in range(6)]
    fake = [login(client(), "9000000999", pin="0000", ip="1.1.1.1") for _ in range(6)]
    assert [(r.status_code, r.json()["detail"]) for r in real] == [(r.status_code, r.json()["detail"]) for r in fake]


# ── wrong OTP ──────────────────────────────────────────────────────────────────────────────────────

def test_wrong_codes_across_challenges_delay_new_codes_before_any_sms_is_sent(sms):
    mk_user()
    for _ in range(2):
        r = login(client(), ip="3.3.3.%d" % _)
        for _i in range(5):
            assert verify(client(), r.json()["challenge_id"], "0000" if sms[-1] != "0000" else "1111", ip="2.2.2.%d" % _i).status_code in (401, 429)
    sent = len(sms)
    r = login(client(), ip="3.3.3.9")
    assert r.status_code == 429 and "try again in about" in r.json()["detail"] and len(sms) == sent
    age_failures(301)
    assert login(client(), ip="3.3.3.9").status_code == 200


def test_many_wrong_codes_from_one_ip_get_a_cooldown_and_a_correct_code_still_works_after_it(sms):
    mk_user()
    c = client()
    r = login(c)
    cid = r.json()["challenge_id"]
    for i in range(20):
        verify(c, "q" * 40, "0000")
    assert verify(c, cid, sms[-1]).status_code == 429 and "try again in about" in verify(c, cid, sms[-1]).json()["detail"]
    age_failures(301)
    assert verify(c, cid, sms[-1]).status_code == 200


def test_the_per_challenge_attempt_cap_and_resend_rules_are_unchanged(sms):
    mk_user()
    c = client()
    r = login(c)
    cid = r.json()["challenge_id"]
    wrong = "0000" if sms[-1] != "0000" else "1111"
    for _ in range(5):
        assert verify(c, cid, wrong).status_code == 401
    assert verify(c, cid, sms[-1]).status_code == 401
    r2 = login(c)
    assert c.post("/admin/auth/resend-otp", json={"challenge_id": r2.json()["challenge_id"]}).status_code == 429       # 30 s resend cooldown


# ── administrators can clear a lock ────────────────────────────────────────────────────────────────

def test_an_administrator_pin_reset_clears_the_cooldown_and_the_failure_counters(sms):
    uid = mk_user()
    for _ in range(5):
        login(client(), pin="0000")
    assert login(client()).status_code == 429
    rid = DB.dashboard_roles.find_one({"role_name": "Super Admin"})
    rid = rid["_id"] if rid else DB.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    DB.dashboard_users.insert_one({"mobile": "9000000999", "full_name": "Boss", "pin": DA.hash_pin(PIN), "status": "active", "token": "boss", "role_id": rid,
                                   "last_active_at": datetime.utcnow()})
    boss = client()
    boss.cookies.set("admin_token", "boss")
    assert boss.put("/admin/auth/users/%s/reset-pin" % uid, json={"new_pin": "4321"}).status_code == 200
    assert login(client(), pin="4321").status_code == 200


def test_a_lock_set_by_an_older_version_is_still_honoured_and_can_be_cleared(sms):
    uid = mk_user()
    DB.dashboard_users.update_one({"_id": uid}, {"$set": {"lockout_until": datetime.utcnow() + timedelta(minutes=10)}})
    assert login(client()).status_code == 429
    DB.dashboard_users.update_one({"_id": uid}, {"$set": {"lockout_until": None}})
    assert login(client()).status_code == 200


def test_cooldown_wording_is_only_used_for_real_limits_and_never_names_an_account(sms):
    assert LIM.wait_message(45) == "Too many incorrect attempts. Please try again in about 45 seconds."
    assert LIM.wait_message(60) == "Too many incorrect attempts. Please try again in about 60 seconds."
    assert LIM.wait_message(300) == "Too many incorrect attempts. Please try again in about 5 minutes."
    assert LIM.wait_message(1800) == "Too many incorrect attempts. Please try again in about 30 minutes."
    assert LIM.wait_message(0) == "Too many incorrect attempts. Please try again in about 1 seconds."
