"""services/default_images.py - country mapping for Default Images (Dashboard > Default Images).

What exists (unchanged): ONE `settings` document {_type: "default_images"} holding, per category, an ARRAY of image URLs
(store, product, offer, city, merchant_banner; older data may be a single string) plus the single Service Unavailable config
(no_service_url / no_service_title / no_service_message). Nothing in that document is rewritten, moved or deleted by this module.

What this adds (two NEW collections, created on first use; no migration, no backfill):
  default_image_countries      category, url_key (sha256 of the URL), countries [ISO codes], updated_by, updated_at
                               one record per MAPPED image. An image with NO record is "All countries" (legacy / global).
  default_no_service_variants  variant_id, url, title, message, countries [ISO codes, at least one], created/updated
                               country-specific Service Unavailable screens. The existing single config stays the "All countries" one.
Countries are ISO codes from the ONE country master (utils.phone.SUPPORTED_COUNTRIES via utils.countries); this module keeps no list.

Retrieval rule ("most specific wins"), the same for EVERY category including Service Unavailable:
  * country known: the images mapped to that country if there are any, otherwise the unmapped (All countries) images;
  * country missing or not in the master (older apps, guests): only the unmapped images. A country-specific image is never shown to
    a customer of another country, and with no mappings at all the result is exactly what it always was.
Staging and production each use their own database, so mappings are per environment like every other default image.
"""
import hashlib
import logging
import threading
import uuid
from datetime import datetime

from database import db
from utils import countries as _countries

logger = logging.getLogger(__name__)

MAPPINGS = "default_image_countries"
VARIANTS = "default_no_service_variants"
CATEGORIES = ("store", "product", "offer", "city", "merchant_banner")
NO_SERVICE = "no_service"
TITLE_MAX, MESSAGE_MAX, URL_MAX = 120, 500, 2_000_000     # URL_MAX: a base64 data URL is accepted exactly as the existing config does


class DefaultImageError(Exception):
    """A refused request. `code` is a short stable string, `status` the HTTP status the routes use."""

    def __init__(self, code: str, status: int = 422, message: str = ""):
        super().__init__(code)
        self.code, self.status, self.message = code, status, message or code


_indexes_ok = False
_index_lock = threading.Lock()


def ensure_indexes() -> None:
    """Creates the two unique indexes. create_index is idempotent (same spec = no-op), never drops or rewrites data, and the
    collections are new, so no existing document can conflict. Raises on failure."""
    db[MAPPINGS].create_index([("category", 1), ("url_key", 1)], unique=True, name="default_image_countries_cat_url")
    db[VARIANTS].create_index("variant_id", unique=True, name="default_no_service_variants_id")


def ensure_indexes_safe() -> bool:
    """Called from the application's startup. Never raises; a failure is LOGGED (error level) and returns False, and the
    lazy guard below retries before the first write."""
    global _indexes_ok
    try:
        ensure_indexes()
        _indexes_ok = True
        return True
    except Exception as e:
        _indexes_ok = False
        logger.error("default image country index creation failed (%s: %s); will retry before the first write", type(e).__name__, e)
        return False


def ensure_indexes_once() -> bool:
    """Lazy guard for the write paths: if startup could not create the indexes (DB briefly unreachable), try again once per
    process as soon as one succeeds. Cheap after success (a flag check)."""
    if _indexes_ok:
        return True
    with _index_lock:
        return _indexes_ok or ensure_indexes_safe()


def url_key(url: str) -> str:
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()


