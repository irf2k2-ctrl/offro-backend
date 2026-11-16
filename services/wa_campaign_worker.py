"""
services/wa_campaign_worker.py
------------------------------
Phase 2E: the MongoDB-backed campaign worker (bulk template sending) and the run controls (pause / resume / cancel).

EVERYTHING HERE IS OFF BY DEFAULT. Nothing runs at import. The loop thread is started only by
`start_worker_if_enabled()` (called from server startup) and only when WA_CAMPAIGN_WORKER_ENABLED is on. A message can leave
only when ALL of these hold at that very moment (each is re-read for every single message):

    WA_CAMPAIGN_WORKER_ENABLED   env   worker key, default OFF, refused on Production unless WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION
    WA_CAMPAIGN_SENDING_ENABLED  env   Phase 2A key  }  ST.sending_allowed(): both required, any error = OFF
    DB kill switch               db    Phase 2A switch}
    WA_CAMPAIGN_BULK_LIVE_ENABLED env  third key, only for campaigns whose run mode is "live" (default mode is "test")
    mode "test": only numbers on WA_CAMPAIGN_TEST_NUMBERS may receive anything; everybody else is marked cancelled.

Lifecycle (campaign): scheduled --(send_at reached, gates pass)--> running <--> paused ; running --> completed | cancelled.
Lifecycle (recipient, one document per number in wa_campaign_recipients):

    snapshot -> queued -> sending -> sent -> delivered -> read          (sent/delivered/read: Phase 2F webhook, forward only)
                  ^  |        |  \\-> failed           (confirmed rejection, or retries exhausted, or a webhook failure)
                  |  |        \\---> uncertain         (outcome unknown: NEVER retried automatically)
                  +--+ bounded retry with backoff       queued/snapshot -> cancelled (STOP, not a test number, campaign cancelled)

Exactly-once protection, in layers:
    1. recipient claim  : find_one_and_update(state=queued -> sending) - atomic; only one replica gets a recipient.
    2. lease            : every claim carries lease_owner / lease_until.
    3. `send_started`   : set (conditional on our own claim) immediately BEFORE the Meta call. Recovery of an expired lease
                          returns the recipient to the queue ONLY if send_started is false; if true the message may be on its way,
                          so the recipient becomes `uncertain` and is never resent.
    4. unique (campaign, phone) in the snapshot: one document per person per campaign.
    5. only the lease owner can write the outcome; a late owner can upgrade `uncertain` -> `sent` (it knows the message id).

Concurrency / rate: a campaign is leased to one worker at a time (run.lease_until); the rate limit is a cluster-wide
per-second counter document (atomic upsert), so N replicas together stay under WA_CAMPAIGN_RATE_PER_SECOND.
"""
import logging
import os
import random
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from services import wa_campaign_audit as audit
from services import wa_campaign_manager as M
from services import wa_campaign_settings as ST
from services import wa_eligibility as E
from services import wa_templates as T
from services import whatsapp as WA
from services.wa_webhook_security import exc_summary

logger = logging.getLogger("offro.wa_campaigns.worker")

RUNNING, PAUSED = "running", "paused"
RATE = "wa_campaign_rate"
EPOCH = datetime(1970, 1, 1)
SYSTEM_ADMIN = {"id": "system", "name": "campaign worker", "role": "system"}
_PRODUCTION_ENV_VARS = ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT", "APP_ENV", "ENVIRONMENT", "ENV")
RATE_LIMIT_CODES = frozenset({4, 80007, 130429, 131056})          # Meta throttling: slow the whole campaign down
RATE_BACKOFF = timedelta(seconds=60)
CONSENT_RETRY_DELAY = 30
MODES = ("test", "live")

_STATE = {"thread": None, "stop": threading.Event(), "last_tick": None, "worker_id": None}
_STATE_LOCK = threading.Lock()


def _db():
    return M._db()


def _now():
    return datetime.utcnow()


# ── configuration (all clamped; unparsable values fall back to the safe default) ─────────────────

def _int_env(name, default, lo, hi):
    try:
        v = int((os.getenv(name, "") or "").strip() or default)
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def config() -> dict:
    return {
        "batch_size": _int_env("WA_CAMPAIGN_BATCH_SIZE", 20, 1, 200),
        "concurrency": _int_env("WA_CAMPAIGN_CONCURRENCY", 1, 1, 8),
        "rate_per_second": _int_env("WA_CAMPAIGN_RATE_PER_SECOND", 5, 1, 80),
        "max_attempts": _int_env("WA_CAMPAIGN_MAX_ATTEMPTS", 3, 1, 5),
        "backoff_base_seconds": _int_env("WA_CAMPAIGN_BACKOFF_BASE_SECONDS", 30, 1, 3600),
        "backoff_max_seconds": _int_env("WA_CAMPAIGN_BACKOFF_MAX_SECONDS", 900, 1, 86400),
        "lease_seconds": _int_env("WA_CAMPAIGN_LEASE_SECONDS", 120, 30, 3600),
        "poll_seconds": _int_env("WA_CAMPAIGN_POLL_SECONDS", 15, 1, 600),
        "max_late_minutes": _int_env("WA_CAMPAIGN_MAX_LATE_MINUTES", 360, 1, 10080),
    }


