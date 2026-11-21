"""services/translations.py - translation overrides: data model, validation, drafts, versions, rollback, kill switch, public data.
(Phase G, backend only. No dashboard, no Excel, no Flutter code.)

What this is
  The app ships its own bundled strings (ARB files). This module stores OPTIONAL display-text overrides that an administrator can
  publish without a new app build. Only VALUES are editable; KEYS come from the app (see tools/sync_translation_keys.py).

Collections (all new; each environment has its own database, so staging and production are fully independent)
  translation_languages   code (unique), name_en, name_native, direction, status (draft|active|disabled), bundled
  translation_keys        key (unique), english, placeholders, is_plural, group (app|legal), screen_group, max_length, deprecated
  translation_values      lang, key, track, value, status (modified|published), submitted, updated_by/at   [the working copy / draft]
  translation_versions    track, lang, version (unique together), strings, hash, note, published_by/at, restored_from,
                          promoted_from, approval                                               [immutable snapshots]
  translation_state       one document: kill_switch (+ who/when/why) and pending production packages
  translation_legal_reviews  lang, key, value_hash, reviewer, reviewed_at, note
  translation_rollbacks   _id "<track>:<lang>", intent_id, target/new version, phase, owner, lease_until   [Phase L1, only while a
                          rollback is unfinished; holds no translation text]
  (translation_audit lives in services/translation_audit.py)

Tracks: "app" (ordinary text) and "legal" (keys with group "legal"). They are versioned and published separately, so an ordinary
publish can never carry or overwrite legal wording, and a legal value needs a recorded review of that exact text before it can be
published.

Rules enforced here (not only in the routes)
  * plural keys are bundled-only and read-only; deprecated keys cannot be edited;
  * value rules: plain text only (no HTML), no hidden / bidirectional-override characters, length limit, placeholders must equal
    the English set, no URL / e-mail / phone number that the English text does not already contain;
  * production never accepts direct edits - its working copy only ever comes from an imported, signed staging package;
  * publishing and rollback create NEW versions; nothing is ever overwritten or deleted.

Rollback (Phase L1) - journaled and resumable, no database transactions needed (a standalone MongoDB has none)
  1. every check is read-only first: impact (which unpublished drafts would be lost), validation of the historical text against the
     CURRENT registry, language state, integrity, legal review, and - when drafts would be lost - the admin's confirmation of the
     exact impact (impact_hash);
  2. a claim document is inserted (unique per track+lang; it blocks publish / edits / submit / another rollback);
  3. the new version is inserted with the claim's intent_id: THIS is the commit point (the only write the app ever sees);
  4. the working copy is rewritten by an idempotent upsert-then-delete pass, the pending package is cleared, the claim is deleted.
  A failure after step 3 leaves a claim that a Super Admin can RESUME (roll forward; the owner at once, another Super Admin once the
  lease has expired). ABORT exists only until the version has been written. Retrying can never create a second version: the
  (track, lang, version) index and the intent_id make every repeated step a no-op.
"""
import csv
import hashlib
import io
import json
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timedelta

from pymongo.errors import DuplicateKeyError

from database import db
from services import app_environment as _env

logger = logging.getLogger("offro.translations")

SCHEMA = 1
LANGS, KEYS, VALUES, VERSIONS, STATE, REVIEWS = (
    "translation_languages", "translation_keys", "translation_values", "translation_versions", "translation_state",
    "translation_legal_reviews")
ROLLBACKS = "translation_rollbacks"     # Phase L1: one claim document per (track, lang) while a rollback is unfinished
STATE_ID = "state"
LEASE_SECONDS = 600                       # a rollback claim's lease; every phase renews it
PREVIEW_PAGE_DEFAULT, PREVIEW_PAGE_MAX = 50, 200
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


def require_env(kind: str) -> str:
    """Return the environment name or raise. kinds: edit | publish | export | import | any_known."""
    env = environment()
    if env == "unknown":
        raise TranslationError("environment_unknown", 503, "OFFRO_ENVIRONMENT is not set to a valid value; translation writes are refused")
    if kind in ("edit", "publish_draft", "add_language", "approve", "rollback") and env == "production":
        raise TranslationError("production_edit_blocked", 403, "Production receives translations only through an approved staging package"
                               + (" (roll back in staging, approve, export, import, then publish; use the kill switch for an emergency)" if kind == "rollback" else ""))
    if kind == "export" and env != "staging":
        raise TranslationError("package_export_staging_only", 403)
    if kind == "import" and env != "production":
        raise TranslationError("package_import_production_only", 403)
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
    db[REVIEWS].create_index([("lang", 1), ("key", 1), ("value_hash", 1)], unique=True, name="translation_legal_reviews_unique")


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


