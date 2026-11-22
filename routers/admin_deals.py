"""
Admin Deals routes  —  /admin/deals/...   (collection: deals)

The admin Deals module manages the SAME `deals` documents the merchant app
creates (routers/merchant_app.py) and the customer app reads (routers/public.py).
There is no second deal system: every change here is a plain update of that
document, so the merchant app and the customer app pick it up immediately.

Status vocabulary (shared with routers/deals.py and public.py):
  active   - shown to customers (unless its end date has passed)
  paused   - deactivated by an admin/merchant; hidden from customers
  expired  - set by the expiry sweep; also derived from end_date when listing
Customers only ever see status == "active" and not past end_date, so
"deactivate" simply sets `paused`, and "activate" sets `active` again.

Permissions: RBAC module "Deals" (view / edit / delete). Super Admin bypasses.
City-scoped admins only see and touch deals of stores in their cities.
"""
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from database import db
from routers.admin import get_current_admin, _check_perm, _city_filter, _created_iso, _scope
from routers.merchant_app import _cloudinary_upload
from routers.public import _is_deal_expired

router = APIRouter(prefix="/admin", tags=["Admin Deals"])
MODULE = "Deals"
_MAX_IMAGE_CHARS = 12_000_000          # ~9 MB of base64; URLs are far smaller
_ACTIVE, _PAUSED, _EXPIRED = "active", "paused", "expired"
_STATUS_FILTERS = {"active", "inactive", "expired"}


# ── helpers ─────────────────────────────────────────────────────────────────

def _oid(v, what="deal"):
    try:
        return ObjectId(str(v))
    except Exception:
        raise HTTPException(400, f"Invalid {what} ID")


