"""
services/wa_templates.py
------------------------
WhatsApp template management for the Campaign Manager (Phase 2C). NOTHING here sends a message.

    fetch_from_meta()      read-only GET of the WABA's message templates (paged, bounded, errors classified)
    parse_template(raw)    -> normalized structure: components, variables (with Meta's example values), support flags
    sync()                 fetch -> upsert into `wa_campaign_templates`, never touching the admin's campaign_use config
    render_preview(...)    fill a template with sample values (pure; no network)
    build_send_components  the exact Meta `components` payload for a template + values (pure; used by later phases)
    resolve_values(...)    turn a template's variable mapping + a recipient into values (pure; used by later phases)
    readiness(doc)         can this template be used by a campaign right now, and if not, why

Rules
  * Template CONFIGURATION (this module / collection) is separate from campaign EXECUTION (later phases).
  * Meta is the source of truth for status/category/content; the local cache is refreshed only by an explicit admin sync.
    A later send phase must re-verify status with Meta immediately before sending - the cache can be stale.
  * Only MARKETING templates with supported components can be enabled for campaigns. Editing a template at Meta changes
    its content hash, which invalidates the admin's variable mapping until it is saved again.
  * Credentials and Meta's free-text error messages are never returned or logged (error CODES only).
"""
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta

import requests
from pymongo.errors import DuplicateKeyError

from services import whatsapp as wa
from services.wa_webhook_security import exc_summary

logger = logging.getLogger("offro.wa_campaigns.templates")

db = None  # set lazily to database.db (patchable in tests)
TEMPLATES = "wa_campaign_templates"
SETTINGS = "wa_campaign_settings"
SYNC_STATE_ID = "template_sync"

SYNC_COOLDOWN = timedelta(seconds=30)
MAX_PAGES = 20
PAGE_LIMIT = 100
CAMPAIGN_CATEGORIES = ("MARKETING",)
FIELDS = "id,name,status,category,language,components,parameter_format,rejected_reason"

VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
MAX_PARAM_LEN = 1024
MAX_HEADER_PARAM_LEN = 60
MAX_BUTTON_PARAM_LEN = 2000

# variable sources an admin can map a variable to
SOURCES = {
    "static": "A fixed text (same for everyone)",
    "merchant_name": "Merchant / account name",
    "store_name": "Store name",
    "store_city": "Store city",
    "store_area": "Store area",
    "recipient_name": "Name from the custom-number upload",
}
SUPPORTED_BUTTONS = {"QUICK_REPLY", "URL", "PHONE_NUMBER"}
SUPPORTED_HEADERS = {"TEXT", "IMAGE", "VIDEO", "DOCUMENT"}
MEDIA_HEADERS = {"IMAGE", "VIDEO", "DOCUMENT"}


def _db():
    global db
    if db is None:
        from database import db as _d
        db = _d
    return db


# ── errors ─────────────────────────────────────────────────────────────────────

class TemplateSyncError(Exception):
    """A sync / Meta problem the admin can act on. `code` is stable; `http` is the API status to return."""
    MESSAGES = {
        "not_configured": "WhatsApp is not fully configured on this server (token, phone number id and business account id).",
        "meta_auth_failed": "Meta rejected the access token (expired or invalid). Update it in the server configuration.",
        "meta_permission_denied": "The access token has no permission to read templates for this WhatsApp Business Account.",
        "meta_rate_limited": "Meta is rate-limiting requests. Wait a few minutes and sync again.",
        "meta_unreachable": "Could not reach Meta (timeout or network error). Nothing was changed; try again.",
        "meta_error": "Meta returned an error. Nothing was changed.",
        "meta_bad_response": "Meta returned an unexpected response. Nothing was changed.",
        "too_many_templates": "More templates than the sync limit. Nothing was changed.",
        "sync_busy": "A sync ran very recently or is in progress. Wait a moment and try again.",
        "storage_failed": "The template cache could not be updated.",
        "template_not_found": "The template was not found at Meta (it may have been deleted).",
    }
    HTTP = {"not_configured": 503, "meta_auth_failed": 502, "meta_permission_denied": 502, "meta_rate_limited": 429,
            "meta_unreachable": 504, "meta_error": 502, "meta_bad_response": 502, "too_many_templates": 502,
            "sync_busy": 429, "storage_failed": 503, "template_not_found": 404}

    def __init__(self, code, meta_code=None):
        super().__init__(code)
        self.code = code
        self.meta_code = meta_code

    @property
    def message(self):
        return self.MESSAGES.get(self.code, self.code)

    @property
    def http(self):
        return self.HTTP.get(self.code, 502)