# ── validation ────────────────────────────────────────────────────────────────────────────────────────────────────────
def clean_countries(value, *, required: bool = False) -> list:
    """List (or comma separated text from a form) of ISO codes -> clean list. [] means "All countries".
    Rejects a code that is not in the country master (invalid_country) and a code given twice (duplicate_country); nothing is
    silently dropped or guessed."""
    if value is None or value == "":
        items = []
    elif isinstance(value, str):
        items = [p.strip() for p in value.split(",") if p.strip()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise DefaultImageError("invalid_countries", 422, "countries must be a list of country codes")
    known, out, seen = _countries.supported_iso_codes(), [], set()
    if len(items) > len(known) * 2:
        raise DefaultImageError("invalid_countries", 422, "too many country codes")
    for raw in items:
        if not isinstance(raw, str):
            raise DefaultImageError("invalid_country", 422, "country codes must be text")
        code = raw.strip().upper()
        if code not in known:
            raise DefaultImageError("invalid_country", 422, f"{raw[:20]!r} is not a supported country")
        if code in seen:
            raise DefaultImageError("duplicate_country", 422, f"{code} was selected more than once")
        seen.add(code)
        out.append(code)
    if required and not out:
        raise DefaultImageError("countries_required", 422, "select at least one country")
    order = [c.iso for c in _countries_master()]
    return sorted(out, key=lambda c: order.index(c) if c in order else 99)


def _countries_master() -> list:
    from utils import phone as _ph
    return list(_ph.SUPPORTED_COUNTRIES)


def clean_filter(value) -> str:
    """Admin list filter: '' / 'all' -> 'all', 'global' (unmapped images), or a supported ISO code."""
    v = str(value or "").strip()
    if v.lower() in ("", "all"):
        return "all"
    if v.lower() == "global":
        return "global"
    code = v.upper()
    if code not in _countries.supported_iso_codes():
        raise DefaultImageError("invalid_country", 422, f"{v[:20]!r} is not a supported country")
    return code


def clean_category(value) -> str:
    if value not in CATEGORIES:
        raise DefaultImageError("invalid_type", 400, "Invalid image type")
    return value


def public_country(value) -> str:
    """The customer's country for retrieval: a supported ISO code, otherwise '' (treated as 'unknown': unmapped images only)."""
    code = str(value or "").strip().upper()
    return code if code in _countries.supported_iso_codes() else ""


# ── reading the existing arrays ───────────────────────────────────────────────────────────────────────────────────────
def as_list(v) -> list:
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str) and x]
    if isinstance(v, str) and v:
        return [v]
    return []


def mapping_for(category: str) -> dict:
    return {d["url_key"]: list(d.get("countries") or []) for d in db[MAPPINGS].find({"category": category})}


def countries_of(category: str, url: str) -> list:
    d = db[MAPPINGS].find_one({"category": category, "url_key": url_key(url)})
    return list(d.get("countries") or []) if d else []


def select(urls: list, mapping: dict, country: str):
    """(selected urls, country_specific) for one category under the retrieval rule."""
    if country:
        mine = [u for u in urls if country in mapping.get(url_key(u), [])]
        if mine:
            return mine, True
    return [u for u in urls if not mapping.get(url_key(u))], False


# ── writing mappings ──────────────────────────────────────────────────────────────────────────────────────────────────
def set_mapping(category: str, url: str, countries: list, actor_id: str = "") -> list:
    """Stores the countries of ONE image (an empty list = All countries = no record). Caller validated `countries`."""
    category = clean_category(category)
    if not countries:
        db[MAPPINGS].delete_one({"category": category, "url_key": url_key(url)})
        return []
    ensure_indexes_once()
    db[MAPPINGS].update_one({"category": category, "url_key": url_key(url)},
                            {"$set": {"category": category, "url_key": url_key(url), "countries": list(countries),
                                      "updated_by": actor_id, "updated_at": datetime.utcnow()}}, upsert=True)
    return list(countries)


def clear_mapping(category: str, url: str) -> None:
    db[MAPPINGS].delete_one({"category": category, "url_key": url_key(url)})


def image_exists(category: str, url: str) -> bool:
    doc = db.settings.find_one({"_type": "default_images"}, {category: 1}) or {}
    return url in as_list(doc.get(category))


def update_image_countries(category: str, url: str, countries, actor_id: str = "") -> dict:
    category = clean_category(category)
    clean = clean_countries(countries)
    url = str(url or "")
    if not url or not image_exists(category, url):
        raise DefaultImageError("image_not_found", 404, "That image is not in this category")
    return {"type": category, "url": url, "countries": set_mapping(category, url, clean, actor_id)}


# ── Service Unavailable variants ──────────────────────────────────────────────────────────────────────────────────────
def _clean_variant(body: dict) -> dict:
    url = str(body.get("url") or "").strip()
    if not url or len(url) > URL_MAX or not (url.startswith("http://") or url.startswith("https://") or url.startswith("data:image")):
        raise DefaultImageError("invalid_url", 422, "Provide an https:// image URL")
    title, message = str(body.get("title") or "").strip(), str(body.get("message") or "").strip()
    if len(title) > TITLE_MAX or len(message) > MESSAGE_MAX:
        raise DefaultImageError("text_too_long", 422, "The title or message is too long")
    return {"url": url, "title": title, "message": message, "countries": clean_countries(body.get("countries"), required=True)}


