"""services/banner_countries.py - country targeting of admin banners (admin_banners) and admin-created promo sliders (promo_sliders).

A banner has an optional `countries` list of ISO codes (the one Country Master). Missing or empty = ALL countries, so every banner created
before this feature stays visible everywhere exactly as before; nothing is migrated or rewritten.

Dashboard rule (same as popup campaigns): a country-restricted dashboard user only sees / changes banners targeted to ONLY their own country;
global (all-country / legacy) banners and banners that include other countries belong to unrestricted users. Super Admin and unrestricted
roles are never limited.
"""
from typing import Iterable, List, Optional

from fastapi import HTTPException

from services import dashboard_country as _dc
from utils import countries as _countries


def normalize(raw) -> List[str]:
    """Validated, de-duplicated, upper-case ISO list. None / [] / "" -> [] (all countries). Unknown code -> 400."""
    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        raw = [x for x in raw.replace(";", ",").split(",")]
    if not isinstance(raw, (list, tuple, set)):
        raise HTTPException(400, "countries must be a list of country codes")
    out: List[str] = []
    for item in raw:
        iso = str(item or "").strip().upper()
        if not iso:
            continue
        if not _countries.country_info(iso):
            raise HTTPException(400, "Unsupported country: %s" % iso)
        if iso not in out:
            out.append(iso)
    return out


def of(doc: Optional[dict]) -> List[str]:
    return [str(c).strip().upper() for c in ((doc or {}).get("countries") or []) if str(c).strip()]


def targets(doc: Optional[dict], iso: str) -> bool:
    """Does the banner target this country? No `countries` = every country."""
    cs = of(doc)
    return (not cs) or (str(iso or "").upper() in cs)


def visible_to_admin(admin: dict, doc: Optional[dict]) -> bool:
    iso = _dc.scope(admin)
    if not iso:
        return True
    if iso == _dc.INVALID_SCOPE:
        return False
    cs = of(doc)
    return bool(cs) and all(c == iso for c in cs)


def require_visible(admin: dict, doc: Optional[dict]) -> None:
    if not visible_to_admin(admin, doc):
        raise HTTPException(404, "Not found")          # existence of another country's banner is not revealed


def for_admin(admin: dict, raw) -> List[str]:
    """The `countries` value an admin may store. Unrestricted: as sent (empty = all countries). Restricted: only their own country."""
    cs = normalize(raw)
    iso = _dc.scope(admin)
    if not iso:
        return cs
    if iso == _dc.INVALID_SCOPE:
        raise HTTPException(403, "Your account's country is not set up")
    if not cs:
        return [iso]
    if any(c != iso for c in cs):
        raise HTTPException(403, "You can only target your own country")
    return cs


def restricted_query(admin: dict) -> dict:
    """Mongo filter that matches only what a restricted admin may see ({} for an unrestricted one)."""
    iso = _dc.scope(admin)
    if not iso:
        return {}
    if iso == _dc.INVALID_SCOPE:
        return {"_id": None}
    return {"countries": [iso]}
