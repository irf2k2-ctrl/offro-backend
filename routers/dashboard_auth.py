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

# ── MSG91 SMS — same gateway as user app OTP ──
try:
    import httpx as _httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False
    import requests as _requests  # fallback — already in requirements.txt

router = APIRouter(prefix="/auth", tags=["Dashboard RBAC & Auth"])

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
INACTIVITY_TIMEOUT_MINUTES = 30          # ← was 15, now 30 per 2FA spec
SESSION_MAX_HOURS = 8
PBKDF2_ITERATIONS = 100_000

# ── 2FA / OTP settings ──
OTP_LENGTH = 4                           # 4-digit numeric OTP
OTP_EXPIRY_MINUTES = 5                   # OTP valid for 5 minutes
OTP_MAX_ATTEMPTS = 5                      # Max wrong OTP tries before requiring new one
OTP_RESEND_SECONDS = 30                   # Resend cooldown
TRUSTED_DEVICE_DAYS = 30                 # Trust this device for 30 days
TRUSTED_DEVICE_COOKIE = "offro_trusted"   # Cookie name for trusted device token

# All modules that support permissions
ALL_MODULES = [
    "Accounts", "Stores", "Products", "Banners", "Admin Banners", "Popup Campaigns",
    "Payments", "Gift Vouchers", "Notifications", "Categories", "Pricing & GST",
    "Reviews", "Discounts", "Terms & Conditions", "Policies",
    "Social Media", "Live Chat", "Default Images", "User Management"
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
# OTP HASHING (stdlib only — never store plain OTP)
# ═══════════════════════════════════════════════════════════════

def _hash_otp(otp: str) -> str:
    """Hash a 4-digit OTP using PBKDF2. Never store plain OTP."""
    salt = secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", otp.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return salt + "$" + h.hex()

def _verify_otp(plain_otp: str, stored: str) -> bool:
    """Verify OTP against stored hash."""
    try:
        salt_str, hash_str = stored.split("$", 1)
        h = hashlib.pbkdf2_hmac("sha256", plain_otp.encode(), bytes.fromhex(salt_str), PBKDF2_ITERATIONS)
        return secrets.compare_digest(h.hex(), hash_str)
    except Exception:
        return False

def _generate_otp() -> str:
    """Generate a cryptographically secure 4-digit OTP."""
    return str(secrets.randbelow(9000) + 1000)

# ═══════════════════════════════════════════════════════════════
# OTP DELIVERY — MSG91 SMS (same gateway as user app)
# ═══════════════════════════════════════════════════════════════

# MSG91 config — same env vars as otp_service.py
_MSG91_AUTH_KEY    = os.getenv("MSG91_AUTH_KEY", "").strip()
_MSG91_TEMPLATE_ID = os.getenv("MSG91_TEMPLATE_ID", "").strip()
_MSG91_SENDER_ID   = os.getenv("MSG91_SENDER_ID", "OFFROO").strip()
_MSG91_URL         = "https://control.msg91.com/api/v5/otp"

# Log MSG91 config on import
print("[2FA] ═══════════════════════════════════════")
print("[2FA] MSG91_AUTH_KEY    : " + ("SET (" + str(len(_MSG91_AUTH_KEY)) + " chars)" if _MSG91_AUTH_KEY else "❌ NOT SET — OTPs will be dev-console only"))
print("[2FA] MSG91_TEMPLATE_ID : " + (_MSG91_TEMPLATE_ID if _MSG91_TEMPLATE_ID else "❌ NOT SET"))
print("[2FA] MSG91_SENDER_ID   : " + _MSG91_SENDER_ID)
print("[2FA] Mode              : " + ("🟢 LIVE (MSG91 SMS)" if _MSG91_AUTH_KEY and _MSG91_TEMPLATE_ID else "🟡 DEV (check server logs for OTP)"))
print("[2FA] ═══════════════════════════════════════")


def _send_otp_to_mobile(mobile: str, otp: str) -> dict:
    """
    Send admin 2FA OTP via MSG91 SMS — same gateway as user app.
    Falls back to console log in dev mode (if MSG91 keys not set).
    Returns {"ok": bool, "method": str, "error": str?}
    """
    # Normalise to E.164 for MSG91 (91XXXXXXXXXX)
    phone = mobile.replace("+", "").replace(" ", "").replace("-", "")
    if not phone.startswith("91") and len(phone) == 10:
        phone = "91" + phone

    print("[2FA] ── _send_otp_to_mobile ────────────────")
    print("[2FA] target: " + phone + " | otp_len: " + str(len(otp)))

    # ── Dev mode (no MSG91 keys) ──
    if not _MSG91_AUTH_KEY or not _MSG91_TEMPLATE_ID:
        print("[2FA] 🟡 DEV MODE — MSG91 keys not set. OTP=" + otp + " for " + mobile)
        return {"ok": True, "method": "dev_console"}

    # ── MSG91 SMS (same payload as otp_service.py) ──
    payload = {
        "template_id": _MSG91_TEMPLATE_ID,
        "mobile":      phone,
        "authkey":     _MSG91_AUTH_KEY,
        "otp":         otp,
        "otp_expiry":  OTP_EXPIRY_MINUTES,
        "sender":      _MSG91_SENDER_ID,
    }
    safe_key = _MSG91_AUTH_KEY[:6] + "..." + _MSG91_AUTH_KEY[-4:] if len(_MSG91_AUTH_KEY) > 10 else "***"
    print("[2FA] MSG91 payload: template_id=" + _MSG91_TEMPLATE_ID + " | mobile=" + phone + " | sender=" + _MSG91_SENDER_ID + " | authkey=" + safe_key)

    try:
        if _HTTPX_AVAILABLE:
            resp = _httpx.post(_MSG91_URL, json=payload,
                               headers={"Content-Type": "application/json"}, timeout=15.0)
        else:
            resp = _requests.post(_MSG91_URL, json=payload,
                                  headers={"Content-Type": "application/json"}, timeout=15)

        print("[2FA] MSG91 HTTP status : " + str(resp.status_code))
        print("[2FA] MSG91 response    : " + resp.text)

        try:
            data = resp.json()
        except Exception:
            return {"ok": False, "method": "sms", "error": "MSG91 non-JSON response: " + resp.text[:100]}

        msg_type = data.get("type", "")
        msg_msg  = data.get("message", data.get("msg", ""))

        if resp.status_code == 200 and msg_type == "success":
            print("[2FA] ✅ OTP SMS sent via MSG91 to " + phone)
            return {"ok": True, "method": "sms"}

        print("[2FA] ❌ MSG91 FAILED — type='" + str(msg_type) + "' message='" + str(msg_msg) + "'")
        return {"ok": False, "method": "sms", "error": "MSG91: " + str(msg_msg or msg_type or "Unknown")}

    except Exception as e:
        print("[2FA] ❌ MSG91 exception: " + str(type(e).__name__) + ": " + str(e))
        return {"ok": False, "method": "sms", "error": "SMS delivery failed: " + str(e)}

# ═══════════════════════════════════════════════════════════════
# TRUSTED DEVICE MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def _create_trusted_device(user_id, mobile: str, request: Request) -> str:
    """Create a trusted device record and return the token."""
    token = secrets.token_urlsafe(48)
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown") if request else "unknown"

    doc = {
        "token": token,
        "user_id": user_id,
        "mobile": mobile,
        "ip_address": ip,
        "user_agent": ua,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(days=TRUSTED_DEVICE_DAYS),
        "last_used_at": datetime.utcnow(),
    }
    db.trusted_devices.insert_one(doc)
    return token

def _verify_trusted_device(request: Request, user_id) -> Optional[str]:
    """
    Check if the request has a valid trusted device cookie.
    Returns the device token if valid, None otherwise.
    """
    token = request.cookies.get(TRUSTED_DEVICE_COOKIE)
    if not token:
        return None

    device = db.trusted_devices.find_one({
        "token": token,
        "user_id": user_id,
        "expires_at": {"$gt": datetime.utcnow()}
    })
    if not device:
        return None

    # Update last used
    db.trusted_devices.update_one(
        {"_id": device["_id"]},
        {"$set": {"last_used_at": datetime.utcnow()}}
    )
    return token

def _set_trusted_cookie(response: Response, token: str):
    """Set the trusted device cookie with 30-day expiry, HttpOnly + SameSite."""
    response.set_cookie(
        key=TRUSTED_DEVICE_COOKIE,
        value=token,
        httponly=True,
        samesite="Lax",
        secure=False,   # Set True in production HTTPS
        max_age=86400 * TRUSTED_DEVICE_DAYS   # 30 days
    )

def _clear_trusted_cookie(response: Response):
    """Clear the trusted device cookie."""
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
    Checks dashboard_users first (new RBAC login), then falls back to
    db.admins (old legacy login) and treats that session as Super Admin.
    """
    token = request.cookies.get("admin_token") or \
            request.headers.get("Authorization", "").replace("Bearer ", "")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = db.dashboard_users.find_one({"token": token})

    # ── Legacy fallback ──
    if not user:
        legacy = db.admins.find_one({"token": token})
        if legacy:
            super_role = db.dashboard_roles.find_one({"role_name": "Super Admin"}) or {}
            return {
                "_id": legacy["_id"],
                "full_name": legacy.get("username", "Admin"),
                "mobile": "legacy",
                "email": None,
                "designation": "Super Administrator (Legacy)",
                "profile_photo_url": None,
                "role_id": super_role.get("_id"),
                "assigned_cities": ["*"],
                "status": "active",
                "role": super_role or {"role_name": "Super Admin", "permissions": {
                    mod: {act: True for act in ["view","add","edit","delete","approve","export"]}
                    for mod in ["Accounts","Stores","Products","Banners","Admin Banners","Popup Campaigns",
                                "Payments","Gift Vouchers","Notifications","Categories","Pricing & GST",
                                "Reviews","Discounts","Terms & Conditions","Policies",
                                "Social Media","Live Chat","Default Images","User Management"]
                }}
            }
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
    """Seed default roles and a Super Admin user if they don't exist."""
    # ── 1. Seed default roles ──
    if db.dashboard_roles.count_documents({}) == 0:
        super_admin_perms = {}
        for mod in ALL_MODULES:
            super_admin_perms[mod] = {act: True for act in ALL_ACTIONS}

        admin_perms = {}
        for mod in ALL_MODULES:
            admin_perms[mod] = {act: True for act in ALL_ACTIONS}
        admin_perms["User Management"] = {act: (act == "view") for act in ALL_ACTIONS}

        readonly_perms = {}
        for mod in ALL_MODULES:
            readonly_perms[mod] = {act: (act == "view") for act in ALL_ACTIONS}

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

    # ── 2. Seed Super Admin user ──
    if db.dashboard_users.count_documents({}) == 0:
        super_role = db.dashboard_roles.find_one({"role_name": "Super Admin"})
        if super_role:
            default_mobile = os.getenv("RBAC_SUPER_ADMIN_MOBILE", "9999999999")
            default_pin = os.getenv("RBAC_SUPER_ADMIN_PIN", "123456")

            db.dashboard_users.insert_one({
                "full_name": "Super Admin",
                "mobile": default_mobile,
                "pin": hash_pin(default_pin),
                "email": None,
                "designation": "Super Administrator",
                "profile_photo_url": None,
                "role_id": super_role["_id"],
                "assigned_cities": ["*"],
                "status": "active",
                "login_attempts": 0,
                "lockout_until": None,
                "last_login_at": None,
                "last_active_at": None,
                "token": None,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            })
            print("[RBAC] Seeded Super Admin user — Mobile: " + default_mobile + " PIN: " + default_pin)

    # ── 3. Create indexes ──
    try:
        db.dashboard_users.create_index("mobile", unique=True)
        db.dashboard_users.create_index("token", sparse=True)
        db.dashboard_roles.create_index("role_name", unique=True)
        db.activity_logs.create_index([("timestamp", -1)])
        db.activity_logs.create_index("user_id")
        db.activity_logs.create_index("module")
        # 2FA collections
        db.otp_store.create_index("mobile", expireAfterSeconds=OTP_EXPIRY_MINUTES * 60)
        db.otp_store.create_index("created_at")
        db.trusted_devices.create_index("token", unique=True)
        db.trusted_devices.create_index("user_id")
        db.trusted_devices.create_index("expires_at")
    except Exception as e:
        print("[RBAC] Index creation (may already exist): " + str(e))


# ═══════════════════════════════════════════════════════════════
# 2FA AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.post("/login")
async def login(data: dict, request: Request, response: Response):
    """
    Step 1: Login with mobile + PIN.
    - If trusted device cookie is valid → skip OTP, return session directly.
    - Otherwise → generate OTP, send it, return otp_required: true.
    """
    mobile = str(data.get("mobile", "")).strip()
    pin = str(data.get("pin", "")).strip()

    if not mobile or not pin:
        raise HTTPException(status_code=400, detail="Mobile and PIN are required")

    user = db.dashboard_users.find_one({"mobile": mobile})

    if not user:
        log_activity(request, None, "Unknown", mobile, "Auth", "LOGIN_FAIL", record_name="Account not found")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Check lockout
    lockout_until = user.get("lockout_until")
    if lockout_until and lockout_until > datetime.utcnow():
        remaining = int((lockout_until - datetime.utcnow()).total_seconds() / 60) + 1
        raise HTTPException(
            status_code=423,
            detail="Account locked. Try again in " + str(remaining) + " min."
        )

    # Check account status
    if user.get("status") in ("disabled", "suspended"):
        log_activity(request, user["_id"], user["full_name"], mobile, "Auth", "LOGIN_FAIL", record_name="Account " + user.get("status", ""))
        raise HTTPException(status_code=403, detail="Account is " + user.get("status", "disabled"))

    # Verify PIN
    if not verify_pin(pin, user.get("pin", "")):
        new_attempts = user.get("login_attempts", 0) + 1
        updates = {"login_attempts": new_attempts}

        if new_attempts >= MAX_LOGIN_ATTEMPTS:
            updates["lockout_until"] = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
            log_activity(request, user["_id"], user["full_name"], mobile, "Auth", "LOCKOUT")
        else:
            log_activity(request, user["_id"], user["full_name"], mobile, "Auth", "LOGIN_FAIL",
                         record_name="Attempt " + str(new_attempts) + "/" + str(MAX_LOGIN_ATTEMPTS))

        db.dashboard_users.update_one({"_id": user["_id"]}, {"$set": updates})
        remaining_attempts = MAX_LOGIN_ATTEMPTS - new_attempts
        if remaining_attempts > 0:
            raise HTTPException(status_code=401,
                detail="Invalid PIN. " + str(remaining_attempts) + " attempts remaining.")
        else:
            raise HTTPException(status_code=401, detail="Invalid PIN. Account is now locked.")

    # ── PIN verified successfully ──

    # Check trusted device → skip OTP
    trusted_token = _verify_trusted_device(request, user["_id"])
    if trusted_token:
        # Trusted device — skip OTP, issue session directly
        session_token = secrets.token_urlsafe(32)
        db.dashboard_users.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "token": session_token,
                "login_attempts": 0,
                "lockout_until": None,
                "last_login_at": datetime.utcnow(),
                "last_active_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            }}
        )

        log_activity(request, user["_id"], user["full_name"], mobile, "Auth", "LOGIN_SUCCESS",
                     record_name="Trusted device — OTP skipped")

        role = db.dashboard_roles.find_one({"_id": user["role_id"]}) if user.get("role_id") else None
        role_name = role["role_name"] if role else "Unknown"

        response.set_cookie(
            key="admin_token",
            value=session_token,
            httponly=True,
            samesite="Lax",
            secure=False,
            max_age=3600 * SESSION_MAX_HOURS
        )

        return {
            "message": "Login successful (trusted device)",
            "otp_required": False,
            "user": {
                "id": str(user["_id"]),
                "full_name": user["full_name"],
                "mobile": user["mobile"],
                "designation": user.get("designation", ""),
                "role": role_name
            }
        }

    # ── Not a trusted device → generate OTP ──
    otp = _generate_otp()
    otp_hash = _hash_otp(otp)

    # Store hashed OTP (replace any previous)
    db.otp_store.delete_many({"mobile": mobile})
    db.otp_store.insert_one({
        "mobile": mobile,
        "user_id": user["_id"],
        "otp_hash": otp_hash,
        "attempts": 0,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINUTES),
        "resend_available_at": datetime.utcnow() + timedelta(seconds=OTP_RESEND_SECONDS),
    })

    # Send OTP
    send_result = _send_otp_to_mobile(mobile, otp)
    send_ok = send_result.get("ok", False)
    send_method = send_result.get("method", "unknown")

    log_activity(request, user["_id"], user["full_name"], mobile, "Auth", "OTP_SENT",
                 record_name="Method: " + send_method)

    if not send_ok:
        # OTP couldn't be delivered — but we still stored it. Let user know.
        return {
            "otp_required": True,
            "mobile": mobile,
            "otp_expires_in": OTP_EXPIRY_MINUTES * 60,
            "warning": "OTP could not be sent via " + send_method + ". Contact admin.",
            "resend_available_in": OTP_RESEND_SECONDS
        }

    return {
        "otp_required": True,
        "mobile": mobile,
        "otp_expires_in": OTP_EXPIRY_MINUTES * 60,
        "otp_sent_via": send_method,
        "resend_available_in": OTP_RESEND_SECONDS
    }


