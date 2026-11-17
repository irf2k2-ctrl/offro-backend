"""Admin login through the existing MSG91 OTP widget: the server never trusts the browser, never sends an SMS itself, and Production can never
use a fixed code. No network: MSG91's verifyAccessToken is replaced by a fake that returns whatever each test needs.
Run:  python -m pytest test_security_admin_otp_widget.py -q -W ignore"""
import base64
import json
import sys
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import server
from routers import dashboard_auth as DA
from services import admin_login_otp as OTP
from services import admin_otp_test_mode as TM
from services import msg91_widget as W

DB = sys.modules["database"].db
_REAL_SEND_SMS, _REAL_VERIFY = OTP._send_sms, W.verify_access_token          # the originals, before any fixture replaces them
MOBILE, PIN = "9000000200", "4321"
WIDGET_ID, WIDGET_TOKEN, AUTH_KEY = "webwid123", "public-web-token-xyz", "SERVER-SECRET-KEY"
MOBILE_WIDGET_ID, MOBILE_WIDGET_TOKEN = "mobwid999", "public-mobile-token-abc"
STAGING = {"RAILWAY_PROJECT_ID": TM.STAGING_PROJECT_ID, "RAILWAY_ENVIRONMENT_ID": TM.STAGING_ENVIRONMENT_ID,
           "RAILWAY_SERVICE_ID": TM.STAGING_SERVICE_ID, "RAILWAY_PUBLIC_DOMAIN": TM.STAGING_PUBLIC_DOMAIN,
           "MONGO_URL": "mongodb://u:p@" + TM.STAGING_MONGO_HOST + ":27017"}
PROD = {"RAILWAY_PROJECT_ID": "11111111-aaaa-4aaa-8aaa-111111111111", "RAILWAY_ENVIRONMENT_ID": "22222222-bbbb-4bbb-8bbb-222222222222",
        "RAILWAY_SERVICE_ID": "33333333-cccc-4ccc-8ccc-333333333333", "RAILWAY_PUBLIC_DOMAIN": "offro-backend-production.up.railway.app",
        "MONGO_URL": "mongodb://u:p@prod-mongo.proxy.rlwy.net:4242"}
OWN = ("admin_login_challenges", "admin_auth_throttle", "admin_trusted_devices", "admin_widget_tokens", "dashboard_users")


def fake_token(n="a"):
    return "eyJhbGciOiJIUzI1NiJ9." + base64.urlsafe_b64encode(json.dumps({"n": n}).encode()).decode().rstrip("=") + ".sig" + n


class Verifier:
    """Stands in for MSG91 verifyAccessToken."""
    def __init__(self):
        self.calls = []
        self.reply = None

    def __call__(self, token):
        self.calls.append(token)
        return self.reply(token) if callable(self.reply) else dict(self.reply)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for n in ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_PUBLIC_DOMAIN", "MONGO_URL",
              "ADMIN_OTP_STAGING_TEST_MODE", "ADMIN_OTP_TEST_MOBILES", "ADMIN_TRUSTED_DEVICE_SUPER_ADMIN"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("MSG91_WEB_WIDGET_ID", WIDGET_ID)
    monkeypatch.setenv("MSG91_WEB_WIDGET_TOKEN_AUTH", WIDGET_TOKEN)
    monkeypatch.setenv("MSG91_WIDGET_ID", MOBILE_WIDGET_ID)                   # the mobile app's widget is present too and must never be used here
    monkeypatch.setenv("MSG91_WIDGET_TOKEN_AUTH", MOBILE_WIDGET_TOKEN)
    monkeypatch.setenv("MSG91_AUTH_KEY", AUTH_KEY)
    monkeypatch.setattr(OTP, "REAL_PROVIDER", "widget")
    for c in OWN:
        DB[c].delete_many({})

    def boom(*a, **k):
        raise AssertionError("an SMS was sent by this server in widget mode")
    monkeypatch.setattr(OTP, "_send_sms", boom)
    from routers import otp_service
    monkeypatch.setattr(otp_service, "send_sms_otp", boom)
    v = Verifier()
    v.reply = {"ok": True, "identifier": "91" + MOBILE, "claims": {}}
    monkeypatch.setattr(W, "verify_access_token", v)
    yield v
    for c in OWN:
        DB[c].delete_many({})


def mk_user(role="Admin", mobile=MOBILE):
    r = DB.dashboard_roles.find_one({"role_name": role})
    rid = r["_id"] if r else DB.dashboard_roles.insert_one({"role_name": role, "permissions": {}}).inserted_id
    return DB.dashboard_users.insert_one({"mobile": mobile, "full_name": "Widget Admin", "pin": DA.hash_pin(PIN), "status": "active", "role_id": rid}).inserted_id


