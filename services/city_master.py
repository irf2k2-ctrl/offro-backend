"""services/city_master.py - admin-side City Master management for every country (Admin -> Cities).

One collection (`db.cities`), one shape for every country. Everything here is ADDITIVE on top of services/cities.py (country, names):

  state          optional text (state / province / governorate), English
  is_popular     optional bool, missing = False
  popular_order  optional int >= 1, only meaningful with is_popular (1 = first)
  name_key       lower-cased English name (duplicate checks)
  source / import_batch / updated_at / updated_by   audit

Uniqueness: (country, name, state). A city with no state matches a same-name city of the same country, so an existing record that
has no state is recognised and never duplicated. A city with no country ("unassigned", every record from before cities carried a
country) counts as a possible duplicate of the same name in any country, so the admin assigns it instead of creating a twin.

Nothing here runs on import or on startup, and nothing here edits an existing city unless an admin asks for exactly that change.
"""
import re
from datetime import datetime
from typing import Optional

from bson import ObjectId

from services import cities as _city
from utils import countries as _countries

MAX_STATE = 60
MAX_POPULAR_SHOWN = 6
_EN_NAME = re.compile(r"^[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ .'’()\-]*$")
_AR_LETTERS = re.compile(r"[؀-ۿ]")
_BAD_AR = re.compile(r"[0-9٠-٩۰-۹A-Za-z]")


class _LiveDb:
    """The current `database.db`, resolved at call time (tests swap it)."""
    def __getattr__(self, name):
        import database
        return getattr(database.db, name)


db = _LiveDb()


class CityError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, field: str = ""):
        super().__init__(message)
        self.code, self.message, self.status, self.field = code, message, status, field


# ── value validation (shared by the admin forms and the importer) ─────────────────────────────────────
def name_key(name) -> str:
    return " ".join(str(name or "").split()).lower()


def clean_name(value) -> str:
    n = " ".join(str(value or "").split())
    if not n:
        raise CityError("name_required", "City name (English) is required", field="name")
    if len(n) < 2 or len(n) > _city.MAX_NAME:
        raise CityError("name_length", "City name must be 2 to %d characters" % _city.MAX_NAME, field="name")
    if not _EN_NAME.match(n):
        raise CityError("name_chars", "City name may contain letters, spaces, hyphen, apostrophe, dot and parentheses only", field="name")
    return n


def clean_arabic(value) -> str:
    """'' when empty. Arabic script only: no digits and no Latin letters."""
    n = " ".join(str(value or "").split())
    if not n:
        return ""
    if len(n) > _city.MAX_NAME:
        raise CityError("arabic_invalid", "Arabic name is too long", field="name_ar")
    if _BAD_AR.search(n):
        raise CityError("arabic_digits" if re.search(r"[0-9٠-٩۰-۹]", n) else "arabic_invalid",
                        "Arabic name contains digits" if re.search(r"[0-9٠-٩۰-۹]", n) else "Arabic name must use Arabic letters only",
                        field="name_ar")
    if not _AR_LETTERS.search(n):
        raise CityError("arabic_invalid", "Arabic name must use Arabic letters", field="name_ar")
    return n


def clean_state(value) -> str:
    s = " ".join(str(value or "").split())
    if len(s) > MAX_STATE:
        raise CityError("state_length", "State / province must be at most %d characters" % MAX_STATE, field="state")
    return s


def clean_int(value, field: str, minimum: int, code: str):
    if isinstance(value, bool):
        raise CityError(code, "%s must be a whole number" % field, field=field)
    try:
        f = float(value)
        if f != int(f):
            raise ValueError
        n = int(f)
    except (TypeError, ValueError):
        raise CityError(code, "%s must be a whole number" % field, field=field)
    if n < minimum:
        raise CityError(code, "%s must be at least %d" % (field, minimum), field=field)
    return n


def to_bool(value, default=False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "active")


def require_country(value) -> str:
    iso = _city.normalize_country(value)
    if not value or not str(value).strip():
        raise CityError("country_required", "Choose a country", field="country")
    if not iso:
        raise CityError("country_unsupported", "Country is not supported", field="country")
    return iso


def country_name(iso: str) -> str:
    info = _countries.country_info(iso) or {}
    return info.get("name") or iso


# ── lookup ───────────────────────────────────────────────────────────────────────────────────────────────
def _no_country():
    return {"$or": [{"country": {"$exists": False}}, {"country": ""}, {"country": None}]}


