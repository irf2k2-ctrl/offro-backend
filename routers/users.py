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
    from routers.admin import _validate_influencer_city, _resolve_influencer_photo, _normalize_influencer_categories
    city = _validate_influencer_city(data.get("city", ""))
    category_str, categories_list = _normalize_influencer_categories(data.get("categories"), data.get("category", ""))
    state = (data.get("state", "") or "").strip()
    # FIX (Issue 2): validate only when the caller actually supplied a
    # phone value — the fallback-to-account-phone case is already a
    # proper number by construction (set at account/OTP login time).
    # Scoped to this influencer-profile endpoint only, per the request.
    raw_phone_input = (data.get("phone", "") or "").strip()
    if raw_phone_input:
        import re as _re
        if not _re.fullmatch(r"\d{10}", raw_phone_input):
            raise HTTPException(400, "Please enter a valid 10-digit mobile number.")
        phone = raw_phone_input
    else:
        phone = acct.get("phone", "")
    social_in = data.get("social") or {}
    social = {
        "instagram": str(social_in.get("instagram", "")).strip(),
        "facebook":  str(social_in.get("facebook", "")).strip(),
        "youtube":   str(social_in.get("youtube", "")).strip(),
    }
    photo_url = _resolve_influencer_photo(data.get("photo_url", ""))
    now = datetime.utcnow()
    doc = {
        "name": name, "state": state, "city": city, "category": category_str, "categories": categories_list,
        "photo_url": photo_url, "social": social,
        "rating": 0, "review_count": 0, "status": "active",
        "phone": phone,
        "account_id": str(acct["_id"]),
        "created_at": now, "updated_at": now,
        # ── Influencer Subscription Fee + Payment + Publish ─────────────
        # A brand-new self-service profile always starts UNPAID/draft —
        # this is what makes "Save" (this endpoint) a draft-only action per
        # the approved business rule: the profile exists and is editable,
        # but is never publicly visible until Save & Publish completes a
        # verified payment (see /influencer-profile/publish below). This
        # never applies to admin-created records (routers/admin.py never
        # sets these fields, and existing profiles with no payment_status/
        # publish_status at all are treated as already-published — see the
        # backward-compatibility handling in routers/public.py).
        "payment_status": "UNPAID",
        "publish_status": "draft",
        "is_active": True,
    }
    # FIX (Issue 1): the previous implementation wrapped this in a MongoDB
    # session/transaction (client.start_session()/start_transaction()).
    # Investigation: staging's generic "Something went wrong" error is
    # produced client-side (Flutter) specifically when the backend's error
    # detail is empty OR exceeds 200 characters — ruling out every other
    # candidate (category/social/phone are all unvalidated free-text at
    # this stage; photo_url is empty when no photo is selected, so
    # _resolve_influencer_photo returns immediately without ever calling
    # Cloudinary) leaves MongoDB transaction support as the only remaining,
    # previously-flagged-as-uncertain candidate: transactions require a
    # replica set/mongos, and this codebase had never used a transaction
    # anywhere before that earlier fix — a standalone staging MongoDB
    # rejects the attempt with a verbose OperationFailure, which easily
    # exceeds 200 characters once wrapped in this function's own message,
    # producing exactly the reported generic fallback text.
    #
    # Fix: removed the transaction dependency entirely, replaced with an
    # explicit compensating action — if the account-linkage step fails for
    # any reason, the just-created influencer document is deleted manually.
    # This does not require a replica set and works on any MongoDB
    # deployment. It is a slightly weaker guarantee than a true ACID
    # transaction (a brief window exists where a concurrent read could see
    # the influencer doc before the compensating delete completes, and if
    # the delete itself fails the record could remain), but per this
    # investigation's finding it is the correct trade-off for this
    # deployment, and matches the "smallest safe alternative" already
    # proposed when the transaction risk was first flagged.
    # FIX (this task — race-safe 1:1 enforcement): the pre-checks above
    # (acct.get("influencer_id") and find_one by account_id) are a fast,
    # friendly early-exit for the common case, but they are NOT what
    # actually guarantees one-profile-per-account under concurrency — two
    # simultaneous requests can both pass those checks before either has
    # written anything. The real guarantee is the sparse unique index on
    # influencers.account_id (see server.py _ensure_indexes) — MongoDB
    # enforces it atomically at the storage layer for every insert, with no
    # transaction or replica set required, so it works unchanged on this
    # standalone deployment. If two requests race, exactly one insert
    # succeeds; the other raises DuplicateKeyError here, caught below and
    # turned into the same friendly message — it never reaches the
    # account-linkage step at all, so there is nothing to roll back for the
    # losing request, and no risk of accounts.influencer_id ever pointing
    # at a document that doesn't exist.
    from pymongo.errors import DuplicateKeyError as _DupKeyError
    try:
        res = db.influencers.insert_one(doc)
    except _DupKeyError:
        raise HTTPException(400, "You already have an influencer profile.")
    influencer_id = str(res.inserted_id)
    try:
        result = db.accounts.update_one(
            {"_id": acct["_id"]},
            {"$set": {"influencer_id": influencer_id}, "$addToSet": {"roles": "influencer"}},
        )
        if result.matched_count == 0:
            raise Exception("account not found during linkage step")
    except Exception as e:
        db.influencers.delete_one({"_id": res.inserted_id})
        print(f"[INFLUENCER-CREATE] account linkage failed, rolled back influencer {influencer_id}: {e}")
        raise HTTPException(500, "Could not create your influencer profile. Please try again.")
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
    # Issue 3: derive `categories` on the fly for a profile saved before
    # multi-category support existed (only has the legacy `category`
    # string) — never modifies the stored document just by reading it.
    from routers.admin import _derive_influencer_categories
    d["categories"] = _derive_influencer_categories(d)
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

    from routers.admin import _validate_influencer_city, _resolve_influencer_photo, _normalize_influencer_categories
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
    if "category" in data or "categories" in data:
        category_str, categories_list = _normalize_influencer_categories(data.get("categories"), data.get("category", ""))
        update["category"] = category_str
        update["categories"] = categories_list
    if "phone" in data:
        new_phone = (data["phone"] or "").strip()
        if new_phone:
            import re as _re
            if not _re.fullmatch(r"\d{10}", new_phone):
                raise HTTPException(400, "Please enter a valid 10-digit mobile number.")
        update["phone"] = new_phone
    if "social" in data:
        social_in = data.get("social") or {}
        update["social"] = {
            "instagram": str(social_in.get("instagram", "")).strip(),
            "facebook":  str(social_in.get("facebook", "")).strip(),
            "youtube":   str(social_in.get("youtube", "")).strip(),
        }
    if "photo_url" in data:
        update["photo_url"] = _resolve_influencer_photo(data["photo_url"], existing.get("photo_url", ""))
    # Enable/Disable (rule 5): a self-service visibility toggle, deliberately
    # separate from payment_status and publish_status — disabling a paid,
    # published profile must NOT touch its payment record, and re-enabling
    # it must NOT require a new payment. It is also separate from the
    # existing admin `status` field (admin moderation/visibility): an
    # admin-suspended profile (status="inactive") must stay hidden even if
    # the influencer re-enables it here, so public visibility (routers/
    # public.py) requires status=="active" AND is_active AND published.
    if "is_active" in data:
        update["is_active"] = bool(data["is_active"])
    if not update:
        raise HTTPException(400, "Nothing to update")
    update["updated_at"] = datetime.utcnow()
    db.influencers.update_one({"_id": oid}, {"$set": update})
    return {"ok": True}


