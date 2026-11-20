# routers/dashboard_auth.py
# OffrO Admin Dashboard — Role-Based Access Control (RBAC) + 2FA OTP
# Phase 1 + 2 + 3: Collections, Auth, Security Helpers, CRUD endpoints, 2FA
#
# Uses stdlib hashlib (pbkdf2_hmac) for PIN hashing — NO external deps needed.
# Mount in server.py: from routers import dashboard_auth
#                     app.include_router(dashboard_auth.router, prefix="/admin")

import os, secrets, hashlib, json
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from bson import ObjectId
from database import db

router = APIRouter(prefix="/auth", tags=["Dashboard RBAC & Auth"])

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
INACTIVITY_TIMEOUT_MINUTES = 30
SESSION_MAX_HOURS = 8
PBKDF2_ITERATIONS = 100_000

# ── 2FA / OTP settings: the OTP rules (4 digits, 5 min expiry, 30 s resend cooldown, 5 attempts, rate limits) live in
#    services/admin_login_otp.py; OTP_* below are re-exported for anything that imported them from here.
from services import admin_login_otp as _otp
from services import admin_login_limits as _lim
from services import admin_trusted_devices as _td
OTP_LENGTH = _otp.OTP_LENGTH
OTP_EXPIRY_MINUTES = _otp.OTP_EXPIRY_MINUTES
OTP_MAX_ATTEMPTS = _otp.OTP_MAX_ATTEMPTS
OTP_RESEND_SECONDS = _otp.OTP_RESEND_SECONDS
TRUSTED_DEVICE_COOKIE = "offro_trusted"      # OLD cookie (plain tokens): never honoured, only cleared. The current cookie is services/admin_trusted_devices.COOKIE

ALL_MODULES = [
    "Accounts", "Stores", "Products", "Banners", "Admin Banners", "Popup Campaigns",
    "Payments", "Gift Vouchers", "Notifications", "Categories", "Pricing & GST",
    "Reviews", "Discounts", "Terms & Conditions", "Policies",
    "Social Media", "Live Chat", "Default Images", "User Management",
    "WhatsApp Campaigns",   # dedicated module (Phase 2A): never granted to an existing/seeded role except Super Admin
    "Deals",                # admin view/manage of merchant deals; existing DB roles lack it, so only Super Admin sees it until a role is granted
    "Translations",         # Phase G: view / edit drafts only. Publishing, rollback, languages, packages and the kill switch are Super Admin only (checked in routers/translations_admin.py), never by this matrix
]

ALL_ACTIONS = ["view", "add", "edit", "delete", "approve", "export"]

# ═══════════════════════════════════════════════════════════════
# PIN HASHING (stdlib only)
# ═══════════════════════════════════════════════════════════════

def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return salt + "$" + h.hex()

def verify_pin(plain_pin: str, stored: str) -> bool:
    try:
        salt_str, hash_str = stored.split("$", 1)
        h = hashlib.pbkdf2_hmac("sha256", plain_pin.encode(), bytes.fromhex(salt_str), PBKDF2_ITERATIONS)
        return secrets.compare_digest(h.hex(), hash_str)
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# 2FA OTP: server-generated, hashed, verified server-side (services/admin_login_otp.py).
# SMS goes through the existing MSG91 integration (routers/otp_service.py: send_sms_otp), with the existing
# MSG91_AUTH_KEY / MSG91_ADMIN_TEMPLATE_ID (else MSG91_TEMPLATE_ID) / MSG91_SENDER_ID settings. No new credentials.
# ═══════════════════════════════════════════════════════════════

def _admin_template_id() -> str:
    return os.getenv("MSG91_ADMIN_TEMPLATE_ID", os.getenv("MSG91_TEMPLATE_ID", "")).strip()


def _clear_trusted_cookie(response: Response):
    """The trusted-device bypass is gone; this only removes a stale cookie from the browser."""
    response.delete_cookie(TRUSTED_DEVICE_COOKIE, samesite="Lax")


# ═══════════════════════════════════════════════════════════════
# ACTIVITY LOG HELPER
# ═══════════════════════════════════════════════════════════════

def log_activity(
    request: Request,
    user_id,
    user_name: str,
    mobile: str,
    module: str,
    action: str,
    record_id: str = "",
    record_name: str = "",
    city: str = "",
    before_value: dict = None,
    after_value: dict = None
):
    ip = ""
    try:
        ip = request.client.host if request.client else "127.0.0.1"
    except Exception:
        ip = "unknown"
    ua = request.headers.get("user-agent", "unknown") if request else "unknown"

    doc = {
        "timestamp": datetime.utcnow(),
        "user_id": user_id,
        "user_name": user_name,
        "mobile": mobile,
        "module": module,
        "action": action,
        "record_id": record_id,
        "record_name": record_name,
        "city": city,
        "ip_address": ip,
        "user_agent": ua,
        "before_value": before_value or {},
        "after_value": after_value or {}
    }
    try:
        db.activity_logs.insert_one(doc)
        cutoff = datetime.utcnow() - timedelta(days=15)
        db.activity_logs.delete_many({"timestamp": {"$lt": cutoff}})
    except Exception as e:
        print("[RBAC] log_activity error: " + str(e))

# ═══════════════════════════════════════════════════════════════
# AUTH DEPENDENCIES
# ═══════════════════════════════════════════════════════════════

async def get_current_dashboard_user(request: Request) -> dict:
    """
    Resolve the current dashboard user from cookie or Bearer token.
    Only dashboard_users sessions (created by the PIN + OTP sign-in) are accepted. The legacy db.admins token fallback was retired (Patch B).
    """
    token = request.cookies.get("admin_token") or \
            request.headers.get("Authorization", "").replace("Bearer ", "")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = db.dashboard_users.find_one({"token": token})

    if not user:
        raise HTTPException(status_code=401, detail="Invalid session")

    if user.get("status") in ("disabled", "suspended"):
        raise HTTPException(status_code=403, detail="Account is disabled or suspended")

    # Inactivity timeout check (30 minutes per 2FA spec)
    last_active = user.get("last_active_at") or user.get("created_at") or datetime.utcnow()
    if datetime.utcnow() - last_active > timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES):
        db.dashboard_users.update_one({"_id": user["_id"]}, {"$set": {"token": None}})
        raise HTTPException(status_code=401, detail="Session expired due to inactivity")

    # Update sliding activity window
    try:
        db.dashboard_users.update_one(
            {"_id": user["_id"]},
            {"$set": {"last_active_at": datetime.utcnow()}}
        )
    except Exception:
        pass

    role = db.dashboard_roles.find_one({"_id": user["role_id"]}) if user.get("role_id") else None
    if not role:
        role = {"role_name": "Unknown", "permissions": {}}
    user["role"] = role
    return user