def _classify_meta_error(status, data):
    err = data.get("error") if isinstance(data, dict) else None
    code = err.get("code") if isinstance(err, dict) and isinstance(err.get("code"), int) else None
    if status == 401 or code == 190:
        return TemplateSyncError("meta_auth_failed", code)
    if status == 429 or code in (4, 17, 32, 613, 80008, 130429):
        return TemplateSyncError("meta_rate_limited", code)
    if status == 403 or code in (10, 200):
        return TemplateSyncError("meta_permission_denied", code)
    return TemplateSyncError("meta_error", code)


# ── Meta fetch ─────────────────────────────────────────────────────────────────

def _meta_get(params: dict) -> dict:
    """One read-only GET of the WABA's message_templates. Returns the decoded JSON object; raises TemplateSyncError."""
    if not wa.WHATSAPP_ACCESS_TOKEN or not wa.WHATSAPP_PHONE_NUMBER_ID or not wa.WHATSAPP_BUSINESS_ACCOUNT_ID:
        raise TemplateSyncError("not_configured")
    url = "%s/%s/message_templates" % (wa._GRAPH_BASE, wa.WHATSAPP_BUSINESS_ACCOUNT_ID)
    try:
        resp = requests.get(url, headers=wa._auth_headers(), params=params, timeout=(5, 15))
    except Exception as e:
        logger.error("[WA-Templates] Meta request failed (%s)", exc_summary(e))
        raise TemplateSyncError("meta_unreachable")
    try:
        data = resp.json()
    except Exception:
        data = None
    if resp.status_code != 200:
        err = _classify_meta_error(resp.status_code, data)
        logger.error("[WA-Templates] Meta returned http=%s code=%s", resp.status_code, err.meta_code)
        raise err
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise TemplateSyncError("meta_bad_response")
    return data


def fetch_from_meta() -> list:
    """All message templates of the configured WABA (read-only). Raises TemplateSyncError; never partial results."""
    out, after = [], None
    for _page in range(MAX_PAGES):
        params = {"fields": FIELDS, "limit": PAGE_LIMIT}
        if after:
            params["after"] = after                      # cursor only: Meta's `paging.next` URL is never followed
        data = _meta_get(params)
        out.extend(t for t in data["data"] if isinstance(t, dict))
        after = ((data.get("paging") or {}).get("cursors") or {}).get("after") if (data.get("paging") or {}).get("next") else None
        if not after:
            return out
    raise TemplateSyncError("too_many_templates")


def fetch_live_template(name: str, language: str) -> dict:
    """The CURRENT Meta definition of one template (read-only). Raises TemplateSyncError('template_not_found') if absent."""
    data = _meta_get({"name": name, "fields": FIELDS, "limit": 50})
    for t in data["data"]:
        if isinstance(t, dict) and t.get("name") == name and t.get("language") == language:
            return t
    raise TemplateSyncError("template_not_found")


def verify_live(doc: dict) -> list:
    """Re-check a cached template against Meta RIGHT NOW. Returns the list of problems ([] = still usable).
    Meta errors propagate as TemplateSyncError (callers must treat them as 'not verified' = do not send)."""
    raw = fetch_live_template(doc["name"], doc["language"])
    problems = []
    if str(raw.get("status") or "").upper() != "APPROVED":
        problems.append("live_status_not_approved")
    if str(raw.get("category") or "").upper() not in CAMPAIGN_CATEGORIES:
        problems.append("live_category_not_allowed")
    comps = raw.get("components") if isinstance(raw.get("components"), list) else []
    live_hash = content_hash(comps)
    cu = doc.get("campaign_use") or {}
    if live_hash != doc.get("content_hash") or live_hash != cu.get("mapped_hash"):
        problems.append("template_changed_since_sync")
    if not parse_template(raw)["campaign_supported"]:
        problems.append("live_unsupported_components")
    return problems


