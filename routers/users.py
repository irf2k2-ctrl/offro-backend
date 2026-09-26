from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from database import db
from bson import ObjectId
import uuid
from datetime import datetime

# ── Phone normalisation helper ─────────────────────────────────
def _phone_variants(raw: str) -> list:
    p = str(raw).strip().replace(" ", "").replace("-", "")
    d = p[1:] if p.startswith("+") else p
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    last10 = d[-10:] if len(d) >= 10 else d
    return list({p, f"+91{last10}", f"91{last10}", last10, f"0{last10}"})

def _normalise_phone(raw: str) -> str:
    p = str(raw).strip().replace(" ", "").replace("-", "")
    d = p[1:] if p.startswith("+") else p
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    last10 = d[-10:] if len(d) >= 10 else d
    return f"+91{last10}" if len(last10) == 10 else p


router = APIRouter(tags=["Users"])

def get_current_user(request: Request):
    token = (request.cookies.get("user_token") or
             request.headers.get("Authorization", "").replace("Bearer ", ""))
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Unified accounts collection
    acct = db.accounts.find_one({"token": token})
    if not acct:
        # Fallback: legacy users collection during transition
        acct = (db.accounts.find_one({"token": token}) or
            db.users.find_one({"token": token}))
        if not acct:
            raise HTTPException(status_code=401, detail="Session expired")
    return acct

@router.post("/check-phone")
def check_phone(data: dict):
    """
    Unified check — accounts first, then legacy users + merchants as fallback.
    Returns {"registered": bool, "role": "user"|"merchant"|"both"|"none"}
    """
    raw_phone = str(data.get("phone", "")).strip()
    if not raw_phone:
        raise HTTPException(status_code=400, detail="Phone is required")
    variants = _phone_variants(raw_phone)

    # Primary: unified accounts collection
    acct = db.accounts.find_one({"phone": {"$in": variants}})

    # Fallback 1: legacy users
    if not acct:
        acct = db.users.find_one({"phone": {"$in": variants}})
        if acct:
            acct["roles"] = acct.get("roles", ["user"])

    # Fallback 2: legacy merchants
    if not acct:
        acct = db.merchants.find_one({"phone": {"$in": variants}})
        if acct:
            acct["roles"] = acct.get("roles", ["merchant"])

    if not acct:
        return {"registered": False, "role": "none"}

    roles = acct.get("roles", [])
    if "user" in roles and "merchant" in roles: role = "both"
    elif "merchant" in roles:                   role = "merchant"
    else:                                       role = "user"
    return {"registered": True, "role": role}


# ══════════════════════════════════════════════════════════════════════════════
# LOGOUT
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# REGISTER — creates new account in unified accounts collection
# POST /user/register
# Called by Flutter on Register tab after check-phone confirms number is new.
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/register")
def register_user(data: dict):
    raw_phone = str(data.get("phone", "")).strip()
    name      = str(data.get("name", "")).strip()
    city      = str(data.get("city", "")).strip()

    if not raw_phone or not name:
        raise HTTPException(status_code=400, detail="Name and phone are required")

    phone    = _normalise_phone(raw_phone)
    variants = _phone_variants(raw_phone)

    # If an existing account on this phone requested/completed deletion, treat this
    # as a brand-new account: wipe the old record's identity fields and reactivate it
    # under the new name. This satisfies "signup again = new account" without losing
    # the ability to reference the old row for support/audit purposes.
    existing = db.accounts.find_one({"phone": {"$in": variants}})
    if existing and existing.get("status") in ("delete_requested", "deleted"):
        now = datetime.utcnow().isoformat()
        fresh = {
            "name":                name,
            "phone":               phone,
            "phone_variants":      variants,
            "city":                city,
            "roles":               ["user"],
            "status":              "active",
            "visit_points":        0,
            "pool_points":         0,
            "token":               None,
            "updated_at":          now,
            "recreated_at":        now,
            "delete_reason":       None,
            "delete_feedback":     None,
            "delete_requested_at": None,
            "terms_version":       "1.0",
            "terms_accepted_at":   now,
        }
        db.accounts.update_one({"_id": existing["_id"]}, {"$set": fresh})
        db.users.update_one({"phone": {"$in": variants}}, {"$set": fresh}, upsert=True)
        acct_id = str(existing["_id"])
        print(f"[REGISTER] ✅ Re-registered as new account (was {existing.get('status')}): {phone} name={name} id={acct_id}")
        return {"message": "Registered successfully", "account_id": acct_id}

    # Block duplicate registration across all collections (still-active accounts only)
    if existing:
        raise HTTPException(status_code=400, detail="Phone already registered. Please login.")
    if db.users.find_one({"phone": {"$in": variants}}):
        raise HTTPException(status_code=400, detail="Phone already registered. Please login.")

    now = datetime.utcnow().isoformat()
    account = {
        "name":           name,
        "phone":          phone,
        "phone_variants": variants,
        "city":           city,
        "roles":          ["user"],
        "status":         "active",
        "visit_points":   0,
        "pool_points":    0,
        "token":          None,
        "created_at":     now,
        "updated_at":     now,
        "terms_version":  "1.0",
        "terms_accepted_at": now,
    }
    result = db.accounts.insert_one(account)
    acct_id = str(result.inserted_id)

    # Sync to legacy users collection for rollback safety
    db.users.update_one(
        {"phone": phone},
        {"$setOnInsert": {**account, "account_id": acct_id}},
        upsert=True,
    )

    print(f"[REGISTER] ✅ New user registered: {phone} name={name} id={acct_id}")
    return {"message": "Registered successfully", "account_id": acct_id}

