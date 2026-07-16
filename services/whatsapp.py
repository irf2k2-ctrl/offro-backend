"""
services/whatsapp.py
--------------------
OFFRO WhatsApp Cloud API service module.

Usage:
    from services.whatsapp import send_whatsapp_message, send_whatsapp_template

Environment variables required:
    WHATSAPP_ACCESS_TOKEN         - Meta permanent / long-lived access token
    WHATSAPP_PHONE_NUMBER_ID      - The Phone Number ID from Meta Business Manager
    WHATSAPP_BUSINESS_ACCOUNT_ID  - WhatsApp Business Account ID (for logs / future reference)
    WHATSAPP_VERIFY_TOKEN         - Any secret string you choose — used for webhook verification

All functions are synchronous (using `requests`), keeping the footprint small and
consistent with the rest of the OFFRO backend (no new async HTTP libraries needed).
"""

import os
import logging
import requests

logger = logging.getLogger("offro.whatsapp")

# ── Config ─────────────────────────────────────────────────────────────────────
WHATSAPP_ACCESS_TOKEN        = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID     = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
WHATSAPP_VERIFY_TOKEN        = os.getenv("WHATSAPP_VERIFY_TOKEN", "offro_webhook_secret")

_GRAPH_API_VERSION = "v20.0"
_GRAPH_BASE        = "https://graph.facebook.com/" + _GRAPH_API_VERSION


def _messages_url() -> str:
    """Build the Graph API messages endpoint URL for the configured phone number."""
    return _GRAPH_BASE + "/" + WHATSAPP_PHONE_NUMBER_ID + "/messages"


def _headers() -> dict:
    return {
        "Authorization": "Bearer " + WHATSAPP_ACCESS_TOKEN,
        "Content-Type":  "application/json",
    }


# ── Core send function ──────────────────────────────────────────────────────────

def send_whatsapp_message(phone_number: str, message: str) -> dict:
    """
    Send a plain-text WhatsApp message to a phone number.

    Args:
        phone_number: Recipient number in E.164 format WITHOUT the '+' prefix.
                      Example: '919876543210' for Indian number +91 98765 43210.
        message:      UTF-8 text body (max 4096 chars per WhatsApp Cloud API limit).

    Returns:
        dict with keys:
            ok      (bool)   – True if accepted by Meta.
            message_id (str) – WhatsApp message ID on success.
            error   (str)    – Error description on failure (only present on failure).

    Raises:
        Does NOT raise — always returns a dict so callers can decide how to handle.
    """
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        logger.error("[WA] WhatsApp env vars not configured — message NOT sent.")
        return {"ok": False, "error": "WhatsApp not configured (missing env vars)"}

    # Normalise phone: strip leading '+', spaces, dashes
    phone = phone_number.replace("+", "").replace(" ", "").replace("-", "")

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                phone,
        "type":              "text",
        "text":              {"preview_url": False, "body": message},
    }

    try:
        resp = requests.post(
            _messages_url(),
            headers=_headers(),
            json=payload,
            timeout=10,
        )
        data = resp.json()

        if resp.status_code == 200 and "messages" in data:
            msg_id = data["messages"][0].get("id", "")
            logger.info("[WA] ✅ Message sent to %s — id=%s", phone, msg_id)
            return {"ok": True, "message_id": msg_id}
        else:
            error_detail = data.get("error", {})
            error_msg    = error_detail.get("message", str(data))
            logger.error("[WA] ❌ Failed to send to %s — %s", phone, error_msg)
            return {"ok": False, "error": error_msg}

    except requests.exceptions.Timeout:
        logger.error("[WA] ❌ Timeout sending to %s", phone)
        return {"ok": False, "error": "Request timed out"}
    except Exception as e:
        logger.exception("[WA] ❌ Unexpected error sending to %s", phone)
        return {"ok": False, "error": str(e)}


# ── Template message (for pre-approved Meta templates) ─────────────────────────