def client():
    return TestClient(server.app, raise_server_exceptions=False)


def login(c, **extra):
    return c.post("/admin/auth/login", json={"mobile": MOBILE, "pin": PIN, **extra})


def start(c, **extra):
    mk_user() if not DB.dashboard_users.find_one({"mobile": MOBILE}) else None
    r = login(c, **extra)
    assert r.status_code == 200, r.text
    return r.json()["challenge_id"], r.json()


def verify(c, cid, token=None, **extra):
    body = {"challenge_id": cid, **extra}
    if token is not None:
        body["access_token"] = token
    return c.post("/admin/auth/verify-otp", json=body)


def age_send(cid, seconds=40):
    DB.admin_login_challenges.update_one({}, {"$set": {"last_sent_at": datetime.utcnow() - timedelta(seconds=seconds)}})


# ── login step ─────────────────────────────────────────────────────────────────────────────────────

def test_login_returns_a_widget_challenge_with_no_otp_no_cookie_and_sends_no_sms():
    c = client()
    cid, body = start(c)
    assert body["provider"] == "widget" and body["otp_required"] is True
    assert "admin_token" not in c.cookies and "set-cookie" not in login(client()).headers
    doc = DB.admin_login_challenges.find_one({})
    assert doc["provider"] == "widget" and "otp_hash" not in doc and doc["test_mode"] is False
    dumped = json.dumps(body)
    assert AUTH_KEY not in dumped and WIDGET_TOKEN not in dumped and MOBILE not in dumped


def test_wrong_pin_creates_no_challenge_and_asks_nothing_of_the_widget():
    mk_user()
    r = client().post("/admin/auth/login", json={"mobile": MOBILE, "pin": "0000"})
    assert r.status_code == 401 and DB.admin_login_challenges.count_documents({}) == 0


def test_unknown_account_gets_the_generic_answer_and_no_challenge():
    r = client().post("/admin/auth/login", json={"mobile": "9111111111", "pin": PIN})
    assert r.status_code == 401 and DB.admin_login_challenges.count_documents({}) == 0


@pytest.mark.parametrize("missing", ["MSG91_WEB_WIDGET_ID", "MSG91_WEB_WIDGET_TOKEN_AUTH", "MSG91_AUTH_KEY"])
def test_missing_widget_configuration_refuses_sign_in_with_no_fallback(monkeypatch, missing):
    monkeypatch.delenv(missing)
    mk_user()
    r = login(client())
    assert r.status_code == 503 and "ref " in r.json()["detail"]
    assert DB.admin_login_challenges.count_documents({}) == 0 and "admin_token" not in r.cookies


# ── send / resend permits ──────────────────────────────────────────────────────────────────────────

def test_first_permit_returns_the_server_derived_identifier_and_public_config_only():
    c = client()
    cid, _ = start(c)
    r = c.post("/admin/auth/otp-request", json={"challenge_id": cid, "identifier": "919999999999", "mobile": "9999999999"})
    assert r.status_code == 200
    d = r.json()
    assert d["identifier"] == "91" + MOBILE and d["first"] is True                 # browser-supplied number ignored
    assert d["widget"] == {"widget_id": WIDGET_ID, "token_auth": WIDGET_TOKEN}
    assert AUTH_KEY not in r.text and r.headers["cache-control"] == "no-store"
    assert MOBILE_WIDGET_ID not in r.text and MOBILE_WIDGET_TOKEN not in r.text


def test_resend_keeps_the_30s_cooldown_and_the_three_resend_cap():
    c = client()
    cid, _ = start(c)
    assert c.post("/admin/auth/otp-request", json={"challenge_id": cid}).status_code == 200
    assert c.post("/admin/auth/otp-request", json={"challenge_id": cid}).status_code == 429          # too soon
    for i in range(3):
        age_send(cid)
        r = c.post("/admin/auth/otp-request", json={"challenge_id": cid})
        assert r.status_code == 200 and r.json()["first"] is False, i
    age_send(cid)
    assert c.post("/admin/auth/otp-request", json={"challenge_id": cid}).status_code == 401           # 4th resend refused


def test_permits_need_a_real_pending_widget_challenge():
    c = client()
    assert c.post("/admin/auth/otp-request", json={"challenge_id": "nope"}).status_code == 401
    assert c.post("/admin/auth/otp-request", json={}).status_code == 401
    cid, _ = start(c)
    DB.admin_login_challenges.update_one({}, {"$set": {"expires_at": datetime.utcnow() - timedelta(seconds=1)}})
    assert c.post("/admin/auth/otp-request", json={"challenge_id": cid}).status_code == 401


