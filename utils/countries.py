"""utils/countries.py - read-only view of the app's supported-country master for display purposes.

The ONE country master is utils.phone.SUPPORTED_COUNTRIES (ISO code, English name, calling code). This module adds nothing to it and
keeps no list of its own: names, codes and the order always come from there, so a country added to the master appears everywhere
(account list, translation country mapping) with no second edit. The flag is computed from the ISO code (regional-indicator letters),
so it is not stored or hardcoded either.
"""
from typing import Optional

from utils import phone as _ph


def flag_emoji(iso: str) -> str:
    """The flag emoji for a two-letter ISO 3166-1 code ('SA' -> flag of Saudi Arabia), '' for anything else."""
    code = str(iso or "").strip().upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code)


def country_info(iso: str) -> Optional[dict]:
    """{'iso','name','dial','flag'} for a supported country, else None."""
    c = _ph.supported_country(str(iso or "").strip().upper()) if iso else None
    if c is None or c.iso != str(iso).strip().upper():      # accept ISO codes only here (supported_country also takes dial codes)
        return None
    return {"iso": c.iso, "name": c.name, "dial": c.dial, "flag": flag_emoji(c.iso)}


def supported_countries() -> list:
    """Every supported country, in the master's order."""
    return [country_info(c.iso) for c in _ph.SUPPORTED_COUNTRIES]


def supported_iso_codes() -> set:
    return {c.iso for c in _ph.SUPPORTED_COUNTRIES}


# Where a resolved country came from (resolve_account_country).
SOURCE_EXPLICIT = "explicit"          # the account's own supported `phone_country`
SOURCE_PARSED = "parsed"              # worked out from a stored number that carries its calling code (+ / 00)
SOURCE_LEGACY_INDIA = "legacy_india"  # a bare legacy Indian number shape (9876543210, 919876543210, 09876543210): taken as India
SOURCE_UNRESOLVED = "unresolved"
SOURCES = (SOURCE_EXPLICIT, SOURCE_PARSED, SOURCE_LEGACY_INDIA, SOURCE_UNRESOLVED)


def resolve_account_country(account: dict) -> dict:
    """The country of an account AND how it was found: {'iso': 'SA'|'', 'source': one of SOURCES, 'reason': str}.

    This is the single implementation of the rule; account_country() is a thin wrapper over it, so the two can never disagree.
    Rule (unchanged): a supported `phone_country` wins; otherwise the stored phone is read with the login parser (a number with a
    calling code), then as a legacy Indian shape; otherwise unresolved. `reason` explains an unresolved result (phone parser error
    code, 'no_phone' or 'legacy_mode') and is '' otherwise. Read-only: nothing is written and no fallback is added or removed.
    """
    acct = account or {}
    iso = str(acct.get("phone_country") or "").strip().upper()
    if iso and country_info(iso):
        return {"iso": iso, "source": SOURCE_EXPLICIT, "reason": ""}
    phone = str(acct.get("phone") or "").strip()
    if not phone:
        return {"iso": "", "source": SOURCE_UNRESOLVED, "reason": "no_phone"}
    if not _ph.intl_enabled():
        return {"iso": "", "source": SOURCE_UNRESOLVED, "reason": "legacy_mode"}
    try:
        r = _ph.parse(phone)
    except Exception:
        r = None
    if r is not None and r.ok and country_info(r.country):
        return {"iso": r.country, "source": SOURCE_PARSED, "reason": ""}
    try:
        legacy = _ph.legacy_india_keys(phone)
    except Exception:
        legacy = []
    if legacy:
        return {"iso": "IN", "source": SOURCE_LEGACY_INDIA, "reason": ""}
    return {"iso": "", "source": SOURCE_UNRESOLVED, "reason": (getattr(r, "error", "") or "invalid_number")}


def account_country(account: dict) -> Optional[dict]:
    """The country of an account document, or None when it cannot be told.

    Newer accounts carry `phone_country`. Older accounts do not, so it is worked out from the stored phone number with the same
    parser the login flow uses (a number with a calling code, or a legacy Indian number). Nothing is guessed beyond that and
    nothing is written back.
    """
    r = resolve_account_country(account)
    return country_info(r["iso"]) if r["iso"] else None
