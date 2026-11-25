"""services/payment_country.py - country and currency of a payment row in the dashboard Payments lists (Phase 2 / Task 2).

READ-ONLY. It never writes, never converts an amount and never changes a stored status. The original amount and the currency it was
charged in are shown exactly as recorded.

Country of a payment, in this order:
  1. the record's own valid `country` (source 'record');
  2. otherwise the country of the paying account (the merchant / influencer owner), by the single shared rule
     utils.countries.resolve_account_country (source 'explicit' | 'parsed' | 'legacy_india');
  3. otherwise empty (source 'unresolved'). India is NOT assumed for a payment whose owner cannot be found or told.

Currency of a payment: the record's own `currency_code` / `currency_symbol` ('recorded'). A record without them predates the
international rollout and is an India record, so an Indian (or legacy-Indian) payment without a stamp is shown as INR ('legacy_default_inr',
the same rule services.country_context.stamp has always applied). A non-Indian or unresolved payment without a stamp shows NO currency
('not_recorded') - INR is never assumed for it.
"""
from typing import Iterable, Optional

from fastapi import HTTPException

from services import dashboard_country as _dc
from utils import countries as _countries
from utils import phone as _ph

NONE_FILTER = "NONE"          # ?country=NONE lists payments whose country could not be determined


def parse_filter(raw) -> Optional[str]:
    """None = no filter; an ISO code of a supported country; NONE_FILTER. Anything else is a 400 (never silently ignored)."""
    c = str(raw or "").strip().upper()
    if not c or c == "ALL":
        return None
    if c == NONE_FILTER:
        return NONE_FILTER
    if _countries.country_info(c):
        return c
    raise HTTPException(400, "Unsupported country")


class PaymentCountries:
    """One per request: remembers each owner it has looked up."""

    def __init__(self):
        self._cache = {}

    def _owner(self, ids: Iterable, phones: Iterable):
        for k in ids:
            k = str(k or "").strip()
            if not k:
                continue
            ck = "id:" + k
            if ck not in self._cache:
                try:
                    self._cache[ck] = _dc._account_by_key(k)
                except Exception:
                    self._cache[ck] = None
            if self._cache[ck]:
                return self._cache[ck]
        for p in phones:
            p = str(p or "").strip()
            if not p:
                continue
            ck = "ph:" + p
            if ck not in self._cache:
                try:
                    from database import db
                    keys = _ph.account_lookup_keys(p) or [p]
                    self._cache[ck] = db.accounts.find_one({"phone": {"$in": list(keys)}})
                except Exception:
                    self._cache[ck] = None
            if self._cache[ck]:
                return self._cache[ck]
        return None

    def fields(self, rec: dict, ids: Iterable = (), phones: Iterable = ()) -> dict:
        rec = rec or {}
        iso, source = "", "unresolved"
        own = str(rec.get("country") or "").strip().upper()
        if own and _countries.country_info(own):
            iso, source = own, "record"
        else:
            acct = self._owner(ids, phones)
            if acct:
                r = _countries.resolve_account_country(acct)
                if r["iso"]:
                    iso, source = r["iso"], r["source"]
        code = str(rec.get("currency_code") or "").strip().upper()
        sym = str(rec.get("currency_symbol") or "").strip()
        if code:
            cur_src = "recorded"
        elif iso == "IN":
            code, sym, cur_src = "INR", sym or "₹", "legacy_default_inr"
        else:
            code, sym, cur_src = "", "", "not_recorded"
        info = _countries.country_info(iso) if iso else None
        return {"country": iso, "country_name": info["name"] if info else "", "country_source": source,
                "currency_code": code, "currency_symbol": sym or code, "currency_source": cur_src}


def keep(row: dict, wanted: Optional[str]) -> bool:
    if not wanted:
        return True
    if wanted == NONE_FILTER:
        return not row.get("country")
    return row.get("country") == wanted
