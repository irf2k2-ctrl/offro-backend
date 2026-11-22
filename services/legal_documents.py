"""services/legal_documents.py - Legal & Information Documents: one registry over the documents the app already has, plus translation.

Why this exists
  The customer / merchant apps show long documents (Privacy Policy, Terms, Refund Policy, About Us ...). Their ENGLISH text already
  lives in different places and is edited in the dashboard exactly as before:
      collection `policies`   type privacy | refund | kyc          (GET /policy/{type},  Dashboard > Policies)
      collection `terms`      type user | merchant                 (GET /terms/{type},   GET /merchant/terms)
      collection `settings`   key about_us                         (GET /about)
  This module does NOT copy, move or duplicate that text. It only knows WHERE each document's English text is, so the translation
  editor can show it read-only next to the selected language. Document types that have no storage yet (Contact Us, FAQ ...) and any
  type an administrator adds later are stored in the one new collection `legal_documents` (English text only). Nothing is migrated.

How translations are stored (no second mechanism)
  A document is a translation KEY of the existing translation module: key "doc_<slug>", group "documents", kind "document", versioned in its
  own "documents" track (so it never mixes with the short app / legal strings, the app's override files or the transfer package). Because of that
  every rule of the existing translation architecture applies unchanged: translation_values (the working copy), immutable
  translation_versions (publish), the language-independent Modified/Published rule (T.effective_status, Stage 1), permissions,
  audit, the kill switch, country/language mapping and per-environment isolation (each environment has its own database).
  Only two things are document specific, both in T.validate_value: the length limit (DOCUMENT_MAX_LENGTH instead of 2000) and
  curly braces are literal text (a document is not a format string, so there are no placeholders to preserve).

English is never written here by the translation editor. The key's `english` field is refreshed from the source by refresh_keys()
(read-only on the source), and no translation route accepts the base language.

Public use: resolve_content() returns the PUBLISHED translation for a language when there is one, else the English text.
"""
import logging
import re
from datetime import datetime

from database import db
from services import translations as T

logger = logging.getLogger("offro.legal_documents")

OWN = "legal_documents"                      # English text of documents that have no existing storage (and admin-added types)
KEY_PREFIX = "doc_"
KIND = "document"
SCREEN_GROUP = "documents"
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
TITLE_MAX = 80

# slug, title, audience, source collection, source selector (None = stored in `legal_documents`)
CATALOG = (
    ("about_us",            "About Us",                     "customer", "settings", {"key": "about_us"}),
    ("contact_us",          "Contact Us",                   "customer", None, None),
    ("privacy_policy",      "Privacy Policy",               "customer", "policies", {"type": "privacy"}),
    ("terms_conditions",    "Terms & Conditions",           "customer", "terms", {"type": "user"}),
    ("refund_policy",       "Refund Policy",                "customer", "policies", {"type": "refund"}),
    ("cancellation_policy", "Cancellation Policy",          "customer", None, None),
    ("disclaimer",          "Disclaimer",                   "customer", None, None),
    ("faq",                 "FAQ",                          "customer", None, None),
    ("how_offro_works",     "How Offro Works",              "customer", None, None),
    ("merchant_terms",      "Merchant Terms & Conditions",  "merchant", "terms", {"type": "merchant"}),
    ("merchant_policy",     "Merchant Policy",              "merchant", None, None),
    ("kyc_policy",          "KYC Policy",                   "merchant", "policies", {"type": "kyc"}),
)
_BY_SLUG = {c[0]: c for c in CATALOG}
# the existing public endpoints -> slug, so they can serve a translation without changing their URL
POLICY_SLUGS = {"privacy": "privacy_policy", "refund": "refund_policy", "kyc": "kyc_policy"}
TERMS_SLUGS = {"user": "terms_conditions", "merchant": "merchant_terms"}


def key_for(slug: str) -> str:
    return KEY_PREFIX + slug


def slug_of_key(key: str) -> str:
    return key[len(KEY_PREFIX):] if str(key).startswith(KEY_PREFIX) else ""


# ── English sources (read only) ───────────────────────────────────────────────────────────────────────────────────────
def _default_english(slug: str) -> str:
    """The built-in default the public endpoint shows when the database has no text yet (so the editor shows what customers see)."""
    try:
        from routers import public as _pub
        fn = {"privacy_policy": "_default_privacy", "refund_policy": "_default_refund", "kyc_policy": "_default_kyc"}.get(slug)
        return getattr(_pub, fn)() if fn else ""
    except Exception:
        return ""


def _read_source(slug: str, coll: str, sel: dict) -> str:
    doc = db[coll].find_one(sel) or {}
    text = doc.get("content") or ""
    return text if isinstance(text, str) else ""


