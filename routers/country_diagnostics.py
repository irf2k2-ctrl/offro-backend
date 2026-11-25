"""routers/country_diagnostics.py - READ-ONLY country diagnostics for the Admin Dashboard (Phase 2, Stage 1, Task 7a).

GET /admin/country-diagnostics   (GET only; nothing here writes)

Permission : the "Accounts" module's view permission (no new module, no role change).
Scope      : the admin's existing city and country scope (the same filter as the Accounts list). A country-restricted admin only
             ever sees accounts of their own country; an account whose country cannot be resolved is hidden from them (fail closed),
             exactly as in the Accounts list.
Phone masks: full numbers only for Super Admin and for dashboard users with NO city and NO country restriction; everyone else gets
             masked numbers.
"""
from fastapi import APIRouter, Depends, HTTPException

from database import db
from routers.admin import _accounts_query, _check_perm, get_current_admin
from services import country_diagnostics as _diag
from services import dashboard_country as _dc
from utils import countries as _countries

router = APIRouter(tags=["Country diagnostics"])

_FIELDS = {"name": 1, "phone": 1, "phone_country": 1, "phone_e164": 1, "roles": 1, "status": 1, "city": 1}


@router.get("/country-diagnostics")
def country_diagnostics(country: str = "", source: str = "", issue: str = "", role: str = "",
                        page: int = 1, page_size: int = 25, a=Depends(get_current_admin)):
    _check_perm(a, "Accounts", "view")
    country = (country or "").strip().upper()
    if country and country != "UNRESOLVED" and country not in _countries.supported_iso_codes():
        raise HTTPException(422, "Unsupported country filter")
    country = "unresolved" if country == "UNRESOLVED" else country
    if source and source not in _countries.SOURCES:
        raise HTTPException(422, "Unknown source filter")
    if issue and issue not in _diag.ISSUES:
        raise HTTPException(422, "Unknown issue filter")
    if role and role not in _diag.ROLE_GROUPS + ("other",):
        raise HTTPException(422, "Unknown role filter")
    cursor = db.accounts.find(_accounts_query(a), _FIELDS)
    out = _diag.build_report(cursor, country=country, source=source, issue=issue, role=role,
                             mask=not _diag.sees_full_numbers(a), page=page, page_size=page_size)
    out["countries"] = [{"iso": c["iso"], "name": c["name"]} for c in _countries.supported_countries()]
    out["scope"] = _dc.scope(a)
    return out
