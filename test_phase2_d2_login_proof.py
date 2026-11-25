"""NOTE: file name sorts AFTER test_banner_media.py on purpose (it swaps the shared fake `database` at import).
Phase 2 / D2: POST /user/account-login and POST /merchant/login need verifiable OTP proof; a phone number alone never yields a token.
MSG91 is replaced by a fake `verify_access_token` (no network, no real OTP, no credentials). What these tests do NOT prove: that the real
MSG91 widget access token names the verified number - that must be checked on staging (see the report).
Run:  python -m pytest test_phase2_d2_login_proof.py -q -W ignore"""
import sys
import time

import pytest
from fastapi.testclient import TestClient

import server
from services import login_proof as LP
from services import msg91_widget

fake = sys.modules["database"].db
c = TestClient(server.app)

IN = "9876543210"
SA = "+966501234567"
NP = "+9779812345678"
JO = "+962791234567"
PH = "+639171234567"


class MSG91:
    """Fake MSG91: token string -> what verifyAccessToken would answer."""
    def __init__(self):
        self.tokens, self.calls = {}, 0

    def issue(self, token, number_digits, **claims):
        self.tokens[token] = {"ok": True, "identifier": number_digits, "identifiers": [number_digits] if number_digits else [], "claims": claims}
        return token

    def __call__(self, token):
        self.calls += 1
        return self.tokens.get(token, {"ok": False, "error": "invalid"})


@pytest.fixture
def m91(monkeypatch):
    for col in ("accounts", "users", "merchants", "login_proofs_used", "wa_contacts"):
        fake[col].delete_many({})
    f = MSG91()
    monkeypatch.setattr(msg91_widget, "verify_access_token", f)
    for v in ("LOGIN_OTP_STAGING_TEST_MODE", "LOGIN_OTP_TEST_MOBILES", "RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID",
              "RAILWAY_PUBLIC_DOMAIN", "MONGO_URL"):
        monkeypatch.delenv(v, raising=False)
    return f


def _acct(phone, roles=("user",), **kw):
    d = {"phone": phone, "name": "N", "roles": list(roles), "status": "active", **kw}
    if "merchant" in roles:
        d["merchant_id"] = "m1"
    fake.accounts.insert_one(d)
    return d


def _proof(token):
    return {"kind": "msg91", "token": token}


def _user(phone, token, **extra):
    return c.post("/user/account-login", json={"phone": phone, "otp_proof": _proof(token), **extra})


def _merch(phone, token, **extra):
    return c.post("/merchant/login", json={"phone": phone, "otp_proof": _proof(token), **extra})


LONG = "x" * 40


# ── the four journeys the apps use (valid proof) ───────────────────────────────────────────────────────────────────
def test_indian_customer_login_with_valid_proof(m91):
    _acct(IN)
    r = _user(IN, m91.issue(LONG + "1", "91" + IN))
    assert r.status_code == 200 and r.json()["token"] and r.json()["phone"] == IN and r.json()["roles"] == ["user"]
    assert fake.accounts.find_one({"phone": IN})["token"] == r.json()["token"]


def test_indian_customer_all_stored_formats_and_plus_form(m91):
    for i, stored in enumerate(["+919876543210", "919876543210", "9876543210", "09876543210"]):
        fake.accounts.delete_many({})
        _acct(stored)
        assert _user("+919876543210", m91.issue(LONG + "f%d" % i, "919876543210"), country="IN").status_code == 200


@pytest.mark.parametrize("phone,iso,digits", [(SA, "SA", "966501234567"), (NP, "NP", "9779812345678"), (JO, "JO", "962791234567"), (PH, "PH", "639171234567")])
def test_international_customer_login_with_valid_proof(m91, phone, iso, digits):
    _acct(phone, phone_country=iso)
    r = _user(phone, m91.issue(LONG + iso, digits), country=iso)
    assert r.status_code == 200 and r.json()["phone"] == phone and r.json()["token"]


def test_indian_merchant_login_with_valid_proof(m91):
    _acct(IN, roles=("merchant",))
    r = _merch(IN, m91.issue(LONG + "m1", "91" + IN))
    assert r.status_code == 200 and r.json()["merchant_id"] == "m1" and r.json()["token"]
    assert "merchant_token" in r.headers.get("set-cookie", "")


