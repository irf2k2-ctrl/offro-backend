"""
routers/webhook.py
------------------
WhatsApp Cloud API webhook router for OFFRO.

Endpoints:
    GET  /webhook  — Meta webhook verification (hub.challenge handshake)
    POST /webhook  — Receive incoming messages & delivery status updates

Mount in server.py:
    from routers import webhook
    app.include_router(webhook.router)

Do NOT add any prefix — Meta sends webhooks to the exact path /webhook.
"""

import json
import logging
from fastapi import APIRouter, Request, Query
from fastapi.responses import PlainTextResponse, JSONResponse
from services.whatsapp import WHATSAPP_VERIFY_TOKEN, send_whatsapp_message, send_whatsapp_message_checked
from services.wa_webhook_security import (check_signature, tokens_equal, mask_phone, safe_error, payload_summary,
                                          exc_summary, install_access_log_redaction, MAX_BODY_BYTES)
from services import wa_consent as consent
import routers.wa_chat as _wa_chat
from routers.wa_chat import store_incoming_message

logger = logging.getLogger("offro.webhook")

router = APIRouter(tags=["WhatsApp Webhook"])

# GET /webhook carries hub.verify_token in its query string; keep it out of Uvicorn's access log.
install_access_log_redaction()


# ── GET /webhook — Meta verification handshake ─────────────────────────────────

@router.get("/webhook")
async def whatsapp_verify(
    hub_mode:         str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge:    str = Query(None, alias="hub.challenge"),
):
    """
    Meta calls this endpoint when you first register (or re-register) the webhook
    in the Meta Business Manager / Developer Console.

    Meta sends:
        hub.mode         = "subscribe"
        hub.verify_token = <the token you set in Meta>
        hub.challenge    = <random string Meta wants you to echo back>

    If your verify token matches, return hub.challenge as plain text with HTTP 200.
    Meta will then confirm the webhook and start sending events.
    """
    logger.info(
        "[WA-Webhook] Verification request — mode=%s token_match=%s",
        hub_mode,
        tokens_equal(hub_verify_token, WHATSAPP_VERIFY_TOKEN),
    )

    if hub_mode == "subscribe" and tokens_equal(hub_verify_token, WHATSAPP_VERIFY_TOKEN):
        logger.info("[WA-Webhook] ✅ Verification successful")
        return PlainTextResponse(content=hub_challenge, status_code=200)

    logger.warning("[WA-Webhook] ❌ Verification failed — invalid token or mode")
    return PlainTextResponse(content="Forbidden", status_code=403)


# ── POST /webhook — Incoming messages & status updates ─────────────────────────

async def _read_body_limited(request: Request):
    """Raw request bytes, or None if the body exceeds MAX_BODY_BYTES (checked via Content-Length first, then
    while streaming, so a missing/lying Content-Length cannot make us buffer an unbounded body)."""
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > MAX_BODY_BYTES:
            return None
    return bytes(buf)