def backoff_seconds(attempts: int, cfg: dict, rng=random.random) -> float:
    """Exponential backoff for the Nth failed attempt (1 -> base, 2 -> 2*base ...), capped, +-20 % jitter."""
    raw = min(cfg["backoff_max_seconds"], cfg["backoff_base_seconds"] * (2 ** max(0, attempts - 1)))
    return max(1.0, raw * (0.8 + 0.4 * rng()))


# ── gates ────────────────────────────────────────────────────────────────────────────────────────

def production_blocked() -> bool:
    for name in _PRODUCTION_ENV_VARS:
        if (os.getenv(name, "") or "").strip().lower() in ("production", "prod"):
            return not ST._env_flag("WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION")
    return False


def live_allowed() -> bool:
    return ST._env_flag("WA_CAMPAIGN_BULK_LIVE_ENABLED")


def worker_gate() -> tuple:
    """(ok, reason): may the worker do ANY work right now? Fails closed."""
    try:
        if not ST.worker_enabled():
            return False, "worker_disabled"
        if production_blocked():
            return False, "production_blocked"
        return ST.sending_allowed()
    except Exception as e:
        logger.error("[WA-Worker] gate check failed (%s) - treating as OFF", exc_summary(e))
        return False, "gate_unavailable"


# ── indexes (new fields / new collection only) ───────────────────────────────────────────────────

def ensure_indexes():
    d = _db()
    r = d[M.RECIPIENTS]
    r.create_index([("campaign_id", 1), ("state", 1), ("next_attempt_at", 1)], name="wa_recipients_queue")
    r.create_index([("state", 1), ("lease_until", 1)], name="wa_recipients_lease")
    r.create_index([("message_id", 1)], unique=True, sparse=True, name="wa_recipients_message_id")
    d[M.CAMPAIGNS].create_index([("status", 1), ("run.lease_until", 1)], name="wa_campaigns_run_lease")
    d[RATE].create_index([("at", 1)], expireAfterSeconds=300, name="wa_campaign_rate_ttl")


def ensure_indexes_safe() -> bool:
    try:
        ensure_indexes()
        return True
    except Exception as e:
        logger.error("[WA-Worker] worker index creation skipped (%s)", exc_summary(e))
        return False


# ── cluster-wide rate limit (atomic) ─────────────────────────────────────────────────────────────

def take_rate_token(cfg: dict, now=None) -> bool:
    """One send slot in the current second, shared by every replica. The upsert only matches while count < limit; when the
    second is full the same _id already exists, so the upsert tries to insert it and fails with DuplicateKeyError."""
    now = now or _now()
    sec = int(now.timestamp())
    try:
        _db()[RATE].update_one({"_id": "s%d" % sec, "count": {"$lt": cfg["rate_per_second"]}},
                               {"$inc": {"count": 1}, "$setOnInsert": {"at": now}}, upsert=True)
        return True
    except DuplicateKeyError:
        return False
    except Exception as e:
        logger.error("[WA-Worker] rate limiter unavailable (%s) - not sending", exc_summary(e))
        return False


# ── run state helpers ────────────────────────────────────────────────────────────────────────────

def _worker_id() -> str:
    return "%s:%d:%s" % (socket.gethostname()[:20], os.getpid(), str(ObjectId())[-6:])


def _run_transition(doc, expect, to, admin, extra_set=None) -> dict:
    """Status change guarded by (status in `expect`, version): two admins / replicas cannot both win."""
    if doc["status"] not in expect:
        raise M.CampaignError("invalid_transition", 409, "A %s campaign cannot become %s." % (doc["status"], to))
    upd = {"$set": {"status": to, "updated_at": _now(), "updated_by": admin, **(extra_set or {})}, "$inc": {"version": 1}}
    res = _db()[M.CAMPAIGNS].update_one({"_id": doc["_id"], "status": doc["status"], "version": doc["version"]}, upd)
    if res.matched_count != 1:
        raise M.CampaignError("conflict", 409, "The campaign was changed by someone else. Reload and try again.")
    return _db()[M.CAMPAIGNS].find_one({"_id": doc["_id"]})


