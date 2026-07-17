"""
routers/wa_chat.py  — incoming image storage via MongoDB (same pattern as notification_images)
==============================================================================================
KEY FIX: _fetch_and_store_media() now:
  1. Calls fetch_media_url(media_id) → gets temporary Meta download URL
  2. Calls download_media(temp_url)  → downloads raw bytes with Bearer token
  3. Stores bytes as base64 in MongoDB 'wa_media_images' collection (same as notification_images)
  4. Saves a permanent /admin/whatsapp/media/{media_id} serving URL to the DB record

This replaces the broken approach of storing the temporary Meta URL directly.
Meta's temp URLs expire in ~5 minutes — the dashboard loads the chat later, so they were already gone.

All existing text-chat endpoints and outgoing image logic are UNCHANGED.
"""

import logging
import base64
import mimetypes
import io
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends, Request, UploadFile, File
from fastapi.responses import JSONResponse, Response
from bson import ObjectId
from database import db
from services.whatsapp import (
    send_whatsapp_message,
    send_whatsapp_image,
    upload_media_to_whatsapp,
    fetch_media_url,
    download_media,
)
import os

logger = logging.getLogger("offro.wa_chat")

router = APIRouter(tags=["WhatsApp Live Chat"])

BASE_URL = os.environ.get("BASE_URL", "https://offro-backend-production.up.railway.app")


# ── Auth ─────────────────────────────────────────────────────────────────────

def get_current_admin(request: Request):
    token = request.cookies.get("admin_token") or \
            request.headers.get("X-Admin-Token", "")
    if not token:
        raise HTTPException(401, "Not authenticated")
    a = db.admins.find_one({"token": token})
    if not a:
        raise HTTPException(403, "Invalid session")
    return a


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _upsert_customer(phone: str, name: str, whatsapp_id: str,
                     last_msg: str, ts: datetime) -> str:
    doc = db.customers_whatsapp.find_one({"phone_number": phone})
    if doc:
        db.customers_whatsapp.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "name":              name if name and name != "Unknown" else doc.get("name", "Unknown"),
                "last_message":      last_msg,
                "last_message_time": ts,
            }}
        )
        return str(doc["_id"])
    else:
        result = db.customers_whatsapp.insert_one({
            "phone_number":      phone,
            "whatsapp_id":       whatsapp_id,
            "name":              name,
            "last_message":      last_msg,
            "last_message_time": ts,
            "created_at":        ts,
            "unread_count":      0,
        })
        return str(result.inserted_id)


def _save_message(customer_id: str, direction: str, message: str,
                  msg_type: str, timestamp: datetime,
                  status: str = "received", whatsapp_msg_id: str = "",
                  media_id: str = "", media_url: str = "",
                  mime_type: str = "", caption: str = "") -> str:
    doc = {
        "customer_id":      customer_id,
        "direction":        direction,
        "message":          message,
        "message_type":     msg_type,
        "timestamp":        timestamp,
        "status":           status,
        "whatsapp_msg_id":  whatsapp_msg_id,
        "read":             direction == "outgoing",
    }
    if media_id:
        doc["media_id"]   = media_id
    if media_url:
        doc["media_url"]  = media_url
    if mime_type:
        doc["mime_type"]  = mime_type
    if caption:
        doc["caption"]    = caption

    result = db.whatsapp_chats.insert_one(doc)
    return str(result.inserted_id)


# ── Media storage: download from Meta → store in MongoDB → serve permanently ──

def _store_media_in_db(media_id: str, image_bytes: bytes, mime_type: str) -> str:
    """
    Store raw image bytes in MongoDB 'wa_media_images' collection as base64.
    Returns the permanent serving URL: /admin/whatsapp/media/{media_id}
    This follows the exact same pattern as /admin/notification-image/{img_id}.
    """
    ext = mimetypes.guess_extension(mime_type) or ".jpg"
    ext = ext.replace(".jpe", ".jpg")

    doc = {
        "_id":          media_id,
        "content_type": mime_type,
        "data":         base64.b64encode(image_bytes).decode(),
        "size":         len(image_bytes),
        "created":      datetime.utcnow().isoformat(),
    }
    db.wa_media_images.replace_one({"_id": media_id}, doc, upsert=True)

    url = BASE_URL + "/admin/whatsapp/media/" + media_id + ext
    logger.info("[WA-Chat] 💾 Media stored in DB — id=%s size=%d url=%s", media_id, len(image_bytes), url)
    return url


