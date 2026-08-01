"""
otp_service.py — MSG91 OTP integration for OFFRO
Place this file alongside users.py in the routers/ directory.

Environment variables required (set in Railway):
  MSG91_AUTH_KEY      — your MSG91 auth key
  MSG91_TEMPLATE_ID   — DLT-approved OTP template ID
  MSG91_SENDER_ID     — 6-char sender ID (e.g. OFFROO)

Optional:
  OTP_EXPIRY_MINUTES  — default 10
  OTP_MAX_ATTEMPTS    — default 5
  OTP_RESEND_SECONDS  — default 30
  OTP_RATE_LIMIT_HOUR — default 5
"""

import os, random, string
from datetime import datetime, timedelta

import httpx
from database import db

# ── Config ────────────────────────────────────────────────────────────────────
MSG91_AUTH_KEY    = os.environ.get("MSG91_AUTH_KEY", "").strip()
MSG91_TEMPLATE_ID = os.environ.get("MSG91_TEMPLATE_ID", "").strip()
MSG91_SENDER_ID   = os.environ.get("MSG91_SENDER_ID", "OFFROO").strip()

OTP_EXPIRY_MINUTES  = int(os.environ.get("OTP_EXPIRY_MINUTES",  "10"))
OTP_MAX_ATTEMPTS    = int(os.environ.get("OTP_MAX_ATTEMPTS",     "5"))
OTP_RESEND_SECONDS  = int(os.environ.get("OTP_RESEND_SECONDS",  "30"))
OTP_RATE_LIMIT_HOUR = int(os.environ.get("OTP_RATE_LIMIT_HOUR",  "5"))

MSG91_URL = "https://control.msg91.com/api/v5/otp"

# ── Print config on first import (visible in Railway logs) ───────────────────
print(f"[OTP_SERVICE] ══════════════════════════════════════════")
print(f"[OTP_SERVICE] MSG91_AUTH_KEY    : {'SET ('+str(len(MSG91_AUTH_KEY))+' chars)' if MSG91_AUTH_KEY else '❌ NOT SET'}")
print(f"[OTP_SERVICE] MSG91_TEMPLATE_ID : '{MSG91_TEMPLATE_ID}' {'✅' if MSG91_TEMPLATE_ID else '❌ NOT SET'}")
print(f"[OTP_SERVICE] MSG91_SENDER_ID   : '{MSG91_SENDER_ID}' {'✅' if MSG91_SENDER_ID else '❌ NOT SET'}")
print(f"[OTP_SERVICE] Mode              : {'🟢 LIVE (MSG91)' if MSG91_AUTH_KEY and MSG91_TEMPLATE_ID else '🟡 DEV (no SMS)'}")
print(f"[OTP_SERVICE] OTP expiry        : {OTP_EXPIRY_MINUTES} min | resend cooldown: {OTP_RESEND_SECONDS}s")
print(f"[OTP_SERVICE] ══════════════════════════════════════════")

# ── Helpers ───────────────────────────────────────────────────────────────────
def _gen_otp(length: int = 4) -> str:
    return "".join(random.choices(string.digits, k=length))

def _e164(phone: str) -> str:
    """Convert +91XXXXXXXXXX → 91XXXXXXXXXX for MSG91."""
    p = str(phone).strip().replace(" ", "").replace("-", "")
    if p.startswith("+"):
        p = p[1:]
    if not p.startswith("91") and len(p) == 10:
        p = "91" + p
    return p

# ── OTP Collection indexes ────────────────────────────────────────────────────
def _ensure_otp_indexes():
    """Creates TTL index so MongoDB auto-purges expired OTPs."""
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
        print("[OTP_SERVICE] ✅ OTP indexes ensured")
    except Exception as e:
        print(f"[OTP_SERVICE] ⚠️  OTP index warning: {e}")

# ── Rate limiting ─────────────────────────────────────────────────────────────
def _check_rate_limit(phone: str) -> tuple:
    """Returns (allowed: bool, reason: str)."""
    now = datetime.utcnow()
    latest = db.otp_sessions.find_one(
        {"phone": phone},
        sort=[("created_at", -1)],
    )
    if latest:
        since_last = (now - latest["created_at"]).total_seconds()
        if since_last < OTP_RESEND_SECONDS:
            wait = int(OTP_RESEND_SECONDS - since_last)
            print(f"[OTP] ⏱  Rate limit hit for {phone} — wait {wait}s")
            return False, f"Please wait {wait} seconds before requesting another OTP."
        hour_ago = now - timedelta(hours=1)
        count_last_hour = db.otp_sessions.count_documents({
            "phone": phone,
            "created_at": {"$gte": hour_ago},
        })
        if count_last_hour >= OTP_RATE_LIMIT_HOUR:
            print(f"[OTP] 🚫 Hourly cap hit for {phone} ({count_last_hour} in last hour)")
            return False, "Too many OTP requests. Please try again after an hour."
    return True, ""

