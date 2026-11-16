"""Staging-only fixed test OTP (1234) for the admin login: environment verification (fail closed) and that every OTP protection still holds.
MSG91 is mocked; no SMS, no network, no Railway. Run:  python -m pytest test_security_admin_otp_test_mode.py -q -W ignore"""
import os
import re
import sys
import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import server
from routers import dashboard_auth as DA
from services import admin_login_otp as OTP
from services import admin_otp_test_mode as TM

DB = sys.modules["database"].db
ROOT = os.path.dirname(os.path.abspath(server.__file__))
MOBILE, OTHER, PIN, CODE = "9000000100", "9000000200", "1234", "1234"
STAGING = {"RAILWAY_PROJECT_ID": TM.STAGING_PROJECT_ID, "RAILWAY_ENVIRONMENT_ID": TM.STAGING_ENVIRONMENT_ID,
           "RAILWAY_SERVICE_ID": TM.STAGING_SERVICE_ID, "RAILWAY_PUBLIC_DOMAIN": TM.STAGING_PUBLIC_DOMAIN}
MONGO = "mongodb://dbuser:dbpass-not-real@mongodb.railway.internal:27017"
PROD = {"RAILWAY_PROJECT_ID": "proj-prod-9", "RAILWAY_ENVIRONMENT_ID": "env-prod-9", "RAILWAY_SERVICE_ID": "svc-prod-9",
        "RAILWAY_PUBLIC_DOMAIN": "offro-backend-production.up.railway.app", "MONGO_URL": "mongodb://u:p@prod-mongo.proxy.rlwy.net:4242"}
SHARED = ("dashboard_users", "dashboard_roles", "activity_logs", "trusted_devices")
OWN = ("admin_login_challenges", "admin_auth_throttle")
VARS = ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_PUBLIC_DOMAIN", "RAILWAY_ENVIRONMENT_NAME", "MONGO_URL",
        "ADMIN_OTP_STAGING_TEST_MODE", "ADMIN_OTP_TEST_MOBILES")


class SMS:
    def __init__(self):
        self.sent, self.ok = [], True

    def __call__(self, mobile, otp, template_id):
        self.sent.append(otp)
        return {"ok": self.ok}


def configure(mp, **over):
    """A correctly configured Staging service; keyword overrides replace (or remove, with None) individual variables."""
    env = dict(STAGING, ADMIN_OTP_STAGING_TEST_MODE="1", MONGO_URL=MONGO, ADMIN_OTP_TEST_MOBILES=MOBILE + ",9111111111")
    env.update(over)
    for k in VARS:
        mp.delenv(k, raising=False)
    for k, v in env.items():
        if v is not None:
            mp.setenv(k, v)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    saved = {c: list(DB[c].find({})) for c in SHARED}
    for c in SHARED + OWN:
        DB[c].delete_many({})
    sms = SMS()
    monkeypatch.setattr(OTP, "_send_sms", sms)
    configure(monkeypatch)
    yield sms
    for c in SHARED + OWN:
        DB[c].delete_many({})
    for c, docs in saved.items():
        if docs:
            DB[c].insert_many(docs)


@pytest.fixture
def sms(env):
    return env


def client():
    return TestClient(server.app, raise_server_exceptions=False)


def mk_user(mobile=MOBILE, **kw):
    r = DB.dashboard_roles.find_one({"role_name": "Super Admin"})
    rid = r["_id"] if r else DB.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    d = {"mobile": mobile, "full_name": "Test Admin", "pin": DA.hash_pin(PIN), "status": "active", "role_id": rid}
    d.update(kw)
    DB.dashboard_users.insert_one(d)


def login(c, mobile=MOBILE, pin=PIN, **h):
    return c.post("/admin/auth/login", json={"mobile": mobile, "pin": pin}, headers=h)


def verify(c, cid, otp):
    return c.post("/admin/auth/verify-otp", json={"challenge_id": cid, "otp": otp})


def resend(c, cid):
    return c.post("/admin/auth/resend-otp", json={"challenge_id": cid})


def age(seconds=31):
    DB.admin_login_challenges.update_one({}, {"$set": {"last_sent_at": datetime.utcnow() - timedelta(seconds=seconds)}})


