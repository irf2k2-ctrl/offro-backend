"""services/translations.py - translation overrides: data model, validation, editing, publishing, kill switch, public data.

What this is
  The app ships its own bundled strings (ARB files). This module stores OPTIONAL display-text overrides that an administrator can
  publish without a new app build. Only VALUES are editable; KEYS come from the app (see tools/sync_translation_keys.py).
  Workflow: edit a value (status "modified") -> Publish Changes -> a new immutable version (number + SHA-256 hash) that the app can
  fetch. There is no approval, review, promotion, package or rollback workflow.

Collections (each environment has its OWN database, chosen by its own MONGO_URL; nothing is shared or synced between environments)
  translation_languages   code (unique), name_en, name_native, direction, status (draft|active|disabled), bundled
  translation_keys        key (unique), english, placeholders, is_plural, group (app|legal), screen_group, max_length, deprecated
  translation_values      lang, key, track, value, status (modified|published), updated_by/at   [the working copy]
  translation_versions    track, lang, version (unique together), strings, hash, note, published_by/at   [immutable snapshots;
                          the app detects changes by comparing version + hash from the manifest]
  translation_state       one document: kill_switch (+ who/when/why)
  translation_language_countries
                          lang (unique), countries [ISO codes], updated_by/at   [which countries a language is offered in; many to many,
                          English (the built-in base language, not a stored language) included. Names/codes/flags are NOT stored: they
                          come from the one country master, utils.phone.SUPPORTED_COUNTRIES. An empty list means "no restriction".]
  (translation_audit lives in services/translation_audit.py)

Tracks: "app" (ordinary text) and "legal" (keys with group "legal"). They are versioned and published separately, so an ordinary
publish can never carry or overwrite legal wording. The legal track is edited and published like any other text. NOTE: the Flutter app
currently reads ONLY the "app" track, so publishing legal text does not change what customers see.

Rules enforced here (not only in the routes)
  * plural keys are bundled-only and read-only; deprecated keys cannot be edited;
  * value rules: plain text only (no HTML), no hidden / bidirectional-override characters, length limit, placeholders must equal
    the English set, no URL / e-mail / phone number that the English text does not already contain;
  * every known environment (staging and production) works the same way and writes only to its OWN database; an unknown
    environment refuses every write;
  * publishing creates a NEW version; nothing is ever overwritten or deleted.
"""
import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime

from pymongo.errors import DuplicateKeyError

from database import db
from services import app_environment as _env
from utils import countries as _countries

logger = logging.getLogger("offro.translations")

SCHEMA = 1
LANGS, KEYS, VALUES, VERSIONS, STATE = (
    "translation_languages", "translation_keys", "translation_values", "translation_versions", "translation_state")
LANG_COUNTRIES = "translation_language_countries"
BASE_LANGUAGE = "en"          # the app's built-in source language; it has no translation_languages record
STATE_ID = "state"
TRACKS = ("app", "legal")
DIRECTIONS = ("ltr", "rtl")
LANG_STATUSES = ("draft", "active", "disabled")
DEFAULT_MAX_LENGTH = 500
ABSOLUTE_MAX_LENGTH = 2000
NOTE_MAX = 300

_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z!?]")
_ENTITY_RE = re.compile(r"&(?:[A-Za-z]{2,8}|#\d{1,7}|#[xX][0-9A-Fa-f]{1,6});")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_PHONE_RE = re.compile(r"\+?\d(?:[\d\s\-().]{5,}\d)")
_ALLOWED_FORMAT_CHARS = {"‌", "‍", "‎", "‏", "؜"}   # ZWNJ, ZWJ, LRM, RLM, ALM (legitimate in Arabic text)
_DIGITS = {0x0660 + i: ord("0") + i for i in range(10)}
_DIGITS.update({0x06F0 + i: ord("0") + i for i in range(10)})