# ── Send OTP via MSG91 ────────────────────────────────────────────────────────
def send_otp(phone: str) -> dict:
    """
    Generates OTP, stores in MongoDB, sends via MSG91.
    Returns {"ok": True} on success or {"ok": False, "error": "..."} on failure.
    """
    e164 = _e164(phone)
    print(f"[OTP] ── send_otp called ──────────────────────────")
    print(f"[OTP] phone (canonical): {phone}")
    print(f"[OTP] phone (e164/MSG91): {e164}")

    if len(e164) < 10:
        print(f"[OTP] ❌ Invalid phone length: {len(e164)}")
        return {"ok": False, "error": "Invalid phone number."}

    allowed, reason = _check_rate_limit(phone)
    if not allowed:
        return {"ok": False, "error": reason}

    otp = _gen_otp(4)
    now = datetime.utcnow()
    exp = now + timedelta(minutes=OTP_EXPIRY_MINUTES)

    # Invalidate prior active OTPs
    invalidated = db.otp_sessions.update_many(
        {"phone": phone, "verified": False},
        {"$set": {"invalidated": True}},
    )
    print(f"[OTP] invalidated {invalidated.modified_count} old OTP(s) for {phone}")

    # Store new OTP
    db.otp_sessions.insert_one({
        "phone":       phone,
        "otp":         otp,
        "created_at":  now,
        "expires_at":  exp,
        "attempts":    0,
        "verified":    False,
        "invalidated": False,
        "sent_via":    "msg91",
    })
    print(f"[OTP] stored OTP in otp_sessions (expires {OTP_EXPIRY_MINUTES}min)")

    # ── Dev mode guard ────────────────────────────────────────────────────────
    if not MSG91_AUTH_KEY or not MSG91_TEMPLATE_ID:
        print(f"[OTP] 🟡 DEV MODE — MSG91 keys missing, OTP={otp} (not sent via SMS)")
        return {"ok": True, "dev_otp": otp, "mode": "dev"}

    # ── Build MSG91 payload ───────────────────────────────────────────────────
    payload = {
        "template_id": MSG91_TEMPLATE_ID,
        "mobile":      e164,
        "authkey":     MSG91_AUTH_KEY,
        "otp":         otp,
        "otp_expiry":  OTP_EXPIRY_MINUTES,
        "sender":      MSG91_SENDER_ID,
    }
    # Log payload (mask authkey middle chars for safety)
    safe_key = MSG91_AUTH_KEY[:6] + "..." + MSG91_AUTH_KEY[-4:] if len(MSG91_AUTH_KEY) > 10 else "***"
    print(f"[OTP] MSG91 payload: template_id={MSG91_TEMPLATE_ID} | mobile={e164} | sender={MSG91_SENDER_ID} | authkey={safe_key} | otp_expiry={OTP_EXPIRY_MINUTES}")
    print(f"[OTP] Calling MSG91 URL: {MSG91_URL}")

    # ── HTTP call ─────────────────────────────────────────────────────────────
    try:
        # Invalidate any existing MSG91 OTP session for this number first.
        # Without this, MSG91 deduplicates the request and returns success
        # without actually sending a new SMS (silent deduplication).
        try:
            retry_url = MSG91_URL + "/retryotp"
            httpx.get(
                retry_url,
                params={"authkey": MSG91_AUTH_KEY, "mobile": e164, "retrytype": "text"},
                timeout=5.0,
            )
            print(f"[MSG91] ↺  retryotp called for {e164}")
        except Exception as _re:
            print(f"[MSG91] ↺  retryotp skipped ({type(_re).__name__})")

        resp = httpx.post(
            MSG91_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15.0,
        )

        # ── FULL RAW LOG ──────────────────────────────────────────────────────
        print(f"[MSG91] ◀ HTTP status  : {resp.status_code}")
        print(f"[MSG91] ◀ Response body: {resp.text}")
        print(f"[MSG91] ◀ Headers      : {dict(resp.headers)}")

        try:
            data = resp.json()
        except Exception as je:
            print(f"[MSG91] ❌ Could not parse JSON: {je} | raw: {resp.text}")
            return {"ok": False, "error": "MSG91 returned non-JSON response. Check logs."}

        print(f"[MSG91] ◀ Parsed JSON  : {data}")
        msg_type = data.get("type", "")
        msg_msg  = data.get("message", data.get("msg", ""))

        if msg_type == "success" and "request_id" in data:
            print(f"[MSG91] ✅ OTP sent successfully to {e164} (HTTP {resp.status_code})")
            return {"ok": True}

        # Error path — log everything
        print(f"[MSG91] ❌ FAILED — type='{msg_type}' message='{msg_msg}' full={data}")
        # Common MSG91 error codes
        if "invalid" in str(msg_msg).lower() and "template" in str(msg_msg).lower():
            print(f"[MSG91] 💡 Hint: Template ID '{MSG91_TEMPLATE_ID}' may be wrong or not approved")
        if "sender" in str(msg_msg).lower():
            print(f"[MSG91] 💡 Hint: Sender ID '{MSG91_SENDER_ID}' may not match the template")
        if "auth" in str(msg_msg).lower() or resp.status_code == 401:
            print(f"[MSG91] 💡 Hint: AUTH KEY may be invalid or expired")

        return {"ok": False, "error": f"MSG91 error: {msg_msg or msg_type or 'Unknown error'}"}

    except httpx.TimeoutException:
        print(f"[MSG91] ⏱  Timeout after 15s calling {MSG91_URL}")
        return {"ok": False, "error": "OTP delivery timed out. Please try again."}
    except httpx.ConnectError as ce:
        print(f"[MSG91] 🔌 ConnectError: {ce}")
        return {"ok": False, "error": "Cannot reach MSG91 servers. Please try again."}
    except Exception as e:
        print(f"[MSG91] ❌ Unexpected exception: {type(e).__name__}: {e}")
        return {"ok": False, "error": "Failed to send OTP. Please try again."}