@router.post("/webhook")
async def whatsapp_webhook(request: Request):
    """
    Meta sends all WhatsApp events here:
        - Incoming customer messages  (type: text, image, audio, interactive, etc.)
        - Message delivery status     (sent, delivered, read, failed)
        - Errors

    Order: size limit -> signature check (per WHATSAPP_WEBHOOK_SIGNATURE_MODE) -> parse -> process each message
    in isolation. Responses:
        200  processed (or deliberately ignored: non-JSON, other objects, duplicates, exhausted retries)
        413  body larger than ~1 MB
        403  enforce mode and the signature is missing/invalid
        500  at least one message could NOT be processed (database/Live Chat failure): Meta will retry the
             delivery; messages already handled are recognised as duplicates, so nothing is done twice.
    Payloads, phone numbers, names and message text are never logged.
    """
    raw = await _read_body_limited(request)
    if raw is None:
        logger.warning("[WA-Webhook] Oversized request body rejected (limit %d bytes)", MAX_BODY_BYTES)
        return JSONResponse({"status": "payload_too_large"}, status_code=413)

    # The signature is computed over the exact bytes received - verify BEFORE parsing.
    allowed, _reason = check_signature(raw, request.headers.get("x-hub-signature-256"))
    if not allowed:
        return JSONResponse({"status": "forbidden"}, status_code=403)

    try:
        body = json.loads(raw)
    except Exception:
        logger.warning("[WA-Webhook] ⚠️  Received non-JSON body — ignoring")
        return JSONResponse({"status": "ok"})  # always 200 to Meta
    if not isinstance(body, dict):
        return JSONResponse({"status": "ok"})

    # Counts only - the payload itself (phone numbers, names, message text) is never printed or logged.
    logger.info("[WA-Webhook] Received %s", payload_summary(body))

    # Guard: only process whatsapp_business_account events
    if body.get("object") != "whatsapp_business_account":
        return JSONResponse({"status": "ok"})

    try:
        needs_retry = _process_webhook(body)
    except Exception as e:
        # Structural problem with this payload: retrying the same bytes cannot help.
        logger.error("[WA-Webhook] ❌ Error processing webhook (%s)", exc_summary(e))
        needs_retry = False

    if needs_retry:
        return JSONResponse({"status": "retry"}, status_code=500)
    return JSONResponse({"status": "ok"})


# ── Internal processor ──────────────────────────────────────────────────────────

def _as_list(x) -> list:
    return x if isinstance(x, list) else []


def _process_webhook(body: dict) -> bool:
    """
    Walk the standard WhatsApp Cloud API webhook payload structure and
    dispatch each event to the appropriate handler. Returns True if Meta should RETRY this delivery.

    Payload shape (simplified):
    {
      "object": "whatsapp_business_account",
      "entry": [{
        "id": "<WABA_ID>",
        "changes": [{
          "value": {
            "messaging_product": "whatsapp",
            "metadata": { "phone_number_id": "...", "display_phone_number": "..." },
            "contacts": [{ "wa_id": "...", "profile": { "name": "..." } }],
            "messages": [{ "id": "...", "from": "...", "type": "text", "text": { "body": "..." }, "timestamp": "..." }],
            "statuses": [{ "id": "...", "recipient_id": "...", "status": "delivered", "timestamp": "..." }],
            "errors":   [{ "code": ..., "title": "..." }]
          },
          "field": "messages"
        }]
      }]
    }
    """
    needs_retry = False
    for entry in _as_list(body.get("entry")):
        if not isinstance(entry, dict):
            continue
        for change in _as_list(entry.get("changes")):
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for step in (_handle_messages, _handle_statuses, _handle_errors):
                try:
                    if step(value) is True:
                        needs_retry = True
                except Exception as e:      # one broken section must not discard the others
                    logger.error("[WA-Webhook] ❌ %s failed (%s)", step.__name__, exc_summary(e))
    return needs_retry


# ── Reply helper ───────────────────────────────────────────────────────────────

def reply_to_sender(sender_phone: str, message: str) -> dict:
    """
    Send a WhatsApp reply back to an incoming message sender.

    This is the primary function to use inside any message handler when you
    want to respond to a customer.

    Args:
        sender_phone: The 'from' field from the incoming webhook message
                      (already in E.164 format without '+', e.g. '919876543210').
        message:      Plain text reply body (max 4096 chars).

    Returns:
        dict — {"ok": True, "message_id": "..."} on success
                {"ok": False, "error": "..."} on failure

    Usage example inside a message handler:
        def my_handler(sender, name, msg_type, text, ...):
            if text.lower() == "hi":
                reply_to_sender(sender, "Hello " + name + "! Welcome to OffrO 👋")

    Then register it:
        _MESSAGE_HANDLERS.append(my_handler)
    """
    result = send_whatsapp_message(sender_phone, message)
    if result.get("ok"):
        logger.info("[WA-Webhook] 📤 Reply sent to %s — id=%s", mask_phone(sender_phone), result.get("message_id"))
    else:
        logger.error("[WA-Webhook] ❌ Reply failed to %s — %s", mask_phone(sender_phone), safe_error(result.get("error")))
    return result