def test_the_legacy_resend_endpoint_cannot_be_used_on_a_widget_challenge():
    c = client()
    cid, _ = start(c)
    assert c.post("/admin/auth/resend-otp", json={"challenge_id": cid}).status_code == 401


def test_a_disabled_account_gets_no_permit():
    c = client()
    cid, _ = start(c)
    DB.dashboard_users.update_one({"mobile": MOBILE}, {"$set": {"status": "disabled"}})
    assert c.post("/admin/auth/otp-request", json={"challenge_id": cid}).status_code == 401


# ── verification ───────────────────────────────────────────────────────────────────────────────────

def test_a_verified_token_for_this_account_signs_in_exactly_once(env):
    c = client()
    cid, _ = start(c)
    r = verify(c, cid, fake_token())
    assert r.status_code == 200 and r.json()["user"]["mobile"] == MOBILE and "admin_token" in c.cookies
    again = verify(client(), cid, fake_token("b"))
    assert again.status_code == 401 and "admin_token" not in again.cookies


def test_the_browsers_own_verified_flags_and_typed_code_are_never_enough(env):
    c = client()
    cid, _ = start(c)
    for body in ({"challenge_id": cid, "verified": True}, {"challenge_id": cid, "msg91_verified": True, "otp": "1234"},
                 {"challenge_id": cid, "access_token": ""}, {"challenge_id": cid, "access_token": 12345}):
        r = c.post("/admin/auth/verify-otp", json=body)
        assert r.status_code == 401 and "admin_token" not in r.cookies
    assert env.calls == []                                                           # not even asked MSG91


def test_a_token_verified_for_another_number_is_refused(env):
    env.reply = {"ok": True, "identifier": "919888888888", "claims": {}}
    c = client()
    cid, _ = start(c)
    r = verify(c, cid, fake_token())
    assert r.status_code == 401 and "admin_token" not in r.cookies
    assert DB.admin_login_challenges.find_one({})["status"] == "pending"           # still not consumed


def test_msg91_rejecting_the_token_is_refused_and_counts_an_attempt(env):
    env.reply = {"ok": False, "error": "invalid"}
    c = client()
    cid, _ = start(c)
    assert verify(c, cid, fake_token()).status_code == 401
    assert DB.admin_login_challenges.find_one({})["attempts"] == 1


def test_five_failures_lock_the_challenge_even_for_a_later_genuine_token(env):
    env.reply = {"ok": False, "error": "invalid"}
    c = client()
    cid, _ = start(c)
    for i in range(5):
        assert verify(c, cid, fake_token(str(i))).status_code == 401
    env.reply = {"ok": True, "identifier": "91" + MOBILE, "claims": {}}
    r = verify(c, cid, fake_token("good"))
    assert r.status_code == 401 and "admin_token" not in r.cookies
    assert DB.admin_login_challenges.find_one({})["status"] == "locked"


def test_msg91_being_unavailable_is_a_503_and_costs_no_attempt(env):
    env.reply = {"ok": False, "error": "unavailable", "detail": "timeout"}
    c = client()
    cid, _ = start(c)
    r = verify(c, cid, fake_token())
    assert r.status_code == 503 and "ref " in r.json()["detail"] and "admin_token" not in r.cookies
    assert DB.admin_login_challenges.find_one({})["attempts"] == 0
    env.reply = {"ok": True, "identifier": "91" + MOBILE, "claims": {}}
    assert verify(c, cid, fake_token()).status_code == 200                        # the same challenge still works afterwards


def test_a_confirmed_token_that_does_not_name_the_number_fails_closed(env):
    env.reply = {"ok": True, "identifier": None, "claims": {}}
    c = client()
    cid, _ = start(c)
    r = verify(c, cid, fake_token())
    assert r.status_code == 503 and "admin_token" not in r.cookies
    assert DB.admin_login_challenges.find_one({})["status"] == "pending"


def test_one_token_can_never_authenticate_two_challenges(env):
    tok = fake_token("shared")
    c1, c2 = client(), client()
    cid1, _ = start(c1)
    assert verify(c1, cid1, tok).status_code == 200
    DB.dashboard_users.update_one({"mobile": MOBILE}, {"$set": {"token": None}})
    cid2, _ = start(c2)
    r = verify(c2, cid2, tok)
    assert r.status_code == 401 and "admin_token" not in r.cookies


def test_a_token_issued_before_the_challenge_began_cannot_authenticate_it(env):
    env.reply = {"ok": True, "identifier": "91" + MOBILE, "claims": {"iat": int(time.time()) - 3600}}
    c = client()
    cid, _ = start(c)
    assert verify(c, cid, fake_token()).status_code == 401


