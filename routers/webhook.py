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

import logging
from fastapi import APIRouter, Request, Query
from fastapi.responses import PlainTextResponse, JSONResponse
from services.whatsapp import WHATSAPP_VERIFY_TOKEN

logger = logging.getLogger("offro.webhook")

router = APIRouter(tags=["WhatsApp Webhook"])


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
        hub_verify_token == WHATSAPP_VERIFY_TOKEN,
    )

    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        logger.info("[WA-Webhook] ✅ Verification successful")
        return PlainTextResponse(content=hub_challenge, status_code=200)

    logger.warning("[WA-Webhook] ❌ Verification failed — invalid token or mode")
    return PlainTextResponse(content="Forbidden", status_code=403)


# ── POST /webhook — Incoming messages & status updates ─────────────────────────

@router.post("/webhook")
async def whatsapp_webhook(request: Request):
    """
    Meta sends all WhatsApp events here:
        - Incoming customer messages  (type: text, image, audio, interactive, etc.)
        - Message delivery status     (sent, delivered, read, failed)
        - Errors

    Current implementation: logs everything and returns 200 immediately.
    Meta REQUIRES a 200 response within 5 seconds — heavy processing must be async.

    Future handlers are registered in _HANDLERS below. Add new ones without
    touching this function.
    """
    try:
        body = await request.json()
    except Exception:
        logger.warning("[WA-Webhook] ⚠️  Received non-JSON body — ignoring")
        return JSONResponse({"status": "ok"})  # always 200 to Meta

    print("WHATSAPP PAYLOAD RECEIVED:")
    print(body)
    logger.debug("[WA-Webhook] Raw payload: %s", body)

    # Guard: only process whatsapp_business_account events
    if body.get("object") != "whatsapp_business_account":
        return JSONResponse({"status": "ok"})

    try:
        _process_webhook(body)
    except Exception as e:
        # Never let processing errors block the 200 response to Meta
        logger.exception("[WA-Webhook] ❌ Error processing webhook: %s", e)

    return JSONResponse({"status": "ok"})


# ── Internal processor ──────────────────────────────────────────────────────────

def _process_webhook(body: dict):
    """
    Walk the standard WhatsApp Cloud API webhook payload structure and
    dispatch each event to the appropriate handler.

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
    entries = body.get("entry", [])
    for entry in entries:
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            _handle_messages(value)
            _handle_statuses(value)
            _handle_errors(value)


def _handle_messages(value: dict):
    """Process incoming customer messages."""
    messages = value.get("messages", [])
    contacts = value.get("contacts", [])
    metadata = value.get("metadata", {})

    contact_map = {c["wa_id"]: c.get("profile", {}).get("name", "Unknown") for c in contacts}

    for msg in messages:
        sender     = msg.get("from", "")
        msg_id     = msg.get("id", "")
        msg_type   = msg.get("type", "")
        timestamp  = msg.get("timestamp", "")
        sender_name = contact_map.get(sender, "Unknown")
        phone_number_id = metadata.get("phone_number_id", "")

        # Extract text body if it's a text message
        text_body = ""
        if msg_type == "text":
            text_body = msg.get("text", {}).get("body", "")

        logger.info(
            "[WA-Webhook] 📩 Message from %s (%s) — type=%s id=%s ts=%s body=%r",
            sender, sender_name, msg_type, msg_id, timestamp, text_body,
        )

        # Dispatch to registered message handlers
        for handler in _MESSAGE_HANDLERS:
            try:
                handler(sender=sender, name=sender_name, msg_type=msg_type,
                        text=text_body, msg_id=msg_id, timestamp=timestamp,
                        phone_number_id=phone_number_id, raw=msg)
            except Exception as e:
                logger.error("[WA-Webhook] Handler %s error: %s", handler.__name__, e)


def _handle_statuses(value: dict):
    """Process message delivery status updates (sent / delivered / read / failed)."""
    statuses = value.get("statuses", [])
    for status in statuses:
        msg_id      = status.get("id", "")
        recipient   = status.get("recipient_id", "")
        status_val  = status.get("status", "")
        timestamp   = status.get("timestamp", "")

        logger.info(
            "[WA-Webhook] 📋 Status update — msg_id=%s recipient=%s status=%s ts=%s",
            msg_id, recipient, status_val, timestamp,
        )

        for handler in _STATUS_HANDLERS:
            try:
                handler(msg_id=msg_id, recipient=recipient,
                        status=status_val, timestamp=timestamp, raw=status)
            except Exception as e:
                logger.error("[WA-Webhook] Status handler %s error: %s", handler.__name__, e)


def _handle_errors(value: dict):
    """Log any errors Meta reports in the webhook payload."""
    errors = value.get("errors", [])
    for err in errors:
        logger.error(
            "[WA-Webhook] ⚠️  Meta error — code=%s title=%s details=%s",
            err.get("code"), err.get("title"), err.get("error_data", {}).get("details", ""),
        )


# ── Handler registries (extend without touching the router) ────────────────────
# To add a new message handler:
#   def my_handler(sender, name, msg_type, text, msg_id, timestamp, phone_number_id, raw):
#       ...
#   _MESSAGE_HANDLERS.append(my_handler)

_MESSAGE_HANDLERS: list = []   # callables(sender, name, msg_type, text, msg_id, timestamp, phone_number_id, raw)
_STATUS_HANDLERS: list  = []   # callables(msg_id, recipient, status, timestamp, raw)