def _discovered() -> list:
    """Documents that exist in the existing collections but are not in the catalog (e.g. another `policies` type): kept, not hidden."""
    out = []
    known_policy, known_terms = set(POLICY_SLUGS), set(TERMS_SLUGS)
    for coll, known, prefix, aud in (("policies", known_policy, "policy_", "customer"), ("terms", known_terms, "terms_", "customer")):
        try:
            for d in db[coll].find({}, {"type": 1}):
                t = str(d.get("type") or "")
                slug = prefix + re.sub(r"[^a-z0-9_]", "_", t.lower())
                if t and t not in known and SLUG_RE.match(slug) and slug not in _BY_SLUG:
                    out.append((slug, t.replace("_", " ").title(), aud, coll, {"type": t}))
        except Exception:
            logger.warning("legal document discovery skipped for %s", coll)
    return out


def _own_docs() -> list:
    out = []
    for d in db[OWN].find({}):
        slug = str(d.get("slug") or "")
        if SLUG_RE.match(slug):
            out.append((slug, str(d.get("title") or slug)[:TITLE_MAX], str(d.get("audience") or "customer"), None, None))
    return out


def definitions() -> dict:
    """slug -> (slug, title, audience, source collection | None, selector | None). Catalog first, then admin-added, then discovered."""
    defs = {c[0]: c for c in CATALOG}
    for d in _own_docs():
        if d[0] in defs and defs[d[0]][3] is None:         # catalog type stored in `legal_documents`: keep catalog title unless renamed
            defs[d[0]] = (d[0], d[1] if d[1] != d[0] else defs[d[0]][1], d[2], None, None)
        elif d[0] not in defs:
            defs[d[0]] = d
    for d in _discovered():
        defs.setdefault(d[0], d)
    return defs


def english_text(slug: str, defs: dict = None) -> str:
    d = (defs or definitions()).get(slug)
    if not d:
        return ""
    if d[3] is None:
        return str((db[OWN].find_one({"slug": slug}) or {}).get("content") or "")
    text = _read_source(slug, d[3], d[4])
    return text or _default_english(slug)


def list_documents() -> list:
    """Every document type with its English state. `has_english` False = defined but nothing to translate yet."""
    defs = definitions()
    rows = []
    for slug, d in defs.items():
        text = english_text(slug, defs)
        rows.append({"slug": slug, "key": key_for(slug), "title": d[1], "audience": d[2], "has_english": bool(text.strip()),
                     "length": len(text), "too_long": len(text) > T.DOCUMENT_MAX_LENGTH,
                     "source": "own" if d[3] is None else d[3], "editable_english_here": d[3] is None})
    return rows


# ── key registry (derived from the sources; never writes a source) ────────────────────────────────────────────────────
def refresh_keys() -> dict:
    """Upserts one legal-track key per document that HAS English text and marks the key deprecated when its English is gone.
    Only `doc_*` keys of kind document are touched; no other key, value, version or source document is modified."""
    defs = definitions()
    live, changed = set(), 0
    for slug, d in defs.items():
        text = english_text(slug, defs)
        key = key_for(slug)
        if text.strip() and len(text) <= T.DOCUMENT_MAX_LENGTH:
            live.add(key)
            doc = {"key": key, "english": text, "placeholders": [], "is_plural": False, "group": T.DOCUMENT_TRACK, "kind": KIND,
                   "screen_group": SCREEN_GROUP, "description": d[1], "title": d[1], "audience": d[2],
                   "max_length": T.DOCUMENT_MAX_LENGTH, "deprecated": False, "schema": T.SCHEMA, "synced_at": datetime.utcnow()}
            old = T.db[T.KEYS].find_one({"key": key}, {"english": 1, "deprecated": 1, "title": 1})
            if not old or old.get("english") != text or old.get("deprecated") or old.get("title") != d[1]:
                T.db[T.KEYS].update_one({"key": key}, {"$set": doc}, upsert=True)
                changed += 1
    dep = T.db[T.KEYS].update_many({"kind": KIND, "key": {"$nin": list(live)}, "deprecated": {"$ne": True}}, {"$set": {"deprecated": True}})
    return {"documents": len(live), "changed": changed, "deprecated": dep.modified_count}


def refresh_keys_safe() -> bool:
    """Best-effort refresh for READ paths: skipped when the environment is not identified (every write is refused there)."""
    try:
        T.require_env("edit")
        refresh_keys()
        return True
    except Exception as e:
        logger.warning("legal document key refresh skipped (%s)", type(e).__name__)
        return False