def test_international_merchant_login_with_valid_proof(m91):
    _acct(SA, roles=("merchant",), phone_country="SA")
    r = _merch(SA, m91.issue(LONG + "m2", "966501234567"), country="SA")
    assert r.status_code == 200 and r.json()["phone"] == SA


def test_unified_login_for_a_customer_and_merchant_account(m91):
    _acct(SA, roles=("user", "merchant"), phone_country="SA")
    r = _user(SA, m91.issue(LONG + "b", "966501234567"), country="SA")
    assert r.status_code == 200 and r.json()["is_merchant"] is True and r.json()["role"] == "both"


# ── refusals ───────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/user/account-login", "/merchant/login"])
def test_direct_call_with_only_a_phone_is_refused_upgrade_required(m91, path):
    _acct(IN, roles=("user", "merchant"))
    r = c.post(path, json={"phone": IN})
    assert r.status_code == 426 and "update" in r.json()["detail"].lower()
    assert "token" not in r.json()
    assert fake.accounts.find_one({"phone": IN}).get("token") in (None, "")          # nothing was issued or rotated
    assert m91.calls == 0


@pytest.mark.parametrize("bad", [{}, "", "   ", {"kind": "msg91"}, {"kind": "msg91", "token": ""}, 123, [], {"token": None}])
def test_empty_or_malformed_proof_is_refused(m91, bad):
    _acct(IN)
    r = c.post("/user/account-login", json={"phone": IN, "otp_proof": bad})
    assert r.status_code in (401, 426) and "token" not in r.json()
    assert fake.accounts.find_one({"phone": IN}).get("token") in (None, "")


@pytest.mark.parametrize("path,fn", [("user", _user), ("merchant", _merch)])
def test_incorrect_otp_unknown_or_rejected_token(m91, path, fn):
    _acct(IN, roles=("user", "merchant"))
    r = fn(IN, "token-that-msg91-never-issued-0000000000")
    assert r.status_code == 401 and "token" not in r.json()
    assert fake.accounts.find_one({"phone": IN}).get("token") in (None, "")
    assert fake.login_proofs_used.count_documents({}) == 0                           # a rejected token is not consumed


def test_phone_number_mismatch_is_refused(m91):
    _acct(IN); _acct("9123456789")
    tok = m91.issue(LONG + "mm", "91" + "9123456789")                                # proof is for the OTHER number
    r = _user(IN, tok)
    assert r.status_code == 403 and "does not match" in r.json()["detail"]
    assert fake.accounts.find_one({"phone": IN}).get("token") in (None, "")
    assert fake.accounts.find_one({"phone": "9123456789"}).get("token") in (None, "")


def test_proof_for_an_indian_number_cannot_log_into_an_international_account_with_the_same_digits(m91):
    _acct(SA, phone_country="SA")
    r = _user(SA, m91.issue(LONG + "x1", "91" + "501234567"), country="SA")
    assert r.status_code == 403


def test_international_proof_for_another_country_is_refused(m91):
    _acct(NP, phone_country="NP")
    assert _user(NP, m91.issue(LONG + "x2", "966501234567"), country="NP").status_code == 403


def test_token_that_msg91_confirms_but_with_no_number_fails_closed(m91):
    _acct(IN)
    r = _user(IN, m91.issue(LONG + "nn", None))
    assert r.status_code == 503 and fake.accounts.find_one({"phone": IN}).get("token") in (None, "")


def test_msg91_unreachable_is_503_not_a_login(m91, monkeypatch):
    _acct(IN)
    monkeypatch.setattr(msg91_widget, "verify_access_token", lambda t: {"ok": False, "error": "unavailable", "detail": "timeout"})
    assert _user(IN, LONG + "u").status_code == 503
    assert fake.accounts.find_one({"phone": IN}).get("token") in (None, "")


def test_conflicting_numbers_in_one_token_are_refused(m91):
    _acct(IN)
    m91.tokens[LONG + "cf"] = {"ok": True, "identifier": "91" + IN, "identifiers": ["91" + IN, "919123456789"], "claims": {}}
    assert _user(IN, LONG + "cf").status_code == 403


