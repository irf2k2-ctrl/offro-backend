"""
Merchant App Router — self-service portal
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from database import db
from bson import ObjectId
from datetime import datetime, timedelta
import uuid, qrcode, io, base64, hmac, hashlib


import os as _cld_os, hashlib as _cld_hash, time as _cld_time
import requests as _cld_req

def _cloudinary_upload(b64_or_url: str, folder: str = "offro") -> str:
    """Upload base64 to Cloudinary → secure_url. No-op if not configured or already URL."""
    if not b64_or_url:
        return b64_or_url
    cloud  = _cld_os.getenv("CLOUDINARY_CLOUD_NAME", "")
    api_key= _cld_os.getenv("CLOUDINARY_API_KEY", "")
    secret = _cld_os.getenv("CLOUDINARY_API_SECRET", "")
    if not cloud or not api_key or not secret:
        return b64_or_url  # Cloudinary not configured — pass-through
    # Already a CDN/HTTP URL → skip upload
    if b64_or_url.startswith("http://") or b64_or_url.startswith("https://"):
        return b64_or_url
    data_str  = b64_or_url.split(",", 1)[-1] if "," in b64_or_url else b64_or_url
    timestamp = str(int(_cld_time.time()))
    sig_str   = f"folder={folder}&timestamp={timestamp}{secret}"
    signature = _cld_hash.sha1(sig_str.encode()).hexdigest()
    try:
        resp = _cld_req.post(
            f"https://api.cloudinary.com/v1_1/{cloud}/image/upload",
            data={"file": f"data:image/jpeg;base64,{data_str}",
                  "folder": folder, "timestamp": timestamp,
                  "api_key": api_key, "signature": signature},
            timeout=25,
        )
        if resp.status_code == 200:
            url = resp.json().get("secure_url", b64_or_url)
            print(f"[CDN] uploaded to {url[:70]}")
            return url
        print(f"[CDN] error {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"[CDN] upload failed: {e}")
    return b64_or_url

def _make_thumb_url(cdn_url: str, w: int = 300) -> str:
    """Return Cloudinary thumbnail URL."""
    if "cloudinary.com" in str(cdn_url):
        return cdn_url.replace("/upload/", f"/upload/w_{w},c_fill,q_auto,f_auto/")
    return ""

router = APIRouter(tags=["MerchantApp"])

import os as _os
import socket as _socket
import requests as _req_module
import urllib3

# ── Force IPv4 + bypass DNS for Razorpay (Railway blocks IPv6 / has DNS issues) ──
_RZP_HOST = "api.razorpay.com"
_RZP_IPS  = ["13.235.137.113", "15.206.107.5"]   # Razorpay AWS ap-south-1 IPs

_orig_getaddrinfo = _socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Force IPv4 for all DNS lookups — Railway doesn't support outbound IPv6."""
    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)
_socket.getaddrinfo = _ipv4_only_getaddrinfo

def _razorpay_request(method: str, path: str, auth: tuple, json_data: dict, timeout: int = 8):
    """
    Make a request to Razorpay, trying each known IP directly if DNS fails.
    Uses Host header to satisfy SNI/TLS verification.
    """
    last_err = None
    urls_to_try = [
        f"https://{_RZP_HOST}{path}",          # normal DNS first
        f"https://{_RZP_IPS[0]}{path}",        # fallback: direct IP 1
        f"https://{_RZP_IPS[1]}{path}",        # fallback: direct IP 2
    ]
    for url in urls_to_try:
        try:
            headers = {}
            # When using IP directly, set Host header for SNI
            if url.startswith(f"https://{_RZP_IPS[0]}") or url.startswith(f"https://{_RZP_IPS[1]}"):
                headers["Host"] = _RZP_HOST
                resp = _req_module.request(
                    method, url, auth=auth, json=json_data,
                    headers=headers, timeout=timeout, verify=False
                )
            else:
                resp = _req_module.request(
                    method, url, auth=auth, json=json_data, timeout=timeout
                )
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_err = e
            continue
    raise last_err