def require_permission(module: str, action: str):
    def dependency(user: dict = Depends(get_current_dashboard_user)) -> dict:
        role_name = user.get("role", {}).get("role_name", "")
        if role_name == "Super Admin":
            return user

        perms = user.get("role", {}).get("permissions", {})
        module_perms = perms.get(module, {})
        if not module_perms.get(action, False):
            raise HTTPException(
                status_code=403,
                detail="Access denied: Missing " + action + " permission for " + module
            )
        return user
    return dependency


def get_assigned_cities_filter(user: dict = Depends(get_current_dashboard_user)) -> dict:
    assigned = user.get("assigned_cities", [])
    role_name = user.get("role", {}).get("role_name", "")

    if role_name == "Super Admin" or "*" in assigned:
        return {}

    if not assigned:
        return {"city": "____IMPOSSIBLE_CITY_NONE____"}

    return {"city": {"$in": assigned}}

# ═══════════════════════════════════════════════════════════════
# SEED FUNCTION
# ═══════════════════════════════════════════════════════════════

def seed_rbac():
    """Seed default roles if they don't exist (no default user; see tools/create_super_admin.py)."""
    # ── 1. Seed default roles ──
    if db.dashboard_roles.count_documents({}) == 0:
        super_admin_perms = {}
        for mod in ALL_MODULES:
            super_admin_perms[mod] = {act: True for act in ALL_ACTIONS}

        admin_perms = {}
        for mod in ALL_MODULES:
            admin_perms[mod] = {act: True for act in ALL_ACTIONS}
        admin_perms["User Management"] = {act: (act == "view") for act in ALL_ACTIONS}
        admin_perms["WhatsApp Campaigns"] = {act: False for act in ALL_ACTIONS}     # explicit grant only
        admin_perms["Translations"] = {act: False for act in ALL_ACTIONS}           # explicit grant only (Phase G)

        readonly_perms = {}
        for mod in ALL_MODULES:
            readonly_perms[mod] = {act: (act == "view") for act in ALL_ACTIONS}
        readonly_perms["WhatsApp Campaigns"] = {act: False for act in ALL_ACTIONS}  # explicit grant only
        readonly_perms["Translations"] = {act: False for act in ALL_ACTIONS}        # explicit grant only (Phase G)

        sales_perms = {}
        for mod in ALL_MODULES:
            sales_perms[mod] = {act: False for act in ALL_ACTIONS}
        for mod in ["Stores", "Products"]:
            sales_perms[mod] = {"view": True, "add": True, "edit": False, "delete": False, "approve": False, "export": True}
        sales_perms["Accounts"] = {"view": True, "add": False, "edit": False, "delete": False, "approve": False, "export": True}
        sales_perms["Notifications"] = {"view": True, "add": False, "edit": False, "delete": False, "approve": False, "export": True}

        city_mgr_perms = {}
        for mod in ALL_MODULES:
            city_mgr_perms[mod] = {act: False for act in ALL_ACTIONS}
        for mod in ["Stores", "Products", "Banners", "Categories"]:
            city_mgr_perms[mod] = {"view": True, "add": True, "edit": True, "delete": False, "approve": True, "export": True}
        city_mgr_perms["Accounts"] = {"view": True, "add": False, "edit": True, "delete": False, "approve": False, "export": True}
        city_mgr_perms["Notifications"] = {"view": True, "add": False, "edit": False, "delete": False, "approve": False, "export": True}

        finance_perms = {}
        for mod in ALL_MODULES:
            finance_perms[mod] = {act: False for act in ALL_ACTIONS}
        for mod in ["Payments", "Gift Vouchers"]:
            finance_perms[mod] = {"view": True, "add": False, "edit": False, "delete": False, "approve": False, "export": True}

        marketing_perms = {}
        for mod in ALL_MODULES:
            marketing_perms[mod] = {act: False for act in ALL_ACTIONS}
        for mod in ["Banners", "Notifications"]:
            marketing_perms[mod] = {"view": True, "add": True, "edit": True, "delete": True, "approve": False, "export": True}
        marketing_perms["Stores"] = {"view": True, "add": False, "edit": False, "delete": False, "approve": False, "export": True}

        roles_to_seed = [
            {"role_name": "Super Admin", "description": "Full access to all modules and cities", "permissions": super_admin_perms, "is_system_role": True},
            {"role_name": "Administrator", "description": "Full access except Settings management", "permissions": admin_perms, "is_system_role": True},
            {"role_name": "Sales Executive", "description": "View merchants, manage stores/products/deals", "permissions": sales_perms, "is_system_role": False},
            {"role_name": "City Manager", "description": "Manage stores/products/banners for assigned cities", "permissions": city_mgr_perms, "is_system_role": False},
            {"role_name": "Finance", "description": "View and export payments, invoices, reports", "permissions": finance_perms, "is_system_role": False},
            {"role_name": "Marketing", "description": "Manage banners, notifications, and deals", "permissions": marketing_perms, "is_system_role": False},
            {"role_name": "Read Only", "description": "View all modules, no write access", "permissions": readonly_perms, "is_system_role": True},
        ]

        for r in roles_to_seed:
            r["created_at"] = datetime.utcnow()
            r["updated_at"] = datetime.utcnow()
            db.dashboard_roles.insert_one(r)

        print("[RBAC] Seeded " + str(len(roles_to_seed)) + " default roles")

    # ── 2. Super Admin user ──
    # (Patch B) No default Super Admin (mobile/PIN) is created any more and no credentials are printed. Existing accounts are untouched.
    # On an EMPTY database create the first one deliberately with: python tools/create_super_admin.py
    if db.dashboard_users.count_documents({}) == 0:
        print("[RBAC] No dashboard users exist. Create the first Super Admin with: python tools/create_super_admin.py")

    # ── 3. Create indexes ──
    try:
        db.dashboard_users.create_index("mobile", unique=True)
        db.dashboard_users.create_index("token", sparse=True)
        db.dashboard_roles.create_index("role_name", unique=True)
        db.activity_logs.create_index([("timestamp", -1)])
        db.activity_logs.create_index("user_id")
        db.activity_logs.create_index("module")
        # 2FA collections
        db.trusted_devices.create_index("token", unique=True)       # old collection (plain tokens): kept as is, never read
        # Admin 2FA OTP sessions (standalone collection)
        try:
            db.admin_otp_sessions.create_index("mobile")
            db.admin_otp_sessions.create_index(
                "expires_at",
                expireAfterSeconds=0,
                name="admin_otp_ttl"
            )
            print("[2FA] admin_otp_sessions indexes ensured")
        except Exception as _idx_e:
            print("[2FA] admin_otp_sessions index note: " + str(_idx_e))
        db.trusted_devices.create_index("user_id")
        db.trusted_devices.create_index("expires_at")
        _otp.ensure_indexes()               # admin_login_challenges + admin_auth_throttle (new collections; TTL-purged)
        _td.ensure_indexes()                # admin_trusted_devices (hashed tokens)
    except Exception as e:
        print("[RBAC] Index creation (may already exist): " + str(e))