@router.post("/verify-otp")
async def verify_otp(data: dict, request: Request, response: Response):
    """
    Step 2: Verify the 4-digit OTP.
    - On success: set session cookie + optional trusted device cookie.
    - On failure: track attempts, lock after 5 wrong tries.
    """
    mobile = str(data.get("mobile", "")).strip()
    otp = str(data.get("otp", "")).strip()
    trust_device = bool(data.get("trust_device", False))

    if not mobile or not otp:
        raise HTTPException(status_code=400, detail="Mobile and OTP are required")

    if not (otp.isdigit() and len(otp) == OTP_LENGTH):
        raise HTTPException(status_code=400, detail="OTP must be " + str(OTP_LENGTH) + " digits")

    # Find the stored OTP
    stored = db.otp_store.find_one({"mobile": mobile})
    if not stored:
        raise HTTPException(status_code=400, detail="No OTP found. Please request a new one.")

    # Check expiry
    if stored.get("expires_at", datetime.utcnow()) < datetime.utcnow():
        db.otp_store.delete_one({"_id": stored["_id"]})
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

    # Check max attempts
    attempts = stored.get("attempts", 0)
    if attempts >= OTP_MAX_ATTEMPTS:
        db.otp_store.delete_one({"_id": stored["_id"]})
        raise HTTPException(status_code=400, detail="Too many incorrect attempts. Please request a new OTP.")

    # Verify OTP
    if not _verify_otp(otp, stored["otp_hash"]):
        new_attempts = attempts + 1
        db.otp_store.update_one(
            {"_id": stored["_id"]},
            {"$set": {"attempts": new_attempts}}
        )

        user = db.dashboard_users.find_one({"mobile": mobile})
        if user:
            log_activity(request, user["_id"], user.get("full_name", ""), mobile,
                         "Auth", "OTP_FAIL", record_name="Attempt " + str(new_attempts) + "/" + str(OTP_MAX_ATTEMPTS))

        remaining = OTP_MAX_ATTEMPTS - new_attempts
        if remaining > 0:
            raise HTTPException(status_code=401,
                detail="Invalid OTP. " + str(remaining) + " attempts remaining.")
        else:
            db.otp_store.delete_one({"_id": stored["_id"]})
            raise HTTPException(status_code=401, detail="Too many incorrect attempts. Please request a new OTP.")

    # ── OTP verified successfully ──
    db.otp_store.delete_one({"_id": stored["_id"]})

    user = db.dashboard_users.find_one({"mobile": mobile})
    if not user:
        raise HTTPException(status_code=401, detail="User account not found")

    # Issue session token
    session_token = secrets.token_urlsafe(32)
    db.dashboard_users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "token": session_token,
            "login_attempts": 0,
            "lockout_until": None,
            "last_login_at": datetime.utcnow(),
            "last_active_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }}
    )

    # Set session cookie
    response.set_cookie(
        key="admin_token",
        value=session_token,
        httponly=True,
        samesite="Lax",
        secure=False,
        max_age=3600 * SESSION_MAX_HOURS
    )

    # Set trusted device cookie if requested
    if trust_device:
        trusted_token = _create_trusted_device(user["_id"], mobile, request)
        _set_trusted_cookie(response, trusted_token)
        log_activity(request, user["_id"], user["full_name"], mobile, "Auth", "OTP_VERIFIED",
                     record_name="Trusted device enabled (30 days)")
    else:
        log_activity(request, user["_id"], user["full_name"], mobile, "Auth", "OTP_VERIFIED")

    role = db.dashboard_roles.find_one({"_id": user["role_id"]}) if user.get("role_id") else None
    role_name = role["role_name"] if role else "Unknown"

    return {
        "message": "Login successful",
        "otp_required": False,
        "user": {
            "id": str(user["_id"]),
            "full_name": user["full_name"],
            "mobile": user["mobile"],
            "designation": user.get("designation", ""),
            "role": role_name
        }
    }