# ── environment verification ──────────────────────────────────────────────────────────────────────

def test_a_correctly_configured_staging_service_activates_test_mode():
    assert TM.status() == (True, "ok")


@pytest.mark.parametrize("over,reason", [
    ({"ADMIN_OTP_STAGING_TEST_MODE": None}, "flag_off"),
    ({"ADMIN_OTP_STAGING_TEST_MODE": "false"}, "flag_off"),
    ({"ADMIN_OTP_STAGING_TEST_MODE": "0"}, "flag_off"),
    ({"RAILWAY_PROJECT_ID": "proj-PRODUCTION"}, "project_mismatch"),
    ({"RAILWAY_ENVIRONMENT_ID": "env-PRODUCTION"}, "environment_mismatch"),
    ({"RAILWAY_SERVICE_ID": "svc-PRODUCTION"}, "service_mismatch"),
    ({"RAILWAY_PROJECT_ID": None}, "not_on_railway"),
    ({"RAILWAY_ENVIRONMENT_ID": None}, "not_on_railway"),
    ({"RAILWAY_SERVICE_ID": None}, "not_on_railway"),
    ({"RAILWAY_PUBLIC_DOMAIN": None}, "domain_mismatch"),
    ({"RAILWAY_PUBLIC_DOMAIN": "offro-backend-production.up.railway.app"}, "domain_mismatch"),
    ({"RAILWAY_PUBLIC_DOMAIN": "admin.theoffro.com"}, "domain_mismatch"),
    ({"RAILWAY_PUBLIC_DOMAIN": "offro-backend-staging-other.up.railway.app"}, "domain_mismatch"),
    ({"RAILWAY_PUBLIC_DOMAIN": "evil-offro-backend-staging-d375.up.railway.app"}, "domain_mismatch"),
    ({"MONGO_URL": None}, "database_unverified"),
    ({"MONGO_URL": "mongodb://u:p@prod-mongo.proxy.rlwy.net:1234"}, "database_mismatch"),
    ({"MONGO_URL": "mongodb+srv://u:p@cluster0.abcde.mongodb.net/offro_db"}, "database_mismatch"),
    ({"MONGO_URL": "mongodb://u:p@mongodb.railway.internal:27017,prod-mongo.example.com:27017/x"}, "database_mismatch"),
    ({"MONGO_URL": "mongodb://u:p@evil-mongodb.railway.internal:27017"}, "database_mismatch"),
    ({"ADMIN_OTP_TEST_MOBILES": None}, "allow_list_empty"),
    ({"ADMIN_OTP_TEST_MOBILES": "123, abc,,"}, "allow_list_empty"),
])
def test_every_failed_check_deactivates_test_mode_and_fails_closed(monkeypatch, over, reason):
    configure(monkeypatch, **over)
    assert TM.status() == (False, reason)
    assert TM.is_active() is False and TM.applies_to(MOBILE) is False


def test_production_can_never_activate_test_mode_even_with_the_flag_on(monkeypatch):
    configure(monkeypatch, RAILWAY_ENVIRONMENT_NAME="production", **PROD)
    assert TM.is_active() is False
    configure(monkeypatch, RAILWAY_PUBLIC_DOMAIN=PROD["RAILWAY_PUBLIC_DOMAIN"])                  # even with every Staging ID copied across
    assert TM.status() == (False, "domain_mismatch")
    configure(monkeypatch, MONGO_URL=PROD["MONGO_URL"])
    assert TM.status() == (False, "database_mismatch")


def test_an_unknown_environment_with_no_railway_identity_is_inactive(monkeypatch):
    configure(monkeypatch, RAILWAY_PROJECT_ID=None, RAILWAY_ENVIRONMENT_ID=None, RAILWAY_SERVICE_ID=None, RAILWAY_PUBLIC_DOMAIN=None)
    assert TM.status() == (False, "not_on_railway")


def test_the_environment_name_is_not_used_so_a_staging_environment_named_production_still_works(monkeypatch):
    configure(monkeypatch, RAILWAY_ENVIRONMENT_NAME="production")
    assert TM.status() == (True, "ok")


def test_an_internal_error_in_the_checks_fails_closed(monkeypatch):
    monkeypatch.setattr(TM, "_env", lambda name: (_ for _ in ()).throw(RuntimeError("boom")))
    assert TM.status() == (False, "check_error")


