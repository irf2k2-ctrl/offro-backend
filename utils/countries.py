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


def account_country(account: dict) -> Optional[dict]:
    """The country of an account document, or None when it cannot be told.

    Newer accounts carry `phone_country`. Older accounts do not, so it is worked out from the stored phone number with the same
    parser the login flow uses (a number with a calling code, or a legacy Indian number). Nothing is guessed beyond that and
    nothing is written back.
    """
    acct = account or {}
    iso = str(acct.get("phone_country") or "").strip().upper()
    info = country_info(iso) if iso else None
    if info:
        return info
    phone = str(acct.get("phone") or "").strip()
    if not phone:
        return None
    try:
        derived = _ph.phone_fields(phone).get("phone_country", "")
    except Exception:
        derived = ""
    return country_info(derived) if derived else None
