"""services/translation_promotion.py - the HMAC-signed promotion package: staging -> production (Phase G).

Flow
  1. STAGING: a Super Admin approves a tested, published version (translations.approve_for_production), then exports it
     (build_package). The package is signed with HMAC-SHA256 using THIS environment's TRANSLATION_PACKAGE_SECRET.
  2. PRODUCTION: a Super Admin uploads the package (import_package). Production verifies, in this order and before it writes
     anything: kind and schema, source environment, hash of the strings, the approval record, the HMAC signature, that every key is
     known to the production registry and every value passes validation. A good package is stored ONLY AS A DRAFT (the working
     copy) and a "pending package" record is sealed. NOTHING IS PUBLISHED by the import.
  3. PRODUCTION: a separate explicit publish (translations.publish) is then required. It checks the pending record, that the
     working copy still equals the package, and the seal, and records `promoted_from` on the new version.

Secrets (never in code, never in the dashboard or the app; each environment has its OWN, independently generated values)
  TRANSLATION_PACKAGE_SECRET          this environment's own secret.
        staging    : signs the packages staging exports.
        production : seals the pending-package record between import and publish, so a change made to the draft in the database
                     during that gap is detected at publish.
  TRANSLATION_PACKAGE_VERIFY_SECRET   production only: the value of STAGING's TRANSLATION_PACKAGE_SECRET, used only to verify
                     incoming packages.
  An HMAC key that verifies can also forge, so production necessarily holds staging's signing key (as VERIFY_SECRET). That is why
  production's OWN secret must be a different value: the import is refused when the two are identical, and a secret that is missing,
  short (< 32 characters) or low-variety disables the feature (fail closed). secret_fingerprint() lets you compare two
  environments without ever showing a secret. (If you later want production to hold no staging key at all, an asymmetric signature
  is the alternative; `cryptography` is already installed.)
"""
import hashlib
import hmac
import os
from datetime import datetime

from database import db
from services import translations as T

KIND = "offro-translation-package"
SECRET_VAR = "TRANSLATION_PACKAGE_SECRET"
VERIFY_VAR = "TRANSLATION_PACKAGE_VERIFY_SECRET"
MIN_SECRET_LENGTH = 32
_WEAK_WORDS = ("changeme", "change-me", "secret", "password", "example", "placeholder", "your-secret", "test", "staging", "production")


def _get(var: str) -> str:
    return (os.getenv(var, "") or "").strip()


def strength_problem(secret: str):
    """None if the secret is acceptable, else a short code. Never returns or logs the secret."""
    if not secret:
        return "missing"
    if len(secret) < MIN_SECRET_LENGTH:
        return "too_short"
    if len(set(secret)) < 12:
        return "low_variety"
    low = secret.lower()
    if any(w in low for w in _WEAK_WORDS):
        return "looks_like_a_placeholder"
    return None


def secret_fingerprint(secret: str) -> str:
    """8 hex characters that identify a secret without revealing it (compare two environments, they must DIFFER for the own
    secrets and production's VERIFY fingerprint must EQUAL staging's own fingerprint)."""
    return hashlib.sha256(b"offro-translation-fp:" + secret.encode("utf-8")).hexdigest()[:8] if secret else ""


def config_status() -> dict:
    """What the dashboard / setup check may show. Codes and fingerprints only - never a secret."""
    own, ver = _get(SECRET_VAR), _get(VERIFY_VAR)
    env = T.environment()
    out = {"environment": env, "own_secret": strength_problem(own) or "ok", "own_fingerprint": secret_fingerprint(own)}
    if env == "production":
        out["verify_secret"] = strength_problem(ver) or "ok"
        out["verify_fingerprint"] = secret_fingerprint(ver)
        out["secrets_distinct"] = bool(own and ver and not hmac.compare_digest(own.encode(), ver.encode()))
        out["ready_to_import"] = out["own_secret"] == "ok" and out["verify_secret"] == "ok" and out["secrets_distinct"]
    elif env == "staging":
        out["ready_to_export"] = out["own_secret"] == "ok"
    return out


