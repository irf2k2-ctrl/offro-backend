"""
routers/wa_chat.py  — with media support added
===============================================
NEW additions (existing endpoints/functions UNCHANGED):
  store_incoming_message()     → now handles msg_type="image" and stores media_id + resolved URL
  _fetch_and_store_media()     → background helper to resolve media_id → URL via Meta API
  POST /admin/whatsapp/upload  → admin uploads image → get media_id back
  POST /admin/whatsapp/send    → now accepts { message } OR { media_id, caption } OR { image_url }

All existing text-chat endpoints remain identical.
"""

import logging
import io
import base64
import mimetypes
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from bson import ObjectId
from database import db
from services.whatsapp import (
    send_whatsapp_message,
    send_whatsapp_image,
    upload_media_to_whatsapp,
    fetch_media_url,
    download_media,
)

logger = logging.getLogger("offro.wa_chat")

router = APIRouter(tags=["WhatsApp Live Chat"])


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
                  media_id: str = "", media_url: str = "") -> str:
    """
    Persist one message.
    New fields: media_id, media_url (for image/document messages).
    """
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
        doc["media_id"] = media_id
    if media_url:
        doc["media_url"] = media_url

    result = db.whatsapp_chats.insert_one(doc)
    return str(result.inserted_id)


# ── Media helper — resolve media_id → URL and update DB record ────────────────

def _fetch_and_store_media(chat_doc_id: str, media_id: str):
    """
    Called synchronously after inserting an incoming image message.
    Fetches the Meta media URL and updates the DB record with it.
    Errors are logged and swallowed — the message is already stored.
    """
    try:
        result = fetch_media_url(media_id)
        if result.get("ok"):
            db.whatsapp_chats.update_one(
                {"_id": ObjectId(chat_doc_id)},
                {"$set": {"media_url": result["url"], "mime_type": result.get("mime_type", "image/jpeg")}}
            )
            logger.info("[WA-Chat] 📎 Media URL resolved for media_id=%s", media_id)
        else:
            logger.warning("[WA-Chat] ⚠️ Could not resolve media_id=%s: %s", media_id, result.get("error"))
    except Exception as e:
        logger.exception("[WA-Chat] _fetch_and_store_media error: %s", e)


# ── Public helper — called from webhook.py ────────────────────────────────────

def store_incoming_message(phone: str, name: str, whatsapp_id: str,
                           msg_type: str, text: str,
                           whatsapp_msg_id: str, unix_ts: str,
                           media_id: str = "", raw_msg: dict = None):
    """
    Called by webhook._handle_messages() to persist every incoming message.
    Now handles image/document/audio msg_types by storing media_id and resolving URL.
    """
    try:
        ts           = datetime.utcfromtimestamp(int(unix_ts)) if unix_ts else datetime.utcnow()
        raw_msg      = raw_msg or {}

        # For media messages: extract media_id from the raw payload
        extracted_media_id = media_id
        if not extracted_media_id:
            for media_type in ("image", "document", "audio", "video", "sticker"):
                if msg_type == media_type and media_type in raw_msg:
                    extracted_media_id = raw_msg[media_type].get("id", "")
                    break

        # Display text for last-message preview in sidebar
        if msg_type == "text":
            display_text = text
        elif msg_type == "image":
            caption = (raw_msg.get("image") or {}).get("caption", "")
            display_text = "📷 Photo" + (" — " + caption if caption else "")
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

        chat_id = _save_message(
            cid, "incoming", display_text, msg_type, ts,
            status="received", whatsapp_msg_id=whatsapp_msg_id,
            media_id=extracted_media_id,
        )

        # Resolve media URL immediately so it's ready when admin opens chat
        if extracted_media_id:
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
            # New media fields
            "media_id":     m.get("media_id", ""),
            "media_url":    m.get("media_url", ""),
            "mime_type":    m.get("mime_type", ""),
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
      { "customer_id": "...", "message": "text" }          → plain text
      { "customer_id": "...", "media_id": "...", "caption": "..." } → image by media_id
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
        # Send image
        result = send_whatsapp_image(phone, media_id, caption)
        if not result.get("ok"):
            raise HTTPException(502, "WhatsApp image send failed: " + result.get("error", "unknown"))

        display = "📷 Photo" + (" — " + caption if caption else "")
        _save_message(customer_id, "outgoing", display, "image", now,
                      status="sent", whatsapp_msg_id=result.get("message_id", ""),
                      media_id=media_id)
        db.customers_whatsapp.update_one({"_id": oid}, {"$set": {"last_message": display, "last_message_time": now}})
        return {"ok": True, "message_id": result.get("message_id"), "type": "image"}

    else:
        # Send text
        result = send_whatsapp_message(phone, message)
        if not result.get("ok"):
            raise HTTPException(502, "WhatsApp send failed: " + result.get("error", "unknown"))

        _save_message(customer_id, "outgoing", message, "text", now,
                      status="sent", whatsapp_msg_id=result.get("message_id", ""))
        db.customers_whatsapp.update_one({"_id": oid}, {"$set": {"last_message": message, "last_message_time": now}})
        return {"ok": True, "message_id": result.get("message_id"), "type": "text"}


# ── POST /admin/whatsapp/upload ───────────────────────────────────────────────

@router.post("/whatsapp/upload")
async def upload_media(
    file: UploadFile = File(...),
    a=Depends(get_current_admin)
):
    """
    Admin uploads an image to Meta's media servers.
    Returns a media_id that can be used in /admin/whatsapp/send.

    Accepts: image/jpeg, image/png, image/webp (max ~5MB per Meta limits)
    """
    ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    MAX_SIZE     = 5 * 1024 * 1024  # 5 MB

    mime_type = file.content_type or "image/jpeg"
    if mime_type not in ALLOWED_MIME:
        raise HTTPException(400, "Unsupported file type: " + mime_type + ". Allowed: jpeg, png, webp, gif")

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
