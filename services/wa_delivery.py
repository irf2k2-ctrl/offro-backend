"""
services/wa_delivery.py
-----------------------
Phase 2F: WhatsApp delivery tracking for campaign recipients, and the campaign report numbers.

Flow: Meta -> POST /webhook (Phase 1: signature check, duplicate-event claim, per-handler progress, retry) ->
`routers.webhook._STATUS_HANDLERS` -> `handle_status` (registered once at startup by `register()`). The Phase 1 webhook code is
NOT changed; this module only adds a handler. The worker (Phase 2E) stores the Meta message id on the recipient when the API
accepts the message; this module maps later status events to that recipient by that id.

Rules:
  * An API acceptance is NOT delivery: a recipient is `sent` (accepted by Meta) until a `delivered` / `read` event arrives.
  * Statuses only move FORWARD: sent(2) < delivered(3) < read(4). A late `sent` after `delivered`, a duplicate, or an older
    event can never overwrite a newer state (conditional atomic update on delivery_rank + event time).
  * `failed` applies while the recipient has not been delivered; a later-timestamped delivered/read may still supersede it.
  * Unknown message ids (chat replies, other products, other environments) are ignored safely. If the id is unknown but a
    recipient with that number is still `sending`, the worker has not saved the id yet: the handler raises so the Phase 1
    webhook asks Meta to redeliver (bounded by Phase 1's MAX_ATTEMPTS).
  * Only the error CODE of a failure is stored; error text, numbers and tokens are never logged.
  * Handlers are idempotent: the same event applied twice changes nothing.
"""
import logging
from datetime import datetime, timezone

from services import wa_campaign_manager as M
from services import wa_consent as C
from services.wa_webhook_security import exc_summary, mask_phone

logger = logging.getLogger("offro.wa_campaigns.delivery")

RANK = {"sent": 2, "delivered": 3, "read": 4}
REPORT_STATES = ("queued", "sending", "sent", "delivered", "read", "failed", "cancelled", "uncertain")


class DeliveryPending(Exception):
    """The status arrived before the worker saved the message id: ask for a redelivery."""


def _event_time(ts) -> datetime:
    try:
        return datetime.utcfromtimestamp(int(str(ts).strip()))
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.utcnow()


def _error_code(raw):
    try:
        c = (raw.get("errors") or [{}])[0].get("code")
        return c if isinstance(c, int) and not isinstance(c, bool) else None
    except Exception:
        return None


def handle_status(msg_id="", recipient="", status="", timestamp="", raw=None, **_ignored) -> str:
    """Apply one Meta status event. Returns a label (for tests / logs): applied | stale | ignored_unknown | ignored_status."""
    raw = raw if isinstance(raw, dict) else {}
    if not msg_id or not isinstance(msg_id, str):
        return "ignored_unknown"
    status = str(status or "").lower()
    if status not in RANK and status != "failed":
        return "ignored_status"                               # e.g. 'deleted', 'warning': not a delivery state
    when = _event_time(timestamp)
    coll = M._db()[M.RECIPIENTS]
    rec = coll.find_one({"message_id": msg_id}, {"_id": 1, "state": 1})
    if not rec:
        phone = C.normalize_phone(recipient, whatsapp_id=True) if recipient else ""
        if phone and coll.count_documents({"phone": phone, "state": "sending", "send_started": True}, limit=1):
            raise DeliveryPending()                           # id not saved yet: let Phase 1 retry the webhook
        logger.info("[WA-Delivery] status for an unknown message id ignored (%s, to=%s)", status, mask_phone(recipient))
        return "ignored_unknown"
    now = datetime.utcnow()
    if status == "failed":
        res = coll.update_one({"_id": rec["_id"], "delivery_rank": {"$lt": 3}, "state": {"$in": ["sent", "failed"]},
                               "$or": [{"status_ts": {"$exists": False}}, {"status_ts": {"$lte": when}}]},
                              {"$set": {"state": "failed", "error": "delivery_failed", "error_code": _error_code(raw),
                                        "status_ts": when, "updated_at": now}})
    else:
        r = RANK[status]
        res = coll.update_one({"_id": rec["_id"], "delivery_rank": {"$lt": r}, "state": {"$in": ["sent", "delivered", "read", "failed"]},
                               "$or": [{"status_ts": {"$exists": False}}, {"status_ts": {"$lte": when}}]},
                              {"$set": {"state": status, "delivery_rank": r, "status_ts": when, "updated_at": now},
                               "$unset": {"error": "", "error_code": ""}})
    label = "applied" if res.modified_count == 1 else "stale"
    logger.info("[WA-Delivery] status %s %s", status, label)
    return label


def register() -> bool:
    """Add the handler to the Phase 1 webhook (idempotent). Passive: it only updates recipients that carry a matching id."""
    from routers import webhook
    if handle_status not in webhook._STATUS_HANDLERS:
        webhook._STATUS_HANDLERS.append(handle_status)
    return True


def campaign_stats(campaign_id) -> dict:
    """Report numbers aggregated from the recipient records themselves (no separate counters that could drift).
    Invariant: total == sum of every bucket. `accepted` = sent + delivered + read (what Meta accepted); API acceptance is not
    delivery, so `sent` counts only recipients with no delivery event yet."""
    cid = str(campaign_id)
    counts = {s: 0 for s in REPORT_STATES + ("snapshot",)}
    other = 0
    for row in M._db()[M.RECIPIENTS].aggregate([{"$match": {"campaign_id": cid}}, {"$group": {"_id": "$state", "n": {"$sum": 1}}}]):
        if row["_id"] in counts:
            counts[row["_id"]] = row["n"]
        else:
            other += row["n"]
    total = sum(counts.values()) + other
    cancel_reasons = {}
    for row in M._db()[M.RECIPIENTS].aggregate([{"$match": {"campaign_id": cid, "state": "cancelled"}},
                                                {"$group": {"_id": "$cancel_reason", "n": {"$sum": 1}}}]):
        cancel_reasons[str(row["_id"] or "unknown")] = row["n"]
    pending_retry = M._db()[M.RECIPIENTS].count_documents({"campaign_id": cid, "state": "queued", "attempts": {"$gt": 0}})
    return {"total": total, **counts, "other": other, "accepted": counts["sent"] + counts["delivered"] + counts["read"],
            "pending_retry": pending_retry, "cancelled_by_reason": cancel_reasons,
            "consistent": total == sum(counts.values()) + other}