def _audit(action, admin, campaign_id, outcome="ok", details=None, strict=False):
    return audit.record(action, admin, outcome=outcome, campaign_id=campaign_id, details=details, strict=strict)


def _recipient_q(camp):
    snap = (camp.get("snapshot") or {}).get("id")
    return {"campaign_id": str(camp["_id"]), "snapshot_id": snap}


# ── operator controls ────────────────────────────────────────────────────────────────────────────

def set_run_config(campaign, mode, confirm, admin) -> dict:
    """Choose the run mode of a SCHEDULED campaign. 'test' (default) can only reach allow-listed numbers. 'live' is a bulk
    send to the whole snapshot: it needs an explicit confirmation of the audience size AND the WA_CAMPAIGN_BULK_LIVE_ENABLED
    key at run time. Setting it sends nothing."""
    if mode not in MODES:
        raise M.CampaignError("invalid_mode", 400, "mode must be 'test' or 'live'.")
    if campaign["status"] != M.SCHEDULED:
        raise M.CampaignError("not_scheduled", 409, "The run mode can only be set on a scheduled campaign.")
    snap = campaign.get("snapshot") or {}
    if mode == "live":
        if not isinstance(confirm, dict) or confirm.get("acknowledge") is not True or confirm.get("mode") != "live" \
                or confirm.get("eligible_count") != snap.get("eligible"):
            raise M.CampaignError("confirmation_required", 400,
                                  "Live mode needs confirm {acknowledge: true, mode: 'live', eligible_count: %s}." % snap.get("eligible"))
    cfg = {"mode": mode, "set_by": admin, "set_at": _now()}
    res = _db()[M.CAMPAIGNS].update_one({"_id": campaign["_id"], "status": M.SCHEDULED, "version": campaign["version"]},
                                        {"$set": {"run_config": cfg, "updated_at": _now(), "updated_by": admin}, "$inc": {"version": 1}})
    if res.matched_count != 1:
        raise M.CampaignError("conflict", 409, "The campaign was changed by someone else. Reload and try again.")
    return _db()[M.CAMPAIGNS].find_one({"_id": campaign["_id"]})


def pause(campaign, admin, reason="admin") -> dict:
    return _run_transition(campaign, (RUNNING,), PAUSED, admin, {"run.paused_at": _now(), "run.pause_reason": reason})


def resume(campaign, admin) -> dict:
    ok, why = ST.sending_allowed()
    if not ok:
        raise M.CampaignError("sending_disabled", 409, "Sending is disabled (%s); resuming now would not send." % why, reason=why)
    return _run_transition(campaign, (PAUSED,), RUNNING, admin, {"run.lease_until": EPOCH, "run.backoff_until": EPOCH,
                                                                "run.pause_reason": None, "run.resumed_at": _now()})


def cancel_run(campaign, admin) -> dict:
    """running/paused -> cancelled (terminal). Queued recipients become `cancelled`; the recipient records are KEPT for the
    report (unlike a never-started campaign, whose snapshot is dropped). Messages already handed to Meta are unaffected."""
    out = _run_transition(campaign, (RUNNING, PAUSED), M.CANCELLED, admin,
                          {"cancelled_at": _now(), "cancelled_by": admin, "run.swept": False})
    _sweep_cancelled_campaign(out)
    return out


def _sweep_cancelled_campaign(camp) -> int:
    n = _db()[M.RECIPIENTS].update_many({"campaign_id": str(camp["_id"]), "state": {"$in": ["queued", "snapshot"]}},
                                        {"$set": {"state": "cancelled", "cancel_reason": "campaign_cancelled", "updated_at": _now()},
                                         "$unset": {"lease_owner": "", "lease_until": ""}}).modified_count
    _db()[M.CAMPAIGNS].update_one({"_id": camp["_id"]}, {"$set": {"run.swept": True}})
    return n


def sweep_cancelled(limit=20) -> int:
    """Finishes a cancel that was interrupted between the status change and the recipient update (idempotent)."""
    total = 0
    for camp in _db()[M.CAMPAIGNS].find({"status": M.CANCELLED, "run.swept": False}).limit(limit):
        total += _sweep_cancelled_campaign(camp)
    return total


# ── recovery of abandoned work ───────────────────────────────────────────────────────────────────