class TranslationError(Exception):
    """A refused operation. `code` is a short stable string, `status` the HTTP status the routes use."""

    def __init__(self, code: str, status: int = 400, detail=None):
        super().__init__(code)
        self.code, self.status, self.detail = code, status, detail


# ── hashing ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_strings(strings: dict) -> str:
    return sha256_hex(canonical_json(strings))


def value_hash(value: str) -> str:
    return sha256_hex(value or "")


# ── environment guard ─────────────────────────────────────────────────────────────────────────────────────────────────
def environment() -> str:
    return _env.classification()


def require_env(kind: str = "any_known") -> str:
    """Return the environment name, or refuse when it is not identified (unknown / not set / invalid / conflicting). `kind` is only a
    label for the call site: staging and production are treated identically, and each writes only to its own database."""
    env = environment()
    if env == "unknown":
        raise TranslationError("environment_unknown", 503, "OFFRO_ENVIRONMENT is not set to a valid value; translation writes are refused")
    return env


# ── value validation ──────────────────────────────────────────────────────────────────────────────────────────────────
def extract_placeholders(text: str) -> list:
    return sorted(set(_PLACEHOLDER_RE.findall(text or "")))


def _risky_tokens(text: str) -> set:
    t = (text or "").translate(_DIGITS)
    found = set()
    for rx in (_URL_RE, _EMAIL_RE, _PHONE_RE):
        for m in rx.findall(t):
            found.add(re.sub(r"[\s\-().]", "", m).lower().rstrip(".,;:!?"))
    return found


def validate_value(key_doc: dict, value) -> str:
    """Return the cleaned (NFC, trimmed) value or raise TranslationError(422, code)."""
    if not isinstance(value, str):
        raise TranslationError("value_not_text", 422)
    v = unicodedata.normalize("NFC", value).strip()
    if not v:
        raise TranslationError("empty_value", 422)
    limit = min(int(key_doc.get("max_length") or DEFAULT_MAX_LENGTH), ABSOLUTE_MAX_LENGTH)
    if len(v) > limit:
        raise TranslationError("too_long", 422, {"max_length": limit})
    for ch in v:
        cat = unicodedata.category(ch)
        if cat == "Cc" and ch != "\n":
            raise TranslationError("control_character", 422)
        if cat == "Cf" and ch not in _ALLOWED_FORMAT_CHARS:
            raise TranslationError("hidden_or_bidi_control_character", 422)
    if _TAG_RE.search(v) or _ENTITY_RE.search(v):
        raise TranslationError("html_not_allowed", 422)
    names = _PLACEHOLDER_RE.findall(v)
    if "{" in _PLACEHOLDER_RE.sub("", v) or "}" in _PLACEHOLDER_RE.sub("", v):
        raise TranslationError("malformed_placeholder", 422)
    expected = sorted(set(key_doc.get("placeholders") or []))
    if sorted(set(names)) != expected:
        raise TranslationError("placeholder_mismatch", 422, {"expected": expected, "found": sorted(set(names))})
    if not _risky_tokens(v) <= _risky_tokens(key_doc.get("english", "")):
        raise TranslationError("url_email_or_phone_not_in_english", 422)
    return v


# ── indexes ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def ensure_indexes():
    db[LANGS].create_index("code", unique=True, name="translation_languages_code")
    db[KEYS].create_index("key", unique=True, name="translation_keys_key")
    db[VALUES].create_index([("lang", 1), ("key", 1)], unique=True, name="translation_values_lang_key")
    db[VERSIONS].create_index([("track", 1), ("lang", 1), ("version", 1)], unique=True, name="translation_versions_track_lang_version")
    db[LANG_COUNTRIES].create_index("lang", unique=True, name="translation_language_countries_lang")


def ensure_indexes_safe() -> bool:
    try:
        ensure_indexes()
        return True
    except Exception as e:
        logger.error("translation index creation skipped (%s)", type(e).__name__)
        return False


