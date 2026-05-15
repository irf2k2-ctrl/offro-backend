"""
otp_service.py — MSG91 OTP integration for OFFRO
Place this file alongside users.py in the routers/ directory.

Environment variables required (set in Railway):
  MSG91_AUTH_KEY      — your MSG91 auth key
  MSG91_TEMPLATE_ID   — DLT-approved OTP template ID
  MSG91_SENDER_ID     — 6-char sender ID (e.g. OFFROO)

Optional:
  OTP_EXPIRY_MINUTES  — default 10
  OTP_MAX_ATTEMPTS    — default 5 (wrong attempts before lockout)
  OTP_RESEND_SECONDS  — default 30 (cooldown between resends)
  OTP_RATE_LIMIT_HOUR — default 5  (max OTPs per phone per hour)
"""

import os, random, string, logging, time
from datetime import datetime, timedelta

import httpx
from database import db

logger = logging.getLogger("otp_service")

# ── Config ────────────────────────────────────────────────────────────────────
MSG91_AUTH_KEY    = os.environ.get("MSG91_AUTH_KEY", "")
MSG91_TEMPLATE_ID = os.environ.get("MSG91_TEMPLATE_ID", "")
MSG91_SENDER_ID   = os.environ.get("MSG91_SENDER_ID", "OFFROO")

OTP_EXPIRY_MINUTES  = int(os.environ.get("OTP_EXPIRY_MINUTES",  "10"))
OTP_MAX_ATTEMPTS    = int(os.environ.get("OTP_MAX_ATTEMPTS",     "5"))
OTP_RESEND_SECONDS  = int(os.environ.get("OTP_RESEND_SECONDS",  "30"))
OTP_RATE_LIMIT_HOUR = int(os.environ.get("OTP_RATE_LIMIT_HOUR",  "5"))

# MSG91 OTP send endpoint
MSG91_URL = "https://control.msg91.com/api/v5/otp"

# ── Helpers ───────────────────────────────────────────────────────────────────
def _gen_otp(length: int = 4) -> str:
    return "".join(random.choices(string.digits, k=length))

def _e164(phone: str) -> str:
    """Strip + for MSG91 — it expects 91XXXXXXXXXX format."""
    p = str(phone).strip().replace(" ", "").replace("-", "")
    if p.startswith("+"):
        p = p[1:]
    if not p.startswith("91") and len(p) == 10:
        p = "91" + p
    return p

# ── OTP Collection helpers ────────────────────────────────────────────────────
def _ensure_otp_indexes():
    """Call once at startup. Creates TTL index so MongoDB auto-purges expired OTPs."""
    try:
        db.otp_sessions.create_index(
            "expires_at",
            expireAfterSeconds=0,
            name="otp_ttl",
            background=True,
        )
        db.otp_sessions.create_index(
            [("phone", 1)],
            name="otp_phone",
            background=True,
        )
        logger.info("OTP indexes ensured")
    except Exception as e:
        logger.warning(f"OTP index warning: {e}")

# ── Rate limiting ─────────────────────────────────────────────────────────────
def _check_rate_limit(phone: str) -> tuple[bool, str]:
    """
    Returns (allowed: bool, reason: str).
    Two checks:
      1. Resend cooldown — must wait OTP_RESEND_SECONDS since last send
      2. Hourly cap — max OTP_RATE_LIMIT_HOUR sends per hour
    """
    now = datetime.utcnow()

    # Most recent OTP for this phone
    latest = db.otp_sessions.find_one(
        {"phone": phone},
        sort=[("created_at", -1)],
    )

    if latest:
        # 1. Resend cooldown
        since_last = (now - latest["created_at"]).total_seconds()
        if since_last < OTP_RESEND_SECONDS:
            wait = int(OTP_RESEND_SECONDS - since_last)
            return False, f"Please wait {wait} seconds before requesting another OTP."

        # 2. Hourly cap
        hour_ago = now - timedelta(hours=1)
        count_last_hour = db.otp_sessions.count_documents({
            "phone": phone,
            "created_at": {"$gte": hour_ago},
        })
        if count_last_hour >= OTP_RATE_LIMIT_HOUR:
            return False, "Too many OTP requests. Please try again after an hour."

    return True, ""

