"""services/cities.py - country and language support for the City Master (`db.cities`), additive only.

* `country`  : ISO 3166-1 alpha-2 code from the ONE country master (utils.phone.SUPPORTED_COUNTRIES via utils.countries). Optional: a city
               without one is "unassigned" (every record that existed before this feature) and is never offered to a country-specific request.
* `names`    : {language code: city name}. The English `name` stays the canonical value (the one stored on locations, stores and accounts and used
               for matching), so a translation is display-only; the city id, name and country mapping never change when a translation is added.
Nothing here creates a collection, a second city per language, or touches an existing record unless an admin asks for it.
"""
import re
from typing import Optional

from utils import countries as _countries

_LANG = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?$")
MAX_NAME = 80
MAX_LANGS = 40


def normalize_country(value) -> Optional[str]:
    """The supported ISO code for `value` ('sa', ' SA ' -> 'SA'), or None when empty / not a supported country."""
    iso = str(value or "").strip().upper()
    return iso if iso and _countries.country_info(iso) else None


def clean_names(value) -> dict:
    """Validate a {language: name} map from an admin. Returns the cleaned map; raises ValueError with a readable message otherwise.
    Blank names are dropped (the display falls back to English), codes are lower-cased."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("names must be an object of language code to city name")
    if len(value) > MAX_LANGS:
        raise ValueError("too many languages")
    out = {}
    for k, v in value.items():
        code = str(k or "").strip()
        if not _LANG.match(code):
            raise ValueError("invalid language code: %s" % code[:12])
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if not isinstance(v, str):
            raise ValueError("the name for %s must be text" % code)
        name = " ".join(v.split())
        if len(name) > MAX_NAME:
            raise ValueError("the name for %s is longer than %d characters" % (code, MAX_NAME))
        out[code.lower().replace("_", "-")] = name
    return out


def display_name(doc: dict, lang: str = "") -> str:
    """The city's name in `lang` (exact code first, then its base language: 'ar-SA' -> 'ar'); the English `name` when there is none."""
    english = str((doc or {}).get("name") or "")
    code = str(lang or "").strip().lower().replace("_", "-")
    names = (doc or {}).get("names")
    if code and isinstance(names, dict):
        for key in (code, code.split("-")[0]):
            v = names.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return english