# ═══════════════════════════════════════════════════════════════
# 2FA AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════

_GENERIC_CREDENTIALS = "Invalid mobile number or PIN."
_GENERIC_THROTTLED = "Too many attempts. Please wait a few minutes and try again."
_GENERIC_OTP = "That code is incorrect or has expired. Please try again or sign in again."
_GENERIC_SEND = "We could not send the verification code. Please try again in a moment."
_GENERIC_FAILURE = "We could not complete sign-in. Please try again."


def _failure(step: str, exc: Exception) -> HTTPException:
    """A friendly 500 for the browser plus a short reference that is also written to the server log with the exception TYPE only (never the
    message, mobile, PIN or OTP), so a failure the user reports can be matched to its log line without exposing anything to the page."""
    ref = secrets.token_hex(4)
    print("[2FA] %s error ref=%s type=%s" % (step, ref, type(exc).__name__))
    return HTTPException(status_code=500, detail=_GENERIC_FAILURE + " (ref " + ref + ")")


def _mobile_for_challenge(user_id):
    """Mobile of an ACTIVE admin account (None if missing / disabled / suspended): the number a widget OTP must be verified for."""
    u = db.dashboard_users.find_one({"_id": user_id}, {"mobile": 1, "status": 1})
    return u.get("mobile") if u and u.get("status") not in ("disabled", "suspended") else None


def _client_ip(request: Request) -> str:
    """Client IP for rate limiting. Behind Railway's proxy request.client is the proxy, so use the right-most X-Forwarded-For entry
    (the one the trusted edge appended; left-hand entries can be forged by the caller)."""
    try:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            last = xff.split(",")[-1].strip()
            if last:
                return last[:64]
        return (request.client.host if request.client else "unknown")[:64]
    except Exception:
        return "unknown"


def _user_payload(user: dict, role_name: str) -> dict:
    return {"id": str(user["_id"]), "full_name": user.get("full_name", ""), "mobile": user.get("mobile", ""),
            "designation": user.get("designation", ""), "role": role_name}


def _role_name(user: dict) -> str:
    role = db.dashboard_roles.find_one({"_id": user["role_id"]}) if user.get("role_id") else None
    return role["role_name"] if role else "Unknown"


def _issue_session(user: dict, response: Response, role_name: str, event: str, request: Request) -> dict:
    """The ONE place an admin session is created. Callers have already proven the PIN and either a server-checked OTP or a valid trusted device."""
    session_token = secrets.token_urlsafe(32)
    db.dashboard_users.update_one({"_id": user["_id"]}, {"$set": {
        "token": session_token, "login_attempts": 0, "lockout_until": None,
        "last_login_at": datetime.utcnow(), "last_active_at": datetime.utcnow(), "updated_at": datetime.utcnow()}})
    log_activity(request, user["_id"], user.get("full_name", ""), user.get("mobile", ""), "Auth", event)
    response.set_cookie(key="admin_token", value=session_token, httponly=True, samesite="Lax", secure=False,
                        max_age=3600 * SESSION_MAX_HOURS)       # the security middleware adds Secure on HTTPS (unchanged)
    _clear_trusted_cookie(response)                              # the OLD plain-token cookie, if any
    _lim.clear_account(user.get("mobile", ""))                   # fully signed in: wrong-PIN / wrong-code history is forgiven
    return {"message": "Login successful", "otp_required": False, "user": _user_payload(user, role_name)}


def _set_device_cookie(response: Response, token: str):
    response.set_cookie(key=_td.COOKIE, value=token, httponly=True, samesite="strict", secure=False, path=_td.COOKIE_PATH,
                        max_age=86400 * _td.lifetime_days())    # Secure is added over HTTPS by the security middleware


def _clear_device_cookie(response: Response):
    response.delete_cookie(_td.COOKIE, path=_td.COOKIE_PATH, samesite="strict")