def _handle_messages(value: dict) -> bool:
    """Process incoming customer messages. Each message is isolated: one malformed or failing message never
    discards the others. Returns True if any message needs a Meta retry (its claim is then marked failed)."""
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    contact_map = {}
    for c in _as_list(value.get("contacts")):
        if isinstance(c, dict) and isinstance(c.get("wa_id"), str) and c["wa_id"]:
            prof = c.get("profile") if isinstance(c.get("profile"), dict) else {}
            contact_map[c["wa_id"]] = prof.get("name", "Unknown")

    needs_retry = False
    for msg in _as_list(value.get("messages")):
        if not isinstance(msg, dict):
            logger.warning("[WA-Webhook] Malformed message entry ignored")
            continue
        msg_id = msg.get("id") if isinstance(msg.get("id"), str) else ""
        key = ("msg:" + msg_id) if msg_id else ""
        claim = consent.claim_event(key) if key else consent.Claim("claimed")
        if claim == "duplicate":
            logger.info("[WA-Webhook] ♻️  Duplicate delivery of message id=%s ignored", msg_id)
            continue
        if claim == "abandoned":
            logger.warning("[WA-Webhook] Message id=%s abandoned after repeated stalled attempts — skipped", msg_id)
            continue
        if claim == "busy":
            # Another worker holds a live lease. Do not process (no duplicate work); Meta's retry will find the
            # event done, or reclaim it once the lease has expired.
            logger.info("[WA-Webhook] Message id=%s is being processed by another worker — asking Meta to retry", msg_id)
            needs_retry = True
            continue
        # 'unavailable' = the claim store is down: still store the message and run handlers, but send no
        # automatic reply (a retry could not be recognised, so it could double-reply).
        tracked = bool(key) and claim != "unavailable"
        token = getattr(claim, "token", "") if tracked else ""
        try:
            outcome = _handle_one_message(msg, metadata, contact_map, replies_ok=claim in ("claimed", "retry"),
                                          key=key if tracked else "", retried=(claim == "retry"), token=token)
        except Exception as e:
            logger.error("[WA-Webhook] ❌ Message %s processing failed (%s) — asking Meta to retry",
                         msg_id, exc_summary(e))
            outcome = "retry"
        if outcome == "retry":
            needs_retry = True
            if tracked:
                consent.set_event_state(key, "failed", token)    # re-claimable on Meta's retry, bounded by MAX_ATTEMPTS
        elif tracked:
            consent.set_event_state(key, "done", token)
    return needs_retry


def _in_live_chat(msg_id: str) -> bool:
    """On a retry: was this message already written to Live Chat by the failed attempt? (read-only)"""
    try:
        return _wa_chat.db.whatsapp_chats.find_one({"whatsapp_msg_id": msg_id}, {"_id": 1}) is not None
    except Exception:
        return False


