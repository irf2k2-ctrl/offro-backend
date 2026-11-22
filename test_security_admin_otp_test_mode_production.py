"""Production release gate: the Staging-only fixed OTP (1234) can NEVER activate in Production, whatever is misconfigured.
Every identity check must fail closed and Production must always use a real, random, MSG91-delivered OTP. No network, no Railway, no SMS.
Run:  python -m pytest test_security_admin_otp_test_mode_production.py -q -W ignore"""
import itertools
import os
import sys

import pytest
from fastapi.testclient import TestClient

import server
from routers import dashboard_auth as DA
from services import admin_login_otp as OTP
from services import admin_otp_test_mode as TM

DB = sys.modules["database"].db
MOBILE, PIN = "9000000100", "1234"
STAGING = {"RAILWAY_PROJECT_ID": TM.STAGING_PROJECT_ID, "RAILWAY_ENVIRONMENT_ID": TM.STAGING_ENVIRONMENT_ID,
           "RAILWAY_SERVICE_ID": TM.STAGING_SERVICE_ID, "RAILWAY_PUBLIC_DOMAIN": TM.STAGING_PUBLIC_DOMAIN,
           "MONGO_URL": "mongodb://u:p@" + TM.STAGING_MONGO_HOST + ":27017"}
PROD = {"RAILWAY_PROJECT_ID": "11111111-aaaa-4aaa-8aaa-111111111111", "RAILWAY_ENVIRONMENT_ID": "22222222-bbbb-4bbb-8bbb-222222222222",
        "RAILWAY_SERVICE_ID": "33333333-cccc-4ccc-8ccc-333333333333", "RAILWAY_PUBLIC_DOMAIN": "offro-backend-production.up.railway.app",
        "MONGO_URL": "mongodb://u:p@prod-mongo.proxy.rlwy.net:4242"}
# Everything an operator could mistakenly copy from Staging to Production:
STAGING_SWITCHES = {"ADMIN_OTP_STAGING_TEST_MODE": "1", "ADMIN_OTP_TEST_MOBILES": MOBILE, "RAILWAY_ENVIRONMENT_NAME": "production",
                    "WA_CAMPAIGN_ENVIRONMENT": "production"}
ALL_VARS = tuple(STAGING) + tuple(STAGING_SWITCHES) + ("APP_ENV", "ENVIRONMENT", "ENV", "RAILWAY_ENVIRONMENT")
SHARED = ("dashboard_users", "dashboard_roles", "activity_logs", "trusted_devices")
OWN = ("admin_login_challenges", "admin_auth_throttle", "admin_trusted_devices")


class SMS(list):
    ok = True

    def __call__(self, mobile, otp, tpl):
        self.append(otp)
        return {"ok": self.ok}


def set_env(mp, **env):
    for k in ALL_VARS:
        mp.delenv(k, raising=False)
    for k, v in env.items():
        if v is not None:
            mp.setenv(k, v)


@pytest.fixture(autouse=True)
def sms(monkeypatch):
    saved = {c: list(DB[c].find({})) for c in SHARED}
    for c in SHARED + OWN:
        DB[c].delete_many({})
    s = SMS()
    monkeypatch.setattr(OTP, "_send_sms", s)
    yield s
    for c in SHARED + OWN:
        DB[c].delete_many({})
    for c, docs in saved.items():
        if docs:
            DB[c].insert_many(docs)


def mk_user():
    r = DB.dashboard_roles.find_one({"role_name": "Super Admin"})
    rid = r["_id"] if r else DB.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    DB.dashboard_users.insert_one({"mobile": MOBILE, "full_name": "Prod Admin", "pin": DA.hash_pin(PIN), "status": "active", "role_id": rid})


def client():
    return TestClient(server.app, raise_server_exceptions=False)


# ── the identity check, exhaustively ───────────────────────────────────────────────────────────────