def _ymd(v) -> str:
    """Date as YYYY-MM-DD when parseable, else the stored text (never raises)."""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    t = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(t[:10] if fmt == "%Y-%m-%d" else t, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return t


def _parse_ymd(v):
    try:
        return datetime.strptime(_ymd(v), "%Y-%m-%d")
    except ValueError:
        return None


def _display_status(d: dict) -> str:
    """active | inactive | expired - what an admin should read."""
    st = str(d.get("status") or _ACTIVE).lower()
    if st == _EXPIRED or (st == _ACTIVE and _is_deal_expired(d)):
        return "expired"
    return "active" if st == _ACTIVE else "inactive"


def _scoped_store_ids(a):
    """None = unrestricted; otherwise the set of store ids this admin may see."""
    scope = _scope(a)                    # city scope (unchanged) + the owner's country for a country-restricted user
    if not scope:
        return None
    return {str(s["_id"]) for s in db.stores.find(scope, {"_id": 1})}


def _load_stores(deals):
    ids = []
    for d in deals:
        try:
            ids.append(ObjectId(str(d.get("store_id") or "")))
        except Exception:
            pass
    return {str(s["_id"]): s for s in db.stores.find({"_id": {"$in": ids}})} if ids else {}


def _load_merchants(deals, stores):
    keys = set()
    for d in deals:
        if d.get("merchant_id"):
            keys.add(str(d["merchant_id"]))
    for s in stores.values():
        if s.get("merchant_id"):
            keys.add(str(s["merchant_id"]))
    oids = []
    for k in keys:
        try:
            oids.append(ObjectId(k))
        except Exception:
            pass
    return {str(m["_id"]): m for m in db.accounts.find({"_id": {"$in": oids}}, {"name": 1, "phone": 1})} if oids else {}


def _row(d: dict, stores: dict, merchants: dict) -> dict:
    store = stores.get(str(d.get("store_id") or "")) or {}
    mid = str(d.get("merchant_id") or store.get("merchant_id") or "")
    m = merchants.get(mid) or {}
    return {
        "id": str(d["_id"]),
        "title": d.get("title") or "",
        "description": d.get("description") or "",
        "image_url": d.get("image_url") or "",
        "discount": d.get("discount") if d.get("discount") is not None else "",
        "category": d.get("category") or "",
        "start_date": _ymd(d.get("start_date")),
        "end_date": _ymd(d.get("end_date")),
        "status": _display_status(d),
        "stored_status": str(d.get("status") or _ACTIVE),
        "created_at": str(d.get("created_at") or "")[:19],
        "created_at_iso": _created_iso(d.get("created_at")),
        "merchant_id": mid,
        "merchant_name": m.get("name") or store.get("merchant_name") or "",
        "merchant_phone": m.get("phone") or store.get("merchant_phone") or "",
        "store_id": str(d.get("store_id") or ""),
        "store_name": store.get("store_name") or ("" if store else "Unknown store"),
        "city": store.get("city") or "",
    }


def _get_deal_or_404(deal_id, a):
    d = db.deals.find_one({"_id": _oid(deal_id)})
    if not d:
        raise HTTPException(404, "Deal not found")
    allowed = _scoped_store_ids(a)
    if allowed is not None and str(d.get("store_id") or "") not in allowed:
        raise HTTPException(403, "This deal is outside your assigned cities")
    return d


def _sync_store_discount(store_id):
    """Keep stores.discount_percent (the merchant app writes it on create/update) in step
    with the deals customers can actually see: the best discount among the store's
    remaining active, non-expired deals, or removed when there are none.  Called after
    every edit / activate / deactivate / delete so a stale value is never left behind.
    (Customer /stores derives its offer from active deals directly; this keeps the
    stored field from going stale for anything else that reads it.)"""
    sid = str(store_id or "")
    try:
        oid = ObjectId(sid)
    except Exception:
        return
    best = None
    for d in db.deals.find({"store_id": sid}):
        if _display_status(d) != "active":
            continue
        try:
            v = float(str(d.get("discount")).strip().rstrip("%"))
        except (TypeError, ValueError):
            continue
        if v > 0 and (best is None or v > best):
            best = v
    if best is None:
        db.stores.update_one({"_id": oid}, {"$unset": {"discount_percent": ""}})
    else:
        db.stores.update_one({"_id": oid}, {"$set": {"discount_percent": int(best) if best == int(best) else best}})


def _bust_store_cache():
    try:
        from routers import admin as _adm
        _adm._store_cache["data"] = None
    except Exception:
        pass


def _clean_discount(v):
    if isinstance(v, bool):
        raise HTTPException(400, "Discount must be a number between 0 and 100")
    try:
        n = float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Discount must be a number between 0 and 100")
    if not (n == n) or n < 0 or n > 100:
        raise HTTPException(400, "Discount must be a number between 0 and 100")
    return int(n) if n == int(n) else n


# ── list ────────────────────────────────────────────────────────────────────

@router.get("/deals")
def list_deals(
    a=Depends(get_current_admin),
    page: int = 1,
    page_size: int = 25,
    search: str = "",
    merchant_id: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
):
    """All deals created by merchants. Filters: free-text search (title, description,
    store, merchant), merchant, status (active / inactive / expired) and created-date range."""
    _check_perm(a, MODULE, "view")
    status = (status or "").strip().lower()
    if status and status not in _STATUS_FILTERS:
        raise HTTPException(400, "status must be active, inactive or expired")
    d_from = _parse_ymd(date_from) if date_from else None
    d_to = _parse_ymd(date_to) if date_to else None
    if (date_from and not d_from) or (date_to and not d_to):
        raise HTTPException(400, "Dates must be YYYY-MM-DD")

    query = {}
    allowed = _scoped_store_ids(a)
    if allowed is not None:
        query["store_id"] = {"$in": sorted(allowed)}
    deals = list(db.deals.find(query))
    stores = _load_stores(deals)
    merchants = _load_merchants(deals, stores)
    rows = [_row(d, stores, merchants) for d in deals]

    # merchants present in scope, for the filter dropdown (before filtering)
    mer_opts = sorted({(r["merchant_id"], r["merchant_name"] or r["merchant_phone"] or r["merchant_id"])
                       for r in rows if r["merchant_id"]}, key=lambda x: x[1].lower())

    q = (search or "").strip().lower()
    if q:
        rows = [r for r in rows if q in " ".join(
            [r["title"], r["description"], r["store_name"], r["merchant_name"], r["merchant_phone"]]).lower()]
    if merchant_id:
        rows = [r for r in rows if r["merchant_id"] == str(merchant_id)]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if d_from or d_to:
        def _in_range(r):
            c = r["created_at_iso"][:10]
            cd = _parse_ymd(c) if c else None
            if cd is None:
                return False
            return (not d_from or cd >= d_from) and (not d_to or cd <= d_to)
        rows = [r for r in rows if _in_range(r)]

    rows.sort(key=lambda r: (r["created_at_iso"] != "", r["created_at_iso"], r["id"]), reverse=True)
    page_size = max(1, min(int(page_size or 25), 100))
    total = len(rows)
    pages = max(1, -(-total // page_size))
    page = min(max(1, int(page or 1)), pages)
    items = rows[(page - 1) * page_size: page * page_size]
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages,
            "merchants": [{"id": i, "name": n} for i, n in mer_opts]}


# ── detail ──────────────────────────────────────────────────────────────────

@router.get("/deals/{deal_id}")
def get_deal(deal_id: str, a=Depends(get_current_admin)):
    _check_perm(a, MODULE, "view")
    d = _get_deal_or_404(deal_id, a)
    stores = _load_stores([d])
    return _row(d, stores, _load_merchants([d], stores))


# ── edit ────────────────────────────────────────────────────────────────────

@router.put("/deals/{deal_id}")
def update_deal(deal_id: str, data: dict, a=Depends(get_current_admin)):
    """Edit title / description / discount / category / dates / image. Only the
    fields that are sent change. Merchant, store and creation data are not editable."""
    _check_perm(a, MODULE, "edit")
    d = _get_deal_or_404(deal_id, a)
    if not isinstance(data, dict):
        raise HTTPException(400, "Invalid request body")

    upd = {}
    if "title" in data:
        t = str(data.get("title") or "").strip()
        if not t:
            raise HTTPException(400, "Title is required")
        if len(t) > 120:
            raise HTTPException(400, "Title is too long (max 120 characters)")
        upd["title"] = t
    if "description" in data:
        desc = str(data.get("description") or "").strip()
        if len(desc) > 2000:
            raise HTTPException(400, "Description is too long (max 2000 characters)")
        upd["description"] = desc
    if "category" in data:
        upd["category"] = str(data.get("category") or "").strip()[:80]
    if "discount" in data:
        upd["discount"] = _clean_discount(data.get("discount"))

    start = _ymd(data["start_date"]) if "start_date" in data else _ymd(d.get("start_date"))
    end = _ymd(data["end_date"]) if "end_date" in data else _ymd(d.get("end_date"))
    for key, val in (("start_date", start), ("end_date", end)):
        if key in data:
            if str(data.get(key) or "").strip() and _parse_ymd(val) is None:
                raise HTTPException(400, f"{key.replace('_', ' ').capitalize()} must be YYYY-MM-DD")
            upd[key] = val if str(data.get(key) or "").strip() else ""
    s_dt, e_dt = _parse_ymd(start), _parse_ymd(end)
    if s_dt and e_dt and e_dt < s_dt:
        raise HTTPException(400, "Expiry date cannot be before the start date")

    if "image_url" in data:
        raw = data.get("image_url")
        if raw is not None:
            raw = str(raw).strip()
            if len(raw) > _MAX_IMAGE_CHARS:
                raise HTTPException(400, "Image is too large")
            upd["image_url"] = _cloudinary_upload(raw, folder="offro/deals") if raw else ""

    if not upd:
        raise HTTPException(400, "Nothing to update")
    upd["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
    upd["updated_by"] = str(a.get("full_name") or a.get("mobile") or "admin")
    db.deals.update_one({"_id": d["_id"]}, {"$set": upd})
    fresh = db.deals.find_one({"_id": d["_id"]})
    _sync_store_discount(fresh.get("store_id"))
    _bust_store_cache()
    stores = _load_stores([fresh])
    return {"message": "Deal updated", "deal": _row(fresh, stores, _load_merchants([fresh], stores))}


# ── activate / deactivate ───────────────────────────────────────────────────

@router.post("/deals/{deal_id}/status")
def set_deal_status(deal_id: str, data: dict, a=Depends(get_current_admin)):
    """{"active": true|false} - activate (status=active) or deactivate (status=paused)."""
    _check_perm(a, MODULE, "edit")
    d = _get_deal_or_404(deal_id, a)
    want = (data or {}).get("active")
    if not isinstance(want, bool):
        raise HTTPException(400, "active must be true or false")
    if want and _is_deal_expired(d):
        raise HTTPException(400, "This deal's expiry date has passed. Edit the expiry date before activating it.")
    new = _ACTIVE if want else _PAUSED
    db.deals.update_one({"_id": d["_id"]}, {"$set": {
        "status": new, "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "updated_by": str(a.get("full_name") or a.get("mobile") or "admin")}})
    fresh = db.deals.find_one({"_id": d["_id"]})
    _sync_store_discount(fresh.get("store_id"))
    _bust_store_cache()
    stores = _load_stores([fresh])
    return {"message": "Deal activated" if want else "Deal deactivated",
            "deal": _row(fresh, stores, _load_merchants([fresh], stores))}


# ── delete ──────────────────────────────────────────────────────────────────

@router.delete("/deals/{deal_id}")
def delete_deal(deal_id: str, a=Depends(get_current_admin)):
    """Permanent delete of one deal (the UI asks for confirmation first)."""
    _check_perm(a, MODULE, "delete")
    d = _get_deal_or_404(deal_id, a)
    db.deals.delete_one({"_id": d["_id"]})
    _sync_store_discount(d.get("store_id"))
    _bust_store_cache()
    return {"message": "Deal deleted", "id": str(d["_id"])}