def _hmac(secret: str, text: str) -> str:
    return hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()


# ── staging: export ───────────────────────────────────────────────────────────────────────────────────────────────────
def build_package(lang: str, track: str, version, actor: dict) -> dict:
    T.require_env("export")
    own = _get(SECRET_VAR)
    prob = strength_problem(own)
    if prob:
        raise T.TranslationError("package_secret_not_configured", 503, {"secret": prob})
    try:
        version = int(version)
    except (TypeError, ValueError):
        raise T.TranslationError("invalid_version", 422)
    language = T.get_language(lang)
    v = db[T.VERSIONS].find_one({"track": track, "lang": lang, "version": version})
    if not v:
        raise T.TranslationError("unknown_version", 404)
    ap = v.get("approval")
    if not ap or ap.get("hash") != v["hash"]:
        raise T.TranslationError("version_not_approved", 409)
    if T.hash_strings(v["strings"]) != v["hash"]:
        raise T.TranslationError("version_integrity_failed", 409)
    payload = {"kind": KIND, "schema": T.SCHEMA, "source_environment": "staging", "track": track, "lang": lang,
               "language": {"name_en": language["name_en"], "name_native": language["name_native"],
                            "direction": language["direction"]},
               "version": version, "strings": v["strings"], "hash": v["hash"], "approval": ap,
               "exported_by": {"id": actor.get("id", ""), "name": actor.get("name", "")},
               "exported_at": datetime.utcnow().isoformat() + "Z"}
    payload = _jsonable(payload)
    return {"payload": payload, "signature": _hmac(own, T.canonical_json(payload)), "alg": "HMAC-SHA256"}


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(x) for k, x in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(x) for x in o]
    if isinstance(o, datetime):
        return o.isoformat() + "Z"
    return o


# ── production: verify + import as DRAFT ──────────────────────────────────────────────────────────────────────────────
def verify_package(envelope) -> dict:
    """Return the verified payload or raise TranslationError. Writes nothing. Production only."""
    T.require_env("import")
    own, ver = _get(SECRET_VAR), _get(VERIFY_VAR)
    for secret, name in ((own, "own"), (ver, "verify")):
        prob = strength_problem(secret)
        if prob:
            raise T.TranslationError("package_secret_not_configured", 503, {name: prob})
    if hmac.compare_digest(own.encode(), ver.encode()):
        raise T.TranslationError("package_secrets_identical", 503,
                                 "Production's own secret must differ from the staging verification secret")
    if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict) or not isinstance(envelope.get("signature"), str):
        raise T.TranslationError("package_malformed", 422)
    if envelope.get("alg") != "HMAC-SHA256":
        raise T.TranslationError("package_algorithm_unsupported", 422)
    p = envelope["payload"]
    if p.get("kind") != KIND or p.get("schema") != T.SCHEMA:
        raise T.TranslationError("package_kind_or_schema_invalid", 422)
    if p.get("source_environment") != "staging":
        raise T.TranslationError("package_not_from_staging", 422)
    if not isinstance(p.get("strings"), dict) or not isinstance(p.get("hash"), str):
        raise T.TranslationError("package_malformed", 422)
    # signature first, so nothing about an unauthentic package is inspected further
    expected = _hmac(ver, T.canonical_json(p))
    if not hmac.compare_digest(expected.encode(), envelope["signature"].encode("utf-8", "replace")):
        raise T.TranslationError("package_signature_invalid", 403)
    if T.hash_strings(p["strings"]) != p["hash"]:
        raise T.TranslationError("package_hash_mismatch", 422)
    ap = p.get("approval")
    if not isinstance(ap, dict) or ap.get("hash") != p["hash"] or not ap.get("approved_by") or not ap.get("approved_at"):
        raise T.TranslationError("package_not_approved", 422)
    lang, track = p.get("lang"), p.get("track")
    if track not in T.TRACKS or not isinstance(lang, str) or not T._LANG_RE.match(lang):
        raise T.TranslationError("package_malformed", 422)
    meta = p.get("language") or {}
    if meta.get("direction") not in T.DIRECTIONS or not meta.get("name_en") or not meta.get("name_native"):
        raise T.TranslationError("package_malformed", 422)
    bad = {}
    for k, v in p["strings"].items():
        try:
            kd = T.get_key(k)
            if T.track_of(kd) != track:
                raise T.TranslationError("wrong_track", 422)
            if kd.get("is_plural") or kd.get("deprecated"):
                raise T.TranslationError("key_not_publishable", 422)
            T.validate_value(kd, v)
        except T.TranslationError as e:
            bad[k] = e.code
    if bad:
        raise T.TranslationError("package_values_rejected", 422, bad)
    return p


