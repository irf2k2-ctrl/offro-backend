"""
services/wa_consent.py
----------------------
WhatsApp consent state (opt-out / opt-in), canonical phone numbers, and webhook event claiming.
Phase 1 of the Campaign Manager.

Collections (all new; nothing existing is read or changed):
    wa_contacts        current consent state, ONE doc per phone. _id == canonical phone, so uniqueness is
                       guaranteed by MongoDB itself (the extra unique index on `phone` is belt and braces)
    wa_consent_events  APPEND-ONLY ledger of opt_out / opt_in with evidence. Never updated or deleted by code.
    wa_webhook_events  event claims (idempotency + bounded retry state), TTL 30 days

Rules:
  * No existing merchant/customer/Live Chat record is ever imported or marked opted-in here. A contact
    gets a wa_contacts doc ONLY when that person themselves sends STOP-type or START.
  * wa_contacts is the single source of truth for consent. The ledger is evidence/audit, never state.
  * A consent event whose timestamp is more than 5 minutes ahead of the server's UTC time is REJECTED before any
    write (InvalidTimestampError) - it cannot create, overwrite or supersede consent. Not clamped.
  * Ordering: every consent write carries consent_seq = wa_timestamp*2 + (1 for opt-out, 0 for opt-in) and is
    applied only if it is not older than the stored one, in ONE atomic conditional update. A late/older
    event can never overwrite a newer one; on an equal timestamp the opt-out wins.
  * Partial failure can only leave a person MORE protected:
        opt-out:  contact -> opted_out first, then the ledger event
        opt-in :  ledger event first, then contact -> opted_in
  * Every write is checked as acknowledged; the record functions RAISE on any failure, so the caller can
    never confirm something that was not saved.

PHASE 2 MANDATORY REQUIREMENTS (not implemented here - no campaign sending exists in Phase 1):
  1. Every campaign recipient must be checked against wa_contacts IMMEDIATELY BEFORE sending (consent_status /
     may_send_marketing below), not only when the audience is built.
  2. Opted-out AND unknown-consent contacts (no wa_contacts doc) and invalid numbers are EXCLUDED. Only
     status == opted_in may receive marketing.
  3. If the consent database is unavailable (consent_status raises), sending FAILS CLOSED - nothing is sent.
  4. The existing POST /admin/whatsapp/template-test must enforce the same check before sending.
"""
import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timedelta

from pymongo.errors import DuplicateKeyError

from database import db
from services.wa_webhook_security import exc_summary

logger = logging.getLogger("offro.wa_consent")

CONTACTS = "wa_contacts"
EVENTS = "wa_consent_events"
WEBHOOK_EVENTS = "wa_webhook_events"
WEBHOOK_EVENT_TTL_SECONDS = 30 * 24 * 3600
FUTURE_TOLERANCE_SECONDS = 300        # an event timestamp more than 5 min ahead of server UTC time is REJECTED
MAX_ATTEMPTS = 3                      # initial delivery + 2 Meta retries per message
LEASE_SECONDS = 120                   # a 'processing' claim not finished within this is considered crashed

STOP_KEYWORDS = frozenset({"stop", "unsubscribe", "cancel", "end", "quit"})
START_KEYWORDS = frozenset({"start"})

STOP_CONFIRMATION = (
    "You have been unsubscribed from OffrO WhatsApp offers and will no longer receive promotional "
    "messages from us.\n\nReply START at any time to subscribe again."
)
START_CONFIRMATION = (
    "You are subscribed to OffrO WhatsApp offers again. Thank you!\n\nReply STOP at any time to unsubscribe."
)
# Neutral: claims nothing was saved. Sent only when the consent write FAILED.
STOP_FAILURE_NOTICE = "We couldn't process your request. Please reply STOP again."
START_FAILURE_NOTICE = "We couldn't process your request. Please reply START again."


class ConsentWriteError(Exception):
    """A consent write was not acknowledged by the database."""


class InvalidTimestampError(Exception):
    """The event's timestamp is more than FUTURE_TOLERANCE_SECONDS ahead of the server's UTC time. Raised BEFORE any
    write, so such an event can never create, overwrite or supersede consent. Deliberately NOT a ValueError (that
    means 'invalid phone' to callers) and never clamped to 'now': the event is rejected, not repaired."""


