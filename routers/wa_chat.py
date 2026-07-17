"""
routers/wa_chat.py
------------------
OFFRO WhatsApp Live Chat — Admin API
Phase 1: No AI, manual replies only.

Endpoints:
    GET  /admin/whatsapp/chats                  — list customers with last message + unread count
    GET  /admin/whatsapp/chats/{customer_id}    — full conversation history for one customer
    POST /admin/whatsapp/send                   — admin sends a reply to a customer
    POST /admin/whatsapp/chats/{customer_id}/read — mark all messages as read

Collections created automatically:
    customers_whatsapp   — one doc per unique WhatsApp sender
    whatsapp_chats       — one doc per message (in or out)

Mounted in server.py:
    from routers import wa_chat
    app.include_router(wa_chat.router, prefix="/admin")
"""

import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from bson import ObjectId
from database import db
from services.whatsapp import send_whatsapp_message

logger = logging.getLogger("offro.wa_chat")

router = APIRouter(tags=["WhatsApp Live Chat"])


# ── Auth (reuse same cookie-based admin auth as admin.py) ─────────────────────

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
    """
    Create or update a customer_whatsapp record.
    Returns the customer _id as a string.
    """
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
                  status: str = "received", whatsapp_msg_id: str = "") -> str:
    """
    Persist one message into whatsapp_chats.
    direction: 'incoming' | 'outgoing'
    Returns inserted _id as string.
    """
    result = db.whatsapp_chats.insert_one({
        "customer_id":      customer_id,
        "direction":        direction,
        "message":          message,
        "message_type":     msg_type,
        "timestamp":        timestamp,
        "status":           status,
        "whatsapp_msg_id":  whatsapp_msg_id,
        "read":             direction == "outgoing",  # outgoing always read; incoming starts unread
    })
    return str(result.inserted_id)


# ── Public helper — called from webhook.py ────────────────────────────────────

def store_incoming_message(phone: str, name: str, whatsapp_id: str,
                           msg_type: str, text: str,
                           whatsapp_msg_id: str, unix_ts: str):
    """
    Called by webhook._handle_messages() to persist every incoming message.
    Safe to call even if DB is temporarily unavailable (logs error, doesn't raise).
    """
    try:
        ts = datetime.utcfromtimestamp(int(unix_ts)) if unix_ts else datetime.utcnow()
        display_text = text if msg_type == "text" else "[" + msg_type + "]"

        cid = _upsert_customer(phone, name, whatsapp_id, display_text, ts)
        _save_message(cid, "incoming", display_text, msg_type, ts,
                      status="received", whatsapp_msg_id=whatsapp_msg_id)

        # Increment unread count
        db.customers_whatsapp.update_one(
            {"_id": ObjectId(cid)},
            {"$inc": {"unread_count": 1}}
        )
        logger.info("[WA-Chat] 💾 Stored message from %s (%s) cid=%s", phone, name, cid)
    except Exception as e:
        logger.exception("[WA-Chat] ❌ Failed to store incoming message: %s", e)


# ── GET /admin/whatsapp/chats ─────────────────────────────────────────────────

@router.get("/whatsapp/chats")
def list_chats(
    search: str = "",
    a=Depends(get_current_admin)
):
    """
    Return all customers with last message preview + unread count.
    Optional ?search= filters by name or phone number.
    Sorted by last_message_time descending (newest first).
    """
    query = {}
    if search.strip():
        query = {"$or": [
            {"name":         {"$regex": search.strip(), "$options": "i"}},
            {"phone_number": {"$regex": search.strip(), "$options": "i"}},
        ]}

    customers = list(
        db.customers_whatsapp.find(query)
        .sort("last_message_time", -1)
        .limit(200)
    )

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
    """
    Return full message history for one customer, oldest first.
    Also returns customer profile info.
    """
    try:
        oid = ObjectId(customer_id)
    except Exception:
        raise HTTPException(400, "Invalid customer_id")

    customer = db.customers_whatsapp.find_one({"_id": oid})
    if not customer:
        raise HTTPException(404, "Customer not found")

    messages = list(
        db.whatsapp_chats.find({"customer_id": customer_id})
        .sort("timestamp", 1)
        .limit(500)
    )

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
    Admin sends a manual WhatsApp reply to a customer.

    Request body:
        { "customer_id": "...", "message": "..." }

    Flow:
        1. Look up customer phone from DB
        2. Call existing send_whatsapp_message()
        3. Save outgoing message to whatsapp_chats
        4. Update last_message on customer record
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    customer_id = (body.get("customer_id") or "").strip()
    message     = (body.get("message") or "").strip()

    if not customer_id:
        raise HTTPException(400, "customer_id is required")
    if not message:
        raise HTTPException(400, "message cannot be empty")

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

    # Send via Meta Graph API
    result = send_whatsapp_message(phone, message)
    print("[WA-Chat] send result:", result)

    if not result.get("ok"):
        raise HTTPException(502, "WhatsApp send failed: " + result.get("error", "unknown"))

    # Persist outgoing message
    now = datetime.utcnow()
    _save_message(
        customer_id, "outgoing", message, "text", now,
        status="sent", whatsapp_msg_id=result.get("message_id", "")
    )

    # Update customer last_message
    db.customers_whatsapp.update_one(
        {"_id": oid},
        {"$set": {"last_message": message, "last_message_time": now}}
    )

    logger.info("[WA-Chat] 📤 Admin reply sent to %s — msg_id=%s", phone, result.get("message_id"))
    return {"ok": True, "message_id": result.get("message_id")}


# ── POST /admin/whatsapp/chats/{customer_id}/read ─────────────────────────────

@router.post("/whatsapp/chats/{customer_id}/read")
def mark_as_read(customer_id: str, a=Depends(get_current_admin)):
    """Mark all incoming messages for this customer as read. Resets unread_count to 0."""
    try:
        oid = ObjectId(customer_id)
    except Exception:
        raise HTTPException(400, "Invalid customer_id")

    db.whatsapp_chats.update_many(
        {"customer_id": customer_id, "direction": "incoming", "read": False},
        {"$set": {"read": True}}
    )
    db.customers_whatsapp.update_one(
        {"_id": oid},
        {"$set": {"unread_count": 0}}
    )
    return {"ok": True}
