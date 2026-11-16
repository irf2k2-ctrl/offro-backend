"""
routers/wa_campaign_audience.py
-------------------------------
Audience management endpoints (Phase 2B), mounted by routers/wa_campaigns.py under /admin/whatsapp-campaigns/audience.
Audience only: NOTHING here sends a message, calls Meta, or queues anything.

    GET    /audience/filters               view    City / Area / State options (active stores, admin's city scope)
    GET    /audience/merchants             view    qualifying merchants (search, city, area, state, paging) + eligibility
    POST   /audience/imports               add     manual numbers  {"entries":[{"phone","name"}]} or {"phones":[...]}
    POST   /audience/imports/csv           add     multipart upload, field "file" (CSV only)
    GET    /audience/imports               view    stored imports (summaries)
    GET    /audience/imports/{id}          view    one import: counts + rejected/duplicate rows (row number + reason)
    DELETE /audience/imports/{id}          delete  remove the stored import (audited)
    POST   /audience/preview               view    combined, deduped audience with per-recipient eligibility

Permission: the dedicated "WhatsApp Campaigns" module, validated server-side on every route. Admin identity comes from
the session. Phone numbers are always returned masked (last 4 digits).
"""
import logging

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File

from routers.dashboard_auth import require_permission
from services import wa_audience as AU
from services import wa_campaign_audit as audit
from services.wa_webhook_security import exc_summary

logger = logging.getLogger("offro.wa_campaigns.audience")

MODULE = "WhatsApp Campaigns"
router = APIRouter(prefix="/audience", tags=["WhatsApp Campaigns - Audience"])


def _unavailable(e, what):
    logger.error("[WA-Audience] %s failed (%s)", what, exc_summary(e))
    return HTTPException(503, "Audience data is temporarily unavailable (%s)" % what)


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object expected")
    return body


@router.get("/filters")
async def filters(user: dict = Depends(require_permission(MODULE, "view"))):
    try:
        return AU.filter_options(user)
    except Exception as e:
        raise _unavailable(e, "filters")


@router.get("/merchants")
async def merchants(q: str = "", city: str = "", area: str = "", state: str = "",
                    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                    user: dict = Depends(require_permission(MODULE, "view"))):
    try:
        return AU.list_merchants({"q": q, "city": city, "area": area, "state": state}, page, page_size, user)
    except Exception as e:
        raise _unavailable(e, "merchants")


def _store_import(kind, entries, user, filename="", ignored=None, headerless=False):
    admin = audit.admin_ref(user)
    try:
        audit.record("audience.import", admin, outcome="attempt", strict=True,
                     details={"kind": kind, "rows": len(entries)})
    except audit.AuditWriteError:
        raise HTTPException(503, "Could not write the audit entry - nothing was stored")
    try:
        doc = AU.create_import(kind, entries, admin, filename, ignored, headerless)
        summary = AU.import_summary(doc)
    except Exception as e:
        raise _unavailable(e, "import")
    audit.record("audience.import", admin, outcome="stored", target={"import_id": summary["import_id"]},
                 details=summary["counts"])
    return {**summary, "problems": AU.rejected_rows(doc, 50), "problems_total": summary["counts"]["invalid"] + summary["counts"]["duplicates"]}


@router.post("/imports")
async def import_manual(request: Request, user: dict = Depends(require_permission(MODULE, "add"))):
    body = await _json_object(request)
    items = body.get("entries")
    if items is None and isinstance(body.get("phones"), list):
        items = [{"phone": p} for p in body["phones"]]
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "Provide a non-empty 'entries' list (or 'phones').")
    if len(items) > AU.MAX_MANUAL_ENTRIES:
        raise HTTPException(400, "At most %d numbers per manual import; use CSV for more." % AU.MAX_MANUAL_ENTRIES)
    entries = []
    for i, it in enumerate(items, start=1):
        if isinstance(it, str):
            it = {"phone": it}
        if not isinstance(it, dict):
            raise HTTPException(400, "Entry %d must be an object with a 'phone'." % i)
        entries.append((i, it.get("phone"), it.get("name", "")))
    return _store_import("manual", entries, user)


@router.post("/imports/csv")
async def import_csv(file: UploadFile = File(...), user: dict = Depends(require_permission(MODULE, "add"))):
    data = await file.read(AU.MAX_CSV_BYTES + 1)           # never read more than the limit + 1 byte
    try:
        parsed = AU.parse_csv(data)
    except AU.AudienceError as e:
        raise HTTPException(400, {"code": e.code, "message": e.message})
    return _store_import("csv", parsed["entries"], user, file.filename or "", parsed["ignored_columns"],
                         parsed["headerless"])


@router.get("/imports")
async def list_imports(limit: int = Query(50, ge=1, le=200), user: dict = Depends(require_permission(MODULE, "view"))):
    try:
        idx = AU._merchant_phone_index()
        docs = AU._db()[AU.IMPORTS].find({}, {"rejected": 0, "duplicates": 0}).sort("_id", -1).limit(limit)
        return {"items": [AU.import_summary(d, idx) for d in docs]}
    except Exception as e:
        raise _unavailable(e, "imports")


@router.get("/imports/{import_id}")
async def get_import(import_id: str, user: dict = Depends(require_permission(MODULE, "view"))):
    try:
        doc = AU.get_import(import_id)
    except Exception as e:
        raise _unavailable(e, "import")
    if not doc:
        raise HTTPException(404, "Import not found")
    return {**AU.import_summary(doc), "problems": AU.rejected_rows(doc, 1000)}


@router.delete("/imports/{import_id}")
async def delete_import(import_id: str, user: dict = Depends(require_permission(MODULE, "delete"))):
    try:
        oid = ObjectId(import_id)
    except (InvalidId, TypeError):
        raise HTTPException(404, "Import not found")
    admin = audit.admin_ref(user)
    try:
        audit.record("audience.import_delete", admin, outcome="attempt", strict=True, target={"import_id": import_id})
    except audit.AuditWriteError:
        raise HTTPException(503, "Could not write the audit entry - nothing was deleted")
    try:
        res = AU._db()[AU.IMPORTS].delete_one({"_id": oid})
    except Exception as e:
        raise _unavailable(e, "delete")
    if not res.deleted_count:
        raise HTTPException(404, "Import not found")
    audit.record("audience.import_delete", admin, outcome="deleted", target={"import_id": import_id})
    return {"ok": True}


@router.post("/preview")
async def preview(request: Request, user: dict = Depends(require_permission(MODULE, "view"))):
    """Body: {"merchants": {"ids": [...], "filter": {"q","city","area","state"}}, "imports": ["<id>"],
              "page": 1, "page_size": 50, "status": "all|eligible|excluded"}"""
    body = await _json_object(request)
    status = body.get("status", "all")
    if status not in ("all", "eligible", "excluded"):
        raise HTTPException(400, "status must be all, eligible or excluded")
    imports = body.get("imports") or []
    if not isinstance(imports, list) or not all(isinstance(i, str) for i in imports):
        raise HTTPException(400, "imports must be a list of import ids")
    try:
        page, page_size = int(body.get("page", 1)), int(body.get("page_size", 50))
    except (TypeError, ValueError):
        raise HTTPException(400, "page and page_size must be integers")
    try:
        return AU.build_preview(body.get("merchants"), imports, user, page, page_size, status)
    except AU.AudienceError as e:
        raise HTTPException(404 if e.code == "import_not_found" else 400, {"code": e.code, "message": e.message})
    except Exception as e:
        raise _unavailable(e, "preview")