# ── registry (keys come from the app) ─────────────────────────────────────────────────────────────────────────────────
def sync_keys(entries: list) -> dict:
    """Upsert the key registry from the app's keys. Keys absent from `entries` are marked deprecated, never deleted. Returns counts.
    Each entry: key, english, optional placeholders, is_plural, group (app|legal), screen_group, description, max_length."""
    seen, added, updated = set(), 0, 0
    for e in entries:
        key = str(e.get("key", ""))
        if not _KEY_RE.match(key):
            raise TranslationError("invalid_key", 422, {"key": key[:60]})
        english = str(e.get("english", ""))
        group = str(e.get("group") or "app")
        if group not in TRACKS:
            raise TranslationError("invalid_group", 422, {"key": key})
        doc = {"key": key, "english": english,
               "placeholders": sorted(set(e.get("placeholders") or extract_placeholders(english))),
               "is_plural": bool(e.get("is_plural", False)), "group": group,
               "screen_group": str(e.get("screen_group", ""))[:60], "description": str(e.get("description", ""))[:300],
               "max_length": min(int(e.get("max_length") or DEFAULT_MAX_LENGTH), ABSOLUTE_MAX_LENGTH),
               "deprecated": False, "schema": SCHEMA, "synced_at": datetime.utcnow()}
        res = db[KEYS].update_one({"key": key}, {"$set": doc}, upsert=True)
        added += 1 if res.upserted_id is not None else 0
        updated += 0 if res.upserted_id is not None else 1
        seen.add(key)
    dep = db[KEYS].update_many({"key": {"$nin": list(seen)}, "deprecated": {"$ne": True}}, {"$set": {"deprecated": True}})
    return {"added": added, "updated": updated, "deprecated": dep.modified_count}


def get_key(key: str) -> dict:
    d = db[KEYS].find_one({"key": key})
    if not d:
        raise TranslationError("unknown_key", 404)
    return d


def track_of(key_doc: dict) -> str:
    return "legal" if key_doc.get("group") == "legal" else "app"


def _check_track(track: str) -> str:
    if track not in TRACKS:
        raise TranslationError("invalid_track", 422)
    return track


# ── languages ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def get_language(code: str) -> dict:
    d = db[LANGS].find_one({"code": code})
    if not d:
        raise TranslationError("unknown_language", 404)
    return d


# ── country mapping (many to many: a language in several countries, a country with several languages) ──────────────────
def clean_countries(countries) -> list:
    """Validated, de-duplicated, sorted ISO codes. Every code must be in the country master (nothing else is ever stored)."""
    if countries is None:
        return []
    if not isinstance(countries, (list, tuple)):
        raise TranslationError("invalid_countries", 422)
    allowed = _countries.supported_iso_codes()
    out = set()
    for c in countries:
        iso = str(c or "").strip().upper() if isinstance(c, str) else ""
        if iso not in allowed:
            raise TranslationError("unsupported_country", 422, {"country": str(c)[:10]})
        out.add(iso)
    return sorted(out)


def language_countries(lang: str) -> list:
    """The saved countries of one language (ISO codes, sorted). [] when none are saved."""
    d = db[LANG_COUNTRIES].find_one({"lang": lang})
    if not d:
        return []
    allowed = _countries.supported_iso_codes()        # a country later removed from the master is simply not shown
    return sorted({c for c in (d.get("countries") or []) if c in allowed})


def all_language_countries() -> dict:
    allowed = _countries.supported_iso_codes()
    out = {}
    for d in db[LANG_COUNTRIES].find():
        out[d["lang"]] = sorted({c for c in (d.get("countries") or []) if c in allowed})
    return out


def set_language_countries(lang: str, countries, actor_id: str = "") -> list:
    """Replaces the saved country list of [lang] ('en' or an existing language). Takes effect at once: it is a setting about where
    a language is offered, not translated text, so it is not versioned and needs no publish. Writes only to THIS environment."""
    require_env("any_known")
    lang = str(lang or "").strip()
    if lang != BASE_LANGUAGE:
        get_language(lang)                                    # unknown_language (404) otherwise
    clean = clean_countries(countries)
    db[LANG_COUNTRIES].update_one(
        {"lang": lang}, {"$set": {"countries": clean, "updated_by": actor_id, "updated_at": datetime.utcnow(), "schema": SCHEMA}},
        upsert=True)
    return clean