def send_whatsapp_template(
    phone_number: str,
    template_name: str,
    language_code: str = "en",
    components: list = None,
) -> dict:
    """
    Send a pre-approved WhatsApp template message.

    Args:
        phone_number:   E.164 number without '+'.
        template_name:  Exact template name as approved in Meta Business Manager.
        language_code:  e.g. 'en', 'en_US', 'hi' — must match your template's language.
        components:     Optional list of component dicts (header/body/button variable params).
                        See: https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-message-templates

    Returns:
        Same shape as send_whatsapp_message().

    Future use-cases in OFFRO:
        - 'merchant_approved'   → notify merchant their store went live
        - 'otp_verification'    → customer login OTP (needs OTP template approved by Meta)
        - 'order_confirmed'     → customer order updates
        - 'promo_blast'         → promotional messages (must be opt-in compliant)
        - 'support_reply'       → support chat responses
    """
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        logger.error("[WA] WhatsApp env vars not configured — template NOT sent.")
        return {"ok": False, "error": "WhatsApp not configured (missing env vars)"}

    phone = phone_number.replace("+", "").replace(" ", "").replace("-", "")

    template_payload = {
        "name":     template_name,
        "language": {"code": language_code},
    }
    if components:
        template_payload["components"] = components

    payload = {
        "messaging_product": "whatsapp",
        "to":                phone,
        "type":              "template",
        "template":          template_payload,
    }

    try:
        resp = requests.post(
            _messages_url(),
            headers=_headers(),
            json=payload,
            timeout=10,
        )
        data = resp.json()

        if resp.status_code == 200 and "messages" in data:
            msg_id = data["messages"][0].get("id", "")
            logger.info("[WA] ✅ Template '%s' sent to %s — id=%s", template_name, phone, msg_id)
            return {"ok": True, "message_id": msg_id}
        else:
            error_detail = data.get("error", {})
            error_msg    = error_detail.get("message", str(data))
            logger.error("[WA] ❌ Template '%s' failed for %s — %s", template_name, phone, error_msg)
            return {"ok": False, "error": error_msg}

    except requests.exceptions.Timeout:
        return {"ok": False, "error": "Request timed out"}
    except Exception as e:
        logger.exception("[WA] ❌ Unexpected error sending template to %s", phone)
        return {"ok": False, "error": str(e)}


# ── Convenience wrappers (ready-to-call for future features) ───────────────────

def notify_merchant_store_approved(merchant_phone: str, store_name: str) -> dict:
    """Notify a merchant that their store was approved. Uses plain text until
    a template is approved by Meta."""
    msg = (
        "✅ *OffrO Update*\n\n"
        "Your store *" + store_name + "* has been approved and is now live on the OffrO app!\n\n"
        "Customers can now discover your store and avail your offers.\n\n"
        "📲 Start adding products and deals from your Merchant Dashboard."
    )
    return send_whatsapp_message(merchant_phone, msg)


def notify_merchant_store_rejected(merchant_phone: str, store_name: str, reason: str = "") -> dict:
    """Notify a merchant that their store submission was rejected."""
    msg = (
        "❌ *OffrO Update*\n\n"
        "Unfortunately, your store *" + store_name + "* was not approved at this time.\n"
    )
    if reason:
        msg += "Reason: " + reason + "\n"
    msg += "\nPlease contact support at offroapp@gmail.com for assistance."
    return send_whatsapp_message(merchant_phone, msg)


def send_otp_message(phone_number: str, otp: str) -> dict:
    """Send an OTP for customer login verification.
    NOTE: For production OTP use, create a Meta-approved 'authentication' template
    and replace this plain-text call with send_whatsapp_template()."""
    msg = (
        "🔐 *OffrO Verification*\n\n"
        "Your OTP is: *" + otp + "*\n\n"
        "Valid for 5 minutes. Do not share this with anyone.\n\n"
        "_If you did not request this, ignore this message._"
    )
    return send_whatsapp_message(phone_number, msg)


def send_order_update(customer_phone: str, status: str, details: str = "") -> dict:
    """Send an order / redemption status update to a customer."""
    msg = "📦 *OffrO Order Update*\n\nStatus: *" + status + "*"
    if details:
        msg += "\n" + details
    return send_whatsapp_message(customer_phone, msg)


def send_promotional_message(phone_number: str, promo_text: str) -> dict:
    """
    Send a promotional message.
    IMPORTANT: WhatsApp requires user opt-in for promotional messages.
    Only call this for users who have explicitly opted in.
    Using a Meta-approved template is strongly recommended for compliance.
    """
    return send_whatsapp_message(phone_number, promo_text)