def recover_abandoned(now=None) -> dict:
    """Recipients whose lease expired (the worker died). If the sender was never reached (send_started false) the recipient
    goes back to the queue; if it WAS reached the message may be on its way, so it becomes `uncertain` - never resent."""
    now = now or _now()
    r = _db()[M.RECIPIENTS]
    requeued = r.update_many({"state": "sending", "lease_until": {"$lt": now}, "send_started": False},
                             {"$set": {"state": "queued", "next_attempt_at": now, "updated_at": now, "recovered": True},
                              "$unset": {"lease_owner": "", "lease_until": ""}, "$inc": {"attempts": -1}}).modified_count
    uncertain = r.update_many({"state": "sending", "lease_until": {"$lt": now}, "send_started": True},
                              {"$set": {"state": "uncertain", "uncertain_reason": "lease_expired_after_send_started",
                                        "updated_at": now}}).modified_count
    if requeued or uncertain:
        logger.warning("[WA-Worker] recovered abandoned recipients: requeued=%d uncertain=%d", requeued, uncertain)
    return {"requeued": requeued, "uncertain": uncertain}


# ── launching due campaigns ──────────────────────────────────────────────────────────────────────

def _enqueue_snapshot(camp) -> int:
    now = _now()
    q = {**_recipient_q(camp), "state": "snapshot"}
    return _db()[M.RECIPIENTS].update_many(q, {"$set": {"state": "queued", "attempts": 0, "next_attempt_at": now, "queued_at": now,
                                                        "send_started": False, "updated_at": now}}).modified_count


def launch_due(now=None, limit=5) -> list:
    """scheduled -> running for campaigns whose send_at has arrived. Fails closed on every doubt: a campaign that cannot be
    started simply stays scheduled."""
    now = now or _now()
    started = []
    oldest = now - timedelta(minutes=config()["max_late_minutes"])         # a blast that missed its slot by hours is NOT sent late
    due = {"status": M.SCHEDULED, "schedule.send_at": {"$lte": now, "$gte": oldest}}
    for camp in _db()[M.CAMPAIGNS].find(due).sort("_id", 1).limit(limit):
        mode = (camp.get("run_config") or {}).get("mode", "test")
        if mode not in MODES or (mode == "live" and not live_allowed()):
            logger.info("[WA-Worker] campaign %s stays scheduled (mode=%s not allowed now)", camp["_id"], mode)
            continue
        try:
            tdoc = M.load_template(camp["template"])
            if M.template_problems(camp) or T.verify_live(tdoc):
                logger.warning("[WA-Worker] campaign %s stays scheduled (template not ready)", camp["_id"])
                continue
        except Exception as e:
            logger.error("[WA-Worker] campaign %s stays scheduled (template unverifiable: %s)", camp["_id"], exc_summary(e))
            continue
        try:
            out = _run_transition(camp, (M.SCHEDULED,), RUNNING, SYSTEM_ADMIN,
                                  {"run": {"mode": mode, "started_at": now, "lease_owner": None, "lease_until": EPOCH,
                                           "backoff_until": EPOCH, "swept": None}})
        except M.CampaignError:
            continue                                      # another replica launched it first
        except Exception as e:
            logger.error("[WA-Worker] launch failed (%s)", exc_summary(e))
            continue
        try:
            _audit("campaign.run_started", SYSTEM_ADMIN, out["_id"], details={"mode": mode}, strict=True)
        except Exception:
            logger.error("[WA-Worker] audit unavailable - pausing the campaign that was just launched")
            try:
                _run_transition(out, (RUNNING,), PAUSED, SYSTEM_ADMIN, {"run.pause_reason": "audit_unavailable"})
            except Exception:
                pass
            continue
        started.append(str(out["_id"]))
    return started


# ── claiming and processing ──────────────────────────────────────────────────────────────────────

def _claim_campaign(wid, cfg, now):
    """ATOMIC campaign lease: only a running campaign whose lease expired (and is not backing off) can be taken."""
    return _db()[M.CAMPAIGNS].find_one_and_update(
        {"status": RUNNING, "run.lease_until": {"$lte": now}, "run.backoff_until": {"$lte": now}},
        {"$set": {"run.lease_owner": wid, "run.lease_until": now + timedelta(seconds=cfg["lease_seconds"])}},
        sort=[("run.lease_until", 1)], return_document=ReturnDocument.AFTER)


def _release_campaign(camp_id, wid, backoff_until=None):
    s = {"run.lease_until": EPOCH, "run.last_batch_at": _now()}
    if backoff_until:
        s["run.backoff_until"] = backoff_until
    _db()[M.CAMPAIGNS].update_one({"_id": camp_id, "run.lease_owner": wid}, {"$set": s})


def claim_recipient(campaign_id, wid, cfg, now):
    """ATOMIC recipient claim queued -> sending (one winner per recipient across all replicas)."""
    return _db()[M.RECIPIENTS].find_one_and_update(
        {"campaign_id": campaign_id, "state": "queued", "next_attempt_at": {"$lte": now}},
        {"$set": {"state": "sending", "lease_owner": wid, "lease_until": now + timedelta(seconds=cfg["lease_seconds"]),
                  "send_started": False, "claimed_at": now, "updated_at": now}, "$inc": {"attempts": 1}},
        sort=[("_id", 1)], return_document=ReturnDocument.AFTER)


