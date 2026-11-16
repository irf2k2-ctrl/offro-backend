"""
routers/wa_campaign_run.py
--------------------------
Phase 2E/2F run controls and reporting, mounted by routers/wa_campaigns.py. NOTHING here sends a message: these endpoints
only change run state or read it. Messages are sent solely by the worker (services/wa_campaign_worker.py), which is OFF by
default and re-checks every gate before every message.

    GET   /worker/status                   view     gate / config snapshot of the worker (no secrets)
    GET   /campaigns/{id}/run              view     run state + report numbers
    GET   /campaigns/{id}/stats            view     report numbers only (aggregated from recipient records)
    POST  /campaigns/{id}/run-config       approve  scheduled campaign: choose mode "test" (default) or "live" (needs confirmation)
    POST  /campaigns/{id}/pause            edit     running -> paused           (moving away from sending: 'edit' is enough)
    POST  /campaigns/{id}/resume           approve  paused -> running
    POST  /campaigns/{id}/cancel-run       edit     running/paused -> cancelled, queued recipients cancelled, records kept

Admin identity always comes from the session. Moving away from sending never depends on the audit log being writable.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from routers.dashboard_auth import require_permission
from services import wa_campaign_audit as audit
from services import wa_campaign_manager as M
from services import wa_campaign_worker as W
from services import wa_delivery as D
from services.wa_webhook_security import exc_summary

logger = logging.getLogger("offro.wa_campaigns.run")

MODULE = "WhatsApp Campaigns"
router = APIRouter(prefix="/campaigns", tags=["WhatsApp Campaigns - Run"])
worker_router = APIRouter(prefix="/worker", tags=["WhatsApp Campaigns - Worker"])


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


def _run_view(c):
    run = c.get("run") or {}
    return {"id": str(c["_id"]), "status": c["status"], "version": c["version"],
            "mode": run.get("mode") or (c.get("run_config") or {}).get("mode", "test"),
            "started_at": _iso(run.get("started_at")), "finished_at": _iso(run.get("finished_at")),
            "paused_at": _iso(run.get("paused_at")), "pause_reason": run.get("pause_reason"),
            "backoff_until": _iso(run.get("backoff_until")) if (run.get("backoff_until") or W.EPOCH) > W.EPOCH else None}


@worker_router.get("/status")
async def worker_status(user: dict = Depends(require_permission(MODULE, "view"))):
    try:
        return W.status()
    except Exception as e:
        raise _unavailable(e, "worker status")


@router.get("/{campaign_id}/stats")
async def stats(campaign_id: str, user: dict = Depends(require_permission(MODULE, "view"))):
    c = _load(campaign_id)
    try:
        return D.campaign_stats(c["_id"])
    except Exception as e:
        raise _unavailable(e, "campaign statistics")


@router.get("/{campaign_id}/run")
async def run_info(campaign_id: str, user: dict = Depends(require_permission(MODULE, "view"))):
    c = _load(campaign_id)
    try:
        return {**_run_view(c), "stats": D.campaign_stats(c["_id"])}
    except Exception as e:
        raise _unavailable(e, "campaign run")


@router.post("/{campaign_id}/run-config")
async def run_config(campaign_id: str, request: Request, user: dict = Depends(require_permission(MODULE, "approve"))):
    """Body: {"mode": "test"|"live", "confirm": {"acknowledge": true, "mode": "live", "eligible_count": N}}  (confirm for live only)"""
    body = await _json_object(request)
    c = _load(campaign_id)
    admin = audit.admin_ref(user)
    try:
        audit.record("campaign.run_config", admin, outcome="attempt", campaign_id=c["_id"], details={"mode": str(body.get("mode"))[:10]}, strict=True)
    except audit.AuditWriteError:
        raise HTTPException(503, "Could not record the audit entry - nothing was changed.")
    try:
        out = W.set_run_config(c, body.get("mode"), body.get("confirm"), admin)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "run configuration")
    return _run_view(out)


def _control(campaign_id, user, fn, action):
    c = _load(campaign_id)
    admin = audit.admin_ref(user)
    try:
        out = fn(c, admin)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, action)
    audit.record("campaign." + action, admin, campaign_id=c["_id"])            # best effort: stopping is never blocked by the log
    return _run_view(out)


@router.post("/{campaign_id}/pause")
async def pause(campaign_id: str, user: dict = Depends(require_permission(MODULE, "edit"))):
    return _control(campaign_id, user, W.pause, "run_paused")


@router.post("/{campaign_id}/cancel-run")
async def cancel_run(campaign_id: str, user: dict = Depends(require_permission(MODULE, "edit"))):
    return _control(campaign_id, user, W.cancel_run, "run_cancelled")


@router.post("/{campaign_id}/resume")
async def resume(campaign_id: str, user: dict = Depends(require_permission(MODULE, "approve"))):
    c = _load(campaign_id)
    admin = audit.admin_ref(user)
    try:
        audit.record("campaign.run_resumed", admin, outcome="attempt", campaign_id=c["_id"], strict=True)
    except audit.AuditWriteError:
        raise HTTPException(503, "Could not record the audit entry - nothing was changed.")
    try:
        out = W.resume(c, admin)
    except M.CampaignError as e:
        raise _fail(e)
    except Exception as e:
        raise _unavailable(e, "resume")
    return _run_view(out)
