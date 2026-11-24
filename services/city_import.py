"""services/city_import.py - admin-triggered city import (preview / apply / undo) and the India verification report.

Safety rules (design section 6.5 / 7):
  * INSERT ONLY. An import never updates, renames, re-images, re-orders, deactivates or deletes an existing city. The one optional
    exception is `fill_empty_state` (off by default, shown in the preview): it only fills a MISSING state on a matching existing city.
  * Preview writes nothing. Apply re-runs exactly the same checks against the current data (it never trusts the preview).
  * The country always comes from the selector, never from the file.
  * Every apply gets a batch id; undo removes only that batch's cities that nothing uses.
  * Nothing runs at import time or on startup.

`active` for imported cities: a reference list may say so (`default_active`). The India reference list defaults to INACTIVE: an active
city in the City Master means "OffrO serves it" (service area, the app's "City not supported yet" check and India's legacy Popular row),
and the 244 bundled cities are a pick list, not a service area. An admin activates the ones OffrO serves.
"""
import csv
import io
import json
import os
import re
import uuid
from datetime import datetime
from typing import Optional

from services import city_master as M

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "city_reference")
MAX_ROWS = 2000
MAX_BYTES = 1024 * 1024
REFERENCES = {"reference:in-v1": "in.json", "reference:sa-v1": "sa.json"}
CSV_COLUMNS = ("name", "name_ar", "state", "sort_order", "is_popular", "popular_order", "active")


def reference_sources() -> list:
    out = []
    for key, fn in REFERENCES.items():
        try:
            d = load_reference(key)
            out.append({"source": key, "country": d["country"], "count": len(d["cities"]), "default_active": d["default_active"], "note": d.get("note", "")})
        except Exception:
            continue
    return out


def load_reference(source: str) -> dict:
    fn = REFERENCES.get(source)
    if not fn:
        raise M.CityError("source_unknown", "Unknown reference list", 400, "source")
    with open(os.path.join(_DIR, fn), encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("default_active", True)
    return d


def parse_csv(text: str) -> list:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise M.CityError("file_too_large", "File is larger than 1 MB", 400)
    rdr = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    heads = [h.strip().lower() for h in (rdr.fieldnames or [])]
    if "name" not in heads:
        raise M.CityError("header_missing", "The first row must be a header with at least a 'name' column", 400)
    if "country" in heads:
        # a file may not choose the country: a mismatch is refused later in rows_from_body
        pass
    rows = []
    for r in rdr:
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in r.items() if k})
    return rows


def rows_from_body(body: dict, iso: str):
    """(rows, default_active, source label) from {source|csv|rows}. Raises CityError for a wrong shape or a country mismatch."""
    src = str((body or {}).get("source") or "")
    default_active = True
    if src.startswith("reference:"):
        d = load_reference(src)
        if d["country"] != iso:
            raise M.CityError("country_mismatch", "This list is for %s, not %s" % (M.country_name(d["country"]), M.country_name(iso)), 400, "country")
        rows, default_active, label = d["cities"], bool(d.get("default_active", True)), "import:" + d["version"]
    elif isinstance(body.get("rows"), list):
        rows, label = body["rows"], "import:json"
    elif isinstance(body.get("csv"), str):
        rows, label = parse_csv(body["csv"]), "import:csv"
    else:
        raise M.CityError("source_required", "Choose a reference list, upload a CSV or paste JSON rows", 400, "source")
    if len(rows) > MAX_ROWS:
        raise M.CityError("file_too_large", "More than %d rows" % MAX_ROWS, 400)
    for r in rows:
        if isinstance(r, dict) and str(r.get("country") or "").strip() and M.require_country(r.get("country")) != iso:
            raise M.CityError("country_mismatch", "The file contains a city for another country", 400, "country")
    return rows, default_active, label


def _row_result(i, raw, result, **kw):
    d = {"row": i, "name": str((raw or {}).get("name", "") if isinstance(raw, dict) else raw), "name_ar": str((raw or {}).get("name_ar", "") if isinstance(raw, dict) else ""),
         "state": str((raw or {}).get("state", "") if isinstance(raw, dict) else ""), "result": result}
    d.update(kw)
    return d


