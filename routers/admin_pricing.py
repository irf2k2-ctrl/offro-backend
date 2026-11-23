"""routers/admin_pricing.py - admin API for the Pricing & Plans module: /admin/pricing-config/*.

Country-wise platform prices, tax and reward conversion. Logic lives in services/pricing_config.py. Nothing here is called by the
mobile apps or by checkout, and nothing here creates a payment. India's values stay in the existing `pricing` document.

Permissions (same model as the existing Pricing & GST screen)
  read   -> the "Pricing & GST" module's view permission
  change -> the "Settings" module's edit permission (what PUT /admin/pricing already requires)
  A dashboard user that is restricted to one country can only see and change that country, and cannot add countries.
"""
from fastapi import APIRouter, Depends, HTTPException

from routers.admin import _check_perm, get_current_admin
from services import dashboard_country as _dc
from services import pricing_config as P

router = APIRouter(prefix="/pricing-config", tags=["Pricing & Plans"])


def _ref(a: dict) -> str:
    return str(a.get("full_name") or a.get("name") or a.get("email") or a.get("username") or a.get("mobile") or a.get("phone") or "admin")[:80]


def _view(a):
    _check_perm(a, "Pricing & GST", "view")


def _edit(a):
    _check_perm(a, "Settings", "edit")


def _allowed(a, code: str) -> bool:
    s = _dc.scope(a)
    return s == "" or s == (code or "").strip().upper()


def _require_country(a, code: str):
    if not _allowed(a, code):
        raise HTTPException(403, {"code": "country_scope", "message": "Your account is limited to another country"})


@router.get("/countries")
def list_countries(a=Depends(get_current_admin)):
    _view(a)
    out = []
    for c in P.list_countries():
        if not _allowed(a, c["code"]):
            continue
        r = P.read_prices(c["code"])
        tax = r["tax"]
        out.append({**c, "tax_configured": tax["configured"], "tax_error": tax["error"],
                    "reward_configured": r["reward"]["configured"], "reward": r["reward"],
                    "plans_configured": sum(1 for p in r["store_plans"] if p["configured"])})
    return {"countries": out, "can_add": _dc.scope(a) == ""}


@router.post("/countries")
def add_country(data: dict, a=Depends(get_current_admin)):
    _edit(a)
    if _dc.scope(a) != "":
        raise HTTPException(403, {"code": "country_scope", "message": "Only an unrestricted admin can add a country"})
    return {"country": P.create_country(data, _ref(a))}


@router.put("/countries/{code}")
def edit_country(code: str, data: dict, a=Depends(get_current_admin)):
    _edit(a)
    _require_country(a, code)
    return {"country": P.update_country(code, data, _ref(a))}


@router.post("/countries/{code}/status")
def country_status(code: str, data: dict, a=Depends(get_current_admin)):
    _edit(a)
    _require_country(a, code)
    return {"country": P.set_country_status(code, data.get("status"), _ref(a))}


@router.get("/countries/{code}/prices")
def get_prices(code: str, a=Depends(get_current_admin)):
    _view(a)
    _require_country(a, code)
    return P.read_prices(P.clean_code(code))


@router.put("/countries/{code}/prices/{section}")
def put_section(code: str, section: str, data: dict, a=Depends(get_current_admin)):
    _edit(a)
    _require_country(a, code)
    return {"section": P.save_section(code, section, data, _ref(a))}


@router.get("/history")
def get_history(country: str = "", limit: int = 50, a=Depends(get_current_admin)):
    _view(a)
    s = _dc.scope(a)
    code = (country or "").strip().upper() or None
    if s != "":
        code = s                                   # a country-restricted admin only ever sees their own country's history
    return {"history": P.history(code, limit)}