def _release_recipient(rec, wid, delay=0.0):
    """Give a claimed recipient back (nothing was sent: send_started is still false)."""
    now = _now()
    _db()[M.RECIPIENTS].update_one(
        {"_id": rec["_id"], "state": "sending", "lease_owner": wid, "send_started": False},
        {"$set": {"state": "queued", "next_attempt_at": now + timedelta(seconds=delay), "updated_at": now},
         "$unset": {"lease_owner": "", "lease_until": ""}, "$inc": {"attempts": -1}})


def _end_recipient(rec, wid, state, extra=None, allowed_from=("sending",)):
    now = _now()
    s = {"state": state, "updated_at": now, **(extra or {})}
    res = _db()[M.RECIPIENTS].update_one({"_id": rec["_id"], "lease_owner": wid, "state": {"$in": list(allowed_from)}},
                                         {"$set": s, "$unset": {"lease_until": ""}})
    if res.matched_count != 1:
        logger.warning("[WA-Worker] outcome '%s' not recorded for a recipient (lease lost)", state)
    return res.matched_count == 1


def _renew_campaign_lease(b) -> tuple:
    """ONE atomic operation that both VERIFIES and RENEWS the campaign lease: it matches only while the campaign is still
    `running`, the lease is still OURS (owner == this worker) and has NOT expired. It can therefore never overwrite another worker's
    lease, never resurrect an expired one, and never succeed after a pause / cancel. -> (ok, reason)."""
    now = _now()
    try:
        res = _db()[M.CAMPAIGNS].update_one(
            {"_id": b.camp["_id"], "status": RUNNING, "run.lease_owner": b.wid, "run.lease_until": {"$gt": now}},
            {"$set": {"run.lease_until": now + timedelta(seconds=b.cfg["lease_seconds"])}})
    except Exception as e:
        logger.error("[WA-Worker] campaign lease check failed (%s) - not sending", exc_summary(e))
        return False, "lease_check_unavailable"
    if res.matched_count == 1:
        return True, "ok"
    try:                                                          # only to explain WHY it did not match; the decision is made
        cs = _db()[M.CAMPAIGNS].find_one({"_id": b.camp["_id"]}, {"status": 1})      # by the atomic update above
    except Exception:
        return False, "lease_check_unavailable"
    if not cs or cs["status"] == M.CANCELLED:
        return False, "cancelled"
    if cs["status"] != RUNNING:
        return False, "paused"
    return False, "lease_lost"


def _unstart(rec, wid) -> bool:
    """Take back `send_started` when the send was NOT made (the lease check failed after the flag was written). Conditional on our
    own claim. If this cannot be done the flag stays true and lease recovery will mark the recipient `uncertain` (safe, never resent)."""
    try:
        res = _db()[M.RECIPIENTS].update_one({"_id": rec["_id"], "state": "sending", "lease_owner": wid, "send_started": True},
                                             {"$set": {"send_started": False, "updated_at": _now()}, "$unset": {"send_started_at": ""}})
        return res.matched_count == 1
    except Exception as e:
        logger.error("[WA-Worker] could not clear send_started (%s) - recovery will mark it uncertain", exc_summary(e))
        return False


class _Batch:
    def __init__(self, camp, tdoc, mode, cfg, wid):
        self.camp, self.tdoc, self.mode, self.cfg, self.wid = camp, tdoc, mode, cfg, wid
        self.cid, self.halt, self.reason = str(camp["_id"]), threading.Event(), ""
        self.backoff_until, self.counts, self._lock = None, {}, threading.Lock()

    def stop(self, reason, backoff=None):
        with self._lock:
            if not self.halt.is_set():
                self.reason = reason
            self.halt.set()
            if backoff:
                self.backoff_until = backoff

    def count(self, key):
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + 1


