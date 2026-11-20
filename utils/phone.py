"""utils/phone.py - the ONE shared phone-number helper (Phase C: international phone foundation).

E.164 is the canonical form (``+<country code><national number>``, no spaces, no leading zero). Validity comes from Google's
libphonenumber (the ``phonenumbers`` package), never from hand-written prefix rules and never from "the last 10 digits".

Supported countries (Phase 1): India +91, Jordan +962, Nepal +977, Philippines +63, Saudi Arabia +966. Only MOBILE numbers are
accepted (these numbers receive OTPs and WhatsApp messages).

Design rules
  * A number that starts with "+" or "00" carries its own country code and is parsed as international.
  * A number WITHOUT a country code is NEVER guessed. The caller must say which country it is (the app's country selector), or
    pass ``legacy_india=True`` for requests from old app versions that send no country (they only ever supported India).
  * Digits are never truncated: a Nepal or Philippines 10-digit mobile and an Indian 10-digit mobile are different numbers and
    get different E.164 values, so they can never collide.
  * Existing Indian records are found through ``lookup_keys`` / ``lookup_keys_for``, which return every legacy Indian format
    (``9876543210``, ``919876543210``, ``+919876543210``, ``09876543210``) for India ONLY. Other countries match their exact
    E.164 value (with and without "+") and nothing else.

Nothing here reads or writes the database.

Phase D wires the account-facing helpers at the bottom of this file (``account_lookup_keys``, ``register_phone``, ``owner_key`` ...)
into the routes. ``PHONE_INTL_MODE=legacy`` in the environment makes every one of them behave exactly like the old last-10-digit code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType, NumberParseException


@dataclass(frozen=True)
class Country:
    iso: str          # ISO 3166-1 alpha-2
    name: str
    dial: str         # country calling code, digits only


# Alphabetical by name, as the app's selector shows them.
SUPPORTED_COUNTRIES = (
    Country("IN", "India", "91"),
    Country("JO", "Jordan", "962"),
    Country("NP", "Nepal", "977"),
    Country("PH", "Philippines", "63"),
    Country("SA", "Saudi Arabia", "966"),
)
_BY_ISO = {c.iso: c for c in SUPPORTED_COUNTRIES}
_BY_DIAL = {c.dial: c for c in SUPPORTED_COUNTRIES}

# Error codes (stable strings; safe to map to user messages later).
EMPTY = "empty"
INVALID_FORMAT = "invalid_format"
COUNTRY_REQUIRED = "country_required"        # a bare number with no country given
UNSUPPORTED_COUNTRY = "unsupported_country"
COUNTRY_MISMATCH = "country_mismatch"        # e.g. Nepal selected but a +91 number typed
INVALID_NUMBER = "invalid_number"            # not a possible/valid number for that country
NOT_MOBILE = "not_mobile"                    # valid, but a landline or other non-mobile type

_MOBILE_TYPES = (PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE)

# Arabic-Indic and Extended Arabic-Indic digits are accepted on input and turned into 0-9.
_DIGIT_MAP = {}
for _i in range(10):
    _DIGIT_MAP[0x0660 + _i] = ord("0") + _i
    _DIGIT_MAP[0x06F0 + _i] = ord("0") + _i
_STRIP = {ord(c): None for c in " \t\r\n-().‎‏‪‫‬ "}


@dataclass(frozen=True)
class PhoneResult:
    ok: bool
    e164: str = ""             # "+966501234567" when ok
    country: str = ""          # ISO code when ok
    national: str = ""         # national significant number, e.g. "501234567"
    error: str = ""            # one of the error codes above when not ok

    def __bool__(self) -> bool:
        return self.ok


def supported_country(iso_or_dial: str) -> Optional[Country]:
    """The supported Country for an ISO code ('SA') or a calling code ('966' or '+966'), else None."""
    s = str(iso_or_dial or "").strip().upper().lstrip("+")
    return _BY_ISO.get(s) or _BY_DIAL.get(s)


def _clean(raw) -> str:
    s = str(raw if raw is not None else "").strip().translate(_DIGIT_MAP).translate(_STRIP)
    if s.startswith("00"):
        s = "+" + s[2:]
    return s


def parse(raw, country: Optional[str] = None, legacy_india: bool = False) -> PhoneResult:
    """Parse and validate one phone number.

    raw          what the user typed or the database holds.
    country      ISO code (or calling code) of the country the user selected. Required for numbers without "+" or "00".
    legacy_india True ONLY for requests from old app versions that send no country: a bare number is then taken as Indian.
    """
    s = _clean(raw)
    if not s or s == "+":
        return PhoneResult(False, error=EMPTY)
    body = s[1:] if s.startswith("+") else s
    if not body.isascii() or not body.isdigit():
        return PhoneResult(False, error=INVALID_FORMAT)

    chosen = None
    if country:
        chosen = supported_country(country)
        if chosen is None:
            return PhoneResult(False, error=UNSUPPORTED_COUNTRY)

    if s.startswith("+"):
        region = None                                   # the number carries its own country code
    elif chosen is not None:
        region = chosen.iso
    elif legacy_india:
        region = "IN"
    else:
        return PhoneResult(False, error=COUNTRY_REQUIRED)

    try:
        num = phonenumbers.parse(s, region)
    except NumberParseException:
        return PhoneResult(False, error=INVALID_FORMAT)

    iso = phonenumbers.region_code_for_number(num)
    found = _BY_ISO.get(iso or "")
    if found is None:
        return PhoneResult(False, error=UNSUPPORTED_COUNTRY)
    if chosen is not None and found.iso != chosen.iso:
        return PhoneResult(False, error=COUNTRY_MISMATCH)
    if not phonenumbers.is_valid_number_for_region(num, found.iso):
        return PhoneResult(False, error=INVALID_NUMBER)
    if phonenumbers.number_type(num) not in _MOBILE_TYPES:
        return PhoneResult(False, error=NOT_MOBILE)

    return PhoneResult(
        True,
        e164=phonenumbers.format_number(num, PhoneNumberFormat.E164),
        country=found.iso,
        national=str(num.national_number),
    )


def to_e164(raw, country: Optional[str] = None, legacy_india: bool = False) -> str:
    """The E.164 string, or '' when the number is not valid. Convenience over parse()."""
    return parse(raw, country, legacy_india).e164


def wa_digits(e164: str) -> str:
    """The digits-only form (no plus) used as the WhatsApp contact key and Meta wa_id, e.g. '966501234567'."""
    return "".join(ch for ch in str(e164 or "") if ch.isdigit())


def _country_of(e164: str) -> str:
    r = parse(e164)
    return r.country if r.ok else ""


def lookup_keys(e164: str) -> List[str]:
    """Every stored string that may legitimately mean this number, for a database `$in` query.

    India  : '+91XXXXXXXXXX', '91XXXXXXXXXX', 'XXXXXXXXXX', '0XXXXXXXXXX' (all formats ever written for Indian accounts).
    Others : '+<E164>' and '<E164 digits>' only. A bare national number is NEVER a key for a non-Indian country.
    """
    r = parse(e164)
    if not r.ok:
        return []
    digits = wa_digits(r.e164)
    if r.country == "IN":
        n = r.national
        return [r.e164, "91" + n, n, "0" + n]
    return [r.e164, digits]


def legacy_india_keys(raw) -> List[str]:
    """The OLD Indian variants for a number that has the SHAPE of an Indian number but may fail modern validation
    (for example an old test account such as 5551234567), so such existing records can still be found.

    Strictly Indian shapes only: 10 digits, 91 + 10 digits, +91 + 10 digits, 0 + 10 digits. Anything else returns [] (the old code
    would have cut ANY longer number down to its last 10 digits, which is the collision this helper removes).
    """
    s = _clean(raw)
    d = s[1:] if s.startswith("+") else s
    if not d.isascii() or not d.isdigit():
        return []
    if len(d) == 12 and d.startswith("91"):
        n = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        n = d[1:]
    elif len(d) == 10 and not s.startswith("+"):
        n = d
    else:
        return []
    return ["+91" + n, "91" + n, n, "0" + n]


def lookup_keys_for(raw, country: Optional[str] = None, legacy_india: bool = False) -> List[str]:
    """Keys for looking up an account from user input.

    A valid number gives its country-aware keys. If it is not valid but is an Indian-shaped legacy value and the request is
    Indian (country 'IN', or legacy_india for old app versions), the old Indian variants are returned so existing accounts still log
    in. Anything else returns [] (no lookup), never a guess.
    """
    r = parse(raw, country, legacy_india)
    if r.ok:
        return lookup_keys(r.e164)
    indian = (country is None and legacy_india) or (country is not None and supported_country(country) is not None
                                                    and supported_country(country).iso == "IN")
    if indian and r.error in (INVALID_NUMBER, NOT_MOBILE, INVALID_FORMAT):
        return legacy_india_keys(raw)
    return []


# ══════════════════════════════════════════════════════════════════════════════
# Phase D - account-facing helpers used by the routes
# ══════════════════════════════════════════════════════════════════════════════

def intl_enabled() -> bool:
    """False when the environment variable PHONE_INTL_MODE=legacy: every helper below then reproduces the old code exactly."""
    return os.environ.get("PHONE_INTL_MODE", "").strip().lower() != "legacy"


def legacy_variants(raw) -> list:
    """The ORIGINAL routers.users._phone_variants, verbatim (used only in legacy mode)."""
    p = str(raw).strip().replace(" ", "").replace("-", "")
    d = p[1:] if p.startswith("+") else p
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    last10 = d[-10:] if len(d) >= 10 else d
    return list({p, f"+91{last10}", f"91{last10}", last10, f"0{last10}"})


def legacy_normalise(raw) -> str:
    """The ORIGINAL routers.users._normalise_phone, verbatim (used only in legacy mode)."""
    p = str(raw).strip().replace(" ", "").replace("-", "")
    d = p[1:] if p.startswith("+") else p
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    last10 = d[-10:] if len(d) >= 10 else d
    return f"+91{last10}" if len(last10) == 10 else p


def request_country(data) -> Optional[str]:
    """The optional `country` field of a request body (ISO code or calling code), or None when absent/blank."""
    try:
        c = str((data or {}).get("country") or "").strip()
    except Exception:
        return None
    return c or None


def _is_india(country: Optional[str]) -> bool:
    if country is None:
        return True                       # old app versions send no country and only ever supported India
    c = supported_country(country)
    return c is not None and c.iso == "IN"


def account_lookup_keys(raw, country: Optional[str] = None) -> list:
    """Stored-string candidates for finding an EXISTING account from a request. Always a non-empty list for non-empty input.

    Indian or country-less Indian-shaped input -> the four legacy Indian formats (existing accounts keep working).
    A valid international number -> its exact E.164 (with and without '+') and nothing else, so it can never match an Indian record.
    Anything else -> only the cleaned input itself (exact match). It is never cut down to its last 10 digits.
    """
    if not intl_enabled():
        return legacy_variants(raw)
    country = str(country).strip() if country else None
    country = country or None
    keys = lookup_keys_for(raw, country, legacy_india=(country is None))
    if keys:
        return keys
    p = str(raw).strip().replace(" ", "").replace("-", "")
    return [p] if p else []


@dataclass(frozen=True)
class RegPhone:
    phone: str = ""      # the value to store in accounts.phone ('' when error)
    country: str = ""    # ISO code, '' in legacy mode
    error: str = ""      # one of the error codes; '' when ok


def register_phone(raw, country: Optional[str] = None) -> RegPhone:
    """Validate and canonicalise a phone for REGISTRATION.

    Indian numbers stay lenient exactly as before (any 10 digits, also 91+10 / +91+10 / 0+10) and are stored as '+91'+10 digits.
    International numbers must be a valid mobile for their country and are stored as E.164.
    """
    if not intl_enabled():
        return RegPhone(legacy_normalise(raw), "", "")
    country = str(country).strip() if country else None
    country = country or None
    r = parse(raw, country, legacy_india=(country is None))
    if r.ok:
        return RegPhone(r.e164, r.country, "")
    if _is_india(country):
        k = legacy_india_keys(raw)
        if k:
            return RegPhone(k[0], "IN", "")
    return RegPhone("", "", r.error or INVALID_NUMBER)


def phone_fields(stored: str) -> dict:
    """Extra fields to write on a NEW account document: {'phone_country': 'SA', 'phone_e164': '+966...'}. {} when not resolvable
    or in legacy mode. Never applied to existing records."""
    if not intl_enabled():
        return {}
    r = parse(stored)
    if r.ok:
        return {"phone_country": r.country, "phone_e164": r.e164}
    k = legacy_india_keys(stored)
    if k:
        return {"phone_country": "IN", "phone_e164": k[0]}
    return {}


def owner_key(raw) -> str:
    """Comparison key for 'is this the same merchant phone' checks (banner / store ownership).

    Indian numbers -> their 10 digits (identical to the old last-10 rule, so existing banners and stores keep matching).
    Valid international numbers -> digits-only E.164 (e.g. '977981...'), so a Nepal 10-digit number never equals an Indian one.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    digits = "".join(c for c in s if c.isdigit())
    if not intl_enabled():
        return digits[-10:]
    r = parse(s, legacy_india=True)
    if r.ok:
        return r.national if r.country == "IN" else wa_digits(r.e164)
    if legacy_india_keys(s):
        return digits[-10:]
    return digits


def same_number(a, b) -> bool:
    """True when two phone strings are the same number (both non-empty)."""
    ka, kb = owner_key(a), owner_key(b)
    return bool(ka) and ka == kb


def slider_phone_key(raw) -> str:
    """Value stored in promo_sliders.merchant_phone: Indian -> last 10 digits (unchanged), international -> digits-only E.164."""
    return owner_key(raw)