def _auto_reply(key: str, token: str, sender: str, text: str) -> str:
    """The ONE automatic reply a message may trigger, guarded by an atomic per-message reply slot.

    Returns one of:
        'sent'       delivered now (slot permanently 'sent')
        'already'    an earlier attempt already delivered it - nothing sent now
        'retryable'  Meta definitively refused it with a temporary error; the slot was RELEASED so a later webhook
                     retry may try again (the only outcome that should make Meta retry)
        'rejected'   Meta permanently refused it: recorded, never retried automatically
        'uncertain'  timeout / connection loss / 5xx / unparseable: it MAY have been delivered - slot kept as
                     'uncertain', NEVER resent automatically
        'blocked'    the slot is not available (taken by an earlier/crashed attempt, stale claim, or store error)
    Only the claim's owner (token) can take or change the slot, so a worker whose lease expired cannot reply."""
    if not key:                                   # untracked (no message id): cannot be retried safely
        res = send_whatsapp_message_checked(sender, text)
        return "sent" if res.get("outcome") == "sent" else "rejected"
    try:
        took = consent.claim_reply_slot(key, token)
    except Exception as e:
        logger.error("[WA-Webhook] reply slot unavailable (%s) — no reply sent", exc_summary(e))
        return "blocked"
    if not took:
        return "already" if consent.reply_state(key) == "sent" else "blocked"

    res = send_whatsapp_message_checked(sender, text)
    outcome = res.get("outcome")
    if outcome == "sent":
        consent.finish_reply_slot(key, token, "sent")
        logger.info("[WA-Webhook] 📤 Reply sent to %s — id=%s", mask_phone(sender), res.get("message_id"))
        return "sent"
    if outcome == "rejected_retryable":
        if consent.release_reply_slot(key, token):
            logger.error("[WA-REPLY-REJECTED] retryable rejection (code=%s) to %s — slot released for retry",
                         res.get("error_code"), mask_phone(sender))
            return "retryable"
        outcome = "uncertain"                     # could not release -> slot stays 'sending': never resent
    if outcome == "rejected_permanent":
        consent.finish_reply_slot(key, token, "rejected")
        logger.error("[WA-REPLY-REJECTED] permanent rejection (code=%s) to %s — not retried",
                     res.get("error_code"), mask_phone(sender))
        return "rejected"
    consent.finish_reply_slot(key, token, "uncertain")
    logger.error("[WA-REPLY-UNCERTAIN] delivery to %s could not be confirmed — NOT resent automatically",
                 mask_phone(sender))
    return "uncertain"


def _handle_one_message(msg: dict, metadata: dict, contact_map: dict, replies_ok: bool,
                        key: str = "", retried: bool = False, token: str = "") -> str:
    """Returns 'ok' or 'retry'."""
    sender     = msg.get("from") if isinstance(msg.get("from"), str) else ""
    msg_id     = msg.get("id") if isinstance(msg.get("id"), str) else ""
    msg_type   = msg.get("type") if isinstance(msg.get("type"), str) else ""
    timestamp  = msg.get("timestamp") if isinstance(msg.get("timestamp"), str) else ""
    sender_name = contact_map.get(sender, "Unknown")
    phone_number_id = metadata.get("phone_number_id", "")

    # Extract text body if it's a text message
    text_body = ""
    if msg_type == "text":
        t = msg.get("text")
        body = t.get("body", "") if isinstance(t, dict) else ""
        text_body = body if isinstance(body, str) else ""

    # No phone number, profile name or message text in logs.
    logger.info(
        "[WA-Webhook] 📩 Message from %s — type=%s id=%s ts=%s len=%d",
        mask_phone(sender), msg_type, msg_id, timestamp, len(text_body),
    )

    # ── Persist incoming message to DB (Live Chat) — always, STOP/START included ─────
    store_failed = False
    if retried and msg_id and _in_live_chat(msg_id):
        logger.info("[WA-Webhook] Message %s already in Live Chat from the earlier attempt", msg_id)
    elif store_incoming_message(
        phone=sender,
        name=sender_name,
        whatsapp_id=sender,
        msg_type=msg_type,
        text=text_body,
        whatsapp_msg_id=msg_id,
        unix_ts=timestamp,
        raw_msg=msg,          # pass full raw msg so wa_chat can extract media_id
    ) is False:
        store_failed = True   # Live Chat could not save it: ask Meta to retry (see below)
        logger.error("[WA-Webhook] ❌ Live Chat store FAILED for message %s", msg_id)

    outcome = "retry" if store_failed else "ok"
    # The reply slot (not a flag) enforces at-most-once; replies_ok is False only when the claim store is down.
    may_reply = replies_ok
    keyword = consent.detect_keyword(consent.message_keyword_text(msg)) if sender else ""

    if keyword:
        # ── STOP-type / START: ALWAYS processed (even if Live Chat storage failed) ───────
        if _handle_consent_keyword(keyword, sender, msg, key, may_reply, token) == "retry":
            outcome = "retry"
    elif store_failed:
        return outcome        # ordinary message not stored: the retry will do welcome + handlers exactly once
    elif msg_type == "text" and sender and may_reply:
        # ── Auto-reply to incoming text messages (unchanged text) ────────────────────
        # Only a definitive, temporary Meta rejection asks for a retry; uncertain outcomes are never resent.
        if _auto_reply(
            key,
            token,
            sender,
            "Hello " + sender_name + "! 👋 Welcome to OffrO.\n\n"
            "We have received your message. Our team will get back to you shortly.\n\n"
            "📲 _OffrO — Discover local stores & exclusive deals_"
        ) == "retryable":
            outcome = "retry"

    if outcome == "retry":
        return outcome        # handlers run once, on the attempt that completes
    # Dispatch to registered message handlers
    for handler in _MESSAGE_HANDLERS:
        try:
            handler(sender=sender, name=sender_name, msg_type=msg_type,
                    text=text_body, msg_id=msg_id, timestamp=timestamp,
                    phone_number_id=phone_number_id, raw=msg)
        except Exception as e:
            logger.error("[WA-Webhook] Handler %s error (%s)", getattr(handler, "__name__", "handler"), exc_summary(e))
    return outcome


