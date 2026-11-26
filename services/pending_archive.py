"""services/pending_archive.py - archive of old, abandoned PENDING subscriptions (Admin -> Payments -> Archived Pending Transactions).

What it does
  1. ARCHIVE: a `subscriptions` document with status == "pending" that is older than PENDING_ARCHIVE_AFTER_DAYS is MOVED (copied, then
     removed) to the `subscriptions_archive` collection. The copy keeps every original field and value (status stays "pending"); it only
     gains `archived_at`. Age = `updated_at` if present (a reused influencer order is touched there), else `created_at`. A document whose
     age cannot be read is never archived.
  2. PURGE: an archived document is permanently deleted PENDING_ARCHIVE_RETENTION_DAYS after its `archived_at`.
  3. MANUAL DELETE: `delete_archived(ids)` permanently deletes the named ARCHIVED documents - nothing else.

What it NEVER touches
  * any status other than "pending" (paid, active, failed, cancelled, ...),
  * a pending document that carries `razorpay_order_id` OR `razorpay_payment_id`: the gateway may have captured the money while the app-side
    verification never finished, so "pending" is not proof of "unpaid". Those stay in the active Payments list at any age,
  * a document with `paid_at` (defensive), invoices, banner_orders, product_orders, stores, or any payment status.

Safe to repeat AND safe with several instances at once (no MongoDB transaction needed; see archive_one): the copy is written with
$setOnInsert, the original is removed by one atomic find_one_and_delete under a guard (still pending, no Razorpay ids, same timestamps),
only the worker that removed the original finalises the record, and a worker deletes an archive record only if it created it AND the
original is still live (it changed). A worker that finds the original already gone never touches the archive record.

Configuration (environment; invalid or non-positive values fall back to the default, so 0 can never archive everything):
  PENDING_ARCHIVE_AFTER_DAYS       default 90   age at which a pending subscription is archived
  PENDING_ARCHIVE_RETENTION_DAYS   default 90   days an archived record is kept, counted from archived_at
  PENDING_ARCHIVE_INTERVAL_HOURS   default 24   how often the background job runs
  PENDING_ARCHIVE_ENABLED          default 1    set to 0 / false / off to stop the background job (manual delete still works)
"""
import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from bson import ObjectId

logger = logging.getLogger("offro.payments.archive")

SOURCE = "subscriptions"
ARCHIVE = "subscriptions_archive"
RAZORPAY_FIELDS = ("razorpay_order_id", "razorpay_payment_id")
MAX_MANUAL_DELETE = 500
_FIRST_RUN_DELAY_SECONDS = 300          # let the app finish starting before the first pass


class _LiveDb:
    """The CURRENT `database.db`, looked up at call time (see services.dashboard_country)."""
    def __getattr__(self, name):
        import database
        return getattr(database.db, name)

    def __getitem__(self, name):
        import database
        return database.db[name]


db = _LiveDb()


# ── configuration ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _int_env(name: str, default: int) -> int:
    try:
        v = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def archive_after_days() -> int:
    return _int_env("PENDING_ARCHIVE_AFTER_DAYS", 90)


def retention_days() -> int:
    return _int_env("PENDING_ARCHIVE_RETENTION_DAYS", 90)


def interval_hours() -> int:
    return _int_env("PENDING_ARCHIVE_INTERVAL_HOURS", 24)


def enabled() -> bool:
    return str(os.environ.get("PENDING_ARCHIVE_ENABLED", "1")).strip().lower() not in ("0", "false", "no", "off")


# ── eligibility ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def has_razorpay_ids(doc: dict) -> bool:
    return any(not _blank((doc or {}).get(f)) for f in RAZORPAY_FIELDS)


def _no_razorpay_query() -> dict:
    """Mongo side of the Razorpay exclusion: both identifiers missing, null or empty."""
    return {"$and": [{f: {"$in": [None, ""]}} for f in RAZORPAY_FIELDS]}


def eligibility_query() -> dict:
    """Exactly: status pending, no Razorpay order/payment id, not paid. Age is checked in Python (created_at may be a datetime or an
    ISO string in older documents)."""
    return {"status": "pending", "paid_at": {"$in": [None, ""]}, **_no_razorpay_query()}