def add_language(code, name_en, name_native, direction, actor_id: str = "", *, bundled: bool = False) -> dict:
    require_env("add_language")
    code = str(code or "").strip()
    if not _LANG_RE.match(code):
        raise TranslationError("invalid_language_code", 422)
    if direction not in DIRECTIONS:
        raise TranslationError("invalid_direction", 422)
    name_en, name_native = str(name_en or "").strip()[:60], str(name_native or "").strip()[:60]
    if not name_en or not name_native:
        raise TranslationError("language_names_required", 422)
    doc = {"code": code, "name_en": name_en, "name_native": name_native, "direction": direction, "status": "draft",
           "bundled": bool(bundled), "created_by": actor_id, "created_at": datetime.utcnow(), "schema": SCHEMA}
    try:
        db[LANGS].insert_one(doc)
    except DuplicateKeyError:
        raise TranslationError("language_exists", 409)
    return doc


def update_language(code: str, changes: dict) -> dict:
    """Super Admin only (route). Production may only enable/disable; names and direction change only in a non-production draft."""
    env = require_env("any_known")
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
            if env == "production":
                raise TranslationError("production_edit_blocked", 403)
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


def _now() -> datetime:
    return datetime.utcnow()


def _assert_no_rollback(lang: str, track: str) -> None:
    """Edits, submit, publish and a new rollback are refused while a rollback of this language+track is unfinished."""
    c = db[ROLLBACKS].find_one({"_id": f"{track}:{lang}"})
    if c:
        raise TranslationError("rollback_in_progress", 409, {"phase": c.get("phase"), "new_version": c.get("new_version"),
                                                              "to_version": c.get("target_version")})


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
    _assert_no_rollback(lang, track)
    old = db[VALUES].find_one({"lang": lang, "key": key})
    status = "published" if published_strings(track, lang).get(key) == clean else "modified"
    db[VALUES].update_one(
        {"lang": lang, "key": key},
        {"$set": {"lang": lang, "key": key, "track": track, "value": clean, "value_hash": value_hash(clean), "status": status,
                  "submitted": False, "updated_by": actor_id, "updated_at": datetime.utcnow(), "source": "dashboard",
                  "schema": SCHEMA}}, upsert=True)
    return {"lang": lang, "key": key, "track": track, "status": status,
            "old_hash": value_hash(old["value"]) if old else "", "new_hash": value_hash(clean)}


def submit(lang: str, track: str) -> int:
    require_env("edit")
    get_language(lang)
    _check_track(track)
    _assert_no_rollback(lang, track)
    pub = published_strings(track, lang)
    n = 0
    for v in db[VALUES].find({"lang": lang, "track": track, "submitted": {"$ne": True}}):
        if pub.get(v["key"]) != v["value"]:
            db[VALUES].update_one({"_id": v["_id"]}, {"$set": {"submitted": True}})
            n += 1
    return n


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
                      "waiting_review": db[VALUES].count_documents({"lang": lang, "track": track, "submitted": True, "status": "modified"}),
                      "complete": miss == 0 and draft == 0,
                      "published_version": lv["version"] if lv else None,
                      "rollback_pending": db[ROLLBACKS].find_one({"_id": f"{track}:{lang}"}) is not None}
    return out


# ── legal review ──────────────────────────────────────────────────────────────────────────────────────────────────────
def record_legal_review(lang: str, key: str, reviewer: dict, note: str) -> dict:
    require_env("edit")
    get_language(lang)
    kd = get_key(key)
    if track_of(kd) != "legal":
        raise TranslationError("not_a_legal_key", 422)
    v = db[VALUES].find_one({"lang": lang, "key": key})
    if not v:
        raise TranslationError("no_value_to_review", 409)
    note = str(note or "").strip()[:NOTE_MAX]
    doc = {"lang": lang, "key": key, "value_hash": value_hash(v["value"]), "reviewer": reviewer,
           "reviewed_at": datetime.utcnow(), "note": note}
    db[REVIEWS].update_one({"lang": lang, "key": key, "value_hash": doc["value_hash"]}, {"$set": doc}, upsert=True)
    return {"lang": lang, "key": key, "value_hash": doc["value_hash"]}