def test_an_expired_token_is_refused(env):
    env.reply = {"ok": True, "identifier": "91" + MOBILE, "claims": {"iat": int(time.time()), "exp": int(time.time()) - 5}}
    c = client()
    cid, _ = start(c)
    assert verify(c, cid, fake_token()).status_code == 401


def test_a_fresh_token_with_claims_is_accepted(env):
    env.reply = {"ok": True, "identifier": "91" + MOBILE, "claims": {"iat": int(time.time()), "exp": int(time.time()) + 600}}
    c = client()
    cid, _ = start(c)
    assert verify(c, cid, fake_token()).status_code == 200


def test_an_expired_or_foreign_challenge_cannot_be_completed(env):
    c = client()
    cid, _ = start(c)
    DB.admin_login_challenges.update_one({}, {"$set": {"expires_at": datetime.utcnow() - timedelta(seconds=1)}})
    assert verify(c, cid, fake_token()).status_code == 401
    assert verify(c, "garbage", fake_token()).status_code == 401


def test_the_account_is_rechecked_at_verification(env):
    c = client()
    cid, _ = start(c)
    DB.dashboard_users.update_one({"mobile": MOBILE}, {"$set": {"status": "suspended"}})
    r = verify(c, cid, fake_token())
    assert r.status_code == 401 and "admin_token" not in r.cookies


def test_a_failed_code_report_only_ever_spends_attempts_and_locks_at_five():
    c = client()
    cid, _ = start(c)
    left = []
    for _ in range(5):
        r = c.post("/admin/auth/otp-failed", json={"challenge_id": cid})
        assert r.status_code == 200 and "admin_token" not in r.cookies
        left.append(r.json()["remaining"])
    assert left == [4, 3, 2, 1, 0]
    assert c.post("/admin/auth/otp-failed", json={"challenge_id": cid}).status_code == 401
    assert DB.admin_login_challenges.find_one({})["status"] == "locked"


def test_the_local_code_path_cannot_complete_a_widget_challenge():
    c = client()
    cid, _ = start(c)
    for guess in ("1234", "0000", "1111"):
        r = c.post("/admin/auth/verify-otp", json={"challenge_id": cid, "otp": guess})
        assert r.status_code == 401 and "admin_token" not in r.cookies


# ── remember this device ───────────────────────────────────────────────────────────────────────────

def test_remember_device_still_works_after_a_widget_sign_in(monkeypatch):
    monkeypatch.setenv("ADMIN_TRUSTED_DEVICE_SUPER_ADMIN", "1")
    DB.dashboard_users.delete_many({})
    mk_user(role="Super Admin")
    c = client()
    cid, _ = start(c, remember_device=True)
    r = verify(c, cid, fake_token())
    assert r.status_code == 200 and r.json()["device_trusted"] is True and "offro_td" in c.cookies
    again = login(c, remember_device=True)                                         # trusted device: PIN is enough
    assert again.status_code == 200 and again.json()["otp_required"] is False


def test_super_admin_gets_no_trusted_device_by_default():
    mk_user(role="Super Admin")
    c = client()
    cid, _ = start(c, remember_device=True)
    r = verify(c, cid, fake_token())
    assert r.status_code == 200 and r.json()["device_trusted"] is False and "offro_td" not in c.cookies


# ── Staging and Production separation ─────────────────────────────────────────────────────────────

def test_production_always_uses_the_widget_even_with_every_staging_switch_copied(monkeypatch):
    for k, v in {**PROD, "ADMIN_OTP_STAGING_TEST_MODE": "1", "ADMIN_OTP_TEST_MOBILES": MOBILE}.items():
        monkeypatch.setenv(k, v)
    c = client()
    cid, body = start(c)
    assert body["provider"] == "widget" and DB.admin_login_challenges.find_one({})["test_mode"] is False
    assert c.post("/admin/auth/verify-otp", json={"challenge_id": cid, "otp": "1234"}).status_code == 401
    r = c.post("/admin/auth/otp-request", json={"challenge_id": cid})
    assert r.status_code == 200 and r.json()["identifier"] == "91" + MOBILE


def test_production_without_widget_configuration_has_no_fallback_even_with_staging_switches(monkeypatch):
    for k, v in {**PROD, "ADMIN_OTP_STAGING_TEST_MODE": "1", "ADMIN_OTP_TEST_MOBILES": MOBILE}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MSG91_WEB_WIDGET_ID")
    mk_user()
    r = login(client())
    assert r.status_code == 503 and DB.admin_login_challenges.count_documents({}) == 0


