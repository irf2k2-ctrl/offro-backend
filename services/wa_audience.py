"""
services/wa_audience.py
-----------------------
Audience management for the WhatsApp Campaign Manager (Phase 2B). Builds and previews WHO could receive a campaign.
NOTHING here sends a message, calls Meta, or starts any processing.

Sources
    merchants : `accounts` (roles contains "merchant") joined to `stores` by stores.merchant_id == str(accounts._id).
                A merchant qualifies when the account is active (status missing or "active") AND it has at least one
                store with status "active". Merchants with several stores are ONE recipient.
    custom    : phone numbers typed in or uploaded as CSV (stored as an "import", see below).

Consent: every recipient goes through services.wa_eligibility (Phase 2A) - the Phase 1 consent store is the only source
of truth. A "consent" column in an upload is NEVER read as consent (it is reported as an ignored column).

Storage lifecycle of imported audience data (collection `wa_audience_imports`):
    * The uploaded file itself is NEVER stored; only the parsed result.
    * Valid rows are kept as {row, canonical phone, sanitized name} - the minimum a later campaign needs.
    * Rejected / duplicate rows keep ONLY the row number and a reason code - the raw cell value is discarded.
    * Consent / eligibility is NOT stored on the import; it is recomputed at every preview (consent changes).
    * No automatic expiry exists yet (retention is pending approval). An admin with the "delete" permission can
      delete an import at any time; deletion is audited.
"""
import csv
import io
import logging
import re
from datetime import datetime

from bson import ObjectId
from bson.errors import InvalidId

from services import wa_consent as C
from services import wa_eligibility as E
from services.wa_webhook_security import exc_summary, mask_phone

logger = logging.getLogger("offro.wa_campaigns.audience")

db = None  # set lazily to database.db (kept patchable for tests)
IMPORTS = "wa_audience_imports"

MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 20000
MAX_MANUAL_ENTRIES = 1000
MAX_AUDIENCE = 20000
MAX_NAME_LEN = 100

# reason codes for rows that cannot become recipients
R_EMPTY = "empty_phone"
R_SCI = "scientific_notation"
R_CHARS = "invalid_characters"
R_SHORT = "too_short"
R_LONG = "too_long"
R_IN_FORMAT = "invalid_indian_mobile"
R_FORMAT = "invalid_format"
R_NO_PLUS = "international_needs_plus"
REASON_TEXT = {
    R_EMPTY: "No phone number in this row.",
    R_SCI: "Number was converted to scientific notation (e.g. 9.19E+11) by a spreadsheet; re-export it as text.",
    R_CHARS: "Contains characters other than digits, spaces, '-', '(', ')', '.' or a leading '+'.",
    R_SHORT: "Too few digits for an international number.",
    R_LONG: "Too many digits for an international number (maximum 15).",
    R_IN_FORMAT: "Looks Indian (+91 / 91 / 0 prefix) but is not a valid 10-digit mobile starting 6-9.",
    R_FORMAT: "Not a valid phone number.",
    R_NO_PLUS: "Not an Indian mobile and has no leading '+'. International numbers must be written with '+' and the country code.",
}

PHONE_HEADERS = {"phone", "phone_number", "phonenumber", "mobile", "mobile_number", "mobilenumber", "mobile_no",
                 "whatsapp", "whatsapp_number", "number", "contact", "contact_number", "msisdn"}
NAME_HEADERS = {"name", "full_name", "fullname", "contact_name", "customer_name", "first_name"}
CONSENT_HEADER_HINTS = ("consent", "optin", "opt_in", "opt-in", "subscribed", "permission", "gdpr", "marketing")

# ── helpers ────────────────────────────────────────────────────────────────────


def _db():
    global db
    if db is None:
        from database import db as _d
        db = _d
    return db


class AudienceError(Exception):
    """A request problem the caller can fix (maps to HTTP 400)."""
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def explain_invalid(raw) -> str:
    """Why `raw` is not a valid number (a reason code from REASON_TEXT). Only call when normalize_phone() == ''."""
    s = str(raw if raw is not None else "").strip()
    if not s:
        return R_EMPTY
    if re.search(r"\d\.?\d*[eE][+-]?\d+", s):
        return R_SCI
    d, _plus = C._split(s)
    if d is None:
        return R_CHARS
    d2 = d[2:] if d.startswith("00") else d
    if d2.startswith("91"):
        return R_IN_FORMAT
    if len(d2) < 8:
        return R_SHORT
    if len(d2) > 15:
        return R_LONG
    return R_FORMAT