def test_the_pinned_staging_ids_are_well_formed_uuids_and_not_placeholders():
    uuid = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    ids = (TM.STAGING_PROJECT_ID, TM.STAGING_ENVIRONMENT_ID, TM.STAGING_SERVICE_ID)
    assert all(uuid.fullmatch(i) for i in ids) and len(set(ids)) == 3
    assert TM.STAGING_SERVICE_ID == "756296c3-d40d-4d5b-8483-c9c9ff922e87"          # as read from the Railway service page and URL


def test_there_are_no_retrieval_or_key_settings_left():
    for gone in ("derive_otp", "key_matches", "MIN_KEY_LENGTH"):
        assert not hasattr(TM, gone)
    assert not hasattr(OTP, "reveal_test_otp")
    r = client().post("/admin/auth/staging-test-otp", json={"mobile": MOBILE})
    assert r.status_code in (404, 405) and "otp" not in r.text.lower().replace("not found", "")


def test_the_startup_line_never_contains_ids_hosts_numbers_or_the_code(capsys):
    OTP.log_test_mode_status()
    out = capsys.readouterr().out
    assert "ACTIVE" in out
    for secret in (MOBILE, TM.STAGING_PROJECT_ID, TM.STAGING_ENVIRONMENT_ID, TM.STAGING_SERVICE_ID, "mongodb.railway", "dbpass-not-real",
                   "offro-backend-staging", "1234"):
        assert secret not in out


def test_the_startup_line_reports_inactive_with_a_code(monkeypatch, capsys):
    configure(monkeypatch, ADMIN_OTP_STAGING_TEST_MODE=None)
    OTP.log_test_mode_status()
    assert "INACTIVE (flag_off)" in capsys.readouterr().out


def test_mongo_url_parsing_handles_credentials_ports_multiple_hosts_and_ipv6():
    assert TM._mongo_hosts("mongodb://u:p%40ss@Host.Example:27017/db?x=1") == ["host.example"]
    assert TM._mongo_hosts("mongodb://u:p@a:1,b:2/db") == ["a", "b"]
    assert TM._mongo_hosts("mongodb+srv://u:p@c.mongodb.net/db") == ["c.mongodb.net"]
    assert TM._mongo_hosts("mongodb://[::1]:27017") == ["::1"]
    assert TM._mongo_hosts(MONGO) == ["mongodb.railway.internal"]
    assert TM._mongo_hosts("") == []


# ── the fixed-code flow ────────────────────────────────────────────────────────────────────────────

def test_an_allow_listed_admin_logs_in_with_the_fixed_code_and_no_sms_is_sent(sms):
    mk_user()
    c = client()
    r = login(c)
    assert r.status_code == 200 and r.json()["otp_required"] is True and "admin_token" not in r.cookies
    assert sms.sent == []
    doc = DB.admin_login_challenges.find_one({})
    assert doc["test_mode"] is True and doc["otp_hash"] != CODE and "otp" not in doc and "test_revealed" not in doc
    assert OTP.hash_otp(CODE, doc["salt"], doc["_id"], doc["user_id"]) == doc["otp_hash"]      # the normal salted, bound hash of the fixed code
    ok = verify(c, r.json()["challenge_id"], CODE)
    assert ok.status_code == 200 and "admin_token" in ok.cookies


def test_the_pin_is_verified_first_and_a_wrong_pin_creates_no_challenge(sms):
    mk_user()
    r = login(client(), pin="9999")
    assert r.status_code == 401 and DB.admin_login_challenges.count_documents({}) == 0 and sms.sent == []
    assert verify(client(), "x" * 40, CODE).status_code == 401


def test_the_fixed_code_does_not_work_for_a_number_that_is_not_allow_listed(sms):
    mk_user(mobile=OTHER)
    c = client()
    r = login(c, OTHER)
    assert r.status_code == 200 and len(sms.sent) == 1 and DB.admin_login_challenges.find_one({})["test_mode"] is False
    real = sms.sent[0]
    if real != CODE:                                                                                   # (1 in 10000 the random code is 1234)
        assert verify(c, r.json()["challenge_id"], CODE).status_code == 401
        assert verify(c, r.json()["challenge_id"], real).status_code == 200