def find_conflict(iso: str, name: str, state: str = "", exclude_id=None) -> Optional[dict]:
    """The existing city that would make (country, name, state) a duplicate, else None."""
    key = name_key(name)
    st = (state or "").strip().lower()
    q = {"$and": [{"$or": [{"country": iso}, {"country": {"$exists": False}}, {"country": ""}, {"country": None}]},
                  {"$or": [{"name_key": key}, {"name": {"$regex": "^" + re.escape(" ".join(str(name).split())) + "$", "$options": "i"}}]}]}
    for d in db.cities.find(q):
        if exclude_id is not None and str(d.get("_id")) == str(exclude_id):
            continue
        other = str(d.get("state") or "").strip().lower()
        if not st or not other or st == other:
            return d
    return None


def conflict_message(d: dict, iso: str, name: str, state: str) -> str:
    if not d.get("country"):
        return "%s already exists without a country. Assign it to %s instead of adding it again." % (d.get("name", name), country_name(iso))
    where = " (%s)" % d["state"] if d.get("state") else ""
    return "%s already exists in %s%s." % (d.get("name", name), country_name(iso), where)


def used_by(doc: dict) -> dict:
    """How many stores / accounts use this city's English name (they keep the canonical English name)."""
    nm = str(doc.get("name") or "")
    if not nm:
        return {"stores": 0, "accounts": 0}
    rx = {"$regex": "^" + re.escape(nm) + "$", "$options": "i"}
    try:
        stores = db.stores.count_documents({"city": rx})
    except Exception:
        stores = 0
    try:
        accounts = db.accounts.count_documents({"city": rx})
    except Exception:
        accounts = 0
    return {"stores": stores, "accounts": accounts}


def row(d: dict) -> dict:
    """Admin view of one city (the existing fields keep their names; the new ones are added)."""
    img = d.get("image_url", "") or ""
    return {
        "id": str(d["_id"]), "name": d.get("name", ""), "image_url": img, "has_image": bool(img),
        "sort_order": d.get("sort_order", 0), "active": d.get("active", True) if d.get("status") != "inactive" else False,
        "status": d.get("status") or ("active" if d.get("active", True) else "inactive"),
        "created_at": str(d.get("created_at", "")), "country": d.get("country", "") or "",
        "names": d.get("names") if isinstance(d.get("names"), dict) else {},
        "state": d.get("state", "") or "", "is_popular": bool(d.get("is_popular")),
        "popular_order": d.get("popular_order") if d.get("is_popular") else None,
        "source": d.get("source", "") or "", "import_batch": d.get("import_batch", "") or "",
    }


def _sort_key(r: dict):
    return (r["name"].lower(), r["state"].lower())


def list_cities(iso: str = "", q: str = "", status: str = "", popular: Optional[bool] = None, scope: str = "", unassigned: bool = False) -> list:
    """Admin list. `iso` '' = every country; `unassigned` = only cities with no country. `scope` is the dashboard user's country
    restriction ('' = none). Filters combine; the result is alphabetical (English name, then state)."""
    flt = {}
    if unassigned:
        flt = _no_country()
    elif iso:
        flt = {"country": iso}
    docs = [row(d) for d in db.cities.find(flt)]
    if scope:
        docs = [r for r in docs if r["country"] == scope]
    st = (status or "").strip().lower()
    if st in ("active", "inactive"):
        docs = [r for r in docs if (r["status"] == "active") == (st == "active")]
    if popular is True:
        docs = [r for r in docs if r["is_popular"]]
    qq = (q or "").strip().lower()
    if qq:
        docs = [r for r in docs if qq in (r["name"] + " " + " ".join(str(v) for v in r["names"].values()) + " " + r["state"]).lower()]
    return sorted(docs, key=_sort_key)


def summary(scope: str = "") -> dict:
    out = {}
    for c in _countries.supported_countries():
        code = c.get("iso") or c.get("code")
        if not code or (scope and code != scope):
            continue
        out[code] = {"code": code, "name": c.get("name") or code, "total": 0, "active": 0, "popular": 0}
    unassigned = 0
    for d in db.cities.find({}):
        r = row(d)
        if not r["country"]:
            unassigned += 1
            continue
        s = out.get(r["country"])
        if not s:
            continue
        s["total"] += 1
        s["active"] += 1 if r["status"] == "active" else 0
        s["popular"] += 1 if r["is_popular"] else 0
    return {"countries": list(out.values()), "unassigned": 0 if scope else unassigned}


# ── popular ordering ────────────────────────────────────────────────────────────────────────────────────
def _popular_docs(iso: str) -> list:
    docs = [d for d in db.cities.find({"country": iso, "is_popular": True})]
    return sorted(docs, key=lambda d: (int(d.get("popular_order") or 10 ** 6), str(d.get("name") or "").lower()))