def plan(iso: str, body: dict) -> dict:
    """Dry run. Returns the per-row outcome and the valid new documents under '_docs' (internal; stripped by the router)."""
    rows, default_active, label = rows_from_body(body, iso)
    opts = body or {}
    fill_state = M.to_bool(opts.get("fill_empty_state"), False)
    active_default = M.to_bool(opts.get("active"), default_active) if opts.get("active") not in (None, "") else default_active
    seen, out, docs, fills = set(), [], [], []
    order_seen = set()
    for i, raw in enumerate(rows, 1):
        if not isinstance(raw, dict):
            out.append(_row_result(i, {}, "error", field="row", code="row_invalid", message="Row is not an object"))
            continue
        r = dict(raw)
        r.pop("country", None)
        r.setdefault("active", active_default)
        if r.get("active") in (None, ""):
            r["active"] = active_default
        try:
            doc = M.build_new(r, source=label, iso=iso)
            key = (doc["name_key"], doc["state"].lower())
            if key in seen:
                raise M.CityError("duplicate_in_file", "This city appears twice in the file", field="name")
            if doc["is_popular"] and doc.get("popular_order"):
                if doc["popular_order"] in order_seen:
                    raise M.CityError("popular_order_invalid", "Popular order %d is used twice in the file" % doc["popular_order"], field="popular_order")
                order_seen.add(doc["popular_order"])
        except M.CityError as e:
            out.append(_row_result(i, r, "error", field=e.field, code=e.code, message=e.message))
            continue
        seen.add(key)
        clash = M.find_conflict(iso, doc["name"], doc["state"])
        if clash:
            res = _row_result(i, r, "exists", existing_id=str(clash["_id"]))
            if fill_state and doc["state"] and not str(clash.get("state") or "").strip() and clash.get("country") == iso:
                res["result"] = "exists_fill_state"
                fills.append((clash["_id"], doc["state"]))
            out.append(res)
            continue
        out.append(_row_result(i, r, "new", active=doc["active"], popular=bool(doc["is_popular"])))
        docs.append((len(out) - 1, doc))
    counts = {"read": len(rows), "new": sum(1 for r in out if r["result"] == "new"),
              "exists": sum(1 for r in out if r["result"] in ("exists", "exists_fill_state")),
              "state_filled": len(fills), "errors": sum(1 for r in out if r["result"] == "error")}
    return {"country": iso, "source": label, "counts": counts, "rows": out, "active_default": active_default, "_docs": docs, "_fills": fills}


def preview(iso: str, body: dict) -> dict:
    p = plan(iso, body)
    return {"country": iso, "source": p["source"], "active_default": p["active_default"], **p["counts"], "rows": p["rows"], "written": 0}


def apply(iso: str, body: dict, who: str = "") -> dict:
    if not M.to_bool((body or {}).get("confirm"), False):
        raise M.CityError("confirm_required", "Confirm the import after reviewing the preview", 400, "confirm")
    p = plan(iso, body)                                   # re-checked now, against the current data
    batch = "imp-%s-%s-%s" % (datetime.utcnow().strftime("%Y%m%d%H%M%S"), iso.lower(), uuid.uuid4().hex[:4])
    max_sort = max([int(d.get("sort_order") or 0) for d in M.db.cities.find({"country": iso})] + [0])
    existing_pop = max([int(d.get("popular_order") or 0) for d in M.db.cities.find({"country": iso, "is_popular": True})] + [0])
    docs = [d for _i, d in p["_docs"]]
    pop_sorted = sorted([d for d in docs if d["is_popular"]], key=lambda d: (d.get("popular_order") or 10 ** 6))
    nxt = existing_pop
    for d in pop_sorted:
        if existing_pop == 0 and d.get("popular_order"):
            continue                                       # empty country: keep the file's own order
        nxt += 1
        d["popular_order"] = nxt
    added = 0
    for d in docs:
        if not (body or {}).get("keep_sort_order") and not d.get("sort_order"):
            max_sort += 1
            d["sort_order"] = max_sort
        d["import_batch"] = batch
        d["updated_by"], d["updated_at"] = who, datetime.utcnow().isoformat()
        M.db.cities.insert_one(d)
        added += 1
    filled = 0
    for cid, st in p["_fills"]:
        M.db.cities.update_one({"_id": cid}, {"$set": {"state": st}})
        filled += 1
    M.renumber_popular(iso)
    c = p["counts"]
    return {"batch": batch, "added": added, "skipped": c["exists"], "errors": c["errors"], "state_filled": filled, "country": iso}


