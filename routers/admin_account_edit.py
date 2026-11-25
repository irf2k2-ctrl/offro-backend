"""routers/admin_account_edit.py - Support/Admin account edit (Phase 2 / Task 3). Dashboard only; no app route exists.

  PUT  /admin/accounts/{id}/edit-name                    Accounts: edit   (+ country / city scope of the account)
  GET  /admin/accounts/{id}/phone-changes                Accounts: edit
  POST /admin/accounts/{id}/phone-change                 Accounts: edit   -> sends a WhatsApp code to the NEW number (or waits for approval)
  POST /admin/phone-changes/{cid}/approve | reject       Super Admin only (never the requester) - old number unavailable
  POST /admin/phone-changes/{cid}/resend | verify | cancel    Accounts: edit + scope
Every step is written to the permanent security audit trail (services.security_audit). A number is applied ONLY by verify with the right code.
"""
from datetime import datetime, timedelta

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request

from database import db
from routers.admin import _check_perm, _city_filter, get_current_admin
from services import account_change as AC
from services import dashboard_country as _dc
from services import security_audit as _sec
from utils import countries as _countries

router = APIRouter()


def _ip(request: Request) -> str:
    return (request.client.host if request and request.client else "")[:64]


def _acct(a, account_id) -> dict:
    try:
        acct = db.accounts.find_one({"_id": ObjectId(account_id)})
    except Exception:
        acct = None
    if not acct:
        raise HTTPException(404, "Account not found")
    _dc.require_account(a, acct)                                   # country restriction (404 for another / unknown country)
    cf = _city_filter(a)
    if cf and not db.accounts.find_one({"$and": [{"_id": acct["_id"]}, cf]}):
        raise HTTPException(404, "Account not found")               # assigned-city restriction
    return acct


def _target(acct) -> dict:
    info = _countries.account_country(acct)
    return {"type": "account", "id": str(acct["_id"]), "name": acct.get("name", ""), "country": info["iso"] if info else ""}


def _audit(action, a, outcome, acct, details=None, request=None, strict=False):
    _sec.record(action, _sec.admin_ref(a), outcome, target=_target(acct), module="Accounts", details=details,
                ip=_ip(request) if request else "", strict=strict)


def _change(a, cid) -> tuple:
    try:
        c = db[AC.COLL].find_one({"_id": ObjectId(cid)})
    except Exception:
        c = None
    if not c:
        raise HTTPException(404, "Request not found")
    acct = _acct(a, c["account_id"])
    return c, acct


def _expire(c):
    """An approval older than APPROVAL_VALID_HOURS is dead."""
    if c["status"] == "pending_approval" and datetime.utcnow() - c["requested_at"] > timedelta(hours=AC.APPROVAL_VALID_HOURS):
        db[AC.COLL].update_one({"_id": c["_id"], "status": "pending_approval"}, {"$set": {"status": "expired"}})
        raise HTTPException(410, "This request expired. Start a new one.")


def _fail(e: AC.ChangeError):
    raise HTTPException(e.status, e.detail)


@router.put("/accounts/{account_id}/edit-name")
def edit_name(account_id: str, data: dict, request: Request, a=Depends(get_current_admin)):
    _check_perm(a, "Accounts", "edit")
    acct = _acct(a, account_id)
    try:
        new = AC.clean_name(data.get("name"))
    except AC.ChangeError as e:
        _fail(e)
    old = acct.get("name", "")
    if new == old:
        return {"ok": True, "name": new, "changed": False}
    _audit("account.name_change", a, "started", acct, {"old_name": old[:80], "new_name": new}, request, strict=True)
    db.accounts.update_one({"_id": acct["_id"]}, {"$set": {"name": new, "name_edited_at": datetime.utcnow()}})
    for coll, ref in (("users", acct.get("user_id")), ("merchants", acct.get("merchant_id"))):
        if ref:
            try:
                db[coll].update_one({"_id": ObjectId(str(ref))}, {"$set": {"name": new}})
            except Exception:
                pass
    _audit("account.name_change", a, "ok", acct, {"old_name": old[:80], "new_name": new}, request)
    return {"ok": True, "name": new, "changed": True}


@router.get("/accounts/{account_id}/phone-changes")
def list_changes(account_id: str, a=Depends(get_current_admin)):
    _check_perm(a, "Accounts", "edit")
    acct = _acct(a, account_id)
    rows = list(db[AC.COLL].find({"account_id": str(acct["_id"])}).sort("requested_at", -1).limit(10))
    return {"requests": [AC.public_view(r) for r in rows]}