def _handle_consent_keyword(keyword: str, sender: str, msg: dict, key: str, may_reply: bool, token: str = "") -> str:
    """STOP/UNSUBSCRIBE/CANCEL/END/QUIT -> opt out; START -> explicit re-subscription. Returns 'ok' or 'retry'.

    * The confirmation is sent ONLY after the consent record is durably saved AND applied - never otherwise.
    * If the write FAILS: a monitored error is logged and a neutral 'please reply again' notice is sent (it claims
      nothing was saved). Unless that notice is known to have been delivered, Meta is asked to retry - that retry
      re-attempts the CONSENT WRITE (idempotent); it can never resend a reply (the reply slot is at-most-once).
    * Consent saved but the confirmation was definitively rejected with a temporary error: Meta is asked to retry
      (the consent write is idempotent; the confirmation is re-attempted once the slot was released). Permanent or
      uncertain confirmation outcomes are logged and NOT retried - the opt-out itself is already saved.
    * The welcome reply is NOT sent for these messages."""
    kw_text = consent.message_keyword_text(msg)
    stop = keyword == "stop"
    event = "opt_out" if stop else "opt_in"
    try:
        record = consent.record_opt_out if stop else consent.record_opt_in
        result = record(sender, kw_text, msg.get("id", ""), msg.get("timestamp", ""), whatsapp_id=True)
    except ValueError:
        logger.warning("[WA-Webhook] %s keyword from an invalid phone number ignored", event)
        return "ok"
    except Exception as e:
        # Monitored line: alert on "[WA-CONSENT-FAILURE]".
        logger.error("[WA-CONSENT-FAILURE] event=%s phone=%s msg_id=%s error=%s — no confirmation sent",
                     event, mask_phone(sender), msg.get("id", ""), exc_summary(e))
        notice = consent.STOP_FAILURE_NOTICE if stop else consent.START_FAILURE_NOTICE
        res = _auto_reply(key, token, sender, notice) if may_reply else "blocked"
        sent = res in ("sent", "already")
        consent.note_consent_failure(key, keyword, sent, token)
        if sent:
            return "ok"
        logger.error("[WA-CONSENT-FAILURE] event=%s phone=%s msg_id=%s failure notice NOT sent — requesting Meta retry",
                     event, mask_phone(sender), msg.get("id", ""))
        return "retry"

    if result == "applied":
        logger.info("[WA-Webhook] 🔕 Consent recorded for %s — %s", mask_phone(sender), event)
        if may_reply:
            res = _auto_reply(key, token, sender, consent.STOP_CONFIRMATION if stop else consent.START_CONFIRMATION)
            if res == "retryable":
                return "retry"
    else:
        logger.info("[WA-Webhook] Consent %s for %s superseded by a newer event — no confirmation",
                    event, mask_phone(sender))
    return "ok"


