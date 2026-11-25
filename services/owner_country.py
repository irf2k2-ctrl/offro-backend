"""services/owner_country.py - the country (and display currency) of the OWNER of a store / product / deal.

Stores, products, deals and merchant banners carry no country of their own: it is the owner account's (utils.countries.account_country,
the same rule the dashboard uses). An owner that cannot be found, or whose country cannot be told, is a legacy India-era record: India.

This only READS. It never writes, converts a price or changes a stored value. The display currency is the country's configured currency
(India: INR / the rupee sign, exactly as before; another country: its Pricing & Plans country record, services.pricing_config).
A country with no configured currency gets an EMPTY currency (the app then shows the bare number) - India's currency is never assumed for it.

One OwnerCountries instance is made per request: it remembers every owner it has already looked up, so a list of products costs one account
lookup per distinct merchant.
"""
from typing import Optional

from services import dashboard_country as _dc
from services import pricing_config as _pc
from utils import countries as _countries

INDIA = "IN"
INR = {"currency_code": "INR", "currency_symbol": "₹"}


class OwnerCountries:
    def __init__(self):
        self._by_key = {}
        self._money = {}

    def iso_of_account(self, acct: Optional[dict]) -> str:
        info = _countries.account_country(acct) if acct else None
        return info["iso"] if info else INDIA

    def iso(self, *owner_keys) -> str:
        """Country ISO of the first owner key that resolves to an account; India when none does."""
        for key in owner_keys:
            k = str(key or "").strip()
            if not k:
                continue
            if k not in self._by_key:
                try:
                    acct = _dc._account_by_key(k)
                except Exception:
                    acct = None
                self._by_key[k] = self.iso_of_account(acct) if acct else None
            if self._by_key[k]:
                return self._by_key[k]
        return INDIA

    def money(self, iso: str) -> dict:
        iso = (iso or INDIA).upper()
        if iso == INDIA:
            return dict(INR)
        if iso not in self._money:
            c = None
            try:
                c = _pc.get_country(iso)
            except Exception:
                c = None
            code = str((c or {}).get("currency_code") or "").strip().upper()
            sym = str((c or {}).get("currency_symbol") or "").strip()
            self._money[iso] = {"currency_code": code, "currency_symbol": sym or code}
        return dict(self._money[iso])

    def fields(self, *owner_keys) -> dict:
        """{'country', 'currency_code', 'currency_symbol'} for the owner (additive response fields)."""
        iso = self.iso(*owner_keys)
        return {"country": iso, **self.money(iso)}

    def in_country(self, iso_wanted: str, *owner_keys) -> bool:
        return self.iso(*owner_keys) == (iso_wanted or "").upper()

    def influencer_iso(self, doc: dict) -> str:
        """An influencer's country: its own stored `country` (set by the dashboard), else its account's, else its phone's; India otherwise."""
        c = str((doc or {}).get("country") or "").strip().upper()
        if c and _countries.country_info(c):
            return c
        if (doc or {}).get("account_id"):
            k = str(doc["account_id"])
            if k in self._by_key or _dc._account_by_key(k) is not None:
                return self.iso(k)
        ph = str((doc or {}).get("phone") or "").strip()
        if ph:
            info = _countries.account_country({"phone": ph})
            if info:
                return info["iso"]
        return INDIA

    def owner_iso_of_banner(self, doc: dict) -> Optional[str]:
        """Country of a merchant-attributed banner / slider from its owner (merchant account, else its store's owner, else the phone).
        None when it has no owner at all (an admin-made banner)."""
        d = doc or {}
        if d.get("merchant_id") and _dc._account_by_key(str(d["merchant_id"])) is not None:
            return self.iso(d["merchant_id"])
        sid = str(d.get("store_id") or "")
        if sid:
            try:
                from bson import ObjectId
                from database import db
                st = db.stores.find_one({"_id": ObjectId(sid)}, {"merchant_id": 1})
            except Exception:
                st = None
            if st and st.get("merchant_id"):
                return self.iso(st["merchant_id"])
        ph = str(d.get("merchant_phone") or "").strip()
        if ph:
            info = _countries.account_country({"phone": ph})
            if info:
                return info["iso"]
        if d.get("merchant_id") or sid or ph or d.get("source_banner_id"):
            return INDIA                                   # an owner that cannot be told is a legacy India-era record
        return None


def requested_country(raw) -> Optional[str]:
    """The optional `country` query parameter of a public list. None = not sent (older apps / guests: unfiltered, exactly as before);
    the upper-case ISO code of a supported country; INVALID for anything else (the list is then empty - never another country's)."""
    c = str(raw or "").strip().upper()
    if not c:
        return None
    return c if _countries.country_info(c) else INVALID


INVALID = "!invalid"