def _legal_unreviewed(lang: str, changed_keys: list, strings: dict) -> list:
    return sorted(k for k in changed_keys
                  if not db[REVIEWS].find_one({"lang": lang, "key": k, "value_hash": value_hash(strings[k])}))


# ── publish / rollback / approve ──────────────────────────────────────────────────────────────────────────────────────
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


def _insert_version(track, lang, strings, note, actor, *, restored_from=None, promoted_from=None, approval=None) -> dict:
    latest = latest_version(track, lang)
    doc = {"track": track, "lang": lang, "version": (latest["version"] + 1) if latest else 1, "strings": strings,
           "hash": hash_strings(strings), "note": str(note or "")[:NOTE_MAX], "published_by": actor,
           "published_at": datetime.utcnow(), "restored_from": restored_from, "promoted_from": promoted_from,
           "approval": approval, "environment": environment(), "schema": SCHEMA}
    try:
        db[VERSIONS].insert_one(doc)
    except DuplicateKeyError:
        raise TranslationError("version_conflict", 409, "Another publish happened at the same time; try again")
    return doc


def _activate_language(lang: str) -> None:
    db[LANGS].update_one({"code": lang, "status": "draft"}, {"$set": {"status": "active"}})


def _check_note(note) -> str:
    note = str(note or "").strip()
    if not note:
        raise TranslationError("note_required", 422)
    return note[:NOTE_MAX]


def publish(lang: str, track: str, note, actor: dict) -> dict:
    """Super Admin only (route). Staging / development / test: publishes the working copy.
    Production: publishes ONLY the pending, signed, imported package (an explicit second step after the import)."""
    env = require_env("any_known")
    note = _check_note(note)
    language = get_language(lang)
    if language["status"] == "disabled":
        raise TranslationError("language_disabled", 409)
    _check_track(track)
    _assert_no_rollback(lang, track)
    work = working_strings(track, lang)
    _validate_all(work)
    pub = published_strings(track, lang)
    d = diff(pub, work)
    promoted_from, approval = None, None

    if env == "production":
        from services import translation_promotion as _promo
        pend = (db[STATE].find_one({"_id": STATE_ID}) or {}).get("pending", {}).get(f"{track}:{lang}")
        if not pend:
            raise TranslationError("no_pending_package", 409, "Import an approved staging package first")
        if pend.get("hash") != hash_strings(work):
            raise TranslationError("pending_package_mismatch", 409)
        if not _promo.verify_seal(pend):
            raise TranslationError("pending_package_tampered", 409)
        promoted_from = {"environment": "staging", "version": pend["source_version"], "hash": pend["hash"],
                         "package_hash": pend.get("package_hash", ""), "imported_by": pend.get("imported_by"),
                         "imported_at": pend.get("imported_at")}
        approval = pend.get("approval")
    elif track == "legal":
        changed = d["added"] + d["changed"]
        unreviewed = _legal_unreviewed(lang, changed, work)
        if unreviewed:
            raise TranslationError("legal_review_required", 409, unreviewed)

    if not work or (not any(d.values()) and pub):
        raise TranslationError("nothing_to_publish", 409)
    doc = _insert_version(track, lang, work, note, actor, promoted_from=promoted_from, approval=approval)
    db[VALUES].update_many({"lang": lang, "track": track, "key": {"$in": list(work)}}, {"$set": {"status": "published", "submitted": False}})
    _activate_language(lang)
    if env == "production":
        db[STATE].update_one({"_id": STATE_ID}, {"$unset": {f"pending.{track}:{lang}": ""}})
    return {"lang": lang, "track": track, "version": doc["version"], "hash": doc["hash"], "count": len(work), "diff": d}


# ── rollback (Phase L1): impact preview, journaled and resumable ───────────────────────────────────────────────────────
PREVIEW_SECTIONS = ("overwritten", "deleted", "working_removed", "live_changed", "status_only", "blockers")