def _make_room(iso: str, order: int, exclude_id=None):
    """Put a city at `order`: the cities already at/after it move one place down."""
    for d in _popular_docs(iso):
        if exclude_id is not None and str(d["_id"]) == str(exclude_id):
            continue
        o = int(d.get("popular_order") or 10 ** 6)
        if o >= order:
            db.cities.update_one({"_id": d["_id"]}, {"$set": {"popular_order": o + 1}})


def _next_order(iso: str) -> int:
    return max([int(d.get("popular_order") or 0) for d in _popular_docs(iso)] + [0]) + 1


def renumber_popular(iso: str):
    for i, d in enumerate(_popular_docs(iso), 1):
        if d.get("popular_order") != i:
            db.cities.update_one({"_id": d["_id"]}, {"$set": {"popular_order": i}})


def set_popular_order(iso: str, ids: list) -> dict:
    """Atomic from the admin's point of view: exactly `ids` are popular for `iso`, in that order; every other city of the country is
    unflagged. Every id must be a city of that country."""
    if not isinstance(ids, list):
        raise CityError("ids_invalid", "ids must be a list", field="ids")
    seen, oids = set(), []
    for x in ids:
        try:
            o = ObjectId(str(x))
        except Exception:
            raise CityError("ids_invalid", "Invalid city id", field="ids")
        if o in seen:
            raise CityError("ids_invalid", "A city is listed twice", field="ids")
        seen.add(o)
        oids.append(o)
    found = {d["_id"]: d for d in db.cities.find({"_id": {"$in": oids}})}
    for o in oids:
        d = found.get(o)
        if not d or d.get("country") != iso:
            raise CityError("ids_invalid", "Every city must belong to the selected country", field="ids")
    unflagged = 0
    for d in db.cities.find({"country": iso, "is_popular": True}):
        if d["_id"] not in seen:
            db.cities.update_one({"_id": d["_id"]}, {"$set": {"is_popular": False}, "$unset": {"popular_order": ""}})
            unflagged += 1
    for i, o in enumerate(oids, 1):
        db.cities.update_one({"_id": o}, {"$set": {"is_popular": True, "popular_order": i}})
    return {"popular": len(oids), "unflagged": unflagged}


# ── create / update ──────────────────────────────────────────────────────────────────────────────────────
def build_new(body: dict, source: str = "admin", batch: str = "", iso: str = "") -> dict:
    """A validated new city document from an admin form or an import row. Raises CityError."""
    iso = iso or require_country(body.get("country"))
    name = clean_name(body.get("name"))
    state = clean_state(body.get("state"))
    ar = clean_arabic(body.get("name_ar") if "name_ar" in body else (body.get("names") or {}).get("ar") if isinstance(body.get("names"), dict) else "")
    names = {}
    if isinstance(body.get("names"), dict):
        try:
            names = _city.clean_names(body["names"])
        except ValueError as e:
            raise CityError("names_invalid", str(e), field="names")
        if "ar" in names:
            names["ar"] = clean_arabic(names["ar"])
    if ar:
        names["ar"] = ar
    names = {k: v for k, v in names.items() if v}
    active = to_bool(body.get("active"), True)
    popular = to_bool(body.get("is_popular"), False)
    doc = {
        "name": name, "slug": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or name.lower(), "name_key": name_key(name),
        "country": iso, "state": state, "image_url": body.get("image_url", "") or "",
        "sort_order": clean_int(body.get("sort_order", 0), "Sort order", 0, "sort_order_invalid") if body.get("sort_order") not in (None, "") else 0,
        "active": active, "status": "active" if active else "inactive", "is_popular": popular,
        "created_at": datetime.utcnow().isoformat(), "source": source,
    }
    if names:
        doc["names"] = names
    if batch:
        doc["import_batch"] = batch
    if popular:
        doc["popular_order"] = clean_int(body["popular_order"], "Popular order", 1, "popular_order_invalid") \
            if body.get("popular_order") not in (None, "") else 0           # 0 = assign on insert
    return doc


def create(body: dict, who: str = "") -> str:
    doc = build_new(body)
    clash = find_conflict(doc["country"], doc["name"], doc["state"])
    if clash:
        raise CityError("duplicate_city", conflict_message(clash, doc["country"], doc["name"], doc["state"]), 409, "name")
    if doc["is_popular"]:
        if doc["popular_order"]:
            _make_room(doc["country"], doc["popular_order"])
        else:
            doc["popular_order"] = _next_order(doc["country"])
    doc["updated_by"], doc["updated_at"] = who, datetime.utcnow().isoformat()
    return str(db.cities.insert_one(doc).inserted_id)


