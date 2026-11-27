"""Language-consistent place names for the location flow (Maps-link resolve, GPS reverse geocode).

Why this exists: a provider asked for English (Nominatim accept-language=en, Places languageCode=en) still answers in the LOCAL language for
any place that has no English name in its data, so one lookup can return state "Amman" (English) next to city "خريبة السوق" (Arabic). The
language request alone does not guarantee one language, so after the providers have answered every field is checked against the app
language and, when it is in the wrong script, looked up again through the remaining providers in the same language.

Rules: never translate or invent a name; never pick a "similar" place; keep the provider's own name when no provider has one in the
requested language (graceful fallback); never touch coordinates or the country. Works for every country (no country-specific code)."""
import os
import unicodedata

import requests

FIELDS = ("state", "city", "area")
_ARABIC_SCRIPT_HINT = "ARABIC"


def _letters(text):
    return [ch for ch in str(text or "") if unicodedata.category(ch).startswith("L")]


def _is_arabic_script(ch):
    try:
        return _ARABIC_SCRIPT_HINT in unicodedata.name(ch)
    except ValueError:
        return False


def in_language(text, lang):
    """Is `text` written in the app language's script? English ('en') = Latin letters only (digits/punctuation ignored);
    Arabic ('ar') = at least one Arabic letter. Empty text is acceptable (nothing to fix)."""
    letters = _letters(text)
    if not letters:
        return True
    if lang == "ar":
        return any(_is_arabic_script(ch) for ch in letters)
    return all("LATIN" in unicodedata.name(ch, "") for ch in letters)


def _google_key():
    return (os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("MAPS_API_KEY") or os.getenv("GOOGLE_MAPS_PLATFORM_API_KEY"))


def _components_to_fields(components):
    out = {"state": "", "city": "", "area": ""}
    for comp in components or []:
        name = (comp.get("long_name") or comp.get("short_name") or "").strip()
        types = set(comp.get("types") or [])
        if not name:
            continue
        if not out["state"] and "administrative_area_level_1" in types:
            out["state"] = name
        if not out["city"] and types & {"locality", "postal_town"}:
            out["city"] = name
        if not out["area"] and types & {"sublocality_level_1", "sublocality", "neighborhood"}:
            out["area"] = name
    return out


def google_reverse(lat, lng, lang, timeout=8):
    """Google Geocoding API reverse lookup in `lang` -> {"state","city","area","address"} (empty dict when unavailable).
    Best effort: a missing key, a disabled API or any network error just means "no answer"."""
    key = _google_key()
    if not key:
        return {}
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/geocode/json",
                         params={"latlng": f"{lat},{lng}", "language": lang, "key": key}, timeout=timeout,
                         headers={"User-Agent": "OffrO/2.0"})
        data = r.json()
        status = str(data.get("status") or "")
        if status != "OK":
            # REQUEST_DENIED (API disabled / invalid or restricted key), OVER_QUERY_LIMIT (quota), ZERO_RESULTS ... : no answer, never an error.
            # Only Google's status word is logged - never the key, the URL or the response body.
            print(f"[GEO] Google Geocoding gave no answer: {status[:40] or 'no status'}")
            return {}
        merged = {"state": "", "city": "", "area": "", "address": ""}
        for res in data.get("results") or []:
            got = _components_to_fields(res.get("address_components"))
            for f in FIELDS:
                merged[f] = merged[f] or got[f]
            merged["address"] = merged["address"] or (res.get("formatted_address") or "")
        return merged
    except Exception as exc:
        print(f"[GEO] Google Geocoding request failed: {type(exc).__name__}")      # class name only: the message can contain the request URL
        return {}


def complete_names(fields, lat, lng, lang, providers=None):
    """Return `fields` (state/city/area/address) with every field that is not in the app language replaced by the same-language answer
    of the fallback providers. A field no provider can give in the language keeps its original value (never blanked, never guessed).
    Only fields of the same place are replaced - the lookup is by the verified coordinates, not by name."""
    out = dict(fields or {})
    lang = lang if lang in ("en", "ar") else "en"
    # the free-text address is only re-checked for English: an Arabic address legitimately carries Latin street/building text
    checked = (*FIELDS, "address") if lang == "en" else FIELDS
    wrong = [f for f in checked if out.get(f) and not in_language(out[f], lang)]
    if lang == "ar" and not any(out.get(f) and in_language(out[f], "ar") and _letters(out[f]) for f in FIELDS):
        # Arabic was asked for but the place has no Arabic names at all (e.g. a city in India or the Philippines): that is a place whose
        # names are Latin everywhere, not a mixed answer. Nothing is "translated" - the names stay exactly as the provider gave them.
        return out
    if not wrong:
        return out
    for provider in (providers if providers is not None else (google_reverse,)):
        try:
            alt = provider(lat, lng, lang) or {}
        except Exception:
            alt = {}
        for f in list(wrong):
            cand = (alt.get(f) or "").strip()
            if cand and in_language(cand, lang):
                out[f] = cand
                wrong.remove(f)
        if not wrong:
            break
    return out
