"""
routers/wa_campaigns.py
-----------------------
Admin API for WhatsApp Campaign Manager - Phase 2A foundations. NOTHING here sends a message.

All routes need the dedicated "WhatsApp Campaigns" RBAC module (not granted to any existing role; Super Admin has
it). Permission is validated server-side on every route, with the same dependency the RBAC dashboard already uses.

    GET  /admin/whatsapp-campaigns/config        view     safe configuration snapshot (no secrets)
    POST /admin/whatsapp-campaigns/sending       edit     turn the runtime kill switch OFF (approve to turn ON)
    POST /admin/whatsapp-campaigns/consent       approve  record admin-evidenced consent (never overrides STOP)
    POST /admin/whatsapp-campaigns/eligibility   view     centralized eligibility check (masked results)
    GET  /admin/whatsapp-campaigns/audit         view     audit trail (masked)

Admin identity always comes from the authenticated session, never from the request body.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Query

from routers import wa_campaign_audience
from routers.dashboard_auth import require_permission
from services import wa_campaign_audit as audit
from services import wa_campaign_settings as settings
from services import wa_consent as consent
from services import wa_eligibility as elig
from services.wa_webhook_security import exc_summary

logger = logging.getLogger("offro.wa_campaigns")

MODULE = "WhatsApp Campaigns"
MAX_ELIGIBILITY_BATCH = 200

router = APIRouter(prefix="/whatsapp-campaigns", tags=["WhatsApp Campaigns"])
router.include_router(wa_campaign_audience.router)          # Phase 2B audience management (no sending)


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object expected")
    return body


@router.get("/config")
async def get_config(user: dict = Depends(require_permission(MODULE, "view"))):
    return settings.config_snapshot()


@router.post("/sending")
async def set_sending(request: Request, user: dict = Depends(require_permission(MODULE, "edit"))):
    """Body: {"enabled": true|false}. OFF always works (kill switch). ON needs 'approve', the environment key
    WA_CAMPAIGN_SENDING_ENABLED, and an audit entry. This only flips the switch - it starts nothing."""
    body = await _json_object(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled must be true or false")
    admin = audit.admin_ref(user)
    try:
        if enabled:
            require_permission(MODULE, "approve")(user)            # 403 without 'approve'
            if not settings.env_sending_enabled():
                audit.record("sending.enable", admin, outcome="refused_env_disabled")
                raise HTTPException(409, "Sending cannot be enabled: WA_CAMPAIGN_SENDING_ENABLED is not set in this environment")
            audit.record("sending.enable", admin, outcome="attempt", strict=True)    # no unaudited enabling
            settings.set_sending_enabled(True, admin)
        else:
            settings.set_sending_enabled(False, admin)             # the kill switch must work even if audit is down
            audit.record("sending.disable", admin)
    except HTTPException:
        raise
    except audit.AuditWriteError:
        raise HTTPException(503, "Could not write the audit entry - sending was NOT enabled")
    except Exception as e:
        logger.error("[WA-Campaign] sending switch update failed (%s)", exc_summary(e))
        raise HTTPException(503, "Could not update the sending switch")
    allowed, reason = settings.sending_allowed()
    return {"ok": True, "switch_on": enabled, "sending_allowed": allowed, "reason": reason}


_CONSENT_ERRORS = {
    "invalid_phone": (400, "phone must be a valid phone number"),
    "invalid_source": (400, "source must be one of: " + ", ".join(sorted(consent.ADMIN_CONSENT_SOURCES))),
    "evidence_required": (400, "evidence_ref is required (at least 6 characters: form id, document or message reference)"),
    "invalid_timestamp": (400, "consented_at must be an ISO-8601 date/time that is not in the future"),
    "attestation_required": (400, "attested must be true: you confirm this evidence exists and is genuine"),
    "admin_required": (403, "Administrator identity could not be determined"),
    "opted_out": (409, "This number has opted out (STOP). Admin-recorded consent cannot override an opt-out; "
                       "only the customer's own START can restore consent."),
}


@router.post("/consent")
async def record_consent(request: Request, user: dict = Depends(require_permission(MODULE, "approve"))):
    """Body: {phone, source, evidence_ref, consented_at, attested: true, note?}. Records consent given outside
    WhatsApp WITH evidence. Audited BEFORE it is applied; refused entirely if the audit entry cannot be stored."""
    body = await _json_object(request)
    admin = audit.admin_ref(user)
    phone = body.get("phone")
    canonical = consent.normalize_phone(phone) if isinstance(phone, str) else ""
    target = audit.phone_ref(canonical) if canonical else None
    details = {"source": body.get("source"), "evidence_ref": body.get("evidence_ref"),
               "consented_at": body.get("consented_at")}
    try:
        audit.record("consent.admin_record", admin, outcome="attempt", target=target, details=details, strict=True)
    except audit.AuditWriteError:
        raise HTTPException(503, "Could not write the audit entry - consent was NOT recorded")
    try:
        res = consent.record_admin_consent(phone if isinstance(phone, str) else "", body.get("source"),
                                           body.get("evidence_ref"), body.get("consented_at"), admin,
                                           body.get("attested"), body.get("note", ""))
    except consent.AdminConsentError as e:
        audit.record("consent.admin_record", admin, outcome="refused:" + e.code, target=target)
        status, msg = _CONSENT_ERRORS.get(e.code, (400, "Consent could not be recorded"))
        raise HTTPException(status, msg)
    except Exception as e:
        logger.error("[WA-Campaign] admin consent write failed (%s)", exc_summary(e))
        audit.record("consent.admin_record", admin, outcome="failed", target=target)
        raise HTTPException(503, "Consent could not be saved - nothing was recorded as consented")
    audit.record("consent.admin_record", admin, outcome=res["result"], target=target)
    return {"ok": True, "result": res["result"], "phone_last4": res["phone_last4"]}


@router.post("/eligibility")
async def check_eligibility(request: Request, user: dict = Depends(require_permission(MODULE, "view"))):
    """Body: {"phones": [...]} (max 200). Returns per-number eligibility with masked numbers only."""
    body = await _json_object(request)
    phones = body.get("phones")
    if not isinstance(phones, list) or not phones or len(phones) > MAX_ELIGIBILITY_BATCH \
            or not all(isinstance(p, str) for p in phones):
        raise HTTPException(400, "phones must be a list of 1 to %d strings" % MAX_ELIGIBILITY_BATCH)
    results = elig.check_many(phones)
    summary = {}
    for r in results:
        summary[r.reason] = summary.get(r.reason, 0) + 1
    return {"results": [{"index": i, "phone_last4": r.phone_last4, "eligible": r.eligible, "reason": r.reason,
                         "basis": r.basis} for i, r in enumerate(results)],
            "summary": summary}


@router.get("/audit")
async def get_audit(limit: int = Query(50, ge=1, le=100), action: str = "", campaign_id: str = "", before: str = "",
                    user: dict = Depends(require_permission(MODULE, "view"))):
    return {"entries": audit.list_entries(limit=limit, action=action, campaign_id=campaign_id, before=before or None)}