def test_without_msg91_a_non_allow_listed_admin_still_cannot_sign_in(sms):
    mk_user(mobile=OTHER)
    sms.ok = False
    r = login(client(), OTHER)
    assert r.status_code == 503 and "admin_token" not in r.cookies


def test_an_unknown_mobile_gets_a_refusal_no_sms_and_no_session(sms):
    r = login(client(), "9000000999")
    assert r.status_code == 401 and sms.sent == [] and "admin_token" not in r.cookies and "challenge_id" not in r.text
    assert DB.admin_login_challenges.count_documents({"test_mode": True}) == 0


def test_production_issues_real_sms_codes_and_rejects_1234(sms, monkeypatch):
    configure(monkeypatch, **PROD)                                                                       # flag and allow-list copied to Production by mistake
    mk_user()
    c = client()
    r = login(c)
    assert r.status_code == 200 and len(sms.sent) == 1 and DB.admin_login_challenges.find_one({})["test_mode"] is False
    if sms.sent[0] != CODE:
        assert verify(c, r.json()["challenge_id"], CODE).status_code == 401
    sms.ok = False
    DB.admin_auth_throttle.delete_many({})
    r = login(client())
    assert r.status_code == 503 and "admin_token" not in r.cookies and "challenge_id" not in r.text


def test_a_staging_service_pointed_at_a_production_database_issues_real_codes(sms, monkeypatch):
    configure(monkeypatch, MONGO_URL=PROD["MONGO_URL"])
    mk_user()
    r = login(client())
    assert len(sms.sent) == 1 and DB.admin_login_challenges.find_one({})["test_mode"] is False


# ── every OTP protection still applies in test mode ────────────────────────────────────────────────

def test_wrong_codes_lock_after_five_attempts_and_the_right_code_is_then_refused(sms):
    mk_user()
    c = client()
    cid = login(c).json()["challenge_id"]
    for _ in range(5):
        assert verify(c, cid, "0000").status_code == 401
    assert DB.admin_login_challenges.find_one({})["status"] == "locked"
    assert verify(c, cid, CODE).status_code == 401 and not DB.dashboard_users.find_one({"mobile": MOBILE}).get("token")


def test_an_expired_code_is_refused(sms):
    mk_user()
    c = client()
    cid = login(c).json()["challenge_id"]
    DB.admin_login_challenges.update_one({}, {"$set": {"expires_at": datetime.utcnow() - timedelta(seconds=1)}})
    assert verify(c, cid, CODE).status_code == 401 and not DB.dashboard_users.find_one({"mobile": MOBILE}).get("token")


def test_the_code_is_single_use_and_cannot_be_replayed(sms):
    mk_user()
    c = client()
    cid = login(c).json()["challenge_id"]
    assert verify(c, cid, CODE).status_code == 200
    assert verify(client(), cid, CODE).status_code == 401


def test_concurrent_verification_has_one_winner(sms):
    mk_user()
    cid = login(client()).json()["challenge_id"]
    res, barrier = [], threading.Barrier(8)

    def go():
        cl = client()
        barrier.wait()
        res.append(verify(cl, cid, CODE).status_code)
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert res.count(200) == 1


def test_a_challenge_is_bound_to_its_own_account_and_challenge_id(sms):
    mk_user()
    mk_user(mobile="9111111111")
    cid_a = login(client()).json()["challenge_id"]
    cid_b = login(client(), "9111111111").json()["challenge_id"]
    a = DB.admin_login_challenges.find_one({"user_id": DB.dashboard_users.find_one({"mobile": MOBILE})["_id"]})
    b = DB.admin_login_challenges.find_one({"user_id": DB.dashboard_users.find_one({"mobile": "9111111111"})["_id"]})
    DB.admin_login_challenges.update_one({"_id": a["_id"]}, {"$set": {"otp_hash": b["otp_hash"], "salt": b["salt"]}})   # B's hash grafted onto A
    assert verify(client(), cid_a, CODE).status_code == 401                                                              # does not validate for A
    assert verify(client(), cid_b, CODE).status_code == 200 and cid_a != cid_b