def undo(batch: str, scope: str = "") -> dict:
    """Remove only the cities this batch added AND that nothing uses. Older cities are never touched."""
    if not re.match(r"^imp-[A-Za-z0-9\-]{6,60}$", str(batch or "")):
        raise M.CityError("batch_invalid", "Invalid import batch", 400)
    removed, kept = 0, []
    countries = set()
    for d in list(M.db.cities.find({"import_batch": batch})):
        if scope and d.get("country") != scope:
            kept.append(d.get("name", ""))
            continue
        u = M.used_by(d)
        if u["stores"] or u["accounts"]:
            kept.append(d.get("name", ""))
            continue
        M.db.cities.delete_one({"_id": d["_id"]})
        countries.add(d.get("country"))
        removed += 1
    for c in countries:
        if c:
            M.renumber_popular(c)
    return {"batch": batch, "removed": removed, "kept_in_use": kept}


# ── verification (read only) ───────────────────────────────────────────────────────────────────────────
def verify(iso: str) -> dict:
    """Read-only report for a country, against its reference list when one exists (India)."""
    unassigned = [M.row(d) for d in M.db.cities.find({"$or": [{"country": {"$exists": False}}, {"country": ""}, {"country": None}]})]
    mine = [M.row(d) for d in M.db.cities.find({"country": iso})]
    ref_src = {"IN": "reference:in-v1", "SA": "reference:sa-v1"}.get(iso)
    missing, extra = [], []
    if ref_src:
        ref = load_reference(ref_src)["cities"]
        have = {(r["name"].strip().lower(), r["state"].strip().lower()) for r in mine}
        have_names = {r["name"].strip().lower() for r in mine if not r["state"]}
        ref_keys = set()
        for c in ref:
            k = (c["name"].strip().lower(), str(c.get("state", "")).strip().lower())
            ref_keys.add(k)
            if k not in have and c["name"].strip().lower() not in have_names:
                missing.append({"name": c["name"], "state": c.get("state", "")})
        ref_names = {k[0] for k in ref_keys}
        extra = [{"name": r["name"], "state": r["state"]} for r in mine if r["name"].strip().lower() not in ref_names]
    dupes, same_name = [], {}
    for r in mine:
        same_name.setdefault(r["name"].strip().lower(), []).append(r)
    for nm, lst in same_name.items():
        if len(lst) > 1:
            states = [x["state"].strip().lower() for x in lst]
            (dupes if len(set(states)) < len(states) or "" in states else []).append({"name": lst[0]["name"], "ids": [x["id"] for x in lst]})
    unas_match = [r for r in unassigned if not ref_src or r["name"].strip().lower() in {c["name"].strip().lower() for c in load_reference(ref_src)["cities"]}]
    return {"country": iso, "reference": ref_src or "", "in_master": len(mine), "active": sum(1 for r in mine if r["status"] == "active"),
            "unassigned": [{"id": r["id"], "name": r["name"], "state": r["state"]} for r in unassigned],
            "unassigned_count": len(unassigned), "unassigned_matching_reference": len(unas_match),
            "missing": missing, "missing_count": len(missing), "not_in_reference": extra, "not_in_reference_count": len(extra),
            "possible_duplicates": dupes, "possible_duplicate_count": len(dupes),
            "popular_flagged": sum(1 for r in mine if r["is_popular"])}