@router.post("/resend-otp")
async def resend_otp(data: dict, request: Request, response: Response):
    """
    Resend OTP — rate-limited to 30 seconds between resends.
    Generates a fresh OTP and updates the stored hash.
    """
    mobile = str(data.get("mobile", "")).strip()

    if not mobile:
        raise HTTPException(status_code=400, detail="Mobile number is required")

    stored = db.otp_store.find_one({"mobile": mobile})
    if not stored:
        raise HTTPException(status_code=400, detail="No pending OTP. Please login again.")

    # Check resend cooldown
    resend_at = stored.get("resend_available_at", datetime.utcnow())
    if resend_at > datetime.utcnow():
        wait_seconds = int((resend_at - datetime.utcnow()).total_seconds()) + 1
        raise HTTPException(status_code=429, detail="Please wait " + str(wait_seconds) + " seconds before resending.")

    # Generate new OTP
    otp = _generate_otp()
    otp_hash = _hash_otp(otp)

    db.otp_store.update_one(
        {"_id": stored["_id"]},
        {"$set": {
            "otp_hash": otp_hash,
            "attempts": 0,
            "created_at": datetime.utcnow(),
            "expires_at": datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINUTES),
            "resend_available_at": datetime.utcnow() + timedelta(seconds=OTP_RESEND_SECONDS),
        }}
    )

    send_result = _send_otp_to_mobile(mobile, otp)
    send_method = send_result.get("method", "unknown")

    user = db.dashboard_users.find_one({"mobile": mobile})
    if user:
        log_activity(request, user["_id"], user.get("full_name", ""), mobile,
                     "Auth", "OTP_RESENT", record_name="Method: " + send_method)

    return {
        "message": "OTP resent",
        "otp_expires_in": OTP_EXPIRY_MINUTES * 60,
        "otp_sent_via": send_method,
        "resend_available_in": OTP_RESEND_SECONDS
    }