def process_recipient(rec, b: _Batch, sleeper=time.sleep) -> str:
    """One message. Every check is re-read HERE, immediately before the send. Returns the outcome label."""
    d = _db()
    if b.halt.is_set():
        _release_recipient(rec, b.wid)
        return "released"
    try:
        cs = d[M.CAMPAIGNS].find_one({"_id": b.camp["_id"]}, {"status": 1})
    except Exception:
        b.stop("campaign_state_unavailable", _now() + timedelta(seconds=30))
        _release_recipient(rec, b.wid, 30)
        return "released"
    if not cs or cs["status"] == M.CANCELLED:
        _end_recipient(rec, b.wid, "cancelled", {"cancel_reason": "campaign_cancelled"})
        return "cancelled"
    if cs["status"] != RUNNING:                                   # paused (by an admin or automatically)
        b.stop("campaign_" + cs["status"])
        _release_recipient(rec, b.wid)
        return "released"
    ok, why = _renew_campaign_lease(b)                            # heartbeat: keeps a long batch's lease alive, stops us if it is lost
    if not ok:
        return _stop_for_lease(rec, b, why)
    if b.mode == "test" and not ST.is_test_number(rec["phone"]):  # test mode: allow-list only, everyone else is NOT contacted
        _end_recipient(rec, b.wid, "cancelled", {"cancel_reason": "not_a_test_number"})
        return "cancelled"
    if not take_rate_token(b.cfg):
        _release_recipient(rec, b.wid)
        sleeper(1.0)
        return "released"
    try:                                                          # consent / STOP: re-read now, STOP always wins
        el = E.check(rec["phone"])
    except Exception:
        el = None
    if el is None or el.reason == E.LOOKUP_FAILED:
        b.stop("consent_lookup_failed", _now() + timedelta(seconds=CONSENT_RETRY_DELAY))
        _release_recipient(rec, b.wid, CONSENT_RETRY_DELAY)
        return "released"
    if not el.eligible:
        _end_recipient(rec, b.wid, "cancelled", {"cancel_reason": el.reason})
        return "cancelled"
    try:
        components = M._components_for(b.tdoc, rec.get("values") or {}, b.camp.get("header_media"))
    except T.ComponentError:
        _end_recipient(rec, b.wid, "failed", {"error": "payload_invalid"})
        return "failed"
    # Live template readiness at Meta, re-read for EVERY message (fail closed: Meta unreachable or any error = no send)
    try:
        live = T.verify_live(b.tdoc)
    except Exception as e:
        logger.error("[WA-Worker] template re-verification failed (%s) - not sending", exc_summary(e))
        b.stop("template_unverified", _now() + timedelta(seconds=60))
        _release_recipient(rec, b.wid, 60)
        return "released"
    if live:
        b.stop("template_not_ready")
        _release_recipient(rec, b.wid)
        return "released"
    # FINAL gate, the last switches read before the sender: worker key, sending env key, DB kill switch, live key, Production guard
    try:
        allowed, why = ST.sending_allowed()
        if allowed and not ST.worker_enabled():
            allowed, why = False, "worker_disabled"
    except Exception:
        allowed, why = False, "settings_unavailable"
    if allowed and b.mode == "live" and not live_allowed():
        allowed, why = False, "live_disabled"
    if allowed and production_blocked():
        allowed, why = False, "production_blocked"
    if not allowed:
        b.stop("kill_switch:" + why)
        _release_recipient(rec, b.wid)
        return "released"
    # (a) record send_started - conditional on OUR claim, an unexpired recipient lease and send_started still false ...
    now = _now()
    res = d[M.RECIPIENTS].update_one(
        {"_id": rec["_id"], "state": "sending", "lease_owner": b.wid, "send_started": False, "lease_until": {"$gt": now}},
        {"$set": {"send_started": True, "send_started_at": now, "lease_until": now + timedelta(seconds=b.cfg["lease_seconds"])}})
    if res.matched_count != 1:
        return "lost_lease"                                       # recovered by someone else / expired: do NOT send
    # (b) ... then, atomically, verify + renew the CAMPAIGN lease and that the campaign is still running. A pause / cancel / lost
    # lease that wins before this single operation prevents the send; there is no separate read between this check and the call.
    ok, why = _renew_campaign_lease(b)
    if not ok:
        if _unstart(rec, b.wid):
            return _stop_for_lease(rec, b, why)
        b.stop("lease_unclear")
        return "lost_lease"                                       # flag stays true: recovery makes it `uncertain`, never resent
    try:
        out = WA.send_whatsapp_template_checked(rec["phone"], b.camp["template"]["name"], b.camp["template"]["language"], components)
    except Exception as e:                                        # the call may have reached Meta
        logger.error("[WA-Worker] sender raised (%s) - outcome uncertain", exc_summary(e))
        out = {"ok": False, "outcome": "uncertain", "error_code": None}
    return _record_outcome(rec, b, out)


def _stop_for_lease(rec, b: _Batch, why: str) -> str:
    """A campaign-level check refused the send (nothing was sent, send_started is false): put the recipient back / cancel it and stop the batch."""
    if why == "cancelled":
        _end_recipient(rec, b.wid, "cancelled", {"cancel_reason": "campaign_cancelled"})
        b.stop("campaign_cancelled")
        return "cancelled"
    if why == "paused":
        b.stop("campaign_paused")
    elif why == "lease_check_unavailable":
        b.stop(why, _now() + timedelta(seconds=30))
    else:
        b.stop("lease_lost")                                      # another worker owns the campaign now: it is not ours to touch
    _release_recipient(rec, b.wid)
    return "released"


