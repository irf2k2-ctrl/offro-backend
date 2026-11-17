"""
services/wa_template_test_delivery.py
-------------------------------------
Delivery status for messages sent by POST /admin/whatsapp/template-test, saved on the matching `whatsapp_template_tests` record.

Flow: Meta -> POST /webhook (signature check, duplicate-event claim, per-handler progress and retry; all unchanged) -> `routers.webhook._STATUS_HANDLERS`
-> `handle_status` here (registered at startup next to the campaign delivery handler, see services/wa_delivery.register()).

Fields written on the record (new, additive; the existing `status` field and the send limits that read it are never touched):
    delivery_status   sent | delivered | read | failed
    delivery_rank     2 sent, 3 delivered, 4 read      (failed leaves the rank alone)
    delivery_ts       time of the event that set it
    error_code        Meta's numeric failure code (failed only; cleared if a later delivered/read supersedes the failure). NO error text is stored.
    message_ref       safe fingerprint of the message id (see services/wa_webhook_diag.msg_ref)
    delivery_events   the last 10 applied events [{status, at, code}]

Rules (same as the campaign delivery tracker):
  * Statuses only move FORWARD: sent < delivered < read. A late `sent` after `delivered`, a duplicate or an older event never overwrites a newer state.
  * `failed` applies only while the message has not been delivered. A stored `failed` is NEVER overwritten by a (late) `sent`; a delivered/read
    event supersedes it only if its timestamp is strictly newer (the error_code is then cleared). A second `failed` is ignored.
  * Unknown message ids (campaign messages, chat replies, other environments) are ignored safely.
  * Race: Meta can post the first `sent` before the send call has returned and the record has its message id. If an `attempting` record for the
    SAME recipient (matched by hash) exists without an id, DeliveryPending is raised so the webhook asks Meta to redeliver (bounded by its MAX_ATTEMPTS).
  * Idempotent: the same event applied twice changes nothing.
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta

from database import db
from services.wa_webhook_diag import error_code, msg_ref

logger = logging.getLogger("offro.wa_template_test_delivery")

AUDIT = "whatsapp_template_tests"          # same collection as routers/wa_template_test.AUDIT
RANK = {"sent": 2, "delivered": 3, "read": 4}
PENDING_WINDOW = timedelta(minutes=5)


class DeliveryPending(Exception):
    """The status arrived before the send call saved the message id: ask Meta to redeliver."""


def _event_time(ts) -> datetime:
    try:
        return datetime.utcfromtimestamp(int(str(ts).strip()))
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.utcnow()


def handle_status(msg_id="", recipient="", status="", timestamp="", raw=None, **_ignored) -> str:
    """Apply one Meta status event. Returns applied | stale | ignored_unknown | ignored_status (for tests / logs)."""
    raw = raw if isinstance(raw, dict) else {}
    if not msg_id or not isinstance(msg_id, str):
        return "ignored_unknown"
    status = str(status or "").lower()
    if status not in RANK and status != "failed":
        return "ignored_status"                                       # 'deleted', 'warning', ...: not a delivery state
    coll = db[AUDIT]
    rec = coll.find_one({"message_id": msg_id}, {"_id": 1})
    if not rec:
        digits = re.sub(r"\D", "", str(recipient or ""))
        if digits and coll.count_documents({"phone_hash": hashlib.sha256(digits.encode()).hexdigest(), "status": "attempting",
                                            "message_id": {"$exists": False},
                                            "created_at": {"$gte": datetime.utcnow() - PENDING_WINDOW}}, limit=1):
            raise DeliveryPending()                                   # the send has not saved its id yet
        return "ignored_unknown"
    when = _event_time(timestamp)
    now = datetime.utcnow()
    code = error_code(raw) if status == "failed" else None
    ts_ok = {"$or": [{"delivery_ts": {"$exists": False}}, {"delivery_ts": {"$lte": when}}]}
    event = {"status": status, "at": when, "code": code}
    history = {"$each": [event], "$slice": -10}
    if status == "failed":
        res = coll.update_one(
            {"_id": rec["_id"], "$and": [ts_ok,
                                         {"$or": [{"delivery_rank": {"$exists": False}}, {"delivery_rank": {"$lt": 3}}]},
                                         {"$or": [{"delivery_status": {"$exists": False}}, {"delivery_status": {"$ne": "failed"}}]}]},
            {"$set": {"delivery_status": "failed", "error_code": code, "delivery_ts": when, "message_ref": msg_ref(msg_id), "updated_at": now},
             "$push": {"delivery_events": history}})
    else:
        r = RANK[status]
        # A stored `failed` is final against a late `sent` (same or older rank-2 information), and is superseded by delivered/read
        # ONLY when that event is strictly NEWER than the failure.
        if status == "sent":
            after_failed = {"delivery_status": {"$ne": "failed"}}
        else:
            after_failed = {"$or": [{"delivery_status": {"$ne": "failed"}}, {"delivery_ts": {"$lt": when}}]}
        res = coll.update_one(
            {"_id": rec["_id"], "$and": [ts_ok, after_failed, {"$or": [{"delivery_rank": {"$exists": False}}, {"delivery_rank": {"$lt": r}}]}]},
            {"$set": {"delivery_status": status, "delivery_rank": r, "delivery_ts": when, "message_ref": msg_ref(msg_id), "updated_at": now},
             "$unset": {"error_code": ""},
             "$push": {"delivery_events": history}})
    label = "applied" if res.modified_count == 1 else "stale"
    logger.info("[WA-TemplateTest] delivery %s %s", status, label)
    return label


def register() -> bool:
    """Add the handler to the webhook's status handlers (idempotent). Passive: it only touches records that carry the matching message id."""
    from routers import webhook
    if handle_status not in webhook._STATUS_HANDLERS:
        webhook._STATUS_HANDLERS.append(handle_status)
    return True


def lookup(ref: str = ""):
    """The delivery picture of one template-test message, found ONLY by its log fingerprint (m_ + 12 hex). None if not found.
    A full message id is deliberately not accepted. Returns only safe fields (never the message id, the phone hash or the consent note)."""
    if not (isinstance(ref, str) and re.fullmatch(r"m_[0-9a-f]{12}", ref.strip())):
        return None
    ref = ref.strip()
    rec = db[AUDIT].find_one({"message_ref": ref})
    if not rec:
        return None
    return {"found": True, "send_status": rec.get("status"), "delivery_status": rec.get("delivery_status"), "error_code": rec.get("error_code"),
            "to_last4": rec.get("phone_last4"), "sent_record_at": rec.get("created_at"), "delivery_at": rec.get("delivery_ts"),
            "message_ref": ref,
            "events": [{"status": e.get("status"), "at": e.get("at"), "code": e.get("code")} for e in (rec.get("delivery_events") or [])]}