@router.post("/login")
async def login(data: dict, request: Request, response: Response):
    """Step 1: mobile + PIN (always required). Without a trusted device this returns an OTP challenge ONLY: no session, no cookie, no token.
    A valid trusted device for THIS account (and remember_device=true) skips the SMS OTP; anything else needs it.
    Every failure is the same generic message so the response never reveals whether the number is registered."""
    try:
        mobile = str(data.get("mobile", "")).strip() if isinstance(data, dict) else ""
        pin = str(data.get("pin", "")).strip() if isinstance(data, dict) else ""
        remember = isinstance(data, dict) and data.get("remember_device") is True       # a REQUEST only; it proves nothing
        ip = _client_ip(request)
        if not mobile or not pin or len(mobile) > 20 or len(pin) > 12:
            raise HTTPException(status_code=400, detail="Please enter your mobile number and PIN.")

        if _otp.throttle_hit("login_ip", ip, 15) > _otp.IP_LOGIN_LIMIT:          # coarse flood cap only; failures have their own limits
            raise HTTPException(status_code=429, detail=_GENERIC_THROTTLED)

        wait = _lim.pin_cooldown(mobile, ip)
        if wait > 0:
            raise HTTPException(status_code=429, detail=_lim.wait_message(wait))
        user = db.dashboard_users.find_one({"mobile": mobile})
        if user and user.get("lockout_until") and user["lockout_until"] > datetime.utcnow():       # a lock set by an older version / an administrator
            raise HTTPException(status_code=429, detail=_GENERIC_THROTTLED)

        pin_ok = False
        if user:
            pin_ok = verify_pin(pin, user.get("pin", ""))
        else:
            _otp.burn_pin_time()

        if user and user.get("status") in ("disabled", "suspended"):
            log_activity(request, user["_id"], user.get("full_name", ""), mobile, "Auth", "LOGIN_FAIL", record_name="Account " + user.get("status", ""))
            pin_ok = False                                           # same generic answer as a wrong PIN

        if not pin_ok:
            _lim.record_pin_failure(mobile, ip)                      # failures (and only failures) feed the limits
            if user:
                db.dashboard_users.update_one({"_id": user["_id"]}, {"$inc": {"login_attempts": 1}, "$set": {"last_failed_at": datetime.utcnow()}})
                log_activity(request, user["_id"], user.get("full_name", ""), mobile, "Auth", "LOGIN_FAIL")
            else:
                log_activity(request, None, "Unknown", "", "Auth", "LOGIN_FAIL", record_name="Account not found")
            wait = _lim.pin_cooldown(mobile, ip)
            if wait > 0:
                if user:
                    log_activity(request, user["_id"], user.get("full_name", ""), mobile, "Auth", "LOCKOUT", record_name=str(wait) + "s")
                raise HTTPException(status_code=429, detail=_lim.wait_message(wait))
            raise HTTPException(status_code=401, detail=_GENERIC_CREDENTIALS)

        _lim.clear_pin_failures_here(mobile, ip)                     # a correct PIN: typos from this place are forgiven
        wait = _lim.otp_cooldown(user["_id"])                        # too many wrong CODES on this account recently: no new code for a while
        if wait > 0:
            raise HTTPException(status_code=429, detail=_lim.wait_message(wait))

        # ── trusted device: skips the SMS OTP only, and only for THIS account on a still-valid, non-revoked device ──
        role_name = _role_name(user)
        device_allowed = _td.allowed_for(role_name)
        device_cookie = request.cookies.get(_td.COOKIE)
        if device_allowed and device_cookie:
            device = _td.find_valid(device_cookie, user["_id"])
            if device and remember:
                _td.touch(device["_id"])
                body = _issue_session(user, response, role_name, "TRUSTED_DEVICE_LOGIN", request)
                body["device_trusted"] = True
                return body
            if device and not remember:                              # the box was unticked on a remembered browser: forget it, OTP as normal
                _td.revoke_token(device_cookie)
                _clear_device_cookie(response)
                log_activity(request, user["_id"], user.get("full_name", ""), mobile, "Auth", "TRUSTED_DEVICE_FORGOTTEN")

        # PIN verified. NO session is created here: an OTP challenge is started and the browser must complete it.
        started = _otp.start_challenge(user, mobile, ip, request.headers.get("user-agent", ""), _admin_template_id(),
                                       remember=bool(remember and device_allowed))
        if started.get("ok"):
            log_activity(request, user["_id"], user.get("full_name", ""), mobile, "Auth", "OTP_SENT")
        else:
            log_activity(request, user["_id"], user.get("full_name", ""), mobile, "Auth", "OTP_SEND_FAILED", record_name=started.get("reason", ""))
            if started.get("reason") == "rate_limited":
                raise HTTPException(status_code=429, detail=_GENERIC_THROTTLED)
            if started.get("reason") == "not_configured":        # the MSG91 widget is not configured here: refuse; there is NO fallback code
                ref = secrets.token_hex(4)
                print("[2FA] login refused: MSG91 widget not configured ref=" + ref)
                raise HTTPException(status_code=503, detail=_GENERIC_SEND + " (ref " + ref + ")")
            raise HTTPException(status_code=503, detail=_GENERIC_SEND)
        return {"otp_required": True, "challenge_id": started["token"], "otp_length": OTP_LENGTH, "mobile_masked": started["mobile_masked"],
                "otp_expires_in": started["expires_in"], "resend_available_in": started["resend_in"],
                "provider": started.get("provider", "local")}
    except HTTPException:
        raise
    except Exception as e:
        raise _failure("login", e)


@router.post("/verify-otp")
async def verify_otp(data: dict, request: Request, response: Response):
    """Step 2: challenge_id + 4-digit OTP. The server alone decides; any browser-supplied 'verified' flag, mobile or trust option is
    ignored here (the trust request was recorded on the challenge at the PIN step). The session cookie is created only here, only for the
    single caller that consumes the challenge."""
    try:
        challenge_id = data.get("challenge_id") if isinstance(data, dict) else None
        otp = data.get("otp") if isinstance(data, dict) else None
        ip = _client_ip(request)
        if _otp.throttle_hit("verify_ip", ip, 15) > _otp.IP_VERIFY_LIMIT:
            raise HTTPException(status_code=429, detail=_GENERIC_THROTTLED)
        wait = _lim.verify_cooldown(ip)
        if wait > 0:
            raise HTTPException(status_code=429, detail=_lim.wait_message(wait))
        cid = challenge_id if isinstance(challenge_id, str) else ""
        if _otp.challenge_provider(cid) == "widget":
            # Widget challenge: the browser's own "verified" result is NEVER trusted. Only the MSG91 access token counts, and the server asks
            # MSG91 itself whether it is genuine and belongs to this account's number (services/admin_login_otp.verify_widget).
            access_token = data.get("access_token") if isinstance(data, dict) else None
            result = _otp.verify_widget(cid, access_token.strip() if isinstance(access_token, str) else "", _mobile_for_challenge)
            if not result.get("ok") and result.get("reason") == "unavailable":
                ref = secrets.token_hex(4)
                print("[2FA] widget verification unavailable ref=" + ref)
                raise HTTPException(status_code=503, detail=_GENERIC_FAILURE + " (ref " + ref + ")")
        else:
            result = _otp.verify_challenge(cid, otp.strip() if isinstance(otp, str) else "")
        if not result.get("ok"):
            _lim.record_verify_failure(ip)
            raise HTTPException(status_code=401, detail=_GENERIC_OTP)

        user = db.dashboard_users.find_one({"_id": result["user_id"]})
        if not user or user.get("status") in ("disabled", "suspended") or (
                user.get("lockout_until") and user["lockout_until"] > datetime.utcnow()):
            raise HTTPException(status_code=401, detail=_GENERIC_OTP)

        role_name = _role_name(user)
        body = _issue_session(user, response, role_name, "OTP_VERIFIED", request)
        trusted = False
        if result.get("remember") and _td.allowed_for(role_name):
            try:
                _set_device_cookie(response, _td.register(user["_id"], ip, request.headers.get("user-agent", "")))
                log_activity(request, user["_id"], user.get("full_name", ""), user.get("mobile", ""), "Auth", "TRUSTED_DEVICE_ADDED")
                trusted = True
            except Exception as e:                                   # the sign-in itself succeeded; the device just is not remembered
                print("[2FA] trusted device not registered type=" + type(e).__name__)
        body["device_trusted"] = trusted
        return body
    except HTTPException:
        raise
    except Exception as e:
        raise _failure("verify", e)