def _record_outcome(rec, b: _Batch, out: dict) -> str:
    outcome = out.get("outcome", "uncertain")
    code = out.get("error_code")
    now = _now()
    if outcome == "sent" and out.get("message_id"):
        _end_recipient(rec, b.wid, "sent", {"message_id": str(out["message_id"]), "sent_at": now, "delivery_rank": 2},
                       allowed_from=("sending", "uncertain"))
        return "sent"
    if outcome == "rejected_retryable":
        if code in RATE_LIMIT_CODES:
            b.stop("meta_rate_limited", now + RATE_BACKOFF)
        if rec["attempts"] < b.cfg["max_attempts"]:
            delay = backoff_seconds(rec["attempts"], b.cfg)
            _db()[M.RECIPIENTS].update_one(
                {"_id": rec["_id"], "lease_owner": b.wid, "state": "sending"},
                {"$set": {"state": "queued", "next_attempt_at": now + timedelta(seconds=delay), "error_code": code,
                          "send_started": False, "updated_at": now}, "$unset": {"lease_owner": "", "lease_until": ""}})
            return "retry"
        _end_recipient(rec, b.wid, "failed", {"error_code": code, "error": "retries_exhausted"})
        return "failed"
    if outcome == "rejected_permanent":
        _end_recipient(rec, b.wid, "failed", {"error_code": code, "error": "rejected"})
        return "failed"
    # uncertain, or anything unexpected (including 'sent' without an id): NEVER retried automatically
    _end_recipient(rec, b.wid, "uncertain", {"error_code": code, "uncertain_reason": "send_outcome_unknown"})
    return "uncertain"


def finish_if_done(camp) -> bool:
    open_n = _db()[M.RECIPIENTS].count_documents({"campaign_id": str(camp["_id"]), "state": {"$in": ["queued", "sending", "snapshot"]}})
    if open_n:
        return False
    fresh = _db()[M.CAMPAIGNS].find_one({"_id": camp["_id"]})
    if not fresh or fresh["status"] != RUNNING:
        return False
    try:
        _run_transition(fresh, (RUNNING,), M.COMPLETED, SYSTEM_ADMIN, {"run.finished_at": _now(), "run.lease_until": EPOCH})
    except M.CampaignError:
        return False
    _audit("campaign.run_completed", SYSTEM_ADMIN, camp["_id"])
    return True


def process_campaign(camp, wid, cfg, sleeper=time.sleep) -> dict:
    mode = (camp.get("run") or {}).get("mode", "test")
    summary = {"campaign": str(camp["_id"]), "claimed": 0, "halt": ""}
    try:
        _enqueue_snapshot(camp)
        tdoc = M.load_template(camp["template"])
        try:
            live = T.verify_live(tdoc)
        except T.TemplateSyncError:
            _release_campaign(camp["_id"], wid, _now() + timedelta(seconds=60))     # Meta unreachable: wait, send nothing
            summary["halt"] = "template_unverified"
            return summary
        if live or M.template_problems(camp):
            _auto_pause(camp, "template_not_ready")
            _release_campaign(camp["_id"], wid)
            summary["halt"] = "template_not_ready"
            return summary
        b = _Batch(camp, tdoc, mode, cfg, wid)
        quota = _Quota(cfg["batch_size"])
        if cfg["concurrency"] > 1:
            with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as ex:
                parts = list(ex.map(lambda _i: _claim_and_process(b, quota, sleeper), range(cfg["concurrency"])))
        else:
            parts = [_claim_and_process(b, quota, sleeper)]
        results = [r for part in parts for r in part]
        summary["claimed"] = len(results)
        for r in results:
            b.count(r)
        summary["results"] = dict(b.counts)
        if b.halt.is_set():
            summary["halt"] = b.reason
            if b.reason.startswith("kill_switch:") or b.reason == "template_not_ready":     # pause visibly, resume is a human act
                _auto_pause(camp, b.reason)
        _release_campaign(camp["_id"], wid, b.backoff_until)
        finish_if_done(camp)
    except Exception as e:
        logger.error("[WA-Worker] campaign batch failed (%s)", exc_summary(e))
        summary["halt"] = "error"
        try:
            _release_campaign(camp["_id"], wid, _now() + timedelta(seconds=30))
        except Exception:
            pass
    return summary