def test_the_staging_constants_are_not_production_values():
    assert TM.STAGING_PUBLIC_DOMAIN not in TM.PRODUCTION_DOMAINS and not TM.STAGING_PUBLIC_DOMAIN.endswith("theoffro.com")
    assert TM.TEST_OTP == "1234"                                                         # pinned: any change must be a deliberate, reviewed edit


@pytest.mark.parametrize("copied", [c for n in range(0, 5) for c in itertools.combinations(sorted(STAGING), n)])
def test_any_mix_of_staging_and_production_identity_stays_inactive(monkeypatch, copied):
    """Every combination of 'this Staging value was copied into Production' (0-4 of the five identity values) leaves test mode OFF,
    even with the switch and the allow-list set."""
    env = dict(PROD)
    env.update({k: STAGING[k] for k in copied})
    env.update({"ADMIN_OTP_STAGING_TEST_MODE": "1", "ADMIN_OTP_TEST_MOBILES": MOBILE})
    set_env(monkeypatch, **env)
    assert TM.is_active() is False and TM.applies_to(MOBILE) is False
    assert TM.status()[0] is False


def test_only_the_complete_staging_identity_activates_it_and_that_is_the_sole_positive_case(monkeypatch):
    set_env(monkeypatch, **STAGING, ADMIN_OTP_STAGING_TEST_MODE="1", ADMIN_OTP_TEST_MOBILES=MOBILE)
    assert TM.status() == (True, "ok")                                                   # control: the guard is not simply always-off
    for k in STAGING:                                                                     # remove or change any single value: inactive
        for bad in (None, "", "x", PROD.get(k, "other")):
            env = dict(STAGING, ADMIN_OTP_STAGING_TEST_MODE="1", ADMIN_OTP_TEST_MOBILES=MOBILE)
            env[k] = bad
            set_env(monkeypatch, **env)
            assert TM.is_active() is False, (k, bad)


@pytest.mark.parametrize("domain", list(TM.PRODUCTION_DOMAINS) + ["api.theoffro.com", "OFFRO-BACKEND-PRODUCTION.UP.RAILWAY.APP", "",
                                                                   TM.STAGING_PUBLIC_DOMAIN + ".theoffro.com"])
def test_production_domains_are_refused_even_with_every_other_staging_value(monkeypatch, domain):
    set_env(monkeypatch, **dict(STAGING, RAILWAY_PUBLIC_DOMAIN=domain), ADMIN_OTP_STAGING_TEST_MODE="1", ADMIN_OTP_TEST_MOBILES=MOBILE)
    assert TM.is_active() is False


@pytest.mark.parametrize("url", ["mongodb://u:p@prod-mongo.proxy.rlwy.net:4242", "mongodb+srv://u:p@cluster0.abc.mongodb.net/offro_db",
                                 "mongodb://u:p@" + TM.STAGING_MONGO_HOST + ":1,prod.example.com:2/x", "", "garbage", "mongodb://"])
def test_a_production_or_unverifiable_database_host_is_refused(monkeypatch, url):
    set_env(monkeypatch, **dict(STAGING, MONGO_URL=url), ADMIN_OTP_STAGING_TEST_MODE="1", ADMIN_OTP_TEST_MOBILES=MOBILE)
    assert TM.is_active() is False


@pytest.mark.parametrize("flag", [None, "", "0", "false", "no", "off", "2", "enabled", "True1"])
def test_the_switch_alone_is_never_trusted_and_only_a_truthy_value_counts(monkeypatch, flag):
    set_env(monkeypatch, **PROD, ADMIN_OTP_TEST_MOBILES=MOBILE, **({} if flag is None else {"ADMIN_OTP_STAGING_TEST_MODE": flag}))
    assert TM.is_active() is False


def test_running_outside_railway_or_locally_is_inactive(monkeypatch):
    set_env(monkeypatch, ADMIN_OTP_STAGING_TEST_MODE="1", ADMIN_OTP_TEST_MOBILES=MOBILE)
    assert TM.status() == (False, "not_on_railway")
    set_env(monkeypatch, MONGO_URL=STAGING["MONGO_URL"], ADMIN_OTP_STAGING_TEST_MODE="1", ADMIN_OTP_TEST_MOBILES=MOBILE)
    assert TM.is_active() is False