def add_language(code, name_en, name_native, direction, actor_id: str = "", *, bundled: bool = False, countries=None) -> dict:
    require_env("add_language")
    code = str(code or "").strip()
    if not _LANG_RE.match(code):
        raise TranslationError("invalid_language_code", 422)
    if direction not in DIRECTIONS:
        raise TranslationError("invalid_direction", 422)
    name_en, name_native = str(name_en or "").strip()[:60], str(name_native or "").strip()[:60]
    if not name_en or not name_native:
        raise TranslationError("language_names_required", 422)
    clean = clean_countries(countries)                       # validated BEFORE anything is written
    doc = {"code": code, "name_en": name_en, "name_native": name_native, "direction": direction, "status": "draft",
           "bundled": bool(bundled), "created_by": actor_id, "created_at": datetime.utcnow(), "schema": SCHEMA}
    try:
        db[LANGS].insert_one(doc)
    except DuplicateKeyError:
        raise TranslationError("language_exists", 409)
    if clean:
        set_language_countries(code, clean, actor_id)
    doc["countries"] = clean
    return doc


def update_language(code: str, changes: dict) -> dict:
    """Super Admin only (route). Status, names and direction can be changed in any known environment; direction is locked once published."""
    require_env("any_known")
    lang = get_language(code)
    upd = {}
    if "status" in changes:
        if changes["status"] not in ("active", "disabled"):
            raise TranslationError("invalid_status", 422)
        if changes["status"] == "active" and not latest_version("app", code) and not latest_version("legal", code):
            raise TranslationError("nothing_published_yet", 409)
        upd["status"] = changes["status"]
    for f in ("name_en", "name_native", "direction"):
        if f in changes:
            if f == "direction":
                if changes[f] not in DIRECTIONS:
                    raise TranslationError("invalid_direction", 422)
                if lang["status"] != "draft":
                    raise TranslationError("direction_locked_after_publish", 409)
            else:
                if not str(changes[f]).strip():
                    raise TranslationError("language_names_required", 422)
            upd[f] = str(changes[f]).strip()[:60]
    if not upd:
        raise TranslationError("nothing_to_change", 400)
    db[LANGS].update_one({"code": code}, {"$set": upd})
    return db[LANGS].find_one({"code": code})


# ── drafts (working copy) ─────────────────────────────────────────────────────────────────────────────────────────────
def latest_version(track: str, lang: str):
    d = db[VERSIONS].find({"track": track, "lang": lang}).sort("version", -1).limit(1)
    for row in d:
        return row
    return None


def published_strings(track: str, lang: str) -> dict:
    v = latest_version(track, lang)
    return dict(v["strings"]) if v else {}


def working_strings(track: str, lang: str) -> dict:
    """The current working copy for a lang + track: only values whose key is registered, not deprecated, not plural, same track."""
    keys = {k["key"]: k for k in db[KEYS].find({"deprecated": {"$ne": True}, "is_plural": {"$ne": True}})
            if track_of(k) == track}
    out = {}
    for v in db[VALUES].find({"lang": lang, "track": track}):
        if v["key"] in keys:
            out[v["key"]] = v["value"]
    return out


