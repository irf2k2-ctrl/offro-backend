"""
routers/wa_campaign_lifecycle.py
--------------------------------
Campaign lifecycle endpoints (Phase 2D), mounted by routers/wa_campaigns.py under /admin/whatsapp-campaigns/campaigns.
There is NO bulk sender, NO worker and NO "send now" here. `schedule` only stores a time. The one route that can reach
WhatsApp is POST /{id}/test-send: one template message to one allow-listed, consenting number, behind the Phase 2A kill
switch (see services/wa_campaign_manager.py for the full gate order).

    POST   /campaigns                       add      create a draft {name, description}
    GET    /campaigns                       view     list (status filter, paging)
    GET    /campaigns/{id}                  view     detail
    PATCH  /campaigns/{id}                  edit     edit a DRAFT: name, description, template, header_media, audience
    DELETE /campaigns/{id}                  delete   delete a draft / cancelled campaign
    POST   /campaigns/{id}/preview          view     render the template for a sample recipient
    POST   /campaigns/{id}/audience-preview view     recipients + eligibility counts for the selected audience (live)
    POST   /campaigns/{id}/validate         edit     draft -> ready; freezes the recipient snapshot
    POST   /campaigns/{id}/revert-to-draft  edit     ready -> draft (drops the snapshot)
    GET    /campaigns/{id}/recipients       view     snapshot (masked numbers)
    GET    /campaigns/{id}/confirmation     view     checklist + warnings to read before scheduling
    POST   /campaigns/{id}/test-send        approve  one test message to an allow-listed number
    POST   /campaigns/{id}/schedule         approve  explicit confirmation + schedule metadata (ready -> scheduled)
    POST   /campaigns/{id}/unschedule       edit     scheduled -> ready
    POST   /campaigns/{id}/cancel           edit     draft/ready/scheduled -> cancelled (terminal)

Permission: the dedicated "WhatsApp Campaigns" module on every route, validated server-side. Actions that can reach a
customer or commit the business (test-send, schedule) need `approve`; moving AWAY from sending (cancel, unschedule) needs
only `edit` and never depends on the audit log being writable. Admin identity always comes from the session.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from routers.dashboard_auth import require_permission
from services import wa_campaign_audit as audit
from services import wa_campaign_manager as M
from services import wa_templates as T
from services.wa_webhook_security import exc_summary

logger = logging.getLogger("offro.wa_campaigns.lifecycle")

MODULE = "WhatsApp Campaigns"
router = APIRouter(prefix="/campaigns", tags=["WhatsApp Campaigns - Lifecycle"])


def _fail(e):
    return HTTPException(e.http, e.body())


def _unavailable(e, what):
    logger.error("[WA-Campaign] %s failed (%s)", what, exc_summary(e))
    return HTTPException(503, "Campaign data is temporarily unavailable (%s)" % what)


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object expected")
    return body


def _iso(v):
    return v.isoformat() + "Z" if isinstance(v, datetime) else None


def _load(campaign_id):
    try:
        return M.get(campaign_id)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "campaign")


def _who(d):
    return (d or {}).get("name", "")


def _detail(c):
    snap = c.get("snapshot")
    plan = c.get("schedule")
    return {"id": str(c["_id"]), "name": c["name"], "description": c.get("description", ""), "status": c["status"],
            "version": c["version"], "template": c.get("template"), "header_media": _media_view(c.get("header_media")),
            "audience": c.get("audience"),
            "snapshot": None if not snap else {**{k: v for k, v in snap.items() if k != "at"}, "at": _iso(snap.get("at"))},
            "schedule": None if not plan else {"mode": plan.get("mode"), "send_at": _iso(plan.get("send_at")),
                                                "confirmed_by": _who(plan.get("confirmed_by")), "confirmed_at": _iso(plan.get("confirmed_at"))},
            "template_problems": M.template_problems(c), "created_by": _who(c.get("created_by")),
            "created_at": _iso(c.get("created_at")), "updated_by": _who(c.get("updated_by")), "updated_at": _iso(c.get("updated_at")),
            "executes_automatically": False}


def _media_view(m):
    if not m:
        return None
    return {"type": m["type"], "kind": "id" if "id" in m else "link"}      # the id / link itself is not echoed back


def _summary(c):
    d = _detail(c)
    return {k: d[k] for k in ("id", "name", "status", "version", "template", "created_by", "created_at", "updated_at")} | {
        "eligible": (d["snapshot"] or {}).get("eligible"), "send_at": (d["schedule"] or {}).get("send_at")}


@router.post("")
async def create_campaign(request: Request, user: dict = Depends(require_permission(MODULE, "add"))):
    body = await _json_object(request)
    admin = audit.admin_ref(user)
    try:
        c = M.create(body.get("name"), body.get("description", ""), admin)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "create")
    audit.record("campaign.create", admin, campaign_id=c["_id"], details={"name": c["name"]})
    return _detail(c)


@router.get("")
async def list_campaigns(status: str = Query("", pattern="^(|draft|ready|scheduled|running|paused|cancelled|completed)$"),
                         page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                         user: dict = Depends(require_permission(MODULE, "view"))):
    q = {"status": status} if status else {}
    try:
        total = M._db()[M.CAMPAIGNS].count_documents(q)
        pages = max(1, -(-total // page_size))
        page = min(page, pages)
        docs = M._db()[M.CAMPAIGNS].find(q).sort("_id", -1).skip((page - 1) * page_size).limit(page_size)
        return {"items": [_summary(d) for d in docs], "total": total, "page": page, "page_size": page_size, "pages": pages}
    except Exception as e:
        raise _unavailable(e, "campaigns")


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: str, user: dict = Depends(require_permission(MODULE, "view"))):
    return _detail(_load(campaign_id))


@router.patch("/{campaign_id}")
async def edit_campaign(campaign_id: str, request: Request, user: dict = Depends(require_permission(MODULE, "edit"))):
    body = await _json_object(request)
    c = _load(campaign_id)
    admin = audit.admin_ref(user)
    expected = body.pop("expected_version", None)
    if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool)):
        raise HTTPException(400, "expected_version must be an integer")
    unknown = set(body) - {"name", "description", "template", "header_media", "audience"}
    if unknown:
        raise HTTPException(400, {"code": "unknown_fields", "fields": sorted(unknown)})
    try:
        out = M.update_draft(c, body, expected, admin)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "update")
    audit.record("campaign.update", admin, campaign_id=c["_id"], details={"fields": sorted(body)})
    return _detail(out)


@router.delete("/{campaign_id}")
async def delete_campaign(campaign_id: str, user: dict = Depends(require_permission(MODULE, "delete"))):
    c = _load(campaign_id)
    admin = audit.admin_ref(user)
    try:
        audit.record("campaign.delete", admin, outcome="attempt", campaign_id=c["_id"], strict=True, details={"name": c["name"]})
    except audit.AuditWriteError:
        raise HTTPException(503, "Could not write the audit entry - nothing was deleted")
    try:
        M.delete(c)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "delete")
    audit.record("campaign.delete", admin, outcome="deleted", campaign_id=c["_id"])
    return {"ok": True}


@router.post("/{campaign_id}/preview")
async def preview_campaign(campaign_id: str, request: Request, user: dict = Depends(require_permission(MODULE, "view"))):
    """Body (optional): {"recipient": {"merchant_name": "...", "store_name": "...", "store_city": "...", "store_area": "...",
    "recipient_name": "..."}} - SAMPLE data only; defaults are obviously fake. Sends nothing."""
    body = await _json_object(request) if request.headers.get("content-length", "0") not in ("", "0") else {}
    sample = body.get("recipient")
    if sample is not None and (not isinstance(sample, dict) or not all(isinstance(v, str) for v in sample.values())):
        raise HTTPException(400, "recipient must be an object of text values")
    c = _load(campaign_id)
    try:
        return M.preview_message(c, sample)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "preview")


@router.post("/{campaign_id}/audience-preview")
async def audience_preview(campaign_id: str, request: Request, user: dict = Depends(require_permission(MODULE, "view"))):
    body = await _json_object(request) if request.headers.get("content-length", "0") not in ("", "0") else {}
    status = body.get("status", "all")
    if status not in ("all", "eligible", "excluded"):
        raise HTTPException(400, "status must be all, eligible or excluded")
    try:
        page, page_size = int(body.get("page", 1)), int(body.get("page_size", 50))
    except (TypeError, ValueError):
        raise HTTPException(400, "page and page_size must be integers")
    c = _load(campaign_id)
    try:
        return M.audience_preview(c, user, page, page_size, status)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "audience")


@router.post("/{campaign_id}/validate")
async def validate_campaign(campaign_id: str, user: dict = Depends(require_permission(MODULE, "edit"))):
    c = _load(campaign_id)
    admin = audit.admin_ref(user)
    try:
        report = M.validate_and_snapshot(c, user)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "validate")
    if not report["ok"]:
        audit.record("campaign.validate", admin, outcome="failed", campaign_id=c["_id"], details={"errors": report["errors"]})
        raise HTTPException(409, {"code": "validation_failed", **report})
    snap = report["snapshot"]
    audit.record("campaign.validate", admin, outcome="ok", campaign_id=c["_id"],
                 details={"eligible": snap["eligible"], "excluded": snap["excluded"], "by_reason": snap["by_reason"]})
    return {**report, "campaign": _detail(M.get(campaign_id))}


@router.post("/{campaign_id}/revert-to-draft")
async def revert_to_draft(campaign_id: str, user: dict = Depends(require_permission(MODULE, "edit"))):
    c = _load(campaign_id)
    admin = audit.admin_ref(user)
    try:
        out = M.revert_to_draft(c, admin)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "revert")
    audit.record("campaign.revert_to_draft", admin, campaign_id=c["_id"])
    return _detail(out)


@router.get("/{campaign_id}/recipients")
async def campaign_recipients(campaign_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                              user: dict = Depends(require_permission(MODULE, "view"))):
    c = _load(campaign_id)
    if not c.get("snapshot"):
        raise HTTPException(409, {"code": "no_snapshot", "message": "Validate the campaign to create a recipient snapshot."})
    try:
        return M.list_snapshot(c, page, page_size)
    except Exception as e:
        raise _unavailable(e, "recipients")


@router.get("/{campaign_id}/confirmation")
async def confirmation(campaign_id: str, user: dict = Depends(require_permission(MODULE, "view"))):
    c = _load(campaign_id)
    try:
        return M.confirmation_report(c)
    except Exception as e:
        raise _unavailable(e, "confirmation")


@router.post("/{campaign_id}/test-send")
async def test_send(campaign_id: str, request: Request, user: dict = Depends(require_permission(MODULE, "approve"))):
    """Body: {"phone": "+91...", "confirm_test_send": true, "dry_run": false, "recipient": {sample context}}.
    One message to ONE number that is on WA_CAMPAIGN_TEST_NUMBERS, has recorded consent and is not opted out, only while
    the Phase 2A kill switch is on, and only after the template is re-verified live with Meta."""
    body = await _json_object(request)
    dry_run = body.get("dry_run", False)
    sample = body.get("recipient")
    if not isinstance(dry_run, bool) or not isinstance(body.get("phone"), str):
        raise HTTPException(400, "phone (text) and dry_run (true/false) are required in the right types")
    if sample is not None and (not isinstance(sample, dict) or not all(isinstance(v, str) for v in sample.values())):
        raise HTTPException(400, "recipient must be an object of text values")
    c = _load(campaign_id)
    try:
        res = M.send_test_message(c, body["phone"], audit.admin_ref(user), body.get("confirm_test_send"), dry_run, sample)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "test send")
    if res.get("dry_run") or res.get("outcome") == "sent":
        return res
    raise HTTPException(202 if res.get("outcome") == "uncertain" else 502, res)


@router.post("/{campaign_id}/schedule")
async def schedule_campaign(campaign_id: str, request: Request, user: dict = Depends(require_permission(MODULE, "approve"))):
    """Body: {"send_at": "2026-12-01T10:00:00+05:30",
              "confirm": {"acknowledge": true, "eligible_count": <snapshot eligible>, "template": "<name>/<language>"}}
    The explicit admin approval. Stores the schedule only - nothing executes it in this release."""
    body = await _json_object(request)
    c = _load(campaign_id)
    admin = audit.admin_ref(user)
    try:
        M.check_schedulable(c, body.get("confirm"))
        send_at = M.parse_send_at(body.get("send_at"))
        try:
            live = T.verify_live(M.load_template(c["template"]))           # fail closed if Meta cannot be asked
        except T.TemplateSyncError as e:
            raise M.CampaignError("template_unverified", 502, e.message, meta_code=e.meta_code, template_error=e.code)
        if live:
            raise M.CampaignError("template_not_ready", 409, "The template is no longer usable at Meta.", reasons=live)
        audit.record("campaign.schedule", admin, outcome="attempt", campaign_id=c["_id"], strict=True,
                     details={"send_at": send_at.isoformat() + "Z", "eligible": c["snapshot"]["eligible"]})
        out = M.schedule(c, send_at, admin)
    except M.CampaignError as e:
        if e.code not in ("not_found",):
            audit.record("campaign.schedule", admin, outcome="refused:" + e.code, campaign_id=c["_id"])
        raise _fail(e)
    except audit.AuditWriteError:
        raise HTTPException(503, "Could not write the audit entry - the campaign was NOT scheduled")
    except Exception as e:
        raise _unavailable(e, "schedule")
    audit.record("campaign.schedule", admin, outcome="ok", campaign_id=c["_id"])
    return {"campaign": _detail(out), "warnings": M.confirmation_report(out)["warnings"]}


@router.post("/{campaign_id}/unschedule")
async def unschedule_campaign(campaign_id: str, user: dict = Depends(require_permission(MODULE, "edit"))):
    c = _load(campaign_id)
    admin = audit.admin_ref(user)
    try:
        out = M.unschedule(c, admin)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "unschedule")
    audit.record("campaign.unschedule", admin, campaign_id=c["_id"])
    return _detail(out)


@router.post("/{campaign_id}/cancel")
async def cancel_campaign(campaign_id: str, user: dict = Depends(require_permission(MODULE, "edit"))):
    c = _load(campaign_id)
    admin = audit.admin_ref(user)
    try:
        out = M.cancel(c, admin)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "cancel")
    audit.record("campaign.cancel", admin, campaign_id=c["_id"], details={"was": c["status"]})      # best effort: never blocks a cancel
    return _detail(out)