def test_staging_allow_listed_accounts_keep_the_fixed_code_and_never_touch_the_widget(monkeypatch, env):
    for k, v in {**STAGING, "ADMIN_OTP_STAGING_TEST_MODE": "1", "ADMIN_OTP_TEST_MOBILES": MOBILE}.items():
        monkeypatch.setenv(k, v)
    c = client()
    cid, body = start(c)
    assert body["provider"] == "local" and DB.admin_login_challenges.find_one({})["test_mode"] is True
    bad = c.post("/admin/auth/verify-otp", json={"challenge_id": cid, "otp": "9999"})
    assert bad.status_code == 401
    ok = c.post("/admin/auth/verify-otp", json={"challenge_id": cid, "otp": TM.TEST_OTP})
    assert ok.status_code == 200 and "admin_token" in c.cookies and env.calls == []


def test_staging_local_challenges_ignore_widget_tokens(monkeypatch, env):
    for k, v in {**STAGING, "ADMIN_OTP_STAGING_TEST_MODE": "1", "ADMIN_OTP_TEST_MOBILES": MOBILE}.items():
        monkeypatch.setenv(k, v)
    c = client()
    cid, _ = start(c)
    r = verify(c, cid, fake_token())
    assert r.status_code == 401 and env.calls == []
    assert c.post("/admin/auth/otp-request", json={"challenge_id": cid}).status_code == 401


def test_staging_accounts_not_on_the_allow_list_use_the_real_widget(monkeypatch):
    for k, v in {**STAGING, "ADMIN_OTP_STAGING_TEST_MODE": "1", "ADMIN_OTP_TEST_MOBILES": "9555555555"}.items():
        monkeypatch.setenv(k, v)
    c = client()
    cid, body = start(c)
    assert body["provider"] == "widget"
    assert c.post("/admin/auth/verify-otp", json={"challenge_id": cid, "otp": TM.TEST_OTP}).status_code == 401
    assert verify(c, cid, fake_token()).status_code == 200


def test_the_old_sms_engine_is_unreachable_by_any_runtime_setting(monkeypatch):
    monkeypatch.setattr(OTP, "REAL_PROVIDER", "widget")
    for name in ("REAL_PROVIDER", "ADMIN_OTP_PROVIDER", "OTP_PROVIDER"):
        monkeypatch.setenv(name, "sms")
    assert _REAL_SEND_SMS("9000000200", "1234", "t") == {"ok": False, "error": "sms_provider_disabled"}


# ── page, CSP, logging ─────────────────────────────────────────────────────────────────────────────

def test_only_the_login_page_may_load_the_msg91_widget():
    r = client().get("/admin")
    csp = r.headers["content-security-policy"]
    assert "https://*.msg91.com" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    other = client().get("/admin/dashboard", follow_redirects=False)
    assert "msg91.com" not in other.headers.get("content-security-policy", "")


def test_the_page_loads_the_official_sdk_with_exposed_methods_and_the_documented_calls():
    page = open("templates/admin_login.html", encoding="utf-8").read()
    for needle in ("https://verify.msg91.com/otp-provider.js", "exposeMethods: true", "window.sendOtp", "window.retryOtp", "window.verifyOtp",
                   "_wm.retry.call(window, null,", "_wm.verify.call(window, String(otp),", "_wm.send.call(window, data.identifier,"):
        assert needle in page, needle


def test_the_page_never_defines_a_function_named_like_an_sdk_method():
    """Regression: a page function called verifyOtp() shadowed/was shadowed by the SDK's window.verifyOtp, so the Verify button called the SDK with
    no OTP ('OTP not provided in verifyOtp() method'). None of the SDK's exposed names may be declared, assigned or used as a handler by the page."""
    import re
    page = open("templates/admin_login.html", encoding="utf-8").read()
    for name in ("sendOtp", "retryOtp", "verifyOtp", "getWidgetData", "isCaptchaVerified"):
        assert not re.search(r"function\s+" + name + r"\b", page), name
        assert not re.search(r"\b(?:var|let|const)\s+" + name + r"\b", page), name
        assert not re.search(r"window\." + name + r"\s*=(?!=)", page), name
        assert not re.search(r"on\w+=\"[^\"]*\b" + name + r"\s*\(", page), name          # no inline handler calls an SDK name
    # every Verify entry point (button, retry button, 4th digit, Enter key) goes through the page's own submitOtp
    assert page.count('onclick="submitOtp()"') == 2 and "if (allFilled) submitOtp();" in page and "submitOtp(); }" in page


def test_the_login_page_csp_allows_the_widget_hosts_and_still_blocks_eval_and_framing():
    csp = client().get("/admin").headers["content-security-policy"]
    parts = {p.split(" ", 1)[0]: p for p in csp.split("; ")}
    assert "https://*.msg91.com" in parts["script-src"] and "https://*.msg91.com" in parts["connect-src"] and "https://*.msg91.com" in parts["style-src"]
    assert "'unsafe-eval'" not in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "default-src 'self'" in csp


