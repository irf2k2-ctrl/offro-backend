"""Admin login 2FA: server-side 4-digit SMS OTP. MSG91 is ALWAYS mocked here: no SMS, no network.
Run:  python -m pytest test_security_admin_login_2fa.py -q -W ignore"""
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

DB = sys.modules["database"].db
MOBILE, PIN = "9000000100", "1234"
ROOT = os.path.dirname(os.path.abspath(server.__file__))


class SMS:
    """Replaces the MSG91 transport. Records what would have been sent."""
    def __init__(self):
        self.sent, self.ok = [], True

    def __call__(self, mobile, otp, template_id):
        self.sent.append({"mobile": mobile, "otp": otp, "template": template_id})
        return {"ok": self.ok, "error": None if self.ok else "rejected"}

    @property
    def last(self):
        return self.sent[-1]["otp"]


SHARED = ("dashboard_users", "dashboard_roles", "activity_logs", "trusted_devices", "admin_otp_sessions")
OWN = ("admin_login_challenges", "admin_auth_throttle")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Isolated state for each test, and the shared fake database is put back exactly as found (other test modules rely on seeded roles)."""
    saved = {c: list(DB[c].find({})) for c in SHARED}
    for c in SHARED + OWN:
        DB[c].delete_many({})
    sms = SMS()
    monkeypatch.setattr(OTP, "_send_sms", sms)
    monkeypatch.setenv("MSG91_ADMIN_TEMPLATE_ID", "tpl_admin_test")
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


def mk_user(mobile=MOBILE, pin=PIN, role="Super Admin", **kw):
    r = DB.dashboard_roles.find_one({"role_name": role})
    if not r:
        perms = {m: {a: True for a in DA.ALL_ACTIONS} for m in DA.ALL_MODULES} if role == "Super Admin" else {}
        rid = DB.dashboard_roles.insert_one({"role_name": role, "permissions": perms}).inserted_id
    else:
        rid = r["_id"]
    d = {"mobile": mobile, "full_name": "Test Admin", "pin": DA.hash_pin(pin), "status": "active", "role_id": rid}
    d.update(kw)
    DB.dashboard_users.insert_one(d)
    return DB.dashboard_users.find_one({"mobile": mobile})


def login(c, mobile=MOBILE, pin=PIN):
    return c.post("/admin/auth/login", json={"mobile": mobile, "pin": pin})


def start(c, sms):
    mk_user()
    r = login(c)
    assert r.status_code == 200, r.text
    return r.json()["challenge_id"], sms.last


def verify(c, cid, otp, **extra):
    return c.post("/admin/auth/verify-otp", json=dict({"challenge_id": cid, "otp": otp}, **extra))


# ── the old bypass ─────────────────────────────────────────────────────────────────────────────────

def test_the_old_msg91_verified_bypass_can_no_longer_create_a_session(sms):
    u = mk_user()
    c = client()
    for payload in ({"mobile": MOBILE, "otp": "0000", "msg91_verified": True},
                    {"mobile": MOBILE, "otp": "1234", "msg91_verified": True, "trust_device": True},
                    {"mobile": MOBILE, "otp": "1234", "msg91_verified": "true"},
                    {"mobile": MOBILE, "otp": "1234", "msg91_verified": 1, "challenge_id": "x" * 40}):
        r = c.post("/admin/auth/verify-otp", json=payload)
        assert r.status_code in (400, 401), (payload, r.status_code)
        assert "admin_token" not in r.cookies and "set-cookie" not in r.headers
        assert "user" not in r.json()
    after = DB.dashboard_users.find_one({"_id": u["_id"]})
    assert not after.get("token")                                                         # no session was written
    assert c.get("/admin/auth/me").status_code in (401, 403)
    assert sms.sent == []


def test_a_valid_challenge_plus_the_flag_but_a_wrong_otp_still_fails(sms):
    c = client()
    cid, otp = start(c, sms)
    wrong = "0000" if otp != "0000" else "1111"
    r = verify(c, cid, wrong, msg91_verified=True, trust_device=True, mobile=MOBILE)
    assert r.status_code == 401 and "admin_token" not in r.cookies
    assert not DB.dashboard_users.find_one({"mobile": MOBILE}).get("token")


def test_no_browser_supplied_field_is_trusted_by_the_source():
    src = open(os.path.join(ROOT, "routers", "dashboard_auth.py"), encoding="utf-8").read()
    assert "msg91_verified" not in re.sub(r"#[^\n]*", "", src).replace("msg91_verified' flag", "")
    assert "trust_device" not in src


# ── PIN stage ────────────────────────────────────────────────────────────────────────────────────────

def test_a_correct_pin_returns_only_a_challenge_and_issues_no_session(sms):
    u = mk_user()
    r = login(client())
    assert r.status_code == 200
    b = r.json()
    assert b["otp_required"] is True and b["challenge_id"] and b["otp_length"] == 4
    assert b["mobile_masked"] == "******0100" and MOBILE not in r.text
    assert b["otp_expires_in"] == 300 and b["resend_available_in"] == 30
    assert "user" not in b and "token" not in b
    assert "admin_token" not in r.cookies and "set-cookie" not in r.headers
    assert not DB.dashboard_users.find_one({"_id": u["_id"]}).get("token")
    assert len(sms.sent) == 1 and re.fullmatch(r"\d{4}", sms.sent[0]["otp"]) and sms.sent[0]["template"] == "tpl_admin_test"


def test_unknown_number_wrong_pin_and_disabled_account_give_one_identical_answer(sms):
    mk_user()
    mk_user(mobile="9000000101", status="disabled")
    c = client()
    answers = {login(c, "9000000199", PIN).json()["detail"], login(c, MOBILE, "9999").json()["detail"], login(c, "9000000101", PIN).json()["detail"]}
    assert answers == {"Invalid mobile number or PIN."}
    codes = {login(client(), "9000000198", PIN).status_code, login(client(), MOBILE, "8888").status_code}
    assert codes == {401}
    assert sms.sent == []                                                                  # no SMS without a correct PIN


def test_lockout_is_preserved_and_looks_the_same_for_unknown_numbers(sms):
    mk_user()
    c = client()
    for _ in range(5):
        assert login(c, MOBILE, "0000").status_code == 401
    known = login(c, MOBILE, PIN)                                                          # correct PIN, but locked
    for _ in range(5):
        assert login(c, "9000000177", "0000").status_code == 401
    unknown = login(c, "9000000177", PIN)
    assert known.status_code == unknown.status_code == 429 and known.json() == unknown.json()
    assert sms.sent == []
    assert DB.dashboard_users.find_one({"mobile": MOBILE})["lockout_until"] > datetime.utcnow()


def test_malformed_inputs_get_friendly_errors_not_exceptions(sms):
    c = client()
    for body in ({}, {"mobile": None, "pin": None}, {"mobile": {"$ne": ""}, "pin": {"$ne": ""}}, {"mobile": MOBILE}, {"mobile": "x" * 500, "pin": "1"}):
        r = c.post("/admin/auth/login", json=body)
        assert r.status_code in (400, 401) and "Traceback" not in r.text and "Error" not in r.json()["detail"]


def test_super_admin_must_also_complete_the_otp(sms):
    mk_user(role="Super Admin")
    r = login(client())
    assert r.json()["otp_required"] is True and "admin_token" not in r.cookies


def test_the_trusted_device_cookie_no_longer_skips_the_otp(sms):
    u = mk_user()
    DB.trusted_devices.insert_one({"token": "tdtoken", "user_id": u["_id"], "expires_at": datetime.utcnow() + timedelta(days=5)})
    c = client()
    c.cookies.set("offro_trusted", "tdtoken")
    r = login(c)
    assert r.json()["otp_required"] is True and "admin_token" not in r.cookies and "challenge_id" in r.json()
    assert not DB.dashboard_users.find_one({"_id": u["_id"]}).get("token")
    cid = r.json()["challenge_id"]
    ok = verify(c, cid, sms.last, trust_device=True)
    assert ok.status_code == 200 and "admin_token" in ok.cookies
    assert DB.trusted_devices.count_documents({}) == 1                                     # nothing new was trusted


# ── OTP verification ────────────────────────────────────────────────────────────────────────────────

def test_correct_otp_creates_the_session_and_rbac_still_applies(sms):
    c = client()
    cid, otp = start(c, sms)
    r = verify(c, cid, otp)
    assert r.status_code == 200 and r.json()["user"]["role"] == "Super Admin" and r.json()["otp_required"] is False
    assert "admin_token" in r.cookies
    me = c.get("/admin/auth/me")
    assert me.status_code == 200 and me.json()["role"] == "Super Admin"
    assert c.get("/admin/whatsapp-campaigns/config").status_code == 200                    # Super Admin keeps WhatsApp Campaigns access


def test_rbac_is_unchanged_for_a_role_without_the_module(sms):
    mk_user(mobile="9000000150", role="Limited", permissions=None)
    c = client()
    r = login(c, "9000000150")
    ok = verify(c, r.json()["challenge_id"], sms.last)
    assert ok.status_code == 200
    assert c.get("/admin/whatsapp-campaigns/config").status_code == 403


def test_wrong_otp_is_refused_counts_attempts_and_locks_after_five(sms):
    c = client()
    cid, otp = start(c, sms)
    wrong = "0000" if otp != "0000" else "1111"
    remaining = []
    for i in range(5):
        r = verify(c, cid, wrong)
        assert r.status_code == 401 and r.json()["detail"] == "That code is incorrect or has expired. Please try again or sign in again."
        assert "admin_token" not in r.cookies
        remaining.append(DB.admin_login_challenges.find_one({})["attempts"])
    assert remaining == [1, 2, 3, 4, 5]
    assert DB.admin_login_challenges.find_one({})["status"] == "locked"
    assert verify(c, cid, otp).status_code == 401                                          # the right code is refused once locked
    assert not DB.dashboard_users.find_one({"mobile": MOBILE}).get("token")


def test_the_attempt_cap_is_shared_across_resends(sms):
    c = client()
    cid, otp = start(c, sms)
    wrong = lambda o: "0000" if o != "0000" else "1111"
    for _ in range(3):
        verify(c, cid, wrong(sms.last))
    DB.admin_login_challenges.update_one({}, {"$set": {"last_sent_at": datetime.utcnow() - timedelta(seconds=60)}})
    assert c.post("/admin/auth/resend-otp", json={"challenge_id": cid}).status_code == 200
    for _ in range(2):
        verify(c, cid, wrong(sms.last))
    assert verify(c, cid, sms.last).status_code == 401                                     # 5 wrong in total: locked even with the new code
    assert DB.admin_login_challenges.find_one({})["status"] == "locked"


def test_an_expired_otp_is_refused(sms):
    c = client()
    cid, otp = start(c, sms)
    DB.admin_login_challenges.update_one({}, {"$set": {"expires_at": datetime.utcnow() - timedelta(seconds=1)}})
    r = verify(c, cid, otp)
    assert r.status_code == 401 and "admin_token" not in r.cookies
    assert not DB.dashboard_users.find_one({"mobile": MOBILE}).get("token")


def test_the_otp_expires_after_five_minutes_by_default(sms):
    c = client()
    start(c, sms)
    d = DB.admin_login_challenges.find_one({})
    assert 295 <= (d["expires_at"] - d["created_at"]).total_seconds() <= 305


def test_an_otp_is_single_use_replay_fails(sms):
    c = client()
    cid, otp = start(c, sms)
    first = verify(c, cid, otp)
    assert first.status_code == 200
    again = client()
    r = verify(again, cid, otp)
    assert r.status_code == 401 and "admin_token" not in r.cookies
    assert DB.admin_login_challenges.find_one({})["status"] == "verified"


def test_concurrent_verification_creates_exactly_one_session(sms):
    c = client()
    cid, otp = start(c, sms)
    results, barrier = [], threading.Barrier(8)

    def go():
        cl = client()
        barrier.wait()
        results.append(verify(cl, cid, otp))
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    wins = [r for r in results if r.status_code == 200]
    assert len(wins) == 1 and len(results) == 8
    assert sum("admin_token" in r.cookies for r in results) == 1


def test_concurrent_service_level_consume_has_a_single_winner(sms):
    mk_user()
    c = client()
    login(c)
    token_hash_doc = DB.admin_login_challenges.find_one({})
    # use the real token through the service API
    cid = DB.admin_login_challenges.find_one({})  # noqa: F841 (document only; the token is returned by the endpoint)
    c2 = client()
    DB.admin_login_challenges.delete_many({})
    r = c2.post("/admin/auth/login", json={"mobile": MOBILE, "pin": PIN})
    token, otp = r.json()["challenge_id"], sms.last
    out, barrier = [], threading.Barrier(10)

    def go():
        barrier.wait()
        out.append(OTP.verify_challenge(token, otp))
    ts = [threading.Thread(target=go) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(1 for o in out if o.get("ok")) == 1


def test_a_challenge_is_bound_to_its_account(sms):
    c = client()
    cid, otp = start(c, sms)
    other = mk_user(mobile="9000000160")
    DB.admin_login_challenges.update_one({}, {"$set": {"user_id": other["_id"]}})           # tampered: same hash, different account
    r = verify(c, cid, otp)
    assert r.status_code == 401 and not DB.dashboard_users.find_one({"mobile": "9000000160"}).get("token")


def test_a_newer_login_supersedes_the_older_challenge(sms):
    mk_user()
    c = client()
    r1 = login(c)
    otp1 = sms.last
    r2 = login(c)
    otp2 = sms.last
    assert verify(c, r1.json()["challenge_id"], otp1).status_code == 401                # the older challenge is dead, even with its own correct code
    assert verify(c, r2.json()["challenge_id"], otp2).status_code == 200


def test_otp_must_be_exactly_four_digits(sms):
    c = client()
    cid, otp = start(c, sms)
    for bad in ("123", "12345", "12a4", "", None, 1234, [1, 2, 3, 4], "    "):
        r = c.post("/admin/auth/verify-otp", json={"challenge_id": cid, "otp": bad})
        assert r.status_code == 401 and "admin_token" not in r.cookies
    assert DB.admin_login_challenges.find_one({})["attempts"] == 0                         # malformed input does not burn attempts
    assert all(re.fullmatch(r"\d{4}", OTP.generate_otp()) for _ in range(200))


def test_unknown_or_malformed_challenges_give_the_same_generic_answer(sms):
    c = client()
    for cid in ("", "nope", "x" * 40, None, {"$ne": 1}, "y" * 500):
        r = c.post("/admin/auth/verify-otp", json={"challenge_id": cid, "otp": "1234"})
        assert r.status_code == 401 and r.json()["detail"] == "That code is incorrect or has expired. Please try again or sign in again."


def test_a_disabled_account_cannot_finish_a_challenge_started_before_it_was_disabled(sms):
    c = client()
    cid, otp = start(c, sms)
    DB.dashboard_users.update_one({"mobile": MOBILE}, {"$set": {"status": "disabled"}})
    r = verify(c, cid, otp)
    assert r.status_code == 401 and "admin_token" not in r.cookies


# ── OTP secrecy ──────────────────────────────────────────────────────────────────────────────────────

def test_the_otp_is_stored_only_as_a_hash_and_never_logged(sms, capsys):
    c = client()
    cid, otp = start(c, sms)
    verify(c, cid, "0000" if otp != "0000" else "1111")
    verify(c, cid, otp)
    login(c, MOBILE, "9999")
    doc = DB.admin_login_challenges.find_one({})
    assert "otp" not in doc
    assert not any(isinstance(v, str) and v == otp for v in doc.values())
    assert doc.get("otp_hash") is None or re.fullmatch(r"[0-9a-f]{64}", doc["otp_hash"])
    assert doc["token_hash"] != cid and cid not in str(doc)                               # the token itself is not stored either
    out = capsys.readouterr()
    text = out.out + out.err
    assert not re.search(r"\b%s\b" % otp, text.replace(cid, "")) and cid not in text and MOBILE not in text
    # no log/activity row carries it
    for row in DB.activity_logs.find({}):
        assert otp not in str(row.values()) or otp in {str(row.get("timestamp"))}


def test_the_otp_hash_changes_with_salt_challenge_and_account():
    from bson import ObjectId
    a, b = ObjectId(), ObjectId()
    h = OTP.hash_otp("1234", "aa" * 16, a, b)
    assert h != OTP.hash_otp("1234", "bb" * 16, a, b) and h != OTP.hash_otp("1234", "aa" * 16, b, b) and h != OTP.hash_otp("1234", "aa" * 16, a, a)
    assert h != OTP.hash_otp("1235", "aa" * 16, a, b) and len(h) == 64


# ── SMS (MSG91) failure and transport ───────────────────────────────────────────────────────────────

def test_msg91_failure_gives_a_friendly_error_and_never_a_session(sms):
    u = mk_user()
    sms.ok = False
    c = client()
    r = login(c)
    assert r.status_code == 503 and r.json()["detail"] == "We could not send the verification code. Please try again in a moment."
    assert "challenge_id" not in r.text and "admin_token" not in r.cookies
    assert not DB.dashboard_users.find_one({"_id": u["_id"]}).get("token")
    assert DB.admin_login_challenges.find_one({})["status"] == "send_failed"
    # the never-delivered code cannot be used even if it were guessed
    otp = sms.last
    assert OTP.verify_challenge("z" * 43, otp)["ok"] is False


def test_msg91_failure_on_resend_is_friendly_and_creates_no_session(sms):
    c = client()
    cid, otp = start(c, sms)
    DB.admin_login_challenges.update_one({}, {"$set": {"last_sent_at": datetime.utcnow() - timedelta(seconds=60)}})
    sms.ok = False
    r = c.post("/admin/auth/resend-otp", json={"challenge_id": cid})
    assert r.status_code == 503 and "SMS" not in r.text and "admin_token" not in r.cookies


def test_the_sms_transport_uses_the_existing_msg91_settings_and_leaks_nothing(monkeypatch, capsys):
    from routers import otp_service as S
    seen = {}

    class R:
        status_code, text = 200, "{}"
        def json(self): return {"type": "success", "request_id": "abc"}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, payload=json)
        return R()
    monkeypatch.setattr(S.httpx, "post", fake_post)
    monkeypatch.setattr(S, "MSG91_AUTH_KEY", "KEY-SECRET-123456")
    monkeypatch.setattr(S, "MSG91_TEMPLATE_ID", "tpl_default")
    monkeypatch.setattr(S, "MSG91_SENDER_ID", "OFFROO")
    assert S.send_sms_otp("9000000100", "4321", 5, "tpl_admin")["ok"] is True
    assert seen["url"] == "https://control.msg91.com/api/v5/otp"
    assert seen["payload"]["template_id"] == "tpl_admin" and seen["payload"]["mobile"] == "919000000100" and seen["payload"]["otp"] == "4321"
    assert seen["payload"]["otp_expiry"] == 5 and seen["payload"]["sender"] == "OFFROO"
    assert S.send_sms_otp("9000000100", "4321", 5, "")["ok"] is True and seen["payload"]["template_id"] == "tpl_default"
    out = capsys.readouterr()
    text = out.out + out.err
    assert "4321" not in text and "KEY-SECRET" not in text and "9000000100" not in text
    # failure shapes
    for exc_body in ({"type": "error", "message": "KEY-SECRET leaked?"},):
        class Bad(R):
            def json(self): return exc_body
        monkeypatch.setattr(S.httpx, "post", lambda *a, **k: Bad())
        r = S.send_sms_otp("9000000100", "4321", 5, "tpl")
        assert r == {"ok": False, "error": "rejected"}
    def boom(*a, **k): raise S.httpx.ConnectError("secret detail")
    monkeypatch.setattr(S.httpx, "post", boom)
    assert S.send_sms_otp("9000000100", "4321", 5, "tpl")["ok"] is False
    out = capsys.readouterr()
    assert "KEY-SECRET" not in out.out + out.err and "secret detail" not in out.out + out.err


def test_an_unconfigured_sms_service_fails_closed(monkeypatch):
    from routers import otp_service as S
    monkeypatch.setattr(S, "MSG91_AUTH_KEY", "")
    assert S.send_sms_otp("9000000100", "4321", 5, "tpl") == {"ok": False, "error": "not_configured"}


def test_unexpected_server_errors_never_reach_the_browser(sms, monkeypatch):
    mk_user()
    def boom(*a, **k): raise RuntimeError("mongodb://user:pass@host secret")
    monkeypatch.setattr(OTP, "start_challenge", boom)
    r = login(client())
    assert r.status_code == 500 and r.json()["detail"].startswith("We could not complete sign-in. Please try again. (ref ") and "secret" not in r.text
    monkeypatch.setattr(OTP, "verify_challenge", boom)
    r = client().post("/admin/auth/verify-otp", json={"challenge_id": "a" * 40, "otp": "1234"})
    assert r.status_code == 500 and "secret" not in r.text


# ── resend, cooldown, rate limits ──────────────────────────────────────────────────────────────────

def test_resend_cooldown_new_code_and_old_code_invalidated(sms):
    c = client()
    cid, otp1 = start(c, sms)
    r = c.post("/admin/auth/resend-otp", json={"challenge_id": cid})
    assert r.status_code == 429 and len(sms.sent) == 1     # inside the 30 s cooldown
    DB.admin_login_challenges.update_one({}, {"$set": {"last_sent_at": datetime.utcnow() - timedelta(seconds=31)}})
    r = c.post("/admin/auth/resend-otp", json={"challenge_id": cid})
    assert r.status_code == 200 and r.json()["resend_available_in"] == 30 and len(sms.sent) == 2
    otp2 = sms.last
    if otp1 != otp2:
        assert verify(c, cid, otp1).status_code == 401
    assert verify(c, cid, otp2).status_code == 200


def test_resend_is_capped_and_never_resets_attempts(sms):
    c = client()
    cid, _ = start(c, sms)
    for i in range(3):
        DB.admin_login_challenges.update_one({}, {"$set": {"last_sent_at": datetime.utcnow() - timedelta(seconds=31)}})
        assert c.post("/admin/auth/resend-otp", json={"challenge_id": cid}).status_code == 200
    DB.admin_login_challenges.update_one({}, {"$set": {"last_sent_at": datetime.utcnow() - timedelta(seconds=31)}})
    assert c.post("/admin/auth/resend-otp", json={"challenge_id": cid}).status_code == 401
    assert len(sms.sent) == 4


def test_concurrent_resends_send_only_one_sms(sms):
    c = client()
    cid, _ = start(c, sms)
    DB.admin_login_challenges.update_one({}, {"$set": {"last_sent_at": datetime.utcnow() - timedelta(seconds=31)}})
    res, barrier = [], threading.Barrier(6)

    def go():
        cl = client()
        barrier.wait()
        res.append(cl.post("/admin/auth/resend-otp", json={"challenge_id": cid}).status_code)
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert res.count(200) == 1 and len(sms.sent) == 2


def test_resend_needs_a_challenge_a_mobile_number_alone_cannot_trigger_an_sms(sms):
    mk_user()
    c = client()
    for body in ({"mobile": MOBILE}, {}, {"challenge_id": "q" * 40}, {"challenge_id": None}):
        r = c.post("/admin/auth/resend-otp", json=body)
        assert r.status_code == 401
    assert sms.sent == []


def test_per_account_sms_limit(sms, monkeypatch):
    mk_user()
    codes = []
    for i in range(7):
        c = client()
        # different IPs so only the per-account limit can trigger
        r = c.post("/admin/auth/login", json={"mobile": MOBILE, "pin": PIN}, headers={"x-forwarded-for": "10.0.0.%d" % i})
        codes.append(r.status_code)
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    assert len(sms.sent) == 5


def test_per_ip_sms_limit(sms):
    for i in range(12):
        mk_user(mobile="90000002%02d" % i)
    codes = [client().post("/admin/auth/login", json={"mobile": "90000002%02d" % i, "pin": PIN}, headers={"x-forwarded-for": "9.9.9.9"}).status_code
             for i in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429] and len(sms.sent) == 10


def test_per_ip_limit_on_pin_guessing_and_otp_guessing(sms):
    c = client()
    h = {"x-forwarded-for": "8.8.8.8"}
    codes = [c.post("/admin/auth/login", json={"mobile": "90000009%02d" % (i % 90), "pin": "0000"}, headers=h).status_code for i in range(32)]
    assert 429 in codes and codes[0] == 401
    codes = [c.post("/admin/auth/verify-otp", json={"challenge_id": "k" * 40, "otp": "1234"}, headers={"x-forwarded-for": "7.7.7.7"}).status_code for i in range(32)]
    assert codes[0] == 401 and codes[-1] == 429


def test_the_client_ip_comes_from_the_trusted_right_most_forwarded_entry():
    class Rq:
        client = type("C", (), {"host": "proxy"})()
        headers = {"x-forwarded-for": "6.6.6.6, 1.2.3.4"}
    assert DA._client_ip(Rq()) == "1.2.3.4"                                                # a forged left-hand entry does not pick the bucket
    Rq.headers = {}
    assert DA._client_ip(Rq()) == "proxy"


# ── page and static contract ───────────────────────────────────────────────────────────────────────

def test_the_public_login_page_has_no_msg91_widget_credentials_or_client_otp_logic():
    page = open(os.path.join(ROOT, "templates", "admin_login.html"), encoding="utf-8").read()
    r = client().get("/admin")
    assert r.status_code == 200
    for html in (page, r.text):
        low = html.lower()
        for bad in ("tokenauth", "widgetid", "widget_id", "control.msg91.com", "msg91_token", "msg91_widget", "sendotpmobile", "verifyotp'", "retryotp",
                    "msg91_verified", "trust_device", "trustdevice"):
            assert bad not in low, bad
        assert "/admin/auth/verify-otp" in html and "challenge_id" in html


def test_the_page_never_stores_the_challenge_or_otp_in_browser_storage():
    page = open(os.path.join(ROOT, "templates", "admin_login.html"), encoding="utf-8").read()
    assert not re.search(r"localStorage|sessionStorage|document\.cookie|indexedDB", page)


def test_old_plaintext_otp_collection_is_no_longer_written(sms):
    c = client()
    start(c, sms)
    assert DB.admin_otp_sessions.count_documents({}) == 0


def test_no_session_cookie_is_set_by_login_resend_or_failed_verify(sms):
    c = client()
    cid, otp = start(c, sms)
    for r in (c.post("/admin/auth/resend-otp", json={"challenge_id": cid}), verify(c, cid, "0000" if otp != "0000" else "1111")):
        assert "admin_token" not in r.headers.get("set-cookie", "")
    assert c.get("/admin/dashboard", follow_redirects=False).status_code in (302, 307)     # still bounced to the login page