RAZORPAY_KEY_ID     = _os.environ.get("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = _os.environ.get("RAZORPAY_KEY_SECRET")
if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
    import warnings as _w
    _w.warn("RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET not set — payment endpoints will fail.", RuntimeWarning)

# ───────────── helpers ─────────────

def _qr(store_id: str) -> str:
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(f"localsaver://redeem?store_id={store_id}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="#3E5F55", back_color="white")
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode()

def get_merchant(request: Request):
    """Unified auth — token lookup across accounts + merchants collections."""
    token = (request.cookies.get("merchant_token") or
             request.cookies.get("user_token") or
             request.headers.get("Authorization", "").replace("Bearer ", ""))
    if not token:
        raise HTTPException(401, "Not authenticated")

    # PRIMARY: unified accounts collection (any role — role checked at endpoint level if needed)
    acct = db.accounts.find_one({"token": token})
    if acct:
        return acct

    # FALLBACK: legacy merchants collection (pre-migration merchants not yet in accounts)
    m = db.merchants.find_one({"token": token})
    if m:
        # Opportunistically sync this merchant into accounts for future lookups
        try:
            from routers.users import _phone_variants as _pv
            variants = _pv(str(m.get("phone", "")))
            db.accounts.update_one(
                {"phone": {"$in": variants}},
                {"$set": {"token": token, "merchant_id": _mid(m), "name": m.get("name",""), "phone": m.get("phone",""),
                             "profile_image": m.get("profile_image","")},
                 "$addToSet": {"roles": "merchant"}},
                upsert=True,
            )
        except Exception:
            pass
        return m

    raise HTTPException(401, "Session expired. Please log in again.")


def _mid(m: dict) -> str:
    """Return the correct merchant_id for DB queries.

    After the accounts migration, m["_id"] is the account ObjectId, but all
    existing stores/banners/subscriptions were written with the OLD merchants
    collection ObjectId stored in m["merchant_id"].  Always prefer that field.
    """
    return m.get("merchant_id") or str(m["_id"])


def plan_days(plan: str) -> int:
    return {"1month": 30, "3months": 90, "6months": 180, "12months": 365}.get(plan, 30)


def _log_tx(merchant_id: str, tx_type: str, description: str, amount: float = 0, meta: dict = None):
    """Write a transaction record for a merchant."""
    try:
        db.merchant_transactions.insert_one({
            "merchant_id": merchant_id,
            "type": tx_type,
            "description": description,
            "amount": amount,
            "meta": meta or {},
            "created_at": datetime.utcnow(),
        })
    except Exception:
        pass  # never crash the main flow due to logging

# ───────────── auth ─────────────

@router.post("/register")
def merchant_register(data: dict):
    name  = data.get("name", "").strip()
    phone = str(data.get("phone", "")).strip()
    city  = data.get("city", "").strip()
    area  = data.get("area", "").strip()
    if not name or not phone:
        raise HTTPException(400, "Name and phone are required")
    if db.accounts.find_one({"phone": {"$in": [phone, phone[-10:] if len(phone)>=10 else phone]}}):
        raise HTTPException(400, "Phone already registered. Please login.")
    merchant = {
        "name": name, "phone": phone,
        "city": city, "area": area,
        "status": "active", "token": None,
        "registered_at": datetime.utcnow(),
    }
    # Insert into unified accounts collection
    merchant["roles"] = merchant.get("roles", ["merchant"])
    result = db.accounts.insert_one(merchant)
    # Also keep merchants collection in sync (for rollback safety)
    db.merchants.update_one({"phone": merchant["phone"]}, {"$set": merchant}, upsert=True)
    merchant_oid = result.inserted_id

    # Sync to unified accounts collection
    from routers.users import _phone_variants as _pv
    db.accounts.update_one(
        {"phone": {"$in": _pv(phone)}},
        {"$set": {
            "phone":         re.sub(r"\D", "", phone)[-10:],
            "name":          name,
            "city":          data.get("city", ""),
            "status":        "active",
            "merchant_id":   str(merchant_oid),
            "merchant_name": name,
            "migrated_from": "register",
        },
        "$addToSet": {"roles": "merchant"}},
        upsert=True,
    )
    _log_tx(str(merchant_oid), "account_created", f"Merchant account created for {name}")
    return {"message": "Registered successfully. You can now login.", "merchant_id": str(merchant_oid)}

@router.post("/login")
def merchant_login(data: dict):
    """Unified merchant login — checks accounts first, falls back to merchants."""
    from routers.users import _phone_variants as _pv
    phone = str(data.get("phone", "")).strip()
    if not phone:
        raise HTTPException(400, "Phone required")
    variants = _pv(phone)

    # Check unified accounts collection first
    acct = db.accounts.find_one({"phone": {"$in": variants}})
    if acct and "merchant" in acct.get("roles", []):
        token = str(uuid.uuid4())
        db.accounts.update_one({"_id": acct["_id"]}, {"$set": {"token": token, "last_login": datetime.utcnow().isoformat()}})
        # Sync to merchants legacy
        db.accounts.update_one({"phone": {"$in": variants}}, {"$set": {"token": token}})
        db.merchants.update_one({"phone": {"$in": variants}}, {"$set": {"token": token}})  # keep in sync
        res = JSONResponse({
            "merchant_id": acct.get("merchant_id", str(acct["_id"])),
            "name":        acct.get("name", ""),
            "phone":       acct.get("phone", phone),
            "token":       token,
        })
        res.set_cookie("merchant_token", token, httponly=True, samesite="lax", max_age=86400*30)
        res.set_cookie("user_token",     token, httponly=True, samesite="lax", max_age=86400*30)
        return res

    # Fallback: legacy merchants collection
    m = db.merchants.find_one({"phone": {"$in": variants}})
    if not m:
        raise HTTPException(401, "Phone not registered as merchant. Please register first.")
    token = str(uuid.uuid4())
    # Save token to merchants collection
    db.merchants.update_one({"_id": m["_id"]}, {"$set": {"token": token, "last_login": datetime.utcnow().isoformat()}})
    # Sync token to accounts collection (upsert so future /me calls work)
    db.accounts.update_one(
        {"phone": {"$in": variants}},
        {"$set": {
            "token":       token,
            "merchant_id": _mid(m),
            "name":        m.get("name", ""),
            "phone":       m.get("phone", phone),
            "last_login":  datetime.utcnow().isoformat(),
        },
         "$addToSet": {"roles": "merchant"}},
        upsert=True,
    )
    print(f"[MERCHANT-LOGIN] ✅ fallback: {phone} → merchant_id={str(m['_id'])}")
    res = JSONResponse({
        "merchant_id": _mid(m),
        "name":        m.get("name", ""),
        "phone":       m.get("phone", phone),
        "token":       token,
        "is_merchant": True,
        "role":        "merchant",
    })
    res.set_cookie("merchant_token", token, httponly=True, samesite="lax", max_age=86400*30)
    res.set_cookie("user_token",     token, httponly=True, samesite="lax", max_age=86400*30)
    return res


@router.post("/check-phone")
def check_merchant_phone(data: dict):
    """Pre-OTP check — unified accounts collection."""
    from routers.users import _phone_variants as _pv
    phone = (data.get("phone") or "").strip()
    if not phone:
        raise HTTPException(400, "Phone required")
    variants = _pv(phone)
    acct = db.accounts.find_one({"phone": {"$in": variants}})
    if acct:
        return {"registered": True, "role": "merchant" if "merchant" in acct.get("roles",[]) else "user"}
    # Legacy fallback
    m = (db.accounts.find_one({"phone": {"$in": variants}, "roles": "merchant"}) or db.merchants.find_one({"phone": {"$in": variants}}))
    return {"registered": m is not None, "role": "merchant" if m else "none"}


@router.post("/logout")
def merchant_logout():
    res = JSONResponse({"message": "Logged out"})
    res.delete_cookie("merchant_token")
    return res

# ───────────── profile ─────────────

@router.get("/me")
def merchant_me(m=Depends(get_merchant)):
    return {
        "merchant_id":   _mid(m),
        "name":          m.get("name"),
        "phone":         m.get("phone"),
        "city":          m.get("city", ""),
        "area":          m.get("area", ""),
        "status":        m.get("status", "active"),
        "profile_image": m.get("profile_image", ""),
        "merchant_terms_accepted": m.get("merchant_terms_accepted", False),
    }

# ───────────── stores ─────────────

@router.get("/stores")
def my_stores(m=Depends(get_merchant)):
    mid = _mid(m)
    result = []
    _now = datetime.utcnow()
    for s in db.stores.find({"merchant_id": mid}):
        sub_end = s.get("subscription_end")
        sub_end_str = ""
        sub_end_dt = None
        if sub_end:
            if isinstance(sub_end, datetime):
                sub_end_dt = sub_end
                sub_end_str = sub_end.strftime("%d %b %Y")
            else:
                sub_end_str = str(sub_end)
                try:
                    sub_end_dt = datetime.fromisoformat(str(sub_end).replace("Z", ""))
                except Exception:
                    sub_end_dt = None
        # EFFECTIVE STATUS: the stored "status" flag is only ever flipped to
        # "active" once by admin approval and never revisited afterwards, so a
        # store whose subscription has since lapsed kept showing "Active"
        # forever here (admin dashboard computes this live from the date, this
        # endpoint didn't). Override the displayed status to "expired" once
        # subscription_end has passed — end-of-day grace, same rule the admin
        # dashboard uses — without touching the underlying stored field.
        raw_status = s.get("status", "draft")
        eff_status = raw_status
        if raw_status == "active" and sub_end_dt is not None:
            _eod = sub_end_dt.replace(hour=23, minute=59, second=59, microsecond=0)
            if _eod < _now:
                eff_status = "expired"
        # Count active deals for this store
        sid = str(s["_id"])
        deal_count = db.deals.count_documents({"store_id": sid, "status": "active"})             if "deals" in db.list_collection_names() else 0
        # Check if store has a paid subscription (to prevent re-subscribe when inactive)
        paid_sub = db.subscriptions.find_one(
            {"store_id": sid, "status": {"$in": ["paid", "active"]}})
        has_paid_sub = paid_sub is not None
        result.append({
            "_id": sid,
            "store_name":      s.get("store_name"),
            "category":        s.get("category", ""),
            "state":           s.get("state", ""),
            "city":            s.get("city", ""),
            "area":            s.get("area", ""),
            "address":         s.get("address", ""),
            "phone":           s.get("phone", ""),
            "status":          eff_status,
            "subscription_end": sub_end_str,
            "subscription_plan": s.get("subscription_plan", ""),
            "visit_points":    s.get("points_per_scan", 10),
            "is_new_in_town":  s.get("is_new_in_town", False),
            "qr_code":         s.get("qr_code", ""),
            "image":           s.get("image") or s.get("image_url") or "",
            "image2":          s.get("image2") or s.get("image2_url") or "",
            "open_time":       s.get("open_time", ""),
            "close_time":      s.get("close_time", ""),
            "deal_count":      deal_count,
            "has_paid_sub":    has_paid_sub,
        })
    return result

@router.post("/stores")
def create_merchant_store(data: dict, m=Depends(get_merchant)):
    store_name = data.get("store_name", "").strip()
    if not store_name: raise HTTPException(400, "Store name required")
    store = {
        "merchant_id": _mid(m),
        "merchant_name": m.get("name"),
        "store_name":    store_name,
        "category":      data.get("category", ""),
        "state":         data.get("state", ""),
        "city":          data.get("city") or m.get("city", ""),
        "area":          data.get("area") or m.get("area", ""),
        "address":       data.get("address", ""),
        "phone":         data.get("phone") or m.get("phone", ""),
        "about":         data.get("about", ""),
        "open_time":     data.get("open_time", ""),
        "close_time":    data.get("close_time", ""),
        "status":        "draft",
        "points_per_scan": 0,
        "lat":  data.get("lat", ""),   "lng": data.get("lng", ""),
        "image_url":    _cloudinary_upload(data.get("image","") or "", folder="offro/stores"),
        "image_thumb":  _make_thumb_url(_cloudinary_upload(data.get("image","") or "", folder="offro/stores")),
        "image":        None,  # clear raw base64 after CDN upload
        "is_new_in_town": False,
        "created_at":   datetime.utcnow(),
    }
    result = db.stores.insert_one(store)
    sid = str(result.inserted_id)
    qr_b64 = _qr(sid)
    db.stores.update_one({"_id": result.inserted_id}, {"$set": {"qr_code": qr_b64}})
    _log_tx(_mid(m), "store_created", f"Store '{store_name}' created", meta={"store_id": sid})
    return {"store_id": sid, "qr_code": qr_b64, "message": "Store created. Subscribe to go live."}

@router.get("/stores/{sid}")
def get_merchant_store(sid: str, m=Depends(get_merchant)):
    """Return full store detail including image2 — used by edit store screen."""
    store = db.stores.find_one({"_id": ObjectId(sid), "merchant_id": _mid(m)})
    if not store: raise HTTPException(404, "Store not found")
    sub_end = store.get("subscription_end")
    sub_end_str = sub_end.strftime("%d %b %Y") if isinstance(sub_end, datetime) else (str(sub_end) if sub_end else "")
    # EFFECTIVE STATUS — same lapsed-subscription check as my_stores() so the
    # Edit screen and My Stores list never disagree on Active vs Expired.
    _raw_status = store.get("status", "draft")
    _eff_status = _raw_status
    if _raw_status == "active" and isinstance(sub_end, datetime):
        _eod = sub_end.replace(hour=23, minute=59, second=59, microsecond=0)
        if _eod < datetime.utcnow():
            _eff_status = "expired"
    deal_count = db.deals.count_documents({"store_id": sid, "status": "active"}) \
        if "deals" in db.list_collection_names() else 0
    paid_sub = db.subscriptions.find_one({"store_id": sid, "status": {"$in": ["paid", "active"]}})
    return {
        "_id":              sid,
        "store_name":       store.get("store_name", ""),
        "category":         store.get("category", ""),
        "state":            store.get("state", ""),
        "city":             store.get("city", ""),
        "area":             store.get("area", ""),
        "address":          store.get("address", ""),
        "phone":            store.get("phone", ""),
        "lat":              store.get("lat", ""),
        "lng":              store.get("lng", ""),
        "status":           _eff_status,
        "subscription_end": sub_end_str,
        "subscription_plan": store.get("subscription_plan", ""),
        "visit_points":     store.get("points_per_scan", 10),
        "is_new_in_town":   store.get("is_new_in_town", False),
        "qr_code":          store.get("qr_code", ""),
        "image":            store.get("image") or store.get("image_url") or "",
        "image2":           store.get("image2") or store.get("image2_url") or "",
        "about":            store.get("about") or "",
        "open_time":        store.get("open_time", ""),
        "close_time":       store.get("close_time", ""),
        "deal_count":       deal_count,
        "has_paid_sub":     paid_sub is not None,
    }

@router.post("/stores/{sid}/reset-qr")
def reset_store_qr(sid: str, m=Depends(get_merchant)):
    """Regenerate QR code for a merchant store. Resets on every scan so stores always have a valid QR."""
    store = db.stores.find_one({"_id": ObjectId(sid), "merchant_id": _mid(m)})
    if not store:
        raise HTTPException(404, "Store not found")
    qr_b64 = _qr(sid)
    db.stores.update_one({"_id": ObjectId(sid)}, {"$set": {"qr_code": qr_b64}})
    return {"qr_code": qr_b64, "message": "QR code regenerated"}


@router.put("/stores/{sid}")
def update_merchant_store(sid: str, data: dict, m=Depends(get_merchant)):
    store = db.stores.find_one({"_id": ObjectId(sid), "merchant_id": _mid(m)})
    if not store: raise HTTPException(404, "Store not found")
    upd = {f: data[f] for f in ["store_name","category","state","city","area","address","phone","lat","lng","about","open_time","close_time"] if data.get(f) is not None}
    # FIX (blank Edit screen root cause): create_merchant_store() uploads to
    # Cloudinary and stores the CDN URL under image_url/image2_url, clearing
    # the raw "image"/"image2" fields. This update endpoint previously just
    # dumped whatever the client sent straight into "image"/"image2" without
    # uploading — so a NEW picked photo (base64) got saved as raw base64 in
    # "image" instead of going through Cloudinary, AND if the Flutter client
    # ever echoed back the existing image_url as "image" (a URL, not base64),
    # that URL got stored in the raw "image" field too — which the my_stores()
    # list endpoint then serves as-is via `image_url or image`, and the Edit
    # screen's base64Decode() call on that URL string threw a FormatException
    # → blank screen. Only accept genuinely NEW base64 uploads here, upload
    # them to Cloudinary exactly like store creation, and ignore anything
    # that's already a URL (nothing changed, keep the existing CDN image).
    new_image = data.get("image")
    if new_image and isinstance(new_image, str) and new_image.startswith("data:"):
        upd["image_url"]   = _cloudinary_upload(new_image, folder="offro/stores")
        upd["image_thumb"] = _make_thumb_url(upd["image_url"])
        upd["image"]       = None  # clear raw base64, mirror create_merchant_store
    new_image2 = data.get("image2")
    if new_image2 and isinstance(new_image2, str) and new_image2.startswith("data:"):
        upd["image2_url"] = _cloudinary_upload(new_image2, folder="offro/stores")
        upd["image2"]     = None
    if upd: db.stores.update_one({"_id": ObjectId(sid)}, {"$set": upd})
    return {"message": "Store updated"}

# ───────────── plans / pricing ─────────────

@router.get("/plans")
def get_plans():
    doc  = db.pricing.find_one({}) or {}
    gst  = doc.get("gst_percent", 18)
    base = doc.get("plans", [
        {"id": "1month",   "label": "1 Month",   "price": 499},
        {"id": "3months",  "label": "3 Months",  "price": 1299},
        {"id": "6months",  "label": "6 Months",  "price": 2299},
        {"id": "12months", "label": "12 Months", "price": 3999},
    ])
    out = []
    for p in base:
        price    = p["price"]
        gst_amt  = round(price * gst / 100, 2)
        total    = round(price + gst_amt, 2)
        out.append({**p, "gst_percent": gst, "gst_amount": gst_amt, "total": total})
    return out

# ───────────── subscription / Razorpay ─────────────

@router.post("/subscribe")
def initiate_subscription(data: dict, m=Depends(get_merchant)):
    store_id      = data.get("store_id")
    plan          = data.get("plan")
    from_date_str = data.get("from_date")
    if not all([store_id, plan, from_date_str]):
        raise HTTPException(400, "store_id, plan, from_date required")

    store = db.stores.find_one({"_id": ObjectId(store_id), "merchant_id": _mid(m)})
    if not store: raise HTTPException(404, "Store not found")

    doc       = db.pricing.find_one({}) or {}
    gst       = doc.get("gst_percent", 18)
    plans_map = {p["id"]: p for p in doc.get("plans", [
        {"id": "1month",   "label": "1 Month",   "price": 499},
        {"id": "3months",  "label": "3 Months",  "price": 1299},
        {"id": "6months",  "label": "6 Months",  "price": 2299},
        {"id": "12months", "label": "12 Months", "price": 3999},
    ])}
    if plan not in plans_map: raise HTTPException(400, "Invalid plan")

    price       = plans_map[plan]["price"]
    gst_amt     = round(price * gst / 100, 2)
    total       = round(price + gst_amt, 2)
    total_paise = int(total * 100)

    # ── Apply discount code ──
    discount_code  = data.get("discount_code")
    discount_value = float(data.get("discount_value", 0))
    if discount_code:
        disc_doc = db.discounts.find_one({"code": discount_code.upper(), "active": True})
        if disc_doc:
            max_u = disc_doc.get("max_uses", 0)
            used  = disc_doc.get("used_count", 0)
            from datetime import datetime as _dt
            expired = disc_doc.get("expiry_date") and _dt.utcnow() > disc_doc["expiry_date"]
            if not expired and (max_u == 0 or used < max_u):
                discount_value = float(disc_doc.get("value", 0))
                db.discounts.update_one({"_id": disc_doc["_id"]}, {"$inc": {"used_count": 1}})
            else:
                discount_value = 0
        else:
            discount_value = 0

    total     = max(0, round(total - discount_value, 2))
    total_paise = int(total * 100)

    from_date = datetime.strptime(from_date_str, "%Y-%m-%d")
    end_date  = from_date + timedelta(days=plan_days(plan))

    # ── Zero-price fast path (FREE discount or 0-price plan) ──
    # Razorpay does NOT accept amount=0; activate immediately instead.
    if total_paise <= 0:
        sub_doc = {
            "store_id":       store_id,
            "merchant_id": _mid(m),
            "plan":           plan,
            "from_date":      from_date,
            "end_date":       end_date,
            "price":          price,
            "gst":            gst_amt,
            "gst_percent":    gst,
            "total":          0,
            "status":         "paid",
            "pay_mode":       "free",
            "discount_code":  discount_code,
            "discount_value": discount_value,
            "created_at":     datetime.utcnow(),
            "paid_at":        datetime.utcnow(),
            "free_activation": True,
        }
        sub_result = db.subscriptions.insert_one(sub_doc)
        sub_id = str(sub_result.inserted_id)
        db.stores.update_one({"_id": ObjectId(store_id)}, {"$set": {
            "status":             "waiting_approval",
            "subscription_plan":  plan,
            "subscription_start": from_date,
            "subscription_end":   end_date,
        }})
        invoice_no = f"LS-FREE-{datetime.utcnow().strftime('%Y%m%d')}-{sub_id[-6:].upper()}"
        db.subscriptions.update_one({"_id": sub_result.inserted_id}, {"$set": {"invoice_no": invoice_no}})  # FIX: keep in sync with invoices collection
        store_doc  = db.stores.find_one({"_id": ObjectId(store_id)}, {"store_name": 1}) or {}
        db.invoices.insert_one({
            "invoice_no":    invoice_no,
            "merchant_id": _mid(m),
            "merchant_name": m.get("name"),
            "merchant_phone": m.get("phone"),
            "store_id":      store_id,
            "store_name":    store_doc.get("store_name", ""),
            "plan":          plan,
            "base_price":    0, "gst": 0, "total": 0,
            "from_date":     from_date,
            "end_date":      end_date,
            "created_at":    datetime.utcnow(),
        })
        _log_tx(_mid(m), "subscription",
                f"Free plan activated for '{store_doc.get('store_name','')}' — {plan}",
                amount=0, meta={"store_id": store_id, "plan": plan, "invoice": invoice_no})
        return {
            "ok":              True,
            "pay_mode":        "free",
            "subscription_id": sub_id,
            "invoice_no":      invoice_no,
            "amount":          0,
            "amount_display":  0,
            "plan_label":      plans_map[plan]["label"],
            "from_date":       from_date_str,
            "end_date":        end_date.strftime("%Y-%m-%d"),
            "gst_percent":     gst,
            "gst_amount":      0,
            "base_price":      price,
            "merchant_name":   m.get("name"),
            "merchant_phone":  m.get("phone"),
            "store_name":      store.get("store_name"),
        }

    # ── Razorpay integration ──
    rp_configured = (
        RAZORPAY_KEY_ID and
        RAZORPAY_KEY_SECRET and
        RAZORPAY_KEY_ID != "YOUR_RAZORPAY_KEY_ID" and
        RAZORPAY_KEY_SECRET != "YOUR_RAZORPAY_KEY_SECRET"
    )

    rp_order_id = None
    pay_mode    = "manual"   # fallback: admin confirms payment manually

    if rp_configured:
        try:
            rp_res = _razorpay_request(
                "POST", "/v1/orders",
                auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
                json_data={"amount": total_paise, "currency": "INR",
                           "receipt": f"OF_{store_id[:8]}_{plan}",
                           "notes":   {"store_id": store_id, "plan": plan}},
                timeout=8,
            )
            try:
                rp_data = rp_res.json()
            except Exception:
                rp_data = {}
            if "id" in rp_data:
                rp_order_id = rp_data["id"]
                pay_mode    = "razorpay"
            else:
                # Razorpay returned error — fall back to manual
                pay_mode = "manual"
        except Exception:
            # All connection attempts failed — fall back to manual silently
            pay_mode = "manual"

    # Insert subscription record
    sub_doc = {
        "store_id":           store_id,
        "merchant_id": _mid(m),
        "plan":               plan,
        "from_date":          from_date,
        "end_date":           end_date,
        "price":              price,
        "gst":                gst_amt,
        "gst_percent":        gst,
        "total":              total,
        "razorpay_order_id":  rp_order_id,
        "status":             "pending",
        "pay_mode":           pay_mode,
        "discount_code":      discount_code,
        "discount_value":     discount_value,
        "created_at":         datetime.utcnow(),
    }
    sub_result = db.subscriptions.insert_one(sub_doc)

    return {
        "ok":                 True,
        "pay_mode":           pay_mode,
        "subscription_id":    str(sub_result.inserted_id),
        "razorpay_order_id":  rp_order_id,
        "razorpay_key":       RAZORPAY_KEY_ID if rp_configured else None,
        "amount":             total_paise,
        "amount_display":     total,
        "plan_label":         plans_map[plan]["label"],
        "from_date":          from_date_str,
        "end_date":           end_date.strftime("%Y-%m-%d"),
        "gst_percent":        gst,
        "gst_amount":         gst_amt,
        "base_price":         price,
        "merchant_name":      m.get("name"),
        "merchant_phone":     m.get("phone"),
        "store_name":         store.get("store_name"),
    }

@router.post("/subscribe/verify")
def verify_payment(data: dict, m=Depends(get_merchant)):
    order_id   = data.get("razorpay_order_id")
    payment_id = data.get("razorpay_payment_id")
    signature  = data.get("razorpay_signature")
    store_id   = data.get("store_id")
    if not all([order_id, payment_id, signature, store_id]):
        raise HTTPException(400, "Missing payment fields")

    # Verify Razorpay signature
    if not RAZORPAY_KEY_SECRET:
        raise HTTPException(503, "Payment verification unavailable — Razorpay not configured")
    msg      = f"{order_id}|{payment_id}"
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(400, "Payment signature mismatch")

    sub = db.subscriptions.find_one({"razorpay_order_id": order_id})
    if not sub: raise HTTPException(404, "Subscription record not found")
    # IDEMPOTENCY GUARD: already paid → return existing invoice, never double-insert
    if sub.get("status") == "paid":
        existing_inv = db.invoices.find_one({"razorpay_payment_id": sub.get("razorpay_payment_id","")})
        return {
            "message":      "✅ Payment already verified.",
            "invoice_no":   sub.get("invoice_no", existing_inv.get("invoice_no","") if existing_inv else ""),
            "store_status": "waiting_approval",
        }

    db.subscriptions.update_one({"_id": sub["_id"]}, {"$set": {
        "status":             "paid",
        "razorpay_payment_id": payment_id,
        "paid_at":            datetime.utcnow(),
    }})
    db.stores.update_one({"_id": ObjectId(store_id)}, {"$set": {
        "status":              "waiting_approval",
        "subscription_plan":   sub["plan"],
        "subscription_start":  sub["from_date"],
        "subscription_end":    sub["end_date"],
        "razorpay_payment_id": payment_id,
    }})

    invoice_no = f"LS-{datetime.utcnow().strftime('%Y%m%d')}-{str(sub['_id'])[-6:].upper()}"
    db.subscriptions.update_one({"_id": sub["_id"]}, {"$set": {"invoice_no": invoice_no}})  # FIX: keep in sync with invoices collection
    store_doc  = db.stores.find_one({"_id": ObjectId(store_id)}, {"store_name": 1}) or {}
    db.invoices.insert_one({
        "invoice_no":         invoice_no,
        "merchant_id": _mid(m),
        "merchant_name":      m.get("name"),
        "merchant_phone":     m.get("phone"),
        "store_id":           store_id,
        "store_name":         store_doc.get("store_name", ""),
        "plan":               sub["plan"],
        "base_price":         sub["price"],
        "gst":                sub["gst"],
        "total":              sub["total"],
        "from_date":          sub["from_date"],
        "end_date":           sub["end_date"],
        "razorpay_payment_id": payment_id,
        "created_at":         datetime.utcnow(),
    })

    _log_tx(_mid(m), "subscription",
            f"Subscribed '{store_doc.get('store_name','')}' — {sub['plan']}",
            amount=sub["total"],
            meta={"store_id": store_id, "plan": sub["plan"], "invoice": invoice_no})

    return {
        "message":       "✅ Payment verified! Store pending admin approval.",
        "invoice_no":    invoice_no,
        "store_status":  "waiting_approval",
    }

@router.post("/subscribe/free")
def activate_free_subscription(data: dict, m=Depends(get_merchant)):
    """Activate a 0-price subscription immediately (no payment gateway needed)."""
    store_id       = data.get("store_id")
    subscription_id = data.get("subscription_id")
    if not store_id or not subscription_id:
        raise HTTPException(400, "store_id and subscription_id required")

    sub = db.subscriptions.find_one({"_id": ObjectId(subscription_id), "status": "pending"})
    if not sub:
        raise HTTPException(404, "Subscription not found")

    now = datetime.utcnow()
    db.subscriptions.update_one({"_id": sub["_id"]}, {"$set": {
        "status": "paid",
        "paid_at": now,
        "free_activation": True,
    }})
    db.stores.update_one({"_id": ObjectId(store_id)}, {"$set": {
        "status":             "waiting_approval",
        "subscription_plan":  sub["plan"],
        "subscription_start": sub["from_date"],
        "subscription_end":   sub["end_date"],
    }})

    invoice_no = f"LS-FREE-{now.strftime('%Y%m%d')}-{str(sub['_id'])[-6:].upper()}"
    db.subscriptions.update_one({"_id": sub["_id"]}, {"$set": {"invoice_no": invoice_no}})  # FIX: keep in sync with invoices collection
    store_doc  = db.stores.find_one({"_id": ObjectId(store_id)}, {"store_name": 1}) or {}
    db.invoices.insert_one({
        "invoice_no":    invoice_no,
        "merchant_id": _mid(m),
        "merchant_name": m.get("name"),
        "merchant_phone": m.get("phone"),
        "store_id":      store_id,
        "store_name":    store_doc.get("store_name", ""),
        "plan":          sub["plan"],
        "base_price":    0,
        "gst":           0,
        "total":         0,
        "from_date":     sub["from_date"],
        "end_date":      sub["end_date"],
        "created_at":    now,
    })
    _log_tx(_mid(m), "subscription",
            f"Free plan activated for '{store_doc.get('store_name','')}' — {sub['plan']}",
            amount=0,
            meta={"store_id": store_id, "plan": sub["plan"]})

    return {
        "message":       "✅ Free subscription activated! Store pending admin approval.",
        "invoice_no":    invoice_no,
        "store_status":  "waiting_approval",
    }

# ───────────── invoices ─────────────

@router.get("/invoices")
def my_invoices(m=Depends(get_merchant)):
    result = []
    merchant_id = _mid(m)
    merchant_phone = str(m.get("phone", ""))
    inv_query = {"$or": [{"merchant_id": merchant_id}, {"merchant_phone": merchant_phone}]}                 if merchant_phone else {"merchant_id": merchant_id}
    for inv in db.invoices.find(inv_query).sort("created_at", -1):
        fd = inv.get("from_date"); ed = inv.get("end_date")
        result.append({
            "invoice_no":  inv.get("invoice_no"),
            "store_name":  inv.get("store_name"),
            "plan":        inv.get("plan"),
            "total":       inv.get("total"),
            "gst":             inv.get("gst"),
            "base_price":      inv.get("base_price"),
            "original_amount": inv.get("original_amount", inv.get("base_price", 0)),
            "discount_code":   inv.get("discount_code", ""),
            "discount_amount": inv.get("discount_amount", 0),
            "final_amount":    inv.get("final_amount",  inv.get("base_price", 0)),
            "from_date":       fd.strftime("%d %b %Y") if isinstance(fd, datetime) else str(fd or ""),
            "end_date":        ed.strftime("%d %b %Y") if isinstance(ed, datetime) else str(ed or ""),
            "created_at":      inv["created_at"].strftime("%d %b %Y") if inv.get("created_at") else "",
        })
    return result

# ───────────── transaction history ─────────────

@router.get("/transactions")
def my_transactions(m=Depends(get_merchant)):
    result = []
    for tx in db.merchant_transactions.find({"merchant_id": _mid(m)}).sort("created_at", -1).limit(100):
        result.append({
            "type":        tx.get("type"),
            "description": tx.get("description"),
            "amount":      tx.get("amount", 0),
            "meta":        tx.get("meta", {}),
            "date":        tx["created_at"].strftime("%d %b %Y %H:%M") if tx.get("created_at") else "",
        })
    return result



# ───────────── deals ─────────────

@router.get("/deals")
def my_deals(m=Depends(get_merchant)):
    merchant_id = _mid(m)
    result = []
    for d in db.deals.find({"merchant_id": merchant_id}).sort("created_at", -1):
        store = db.stores.find_one({"_id": ObjectId(d.get("store_id", ""))}) if d.get("store_id") else None
        result.append({
            "_id": str(d["_id"]),
            "store_name": store.get("store_name") if store else "Unknown",
            "store_id": d.get("store_id"),
            "title": d.get("title"),
            "discount": d.get("discount"),
            "category": d.get("category"),
            "description": d.get("description"),
            "start_date": d.get("start_date"),
            "end_date": d.get("end_date"),
            "status": d.get("status", "active")
        })
    return result

@router.post("/deals")
def create_deal(data: dict, m=Depends(get_merchant)):
    merchant_id = _mid(m)
    store_id = data.get("store_id")
    if not store_id:
        raise HTTPException(400, "store_id required")
    store = db.stores.find_one({"_id": ObjectId(store_id), "merchant_id": merchant_id})
    if not store:
        raise HTTPException(403, "Store not found or not yours")
    if store.get("status") != "active":
        raise HTTPException(400, "Store must be active to add deals")
    deal = {
        "merchant_id": merchant_id,
        "store_id": store_id,
        "title": data.get("title", ""),
        "discount": data.get("discount", 0),
        "category": data.get("category", ""),
        "description": data.get("description", ""),
        "start_date": data.get("start_date", ""),
        "end_date": data.get("end_date", ""),
        "status": "active",
        "created_at": datetime.utcnow(),
    }
    result = db.deals.insert_one(deal)
    # Update store discount_percent for user app display
    db.stores.update_one({"_id": ObjectId(store_id)}, {"$set": {"discount_percent": deal["discount"]}})
    return {"message": "Deal added", "deal_id": str(result.inserted_id)}

@router.put("/deals/{deal_id}")
def update_deal(deal_id: str, data: dict, m=Depends(get_merchant)):
    """Edit an existing deal. Validates end_date against store subscription_end."""
    merchant_id = _mid(m)
    existing = db.deals.find_one({"_id": ObjectId(deal_id), "merchant_id": merchant_id})
    if not existing:
        raise HTTPException(404, "Deal not found or not yours")

    store_id = data.get("store_id", existing.get("store_id", ""))
    store = db.stores.find_one({"_id": ObjectId(store_id), "merchant_id": merchant_id})
    if not store:
        raise HTTPException(403, "Store not found or not yours")

    # Validate end_date doesn't exceed store subscription_end
    end_date = data.get("end_date", "")
    if end_date:
        sub_end = store.get("subscription_end")
        if sub_end:
            # Parse sub_end (datetime or string)
            sub_end_dt = None
            if isinstance(sub_end, datetime):
                sub_end_dt = sub_end
            else:
                sub_end_str = str(sub_end).strip()
                for fmt in ("%Y-%m-%d", "%d %b %Y", "%d-%m-%Y"):
                    try:
                        sub_end_dt = datetime.strptime(sub_end_str, fmt)
                        break
                    except Exception:
                        pass
            # Parse end_date
            end_dt = None
            for fmt in ("%Y-%m-%d", "%d %b %Y", "%d-%m-%Y"):
                try:
                    end_dt = datetime.strptime(end_date, fmt)
                    break
                except Exception:
                    pass
            if sub_end_dt and end_dt and end_dt > sub_end_dt:
                raise HTTPException(400, "Deal end date cannot exceed store subscription end date (" + sub_end_dt.strftime("%d %b %Y") + ")")

    update_fields = {
        "title":       data.get("title", existing.get("title", "")),
        "discount":    data.get("discount", existing.get("discount", 0)),
        "category":    data.get("category", existing.get("category", "")),
        "description": data.get("description", existing.get("description", "")),
        "start_date":  data.get("start_date", existing.get("start_date", "")),
        "end_date":    end_date or existing.get("end_date", ""),
        "store_id":     store_id,
    }
    db.deals.update_one({"_id": ObjectId(deal_id)}, {"$set": update_fields})
    # Update store discount_percent for user app display
    db.stores.update_one({"_id": ObjectId(store_id)}, {"$set": {"discount_percent": update_fields["discount"]}})
    return {"message": "Deal updated"}


@router.delete("/deals/{deal_id}")
def delete_deal(deal_id: str, m=Depends(get_merchant)):
    merchant_id = _mid(m)
    db.deals.delete_one({"_id": ObjectId(deal_id), "merchant_id": merchant_id})
    return {"message": "Deal deleted"}


# ───────────── terms (public read) ─────────────

@router.get("/terms")
def merchant_terms():
    doc = db.terms.find_one({"type": "merchant"}) or {}
    return {"content": doc.get("content", "Merchant terms and conditions will be posted here.")}

@router.post("/accept-terms")
def accept_merchant_terms(data: dict, m=Depends(get_merchant)):
    """Store merchant terms acceptance — version + timestamp."""
    version = data.get("version", "1.0")
    db.accounts.update_one({"_id": m["_id"]}, {"$set": {
        "merchant_terms_accepted": True,
        "merchant_terms_version": version,
        "merchant_terms_accepted_at": datetime.utcnow().isoformat(),
    }})
    return {"ok": True, "message": "Merchant terms accepted."}

# ───────────── subscriptions list ─────────────

@router.get("/subscriptions")
def my_subscriptions(m=Depends(get_merchant)):
    result = []
    # Query by merchant_id OR by phone (unified auth — both fields may exist)
    merchant_id = _mid(m)
    merchant_phone = str(m.get("phone", ""))
    query = {"$or": [{"merchant_id": merchant_id}, {"merchant_phone": merchant_phone}]}
    for s in db.subscriptions.find(query).sort("created_at", -1):
        fd = s.get("from_date"); ed = s.get("end_date")
        store_doc = {}
        try: store_doc = db.stores.find_one({"_id": ObjectId(s.get("store_id",""))}, {"store_name":1}) or {}
        except: pass
        result.append({
            "store_name": store_doc.get("store_name", s.get("store_id","")),
            "plan":       s.get("plan"),
            "total":      s.get("total"),
            "status":     s.get("status"),
            "from_date":  fd.strftime("%d %b %Y") if isinstance(fd, datetime) else str(fd or ""),
            "end_date":   ed.strftime("%d %b %Y") if isinstance(ed, datetime) else str(ed or ""),
        })
    return result


# =================== UPDATE MERCHANT PROFILE ===================
@router.put("/profile")
def update_merchant_profile(data: dict, m=Depends(get_merchant)):
    allowed = ["profile_image", "name"]
    update = {k: v for k, v in data.items() if k in allowed}
    if not update:
        raise HTTPException(400, "Nothing to update")
    db.accounts.update_one({"_id": m["_id"]}, {"$set": update})
    # SYNC: also update legacy merchants collection so get_merchant() fallback
    # returns the updated profile_image (fixes profile image disappearing bug)
    mid = _mid(m)
    db.merchants.update_one(
        {"_id": m["_id"]},
        {"$set": update}
    )
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════════
# MERCHANT BANNERS — self-service banner ordering
# ═══════════════════════════════════════════════════════════════════════════════

def _pricing_doc():
    return db.pricing.find_one({}) or {}

# ── GET /merchant/banners  ──────────────────────────────────────────────────
def _banner_parse_any_date(val):
    """PERMANENT FIX: robustly parse a banner end_date that may be stored as
    ISO ('2026-08-02' / '2026-08-02T00:00:00') OR as '%d %b %Y' (e.g. '02 Aug 2026').
    Returns a naive datetime or None. Never raises."""
    from datetime import datetime as _ddt
    if val is None:
        return None
    if hasattr(val, 'strftime'):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        return _ddt.fromisoformat(s.replace("Z", "")[:19])
    except Exception:
        pass
    try:
        return _ddt.strptime(s[:11].strip(), "%d %b %Y")
    except Exception:
        pass
    try:
        return _ddt.strptime(s[:10].strip(), "%Y-%m-%d")
    except Exception:
        pass
    return None

@router.get("/banners")
def get_my_banners(m=Depends(get_merchant)):
    from datetime import datetime as dt
    merchant_id = _mid(m)
    merchant_phone = str(m.get("phone", ""))
    # Also check legacy merchant_id field stored in accounts
    legacy_mid     = str(m.get("merchant_id", ""))
    today_dt       = dt.utcnow()

    result = []

    def _safe_str_date(val):
        """Convert datetime or string to ISO string safely."""
        if val is None:
            return ""
        if hasattr(val, 'strftime'):
            return val.strftime("%Y-%m-%dT%H:%M:%S")
        return str(val)

    # 1. Merchant-submitted banners — match by merchant_id OR legacy_mid OR phone
    try:
        id_candidates = list({mid for mid in [merchant_id, legacy_mid] if mid and mid != "None"})
        # FIX: exclude deleted banners so a re-registered account (same phone,
        # new merchant_id) does NOT inherit banners from the previous account.
        # The admin delete-cascade sets status="deleted" + is_active=False.
        base_q = {"status": {"$ne": "deleted"}, "is_active": {"$ne": False}}
        if merchant_phone:
            mb_query = {"$and": [base_q, {"$or": [
                {"merchant_id": {"$in": id_candidates}},
                {"merchant_phone": merchant_phone},
            ]}]}
        else:
            mb_query = {"$and": [base_q, {"merchant_id": {"$in": id_candidates}}]}
        for b in db.merchant_banners.find(mb_query).sort("created_at", -1):
            _end_dt    = _banner_parse_any_date(b.get("end_date", ""))
            is_expired = bool(_end_dt and _end_dt < today_dt)
            # PERMANENT FIX: an expired banner is expired regardless of its
            # stored status/approval_status — override so the client (any
            # app version) always displays the correct badge without having
            # to re-derive expiry itself.
            _status          = "expired" if is_expired else b.get("status", "pending")
            _approval_status = "expired" if is_expired else b.get("approval_status", "pending")
            result.append({
                "_id":             str(b["_id"]),
                "title":           b.get("title", ""),
                "image_url":       b.get("image_url", ""),
                "duration":        b.get("duration_days", 0),
                "from_date":       b.get("from_date", ""),
                "end_date":        b.get("end_date", ""),
                "amount":          b.get("total", 0),
                "status":          _status,
                "approval_status": _approval_status,
                "created_at":      _safe_str_date(b.get("created_at", "")),
                "source":          "merchant",
                "is_expired":      is_expired,
                "is_active":       bool(b.get("is_active", True)),
                "deleted_by_admin": bool(b.get("deleted_by_admin", False)),
            })
    except Exception as e:
        print(f"[BANNERS] merchant_banners query error: {e}")

    # 2. Admin-created banners (promo_sliders) assigned to this merchant
    try:
        phone_10 = re.sub(r'\D', '', merchant_phone)[-10:] if merchant_phone else ""
        if phone_10:
            for s in db.promo_sliders.find(
                {"merchant_phone": {"$exists": True, "$ne": ""}}
            ):
                s_phone_10 = re.sub(r'\D', '', str(s.get("merchant_phone", "")))[-10:]
                if s_phone_10 != phone_10:
                    continue
                _end_dt    = _banner_parse_any_date(s.get("end_date") or s.get("expires_at"))
                is_expired = bool(_end_dt and _end_dt < today_dt)
                result.append({
                    "_id":             str(s["_id"]),
                    "title":           s.get("title", ""),
                    "image_url":       s.get("image_url", ""),
                    "duration":        s.get("duration_days", 0),
                    "from_date":       s.get("from_date", ""),
                    "end_date":        s.get("end_date", s.get("expires_at", "")),
                    "amount":          0,
                    "status":          "expired" if is_expired else ("removed" if s.get("deleted_by_admin") else ("approved" if s.get("is_active") else "hidden")),
                    "approval_status": "expired" if is_expired else ("removed" if s.get("deleted_by_admin") else "approved"),
                    "is_active":       bool(s.get("is_active", True)),
                    "deleted_by_admin": bool(s.get("deleted_by_admin", False)),
                    "created_at":      _safe_str_date(s.get("created_at", "")),
                    "source":          "admin",
                    "is_expired":      is_expired,
                })
    except Exception as e:
        print(f"[BANNERS] promo_sliders query error: {e}")

    # Safe sort — all created_at are now strings
    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return result

# ── GET /merchant/banners/pricing  ─────────────────────────────────────────
@router.get("/banners/pricing")
def get_banner_pricing_merchant(m=Depends(get_merchant)):
    doc = _pricing_doc()
    gst_pct = float(doc.get("gst_percent", 18))
    return {
        "price_per_day":  float(doc.get("banner_price_per_day", 15)),
        "gst_pct":        gst_pct,
    }

# ── PATCH /merchant/banners/{bid}/toggle  ──────────────────────────────────
@router.put("/banners/{bid}/toggle")
def merchant_toggle_banner(bid: str, m=Depends(get_merchant)):
    """Merchant can turn their own approved banner ON or OFF.
    PERMANENT FIX: Searches merchant_banners first, then promo_sliders.
    Always syncs both collections."""
    merchant_id = _mid(m)
    merchant_phone = str(m.get("phone", ""))
    try:
        oid = ObjectId(bid)
    except Exception:
        raise HTTPException(400, "Invalid banner id")

    ts = datetime.utcnow().isoformat()
    b = db.merchant_banners.find_one({"_id": oid})
    found_in = "merchant_banners"
    if not b:
        b = db.promo_sliders.find_one({"_id": oid})
        found_in = "promo_sliders"
    if not b:
        raise HTTPException(404, "Banner not found")

    # Verify ownership
    b_phone = re.sub(r'\D', '', str(b.get("merchant_phone", "")))[-10:]
    m_phone = re.sub(r'\D', '', merchant_phone)[-10:] if merchant_phone else ""
    if str(b.get("merchant_id","")) != merchant_id and (not m_phone or b_phone != m_phone):
        raise HTTPException(403, "Not your banner")

    # Cannot re-activate a banner removed by admin
    if b.get("deleted_by_admin") and not bool(b.get("is_active", True)):
        raise HTTPException(403, "This banner was removed by admin and cannot be reactivated.")

    new_active = not bool(b.get("is_active", True))

    if found_in == "merchant_banners":
        db.merchant_banners.update_one({"_id": oid}, {"$set": {"is_active": new_active, "toggled_at": ts}})
        db.promo_sliders.update_many({"source_banner_id": bid}, {"$set": {"is_active": new_active, "toggled_at": ts}})
    else:
        db.promo_sliders.update_one({"_id": oid}, {"$set": {"is_active": new_active, "toggled_at": ts}})
        src_bid = b.get("source_banner_id", "")
        if src_bid:
            try:
                db.merchant_banners.update_one({"_id": ObjectId(src_bid)}, {"$set": {"is_active": new_active, "toggled_at": ts}})
            except Exception:
                pass
    return {"ok": True, "is_active": new_active}

# ── POST /merchant/banners/order  ──────────────────────────────────────────
@router.post("/banners/order")
def create_banner_order(data: dict, m=Depends(get_merchant)):
    """
    Accepts: { "days": int, "from_date": "YYYY-MM-DD" }
    Returns an order summary with pricing + Razorpay order if payment needed.
    """
    merchant_id = _mid(m)
    days = int(data.get("days", 30))
    from_date_str = data.get("from_date", "")
    # TASK 4 FIX: read discount_code from request body
    discount_code  = (data.get("discount_code") or "").strip().upper()
    discount_value = 0.0
    discount_msg   = ""
    if discount_code:
        disc_doc = db.discounts.find_one({"code": discount_code, "active": True})
        if disc_doc:
            discount_value = float(disc_doc.get("value", 0))
            discount_msg = f"✅ Code '{discount_code}' applied — ₹{discount_value:.0f} off!"
        # ignore invalid codes silently (don't block order creation)

    if days < 1:
        raise HTTPException(400, "days must be ≥ 1")
    try:
        from_date = datetime.strptime(from_date_str, "%Y-%m-%d")
    except Exception:
        from_date = datetime.utcnow()

    end_date = from_date + timedelta(days=days)

    doc = _pricing_doc()
    gst_pct       = float(doc.get("gst_percent", 18))
    price_per_day = float(doc.get("banner_price_per_day", 15))
    base_price    = round(price_per_day * days, 2)
    gst_amount    = round(base_price * gst_pct / 100, 2)
    # TASK 4 FIX: apply discount before GST total calculation
    final_pre_tax = max(0.0, base_price - discount_value)
    gst_amount    = round(final_pre_tax * gst_pct / 100, 2)
    total         = round(final_pre_tax + gst_amount, 2)
    amount_paise  = int(total * 100)

    plan_label = f"{days} Day{'s' if days!=1 else ''} Banner"

    # Create a pending order record
    order_doc = {
        "merchant_id":    merchant_id,
        "merchant_phone": str(m.get("phone", "")),
        "merchant_name":  str(m.get("name", "")),
        "type":           "banner",
        "days":           days,
        "from_date":      from_date.strftime("%d %b %Y"),
        "end_date":       end_date.strftime("%d %b %Y"),
        "price_per_day":  price_per_day,
        "base_price":     base_price,
        "gst_percent":    gst_pct,
        "gst_amount":     gst_amount,
        "total":          total,
        "amount_paise":   amount_paise,
        "discount_code":  discount_code,
        "discount_amount": discount_value,
        "final_amount":   total,
        "status":         "pending",
        "approval_status": "pending",
        "created_at":     datetime.utcnow().isoformat(),
    }
    inserted = db.banner_orders.insert_one(order_doc)
    order_id = str(inserted.inserted_id)

    rp_order_id    = None
    pay_mode       = "manual"

    if amount_paise > 0 and RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
        try:
            rp = _razorpay_request("POST", "/v1/orders",
                auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
                json_data={"amount": amount_paise, "currency": "INR",
                           "receipt": f"bnr_{order_id[:8]}"})
            rp_data = rp.json() if hasattr(rp, 'json') else {}
            rp_order_id = rp_data.get("id")
            if rp_order_id:
                pay_mode = "razorpay"
            else:
                pay_mode = "manual"
        except Exception:
            pay_mode = "manual"

    db.banner_orders.update_one({"_id": inserted.inserted_id},
        {"$set": {"razorpay_order_id": rp_order_id, "pay_mode": pay_mode}})

    return {
        "order_id":          order_id,
        "plan_label":        plan_label,
        "days":              days,
        "from_date":         from_date.strftime("%d %b %Y"),
        "end_date":          end_date.strftime("%d %b %Y"),
        "price_per_day":     price_per_day,
        "base_price":        base_price,
        "gst_percent":       gst_pct,
        "gst_amount":        gst_amount,
        "discount_code":     discount_code or "",
        "discount_value":    discount_value,
        "discount_amount":   discount_value,
        "discount_msg":      discount_msg,
        "amount_display":    total,
        "amount_paise":      amount_paise,
        "pay_mode":          pay_mode,
        "razorpay_key":      RAZORPAY_KEY_ID,
        "razorpay_order_id": rp_order_id,
    }

# ── POST /merchant/banners/activate-free  ──────────────────────────────────
@router.post("/banners/activate-free")
def activate_free_banner(data: dict, m=Depends(get_merchant)):
    merchant_id = _mid(m)
    order_id    = data.get("order_id", "")
    title       = data.get("title", "")
    image_url   = _cloudinary_upload(data.get("image_url",""), folder="offro/banners")
    image_thumb = _make_thumb_url(image_url)

    order = db.banner_orders.find_one({"_id": ObjectId(order_id), "merchant_id": merchant_id})
    if not order:
        raise HTTPException(404, "Order not found")

    # IDEMPOTENCY GUARD: if already submitted → return existing data, never insert twice.
    # (Mirrors the guard in verify_banner_payment below — prevents double-tap / retry
    # from creating a second merchant_banners document for the same order, which showed
    # up in the admin dashboard as a "duplicate" banner: one pending, one approved.)
    if order.get("status") in ("submitted", "paid"):
        existing_banner_id = order.get("banner_id", "")
        existing_banner = db.merchant_banners.find_one({"_id": ObjectId(existing_banner_id)}) if existing_banner_id else None
        existing_inv_no = (existing_banner or {}).get("invoice_no", "")
        return {"message": "Banner already activated.", "banner_id": existing_banner_id, "invoice_no": existing_inv_no}

    # Mark discount code used if applied
    disc_code = order.get("discount_code")
    if disc_code:
        db.discounts.update_one({"code": disc_code}, {"$inc": {"used_count": 1}})

    # Read store/city from Flutter payload (Flutter sends these on activation)
    store_id   = (data.get("store_id")   or "").strip()
    store_name = (data.get("store_name") or "").strip()
    city       = (data.get("city")       or "").strip()
    # Fallback: look up city from store record if not provided directly
    if not city and store_id:
        try:
            st = db.stores.find_one({"_id": ObjectId(store_id)}, {"city": 1})
            if st:
                city = st.get("city", "")
        except Exception:
            pass

    # CONTENT-BASED DEDUP GUARD (defense-in-depth, independent of order_id):
    # Even if a DIFFERENT banner_orders doc was created (e.g. merchant backed
    # out and resubmitted, or a client retry created a second order), this
    # catches it by matching the actual banner content — one merchant
    # submission for the same title/store/dates must always be ONE record.
    _dedup_window = datetime.utcnow() - timedelta(minutes=15)
    _dup = db.merchant_banners.find_one({
        "merchant_id": merchant_id,
        "title":       title,
        "store_id":    store_id,
        "from_date":   order.get("from_date", ""),
        "end_date":    order.get("end_date", ""),
        "created_at":  {"$gte": _dedup_window.isoformat()},
    })
    if _dup:
        db.banner_orders.update_one({"_id": ObjectId(order_id)},
            {"$set": {"status": "submitted", "banner_id": str(_dup["_id"]),
                      "store_id": store_id, "city": city}})
        return {"message": "Banner already submitted for review.",
                "banner_id": str(_dup["_id"]), "invoice_no": _dup.get("invoice_no", "")}

    banner = {
        "merchant_id":      merchant_id,
        "merchant_name":    m.get("name", ""),
        "merchant_phone":   str(m.get("phone", "")),
        "title":            title,
        "image_url":        image_url,
        "image_thumb":      image_thumb,
        "store_id":         store_id,
        "store_name":       store_name,
        "city":             city,
        "duration_days":    order.get("days", 30),
        "from_date":        order.get("from_date", ""),
        "end_date":         order.get("end_date", ""),
        "price_per_day":    order.get("price_per_day", 0),
        "base_price":       order.get("base_price", 0),
        "discount_code":    disc_code or "",
        "discount_value":   order.get("discount_value", 0),
        "gst_percent":      order.get("gst_percent", 18),
        "gst_amount":       order.get("gst_amount", 0),
        "total":            order.get("total", 0),
        "payment_status":   "free",
        "status":           "pending",
        "approval_status":  "pending",
        "created_at":       datetime.utcnow().isoformat(),
    }
    res = db.merchant_banners.insert_one(banner)
    db.banner_orders.update_one({"_id": ObjectId(order_id)},
        {"$set": {"status": "submitted", "banner_id": str(res.inserted_id),
                  "store_id": store_id, "city": city}})

    # Write free invoice for record-keeping
    invoice_no = f"BNR-FREE-{datetime.utcnow().strftime('%Y%m%d')}-{str(res.inserted_id)[-6:].upper()}"
    db.invoices.insert_one({
        "invoice_no":     invoice_no,
        "type":           "banner",
        "merchant_id":    merchant_id,
        "merchant_name":  m.get("name"),
        "merchant_phone": str(m.get("phone", "")),
        "item_label":     f"Banner – {order.get('days', 30)} Days",
        "store_name":     title,
        "plan":           f"{order.get('from_date','')} → {order.get('end_date','')}",
        "base_price":     order.get("base_price", 0),
        "original_amount":order.get("base_price", 0),
        "discount_code":  order.get("discount_code", ""),
        "discount_amount":order.get("discount_value", 0),
        "final_amount":   order.get("total", 0),
        "gst":            order.get("gst_amount", 0),
        "gst_percent":    order.get("gst_percent", 18),
        "total":          order.get("total", 0),
        "from_date":      order.get("from_date", ""),
        "end_date":       order.get("end_date", ""),
        "payment_status": "free",
        "created_at":     datetime.utcnow(),
    })
    db.merchant_banners.update_one({"_id": res.inserted_id}, {"$set": {"invoice_no": invoice_no}})
    _log_tx(merchant_id, "banner",
            f"Free Banner '{title}' – {order.get('days', 30)} days",
            amount=0, meta={"banner_id": str(res.inserted_id)})
    return {"message": "Banner submitted for review", "banner_id": str(res.inserted_id), "invoice_no": invoice_no}

# ── POST /merchant/banners/verify  ─────────────────────────────────────────
@router.post("/banners/verify")
def verify_banner_payment(data: dict, m=Depends(get_merchant)):
    merchant_id = _mid(m)
    order_id          = data.get("order_id", "")
    title             = data.get("title", "")
    image_url         = _cloudinary_upload(data.get("image_url",""), folder="offro/banners")
    image_thumb       = _make_thumb_url(image_url)
    razorpay_payment_id = data.get("razorpay_payment_id", "")
    razorpay_order_id   = data.get("razorpay_order_id", "")
    razorpay_signature  = data.get("razorpay_signature", "")

    order = db.banner_orders.find_one({"_id": ObjectId(order_id), "merchant_id": merchant_id})
    if not order:
        raise HTTPException(404, "Order not found")

    # IDEMPOTENCY GUARD: if already paid → return existing data, never insert twice
    if order.get("status") == "paid":
        existing_banner = db.merchant_banners.find_one({"_id": ObjectId(order.get("banner_id", ""))}) if order.get("banner_id") else None
        existing_inv_no = (existing_banner or {}).get("invoice_no") or order.get("invoice_no", "")
        return {"message": "Payment already verified.", "banner_id": order.get("banner_id", ""), "invoice_no": existing_inv_no}

    if RAZORPAY_KEY_SECRET and razorpay_order_id and razorpay_payment_id:
        msg = f"{razorpay_order_id}|{razorpay_payment_id}"
        expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
        if expected != razorpay_signature:
            raise HTTPException(400, "Payment verification failed")

    # Read store/city from Flutter payload (Flutter sends these on payment verification)
    store_id   = (data.get("store_id")   or "").strip()
    store_name = (data.get("store_name") or "").strip()
    city       = (data.get("city")       or "").strip()
    # Fallback: look up city from store record if not provided directly
    if not city and store_id:
        try:
            st = db.stores.find_one({"_id": ObjectId(store_id)}, {"city": 1})
            if st:
                city = st.get("city", "")
        except Exception:
            pass

    # CONTENT-BASED DEDUP GUARD (defense-in-depth, independent of order_id):
    # Same protection as the free-activation path — catches duplicates even
    # if a second banner_orders doc was created for identical banner content.
    _dedup_window = datetime.utcnow() - timedelta(minutes=15)
    _dup = db.merchant_banners.find_one({
        "merchant_id": merchant_id,
        "title":       title,
        "store_id":    store_id,
        "from_date":   order.get("from_date", ""),
        "end_date":    order.get("end_date", ""),
        "created_at":  {"$gte": _dedup_window.isoformat()},
    })
    if _dup:
        db.banner_orders.update_one({"_id": ObjectId(order_id)},
            {"$set": {"status": "paid", "banner_id": str(_dup["_id"]),
                      "store_id": store_id, "city": city}})
        return {"message": "Payment already verified.",
                "banner_id": str(_dup["_id"]), "invoice_no": _dup.get("invoice_no", "")}

    banner = {
        "merchant_id":      merchant_id,
        "merchant_name":    m.get("name", ""),
        "merchant_phone":   str(m.get("phone", "")),
        "title":            title,
        "image_url":        image_url,
        "image_thumb":      image_thumb,
        "store_id":         store_id,
        "store_name":       store_name,
        "city":             city,
        "duration_days":    order.get("days", 30),
        "from_date":        order.get("from_date", ""),
        "end_date":         order.get("end_date", ""),
        "price_per_day":    order.get("price_per_day", 0),
        "base_price":       order.get("base_price", 0),
        "gst_percent":      order.get("gst_percent", 18),
        "gst_amount":       order.get("gst_amount", 0),
        "total":            order.get("total", 0),
        "discount_code":    order.get("discount_code", ""),
        "discount_amount":  order.get("discount_amount", order.get("discount", 0)),
        "final_amount":     order.get("final_amount", order.get("total", 0)),
        "razorpay_payment_id": razorpay_payment_id,
        "payment_status":   "paid",
        "status":           "pending",
        "approval_status":  "pending",
        "created_at":       datetime.utcnow().isoformat(),
    }
    res = db.merchant_banners.insert_one(banner)
    db.banner_orders.update_one({"_id": ObjectId(order_id)},
        {"$set": {"status": "paid", "banner_id": str(res.inserted_id),
                  "store_id": store_id, "city": city}})

    # Generate invoice for banner payment (mirrors store subscription verify_payment)
    invoice_no = f"BNR-{datetime.utcnow().strftime('%Y%m%d')}-{str(res.inserted_id)[-6:].upper()}"
    db.invoices.insert_one({
        "invoice_no":         invoice_no,
        "type":               "banner",
        "merchant_id":        merchant_id,
        "merchant_name":      m.get("name"),
        "merchant_phone":     str(m.get("phone", "")),
        "item_label":         f"Banner – {order.get('days', 30)} Days",
        "store_name":         title,
        "plan":               f"{order.get('from_date','')} → {order.get('end_date','')}",
        "base_price":         order.get("base_price", 0),
        "original_amount":    order.get("base_price", 0),
        "discount_code":      order.get("discount_code", ""),
        "discount_amount":    order.get("discount_amount", order.get("discount", 0)),
        "final_amount":       order.get("final_amount", order.get("total", 0)),
        "gst":                order.get("gst_amount", 0),
        "gst_percent":        order.get("gst_percent", 18),
        "total":              order.get("total", 0),
        "from_date":          order.get("from_date", ""),
        "end_date":           order.get("end_date", ""),
        "razorpay_payment_id": razorpay_payment_id,
        "payment_status":     "paid",
        "created_at":         datetime.utcnow(),
    })
    # Update banner record with invoice_no
    db.merchant_banners.update_one({"_id": res.inserted_id}, {"$set": {"invoice_no": invoice_no}})
    _log_tx(merchant_id, "banner",
            f"Banner '{title}' – {order.get('days', 30)} days",
            amount=order.get("total", 0),
            meta={"banner_id": str(res.inserted_id), "invoice_no": invoice_no})
    return {"message": "Payment verified. Banner pending admin approval.",
            "banner_id": str(res.inserted_id), "invoice_no": invoice_no}


# ═══════════════════════════════════════════════════════════════════════════════
# MERCHANT VOUCHERS — self-service voucher ordering
# Issue 3: now uses `days` + `from_date` instead of fixed 30/60/90 plans
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/vouchers")
def get_my_vouchers(m=Depends(get_merchant)):
    merchant_id = _mid(m)
    merchant_phone = str(m.get("phone", ""))
    # Unified auth: match by merchant_id OR merchant_phone
    query = {"$or": [{"merchant_id": merchant_id}, {"merchant_phone": merchant_phone}]}             if merchant_phone else {"merchant_id": merchant_id}
    vouchers = list(db.merchant_vouchers.find(query).sort("created_at", -1))
    result = []
    for v in vouchers:
        result.append({
            "_id":             str(v["_id"]),
            "title":           v.get("title", ""),
            "offer_text":      v.get("offer_text", ""),
            "logo_url":        v.get("logo_url", ""),
            "price":           v.get("price", ""),
            "original_price":  v.get("original_price", ""),
            "discount_label":  v.get("discount_label", ""),
            "duration_days":   v.get("duration_days", 0),
            "from_date":       v.get("from_date", ""),
            "end_date":        v.get("end_date", ""),
            "amount":          v.get("total", 0),
            "status":          v.get("status", "pending"),
            "approval_status": v.get("approval_status", "pending"),
            "created_at":      v.get("created_at", ""),
        })
    return result

@router.get("/vouchers/pricing")
def get_voucher_pricing_merchant(m=Depends(get_merchant)):
    doc = _pricing_doc()
    gst_pct = float(doc.get("gst_percent", 18))
    return {
        "price_per_day": float(doc.get("voucher_price_per_day", 10)),
        "gst_pct":       gst_pct,
    }

@router.post("/vouchers/order")
def create_voucher_order(data: dict, m=Depends(get_merchant)):
    """
    Issue 3: accepts { "days": int, "from_date": "YYYY-MM-DD" }
    No fixed plan chips — merchant chooses exact number of days and start date.
    """
    merchant_id = _mid(m)
    days          = int(data.get("days", 30))
    from_date_str = data.get("from_date", "")

    if days < 1:
        raise HTTPException(400, "days must be ≥ 1")
    try:
        from_date = datetime.strptime(from_date_str, "%Y-%m-%d")
    except Exception:
        from_date = datetime.utcnow()

    end_date = from_date + timedelta(days=days)

    doc = _pricing_doc()
    gst_pct        = float(doc.get("gst_percent", 18))
    price_per_day  = float(doc.get("voucher_price_per_day", 10))
    base_price     = round(price_per_day * days, 2)
    gst_amount     = round(base_price * gst_pct / 100, 2)
    total          = round(base_price + gst_amount, 2)
    amount_paise   = int(total * 100)

    plan_label = f"{days} Day{'s' if days!=1 else ''} Product"

    # ── Discount code validation (Item 6) ──
    discount_code  = (data.get("discount_code") or "").strip().upper()
    discount_value = 0.0
    discount_msg   = ""
    if discount_code:
        disc_doc = db.discounts.find_one({"code": discount_code, "active": True})
        if not disc_doc:
            raise HTTPException(400, "Invalid or expired discount code.")
        max_u = disc_doc.get("max_uses", 0)
        used  = disc_doc.get("used_count", 0)
        if max_u > 0 and used >= max_u:
            raise HTTPException(400, "Discount code has reached its usage limit.")
        if disc_doc.get("expiry_date") and disc_doc["expiry_date"] < datetime.utcnow():
            raise HTTPException(400, "Discount code has expired.")
        discount_value = float(disc_doc.get("value", 0))
        if discount_value >= base_price:
            discount_value = base_price
        discount_msg = f"Code {discount_code} applied"

    discounted_base = round(base_price - discount_value, 2)
    gst_amount      = round(discounted_base * gst_pct / 100, 2)
    total           = round(discounted_base + gst_amount, 2)
    amount_paise    = int(total * 100)

    order_doc = {
        "merchant_id":    merchant_id,
        "merchant_phone": str(m.get("phone", "")),
        "merchant_name":  str(m.get("name", "")),
        "type":           "product",
        "days":           days,
        "from_date":      from_date.strftime("%d %b %Y"),
        "end_date":       end_date.strftime("%d %b %Y"),
        "price_per_day":  price_per_day,
        "base_price":     base_price,
        "discount_code":  discount_code or None,
        "discount_value": discount_value,
        "gst_percent":    gst_pct,
        "gst_amount":     gst_amount,
        "total":          total,
        "amount_paise":   amount_paise,
        "status":         "pending",
        "approval_status": "pending",
        "created_at":     datetime.utcnow().isoformat(),
    }
    inserted = db.voucher_orders.insert_one(order_doc)
    order_id = str(inserted.inserted_id)

    rp_order_id    = None
    pay_mode       = "manual"

    if amount_paise > 0 and RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
        try:
            rp = _razorpay_request("POST", "/v1/orders",
                auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
                json_data={"amount": amount_paise, "currency": "INR",
                           "receipt": f"vch_{order_id[:8]}"})
            rp_data = rp.json() if hasattr(rp, 'json') else {}
            rp_order_id = rp_data.get("id")
            if rp_order_id:
                pay_mode = "razorpay"
            else:
                pay_mode = "manual"
        except Exception:
            pay_mode = "manual"

    db.voucher_orders.update_one({"_id": inserted.inserted_id},
        {"$set": {"razorpay_order_id": rp_order_id, "pay_mode": pay_mode}})

    return {
        "order_id":          order_id,
        "plan_label":        plan_label,
        "days":              days,
        "from_date":         from_date.strftime("%d %b %Y"),
        "end_date":          end_date.strftime("%d %b %Y"),
        "price_per_day":     price_per_day,
        "base_price":        base_price,
        "gst_percent":       gst_pct,
        "gst_amount":        gst_amount,
        "discount_code":     discount_code or "",
        "discount_value":    discount_value,
        "discount_amount":   discount_value,
        "discount_msg":      discount_msg,
        "amount_display":    total,
        "amount_paise":      amount_paise,
        "pay_mode":          pay_mode,
        "razorpay_key":      RAZORPAY_KEY_ID,
        "razorpay_order_id": rp_order_id,
    }

@router.post("/vouchers/activate-free")
def activate_free_voucher(data: dict, m=Depends(get_merchant)):
    merchant_id = _mid(m)
    order_id    = data.get("order_id", "")
    title       = data.get("title", "")
    offer_text  = data.get("offer_text", "")
    logo_url    = _cloudinary_upload(data.get("logo_url",""), folder="offro/vouchers")
    logo_thumb  = _make_thumb_url(logo_url)
    validity       = data.get("validity", "")
    price          = str(data.get("price", "") or "").strip()
    original_price = str(data.get("original_price", "") or "").strip()
    # Auto-compute discount_label if both prices present
    discount_label = ""
    try:
        if price and original_price:
            p = float(price); op = float(original_price)
            if op > p > 0:
                discount_label = f"{round((op - p) / op * 100)}% OFF"
    except Exception:
        pass

    order = db.voucher_orders.find_one({"_id": ObjectId(order_id), "merchant_id": merchant_id})
    if not order:
        raise HTTPException(404, "Order not found")

    # Mark discount used if applied
    disc_code = order.get("discount_code")
    if disc_code:
        db.discounts.update_one({"code": disc_code}, {"$inc": {"used_count": 1}})

    # Read store/city from Flutter payload — CRITICAL: these must be stored
    # on the voucher so admin dashboard shows the correct store and city.
    store_id   = (data.get("store_id")   or "").strip()
    store_name = (data.get("store_name") or "").strip()
    city       = (data.get("city")       or "").strip()
    if not city and store_id:
        try:
            st = db.stores.find_one({"_id": ObjectId(store_id)}, {"city": 1})
            if st: city = st.get("city", "")
        except Exception: pass

    voucher = {
        "merchant_id":    merchant_id,
        "merchant_name":  m.get("name", ""),
        "merchant_phone": str(m.get("phone", "")),
        "title":          title,
        "offer_text":     offer_text,
        "logo_url":       logo_url,
        "logo_thumb":     logo_thumb,
        "store_id":       store_id,
        "store_name":     store_name,
        "city":           city,
        "validity":       validity,
        "price":          price,
        "original_price": original_price,
        "discount_label": discount_label,
        "duration_days":  order.get("days", 30),
        "from_date":      order.get("from_date", ""),
        "end_date":       order.get("end_date", ""),
        "price_per_day":  order.get("price_per_day", 0),
        "base_price":     order.get("base_price", 0),
        "discount_code":  disc_code or "",
        "discount_value": order.get("discount_value", 0),
        "gst_percent":    order.get("gst_percent", 18),
        "gst_amount":     order.get("gst_amount", 0),
        "total":          order.get("total", 0),
        "payment_status": "free",
        "status":         "pending",
        "approval_status":"pending",
        "created_at":     datetime.utcnow().isoformat(),
    }
    res = db.merchant_vouchers.insert_one(voucher)
    db.voucher_orders.update_one({"_id": ObjectId(order_id)},
        {"$set": {"status": "submitted", "voucher_id": str(res.inserted_id),
                  "store_id": store_id, "city": city}})

    # Write free invoice for record-keeping
    invoice_no = f"PRD-FREE-{datetime.utcnow().strftime('%Y%m%d')}-{str(res.inserted_id)[-6:].upper()}"
    db.invoices.insert_one({
        "invoice_no":     invoice_no,
        "type":           "product",
        "merchant_id":    merchant_id,
        "merchant_name":  m.get("name"),
        "merchant_phone": str(m.get("phone", "")),
        "item_label":     f"Discover Product – {order.get('days', 30)} Days",
        "store_name":     title,
        "plan":           f"{order.get('from_date','')} → {order.get('end_date','')}",
        "base_price":     order.get("base_price", 0),
        "original_amount":order.get("base_price", 0),
        "discount_code":  order.get("discount_code", ""),
        "discount_amount":order.get("discount_value", 0),
        "final_amount":   order.get("total", 0),
        "gst":            order.get("gst_amount", 0),
        "gst_percent":    order.get("gst_percent", 18),
        "total":          order.get("total", 0),
        "from_date":      order.get("from_date", ""),
        "end_date":       order.get("end_date", ""),
        "payment_status": "free",
        "created_at":     datetime.utcnow(),
    })
    db.merchant_vouchers.update_one({"_id": res.inserted_id}, {"$set": {"invoice_no": invoice_no}})
    _log_tx(merchant_id, "product",
            f"Free Discover Product '{title}' – {order.get('days', 30)} days",
            amount=0, meta={"voucher_id": str(res.inserted_id)})
    return {"message": "Product submitted for review", "voucher_id": str(res.inserted_id), "invoice_no": invoice_no}

@router.post("/vouchers/verify")
def verify_voucher_payment(data: dict, m=Depends(get_merchant)):
    merchant_id = _mid(m)
    order_id            = data.get("order_id", "")
    title               = data.get("title", "")
    offer_text          = data.get("offer_text", "")
    logo_url            = _cloudinary_upload(data.get("logo_url",""), folder="offro/vouchers")
    logo_thumb          = _make_thumb_url(logo_url)
    validity            = data.get("validity", "")
    razorpay_payment_id = data.get("razorpay_payment_id", "")
    razorpay_order_id   = data.get("razorpay_order_id", "")
    razorpay_signature  = data.get("razorpay_signature", "")
    price          = str(data.get("price", "") or "").strip()
    original_price = str(data.get("original_price", "") or "").strip()
    discount_label = ""
    try:
        if price and original_price:
            p = float(price); op = float(original_price)
            if op > p > 0:
                discount_label = f"{round((op - p) / op * 100)}% OFF"
    except Exception:
        pass

    order = db.voucher_orders.find_one({"_id": ObjectId(order_id), "merchant_id": merchant_id})
    if not order:
        raise HTTPException(404, "Order not found")

    # IDEMPOTENCY GUARD: if already paid → return existing data, never insert twice
    if order.get("status") == "paid":
        existing_vch = db.merchant_vouchers.find_one({"_id": ObjectId(order.get("voucher_id", ""))}) if order.get("voucher_id") else None
        existing_inv_no = (existing_vch or {}).get("invoice_no") or order.get("invoice_no", "")
        return {"message": "Payment already verified.", "voucher_id": order.get("voucher_id", ""), "invoice_no": existing_inv_no}

    if RAZORPAY_KEY_SECRET and razorpay_order_id and razorpay_payment_id:
        msg = f"{razorpay_order_id}|{razorpay_payment_id}"
        expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
        if expected != razorpay_signature:
            raise HTTPException(400, "Payment verification failed")

    # Read store/city from Flutter payload — CRITICAL: must be stored on voucher
    store_id   = (data.get("store_id")   or "").strip()
    store_name = (data.get("store_name") or "").strip()
    city       = (data.get("city")       or "").strip()
    if not city and store_id:
        try:
            st = db.stores.find_one({"_id": ObjectId(store_id)}, {"city": 1})
            if st: city = st.get("city", "")
        except Exception: pass

    voucher = {
        "merchant_id":      merchant_id,
        "merchant_name":    m.get("name", ""),
        "merchant_phone":   str(m.get("phone", "")),
        "title":            title,
        "offer_text":       offer_text,
        "logo_url":         logo_url,
        "logo_thumb":       logo_thumb,
        "store_id":         store_id,
        "store_name":       store_name,
        "city":             city,
        "validity":         validity,
        "price":            price,
        "original_price":   original_price,
        "discount_label":   discount_label,
        "duration_days":    order.get("days", 30),
        "from_date":        order.get("from_date", ""),
        "end_date":         order.get("end_date", ""),
        "price_per_day":    order.get("price_per_day", 0),
        "base_price":       order.get("base_price", 0),
        "gst_percent":      order.get("gst_percent", 18),
        "gst_amount":       order.get("gst_amount", 0),
        "total":            order.get("total", 0),
        "discount_code":    order.get("discount_code", ""),
        "discount_amount":  order.get("discount", 0),
        "final_amount":     order.get("final_amount", order.get("total", 0)),
        "razorpay_payment_id": razorpay_payment_id,
        "payment_status":   "paid",
        "status":           "pending",
        "approval_status":  "pending",
        "created_at":       datetime.utcnow().isoformat(),
    }
    res = db.merchant_vouchers.insert_one(voucher)
    db.voucher_orders.update_one({"_id": ObjectId(order_id)},
        {"$set": {"status": "paid", "voucher_id": str(res.inserted_id),
                  "store_id": store_id, "city": city}})

    # Generate invoice for product payment (mirrors store subscription verify_payment)
    invoice_no = f"PRD-{datetime.utcnow().strftime('%Y%m%d')}-{str(res.inserted_id)[-6:].upper()}"
    db.invoices.insert_one({
        "invoice_no":         invoice_no,
        "type":               "product",
        "merchant_id":        merchant_id,
        "merchant_name":      m.get("name"),
        "merchant_phone":     str(m.get("phone", "")),
        "item_label":         f"Discover Product – {order.get('days', 30)} Days",
        "store_name":         title,
        "plan":               f"{order.get('from_date','')} → {order.get('end_date','')}",
        "base_price":         order.get("base_price", 0),
        "original_amount":    order.get("base_price", 0),
        "discount_code":      order.get("discount_code", ""),
        "discount_amount":    order.get("discount_value", 0),
        "final_amount":       order.get("final_amount", order.get("total", 0)),
        "gst":                order.get("gst_amount", 0),
        "gst_percent":        order.get("gst_percent", 18),
        "total":              order.get("total", 0),
        "from_date":          order.get("from_date", ""),
        "end_date":           order.get("end_date", ""),
        "razorpay_payment_id": razorpay_payment_id,
        "payment_status":     "paid",
        "created_at":         datetime.utcnow(),
    })
    db.merchant_vouchers.update_one({"_id": res.inserted_id}, {"$set": {"invoice_no": invoice_no}})
    _log_tx(merchant_id, "product",
            f"Discover Product '{title}' – {order.get('days', 30)} days",
            amount=order.get("total", 0),
            meta={"voucher_id": str(res.inserted_id), "invoice_no": invoice_no})
    return {"message": "Payment verified. Product pending admin approval.",
            "voucher_id": str(res.inserted_id), "invoice_no": invoice_no}


# ═══════════════════════════════════════════════════════════════════════════════
# MERCHANT FULL INVOICES — banners + vouchers + store subscriptions
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/invoices/full")
def get_full_invoices(m=Depends(get_merchant)):
    # TASK 6: Full invoice restore with discount fields + proper invoice_no
    merchant_id = _mid(m)
    result = []

    def _fmt_dt(v):
        # FIX: convert UTC -> IST (+5:30) and include time so invoices don't
        # all show a misleading "12:00 AM". Backend always stores UTC
        # (datetime.utcnow()); the app must see IST, matching activity_logs.
        if not v: return ""
        try:
            from datetime import datetime as _dt, timedelta as _td
            if isinstance(v, _dt):
                return (v + _td(hours=5, minutes=30)).strftime("%d %b %Y | %I:%M %p")
            return str(v)[:10]
        except Exception:
            return str(v)[:10]

    def _date_key(v):
        # Normalised date-only key (IST) used for de-dup matching across
        # collections, independent of exact invoice_no string.
        from datetime import datetime as _dt, timedelta as _td
        if isinstance(v, _dt):
            return (v + _td(hours=5, minutes=30)).strftime("%Y-%m-%d")
        return str(v or "")[:10]

    # 1. Central invoices collection (most complete — has discount info)
    inv_date_keys = set()   # FIX: (store_id, from_date) keys already covered by an invoice doc
    for inv in db.invoices.find({"merchant_id": merchant_id}).sort("created_at", -1):
        fd = inv.get("from_date"); ed = inv.get("end_date")
        if inv.get("store_id") and fd is not None:
            inv_date_keys.add((str(inv.get("store_id")), _date_key(fd)))
        result.append({
            "invoice_no":      inv.get("invoice_no", str(inv["_id"])[:8].upper()),
            "type":            inv.get("type", "store"),
            "item_label":      inv.get("item_label") or inv.get("plan", ""),
            "store_name":      inv.get("store_name", ""),
            "plan":            inv.get("plan", ""),
            "from_date":       _fmt_dt(fd),
            "end_date":        _fmt_dt(ed),
            "original_amount": inv.get("original_amount", inv.get("base_price", 0)),
            "discount_code":   inv.get("discount_code", ""),
            "discount_amount": inv.get("discount_amount", 0),
            "final_amount":    inv.get("final_amount", inv.get("base_price", 0)),
            "base_price":      inv.get("base_price", inv.get("original_amount", 0)),
            "gst_percent":     inv.get("gst_percent", 18),
            "gst":             inv.get("gst", 0),
            "total":           inv.get("total", inv.get("amount", 0)),
            "status":          inv.get("status", "paid"),
            "created_at":      _fmt_dt(inv.get("created_at")),
        })

    # 2. Fallback: store subscriptions not yet in invoices collection
    # FIX: dedup by invoice_no AND by (store_id, from_date) — subscription
    # docs historically never got invoice_no written back onto them, so a
    # string match against inv_ids always failed and produced a duplicate
    # "ghost" invoice entry for every free/paid subscription. The
    # (store_id, from_date) key catches this even for already-existing data.
    inv_ids = {r["invoice_no"] for r in result}
    for sub in db.subscriptions.find({"merchant_id": merchant_id}).sort("created_at", -1):
        ino = sub.get("invoice_no", str(sub["_id"])[:8].upper())
        fd = sub.get("from_date"); ed = sub.get("end_date")
        sub_key = (str(sub.get("store_id")), _date_key(fd)) if sub.get("store_id") and fd is not None else None
        if ino in inv_ids: continue
        if sub_key and sub_key in inv_date_keys: continue
        result.append({
            "invoice_no":      ino,
            "type":            "store",
            "item_label":      sub.get("plan", "Store Subscription"),
            "store_name":      sub.get("store_name", ""),
            "plan":            sub.get("plan", ""),
            "from_date":       _fmt_dt(fd),
            "end_date":        _fmt_dt(ed),
            "original_amount": sub.get("base_price", sub.get("amount", 0)),
            "discount_code":   sub.get("discount_code", ""),
            "discount_amount": sub.get("discount_amount", 0),
            "final_amount":    sub.get("final_amount", sub.get("base_price", sub.get("amount", 0))),
            "base_price":      sub.get("base_price", sub.get("amount", 0)),
            "gst_percent":     sub.get("gst_percent", 18),
            "gst":             sub.get("gst", 0),
            "total":           sub.get("total", sub.get("amount", 0)),
            "status":          sub.get("status", "paid"),
            "created_at":      _fmt_dt(sub.get("created_at")),
        })

    # 3. Fallback: banner invoices not in central invoices
    for b in db.merchant_banners.find({"merchant_id": merchant_id, "payment_status": "paid"}).sort("created_at", -1):
        ino = b.get("invoice_no", str(b["_id"])[:8].upper())
        if ino in inv_ids: continue
        result.append({
            "invoice_no":      ino,
            "type":            "banner",
            "item_label":      f"Banner – {b.get('duration_days', b.get('duration', 30))} Days",
            "store_name":      b.get("title", ""),
            "plan":            f"{b.get('from_date','')} → {b.get('end_date','')}",
            "from_date":       b.get("from_date", ""),
            "end_date":        b.get("end_date", ""),
            "original_amount": b.get("base_price", b.get("original_amount", 0)),
            "discount_code":   b.get("discount_code", ""),
            "discount_amount": b.get("discount_amount", 0),
            "final_amount":    b.get("final_amount", b.get("base_price", 0)),
            "base_price":      b.get("base_price", 0),
            "gst_percent":     b.get("gst_percent", 18),
            "gst":             b.get("gst_amount", b.get("gst", 0)),
            "total":           b.get("total", 0),
            "status":          "paid",
            "created_at":      _fmt_dt(b.get("created_at")),
        })

    # 4. Fallback: voucher/product invoices not in central invoices
    for v in db.merchant_vouchers.find({"merchant_id": merchant_id, "payment_status": "paid"}).sort("created_at", -1):
        ino = v.get("invoice_no", str(v["_id"])[:8].upper())
        if ino in inv_ids: continue
        result.append({
            "invoice_no":      ino,
            "type":            "product",
            "item_label":      f"Discover Product – {v.get('duration_days', v.get('duration', 30))} Days",
            "store_name":      v.get("title", ""),
            "plan":            f"{v.get('from_date','')} → {v.get('end_date','')}",
            "from_date":       v.get("from_date", ""),
            "end_date":        v.get("end_date", ""),
            "original_amount": v.get("base_price", v.get("original_amount", 0)),
            "discount_code":   v.get("discount_code", ""),
            "discount_amount": v.get("discount_amount", 0),
            "final_amount":    v.get("final_amount", v.get("base_price", 0)),
            "base_price":      v.get("base_price", 0),
            "gst_percent":     v.get("gst_percent", 18),
            "gst":             v.get("gst_amount", v.get("gst", 0)),
            "total":           v.get("total", 0),
            "status":          "paid",
            "created_at":      _fmt_dt(v.get("created_at")),
        })

    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return result

@router.post("/discounts/validate")
def validate_discount_code(data: dict, m=Depends(get_merchant)):
    """Validate a discount code for merchant checkout (Item 6)."""
    code = (data.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(400, "Code is required")
    disc = db.discounts.find_one({"code": code, "active": True})
    if not disc:
        raise HTTPException(404, "Invalid or expired discount code.")
    max_u = disc.get("max_uses", 0)
    used  = disc.get("used_count", 0)
    if max_u > 0 and used >= max_u:
        raise HTTPException(400, "This code has reached its usage limit.")
    from datetime import datetime
    if disc.get("expiry_date") and disc["expiry_date"] < datetime.utcnow():
        raise HTTPException(400, "This discount code has expired.")
    return {
        "code":  code,
        "value": float(disc.get("value", 0)),
        "message": f"Code valid — ₹{disc.get('value', 0):.0f} discount applied",
    }


# ═══════════════════════════════════════════════════════════════════
# PHASE 1: /merchant/products  —  Standard + Premium Products
# ═══════════════════════════════════════════════════════════════════

def _get_standard_product_limit() -> int:
    """Return the configurable standard product limit from pricing collection."""
    doc = _pricing_doc()
    return int(doc.get("standard_product_limit", 10))

def _is_store_subscription_active(store_id: str) -> bool:
    """Return True if the given store has an active/paid subscription that has not expired."""
    from datetime import datetime as _dt
    now = _dt.utcnow()
    sub = db.subscriptions.find_one(
        {"store_id": store_id, "status": {"$in": ["active", "paid"]}},
        sort=[("end_date", -1)],
    )
    if not sub:
        return False
    end = sub.get("end_date")
    if end is None:
        return True
    if isinstance(end, _dt):
        return end >= now
    try:
        return _dt.fromisoformat(str(end).replace("Z", "")) >= now
    except Exception:
        return False

@router.get("/products")
def list_merchant_products(m=Depends(get_merchant)):
    """List all products for this merchant: Standard (gift_vouchers) + Premium (merchant_vouchers)."""
    merchant_id    = _mid(m)
    merchant_phone = str(m.get("phone", ""))
    result = []

    # ── 1. Standard products from gift_vouchers ──
    for v in db.gift_vouchers.find({"product_type": "standard", "merchant_id": merchant_id}).sort("_id", -1):
        store_id      = v.get("store_id", "")
        sub_active    = _is_store_subscription_active(store_id) if store_id else False
        prod_approval = (v.get("approval_status") or v.get("status") or "waiting_approval")
        # Only show as Live if BOTH: store subscription active AND admin has approved the product
        if prod_approval == "approved":
            app_status = "approved"
        elif prod_approval == "subscription_expired":
            app_status = "subscription_expired"
        else:
            # Still pending admin approval (waiting_approval, pending_approval, etc.)
            app_status = prod_approval
        result.append({
            "_id":             str(v["_id"]),
            "product_type":    "standard",
            "title":           v.get("title", ""),
            "offer_text":      v.get("text", "") or v.get("offer_text", ""),
            "logo_url":        v.get("logo_url", "") or v.get("logo", ""),
            "price":           str(v.get("price", "") or ""),
            "original_price":  str(v.get("original_price", "") or ""),
            "store_id":        store_id,
            "store_name":      v.get("store_name", ""),
            "city":            v.get("city", ""),
            "is_active":       bool(v.get("is_active", True)),
            "approval_status": app_status,
            "status":          app_status,
            "end_date":        "",
            "duration_days":   0,
            "amount":          0,
            "created_at":      str(v.get("created_at", ""))[:19],
        })

    # ── 2. Premium products from merchant_vouchers ──
    # FIX: merchant_id in merchant_vouchers may be stored as ObjectId (from **prod spread
    # during upgrade) or as string — match both variants + phone fallback.
    _prem_or: list = [{"merchant_id": merchant_id}]
    try:
        _prem_or.append({"merchant_id": ObjectId(merchant_id)})
    except Exception:
        pass
    if merchant_phone:
        _prem_or.append({"merchant_phone": merchant_phone})
    # FIX: exclude deleted vouchers — same isolation fix as banners.
    # Admin delete-cascade sets status="deleted" + is_deleted=True.
    prem_query: dict = {"$and": [
        {"status": {"$ne": "deleted"}, "is_deleted": {"$ne": True}},
        {"$or": _prem_or},
    ]}
    for v in db.merchant_vouchers.find(prem_query).sort("created_at", -1):
        result.append({
            "_id":             str(v["_id"]),
            "product_type":    "premium",
            "title":           v.get("title", ""),
            "offer_text":      v.get("offer_text", ""),
            "logo_url":        v.get("logo_url", ""),
            "price":           str(v.get("price", "") or ""),
            "original_price":  str(v.get("original_price", "") or ""),
            "store_id":        v.get("store_id", ""),
            "store_name":      v.get("store_name", ""),
            "city":            v.get("city", ""),
            "is_active":       True,
            "duration_days":   v.get("duration_days", 0),
            "from_date":       v.get("from_date", ""),
            "end_date":        v.get("end_date", ""),
            "approval_status": v.get("approval_status") or v.get("status") or "pending_approval",
            "status":          v.get("status") or v.get("approval_status") or "pending",
            "amount":          v.get("total", 0),
            "created_at":      str(v.get("created_at", ""))[:19],
        })
    return result

@router.get("/products/limit")
def get_product_limit(m=Depends(get_merchant)):
    """Return the standard product limit and current standard / premium counts."""
    merchant_id    = _mid(m)
    limit          = _get_standard_product_limit()
    standard_count = db.gift_vouchers.count_documents({"product_type": "standard", "merchant_id": merchant_id})
    premium_count  = db.merchant_vouchers.count_documents({"merchant_id": merchant_id})
    return {
        "standard_product_limit": limit,
        "standard_count":         standard_count,
        "premium_count":          premium_count,
    }

@router.get("/products/pricing")
def get_product_pricing(m=Depends(get_merchant)):
    """Product pricing — returns per-day rate and GST for Premium products."""
    doc     = _pricing_doc()
    gst_pct = float(doc.get("gst_percent", 18))
    return {
        "price_per_day": float(doc.get("voucher_price_per_day", 10)),
        "gst_pct":       gst_pct,
    }

@router.post("/products/standard")
def create_standard_product(data: dict, m=Depends(get_merchant)):
    """Create a Standard Product — free, requires admin approval, linked to store subscription."""
    merchant_id = _mid(m)
    limit         = _get_standard_product_limit()
    current_count = db.gift_vouchers.count_documents({"product_type": "standard", "merchant_id": merchant_id})
    if current_count >= limit:
        raise HTTPException(
            400,
            f"Standard product limit reached ({limit}). "
            "Please delete an existing standard product or create a Premium Product instead.",
        )
    title = (data.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Product title is required")
    store_id = (data.get("store_id") or "").strip()
    if not store_id:
        raise HTTPException(400, "Please select a store for this product")
    try:
        store = db.stores.find_one({"_id": ObjectId(store_id)})
    except Exception:
        raise HTTPException(400, "Invalid store ID")
    if not store:
        raise HTTPException(404, "Store not found")

    logo_raw = (data.get("logo_url") or "").strip()
    if not logo_raw:
        raise HTTPException(400, "Product image is required")
    logo_url = _cloudinary_upload(logo_raw, folder="offro/products") if logo_raw else ""
    city     = (data.get("city") or store.get("city", "")).strip()

    product = {
        "product_type":    "standard",
        "merchant_id":     merchant_id,
        "merchant_name":   m.get("name", ""),
        "merchant_phone":  str(m.get("phone", "")),
        "title":           title,
        "text":            (data.get("offer_text") or "").strip(),
        "logo":            logo_url,
        "logo_url":        logo_url,
        "price":           str(data.get("price") or ""),
        "original_price":  str(data.get("original_price") or ""),
        "store_id":        store_id,
        "store_name":      store.get("store_name", data.get("store_name", "")),
        "city":            city,
        "is_active":       True,
        "status":          "pending",
        "approval_status": "pending",
        "source":          "merchant_standard",
        "created_at":      datetime.utcnow(),
    }
    res = db.gift_vouchers.insert_one(product)
    _log_tx(merchant_id, "product_standard",
            f"Standard Product created: '{title}'",
            amount=0, meta={"product_id": str(res.inserted_id)})
    return {
        "ok":        True,
        "product_id": str(res.inserted_id),
        "message":   "Standard product created successfully!",
    }

@router.post("/products/order")
def create_product_order(data: dict, m=Depends(get_merchant)):
    """Create Premium product order — delegates to voucher order flow."""
    return create_voucher_order(data, m)

@router.post("/products/activate-free")
def activate_free_product(data: dict, m=Depends(get_merchant)):
    """Activate free-tier Premium product — delegates to voucher activate-free flow."""
    return activate_free_voucher(data, m)

@router.post("/products/verify")
def verify_product_payment(data: dict, m=Depends(get_merchant)):
    """Verify Premium product Razorpay payment — delegates to voucher verify flow."""
    return verify_voucher_payment(data, m)

@router.put("/products/{pid}")
def update_product(pid: str, data: dict, m=Depends(get_merchant)):
    """Update a product (Standard or Premium) — only title, offer text, and prices."""
    merchant_id = _mid(m)
    try:
        oid = ObjectId(pid)
    except Exception:
        raise HTTPException(400, "Invalid product ID")

    # Try Premium (merchant_vouchers) first
    prem = db.merchant_vouchers.find_one({"_id": oid, "merchant_id": merchant_id})
    if prem:
        allowed = {"title", "offer_text", "price", "original_price"}
        upd = {k: v for k, v in data.items() if k in allowed}
        if upd:
            db.merchant_vouchers.update_one({"_id": oid}, {"$set": upd})
        return {"ok": True, "updated": "premium"}

    # Try Standard (gift_vouchers)
    std = db.gift_vouchers.find_one({"_id": oid, "merchant_id": merchant_id, "product_type": "standard"})
    if std:
        upd = {}
        for k, v in data.items():
            if k == "offer_text":
                upd["text"] = v
            elif k in {"title", "price", "original_price"}:
                upd[k] = v
        if upd:
            db.gift_vouchers.update_one({"_id": oid}, {"$set": upd})
        return {"ok": True, "updated": "standard"}

    raise HTTPException(404, "Product not found")

@router.delete("/products/{pid}")
def delete_product(pid: str, m=Depends(get_merchant)):
    """Delete a product (Standard or Premium) owned by this merchant."""
    merchant_id = _mid(m)
    try:
        oid = ObjectId(pid)
    except Exception:
        raise HTTPException(400, "Invalid product ID")

    res = db.merchant_vouchers.delete_one({"_id": oid, "merchant_id": merchant_id})
    if res.deleted_count > 0:
        return {"ok": True, "deleted": "premium"}

    res = db.gift_vouchers.delete_one({"_id": oid, "merchant_id": merchant_id, "product_type": "standard"})
    if res.deleted_count > 0:
        return {"ok": True, "deleted": "standard"}

    raise HTTPException(404, "Product not found")



# ═══════════════════════════════════════════════════════════
# PHASE 2 — PRODUCT MANAGEMENT ENHANCEMENTS
# ═══════════════════════════════════════════════════════════

@router.put("/products/{pid}/availability")
def toggle_product_availability(pid: str, data: dict, m=Depends(get_merchant)):
    """Toggle a product's is_available flag on/off."""
    merchant_id = _mid(m)
    available = bool(data.get("available", True))
    try:
        oid = ObjectId(pid)
    except Exception:
        raise HTTPException(400, "Invalid product ID")
    upd = {"$set": {"is_active": available, "updated_at": datetime.utcnow()}}
    res = db.gift_vouchers.update_one({"_id": oid, "merchant_id": merchant_id}, upd)
    if res.matched_count == 0:
        res = db.merchant_vouchers.update_one({"_id": oid, "merchant_id": merchant_id}, upd)
    if res.matched_count == 0:
        raise HTTPException(404, "Product not found")
    return {"ok": True, "available": available}


@router.get("/products/search")
def search_merchant_products(
    q: str = "", product_type: str = "", status: str = "", store_id: str = "",
    m=Depends(get_merchant)
):
    """Search/filter merchant's own products."""
    merchant_id = _mid(m)
    results = []

    def _match(p):
        title = (p.get("title") or "").lower()
        offer  = (p.get("offer_text") or "").lower()
        if q and q.lower() not in title and q.lower() not in offer:
            return False
        ptype = (p.get("product_type") or "premium")
        if product_type and ptype != product_type:
            return False
        st = (p.get("approval_status") or p.get("status") or "")
        if status and st != status:
            return False
        if store_id and str(p.get("store_id", "")) != store_id:
            return False
        return True

    for p in db.gift_vouchers.find({"merchant_id": merchant_id}).limit(200):
        if _match(p):
            p["_id"] = str(p["_id"])
            results.append(p)
    for p in db.merchant_vouchers.find({"merchant_id": merchant_id}).limit(200):
        if _match(p):
            p["_id"] = str(p["_id"])
            results.append(p)
    return results


@router.post("/products/{pid}/upgrade")
def upgrade_to_premium_order(pid: str, data: dict, m=Depends(get_merchant)):
    """Create a Razorpay order to upgrade a Standard product to Premium (per-day pricing)."""
    merchant_id = _mid(m)
    try:
        oid = ObjectId(pid)
    except Exception:
        raise HTTPException(400, "Invalid product ID")
    prod = db.gift_vouchers.find_one({"_id": oid, "merchant_id": merchant_id})
    if not prod:
        raise HTTPException(404, "Standard product not found")
    days          = max(1, int(data.get("days", 30)))
    from_date_str = data.get("from_date", datetime.utcnow().strftime("%Y-%m-%d"))
    pricing       = _pricing_doc()
    price_per_day = float(pricing.get("voucher_price_per_day", 10))
    gst_pct       = float(pricing.get("gst_percent", 0))
    base_price    = round(price_per_day * days, 2)
    discount_code  = (data.get("discount_code") or "").strip().upper()
    discount_value = 0.0
    discount_msg   = ""
    if discount_code:
        dc = db.discounts.find_one({"code": discount_code, "active": True})
        if dc:
            discount_value = float(dc.get("value", 0))
            discount_msg = f"Code {discount_code} applied"
        # ignore invalid codes silently for upgrade flow
    gst_amount = round(max(0, base_price - discount_value) * gst_pct / 100, 2)
    total      = round(max(0, base_price - discount_value) + gst_amount, 2)
    amount_paise = int(total * 100)
    try:
        from_date = datetime.strptime(from_date_str, "%Y-%m-%d")
    except Exception:
        from_date = datetime.utcnow()
    end_date = from_date + timedelta(days=days)
    _months  = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    def _df(dt): return f"{dt.day:02d} {_months[dt.month-1]} {dt.year}"

    # For ₹0 (free after discount) — skip Razorpay, create a manual order
    rp_order_id = None
    pay_mode    = "manual"
    if amount_paise > 0 and RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
        try:
            order_data = {"amount": amount_paise, "currency": "INR",
                          "receipt": f"upg_{pid[:8]}_{days}d",
                          "notes": {"product_id": pid, "days": str(days), "merchant_id": merchant_id, "type": "upgrade"}}
            resp = _razorpay_request("POST", "/v1/orders", (RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), order_data)
            rz = resp.json()
            rp_order_id = rz.get("id")
            if rp_order_id: pay_mode = "razorpay"
        except Exception:
            pay_mode = "manual"

    db.product_upgrade_orders.insert_one({
        "razorpay_order_id": rp_order_id, "product_id": pid, "merchant_id": merchant_id,
        "days": days, "from_date": from_date.isoformat(), "amount": total,
        "status": "created", "created_at": datetime.utcnow(),
    })
    return {"order_id": rp_order_id or "", "amount": total, "currency": "INR",
            "key": RAZORPAY_KEY_ID, "gst": gst_amount, "days": days,
            "from_date": _df(from_date), "end_date": _df(end_date),
            "price_per_day": price_per_day, "base_price": base_price,
            "gst_percent": gst_pct, "gst_amount": gst_amount,
            "discount_amount": discount_value, "amount_display": total,
            "amount_paise": amount_paise, "pay_mode": pay_mode,
            "razorpay_key": RAZORPAY_KEY_ID, "razorpay_order_id": rp_order_id,
            "discount_msg": discount_msg}


@router.post("/products/{pid}/upgrade/verify")
def verify_upgrade_payment(pid: str, data: dict, m=Depends(get_merchant)):
    """Verify Razorpay payment and convert Standard product to Premium.
    For ₹0 (free after discount) orders, signature verification is skipped."""
    merchant_id = _mid(m)
    order_id   = data.get("razorpay_order_id", "")
    payment_id = data.get("razorpay_payment_id", "")
    signature  = data.get("razorpay_signature", "")
    plan       = data.get("plan", "1month")

    # Check if this is a free (₹0) order — skip Razorpay verification
    # For ₹0 orders, order_id may be empty (no Razorpay order created).
    # Look up by product_id as fallback.
    upg_order_check = db.product_upgrade_orders.find_one({"razorpay_order_id": order_id}) if order_id else None
    if not upg_order_check:
        upg_order_check = db.product_upgrade_orders.find_one(
            {"product_id": pid, "status": "created"}, sort=[("created_at", -1)])
    _is_free = upg_order_check and float(upg_order_check.get("amount", 0)) <= 0

    if not _is_free:
        if not RAZORPAY_KEY_SECRET:
            raise HTTPException(503, "Payment verification unavailable — Razorpay not configured")
        body   = f"{order_id}|{payment_id}"
        digest = hmac.new(RAZORPAY_KEY_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if digest != signature:
            raise HTTPException(400, "Payment verification failed")
    try:
        oid = ObjectId(pid)
    except Exception:
        raise HTTPException(400, "Invalid product ID")
    prod = db.gift_vouchers.find_one({"_id": oid, "merchant_id": merchant_id})
    if not prod:
        # Check if already upgraded (idempotency — double-tap/retry)
        already = db.merchant_vouchers.find_one({"_id": oid})
        if already:
            return {"ok": True, "message": "Product already upgraded to premium.", "already_upgraded": True}
        raise HTTPException(404, "Product not found")
    # Retrieve days + from_date from the stored upgrade order (use fallback if already found)
    upg_order   = upg_order_check or db.product_upgrade_orders.find_one({"razorpay_order_id": order_id})
    days        = int(upg_order.get("days", 30)) if upg_order else 30
    from_date_s = (upg_order or {}).get("from_date")
    try:
        from_date = datetime.fromisoformat(from_date_s) if from_date_s else datetime.utcnow()
    except Exception:
        from_date = datetime.utcnow()
    end_date = from_date + timedelta(days=days)

    # ── Move the product IN-PLACE: same _id, gift_vouchers → merchant_vouchers ──
    # This avoids creating a duplicate — the product list shows it as the same item,
    # now as premium/pending_approval.
    # FIX Issue-2: standard products store offer text in "text" field; premium products
    # read "offer_text". Carry the value across so offer text is not lost on upgrade.
    # Also carry price/original_price ensuring they are preserved as strings.
    _offer_text_upgraded = (prod.get("offer_text") or "").strip() or (prod.get("text") or "").strip()
    _price_upgraded       = str(prod.get("price") or prod.get("offer_price") or "").strip()
    _orig_price_upgraded  = str(prod.get("original_price") or prod.get("mrp") or "").strip()
    updated_doc = {
        **prod,
        "product_type":       "premium",
        "status":             "pending_approval",
        "approval_status":    "pending_approval",
        "duration_days":      days,
        "start_date":         from_date,
        "end_date":           end_date,
        "razorpay_order_id":  order_id,
        "razorpay_payment_id": payment_id,
        "is_active":          True,
        "updated_at":         datetime.utcnow(),
        # FIX Issue-2: normalise field names for premium tier
        "offer_text":         _offer_text_upgraded,
        "text":               _offer_text_upgraded,
        "price":              _price_upgraded,
        "original_price":     _orig_price_upgraded,
    }
    updated_doc.pop("upgraded",    None)
    updated_doc.pop("upgraded_at", None)
    try:
        db.merchant_vouchers.insert_one(updated_doc)
    except Exception:
        # Already moved on a prior retry — just refresh the payment fields
        db.merchant_vouchers.update_one(
            {"_id": oid},
            {"$set": {k: v for k, v in updated_doc.items() if k != "_id"}},
        )
    db.gift_vouchers.delete_one({"_id": oid})
    db.product_upgrade_orders.update_one(
        {"razorpay_order_id": order_id},
        {"$set": {"status": "paid", "payment_id": payment_id, "paid_at": datetime.utcnow()}},
    )
    _log_tx(merchant_id, "product_upgrade", f"Product upgraded to premium ({plan})",
            amount=data.get("amount", 0), meta={"product_id": pid, "plan": plan})
    return {"ok": True, "message": "Product upgraded to premium. Awaiting admin approval."}


@router.post("/products/{pid}/renew")
def renew_premium_order(pid: str, data: dict, m=Depends(get_merchant)):
    """Create a Razorpay order to renew an expiring/expired Premium product (per-day pricing)."""
    merchant_id = _mid(m)
    try:
        oid = ObjectId(pid)
    except Exception:
        raise HTTPException(400, "Invalid product ID")
    prod = db.merchant_vouchers.find_one({"_id": oid, "merchant_id": merchant_id})
    if not prod:
        raise HTTPException(404, "Premium product not found")
    days          = max(1, int(data.get("days", 30)))
    pricing       = _pricing_doc()
    price_per_day = float(pricing.get("voucher_price_per_day", 10))
    gst_pct       = float(pricing.get("gst_percent", 0))
    base_price    = round(price_per_day * days, 2)
    discount_code  = (data.get("discount_code") or "").strip().upper()
    discount_value = 0.0
    discount_msg   = ""
    if discount_code:
        dc = db.discounts.find_one({"code": discount_code, "active": True})
        if dc:
            discount_value = float(dc.get("value", 0))
            discount_msg = f"Code {discount_code} applied"
    gst_amount = round(max(0, base_price - discount_value) * gst_pct / 100, 2)
    total      = round(max(0, base_price - discount_value) + gst_amount, 2)
    amount_paise = int(total * 100)
    # Compute renewal period from current end_date
    existing_end = prod.get("end_date")
    if isinstance(existing_end, datetime) and existing_end > datetime.utcnow():
        renew_from = existing_end
    else:
        renew_from = datetime.utcnow()
    new_end  = renew_from + timedelta(days=days)
    _months  = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    def _df(dt): return f"{dt.day:02d} {_months[dt.month-1]} {dt.year}"

    # For ₹0 (free after discount) — skip Razorpay, create a manual order
    rp_order_id = None
    pay_mode    = "manual"
    if amount_paise > 0 and RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
        try:
            order_data = {"amount": amount_paise, "currency": "INR",
                          "receipt": f"ren_{pid[:8]}_{days}d",
                          "notes": {"product_id": pid, "days": str(days), "merchant_id": merchant_id, "type": "renew"}}
            resp = _razorpay_request("POST", "/v1/orders", (RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), order_data)
            rz = resp.json()
            rp_order_id = rz.get("id")
            if rp_order_id: pay_mode = "razorpay"
        except Exception:
            pay_mode = "manual"

    db.product_renewal_orders.insert_one({
        "razorpay_order_id": rp_order_id, "product_id": pid, "merchant_id": merchant_id,
        "days": days, "amount": total, "status": "created", "created_at": datetime.utcnow(),
    })
    return {"order_id": rp_order_id or "", "amount": total, "currency": "INR",
            "key": RAZORPAY_KEY_ID, "gst": gst_amount, "days": days,
            "from_date": _df(renew_from), "end_date": _df(new_end),
            "price_per_day": price_per_day, "base_price": base_price,
            "gst_percent": gst_pct, "gst_amount": gst_amount,
            "discount_amount": discount_value, "amount_display": total,
            "amount_paise": amount_paise, "pay_mode": pay_mode,
            "razorpay_key": RAZORPAY_KEY_ID, "razorpay_order_id": rp_order_id,
            "discount_msg": discount_msg}


@router.post("/products/{pid}/renew/verify")
def verify_renewal_payment(pid: str, data: dict, m=Depends(get_merchant)):
    """Verify Razorpay payment and extend Premium product end_date.
    For ₹0 (free after discount) orders, signature verification is skipped."""
    merchant_id = _mid(m)
    order_id   = data.get("razorpay_order_id", "")
    payment_id = data.get("razorpay_payment_id", "")
    signature  = data.get("razorpay_signature", "")
    plan       = data.get("plan", "1month")

    # Check if this is a free (₹0) order — skip Razorpay verification
    # For ₹0 orders, order_id may be empty (no Razorpay order created).
    ren_order_check = db.product_renewal_orders.find_one({"razorpay_order_id": order_id}) if order_id else None
    if not ren_order_check:
        ren_order_check = db.product_renewal_orders.find_one(
            {"product_id": pid, "status": "created"}, sort=[("created_at", -1)])
    _is_free = ren_order_check and float(ren_order_check.get("amount", 0)) <= 0

    if not _is_free:
        if not RAZORPAY_KEY_SECRET:
            raise HTTPException(503, "Payment verification unavailable — Razorpay not configured")
        body   = f"{order_id}|{payment_id}"
        digest = hmac.new(RAZORPAY_KEY_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if digest != signature:
            raise HTTPException(400, "Payment verification failed")
    try:
        oid = ObjectId(pid)
    except Exception:
        raise HTTPException(400, "Invalid product ID")
    # Retrieve days from the stored renewal order (use fallback if already found)
    ren_order = ren_order_check or db.product_renewal_orders.find_one({"razorpay_order_id": order_id})
    days = int(ren_order.get("days", 30)) if ren_order else 30
    prod = db.merchant_vouchers.find_one({"_id": oid, "merchant_id": merchant_id}, {"end_date": 1})
    existing_end = prod.get("end_date") if prod else None
    base = (existing_end if isinstance(existing_end, datetime) and existing_end > datetime.utcnow()
            else datetime.utcnow())
    new_end = base + timedelta(days=days)
    db.merchant_vouchers.update_one(
        {"_id": oid},
        {"$set": {"end_date": new_end, "status": "approved", "approval_status": "approved",
                  "renewed_at": datetime.utcnow(), "last_renewal_plan": plan,
                  "razorpay_renewal_order_id": order_id}},
    )
    db.product_renewal_orders.update_one(
        {"razorpay_order_id": order_id},
        {"$set": {"status": "paid", "payment_id": payment_id, "paid_at": datetime.utcnow()}},
    )
    _log_tx(merchant_id, "product_renew", f"Premium product renewed ({plan})",
            amount=data.get("amount", 0), meta={"product_id": pid, "plan": plan})
    return {"ok": True, "new_end_date": new_end.isoformat(),
            "message": f"Product renewed for {days} days."}


@router.post("/products/bulk-action")
def bulk_product_action(data: dict, m=Depends(get_merchant)):
    """Bulk activate / deactivate / delete products."""
    merchant_id = _mid(m)
    ids    = [i for i in data.get("ids", []) if i]
    action = data.get("action", "")
    if not ids or action not in ("activate", "deactivate", "delete"):
        raise HTTPException(400, "ids and valid action required")
    try:
        oids = [ObjectId(i) for i in ids]
    except Exception:
        raise HTTPException(400, "One or more invalid product IDs")
    filter_ = {"_id": {"$in": oids}, "merchant_id": merchant_id}
    count = 0
    if action == "delete":
        count += db.gift_vouchers.delete_many(filter_).deleted_count
        count += db.merchant_vouchers.delete_many(filter_).deleted_count
    elif action == "activate":
        upd = {"$set": {"is_available": True, "updated_at": datetime.utcnow()}}
        count += db.gift_vouchers.update_many(filter_, upd).modified_count
        count += db.merchant_vouchers.update_many(filter_, upd).modified_count
    else:
        upd = {"$set": {"is_available": False, "updated_at": datetime.utcnow()}}
        count += db.gift_vouchers.update_many(filter_, upd).modified_count
        count += db.merchant_vouchers.update_many(filter_, upd).modified_count
    return {"ok": True, "affected": count, "action": action}


@router.get("/products/expiry-check")
def check_product_expiry(m=Depends(get_merchant)):
    """Return premium products expiring within the next 7 days."""
    merchant_id = _mid(m)
    now  = datetime.utcnow()
    soon = now + timedelta(days=7)
    docs = list(db.merchant_vouchers.find(
        {"merchant_id": merchant_id, "end_date": {"$gte": now, "$lte": soon}},
        {"_id": 1, "title": 1, "end_date": 1, "status": 1}
    ).limit(50))
    return [{"id": str(d["_id"]), "title": d.get("title",""),
             "end_date": str(d.get("end_date",""))[:19],
             "days_left": max(0,(d["end_date"]-now).days) if isinstance(d.get("end_date"), datetime) else 0}
            for d in docs]


@router.get("/products/{pid}/analytics")
def get_product_analytics(pid: str, m=Depends(get_merchant)):
    """Return view/share/open event counts for a product."""
    merchant_id = _mid(m)
    try:
        ObjectId(pid)
    except Exception:
        raise HTTPException(400, "Invalid product ID")
    events = list(db.product_events.find({"product_id": pid, "merchant_id": merchant_id}, {"event": 1}))
    counts = {"view": 0, "share": 0, "open": 0}
    for e in events:
        ev = e.get("event", "")
        if ev in counts:
            counts[ev] += 1
    return {"product_id": pid, **counts, "total": sum(counts.values())}


@router.get("/products/{pid}/history")
def get_product_history(pid: str, m=Depends(get_merchant)):
    """Return the activity history log for a product."""
    merchant_id = _mid(m)
    try:
        ObjectId(pid)
    except Exception:
        raise HTTPException(400, "Invalid product ID")
    logs = list(db.product_history.find(
        {"product_id": pid, "merchant_id": merchant_id}, {"_id": 0}
    ).sort("created_at", -1).limit(50))
    for log in logs:
        if "created_at" in log:
            log["created_at"] = str(log["created_at"])[:19]
    return logs
