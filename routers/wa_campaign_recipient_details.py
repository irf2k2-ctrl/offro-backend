"""
routers/wa_campaign_recipient_details.py
----------------------------------------
Phase 2G read-only recipient delivery details, mounted by routers/wa_campaigns.py. NOTHING here sends or changes anything.

    GET  /campaigns/{id}/recipient-details   view   one page of the campaign's recipients with their send / delivery state

Privacy: the phone number is ALWAYS masked (last 4 digits). The Meta message id (wamid) is never returned, because a wamid encodes the
recipient's number; a one-way reference (`message_ref`) is returned instead so rows can be told apart and quoted in a support case.
Consent is read live for the rows on the page (fails soft: a database error shows "unavailable", never a guess).
"""
import hashlib
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from routers.dashboard_auth import require_permission
from services import wa_campaign_manager as M
from services import wa_consent as C
from services.wa_webhook_security import exc_summary, mask_phone

logger = logging.getLogger("offro.wa_campaigns.recipients")

MODULE = "WhatsApp Campaigns"
router = APIRouter(prefix="/campaigns", tags=["WhatsApp Campaigns - Recipient details"])

STATES = ("snapshot", "queued", "sending", "sent", "delivered", "read", "failed", "uncertain", "cancelled")
_LAST4 = re.compile(r"^[0-9]{4}$")


def _iso(v):
    return v.isoformat() + "Z" if isinstance(v, datetime) else None


def _message_ref(message_id):
    return hashlib.sha256(str(message_id).encode()).hexdigest()[:12] if message_id else None


def _failure(r):
    """Failure classification from the stored fields only (no raw provider text is ever stored or shown)."""
    state = r.get("state")
    if state == "failed":
        return {"class": r.get("error") or "failed", "code": r.get("error_code")}
    if state == "uncertain":
        return {"class": r.get("uncertain_reason") or "send_outcome_unknown", "code": r.get("error_code")}
    if state == "cancelled":
        return {"class": r.get("cancel_reason") or "cancelled", "code": None}
    if state == "queued" and r.get("error_code"):
        return {"class": "retry_scheduled", "code": r.get("error_code")}
    return None


def _consent_now(phone):
    try:
        return C.consent_status(phone)
    except Exception as e:
        logger.error("[WA-Campaign] consent read failed for recipient details (%s)", exc_summary(e))
        return "unavailable"


def _row(r, consent):
    return {"phone_masked": mask_phone(r.get("phone")), "source": r.get("source"), "state": r.get("state"),
            "eligibility": {"eligible_at_snapshot": True, "consent_basis": r.get("consent_basis"), "consent_now": consent},
            "message_ref": _message_ref(r.get("message_id")), "has_message_id": bool(r.get("message_id")),
            "timestamps": {"queued_at": _iso(r.get("queued_at")), "sent_at": _iso(r.get("sent_at")),
                           "last_status_at": _iso(r.get("status_ts")), "updated_at": _iso(r.get("updated_at"))},
            "failure": _failure(r),
            "retry": {"attempts": int(r.get("attempts") or 0), "next_attempt_at": _iso(r.get("next_attempt_at")) if r.get("state") == "queued" else None,
                      "recovered": bool(r.get("recovered"))}}


@router.get("/{campaign_id}/recipient-details")
async def recipient_details(campaign_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                            state: str = "", last4: str = "", user: dict = Depends(require_permission(MODULE, "view"))):
    if state and state not in STATES:
        raise HTTPException(400, {"code": "invalid_state", "message": "Unknown recipient state."})
    if last4 and not _LAST4.match(last4):
        raise HTTPException(400, {"code": "invalid_last4", "message": "last4 must be exactly four digits."})
    try:
        c = M.get(campaign_id)
    except M.CampaignError as e:
        raise HTTPException(e.http, e.body())
    except Exception as e:
        logger.error("[WA-Campaign] campaign read failed (%s)", exc_summary(e))
        raise HTTPException(503, "Campaign data is temporarily unavailable (campaign)")
    snap = c.get("snapshot") or {}
    if not snap.get("id"):
        raise HTTPException(409, {"code": "no_snapshot", "message": "Validate the campaign to create a recipient snapshot."})
    q = {"campaign_id": str(c["_id"]), "snapshot_id": snap["id"]}
    if state:
        q["state"] = state
    if last4:
        q["phone"] = {"$regex": last4 + "$"}
    try:
        coll = M._db()[M.RECIPIENTS]
        total = coll.count_documents(q)
        pages = max(1, -(-total // page_size))
        page = min(page, pages)
        rows = list(coll.find(q).sort("_id", 1).skip((page - 1) * page_size).limit(page_size))
    except Exception as e:
        logger.error("[WA-Campaign] recipient details failed (%s)", exc_summary(e))
        raise HTTPException(503, "Campaign data is temporarily unavailable (recipient details)")
    return {"items": [_row(r, _consent_now(r.get("phone"))) for r in rows], "total": total, "page": page,
            "page_size": page_size, "pages": pages, "state": state or None}