def _seal_text(pend: dict) -> str:
    return T.canonical_json({"lang": pend["lang"], "track": pend["track"], "hash": pend["hash"],
                             "source_version": pend["source_version"], "package_hash": pend["package_hash"],
                             "imported_at": pend["imported_at"]})


def verify_seal(pend: dict) -> bool:
    own = _get(SECRET_VAR)
    if strength_problem(own) or not pend or not pend.get("seal"):
        return False
    try:
        return hmac.compare_digest(_hmac(own, _seal_text(pend)).encode(), str(pend["seal"]).encode("utf-8", "replace"))
    except Exception:
        return False


def import_package(envelope, actor: dict) -> dict:
    """Verify, then store the package as the DRAFT working copy for its lang + track and seal a pending record. NEVER publishes."""
    p = verify_package(envelope)
    lang, track = p["lang"], p["track"]
    meta = p["language"]
    existing = db[T.LANGS].find_one({"code": lang})
    if existing:
        if existing["direction"] != meta["direction"]:
            raise T.TranslationError("package_direction_mismatch", 409)
        if existing["status"] == "disabled":
            raise T.TranslationError("language_disabled", 409)
    else:
        db[T.LANGS].insert_one({"code": lang, "name_en": str(meta["name_en"])[:60], "name_native": str(meta["name_native"])[:60],
                                "direction": meta["direction"], "status": "draft", "bundled": False,
                                "created_by": actor.get("id", ""), "created_at": datetime.utcnow(), "schema": T.SCHEMA})
    pub = T.published_strings(track, lang)
    now = datetime.utcnow()
    db[T.VALUES].delete_many({"lang": lang, "track": track})
    for k, v in p["strings"].items():
        db[T.VALUES].insert_one({"lang": lang, "key": k, "track": track, "value": v, "value_hash": T.value_hash(v),
                                 "status": "published" if pub.get(k) == v else "modified", "submitted": False,
                                 "updated_by": actor.get("id", ""), "updated_at": now, "source": "package", "schema": T.SCHEMA})
    pend = {"lang": lang, "track": track, "source_version": p["version"], "hash": p["hash"],
            "package_hash": T.hash_strings({"payload": p}), "approval": p["approval"],
            "imported_by": {"id": actor.get("id", ""), "name": actor.get("name", "")},
            "imported_at": now.isoformat() + "Z"}
    pend["seal"] = _hmac(_get(SECRET_VAR), _seal_text(pend))
    db[T.STATE].update_one({"_id": T.STATE_ID}, {"$set": {f"pending.{track}:{lang}": pend, "schema": T.SCHEMA}}, upsert=True)
    return {"lang": lang, "track": track, "source_version": p["version"], "hash": p["hash"], "count": len(p["strings"]),
            "published": False, "next_step": "Review the draft, then publish explicitly (POST /admin/translations/publish)."}


def pending_packages() -> dict:
    pend = (db[T.STATE].find_one({"_id": T.STATE_ID}) or {}).get("pending", {})
    return {k: {x: v[x] for x in ("lang", "track", "source_version", "hash", "imported_by", "imported_at") if x in v}
            for k, v in pend.items()}