def _handler_name(index: int, handler) -> str:
    """Stable per-handler id for progress tracking (index disambiguates lambdas / same-named handlers)."""
    return "%d:%s" % (index, getattr(handler, "__qualname__", None) or getattr(handler, "__name__", "handler"))


def _handle_statuses(value: dict) -> bool:
    """Process message delivery status updates (sent / delivered / read / failed).

    Handler failures are retryable: the claim is marked 'failed' and True is returned (HTTP 500), so Meta
    redelivers it (bounded by MAX_ATTEMPTS). Progress is kept per handler: a retry runs ONLY the handlers that have
    not yet succeeded, and a finished event is never repeated. Handlers must nevertheless be IDEMPOTENT: if the claim
    store is down, progress cannot be recorded and a retry re-runs all of them."""
    needs_retry = False
    for status in _as_list(value.get("statuses")):
        if not isinstance(status, dict):
            continue
        msg_id      = status.get("id", "")
        recipient   = status.get("recipient_id", "")
        status_val  = status.get("status", "")
        timestamp   = status.get("timestamp", "")

        key = "status:%s:%s:%s" % (msg_id, status_val, timestamp) if msg_id else ""
        claim = consent.claim_event(key) if key else consent.Claim("claimed")
        if claim == "duplicate":
            logger.info("[WA-Webhook] ♻️  Duplicate status update ignored — msg_id=%s status=%s", msg_id, status_val)
            continue
        if claim == "abandoned":
            logger.warning("[WA-Webhook] Status update msg_id=%s abandoned after repeated stalled attempts", msg_id)
            continue
        if claim == "busy":
            logger.info("[WA-Webhook] Status update msg_id=%s is being processed by another worker — retry", msg_id)
            needs_retry = True
            continue
        tracked = bool(key) and claim != "unavailable"
        token = getattr(claim, "token", "") if tracked else ""

        logger.info(
            "[WA-Webhook] 📋 Status update — msg_id=%s recipient=%s status=%s ts=%s",
            msg_id, mask_phone(recipient), status_val, timestamp,
        )

        already = consent.handlers_done(key) if tracked and claim == "retry" else set()
        failed = False
        for i, handler in enumerate(_STATUS_HANDLERS):
            name = _handler_name(i, handler)
            if name in already:
                continue
            try:
                handler(msg_id=msg_id, recipient=recipient,
                        status=status_val, timestamp=timestamp, raw=status)
            except Exception as e:
                failed = True
                logger.error("[WA-Webhook] Status handler %s error (%s)", getattr(handler, "__name__", "handler"),
                             exc_summary(e))
                continue
            if tracked:
                consent.mark_handler_done(key, token, name)

        if not failed:
            if tracked:
                consent.set_event_state(key, "done", token)
        elif tracked:
            consent.set_event_state(key, "failed", token)
            needs_retry = True
        elif key:                     # claim store down: cannot track or bound retries, ask Meta to redeliver
            needs_retry = True
        # no message id: nothing to track and the same bytes would fail again - not retried
    return needs_retry


def _handle_errors(value: dict):
    """Log any errors Meta reports in the webhook payload (code only; free-text details are withheld)."""
    for err in _as_list(value.get("errors")):
        if not isinstance(err, dict):
            continue
        code = str(err.get("code"))
        # Code only: Meta's title/details are free text (can embed names, numbers, message text) and are withheld.
        logger.error("[WA-Webhook] ⚠️  Meta error — code=%s (details withheld)", code if code.isdigit() else "unknown")


# ── Handler registries (extend without touching the router) ────────────────────
# To add a new message handler:
#   def my_handler(sender, name, msg_type, text, msg_id, timestamp, phone_number_id, raw):
#       ...
#   _MESSAGE_HANDLERS.append(my_handler)
#
# To reply inside a handler:
#   reply_to_sender(sender, "your reply text here")

_MESSAGE_HANDLERS: list = []   # callables(sender, name, msg_type, text, msg_id, timestamp, phone_number_id, raw)
_STATUS_HANDLERS: list  = []   # callables(msg_id, recipient, status, timestamp, raw)
