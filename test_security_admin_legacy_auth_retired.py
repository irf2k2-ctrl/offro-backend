"""Patch B: legacy (password-only / admins.token) admin authentication is retired; only PIN + OTP sessions (dashboard_users) are accepted.
Nothing is deleted or revoked: legacy rows stay in the database, they simply no longer authenticate. MSG91 is mocked. Run:
python -m pytest test_security_admin_legacy_auth_retired.py -q -W ignore"""
import os
import re
import sys

import mongomock
import pytest
from fastapi.testclient import TestClient

import server
from routers import admin as ADMIN
from routers import dashboard_auth as DA
from routers import popup_campaigns as POPUP
from routers import wa_chat as WA
from services import admin_login_otp as OTP
from tools import create_super_admin as TOOL

DB = sys.modules["database"].db
ROOT = os.path.dirname(os.path.abspath(server.__file__))
LEGACY, GOOD = "legacy-token-123", "otp-session-token"
SHARED = ("admins", "dashboard_users", "dashboard_roles", "activity_logs", "trusted_devices")
OWN = ("admin_login_challenges", "admin_auth_throttle")


class Sent(list):
    def __call__(self, mobile, otp, tpl):
        self.append(otp)
        return {"ok": True}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    saved = {c: list(DB[c].find({})) for c in SHARED}
    for c in SHARED + OWN:
        DB[c].delete_many({})
    for mod in (WA, POPUP, ADMIN, DA, server):                 # other test files re-point some of these modules at private databases; use the shared one here
        monkeypatch.setattr(mod, "db", DB)
    for k in ("ADMIN_OTP_STAGING_TEST_MODE", "ADMIN_OTP_TEST_MOBILES"):
        monkeypatch.delenv(k, raising=False)
    sent = Sent()
    monkeypatch.setattr(OTP, "_send_sms", sent)
    DB.admins.insert_one({"username": "admin", "password": "admin123", "token": LEGACY})         # a still-valid legacy session and default account
    role = DB.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    ro = DB.dashboard_roles.insert_one({"role_name": "Read Only", "permissions": {"Categories": {"view": True}}}).inserted_id
    DB.dashboard_users.insert_one({"mobile": "9000000100", "full_name": "Boss", "pin": DA.hash_pin("1234"), "status": "active", "token": GOOD,
                                   "role_id": role, "last_active_at": __import__("datetime").datetime.utcnow()})
    DB.dashboard_users.insert_one({"mobile": "9000000200", "full_name": "Viewer", "pin": DA.hash_pin("1234"), "status": "active", "token": "ro-token",
                                   "role_id": ro, "last_active_at": __import__("datetime").datetime.utcnow()})
    yield sent
    for c in SHARED + OWN:
        DB[c].delete_many({})
    for c, docs in saved.items():
        if docs:
            DB[c].insert_many(docs)


def client():
    return TestClient(server.app, raise_server_exceptions=False, follow_redirects=False)


# every active consumer of an admin session: (name, request callable)
CONSUMERS = [
    ("admin.get_current_admin", lambda c, t: c.get("/admin/categories", cookies={"admin_token": t})),
    ("admin.get_current_admin (bearer)", lambda c, t: c.get("/admin/categories", headers={"Authorization": "Bearer " + t})),
    ("dashboard_auth.get_current_dashboard_user", lambda c, t: c.get("/admin/auth/me", cookies={"admin_token": t})),
    ("wa_chat", lambda c, t: c.get("/admin/whatsapp/chats", cookies={"admin_token": t})),
    ("wa_chat (header)", lambda c, t: c.get("/admin/whatsapp/chats", headers={"X-Admin-Token": t})),
    ("popup_campaigns", lambda c, t: c.get("/admin/popup-campaigns", cookies={"admin_token": t})),
    ("popup_campaigns (header)", lambda c, t: c.get("/admin/popup-campaigns", headers={"x-admin-token": t})),
    ("download", lambda c, t: c.get("/download/admin_dashboard", cookies={"admin_token": t})),
    ("dashboard page", lambda c, t: c.get("/admin/dashboard", cookies={"admin_token": t})),
]


# ── 1. the password-only login ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [{"username": "admin", "password": "admin123"}, {"username": "x", "password": "y"},
                                  {"username": {"$ne": None}, "password": {"$ne": None}}, {}, {"username": "admin"}])
def test_legacy_login_returns_410_and_creates_no_session_or_token(body):
    before = DB.admins.find_one({"username": "admin"})
    users_before = list(DB.dashboard_users.find({}))
    r = client().post("/admin/login", json=body)
    assert r.status_code == 410 and "retired" in r.json()["detail"] and "/admin" in r.json()["detail"]
    assert "admin_token" not in r.cookies and "set-cookie" not in {k.lower() for k in r.headers}
    assert DB.admins.find_one({"username": "admin"}) == before                                   # no token written, nothing changed
    assert list(DB.dashboard_users.find({})) == users_before