def _parse(value) -> Optional[datetime]:
    from services import store_status as _ss
    d = _ss.parse_dt(value)
    if d is not None and getattr(d, "tzinfo", None) is not None:
        d = d.replace(tzinfo=None) - (d.utcoffset() or timedelta(0))
    return d


def record_time(doc: dict) -> Optional[datetime]:
    """The ONE timestamp used for ageing: updated_at, else created_at."""
    return _parse(doc.get("updated_at")) or _parse(doc.get("created_at"))


def is_archivable(doc: dict, now: datetime, days: int) -> bool:
    if (doc or {}).get("status") != "pending" or has_razorpay_ids(doc) or not _blank(doc.get("paid_at")):
        return False
    t = record_time(doc)
    return t is not None and t <= now - timedelta(days=days)


# ── archive / purge ───────────────────────────────────────────────────────────────────────────────────────────────────
def _delete_guard(doc: dict) -> dict:
    """The ONLY condition under which the live original may be removed: still pending, still no Razorpay ids, still unpaid, and its
    age timestamps are exactly what was judged old (a reused order that was touched meanwhile no longer matches)."""
    return {"_id": doc["_id"], **eligibility_query(), "updated_at": doc.get("updated_at"), "created_at": doc.get("created_at")}


def archive_one(doc: dict, now: datetime, days: int) -> str:
    """Move ONE subscription to the archive. Safe when several workers/instances do this at the same moment (no MongoDB transaction needed).

    Ownership rules (this is what makes it safe):
      * COPY FIRST, with $setOnInsert: whoever creates the archive record knows it (`upserted_id`); nobody ever overwrites it.
      * REMOVE the original with ONE atomic find_one_and_delete under _delete_guard(): exactly one worker can win it. The winner (and
        only the winner) rewrites the archive copy with the document it really removed, so the archive holds the final original
        fields + archived_at, nothing else added.
      * A worker that LOSES the delete never deletes the archive record just because the original is gone - that means another worker
        moved it, and the record is theirs. A worker withdraws an archive record ONLY if it created that record AND the original is still
        in `subscriptions` (i.e. it changed to paid/failed/... or got a Razorpay id, so it must stay an active payment).
    Returns: archived | skipped_changed | skipped_concurrent.
    """
    _id = doc["_id"]
    copy = dict(doc)
    copy["archived_at"] = now
    res = db[ARCHIVE].update_one({"_id": _id}, {"$setOnInsert": copy}, upsert=True)
    created_here = getattr(res, "upserted_id", None) is not None
    existing = db[ARCHIVE].find_one({"_id": _id}, {"archived_at": 1})
    archived_at = (existing or {}).get("archived_at") or now          # keep the first archive date (crash recovery / another worker)

    removed = db[SOURCE].find_one_and_delete(_delete_guard(doc))
    if removed is not None:
        # We alone removed the original: make the archive record exactly that final document (+ archived_at), even if it was
        # edited after our snapshot or the copy vanished meanwhile.
        final = dict(removed)
        final["archived_at"] = archived_at
        db[ARCHIVE].replace_one({"_id": _id}, final, upsert=True)
        return "archived"

    if db[SOURCE].find_one({"_id": _id}, {"_id": 1}) is None:
        return "skipped_concurrent"        # the original is gone: another worker moved it. NOT ours to delete.
    # The original is still there but no longer qualifies (paid / active / failed / cancelled / Razorpay id / touched).
    if created_here:
        db[ARCHIVE].delete_one({"_id": _id})                        # undo our own copy; the payment stays in Payments
    return "skipped_changed"


def archive_old_pending(now: Optional[datetime] = None, days: Optional[int] = None) -> dict:
    now = now or datetime.utcnow()
    days = days or archive_after_days()
    out = {"scanned": 0, "archived": 0, "skipped_not_old": 0, "skipped_changed": 0, "skipped_concurrent": 0, "errors": 0}
    for doc in list(db[SOURCE].find(eligibility_query())):
        out["scanned"] += 1
        if not is_archivable(doc, now, days):
            out["skipped_not_old"] += 1
            continue
        try:
            out[archive_one(doc, now, days)] += 1
        except Exception as e:                                  # one bad document never stops the pass
            out["errors"] += 1
            logger.error("pending archive: could not archive %s (%s)", doc.get("_id"), type(e).__name__)
    return out