@pytest.mark.parametrize("boom", [RuntimeError, KeyError, UnicodeError, TypeError])
def test_any_error_while_checking_fails_closed(monkeypatch, boom):
    set_env(monkeypatch, **STAGING, ADMIN_OTP_STAGING_TEST_MODE="1", ADMIN_OTP_TEST_MOBILES=MOBILE)
    monkeypatch.setattr(TM, "_mongo_hosts", lambda url: (_ for _ in ()).throw(boom("x")))
    assert TM.status() == (False, "check_error") and TM.applies_to(MOBILE) is False


def test_the_environment_name_and_generic_labels_never_enable_or_disable_it(monkeypatch):
    set_env(monkeypatch, **PROD, RAILWAY_ENVIRONMENT_NAME="staging", APP_ENV="staging", ENVIRONMENT="staging", ENV="staging",
            WA_CAMPAIGN_ENVIRONMENT="staging", ADMIN_OTP_STAGING_TEST_MODE="1", ADMIN_OTP_TEST_MOBILES=MOBILE)
    assert TM.is_active() is False                                                       # calling Production "staging" changes nothing


# ── behaviour in a Production-like process: real OTP only ─────────────────────────────────────────────

PROD_WITH_COPIED_SWITCHES = dict(PROD, ADMIN_OTP_STAGING_TEST_MODE="1", ADMIN_OTP_TEST_MOBILES=MOBILE, RAILWAY_ENVIRONMENT_NAME="production")


def test_production_login_sends_a_real_sms_code_and_never_uses_the_fixed_code(monkeypatch, sms):
    set_env(monkeypatch, **PROD_WITH_COPIED_SWITCHES)
    monkeypatch.setattr(OTP, "generate_otp", lambda: "5678")                              # deterministic 'random' code, never 1234
    mk_user()
    c = client()
    r = c.post("/admin/auth/login", json={"mobile": MOBILE, "pin": PIN})
    assert r.status_code == 200 and r.json()["otp_required"] is True
    assert sms == ["5678"]                                                                # exactly one real SMS carrying the generated code
    ch = DB.admin_login_challenges.find_one({})
    assert ch["test_mode"] is False
    cid = r.json()["challenge_id"]
    bad = c.post("/admin/auth/verify-otp", json={"challenge_id": cid, "otp": "1234"})
    assert bad.status_code == 401 and "admin_token" not in bad.cookies and "offro_td" not in bad.cookies
    good = c.post("/admin/auth/verify-otp", json={"challenge_id": cid, "otp": "5678"})
    assert good.status_code == 200 and "admin_token" in good.cookies


def test_production_with_no_working_sms_refuses_the_login_instead_of_falling_back_to_1234(monkeypatch, sms):
    set_env(monkeypatch, **PROD_WITH_COPIED_SWITCHES)
    sms.ok = False
    mk_user()
    c = client()
    r = c.post("/admin/auth/login", json={"mobile": MOBILE, "pin": PIN})
    assert r.status_code == 503 and "challenge_id" not in r.text and "admin_token" not in r.cookies
    assert DB.admin_login_challenges.count_documents({"status": "pending"}) == 0 or all(
        d.get("test_mode") is False for d in DB.admin_login_challenges.find({}))
    assert c.post("/admin/auth/verify-otp", json={"challenge_id": "x" * 43, "otp": "1234"}).status_code == 401


