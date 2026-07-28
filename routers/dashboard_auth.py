# routers/dashboard_auth.py
# OffrO Admin Dashboard — Role-Based Access Control (RBAC)
# Phase 1 + 2: Collections, Auth, Security Helpers, CRUD endpoints
#
# Uses stdlib hashlib (pbkdf2_hmac) for PIN hashing — NO external deps needed.
# Mount in server.py: from routers import dashboard_auth
#                     app.include_router(dashboard_auth.router, prefix="/admin")

import os, secrets, hashlib
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
INACTIVITY_TIMEOUT_MINUTES = 15
SESSION_MAX_HOURS = 8
PBKDF2_ITERATIONS = 100_000

# All modules that support permissions — must match RBAC_MODULES in admin_dashboard.html
ALL_MODULES = [
    "Accounts", "Stores", "Products", "Banners", "Admin Banners", "Popup Campaigns",
    "Payments", "Gift Vouchers", "Notifications", "Categories", "Pricing & GST",
    "Reviews", "Discounts", "Terms & Conditions", "Policies",
    "Social Media", "Live Chat", "Default Images", "User Management"
]

# All permission actions per module
ALL_ACTIONS = ["view", "add", "edit", "delete", "approve", "export"]

# ═══════════════════════════════════════════════════════════════
# PIN HASHING (stdlib only — no passlib/bcrypt needed)
# ═══════════════════════════════════════════════════════════════

def hash_pin(pin: str) -> str:
    """Hash a 4 or 6 digit PIN using PBKDF2-HMAC-SHA256. Returns 'salt$hash'."""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return salt + "$" + h.hex()

def verify_pin(plain_pin: str, stored: str) -> bool:
    """Verify a PIN against stored 'salt$hash' string."""
    try:
        salt_str, hash_str = stored.split("$", 1)
        h = hashlib.pbkdf2_hmac("sha256", plain_pin.encode(), bytes.fromhex(salt_str), PBKDF2_ITERATIONS)
        return secrets.compare_digest(h.hex(), hash_str)
    except Exception:
        return False

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
    """Write an audit log entry to activity_logs collection."""
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
        # Auto-purge logs older than 15 days (runs inline — cheap delete, rarely hits)
        cutoff = datetime.utcnow() - timedelta(days=15)
        db.activity_logs.delete_many({"timestamp": {"$lt": cutoff}})
    except Exception as e:
        print(f"[RBAC] log_activity error: {e}")

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

    # ── Legacy fallback: old /admin/login token → treat as Super Admin ──
    if not user:
        legacy = db.admins.find_one({"token": token})
        if legacy:
            # Resolve the Super Admin role
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

    # Inactivity timeout check
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

    # Resolve role + permissions
    role = db.dashboard_roles.find_one({"_id": user["role_id"]}) if user.get("role_id") else None
    if not role:
        role = {"role_name": "Unknown", "permissions": {}}
    user["role"] = role
    return user


def require_permission(module: str, action: str):
    """
    FastAPI dependency generator — checks if the current user has
    the specified permission for a module. Super Admin bypasses all checks.
    """
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
    """
    Returns a MongoDB query fragment restricting data to the user's assigned cities.
    Super Admin or users with ["*"] get unrestricted access.
    """
    assigned = user.get("assigned_cities", [])
    role_name = user.get("role", {}).get("role_name", "")

    if role_name == "Super Admin" or "*" in assigned:
        return {}

    if not assigned:
        return {"city": "____IMPOSSIBLE_CITY_NONE____"}

    return {"city": {"$in": assigned}}

# ═══════════════════════════════════════════════════════════════
# SEED FUNCTION — call once on startup
# ═══════════════════════════════════════════════════════════════

def seed_rbac():
    """
    Seed default roles and a Super Admin user if they don't exist.
    Call this from server.py startup alongside existing seed_admin().
    """
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

    # ── 3. Create indexes if not exist ──
    try:
        db.dashboard_users.create_index("mobile", unique=True)
        db.dashboard_users.create_index("token", sparse=True)
        db.dashboard_roles.create_index("role_name", unique=True)
        db.activity_logs.create_index([("timestamp", -1)])
        db.activity_logs.create_index("user_id")
        db.activity_logs.create_index("module")
    except Exception as e:
        print("[RBAC] Index creation (may already exist): " + str(e))