def reconcile_orphans(chunk: int = 500) -> int:
    """Remove an archive record whose original is STILL in `subscriptions` as something that must stay active (paid, Razorpay id, ...):
    left behind only if a worker died between copying and withdrawing. Nothing is lost: the live original is the authority.
    An original that still qualifies is left alone - the normal pass finishes that move."""
    removed, ids = 0, []

    def flush():
        nonlocal removed, ids
        if not ids:
            return
        for live in db[SOURCE].find({"_id": {"$in": ids}}):
            if live.get("status") != "pending" or has_razorpay_ids(live) or not _blank(live.get("paid_at")):
                removed += int(db[ARCHIVE].delete_one({"_id": live["_id"]}).deleted_count or 0)
        ids = []

    for d in db[ARCHIVE].find({}, {"_id": 1}):
        ids.append(d["_id"])
        if len(ids) >= chunk:
            flush()
    flush()
    return removed


def purge_expired_archive(now: Optional[datetime] = None, days: Optional[int] = None) -> int:
    now = now or datetime.utcnow()
    days = days or retention_days()
    res = db[ARCHIVE].delete_many({"archived_at": {"$lte": now - timedelta(days=days)}})
    return int(res.deleted_count or 0)


def run_cleanup(now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    arch = archive_old_pending(now)
    reconciled = reconcile_orphans()
    purged = purge_expired_archive(now)
    result = {**arch, "reconciled": reconciled, "purged": purged, "archive_after_days": archive_after_days(), "retention_days": retention_days()}
    logger.info("pending archive run: scanned=%s archived=%s skipped_not_old=%s skipped_changed=%s skipped_concurrent=%s reconciled=%s "
                "errors=%s purged=%s (archive after %sd, keep %sd)", result["scanned"], result["archived"], result["skipped_not_old"],
                result["skipped_changed"], result["skipped_concurrent"], result["reconciled"], result["errors"], result["purged"],
                result["archive_after_days"], result["retention_days"])
    return result


# ── manual delete (archive only) ──────────────────────────────────────────────────────────────────────────────────────
def clean_ids(ids: Iterable) -> List[ObjectId]:
    out, seen = [], set()
    for i in ids or []:
        try:
            o = ObjectId(str(i))
        except Exception:
            continue
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


def delete_archived(ids: Iterable, extra_query: Optional[dict] = None) -> int:
    """Permanently delete the named documents from the ARCHIVE collection. This function cannot reach `subscriptions`."""
    oids = clean_ids(ids)
    if not oids:
        return 0
    q = {"_id": {"$in": oids}}
    if extra_query:
        q = {"$and": [q, extra_query]}
    return int(db[ARCHIVE].delete_many(q).deleted_count or 0)


# ── indexes + background job ──────────────────────────────────────────────────────────────────────────────────────────
def ensure_indexes_safe() -> bool:
    try:
        db[ARCHIVE].create_index([("archived_at", -1)], name="subscriptions_archive_archived_at", background=True)
        return True
    except Exception as e:
        logger.error("pending archive index creation skipped (%s)", type(e).__name__)
        return False


_STATE = {"thread": None, "stop": threading.Event()}
_LOCK = threading.Lock()


def _loop(stop: threading.Event):
    if stop.wait(_FIRST_RUN_DELAY_SECONDS):
        return
    while not stop.is_set():
        try:
            if enabled():
                run_cleanup()
        except Exception as e:                                  # never let the job die, never touch payment processing
            logger.error("pending archive run failed (%s)", type(e).__name__)
        if stop.wait(interval_hours() * 3600):
            return


def start_if_enabled() -> bool:
    if not enabled():
        logger.info("pending archive job disabled (PENDING_ARCHIVE_ENABLED)")
        return False
    with _LOCK:
        if _STATE["thread"] and _STATE["thread"].is_alive():
            return True
        _STATE["stop"] = threading.Event()
        t = threading.Thread(target=_loop, args=(_STATE["stop"],), name="pending-archive", daemon=True)
        _STATE["thread"] = t
        t.start()
    return True