def test_a_test_mode_challenge_copied_into_a_production_database_is_unusable(monkeypatch, sms):
    """E.g. a Staging backup restored into Production: its 1234 challenges must not log anyone in."""
    set_env(monkeypatch, **STAGING, ADMIN_OTP_STAGING_TEST_MODE="1", ADMIN_OTP_TEST_MOBILES=MOBILE)
    mk_user()
    r = client().post("/admin/auth/login", json={"mobile": MOBILE, "pin": PIN})
    assert r.status_code == 200 and sms == [] and DB.admin_login_challenges.find_one({})["test_mode"] is True      # created on Staging
    set_env(monkeypatch, **PROD_WITH_COPIED_SWITCHES)                                                              # ...then seen from Production
    c = client()
    v = c.post("/admin/auth/verify-otp", json={"challenge_id": r.json()["challenge_id"], "otp": "1234"})
    assert v.status_code == 401 and "admin_token" not in v.cookies
    rs = c.post("/admin/auth/resend-otp", json={"challenge_id": r.json()["challenge_id"]})
    assert rs.status_code in (401, 429) and sms == []


def test_the_startup_line_reports_inactive_in_production_and_leaks_nothing(monkeypatch, capsys):
    set_env(monkeypatch, **PROD_WITH_COPIED_SWITCHES)
    OTP.log_test_mode_status()
    out = capsys.readouterr().out
    assert "INACTIVE" in out and "ACTIVE (" not in out.replace("INACTIVE", "")
    for secret in (MOBILE, "1234", PROD["RAILWAY_SERVICE_ID"], PROD["MONGO_URL"]):
        assert secret not in out


# ── nothing else can switch it on ──────────────────────────────────────────────────────────────────────

def test_the_fixed_code_is_used_only_through_the_guarded_helpers():
    root = os.path.dirname(os.path.abspath(server.__file__))
    hits = []
    for folder in ("routers", "services", "templates", "static"):
        for dp, _, files in os.walk(os.path.join(root, folder)):
            for f in files:
                if f.endswith((".py", ".html", ".js")):
                    p = os.path.join(dp, f)
                    s = open(p, encoding="utf-8", errors="ignore").read()
                    if "TEST_OTP" in s or "test_mode" in s or "admin_otp_test_mode" in s:
                        hits.append(os.path.relpath(p, root).replace(os.sep, "/"))
    # services/app_environment.py is the only other file allowed to mention the module, and only to read the three Railway STAGING
    # identifiers (constants) it uses to recognise Staging. It can never reach the fixed code or the activation switch: the strict
    # check right below pins exactly what it may touch. (It was added after this list was written, in the same commit as this test.)
    assert sorted(hits) == ["services/admin_login_otp.py", "services/admin_otp_test_mode.py", "services/app_environment.py"]
    env_src = open(os.path.join(root, "services", "app_environment.py"), encoding="utf-8").read()
    import re
    assert set(re.findall(r"\b_m\.(\w+)", env_src)) == {"STAGING_PROJECT_ID", "STAGING_ENVIRONMENT_ID", "STAGING_SERVICE_ID"}
    assert env_src.count("admin_otp_test_mode") == 1 and "TEST_OTP" not in env_src and "test_mode" not in env_src.replace("admin_otp_test_mode", "")
    for forbidden in ("is_active", "applies_to", "mobile_allowed", "allowed_digits", "ADMIN_OTP_STAGING_TEST_MODE", "ADMIN_OTP_TEST_MOBILES"):
        assert forbidden not in env_src, forbidden
    src = open(os.path.join(root, "services", "admin_login_otp.py"), encoding="utf-8").read()
    assert src.count("TM.TEST_OTP") == 2 and src.count("TM.applies_to(") == 1 and src.count("TM.is_active()") >= 2


def test_the_login_page_and_api_never_mention_the_fixed_code_or_test_mode():
    root = os.path.dirname(os.path.abspath(server.__file__))
    page = open(os.path.join(root, "templates", "admin_login.html"), encoding="utf-8").read().lower()
    assert "1234" not in page and "test mode" not in page and "test_mode" not in page
    r = client().post("/admin/auth/login", json={"mobile": "9000000999", "pin": "0000"})
    assert "1234" not in r.text and "test" not in r.text.lower()


def test_no_test_mode_variable_has_a_default_that_could_enable_it():
    src = open(TM.__file__, encoding="utf-8").read()
    assert 'getenv(name, "")' in src and "ADMIN_OTP_STAGING_TEST_MODE" in src