@router.delete("/influencer-profile")
def delete_my_influencer_profile(user=Depends(get_current_user)):
    """Permanently deletes the authenticated account's OWN influencer
    profile (rule 4 — DELETE is permanent and ends the profile/payment
    relationship for good). Historical payment records in db.subscriptions/
    db.invoices are left untouched for audit purposes but become orphaned
    from any live profile — they are never reused. Clearing
    accounts.influencer_id here (rather than leaving it dangling) is what
    makes the NEXT create_influencer_profile() call for this account
    succeed as a genuinely brand-new profile with a fresh influencer_id and
    payment_status starting at UNPAID; the account keeps its "influencer"
    role so Switch Mode still offers Influencer mode, which — per the
    existing C1/C2 empty-state behavior already built into
    InfluencerModuleScreen — correctly routes back to "Add Influencer
    Profile" rather than any second registration flow."""
    acct = _resolve_own_account(user)
    if not acct or not acct.get("influencer_id"):
        raise HTTPException(404, "No influencer profile found for this account.")
    try:
        oid = ObjectId(acct["influencer_id"])
    except Exception:
        raise HTTPException(400, "Invalid influencer profile reference.")
    existing = db.influencers.find_one({"_id": oid})
    if existing and existing.get("account_id") != str(acct["_id"]):
        raise HTTPException(403, "You do not have permission to delete this influencer profile.")
    db.influencers.delete_one({"_id": oid})
    db.accounts.update_one({"_id": acct["_id"]}, {"$unset": {"influencer_id": ""}})
    return {"ok": True}