# ── translation view for the editor ───────────────────────────────────────────────────────────────────────────────────
def document_view(slug: str, lang: str) -> dict:
    """English (read-only) + the selected language's working translation + its status, judged by the ONE Stage 1 rule."""
    T.get_language(lang)
    defs = definitions()
    if slug not in defs:
        raise T.TranslationError("unknown_document", 404)
    english = english_text(slug, defs)
    key = key_for(slug)
    v = T.db[T.VALUES].find_one({"lang": lang, "key": key})
    baseline = T.published_strings(T.DOCUMENT_TRACK, lang)
    status = T.effective_status(baseline, key, v["value"]) if v else "missing"
    stale = bool(v and v.get("english_hash") and v["english_hash"] != T.value_hash(english))
    return {"slug": slug, "key": key, "title": defs[slug][1], "audience": defs[slug][2], "lang": lang, "english": english,
            "has_english": bool(english.strip()), "too_long": len(english) > T.DOCUMENT_MAX_LENGTH,
            "value": v["value"] if v else None, "status": status, "english_changed": stale,
            "max_length": T.DOCUMENT_MAX_LENGTH}


def list_for_language(lang: str) -> list:
    T.get_language(lang)
    baseline = T.published_strings(T.DOCUMENT_TRACK, lang)
    work = {v["key"]: v for v in T.db[T.VALUES].find({"lang": lang, "track": T.DOCUMENT_TRACK, "key": {"$regex": "^" + KEY_PREFIX}})}
    rows = []
    for r in list_documents():
        v = work.get(r["key"])
        r["status"] = T.effective_status(baseline, r["key"], v["value"]) if v else "missing"
        rows.append(r)
    return rows


def language_summaries(langs: list) -> dict:
    """Overview table: per language code, the state of the Legal Documents that have English text (the same documents, statuses and
    Stage 1 Modified / Published rule as the editor; read only). Same shape as T.language_status() entries, plus `total` and `last_updated`
    (UTC ISO, newest of the last published version and the last draft edit, or None)."""
    docs = [r for r in list_documents() if r["has_english"] and not r["too_long"]]
    out = {}
    for lang in langs:
        baseline = T.published_strings(T.DOCUMENT_TRACK, lang)
        work = {v["key"]: v for v in T.db[T.VALUES].find({"lang": lang, "track": T.DOCUMENT_TRACK, "key": {"$regex": "^" + KEY_PREFIX}})}
        modified = missing = 0
        for r in docs:
            v = work.get(r["key"])
            st = T.effective_status(baseline, r["key"], v["value"]) if v else "missing"
            modified += st == "modified"
            missing += st == "missing"
        lv = T.latest_version(T.DOCUMENT_TRACK, lang)
        stamps = [x for x in ((lv or {}).get("published_at"),) if isinstance(x, datetime)]
        e = T.db[T.VALUES].find_one({"lang": lang, "track": T.DOCUMENT_TRACK, "updated_at": {"$type": "date"}}, sort=[("updated_at", -1)])
        if e:
            stamps.append(e["updated_at"])
        out[lang] = {"total": len(docs), "missing": missing, "draft_changes": modified,
                     "complete": bool(docs) and missing == 0 and modified == 0 and bool(lv),
                     "published_version": lv["version"] if lv else None,
                     "last_updated": max(stamps).isoformat() + "Z" if stamps else None}
    return out


def save_translation(lang: str, slug: str, value, actor_id: str) -> dict:
    """Same path as every other translation (T.set_value: validation, environment, language checks, Stage 1 status), plus the hash of
    the English text it was written against so the editor can tell when the English changed afterwards."""
    if slug not in definitions():
        raise T.TranslationError("unknown_document", 404)
    refresh_keys()
    res = T.set_value(lang, key_for(slug), value, actor_id)
    T.db[T.VALUES].update_one({"lang": lang, "key": key_for(slug)},
                              {"$set": {"english_hash": T.value_hash(english_text(slug))}})
    return res


# ── English text of documents stored in `legal_documents` (Super Admin only, route-checked) ────────────────────────────
def _clean_title(title) -> str:
    t = " ".join(str(title or "").split())
    if not t or len(t) > TITLE_MAX:
        raise T.TranslationError("invalid_title", 422, {"max": TITLE_MAX})
    return t


def _clean_content(content) -> str:
    c = str(content or "").replace("\r\n", "\n").strip()
    if not c:
        raise T.TranslationError("empty_content", 422)
    if len(c) > T.DOCUMENT_MAX_LENGTH:
        raise T.TranslationError("too_long", 422, {"max_length": T.DOCUMENT_MAX_LENGTH})
    return c


