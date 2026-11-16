"""services/admin_trusted_devices.py - "Remember this device" for the admin sign-in (Patch C).

A trusted device lets an administrator skip the SMS OTP (never the PIN) on a browser that already completed PIN + OTP once.

  * The browser holds a random 256-bit token in an HttpOnly, SameSite=Strict cookie scoped to /admin/auth (Secure is added over HTTPS by the
    security middleware). The database stores ONLY the SHA-256 of the token, bound to one admin account. Tokens are never put in URLs or logs.
  * A device is registered ONLY after a successful OTP verification, and only if the user asked for it at the PIN step (the checkbox is a request,
    never proof). A browser-supplied device id is not a thing here: trust = a token whose hash exists for THIS account and has not expired.
  * Default lifetime 30 days (ADMIN_TRUSTED_DEVICE_DAYS; 0 turns the feature off). No sliding extension: after the lifetime OTP is required again.
  * Revoked (all of an account's devices) on PIN change/reset, mobile or role change, deactivation/suspension, and account deletion; also from the
    account's own trusted-device list and on "log out and forget this device".
  * Super Admin: protected by default: Super Admin accounts always need the OTP. Set ADMIN_TRUSTED_DEVICE_SUPER_ADMIN=1 to allow it.
Collection: admin_trusted_devices (the old `trusted_devices` collection held plain tokens and is never read).
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta

from database import db

COLLECTION = "admin_trusted_devices"
COOKIE = "offro_td"
COOKIE_PATH = "/admin/auth"
DEFAULT_DAYS = 30
MAX_DEVICES_PER_USER = 10


def lifetime_days() -> int:
    raw = os.getenv("ADMIN_TRUSTED_DEVICE_DAYS", "").strip()
    try:
        days = int(raw) if raw else DEFAULT_DAYS
    except ValueError:
        days = DEFAULT_DAYS
    return max(0, min(days, 90))


def enabled() -> bool:
    return lifetime_days() > 0


def allowed_for(role_name: str) -> bool:
    """Is a trusted device allowed for an account with this role?"""
    if not enabled():
        return False
    if (role_name or "") == "Super Admin":
        return os.getenv("ADMIN_TRUSTED_DEVICE_SUPER_ADMIN", "").strip().lower() in ("1", "true", "yes", "on")
    return True


def _hash(token: str) -> str:
    return hashlib.sha256(("offro-td|" + token).encode("utf-8")).hexdigest()


def _valid_shape(token) -> bool:
    return isinstance(token, str) and 20 <= len(token) <= 128


def ensure_indexes():
    try:
        db[COLLECTION].create_index("token_hash", unique=True)
        db[COLLECTION].create_index("user_id")
        db[COLLECTION].create_index("purge_at", expireAfterSeconds=0)
    except Exception as e:                                        # never block startup
        print("[2FA] trusted-device index note: " + type(e).__name__)


def register(user_id, ip: str, user_agent: str) -> str:
    """Create a device for this account and return the raw token (shown to the browser once, via the cookie)."""
    now = datetime.utcnow()
    token = secrets.token_urlsafe(32)
    expires = now + timedelta(days=lifetime_days())
    db[COLLECTION].insert_one({
        "token_hash": _hash(token), "user_id": user_id, "created_at": now, "expires_at": expires, "purge_at": expires + timedelta(days=1),
        "last_used_at": None, "ip_address": (ip or "")[:64], "user_agent": (user_agent or "")[:200]})
    extra = db[COLLECTION].count_documents({"user_id": user_id}) - MAX_DEVICES_PER_USER      # keep the list small: drop the oldest beyond the cap
    if extra > 0:
        for old in db[COLLECTION].find({"user_id": user_id}).sort("created_at", 1).limit(extra):
            db[COLLECTION].delete_one({"_id": old["_id"]})
    return token


def find_valid(token, user_id=None):
    """The device record for this raw token if it exists, has not expired and (when given) belongs to this account; else None."""
    if not _valid_shape(token):
        return None
    q = {"token_hash": _hash(token), "expires_at": {"$gt": datetime.utcnow()}}
    if user_id is not None:
        q["user_id"] = user_id
    return db[COLLECTION].find_one(q)


def touch(device_id):
    db[COLLECTION].update_one({"_id": device_id}, {"$set": {"last_used_at": datetime.utcnow()}})


def revoke_token(token) -> int:
    if not _valid_shape(token):
        return 0
    return db[COLLECTION].delete_many({"token_hash": _hash(token)}).deleted_count


def revoke_all(user_id) -> int:
    return db[COLLECTION].delete_many({"user_id": user_id}).deleted_count