def set_value(lang: str, key: str, value, actor_id: str = "") -> dict:
    require_env("edit")
    language = get_language(lang)
    if language["status"] == "disabled":
        raise TranslationError("language_disabled", 409)
    kd = get_key(key)
    if kd.get("deprecated"):
        raise TranslationError("key_deprecated", 409)
    if kd.get("is_plural"):
        raise TranslationError("plural_read_only", 422, "Plural strings are bundled-only and read-only in this release")
    clean = validate_value(kd, value)
    track = track_of(kd)
    old = db[VALUES].find_one({"lang": lang, "key": key})
    status = "published" if published_strings(track, lang).get(key) == clean else "modified"
    db[VALUES].update_one(
        {"lang": lang, "key": key},
        {"$set": {"lang": lang, "key": key, "track": track, "value": clean, "value_hash": value_hash(clean), "status": status,
                  "updated_by": actor_id, "updated_at": datetime.utcnow(), "source": "dashboard",
                  "schema": SCHEMA}}, upsert=True)
    return {"lang": lang, "key": key, "track": track, "status": status,
            "old_hash": value_hash(old["value"]) if old else "", "new_hash": value_hash(clean)}


def diff(published: dict, working: dict) -> dict:
    return {"added": sorted(k for k in working if k not in published),
            "changed": sorted(k for k in working if k in published and published[k] != working[k]),
            "removed": sorted(k for k in published if k not in working)}


def missing_keys(lang: str, track: str) -> list:
    have = set(working_strings(track, lang))
    return sorted(k["key"] for k in db[KEYS].find({"deprecated": {"$ne": True}, "is_plural": {"$ne": True}})
                  if track_of(k) == track and k["key"] not in have)


def preview(lang: str, track: str) -> dict:
    get_language(lang)
    _check_track(track)
    work, pub = working_strings(track, lang), published_strings(track, lang)
    d = diff(pub, work)
    return {"lang": lang, "track": track, "strings": work, "hash": hash_strings(work), "diff": d,
            "live_version": (latest_version(track, lang) or {}).get("version"),
            "counts": {"total": len(work), "changed": len(d["added"]) + len(d["changed"]) + len(d["removed"]),
                       "missing": len(missing_keys(lang, track))}}


def language_status(lang: str) -> dict:
    out = {}
    for track in TRACKS:
        d = diff(published_strings(track, lang), working_strings(track, lang))
        draft = len(d["added"]) + len(d["changed"]) + len(d["removed"])
        miss = len(missing_keys(lang, track))
        lv = latest_version(track, lang)
        out[track] = {"missing": miss, "draft_changes": draft,
                      "complete": miss == 0 and draft == 0,
                      "published_version": lv["version"] if lv else None}
    return out


# ── publish ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _validation_problems(strings: dict) -> dict:
    """key -> error code for every text that could not be published against the CURRENT registry (read-only)."""
    bad = {}
    for k, v in strings.items():
        try:
            kd = get_key(k)
            if kd.get("is_plural") or kd.get("deprecated"):
                raise TranslationError("key_not_publishable", 422)
            validate_value(kd, v)
        except TranslationError as e:
            bad[k] = e.code
    return bad


def _validate_all(strings: dict) -> None:
    bad = _validation_problems(strings)
    if bad:
        raise TranslationError("invalid_values", 422, bad)


def _insert_version(track, lang, strings, note, actor) -> dict:
    latest = latest_version(track, lang)
    doc = {"track": track, "lang": lang, "version": (latest["version"] + 1) if latest else 1, "strings": strings,
           "hash": hash_strings(strings), "note": str(note or "")[:NOTE_MAX], "published_by": actor,
           "published_at": datetime.utcnow(), "environment": environment(), "schema": SCHEMA}
    try:
        db[VERSIONS].insert_one(doc)
    except DuplicateKeyError:
        raise TranslationError("version_conflict", 409, "Another publish happened at the same time; try again")
    return doc


def _activate_language(lang: str) -> None:
    db[LANGS].update_one({"code": lang, "status": "draft"}, {"$set": {"status": "active"}})


