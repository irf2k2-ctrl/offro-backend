"""
routers/wa_campaign_templates.py
--------------------------------
Template management endpoints (Phase 2C), mounted by routers/wa_campaigns.py under
/admin/whatsapp-campaigns/templates. NOTHING here sends a WhatsApp message; the only Meta call is the read-only
template listing behind POST /templates/sync.

    POST /templates/sync                              edit     refresh the local cache from Meta (cooldown, audited)
    GET  /templates                                   view     list (status, category, language, search, campaign filters)
    GET  /templates/{name}/{language}                 view     detail: components, variables, campaign use, readiness
    POST /templates/{name}/{language}/preview         view     render with sample values (no send)
    PUT  /templates/{name}/{language}/campaign-use    edit     save variable mapping / disable;  enabling needs approve

Permission: the dedicated "WhatsApp Campaigns" module on every route, validated server-side. Admin identity comes from
the session. Meta error text and credentials are never returned.
"""
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from routers.dashboard_auth import require_permission
from services import wa_campaign_audit as audit
from services import wa_templates as T
from services.wa_webhook_security import exc_summary

logger = logging.getLogger("offro.wa_campaigns.templates")

MODULE = "WhatsApp Campaigns"
router = APIRouter(prefix="/templates", tags=["WhatsApp Campaigns - Templates"])

_NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(_[A-Za-z0-9]{2,4})?$")


def _unavailable(e, what):
    logger.error("[WA-Templates] %s failed (%s)", what, exc_summary(e))
    return HTTPException(503, "Template data is temporarily unavailable (%s)" % what)


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object expected")
    return body


def _load(name, language):
    if not _NAME_RE.match(name) or not _LANG_RE.match(language):
        raise HTTPException(404, "Template not found")
    try:
        doc = T._db()[T.TEMPLATES].find_one({"_id": T.template_id(name, language)})
    except Exception as e:
        raise _unavailable(e, "template")
    if not doc:
        raise HTTPException(404, "Template not found")
    return doc


def _iso(v):
    return v.isoformat() + "Z" if isinstance(v, datetime) else None


def _summary(doc):
    p = doc.get("parsed") or {}
    cu = doc.get("campaign_use") or {}
    r = T.readiness(doc)
    return {"name": doc["name"], "language": doc["language"], "status": doc.get("status", ""),
            "category": doc.get("category", ""), "variable_count": len(p.get("variables", [])),
            "campaign_supported": bool(p.get("campaign_supported")), "unsupported": p.get("unsupported", []),
            "requires_header_media": bool(p.get("requires_header_media")), "missing_from_meta": bool(doc.get("missing_from_meta")),
            "campaign_enabled": bool(cu.get("enabled")), "usable_for_campaign": r["usable"], "readiness": r["reasons"],
            "synced_at": _iso(doc.get("synced_at"))}


def _detail(doc):
    out = _summary(doc)
    cu = doc.get("campaign_use") or {}
    out.update({"parsed": doc.get("parsed"), "rejected_reason": doc.get("rejected_reason", ""),
                "previous_status": doc.get("previous_status"), "status_changed_at": _iso(doc.get("status_changed_at")),
                "first_seen_at": _iso(doc.get("first_seen_at")),
                "campaign_use": None if not cu else {
                    "enabled": bool(cu.get("enabled")), "variables": cu.get("variables", {}),
                    "sample_values": cu.get("sample_values", {}), "stale": cu.get("mapped_hash") != doc.get("content_hash"),
                    "updated_by": (cu.get("updated_by") or {}).get("name", ""), "updated_at": _iso(cu.get("updated_at"))},
                "variable_sources": T.SOURCES})
    return out


@router.post("/sync")
async def sync_templates(user: dict = Depends(require_permission(MODULE, "edit"))):
    admin = audit.admin_ref(user)
    try:
        result = T.sync()
    except T.TemplateSyncError as e:
        audit.record("templates.sync", admin, outcome="failed:" + e.code, details={"meta_code": e.meta_code})
        raise HTTPException(e.http, {"code": e.code, "message": e.message, "meta_code": e.meta_code})
    except Exception as e:
        raise _unavailable(e, "sync")
    audit.record("templates.sync", admin, outcome="ok", details=result["counts"])
    return {"ok": True, "counts": result["counts"], "status_changes": result["status_changes"],
            "synced_at": _iso(result["synced_at"])}