# ── Verify OTP ────────────────────────────────────────────────────────────────
def verify_otp(phone: str, otp_input: str) -> dict:
    """
    Validates submitted OTP against stored session.
    Returns {"ok": True} or {"ok": False, "error": "...", "locked": bool}
    """
    now = datetime.utcnow()
    print(f"[OTP] ── verify_otp called ─────────────────────────")
    print(f"[OTP] phone={phone} | otp_input={'*' * len(otp_input)} (len={len(otp_input)})")

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
        # Check if expired
        expired = db.otp_sessions.find_one(
            {"phone": phone, "verified": False, "invalidated": False},
            sort=[("created_at", -1)],
        )
        if expired:
            print(f"[OTP] ❌ OTP expired for {phone} (expired_at={expired.get('expires_at')})")
            return {"ok": False, "error": "OTP has expired. Please request a new one."}
        print(f"[OTP] ❌ No active OTP found for {phone}")
        return {"ok": False, "error": "No active OTP found. Please request a new one."}

    attempts = session.get("attempts", 0)
    print(f"[OTP] session found | attempts so far: {attempts}/{OTP_MAX_ATTEMPTS}")

    if attempts >= OTP_MAX_ATTEMPTS:
        db.otp_sessions.update_one({"_id": session["_id"]}, {"$set": {"invalidated": True}})
        print(f"[OTP] 🔒 Locked — too many attempts for {phone}")
        return {"ok": False, "error": "Too many incorrect attempts. Please request a new OTP.", "locked": True}

    if otp_input.strip() != session["otp"]:
        db.otp_sessions.update_one({"_id": session["_id"]}, {"$inc": {"attempts": 1}})
        remaining = OTP_MAX_ATTEMPTS - attempts - 1
        print(f"[OTP] ❌ Wrong OTP for {phone} | remaining attempts: {remaining}")
        if remaining <= 0:
            db.otp_sessions.update_one({"_id": session["_id"]}, {"$set": {"invalidated": True}})
            return {"ok": False, "error": "Too many incorrect attempts. Please request a new OTP.", "locked": True}
        return {"ok": False, "error": f"Incorrect OTP. {remaining} attempt{'s' if remaining > 1 else ''} remaining."}

    # ✅ Correct
    db.otp_sessions.update_one(
        {"_id": session["_id"]},
        {"$set": {"verified": True, "verified_at": now}},
    )
    print(f"[OTP] ✅ OTP verified successfully for {phone}")
    return {"ok": True}
