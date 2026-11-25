"""services/login_proof.py - proof that the person logging in really completed the OTP for THAT phone number (Phase 2, D2).

PROBLEM (audit D2): the apps verify the OTP on the device with the MSG91 widget SDK and then call POST /user/account-login or
POST /merchant/login with only the phone number. The server never learned that an OTP had been verified, so anyone who knew a registered
number could call those endpoints directly and receive that account's session token.

WHAT THIS CHANGES: nothing about how the OTP is sent (WhatsApp / SMS through the MSG91 widget) or entered. After the widget says
"verified" the app also hands the widget's ACCESS TOKEN to the backend. The backend does not trust the app: it asks MSG91
(services.msg91_widget.verify_access_token, the same call the admin login already uses) whether the token is genuine and which number it
verified, and only then issues the session. A session token is issued only for ONE of:

  1. OTP proof       {"otp_proof": {"kind": "msg91", "token": "<widget access token>"}}  (a bare string is accepted too)
       * MSG91 must confirm the token (anything unclear = refused),
       * MSG91 must NAME the verified number and it must be the number being logged in (no number named = refused, fail closed),
       * the token must be fresh (token claims `exp`/`iat` when present, else MSG91's own expiry; and never older than MAX_AGE_SECONDS),
       * it is SINGLE USE: sha256(token) is stored with a unique key (collection login_proofs_used, TTL 24 h) before the session is issued,
         so a replay - even of a valid token - fails.
  2. An existing session of the SAME account (Authorization: Bearer <token> or the user/merchant cookie). Used by "switch mode" and session
     restore in the app, which previously re-logged in with the phone alone. It proves nothing new and grants nothing the caller does not
     already hold.
  3. Staging test code (see staging_test_ok): ONLY on the verified Staging service, ONLY when explicitly switched on and the number is listed.
     This replaces the app's old client-side "master OTP", which the server could never see.

Anything else - in particular a request with just a phone number, which is exactly what an app from before this fix sends - is refused with
HTTP 426 ("update the app"). There is no switch that turns the check off and no fallback to phone-only login.

Nothing here logs a token, a code, a number or the MSG91 key. Callers get short fixed messages.
"""
import hashlib
import hmac
import os
import time
from datetime import datetime
from typing import Optional

from fastapi import HTTPException, Request

STAGING_CODE = "1234"      # the code the app's staging-only test path sends; honoured only when staging_test_status() is active

COLLECTION = "login_proofs_used"
MAX_AGE_SECONDS = 600                     # a widget access token older than this is never accepted, whatever MSG91 says
TTL_SECONDS = 24 * 3600                   # how long a used-token marker is kept (>> MAX_AGE_SECONDS)

UPGRADE_MESSAGE = "Please update the OffrO app to the latest version to log in."
VERIFY_FAILED = "We could not verify your code. Please request a new code and try again."
MISMATCH = "This verification does not match this phone number."
UNAVAILABLE = "Verification is temporarily unavailable. Please try again in a moment."
USED = "This verification has already been used. Please request a new code."


class _LiveDb:
    def __getattr__(self, name):
        import database
        return getattr(database.db, name)

    def __getitem__(self, name):
        import database
        return database.db[name]


db = _LiveDb()


def ensure_indexes():
    db[COLLECTION].create_index([("at", 1)], name="login_proofs_used_ttl", expireAfterSeconds=TTL_SECONDS)


def ensure_indexes_safe() -> bool:
    try:
        ensure_indexes()
        return True
    except Exception as e:
        print("[LOGIN-PROOF] index creation skipped (%s)" % type(e).__name__)
        return False


# ── helpers ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _digits(value) -> str:
    return "".join(c for c in str(value or "") if c.isdigit())


def candidate_numbers(variants) -> set:
    """Every international digit string the stored/typed forms of one number can mean (wa-style: country code + national number, no plus)."""
    out = set()
    for v in variants or []:
        d = _digits(v)
        if len(d) == 10:                               # bare Indian national number
            out.add("91" + d)
        elif len(d) == 11 and d.startswith("0"):      # 0 + Indian national number
            out.add("91" + d[1:])
        elif d:
            out.add(d)
    return out


def _bearer(request: Request) -> str:
    h = request.headers.get("Authorization", "")
    tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
    return tok or request.cookies.get("user_token") or request.cookies.get("merchant_token") or ""


def _session_ok(request: Request, variants) -> bool:
    tok = _bearer(request)
    if not tok:
        return False
    acct = db.accounts.find_one({"token": tok}) or db.users.find_one({"token": tok})
    if not acct:
        return False
    if str(acct.get("status") or "") in ("blocked", "deleted", "delete_requested"):
        return False
    return str(acct.get("phone") or "") in set(variants or [])