# ── Send OTP via MSG91 ────────────────────────────────────────────────────────
def send_otp(phone: str) -> dict:
    """
    Generates OTP, stores in MongoDB, sends via MSG91.

    Returns:
      {"ok": True,  "dev_otp": "XXXX" (only if MSG91 not configured)}
      {"ok": False, "error": "...", "wait": <seconds>}
    """
    # Validate phone length
    e164 = _e164(phone)
    if len(e164) < 10:
        return {"ok": False, "error": "Invalid phone number."}

    # Rate limit check
    allowed, reason = _check_rate_limit(phone)
    if not allowed:
        return {"ok": False, "error": reason}

    otp   = _gen_otp(4)
    now   = datetime.utcnow()
    exp   = now + timedelta(minutes=OTP_EXPIRY_MINUTES)

    # Invalidate any prior active OTP for this phone
    db.otp_sessions.update_many(
        {"phone": phone, "verified": False},
        {"$set": {"invalidated": True}},
    )

    # Insert new OTP session
    db.otp_sessions.insert_one({
        "phone":      phone,
        "otp":        otp,
        "created_at": now,
        "expires_at": exp,
        "attempts":   0,
        "verified":   False,
        "invalidated": False,
        "sent_via":   "msg91",
    })

    # ── MSG91 Send ────────────────────────────────────────────────────────────
    if not MSG91_AUTH_KEY or not MSG91_TEMPLATE_ID:
        # Dev / unconfigured mode — return OTP in response (never do this in prod!)
        logger.warning(f"[OTP] MSG91 not configured — DEV mode OTP for {phone}: {otp}")
        return {"ok": True, "dev_otp": otp, "mode": "dev"}

    payload = {
        "template_id": MSG91_TEMPLATE_ID,
        "mobile":      e164,
        "authkey":     MSG91_AUTH_KEY,
        "otp":         otp,
        "otp_expiry":  OTP_EXPIRY_MINUTES,
        "sender":      MSG91_SENDER_ID,
    }

    try:
        resp = httpx.post(
            MSG91_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
        data = resp.json()
        logger.info(f"[MSG91] send response for {phone}: {data}")

        if resp.status_code == 200 and data.get("type") == "success":
            return {"ok": True}

        # MSG91 returned an error
        err_msg = data.get("message") or data.get("error") or "SMS delivery failed."
        logger.error(f"[MSG91] error for {phone}: {err_msg}")
        return {"ok": False, "error": f"Could not send OTP: {err_msg}"}

    except httpx.TimeoutException:
        logger.error(f"[MSG91] timeout for {phone}")
        return {"ok": False, "error": "OTP delivery timed out. Please try again."}
    except Exception as e:
        logger.error(f"[MSG91] exception for {phone}: {e}")
        return {"ok": False, "error": "Failed to send OTP. Please try again."}

# ── Verify OTP ────────────────────────────────────────────────────────────────
def verify_otp(phone: str, otp_input: str) -> dict:
    """
    Validates submitted OTP against stored session.

    Returns:
      {"ok": True}
      {"ok": False, "error": "...", "locked": bool}
    """
    now = datetime.utcnow()

    session = db.otp_sessions.find_one(
        {
            "phone":       phone,
            "verified":    False,
            "invalidated": False,
            "expires_at":  {"$gt": now},
        },
        sort=[("created_at", -1)],
    )

    if not session:
        # Check if there's an expired one to give better UX
        expired = db.otp_sessions.find_one(
            {"phone": phone, "verified": False, "invalidated": False},
            sort=[("created_at", -1)],
        )
        if expired:
            return {"ok": False, "error": "OTP has expired. Please request a new one."}
        return {"ok": False, "error": "No active OTP found. Please request a new one."}

    # Check attempt count (lockout protection)
    attempts = session.get("attempts", 0)
    if attempts >= OTP_MAX_ATTEMPTS:
        db.otp_sessions.update_one(
            {"_id": session["_id"]},
            {"$set": {"invalidated": True}},
        )
        return {
            "ok":     False,
            "error":  "Too many incorrect attempts. Please request a new OTP.",
            "locked": True,
        }

    # Validate OTP
    if otp_input.strip() != session["otp"]:
        db.otp_sessions.update_one(
            {"_id": session["_id"]},
            {"$inc": {"attempts": 1}},
        )
        remaining = OTP_MAX_ATTEMPTS - attempts - 1
        if remaining <= 0:
            db.otp_sessions.update_one(
                {"_id": session["_id"]},
                {"$set": {"invalidated": True}},
            )
            return {
                "ok":     False,
                "error":  "Too many incorrect attempts. Please request a new OTP.",
                "locked": True,
            }
        return {
            "ok":    False,
            "error": f"Incorrect OTP. {remaining} attempt{'s' if remaining > 1 else ''} remaining.",
        }

    # ✅ Correct OTP — mark as verified
    db.otp_sessions.update_one(
        {"_id": session["_id"]},
        {"$set": {"verified": True, "verified_at": now}},
    )
    return {"ok": True}