# ===================== INFLUENCER SUBSCRIPTION FEE + PAYMENT + PUBLISH =====================
# Reuses the exact existing Razorpay implementation (order creation helper,
# key env vars, HMAC-SHA256 signature verification) from routers/merchant_app.py
# — imported locally to avoid the module-level circular-import issue that
# affects every other cross-router helper in this file (see
# _validate_influencer_city et al. above) — rather than standing up a second
# payment integration. Amounts are always computed here, server-side, from
# db.pricing's influencer_subscription block; the Flutter client never
# supplies and is never trusted for price, GST, or total.

def _influencer_subscription_pricing() -> dict:
    from routers.admin import _influencer_subscription_block
    pricing = db.pricing.find_one({}) or {}
    return _influencer_subscription_block(pricing)


@router.get("/influencer-profile/subscription-pricing")
def get_influencer_subscription_pricing(user=Depends(get_current_user)):
    """Returns the current admin-configured fee/GST/total for the profile
    creation screen to display before Save & Publish — and whether THIS
    account's own profile (if any) already has payment_status PAID, so the
    app can skip the payment step and its explanation entirely."""
    acct = _resolve_own_account(user)
    already_paid = False
    if acct and acct.get("influencer_id"):
        try:
            d = db.influencers.find_one({"_id": ObjectId(acct["influencer_id"])})
            already_paid = bool(d) and d.get("payment_status", "PAID") == "PAID"
        except Exception:
            pass
    pricing = _influencer_subscription_pricing()
    pricing["currency"] = "INR"
    pricing["already_paid"] = already_paid
    return pricing