# ── staging test code ──────────────────────────────────────────────────────────────────────────────────────────────
def staging_test_status() -> tuple:
    """(active, reason). Active only when ALL hold: the explicit switch is on, services.app_environment recognises this process as the
    Staging service (the exact Railway project + environment + service ids; Production can never match), the public domain is not a
    production domain, and an allow-list of numbers is set."""
    try:
        from services import app_environment as env
        if (os.getenv("LOGIN_OTP_STAGING_TEST_MODE", "") or "").strip().lower() not in ("1", "true", "yes", "on"):
            return False, "flag_off"
        st = env.status()
        if not st.get("staging_ids_match"):
            return False, "not_verified_staging"
        if st.get("classification") != "staging" or st.get("conflict"):
            return False, "environment_mismatch"
        domain = (os.getenv("RAILWAY_PUBLIC_DOMAIN", "") or "").strip().lower()
        if not domain or "production" in domain or domain.endswith("theoffro.com"):
            return False, "domain_mismatch"
        if not _test_numbers():
            return False, "allow_list_empty"
        return True, "ok"
    except Exception:
        return False, "check_error"


def _test_numbers() -> set:
    raw = os.getenv("LOGIN_OTP_TEST_MOBILES", "")
    return {x for x in (_digits(p) for p in raw.split(",")) if x}


def staging_test_ok(code, variants) -> bool:
    if not staging_test_status()[0]:
        return False
    if not isinstance(code, str) or not hmac.compare_digest(code.encode("utf-8"), STAGING_CODE.encode("utf-8")):
        return False
    listed = _test_numbers()
    return bool(candidate_numbers(variants) & listed)


# ── the check ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _token_of(proof) -> Optional[str]:
    if isinstance(proof, str):
        return proof.strip() or None
    if isinstance(proof, dict):
        t = proof.get("token") or proof.get("access_token") or proof.get("access-token")
        return str(t).strip() if t else None
    return None


def _fresh(claims: dict) -> bool:
    now = time.time()
    try:
        exp = claims.get("exp")
        if exp is not None and float(exp) < now:
            return False
        iat = claims.get("iat")
        if iat is not None and now - float(iat) > MAX_AGE_SECONDS:
            return False
    except (TypeError, ValueError):
        return False
    return True


def _consume(token: str) -> bool:
    """Mark the token as used. False = it was already used (replay)."""
    from pymongo.errors import DuplicateKeyError
    try:
        db[COLLECTION].insert_one({"_id": hashlib.sha256(token.encode("utf-8")).hexdigest(), "at": datetime.utcnow()})
        return True
    except DuplicateKeyError:
        return False


def require_login_proof(request: Request, data: dict, variants) -> str:
    """Raise HTTPException unless this login is proven. Returns how it was proven: 'session' | 'otp' | 'staging_test'."""
    proof = (data or {}).get("otp_proof")
    if proof is None:
        if _session_ok(request, variants):
            return "session"
        raise HTTPException(status_code=426, detail=UPGRADE_MESSAGE)

    if isinstance(proof, dict) and str(proof.get("kind") or "").lower() == "staging_test":
        if staging_test_ok(proof.get("code"), variants):
            return "staging_test"
        raise HTTPException(status_code=401, detail=VERIFY_FAILED)

    token = _token_of(proof)
    if not token:
        raise HTTPException(status_code=401, detail=VERIFY_FAILED)
    from services import msg91_widget
    res = msg91_widget.verify_access_token(token)
    if not res.get("ok"):
        if res.get("error") == "unavailable":
            raise HTTPException(status_code=503, detail=UNAVAILABLE)
        raise HTTPException(status_code=401, detail=VERIFY_FAILED)
    ids = [_digits(i) for i in (res.get("identifiers") or ([res["identifier"]] if res.get("identifier") else []))]
    ids = [i for i in ids if i]
    if not ids:
        # MSG91 confirmed the token but did not say which number it verified: it cannot be tied to this phone, so it is NOT accepted.
        print("[LOGIN-PROOF] token confirmed by MSG91 but no verified number was returned - refusing (fail closed)")
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    wanted = candidate_numbers(variants)
    if not wanted or any(i not in wanted for i in ids):
        raise HTTPException(status_code=403, detail=MISMATCH)
    if not _fresh(res.get("claims") or {}):
        raise HTTPException(status_code=401, detail=VERIFY_FAILED)
    if not _consume(token):
        raise HTTPException(status_code=401, detail=USED)
    return "otp"
