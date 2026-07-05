from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response
from database import db
from bson import ObjectId
import datetime, base64, re

router = APIRouter()

# ── Auth helper ────────────────────────────────────────────────────────────────
def _require_admin(request: Request):
    token = (request.cookies.get("admin_token") or
             request.headers.get("x-admin-token", "")).strip()
    if not token:
        raise HTTPException(401, "Unauthorized")
    admin = db.admins.find_one({"token": token})
    if not admin:
        raise HTTPException(401, "Unauthorized")
    return admin

def _fmt(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc

# ── Admin: list ────────────────────────────────────────────────────────────────
@router.get("/admin/popup-campaigns")
def list_popup_campaigns(request: Request):
    _require_admin(request)
    docs = list(db.popup_campaigns.find().sort("_id", -1))
    return [_fmt(d) for d in docs]

# ── Admin: create ──────────────────────────────────────────────────────────────
@router.post("/admin/popup-campaigns")
async def create_popup_campaign(request: Request):
    _require_admin(request)
    data = await request.json()
    now  = datetime.datetime.utcnow()
    doc  = {
        "name":         (data.get("name") or "").strip(),
        "image_url":    (data.get("image_url") or "").strip(),
        "start_dt":     (data.get("start_dt") or ""),
        "end_dt":       (data.get("end_dt") or ""),
        "frequency":    data.get("frequency", "once_per_day"),
        "target":       data.get("target", "all"),
        "cities":       data.get("cities") or [],
        "click_action": data.get("click_action", "none"),
        "action_value": (data.get("action_value") or "").strip(),
        "is_active":    bool(data.get("is_active", True)),
        "created_at":   now,
        "updated_at":   now,
    }
    result = db.popup_campaigns.insert_one(doc)
    if doc["image_url"].startswith("data:image"):
        proxy = f"/popup-image/{result.inserted_id}"
        db.popup_campaigns.update_one(
            {"_id": result.inserted_id},
            {"$set": {"_raw_image": doc["image_url"], "image_url": proxy}}
        )
    return {"ok": True, "id": str(result.inserted_id)}

# ── Admin: update ──────────────────────────────────────────────────────────────
@router.put("/admin/popup-campaigns/{cid}")
async def update_popup_campaign(cid: str, request: Request):
    _require_admin(request)
    data = await request.json()
    now  = datetime.datetime.utcnow()
    upd  = {
        "name":         (data.get("name") or "").strip(),
        "image_url":    (data.get("image_url") or "").strip(),
        "start_dt":     (data.get("start_dt") or ""),
        "end_dt":       (data.get("end_dt") or ""),
        "frequency":    data.get("frequency", "once_per_day"),
        "target":       data.get("target", "all"),
        "cities":       data.get("cities") or [],
        "click_action": data.get("click_action", "none"),
        "action_value": (data.get("action_value") or "").strip(),
        "is_active":    bool(data.get("is_active", True)),
        "updated_at":   now,
    }
    if upd["image_url"].startswith("data:image"):
        upd["_raw_image"] = upd["image_url"]
        upd["image_url"]  = f"/popup-image/{cid}"
    try:
        db.popup_campaigns.update_one({"_id": ObjectId(cid)}, {"$set": upd})
    except Exception:
        raise HTTPException(400, "Invalid id")
    return {"ok": True}

# ── Admin: delete ──────────────────────────────────────────────────────────────
@router.delete("/admin/popup-campaigns/{cid}")
def delete_popup_campaign(cid: str, request: Request):
    _require_admin(request)
    try:
        db.popup_campaigns.delete_one({"_id": ObjectId(cid)})
    except Exception:
        raise HTTPException(400, "Invalid id")
    return {"ok": True}

# ── Admin: toggle active ───────────────────────────────────────────────────────
@router.patch("/admin/popup-campaigns/{cid}/toggle")
async def toggle_popup_campaign(cid: str, request: Request):
    _require_admin(request)
    data = await request.json()
    try:
        db.popup_campaigns.update_one(
            {"_id": ObjectId(cid)},
            {"$set": {"is_active": bool(data.get("is_active", False)),
                      "updated_at": datetime.datetime.utcnow()}}
        )
    except Exception:
        raise HTTPException(400, "Invalid id")
    return {"ok": True}

# ── Public: active campaigns for Flutter ──────────────────────────────────────
@router.get("/public/popup-campaigns")
def get_active_popup_campaigns(city: str = ""):
    docs = list(db.popup_campaigns.find({"is_active": True}).sort("_id", -1))
    result = []
    for d in docs:
        target = d.get("target", "all")
        if target == "selected_city" and city:
            cities = [c.lower().strip() for c in (d.get("cities") or [])]
            if city.lower().strip() not in cities:
                continue
        image_url = d.get("image_url") or ""
        doc_id    = str(d["_id"])

        # Cache-bust: append ?v=<updated_at_ms> so CachedNetworkImage treats
        # a newly uploaded image as a different URL and fetches it fresh.
        updated_at = d.get("updated_at")
        v = int(updated_at.timestamp() * 1000) if updated_at else 0

        if image_url.startswith("data:image"):
            image_url = f"/popup-image/{doc_id}?v={v}"
        elif image_url.startswith("/popup-image/"):
            base = image_url.split("?")[0]
            image_url = f"{base}?v={v}"

        result.append({
            "id":           doc_id,
            "name":         d.get("name", ""),
            "image_url":    image_url,
            "frequency":    d.get("frequency", "once_per_day"),
            "target":       target,
            "cities":       d.get("cities") or [],
            "click_action": d.get("click_action", "none"),
            "action_value": d.get("action_value", ""),
            "start_dt":     d.get("start_dt", ""),
            "end_dt":       d.get("end_dt", ""),
        })
    return result

# ── Image proxy (serves stored base64 as binary) ──────────────────────────────
@router.get("/popup-image/{cid}")
def get_popup_image(cid: str):
    try:
        doc = db.popup_campaigns.find_one({"_id": ObjectId(cid)}, {"_raw_image": 1, "image_url": 1})
    except Exception:
        raise HTTPException(400, "Bad id")
    if not doc:
        raise HTTPException(404, "Not found")
    raw = doc.get("_raw_image") or doc.get("image_url") or ""
    if not raw.startswith("data:image"):
        raise HTTPException(404, "No image stored")
    m = re.match(r"data:([^;]+);base64,(.+)", raw, re.DOTALL)
    if not m:
        raise HTTPException(400, "Bad image data")
    try:
        img_bytes = base64.b64decode(m.group(2))
    except Exception:
        raise HTTPException(500, "Decode error")
    return Response(
        content=img_bytes,
        media_type=m.group(1),
        # no-cache forces the client to revalidate; the ?v= param in the URL
        # is the primary bust so CachedNetworkImage sees a new URL on update.
        headers={"Cache-Control": "no-cache",
                 "Content-Length": str(len(img_bytes))},
    )
