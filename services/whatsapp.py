"""
services/whatsapp.py  — with media support added
=================================================
NEW functions added (existing ones UNCHANGED):
  send_whatsapp_image(phone, media_id)           → send image by media_id to customer
  upload_media_to_whatsapp(file_bytes, mime)     → upload file, return media_id
  fetch_media_url(media_id)                      → resolve media_id → download URL
  download_media(media_url)                      → download bytes from resolved URL
"""

import os
import logging
import requests
import io

logger = logging.getLogger("offro.whatsapp")

# ── Config ─────────────────────────────────────────────────────────────────────
WHATSAPP_ACCESS_TOKEN        = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID     = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
WHATSAPP_VERIFY_TOKEN        = os.getenv("WHATSAPP_VERIFY_TOKEN", "offro_webhook_secret")

_GRAPH_API_VERSION = "v20.0"
_GRAPH_BASE        = "https://graph.facebook.com/" + _GRAPH_API_VERSION


def _messages_url() -> str:
    return _GRAPH_BASE + "/" + WHATSAPP_PHONE_NUMBER_ID + "/messages"


def _headers() -> dict:
    return {
        "Authorization": "Bearer " + WHATSAPP_ACCESS_TOKEN,
        "Content-Type":  "application/json",
    }


def _auth_headers() -> dict:
    """Auth-only headers (no Content-Type — for multipart/form-data uploads)."""
    return {"Authorization": "Bearer " + WHATSAPP_ACCESS_TOKEN}


# ══════════════════════════════════════════════════════════════════════════════
# EXISTING FUNCTIONS — UNCHANGED
# ══════════════════════════════════════════════════════════════════════════════

def send_whatsapp_message(phone_number: str, message: str) -> dict:
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        logger.error("[WA] WhatsApp env vars not configured — message NOT sent.")
        return {"ok": False, "error": "WhatsApp not configured (missing env vars)"}

    phone = phone_number.replace("+", "").replace(" ", "").replace("-", "")

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                phone,
        "type":              "text",
        "text":              {"preview_url": False, "body": message},
    }

    try:
        resp = requests.post(_messages_url(), headers=_headers(), json=payload, timeout=10)
        data = resp.json()
        if resp.status_code == 200 and "messages" in data:
            msg_id = data["messages"][0].get("id", "")
            logger.info("[WA] ✅ Message sent to %s — id=%s", phone, msg_id)
            return {"ok": True, "message_id": msg_id}
        else:
            error_msg = data.get("error", {}).get("message", str(data))
            logger.error("[WA] ❌ Failed to send to %s — %s", phone, error_msg)
            return {"ok": False, "error": error_msg}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "Request timed out"}
    except Exception as e:
        logger.exception("[WA] ❌ Unexpected error sending to %s", phone)
        return {"ok": False, "error": str(e)}