@router.post("/influencer-profile/publish")
def publish_my_influencer_profile(data: dict, user=Depends(get_current_user)):
    """SAVE & PUBLISH. `data` may optionally carry the same profile fields
    accepted by PUT /influencer-profile (name/state/city/category/phone/
    social/photo_url) — the profile is saved first with those fields (the
    "validate/save" step), exactly like a normal edit, before the
    payment/publish decision is made. Never accepts or trusts
    payment_status/publish_status/amount fields from the client.

    Returns one of:
      - {"ok": True, "publish_status": "published", "payment_required": False, ...}
            when payment isn't needed (already PAID, or admin has the
            subscription toggle disabled, or the configured fee is 0).
      - {"ok": True, "payment_required": True, "razorpay_order_id": ..., ...}
            when a Razorpay order was created and the app must open
            checkout, then call /influencer-profile/verify-payment.
    """
    acct = _resolve_own_account(user)
    if not acct or not acct.get("influencer_id"):
        raise HTTPException(404, "Please save your influencer profile before publishing.")
    try:
        oid = ObjectId(acct["influencer_id"])
    except Exception:
        raise HTTPException(400, "Invalid influencer profile reference.")
    existing = db.influencers.find_one({"_id": oid})
    if not existing:
        raise HTTPException(404, "Influencer profile not found.")
    if existing.get("account_id") != str(acct["_id"]):
        raise HTTPException(403, "You do not have permission to publish this influencer profile.")

    # Step 1 — validate/save (reuses the same field handling as PUT, minus
    # is_active, which has nothing to do with publishing).
    from routers.admin import _validate_influencer_city, _resolve_influencer_photo, _normalize_influencer_categories
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
    if "category" in data or "categories" in data:
        category_str, categories_list = _normalize_influencer_categories(data.get("categories"), data.get("category", ""))
        update["category"] = category_str
        update["categories"] = categories_list
    if "phone" in data:
        new_phone = (data["phone"] or "").strip()
        if new_phone:
            import re as _re
            if not _re.fullmatch(r"\d{10}", new_phone):
                raise HTTPException(400, "Please enter a valid 10-digit mobile number.")
        update["phone"] = new_phone
    if "social" in data:
        social_in = data.get("social") or {}
        update["social"] = {
            "instagram": str(social_in.get("instagram", "")).strip(),
            "facebook":  str(social_in.get("facebook", "")).strip(),
            "youtube":   str(social_in.get("youtube", "")).strip(),
        }
    if "photo_url" in data:
        update["photo_url"] = _resolve_influencer_photo(data["photo_url"], existing.get("photo_url", ""))
    if update:
        update["updated_at"] = datetime.utcnow()
        db.influencers.update_one({"_id": oid}, {"$set": update})
        existing = db.influencers.find_one({"_id": oid})  # re-read post-save state

    # Step 2 — payment/publish decision. Never trust client-supplied
    # payment/publish state; everything below is derived from the
    # server's own record and the server's own pricing config.
    pricing = _influencer_subscription_pricing()

    if existing.get("payment_status", "PAID") == "PAID":
        # Already paid (rule: one-time payment) — publish/update only.
        db.influencers.update_one({"_id": oid}, {"$set": {
            "publish_status": "published", "updated_at": datetime.utcnow(),
        }})
        return {"ok": True, "publish_status": "published", "payment_required": False, "already_paid": True}

    if not pricing["enabled"]:
        # Admin has switched the subscription requirement off entirely —
        # the explicit admin override the original analysis anticipated
        # for the UNPAID → PUBLISHED path. No charge, no payment_status
        # change (there was never a charge to record).
        db.influencers.update_one({"_id": oid}, {"$set": {
            "publish_status": "published", "updated_at": datetime.utcnow(),
        }})
        return {"ok": True, "publish_status": "published", "payment_required": False, "subscription_enabled": False}

    # ── Discount code (optional) — backend-authoritative. Reuses the exact
    # same discount resolution already used by Store/Banner/Product
    # checkout (routers/merchant_app.py::_resolve_discount), scoped
    # strictly to "INFLUENCER" so a Store/Banner/Product-only code can never
    # be applied here, and an Influencer-only code can never be applied to
    # those checkouts (checkout_scope must equal the code's applies_to,
    # unless the code is scoped "ALL"). Raises a friendly HTTPException for
    # an invalid/inactive/expired/exhausted/wrong-scope code — the caller
    # (Flutter) surfaces that as the error and does NOT continue with any
    # discounted amount, since nothing further executes past this line.
    from routers.merchant_app import _resolve_discount, _mark_discount_used
    discount_code_in = (data.get("discount_code") or "").strip()
    disc = _resolve_discount(discount_code_in, "INFLUENCER", pricing["fee"])
    discount_amount = disc["discount_amount"]

    # Backend computes: base fee → minus discount → taxable amount → plus
    # GST (on the DISCOUNTED taxable amount, same order as every other
    # checkout in this codebase) → final total. Flutter never supplies or
    # influences any of these numbers.
    taxable = max(0.0, round(pricing["fee"] - discount_amount, 2))
    gst_amount = round(taxable * pricing["gst_percent"] / 100, 2)
    total = round(taxable + gst_amount, 2)

    if total <= 0:
        # Configured fee (after any discount) resolves to ₹0 — mirrors the
        # existing merchant subscription's zero-price fast path (Razorpay
        # rejects amount=0 orders): activate immediately as PAID, no
        # Razorpay order, but still recorded as a real (₹0) invoice for
        # audit, same as the merchant "LS-FREE-" convention.
        now = datetime.utcnow()
        db.influencers.update_one({"_id": oid}, {"$set": {
            "payment_status": "PAID", "publish_status": "published",
            "subscription_amount": pricing["fee"], "gst_percent": pricing["gst_percent"],
            "gst_amount": gst_amount, "total_amount": total,
            "discount_code": disc["code"], "discount_amount": discount_amount,
            "paid_at": now, "updated_at": now,
        }})
        if disc["code"]:
            _mark_discount_used(disc["code"])
        invoice_no = f"INF-FREE-{now.strftime('%Y%m%d')}-{str(oid)[-6:].upper()}"
        db.invoices.insert_one({
            "invoice_no": invoice_no, "entity_type": "influencer", "influencer_id": str(oid),
            "account_id": str(acct["_id"]), "influencer_name": existing.get("name", ""),
            "type": "influencer", "item_label": "Influencer Subscription", "plan": "One-Time Subscription",
            "merchant_name": existing.get("name", ""), "merchant_phone": existing.get("phone", ""),
            "store_name": "Influencer Subscription",
            "base_price": pricing["fee"], "original_amount": pricing["fee"],
            "discount_code": disc["code"], "discount_amount": discount_amount,
            "final_amount": taxable, "gst": gst_amount, "total": total,
            "created_at": now,
        })
        return {"ok": True, "publish_status": "published", "payment_required": False, "total": total}

    amount_paise = int(round(total * 100))
    from routers.merchant_app import _razorpay_request, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET

    # ── Reuse a still-valid PAYMENT_PENDING Razorpay order instead of
    # creating a new one on every retry (e.g. the user's network dropped
    # right after Save & Publish, before they ever saw the checkout sheet,
    # or they cancelled Razorpay and pressed Save & Publish again for the
    # SAME draft profile). Reused ONLY when the pending order's amount AND
    # discount match the CURRENT pricing/code exactly — if admin changed
    # the fee/GST, or the user applied a different/no discount code this
    # time, that match fails and a fresh order is created below, so the
    # amount actually charged can never be stale. Never touches an
    # already-PAID profile's behavior (that case already returned above).
    now = datetime.utcnow()
    existing_pending = db.subscriptions.find_one({
        "entity_type": "influencer",
        "influencer_id": str(oid),
        "status": "pending",
        "pay_mode": "razorpay",
        "razorpay_order_id": {"$ne": None},
        "base_price": pricing["fee"],
        "gst_percent": pricing["gst_percent"],
        "discount_code": disc["code"],
        "discount_amount": discount_amount,
        "total": total,
    }, sort=[("created_at", -1)])

    if existing_pending:
        rp_order_id = existing_pending["razorpay_order_id"]
        pay_mode = "razorpay"
        sub_id_str = str(existing_pending["_id"])
        db.subscriptions.update_one({"_id": existing_pending["_id"]}, {"$set": {"updated_at": now}})
    else:
        # No reusable order — create a new Razorpay order the same way the
        # existing merchant subscription/upgrade flows do.
        rp_order_id = None
        pay_mode = "manual"
        if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
            try:
                order_data = {
                    "amount": amount_paise, "currency": "INR",
                    "receipt": f"infl_{str(oid)[-8:]}",
                    "notes": {"influencer_id": str(oid), "account_id": str(acct["_id"]), "type": "influencer_subscription"},
                }
                resp = _razorpay_request("POST", "/v1/orders", (RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), order_data)
                rz = resp.json()
                rp_order_id = rz.get("id")
                if rp_order_id:
                    pay_mode = "razorpay"
            except Exception:
                pay_mode = "manual"

        sub_doc = {
            "entity_type": "influencer",
            "influencer_id": str(oid),
            "account_id": str(acct["_id"]),
            "razorpay_order_id": rp_order_id,
            "base_price": pricing["fee"], "gst_percent": pricing["gst_percent"],
            "discount_code": disc["code"], "discount_amount": discount_amount,
            "gst_amount": gst_amount, "total": total,
            "currency": "INR",
            "status": "pending",
            "pay_mode": pay_mode,
            "created_at": now, "updated_at": now,
        }
        sub_result = db.subscriptions.insert_one(sub_doc)
        sub_id_str = str(sub_result.inserted_id)

    db.influencers.update_one({"_id": oid}, {"$set": {
        "payment_status": "PAYMENT_PENDING",
        "razorpay_order_id": rp_order_id,
        "subscription_amount": pricing["fee"], "gst_percent": pricing["gst_percent"],
        "gst_amount": gst_amount, "total_amount": total,
        "discount_code": disc["code"], "discount_amount": discount_amount,
        "updated_at": now,
    }})

    return {
        "ok": True,
        "payment_required": True,
        "publish_status": existing.get("publish_status", "draft"),
        "subscription_id": sub_id_str,
        "pay_mode": pay_mode,
        "razorpay_order_id": rp_order_id,
        "razorpay_key": RAZORPAY_KEY_ID if pay_mode == "razorpay" else None,
        "amount": amount_paise,
        "amount_display": total,
        "base_price": pricing["fee"],
        "gst_percent": pricing["gst_percent"],
        "gst_amount": gst_amount,
        "discount_code": disc["code"],
        "discount_amount": discount_amount,
        "total": total,
        "currency": "INR",
    }