# ═══════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.post("/login")
async def login(data: dict, request: Request, response: Response):
    """
    Login with mobile number + PIN.
    Sets HttpOnly cookie on success.
    Returns user identity for frontend.
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

    # ── Success ──
    token = secrets.token_urlsafe(32)
    db.dashboard_users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "token": token,
            "login_attempts": 0,
            "lockout_until": None,
            "last_login_at": datetime.utcnow(),
            "last_active_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }}
    )

    log_activity(request, user["_id"], user["full_name"], mobile, "Auth", "LOGIN_SUCCESS")

    # Resolve role for response
    role = db.dashboard_roles.find_one({"_id": user["role_id"]}) if user.get("role_id") else None
    role_name = role["role_name"] if role else "Unknown"

    response.set_cookie(
        key="admin_token",
        value=token,
        httponly=True,
        samesite="Lax",
        secure=False,  # Set True in production HTTPS
        max_age=3600 * SESSION_MAX_HOURS
    )

    return {
        "message": "Login successful",
        "user": {
            "id": str(user["_id"]),
            "full_name": user["full_name"],
            "mobile": user["mobile"],
            "designation": user.get("designation", ""),
            "role": role_name,
            "assigned_cities": user.get("assigned_cities", [])
        }
    }


@router.post("/logout")
async def logout(request: Request):
    """Clear the session token and cookie."""
    token = request.cookies.get("admin_token")
    if token:
        user = db.dashboard_users.find_one({"token": token})
        if user:
            db.dashboard_users.update_one({"_id": user["_id"]}, {"$set": {"token": None}})
            log_activity(request, user["_id"], user["full_name"], user["mobile"], "Auth", "LOGOUT")

    res = JSONResponse({"message": "Logged out"})
    res.delete_cookie("admin_token")
    return res


@router.get("/me")
async def get_me(user: dict = Depends(get_current_dashboard_user)):
    """Return current user identity, role, permissions, and assigned cities."""
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
# DASHBOARD USERS CRUD
# ═══════════════════════════════════════════════════════════════

def _serialize_user(u: dict) -> dict:
    """Convert a dashboard user doc to a JSON-safe dict."""
    return {
        "id": str(u["_id"]),
        "full_name": u.get("full_name", ""),
        "mobile": u.get("mobile", ""),
        "email": u.get("email"),
        "designation": u.get("designation", ""),
        "profile_photo_url": u.get("profile_photo_url"),
        "role_id": str(u["role_id"]) if u.get("role_id") else None,
        "role_name": u.get("role_name", "Unassigned"),   # ← included for dashboard display
        "status": u.get("status", "active"),
        "assigned_cities": u.get("assigned_cities", []),
        "last_login_at": u.get("last_login_at").isoformat() if u.get("last_login_at") else None,
        "created_at": u.get("created_at").isoformat() if u.get("created_at") else None,
    }


@router.get("/users")
async def list_users(
    request: Request,
    user: dict = Depends(require_permission("Users", "view"))
):
    """List all dashboard users."""
    users = list(db.dashboard_users.find({}, {"pin": 0}).sort("created_at", -1))
    for u in users:
        role = db.dashboard_roles.find_one({"_id": u["role_id"]}) if u.get("role_id") else None
        u["role_name"] = role["role_name"] if role else "Unassigned"
    return [_serialize_user(u) for u in users]


@router.post("/users")
async def create_user(
    data: dict,
    request: Request,
    user: dict = Depends(require_permission("Users", "add"))
):
    """Create a new dashboard user. Requires Users:add permission."""
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
        "Users", "ADD",
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
    user: dict = Depends(require_permission("Users", "edit"))
):
    """Update a dashboard user's profile, role, status, or cities."""
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    existing = db.dashboard_users.find_one({"_id": ObjectId(user_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    updates = {}
    for field in ("full_name", "email", "designation", "profile_photo_url", "status", "assigned_cities"):
        if field in data:
            updates[field] = data[field]

    if "role_id" in data and data["role_id"]:
        if not db.dashboard_roles.find_one({"_id": ObjectId(data["role_id"])}):
            raise HTTPException(status_code=400, detail="Invalid role_id")
        updates["role_id"] = ObjectId(data["role_id"])

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
        "Users", "EDIT",
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
    user: dict = Depends(require_permission("Users", "edit"))
):
    """Reset a user's PIN. Super Admin only in practice."""
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
        "Users", "RESET_PIN",
        record_id=user_id,
        record_name=existing.get("full_name", "")
    )

    return {"message": "PIN reset successful"}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    request: Request,
    user: dict = Depends(require_permission("Users", "delete"))
):
    """Permanently delete a dashboard user."""
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    existing = db.dashboard_users.find_one({"_id": ObjectId(user_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent deleting the last Super Admin
    super_role = db.dashboard_roles.find_one({"role_name": "Super Admin"})
    if super_role and existing.get("role_id") == super_role["_id"]:
        super_admin_count = db.dashboard_users.count_documents({
            "role_id": super_role["_id"], "status": "active"
        })
        if super_admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last Super Admin")

    db.dashboard_users.delete_one({"_id": ObjectId(user_id)})

    log_activity(
        request, user["_id"], user["full_name"], user["mobile"],
        "Users", "DELETE",
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
    """List all roles with their permission grids."""
    roles = list(db.dashboard_roles.find().sort("created_at", 1))
    return [_serialize_role(r) for r in roles]


@router.post("/roles")
async def create_role(
    data: dict,
    request: Request,
    user: dict = Depends(require_permission("User Management", "edit"))
):
    """Create a new custom role with a permission grid."""
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
    """Update a role's name, description, or permission grid."""
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
    """Delete a custom role. Cannot delete system roles or roles assigned to active users."""
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
    user: dict = Depends(require_permission("Notifications", "view"))
):
    """
    Get paginated activity logs with optional filters.
    Returns most recent first.
    """
    query = {}

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
# HELPER: Get all available modules (for frontend permission matrix)
# ═══════════════════════════════════════════════════════════════

@router.get("/modules")
async def list_modules(
    user: dict = Depends(get_current_dashboard_user)
):
    """Return all module names and action types for the permission matrix UI."""
    return {"modules": ALL_MODULES, "actions": ALL_ACTIONS}
