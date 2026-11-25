"""services/areas.py - safe area lookup shared by GET /areas (public) and GET /admin/areas (Phase 2, Stage 1, Task D).

Fixes four defects of the old inline code and changes nothing else (response shapes, the predefined Indian maps and the Indian partial
matching stay as they were):
  * a missing / blank city returns [] (it used to match the FIRST predefined city, because "" is contained in every key);
  * the city text is regex-ESCAPED before the database fallback (a value such as "Zzz(x" raised a server error);
  * the optional `country` parameter limits the database fallback to stores whose OWNER account is in that country, and skips the
    predefined list (it is Indian) for any other country, so identical city names in two countries do not mix areas;
  * without `country` (older apps, which only ever supported India) the behaviour is the old one, except that when stores of that
    city belong to MORE than one resolved country only the Indian stores' areas are used.
Store area data is only read, never changed. A store whose owner's country cannot be resolved is never counted as India here.
"""
import re
from typing import Dict, List, Optional

from bson import ObjectId

from utils import countries as _countries


class _LiveDb:
    def __getattr__(self, name):
        import database
        return getattr(database.db, name)


db = _LiveDb()


def _owner_iso(store: dict, cache: Dict[str, str]) -> str:
    """Resolved ISO of the store owner's account, or '' (no owner / unresolved - never defaulted to India)."""
    mid = str(store.get("merchant_id") or "")
    if not mid:
        return ""
    if mid not in cache:
        acct = None
        try:
            acct = db.accounts.find_one({"_id": ObjectId(mid)})
        except Exception:
            acct = None
        if acct is None:
            acct = db.accounts.find_one({"$or": [{"merchant_id": mid}, {"user_id": mid}]})
        cache[mid] = _countries.resolve_account_country(acct)["iso"] if acct else ""
    return cache[mid]


def areas_for(city, predefined: Dict[str, List[str]], country: Optional[str] = None) -> List[str]:
    """Areas of `city`. `country`: None = not sent; a supported ISO code; anything else (already validated by the caller as
    invalid) must be passed as '!invalid' and yields []."""
    key = str(city or "").strip().lower()
    if not key:
        return []
    if country == "!invalid":
        return []
    if not country or country == "IN":
        if key in predefined:
            return list(predefined[key])
        for k, v in predefined.items():
            if key in k or k in key:
                return list(v)
    rows = list(db.stores.find({"city": {"$regex": re.escape(key), "$options": "i"}, "area": {"$exists": True, "$ne": ""}},
                               {"area": 1, "merchant_id": 1}))
    cache: Dict[str, str] = {}
    by_country: Dict[str, set] = {}
    for st in rows:
        area = str(st.get("area") or "").strip()
        if not area:
            continue
        by_country.setdefault(_owner_iso(st, cache), set()).add(area)
    if country:
        found = by_country.get(country, set())
    else:
        resolved = [c for c in by_country if c]
        if len(resolved) > 1:                      # same city text in several countries: older apps are India-only
            found = by_country.get("IN", set())
        else:
            found = set().union(*by_country.values()) if by_country else set()
    return sorted(found)
