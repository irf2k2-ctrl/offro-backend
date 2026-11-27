"""Language-consistent place names for the location flow (Maps-link resolve, GPS reverse geocode).

Why this exists: a provider asked for English (Nominatim accept-language=en, Places languageCode=en) still answers in the LOCAL language for
any place that has no English name in its data, so one lookup can return state "Amman" (English) next to city "خريبة السوق" (Arabic). The
language request alone does not guarantee one language, so after the providers have answered every field is checked against the app
language and, when it is in the wrong script, looked up again through the remaining providers in the same language.

Rules: never translate or invent a name; never pick a "similar" place; keep the provider's own name when no provider has one in the
requested language (graceful fallback); never touch coordinates or the country. Works for every country (no country-specific code)."""
import os
import time
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
    """Google's TYPED address components -> state / city / area. Google classifies every component, so the hierarchy is read by type
    (never "first available"): city = locality (or postal_town), state = administrative_area_level_1, area = sublocality / neighbourhood.
    `subs` collects every sub-locality / neighbourhood name seen (used to recognise a neighbourhood that another provider called a city)."""
    out = {"state": "", "city": "", "area": "", "subs": []}
    for comp in components or []:
        name = (comp.get("long_name") or comp.get("short_name") or "").strip()
        types = set(comp.get("types") or [])
        if not name:
            continue
        if not out["state"] and "administrative_area_level_1" in types:
            out["state"] = name
        if not out["city"] and types & {"locality", "postal_town"}:
            out["city"] = name
        if types & {"sublocality_level_1", "sublocality", "neighborhood"}:
            out["subs"].append(name)
            if not out["area"]:
                out["area"] = name
    return out


# ── Google Geocoding: why it can answer REQUEST_DENIED, and a safe way to say so ──────────────────────────────────────────────
# Only these fixed words are ever logged - never the key, the request URL, the caller's IP or Google's raw message text.
_DENIED_REASONS = (
    ("billing", "billing_not_enabled", "Billing is not enabled on the Google Cloud project that owns this key."),
    ("referer restrictions", "referrer_restricted", "The key has an HTTP-referrer restriction; server calls need application restriction None or IP addresses."),
    ("ip, site or mobile application", "ip_or_app_restricted", "The key's application restriction (IP / Android / iOS) does not allow this server."),
    ("not authorized to use this api", "api_not_enabled_or_not_allowed_for_key",
     "Geocoding API is not enabled for the project, or the key's API restrictions do not list it."),
    ("api key is invalid", "invalid_key", "Google reports the key as invalid (wrong, deleted or truncated)."),
    ("api key not valid", "invalid_key", "Google reports the key as invalid (wrong, deleted or truncated)."),
)


def classify_google_denial(message):
    """Google's error_message -> (reason_code, hint) using fixed vocabulary only. Unknown text -> ("unrecognised", ...)."""
    low = str(message or "").lower()
    for needle, code, hint in _DENIED_REASONS:
        if needle in low:
            return code, hint
    return "unrecognised", "REQUEST_DENIED without a recognised reason; check the Geocoding API, billing and the key's restrictions in Google Cloud."


_LOGGED = {}


def _log_throttled(msg, every=60):
    now = time.time()
    if now - _LOGGED.get(msg, 0) >= every:
        _LOGGED[msg] = now
        print(msg)


_CACHE = {}


def _cache_get(k):
    hit = _CACHE.get(k)
    if hit and hit[0] > time.time():
        return hit[1]
    _CACHE.pop(k, None)
    return None


def _cache_put(k, v, ttl):
    if len(_CACHE) > 500:
        _CACHE.clear()
    _CACHE[k] = (time.time() + ttl, v)


def google_reverse(lat, lng, lang, timeout=8):
    """Google Geocoding API (legacy web service) reverse lookup in `lang` ->
    {"state","city","area","address","subs"} (empty dict when unavailable). Best effort: a missing key, a disabled API or any network
    error just means "no answer". Endpoint https://maps.googleapis.com/maps/api/geocode/json with latlng, language and key as query
    parameters - the key is only ever sent to Google. Answers are cached briefly (10 min; a failure 60 s so a mis-configured key is not
    retried on every request)."""
    key = _google_key()
    if not key:
        return {}
    ck = (round(float(lat), 4), round(float(lng), 4), lang)
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/geocode/json",
                         params={"latlng": f"{lat},{lng}", "language": lang, "key": key}, timeout=timeout,
                         headers={"User-Agent": "OffrO/2.0"})
        data = r.json()
        status = str(data.get("status") or "")[:40]
        if status != "OK":
            if status == "REQUEST_DENIED":
                reason, hint = classify_google_denial(data.get("error_message"))
                _log_throttled(f"[GEO] Google Geocoding gave no answer: REQUEST_DENIED reason={reason} http={getattr(r, 'status_code', '?')} "
                               f"message_present={bool(data.get('error_message'))} hint={hint}")
            else:
                _log_throttled(f"[GEO] Google Geocoding gave no answer: {status or 'no status'} http={getattr(r, 'status_code', '?')}")
            _cache_put(ck, {}, 60)
            return {}
        merged = {"state": "", "city": "", "area": "", "address": "", "subs": []}
        for res in data.get("results") or []:
            got = _components_to_fields(res.get("address_components"))
            for f in FIELDS:
                merged[f] = merged[f] or got[f]
            merged["subs"].extend(x for x in got["subs"] if x not in merged["subs"])
            merged["address"] = merged["address"] or (res.get("formatted_address") or "")
        _cache_put(ck, merged, 600)
        return merged
    except Exception as exc:
        _log_throttled(f"[GEO] Google Geocoding request failed: {type(exc).__name__}")      # class name only: the message can contain the URL
        return {}


def _norm(text):
    """Comparison key for a place name: case, diacritics (incl. Arabic tashkeel), punctuation and spacing ignored."""
    t = unicodedata.normalize("NFKD", str(text or "")).casefold()
    return "".join(ch for ch in t if unicodedata.category(ch).startswith(("L", "N")))


def _master_knows_city(name, iso):
    """Does the City Master already list `name` for country `iso`? (read-only). On any error answer True: never override when unsure."""
    try:
        from services import city_master as CM
        return any(str(d.get("country") or "").upper() == str(iso or "").upper() for d in CM._same_name_docs(name))
    except Exception:
        return True


def fix_city_level(fields, lat, lng, lang, iso, city_key, provider=None):
    """OSM's address has no notion of "city level": the City field is the first of city / town / county / village that exists, so a
    town-, district- or village-level name (often a neighbourhood) can land in City. Google types its components, so when the name came
    from one of those weaker keys, Google (same language) is asked and, ONLY if Google itself lists that very name as a sub-locality /
    neighbourhood and has a real locality, the city is corrected to Google's locality; the demoted name is kept as Area when Area is empty.
    Never overrides a city the City Master lists for the country, never changes coordinates/country, never guesses: with no Google
    answer (no key, denied, quota, no locality, no match) the provider's value is returned unchanged."""
    out = dict(fields or {})
    city = out.get("city") or ""
    if not city or city_key not in ("town", "county", "village") or _master_knows_city(city, iso):
        return out
    try:
        alt = (provider or google_reverse)(lat, lng, lang) or {}
    except Exception:
        alt = {}
    loc = (alt.get("city") or "").strip()
    subs = {_norm(x) for x in alt.get("subs") or []}
    if loc and _norm(city) in subs and _norm(loc) != _norm(city) and in_language(loc, lang):
        if not out.get("area"):
            out["area"] = city
        out["city"] = loc
    return out


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