def _fetch_and_store_media(chat_doc_id: str, media_id: str):
    """
    Full pipeline: Meta media_id → download bytes → store in MongoDB → save permanent URL to chat record.

    Called synchronously right after inserting an incoming image message.
    Any failure is logged and swallowed — the message is already stored with a placeholder.
    """
    try:
        # Step 1: Resolve media_id → temporary Meta download URL
        meta_result = fetch_media_url(media_id)
        if not meta_result.get("ok"):
            logger.warning("[WA-Chat] ⚠️ fetch_media_url failed for id=%s: %s",
                           media_id, meta_result.get("error"))
            return

        temp_url  = meta_result["url"]
        mime_type = meta_result.get("mime_type", "image/jpeg")

        # Step 2: Download raw bytes from Meta (requires Bearer token)
        dl_result = download_media(temp_url)
        if not dl_result.get("ok"):
            logger.warning("[WA-Chat] ⚠️ download_media failed for id=%s: %s",
                           media_id, dl_result.get("error"))
            return

        image_bytes  = dl_result["data"]
        actual_mime  = dl_result.get("content_type", mime_type)

        # Step 3: Store in MongoDB and get permanent URL
        permanent_url = _store_media_in_db(media_id, image_bytes, actual_mime)

        # Step 4: Update the chat record with the permanent URL
        db.whatsapp_chats.update_one(
            {"_id": ObjectId(chat_doc_id)},
            {"$set": {"media_url": permanent_url, "mime_type": actual_mime}}
        )
        logger.info("[WA-Chat] ✅ Incoming image fully stored — id=%s bytes=%d", media_id, len(image_bytes))

    except Exception as e:
        logger.exception("[WA-Chat] _fetch_and_store_media error for id=%s: %s", media_id, e)


# ── GET /admin/whatsapp/media/{media_id} — serve stored media permanently ─────

@router.get("/whatsapp/media/{media_id_with_ext}")
def serve_whatsapp_media(media_id_with_ext: str):
    """
    Serve a stored WhatsApp media image from MongoDB.
    Permanent URL — never expires (unlike Meta's temp URLs).
    Format: /admin/whatsapp/media/{media_id}.jpg
    """
    # Strip extension to get bare media_id
    bare_id = media_id_with_ext.split(".")[0]
    doc = db.wa_media_images.find_one({"_id": bare_id})
    if not doc:
        raise HTTPException(404, "Media not found")

    image_bytes = base64.b64decode(doc["data"])
    content_type = doc.get("content_type", "image/jpeg")
    return Response(
        content=image_bytes,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=2592000"},  # 30 days
    )


# ── Public helper — called from webhook.py ────────────────────────────────────