def _version_number(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise TranslationError("invalid_version", 422)
    try:
        return int(v)
    except ValueError:
        raise TranslationError("invalid_version", 422)


def _iso(d) -> str:
    return d.isoformat() + "Z" if isinstance(d, datetime) else str(d or "")


def rollback_impact(lang: str, track: str, to_version) -> dict:
    """Read-only. What restoring `to_version` would do, including the UNPUBLISHED drafts it would overwrite or delete.
    A working value is an unpublished draft when its text differs from the latest published text OR its status is not 'published'
    (a status-only difference, same text, loses nothing and is reported separately). Raw working values are used (not only registered
    keys) because the restore deletes every working value of this language+track that is not in the target version."""
    require_env("rollback")
    language = get_language(lang)
    _check_track(track)
    ver = _version_number(to_version)
    target = db[VERSIONS].find_one({"track": track, "lang": lang, "version": ver})
    if not target:
        raise TranslationError("unknown_version", 404)
    latest = latest_version(track, lang)
    live = dict(latest["strings"]) if latest else {}
    tgt = dict(target["strings"])
    work = {d["key"]: d for d in db[VALUES].find({"lang": lang, "track": track})}

    overwritten, deleted, working_removed, status_only, live_changed = [], [], [], [], []
    for k in sorted(work):
        d = work[k]
        val = d.get("value", "")
        sub = bool(d.get("submitted"))
        if val != live.get(k):                                   # unpublished text
            if k in tgt:
                if tgt[k] == val:
                    status_only.append({"key": k, "reason": "draft_equals_restored_text"})
                else:
                    overwritten.append({"key": k, "draft": val, "live": live.get(k), "restored": tgt[k], "ready_for_review": sub})
            else:
                deleted.append({"key": k, "draft": val, "live": live.get(k), "ready_for_review": sub})
        elif k not in tgt:                                       # published text that is not in the target version
            working_removed.append({"key": k, "live": val})
        elif d.get("status") != "published":                     # same text as live, only the status flag differs
            status_only.append({"key": k, "reason": "status_flag_only"})
    for k in sorted(set(live) | set(tgt)):
        if k in live and k in tgt:
            if live[k] != tgt[k]:
                live_changed.append({"key": k, "change": "changed", "live": live[k], "restored": tgt[k]})
        elif k in tgt:
            live_changed.append({"key": k, "change": "added", "live": None, "restored": tgt[k]})
        else:
            live_changed.append({"key": k, "change": "removed", "live": live[k], "restored": None})

    already = bool(latest and latest["hash"] == target["hash"])
    glob = []
    if language["status"] == "disabled":
        glob.append("language_disabled")
    if hash_strings(tgt) != target["hash"]:
        glob.append("version_integrity_failed")
    keys_bad = {}
    if not already:
        keys_bad = _validation_problems(tgt)
        if track == "legal":
            changed = [r["key"] for r in live_changed if r["change"] in ("changed", "added")]
            for k in _legal_unreviewed(lang, changed, tgt):
                keys_bad.setdefault(k, "legal_review_required")

    core = {"lang": lang, "track": track, "from": latest["version"] if latest else 0, "to": ver, "target_hash": target["hash"],
            "overwritten": [[r["key"], value_hash(r["draft"]), value_hash(r["restored"]), r["ready_for_review"]] for r in overwritten],
            "deleted": [[r["key"], value_hash(r["draft"]), r["ready_for_review"]] for r in deleted],
            "working_removed": [r["key"] for r in working_removed], "status_only": [r["key"] for r in status_only],
            "live": [[r["key"], r["change"]] for r in live_changed]}
    counts = {"overwritten": len(overwritten), "deleted": len(deleted), "working_removed": len(working_removed),
              "status_only": len(status_only), "live_changed": len(live_changed),
              "live_restored": sum(1 for r in live_changed if r["change"] in ("changed", "added")),
              "live_removed": sum(1 for r in live_changed if r["change"] == "removed"),
              "ready_for_review": sum(1 for r in overwritten + deleted if r["ready_for_review"]),
              "blockers": len(keys_bad) + len(glob)}
    return {"lang": lang, "track": track, "from_version": latest["version"] if latest else 0, "to_version": ver,
            "already_current": already, "would_create_version": not already,
            "requires_confirmation": (not already) and bool(overwritten or deleted),
            "impact_hash": hash_strings(core), "counts": counts, "blockers": {"global": glob, "keys": keys_bad},
            "rows": {"overwritten": overwritten, "deleted": deleted, "working_removed": working_removed,
                     "live_changed": live_changed, "status_only": status_only,
                     "blockers": [{"key": k, "code": c} for k, c in sorted(keys_bad.items())]}}


def rollback_preview(lang: str, track: str, to_version, section=None, page=1, page_size=PREVIEW_PAGE_DEFAULT) -> dict:
    """The impact with TOTALS for everything and rows paged (the full set is always reachable: pass section + page, or use the CSV)."""
    imp = rollback_impact(lang, track, to_version)
    rows = imp.pop("rows")
    try:
        ps = max(1, min(PREVIEW_PAGE_MAX, int(page_size)))
        pg = max(1, int(page))
    except (TypeError, ValueError):
        raise TranslationError("invalid_paging", 422)
    imp["page_size"] = ps
    if section:
        if section not in PREVIEW_SECTIONS:
            raise TranslationError("invalid_section", 422)
        lst = rows[section]
        imp.update({"section": section, "page": pg, "total": len(lst), "rows": lst[(pg - 1) * ps: pg * ps]})
    else:
        imp["keys"] = {sec: rows[sec][:ps] for sec in PREVIEW_SECTIONS}
        imp["totals"] = {sec: len(rows[sec]) for sec in PREVIEW_SECTIONS}
    return imp


def _csv_safe(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def rollback_impact_csv(lang: str, track: str, to_version, section=None) -> str:
    """Every affected row (all sections, or one) as UTF-8 CSV with a BOM so spreadsheets read Arabic. Cells that could be read as
    formulas are neutralised."""
    if section and section not in PREVIEW_SECTIONS:
        raise TranslationError("invalid_section", 422)
    rows = rollback_impact(lang, track, to_version)["rows"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["section", "key", "change", "current_draft", "live_text", "restored_text"])
    for sec in ([section] if section else PREVIEW_SECTIONS):
        for r in rows[sec]:
            change = {"overwritten": "draft_overwritten", "deleted": "draft_deleted", "working_removed": "working_value_removed"}.get(sec)
            if sec == "live_changed":
                change = r["change"]
            elif sec == "status_only":
                change = r["reason"]
            elif sec == "blockers":
                change = r["code"]
            w.writerow([_csv_safe(x) for x in (sec, r["key"], change, r.get("draft"), r.get("live"), r.get("restored"))])
    return "﻿" + buf.getvalue()


def _impact_summary(imp: dict) -> dict:
    return {"impact_hash": imp["impact_hash"], "counts": imp["counts"], "from_version": imp["from_version"], "to_version": imp["to_version"]}


# -- the claim (journal) ------------------------------------------------------------------------------------------------
def _claim_id(track: str, lang: str) -> str:
    return f"{track}:{lang}"


def _claim_insert(claim: dict) -> None:
    db[ROLLBACKS].insert_one(claim)


def _claim_update(claim: dict, fields: dict, phases=None) -> None:
    """Renews the lease together with `fields`. Matches only this claim (intent_id) and, when given, only these phases, so an
    aborted or finished claim can never be revived by a late write."""
    q = {"_id": claim["_id"], "intent_id": claim["intent_id"]}
    if phases:
        q["phase"] = {"$in": list(phases)}
    res = db[ROLLBACKS].update_one(q, {"$set": {**fields, "lease_until": _now() + timedelta(seconds=LEASE_SECONDS)}})
    if res.matched_count != 1:
        raise TranslationError("rollback_not_found", 409, "This rollback was aborted or has already finished")


def _claim_delete(claim: dict) -> None:
    db[ROLLBACKS].delete_one({"_id": claim["_id"], "intent_id": claim["intent_id"]})


def _version_written(claim: dict) -> bool:
    return db[VERSIONS].find_one({"track": claim["track"], "lang": claim["lang"], "version": claim["new_version"],
                                  "intent_id": claim["intent_id"]}) is not None


def _insert_rollback_version(claim: dict, target: dict, actor: dict) -> None:
    doc = {"track": claim["track"], "lang": claim["lang"], "version": claim["new_version"], "strings": dict(target["strings"]),
           "hash": hash_strings(target["strings"]), "note": str(claim.get("note") or "")[:NOTE_MAX], "published_by": actor,
           "published_at": _now(), "restored_from": claim["target_version"], "promoted_from": None, "approval": None,
           "environment": environment(), "schema": SCHEMA, "intent_id": claim["intent_id"]}
    db[VERSIONS].insert_one(doc)


def _restore_working_copy(claim: dict, target: dict, actor: dict) -> None:
    """Rewrites the working copy from the target version: upsert every key, then delete the rest. Idempotent: running it again (after
    a failure) ends in exactly the same state, whatever part of the first run had been applied."""
    strings, lang, track, now = target["strings"], claim["lang"], claim["track"], _now()
    for k, v in strings.items():
        db[VALUES].update_one({"lang": lang, "key": k},
                              {"$set": {"lang": lang, "key": k, "track": track, "value": v, "value_hash": value_hash(v), "status": "published",
                                        "submitted": False, "updated_by": actor.get("id", ""), "updated_at": now, "source": "rollback",
                                        "schema": SCHEMA}}, upsert=True)
    db[VALUES].delete_many({"lang": lang, "track": track, "key": {"$nin": list(strings)}})


def _clear_pending(claim: dict) -> None:
    db[STATE].update_one({"_id": STATE_ID}, {"$unset": {f"pending.{claim['track']}:{claim['lang']}": ""}})


def _incomplete_error(claim: dict) -> TranslationError:
    try:
        written = _version_written(claim)
    except Exception:
        written = None
    code = "rollback_incomplete" if written else "rollback_interrupted"
    phase = "version_written" if written else ("started" if written is False else "unknown")
    return TranslationError(code, 503, {"phase": phase, "new_version": claim.get("new_version"), "intent_id": str(claim.get("intent_id", ""))[:12]})


def _rollback_execute(claim: dict, actor: dict, *, initial: bool, resumed: bool = False, prev_owner: str = "", takeover: bool = False) -> dict:
    """Steps 3-4 of the journal. Safe to repeat: each step is a no-op when it was already done."""
    lang, track = claim["lang"], claim["track"]
    try:
        target = db[VERSIONS].find_one({"track": track, "lang": lang, "version": claim["target_version"]})
        if not target:
            raise TranslationError("unknown_version", 404)
        if not _version_written(claim):
            # nothing of ours is live yet: re-check what could have changed, then write the commit point
            problems = []
            if get_language(lang)["status"] == "disabled":
                problems.append(TranslationError("language_disabled", 409))
            elif hash_strings(target["strings"]) != target["hash"]:
                problems.append(TranslationError("version_integrity_failed", 409))
            else:
                bad = _validation_problems(target["strings"])
                if bad:
                    problems.append(TranslationError("invalid_values", 422, bad))
            if problems:
                if initial:
                    _claim_delete(claim)                       # nothing was written: leave no claim behind
                raise problems[0]
            _claim_update(claim, {"phase": "writing"}, phases=("started", "writing"))
            try:
                _insert_rollback_version(claim, target, actor)
            except DuplicateKeyError:
                pass                                           # the version number exists: ours (a repeat) or someone else's, checked next
            if not _version_written(claim):
                _claim_delete(claim)
                raise TranslationError("version_conflict", 409, "Another publish happened at the same time; try again")
        _claim_update(claim, {"phase": "version_written"})
        _restore_working_copy(claim, target, actor)
        _clear_pending(claim)
        _claim_delete(claim)
    except TranslationError:
        raise
    except Exception as _e:
        logger.error("translation rollback interrupted for %s:%s (new version %s): %s", track, lang, claim.get("new_version"), type(_e).__name__, exc_info=True)
        raise _incomplete_error(claim)
    out = {"lang": lang, "track": track, "version": claim["new_version"], "restored_from": claim["target_version"], "hash": target["hash"],
           "impact": claim.get("counts"), "audit_details": {"counts": claim.get("counts"), "resumed": resumed, "takeover": takeover,
                                                            "previous_owner_id": prev_owner}}
    if resumed:
        out["resumed"] = True
    return out


def rollback(lang: str, track: str, to_version, note, actor: dict, confirm_impact_hash=None) -> dict:
    """Super Admin only (route), staging/dev/test only. Publishes the strings of an earlier version as a NEW version (restored_from).
    Nothing is deleted from the version history. The working copy follows the restored text; unpublished drafts that this would
    overwrite or delete need the admin's confirmation of the exact impact (confirm_impact_hash from rollback_preview).
    Production cannot roll back directly: roll back in staging, approve, export, import, then publish."""
    require_env("rollback")
    note = _check_note(note)
    get_language(lang)
    _check_track(track)
    _assert_no_rollback(lang, track)
    imp = rollback_impact(lang, track, to_version)
    summary = _impact_summary(imp)
    if imp["already_current"]:
        raise TranslationError("already_current", 409, summary)          # nothing is written: drafts stay exactly as they are
    glob = imp["blockers"]["global"]
    for code in ("language_disabled", "version_integrity_failed"):
        if code in glob:
            raise TranslationError(code, 409, summary)
    keys_bad = imp["blockers"]["keys"]
    legal = sorted(k for k, c in keys_bad.items() if c == "legal_review_required")
    other = {k: c for k, c in keys_bad.items() if c != "legal_review_required"}
    if other:
        raise TranslationError("invalid_values", 422, other)
    if legal:
        raise TranslationError("legal_review_required", 409, legal)
    if imp["requires_confirmation"]:
        if not confirm_impact_hash:
            raise TranslationError("draft_loss_confirmation_required", 409, summary)
        if not isinstance(confirm_impact_hash, str) or confirm_impact_hash != imp["impact_hash"]:
            raise TranslationError("impact_changed", 409, summary)

    now = _now()
    claim = {"_id": _claim_id(track, lang), "intent_id": uuid.uuid4().hex, "lang": lang, "track": track,
             "from_version": imp["from_version"], "target_version": imp["to_version"], "new_version": imp["from_version"] + 1,
             "target_hash": db[VERSIONS].find_one({"track": track, "lang": lang, "version": imp["to_version"]})["hash"],
             "phase": "started", "note": note, "started_by": actor, "owner": actor, "started_at": now,
             "lease_until": now + timedelta(seconds=LEASE_SECONDS), "takeover_count": 0, "counts": imp["counts"], "schema": SCHEMA}
    try:
        _claim_insert(claim)
    except DuplicateKeyError:
        raise TranslationError("rollback_in_progress", 409)
    except Exception:
        raise TranslationError("rollback_not_started", 503, {"phase": "not_started"})
    try:
        again = rollback_impact(lang, track, imp["to_version"])           # re-verify now that edits are blocked
        if again["impact_hash"] != imp["impact_hash"]:
            _claim_delete(claim)
            raise TranslationError("impact_changed", 409, _impact_summary(again))
    except TranslationError:
        raise
    except Exception:
        raise _incomplete_error(claim)
    return _rollback_execute(claim, actor, initial=True)


def rollback_status(lang: str, track: str, actor: dict = None) -> dict:
    """The unfinished rollback of this language+track, who owns it, when the lease ends and what the caller may do."""
    require_env("rollback")
    get_language(lang)
    _check_track(track)
    c = db[ROLLBACKS].find_one({"_id": _claim_id(track, lang)})
    if not c:
        return {"lang": lang, "track": track, "active": False}
    try:
        written = _version_written(c)
    except Exception:
        written = c.get("phase") == "version_written"
    expired = c["lease_until"] < _now()
    owner = c.get("owner") or {}
    is_owner = bool(actor and owner.get("id") and owner.get("id") == actor.get("id"))
    can_resume = is_owner or expired
    return {"lang": lang, "track": track, "active": True, "phase": "version_written" if written else c.get("phase"),
            "version_written": written, "from_version": c.get("from_version"), "target_version": c.get("target_version"),
            "new_version": c.get("new_version"), "started_by": c.get("started_by"), "owner": owner,
            "started_at": _iso(c.get("started_at")), "lease_until": _iso(c.get("lease_until")), "lease_expired": expired,
            "takeover_count": c.get("takeover_count", 0), "counts": c.get("counts"),
            "is_owner": is_owner, "can_resume": can_resume, "can_abort": can_resume and not written,
            "reason": None if can_resume else "owned_by_other"}


def rollback_resume(lang: str, track: str, actor: dict, action: str) -> dict:
    """Super Admin only. `resume` finishes an unfinished rollback (roll forward). `abort` cancels it, but only until the new version
    has been written. The owner may act at once; any other Super Admin only after the lease has expired (an atomic takeover)."""
    require_env("rollback")
    get_language(lang)
    _check_track(track)
    if action not in ("resume", "abort"):
        raise TranslationError("invalid_action", 422)
    c = db[ROLLBACKS].find_one({"_id": _claim_id(track, lang)})
    if not c:
        raise TranslationError("no_rollback_in_progress", 404)
    now, prev, takeover = _now(), (c.get("owner") or {}).get("id", ""), False
    if prev != actor.get("id"):
        if c["lease_until"] >= now:
            raise TranslationError("rollback_owned_by_other", 409, {"owner": (c.get("owner") or {}).get("name", ""),
                                                                      "lease_until": _iso(c["lease_until"])})
        res = db[ROLLBACKS].update_one({"_id": c["_id"], "intent_id": c["intent_id"], "lease_until": {"$lt": now}},
                                       {"$set": {"owner": actor, "lease_until": now + timedelta(seconds=LEASE_SECONDS)},
                                        "$inc": {"takeover_count": 1}})
        if res.modified_count != 1:                                      # another Super Admin took over first
            raise TranslationError("rollback_owned_by_other", 409, {"owner": "", "lease_until": ""})
        takeover = True
    else:
        _claim_update(c, {})
    c = db[ROLLBACKS].find_one({"_id": c["_id"]})
    if not c:
        raise TranslationError("rollback_not_found", 409, "This rollback was aborted or has already finished")
    if action == "abort":
        if c.get("phase") == "version_written" or _version_written(c):
            raise TranslationError("abort_not_allowed_version_written", 409, {"phase": "version_written", "new_version": c.get("new_version")})
        deleted = db[ROLLBACKS].delete_one({"_id": c["_id"], "intent_id": c["intent_id"], "phase": {"$in": ["started", "writing"]}})
        if deleted.deleted_count != 1:
            raise TranslationError("rollback_not_found", 409, "This rollback was aborted or has already finished")
        if _version_written(c):                                          # a Resume wrote the version in the same instant: keep it recoverable
            try:
                db[ROLLBACKS].insert_one({**c, "phase": "version_written"})
            except DuplicateKeyError:
                pass
            raise TranslationError("abort_not_allowed_version_written", 409, {"phase": "version_written", "new_version": c.get("new_version")})
        return {"lang": lang, "track": track, "aborted": True,
                "audit_details": {"takeover": takeover, "previous_owner_id": prev, "phase": c.get("phase")}}
    return _rollback_execute(c, actor, initial=False, resumed=True, prev_owner=prev, takeover=takeover)



def approve_for_production(lang: str, track: str, version, note, actor: dict) -> dict:
    """Super Admin only (route), staging / dev / test. Marks a tested, published version as approved for promotion."""
    require_env("approve")
    note = _check_note(note)
    _check_track(track)
    try:
        version = int(version)
    except (TypeError, ValueError):
        raise TranslationError("invalid_version", 422)
    v = db[VERSIONS].find_one({"track": track, "lang": lang, "version": version})
    if not v:
        raise TranslationError("unknown_version", 404)
    if v.get("approval"):
        raise TranslationError("already_approved", 409)
    approval = {"approved_by": actor, "approved_at": datetime.utcnow().isoformat() + "Z", "note": note, "hash": v["hash"]}
    db[VERSIONS].update_one({"_id": v["_id"], "approval": None}, {"$set": {"approval": approval}})
    return {"lang": lang, "track": track, "version": version, "approval": approval}


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
    for l in db[LANGS].find({"status": "active"}).sort("code", 1):
        tracks = {}
        for t in TRACKS:
            v = latest_version(t, l["code"])
            if v:
                tracks[t] = {"version": v["version"], "hash": v["hash"]}
        if tracks:
            langs.append({"code": l["code"], "name_en": l["name_en"], "name_native": l["name_native"],
                          "direction": l["direction"], "tracks": tracks})
    return {"schema": SCHEMA, "kill_switch": False, "overrides_enabled": True, "languages": langs}


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
