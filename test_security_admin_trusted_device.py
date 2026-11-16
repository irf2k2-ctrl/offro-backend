"""Patch C: 'Remember this device' for the admin sign-in. MSG91 is mocked; no SMS, no network.
Run:  python -m pytest test_security_admin_trusted_device.py -q -W ignore"""
import hashlib
import os
import re
import sys
from datetime import datetime, timedelta

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import server
from routers import dashboard_auth as DA
from services import admin_login_otp as OTP
from services import admin_trusted_devices as TD

DB = sys.modules["database"].db
ROOT = os.path.dirname(os.path.abspath(server.__file__))
MOBILE, OTHER, PIN, ADMIN_MOBILE = "9000000100", "9000000200", "1234", "9000000999"
COOKIE = TD.COOKIE
SHARED = ("dashboard_users", "dashboard_roles", "activity_logs", "trusted_devices")
OWN = ("admin_login_challenges", "admin_auth_throttle", TD.COLLECTION)
MODS = ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_PUBLIC_DOMAIN", "ADMIN_OTP_STAGING_TEST_MODE", "ADMIN_OTP_TEST_MOBILES",
        "ADMIN_TRUSTED_DEVICE_DAYS", "ADMIN_TRUSTED_DEVICE_SUPER_ADMIN")


class SMS(list):
    def __call__(self, mobile, otp, tpl):
        self.append(otp)
        return {"ok": True}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    saved = {c: list(DB[c].find({})) for c in SHARED}
    for c in SHARED + OWN:
        DB[c].delete_many({})
    for k in MODS:
        monkeypatch.delenv(k, raising=False)
    sms = SMS()
    monkeypatch.setattr(OTP, "_send_sms", sms)
    for mod in (DA,):
        monkeypatch.setattr(mod, "db", DB)
    yield sms
    for c in SHARED + OWN:
        DB[c].delete_many({})
    for c, docs in saved.items():
        if docs:
            DB[c].insert_many(docs)


@pytest.fixture
def sms(env):
    return env


def role(name):
    r = DB.dashboard_roles.find_one({"role_name": name})
    return r["_id"] if r else DB.dashboard_roles.insert_one({"role_name": name, "permissions": {}}).inserted_id


def mk_user(mobile=MOBILE, role_name="Sales Executive", **kw):
    d = {"mobile": mobile, "full_name": "User " + mobile[-2:], "pin": DA.hash_pin(PIN), "status": "active", "role_id": role(role_name)}
    d.update(kw)
    return DB.dashboard_users.insert_one(d).inserted_id


def client():
    return TestClient(server.app, raise_server_exceptions=False)


def login(c, mobile=MOBILE, pin=PIN, remember=None, **h):
    body = {"mobile": mobile, "pin": pin}
    if remember is not None:
        body["remember_device"] = remember
    return c.post("/admin/auth/login", json=body, headers=h)


def full_login(c, sms, mobile=MOBILE, remember=True):
    r = login(c, mobile, remember=remember)
    assert r.status_code == 200 and r.json()["otp_required"] is True, r.text
    v = c.post("/admin/auth/verify-otp", json={"challenge_id": r.json()["challenge_id"], "otp": sms[-1]})
    assert v.status_code == 200, v.text
    return v


def devices(**q):
    return list(DB[TD.COLLECTION].find(q))