class _Quota:
    def __init__(self, n):
        self.n, self._l = n, threading.Lock()

    def take(self) -> bool:
        with self._l:
            if self.n <= 0:
                return False
            self.n -= 1
            return True


def _claim_and_process(b: _Batch, quota: _Quota, sleeper) -> list:
    """Claim ONE recipient at a time right before handling it, so its lease is always fresh (a 200-recipient batch never holds
    199 claims that could expire while it works through them)."""
    out = []
    while not b.halt.is_set() and quota.take():
        rec = claim_recipient(b.cid, b.wid, b.cfg, _now())
        if not rec:
            break
        out.append(_safe_process(rec, b, sleeper))
    return out


def _safe_process(rec, b, sleeper):
    try:
        return process_recipient(rec, b, sleeper)
    except Exception as e:                       # never let one recipient take the batch down; the lease recovery decides its fate
        logger.error("[WA-Worker] recipient processing error (%s)", exc_summary(e))
        b.stop("processing_error", _now() + timedelta(seconds=30))
        return "error"


def _auto_pause(camp, reason):
    try:
        fresh = _db()[M.CAMPAIGNS].find_one({"_id": camp["_id"]})
        if fresh and fresh["status"] == RUNNING:
            _run_transition(fresh, (RUNNING,), PAUSED, SYSTEM_ADMIN, {"run.paused_at": _now(), "run.pause_reason": reason[:60]})
            _audit("campaign.run_auto_paused", SYSTEM_ADMIN, camp["_id"], details={"reason": reason[:60]})
    except Exception as e:
        logger.error("[WA-Worker] auto-pause failed (%s)", exc_summary(e))


# ── the tick and the loop ────────────────────────────────────────────────────────────────────────

def run_once(wid, now=None, sleeper=time.sleep) -> dict:
    """One scheduling pass. Safe to call from any number of replicas at once."""
    now = now or _now()
    cfg = config()
    out = {"gate": "", "launched": [], "recovered": {}, "batch": None, "idle": True}
    ok, why = worker_gate()
    out["gate"] = why
    _STATE["last_tick"] = {"at": now, "gate": why}
    if not ok:
        return out
    try:
        out["recovered"] = recover_abandoned(now)
        sweep_cancelled()
        out["launched"] = launch_due(now)
        camp = _claim_campaign(wid, cfg, now)
        if camp:
            out["batch"] = process_campaign(camp, wid, cfg, sleeper)
            out["idle"] = not out["batch"].get("claimed")
    except Exception as e:
        logger.error("[WA-Worker] tick failed (%s)", exc_summary(e))
        out["gate"] = "tick_error"
    return out


def _loop(wid, stop):
    cfg = config()
    logger.warning("[WA-Worker] loop started (worker=%s)", wid)
    while not stop.is_set():
        try:
            res = run_once(wid)
            idle = res.get("idle", True)
        except Exception as e:
            logger.error("[WA-Worker] loop error (%s)", exc_summary(e))
            idle = True
        stop.wait(cfg["poll_seconds"] if idle else 0.2)
    logger.warning("[WA-Worker] loop stopped")


def start_worker_if_enabled() -> bool:
    """Called at server startup. Starts NOTHING unless WA_CAMPAIGN_WORKER_ENABLED is on (and not Production without the
    explicit override). The loop still re-checks the full gate on every tick and before every message."""
    if not ST.worker_enabled():
        return False
    if production_blocked():
        logger.error("[WA-Worker] refusing to start on Production (WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION not set)")
        return False
    with _STATE_LOCK:
        if _STATE["thread"] and _STATE["thread"].is_alive():
            return True
        _STATE["stop"] = threading.Event()
        _STATE["worker_id"] = _worker_id()
        t = threading.Thread(target=_loop, args=(_STATE["worker_id"], _STATE["stop"]), name="wa-campaign-worker", daemon=True)
        _STATE["thread"] = t
        t.start()
    return True


def stop_worker():
    _STATE["stop"].set()


def status() -> dict:
    ok, why = worker_gate()
    t = _STATE["thread"]
    last = _STATE["last_tick"]
    try:
        running = _db()[M.CAMPAIGNS].count_documents({"status": RUNNING})
        paused = _db()[M.CAMPAIGNS].count_documents({"status": PAUSED})
    except Exception:
        running = paused = None
    return {"worker_env_enabled": ST.worker_enabled(), "production_blocked": production_blocked(),
            "live_key_on": live_allowed(), "gate": {"ok": ok, "reason": why}, "thread_alive": bool(t and t.is_alive()),
            "last_tick": {"at": last["at"].isoformat() + "Z", "gate": last["gate"]} if last else None,
            "running_campaigns": running, "paused_campaigns": paused, "config": config()}
