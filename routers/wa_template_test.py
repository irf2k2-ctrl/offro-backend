"""
routers/wa_template_test.py — admin-only, SINGLE-recipient WhatsApp template TEST
================================================================================
POST /admin/whatsapp/template-test

Sends the approved marketing template `offro_merchant_invitation` (image header, static body,
static "Download OffrO" URL button) to ONE opted-in recipient, using the existing
services.whatsapp.send_whatsapp_template().

Safety by design:
  * disabled unless WHATSAPP_TEMPLATE_TEST_ENABLED=1 (new env var, off by default)
  * admin-authenticated (same get_current_admin as the live-chat endpoints)
  * template name is a constant - the caller cannot pick another template
  * exactly one phone number per call; lists / comma-separated values are rejected
  * explicit opt-in confirmation + a written consent note are required and stored in an audit log
  * per-admin rate limit and a per-recipient 24h cool-down (so it cannot be looped into a bulk send)
  * the live template status is fetched from Meta first and the send is refused unless it is APPROVED
  * `dry_run: true` performs every check and returns the payload WITHOUT sending anything
  * access tokens are never returned or logged; the recipient is logged only as its last 4 digits
Nothing here is triggered automatically and no merchant list is ever read.
"""
import hashlib
import logging
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, Depends, HTTPException, Request

from database import db
from routers.wa_chat import get_current_admin
from services import whatsapp as wa
from services import wa_campaign_audit as campaign_audit
from services import wa_consent as consent

logger = logging.getLogger("offro.wa_template_test")
router = APIRouter(tags=["WhatsApp Template Test"])

TEMPLATE_NAME = "offro_merchant_invitation"
TEMPLATE_LANG = "en"
AUDIT = "whatsapp_template_tests"
MAX_SENDS_PER_ADMIN_PER_HOUR = 5
RECIPIENT_COOLDOWN = timedelta(hours=24)

_PHONE_RE = re.compile(r"^[1-9][0-9]{7,14}$")        # E.164 digits, country code included, no leading 0
_MEDIA_ID_RE = re.compile(r"^[0-9]{5,32}$")
_VAR_RE = re.compile(r"\{\{\s*\d+\s*\}\}")


def _enabled() -> bool:
    return os.getenv("WHATSAPP_TEMPLATE_TEST_ENABLED", "").strip().lower() in ("1", "true", "yes")


def _redact(text) -> str:
    """Never let a credential reach a response or a log line."""
    out = str(text or "")
    tok = wa.WHATSAPP_ACCESS_TOKEN
    if tok:
        out = out.replace(tok, "***")
    return re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer ***", out)[:500]


def normalize_phone(raw) -> str:
    """One E.164 number (digits only). Raises 400 for anything else, including lists and bulk-looking input."""
    if not isinstance(raw, str):
        raise HTTPException(400, "phone must be a single phone number string")
    if re.search(r"[,;\n\r|]", raw) or len(raw.split()) > 3:
        raise HTTPException(400, "Only ONE phone number per request is allowed")
    digits = re.sub(r"[\s\-()]", "", raw)
    if digits.startswith("+"):
        digits = digits[1:]
    if not _PHONE_RE.match(digits):
        raise HTTPException(400, "phone must be in international format with country code, e.g. 919876543210")
    return digits


def _header_param(body: dict) -> tuple:
    """Exactly one of header_media_id (from POST /admin/whatsapp/upload) or header_image_url (public https)."""
    media_id = str(body.get("header_media_id") or "").strip()
    url = str(body.get("header_image_url") or "").strip()
    if bool(media_id) == bool(url):
        raise HTTPException(400, "Provide exactly one of header_media_id or header_image_url")
    if media_id:
        if not _MEDIA_ID_RE.match(media_id):
            raise HTTPException(400, "header_media_id is not a valid WhatsApp media id")
        return "id", {"id": media_id}
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or len(url) > 2048:
        raise HTTPException(400, "header_image_url must be a public https URL")
    return "link", {"link": url}


def fetch_template_status() -> dict:
    """Ask Meta for the live state of the template. Returns {"status","category","components"} or raises HTTPException."""
    if not wa.WHATSAPP_ACCESS_TOKEN or not wa.WHATSAPP_PHONE_NUMBER_ID or not wa.WHATSAPP_BUSINESS_ACCOUNT_ID:
        raise HTTPException(503, "WhatsApp is not fully configured (token, phone number id and business account id are required)")
    url = f"{wa._GRAPH_BASE}/{wa.WHATSAPP_BUSINESS_ACCOUNT_ID}/message_templates"
    try:
        r = requests.get(url, headers=wa._auth_headers(), timeout=10,
                         params={"name": TEMPLATE_NAME, "fields": "name,status,category,language,components", "limit": 20})
        data = r.json()
    except Exception as e:
        raise HTTPException(502, "Could not verify the template status with Meta: " + _redact(e))
    if r.status_code != 200:
        raise HTTPException(502, "Could not verify the template status with Meta: "
                            + _redact((data.get("error") or {}).get("message", r.status_code)))
    for t in data.get("data", []):
        if t.get("name") == TEMPLATE_NAME and t.get("language") == TEMPLATE_LANG:
            return {"status": t.get("status", ""), "category": t.get("category", ""),
                    "components": t.get("components", []) or []}
    raise HTTPException(404, f"Template '{TEMPLATE_NAME}' ({TEMPLATE_LANG}) was not found on this WhatsApp Business Account")