def variant_view(d: dict) -> dict:
    return {"id": d["variant_id"], "url": d.get("url", ""), "title": d.get("title", ""), "message": d.get("message", ""),
            "countries": list(d.get("countries") or [])}


def list_variants() -> list:
    return [variant_view(d) for d in db[VARIANTS].find({}).sort("created_at", 1)]


def create_variant(body: dict, actor_id: str = "") -> dict:
    clean = _clean_variant(body or {})
    ensure_indexes_once()
    now = datetime.utcnow()
    doc = {"variant_id": uuid.uuid4().hex, **clean, "created_by": actor_id, "created_at": now, "updated_by": actor_id, "updated_at": now}
    db[VARIANTS].insert_one(doc)
    return variant_view(doc)


def update_variant(variant_id: str, body: dict, actor_id: str = "") -> dict:
    cur = db[VARIANTS].find_one({"variant_id": str(variant_id)})
    if not cur:
        raise DefaultImageError("variant_not_found", 404, "That Service Unavailable entry does not exist")
    merged = {"url": cur.get("url"), "title": cur.get("title", ""), "message": cur.get("message", ""), "countries": cur.get("countries")}
    merged.update({k: v for k, v in (body or {}).items() if k in merged})
    clean = _clean_variant(merged)
    db[VARIANTS].update_one({"variant_id": cur["variant_id"]}, {"$set": {**clean, "updated_by": actor_id, "updated_at": datetime.utcnow()}})
    return variant_view(db[VARIANTS].find_one({"variant_id": cur["variant_id"]}))


def delete_variant(variant_id: str) -> None:
    if db[VARIANTS].delete_one({"variant_id": str(variant_id)}).deleted_count != 1:
        raise DefaultImageError("variant_not_found", 404, "That Service Unavailable entry does not exist")


# ── admin listing ─────────────────────────────────────────────────────────────────────────────────────────────────────
def admin_items(doc: dict, flt: str) -> dict:
    """category -> [{'url', 'countries'}] after the admin's country filter (all | global | ISO). Order of the stored array is kept."""
    out = {}
    for cat in CATEGORIES:
        m, rows = mapping_for(cat), []
        for u in as_list((doc or {}).get(cat)):
            cs = m.get(url_key(u), [])
            if flt == "all" or (flt == "global" and not cs) or (flt not in ("all", "global") and flt in cs):
                rows.append({"url": u, "countries": cs})
        out[cat] = rows
    return out


def admin_variants(flt: str) -> list:
    rows = list_variants()
    if flt == "all":
        return rows
    if flt == "global":
        return []
    return [r for r in rows if flt in r["countries"]]


# ── customer retrieval ────────────────────────────────────────────────────────────────────────────────────────────────
def apply_country(doc: dict, country: str):
    """A COPY of the default_images document narrowed to what this country's customer may see, plus {category: country_specific}.
    The caller then formats it exactly as before, so the response shape and the legacy string/array handling are unchanged."""
    out, specific = dict(doc or {}), {}
    for cat in CATEGORIES:
        if cat in out:
            out[cat], specific[cat] = select(as_list(out[cat]), mapping_for(cat), country)
    base_url = (doc or {}).get("no_service_url", (doc or {}).get("no_service", ""))
    variant = None
    if country:
        for d in db[VARIANTS].find({}).sort("created_at", 1):
            if country in (d.get("countries") or []):
                variant = d
                break
    if variant:
        out["no_service_url"] = variant.get("url", "")
        if variant.get("title"):
            out["no_service_title"] = variant["title"]
        if variant.get("message"):
            out["no_service_message"] = variant["message"]
        out.pop("no_service", None)
    specific[NO_SERVICE] = bool(variant)
    expl = out.get("merchant_banner_explore_url") or ""
    if expl and expl not in out.get("merchant_banner", []):
        out["merchant_banner_explore_url"] = ""          # never point at a media this customer is not shown
    return out, specific


def has_any_variant_for(country: str) -> bool:
    return bool(country) and db[VARIANTS].count_documents({"countries": country}) > 0
