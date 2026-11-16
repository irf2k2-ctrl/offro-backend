"""
services/wa_campaign_audit.py
-----------------------------
Append-only audit trail for WhatsApp campaign administration (Phase 2A).

Collection wa_campaign_audit - one document per action:
    at, action, outcome, admin {id, name, role}, campaign_id, target, details
Rules:
  * APPEND-ONLY: this module offers no update or delete function, and nothing else in the code base writes here.
  * Never stores phone numbers: values that look like numbers inside `details` are masked to the last 4 digits and
    `target` carries only phone_last4 / a SHA-256 hash. No tokens, secrets or message bodies.
  * record(..., strict=True) RAISES AuditWriteError when the entry could not be stored - used for actions that must
    not happen unaudited (granting consent, enabling sending). strict=False never raises (best effort).
  * No TTL is set: the retention period is a pending decision (see docs/WHATSAPP_PHASE2A_FOUNDATIONS.md).
"""
import hashlib
import logging
from datetime import datetime

from database import db
from services.wa_webhook_security import exc_summary, redact

logger = logging.getLogger("offro.wa_campaigns.audit")

AUDIT = "wa_campaign_audit"
_MAX_STR = 300
_MAX_ITEMS = 30


class AuditWriteError(Exception):
    """An audit entry could not be stored."""


def ensure_indexes():
    db[AUDIT].create_index([("at", -1)], name="wa_campaign_audit_at")
    db[AUDIT].create_index([("action", 1), ("at", -1)], name="wa_campaign_audit_action_at")
    db[AUDIT].create_index([("campaign_id", 1), ("at", -1)], name="wa_campaign_audit_campaign_at")


def ensure_indexes_safe() -> bool:
    """Startup wrapper: never raises; logs only the exception type and location."""
    try:
        ensure_indexes()
        return True
    except Exception as e:
        logger.error("[WA-Campaign] audit index creation skipped (%s)", exc_summary(e))
        return False


def phone_ref(canonical_phone: str) -> dict:
    """Privacy-safe reference to a number for audit targets."""
    p = str(canonical_phone or "")
    return {"phone_last4": p[-4:], "phone_hash": hashlib.sha256(p.encode()).hexdigest()}


def admin_ref(user: dict) -> dict:
    """Identity of the acting admin, taken from the authenticated session (never from a request body)."""
    user = user or {}
    role = user.get("role")
    role_name = role.get("role_name", "") if isinstance(role, dict) else str(user.get("role_name", "") or "")
    return {"id": str(user.get("_id", user.get("id", ""))), "name": str(user.get("full_name", user.get("name", "")))[:80],
            "role": str(role_name)[:60]}


def _clean(value, depth=0):
    if isinstance(value, str):
        return redact(value, _MAX_STR)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if depth < 3 and isinstance(value, dict):
        return {str(k)[:60]: _clean(v, depth + 1) for k, v in list(value.items())[:_MAX_ITEMS]}
    if depth < 3 and isinstance(value, (list, tuple)):
        return [_clean(v, depth + 1) for v in list(value)[:_MAX_ITEMS]]
    return redact(repr(value), 80)


def record(action: str, admin: dict, outcome: str = "ok", campaign_id=None, target=None, details=None,
           strict: bool = False) -> bool:
    doc = {"at": datetime.utcnow(), "action": str(action)[:80], "outcome": str(outcome)[:40],
           "admin": admin if isinstance(admin, dict) else admin_ref(admin),
           "campaign_id": str(campaign_id) if campaign_id else None,
           "target": _clean(target) if target else None, "details": _clean(details) if details else None}
    try:
        res = db[AUDIT].insert_one(doc)
        if not res.acknowledged:
            raise AuditWriteError("audit write not acknowledged")
        return True
    except Exception as e:
        logger.error("[WA-Campaign] audit write failed for action=%s (%s)", doc["action"], exc_summary(e))
        if strict:
            raise AuditWriteError("audit write failed")
        return False


def list_entries(limit: int = 50, action: str = "", campaign_id: str = "", before=None) -> list:
    """Newest first. `before` is an entry id for cursor paging. Documents are returned with string ids."""
    from bson import ObjectId
    q = {}
    if action:
        q["action"] = action
    if campaign_id:
        q["campaign_id"] = campaign_id
    if before:
        try:
            q["_id"] = {"$lt": ObjectId(str(before))}
        except Exception:
            pass
    out = []
    for d in db[AUDIT].find(q).sort("_id", -1).limit(max(1, min(int(limit), 100))):
        d["_id"] = str(d["_id"])
        if isinstance(d.get("at"), datetime):
            d["at"] = d["at"].isoformat() + "Z"
        out.append(d)
    return out