# ── Canonical phone numbers ────────────────────────────────────────────────────

_E164_RE = re.compile(r"^[1-9][0-9]{7,14}$")
_IN_MOBILE_RE = re.compile(r"^91[6-9][0-9]{9}$")          # canonical Indian mobile: 91 + 10 digits starting 6-9
_ASCII_DIGITS_RE = re.compile(r"^[0-9]+$")


def _split(raw):
    """-> (digits or None, had_plus). Separators (space - ( ) .) are ignored; anything else non-numeric is invalid."""
    s = re.sub(r"[\s\-().]", "", str(raw or "").strip())
    plus = s.startswith("+")
    if plus:
        s = s[1:]
    return (s if _ASCII_DIGITS_RE.match(s) else None), plus


def normalize_phone(raw, whatsapp_id: bool = False) -> str:
    """The ONE canonical key for a phone number, or '' if invalid. Never stores two keys for one person.

    Indian numbers - '+91XXXXXXXXXX', '91XXXXXXXXXX', '0091XXXXXXXXXX', '0XXXXXXXXXX' and a bare 10-digit
    mobile starting 6-9 - all resolve to '91XXXXXXXXXX'. Anything starting with 91 that is not a valid
    12-digit Indian mobile is REJECTED (so it can never become a second key). Other international numbers
    are preserved as E.164 digits without '+'.

    whatsapp_id=True is for Meta's `from` / wa_id, which is ALWAYS already international: a bare 10-digit
    value is then NOT assumed to be Indian. An explicit '+' also means "already international".
    """
    d, plus = _split(raw)
    if d is None:
        return ""
    if d.startswith("00") and not whatsapp_id:        # 0091... / 00<cc>...
        d, plus = d[2:], True
    if not plus and not whatsapp_id:
        if len(d) == 10 and d[0] in "6789":
            return "91" + d
        if len(d) == 11 and d[0] == "0" and d[1] in "6789":
            return "91" + d[1:]
    if d.startswith("91"):
        return d if _IN_MOBILE_RE.match(d) else ""
    return d if _E164_RE.match(d) else ""


def phone_lookup_keys(raw) -> list:
    """Every key under which this number could be stored. Used for CHECKS so an opt-out can't be bypassed by
    writing the same number differently. A bare 10-digit number is normally Indian ('91'+d) but could also be a
    full international number stored as-is, so both are checked."""
    keys = []
    c = normalize_phone(raw)
    if c:
        keys.append(c)
    d, plus = _split(raw)
    if d and not plus and len(d) == 10 and d[0] in "6789" and _E164_RE.match(d) and d not in keys:
        keys.append(d)
    return keys


# ── Keyword detection ──────────────────────────────────────────────────────────

def detect_keyword(text) -> str:
    """'stop' | 'start' | ''. The WHOLE message must be the keyword (case-insensitive, surrounding
    whitespace/punctuation ignored): 'STOP', ' stop. ', 'Unsubscribe!' match; 'please do not stop
    the offers' and 'start now' do not."""
    t = re.sub(r"\s+", " ", str(text or "")).strip().strip(".,!?;:'\"").strip().casefold()
    if t in STOP_KEYWORDS:
        return "stop"
    if t in START_KEYWORDS:
        return "start"
    return ""


def message_keyword_text(msg: dict) -> str:
    """Text to test for a keyword: a text body, or the title/payload of a button / interactive reply."""
    if not isinstance(msg, dict):
        return ""
    t = msg.get("type")
    if t == "text":
        return (msg.get("text") or {}).get("body", "") or ""
    if t == "button":
        b = msg.get("button") or {}
        return b.get("text") or b.get("payload") or ""
    if t == "interactive":
        i = msg.get("interactive") or {}
        r = i.get("button_reply") or i.get("list_reply") or {}
        return r.get("title") or r.get("id") or ""
    return ""


# ── Indexes ────────────────────────────────────────────────────────────────────

