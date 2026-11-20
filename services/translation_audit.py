"""services/translation_audit.py - append-only audit trail for the translation module (Phase G).

Collection `translation_audit`, one document per event:
    at, action, outcome (started | ok | failed | denied), environment, admin {id, name, role, system_super_admin},
    lang, track, key, version, old_hash, new_hash, details, ip
Rules
  * APPEND-ONLY: this module offers no update or delete, and nothing else writes to the collection. No TTL (unlike
    activity_logs, which is purged after 15 days).
  * Never stores translation text, secrets, signatures or tokens: only hashes, counts, versions and short codes.
  * record(..., strict=True) RAISES AuditWriteError when the entry cannot be stored. Every sensitive operation writes a strict
    "started" entry BEFORE it changes anything, so a sensitive operation can never happen unaudited. Denials and ordinary draft
    edits are best effort (a failed audit write is logged at ERROR, never swallowed silently); a denial is still a denial.
"""
import logging
from datetime import datetime

from database import db

logger = logging.getLogger("offro.translations.audit")

AUDIT = "translation_audit"
_MAX_STR = 200
_MAX_ITEMS = 30


class AuditWriteError(Exception):
    """An audit entry could not be stored."""


def ensure_indexes():
    db[AUDIT].create_index([("at", -1)], name="translation_audit_at")
    db[AUDIT].create_index([("action", 1), ("at", -1)], name="translation_audit_action_at")
    db[AUDIT].create_index([("lang", 1), ("at", -1)], name="translation_audit_lang_at")


def ensure_indexes_safe() -> bool:
    try:
        ensure_indexes()
        return True
    except Exception as e:
        logger.error("translation audit index creation skipped (%s)", type(e).__name__)
        return False


def admin_ref(user: dict) -> dict:
    """Identity of the acting admin taken from the authenticated session (never from a request body)."""
    user = user or {}
    role = user.get("role") if isinstance(user.get("role"), dict) else {}
    return {
        "id": str(user.get("_id", user.get("id", "")))[:40],
        "name": str(user.get("full_name", user.get("name", "")))[:80],
        "role": str(role.get("role_name", ""))[:60],
        "system_super_admin": bool(role.get("role_name") == "Super Admin" and role.get("is_system_role") is True),
    }


def _clean(value, depth=0):
    if isinstance(value, str):
        return value[:_MAX_STR]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if depth < 3 and isinstance(value, dict):
        return {str(k)[:60]: _clean(v, depth + 1) for k, v in list(value.items())[:_MAX_ITEMS]}
    if depth < 3 and isinstance(value, (list, tuple)):
        return [_clean(v, depth + 1) for v in list(value)[:_MAX_ITEMS]]
    return repr(value)[:80]


def record(action: str, admin, outcome: str = "ok", *, environment: str = "", lang: str = "", track: str = "", key: str = "",
           version=None, old_hash: str = "", new_hash: str = "", details=None, ip: str = "", strict: bool = False) -> bool:
    doc = {
        "at": datetime.utcnow(), "action": str(action)[:80], "outcome": str(outcome)[:20],
        "environment": str(environment)[:20],
        "admin": admin if isinstance(admin, dict) else admin_ref(admin),
        "lang": str(lang)[:20], "track": str(track)[:10], "key": str(key)[:120],
        "version": version if isinstance(version, int) else None,
        "old_hash": str(old_hash)[:80], "new_hash": str(new_hash)[:80],
        "details": _clean(details) if details else None, "ip": str(ip)[:64],
    }
    try:
        res = db[AUDIT].insert_one(doc)
        if not res.acknowledged:
            raise AuditWriteError("audit write not acknowledged")
        return True
    except Exception as e:
        logger.error("translation audit write FAILED for action=%s outcome=%s (%s)", doc["action"], doc["outcome"], type(e).__name__)
        if strict:
            raise AuditWriteError("audit write failed")
        return False


def list_entries(limit: int = 50, action: str = "", lang: str = "", before=None) -> list:
    from bson import ObjectId
    q = {}
    if action:
        q["action"] = action
    if lang:
        q["lang"] = lang
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
