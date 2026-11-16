"""services/admin_login_otp.py - server-side 4-digit SMS OTP challenges for admin login.

The browser never decides whether an OTP is correct. Flow (see docs/ADMIN_LOGIN_2FA.md):
  1. After the PIN is verified the server calls start_challenge(): it generates the OTP with a CSPRNG, stores ONLY a salted hash,
     sends the SMS through the existing MSG91 integration (routers.otp_service.send_sms_otp) and returns an opaque challenge token.
  2. verify_challenge() reserves an attempt atomically, compares the hash, then consumes the challenge atomically; only the single
     winner of that consume may create a session.
  3. resend_challenge() issues a NEW OTP for the same challenge (cooldown, per-challenge cap, shared attempt budget).

Never logged or returned: the OTP, its hash, the challenge token, the full mobile number. Unknown accounts get a "dummy" challenge that
behaves identically (same shapes, timing class, limits) so the endpoints do not reveal whether a mobile number is registered.
No collection read here is used for anything but admin login.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from database import db
from services import admin_otp_test_mode as TM

OTP_LENGTH = 4
OTP_EXPIRY_MINUTES = 5            # per issued OTP
OTP_RESEND_SECONDS = 30           # cooldown between sends for one challenge
OTP_MAX_ATTEMPTS = 5              # wrong codes per challenge (cumulative across resends)
CHALLENGE_MAX_RESENDS = 3         # extra sends per challenge
CHALLENGE_MAX_AGE_MINUTES = 15    # absolute lifetime of one challenge, resends included
SEND_LIMIT_PER_ACCOUNT_HOUR = 10  # SMS sends (first + resends) per mobile number that were NOT followed by a successful sign-in (see throttle_release)
SEND_LIMIT_PER_IP_HOUR = 20       # same, per client IP
IP_LOGIN_LIMIT = 200              # coarse cap on ALL PIN-stage requests per IP per 15 minutes (flood protection only; failures have their own, tighter limits in admin_login_limits)
IP_VERIFY_LIMIT = 200             # coarse cap on ALL OTP-verify / resend requests per IP per 15 minutes
PBKDF2_ITERATIONS = 100_000

CHALLENGES = "admin_login_challenges"
THROTTLE = "admin_auth_throttle"

# A fixed hash used only to burn the same CPU time for unknown accounts as for real ones.
_DUMMY_SALT = bytes.fromhex("00" * 16)


def _now():
    return datetime.utcnow()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mobile_key(mobile: str) -> str:
    return _sha("mobile:" + str(mobile))


def mask_mobile(mobile: str) -> str:
    digits = "".join(c for c in str(mobile) if c.isdigit())
    return ("*" * 6 + digits[-4:]) if len(digits) >= 4 else "****"


def generate_otp() -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(OTP_LENGTH))


def hash_otp(otp: str, salt_hex: str, challenge_id, user_id) -> str:
    """Salted PBKDF2 bound to this challenge and this account: a hash from one challenge/user cannot validate under another."""
    msg = ("%s|%s|%s" % (str(challenge_id), str(user_id), otp)).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", msg, bytes.fromhex(salt_hex), PBKDF2_ITERATIONS).hex()


def burn_pin_time():
    """Equalise timing when the account does not exist."""
    hashlib.pbkdf2_hmac("sha256", b"0000", _DUMMY_SALT, PBKDF2_ITERATIONS)


def log_test_mode_status():
    active, reason = TM.status()
    print("[2FA] admin OTP test mode: " + ("ACTIVE (Staging identity verified; allow-listed admins get the fixed code, no SMS)" if active else "INACTIVE (" + reason + ")"))


def ensure_indexes():
    log_test_mode_status()
    try:
        db[CHALLENGES].create_index("token_hash", unique=True)
        db[CHALLENGES].create_index("purge_at", expireAfterSeconds=0)
        db[THROTTLE].create_index([("kind", 1), ("key", 1), ("at", -1)])
        db[THROTTLE].create_index("purge_at", expireAfterSeconds=0)
    except Exception as e:                                       # never block startup
        print("[2FA] index note: " + type(e).__name__)


# ── throttling (shared by the PIN stage, the OTP sends and the OTP verifies) ─────────────────────────────
def throttle_hit(kind: str, key: str, window_minutes: int, ref: str = None, group: str = None) -> int:
    """Record one event and return how many events (including this one) this key has in the window. Insert-then-count, so
    concurrent requests cannot all slip under a check-then-insert race. `ref` ties an event to one challenge (released when that
    sign-in succeeds); `group` lets an admin reset clear every counter of one mobile number."""
    now = _now()
    doc = {"kind": kind, "key": key, "at": now, "purge_at": now + timedelta(minutes=window_minutes + 5)}
    if ref:
        doc["ref"] = ref
    if group:
        doc["group"] = group
    db[THROTTLE].insert_one(doc)
    return db[THROTTLE].count_documents({"kind": kind, "key": key, "at": {"$gte": now - timedelta(minutes=window_minutes)}})


def throttle_count(kind: str, key: str, window_minutes: int) -> int:
    return db[THROTTLE].count_documents({"kind": kind, "key": key, "at": {"$gte": _now() - timedelta(minutes=window_minutes)}})


def throttle_clear(kind: str, key: str):
    db[THROTTLE].delete_many({"kind": kind, "key": key})


def throttle_release(ref: str):
    """A sign-in that completed (correct OTP) gives its own SMS sends back: successful logins must never use up the budget that exists
    to stop SMS spam and guessing. Only the events tied to that one challenge are removed."""
    if ref:
        db[THROTTLE].delete_many({"ref": ref})


def _send_limited(mkey: str, ip: str, ref: str = None) -> bool:
    """True if a new SMS may be sent now (and records the send, tied to `ref` so a completed sign-in can release it)."""
    acct = throttle_hit("otp_send_acct", mkey, 60, ref=ref, group=mkey)
    ipn = throttle_hit("otp_send_ip", ip, 60, ref=ref)
    return acct <= SEND_LIMIT_PER_ACCOUNT_HOUR and ipn <= SEND_LIMIT_PER_IP_HOUR


# ── SMS transport (the existing MSG91 integration; replaced by a mock in tests) ──────────────────────────
def _send_sms(mobile: str, otp: str, template_id: str) -> dict:
    from routers import otp_service
    return otp_service.send_sms_otp(mobile, otp, OTP_EXPIRY_MINUTES, template_id)


# ── challenge lifecycle ─────────────────────────────────────────────────────────────────────────────────
def _new_token() -> str:
    return secrets.token_urlsafe(32)


def start_challenge(user, mobile: str, ip: str, user_agent: str, template_id: str = "", remember: bool = False) -> dict:
    """Create a challenge and send the first OTP. `user` is the dashboard user document, or None for an unknown mobile number
    (dummy challenge: nothing is sent, the response is identical). Returns:
        {"ok": True, "token", "expires_in", "resend_in", "mobile_masked"}  or
        {"ok": False, "reason": "rate_limited" | "send_failed"}"""
    mkey = mobile_key(mobile)
    now = _now()
    token = _new_token()
    salt = secrets.token_hex(16)
    token_hash = _sha(token)
    # Staging-only test OTP (services/admin_otp_test_mode.py): only when the Staging identity is verified AND this admin is allow-listed.
    test = bool(user) and TM.applies_to(mobile)
    # The SMS budget exists to stop SMS spam and cost, so it only counts real sends (test mode sends none).
    if not test and not _send_limited(mkey, ip, ref=token_hash):
        return {"ok": False, "reason": "rate_limited"}
    otp = TM.TEST_OTP if test else generate_otp()
    doc = {
        "token_hash": token_hash,
        "test_mode": test,
        "remember": bool(remember),            # the user asked to trust this browser; honoured ONLY after the OTP is verified
        "user_id": user["_id"] if user else None,
        "dummy": user is None,
        "mobile_key": mkey,
        "salt": salt,
        "status": "pending",
        "attempts": 0,
        "resend_count": 0,
        "created_at": now,
        "last_sent_at": now,
        "expires_at": now + timedelta(minutes=OTP_EXPIRY_MINUTES),
        "max_expires_at": now + timedelta(minutes=CHALLENGE_MAX_AGE_MINUTES),
        "purge_at": now + timedelta(minutes=CHALLENGE_MAX_AGE_MINUTES + 30),
        "ip": ip,
        "user_agent": (user_agent or "")[:200],
    }
    res = db[CHALLENGES].insert_one(doc)
    doc["otp_hash"] = hash_otp(otp, salt, res.inserted_id, doc["user_id"])
    db[CHALLENGES].update_one({"_id": res.inserted_id}, {"$set": {"otp_hash": doc["otp_hash"]}})
    # An older pending challenge for the same account can no longer be completed once a newer login started.
    if user:
        db[CHALLENGES].update_many({"user_id": user["_id"], "status": "pending", "_id": {"$ne": res.inserted_id}},
                                   {"$set": {"status": "superseded"}})
        sent = {"ok": True} if test else _send_sms(mobile, otp, template_id)         # test mode: no SMS (fixed Staging code)
        if not sent.get("ok"):
            db[CHALLENGES].update_one({"_id": res.inserted_id}, {"$set": {"status": "send_failed"}})
            return {"ok": False, "reason": "send_failed"}
    otp = None
    return {"ok": True, "token": token, "expires_in": OTP_EXPIRY_MINUTES * 60, "resend_in": OTP_RESEND_SECONDS,
            "mobile_masked": mask_mobile(mobile)}


def _find(token: str):
    if not isinstance(token, str) or not (16 <= len(token) <= 128):
        return None
    return db[CHALLENGES].find_one({"token_hash": _sha(token)})


FAIL = {"ok": False, "reason": "invalid"}        # wrong code, expired, used, unknown, locked: ONE shape so nothing leaks


def verify_challenge(token: str, otp: str) -> dict:
    """Returns {"ok": True, "user_id"} for exactly one caller per challenge, else {"ok": False, "reason", "remaining"?}.
    Atomic steps: (1) reserve an attempt (cap holds under concurrency), (2) compare hash, (3) consume (single winner)."""
    if not (isinstance(otp, str) and len(otp) == OTP_LENGTH and otp.isdigit()):
        return dict(FAIL)
    doc = _find(token)
    if not doc or (doc.get("test_mode") and not TM.is_active()):       # a test-mode challenge is unusable once test mode stops verifying
        burn_pin_time()
        return dict(FAIL)
    now = _now()
    reserved = db[CHALLENGES].find_one_and_update(
        {"_id": doc["_id"], "status": "pending", "expires_at": {"$gt": now}, "max_expires_at": {"$gt": now},
         "attempts": {"$lt": OTP_MAX_ATTEMPTS}},
        {"$inc": {"attempts": 1}}, return_document=True)
    if not reserved:
        burn_pin_time()
        return dict(FAIL)
    candidate = hash_otp(otp, reserved["salt"], reserved["_id"], reserved["user_id"])
    good = (not reserved.get("dummy")) and hmac.compare_digest(candidate, reserved.get("otp_hash", ""))
    if not good:
        if not reserved.get("dummy") and reserved.get("user_id") is not None:
            throttle_hit("otp_fail_acct", str(reserved["user_id"]), 60, group=reserved.get("mobile_key"))     # wrong codes per account, across challenges
        remaining = max(0, OTP_MAX_ATTEMPTS - reserved["attempts"])
        if remaining == 0:
            db[CHALLENGES].update_one({"_id": reserved["_id"], "status": "pending"}, {"$set": {"status": "locked"}})
        return {"ok": False, "reason": "invalid", "remaining": remaining}
    won = db[CHALLENGES].find_one_and_update(
        {"_id": reserved["_id"], "status": "pending", "expires_at": {"$gt": _now()}},
        {"$set": {"status": "verified", "verified_at": _now(), "otp_hash": None}}, return_document=True)
    if not won:                                                  # lost a concurrent race, or it expired between the steps
        return dict(FAIL)
    throttle_release(won.get("token_hash"))                     # a completed sign-in does not consume the SMS budget
    return {"ok": True, "user_id": won["user_id"], "remember": bool(won.get("remember"))}


def resend_challenge(token: str, mobile_lookup, ip: str, template_id: str = "") -> dict:
    """Issue a new OTP for a pending challenge. `mobile_lookup(user_id)` returns the account's mobile number (kept out of the
    challenge document). Returns {"ok": True, "expires_in", "resend_in"} or {"ok": False, "reason": "invalid"|"cooldown"|
    "rate_limited"|"send_failed", "retry_in"?}."""
    doc = _find(token)
    now = _now()
    if not doc or doc.get("status") != "pending":
        return dict(FAIL)
    if doc["max_expires_at"] <= now or doc.get("resend_count", 0) >= CHALLENGE_MAX_RESENDS or doc.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
        return dict(FAIL)
    test = bool(doc.get("test_mode"))
    if test and not TM.is_active():
        return dict(FAIL)
    since = (now - doc["last_sent_at"]).total_seconds()
    if since < OTP_RESEND_SECONDS:
        return {"ok": False, "reason": "cooldown", "retry_in": int(OTP_RESEND_SECONDS - since) + 1}
    if not test and not _send_limited(doc["mobile_key"], ip, ref=doc["token_hash"]):
        return {"ok": False, "reason": "rate_limited"}
    salt = secrets.token_hex(16)
    otp = TM.TEST_OTP if test else generate_otp()
    # Claim the resend atomically: two concurrent resends cannot both pass the cooldown / cap.
    claimed = db[CHALLENGES].find_one_and_update(
        {"_id": doc["_id"], "status": "pending", "last_sent_at": {"$lte": now - timedelta(seconds=OTP_RESEND_SECONDS)},
         "resend_count": doc.get("resend_count", 0), "max_expires_at": {"$gt": now}, "attempts": {"$lt": OTP_MAX_ATTEMPTS}},
        {"$set": {"salt": salt, "otp_hash": hash_otp(otp, salt, doc["_id"], doc["user_id"]), "last_sent_at": now,
                  "expires_at": now + timedelta(minutes=OTP_EXPIRY_MINUTES)},
         "$inc": {"resend_count": 1}}, return_document=True)
    if not claimed:
        return {"ok": False, "reason": "cooldown", "retry_in": OTP_RESEND_SECONDS}
    if not claimed.get("dummy") and not test:
        mobile = mobile_lookup(claimed["user_id"])
        sent = _send_sms(mobile, otp, template_id) if mobile else {"ok": False}
        if not sent.get("ok"):
            return {"ok": False, "reason": "send_failed"}
    otp = None
    return {"ok": True, "expires_in": OTP_EXPIRY_MINUTES * 60, "resend_in": OTP_RESEND_SECONDS}