def test_resend_cooldown_cap_and_it_reissues_the_same_fixed_code_without_sms(sms):
    mk_user()
    c = client()
    cid = login(c).json()["challenge_id"]
    assert resend(c, cid).status_code == 429                                                             # inside the 30 s cooldown
    for i in range(3):
        before = DB.admin_login_challenges.find_one({})
        age()
        assert resend(c, cid).status_code == 200
        after = DB.admin_login_challenges.find_one({})
        assert after["resend_count"] == i + 1 and after["salt"] != before["salt"] and after["otp_hash"] != before["otp_hash"]
        assert OTP.hash_otp(CODE, after["salt"], after["_id"], after["user_id"]) == after["otp_hash"]    # fresh salt, still 1234
    age()
    assert resend(c, cid).status_code == 401                                                             # at most 3 resends
    assert sms.sent == []
    assert verify(c, cid, CODE).status_code == 200


def test_the_sms_budget_is_not_used_up_in_test_mode_because_no_sms_is_sent(sms):
    mk_user()
    codes = [login(client(), **{"x-forwarded-for": "10.0.0.%d" % i}).status_code for i in range(15)]
    assert codes == [200] * 15 and sms.sent == [] and DB.admin_auth_throttle.count_documents({"kind": "otp_send_acct"}) == 0


def test_a_test_challenge_stops_working_if_test_mode_stops_verifying(sms, monkeypatch):
    mk_user()
    c = client()
    cid = login(c).json()["challenge_id"]
    configure(monkeypatch, RAILWAY_ENVIRONMENT_ID="env-somewhere-else")
    assert verify(c, cid, CODE).status_code == 401
    age()
    assert resend(c, cid).status_code == 401
    assert not DB.dashboard_users.find_one({"mobile": MOBILE}).get("token")


def test_no_session_cookie_or_token_exists_before_the_code_is_verified(sms):
    mk_user()
    c = client()
    r = login(c)
    assert "admin_token" not in r.cookies and "set-cookie" not in {k.lower() for k in r.headers}
    assert not DB.dashboard_users.find_one({"mobile": MOBILE}).get("token")


def test_neither_the_pin_nor_the_code_nor_the_number_reaches_logs_or_records(sms, capsys):
    mk_user()
    c = client()
    cid = login(c).json()["challenge_id"]
    verify(c, cid, "0000")
    verify(c, cid, CODE)
    out = capsys.readouterr()
    text = (out.out + out.err).replace(cid, "")
    assert MOBILE not in text and "dbpass-not-real" not in text and "pin" not in text.lower().replace("pinned", "")
    for coll in ("admin_login_challenges", "activity_logs", "admin_auth_throttle"):
        for row in DB[coll].find({}):
            assert "otp" not in row and "pin" not in row and CODE not in [str(v) for v in row.values()]


# ── friendly errors on the page and from the API ──────────────────────────────────────────────────

def test_a_server_failure_is_friendly_has_a_log_reference_and_leaks_nothing(sms, monkeypatch, capsys):
    mk_user()
    monkeypatch.setattr(OTP, "start_challenge", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mongodb://u:secret@host Traceback")))
    r = login(client())
    assert r.status_code == 500
    m = re.fullmatch(r"We could not complete sign-in\. Please try again\. \(ref ([0-9a-f]{8})\)", r.json()["detail"])
    assert m and "secret" not in r.text and "Traceback" not in r.text
    log = capsys.readouterr().out
    assert "ref=" + m.group(1) in log and "type=RuntimeError" in log and "secret" not in log and MOBILE not in log


def test_the_login_page_offers_retry_for_transient_failures_and_mentions_no_test_mode():
    page = open(os.path.join(ROOT, "templates", "admin_login.html"), encoding="utf-8").read()
    for needle in ('id="retryBtn"', 'id="retryVerifyBtn"', "function transientFailure", "function retryLogin", "Try again"):
        assert needle in page, needle
    low = page.lower()
    for bad in ("staging-test-otp", "admin_otp_", "test mode", "x-admin-otp-test-key", "1234", "localstorage", "sessionstorage"):
        assert bad not in low, bad
    assert re.search(r"if \(!transientFailure\(res\)\) document\.getElementById\(\"pin\"\)\.value = \"\"", page)