def send_whatsapp_template(phone_number, template_name, language_code="en", components=None):
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        return {"ok": False, "error": "WhatsApp not configured (missing env vars)"}
    phone = phone_number.replace("+", "").replace(" ", "").replace("-", "")
    template_payload = {"name": template_name, "language": {"code": language_code}}
    if components:
        template_payload["components"] = components
    payload = {"messaging_product": "whatsapp", "to": phone, "type": "template", "template": template_payload}
    try:
        resp = requests.post(_messages_url(), headers=_headers(), json=payload, timeout=10)
        data = resp.json()
        if resp.status_code == 200 and "messages" in data:
            return {"ok": True, "message_id": data["messages"][0].get("id", "")}
        return {"ok": False, "error": data.get("error", {}).get("message", str(data))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def notify_merchant_store_approved(merchant_phone, store_name):
    msg = ("✅ *OffrO Update*\n\nYour store *" + store_name + "* has been approved and is now live on the OffrO app!\n\n"
           "Customers can now discover your store and avail your offers.\n\n"
           "📲 Start adding products and deals from your Merchant Dashboard.")
    return send_whatsapp_message(merchant_phone, msg)


def notify_merchant_store_rejected(merchant_phone, store_name, reason=""):
    msg = "❌ *OffrO Update*\n\nUnfortunately, your store *" + store_name + "* was not approved at this time.\n"
    if reason:
        msg += "Reason: " + reason + "\n"
    msg += "\nPlease contact support at offroapp@gmail.com for assistance."
    return send_whatsapp_message(merchant_phone, msg)


def send_otp_message(phone_number, otp):
    msg = ("🔐 *OffrO Verification*\n\nYour OTP is: *" + otp + "*\n\n"
           "Valid for 5 minutes. Do not share this with anyone.\n\n"
           "_If you did not request this, ignore this message._")
    return send_whatsapp_message(phone_number, msg)


def send_order_update(customer_phone, status, details=""):
    msg = "📦 *OffrO Order Update*\n\nStatus: *" + status + "*"
    if details:
        msg += "\n" + details
    return send_whatsapp_message(customer_phone, msg)


def send_promotional_message(phone_number, promo_text):
    return send_whatsapp_message(phone_number, promo_text)


# ══════════════════════════════════════════════════════════════════════════════
# NEW — MEDIA FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_media_url(media_id: str) -> dict:
    """
    Resolve a WhatsApp media_id to a temporary download URL.

    Meta's webhook only gives you a media_id for image/audio/document messages.
    Call this first to get the real URL, then call download_media() to get bytes.

    Args:
        media_id: The media id from the webhook payload (msg["image"]["id"])

    Returns:
        {"ok": True,  "url": "https://lookaside.fbsbx.com/....", "mime_type": "image/jpeg", "file_size": 12345}
        {"ok": False, "error": "..."}

    Note: The URL returned by Meta expires in ~5 minutes. Download immediately.
    """
    if not WHATSAPP_ACCESS_TOKEN:
        return {"ok": False, "error": "WhatsApp not configured"}

    url = _GRAPH_BASE + "/" + media_id
    try:
        resp = requests.get(url, headers=_auth_headers(), timeout=10)
        data = resp.json()
        if resp.status_code == 200 and "url" in data:
            return {
                "ok":        True,
                "url":       data["url"],
                "mime_type": data.get("mime_type", "image/jpeg"),
                "file_size": data.get("file_size", 0),
                "sha256":    data.get("sha256", ""),
            }
        return {"ok": False, "error": data.get("error", {}).get("message", str(data))}
    except Exception as e:
        logger.exception("[WA] fetch_media_url error for id=%s", media_id)
        return {"ok": False, "error": str(e)}


def download_media(media_url: str) -> dict:
    """
    Download raw bytes from a Meta media URL (obtained via fetch_media_url).

    Args:
        media_url: The temporary URL returned by fetch_media_url.

    Returns:
        {"ok": True,  "data": bytes, "content_type": "image/jpeg"}
        {"ok": False, "error": "..."}
    """
    if not WHATSAPP_ACCESS_TOKEN:
        return {"ok": False, "error": "WhatsApp not configured"}

    try:
        resp = requests.get(
            media_url,
            headers=_auth_headers(),   # Meta requires the Bearer token even for the download
            timeout=20,
            stream=True,
        )
        if resp.status_code == 200:
            return {
                "ok":           True,
                "data":         resp.content,
                "content_type": resp.headers.get("Content-Type", "image/jpeg"),
            }
        return {"ok": False, "error": "HTTP " + str(resp.status_code)}
    except Exception as e:
        logger.exception("[WA] download_media error")
        return {"ok": False, "error": str(e)}


def upload_media_to_whatsapp(file_bytes: bytes, mime_type: str, filename: str = "image.jpg") -> dict:
    """
    Upload a local file to Meta's media servers and get back a media_id.
    Use this when admin wants to send an image to a customer.

    Meta Media Upload API:
        POST https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/media
        multipart/form-data: file + messaging_product + type

    Args:
        file_bytes:  Raw bytes of the image/document to upload.
        mime_type:   e.g. "image/jpeg", "image/png", "application/pdf"
        filename:    Optional filename for the multipart form.

    Returns:
        {"ok": True,  "media_id": "123456789"}
        {"ok": False, "error": "..."}
    """
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        return {"ok": False, "error": "WhatsApp not configured"}

    upload_url = _GRAPH_BASE + "/" + WHATSAPP_PHONE_NUMBER_ID + "/media"

    try:
        resp = requests.post(
            upload_url,
            headers=_auth_headers(),   # No Content-Type — requests sets multipart boundary
            files={"file": (filename, io.BytesIO(file_bytes), mime_type)},
            data={"messaging_product": "whatsapp", "type": mime_type},
            timeout=30,
        )
        data = resp.json()
        if resp.status_code == 200 and "id" in data:
            media_id = data["id"]
            logger.info("[WA] ✅ Media uploaded — id=%s mime=%s size=%d", media_id, mime_type, len(file_bytes))
            return {"ok": True, "media_id": media_id}
        return {"ok": False, "error": data.get("error", {}).get("message", str(data))}
    except Exception as e:
        logger.exception("[WA] upload_media_to_whatsapp error")
        return {"ok": False, "error": str(e)}


def send_whatsapp_image(phone_number: str, media_id: str, caption: str = "") -> dict:
    """
    Send an image message to a WhatsApp customer using a previously-uploaded media_id.

    Args:
        phone_number: Recipient in E.164 format without '+'.
        media_id:     The id returned by upload_media_to_whatsapp().
        caption:      Optional image caption (max 1024 chars).

    Returns:
        {"ok": True,  "message_id": "..."}
        {"ok": False, "error": "..."}
    """
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        return {"ok": False, "error": "WhatsApp not configured"}

    phone = phone_number.replace("+", "").replace(" ", "").replace("-", "")

    image_obj = {"id": media_id}
    if caption:
        image_obj["caption"] = caption[:1024]

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                phone,
        "type":              "image",
        "image":             image_obj,
    }

    try:
        resp = requests.post(_messages_url(), headers=_headers(), json=payload, timeout=15)
        data = resp.json()
        if resp.status_code == 200 and "messages" in data:
            msg_id = data["messages"][0].get("id", "")
            logger.info("[WA] ✅ Image sent to %s — msg_id=%s media_id=%s", phone, msg_id, media_id)
            return {"ok": True, "message_id": msg_id}
        error_msg = data.get("error", {}).get("message", str(data))
        logger.error("[WA] ❌ Image send failed to %s — %s", phone, error_msg)
        return {"ok": False, "error": error_msg}
    except Exception as e:
        logger.exception("[WA] send_whatsapp_image error")
        return {"ok": False, "error": str(e)}
