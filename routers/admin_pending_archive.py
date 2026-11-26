"""routers/admin_pending_archive.py - Admin -> Payments -> Archived Pending Transactions: /admin/payments/archive/*.

GET  /admin/payments/archive           list (search, date_from, date_to, country, page) + counts       Payments: view
POST /admin/payments/archive/delete    permanently delete the SELECTED archived records {ids: [...]}   Payments: delete

Only the archive collection (`subscriptions_archive`) is ever read or deleted here; the active Payments list and the
`subscriptions` collection are not reachable from these routes. The archive itself is filled by services.pending_archive.
Every delete attempt (done, refused or failed) is written to the permanent security audit trail.
"""
from datetime import datetime, timedelta
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from routers.admin import _check_perm, _scope, get_current_admin
from services import payment_country as _pcountry
from services import pending_archive as PA
from services import security_audit as _sec

router = APIRouter(prefix="/payments/archive", tags=["Admin Payments Archive"])

IST = timedelta(hours=5, minutes=30)


def _db():
    return PA.db


def _ist(d) -> str:
    d = PA._parse(d)
    return (d + IST).strftime("%d %b %Y, %I:%M %p") if d else ""


def _day(raw, end=False) -> Optional[datetime]:
    """'YYYY-MM-DD' (an IST calendar day) -> naive UTC boundary. A bad date is a 422, never silently ignored."""
    if not raw:
        return None
    try:
        d = datetime.strptime(str(raw).strip(), "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "Dates must be YYYY-MM-DD")
    return (d + timedelta(days=1) if end else d) - IST


def _row(s: dict, pcs, now: datetime) -> dict:
    db = _db()
    arch = PA._parse(s.get("archived_at"))
    base = {
        "id": str(s["_id"]), "status": s.get("status"), "pay_mode": s.get("pay_mode", ""),
        "total": s.get("total", 0), "base_price": s.get("base_price", s.get("price", 0)), "gst": s.get("gst", s.get("gst_amount", 0)),
        "created_at": _ist(s.get("created_at")), "archived_at": _ist(s.get("archived_at")),
        "delete_on": (arch + timedelta(days=PA.retention_days()) + IST).strftime("%d %b %Y") if arch else "",
        "_created": PA._parse(s.get("created_at")),
    }
    if s.get("entity_type") == "influencer":
        infl = None
        try:
            infl = db.influencers.find_one({"_id": ObjectId(s.get("influencer_id", ""))}, {"name": 1, "phone": 1, "account_id": 1})
        except Exception:
            pass
        base.update(type="influencer", item_label="Influencer Subscription", merchant_name=(infl or {}).get("name", ""),
                    merchant_phone=(infl or {}).get("phone", ""), store_name="Influencer Subscription", plan="One-Time Subscription")
        base.update(pcs.fields(s, [(infl or {}).get("account_id")] if (infl or {}).get("account_id") else [], [(infl or {}).get("phone")]))
        return base
    merchant = None
    try:
        merchant = db.accounts.find_one({"_id": ObjectId(s.get("merchant_id", ""))}) or db.merchants.find_one({"_id": ObjectId(s.get("merchant_id", ""))})
    except Exception:
        pass
    store = {}
    try:
        store = db.stores.find_one({"_id": ObjectId(s.get("store_id", ""))}, {"store_name": 1}) or {}
    except Exception:
        pass
    base.update(type=s.get("type", "store"), item_label=s.get("item_label") or ("Store – %s" % (s.get("plan") or "")),
                merchant_name=merchant.get("name") if merchant else "Unknown", merchant_phone=merchant.get("phone") if merchant else "",
                store_name=store.get("store_name", s.get("store_id", "")), plan=s.get("plan"))
    base.update(pcs.fields(s, [s.get("merchant_id")], [merchant.get("phone") if merchant else None]))
    return base


@router.get("")
def list_archive(search: str = "", date_from: str = "", date_to: str = "", country: str = "", page: int = 1, page_size: int = 25,
                 a=Depends(get_current_admin)):
    _check_perm(a, "Payments", "view")
    want = _pcountry.parse_filter(country)
    lo, hi = _day(date_from), _day(date_to, end=True)
    if lo and hi and lo >= hi:
        raise HTTPException(422, "date_from must not be after date_to")
    page, page_size = max(1, int(page or 1)), min(max(1, int(page_size or 25)), 100)
    scope_q = _scope(a, ("merchant_id",), ("merchant_phone",))
    now = datetime.utcnow()
    pcs = _pcountry.PaymentCountries()
    docs = list(_db()[PA.ARCHIVE].find(scope_q).sort("archived_at", -1).limit(5000))
    rows = [_row(d, pcs, now) for d in docs]
    total_all = len(rows)
    q = (search or "").strip().lower()
    out = []
    for r in rows:
        if want and not _pcountry.keep(r, want):
            continue
        c = r.get("_created")
        if lo and (c is None or c < lo):
            continue
        if hi and (c is None or c >= hi):
            continue
        if q and q not in " ".join(str(r.get(k) or "") for k in ("merchant_name", "merchant_phone", "store_name", "plan", "item_label", "country", "country_name", "id")).lower():
            continue
        out.append(r)
    for r in out:
        r.pop("_created", None)
    total = len(out)
    pages = max(1, -(-total // page_size))
    page = min(page, pages)
    return {"items": out[(page - 1) * page_size: page * page_size], "total": total, "archived_total": total_all, "page": page,
            "pages": pages, "page_size": page_size,
            "retention": {"archive_after_days": PA.archive_after_days(), "retention_days": PA.retention_days()}}


class DeleteIn(BaseModel):
    ids: List[str]


@router.post("/delete")
def delete_selected(body: DeleteIn, request: Request, a=Depends(get_current_admin)):
    ip = (request.client.host if request.client else "") or ""
    who = _sec.admin_ref(a)
    try:
        _check_perm(a, "Payments", "delete")
    except HTTPException:
        _sec.record("payments.archive.delete", who, "denied", module="Payments", ip=ip, details={"requested": len(body.ids or [])})
        raise
    ids = PA.clean_ids(body.ids)
    if not ids:
        raise HTTPException(422, "Select at least one archived transaction")
    if len(ids) > PA.MAX_MANUAL_DELETE:
        raise HTTPException(422, "Select at most %d records at a time" % PA.MAX_MANUAL_DELETE)
    # Only records that exist IN THE ARCHIVE and inside this admin's own data scope can be deleted.
    scope_q = _scope(a, ("merchant_id",), ("merchant_phone",))
    in_scope = [d["_id"] for d in _db()[PA.ARCHIVE].find({"$and": [{"_id": {"$in": ids}}, scope_q]}, {"_id": 1})] if scope_q else \
               [d["_id"] for d in _db()[PA.ARCHIVE].find({"_id": {"$in": ids}}, {"_id": 1})]
    # The audit entry is written FIRST and must succeed (strict): an unaudited permanent delete is refused.
    try:
        _sec.record("payments.archive.delete", who, "started", module="Payments", ip=ip, strict=True,
                    details={"requested": len(ids), "matched": len(in_scope), "ids": [str(i) for i in in_scope[:20]]})
    except _sec.AuditWriteError:
        raise HTTPException(503, "Could not write the audit log; nothing was deleted")
    try:
        deleted = PA.delete_archived(in_scope)
    except Exception:
        _sec.record("payments.archive.delete", who, "failed", module="Payments", ip=ip, details={"requested": len(ids)})
        raise HTTPException(500, "Could not delete the selected records")
    _sec.record("payments.archive.delete", who, "ok", module="Payments", ip=ip,
                details={"requested": len(ids), "deleted": deleted, "not_found_or_out_of_scope": len(ids) - deleted})
    return {"deleted": deleted, "requested": len(ids), "not_deleted": len(ids) - deleted}