def ensure_indexes():
    db[CONTACTS].create_index("phone", unique=True, name="wa_contacts_phone", background=True)
    db[CONTACTS].create_index([("status", 1), ("updated_at", -1)], name="wa_contacts_status", background=True)
    db[EVENTS].create_index([("phone", 1), ("at", -1)], name="wa_consent_events_phone_at", background=True)
    db[EVENTS].create_index("message_id", name="wa_consent_events_message", background=True)
    db[WEBHOOK_EVENTS].create_index("event_key", unique=True, name="wa_webhook_events_key", background=True)
    db[WEBHOOK_EVENTS].create_index("received_at", expireAfterSeconds=WEBHOOK_EVENT_TTL_SECONDS,
                                    name="wa_webhook_events_ttl", background=True)


def ensure_indexes_safe() -> bool:
    """Startup wrapper: never raises, never prints/logs raw exception text (only type + location)."""
    try:
        ensure_indexes()
        return True
    except Exception as e:
        logger.error("[WA-Consent] index creation skipped (%s)", exc_summary(e))
        return False


# ── Consent state ──────────────────────────────────────────────────────────────

def _evidence(keyword_text, message_id, wa_timestamp) -> dict:
    return {"source": "whatsapp_keyword", "text": str(keyword_text or "")[:40],
            "message_id": str(message_id or ""), "wa_timestamp": str(wa_timestamp or "")}


def _event_ts(wa_timestamp) -> int:
    """Ordering timestamp (unix seconds) of a Meta event.
      * 1-12 digit value up to now + FUTURE_TOLERANCE_SECONDS -> used as is (Meta timestamps, small clock skew)
      * more than that in the future                           -> InvalidTimestampError (never clamped)
      * missing / non-numeric (Meta always sends one)          -> server receive time, as before
    Very old values are harmless: they can only lose to newer consent state."""
    s = str(wa_timestamp or "").strip()
    if re.fullmatch(r"[0-9]{1,12}", s):
        ts = int(s)
        if ts > time.time() + FUTURE_TOLERANCE_SECONDS:
            raise InvalidTimestampError("event timestamp is in the future")
        return ts
    return int(time.time())


def _write_contact(p, status, seq, ev, now) -> str:
    """ONE atomic conditional upsert: applied only if no newer consent_seq is stored.
    Returns 'applied' or 'superseded' (a newer event already decided the state - NOT an error).

    A DuplicateKeyError is NOT assumed to mean 'superseded'. The stored consent_seq is read back:
      * stored seq > this event's seq  -> superseded (a genuinely newer decision exists)
      * no document under this _id     -> the conflict came from ANOTHER unique index: failure
      * stored seq older/equal/missing -> we lost an insert race; the update is retried ONCE, a second
                                          duplicate error is a failure
    Failures raise ConsentWriteError, so the caller never confirms something that was not saved."""
    at_field = "opted_out_at" if status == "opted_out" else "opted_in_at"
    for attempt in (1, 2):
        try:
            res = db[CONTACTS].update_one(
                {"_id": p, "$or": [{"consent_seq": {"$exists": False}}, {"consent_seq": {"$lte": seq}}]},
                {"$set": {"phone": p, "phone_last4": p[-4:], "status": status, at_field: now, "updated_at": now,
                          "consent_seq": seq, "last_consent": ev},
                 "$setOnInsert": {"created_at": now}},
                upsert=True)
        except DuplicateKeyError:
            stored = db[CONTACTS].find_one({"_id": p}, {"consent_seq": 1})
            if stored is None:
                raise ConsentWriteError("contact write conflict (duplicate key, no contact record)")
            stored_seq = stored.get("consent_seq")
            if isinstance(stored_seq, int) and not isinstance(stored_seq, bool) and stored_seq > seq:
                return "superseded"
            if attempt == 1:
                continue
            raise ConsentWriteError("contact write conflict (duplicate key, stored consent not newer)")
        if not res.acknowledged:
            raise ConsentWriteError("contact write not acknowledged")
        return "applied"
    raise ConsentWriteError("contact write conflict")      # unreachable; fail closed