def store_incoming_message(phone: str, name: str, whatsapp_id: str,
                           msg_type: str, text: str,
                           whatsapp_msg_id: str, unix_ts: str,
                           media_id: str = "", raw_msg: dict = None):
    """
    Called by webhook._handle_messages() to persist every incoming message.
    For image messages: extracts media_id, downloads image, stores permanently in MongoDB.
    """
    try:
        ts      = datetime.utcfromtimestamp(int(unix_ts)) if unix_ts else datetime.utcnow()
        raw_msg = raw_msg or {}

        # Extract media_id and caption from raw payload for media messages
        extracted_media_id = media_id
        extracted_caption  = ""

        if not extracted_media_id:
            for media_type in ("image", "document", "audio", "video", "sticker"):
                if msg_type == media_type and media_type in raw_msg:
                    block = raw_msg[media_type]
                    extracted_media_id = block.get("id", "")
                    extracted_caption  = block.get("caption", "")
                    break

        # Build display text for sidebar preview
        if msg_type == "text":
            display_text = text
        elif msg_type == "image":
            display_text = "📷 Photo" + (" — " + extracted_caption if extracted_caption else "")
        elif msg_type == "document":
            fname = (raw_msg.get("document") or {}).get("filename", "document")
            display_text = "📄 " + fname
        elif msg_type == "audio":
            display_text = "🎵 Voice message"
        elif msg_type == "video":
            display_text = "🎬 Video"
        elif msg_type == "sticker":
            display_text = "🎭 Sticker"
        else:
            display_text = "[" + msg_type + "]"

        cid = _upsert_customer(phone, name, whatsapp_id, display_text, ts)

        # Insert message record (media_url will be filled in by _fetch_and_store_media)
        chat_id = _save_message(
            cid, "incoming", display_text, msg_type, ts,
            status="received", whatsapp_msg_id=whatsapp_msg_id,
            media_id=extracted_media_id,
            caption=extracted_caption,
        )

        # For image messages: download + store permanently (synchronous but fast enough)
        if extracted_media_id and msg_type in ("image", "document", "video"):
            _fetch_and_store_media(chat_id, extracted_media_id)

        # Increment unread count
        db.customers_whatsapp.update_one(
            {"_id": ObjectId(cid)},
            {"$inc": {"unread_count": 1}}
        )
        logger.info("[WA-Chat] 💾 Stored %s from %s (%s) cid=%s", msg_type, phone, name, cid)
    except Exception as e:
        logger.exception("[WA-Chat] ❌ Failed to store incoming message: %s", e)


# ── GET /admin/whatsapp/chats ─────────────────────────────────────────────────

@router.get("/whatsapp/chats")
def list_chats(search: str = "", a=Depends(get_current_admin)):
    query = {}
    if search.strip():
        query = {"$or": [
            {"name":         {"$regex": search.strip(), "$options": "i"}},
            {"phone_number": {"$regex": search.strip(), "$options": "i"}},
        ]}
    customers = list(db.customers_whatsapp.find(query).sort("last_message_time", -1).limit(200))
    result = []
    for c in customers:
        result.append({
            "customer_id":       str(c["_id"]),
            "name":              c.get("name") or "Unknown",
            "phone_number":      c.get("phone_number", ""),
            "last_message":      c.get("last_message", ""),
            "last_message_time": c.get("last_message_time", "").isoformat()
                                  if isinstance(c.get("last_message_time"), datetime) else "",
            "unread_count":      c.get("unread_count", 0),
        })
    return {"customers": result, "total": len(result)}


# ── GET /admin/whatsapp/chats/{customer_id} ────────────────────────────────────

@router.get("/whatsapp/chats/{customer_id}")
def get_conversation(customer_id: str, a=Depends(get_current_admin)):
    try:
        oid = ObjectId(customer_id)
    except Exception:
        raise HTTPException(400, "Invalid customer_id")

    customer = db.customers_whatsapp.find_one({"_id": oid})
    if not customer:
        raise HTTPException(404, "Customer not found")

    messages = list(db.whatsapp_chats.find({"customer_id": customer_id}).sort("timestamp", 1).limit(500))

    msgs_out = []
    for m in messages:
        msgs_out.append({
            "id":           str(m["_id"]),
            "direction":    m.get("direction", "incoming"),
            "message":      m.get("message", ""),
            "message_type": m.get("message_type", "text"),
            "timestamp":    m.get("timestamp", "").isoformat()
                            if isinstance(m.get("timestamp"), datetime) else "",
            "status":       m.get("status", ""),
            "read":         m.get("read", False),
            "media_id":     m.get("media_id", ""),
            "media_url":    m.get("media_url", ""),
            "mime_type":    m.get("mime_type", ""),
            "caption":      m.get("caption", ""),
        })

    return {
        "customer": {
            "customer_id":  str(customer["_id"]),
            "name":         customer.get("name") or "Unknown",
            "phone_number": customer.get("phone_number", ""),
            "whatsapp_id":  customer.get("whatsapp_id", ""),
            "created_at":   customer.get("created_at", "").isoformat()
                            if isinstance(customer.get("created_at"), datetime) else "",
        },
        "messages": msgs_out,
        "total":    len(msgs_out),
    }


# ── POST /admin/whatsapp/send ─────────────────────────────────────────────────