def _widget_limits(request: Request):
    ip = _client_ip(request)
    if _otp.throttle_hit("resend_ip", ip, 15) > _otp.IP_VERIFY_LIMIT:
        raise HTTPException(status_code=429, detail=_GENERIC_THROTTLED)
    return ip


@router.post("/otp-request")
async def otp_request(data: dict, request: Request):
    """Widget mode: permission to ask the MSG91 widget to send (first call) or resend (later calls) the code for this challenge. Returns the
    verification identifier (91 + the account's own mobile, derived server-side from the challenge: the browser cannot choose it) and the PUBLIC
    widget config. Enforces the 30 s cooldown, the 3-resend cap, the SMS budget and the challenge lifetime."""
    try:
        ip = _widget_limits(request)
        cid = data.get("challenge_id") if isinstance(data, dict) else None
        result = _otp.request_send(cid if isinstance(cid, str) else "", _mobile_for_challenge, ip)
        headers = {"Cache-Control": "no-store"}
        if result.get("ok"):
            return JSONResponse({"identifier": result["identifier"], "widget": result["widget"], "first": result["first"],
                                 "otp_expires_in": result["expires_in"], "resend_available_in": result["resend_in"]}, headers=headers)
        reason = result.get("reason")
        if reason == "cooldown":
            raise HTTPException(status_code=429, detail="Please wait before requesting another code.")
        if reason == "rate_limited":
            raise HTTPException(status_code=429, detail=_GENERIC_THROTTLED)
        if reason == "not_configured":
            ref = secrets.token_hex(4)
            print("[2FA] otp-request refused: MSG91 widget not configured ref=" + ref)
            raise HTTPException(status_code=503, detail=_GENERIC_SEND + " (ref " + ref + ")")
        raise HTTPException(status_code=401, detail="This sign-in has expired. Please sign in again.")
    except HTTPException:
        raise
    except Exception as e:
        raise _failure("otp-request", e)


@router.post("/otp-failed")
async def otp_failed(data: dict, request: Request):
    """Widget mode: the page reports that the widget rejected a typed code. This can ONLY use up attempts (5 locks the challenge); it can never
    grant anything, so it is safe to accept from an untrusted browser. Returns the attempts left."""
    try:
        ip = _widget_limits(request)
        cid = data.get("challenge_id") if isinstance(data, dict) else None
        if _otp.throttle_hit("verify_ip", ip, 15) > _otp.IP_VERIFY_LIMIT:
            raise HTTPException(status_code=429, detail=_GENERIC_THROTTLED)
        result = _otp.report_failed_code(cid if isinstance(cid, str) else "")
        if result.get("ok") is False and result.get("reason") == "invalid" and "remaining" in result:
            return {"remaining": result["remaining"]}
        raise HTTPException(status_code=401, detail="This sign-in has expired. Please sign in again.")
    except HTTPException:
        raise
    except Exception as e:
        raise _failure("otp-failed", e)


@router.get("/device-status")
async def device_status(request: Request):
    """Lets the login page pre-tick 'Remember this device' when THIS browser already holds a valid trusted-device token. Reveals only a yes/no
    about the caller's own cookie: no account, no token, no list."""
    ok = False
    device = _td.find_valid(request.cookies.get(_td.COOKIE))
    if device:
        user = db.dashboard_users.find_one({"_id": device["user_id"]}, {"status": 1, "role_id": 1})
        ok = bool(user and user.get("status") not in ("disabled", "suspended") and _td.allowed_for(_role_name(user)))
    return JSONResponse({"remembered": ok}, headers={"Cache-Control": "no-store"})


@router.post("/resend-otp")
async def resend_otp(data: dict, request: Request):
    """Issue a new OTP for the same challenge: 30 s cooldown, at most 3 resends, shared wrong-attempt budget, per-account and per-IP
    limits. Needs the challenge from the PIN step; a mobile number alone can never request an SMS."""
    try:
        challenge_id = data.get("challenge_id") if isinstance(data, dict) else None
        ip = _client_ip(request)
        if _otp.throttle_hit("resend_ip", ip, 15) > _otp.IP_VERIFY_LIMIT:
            raise HTTPException(status_code=429, detail=_GENERIC_THROTTLED)

        result = _otp.resend_challenge(challenge_id if isinstance(challenge_id, str) else "", _mobile_for_challenge, ip, _admin_template_id())
        if result.get("ok"):
            return {"message": "A new code has been sent.", "otp_expires_in": result["expires_in"], "resend_available_in": result["resend_in"]}
        reason = result.get("reason")
        if reason == "cooldown":
            raise HTTPException(status_code=429, detail="Please wait before requesting another code.")
        if reason == "rate_limited":
            raise HTTPException(status_code=429, detail=_GENERIC_THROTTLED)
        if reason == "send_failed":
            raise HTTPException(status_code=503, detail=_GENERIC_SEND)
        raise HTTPException(status_code=401, detail="This sign-in has expired. Please sign in again.")
    except HTTPException:
        raise
    except Exception as e:
        raise _failure("resend", e)


@router.post("/logout")
async def logout(request: Request, response: Response, data: dict = None):
    """
    Logout — clears session token and cookie.
    The trusted-device cookie is preserved unless keep_trusted=false is sent.
    """
    token = request.cookies.get("admin_token") or \
            (data.get("token", "") if data else "") or \
            request.headers.get("Authorization", "").replace("Bearer ", "")

    if token:
        user = db.dashboard_users.find_one({"token": token})
        if user:
            db.dashboard_users.update_one({"_id": user["_id"]}, {"$set": {"token": None}})
            log_activity(request, user["_id"], user.get("full_name", ""), user.get("mobile", ""),
                         "Auth", "LOGOUT")

    # Clear session cookie
    response.delete_cookie("admin_token", samesite="Lax")

    # "Log out and forget this device": keep_trusted=false revokes THIS browser's trusted-device token (by hash) and clears its cookie.
    # A plain logout keeps the device trusted (PIN is still required next time). Logout never touches any failure counter.
    if data and data.get("keep_trusted") is False:
        _td.revoke_token(request.cookies.get(_td.COOKIE))
        _clear_device_cookie(response)
        _clear_trusted_cookie(response)

    return {"message": "Logged out successfully"}