@router.post("/accounts/{account_id}/phone-change")
def request_change(account_id: str, data: dict, request: Request, a=Depends(get_current_admin)):
    _check_perm(a, "Accounts", "edit")
    acct = _acct(a, account_id)
    try:
        e164, ctry = AC.validate_new_number(data.get("new_phone"), data.get("country"))
    except AC.ChangeError as e:
        _fail(e)
    iso = _dc.scope(a)
    if iso and ctry != iso:
        _audit("account.phone_change_requested", a, "denied", acct, {"reason": "country_out_of_scope", "new_country": ctry}, request)
        raise HTTPException(403, "You can only assign numbers of your own country")
    if acct.get("phone") in AC._keys(e164) or e164 == acct.get("phone_e164"):   # also the legacy forms of the same number
        raise HTTPException(400, "The new number is the same as the current number")
    if AC.number_in_use(e164, acct):
        _audit("account.phone_change_requested", a, "denied", acct, {"reason": "number_in_use", "new": AC.mask(e164)}, request)
        raise HTTPException(409, "This number is already assigned to another account")
    live = db[AC.COLL].find_one({"account_id": str(acct["_id"]), "status": {"$in": list(AC.ACTIVE)}})
    if live:
        raise HTTPException(409, "This account already has an open number-change request. Complete or cancel it first.")
    unavailable = bool(data.get("old_number_unavailable"))
    reason = str(data.get("reason") or "").strip()[:200]
    if unavailable and len(reason) < 5:
        raise HTTPException(400, "Give the reason the old number cannot be used")
    doc = {"account_id": str(acct["_id"]), "new_phone": e164, "new_country": ctry, "old_unavailable": unavailable, "reason": reason,
           "status": "pending_approval" if unavailable else "awaiting_otp", "requested_by": _sec.admin_ref(a),
           "requested_at": datetime.utcnow(), "attempts": 0, "sends": 0, "otp": None}
    det = {"old": AC.mask(acct.get("phone", "")), "new": AC.mask(e164), "new_digest": AC.digest(e164), "new_country": ctry,
           "old_unavailable": unavailable, "reason": reason}
    _audit("account.phone_change_requested", a, "started", acct, det, request, strict=True)
    cid = db[AC.COLL].insert_one(doc).inserted_id
    doc["_id"] = cid
    if unavailable:
        return {"ok": True, "request": AC.public_view(doc), "next": "Super Admin approval is required before a code is sent."}
    try:
        AC.send_code(doc)
    except AC.ChangeError as e:
        _audit("account.phone_change_otp_sent", a, "failed", acct, {"request": str(cid)}, request)
        raise HTTPException(e.status, e.detail)
    _audit("account.phone_change_otp_sent", a, "ok", acct, {"request": str(cid), "new": AC.mask(e164)}, request)
    return {"ok": True, "request": AC.public_view(db[AC.COLL].find_one({"_id": cid})), "next": "Enter the code the customer received on WhatsApp."}


def _super(a):
    if a.get("role_name") != "Super Admin":
        raise HTTPException(403, "Only a Super Admin can approve this")


@router.post("/phone-changes/{cid}/approve")
def approve(cid: str, request: Request, a=Depends(get_current_admin)):
    _check_perm(a, "Accounts", "edit")
    c, acct = _change(a, cid)
    if a.get("role_name") != "Super Admin":
        _audit("account.phone_change_approved", a, "denied", acct, {"request": cid, "reason": "not_super_admin"}, request)
        _super(a)
    _expire(c)
    if c["status"] != "pending_approval":
        raise HTTPException(409, "This request is not waiting for approval")
    if (c.get("requested_by") or {}).get("id") == _sec.admin_ref(a)["id"]:
        _audit("account.phone_change_approved", a, "denied", acct, {"request": cid, "reason": "self_approval"}, request)
        raise HTTPException(403, "A request must be approved by a different Super Admin")
    if AC.number_in_use(c["new_phone"], acct, ignore_change_id=c["_id"]):
        raise HTTPException(409, "This number is already assigned to another account")
    _audit("account.phone_change_approved", a, "ok", acct, {"request": cid}, request, strict=True)
    db[AC.COLL].update_one({"_id": c["_id"], "status": "pending_approval"},
                           {"$set": {"approved_by": _sec.admin_ref(a), "approved_at": datetime.utcnow()}})
    c = db[AC.COLL].find_one({"_id": c["_id"]})
    try:
        AC.send_code(c)
    except AC.ChangeError as e:
        db[AC.COLL].update_one({"_id": c["_id"]}, {"$set": {"status": "awaiting_otp"}})   # approved; the code can be re-sent
        _audit("account.phone_change_otp_sent", a, "failed", acct, {"request": cid}, request)
        raise HTTPException(e.status, e.detail)
    _audit("account.phone_change_otp_sent", a, "ok", acct, {"request": cid}, request)
    return {"ok": True, "request": AC.public_view(db[AC.COLL].find_one({"_id": c["_id"]}))}


