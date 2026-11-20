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

Nothing here reads or writes the database. The helper is not wired into any route in Phase C.
"""
from __future__ import annotations

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