@router.get("/me")
async def get_current_user_info(user: dict = Depends(get_current_dashboard_user)):
    """Get current authenticated user's full profile + permissions."""
    role = user.get("role", {})
    return {
        "id": str(user["_id"]),
        "full_name": user["full_name"],
        "mobile": user["mobile"],
        "email": user.get("email"),
        "designation": user.get("designation", ""),
        "profile_photo_url": user.get("profile_photo_url"),
        "role": role.get("role_name", "Unknown"),
        "permissions": role.get("permissions", {}),
        "assigned_cities": user.get("assigned_cities", [])
    }


# ═══════════════════════════════════════════════════════════════
# TRUSTED DEVICES MANAGEMENT (for future security dashboard)
# ═══════════════════════════════════════════════════════════════

@router.get("/trusted-devices")
async def list_trusted_devices(
    request: Request,
    user: dict = Depends(get_current_dashboard_user)
):
    """List the current user's trusted devices. Never returns a token or its hash; `current` marks this browser."""
    current = _td.find_valid(request.cookies.get(_td.COOKIE), user["_id"])
    devices = list(db[_td.COLLECTION].find({
        "user_id": user["_id"],
        "expires_at": {"$gt": datetime.utcnow()}
    }).sort("created_at", -1))

    return [{
        "id": str(d["_id"]),
        "current": bool(current and current["_id"] == d["_id"]),
        "ip_address": d.get("ip_address", ""),
        "user_agent": d.get("user_agent", ""),
        "created_at": d.get("created_at").isoformat() if d.get("created_at") else None,
        "expires_at": d.get("expires_at").isoformat() if d.get("expires_at") else None,
        "last_used_at": d.get("last_used_at").isoformat() if d.get("last_used_at") else None,
    } for d in devices]


@router.delete("/trusted-devices/{device_id}")
async def revoke_trusted_device(
    device_id: str,
    request: Request,
    user: dict = Depends(get_current_dashboard_user)
):
    """Revoke one of the current user's trusted devices by its ID (that browser needs the OTP again)."""
    if not ObjectId.is_valid(device_id):
        raise HTTPException(status_code=400, detail="Invalid device ID")

    device = db[_td.COLLECTION].find_one({"_id": ObjectId(device_id), "user_id": user["_id"]})
    if not device:
        raise HTTPException(status_code=404, detail="Trusted device not found")

    db[_td.COLLECTION].delete_one({"_id": ObjectId(device_id)})

    log_activity(
        request, user["_id"], user["full_name"], user["mobile"],
        "Auth", "REVOKE_TRUSTED_DEVICE",
        record_id=device_id,
        record_name=device.get("user_agent", "")[:50]
    )

    return {"message": "Trusted device revoked"}


@router.delete("/trusted-devices")
async def revoke_all_trusted_devices(
    request: Request,
    user: dict = Depends(get_current_dashboard_user)
):
    """Revoke all of the current user's trusted devices."""
    count = _td.revoke_all(user["_id"])

    log_activity(
        request, user["_id"], user["full_name"], user["mobile"],
        "Auth", "REVOKE_ALL_TRUSTED_DEVICES",
        record_name=str(count) + " devices"
    )

    return {"message": "Revoked " + str(count) + " trusted device(s)"}


# ═══════════════════════════════════════════════════════════════
# DASHBOARD USERS CRUD
# ═══════════════════════════════════════════════════════════════

def _serialize_user(u: dict) -> dict:
    return {
        "id": str(u["_id"]),
        "full_name": u.get("full_name", ""),
        "mobile": u.get("mobile", ""),
        "email": u.get("email"),
        "designation": u.get("designation", ""),
        "profile_photo_url": u.get("profile_photo_url"),
        "role_id": str(u["role_id"]) if u.get("role_id") else None,
        "role_name": u.get("role_name", "Unassigned"),
        "status": u.get("status", "active"),
        "assigned_cities": u.get("assigned_cities", []),
        "last_login_at": u.get("last_login_at").isoformat() if u.get("last_login_at") else None,
        "created_at": u.get("created_at").isoformat() if u.get("created_at") else None,
    }


@router.get("/users")
async def list_users(
    request: Request,
    user: dict = Depends(require_permission("User Management", "view"))
):
    users = list(db.dashboard_users.find({}, {"pin": 0}).sort("created_at", -1))
    for u in users:
        role = db.dashboard_roles.find_one({"_id": u["role_id"]}) if u.get("role_id") else None
        u["role_name"] = role["role_name"] if role else "Unassigned"
    return [_serialize_user(u) for u in users]


@router.post("/users")
async def create_user(
    data: dict,
    request: Request,
    user: dict = Depends(require_permission("User Management", "add"))
):
    full_name = str(data.get("full_name", "")).strip()
    mobile = str(data.get("mobile", "")).strip()
    pin = str(data.get("pin", "")).strip()
    designation = str(data.get("designation", "")).strip()
    role_id = str(data.get("role_id", "")).strip()
    assigned_cities = data.get("assigned_cities", [])
    email = data.get("email") or None
    profile_photo_url = data.get("profile_photo_url") or None

    if not full_name or not mobile or not pin or not role_id:
        raise HTTPException(status_code=400, detail="full_name, mobile, pin, and role_id are required")

    if not (pin.isdigit() and len(pin) in (4, 6)):
        raise HTTPException(status_code=400, detail="PIN must be 4 or 6 digits")

    if db.dashboard_users.find_one({"mobile": mobile}):
        raise HTTPException(status_code=409, detail="Mobile number already registered")

    if not db.dashboard_roles.find_one({"_id": ObjectId(role_id)}):
        raise HTTPException(status_code=400, detail="Invalid role_id")

    doc = {
        "full_name": full_name,
        "mobile": mobile,
        "pin": hash_pin(pin),
        "email": email,
        "designation": designation,
        "profile_photo_url": profile_photo_url,
        "role_id": ObjectId(role_id),
        "assigned_cities": assigned_cities if assigned_cities else [],
        "status": "active",
        "login_attempts": 0,
        "lockout_until": None,
        "last_login_at": None,
        "last_active_at": None,
        "token": None,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }

    result = db.dashboard_users.insert_one(doc)
    doc["_id"] = result.inserted_id

    log_activity(
        request, user["_id"], user["full_name"], user["mobile"],
        "User Management", "ADD",
        record_id=str(result.inserted_id),
        record_name=full_name,
        after_value={"full_name": full_name, "mobile": mobile, "role_id": role_id, "assigned_cities": assigned_cities}
    )

    return {"message": "User created", "id": str(result.inserted_id)}