def check_template_shape(tpl: dict) -> None:
    """The send builds ONLY an image-header component. Refuse if the approved template needs anything else."""
    if tpl["status"] != "APPROVED":
        raise HTTPException(409, f"Template status is {tpl['status'] or 'UNKNOWN'}, not APPROVED - not sending")
    comps = {str(c.get("type", "")).upper(): c for c in tpl["components"]}
    header = comps.get("HEADER") or {}
    if str(header.get("format", "")).upper() != "IMAGE":
        raise HTTPException(409, "The approved template no longer has an IMAGE header - not sending")
    if _VAR_RE.search((comps.get("BODY") or {}).get("text", "")):
        raise HTTPException(409, "The approved template body now has variables, which this test endpoint does not fill - not sending")
    for b in (comps.get("BUTTONS") or {}).get("buttons", []):
        if str(b.get("type", "")).upper() == "URL" and _VAR_RE.search(b.get("url", "")):
            raise HTTPException(409, "The template button URL is dynamic, which this test endpoint does not fill - not sending")


def _audit(admin, phone: str, status: str, **extra) -> object:
    return db[AUDIT].insert_one({
        "admin_id": str((admin or {}).get("_id", "")), "template": TEMPLATE_NAME,
        "phone_hash": hashlib.sha256(phone.encode()).hexdigest(), "phone_last4": phone[-4:],
        "status": status, "created_at": datetime.utcnow(), **extra}).inserted_id


def _enforce_limits(admin, phone: str) -> None:
    now = datetime.utcnow()
    admin_id = str((admin or {}).get("_id", ""))
    recent = db[AUDIT].count_documents({"admin_id": admin_id, "status": {"$in": ["attempting", "sent", "failed"]},
                                        "created_at": {"$gte": now - timedelta(hours=1)}})
    if recent >= MAX_SENDS_PER_ADMIN_PER_HOUR:
        raise HTTPException(429, f"Limit reached: at most {MAX_SENDS_PER_ADMIN_PER_HOUR} template test sends per hour")
    h = hashlib.sha256(phone.encode()).hexdigest()
    if db[AUDIT].find_one({"phone_hash": h, "status": {"$in": ["attempting", "sent"]},
                           "created_at": {"$gte": now - RECIPIENT_COOLDOWN}}):
        raise HTTPException(429, "This number already received (or is receiving) the test template in the last 24 hours")


def _enforce_not_opted_out(admin, phone: str) -> None:
    """Phase 2A: a number that has opted out (STOP) can never receive a test template, whatever the admin attests.
    Fails CLOSED: if the consent store cannot be read, nothing is sent. Runs before any Meta call and before the
    send-limit bookkeeping, so a blocked attempt consumes no quota."""
    try:
        status = consent.consent_status(phone)
    except Exception:
        raise HTTPException(503, "Could not verify this recipient's WhatsApp opt-out status - not sending")
    # status 'invalid' = the consent normalizer cannot canonicalize this number (e.g. '91' + 11 digits). Phase 1 refuses
    # to record consent for such numbers, so no opt-out can exist for them: nothing to block, behaviour unchanged.
    if status == "opted_out":
        campaign_audit.record("template_test.blocked_opted_out", campaign_audit.admin_ref(admin), outcome="blocked",
                              target=campaign_audit.phone_ref(phone))
        raise HTTPException(409, "This number has opted out of OffrO WhatsApp messages (STOP). It cannot be messaged "
                                 "until the person sends START themselves")


@router.post("/whatsapp/template-test")
async def send_template_test(request: Request, a=Depends(get_current_admin)):
    """Body: {"phone": "919876543210", "consent_confirmed": true, "consent_note": "...",
              "header_media_id": "123..."  |  "header_image_url": "https://...",  "dry_run": false}"""
    if not _enabled():
        raise HTTPException(403, "Template test sending is disabled (WHATSAPP_TEMPLATE_TEST_ENABLED is not set)")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object expected")

    phone = normalize_phone(body.get("phone"))
    if body.get("consent_confirmed") is not True:
        raise HTTPException(400, "consent_confirmed must be true: this recipient must have explicitly opted in to WhatsApp marketing messages from OffrO")
    note = str(body.get("consent_note") or "").strip()
    if len(note) < 10:
        raise HTTPException(400, "consent_note is required (how/when this recipient opted in)")
    kind, image = _header_param(body)
    dry_run = body.get("dry_run") is True
    _enforce_not_opted_out(a, phone)

    tpl = fetch_template_status()
    check_template_shape(tpl)
    components = [{"type": "header", "parameters": [{"type": "image", "image": image}]}]

    if dry_run:
        return {"ok": True, "dry_run": True, "sent": False, "template": TEMPLATE_NAME, "language": TEMPLATE_LANG,
                "template_status": tpl["status"], "category": tpl["category"], "to_last4": phone[-4:],
                "header_source": kind, "components": components}

    _enforce_limits(a, phone)
    audit_id = _audit(a, phone, "attempting", consent_note=note[:500], header_source=kind)
    logger.info("[WA-TemplateTest] sending %s to ***%s (admin=%s)", TEMPLATE_NAME, phone[-4:], (a or {}).get("_id"))
    result = wa.send_whatsapp_template(phone, TEMPLATE_NAME, TEMPLATE_LANG, components)
    if not result.get("ok"):
        err = _redact(result.get("error", "unknown"))
        db[AUDIT].update_one({"_id": audit_id}, {"$set": {"status": "failed", "error": err}})
        raise HTTPException(502, "WhatsApp template send failed: " + err)
    db[AUDIT].update_one({"_id": audit_id}, {"$set": {"status": "sent", "message_id": result.get("message_id", "")}})
    return {"ok": True, "sent": True, "message_id": result.get("message_id", ""), "template": TEMPLATE_NAME,
            "language": TEMPLATE_LANG, "template_status": tpl["status"], "to_last4": phone[-4:], "header_source": kind}
