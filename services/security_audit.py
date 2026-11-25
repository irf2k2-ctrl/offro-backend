"""services/security_audit.py - PERMANENT, append-only audit trail for sensitive account changes and security actions (Phase 2, Stage 1, Task C).

Collection `security_audit`, one document per event:
    at, action, outcome (started | ok | failed | denied), admin {id, name, role}, target {type, id, name, country}, module,
    details (small, sanitised), ip

Rules
  * APPEND-ONLY: this module offers no update and no delete, no router exposes one, and nothing else writes to the collection.
  * NO expiry: there is no TTL index and nothing purges it (unlike `activity_logs`, which log_activity trims after 15 days; that
    behaviour is deliberately left exactly as it is).
  * NEVER stores secrets: OTP values, codes, tokens, PINs, passwords, authorisation headers and device (FCM) tokens are removed from
    `details` by key name, and any other long string is cut. Callers should still pass counts and ids, not private data.
  * record(..., strict=True) raises AuditWriteError when the entry cannot be stored, so a sensitive operation can be refused instead of
    happening unaudited. The default is best effort, and a failed write is logged at ERROR, never swallowed silently.
This is the same design as services/translation_audit.py (a separate trail for the translation module). It will also carry the later
Support/Admin-assisted account change records.
"""
import logging
from datetime import datetime

from database import db

logger = logging.getLogger("offro.security.audit")

AUDIT = "security_audit"
OUTCOMES = ("started", "ok", "failed", "denied")
_MAX_STR = 200
_MAX_ITEMS = 30
_SECRET_WORDS = ("otp", "token", "secret", "password", "passwd", "pin", "code", "authorization", "auth", "cookie", "fcm", "key", "signature")


class AuditWriteError(Exception):
    """An audit entry could not be stored."""


def ensure_indexes():
    db[AUDIT].create_index([("at", -1)], name="security_audit_at")
    db[AUDIT].create_index([("action", 1), ("at", -1)], name="security_audit_action_at")
    db[AUDIT].create_index([("target.id", 1), ("at", -1)], name="security_audit_target_at")


def ensure_indexes_safe() -> bool:
    try:
        ensure_indexes()
        return True
    except Exception as e:
        logger.error("security audit index creation skipped (%s)", type(e).__name__)
        return False


def admin_ref(user: dict) -> dict:
    """Identity of the acting admin taken from the authenticated session (never from a request body)."""
    user = user or {}
    role = user.get("role") if isinstance(user.get("role"), dict) else {}
    return {
        "id": str(user.get("_id", user.get("id", "")))[:40],
        "name": str(user.get("full_name", user.get("name", "")))[:80],
        "role": str(role.get("role_name") or user.get("role_name") or "")[:60],
    }


def _is_secret_key(k) -> bool:
    k = str(k).lower()
    return any(w in k for w in _SECRET_WORDS)


def _clean(value, depth=0):
    if isinstance(value, str):
        return value[:_MAX_STR]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if depth < 3 and isinstance(value, dict):
        return {str(k)[:60]: ("[redacted]" if _is_secret_key(k) else _clean(v, depth + 1)) for k, v in list(value.items())[:_MAX_ITEMS]}
    if depth < 3 and isinstance(value, (list, tuple)):
        return [_clean(v, depth + 1) for v in list(value)[:_MAX_ITEMS]]
    return repr(value)[:80]


def _target(t) -> dict:
    t = t if isinstance(t, dict) else {}
    return {"type": str(t.get("type") or "")[:30], "id": str(t.get("id") or "")[:60],
            "name": str(t.get("name") or "")[:80], "country": str(t.get("country") or "").upper()[:2]}


def record(action: str, admin, outcome: str = "ok", *, target=None, module: str = "", details=None, ip: str = "",
           strict: bool = False) -> bool:
    if outcome not in OUTCOMES:
        outcome = "failed"
    doc = {
        "at": datetime.utcnow(), "action": str(action)[:80], "outcome": outcome,
        "admin": admin if isinstance(admin, dict) else admin_ref(admin),
        "target": _target(target), "module": str(module)[:40],
        "details": _clean(details) if details else None, "ip": str(ip)[:64],
    }
    try:
        res = db[AUDIT].insert_one(doc)
        if not res.acknowledged:
            raise AuditWriteError("audit write not acknowledged")
        return True
    except Exception as e:
        logger.error("security audit write FAILED for action=%s outcome=%s (%s)", doc["action"], doc["outcome"], type(e).__name__)
        if strict:
            raise AuditWriteError("audit write failed")
        return False


def list_entries(limit: int = 50, action: str = "", outcome: str = "", target_id: str = "", country: str = "", before=None) -> list:
    """Newest first. `country` (restricted admins) keeps only entries whose target is in that country; entries with no country
    are hidden from a country-restricted reader (fail closed)."""
    from bson import ObjectId
    q = {}
    if action:
        q["action"] = action
    if outcome:
        q["outcome"] = outcome
    if target_id:
        q["target.id"] = str(target_id)
    if country:
        q["target.country"] = country
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