def update(city_id: str, body: dict, who: str = "", scope: str = "") -> dict:
    """Apply only the fields present in `body`. Image, names, order etc. that are not sent are never touched."""
    try:
        cur = db.cities.find_one({"_id": ObjectId(city_id)})
    except Exception:
        cur = None
    if not cur:
        raise CityError("not_found", "City not found", 404)
    if scope and cur.get("country") != scope:
        raise CityError("country_scope", "Your account is limited to another country", 403)
    s, u = {}, {}
    if "name" in body:
        s["name"] = clean_name(body["name"])
        s["name_key"] = name_key(s["name"])
        if s["name"] != cur.get("name"):
            s["slug"] = re.sub(r"[^a-z0-9]+", "-", s["name"].lower()).strip("-") or s["name"].lower()
    if "state" in body:
        s["state"] = clean_state(body["state"])
    iso = cur.get("country") or ""
    if "country" in body:
        if body["country"] in (None, ""):
            u["country"] = ""
            iso = ""
        else:
            iso = require_country(body["country"])
            if scope and iso != scope:
                raise CityError("country_scope", "Your account is limited to another country", 403)
            s["country"] = iso
    if "image_url" in body:
        s["image_url"] = body["image_url"] or ""
    if "sort_order" in body:
        s["sort_order"] = clean_int(body["sort_order"], "Sort order", 0, "sort_order_invalid")
    if "active" in body:
        s["active"] = to_bool(body["active"], True)
        s["status"] = "active" if s["active"] else "inactive"
    if "names" in body or "name_ar" in body:
        names = dict(cur.get("names") if isinstance(cur.get("names"), dict) else {})
        if "names" in body:
            try:
                names = _city.clean_names(body["names"])
            except ValueError as e:
                raise CityError("names_invalid", str(e), field="names")
        if "name_ar" in body:
            ar = clean_arabic(body["name_ar"])
            if ar:
                names["ar"] = ar
            else:
                names.pop("ar", None)
        if "ar" in names:
            names["ar"] = clean_arabic(names["ar"])
        s["names"] = {k: v for k, v in names.items() if v}
    # duplicate check only when something that identifies the city changed
    if iso and any(k in s for k in ("name", "state", "country")):
        clash = find_conflict(iso, s.get("name", cur.get("name", "")), s.get("state", cur.get("state", "")), exclude_id=city_id)
        if clash:
            raise CityError("duplicate_city", conflict_message(clash, iso, s.get("name", cur.get("name", "")), s.get("state", cur.get("state", ""))), 409, "name")
    old_iso = cur.get("country") or ""
    if "is_popular" in body or "popular_order" in body:
        pop = to_bool(body.get("is_popular"), bool(cur.get("is_popular"))) if "is_popular" in body else bool(cur.get("is_popular"))
        if pop:
            s["is_popular"] = True
            if body.get("popular_order") not in (None, ""):
                o = clean_int(body["popular_order"], "Popular order", 1, "popular_order_invalid")
                _make_room(iso, o, exclude_id=city_id)
                s["popular_order"] = o
            elif not cur.get("is_popular"):
                s["popular_order"] = _next_order(iso)
        else:
            s["is_popular"] = False
            u["popular_order"] = ""
    elif "country" in s and cur.get("is_popular") and iso != old_iso:
        s["popular_order"] = _next_order(iso)          # moved to another country: goes to the end of that country's Popular list
    if not s and not u:
        raise CityError("nothing_to_update", "Nothing to update")
    s["updated_by"], s["updated_at"] = who, datetime.utcnow().isoformat()
    ops = {"$set": s}
    if u:
        ops["$unset"] = u
    db.cities.update_one({"_id": cur["_id"]}, ops)
    if old_iso and (s.get("is_popular") is False or ("country" in s and iso != old_iso)):
        renumber_popular(old_iso)
    return row(db.cities.find_one({"_id": cur["_id"]}))


def delete(city_id: str, scope: str = "") -> None:
    try:
        cur = db.cities.find_one({"_id": ObjectId(city_id)})
    except Exception:
        cur = None
    if not cur:
        raise CityError("not_found", "City not found", 404)
    if scope and cur.get("country") != scope:
        raise CityError("country_scope", "Your account is limited to another country", 403)
    u = used_by(cur)
    if u["stores"] or u["accounts"]:
        parts = []
        if u["stores"]:
            parts.append("%d store%s" % (u["stores"], "" if u["stores"] == 1 else "s"))
        if u["accounts"]:
            parts.append("%d account%s" % (u["accounts"], "" if u["accounts"] == 1 else "s"))
        raise CityError("city_in_use", "In use by %s. Deactivate it instead." % " and ".join(parts), 409)
    db.cities.delete_one({"_id": cur["_id"]})
    if cur.get("country"):
        renumber_popular(cur["country"])