def test_nothing_secret_is_ever_printed(capsys, env):
    c = client()
    cid, _ = start(c)
    tok = fake_token("secretlog")
    env.reply = {"ok": True, "identifier": "919888888888", "claims": {}}
    verify(c, cid, tok)
    env.reply = {"ok": False, "error": "unavailable", "detail": "timeout"}
    verify(c, cid, tok)
    out = capsys.readouterr().out + capsys.readouterr().err
    assert tok not in out and AUTH_KEY not in out and WIDGET_TOKEN not in out and "1234" not in out


# ── the MSG91 client itself ────────────────────────────────────────────────────────────────────────

class Resp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        if isinstance(self._b, Exception):
            raise self._b
        return self._b


_DEFAULT = object()


def call_real(monkeypatch, resp, token=_DEFAULT):
    monkeypatch.undo()                                                   # drop the autouse fake verifier (and env) for these unit tests
    monkeypatch.setenv("MSG91_AUTH_KEY", AUTH_KEY)
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json)
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(W.httpx, "post", post)
    out = _REAL_VERIFY(fake_token() if token is _DEFAULT else token)
    return out, seen


def test_the_client_posts_the_documented_request(monkeypatch):
    out, seen = call_real(monkeypatch, Resp(200, {"type": "success", "message": {"mobile": "919000000200"}}), token=fake_token("req"))
    assert seen["url"] == "https://control.msg91.com/api/v5/widget/verifyAccessToken"
    assert seen["json"] == {"authkey": AUTH_KEY, "access-token": fake_token("req")}
    assert out["ok"] is True and out["identifier"] == "919000000200"


def test_identifier_is_read_from_the_token_claims_if_the_reply_has_none(monkeypatch):
    payload = base64.urlsafe_b64encode(json.dumps({"identifier": "919000000200", "iat": 1}).encode()).decode().rstrip("=")
    out, _ = call_real(monkeypatch, Resp(200, {"type": "success", "message": "Access Token verified"}), token="aaa." + payload + ".sig")
    assert out["ok"] is True and out["identifier"] == "919000000200"


def test_a_success_reply_without_any_number_yields_no_identifier(monkeypatch):
    out, _ = call_real(monkeypatch, Resp(200, {"type": "success", "message": "ok"}))
    assert out["ok"] is True and out["identifier"] is None


@pytest.mark.parametrize("status,body", [(200, {"type": "error", "message": "invalid token"}), (200, {"type": "failure"}), (200, {}),
                                         (200, [1, 2]), (400, {"type": "error"})])
def test_http_200_error_payloads_and_odd_replies_are_failures(monkeypatch, status, body):
    out, _ = call_real(monkeypatch, Resp(status, body))
    assert out["ok"] is False and out["error"] == "invalid"


@pytest.mark.parametrize("resp", [Resp(500, {"type": "error"}), Resp(200, ValueError("html")), Resp(401, {"type": "error"}),
                                  Resp(200, {"type": "error", "code": "201"}), W.httpx.ReadTimeout("slow"), RuntimeError("net")])
def test_provider_problems_are_unavailable_not_invalid(monkeypatch, resp):
    out, _ = call_real(monkeypatch, resp)
    assert out["ok"] is False and out["error"] == "unavailable"


def test_junk_tokens_never_reach_msg91(monkeypatch):
    for junk in ("", "short", "x" * 5000, "has space in it xxxxxxxxxxxxxxxxxx", None, 5):
        out, seen = call_real(monkeypatch, Resp(200, {"type": "success"}), token=junk)
        assert out["ok"] is False and not seen, junk


# ── web widget vs mobile widget ────────────────────────────────────────────────────────────────────

def test_admin_never_falls_back_to_the_mobile_widget_when_the_web_one_is_missing(monkeypatch):
    monkeypatch.delenv("MSG91_WEB_WIDGET_ID")
    monkeypatch.delenv("MSG91_WEB_WIDGET_TOKEN_AUTH")                          # mobile variables are still set
    assert W.configured() is False and W.public_config() == {"widget_id": "", "token_auth": ""}
    mk_user()
    assert login(client()).status_code == 503


def test_a_web_id_copied_from_the_mobile_widget_is_refused(monkeypatch):
    monkeypatch.setenv("MSG91_WEB_WIDGET_ID", MOBILE_WIDGET_ID)
    assert W.configured() is False and any("same as the mobile" in p for p in W.config_problems())
    mk_user()
    assert login(client()).status_code == 503