@router.post("/influencer-profile/verify-payment")
def verify_influencer_payment(data: dict, user=Depends(get_current_user)):
    """Verifies a completed Razorpay payment for the authenticated account's
    OWN influencer profile using the exact same HMAC-SHA256 signature check
    already used by routers/merchant_app.py's verify_payment(). Only a
    successful, verified signature can ever set payment_status=PAID /
    publish_status=published — the client's claim of success is never
    trusted on its own."""
    acct = _resolve_own_account(user)
    if not acct or not acct.get("influencer_id"):
        raise HTTPException(404, "No influencer profile found for this account.")
    try:
        oid = ObjectId(acct["influencer_id"])
    except Exception:
        raise HTTPException(400, "Invalid influencer profile reference.")
    existing = db.influencers.find_one({"_id": oid})
    if not existing or existing.get("account_id") != str(acct["_id"]):
        raise HTTPException(403, "You do not have permission to verify payment for this influencer profile.")

    # Idempotency — a retry/double-tap after a successful verification must
    # not fail or double-charge; just report the already-published state.
    if existing.get("payment_status") == "PAID":
        return {"ok": True, "publish_status": "published", "already_paid": True}

    order_id   = str(data.get("razorpay_order_id", "") or "")
    payment_id = str(data.get("razorpay_payment_id", "") or "")
    signature  = str(data.get("razorpay_signature", "") or "")

    sub = db.subscriptions.find_one({
        "entity_type": "influencer",
        "influencer_id": str(oid),
        "razorpay_order_id": order_id,
    }, sort=[("created_at", -1)]) if order_id else None
    if not sub:
        raise HTTPException(400, "No matching payment order found for this influencer profile.")

    from routers.merchant_app import RAZORPAY_KEY_SECRET
    import hmac as _hmac, hashlib as _hashlib
    if not RAZORPAY_KEY_SECRET:
        raise HTTPException(503, "Payment verification unavailable — Razorpay not configured")
    msg = f"{order_id}|{payment_id}"
    expected = _hmac.new(RAZORPAY_KEY_SECRET.encode(), msg.encode(), _hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(expected, signature):
        db.subscriptions.update_one({"_id": sub["_id"]}, {"$set": {"status": "failed", "updated_at": datetime.utcnow()}})
        db.influencers.update_one({"_id": oid}, {"$set": {"payment_status": "PAYMENT_FAILED", "updated_at": datetime.utcnow()}})
        raise HTTPException(400, "Payment verification failed. Please try again.")

    now = datetime.utcnow()
    db.subscriptions.update_one({"_id": sub["_id"]}, {"$set": {
        "status": "paid", "razorpay_payment_id": payment_id, "razorpay_signature": signature,
        "updated_at": now,
    }})
    # Discount usage is only ever counted on a VERIFIED, successful payment
    # — never at order-creation time — matching the exact same timing the
    # existing Store/Banner/Product checkouts use for _mark_discount_used().
    if sub.get("discount_code"):
        from routers.merchant_app import _mark_discount_used
        _mark_discount_used(sub["discount_code"])
    invoice_no = f"INF-{now.strftime('%Y%m%d')}-{str(sub['_id'])[-6:].upper()}"
    base_price = sub.get("base_price", 0)
    discount_amount = sub.get("discount_amount", 0)
    db.invoices.insert_one({
        "invoice_no": invoice_no,
        "entity_type": "influencer",
        "influencer_id": str(oid),
        "account_id": str(acct["_id"]),
        "influencer_name": existing.get("name", ""),
        # FIX: without an explicit "type"/"item_label", the shared admin
        # Payments dashboard (routers/admin.py::list_all_invoices) defaults
        # an untyped invoice to "store" / "Store – {plan}" — which is
        # exactly why an influencer payment was showing up as a Store
        # transaction. These fields are what the dashboard actually reads.
        "type": "influencer",
        "item_label": "Influencer Subscription",
        "plan": "One-Time Subscription",
        # Reuses the same "merchant_name"/"merchant_phone"/"store_name"
        # columns the dashboard already renders for every other type —
        # populated with the influencer's own real name/phone, never
        # fabricated merchant/store data.
        "merchant_name": existing.get("name", ""),
        "merchant_phone": existing.get("phone", ""),
        "store_name": "Influencer Subscription",
        "base_price": base_price,
        "original_amount": base_price,
        "discount_code": sub.get("discount_code", ""),
        "discount_amount": discount_amount,
        "final_amount": round(base_price - discount_amount, 2),
        "gst": sub.get("gst_amount", 0),
        "total": sub.get("total", 0),
        "razorpay_order_id": order_id, "razorpay_payment_id": payment_id,
        "created_at": now,
    })
    db.influencers.update_one({"_id": oid}, {"$set": {
        "payment_status": "PAID", "publish_status": "published",
        "razorpay_payment_id": payment_id, "paid_at": now, "updated_at": now,
    }})
    return {"ok": True, "publish_status": "published", "invoice_no": invoice_no}


@router.post("/influencer-profile/validate-discount")
def validate_influencer_discount_code(data: dict, user=Depends(get_current_user)):
    """Preview-only discount check for the Save & Publish screen's "Apply"
    button — mirrors routers/merchant_app.py::validate_discount_code (Store/
    Banner/Product's own preview endpoint) but scoped to INFLUENCER and
    authenticated the same way the rest of this influencer feature is
    (get_current_user, not get_merchant). This is a convenience preview
    only: /influencer-profile/publish remains the sole authority at
    order-creation time and re-validates the code itself regardless of
    what this endpoint returned."""
    acct = _resolve_own_account(user)
    if not acct or not acct.get("influencer_id"):
        raise HTTPException(404, "No influencer profile found for this account.")
    code = (data.get("code") or data.get("discount_code") or "").strip()
    if not code:
        raise HTTPException(400, "Code is required")
    pricing = _influencer_subscription_pricing()
    from routers.merchant_app import _resolve_discount
    disc = _resolve_discount(code, "INFLUENCER", pricing["fee"])
    if not disc["code"]:
        raise HTTPException(400, "Invalid or inactive discount code.")
    taxable = max(0.0, round(pricing["fee"] - disc["discount_amount"], 2))
    gst_amount = round(taxable * pricing["gst_percent"] / 100, 2)
    total = round(taxable + gst_amount, 2)
    return {
        "ok": True,
        "code": disc["code"],
        "type": disc["type"],
        "discount_value": disc["discount_value"],
        "discount_amount": disc["discount_amount"],
        "message": disc["message"],
        "fee": pricing["fee"],
        "gst_percent": pricing["gst_percent"],
        "gst_amount": gst_amount,
        "total": total,
    }


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