@router.put("/users/{user_id}")
async def update_user(
    user_id: str,
    data: dict,
    request: Request,
    user: dict = Depends(require_permission("User Management", "edit"))
):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    existing = db.dashboard_users.find_one({"_id": ObjectId(user_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    updates = {}
    for field in ("full_name", "email", "designation", "profile_photo_url", "status", "assigned_cities"):
        if field in data:
            updates[field] = data[field]

    if "mobile" in data and str(data["mobile"]).strip():
        new_mobile = str(data["mobile"]).strip()
        if not (new_mobile.isdigit() and len(new_mobile) == 10):
            raise HTTPException(status_code=400, detail="Mobile number must be exactly 10 digits")
        conflict = db.dashboard_users.find_one({"mobile": new_mobile, "_id": {"$ne": ObjectId(user_id)}})
        if conflict:
            raise HTTPException(status_code=409, detail="Mobile number already in use by another user")
        updates["mobile"] = new_mobile

    if "role_id" in data and data["role_id"]:
        if not db.dashboard_roles.find_one({"_id": ObjectId(data["role_id"])}):
            raise HTTPException(status_code=400, detail="Invalid role_id")
        updates["role_id"] = ObjectId(data["role_id"])

    if "pin" in data and str(data.get("pin", "")).strip():
        new_pin = str(data["pin"]).strip()
        if not (new_pin.isdigit() and len(new_pin) in (4, 6)):
            raise HTTPException(status_code=400, detail="PIN must be 4 or 6 digits")
        updates["pin"] = hash_pin(new_pin)
        updates["login_attempts"] = 0
        updates["lockout_until"] = None

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updates["updated_at"] = datetime.utcnow()
    # Security-sensitive change (PIN, mobile, role, deactivation/suspension): every trusted device of this account stops working.
    revoke_devices = any(k in updates for k in ("pin", "mobile", "role_id")) or (updates.get("status") not in (None, "active"))

    before_snapshot = {
        "full_name": existing.get("full_name"),
        "email": existing.get("email"),
        "designation": existing.get("designation"),
        "status": existing.get("status"),
        "assigned_cities": existing.get("assigned_cities"),
        "role_id": str(existing.get("role_id")) if existing.get("role_id") else None
    }

    db.dashboard_users.update_one({"_id": ObjectId(user_id)}, {"$set": updates})
    if revoke_devices:
        _td.revoke_all(ObjectId(user_id))
    if "pin" in updates:                                             # an administrator reset: forgive wrong-PIN / wrong-code counters too
        _lim.clear_account(existing.get("mobile", ""))

    log_activity(
        request, user["_id"], user["full_name"], user["mobile"],
        "User Management", "EDIT",
        record_id=user_id,
        record_name=existing.get("full_name", ""),
        before_value=before_snapshot,
        after_value=updates
    )

    return {"message": "User updated"}


@router.put("/users/{user_id}/reset-pin")
async def reset_pin(
    user_id: str,
    data: dict,
    request: Request,
    user: dict = Depends(require_permission("User Management", "edit"))
):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    new_pin = str(data.get("new_pin", "")).strip()
    if not (new_pin.isdigit() and len(new_pin) in (4, 6)):
        raise HTTPException(status_code=400, detail="PIN must be 4 or 6 digits")

    existing = db.dashboard_users.find_one({"_id": ObjectId(user_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    db.dashboard_users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"pin": hash_pin(new_pin), "updated_at": datetime.utcnow(),
                  "login_attempts": 0, "lockout_until": None}}
    )
    _td.revoke_all(ObjectId(user_id))                                # a new PIN invalidates every remembered browser
    _lim.clear_account(existing.get("mobile", ""))

    log_activity(
        request, user["_id"], user["full_name"], user["mobile"],
        "User Management", "RESET_PIN",
        record_id=user_id,
        record_name=existing.get("full_name", "")
    )

    return {"message": "PIN reset successful"}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    request: Request,
    user: dict = Depends(require_permission("User Management", "delete"))
):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    existing = db.dashboard_users.find_one({"_id": ObjectId(user_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    super_role = db.dashboard_roles.find_one({"role_name": "Super Admin"})
    if super_role and existing.get("role_id") == super_role["_id"]:
        super_admin_count = db.dashboard_users.count_documents({
            "role_id": super_role["_id"], "status": "active"
        })
        if super_admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last Super Admin")

    # Also revoke all trusted devices for this user (current collection; the old one is cleaned the same way)
    _td.revoke_all(ObjectId(user_id))
    db.trusted_devices.delete_many({"user_id": ObjectId(user_id)})

    db.dashboard_users.delete_one({"_id": ObjectId(user_id)})

    log_activity(
        request, user["_id"], user["full_name"], user["mobile"],
        "User Management", "DELETE",
        record_id=user_id,
        record_name=existing.get("full_name", "")
    )

    return {"message": "User deleted"}


# ═══════════════════════════════════════════════════════════════
# ROLES & PERMISSIONS CRUD
# ═══════════════════════════════════════════════════════════════

def _serialize_role(r: dict) -> dict:
    return {
        "id": str(r["_id"]),
        "role_name": r.get("role_name", ""),
        "description": r.get("description", ""),
        "permissions": r.get("permissions", {}),
        "is_system_role": r.get("is_system_role", False),
        "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
    }


@router.get("/roles")
async def list_roles(
    user: dict = Depends(require_permission("User Management", "view"))
):
    roles = list(db.dashboard_roles.find().sort("created_at", 1))
    return [_serialize_role(r) for r in roles]


@router.post("/roles")
async def create_role(
    data: dict,
    request: Request,
    user: dict = Depends(require_permission("User Management", "edit"))
):
    role_name = str(data.get("role_name", "")).strip()
    description = str(data.get("description", "")).strip()
    permissions = data.get("permissions", {})

    if not role_name:
        raise HTTPException(status_code=400, detail="role_name is required")

    if db.dashboard_roles.find_one({"role_name": {"$regex": "^" + role_name + "$", "$options": "i"}}):
        raise HTTPException(status_code=409, detail="Role name already exists")

    validated_perms = {}
    for mod in ALL_MODULES:
        mod_perms = permissions.get(mod, {})
        validated_perms[mod] = {
            act: bool(mod_perms.get(act, False)) for act in ALL_ACTIONS
        }

    doc = {
        "role_name": role_name,
        "description": description,
        "permissions": validated_perms,
        "is_system_role": False,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }

    result = db.dashboard_roles.insert_one(doc)
    doc["_id"] = result.inserted_id

    log_activity(
        request, user["_id"], user["full_name"], user["mobile"],
        "User Management", "ADD_ROLE",
        record_id=str(result.inserted_id),
        record_name=role_name
    )

    return {"message": "Role created", "id": str(result.inserted_id)}


@router.put("/roles/{role_id}")
async def update_role(
    role_id: str,
    data: dict,
    request: Request,
    user: dict = Depends(require_permission("User Management", "edit"))
):
    if not ObjectId.is_valid(role_id):
        raise HTTPException(status_code=400, detail="Invalid role ID")

    existing = db.dashboard_roles.find_one({"_id": ObjectId(role_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Role not found")

    updates = {}
    if "role_name" in data:
        new_name = str(data["role_name"]).strip()
        if new_name and new_name != existing["role_name"]:
            if db.dashboard_roles.find_one({"role_name": {"$regex": "^" + new_name + "$", "$options": "i"}}):
                raise HTTPException(status_code=409, detail="Role name already exists")
            updates["role_name"] = new_name

    if "description" in data:
        updates["description"] = str(data["description"]).strip()

    if "permissions" in data:
        validated_perms = {}
        for mod in ALL_MODULES:
            mod_perms = data["permissions"].get(mod, {})
            validated_perms[mod] = {
                act: bool(mod_perms.get(act, False)) for act in ALL_ACTIONS
            }
        updates["permissions"] = validated_perms

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updates["updated_at"] = datetime.utcnow()

    before_snapshot = {
        "role_name": existing.get("role_name"),
        "description": existing.get("description"),
        "permissions": existing.get("permissions")
    }

    db.dashboard_roles.update_one({"_id": ObjectId(role_id)}, {"$set": updates})

    log_activity(
        request, user["_id"], user["full_name"], user["mobile"],
        "User Management", "EDIT_ROLE",
        record_id=role_id,
        record_name=existing.get("role_name", ""),
        before_value=before_snapshot,
        after_value=updates
    )

    return {"message": "Role updated"}


@router.delete("/roles/{role_id}")
async def delete_role(
    role_id: str,
    request: Request,
    user: dict = Depends(require_permission("User Management", "edit"))
):
    if not ObjectId.is_valid(role_id):
        raise HTTPException(status_code=400, detail="Invalid role ID")

    existing = db.dashboard_roles.find_one({"_id": ObjectId(role_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Role not found")

    if existing.get("is_system_role"):
        raise HTTPException(status_code=400, detail="Cannot delete a system role")

    user_count = db.dashboard_users.count_documents({"role_id": ObjectId(role_id)})
    if user_count > 0:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete role — " + str(user_count) + " user(s) are assigned to it. Reassign them first."
        )

    db.dashboard_roles.delete_one({"_id": ObjectId(role_id)})

    log_activity(
        request, user["_id"], user["full_name"], user["mobile"],
        "User Management", "DELETE_ROLE",
        record_id=role_id,
        record_name=existing.get("role_name", "")
    )

    return {"message": "Role deleted"}


# ═══════════════════════════════════════════════════════════════
# ACTIVITY LOGS
# ═══════════════════════════════════════════════════════════════

@router.get("/activity-logs")
async def list_activity_logs(
    request: Request,
    skip: int = 0,
    limit: int = 50,
    user_name: str = "",
    module: str = "",
    action: str = "",
    city: str = "",
    date_from: str = "",
    date_to: str = "",
    user: dict = Depends(get_current_dashboard_user)
):
    query = {}

    role_name = user.get("role", {}).get("role_name", "")
    assigned_cities = user.get("assigned_cities", [])
    if role_name != "Super Admin" and "*" not in assigned_cities:
        if assigned_cities:
            query["city"] = {"$in": assigned_cities}

    if user_name:
        query["user_name"] = {"$regex": user_name, "$options": "i"}
    if module:
        query["module"] = {"$regex": module, "$options": "i"}
    if action:
        query["action"] = {"$regex": action, "$options": "i"}
    if city:
        query["city"] = {"$regex": city, "$options": "i"}

    if date_from:
        try:
            query.setdefault("timestamp", {})["$gte"] = datetime.fromisoformat(date_from)
        except ValueError:
            pass
    if date_to:
        try:
            query.setdefault("timestamp", {})["$lte"] = datetime.fromisoformat(date_to)
        except ValueError:
            pass

    total = db.activity_logs.count_documents(query)
    logs = list(db.activity_logs.find(query).sort("timestamp", -1).skip(skip).limit(min(limit, 200)))

    result = []
    for log in logs:
        result.append({
            "id": str(log["_id"]),
            "timestamp": log.get("timestamp").isoformat() if log.get("timestamp") else None,
            "user_name": log.get("user_name", ""),
            "mobile": log.get("mobile", ""),
            "module": log.get("module", ""),
            "action": log.get("action", ""),
            "record_id": log.get("record_id", ""),
            "record_name": log.get("record_name", ""),
            "city": log.get("city", ""),
            "ip_address": log.get("ip_address", ""),
            "user_agent": log.get("user_agent", ""),
        })

    return {"logs": result, "total": total, "skip": skip, "limit": limit}


# ═══════════════════════════════════════════════════════════════
# HELPER: Get all available modules
# ═══════════════════════════════════════════════════════════════

@router.get("/modules")
async def list_modules(
    user: dict = Depends(get_current_dashboard_user)
):
    return {"modules": ALL_MODULES, "actions": ALL_ACTIONS}