def test_expired_proof_is_refused(m91):
    _acct(IN)
    assert _user(IN, m91.issue(LONG + "e1", "91" + IN, exp=int(time.time()) - 5)).status_code == 401
    assert _user(IN, m91.issue(LONG + "e2", "91" + IN, iat=int(time.time()) - LP.MAX_AGE_SECONDS - 5)).status_code == 401
    assert _user(IN, m91.issue(LONG + "e3", "91" + IN, exp="garbage")).status_code == 401
    assert fake.accounts.find_one({"phone": IN}).get("token") in (None, "")
    assert _user(IN, m91.issue(LONG + "e4", "91" + IN, iat=int(time.time()) - 30, exp=int(time.time()) + 300)).status_code == 200


def test_reused_proof_is_refused_and_second_account_is_not_reachable_with_it(m91):
    _acct(IN)
    tok = m91.issue(LONG + "r1", "91" + IN)
    first = _user(IN, tok)
    assert first.status_code == 200
    second = _user(IN, tok)
    assert second.status_code == 401 and "already been used" in second.json()["detail"]
    assert fake.accounts.find_one({"phone": IN})["token"] == first.json()["token"]
    assert _merch(IN, tok).status_code == 401                                      # also not on the merchant endpoint


def test_the_used_marker_never_contains_the_token(m91):
    _acct(IN)
    tok = m91.issue(LONG + "hash", "91" + IN)
    _user(IN, tok)
    doc = fake.login_proofs_used.find_one({})
    assert tok not in str(doc) and len(doc["_id"]) == 64


def test_unregistered_number_with_valid_proof_is_still_404(m91):
    r = _user(IN, m91.issue(LONG + "nr", "91" + IN))
    assert r.status_code == 404


def test_blocked_and_deleted_accounts_still_refused_with_valid_proof(m91):
    _acct(IN, status="blocked")
    assert _user(IN, m91.issue(LONG + "b1", "91" + IN)).status_code == 403
    fake.accounts.delete_many({})
    _acct(IN, status="deleted")
    assert _user(IN, m91.issue(LONG + "b2", "91" + IN)).status_code == 404


# ── existing session (switch mode / restore) ───────────────────────────────────────────────────────────────────────
def test_switch_mode_with_an_existing_session_of_the_same_account(m91):
    _acct(IN, roles=("user", "merchant"), token="sess-1")
    r = c.post("/user/account-login", json={"phone": IN}, headers={"Authorization": "Bearer sess-1"})
    assert r.status_code == 200 and r.json()["token"] and m91.calls == 0
    r2 = c.post("/merchant/login", json={"phone": IN}, headers={"Authorization": "Bearer " + r.json()["token"]})
    assert r2.status_code == 200


def test_session_of_another_account_or_unknown_or_blocked_is_not_enough(m91):
    _acct(IN, token="sess-in"); _acct(SA, phone_country="SA", token="sess-sa")
    assert c.post("/user/account-login", json={"phone": IN}, headers={"Authorization": "Bearer sess-sa"}).status_code == 426
    assert c.post("/user/account-login", json={"phone": IN}, headers={"Authorization": "Bearer nope"}).status_code == 426
    fake.accounts.update_one({"phone": IN}, {"$set": {"status": "blocked"}})
    assert c.post("/user/account-login", json={"phone": IN}, headers={"Authorization": "Bearer sess-in"}).status_code == 426


def test_a_proof_always_wins_over_a_session(m91):
    _acct(IN, token="sess-in")
    r = c.post("/user/account-login", json={"phone": IN, "otp_proof": _proof("bad-token-00000000000000000000000000")}, headers={"Authorization": "Bearer sess-in"})
    assert r.status_code == 401


# ── staging test code (replaces the app's client-side master OTP) ──────────────────────────────────────────────────
def _staging_env(monkeypatch, numbers="966501234567,919876543210"):
    from services import admin_otp_test_mode as t   # (test file only: the staging ids this process must have)
    monkeypatch.setenv("LOGIN_OTP_STAGING_TEST_MODE", "1")
    monkeypatch.setenv("LOGIN_OTP_TEST_MOBILES", numbers)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", t.STAGING_PROJECT_ID)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", t.STAGING_ENVIRONMENT_ID)
    monkeypatch.setenv("RAILWAY_SERVICE_ID", t.STAGING_SERVICE_ID)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", t.STAGING_PUBLIC_DOMAIN)
    monkeypatch.setenv("MONGO_URL", "mongodb://u:p@mongodb.railway.internal:27017")