def set_cookie_header(resp, name):
    return next((v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie" and v.startswith(name + "=")), "")


# ── registration ───────────────────────────────────────────────────────────────────────────────────

def test_without_the_checkbox_every_login_needs_pin_and_otp_and_nothing_is_remembered(sms):
    mk_user()
    c = client()
    for _ in range(3):
        v = full_login(c, sms, remember=False)
        assert v.json()["device_trusted"] is False and not set_cookie_header(v, COOKIE)
    assert devices() == [] and len(sms) == 3
    assert login(c).json()["otp_required"] is True                                           # remember omitted: same as unticked


def test_the_first_login_with_the_checkbox_still_needs_the_otp_and_registers_only_after_it(sms):
    mk_user()
    c = client()
    r = login(c, remember=True)
    assert r.json()["otp_required"] is True and "admin_token" not in r.cookies and not set_cookie_header(r, COOKIE)
    assert devices() == []                                                                    # nothing is trusted before the OTP is verified
    v = c.post("/admin/auth/verify-otp", json={"challenge_id": r.json()["challenge_id"], "otp": sms[-1]})
    assert v.status_code == 200 and v.json()["device_trusted"] is True and len(devices()) == 1


def test_only_a_hash_is_stored_bound_to_the_account_with_a_30_day_expiry_and_secure_cookie_flags(sms):
    uid = mk_user()
    v = full_login(client(), sms)
    ck = set_cookie_header(v, COOKIE)
    token = re.match(COOKIE + r"=([^;]+)", ck).group(1)
    assert len(token) >= 40 and "HttpOnly" in ck and "SameSite=strict" in ck.replace("Strict", "strict") and "Path=/admin/auth" in ck
    assert "Max-Age=%d" % (30 * 86400) in ck
    d = devices()[0]
    assert d["user_id"] == uid and d["token_hash"] == hashlib.sha256(("offro-td|" + token).encode()).hexdigest()
    assert token not in str(d) and token not in str(list(DB.activity_logs.find({}))) and "token" not in d
    assert timedelta(days=29, hours=23) < d["expires_at"] - d["created_at"] < timedelta(days=30, minutes=1)
    c = client()
    r = login(c, remember=True, **{"x-forwarded-proto": "https"})
    v = c.post("/admin/auth/verify-otp", json={"challenge_id": r.json()["challenge_id"], "otp": sms[-1]}, headers={"x-forwarded-proto": "https"})
    assert "Secure" in set_cookie_header(v, COOKIE)                                           # added by the security middleware over HTTPS


def test_a_non_boolean_or_missing_remember_request_is_not_a_request(sms):
    mk_user()
    for val in ("true", 1, "yes", None, [True]):
        c = client()
        r = c.post("/admin/auth/login", json={"mobile": MOBILE, "pin": PIN, "remember_device": val})
        v = c.post("/admin/auth/verify-otp", json={"challenge_id": r.json()["challenge_id"], "otp": sms[-1]})
        assert v.status_code == 200 and v.json()["device_trusted"] is False
    assert devices() == []


def test_a_browser_supplied_trust_flag_at_verify_time_is_ignored(sms):
    mk_user()
    c = client()
    r = login(c, remember=False)
    v = c.post("/admin/auth/verify-otp", json={"challenge_id": r.json()["challenge_id"], "otp": sms[-1], "remember_device": True,
                                               "trust_device": True, "device_id": "abc", "trusted": True})
    assert v.status_code == 200 and v.json()["device_trusted"] is False and devices() == []


# ── using a trusted device ─────────────────────────────────────────────────────────────────────────

def test_a_trusted_device_skips_the_otp_but_still_requires_the_correct_pin(sms):
    mk_user()
    c = client()
    full_login(c, sms)
    sent = len(sms)
    bad = login(c, pin="0000", remember=True)
    assert bad.status_code == 401 and "admin_token" not in bad.cookies
    ok = login(c, remember=True)
    assert ok.status_code == 200 and ok.json()["otp_required"] is False and ok.json()["device_trusted"] is True
    assert "admin_token" in ok.cookies and len(sms) == sent and "challenge_id" not in ok.text
    assert c.get("/admin/auth/me").status_code == 200
    assert DB.activity_logs.count_documents({"action": "TRUSTED_DEVICE_LOGIN"}) == 1
    assert devices()[0]["last_used_at"] is not None


def test_other_browsers_and_other_accounts_still_need_the_otp(sms):
    mk_user()
    mk_user(OTHER)
    c = client()
    full_login(c, sms)
    other_browser = login(client(), remember=True)
    assert other_browser.json()["otp_required"] is True
    token = c.cookies.get(COOKIE)
    stolen = client()
    stolen.cookies.set(COOKIE, token, path="/admin/auth")
    assert login(stolen, OTHER, remember=True).json()["otp_required"] is True                  # a device is bound to its own account
    assert len(devices()) == 1


@pytest.mark.parametrize("how", ["expired", "revoked", "garbage", "tampered"])
def test_an_expired_revoked_or_invalid_token_needs_the_otp_again(sms, how):
    mk_user()
    c = client()
    full_login(c, sms)
    if how == "expired":
        DB[TD.COLLECTION].update_many({}, {"$set": {"expires_at": datetime.utcnow() - timedelta(seconds=1)}})
    elif how == "revoked":
        DB[TD.COLLECTION].delete_many({})
    elif how == "garbage":
        c.cookies.set(COOKIE, "x" * 43, path="/admin/auth")
    else:
        c.cookies.set(COOKIE, c.cookies.get(COOKIE)[:-2] + "ZZ", path="/admin/auth")
    r = login(c, remember=True)
    assert r.status_code == 200 and r.json()["otp_required"] is True and "admin_token" not in r.cookies


def test_unticking_the_box_on_a_remembered_browser_forgets_it_and_asks_for_the_code(sms):
    mk_user()
    c = client()
    full_login(c, sms)
    r = login(c, remember=False)
    assert r.json()["otp_required"] is True and devices() == []
    assert DB.activity_logs.count_documents({"action": "TRUSTED_DEVICE_FORGOTTEN"}) == 1
    assert login(c, remember=True).json()["otp_required"] is True                              # and it stays forgotten


def test_a_disabled_account_cannot_use_its_trusted_device(sms):
    uid = mk_user()
    c = client()
    full_login(c, sms)
    DB.dashboard_users.update_one({"_id": uid}, {"$set": {"status": "disabled"}})
    r = login(c, remember=True)
    assert r.status_code == 401 and "admin_token" not in r.cookies


def test_the_pin_cooldown_still_applies_to_trusted_devices(sms):
    mk_user()
    c = client()
    full_login(c, sms)
    for _ in range(5):
        login(c, pin="0000", remember=True)
    r = login(c, remember=True)                                                                # correct PIN, but cooling down
    assert r.status_code == 429 and "admin_token" not in r.cookies


def test_device_status_is_a_yes_or_no_about_this_browser_only(sms):
    mk_user()
    c = client()
    assert c.get("/admin/auth/device-status").json() == {"remembered": False}
    full_login(c, sms)
    r = c.get("/admin/auth/device-status")
    assert r.json() == {"remembered": True} and r.headers["cache-control"] == "no-store"
    assert client().get("/admin/auth/device-status").json() == {"remembered": False}
    DB.dashboard_users.update_many({}, {"$set": {"status": "suspended"}})
    assert c.get("/admin/auth/device-status").json() == {"remembered": False}


# ── Super Admin protection, lifetime and kill switch ───────────────────────────────────────────────

def test_super_admin_always_needs_the_otp_by_default(sms):
    mk_user(ADMIN_MOBILE, role_name="Super Admin")
    c = client()
    v = full_login(c, sms, ADMIN_MOBILE)
    assert v.json()["device_trusted"] is False and devices() == [] and not set_cookie_header(v, COOKIE)
    assert login(c, ADMIN_MOBILE, remember=True).json()["otp_required"] is True


def test_super_admin_can_be_allowed_explicitly_and_still_needs_the_pin(sms, monkeypatch):
    monkeypatch.setenv("ADMIN_TRUSTED_DEVICE_SUPER_ADMIN", "1")
    mk_user(ADMIN_MOBILE, role_name="Super Admin")
    c = client()
    assert full_login(c, sms, ADMIN_MOBILE).json()["device_trusted"] is True
    assert login(c, ADMIN_MOBILE, pin="0000", remember=True).status_code == 401
    assert login(c, ADMIN_MOBILE, remember=True).json()["otp_required"] is False
    monkeypatch.delenv("ADMIN_TRUSTED_DEVICE_SUPER_ADMIN")
    assert login(c, ADMIN_MOBILE, remember=True).json()["otp_required"] is True               # switching it off takes effect immediately


def test_lifetime_is_configurable_and_zero_turns_the_feature_off(sms, monkeypatch):
    mk_user()
    monkeypatch.setenv("ADMIN_TRUSTED_DEVICE_DAYS", "7")
    v = full_login(client(), sms)
    assert "Max-Age=%d" % (7 * 86400) in set_cookie_header(v, COOKIE)
    assert devices()[0]["expires_at"] - devices()[0]["created_at"] < timedelta(days=7, minutes=1)
    monkeypatch.setenv("ADMIN_TRUSTED_DEVICE_DAYS", "0")
    c = client()
    v = full_login(c, sms)
    assert v.json()["device_trusted"] is False and login(c, remember=True).json()["otp_required"] is True
    monkeypatch.setenv("ADMIN_TRUSTED_DEVICE_DAYS", "garbage")
    assert TD.lifetime_days() == 30
    monkeypatch.setenv("ADMIN_TRUSTED_DEVICE_DAYS", "9999")
    assert TD.lifetime_days() == 90


def test_a_user_keeps_at_most_ten_devices(sms):
    uid = mk_user()
    for _ in range(12):
        TD.register(uid, "1.1.1.1", "UA")
    assert len(devices(user_id=uid)) == TD.MAX_DEVICES_PER_USER


def test_the_old_plain_token_collection_and_cookie_are_never_honoured(sms):
    uid = mk_user()
    DB.trusted_devices.insert_one({"token": "tdtoken" * 6, "user_id": uid, "expires_at": datetime.utcnow() + timedelta(days=5)})
    c = client()
    c.cookies.set("offro_trusted", "tdtoken" * 6)
    c.cookies.set(COOKIE, "tdtoken" * 6, path="/admin/auth")
    assert login(c, remember=True).json()["otp_required"] is True


# ── revocation ─────────────────────────────────────────────────────────────────────────────────────

def admin_session():
    DB.dashboard_users.insert_one({"mobile": ADMIN_MOBILE, "full_name": "Boss", "pin": DA.hash_pin(PIN), "status": "active", "token": "boss-token",
                                   "role_id": role("Super Admin"), "last_active_at": datetime.utcnow()})
    c = client()
    c.cookies.set("admin_token", "boss-token")
    return c


def trusted(sms, uid_mobile=MOBILE):
    c = client()
    full_login(c, sms, uid_mobile)
    assert len(devices()) >= 1
    return c


def test_each_security_sensitive_change_revokes_every_trusted_device_of_that_account(sms):
    uid = mk_user()
    other = mk_user(OTHER)
    boss = admin_session()
    TD.register(other, "1.1.1.1", "other-device")                                               # another account's device must survive all of this
    cases = [
        ("PUT", "/admin/auth/users/%s" % uid, {"pin": "5678"}),
        ("PUT", "/admin/auth/users/%s/reset-pin" % uid, {"new_pin": "4321"}),
        ("PUT", "/admin/auth/users/%s" % uid, {"status": "disabled"}),
        ("PUT", "/admin/auth/users/%s" % uid, {"status": "suspended"}),
        ("PUT", "/admin/auth/users/%s" % uid, {"mobile": "9111111111"}),
        ("PUT", "/admin/auth/users/%s" % uid, {"role_id": str(role("Finance"))}),
    ]
    for method, path, body in cases:
        TD.register(uid, "2.2.2.2", "dev")
        assert devices(user_id=uid)
        r = boss.request(method, path, json=body)
        assert r.status_code == 200, (path, body, r.text)
        assert devices(user_id=uid) == [], (path, body)
        assert len(devices(user_id=other)) == 1


def test_a_harmless_change_does_not_revoke_devices(sms):
    uid = mk_user()
    boss = admin_session()
    TD.register(uid, "2.2.2.2", "dev")
    assert boss.put("/admin/auth/users/%s" % uid, json={"designation": "Lead", "status": "active"}).status_code == 200
    assert len(devices(user_id=uid)) == 1


def test_deleting_an_account_revokes_its_devices(sms):
    uid = mk_user()
    boss = admin_session()
    TD.register(uid, "2.2.2.2", "dev")
    assert boss.delete("/admin/auth/users/%s" % uid).status_code == 200 and devices(user_id=uid) == []


def test_a_revoked_browser_needs_the_otp_after_a_pin_reset(sms):
    uid = mk_user()
    c = trusted(sms)
    boss = admin_session()
    assert boss.put("/admin/auth/users/%s/reset-pin" % uid, json={"new_pin": "4321"}).status_code == 200
    r = login(c, pin="4321", remember=True)
    assert r.status_code == 200 and r.json()["otp_required"] is True


def test_the_account_can_list_and_revoke_its_own_devices_without_ever_seeing_tokens(sms):
    uid = mk_user()
    c = trusted(sms)
    TD.register(uid, "3.3.3.3", "second-browser")
    other_device = TD.register(mk_user(OTHER), "4.4.4.4", "someone else")
    listing = c.get("/admin/auth/trusted-devices")
    assert listing.status_code == 200 and len(listing.json()) == 2 and sum(1 for d in listing.json() if d["current"]) == 1
    flat = str(listing.json())
    assert "token" not in flat and other_device not in flat and set(listing.json()[0]) == {"id", "current", "ip_address", "user_agent", "created_at", "expires_at", "last_used_at"}
    foreign = devices(user_id=DB.dashboard_users.find_one({"mobile": OTHER})["_id"])[0]
    assert c.delete("/admin/auth/trusted-devices/%s" % foreign["_id"]).status_code == 404       # never someone else's
    assert c.delete("/admin/auth/trusted-devices/%s" % ObjectId()).status_code == 404
    assert c.delete("/admin/auth/trusted-devices/not-an-id").status_code == 400
    mine = [d for d in listing.json() if not d["current"]][0]["id"]
    assert c.delete("/admin/auth/trusted-devices/%s" % mine).status_code == 200 and len(c.get("/admin/auth/trusted-devices").json()) == 1
    assert c.delete("/admin/auth/trusted-devices").status_code == 200 and devices(user_id=uid) == [] and len(devices()) == 1
    assert login(c, remember=True).json()["otp_required"] is True


def test_logout_keeps_the_device_unless_asked_to_forget_it(sms):
    mk_user()
    c = trusted(sms)
    assert c.post("/admin/auth/logout", json={}).status_code == 200 and len(devices()) == 1
    assert login(c, remember=True).json()["otp_required"] is False                              # still trusted after a normal logout
    r = c.post("/admin/auth/logout", json={"keep_trusted": False})
    assert r.status_code == 200 and devices() == []
    assert login(c, remember=True).json()["otp_required"] is True


# ── secrets stay out of logs, URLs and responses ──────────────────────────────────────────────────

def test_device_tokens_never_appear_in_logs_urls_or_response_bodies(sms, capsys):
    mk_user()
    c = client()
    v = full_login(c, sms)
    token = c.cookies.get(COOKIE)
    r2 = login(c, remember=True)
    for resp in (v, r2, c.get("/admin/auth/device-status"), c.get("/admin/auth/trusted-devices")):
        assert token not in resp.text and token not in str(resp.url)
    out = capsys.readouterr()
    assert token not in out.out + out.err
    assert all(token not in str(row) for coll in (TD.COLLECTION, "activity_logs", "admin_auth_throttle", "dashboard_users") for row in DB[coll].find({}))


def test_the_login_page_has_the_checkbox_and_sends_only_a_request_not_a_proof():
    page = open(os.path.join(ROOT, "templates", "admin_login.html"), encoding="utf-8").read()
    for needle in ('id="rememberDevice"', "remember_device: document.getElementById", "/admin/auth/device-status", "data.otp_required === false"):
        assert needle in page, needle
    assert "localstorage" not in page.lower() and "sessionstorage" not in page.lower() and "document.cookie" not in page
