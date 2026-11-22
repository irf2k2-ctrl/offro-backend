"""services/dashboard_country.py - country access for Dashboard users (Stage 4, Part A).

A dashboard user (collection `dashboard_users`) may carry ONE optional `country`: an ISO code from the single country master
(utils.phone.SUPPORTED_COUNTRIES, via utils.countries). Nothing here keeps a country list of its own.

Rules
  * Super Admin: never restricted (as before).
  * No `country` on the user (every existing user): NOT restricted by country - exactly today's behaviour; their city restriction
    still applies. Nobody is locked out and nobody gets wider access than they have now.
  * `country` set: the user can only see / change records of that country, ON TOP OF the existing city restriction.
  * A record's country comes from the owner's account phone (account.phone_country, else parsed from the stored phone - the same
    rule as utils.countries.account_country). Stores, products, deals, subscriptions, invoices, banners ... belong to a merchant
    account (merchant_id), so they take that account's country. A record whose owner country cannot be resolved is HIDDEN from a
    country-restricted user (fail closed). No migration and no new field on any record.
"""
from typing import Iterable, Optional

from fastapi import HTTPException

from utils import countries as _countries
from utils import phone as _ph

SUPER_ADMIN = "Super Admin"


class _LiveDb:
    """The CURRENT `database.db`, looked up at call time (like services.wa_audience does), never captured at import time.
    A module-level `from database import db` freezes whichever `database` module existed when this file was first imported;
    anything that replaces the module afterwards (the test suite does, and a plain `pytest` run imports this file earlier than
    the rest of the app) would leave these helpers reading a different database than the routers that call them."""
    def __getattr__(self, name):
        import database                 # resolved through sys.modules on every call
        return getattr(database.db, name)


db = _LiveDb()


# ── the user's own country ────────────────────────────────────────────────────────────────────────────────────────────
def clean_country(value) -> str:
    """'' (no restriction) or a supported ISO code (upper-cased). Anything else -> 422 (never silently dropped)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(422, {"code": "invalid_country", "message": "country must be a country code"})
    v = value.strip().upper()
    if not v:
        return ""
    if v not in _countries.supported_iso_codes():
        raise HTTPException(422, {"code": "invalid_country", "message": f"{value[:20]!r} is not a supported country"})
    return v


INVALID_SCOPE = "!invalid"      # never a real ISO code: matches no country, so every helper below denies everything


def scope(admin: dict) -> str:
    """The restriction of this dashboard user:
      ''            unrestricted - Super Admin, or a user with NO country (missing / null / blank = the agreed legacy behaviour);
      'SA' ...      a valid supported country: restricted to it;
      INVALID_SCOPE a country IS stored but is not a supported code (typo, unsupported, wrong type, corrupted): FAIL CLOSED. It is a
                    truthy value that equals no country, so every check built on scope() grants nothing (no data, no mutations).
    It never turns an invalid value into 'unrestricted'."""
    admin = admin or {}
    _r = admin.get("role")
    role = admin.get("role_name") or (_r.get("role_name") if isinstance(_r, dict) else _r) or ""
    if not role and admin.get("role_id"):                 # a raw dashboard_users document (e.g. the WhatsApp chat auth)
        try:
            role = (db.dashboard_roles.find_one({"_id": admin["role_id"]}) or {}).get("role_name", "")
        except Exception:
            role = ""
    if role == SUPER_ADMIN:
        return ""
    raw = admin.get("country")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""                                          # no country assigned: legacy user
    if isinstance(raw, str) and raw.strip().upper() in _countries.supported_iso_codes():
        return raw.strip().upper()
    return INVALID_SCOPE


# ── which accounts belong to a country (Mongo side, mirrors utils.countries.account_country) ──────────────────────────
def account_query(iso: str) -> dict:
    """Filter for `accounts` of one country. phone_country wins; accounts without it are classified by their phone exactly as
    account_country does: +<dial>/00<dial> numbers by their calling code, bare legacy Indian shapes as India. Fail closed:
    an unrecognisable phone matches no country."""
    c = _ph._BY_ISO.get(iso)
    if c is None:
        return {"_id": None}
    explicit = {"phone_country": {"$in": [c.iso, c.iso.lower()]}}
    no_pc = {"phone_country": {"$in": [None, ""]}}
    if c.iso == "IN":
        rx = r"^((\+|00)91\d{10}|91\d{10}|0\d{10}|\d{10})$"
    else:
        rx = r"^(\+|00)" + c.dial + r"\d+$"
    return {"$or": [explicit, {"$and": [no_pc, {"phone": {"$regex": rx}}]}]}


def owner_ids(iso: str):
    """(ids, phones) of the accounts of one country: str(_id), their merchant_id, and their stored phone."""
    ids, phones = set(), set()
    for d in db.accounts.find(account_query(iso), {"_id": 1, "merchant_id": 1, "user_id": 1, "phone": 1}):
        ids.add(str(d["_id"]))
        for k in ("merchant_id", "user_id"):
            if d.get(k):
                ids.add(str(d[k]))
        if d.get("phone"):
            phones.add(str(d["phone"]))
    return sorted(ids), sorted(phones)


def owned_query(iso: str, id_fields: Iterable[str] = ("merchant_id",), phone_fields: Iterable[str] = ()) -> dict:
    """Filter for records owned by an account of this country (fail closed: nobody owns it -> matches nothing)."""
    ids, phones = owner_ids(iso)
    parts = [{f: {"$in": ids}} for f in id_fields]
    parts += [{f: {"$in": phones}} for f in phone_fields]
    return {"$or": parts} if len(parts) > 1 else parts[0]


# ── single-record checks (detail / update / delete) ───────────────────────────────────────────────────────────────────
def _account_by_key(key):
    if not key:
        return None
    key = str(key)
    try:
        from bson import ObjectId
        d = db.accounts.find_one({"_id": ObjectId(key)})
        if d:
            return d
    except Exception:
        pass
    return db.accounts.find_one({"$or": [{"merchant_id": key}, {"user_id": key}]})


def account_in_scope(admin: dict, acct: Optional[dict]) -> bool:
    iso = scope(admin)
    if not iso:
        return True
    if not acct:
        return False
    info = _countries.account_country(acct)
    return bool(info and info["iso"] == iso)


def require_account(admin: dict, acct: Optional[dict]) -> None:
    """404 (not 403, so existence is not revealed) for an account outside the admin's country."""
    if not account_in_scope(admin, acct):
        raise HTTPException(404, "Not found")


def record_in_scope(admin: dict, rec: Optional[dict], id_fields: Iterable[str] = ("merchant_id",),
                    phone_fields: Iterable[str] = ()) -> bool:
    iso = scope(admin)
    if not iso:
        return True
    if not rec:
        return False
    for f in id_fields:
        acct = _account_by_key(rec.get(f))
        if acct is not None:
            return account_in_scope(admin, acct)
    for f in phone_fields:
        p = rec.get(f)
        if p:
            acct = db.accounts.find_one({"phone": str(p)})
            if acct is not None:
                return account_in_scope(admin, acct)
            info = _countries.account_country({"phone": str(p)})
            if info:
                return info["iso"] == iso
    return False


def require_record(admin: dict, rec: Optional[dict], id_fields: Iterable[str] = ("merchant_id",),
                   phone_fields: Iterable[str] = ()) -> None:
    if not record_in_scope(admin, rec, id_fields, phone_fields):
        raise HTTPException(404, "Not found")