@router.get("")
async def list_templates(status: str = "", category: str = "", language: str = "", q: str = "",
                         campaign: str = Query("", pattern="^(|usable|enabled|not_ready)$"),
                         page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                         user: dict = Depends(require_permission(MODULE, "view"))):
    try:
        docs = list(T._db()[T.TEMPLATES].find({}))
    except Exception as e:
        raise _unavailable(e, "templates")
    rows = [_summary(d) for d in docs]
    if status:
        rows = [r for r in rows if r["status"] == status.upper()]
    if category:
        rows = [r for r in rows if r["category"] == category.upper()]
    if language:
        rows = [r for r in rows if r["language"].casefold() == language.casefold()]
    if q.strip():
        rows = [r for r in rows if q.strip().casefold() in r["name"].casefold()]
    if campaign == "usable":
        rows = [r for r in rows if r["usable_for_campaign"]]
    elif campaign == "enabled":
        rows = [r for r in rows if r["campaign_enabled"]]
    elif campaign == "not_ready":
        rows = [r for r in rows if not r["usable_for_campaign"]]
    rows.sort(key=lambda r: (r["name"], r["language"]))
    total = len(rows)
    pages = max(1, -(-total // page_size))
    page = min(page, pages)
    facets = {"statuses": sorted({d.get("status", "") for d in docs if d.get("status")}),
              "categories": sorted({d.get("category", "") for d in docs if d.get("category")}),
              "languages": sorted({d["language"] for d in docs})}
    return {"items": rows[(page - 1) * page_size: page * page_size], "total": total, "page": page,
            "page_size": page_size, "pages": pages, "facets": facets}


@router.get("/{name}/{language}")
async def get_template(name: str, language: str, user: dict = Depends(require_permission(MODULE, "view"))):
    return _detail(_load(name, language))


@router.post("/{name}/{language}/preview")
async def preview_template(name: str, language: str, request: Request,
                           user: dict = Depends(require_permission(MODULE, "view"))):
    """Body: {"values": {"body.1": "Asha"}, "use_samples": true}. Unset variables fall back to Meta's example values
    when use_samples is true (the default). Returns the rendered message and what is still missing / invalid."""
    doc = _load(name, language)
    body = await _json_object(request) if request.headers.get("content-length", "0") not in ("", "0") else {}
    values = body.get("values", {})
    if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
        raise HTTPException(400, "values must be an object of text values keyed by variable key (e.g. 'body.1')")
    use_samples = body.get("use_samples", True)
    if not isinstance(use_samples, bool):
        raise HTTPException(400, "use_samples must be true or false")
    parsed = doc.get("parsed") or {}
    unknown = sorted(set(values) - {v["key"] for v in parsed.get("variables", [])})
    result = T.render_preview(parsed, values, use_samples)
    result.update({"name": name, "language": language, "status": doc.get("status", ""), "category": doc.get("category", ""),
                   "unknown_variables": unknown, "sent": False,
                   "note": "Preview only. Nothing was sent. Status is from the last sync and is re-verified with Meta before any send."})
    return result


@router.put("/{name}/{language}/campaign-use")
async def set_campaign_use(name: str, language: str, request: Request,
                           user: dict = Depends(require_permission(MODULE, "edit"))):
    """Body: {"enabled": bool, "variables": {"body.1": {"source": "static", "value": "..."} |
                                              {"source": "merchant_name", "fallback": "Valued merchant"}},
              "sample_values": {"body.1": "Asha"}}
    Mapping every variable is required to enable. ENABLING needs 'approve', status APPROVED, category MARKETING and only
    supported components. Disabling always works. This only stores configuration - it starts nothing."""
    doc = _load(name, language)
    body = await _json_object(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled must be true or false")
    variables, samples = body.get("variables", {}), body.get("sample_values", {})
    if not isinstance(variables, dict) or not isinstance(samples, dict):
        raise HTTPException(400, "variables and sample_values must be objects")
    clean = {}
    for key, m in variables.items():
        if not isinstance(key, str) or not isinstance(m, dict):
            raise HTTPException(400, "Each variable mapping must be an object")
        clean[key] = {k: m[k] for k in ("source", "value", "fallback") if isinstance(m.get(k), str)}
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in samples.items()):
        raise HTTPException(400, "sample_values must be text values")

    admin = audit.admin_ref(user)
    now = datetime.utcnow()
    candidate = dict(doc)
    candidate["campaign_use"] = {"enabled": enabled, "variables": clean, "sample_values": samples,
                                 "mapped_hash": doc.get("content_hash"), "updated_by": admin, "updated_at": now}
    problems = T.mapping_problems(doc.get("parsed") or {}, clean)
    parsed_keys = {v["key"] for v in (doc.get("parsed") or {}).get("variables", [])}
    bad_samples = {k: T.validate_value(v) for k, v in samples.items() if k in parsed_keys and T.validate_value(v)}
    if bad_samples:
        raise HTTPException(400, {"code": "invalid_sample_values", "invalid": bad_samples})
    if enabled:
        require_permission(MODULE, "approve")(user)                    # 403 without 'approve'
        r = T.readiness(candidate)
        blockers = [x for x in r["reasons"] if x != "not_enabled"]
        if blockers or problems:
            audit.record("template.campaign_enable", admin, outcome="refused", target={"template": T.template_id(name, language)},
                         details={"reasons": blockers, "mapping_problems": problems[:20]})
            raise HTTPException(409, {"code": "template_not_ready", "reasons": blockers, "mapping_problems": problems})
        try:
            audit.record("template.campaign_enable", admin, outcome="attempt", strict=True,
                         target={"template": T.template_id(name, language)})
        except audit.AuditWriteError:
            raise HTTPException(503, "Could not write the audit entry - the template was NOT enabled")
    try:
        res = T._db()[T.TEMPLATES].update_one({"_id": doc["_id"]}, {"$set": {"campaign_use": candidate["campaign_use"]}})
        if not res.acknowledged:
            raise RuntimeError("not acknowledged")
    except Exception as e:
        raise _unavailable(e, "save")
    audit.record("template.campaign_enable" if enabled else "template.campaign_update", admin, outcome="ok" if enabled else "saved",
                 target={"template": T.template_id(name, language)},
                 details={"enabled": enabled, "variables": len(clean), "mapping_problems": len(problems)})
    return _detail({**doc, "campaign_use": candidate["campaign_use"]})