def _stg(phone, code="1234", **extra):
    return c.post("/user/account-login", json={"phone": phone, "otp_proof": {"kind": "staging_test", "code": code}, **extra})


def test_staging_test_code_is_off_by_default_everywhere(m91):
    _acct(IN)
    assert _stg(IN).status_code == 401


def test_staging_test_code_works_only_on_verified_staging_for_listed_numbers(m91, monkeypatch):
    _acct(IN); _acct(SA, phone_country="SA"); _acct("9123456789")
    _staging_env(monkeypatch)
    assert _stg(IN).status_code == 200
    assert _stg(SA, country="SA").status_code == 200
    assert _stg(IN, code="0000").status_code == 401
    assert _stg("9123456789").status_code == 401                                   # not on the allow-list


@pytest.mark.parametrize("var,val", [("RAILWAY_PROJECT_ID", "other"), ("RAILWAY_ENVIRONMENT_ID", "other"), ("RAILWAY_SERVICE_ID", "other"),
                                      ("RAILWAY_PUBLIC_DOMAIN", "theoffro.com"), ("RAILWAY_PUBLIC_DOMAIN", "offro-backend-production.up.railway.app"),
                                      ("OFFRO_ENVIRONMENT", "production"), ("LOGIN_OTP_STAGING_TEST_MODE", "0"),
                                      ("LOGIN_OTP_TEST_MOBILES", "")])
def test_staging_test_code_refused_when_any_identity_check_fails(m91, monkeypatch, var, val):
    _acct(IN)
    _staging_env(monkeypatch)
    monkeypatch.setenv(var, val)
    assert _stg(IN).status_code == 401
    assert fake.accounts.find_one({"phone": IN}).get("token") in (None, "")


def test_staging_test_code_is_not_accepted_as_an_msg91_token(m91, monkeypatch):
    _acct(IN)
    _staging_env(monkeypatch)
    assert _user(IN, "1234").status_code == 401


# ── registration and the rest of the flow ──────────────────────────────────────────────────────────────────────────
def test_registration_flow_is_unchanged_and_then_login_needs_proof(m91):
    r = c.post("/user/check-phone", json={"phone": SA, "country": "SA"})
    assert r.json()["registered"] is False
    reg = c.post("/user/register", json={"name": "Sara", "phone": SA, "country": "SA", "city": "Riyadh", "consent": True})
    assert reg.status_code == 200 and "token" not in reg.json()
    assert c.post("/user/check-phone", json={"phone": SA, "country": "SA"}).json()["registered"] is True
    assert c.post("/user/account-login", json={"phone": SA, "country": "SA"}).status_code == 426
    ok = _user(SA, m91.issue(LONG + "reg", "966501234567"), country="SA")
    assert ok.status_code == 200 and ok.json()["name"] == "Sara" and ok.json()["roles"] == ["user"]


def test_merchant_registration_then_login(m91):
    reg = c.post("/merchant/register", json={"name": "Shop", "phone": IN, "city": "Pune", "area": "X"})
    assert reg.status_code == 200 and "token" not in reg.json()
    assert c.post("/merchant/login", json={"phone": IN}).status_code == 426
    assert _merch(IN, m91.issue(LONG + "mr", "91" + IN)).status_code == 200


def test_first_login_still_records_pending_whatsapp_consent(m91):
    c.post("/user/register", json={"name": "A", "phone": IN, "city": "", "consent": True, "whatsapp_consent": True,
                                    "consent_version": __import__("services.wa_consent", fromlist=["x"]).REGISTRATION_CONSENT_VERSION})
    acct = fake.accounts.find_one({})
    assert acct and acct.get("wa_consent_pending")
    assert _user(IN, m91.issue(LONG + "wc", "91" + IN)).status_code == 200
    assert "wa_consent_pending" not in fake.accounts.find_one({})


def test_helpers():
    assert LP.candidate_numbers(["+919876543210", "919876543210", "9876543210", "09876543210"]) == {"919876543210"}
    assert LP.candidate_numbers(["+966501234567", "966501234567"]) == {"966501234567"}
    assert LP.candidate_numbers([]) == set() and LP.candidate_numbers([""]) == set()