def test_config_problem_names_never_contain_values(monkeypatch):
    monkeypatch.delenv("MSG91_AUTH_KEY")
    assert W.config_problems() == ["MSG91_AUTH_KEY"]


def test_the_log_says_which_widget_is_selected_without_any_credential(capsys):
    c = client()
    cid, _ = start(c)
    c.post("/admin/auth/otp-request", json={"challenge_id": cid})
    out = capsys.readouterr().out
    assert "widget=web(OffroWeb)" in out and "id_fp=" in out
    for secret in (AUTH_KEY, WIDGET_TOKEN, WIDGET_ID, MOBILE_WIDGET_ID, MOBILE_WIDGET_TOKEN):
        assert secret not in out


def test_a_rejected_token_logs_msg91s_reason_but_not_the_token(monkeypatch, capsys):
    tok = fake_token("rej")
    out, _ = call_real(monkeypatch, Resp(200, {"type": "error", "message": "Invalid access token"}), token=tok)
    logged = capsys.readouterr().out
    assert out == {"ok": False, "error": "invalid"} and "Invalid access token" in logged and tok not in logged and AUTH_KEY not in logged


# ── verified-number binding (production failure: token claims were only companyId + requestId) ────────────────────────────────────────

def jwt_with(claims: dict) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9." + seg + ".sig" + str(abs(hash(json.dumps(claims, sort_keys=True))) % 10**6)


PROD_CLAIMS = {"companyId": "123456789012", "requestId": "336870744532313134323444"}      # the exact claim names seen in the Railway logs (values invented)


@pytest.fixture
def msg91(monkeypatch):
    """The REAL verify_access_token talking to a fake MSG91 (httpx.post replaced), so the whole path router -> engine -> parser runs."""
    state = {"reply": Resp(200, {"type": "success", "message": "91" + MOBILE}), "posts": []}
    monkeypatch.setattr(W, "verify_access_token", _REAL_VERIFY)

    def post(url, json=None, headers=None, timeout=None):
        state["posts"].append((url, dict(json)))
        return state["reply"]
    monkeypatch.setattr(W.httpx, "post", post)
    return state


def test_the_exact_production_failure_message_is_the_documented_identifier_and_login_now_succeeds(msg91):
    """Railway: HTTP 200 from verifyAccessToken, token claims companyId + requestId only, number not found -> 503. MSG91's reference puts the verified
    identifier in the reply's `message`; it was never read. It is now, and it must equal the account's number."""
    c = client()
    cid, _ = start(c)
    tok = jwt_with(PROD_CLAIMS)
    r = verify(c, cid, tok)
    assert r.status_code == 200 and "admin_token" in c.cookies
    url, sent = msg91["posts"][0]
    assert url.endswith("/api/v5/widget/verifyAccessToken") and sent == {"authkey": AUTH_KEY, "access-token": tok}


@pytest.mark.parametrize("message", ["91" + MOBILE, "+91" + MOBILE, MOBILE, " 91" + MOBILE + " "])
def test_the_documented_phone_spellings_all_match_the_account_number(msg91, message):
    msg91["reply"] = Resp(200, {"type": "success", "message": message})
    c = client()
    cid, _ = start(c)
    assert verify(c, cid, jwt_with(PROD_CLAIMS)).status_code == 200


def test_the_attacker_scenario_a_real_token_for_the_attackers_own_phone_is_refused(msg91):
    """PIN known to the attacker; the widget is genuinely completed, but for the attacker's phone. MSG91 says so; the account's number differs."""
    msg91["reply"] = Resp(200, {"type": "success", "message": "919876500000"})
    c = client()
    cid, _ = start(c)
    r = verify(c, cid, jwt_with(PROD_CLAIMS))
    assert r.status_code == 401 and "admin_token" not in c.cookies
    doc = DB.admin_login_challenges.find_one({})
    assert doc["status"] == "pending" and doc["attempts"] == 1                       # an attempt is spent, nothing consumed


def test_company_id_and_request_id_never_count_as_proof_of_identity(msg91):
    """Even when they LOOK like phone numbers (digits, 12 / 15 chars) and even when they equal the account's number."""
    msg91["reply"] = Resp(200, {"type": "success", "message": "Access Token verified"})
    c = client()
    cid, _ = start(c)
    for claims in (PROD_CLAIMS, {"companyId": "91" + MOBILE, "requestId": "91" + MOBILE}, {"requestId": "9" * 15, "companyId": 919000000200}):
        r = verify(c, cid, jwt_with(claims))
        assert r.status_code == 503 and "admin_token" not in c.cookies               # fail closed: nothing names a verified number
    assert DB.admin_login_challenges.find_one({})["attempts"] == 0 and DB.admin_login_challenges.find_one({})["status"] == "pending"