def _append_event(p, event, seq, ev, now):
    """Append-only ledger. Idempotent per (event, message id): a Meta retry cannot add a second row."""
    doc = {"phone": p, "phone_last4": p[-4:], "event": event, "at": now, "consent_seq": seq, **ev}
    if ev["message_id"]:
        doc["_id"] = event + ":" + ev["message_id"]
    try:
        res = db[EVENTS].insert_one(doc)
    except DuplicateKeyError:
        return                          # this exact event is already recorded
    if not res.acknowledged:
        raise ConsentWriteError("ledger write not acknowledged")


def record_opt_out(phone, keyword_text="", message_id="", wa_timestamp="", whatsapp_id=False) -> str:
    """Opt the number out. Contact first, then ledger. Returns 'applied' or 'superseded'; RAISES (ValueError for an
    invalid number, ConsentWriteError / pymongo errors otherwise) if anything was not durably saved."""
    p = normalize_phone(phone, whatsapp_id=whatsapp_id)
    if not p:
        raise ValueError("invalid phone for consent record")
    now = datetime.utcnow()
    seq = _event_ts(wa_timestamp) * 2 + 1
    ev = _evidence(keyword_text, message_id, wa_timestamp)
    result = _write_contact(p, "opted_out", seq, ev, now)
    _append_event(p, "opt_out", seq, ev, now)
    return result


def record_opt_in(phone, keyword_text="", message_id="", wa_timestamp="", whatsapp_id=False) -> str:
    """Explicit START by the user. Ledger first, then contact. Same return/raise contract as record_opt_out."""
    p = normalize_phone(phone, whatsapp_id=whatsapp_id)
    if not p:
        raise ValueError("invalid phone for consent record")
    now = datetime.utcnow()
    seq = _event_ts(wa_timestamp) * 2
    ev = _evidence(keyword_text, message_id, wa_timestamp)
    _append_event(p, "opt_in", seq, ev, now)
    return _write_contact(p, "opted_in", seq, ev, now)


# ── Admin-recorded consent (Phase 2A) ───────────────────────────────────────────
#
# Consent a person gave OUTSIDE WhatsApp, recorded by an administrator WITH EVIDENCE. Hard rules:
#   * source, an evidence reference, the time the consent was given, an explicit attestation and the acting
#     administrator's identity (taken from the authenticated session by the caller) are ALL mandatory;
#   * it can NEVER override an opt-out: if the number is opted out (under any storage variant) it is refused, and the
#     contact write itself is conditional (status != opted_out), so a STOP that arrives concurrently also wins. Only
#     the customer's own START can restore consent after a STOP;
#   * consent_seq of a contact created here is 0, so ANY later customer event (STOP or START) applies normally and
#     a late STOP can never be 'superseded' by an admin record;
#   * an existing opted_in contact is left untouched (only an evidence row is appended);
#   * ledger row first, then contact (a failure can only leave extra evidence, never consent without evidence).

ADMIN_CONSENT_SOURCES = frozenset({"signup_form", "written_form", "email", "in_person", "phone_call",
                                   "whatsapp_optin_message", "other"})


