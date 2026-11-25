"""services/account_change.py - Support/Admin-assisted account edits (Phase 2 / Task 3).

Name edit and VERIFIED mobile-number change, from the Admin Dashboard only (there is no self-service change in the apps).

Number change = a request document (collection `account_phone_changes`) that moves through
    pending_approval  (only when the old number is unavailable: a Super Admin must approve first)
 -> awaiting_otp      (a one-time code was sent by WhatsApp to the NEW number)
 -> completed | cancelled | rejected | expired
The account keeps its OLD number untouched until the code is verified. Support staff cannot skip the code: there is no endpoint, flag or
role that applies a number without it. The code is generated here, stored only as a salted hash (same primitive as the admin login code),
sent through the existing WhatsApp OTP sender (services.whatsapp.send_otp_message) and never returned, logged or audited.

Applying the change updates the account's phone, phone_country, phone_e164, the linked legacy user/merchant documents, and the phone copies
that ownership checks read (stores, merchant banners, influencers). Historical invoices / subscriptions / orders keep the number they were
issued with. The account's session token is cleared so the next sign-in must use the new number.
"""
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from bson import ObjectId

from database import db
from services import admin_login_otp as _otp
from utils import phone as _ph

COLL = "account_phone_changes"
OTP_VALID_MINUTES = 5
RESEND_SECONDS = 30
MAX_RESENDS = 3
MAX_ATTEMPTS = 5
APPROVAL_VALID_HOURS = 24
ACTIVE = ("pending_approval", "awaiting_otp")


class ChangeError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _now():
    return datetime.utcnow()


def ensure_indexes_safe():
    try:
        db[COLL].create_index([("account_id", 1), ("status", 1)], name="apc_account_status")
        db[COLL].create_index([("new_phone", 1), ("status", 1)], name="apc_new_phone_status")
        return True
    except Exception:
        return False


def clean_name(raw) -> str:
    n = " ".join(str(raw or "").split())
    if len(n) < 2 or len(n) > 80:
        raise ChangeError(400, "Name must be 2 to 80 characters")
    if any(ord(c) < 32 for c in n) or "<" in n or ">" in n:
        raise ChangeError(400, "Name contains characters that are not allowed")
    return n


def validate_new_number(raw, country: Optional[str]):
    """-> RegPhone-like (e164, country). Same validator registration uses: the number must be a valid mobile of its country."""
    country = (str(country).strip().upper() if country else None) or None
    if country and not _ph.supported_country(country):
        raise ChangeError(400, "Unsupported country")
    r = _ph.register_phone(raw, country)
    if getattr(r, "error", "") or not r.phone or not r.country:
        raise ChangeError(400, "Enter a valid mobile number for the selected country")
    return r.phone, r.country


def _keys(e164: str) -> list:
    return list(_ph.account_lookup_keys(e164, None)) or [e164]


def number_in_use(e164: str, own_acct: dict, ignore_change_id=None) -> bool:
    """True when ANY other account / legacy user / merchant already uses this number, or another live request is about to."""
    keys = _keys(e164)
    own_id = own_acct.get("_id")
    for a in db.accounts.find({"phone": {"$in": keys}}, {"_id": 1}):
        if a["_id"] != own_id:
            return True
    linked = {str(x) for x in (own_acct.get("user_id"), own_acct.get("merchant_id")) if x}
    for coll in ("users", "merchants"):
        try:
            for d in db[coll].find({"phone": {"$in": keys}}, {"_id": 1}):
                if str(d["_id"]) not in linked:
                    return True
        except Exception:
            pass
    q = {"new_phone": e164, "status": {"$in": list(ACTIVE)}, "account_id": {"$ne": str(own_id)}}
    if ignore_change_id is not None:
        q["_id"] = {"$ne": ignore_change_id}
    return db[COLL].find_one(q) is not None


def digest(e164: str) -> str:
    return hashlib.sha256(("phone:" + str(e164)).encode()).hexdigest()[:24]


def mask(e164: str) -> str:
    s = str(e164 or "")
    return (s[:3] + "•" * max(0, len(s) - 5) + s[-2:]) if len(s) > 6 else "•" * len(s)


def _issue_code(change: dict) -> str:
    """Create a fresh code for this request, store only its hash, return the plain code (to be sent, never stored)."""
    code = _otp.generate_otp()
    salt = secrets.token_hex(16)
    db[COLL].update_one({"_id": change["_id"]}, {"$set": {
        "otp": {"hash": _otp.hash_otp(code, salt, change["_id"], change["account_id"]), "salt": salt,
                "sent_at": _now(), "expires_at": _now() + timedelta(minutes=OTP_VALID_MINUTES)},
        "attempts": 0, "status": "awaiting_otp"}, "$inc": {"sends": 1}})
    return code