@router.post("/whatsapp/send")
async def send_reply(request: Request, a=Depends(get_current_admin)):
    """
    Admin sends a reply to a customer.
    Accepts:
      { "customer_id": "...", "message": "text" }
      { "customer_id": "...", "media_id": "...", "caption": "..." }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    customer_id = (body.get("customer_id") or "").strip()
    message     = (body.get("message") or "").strip()
    media_id    = (body.get("media_id") or "").strip()
    caption     = (body.get("caption") or "").strip()

    if not customer_id:
        raise HTTPException(400, "customer_id is required")
    if not message and not media_id:
        raise HTTPException(400, "Either message or media_id is required")

    try:
        oid = ObjectId(customer_id)
    except Exception:
        raise HTTPException(400, "Invalid customer_id")

    customer = db.customers_whatsapp.find_one({"_id": oid})
    if not customer:
        raise HTTPException(404, "Customer not found")

    phone = customer.get("phone_number", "")
    if not phone:
        raise HTTPException(400, "Customer has no phone number on record")

    now = datetime.utcnow()

    if media_id:
        result = send_whatsapp_image(phone, media_id, caption)
        if not result.get("ok"):
            raise HTTPException(502, "WhatsApp image send failed: " + result.get("error", "unknown"))
        display = "📷 Photo" + (" — " + caption if caption else "")
        _save_message(customer_id, "outgoing", display, "image", now,
                      status="sent", whatsapp_msg_id=result.get("message_id", ""),
                      media_id=media_id, caption=caption)
        db.customers_whatsapp.update_one({"_id": oid}, {"$set": {"last_message": display, "last_message_time": now}})
        return {"ok": True, "message_id": result.get("message_id"), "type": "image"}
    else:
        result = send_whatsapp_message(phone, message)
        if not result.get("ok"):
            raise HTTPException(502, "WhatsApp send failed: " + result.get("error", "unknown"))
        _save_message(customer_id, "outgoing", message, "text", now,
                      status="sent", whatsapp_msg_id=result.get("message_id", ""))
        db.customers_whatsapp.update_one({"_id": oid}, {"$set": {"last_message": message, "last_message_time": now}})
        return {"ok": True, "message_id": result.get("message_id"), "type": "text"}


# ── POST /admin/whatsapp/upload ───────────────────────────────────────────────

@router.post("/whatsapp/upload")
async def upload_media(file: UploadFile = File(...), a=Depends(get_current_admin)):
    """Admin uploads an image → gets media_id for use in /admin/whatsapp/send."""
    ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    MAX_SIZE     = 5 * 1024 * 1024

    mime_type = file.content_type or "image/jpeg"
    if mime_type not in ALLOWED_MIME:
        raise HTTPException(400, "Unsupported file type: " + mime_type)

    file_bytes = await file.read()
    if len(file_bytes) > MAX_SIZE:
        raise HTTPException(400, "File too large (max 5MB)")
    if len(file_bytes) == 0:
        raise HTTPException(400, "Empty file")

    filename = file.filename or ("upload." + (mime_type.split("/")[-1]))
    result   = upload_media_to_whatsapp(file_bytes, mime_type, filename)

    if not result.get("ok"):
        raise HTTPException(502, "Media upload to WhatsApp failed: " + result.get("error", "unknown"))

    logger.info("[WA-Chat] 📤 Admin uploaded media — id=%s size=%d", result["media_id"], len(file_bytes))
    return {"ok": True, "media_id": result["media_id"], "mime_type": mime_type, "size": len(file_bytes)}


# ── POST /admin/whatsapp/chats/{customer_id}/read ─────────────────────────────

@router.post("/whatsapp/chats/{customer_id}/read")
def mark_read(customer_id: str, a=Depends(get_current_admin)):
    try:
        oid = ObjectId(customer_id)
    except Exception:
        raise HTTPException(400, "Invalid customer_id")

    customer = db.customers_whatsapp.find_one({"_id": oid})
    if not customer:
        raise HTTPException(404, "Customer not found")

    db.whatsapp_chats.update_many(
        {"customer_id": customer_id, "direction": "incoming", "read": False},
        {"$set": {"read": True}}
    )
    db.customers_whatsapp.update_one({"_id": oid}, {"$set": {"unread_count": 0}})
    return {"ok": True}