class AdminConsentError(Exception):
    """Admin-recorded consent refused. .code: invalid_phone | invalid_source | evidence_required |
    invalid_timestamp | attestation_required | admin_required | opted_out"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _parse_consent_time(value):
    """ISO-8601 string (or datetime) -> naive UTC datetime, or None if invalid / more than 5 minutes in the future."""
    from datetime import timezone, timedelta
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    if dt > datetime.utcnow() + timedelta(seconds=FUTURE_TOLERANCE_SECONDS) or dt.year < 2000:
        return None
    return dt


def record_admin_consent(phone, source, evidence_ref, consented_at, admin, attested, note="") -> dict:
    """Returns {"result": "recorded" | "already_opted_in", "phone_last4", "event_id"}. Raises AdminConsentError for a
    refused request; database errors (and ConsentWriteError) propagate - nothing is reported as saved unless it was."""
    admin = admin if isinstance(admin, dict) else {}
    if not str(admin.get("id", "")).strip():
        raise AdminConsentError("admin_required")
    p = normalize_phone(phone)
    if not p:
        raise AdminConsentError("invalid_phone")
    if source not in ADMIN_CONSENT_SOURCES:
        raise AdminConsentError("invalid_source")
    ref = str(evidence_ref or "").strip()
    if len(ref) < 6:
        raise AdminConsentError("evidence_required")
    when = _parse_consent_time(consented_at)
    if when is None:
        raise AdminConsentError("invalid_timestamp")
    if attested is not True:
        raise AdminConsentError("attestation_required")

    if consent_status(p) == "opted_out":          # raises on a database error: fail closed
        raise AdminConsentError("opted_out")

    now = datetime.utcnow()
    ref, note = ref[:300], str(note or "").strip()[:500]
    ev = {"source": "admin_recorded", "consent_source": source, "evidence_ref": ref, "note": note,
          "consented_at": when.isoformat(), "recorded_at": now.isoformat(), "attested": True,
          "admin_id": str(admin.get("id", ""))[:60], "admin_name": str(admin.get("name", ""))[:80],
          "admin_role": str(admin.get("role", ""))[:60], "message_id": ""}
    event_id = "admin_opt_in:" + hashlib.sha256("|".join([p, source, ref, when.isoformat()]).encode()).hexdigest()[:32]

    try:                                           # idempotent: the same evidence recorded twice is ONE row
        res = db[EVENTS].insert_one({"_id": event_id, "phone": p, "phone_last4": p[-4:], "event": "admin_opt_in",
                                     "at": now, "consent_seq": 0, **ev})
        if not res.acknowledged:
            raise ConsentWriteError("ledger write not acknowledged")
    except DuplicateKeyError:
        pass

    try:
        res = db[CONTACTS].update_one(
            {"_id": p, "status": {"$ne": "opted_out"}},
            {"$setOnInsert": {"phone": p, "phone_last4": p[-4:], "status": "opted_in", "opted_in_at": now,
                              "updated_at": now, "consent_seq": 0, "last_consent": ev, "created_at": now}},
            upsert=True)
    except DuplicateKeyError:
        stored = db[CONTACTS].find_one({"_id": p}, {"status": 1})
        if stored and stored.get("status") == "opted_out":
            raise AdminConsentError("opted_out")   # a STOP won the race
        raise ConsentWriteError("contact write conflict (duplicate key)")
    if not res.acknowledged:
        raise ConsentWriteError("contact write not acknowledged")
    return {"result": "recorded" if res.upserted_id is not None else "already_opted_in",
            "phone_last4": p[-4:], "event_id": event_id}


def consent_status(phone) -> str:
    """'opted_in' | 'opted_out' | 'unknown' | 'invalid'. Checks every storage variant of the number, and an
    opted_out record under ANY variant wins. RAISES if the database cannot be read - callers must fail closed."""
    keys = phone_lookup_keys(phone)
    if not keys:
        return "invalid"
    statuses = {d.get("status") for d in db[CONTACTS].find({"_id": {"$in": keys}}, {"status": 1})}
    if "opted_out" in statuses:
        return "opted_out"
    if "opted_in" in statuses:
        return "opted_in"
    return "unknown"


def is_opted_out(phone) -> bool:
    return consent_status(phone) == "opted_out"


def may_send_marketing(phone) -> bool:
    """For Phase 2 senders: True ONLY for an explicitly opted-in contact. Unknown / opted-out / invalid -> False.
    Raises if the consent DB is unavailable (caller must treat that as 'do not send')."""
    return consent_status(phone) == "opted_in"


def note_consent_failure(event_key, keyword, notice_sent) -> bool:
    """Best-effort durable marker on the event claim that a STOP/START write failed. Never raises."""
    if not event_key:
        return False
    try:
        db[WEBHOOK_EVENTS].update_one(
            {"_id": event_key},
            {"$set": {"consent_failed": True, "consent_keyword": keyword, "notice_sent": bool(notice_sent),
                      "updated_at": datetime.utcnow()}})
        return True
    except Exception as e:
        logger.error("[WA-Consent] could not store consent-failure marker (%s)", exc_summary(e))
        return False


# ── Webhook event claiming (idempotency, lease, bounded retry) ─────────────────
#
# One doc per Meta event in wa_webhook_events (_id == event key):
#     state        processing | done | failed | abandoned
#     attempts     1..MAX_ATTEMPTS
#     claim_token  random per claim; EVERY later write (state, handler progress, reply slot, failure marker)
#                  is filtered on it, so a worker whose lease expired can never overwrite the newer owner
#     lease_until  a 'processing' claim past this time is a crashed/stalled worker and may be reclaimed
#     handlers_done  status-callback handlers already completed (retries run only the others)
#     reply_state  absent | sending | sent | rejected | uncertain   (the ONE automatic reply per message)

class Claim(str):
    """claim_event() result. Compares equal to its outcome string; `.token` identifies this claim."""
    token = ""

    def __new__(cls, outcome, token=""):
        o = super().__new__(cls, outcome)
        o.token = token
        return o


def _lease_until(now):
    return now + timedelta(seconds=LEASE_SECONDS)


def claim_event(event_key: str) -> "Claim":
    """Atomically claim a Meta event.
        'claimed'     first delivery - process it
        'retry'       reclaimed: a previous attempt FAILED, or its lease EXPIRED (crashed worker), and attempts
                      are under MAX_ATTEMPTS - process it again
        'busy'        another worker holds a live lease - do NOT process; ask Meta to retry later
        'duplicate'   already done / retries exhausted - skip
        'abandoned'   lease expired and attempts are exhausted: marked abandoned, skip (monitored)
        'unavailable' the store is down - process the message but send NO automatic reply
    _id is the key, so MongoDB itself guarantees one winner even if the extra index was never created."""
    now = datetime.utcnow()
    token = uuid.uuid4().hex
    try:
        db[WEBHOOK_EVENTS].insert_one({"_id": event_key, "event_key": event_key, "received_at": now,
                                       "updated_at": now, "state": "processing", "attempts": 1,
                                       "claim_token": token, "lease_until": _lease_until(now)})
        return Claim("claimed", token)
    except DuplicateKeyError:
        pass
    except Exception as e:
        logger.error("[WA-Consent] webhook event store unavailable (%s)", exc_summary(e))
        return Claim("unavailable")
    try:
        won = db[WEBHOOK_EVENTS].find_one_and_update(
            {"_id": event_key, "attempts": {"$lt": MAX_ATTEMPTS},
             "$or": [{"state": "failed"},
                     {"state": "processing", "lease_until": {"$lt": now}},
                     {"state": "processing", "lease_until": {"$exists": False}}]},
            {"$set": {"state": "processing", "updated_at": now, "claim_token": token,
                      "lease_until": _lease_until(now)},
             "$inc": {"attempts": 1}})
        if won:
            return Claim("retry", token)
        doc = db[WEBHOOK_EVENTS].find_one({"_id": event_key}, {"state": 1, "lease_until": 1, "attempts": 1})
        if doc and doc.get("state") == "processing":
            lease = doc.get("lease_until")
            if isinstance(lease, datetime) and lease >= now:
                return Claim("busy")
            # Expired and not reclaimable above -> attempts exhausted. Record it, loudly.
            res = db[WEBHOOK_EVENTS].update_one(
                {"_id": event_key, "state": "processing", "attempts": {"$gte": MAX_ATTEMPTS}},
                {"$set": {"state": "abandoned", "updated_at": now}})
            if res.modified_count:
                logger.error("[WA-EVENT-ABANDONED] event=%s attempts exhausted after a stalled worker - "
                             "event NOT processed", event_key)
            return Claim("abandoned")
        return Claim("duplicate")
    except Exception as e:
        logger.error("[WA-Consent] webhook event store unavailable (%s)", exc_summary(e))
        return Claim("unavailable")


def _owned_update(event_key, token, update, extra_filter=None) -> int:
    """Update the claim doc ONLY if `token` is still its owner. Returns matched count (0 = not the owner).
    Raises on a database error - callers decide whether that is fatal."""
    if not event_key or not token:
        return 0
    flt = {"_id": event_key, "claim_token": token}
    if extra_filter:
        flt.update(extra_filter)
    update = dict(update)
    update.setdefault("$set", {})["updated_at"] = datetime.utcnow()
    return db[WEBHOOK_EVENTS].update_one(flt, update).matched_count


def set_event_state(event_key: str, state: str, token: str = "") -> bool:
    """'done' or 'failed' (failed = Meta may retry, bounded by MAX_ATTEMPTS). Applied only for the claim's
    owner (`token`): a stale worker returns False and changes nothing. Best effort, never raises."""
    if not event_key or not token:
        return False
    try:
        n = _owned_update(event_key, token, {"$set": {"state": state}})
        if not n:
            logger.warning("[WA-Consent] stale claim: event state '%s' not applied", state)
        return bool(n)
    except Exception as e:
        logger.error("[WA-Consent] could not set webhook event state (%s)", exc_summary(e))
        return False


def note_consent_failure(event_key, keyword, notice_sent, token="") -> bool:
    """Best-effort durable marker on the event claim that a STOP/START write failed. Owner only. Never raises."""
    if not event_key or not token:
        return False
    try:
        return bool(_owned_update(event_key, token, {"$set": {
            "consent_failed": True, "consent_keyword": keyword, "notice_sent": bool(notice_sent)}}))
    except Exception as e:
        logger.error("[WA-Consent] could not store consent-failure marker (%s)", exc_summary(e))
        return False


# ── Reply slot: at most ONE automatic reply per message ────────────────────────
#   claim_reply_slot -> 'sending'; then exactly one of
#       finish_reply_slot(.., 'sent')       delivered: never resent
#       finish_reply_slot(.., 'rejected')   permanently rejected by Meta: recorded, not retried automatically
#       finish_reply_slot(.., 'uncertain')  timeout / connection loss / 5xx / unparseable: NEVER auto-resent
#       release_reply_slot(..)              definitively rejected AND retryable: slot freed for a later retry
#   A crash between claim and finish leaves 'sending', which is never resent either.

def claim_reply_slot(event_key: str, token: str) -> bool:
    """True only if THIS claim owner atomically took the free slot. Raises on database errors."""
    return bool(_owned_update(event_key, token, {"$set": {"reply_state": "sending"}},
                              {"reply_state": {"$exists": False}}))


def finish_reply_slot(event_key: str, token: str, state: str) -> bool:
    try:
        return bool(_owned_update(event_key, token, {"$set": {"reply_state": state}}, {"reply_state": "sending"}))
    except Exception as e:
        logger.error("[WA-Consent] could not record reply state (%s)", exc_summary(e))
        return False


def release_reply_slot(event_key: str, token: str) -> bool:
    try:
        return bool(_owned_update(event_key, token, {"$unset": {"reply_state": ""}}, {"reply_state": "sending"}))
    except Exception as e:
        logger.error("[WA-Consent] could not release reply slot (%s)", exc_summary(e))
        return False


def reply_state(event_key: str) -> str:
    """'' | sending | sent | rejected | uncertain. Unreadable -> 'uncertain' (at-most-once beats a double reply)."""
    if not event_key:
        return ""
    try:
        doc = db[WEBHOOK_EVENTS].find_one({"_id": event_key}, {"reply_state": 1})
        return (doc or {}).get("reply_state", "") or ""
    except Exception:
        return "uncertain"


# ── Status-callback handler progress ────────────────────────────────────────────

def handlers_done(event_key: str) -> set:
    """Handlers already completed for this event (empty if unknown)."""
    if not event_key:
        return set()
    try:
        doc = db[WEBHOOK_EVENTS].find_one({"_id": event_key}, {"handlers_done": 1})
        return set((doc or {}).get("handlers_done") or [])
    except Exception as e:
        logger.error("[WA-Consent] could not read handler progress (%s)", exc_summary(e))
        return set()


def mark_handler_done(event_key: str, token: str, name: str) -> bool:
    """Owner only. Best effort, never raises."""
    if not event_key or not token:
        return False
    try:
        return bool(_owned_update(event_key, token, {"$addToSet": {"handlers_done": name}}))
    except Exception as e:
        logger.error("[WA-Consent] could not store handler progress (%s)", exc_summary(e))
        return False