# ── parsing ────────────────────────────────────────────────────────────────────

def extract_variables(text) -> list:
    """Ordered, de-duplicated variable names found in `text` ('1', '2' or named)."""
    seen, out = set(), []
    for m in VAR_RE.finditer(str(text or "")):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def _examples(comp, kind, names, named):
    """{name: example} from Meta's `example` block (positional lists or named params)."""
    ex = comp.get("example") if isinstance(comp.get("example"), dict) else {}
    out = {}
    if named:
        for p in ex.get(kind + "_named_params", []) or []:
            if isinstance(p, dict) and p.get("param_name"):
                out[str(p["param_name"])] = str(p.get("example", ""))
        return out
    vals = ex.get(kind)
    if kind == "body_text" and isinstance(vals, list) and vals and isinstance(vals[0], list):
        vals = vals[0]
    for i, v in enumerate(vals or [], start=1):
        out[str(i)] = str(v)
    return out


def parse_template(raw: dict) -> dict:
    """Normalize one Meta template. Never raises for odd content: unsupported things are flagged, not fatal."""
    named = str(raw.get("parameter_format", "")).upper() == "NAMED"
    comps = raw.get("components") if isinstance(raw.get("components"), list) else []
    unsupported, variables = [], []
    parsed = {"header": None, "body": None, "footer": None, "buttons": []}

    for comp in comps:
        if not isinstance(comp, dict):
            continue
        ctype = str(comp.get("type", "")).upper()
        if ctype == "HEADER":
            fmt = str(comp.get("format", "")).upper()
            if fmt not in SUPPORTED_HEADERS:
                unsupported.append("header_format_" + (fmt.lower() or "unknown"))
            text = str(comp.get("text", "")) if fmt == "TEXT" else ""
            names = extract_variables(text)
            ex = _examples(comp, "header_text", names, named)
            parsed["header"] = {"format": fmt, "text": text, "variables": names, "requires_media": fmt in MEDIA_HEADERS}
            for n in names:
                variables.append({"key": "header." + n, "component": "header", "name": n, "example": ex.get(n, "")})
        elif ctype == "BODY":
            text = str(comp.get("text", ""))
            names = extract_variables(text)
            ex = _examples(comp, "body_text", names, named)
            parsed["body"] = {"text": text, "variables": names}
            for n in names:
                variables.append({"key": "body." + n, "component": "body", "name": n, "example": ex.get(n, "")})
        elif ctype == "FOOTER":
            parsed["footer"] = {"text": str(comp.get("text", ""))}
        elif ctype == "BUTTONS":
            for pos, b in enumerate(comp.get("buttons", []) or []):
                if not isinstance(b, dict):
                    continue
                btype = str(b.get("type", "")).upper()
                entry = {"position": pos, "type": btype, "text": str(b.get("text", "")), "url": "", "variables": []}
                if btype not in SUPPORTED_BUTTONS:
                    unsupported.append("button_" + (btype.lower() or "unknown"))
                if btype == "URL":
                    entry["url"] = str(b.get("url", ""))
                    entry["variables"] = extract_variables(entry["url"])
                    exs = b.get("example") if isinstance(b.get("example"), list) else []
                    prefix = entry["url"].split("{{", 1)[0]
                    sample = str(exs[0]) if exs else ""
                    if sample.startswith(prefix):                  # Meta's example is the FULL url; the variable is the tail
                        sample = sample[len(prefix):]
                    for n in entry["variables"]:
                        variables.append({"key": "button.%d" % pos, "component": "button", "name": n, "position": pos,
                                          "example": sample})
                if btype == "PHONE_NUMBER":
                    entry["phone_masked"] = "***" + re.sub(r"\D", "", str(b.get("phone_number", "")))[-4:]
                parsed["buttons"].append(entry)
        else:
            unsupported.append("component_" + (ctype.lower() or "unknown"))

    if parsed["body"] is None:
        unsupported.append("no_body")
    if not named:
        # positional numbering must be 1..n without gaps, otherwise Meta (and we) cannot map values safely
        for comp_name in ("header", "body"):
            names = (parsed[comp_name] or {}).get("variables", []) if parsed[comp_name] else []
            if names and sorted(names) != [str(i) for i in range(1, len(names) + 1)]:
                unsupported.append("non_sequential_variables_" + comp_name)
    if len({v["key"] for v in variables}) != len(variables):
        unsupported.append("duplicate_variable_keys")
    return {"parameter_format": "NAMED" if named else "POSITIONAL", **parsed, "variables": variables,
            "requires_header_media": bool(parsed["header"] and parsed["header"]["requires_media"]),
            "unsupported": sorted(set(unsupported)), "campaign_supported": not unsupported}