def clean_name(value) -> str:
    """Display name safe to show and to re-export: no control chars, no spreadsheet-formula prefix, bounded."""
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    s = re.sub(r"\s+", " ", s).strip()
    s = s.lstrip("=+-@").strip()
    return s[:MAX_NAME_LEN]


def _norm_header(h) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(h or "").strip().lower()).strip("_")


def _phone_cell(raw) -> str:
    """Tolerate a spreadsheet's trailing '.0' ('919876543210.0'); leave everything else to normalize_phone."""
    s = str(raw or "").strip()
    return re.sub(r"\.0+$", "", s) if re.fullmatch(r"\+?\d+\.0+", s) else s


def _explicit_international(cell) -> bool:
    d, plus = C._split(cell)
    return bool(plus or (d and d.startswith("00")))


def normalize_entries(entries) -> dict:
    """entries: iterable of (row_number, raw_phone, raw_name).
    -> {"recipients": [{row, phone, name}], "rejected": [{row, reason}], "duplicates": [{row, of_row}]}
    First occurrence wins; the same number written differently is still a duplicate."""
    recipients, rejected, duplicates, seen = [], [], [], {}
    for row, raw_phone, raw_name in entries:
        cell = _phone_cell(raw_phone)
        canonical = C.normalize_phone(cell) if cell else ""
        if not canonical:
            rejected.append({"row": row, "reason": explain_invalid(cell)})
            continue
        if not canonical.startswith("91") and not _explicit_international(cell):
            # a bare number that is not an Indian mobile is ambiguous (typo'd Indian number or foreign number?):
            # require the '+' so nobody gets a message because of a guess
            rejected.append({"row": row, "reason": R_NO_PLUS})
            continue
        if canonical in seen:
            duplicates.append({"row": row, "of_row": seen[canonical]})
            continue
        seen[canonical] = row
        recipients.append({"row": row, "phone": canonical, "name": clean_name(raw_name)})
    return {"recipients": recipients, "rejected": rejected, "duplicates": duplicates}


# ── CSV ────────────────────────────────────────────────────────────────────────