@router.post("/logout")
async def logout(request: Request, response: Response, data: dict = None):
    """
    Logout — clears session token and cookie.
    Trusted device cookie is preserved unless keep_trusted=false is sent.
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

    # Check if we should also clear trusted device
    keep_trusted = True
    if data and data.get("keep_trusted") is False:
        keep_trusted = False

    if not keep_trusted:
        trusted_token = request.cookies.get(TRUSTED_DEVICE_COOKIE)
        if trusted_token:
            db.trusted_devices.delete_one({"token": trusted_token})
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
    """List all trusted devices for the current user."""
    devices = list(db.trusted_devices.find({
        "user_id": user["_id"],
        "expires_at": {"$gt": datetime.utcnow()}
    }).sort("created_at", -1))

    return [{
        "id": str(d["_id"]),
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
    """Revoke a trusted device by its ID."""
    if not ObjectId.is_valid(device_id):
        raise HTTPException(status_code=400, detail="Invalid device ID")

    device = db.trusted_devices.find_one({"_id": ObjectId(device_id), "user_id": user["_id"]})
    if not device:
        raise HTTPException(status_code=404, detail="Trusted device not found")

    db.trusted_devices.delete_one({"_id": ObjectId(device_id)})

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
    """Revoke all trusted devices for the current user."""
    result = db.trusted_devices.delete_many({"user_id": user["_id"]})

    log_activity(
        request, user["_id"], user["full_name"], user["mobile"],
        "Auth", "REVOKE_ALL_TRUSTED_DEVICES",
        record_name=str(result.deleted_count) + " devices"
    )

    return {"message": "Revoked " + str(result.deleted_count) + " trusted device(s)"}


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

    before_snapshot = {
        "full_name": existing.get("full_name"),
        "email": existing.get("email"),
        "designation": existing.get("designation"),
        "status": existing.get("status"),
        "assigned_cities": existing.get("assigned_cities"),
        "role_id": str(existing.get("role_id")) if existing.get("role_id") else None
    }

    db.dashboard_users.update_one({"_id": ObjectId(user_id)}, {"$set": updates})

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

    # Also revoke all trusted devices for this user
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
