"""
routers/wa_campaign_media.py
----------------------------
Phase 2G dedicated header-media upload for WhatsApp campaign templates, mounted by routers/wa_campaigns.py.

    POST /media/header     edit   multipart `file` -> uploaded to Meta's media store -> {"header_media": {"type", "id"}}

This REPLACES using the general admin image-upload routes for campaign media: it is gated by the WhatsApp Campaigns permission,
accepts only the formats WhatsApp template headers allow, checks size AND the file's real content (magic bytes) rather than trusting the
declared type, and stores nothing in OffrO's database or CDN. The returned object is exactly what PATCH /campaigns/{id} accepts as
`header_media`. Uploading is NOT sending: no message is sent and the kill switch is not involved.
"""
import logging
import os
import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from routers.dashboard_auth import require_permission
from services import wa_campaign_audit as audit
from services import whatsapp as WA
from services.wa_webhook_security import exc_summary

logger = logging.getLogger("offro.wa_campaigns.media")

MODULE = "WhatsApp Campaigns"
router = APIRouter(prefix="/media", tags=["WhatsApp Campaigns - Media"])

MB = 1024 * 1024
# mime -> (header type, max bytes). Limits are deliberately below Meta's own (image 5 MB, video 16 MB, document 100 MB).
ALLOWED = {"image/jpeg": ("image", 5 * MB), "image/png": ("image", 5 * MB),
           "video/mp4": ("video", 16 * MB), "video/3gpp": ("video", 16 * MB),
           "application/pdf": ("document", 10 * MB)}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


def _looks_like(mime: str, head: bytes) -> bool:
    if mime == "image/jpeg":
        return head[:3] == b"\xff\xd8\xff"
    if mime == "image/png":
        return head[:8] == b"\x89PNG\r\n\x1a\n"
    if mime in ("video/mp4", "video/3gpp"):
        return head[4:8] == b"ftyp"
    if mime == "application/pdf":
        return head[:5] == b"%PDF-"
    return False


def _safe_filename(name, mime):
    base = _SAFE_NAME.sub("_", os.path.basename(str(name or "")).strip())[:80]
    return base or ("upload." + {"image/jpeg": "jpg", "image/png": "png", "video/mp4": "mp4", "video/3gpp": "3gp",
                                 "application/pdf": "pdf"}[mime])


@router.post("/header")
async def upload_header_media(file: UploadFile = File(...), user: dict = Depends(require_permission(MODULE, "edit"))):
    admin = audit.admin_ref(user)
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in ALLOWED:
        audit.record("campaign.media_upload", admin, outcome="blocked:unsupported_type")
        raise HTTPException(415, {"code": "unsupported_type", "message": "Allowed: JPEG, PNG (image), MP4, 3GP (video), PDF (document)."})
    kind, limit = ALLOWED[mime]
    data = await file.read(limit + 1)                                   # never reads more than the limit + 1 byte
    if not data:
        raise HTTPException(400, {"code": "empty_file", "message": "The file is empty."})
    if len(data) > limit:
        audit.record("campaign.media_upload", admin, outcome="blocked:too_large", details={"type": kind})
        raise HTTPException(413, {"code": "file_too_large", "message": "Maximum size for %s is %d MB." % (kind, limit // MB)})
    if not _looks_like(mime, data[:16]):
        audit.record("campaign.media_upload", admin, outcome="blocked:content_mismatch", details={"type": kind})
        raise HTTPException(415, {"code": "content_mismatch", "message": "The file content does not match its declared type."})
    try:
        out = WA.upload_media_to_whatsapp(data, mime, _safe_filename(file.filename, mime))
    except Exception as e:
        logger.error("[WA-Campaign] media upload raised (%s)", exc_summary(e))
        out = {"ok": False}
    if not out.get("ok") or not out.get("media_id"):
        audit.record("campaign.media_upload", admin, outcome="failed", details={"type": kind, "size_bytes": len(data)})
        raise HTTPException(502, {"code": "upload_failed", "message": "WhatsApp did not accept the file. Check the WhatsApp configuration and try again."})
    media_id = str(out["media_id"])
    audit.record("campaign.media_upload", admin, details={"type": kind, "mime": mime, "size_bytes": len(data), "media_id": media_id})
    return {"header_media": {"type": kind, "id": media_id}, "mime": mime, "size_bytes": len(data)}