def parse_csv(data: bytes) -> dict:
    """Parse an uploaded CSV. -> {"entries": [(row, phone, name)], "ignored_columns": [...], "headerless": bool}
    Raises AudienceError for a file that cannot be used at all. Consent-like columns are NEVER used."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise AudienceError("empty_file", "The file is empty.")
    if len(data) > MAX_CSV_BYTES:
        raise AudienceError("file_too_large", "The file is larger than %d MB." % (MAX_CSV_BYTES // (1024 * 1024)))
    if b"\x00" in data[:4096]:
        raise AudienceError("not_csv", "This looks like a binary file (e.g. Excel). Only CSV is supported - use Save as > CSV.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AudienceError("bad_encoding", "The file is not UTF-8. Save it as 'CSV UTF-8'.")
    sample = text[:4096]
    delimiter = ","
    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    counts = {d: first_line.count(d) for d in (",", ";", "\t")}
    if max(counts.values()) > 0:
        delimiter = max(counts, key=counts.get)
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    except csv.Error as e:
        raise AudienceError("malformed_csv", "The CSV could not be read (%s)." % type(e).__name__)
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        raise AudienceError("empty_file", "The file has no rows.")
    header = [_norm_header(c) for c in rows[0]]
    phone_idx = next((i for i, h in enumerate(header) if h in PHONE_HEADERS), None)
    name_idx = next((i for i, h in enumerate(header) if h in NAME_HEADERS), None)
    headerless = False
    if phone_idx is None:
        # no recognisable header: accept only if the first cell of the first row looks like a number
        first = _phone_cell(rows[0][0]) if rows[0] else ""
        if first and (C.normalize_phone(first) or re.fullmatch(r"\+?[\d\s\-().]{6,}", first)):
            headerless, phone_idx = True, 0
            name_idx = 1 if len(rows[0]) > 1 else None
        else:
            raise AudienceError("no_phone_column",
                                "No phone column found. Use a header such as 'phone' or 'mobile' (optional 'name').")
    ignored = []
    if not headerless:
        ignored = [h for i, h in enumerate(header)
                   if i not in (phone_idx, name_idx) and h and any(k in h for k in CONSENT_HEADER_HINTS)]
    body = rows if headerless else rows[1:]
    if len(body) > MAX_IMPORT_ROWS:
        raise AudienceError("too_many_rows", "More than %d rows. Split the file." % MAX_IMPORT_ROWS)
    base = 1 if headerless else 2                      # row numbers match what the admin sees in a spreadsheet
    entries = []
    for i, r in enumerate(body):
        phone = r[phone_idx] if phone_idx < len(r) else ""
        name = r[name_idx] if name_idx is not None and name_idx < len(r) else ""
        entries.append((base + i, phone, name))
    return {"entries": entries, "ignored_columns": ignored, "headerless": headerless}


# ── merchants ──────────────────────────────────────────────────────────────────

R_ACCOUNT_INACTIVE = "merchant_inactive"
R_NO_ACTIVE_STORE = "merchant_no_active_store"
R_NOT_FOUND = "merchant_not_found"
R_OUT_OF_SCOPE = "merchant_out_of_scope"
R_MERCHANT_BAD_PHONE = "invalid_number"


def _rx(value):
    return {"$regex": "^" + re.escape(str(value).strip()) + "$", "$options": "i"}


def _scope_cities(admin):
    """None = unrestricted; list = the cities this admin may see (empty list = none), mirroring the dashboard's
    assigned-city rule (Super Admin or '*' = all)."""
    admin = admin or {}
    role = admin.get("role")
    role_name = role.get("role_name", "") if isinstance(role, dict) else ""
    cities = admin.get("assigned_cities", []) or []
    if role_name == "Super Admin" or "*" in cities:
        return None
    return [str(c) for c in cities]


def _in_scope(store_cities, scope):
    if scope is None:
        return True
    low = {c.strip().casefold() for c in scope}
    return any(str(c or "").strip().casefold() in low for c in store_cities)


def _account_active(acct) -> bool:
    return str(acct.get("status") or "active").strip().lower() == "active"


def _store_view(s):
    return {"store_id": str(s["_id"]), "store_name": str(s.get("store_name") or ""),
            "city": str(s.get("city") or ""), "area": str(s.get("area") or ""),
            "state": str(s.get("state") or ""), "category": str(s.get("category") or "")}


def filter_options(admin=None) -> dict:
    """Distinct City / Area / State values from ACTIVE stores of merchant accounts (admin's city scope applied)."""
    scope = _scope_cities(admin)
    cities, areas, states = set(), set(), set()
    q = {"status": "active"}
    for s in _db().stores.find(q, {"city": 1, "area": 1, "state": 1}):
        if scope is not None and not _in_scope([s.get("city")], scope):
            continue
        for bucket, key in ((cities, "city"), (areas, "area"), (states, "state")):
            v = str(s.get(key) or "").strip()
            if v:
                bucket.add(v)
    return {"cities": sorted(cities, key=str.casefold), "areas": sorted(areas, key=str.casefold),
            "states": sorted(states, key=str.casefold)}


def _object_ids(ids):
    out = []
    for i in ids:
        try:
            out.append(ObjectId(str(i)))
        except (InvalidId, TypeError):
            pass
    return out


def _merchant_phone_index() -> dict:
    """canonical phone -> [account docs (id, name, status)] for ALL merchant accounts. Used for matching custom
    numbers to registered merchants whatever format the account stored the number in."""
    idx = {}
    for a in _db().accounts.find({"roles": "merchant"}, {"name": 1, "phone": 1, "status": 1}):
        c = C.normalize_phone(a.get("phone"))
        if c:
            idx.setdefault(c, []).append(a)
    return idx


def resolve_merchants(ids=None, flt=None, admin=None) -> list:
    """Merchant recipients. -> list of dicts, one per merchant account (never per store):
       {merchant_id, name, phone (raw account phone), stores:[...], qualifies:bool, reason:''|code}
    ids : explicit account ids (those that do not qualify are returned with qualifies False and a reason).
    flt : {q, city, area, state} - every qualifying merchant that matches (None = not requested)."""
    scope = _scope_cities(admin)
    result = {}                                           # merchant_id -> entry (ordered by insertion)

    def store_query(extra=None):
        q = {"status": "active"}
        if flt:
            for key in ("city", "area", "state"):
                if str(flt.get(key) or "").strip():
                    q[key] = _rx(flt[key])
        if extra:
            q.update(extra)
        return q

    def build(acct, stores):
        mid = str(acct["_id"])
        views = [_store_view(s) for s in stores]
        entry = {"merchant_id": mid, "name": str(acct.get("name") or ""), "phone": acct.get("phone"),
                 "stores": views, "qualifies": True, "reason": ""}
        if not _account_active(acct):
            entry.update(qualifies=False, reason=R_ACCOUNT_INACTIVE)
        elif not stores:
            entry.update(qualifies=False, reason=R_NO_ACTIVE_STORE)
        elif not _in_scope([s["city"] for s in views], scope):
            entry.update(qualifies=False, reason=R_OUT_OF_SCOPE)
        elif not C.normalize_phone(acct.get("phone")):
            entry.update(qualifies=False, reason=R_MERCHANT_BAD_PHONE)
        return entry

    # explicit selection
    wanted = [str(i) for i in (ids or [])]
    if wanted:
        oids = _object_ids(wanted)
        accts = {str(a["_id"]): a for a in _db().accounts.find({"_id": {"$in": oids}, "roles": "merchant"})}
        stores_by = {}
        for s in _db().stores.find({"merchant_id": {"$in": list(accts)}, "status": "active"}):
            stores_by.setdefault(str(s.get("merchant_id")), []).append(s)
        for mid in wanted:
            if mid in result:
                continue
            if mid not in accts:
                result[mid] = {"merchant_id": mid, "name": "", "phone": None, "stores": [], "qualifies": False,
                               "reason": R_NOT_FOUND}
            else:
                result[mid] = build(accts[mid], stores_by.get(mid, []))

    # filter / group selection
    if flt is not None:
        by_merchant = {}
        for s in _db().stores.find(store_query()):
            by_merchant.setdefault(str(s.get("merchant_id") or ""), []).append(s)
        by_merchant.pop("", None)
        q_text = str(flt.get("q") or "").strip().casefold()
        q_digits = re.sub(r"\D", "", q_text)
        mids = list(by_merchant)
        accts = {}
        for i in range(0, len(mids), 500):
            for a in _db().accounts.find({"_id": {"$in": _object_ids(mids[i:i + 500])}, "roles": "merchant"}):
                accts[str(a["_id"])] = a
        for mid, a in accts.items():
            stores = by_merchant[mid]
            if q_text:
                hay = [str(a.get("name") or "").casefold()] + [str(s.get("store_name") or "").casefold() for s in stores]
                phone_digits = re.sub(r"\D", "", str(a.get("phone") or ""))
                hit = any(q_text in h for h in hay) or (len(q_digits) >= 3 and q_digits in phone_digits)
                if not hit:
                    continue
            if mid in result:
                continue
            e = build(a, stores)
            if e["qualifies"]:                                    # a group never lists non-qualifying merchants
                result[mid] = e
    return list(result.values())


def list_merchants(flt=None, page=1, page_size=50, admin=None) -> dict:
    """Merchant audience retrieval (qualifying merchants only), paged, with masked phone numbers."""
    page_size = max(1, min(int(page_size or 50), 100))
    found = [m for m in resolve_merchants(flt=flt or {}, admin=admin) if m["qualifies"]]
    found.sort(key=lambda m: (m["name"].casefold(), m["merchant_id"]))
    total = len(found)
    pages = max(1, -(-total // page_size))
    page = min(max(1, int(page or 1)), pages)
    chunk = found[(page - 1) * page_size: page * page_size]
    elig = E.check_many([m["phone"] for m in chunk])
    items = []
    for m, e in zip(chunk, elig):
        items.append({"merchant_id": m["merchant_id"], "name": m["name"], "phone_masked": mask_phone(m["phone"]),
                      "store_count": len(m["stores"]), "stores": m["stores"][:5],
                      "eligibility": _elig_view(e)})
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages,
            "universe": {"merchant_accounts": _db().accounts.count_documents({"roles": "merchant"})}}


def _elig_view(e) -> dict:
    return {"eligible": e.eligible, "reason": e.reason, "consent_source": e.basis or "none",
            "opted_out": e.reason == E.OPTED_OUT}


# ── imports (stored custom audience) ───────────────────────────────────────────

def create_import(kind, entries, admin, filename="", ignored_columns=None, headerless=False) -> dict:
    """Validate + store a custom audience. `entries` = [(row, phone, name)]. The raw input is not stored."""
    parsed = normalize_entries(entries)
    doc = {"kind": kind, "created_at": datetime.utcnow(), "created_by": admin,
           "filename": re.sub(r"[^\w.\- ]", "_", str(filename or ""))[:100],
           "ignored_columns": list(ignored_columns or []), "headerless": bool(headerless),
           "counts": {"rows": len(entries), "valid": len(parsed["recipients"]),
                      "invalid": len(parsed["rejected"]), "duplicates": len(parsed["duplicates"])},
           "recipients": parsed["recipients"], "rejected": parsed["rejected"], "duplicates": parsed["duplicates"]}
    res = _db()[IMPORTS].insert_one(doc)
    if not res.acknowledged:
        raise RuntimeError("import not acknowledged")
    doc["_id"] = res.inserted_id
    return doc


def import_summary(doc, merchant_idx=None) -> dict:
    merchant_idx = merchant_idx if merchant_idx is not None else _merchant_phone_index()
    registered = sum(1 for r in doc.get("recipients", []) if r["phone"] in merchant_idx)
    return {"import_id": str(doc["_id"]), "kind": doc.get("kind"), "filename": doc.get("filename", ""),
            "created_at": doc["created_at"].isoformat() + "Z" if isinstance(doc.get("created_at"), datetime) else "",
            "created_by": (doc.get("created_by") or {}).get("name", ""),
            "counts": {**doc.get("counts", {}), "registered_merchants": registered},
            "ignored_columns": doc.get("ignored_columns", []),
            "warnings": (["consent_columns_ignored"] if doc.get("ignored_columns") else [])}


def rejected_rows(doc, limit=200) -> list:
    out = [{"row": r["row"], "problem": "invalid", "reason": r["reason"], "message": REASON_TEXT.get(r["reason"], r["reason"])}
           for r in doc.get("rejected", [])]
    out += [{"row": r["row"], "problem": "duplicate", "reason": "duplicate_in_upload",
             "message": "Same number as row %s." % r["of_row"]} for r in doc.get("duplicates", [])]
    out.sort(key=lambda x: x["row"])
    return out[:limit]


def get_import(import_id):
    try:
        oid = ObjectId(str(import_id))
    except (InvalidId, TypeError):
        return None
    return _db()[IMPORTS].find_one({"_id": oid})


# ── preview ────────────────────────────────────────────────────────────────────

DUP_NONE = "unique"
DUP_IN_UPLOAD = "duplicate_in_upload"
DUP_MERCHANT = "duplicate_of_merchant"
DUP_REPEAT = "duplicate_recipient"


def build_preview(merchant_sel=None, import_ids=None, admin=None, page=1, page_size=50, status="all") -> dict:
    """Combine merchants + imports into ONE recipient list (deduped by canonical number), check eligibility for every
    recipient, and return counts plus a page of masked rows. Stateless: nothing is stored or sent."""
    merchant_sel = merchant_sel if isinstance(merchant_sel, dict) else None
    import_ids = [str(i) for i in (import_ids or [])]
    if not merchant_sel and not import_ids:
        raise AudienceError("empty_selection", "Select merchants and/or imports.")

    rows, seen = [], {}                                   # seen: canonical -> index in rows
    merchants = []
    if merchant_sel:
        flt = merchant_sel.get("filter")
        if flt is not None and not isinstance(flt, dict):
            raise AudienceError("invalid_filter", "merchants.filter must be an object.")
        ids = merchant_sel.get("ids") or []
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise AudienceError("invalid_ids", "merchants.ids must be a list of strings.")
        merchants = resolve_merchants(ids=ids, flt=flt, admin=admin)
    merchant_canon = {}
    for m in merchants:
        row = {"source": "merchant", "ref": m["merchant_id"], "name": m["name"], "phone_raw": m["phone"],
               "canonical": C.normalize_phone(m["phone"]), "forced_reason": "" if m["qualifies"] else m["reason"],
               "duplicate_status": DUP_NONE, "store_count": len(m["stores"]), "matched_merchant": None}
        if m["qualifies"] and row["canonical"]:
            if row["canonical"] in seen:
                row["duplicate_status"] = DUP_REPEAT
            else:
                seen[row["canonical"]] = len(rows)
                merchant_canon[row["canonical"]] = m["merchant_id"]
        rows.append(row)

    index = _merchant_phone_index() if import_ids else {}
    docs = []
    for iid in dict.fromkeys(import_ids):
        d = get_import(iid)
        if d is None:
            raise AudienceError("import_not_found", "Import %s was not found." % iid[:24])
        docs.append(d)
    for d in docs:
        for r in d.get("recipients", []):
            c = r["phone"]
            match = index.get(c)
            row = {"source": "custom", "ref": "%s#%s" % (str(d["_id"]), r["row"]), "name": r.get("name", ""),
                   "phone_raw": c, "canonical": c, "forced_reason": "", "duplicate_status": DUP_NONE, "store_count": 0,
                   "matched_merchant": ({"merchant_id": str(match[0]["_id"]), "name": str(match[0].get("name") or "")}
                                        if match else None)}
            if c in merchant_canon:
                row["duplicate_status"] = DUP_MERCHANT
            elif c in seen:
                row["duplicate_status"] = DUP_IN_UPLOAD
            else:
                seen[c] = len(rows)
            rows.append(row)
        # rows the import itself rejected are counted, never become recipients
    if len(rows) > MAX_AUDIENCE:
        raise AudienceError("audience_too_large", "The audience exceeds %d recipients. Narrow the selection." % MAX_AUDIENCE)

    to_check = [r for r in rows if not r["forced_reason"] and r["duplicate_status"] == DUP_NONE]
    results = E.check_many([r["phone_raw"] for r in to_check])
    elig_of = {id(r): e for r, e in zip(to_check, results)}

    out, by_reason = [], {}
    totals = {"merchant": {"total": 0, "eligible": 0}, "custom": {"total": 0, "eligible": 0}}
    lookup_failed = False
    for r in rows:
        e = elig_of.get(id(r))
        if r["forced_reason"]:
            eligible, reason, basis, opted_out = False, r["forced_reason"], "none", False
        elif r["duplicate_status"] != DUP_NONE:
            eligible, reason, basis, opted_out = False, "duplicate", "none", False
        else:
            eligible, reason, basis, opted_out = e.eligible, e.reason, e.basis or "none", e.reason == E.OPTED_OUT
            lookup_failed = lookup_failed or reason == E.LOOKUP_FAILED
        invalid = reason in (E.INVALID_NUMBER, R_MERCHANT_BAD_PHONE)
        t = totals[r["source"]]
        t["total"] += 1
        t["eligible"] += 1 if eligible else 0
        if not eligible:
            by_reason[reason] = by_reason.get(reason, 0) + 1
        out.append({"source": r["source"], "ref": r["ref"], "name": r["name"],
                    "phone_masked": mask_phone(r["phone_raw"]), "eligible": eligible,
                    "exclusion_reason": "" if eligible else reason, "consent_source": basis, "opted_out": opted_out,
                    "duplicate_status": r["duplicate_status"], "invalid_phone": invalid,
                    "registered_merchant": r["matched_merchant"], "store_count": r["store_count"]})
    rejected_in_imports = sum(d.get("counts", {}).get("invalid", 0) for d in docs)
    dup_in_imports = sum(d.get("counts", {}).get("duplicates", 0) for d in docs)
    eligible_total = sum(1 for o in out if o["eligible"])
    summary = {"total": len(out), "eligible": eligible_total, "excluded": len(out) - eligible_total,
               "by_reason": by_reason, "by_source": totals,
               "rejected_at_import": rejected_in_imports, "duplicates_at_import": dup_in_imports,
               "consent_check_ok": not lookup_failed,
               "note": "Eligibility is re-checked immediately before every send; this is a preview."}
    if status == "eligible":
        out = [o for o in out if o["eligible"]]
    elif status == "excluded":
        out = [o for o in out if not o["eligible"]]
    page_size = max(1, min(int(page_size or 50), 200))
    pages = max(1, -(-len(out) // page_size))
    page = min(max(1, int(page or 1)), pages)
    return {"summary": summary, "recipients": out[(page - 1) * page_size: page * page_size],
            "page": page, "page_size": page_size, "pages": pages, "filtered_total": len(out)}
