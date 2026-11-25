"""services/country_diagnostics.py - READ-ONLY country diagnostics for accounts (Phase 2, Stage 1, Task 7a).

Shows how the country of every account is resolved today, so the unsafe fallbacks can be reviewed before anything is changed (Task 7b).
It uses the ONE shared resolver (utils.countries.resolve_account_country) and the existing Country Master; it adds no fallback, changes
no fallback and writes NOTHING (no account, no log, no index). Phone numbers are masked for every admin except Super Admin and
dashboard users with no city/country restriction.

Resolution source of an account
  explicit      the account has a supported `phone_country`
  parsed        no usable `phone_country`; the stored number carries its calling code (+... / 00...)
  legacy_india  no usable `phone_country`; the stored number is a bare legacy Indian shape and is taken as India (the legacy fallback)
  unresolved    none of the above (nothing is guessed)

Issues flagged on an account (an account can have several)
  unresolved             no country could be determined
  country_mismatch       `phone_country` disagrees with the calling code of the stored phone or `phone_e164`
  invalid_phone_country  `phone_country` is stored but is not a supported ISO code (it is ignored by the resolver)
  legacy_india           the country is India only because of the bare-number fallback
"""
from typing import Dict, List, Optional

from utils import countries as _countries
from utils import phone as _ph

ISSUES = ("unresolved", "country_mismatch", "invalid_phone_country", "legacy_india")
ROLE_GROUPS = ("customer", "merchant", "influencer")
_ROLE_OF = {"user": "customer", "customer": "customer", "merchant": "merchant", "influencer": "influencer"}


def role_groups(account: dict) -> List[str]:
    """customer / merchant / influencer groups of an account (an account may be in several). [] = no recognised role."""
    roles = account.get("roles")
    if isinstance(roles, str):
        roles = [roles]
    out = []
    for r in roles or []:
        g = _ROLE_OF.get(str(r).strip().lower())
        if g and g not in out:
            out.append(g)
    return out


def sees_full_numbers(admin: dict) -> bool:
    """Full phone numbers only for Super Admin and for dashboard users with NO country restriction and NO city restriction."""
    from services import dashboard_country as _dc
    admin = admin or {}
    if admin.get("role_name") == _dc.SUPER_ADMIN:
        return True
    cities = admin.get("assigned_cities") or []
    return not _dc.scope(admin) and "*" in cities


def mask_phone(value) -> str:
    """'+966501234567' -> '+96•••••••67'. Keeps just enough to recognise the number; '' stays ''."""
    s = str(value or "").strip()
    if not s:
        return ""
    if len(s) <= 6:
        return "•" * len(s)
    return s[:3] + "•" * (len(s) - 5) + s[-2:]


def _prefix_iso(value) -> str:
    """The supported country whose calling code the number STARTS with ('+'/'00' numbers only; a bare number has no prefix).
    '' when there is no prefix. 'other' when it has a prefix of a country that is not supported."""
    s = _ph._clean(value)
    if not s.startswith("+"):
        return ""
    digits = s[1:]
    if not digits.isascii() or not digits.isdigit():
        return ""
    for c in sorted(_ph.SUPPORTED_COUNTRIES, key=lambda x: -len(x.dial)):
        if digits.startswith(c.dial):
            return c.iso
    return "other"


def classify(account: dict) -> dict:
    """Diagnostics of ONE account document (no I/O)."""
    res = _countries.resolve_account_country(account)
    raw = str(account.get("phone_country") or "").strip().upper()
    issues = []
    if res["source"] == _countries.SOURCE_UNRESOLVED:
        issues.append("unresolved")
    if raw and not _countries.country_info(raw):
        issues.append("invalid_phone_country")
    if res["source"] == _countries.SOURCE_EXPLICIT:
        for field in ("phone", "phone_e164"):
            p = _prefix_iso(account.get(field))
            if p and p != res["iso"]:
                issues.append("country_mismatch")
                break
    if res["source"] == _countries.SOURCE_LEGACY_INDIA:
        issues.append("legacy_india")
    return {"iso": res["iso"], "source": res["source"], "reason": res["reason"], "issues": issues, "stored_country": raw}


def _blank_sources() -> Dict[str, int]:
    return {s: 0 for s in _countries.SOURCES}


def build_report(cursor, *, country: str = "", source: str = "", issue: str = "", role: str = "",
                 mask: bool = True, page: int = 1, page_size: int = 25) -> dict:
    """One pass over an iterable of account documents. The summary always covers ALL of them (so the counts stay stable while the
    list is filtered); the list is filtered by country (resolved ISO or 'unresolved'), source, issue and role group."""
    page_size = max(1, min(int(page_size or 25), 100))
    summary = {"total": 0, "by_source": _blank_sources(), "by_issue": {i: 0 for i in ISSUES},
               "by_country": {}, "by_role": {g: {"total": 0, "by_source": _blank_sources()} for g in ROLE_GROUPS}}
    summary["by_role"]["other"] = {"total": 0, "by_source": _blank_sources()}
    rows = []
    for acct in cursor:
        d = classify(acct)
        groups = role_groups(acct) or ["other"]
        summary["total"] += 1
        summary["by_source"][d["source"]] += 1
        key = d["iso"] or "unresolved"
        summary["by_country"][key] = summary["by_country"].get(key, 0) + 1
        for i in d["issues"]:
            summary["by_issue"][i] += 1
        for g in groups:
            summary["by_role"][g]["total"] += 1
            summary["by_role"][g]["by_source"][d["source"]] += 1
        if country and key != country:
            continue
        if source and d["source"] != source:
            continue
        if issue and issue not in d["issues"]:
            continue
        if role and role not in groups:
            continue
        rows.append((acct, d, groups))
    total = len(rows)
    pages = max(1, -(-total // page_size))
    page = min(max(1, int(page or 1)), pages)
    items = []
    for acct, d, groups in rows[(page - 1) * page_size: page * page_size]:
        ph, e164 = acct.get("phone") or "", acct.get("phone_e164") or ""
        items.append({
            "id": str(acct.get("_id")), "name": acct.get("name") or "", "roles": groups,
            "status": acct.get("status") or "", "city": acct.get("city") or "",
            "phone": mask_phone(ph) if mask else str(ph),
            "phone_e164": mask_phone(e164) if mask else str(e164),
            "stored_country": d["stored_country"], "country": d["iso"], "source": d["source"],
            "reason": d["reason"], "issues": d["issues"],
        })
    return {"summary": summary, "items": items, "total": total, "page": page, "page_size": page_size, "pages": pages,
            "masked": bool(mask)}