def content_hash(raw_components) -> str:
    blob = json.dumps(raw_components or [], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def template_id(name, language) -> str:
    return "%s|%s" % (name, language)


# ── parameter validation (Meta rejects some values at send time; catch them here) ──

def validate_value(value, component="body") -> str:
    """'' if `value` is acceptable as a template parameter, else a reason code."""
    if not isinstance(value, str) or not value.strip():
        return "empty"
    if "\n" in value or "\r" in value or "\t" in value:
        return "contains_newline_or_tab"
    if "    " in value:
        return "four_consecutive_spaces"
    limit = {"header": MAX_HEADER_PARAM_LEN, "button": MAX_BUTTON_PARAM_LEN}.get(component, MAX_PARAM_LEN)
    if len(value) > limit:
        return "too_long"
    return ""


# ── sync ───────────────────────────────────────────────────────────────────────

def _claim_sync() -> None:
    """Cross-replica guard: one sync per cooldown window (atomic upsert on a single state document)."""
    now = datetime.utcnow()
    try:
        _db()[SETTINGS].update_one({"_id": SYNC_STATE_ID, "next_allowed_at": {"$lte": now}},
                                   {"$set": {"next_allowed_at": now + SYNC_COOLDOWN, "last_started_at": now}}, upsert=True)
    except DuplicateKeyError:
        raise TemplateSyncError("sync_busy")
    except Exception as e:
        logger.error("[WA-Templates] sync claim failed (%s)", exc_summary(e))
        raise TemplateSyncError("storage_failed")


def sync() -> dict:
    """Fetch every template from Meta and refresh the cache. All-or-nothing on the Meta side: nothing is written
    unless the whole listing was fetched. Admin campaign_use config is never overwritten."""
    _claim_sync()
    fetched = fetch_from_meta()
    now = datetime.utcnow()
    coll = _db()[TEMPLATES]
    counts = {"fetched": len(fetched), "added": 0, "updated": 0, "unchanged": 0, "status_changed": 0,
              "marked_missing": 0, "restored": 0, "skipped_invalid": 0}
    changes, seen_ids = [], set()
    try:
        for raw in fetched:
            name, lang = str(raw.get("name") or "").strip(), str(raw.get("language") or "").strip()
            if not name or not lang:
                counts["skipped_invalid"] += 1
                continue
            tid = template_id(name, lang)
            seen_ids.add(tid)
            comps = raw.get("components") if isinstance(raw.get("components"), list) else []
            fields = {"name": name, "language": lang, "meta_id": str(raw.get("id") or ""),
                      "status": str(raw.get("status") or "").upper(), "category": str(raw.get("category") or "").upper(),
                      "rejected_reason": str(raw.get("rejected_reason") or "")[:100] if str(raw.get("rejected_reason") or "") != "NONE" else "",
                      "components": comps, "content_hash": content_hash(comps), "parsed": parse_template(raw),
                      "missing_from_meta": False, "synced_at": now}
            old = coll.find_one({"_id": tid}, {"status": 1, "category": 1, "content_hash": 1, "missing_from_meta": 1})
            if old is None:
                coll.update_one({"_id": tid}, {"$set": fields, "$setOnInsert": {"first_seen_at": now, "campaign_use": None}},
                                upsert=True)
                counts["added"] += 1
                continue
            extra = {}
            if old.get("status") != fields["status"]:
                extra = {"previous_status": old.get("status"), "status_changed_at": now}
                counts["status_changed"] += 1
                changes.append({"name": name, "language": lang, "from": old.get("status"), "to": fields["status"]})
            if old.get("missing_from_meta"):
                counts["restored"] += 1
            if old.get("content_hash") == fields["content_hash"] and old.get("status") == fields["status"] \
                    and old.get("category") == fields["category"] and not old.get("missing_from_meta"):
                coll.update_one({"_id": tid}, {"$set": {"synced_at": now}})
                counts["unchanged"] += 1
            else:
                coll.update_one({"_id": tid}, {"$set": {**fields, **extra}})
                counts["updated"] += 1
        # templates Meta no longer returns (deleted there): keep the record, mark it, make it unusable
        for d in coll.find({"missing_from_meta": {"$ne": True}}, {"_id": 1}):
            if d["_id"] not in seen_ids:
                coll.update_one({"_id": d["_id"]}, {"$set": {"missing_from_meta": True, "missing_since": now}})
                counts["marked_missing"] += 1
    except Exception as e:
        logger.error("[WA-Templates] cache update failed (%s)", exc_summary(e))
        raise TemplateSyncError("storage_failed")
    return {"counts": counts, "status_changes": changes[:100], "synced_at": now}


# ── readiness / campaign use ───────────────────────────────────────────────────

def mapping_problems(parsed: dict, variables_cfg) -> list:
    """Reasons the admin's variable mapping is incomplete / invalid for this template ([] = complete)."""
    problems = []
    cfg = variables_cfg if isinstance(variables_cfg, dict) else {}
    keys = {v["key"]: v for v in parsed.get("variables", [])}
    for key in keys:
        m = cfg.get(key)
        if not isinstance(m, dict):
            problems.append("unmapped:" + key)
            continue
        src, comp = m.get("source"), keys[key]["component"]
        if src not in SOURCES:
            problems.append("bad_source:" + key)
        elif src == "static":
            if validate_value(m.get("value"), comp):
                problems.append("bad_static_value:" + key)
        else:
            if validate_value(m.get("fallback"), comp):
                problems.append("fallback_required:" + key)
    for key in cfg:
        if key not in keys:
            problems.append("unknown_variable:" + key)
    return problems


def readiness(doc: dict) -> dict:
    """{"usable": bool, "ready_to_enable": bool, "reasons": [...]} - cache-based; re-verify status with Meta before sending."""
    reasons = []
    if doc.get("missing_from_meta"):
        reasons.append("missing_from_meta")
    if doc.get("status") != "APPROVED":
        reasons.append("status_not_approved")
    if doc.get("category") not in CAMPAIGN_CATEGORIES:
        reasons.append("category_not_allowed")
    parsed = doc.get("parsed") or {}
    if not parsed.get("campaign_supported"):
        reasons.append("unsupported_components")
    cu = doc.get("campaign_use") or {}
    if not cu:
        reasons.append("not_configured")
    else:
        if mapping_problems(parsed, cu.get("variables")):
            reasons.append("mapping_incomplete")
        if cu.get("mapped_hash") != doc.get("content_hash"):
            reasons.append("mapping_stale")
    ready = not reasons
    if not (cu or {}).get("enabled"):
        reasons = reasons + ["not_enabled"]
    return {"usable": not reasons, "ready_to_enable": ready, "reasons": reasons}


# ── preview / payload / resolution (pure) ──────────────────────────────────────

def _fill(text, values_by_name):
    missing = []

    def sub(m):
        n = m.group(1)
        if n in values_by_name:
            return values_by_name[n]
        missing.append(n)
        return m.group(0)
    return VAR_RE.sub(sub, str(text or "")), missing


def _values_for(parsed, values, use_samples):
    """-> ({key: value}, errors{key: reason}, missing[keys]); values override Meta's example values."""
    values = values if isinstance(values, dict) else {}
    final, errors, missing = {}, {}, []
    for v in parsed.get("variables", []):
        key = v["key"]
        val = values.get(key)
        if val is None and use_samples and v.get("example"):
            val = v["example"]
        if val is None:
            missing.append(key)
            continue
        bad = validate_value(val, v["component"])
        if bad:
            errors[key] = bad
        else:
            final[key] = val
    return final, errors, missing


def render_preview(parsed: dict, values=None, use_samples=True) -> dict:
    final, errors, missing = _values_for(parsed, values, use_samples)

    def names_for(component, pos=None):
        out = {}
        for v in parsed.get("variables", []):
            if v["component"] == component and (pos is None or v.get("position") == pos) and v["key"] in final:
                out[v["name"]] = final[v["key"]]
        return out

    pv = {"header": None, "body": None, "footer": (parsed.get("footer") or {}).get("text"), "buttons": []}
    h = parsed.get("header")
    if h:
        pv["header"] = {"format": h["format"], "requires_media": h["requires_media"]}
        if h["format"] == "TEXT":
            pv["header"]["text"] = _fill(h["text"], names_for("header"))[0]
    if parsed.get("body"):
        pv["body"] = _fill(parsed["body"]["text"], names_for("body"))[0]
    for b in parsed.get("buttons", []):
        item = {"type": b["type"], "text": b["text"]}
        if b["type"] == "URL":
            item["url"] = _fill(b["url"], names_for("button", b["position"]))[0]
        if b["type"] == "PHONE_NUMBER":
            item["phone_masked"] = b.get("phone_masked", "")
        pv["buttons"].append(item)
    return {"preview": pv, "missing_variables": missing, "invalid_values": errors,
            "complete": not missing and not errors and bool(parsed.get("campaign_supported")),
            "requires_header_media": parsed.get("requires_header_media", False),
            "unsupported": parsed.get("unsupported", [])}


class ComponentError(Exception):
    pass


def build_send_components(parsed: dict, values: dict, header_media=None) -> list:
    """The Meta `template.components` list for these values. Raises ComponentError (never guesses) when a required
    value is missing/invalid or the template is unsupported. Pure: sends nothing."""
    if not parsed.get("campaign_supported"):
        raise ComponentError("unsupported_template")
    final, errors, missing = _values_for(parsed, values, use_samples=False)
    if errors or missing:
        raise ComponentError("invalid_or_missing_values")
    named = parsed.get("parameter_format") == "NAMED"

    def params(component, pos=None):
        out = []
        for v in parsed["variables"]:
            if v["component"] == component and (pos is None or v.get("position") == pos):
                p = {"type": "text", "text": final[v["key"]]}
                if named:
                    p["parameter_name"] = v["name"]
                out.append(p)
        return out

    comps = []
    h = parsed.get("header")
    if h and h["requires_media"]:
        if not (isinstance(header_media, dict) and header_media.get("type") == h["format"].lower()
                and ((header_media.get("id") or "") or (header_media.get("link") or ""))):
            raise ComponentError("header_media_required")
        kind = h["format"].lower()
        comps.append({"type": "header", "parameters": [{"type": kind, kind: {k: header_media[k] for k in ("id", "link") if header_media.get(k)}}]})
    elif h and h["variables"]:
        comps.append({"type": "header", "parameters": params("header")})
    if parsed.get("body") and parsed["body"]["variables"]:
        comps.append({"type": "body", "parameters": params("body")})
    for b in parsed.get("buttons", []):
        if b["type"] == "URL" and b["variables"]:
            comps.append({"type": "button", "sub_type": "url", "index": str(b["position"]), "parameters": params("button", b["position"])})
    return comps


def resolve_values(campaign_use: dict, recipient: dict) -> dict:
    """{variable key: value} for ONE recipient from the admin's mapping. A missing source value uses the mapping's
    fallback; if that is missing too the key is omitted (build_send_components then refuses - never sends blanks).
    recipient keys: merchant_name, store_name, store_city, store_area, recipient_name."""
    out = {}
    recipient = recipient or {}
    for key, m in ((campaign_use or {}).get("variables") or {}).items():
        if not isinstance(m, dict):
            continue
        if m.get("source") == "static":
            val = m.get("value")
        else:
            raw = recipient.get(m.get("source"))
            val = raw if isinstance(raw, str) and raw.strip() else m.get("fallback")
        if isinstance(val, str) and val.strip():
            out[key] = re.sub(r"\s+", " ", val).strip()          # newlines / tab / long space runs are invalid for Meta
    return out