@router.post("/logout")
def logout_user():
    res = JSONResponse(content={"message": "Logged out"})
    res.delete_cookie("user_token")
    return res


# ══════════════════════════════════════════════════════════════════════════════
# PROFILE
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/me")
def get_profile(user=Depends(get_current_user)):
    return {
        "user_id":       str(user["_id"]),
        "_id":           str(user["_id"]),
        "name":          user.get("name", ""),
        "phone":         user.get("phone", ""),
        "city":          user.get("city", ""),
        "visit_points":  user.get("visit_points", 0),
        "pool_points":   user.get("pool_points", 0),
        "total_points":  user.get("visit_points", 0) + user.get("pool_points", 0),
        "profile_image":      user.get("profile_image", ""),
        "terms_version":      user.get("terms_version", ""),
        "terms_accepted_at":  user.get("terms_accepted_at", ""),
        "merchant_terms_accepted": user.get("merchant_terms_accepted", False),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT DELETION REQUEST
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/request-delete")
def request_account_deletion(data: dict, user=Depends(get_current_user)):
    """
    Customer-initiated account deletion request (Apple/Play Store compliance).
    Marks the account as 'delete_requested' — this immediately:
      1. Logs the user out (token cleared)
      2. Blocks future login attempts on this phone
      3. Surfaces the account with status "Delete Request" on the admin Accounts dashboard
    An admin must review and permanently purge the account (or reject the request)
    from the dashboard. This two-step flow avoids irreversible data loss from
    accidental/abusive requests while still honouring the user's request instantly
    by blocking the account.
    """
    reason   = str(data.get("reason", "")).strip()
    feedback = str(data.get("feedback", "")).strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Please select a reason")

    now = datetime.utcnow().isoformat()
    update = {
        "status":               "delete_requested",
        "delete_reason":        reason,
        "delete_feedback":      feedback,
        "delete_requested_at":  now,
        "token":                None,   # force logout everywhere
    }
    db.accounts.update_one({"_id": user["_id"]}, {"$set": update})
    # Keep legacy users collection in sync (rollback safety)
    db.users.update_one({"_id": user["_id"]}, {"$set": update})

    print(f"[DELETE-REQUEST] phone={user.get('phone')} reason={reason}")
    return {"message": "Account deletion requested. You have been logged out."}


# ══════════════════════════════════════════════════════════════════════════════
# WALLET
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/wallet")
def get_wallet(user=Depends(get_current_user)):
    visit   = user.get("visit_points", 0)
    pool    = user.get("pool_points", 0)
    pricing = db.pricing.find_one({}) or {}
    rate    = float(pricing.get("conversion_rate", 0.10))
    min_w   = int(pricing.get("min_withdraw_points", 200))
    total   = visit + pool
    return {
        "visit_points":       visit,
        "pool_points":        pool,
        "total_points":       total,
        "conversion_rate":    rate,
        "min_withdraw_points": min_w,
        "value_in_rupees":    round(total * rate, 2),
        "profile_image":      user.get("profile_image", None),
    }

# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED ACCOUNT LOGIN — single endpoint for all account types
# POST /user/account-login
# Called after MSG91 OTP verified on Flutter side.
# Checks accounts collection first (unified), falls back to users/merchants.
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/account-login")
def account_login(data: dict):
    """
    Single login for all accounts. No role selection needed.
    Checks accounts collection → falls back to users → merchants.
    Returns: token, name, phone, roles, user_id, merchant_id, is_merchant
    """
    raw_phone = str(data.get("phone", "")).strip()
    if not raw_phone:
        raise HTTPException(status_code=400, detail="Phone is required")

    variants = _phone_variants(raw_phone)

    # ── Primary: unified accounts collection ──
    acct = db.accounts.find_one({"phone": {"$in": variants}})

    # ── Fallback 1: legacy users collection ──
    if not acct:
        acct = db.users.find_one({"phone": {"$in": variants}})
        if acct:
            acct["roles"] = acct.get("roles", ["user"])

    # ── Fallback 2: legacy merchants collection ──
    if not acct:
        acct = db.merchants.find_one({"phone": {"$in": variants}})
        if acct:
            acct["roles"] = acct.get("roles", ["merchant"])

    if not acct:
        raise HTTPException(status_code=404, detail="Phone not registered. Please register first.")

    if acct.get("status") == "blocked":
        raise HTTPException(status_code=403, detail="Account suspended. Contact support.")
    if acct.get("status") == "delete_requested":
        raise HTTPException(status_code=403, detail="This account has a pending deletion request and cannot be used to log in. Contact support if this was a mistake.")
    if acct.get("status") == "deleted":
        raise HTTPException(status_code=404, detail="Phone not registered. Please register first.")

    roles       = acct.get("roles", ["user"])
    is_merchant = "merchant" in roles
    token       = str(uuid.uuid4())

    # Update token in accounts (primary)
    db.accounts.update_one(
        {"_id": acct["_id"]},
        {"$set": {"token": token, "last_login": datetime.utcnow().isoformat()}}
    )
    # Sync token to legacy collections for rollback safety
    db.users.update_one({"phone": {"$in": variants}}, {"$set": {"token": token}})
    if is_merchant:
        db.merchants.update_one({"phone": {"$in": variants}}, {"$set": {"token": token}})

    acct_id   = str(acct["_id"])
    user_id   = acct.get("user_id", acct_id if not is_merchant else "")
    merch_id  = acct.get("merchant_id", acct_id if is_merchant else "")
    # C1: same additive pattern as merchant_id above — resolves to "" for
    # every existing account (nothing currently sets this), so this cannot
    # change behavior for any existing user/merchant login.
    influencer_id = acct.get("influencer_id", "") if "influencer" in roles else ""

    print(f"[ACCOUNT-LOGIN] ✅ {raw_phone} roles={roles} id={acct_id}")

    resp_data = {
        "account_id":     acct_id,
        "user_id":        user_id,
        "merchant_id":    merch_id,
        "influencer_id":  influencer_id,
        "name":           acct.get("name", ""),
        "phone":          acct.get("phone", raw_phone),
        "token":          token,
        "roles":          roles,
        "is_merchant":    is_merchant,
        "role":           "merchant" if is_merchant and "user" not in roles else ("both" if is_merchant else "user"),
        "visit_points":   acct.get("visit_points", 0),
        "pool_points":    acct.get("pool_points", 0),
        "city":           acct.get("city", ""),
    }
    response = JSONResponse(content=resp_data)
    response.set_cookie(key="user_token", value=token, httponly=True,
        samesite="Lax", secure=False, max_age=3600 * 24 * 30)
    if is_merchant:
        response.set_cookie(key="merchant_token", value=token, httponly=True,
            samesite="Lax", secure=False, max_age=3600 * 24 * 30)
    return response


# ===================== INFLUENCER PROFILE (Step C1) =====================
# Self-service profile creation/edit for the AUTHENTICATED account — not an
# admin-side operation. Reuses get_current_user (the same dependency every
# other authenticated account endpoint here uses) rather than a new auth
# system. Ownership is always the caller's own account, resolved from their
# token — never a client-supplied account_id, per the explicit security
# requirement. Follows the exact "$addToSet roles" + linkage-field pattern
# already used by merchant_register()/merchant_app.py for account↔identity
# linkage, just from the account side rather than at registration time,
# since an influencer's account already exists via the normal login flow.

def _resolve_own_account(user: dict):
    """get_current_user() may return either a db.accounts doc or (via its
    legacy fallback) a db.users doc — only db.accounts has influencer_id/
    roles in the unified sense this feature relies on. Resolves robustly to
    the real db.accounts document either way."""
    acct = db.accounts.find_one({"_id": user["_id"]})
    if not acct:
        acct = db.accounts.find_one({"phone": {"$in": _phone_variants(user.get("phone", ""))}})
    return acct

@router.post("/influencer-profile")
def create_influencer_profile(data: dict, user=Depends(get_current_user)):
    """Create the influencer profile for the AUTHENTICATED account. One
    account can have at most one influencer profile (1:1)."""
    acct = _resolve_own_account(user)
    if not acct:
        raise HTTPException(400, "Please log in again to continue.")
    if acct.get("influencer_id"):
        raise HTTPException(400, "You already have an influencer profile.")
    # Defense in depth: also check by account_id directly, in case
    # influencer_id was ever unset on the account without removing the
    # underlying profile — keeps the relationship 1:1 either way.
    if db.influencers.find_one({"account_id": str(acct["_id"])}):
        raise HTTPException(400, "You already have an influencer profile.")

    name = (data.get("name", "") or "").strip()
    if not name:
        raise HTTPException(400, "Name is required")
    from routers.admin import _validate_influencer_city, _resolve_influencer_photo
    city = _validate_influencer_city(data.get("city", ""))
    category = (data.get("category", "") or "").strip()
    state = (data.get("state", "") or "").strip()
    phone = (data.get("phone", "") or "").strip() or acct.get("phone", "")
    social_in = data.get("social") or {}
    social = {
        "instagram": str(social_in.get("instagram", "")).strip(),
        "facebook":  str(social_in.get("facebook", "")).strip(),
        "youtube":   str(social_in.get("youtube", "")).strip(),
    }
    photo_url = _resolve_influencer_photo(data.get("photo_url", ""))
    now = datetime.utcnow()
    doc = {
        "name": name, "state": state, "city": city, "category": category,
        "photo_url": photo_url, "social": social,
        "rating": 0, "review_count": 0, "status": "active",
        "phone": phone,
        "account_id": str(acct["_id"]),
        "created_at": now, "updated_at": now,
    }
    # Atomic create + link (this fix): both the influencer document and the
    # account linkage (influencer_id + role) must succeed together, or
    # neither should remain — using a real MongoDB session/transaction via
    # the existing pymongo client (see database.py: `from pymongo import
    # MongoClient` / `client = MongoClient(...)`), not a manual compensating
    # rollback. PyMongo's transaction context manager aborts automatically
    # if anything inside it raises, so the insert_one is rolled back too if
    # the account update fails for any reason.
    #
    # IMPORTANT — could not verify from this environment whether the actual
    # staging MongoDB deployment is a replica set/mongos, which MongoDB
    # transactions require (a standalone mongod does not support them at
    # all). No use of transactions exists anywhere else in this codebase to
    # confirm support either way. If this deployment is standalone, MongoDB
    # itself will reject the transaction attempt with an OperationFailure
    # (typically mentioning "replica set") the first time this endpoint is
    # called — that failure is surfaced below as a clear 500, not silently
    # caught or worked around with a non-atomic fallback.
    from database import client as _mongo_client
    from pymongo.errors import OperationFailure as _MongoOpFailure
    influencer_id = None
    try:
        with _mongo_client.start_session() as session:
            with session.start_transaction():
                res = db.influencers.insert_one(doc, session=session)
                influencer_id = str(res.inserted_id)
                db.accounts.update_one(
                    {"_id": acct["_id"]},
                    {"$set": {"influencer_id": influencer_id}, "$addToSet": {"roles": "influencer"}},
                    session=session,
                )
    except _MongoOpFailure as e:
        raise HTTPException(500, f"Could not create influencer profile atomically: {e}")
    return {"ok": True, "influencer_id": influencer_id}

@router.get("/influencer-profile")
def get_my_influencer_profile(user=Depends(get_current_user)):
    """Returns the authenticated account's OWN influencer profile (full
    detail, including phone — this is the owner's own view, not the public
    directory API, so the usual public-field restriction doesn't apply
    here). Returns {} if this account has no influencer profile."""
    acct = _resolve_own_account(user)
    influencer_id = acct.get("influencer_id") if acct else None
    if not influencer_id:
        return {}
    try:
        oid = ObjectId(influencer_id)
    except Exception:
        return {}
    d = db.influencers.find_one({"_id": oid})
    if not d:
        return {}
    d["_id"] = str(d["_id"])
    return d

@router.put("/influencer-profile")
def update_my_influencer_profile(data: dict, user=Depends(get_current_user)):
    """Edit the authenticated account's OWN influencer profile only.
    Ownership is enforced by checking the influencer document's own
    account_id against the caller's authenticated account — the client
    cannot submit someone else's account_id or influencer_id to bypass
    this, since neither is ever read from the request body here."""
    acct = _resolve_own_account(user)
    if not acct or not acct.get("influencer_id"):
        raise HTTPException(404, "No influencer profile found for this account.")
    try:
        oid = ObjectId(acct["influencer_id"])
    except Exception:
        raise HTTPException(400, "Invalid influencer profile reference.")
    existing = db.influencers.find_one({"_id": oid})
    if not existing:
        raise HTTPException(404, "Influencer profile not found.")
    if existing.get("account_id") != str(acct["_id"]):
        raise HTTPException(403, "You do not have permission to edit this influencer profile.")

    from routers.admin import _validate_influencer_city, _resolve_influencer_photo
    update = {}
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise HTTPException(400, "Name cannot be empty")
        update["name"] = name
    if "state" in data:
        update["state"] = (data["state"] or "").strip()
    if "city" in data:
        update["city"] = _validate_influencer_city(data["city"])
    if "category" in data:
        update["category"] = (data["category"] or "").strip()
    if "phone" in data:
        update["phone"] = (data["phone"] or "").strip()
    if "social" in data:
        social_in = data.get("social") or {}
        update["social"] = {
            "instagram": str(social_in.get("instagram", "")).strip(),
            "facebook":  str(social_in.get("facebook", "")).strip(),
            "youtube":   str(social_in.get("youtube", "")).strip(),
        }
    if "photo_url" in data:
        update["photo_url"] = _resolve_influencer_photo(data["photo_url"], existing.get("photo_url", ""))
    if not update:
        raise HTTPException(400, "Nothing to update")
    update["updated_at"] = datetime.utcnow()
    db.influencers.update_one({"_id": oid}, {"$set": update})
    return {"ok": True}


@router.post("/wallet/withdraw")
def withdraw(data: dict, user=Depends(get_current_user)):
    pricing    = db.pricing.find_one({}) or {}
    min_withdraw = int(pricing.get("min_withdraw_points", 200))
    visit      = user.get("visit_points", 0)
    pool       = user.get("pool_points", 0)
    total      = visit + pool
    amount     = int(data.get("amount", min_withdraw))
    if total < min_withdraw:
        raise HTTPException(status_code=400, detail=f"Minimum {min_withdraw} points required. You have {total}.")
    if total < amount:
        raise HTTPException(status_code=400, detail=f"Not enough points. You have {total}.")
    db.accounts.update_one({"_id": user["_id"]}, {"$set": {"pending_withdraw": True}})
    db.withdraw_requests.insert_one({
        "user_id":      str(user["_id"]),
        "user_name":    user.get("name"),
        "phone":        user.get("phone"),
        "email":        user.get("email", ""),
        "points":       amount,
        "voucher_value": round(amount / 10, 2),
        "status":       "pending",
        "created_at":   datetime.utcnow(),
    })
    return {
        "message":         "Gift Voucher request submitted! You will receive your Amazon/Flipkart voucher within 3-5 business days.",
        "remaining_points": total,
    }


# ══════════════════════════════════════════════════════════════════════════════
# QR REDEEM  (unchanged)
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/redeem")
def redeem_qr(data: dict, request: Request):
    store_id   = data.get("store_id")
    user_token = data.get("user_token") or request.cookies.get("user_token")
    if not store_id:
        raise HTTPException(status_code=400, detail="store_id required")
    if not user_token:
        raise HTTPException(status_code=401, detail="User not authenticated")
    user = db.accounts.find_one({"token": user_token})
    if not user:
        user = db.accounts.find_one({"token": user_token}) or db.users.find_one({"token": user_token})  # unified + legacy fallback
    if not user:
        raise HTTPException(status_code=403, detail="Invalid user session")
    try:
        store = db.stores.find_one({"_id": ObjectId(store_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid store_id")
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    if store.get("status") != "active":
        raise HTTPException(status_code=400, detail="Store is not active")

    points_to_add = int(store.get("points_per_scan", 10))
    user_id       = str(user["_id"])

    from datetime import timedelta
    recent = db.redemptions.find_one({
        "user_id":  user_id,
        "store_id": store_id,
        "created_at": {"$gte": datetime.utcnow() - timedelta(hours=24)},
    })
    if recent:
        raise HTTPException(status_code=429, detail="Already redeemed from this store today. Try again tomorrow.")

    db.accounts.update_one({"_id": user["_id"]}, {"$inc": {"visit_points": points_to_add, "visit_pts": points_to_add}})
    # Keep legacy users collection in sync
    if user.get("user_id"):
        db.users.update_one({"token": user_token}, {"$inc": {"visit_points": points_to_add}})
    db.redemptions.insert_one({
        "user_id":     user_id,
        "store_id":    store_id,
        "store_name":  store.get("store_name"),
        "merchant_id": store.get("merchant_id"),
        "points":      points_to_add,
        "created_at":  datetime.utcnow(),
    })
    updated = db.accounts.find_one({"_id": user["_id"]}) or db.users.find_one({"_id": user["_id"]})
    return {
        "message":      f"✅ {points_to_add} points added!",
        "store_name":   store.get("store_name"),
        "points_earned": points_to_add,
        "total_points":  updated.get("visit_points", 0) + updated.get("pool_points", 0),
    }


# ══════════════════════════════════════════════════════════════════════════════
# CITY / PROFILE / HISTORY / FAVOURITES / FCM  (all unchanged from previous)
# ══════════════════════════════════════════════════════════════════════════════
@router.put("/city")
def update_city(data: dict, user=Depends(get_current_user)):
    city = data.get("city", "").strip()
    if city:
        db.accounts.update_one({"_id": user["_id"]}, {"$set": {"city": city}})
    return {"message": "City updated", "city": city}

@router.get("/redemptions")
def redemption_history(user=Depends(get_current_user)):
    user_id    = str(user["_id"])
    redemptions = list(db.redemptions.find({"user_id": user_id}).sort("created_at", -1).limit(50))
    result = []
    for r in redemptions:
        result.append({
            "store_name": r.get("store_name"),
            "points":     r.get("points"),
            "date": (
                (lambda dt: dt.strftime("%d %b %Y %H:%M IST")
                 if hasattr(dt, "strftime") else str(dt)[:16].replace("T", " ")
                )(r["created_at"])
            ) if r.get("created_at") else "",
        })
    return result

@router.get("/wallet/history")
def wallet_transaction_history(user=Depends(get_current_user)):
    user_id = str(user["_id"])
    txns    = list(db.point_transactions.find({"user_id": user_id}).sort("created_at", -1).limit(100))
    result  = []
    for t in txns:
        result.append({
            "type":   t.get("type", "credit"),
            "points": t.get("points", 0),
            "note":   t.get("note", ""),
            "date": (
                (lambda dt: dt.strftime("%d %b %Y %H:%M")
                 if hasattr(dt, "strftime") else str(dt)[:16].replace("T", " ")
                )(t["created_at"])
            ) if t.get("created_at") else "",
        })
    return result

@router.get("/favorites")
def list_favorites(user=Depends(get_current_user)):
    fav_ids = user.get("favorite_store_ids", [])
    from bson import ObjectId as OId
    valid_ids = []
    for fid in fav_ids:
        try: valid_ids.append(OId(str(fid)))
        except: pass
    stores = list(db.stores.find({"_id": {"$in": valid_ids}}))
    result = []
    for s in stores:
        img = s.get("image") or (s.get("images") or [None])[0] or ""
        result.append({
            "_id":        str(s["_id"]),
            "store_name": s.get("store_name", ""),
            "category":   s.get("category", ""),
            "area":       s.get("area", ""),
            "city":       s.get("city", ""),
            "rating":     float(s.get("admin_rating") or s.get("rating") or 0),
            "image":      img,
            "image_url":   s.get("image_url", "") or s.get("_thumb", "") or "",
            "image_thumb": s.get("image_thumb", "") or s.get("_thumb", "") or "",
            "deal_count":  0,
        })
    return result

# ── Product Favourites ────────────────────────────────────────────────────────

@router.get("/product-favorites")
def list_product_favorites(user=Depends(get_current_user)):
    """Return all product IDs favourited by the current user."""
    return [str(pid) for pid in user.get("favorite_product_ids", [])]

def _persist_user_update(user_id, update_dict):
    """FIX: get_current_user can return a doc from either 'accounts' (primary,
    unified login) or the legacy 'users' collection (fallback). Writing
    unconditionally to 'accounts' silently no-ops (matched_count=0, no error)
    when the doc actually lives in 'users' — the favorite then never
    persists, which is exactly why it "disappeared" after refresh. Try both,
    whichever collection actually holds the record gets the write."""
    res = db.accounts.update_one({"_id": user_id}, update_dict)
    if res.matched_count == 0:
        db.users.update_one({"_id": user_id}, update_dict)

def _read_fresh_favorites(user_id, field):
    """Re-read the account doc after a write and return the field as it
    ACTUALLY is in the DB — instead of just assuming the write direction we
    intended succeeded. This exposes any silent persistence failure directly
    in the API response so the client can react to it correctly."""
    doc = db.accounts.find_one({"_id": user_id}, {field: 1}) or db.users.find_one({"_id": user_id}, {field: 1}) or {}
    return [str(f) for f in doc.get(field, [])]

@router.post("/product-favorites/{product_id}")
def toggle_product_favorite(product_id: str, user=Depends(get_current_user)):
    user_id  = user["_id"]
    fav_ids  = [str(f) for f in user.get("favorite_product_ids", [])]
    if product_id in fav_ids:
        _persist_user_update(user_id, {"$pull":     {"favorite_product_ids": product_id}})
    else:
        _persist_user_update(user_id, {"$addToSet": {"favorite_product_ids": product_id}})
    fresh_ids = _read_fresh_favorites(user_id, "favorite_product_ids")
    return {"is_favorite": product_id in fresh_ids}

@router.get("/product-favorites/{product_id}/check")
def check_product_favorite(product_id: str, user=Depends(get_current_user)):
    fav_ids = [str(f) for f in user.get("favorite_product_ids", [])]
    return {"is_favorite": product_id in fav_ids}

@router.post("/favorites/{store_id}")
def toggle_favorite(store_id: str, user=Depends(get_current_user)):
    user_id = user["_id"]
    fav_ids = [str(f) for f in user.get("favorite_store_ids", [])]
    if store_id in fav_ids:
        _persist_user_update(user_id, {"$pull":     {"favorite_store_ids": store_id}})
    else:
        _persist_user_update(user_id, {"$addToSet": {"favorite_store_ids": store_id}})
    fresh_ids = _read_fresh_favorites(user_id, "favorite_store_ids")
    return {"is_favorite": store_id in fresh_ids}

@router.get("/favorites/{store_id}/check")
def check_favorite(store_id: str, user=Depends(get_current_user)):
    fav_ids = [str(f) for f in user.get("favorite_store_ids", [])]
    return {"is_favorite": store_id in fav_ids}

@router.put("/profile")
def update_user_profile(data: dict, user=Depends(get_current_user)):
    allowed = ["profile_image", "name"]
    update  = {k: v for k, v in data.items() if k in allowed}
    if not update:
        raise HTTPException(400, "Nothing to update")
    db.accounts.update_one({"_id": user["_id"]}, {"$set": update})
    # SYNC: also update legacy users collection so get_current_user() fallback
    # returns the updated profile_image (fixes profile image disappearing bug)
    db.users.update_one({"_id": user["_id"]}, {"$set": update})
    return {"ok": True}

@router.post("/fcm-token")
def save_fcm_token(data: dict, user=Depends(get_current_user)):
    fcm_token = data.get("fcm_token", "").strip()
    if fcm_token:
        db.accounts.update_one(
            {"_id": user["_id"]},
            {"$set": {"fcm_token": fcm_token, "fcm_updated_at": datetime.utcnow()}}
        )
    return {"ok": True}