def create_document(slug, title, content, audience, actor_id: str) -> dict:
    """Adds a document type (or fills a catalog type that has no storage yet). Refuses a slug that already exists anywhere, so a
    document is never duplicated and an existing policy/terms/about text is never replaced."""
    T.require_env("edit")
    slug = str(slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise T.TranslationError("invalid_slug", 422)
    title, content = _clean_title(title), _clean_content(content)
    audience = audience if audience in ("customer", "merchant") else "customer"
    defs = definitions()
    if slug in defs and (defs[slug][3] is not None or english_text(slug, defs).strip()):
        raise T.TranslationError("document_exists", 409)
    if slug in (set(POLICY_SLUGS.values()) | set(TERMS_SLUGS.values()) | {"about_us"}):
        raise T.TranslationError("document_exists", 409)
    now = datetime.utcnow()
    db[OWN].update_one({"slug": slug}, {"$set": {"slug": slug, "title": title, "audience": audience, "content": content,
                                                 "updated_by": actor_id, "updated_at": now},
                                        "$setOnInsert": {"created_by": actor_id, "created_at": now}}, upsert=True)
    refresh_keys()
    return {"slug": slug, "key": key_for(slug), "title": title}


def update_own_english(slug: str, content, actor_id: str, title=None) -> dict:
    T.require_env("edit")
    d = definitions().get(slug)
    if not d or d[3] is not None:
        raise T.TranslationError("not_editable_here", 409, "Edit this document where it is managed (Dashboard > Policies / Terms / About)")
    upd = {"content": _clean_content(content), "updated_by": actor_id, "updated_at": datetime.utcnow()}
    if title is not None:
        upd["title"] = _clean_title(title)
    db[OWN].update_one({"slug": slug}, {"$set": upd, "$setOnInsert": {"slug": slug, "created_by": actor_id,
                                                                      "created_at": datetime.utcnow(), "audience": d[2]}}, upsert=True)
    refresh_keys()
    return {"slug": slug, "key": key_for(slug)}


# ── public: published translation or English ──────────────────────────────────────────────────────────────────────────
def _lang_candidates(lang: str) -> list:
    code = str(lang or "").strip().replace("_", "-")
    if not code or len(code) > 12 or not re.match(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$", code):
        return []
    first = code.split("-")[0].lower()
    return [code.lower(), first] if code.lower() != first else [first]


def resolve_content(slug: str, english: str, lang: str) -> dict:
    """{'content', 'lang', 'translated', 'direction'}. The PUBLISHED legal-track text for `lang` if it exists, otherwise the English
    text (also when the kill switch is on, the language is not active, or anything is unreadable). Drafts are never returned."""
    base = {"content": english, "lang": "en", "translated": False, "direction": "ltr"}
    try:
        cands = [c for c in _lang_candidates(lang) if c != T.BASE_LANGUAGE]
        if not cands or T.kill_switch_on():
            return base
        key = key_for(slug)
        for code in cands:
            language = T.db[T.LANGS].find_one({"code": code, "status": "active"})
            if not language:
                continue
            text = T.published_strings(T.DOCUMENT_TRACK, code).get(key)
            if isinstance(text, str) and text.strip():
                return {"content": text, "lang": code, "translated": True, "direction": language.get("direction", "ltr")}
    except Exception as e:
        logger.warning("legal document translation lookup failed (%s); serving English", type(e).__name__)
    return base


def slug_for(coll: str, type_: str) -> str:
    """The document slug of an existing public endpoint's type (policies/{type}, terms/{type})."""
    t = str(type_ or "")
    known = POLICY_SLUGS if coll == "policies" else TERMS_SLUGS
    return known.get(t) or (("policy_" if coll == "policies" else "terms_") + re.sub(r"[^a-z0-9_]", "_", t.lower()))


def localize(slug: str, english: str, lang: str) -> dict:
    """Extra response fields for an EXISTING public endpoint when the app passes ?lang=. Nothing is added without a language, so the
    existing responses stay byte-for-byte what they were. English text (and an empty document) is returned as is."""
    if not str(lang or "").strip():
        return {"content": english}
    if not str(english or "").strip():
        return {"content": english, "lang": "en", "translated": False, "direction": "ltr"}
    r = resolve_content(slug, english, lang)
    return {k: r[k] for k in ("content", "lang", "translated", "direction")}


def public_list() -> list:
    return [{"slug": r["slug"], "title": r["title"], "audience": r["audience"]} for r in list_documents() if r["has_english"]]


def public_document(slug: str, lang: str = ""):
    defs = definitions()
    if slug not in defs:
        return None
    english = english_text(slug, defs)
    if not english.strip():
        return None
    out = resolve_content(slug, english, lang)
    out.update({"slug": slug, "title": defs[slug][1]})
    return out