def test_legacy_login_with_no_body_and_wrong_method_is_not_a_login():
    assert client().post("/admin/login").status_code in (410, 422)
    assert client().get("/admin/login").status_code == 405


def test_the_unmounted_super_admins_login_is_still_not_reachable():
    c = client()
    for path in ("/admin-dashboard/admin/login", "/admin/admin/login"):
        assert c.post(path, json={"username": "admin", "password": "admin123"}).status_code in (404, 405)


# ── 2. legacy tokens are refused by every consumer; OTP sessions still work ───────────────────────

@pytest.mark.parametrize("name,call", CONSUMERS, ids=[n for n, _ in CONSUMERS])
def test_a_legacy_admins_token_is_refused_everywhere(name, call):
    r = call(client(), LEGACY)
    assert r.status_code in (401, 403, 307), (name, r.status_code)
    if r.status_code == 307:
        assert r.headers["location"] == "/admin"                                                  # dashboard page redirects to the login page


@pytest.mark.parametrize("name,call", CONSUMERS, ids=[n for n, _ in CONSUMERS])
def test_an_unknown_token_is_refused_everywhere(name, call):
    assert call(client(), "no-such-token").status_code in (401, 403, 307)


@pytest.mark.parametrize("name,call", CONSUMERS, ids=[n for n, _ in CONSUMERS])
def test_an_otp_created_dashboard_session_still_works_everywhere(name, call):
    assert call(client(), GOOD).status_code == 200, name


def test_disabled_and_suspended_accounts_are_still_refused(env):
    for status in ("disabled", "suspended"):
        DB.dashboard_users.update_one({"token": GOOD}, {"$set": {"status": status}})
        for name, call in CONSUMERS:
            if name == "dashboard page":
                continue                                                                          # unchanged: the page itself only checks the session exists
            assert call(client(), GOOD).status_code == 403, (status, name)


def test_role_permissions_are_still_enforced():
    c = client()
    assert c.get("/admin/categories", cookies={"admin_token": "ro-token"}).status_code == 200
    r = c.post("/admin/categories", json={"name": "Zed"}, cookies={"admin_token": "ro-token"})
    assert r.status_code == 403 and "Missing" in r.json()["detail"]


def test_refusing_a_legacy_token_does_not_delete_or_modify_it():
    before = DB.admins.find_one({"token": LEGACY})
    for name, call in CONSUMERS:
        call(client(), LEGACY)
    assert DB.admins.find_one({"token": LEGACY}) == before and DB.admins.count_documents({}) == 1


def test_the_full_pin_and_otp_sign_in_still_works_and_its_session_reaches_the_consumers(env):
    c = client()
    lr = c.post("/admin/auth/login", json={"mobile": "9000000100", "pin": "1234"})
    assert lr.status_code == 200 and "admin_token" not in lr.cookies
    vr = c.post("/admin/auth/verify-otp", json={"challenge_id": lr.json()["challenge_id"], "otp": env[-1]})
    assert vr.status_code == 200 and "admin_token" in vr.cookies
    tok = vr.cookies["admin_token"]
    assert tok not in (LEGACY, GOOD)
    for name, call in CONSUMERS:
        assert call(client(), tok).status_code == 200, name


def test_super_admin_still_needs_the_otp_and_a_wrong_pin_is_still_refused(env):
    c = client()
    assert c.post("/admin/auth/login", json={"mobile": "9000000100", "pin": "9999"}).status_code == 401
    lr = c.post("/admin/auth/login", json={"mobile": "9000000100", "pin": "1234"})
    assert "admin_token" not in lr.cookies and c.get("/admin/auth/me").status_code in (401, 403)


# ── 3. default credentials and logging ─────────────────────────────────────────────────────────────

def test_seed_admin_creates_no_default_account_and_prints_no_credentials(monkeypatch, capsys):
    private = mongomock.MongoClient()["seed_admin_test"]
    monkeypatch.setattr(ADMIN, "db", private)
    ADMIN.seed_admin()
    out = capsys.readouterr().out
    assert private.admins.count_documents({}) == 0 and "admin123" not in out and "password" not in out.lower()
    assert private.categories.count_documents({}) > 0                                              # the unrelated category migration still runs


def test_seed_admin_leaves_an_existing_legacy_row_untouched(monkeypatch):
    private = mongomock.MongoClient()["seed_admin_test2"]
    private.admins.insert_one({"username": "admin", "password": "something-else", "token": "t"})
    monkeypatch.setattr(ADMIN, "db", private)
    ADMIN.seed_admin()
    assert private.admins.count_documents({}) == 1 and private.admins.find_one({})["password"] == "something-else"