def send_code(change: dict, sender=None) -> None:
    """Generate + send. On a delivery failure the code is invalidated and ChangeError(502) is raised (request stays resendable)."""
    if sender is None:
        from services import whatsapp as _wa
        sender = _wa.send_otp_message
    code = _issue_code(change)
    try:
        res = sender(change["new_phone"], code)
        ok = bool(res and res.get("ok"))
    except Exception:
        ok = False
    if not ok:
        db[COLL].update_one({"_id": change["_id"]}, {"$set": {"otp": None}})
        raise ChangeError(502, "The WhatsApp code could not be delivered to the new number. Try again shortly.")


def verify_code(change: dict, code: str) -> None:
    """Raises ChangeError unless `code` is the live code. Attempts are reserved atomically BEFORE the comparison."""
    if change.get("status") != "awaiting_otp" or not change.get("otp"):
        raise ChangeError(409, "No code is waiting to be verified for this request")
    if _now() > change["otp"]["expires_at"]:
        raise ChangeError(410, "The code has expired. Send a new code.")
    got = db[COLL].find_one_and_update({"_id": change["_id"], "status": "awaiting_otp", "attempts": {"$lt": MAX_ATTEMPTS}},
                                       {"$inc": {"attempts": 1}})
    if not got:
        raise ChangeError(429, "Too many wrong codes. Send a new code.")
    expect = _otp.hash_otp(str(code or "").strip(), change["otp"]["salt"], change["_id"], change["account_id"])
    if not secrets.compare_digest(expect, change["otp"]["hash"]):
        raise ChangeError(400, "The code is not correct")


def apply_change(change: dict, acct: dict, admin: dict) -> dict:
    """Atomically claim the request, re-check the number is still free, then write every phone field. Idempotent per request."""
    claimed = db[COLL].find_one_and_update({"_id": change["_id"], "status": "awaiting_otp"},
                                           {"$set": {"status": "applying", "applying_at": _now()}})
    if not claimed:
        raise ChangeError(409, "This request was already completed or cancelled")
    try:
        new, ctry = change["new_phone"], change["new_country"]
        if number_in_use(new, acct, ignore_change_id=change["_id"]):
            raise ChangeError(409, "This number is already assigned to another account")
        old = acct.get("phone", "")
        old_keys = set(_ph.account_lookup_keys(old, None) or [old]) if old else set()
        fields = _ph.phone_fields(new) or {"phone_country": ctry, "phone_e164": new}
        upd = {"phone": new, "phone_country": fields.get("phone_country", ctry), "phone_e164": fields.get("phone_e164", new),
               "token": None, "phone_changed_at": _now()}
        db.accounts.update_one({"_id": acct["_id"]}, {"$set": upd, "$push": {"previous_phones": {
            "phone_digest": digest(old), "masked": mask(old), "changed_at": _now(), "by": admin.get("id", ""), "request_id": str(change["_id"])}}})
        mid = str(acct.get("merchant_id") or acct["_id"])
        for coll, f in (("users", {"_id": acct.get("user_id")}), ("merchants", {"_id": acct.get("merchant_id")})):
            v = f["_id"]
            if not v:
                continue
            try:
                db[coll].update_one({"_id": ObjectId(str(v))}, {"$set": {"phone": new, "token": None}})
            except Exception:
                pass
        if old_keys:
            for coll, idf in (("stores", "merchant_id"), ("merchant_banners", "merchant_id")):
                try:
                    db[coll].update_many({idf: {"$in": list({mid, str(acct["_id"])})}, "merchant_phone": {"$in": list(old_keys)}},
                                         {"$set": {"merchant_phone": new}})
                except Exception:
                    pass
            try:
                db.influencers.update_many({"account_id": str(acct["_id"]), "phone": {"$in": list(old_keys)}}, {"$set": {"phone": new}})
            except Exception:
                pass
        db[COLL].update_one({"_id": change["_id"]}, {"$set": {"status": "completed", "completed_at": _now(), "otp": None}})
        return {"old_masked": mask(old), "new_masked": mask(new)}
    except Exception:
        db[COLL].update_one({"_id": change["_id"], "status": "applying"}, {"$set": {"status": "awaiting_otp"}})
        raise


def public_view(c: dict) -> dict:
    """Safe view of a request: masked numbers, no code, no hash."""
    return {"id": str(c["_id"]), "account_id": c["account_id"], "status": c["status"], "new_phone_masked": mask(c.get("new_phone", "")),
            "new_country": c.get("new_country", ""), "old_number_unavailable": bool(c.get("old_unavailable")),
            "reason": c.get("reason", ""), "requested_by": (c.get("requested_by") or {}).get("name", ""),
            "approved_by": (c.get("approved_by") or {}).get("name", ""),
            "requested_at": c["requested_at"].isoformat() + "Z" if c.get("requested_at") else "",
            "code_expires_at": (c["otp"]["expires_at"].isoformat() + "Z") if c.get("otp") else ""}
