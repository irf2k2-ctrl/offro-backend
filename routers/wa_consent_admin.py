"""
routers/wa_consent_admin.py
---------------------------
Phase 2G read-only consent listing, mounted by routers/wa_campaigns.py. NOTHING here changes consent (admin-evidenced consent is
recorded by the existing POST /consent; STOP / START arrive through the webhook).

    GET  /consents          view   contacts: masked number, current status, how/when consent was given or withdrawn
    GET  /consent-events    view   the append-only STOP / START / admin-consent ledger, newest first

Privacy: numbers are ALWAYS masked (last 4 digits). Meta message ids are never returned (a wamid encodes the number); admin ids are never
returned (name and role only). `evidence_ref` is the free-text reference the recording admin typed (e.g. a form number).
"""
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from routers.dashboard_auth import require_permission
from services import wa_consent as C
from services.wa_webhook_security import exc_summary

logger = logging.getLogger("offro.wa_campaigns.consent")

MODULE = "WhatsApp Campaigns"
router = APIRouter(tags=["WhatsApp Campaigns - Consent"])

CONTACT_STATUSES = ("opted_in", "opted_out")
EVENT_TYPES = ("opt_in", "opt_out", "admin_opt_in", "registration_opt_in")
_LAST4 = re.compile(r"^[0-9]{4}$")


def _iso(v):
    return v.isoformat() + "Z" if isinstance(v, datetime) else (str(v) if v else None)


def _masked(last4):
    return "***" + last4 if last4 and len(last4) == 4 else "***"


def _evidence(ev):
    """Safe subset of an evidence block (the stored `last_consent` / ledger fields). No ids, no message ids."""
    ev = ev if isinstance(ev, dict) else {}
    out = {"channel": ev.get("source")}
    if ev.get("source") == "admin_recorded":
        out.update(consent_source=ev.get("consent_source"), evidence_ref=ev.get("evidence_ref"), consented_at=ev.get("consented_at"),
                   recorded_at=ev.get("recorded_at"), recorded_by=ev.get("admin_name") or None, recorded_by_role=ev.get("admin_role") or None,
                   attested=bool(ev.get("attested")))
    elif ev.get("source") == "registration_checkbox":
        # wording + version are shown so the evidence is reviewable; no account id, no full number.
        out.update(consent_source=ev.get("consent_source"), consent_version=ev.get("consent_version"),
                   consent_text=ev.get("consent_text"), consented_at=ev.get("consented_at"), recorded_at=ev.get("recorded_at"))
    else:
        out.update(keyword=ev.get("text"))
    return out


def _filters(last4):
    if last4 and not _LAST4.match(last4):
        raise HTTPException(400, {"code": "invalid_last4", "message": "last4 must be exactly four digits."})
    return {"phone_last4": last4} if last4 else {}


def _page(coll, q, page, page_size, sort):
    total = coll.count_documents(q)
    pages = max(1, -(-total // page_size))
    page = min(page, pages)
    return list(coll.find(q).sort(sort).skip((page - 1) * page_size).limit(page_size)), total, page, pages


@router.get("/consents")
async def list_consents(status: str = "", last4: str = "", page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                        user: dict = Depends(require_permission(MODULE, "view"))):
    if status and status not in CONTACT_STATUSES:
        raise HTTPException(400, {"code": "invalid_status", "message": "status must be opted_in or opted_out."})
    q = {**_filters(last4), **({"status": status} if status else {})}
    try:
        rows, total, page, pages = _page(C.db[C.CONTACTS], q, page, page_size, [("updated_at", -1), ("_id", 1)])
    except Exception as e:
        logger.error("[WA-Campaign] consent listing failed (%s)", exc_summary(e))
        raise HTTPException(503, "Consent data is temporarily unavailable")
    items = [{"phone_masked": _masked(r.get("phone_last4")), "status": r.get("status"), "opted_in_at": _iso(r.get("opted_in_at")),
              "opted_out_at": _iso(r.get("opted_out_at")), "updated_at": _iso(r.get("updated_at")),
              "last_event": _evidence(r.get("last_consent"))} for r in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages, "status": status or None,
            "admin_consent_sources": sorted(C.ADMIN_CONSENT_SOURCES)}


@router.get("/consent-events")
async def list_consent_events(event: str = "", last4: str = "", page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                              user: dict = Depends(require_permission(MODULE, "view"))):
    if event and event not in EVENT_TYPES:
        raise HTTPException(400, {"code": "invalid_event", "message": "event must be opt_in, opt_out, admin_opt_in or registration_opt_in."})
    q = {**_filters(last4), **({"event": event} if event else {})}
    try:
        rows, total, page, pages = _page(C.db[C.EVENTS], q, page, page_size, [("at", -1), ("_id", 1)])
    except Exception as e:
        logger.error("[WA-Campaign] consent events failed (%s)", exc_summary(e))
        raise HTTPException(503, "Consent data is temporarily unavailable")
    items = [{"phone_masked": _masked(r.get("phone_last4")), "event": r.get("event"), "at": _iso(r.get("at")), **_evidence(r)} for r in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages, "event": event or None}
