"""routers/admin_cities.py - admin API for Admin -> Cities (City Master, every country): /admin/cities/*.

Logic lives in services/city_master.py and services/city_import.py. Permissions use the existing "Cities" module
(view / add / edit / delete). A dashboard user restricted to one country (services.dashboard_country) sees and changes only that
country. Image upload, "seed" and "assign-country" stay in routers/admin.py.

This router is registered BEFORE routers.admin so its fixed paths (popular-order, summary, verify, import/*) are never captured by
the {city_id} routes.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from routers.admin import _check_perm, get_current_admin
from services import city_import as I
from services import city_master as M
from services import dashboard_country as _dc

router = APIRouter(prefix="/cities", tags=["Admin Cities"])


def _who(a) -> str:
    return str(a.get("full_name") or a.get("name") or a.get("email") or a.get("username") or "admin")[:80]


def _fail(e: M.CityError):
    return HTTPException(e.status, {"code": e.code, "message": e.message, "field": e.field})


def _country(a, value) -> str:
    """The ISO code the request works on, validated, and inside the dashboard user's country restriction."""
    try:
        iso = M.require_country(value)
    except M.CityError as e:
        raise _fail(e)
    s = _dc.scope(a)
    if s and s != iso:
        raise HTTPException(403, {"code": "country_scope", "message": "Your account is limited to another country"})
    return iso


@router.get("")
def list_cities(country: str = "", q: str = "", status: str = "", popular: Optional[bool] = None, a=Depends(get_current_admin)):
    """Every city (no filter = the old behaviour), alphabetical. ?country=SA | ?country=unassigned, ?q=, ?status=active|inactive, ?popular=true."""
    _check_perm(a, "Cities", "view")
    scope = _dc.scope(a)
    c = (country or "").strip()
    unassigned = c.lower() == "unassigned"
    iso = ""
    if c and not unassigned:
        iso = M.require_country(c) if M._city.normalize_country(c) else "__none__"
    if iso == "__none__":
        return []
    if unassigned and scope:
        return []
    return M.list_cities(iso=iso, q=q, status=status, popular=popular, scope=scope, unassigned=unassigned)


@router.get("/summary")
def summary(a=Depends(get_current_admin)):
    _check_perm(a, "Cities", "view")
    return M.summary(_dc.scope(a))


@router.post("")
def create_city(body: dict, a=Depends(get_current_admin)):
    _check_perm(a, "Cities", "add")
    body = body or {}
    _country(a, body.get("country"))
    try:
        cid = M.create(body, who=_who(a))
    except M.CityError as e:
        raise _fail(e)
    return {"ok": True, "id": cid}


@router.put("/popular-order")
def popular_order(body: dict, a=Depends(get_current_admin)):
    """Exactly `ids` are popular for `country`, in that order; every other city of the country is unflagged."""
    _check_perm(a, "Cities", "edit")
    iso = _country(a, (body or {}).get("country"))
    try:
        res = M.set_popular_order(iso, (body or {}).get("ids"))
    except M.CityError as e:
        raise _fail(e)
    return {"ok": True, **res}


@router.put("/{city_id}")
def update_city(city_id: str, body: dict, a=Depends(get_current_admin)):
    _check_perm(a, "Cities", "edit")
    try:
        r = M.update(city_id, body or {}, who=_who(a), scope=_dc.scope(a))
    except M.CityError as e:
        raise _fail(e)
    return {"ok": True, "city": r}


@router.post("/{city_id}/status")
def city_status(city_id: str, body: dict, a=Depends(get_current_admin)):
    _check_perm(a, "Cities", "edit")
    if "active" not in (body or {}):
        raise HTTPException(400, {"code": "active_required", "message": "active is required", "field": "active"})
    try:
        r = M.update(city_id, {"active": body["active"]}, who=_who(a), scope=_dc.scope(a))
    except M.CityError as e:
        raise _fail(e)
    return {"ok": True, "city": r}


@router.delete("/{city_id}")
def delete_city(city_id: str, a=Depends(get_current_admin)):
    _check_perm(a, "Cities", "delete")
    try:
        M.delete(city_id, scope=_dc.scope(a))
    except M.CityError as e:
        raise _fail(e)
    return {"ok": True}


# ── import / verify ──────────────────────────────────────────────────────────────────────────────────────
@router.get("/import/sources")
def import_sources(a=Depends(get_current_admin)):
    _check_perm(a, "Cities", "add")
    return {"sources": I.reference_sources(), "csv_columns": list(I.CSV_COLUMNS), "max_rows": I.MAX_ROWS}


@router.post("/import/preview")
def import_preview(body: dict, a=Depends(get_current_admin)):
    """Dry run: nothing is written."""
    _check_perm(a, "Cities", "add")
    iso = _country(a, (body or {}).get("country"))
    try:
        return I.preview(iso, body or {})
    except M.CityError as e:
        raise _fail(e)


@router.post("/import/apply")
def import_apply(body: dict, a=Depends(get_current_admin)):
    """Inserts only the new, valid rows (checked again now). Needs "confirm": true."""
    _check_perm(a, "Cities", "add")
    iso = _country(a, (body or {}).get("country"))
    try:
        return {"ok": True, **I.apply(iso, body or {}, who=_who(a))}
    except M.CityError as e:
        raise _fail(e)


@router.post("/import/{batch}/undo")
def import_undo(batch: str, a=Depends(get_current_admin)):
    _check_perm(a, "Cities", "delete")
    try:
        return {"ok": True, **I.undo(batch, scope=_dc.scope(a))}
    except M.CityError as e:
        raise _fail(e)


@router.get("/verify")
def verify(country: str = "IN", a=Depends(get_current_admin)):
    """Read-only report: unassigned cities, reference cities missing from the City Master, possible duplicates."""
    _check_perm(a, "Cities", "view")
    iso = _country(a, country)
    return I.verify(iso)