@router.post("/phone-changes/{cid}/reject")
def reject(cid: str, data: dict, request: Request, a=Depends(get_current_admin)):
    _check_perm(a, "Accounts", "edit")
    c, acct = _change(a, cid)
    _super(a)
    if c["status"] != "pending_approval":
        raise HTTPException(409, "This request is not waiting for approval")
    db[AC.COLL].update_one({"_id": c["_id"], "status": "pending_approval"}, {"$set": {"status": "rejected", "rejected_by": _sec.admin_ref(a)}})
    _audit("account.phone_change_rejected", a, "ok", acct, {"request": cid, "note": str(data.get("note") or "")[:200]}, request)
    return {"ok": True}


@router.post("/phone-changes/{cid}/resend")
def resend(cid: str, request: Request, a=Depends(get_current_admin)):
    _check_perm(a, "Accounts", "edit")
    c, acct = _change(a, cid)
    if c["status"] != "awaiting_otp":
        raise HTTPException(409, "No code can be sent for this request in its current state")
    if c.get("sends", 0) >= 1 + AC.MAX_RESENDS:
        raise HTTPException(429, "Too many codes were sent. Start a new request.")
    last = (c.get("otp") or {}).get("sent_at")
    if last and (datetime.utcnow() - last).total_seconds() < AC.RESEND_SECONDS:
        raise HTTPException(429, "Please wait a few seconds before sending another code")
    try:
        AC.send_code(c)
    except AC.ChangeError as e:
        _audit("account.phone_change_otp_sent", a, "failed", acct, {"request": cid}, request)
        raise HTTPException(e.status, e.detail)
    _audit("account.phone_change_otp_sent", a, "ok", acct, {"request": cid, "resend": True}, request)
    return {"ok": True, "request": AC.public_view(db[AC.COLL].find_one({"_id": c["_id"]}))}


@router.post("/phone-changes/{cid}/verify")
def verify(cid: str, data: dict, request: Request, a=Depends(get_current_admin)):
    _check_perm(a, "Accounts", "edit")
    c, acct = _change(a, cid)
    try:
        AC.verify_code(c, data.get("code"))
    except AC.ChangeError as e:
        _audit("account.phone_change_verify", a, "failed", acct, {"request": cid, "http": e.status}, request)
        _fail(e)
    _audit("account.phone_change_applied", a, "started", acct, {"request": cid, "old": AC.mask(acct.get("phone", "")), "new": AC.mask(c["new_phone"]),
           "new_digest": AC.digest(c["new_phone"]), "new_country": c["new_country"], "old_unavailable": bool(c.get("old_unavailable")),
           "approved_by": (c.get("approved_by") or {}).get("name", "")}, request, strict=True)
    try:
        res = AC.apply_change(c, acct, _sec.admin_ref(a))
    except AC.ChangeError as e:
        _audit("account.phone_change_applied", a, "failed", acct, {"request": cid, "http": e.status}, request)
        _fail(e)
    _audit("account.phone_change_applied", a, "ok", acct, {"request": cid, **res}, request)
    return {"ok": True, **res}


@router.post("/phone-changes/{cid}/cancel")
def cancel(cid: str, request: Request, a=Depends(get_current_admin)):
    _check_perm(a, "Accounts", "edit")
    c, acct = _change(a, cid)
    if c["status"] not in AC.ACTIVE:
        raise HTTPException(409, "This request is already closed")
    db[AC.COLL].update_one({"_id": c["_id"], "status": {"$in": list(AC.ACTIVE)}}, {"$set": {"status": "cancelled", "otp": None}})
    _audit("account.phone_change_cancelled", a, "ok", acct, {"request": cid}, request)
    return {"ok": True}