def publish(lang: str, track: str, note, actor: dict) -> dict:
    """Super Admin only (route). Publishes the working copy of this language + track as a NEW version in THIS environment's own
    database. Works the same in staging and production. The note is optional. Nothing is ever sent to another environment."""
    require_env("any_known")
    note = str(note or "").strip()[:NOTE_MAX]
    language = get_language(lang)
    if language["status"] == "disabled":
        raise TranslationError("language_disabled", 409)
    _check_track(track)
    work = working_strings(track, lang)
    _validate_all(work)
    pub = published_strings(track, lang)
    d = diff(pub, work)
    if not work or (not any(d.values()) and pub):
        raise TranslationError("nothing_to_publish", 409)
    doc = _insert_version(track, lang, work, note, actor)
    db[VALUES].update_many({"lang": lang, "track": track, "key": {"$in": list(work)}}, {"$set": {"status": "published"}})
    _activate_language(lang)
    return {"lang": lang, "track": track, "version": doc["version"], "hash": doc["hash"], "count": len(work), "diff": d}


# ── kill switch (per environment: lives in this environment's own database) ───────────────────────────────────────────
def kill_switch_state() -> dict:
    d = db[STATE].find_one({"_id": STATE_ID}) or {}
    return {"enabled": bool(d.get("kill_switch", False)), "changed_by": d.get("kill_switch_by"),
            "changed_at": d.get("kill_switch_at"), "reason": d.get("kill_switch_reason", "")}


def set_kill_switch(enabled: bool, reason, actor: dict) -> dict:
    """Super Admin only (route). ON  = the app must ignore every remote override and use its bundled text.
    OFF = overrides apply again. Affects only THIS environment's database."""
    require_env("any_known")
    if not isinstance(enabled, bool):
        raise TranslationError("enabled_must_be_boolean", 422)
    db[STATE].update_one({"_id": STATE_ID}, {"$set": {"kill_switch": enabled, "kill_switch_by": actor,
                         "kill_switch_at": datetime.utcnow().isoformat() + "Z",
                         "kill_switch_reason": str(reason or "")[:200], "schema": SCHEMA}}, upsert=True)
    return kill_switch_state()


def kill_switch_on() -> bool:
    """Public-API view. FAILS CLOSED: if the switch cannot be read the answer is ON (the app keeps its bundled text)."""
    try:
        return bool((db[STATE].find_one({"_id": STATE_ID}, {"kill_switch": 1}) or {}).get("kill_switch", False))
    except Exception as e:
        logger.error("translation kill switch unreadable (%s) - treating overrides as DISABLED", type(e).__name__)
        return True


# ── public data (published only; never drafts) ────────────────────────────────────────────────────────────────────────
def public_manifest() -> dict:
    if kill_switch_on():
        return {"schema": SCHEMA, "kill_switch": True, "overrides_enabled": False, "languages": []}
    langs = []
    mapping = all_language_countries()
    for l in db[LANGS].find({"status": "active"}).sort("code", 1):
        tracks = {}
        for t in TRACKS:
            v = latest_version(t, l["code"])
            if v:
                tracks[t] = {"version": v["version"], "hash": v["hash"]}
        if tracks:
            langs.append({"code": l["code"], "name_en": l["name_en"], "name_native": l["name_native"],
                          "direction": l["direction"], "tracks": tracks, "countries": mapping.get(l["code"], [])})
    # Where each language is offered, for the language chooser. English is included (it is the built-in base language). Only languages
    # the app can actually offer (English and active languages) are listed; an empty list means "no restriction". Additive: older
    # apps ignore this key, and nothing here is part of any version hash.
    offered = {BASE_LANGUAGE} | {l["code"] for l in langs}
    language_countries = {code: mapping.get(code, []) for code in sorted(offered)}
    return {"schema": SCHEMA, "kill_switch": False, "overrides_enabled": True, "languages": langs,
            "language_countries": language_countries}


def public_language(lang: str, track: str):
    """The published file for one language + track, or None (unknown / disabled / nothing published / kill switch on)."""
    if kill_switch_on() or track not in TRACKS:
        return None
    l = db[LANGS].find_one({"code": lang, "status": "active"})
    v = latest_version(track, lang) if l else None
    if not v:
        return None
    return {"schema": SCHEMA, "lang": lang, "track": track, "version": v["version"], "direction": l["direction"],
            "hash": v["hash"], "strings": v["strings"]}