@pytest.mark.parametrize("message", ["Access Token verified", "", None, "1234567", "9" * 16, "email@example.com", "my number is 9000000200", 9000000200])
def test_a_message_that_is_not_exactly_a_phone_number_is_never_used(msg91, message):
    msg91["reply"] = Resp(200, {"type": "success", "message": message})
    out = _REAL_VERIFY(jwt_with(PROD_CLAIMS))
    assert out["ok"] is True and out["identifier"] is None and out["identifiers"] == []


def test_the_browser_cannot_supply_the_number_request_id_or_widget_id(msg91):
    msg91["reply"] = Resp(200, {"type": "success", "message": "919876500000"})            # MSG91 verified someone else's phone
    c = client()
    cid, _ = start(c)
    r = c.post("/admin/auth/verify-otp", json={"challenge_id": cid, "access_token": jwt_with(PROD_CLAIMS), "mobile": MOBILE, "identifier": "91" + MOBILE,
                                              "requestId": PROD_CLAIMS["requestId"], "reqId": PROD_CLAIMS["requestId"], "widget_id": WIDGET_ID})
    assert r.status_code == 401 and "admin_token" not in c.cookies


def test_the_reply_and_the_token_must_agree_when_both_name_a_number(msg91):
    msg91["reply"] = Resp(200, {"type": "success", "message": "91" + MOBILE})
    c = client()
    cid, _ = start(c)
    r = verify(c, cid, jwt_with({**PROD_CLAIMS, "mobile": "919876500000"}))                # token claims a different number than MSG91's reply
    assert r.status_code == 401 and "admin_token" not in c.cookies
    ok = verify(c, cid, jwt_with({**PROD_CLAIMS, "mobile": "91" + MOBILE}))
    assert ok.status_code == 200


def test_a_number_in_the_token_claims_alone_is_still_checked_against_the_account(msg91):
    msg91["reply"] = Resp(200, {"type": "success", "message": "Access Token verified"})
    c = client()
    cid, _ = start(c)
    assert verify(c, cid, jwt_with({**PROD_CLAIMS, "identifier": "919876500000"})).status_code == 401
    assert verify(c, cid, jwt_with({**PROD_CLAIMS, "identifier": "91" + MOBILE, "n": 1})).status_code == 200


def test_a_verified_number_never_creates_a_session_unless_msg91_said_success(msg91):
    for body in ({"type": "error", "message": "91" + MOBILE}, {"type": "error", "code": "201", "message": "AuthenticationFailure"}, {"message": "91" + MOBILE}):
        msg91["reply"] = Resp(200, body)
        c = client()
        DB.admin_login_challenges.delete_many({})
        cid, _ = start(c)
        r = verify(c, cid, jwt_with(PROD_CLAIMS))
        assert r.status_code in (401, 503) and "admin_token" not in c.cookies, body


def test_a_successful_verification_is_single_use_and_the_token_cannot_be_replayed(msg91):
    tok = jwt_with(PROD_CLAIMS)
    c1, c2 = client(), client()
    cid1, _ = start(c1)
    assert verify(c1, cid1, tok).status_code == 200
    cid2, _ = start(c2)
    assert verify(c2, cid2, tok).status_code == 401 and "admin_token" not in c2.cookies         # same token, new challenge
    assert verify(client(), cid1, jwt_with({**PROD_CLAIMS, "n": 2})).status_code == 401          # same challenge, new token


def test_the_failure_log_shows_shape_and_claim_names_but_no_values(msg91, capsys):
    msg91["reply"] = Resp(200, {"type": "success", "message": "Access Token verified"})
    tok = jwt_with({"companyId": "COMPANY-SECRET-VALUE", "requestId": "REQUEST-SECRET-VALUE"})
    c = client()
    cid, _ = start(c)
    assert verify(c, cid, tok).status_code == 503
    out = capsys.readouterr().out
    assert "claim names=['companyId', 'requestId']" in out and "message=text len=21" in out and "verified_number_found=False" in out
    assert "fail closed" in out
    for secret in ("COMPANY-SECRET-VALUE", "REQUEST-SECRET-VALUE", "Access Token verified", tok, AUTH_KEY):
        assert secret not in out


def test_a_successful_login_logs_no_number_token_or_key(msg91, capsys):
    tok = jwt_with(PROD_CLAIMS)
    c = client()
    cid, _ = start(c)
    assert verify(c, cid, tok).status_code == 200
    out = capsys.readouterr().out
    for secret in (tok, AUTH_KEY, MOBILE, PROD_CLAIMS["requestId"], PROD_CLAIMS["companyId"]):
        assert secret not in out