def test_seed_rbac_seeds_roles_but_no_default_user_and_prints_no_credentials(monkeypatch, capsys):
    private = mongomock.MongoClient()["seed_rbac_test"]
    monkeypatch.setattr(DA, "db", private)
    monkeypatch.setenv("RBAC_SUPER_ADMIN_MOBILE", "9999999999")
    monkeypatch.setenv("RBAC_SUPER_ADMIN_PIN", "123456")                                            # the old env settings are ignored now
    DA.seed_rbac()
    out = capsys.readouterr().out
    assert private.dashboard_roles.count_documents({"role_name": "Super Admin"}) == 1
    assert private.dashboard_users.count_documents({}) == 0
    for secret in ("123456", "9999999999", "PIN:", "Mobile:"):
        assert secret not in out
    assert "create_super_admin" in out


def test_seed_rbac_does_not_touch_existing_users_or_roles(monkeypatch):
    private = mongomock.MongoClient()["seed_rbac_test2"]
    rid = private.dashboard_roles.insert_one({"role_name": "Custom", "permissions": {"X": {"view": True}}}).inserted_id
    private.dashboard_users.insert_one({"mobile": "9111111111", "pin": "salt$hash", "role_id": rid, "status": "active", "token": "keep"})
    snap = (list(private.dashboard_roles.find({})), list(private.dashboard_users.find({})))
    monkeypatch.setattr(DA, "db", private)
    DA.seed_rbac()
    assert (list(private.dashboard_roles.find({})), list(private.dashboard_users.find({}))) == snap


def test_no_active_module_reads_the_legacy_admins_collection_or_embeds_default_credentials():
    for rel in ("routers/admin.py", "routers/dashboard_auth.py", "routers/wa_chat.py", "routers/popup_campaigns.py", "server.py"):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert not re.search(r"db\.admins\s*\.\s*\w+\(|db\[[\"']admins[\"']\]|admin_collection", src), rel          # (comments may mention it)
        assert "admin123" not in src and "RBAC_SUPER_ADMIN" not in src, rel
    sessions = [r for r in ("routers/admin.py", "routers/dashboard_auth.py", "routers/wa_chat.py", "routers/popup_campaigns.py", "server.py")
                if re.search(r"set_cookie\(\s*(key=)?[\"']admin_token", open(os.path.join(ROOT, r), encoding="utf-8").read())]
    assert sessions == ["routers/dashboard_auth.py"]                                                # only the OTP verify step can create an admin session


# ── 4. first Super Admin on an empty database ──────────────────────────────────────────────────────

def test_the_first_super_admin_tool_creates_a_hashed_super_admin_and_cannot_overwrite(capsys):
    private = mongomock.MongoClient()["tool_test"]
    private.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}})
    uid = TOOL.create_first_super_admin(private, "9222222222", "246810", DA.hash_pin)
    u = private.dashboard_users.find_one({})
    assert str(u["_id"]) == uid and u["pin"] != "246810" and "246810" not in str(u) and DA.verify_pin("246810", u["pin"])
    assert u["role_id"] == private.dashboard_roles.find_one({})["_id"] and u["token"] is None and u["status"] == "active"
    with pytest.raises(ValueError):
        TOOL.create_first_super_admin(private, "9222222222", "111111", DA.hash_pin)                 # existing mobile: refuse, change nothing
    assert private.dashboard_users.count_documents({}) == 1 and DA.verify_pin("246810", private.dashboard_users.find_one({})["pin"])
    for bad_mobile, bad_pin in (("123", "1234"), ("9222222223", "12"), ("9222222223", "abcd"), ("", "1234")):
        with pytest.raises(ValueError):
            TOOL.create_first_super_admin(private, bad_mobile, bad_pin, DA.hash_pin)
    empty = mongomock.MongoClient()["tool_test2"]
    with pytest.raises(ValueError):
        TOOL.create_first_super_admin(empty, "9222222224", "1234", DA.hash_pin)                     # no role seeded yet
    assert "246810" not in capsys.readouterr().out


def test_a_tool_created_super_admin_can_sign_in_with_pin_and_otp(env):
    TOOL.create_first_super_admin(DB, "9333333333", "135790", DA.hash_pin)
    c = client()
    lr = c.post("/admin/auth/login", json={"mobile": "9333333333", "pin": "135790"})
    assert lr.status_code == 200 and "admin_token" not in lr.cookies
    vr = c.post("/admin/auth/verify-otp", json={"challenge_id": lr.json()["challenge_id"], "otp": env[-1]})
    assert vr.status_code == 200 and c.get("/admin/categories", cookies={"admin_token": vr.cookies["admin_token"]}).status_code == 200
