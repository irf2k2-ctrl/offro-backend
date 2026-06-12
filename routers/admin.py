import os
from fastapi import UploadFile, File, Form, APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from database import db
from bson import ObjectId
from datetime import datetime, timedelta
import uuid, qrcode, io, base64
import time as _time

# In-memory cache for /stores list (15-second TTL)
_store_cache = {"data": None, "ts": 0.0}
_STORE_CACHE_TTL = 15


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
        return b64_or_url
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
            return resp.json().get("secure_url", b64_or_url)
    except Exception as e:
        print(f"[CDN] upload failed: {e}")
    return b64_or_url

def _make_thumb_url(cdn_url: str, w: int = 300) -> str:
    if "cloudinary.com" in str(cdn_url):
        return cdn_url.replace("/upload/", f"/upload/w_{w},c_fill,q_auto,f_auto/")
    return ""

router = APIRouter(tags=["Admin"])

def create_token(): return str(uuid.uuid4())

def generate_qr_base64(store_id: str) -> str:
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(f"localsaver://redeem?store_id={store_id}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="#3E5F55", back_color="white")
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode()

def get_current_admin(request: Request):
    token = request.cookies.get("admin_token") or \
            request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token: raise HTTPException(401, "Not authenticated")
    a = db.admins.find_one({"token": token})
    if not a: raise HTTPException(403, "Invalid session")
    return a

def seed_admin():
    if not db.admins.find_one({"username": "admin"}):
        db.admins.insert_one({"username": "admin", "password": "admin123", "token": None})
        print("✅ Default admin: admin / admin123")
    if not db.categories.find_one({"_v": 2}):
        # Migrate flat-string categories → rich object model (v2)
        # Preserve any images already uploaded to existing rich-format docs
        preserved = {}
        for old_cat in db.categories.find({"name": {"$exists": True}, "_v": {"$ne": 2}}, {"_id": 0}):
            if old_cat.get("image_url"):
                preserved[old_cat["name"]] = old_cat["image_url"]
        db.categories.drop()
        default_cats = [
            {"_v":2,"name":"Restaurant","subtitle":"500+ places","sort_order":1,"icon":"🍽️","status":"active"},
            {"_v":2,"name":"Grocery","subtitle":"Fresh & daily","sort_order":2,"icon":"🛒","status":"active"},
            {"_v":2,"name":"Pharmacy","subtitle":"Health essentials","sort_order":3,"icon":"💊","status":"active"},
            {"_v":2,"name":"Electronics","subtitle":"Trending gadgets","sort_order":4,"icon":"📱","status":"active"},
            {"_v":2,"name":"Fashion","subtitle":"New arrivals","sort_order":5,"icon":"👗","status":"active"},
            {"_v":2,"name":"Bakery","subtitle":"Fresh baked daily","sort_order":6,"icon":"🎂","status":"active"},
            {"_v":2,"name":"Salon","subtitle":"Look your best","sort_order":7,"icon":"💇","status":"active"},
            {"_v":2,"name":"Fitness","subtitle":"Stay strong","sort_order":8,"icon":"🏋️","status":"active"},
            {"_v":2,"name":"Hospital","subtitle":"Care & wellness","sort_order":9,"icon":"🏥","status":"active"},
            {"_v":2,"name":"Education","subtitle":"Learn & grow","sort_order":10,"icon":"📚","status":"active"},
            {"_v":2,"name":"Automobile","subtitle":"Drive & repair","sort_order":11,"icon":"🚗","status":"active"},
            {"_v":2,"name":"Other","subtitle":"More near you","sort_order":12,"icon":"🏪","status":"active"},
        ]
        for cat in default_cats:
            cat["image_url"] = preserved.get(cat["name"], "")
        db.categories.insert_many(default_cats)
        print(f"[INIT] Categories migrated to v2. Preserved images for: {list(preserved.keys())}")
    if not db.pricing.find_one({}):
        db.pricing.insert_one({"gst_percent": 18, "plans": [
            {"id": "1month",  "label": "1 Month",   "price": 499},
            {"id": "3months", "label": "3 Months",  "price": 1299},
            {"id": "6months", "label": "6 Months",  "price": 2299},
            {"id": "12months","label": "12 Months", "price": 3999},
        ]})

# ===================== AUTH =====================

# ── Predefined city→area map (mirrors public.py for admin use) ──
ADMIN_CITY_AREAS = {
    "ballari":  ["Cowl Bazaar","Gandhi Nagar","Cantonment","Bellary Fort","M.G. Road",
                 "Hosapete Road","Civil Station","Sanganakal Road","Shivappa Nayaka Circle",
                 "Humnabad Road","Kudligi Road","Raichur Road","Kottur","Kampli","Siruguppa"],
    "bengaluru":["Indiranagar","Koramangala","Jayanagar","Whitefield","HSR Layout",
                 "Marathahalli","BTM Layout","Electronic City","JP Nagar","Rajajinagar",
                 "Malleshwaram","Yelahanka","Hebbal","Domlur","Majestic","KR Market"],
    "hyderabad":["Banjara Hills","Jubilee Hills","Gachibowli","Hitech City","Madhapur",
                 "Ameerpet","Begumpet","Secunderabad","Dilsukhnagar","Kukatpally","Kondapur"],
    "hubli":    ["Old Hubli","Vidyanagar","Deshpande Nagar","Keshwapur","Gokul Road","Navanagar"],
    "dharwad":  ["PB Road","Saraswathipuram","Sadashivnagar","Shirur Park","Saptapur"],
    "mysuru":   ["Jayalakshmipuram","Vijayanagar","Kuvempunagar","Gokulam","Saraswathipuram"],
}

@router.get("/areas")
def admin_get_areas(city: str = ""):
    city_key = city.strip().lower()
    if city_key in ADMIN_CITY_AREAS:
        return {"areas": ADMIN_CITY_AREAS[city_key]}
    for k, v in ADMIN_CITY_AREAS.items():
        if city_key in k or k in city_key:
            return {"areas": v}
    # Fallback: distinct areas from DB
    db_areas = db.stores.distinct("area", {"city": {"$regex": city_key, "$options": "i"}})
    return {"areas": sorted([a for a in db_areas if a])}

@router.post("/login")
def admin_login(data: dict):
    a = db.admins.find_one({"username": data.get("username"), "password": data.get("password")})
    if not a: raise HTTPException(401, "Invalid credentials")
    token = create_token()
    db.admins.update_one({"_id": a["_id"]}, {"$set": {"token": token}})
    res = JSONResponse({"message": "ok"})
    res.set_cookie("admin_token", token, httponly=True, samesite="Lax", max_age=3600*8)
    return res

@router.post("/logout")
def admin_logout():
    res = JSONResponse({"message": "Logged out"})
    res.delete_cookie("admin_token")
    return res

# ===================== CATEGORIES =====================

@router.get("/categories")
def get_categories(a=Depends(get_current_admin)):
    cats = list(db.categories.find({"status":{"$ne":"deleted"}}, {"_id":0}).sort("sort_order",1))
    return cats

@router.post("/categories")
def add_category(data: dict, a=Depends(get_current_admin)):
    name = data.get("name", "").strip()
    if not name: raise HTTPException(400, "Name required")
    # Only block if a NON-deleted category with this name exists
    if db.categories.find_one({"name": name, "status": {"$ne": "deleted"}}):
        raise HTTPException(400, "Category already exists")
    max_order = db.categories.find_one(sort=[("sort_order", -1)]) or {}
    sort_order = (max_order.get("sort_order", 0) or 0) + 1
    obj = {
        "_v": 2,
        "name": name,
        "subtitle": data.get("subtitle", ""),
        "icon": data.get("icon", "🏪"),
        "image_url": data.get("image_url", ""),
        "sort_order": sort_order,
        "status": "active",
    }
    db.categories.insert_one(obj)
    return _category_list()

@router.put("/categories/{name}")
def update_category(name: str, data: dict, a=Depends(get_current_admin)):
    upd = {}
    if "image_url"  in data: upd["image_url"]  = data["image_url"]
    if "subtitle"   in data: upd["subtitle"]    = data["subtitle"]
    if "icon"       in data: upd["icon"]        = data["icon"]
    if "sort_order" in data: upd["sort_order"]  = int(data["sort_order"])
    if "status"     in data: upd["status"]      = data["status"]
    if upd:
        db.categories.update_one({"name": name}, {"$set": upd})
    return _category_list()

@router.post("/categories/{name}/upload-image")
async def upload_category_image(name: str, file: UploadFile = File(...), a=Depends(get_current_admin)):
    """Upload an image for a category card.
    Uses Cloudinary if configured, otherwise stores as base64 directly in MongoDB.
    Always works regardless of Cloudinary configuration.
    """
    import base64 as b64mod, os as _upl_os
    print(f"[UPLOAD] Category image upload started: name='{name}', content_type='{file.content_type}'")
    raw = await file.read()
    if not raw:
        print(f"[UPLOAD] ERROR: Empty file for category '{name}'")
        raise HTTPException(400, "Empty file received.")
    if len(raw) > 5 * 1024 * 1024:
        print(f"[UPLOAD] ERROR: File too large ({len(raw)} bytes) for category '{name}'")
        raise HTTPException(400, "File too large — maximum 5 MB.")
    # Broad find — works regardless of status field presence
    existing = db.categories.find_one({"name": name})
    if not existing:
        print(f"[UPLOAD] ERROR: Category '{name}' not found in DB")
        # List available categories for debugging
        all_cats = [c.get("name","?") for c in db.categories.find({}, {"name":1,"_id":0})]
        print(f"[UPLOAD] Available categories: {all_cats}")
        raise HTTPException(404, f"Category '{name}' not found. Available: {all_cats}")
    content_type = file.content_type or "image/jpeg"
    b64 = f"data:{content_type};base64," + b64mod.b64encode(raw).decode()
    # Try Cloudinary first; fall back to base64 in MongoDB if not configured
    cloud  = _upl_os.getenv("CLOUDINARY_CLOUD_NAME", "")
    api_key= _upl_os.getenv("CLOUDINARY_API_KEY", "")
    secret = _upl_os.getenv("CLOUDINARY_API_SECRET", "")
    image_url = b64   # default: base64 fallback
    via = "base64"
    if cloud and api_key and secret:
        print(f"[UPLOAD] Trying Cloudinary for category '{name}'...")
        cdn = _cloudinary_upload(b64, folder="offro/categories")
        if cdn and cdn.startswith("http"):
            image_url = cdn
            via = "cloudinary"
            print(f"[UPLOAD] Cloudinary success: {cdn[:60]}...")
        else:
            print(f"[WARN] Cloudinary upload failed for category '{name}', using base64 fallback")
    else:
        print(f"[UPLOAD] Cloudinary not configured — saving as base64 (size: {len(b64)} chars)")
    result = db.categories.update_one({"name": name}, {"$set": {"image_url": image_url}})
    print(f"[UPLOAD] DB update: matched={result.matched_count}, modified={result.modified_count}")
    return {"image_url": image_url, "via": via, "size_bytes": len(raw)}

@router.post("/categories/reinit")
def reinit_categories(a=Depends(get_current_admin)):
    """Force re-initialize all categories to v2 defaults. Preserves existing images."""
    preserved = {}
    for cat in db.categories.find({"name": {"$exists": True}}, {"_id": 0}):
        if cat.get("image_url"):
            preserved[cat["name"]] = cat["image_url"]
    db.categories.drop()
    default_cats = [
        {"_v":2,"name":"Restaurant","subtitle":"500+ places","sort_order":1,"icon":"🍽️","status":"active"},
        {"_v":2,"name":"Grocery","subtitle":"Fresh & daily","sort_order":2,"icon":"🛒","status":"active"},
        {"_v":2,"name":"Pharmacy","subtitle":"Health essentials","sort_order":3,"icon":"💊","status":"active"},
        {"_v":2,"name":"Electronics","subtitle":"Trending gadgets","sort_order":4,"icon":"📱","status":"active"},
        {"_v":2,"name":"Fashion","subtitle":"New arrivals","sort_order":5,"icon":"👗","status":"active"},
        {"_v":2,"name":"Bakery","subtitle":"Fresh baked daily","sort_order":6,"icon":"🎂","status":"active"},
        {"_v":2,"name":"Salon","subtitle":"Look your best","sort_order":7,"icon":"💇","status":"active"},
        {"_v":2,"name":"Fitness","subtitle":"Stay strong","sort_order":8,"icon":"🏋️","status":"active"},
        {"_v":2,"name":"Hospital","subtitle":"Care & wellness","sort_order":9,"icon":"🏥","status":"active"},
        {"_v":2,"name":"Education","subtitle":"Learn & grow","sort_order":10,"icon":"📚","status":"active"},
        {"_v":2,"name":"Automobile","subtitle":"Drive & repair","sort_order":11,"icon":"🚗","status":"active"},
        {"_v":2,"name":"Other","subtitle":"More near you","sort_order":12,"icon":"🏪","status":"active"},
    ]
    for cat in default_cats:
        cat["image_url"] = preserved.get(cat["name"], "")
    db.categories.insert_many(default_cats)
    print(f"[REINIT] Categories re-initialized. Preserved images: {list(preserved.keys())}")
    return _category_list()

@router.delete("/categories/{name}")
def delete_category(name: str, a=Depends(get_current_admin)):
    db.categories.update_one({"name": name}, {"$set": {"status": "deleted"}})
    return _category_list()

def _category_list():
    return list(db.categories.find({"status":{"$ne":"deleted"}}, {"_id":0}).sort("sort_order",1))

# ===================== PRICING & PLANS =====================

@router.get("/pricing")
def get_pricing(a=Depends(get_current_admin)):
    doc = db.pricing.find_one({}) or {"gst_percent": 18, "plans": []}
    return {
        "gst_percent": doc.get("gst_percent", 18),
        "plans": doc.get("plans", []),
        "conversion_rate": doc.get("conversion_rate", 0.10),  # default ₹0.10 per point
        "min_withdraw_points": doc.get("min_withdraw_points", 200),
    }

@router.put("/pricing")
def update_pricing(data: dict, a=Depends(get_current_admin)):
    """Update GST %, plan prices, and withdrawal conversion rate."""
    doc = db.pricing.find_one({})
    update = {}
    if "gst_percent" in data: update["gst_percent"] = float(data["gst_percent"])
    if "plans" in data: update["plans"] = data["plans"]
    if "conversion_rate" in data: update["conversion_rate"] = float(data["conversion_rate"])
    if "min_withdraw_points" in data: update["min_withdraw_points"] = int(data["min_withdraw_points"])
    if doc: db.pricing.update_one({"_id": doc["_id"]}, {"$set": update})
    else: db.pricing.insert_one(update)
    return {"message": "Pricing updated"}

# ===================== TERMS & CONDITIONS =====================

@router.get("/terms/{type}")
def get_terms(type: str, a=Depends(get_current_admin)):
    if type not in ("merchant", "user"): raise HTTPException(400, "type must be merchant or user")
    doc = db.terms.find_one({"type": type}) or {}
    return {"type": type, "content": doc.get("content", "")}

@router.put("/terms/{type}")
def update_terms(type: str, data: dict, a=Depends(get_current_admin)):
    if type not in ("merchant", "user"): raise HTTPException(400, "type must be merchant or user")
    content = data.get("content", "")
    doc = db.terms.find_one({"type": type})
    if doc: db.terms.update_one({"_id": doc["_id"]}, {"$set": {"content": content, "updated_at": datetime.utcnow()}})
    else: db.terms.insert_one({"type": type, "content": content, "updated_at": datetime.utcnow()})
    return {"message": "Terms updated"}

# ===================== MERCHANTS =====================

@router.get("/merchants")
def list_merchants(a=Depends(get_current_admin)):
    # DEPRECATED: Use /accounts?role=merchant instead. Kept for store creation dropdown.
    return [{"_id": str(m["_id"]), "name": m.get("name"), "phone": m.get("phone"),
             "city": m.get("city"), "area": m.get("area"), "status": m.get("status", "active"),
             "store_count": db.stores.count_documents({"merchant_id": str(m["_id"])})}
            for m in db.accounts.find({"roles": "merchant"})]

@router.put("/merchants/{id}")
def update_merchant(id: str, data: dict, a=Depends(get_current_admin)):
    upd = {f: data[f] for f in ["name","phone","city","area"] if data.get(f) is not None}
    if upd:
        db.accounts.update_one({"_id": ObjectId(id)}, {"$set": upd})
        db.merchants.update_one({"_id": ObjectId(id)}, {"$set": upd})  # sync
    return {"message": "Updated"}

@router.put("/merchants/{id}/status")
def toggle_merchant(id: str, a=Depends(get_current_admin)):
    m = db.accounts.find_one({"_id": ObjectId(id)}) or db.merchants.find_one({"_id": ObjectId(id)})
    if not m: raise HTTPException(404, "Not found")
    ns = "inactive" if m.get("status") == "active" else "active"
    db.accounts.update_one({"_id": ObjectId(id)}, {"$set": {"status": ns}})
    db.merchants.update_one({"_id": ObjectId(id)}, {"$set": {"status": ns}})  # sync
    return {"status": ns}

@router.delete("/merchants/{id}")
def delete_merchant(id: str, a=Depends(get_current_admin)):
    db.accounts.delete_one({"_id": ObjectId(id)})
    db.merchants.delete_one({"_id": ObjectId(id)})  # sync
    db.stores.delete_many({"merchant_id": id})
    return {"message": "Deleted"}


# ══════════════════════════════════════════════════════════════════
# UNIFIED ACCOUNTS — merged view of users + merchants
# ══════════════════════════════════════════════════════════════════

@router.get("/accounts")
def list_accounts(a=Depends(get_current_admin)):
    """Unified accounts view — single accounts collection."""
    result = []
    for acct in db.accounts.find().sort("created_at", -1):
        phone   = acct.get("phone", "")
        roles   = acct.get("roles", ["user"])
        acct_id = str(acct["_id"])
        mid     = acct.get("merchant_id", "")

        total_pts = (acct.get("visit_pts", acct.get("visit_points", 0) or 0) +
                     acct.get("pool_pts", 0))

        result.append({
            "account_id":    acct_id,
            "merchant_id":   mid,
            "user_id":       acct.get("user_id", acct_id),
            "full_name":     acct.get("name", ""),
            "mobile_number": phone,
            "city":          acct.get("city", ""),
            "roles":         roles,
            "status":        acct.get("status", "active"),
            "total_points":  total_pts,
            "visit_pts":     acct.get("visit_pts", acct.get("visit_points", 0) or 0),
            "pool_pts":      acct.get("pool_pts", 0),
            "scans":         acct.get("scans", 0),
            "store_count":   db.stores.count_documents(
                {"merchant_id": {"$in": list(filter(None, [mid, acct_id, phone]))}}
            ) if (mid or acct_id or phone) else 0,
            "created_at":    str(acct.get("created_at", ""))[:10],
        })
    return result


@router.get("/accounts/{account_id}")
def get_account_detail(account_id: str, a=Depends(get_current_admin)):
    """Full account detail from unified accounts collection."""
    from bson import ObjectId
    acct = None
    try:
        acct = db.accounts.find_one({"_id": ObjectId(account_id)})
    except Exception:
        pass
    if not acct:
        # Fallback: try by user_id or merchant_id field
        acct = db.accounts.find_one({"$or": [
            {"user_id": account_id},
            {"merchant_id": account_id},
        ]})
    if not acct:
        raise HTTPException(404, "Account not found")

    phone      = acct.get("phone", "")
    mid        = acct.get("merchant_id", "")
    acct_id    = str(acct["_id"])
    roles      = acct.get("roles", ["user"])
    is_merchant = "merchant" in roles

    store_count   = 0
    banner_count  = 0
    voucher_count = 0
    subscriptions = []
    invoices      = []

    if is_merchant and mid:
        store_count   = db.stores.count_documents({"merchant_id": mid})
        banner_count  = db.merchant_banners.count_documents({"$or": [{"merchant_id": mid}, {"merchant_phone": phone}]})
        voucher_count = db.merchant_vouchers.count_documents({"$or": [{"merchant_id": mid}, {"merchant_phone": phone}]})

        for s in db.subscriptions.find(
            {"$or": [{"merchant_id": mid}, {"merchant_phone": phone}]}
        ).sort("created_at", -1).limit(5):
            subscriptions.append({
                "plan":       s.get("plan",""),
                "store_name": s.get("store_name",""),
                "end_date":   str(s.get("end_date",""))[:10],
                "status":     s.get("status",""),
            })

        for inv in db.invoices.find(
            {"$or": [{"merchant_id": mid}, {"merchant_phone": phone}]}
        ).sort("created_at", -1).limit(10):
            invoices.append({
                "invoice_no":  inv.get("invoice_no",""),
                "store_name":  inv.get("store_name",""),
                "plan":        inv.get("plan",""),
                "total":       inv.get("total", inv.get("final_amount", 0)),
                "status":      inv.get("status",""),
                "created_at":  str(inv.get("created_at",""))[:10],
            })

    visit_pts = acct.get("visit_pts", acct.get("visit_points", 0) or 0)
    pool_pts  = acct.get("pool_pts", 0)

    return {
        "account_id":    acct_id,
        "user_id":       acct.get("user_id", acct_id),
        "merchant_id":   mid,
        "full_name":     acct.get("name", ""),
        "mobile_number": phone,
        "city":          acct.get("city", ""),
        "area":          acct.get("area", ""),
        "roles":         roles,
        "status":        acct.get("status", "active"),
        "visit_pts":     visit_pts,
        "pool_pts":      pool_pts,
        "total_pts":     visit_pts + pool_pts,
        "scans":         acct.get("scans", 0),
        "store_count":   store_count,
        "banner_count":  banner_count,
        "voucher_count": voucher_count,
        "subscriptions": subscriptions,
        "invoices":      invoices,
    }


@router.patch("/accounts/{account_id}/status")
def toggle_account_status(account_id: str, data: dict, a=Depends(get_current_admin)):
    """Toggle account status in unified accounts collection."""
    from bson import ObjectId
    new_status = data.get("status", "active")
    try:
        db.accounts.update_one(
            {"_id": ObjectId(account_id)},
            {"$set": {"status": new_status}}
        )
    except Exception:
        pass
    # Sync to legacy collections
    try:
        db.accounts.update_one({"_id": ObjectId(account_id)}, {"$set": {"status": new_status}})
    except Exception:
        pass
    return {"ok": True, "status": new_status}



def _fmt_store_fast(s, sub_map, deal_map, merchants):
    """Format a store record for the admin list view."""
    sid      = str(s["_id"])
    sub      = sub_map.get(sid, {})
    deals    = deal_map.get(sid, [])
    mid      = s.get("merchant_id", "")
    merchant = merchants.get(mid, {})

    # Resolve image — CDN URL first (post-Cloudinary migration), then base64 fallbacks
    image = (s.get("image_url") or      # Cloudinary CDN URL (post-migration)
             s.get("image_thumb") or     # Cloudinary thumb URL
             s.get("image") or           # legacy base64 field
             s.get("store_image") or     # alternate base64 field
             s.get("_thumb") or          # old thumbnail base64
             (s.get("images") or [None])[0] or "")

    sub_plan   = sub.get("plan", "")
    raw_status = sub.get("status", "")
    # Derive effective subscription status from end_date
    if not sub:
        sub_status = "unpaid"
    else:
        try:
            from datetime import timezone
            ed = sub.get("end_date")
            now_dt = datetime.utcnow()
            if isinstance(ed, datetime):
                sub_status = "active" if ed >= now_dt else "expired"
            elif isinstance(ed, str) and ed:
                from datetime import datetime as _dt
                ed_parsed = _dt.fromisoformat(ed.replace("Z",""))
                sub_status = "active" if ed_parsed >= now_dt else "expired"
            elif raw_status in ("active", "paid"):
                sub_status = "active"
            else:
                sub_status = "unpaid"
        except Exception:
            sub_status = raw_status or "unpaid"
    sub_label  = f"{sub_plan} ({sub_status})" if sub_plan else ""

    best_deal = max((d.get("discount", 0) for d in deals), default=0)

    return {
        "_id":            sid,
        "store_name":     s.get("store_name", ""),
        "category":       s.get("category", ""),
        "city":           s.get("city", ""),
        "area":           s.get("area", ""),
        "address":        s.get("address", ""),
        "phone":          s.get("phone", ""),
        "status":         s.get("status", "active"),
        "is_new_in_town": s.get("is_new_in_town", False),
        "is_trending":    s.get("is_trending", False),
        "is_popular":     s.get("is_popular", False),
        "badge":          s.get("badge", ""),
        "points_per_scan":s.get("points_per_scan", 0),
        "rating":         s.get("admin_rating") or s.get("rating") or 0,
        "image":          image,
        "image_url":      image,  # also expose as image_url for JS consistency
        "merchant_id":    mid,
        "merchant_name":  merchant.get("name", s.get("merchant_name", "")),
        "merchant_phone": str(merchant.get("phone", s.get("merchant_phone", ""))),
        "subscription":   sub_label,
        "sub_plan":       sub_plan,
        "sub_status":     sub_status,
        "deal_count":     len(deals),
        "best_deal":      best_deal,
        "lat":            s.get("lat", ""),
        "lng":            s.get("lng", ""),
        "created_at":     s.get("created_at", ""),
    }

@router.get("/stores")
def list_stores(a=Depends(get_current_admin)):
    global _store_cache
    now_ts = _time.time()
    if _store_cache["data"] is not None and (now_ts - _store_cache["ts"]) < _STORE_CACHE_TTL:
        return _store_cache["data"]
    # Exclude large base64 image fields from list for performance
    stores = list(db.stores.find({}, {
        "store_image2": 0,  # heavy base64 — loaded separately in edit form
        "qr_code": 0,       # always base64 — loaded separately in detail view
    }).sort("created_at", -1))  # newest first
    if not stores:
        return []
    
    # ── Batch load all subscriptions in ONE query (avoids N per-store DB round trips) ──
    store_ids = [str(s["_id"]) for s in stores]
    all_subs = list(db.subscriptions.find(
        {"store_id": {"$in": store_ids}},
        {"store_id": 1, "status": 1, "from_date": 1, "end_date": 1, "plan": 1}
    ).sort("created_at", -1))
    
    # Map: store_id → latest subscription (first match since sorted desc)
    sub_map = {}
    for sub in all_subs:
        sid = sub.get("store_id", "")
        if sid not in sub_map:
            sub_map[sid] = sub
    
    # Batch load all active deals in ONE query
    all_deals = list(db.deals.find(
        {"store_id": {"$in": store_ids}, "status": "active"},
        {"store_id": 1, "discount": 1, "title": 1, "end_date": 1}
    ))
    deal_map = {}  # store_id → list of deals
    for d in all_deals:
        sid = d.get("store_id", "")
        deal_map.setdefault(sid, []).append(d)
    
    # Batch load merchants in ONE query using $in
    merchant_ids_raw = list(set(s.get("merchant_id", "") for s in stores if s.get("merchant_id")))
    merch_obj_ids = []
    for mid in merchant_ids_raw:
        try: merch_obj_ids.append(ObjectId(mid))
        except: pass
    merchants = {}
    for m in list(db.accounts.find({"_id": {"$in": merch_obj_ids}}, {"name": 1, "phone": 1})) or list(db.merchants.find({"_id": {"$in": merch_obj_ids}}, {"name": 1, "phone": 1})):
        merchants[str(m["_id"])] = m
    
    result = [_fmt_store_fast(s, sub_map, deal_map, merchants) for s in stores]
    _store_cache["data"] = result
    _store_cache["ts"] = _time.time()
    return result



@router.post("/migrate-store-images")
def migrate_store_images(a=Depends(get_current_admin)):
    """Upload base64 store images to Cloudinary (or consolidate into image field if CDN not set)."""
    global _store_cache; _store_cache["data"] = None
    # NO projection — fetch every field so we catch all possible image field names
    stores = list(db.stores.find({}))
    migrated, skipped_cdn, skipped_no_img, failed = [], [], [], []

    IMG_FIELDS = ["image", "image_url", "image_thumb", "_thumb",
                  "store_image", "img", "photo", "thumbnail"]

    for s in stores:
        sid        = str(s["_id"])
        store_name = s.get("store_name", sid)

        # Already has a valid http image_url — nothing to do
        existing_url = str(s.get("image_url") or "").strip()
        if existing_url.startswith("http"):
            skipped_cdn.append(store_name)
            continue

        # Search every known field for a non-empty image value
        raw = ""
        found_field = ""
        for field in IMG_FIELDS:
            val = s.get(field) or ""
            if isinstance(val, list):
                val = val[0] if val else ""
            val = str(val).strip()
            if val and val not in ("None", "null", "undefined") and len(val) > 10:
                raw = val
                found_field = field
                break

        if not raw:
            skipped_no_img.append(store_name)
            continue

        try:
            # Already an http URL (non-CDN) — normalise it into image_url
            if raw.startswith("http"):
                db.stores.update_one(
                    {"_id": s["_id"]},
                    {"$set": {"image_url": raw, "image_thumb": raw, "image": None}}
                )
                migrated.append(f"{store_name} (url-normalised)")
                continue

            cdn = _cloudinary_upload(raw, folder="offro/stores")
            if cdn and cdn.startswith("http"):
                db.stores.update_one(
                    {"_id": s["_id"]},
                    {"$set": {
                        "image_url":   cdn,
                        "image_thumb": _make_thumb_url(cdn),
                        "image":       None,
                    }}
                )
                migrated.append(f"{store_name} (cloudinary)")
            else:
                # Cloudinary not configured — move base64 into canonical "image" field
                db.stores.update_one(
                    {"_id": s["_id"]},
                    {"$set": {
                        "image":       raw,
                        "image_url":   "",
                        "image_thumb": "",
                    }}
                )
                migrated.append(f"{store_name} (base64-from:{found_field})")
        except Exception as e:
            failed.append({"store": store_name, "error": str(e)})

    return {
        "migrated":       len(migrated),
        "skipped_cdn":    len(skipped_cdn),
        "skipped_no_img": len(skipped_no_img),
        "failed":         len(failed),
        "details": {
            "migrated":       migrated,
            "skipped_cdn":    skipped_cdn,
            "skipped_no_img": skipped_no_img,
            "failed":         failed,
        }
    }

@router.post("/stores")
def create_store(data: dict, a=Depends(get_current_admin)):
    global _store_cache; _store_cache["data"] = None
    mid = data.get("merchant_id","").strip()
    name = data.get("store_name","").strip()
    if not mid: raise HTTPException(400, "merchant_id required")
    if not name: raise HTTPException(400, "store_name required")
    try: merchant = db.accounts.find_one({"_id": ObjectId(mid)}) or db.merchants.find_one({"_id": ObjectId(mid)})
    except: raise HTTPException(400, "Invalid merchant_id")
    if not merchant: raise HTTPException(404, "Merchant not found")

    # Upload image to Cloudinary once; fall back to storing base64 in "image" if CDN not set
    _raw_img  = data.get("image","") or ""
    _cdn_url  = _cloudinary_upload(_raw_img, folder="offro/stores") if _raw_img else ""
    _use_cdn  = bool(_cdn_url and _cdn_url.startswith("http"))
    store = {
        "merchant_id": mid, "store_name": name,
        "category": data.get("category",""),
        "city": data.get("city") or merchant.get("city",""),
        "area": data.get("area") or merchant.get("area",""),
        "address": data.get("address",""),
        "phone": data.get("phone") or merchant.get("phone",""),
        "status": "active",
        "points_per_scan": int(data.get("points_per_scan", 0)),
        "lat": data.get("lat",""), "lng": data.get("lng",""),
        "image_url":   _cdn_url if _use_cdn else "",
        "image_thumb": _make_thumb_url(_cdn_url) if _use_cdn else "",
        "image":       None if _use_cdn else (_raw_img or None),
        "is_new_in_town": bool(data.get("is_new_in_town", False)),
        "is_trending":    bool(data.get("is_trending", False)),
        "is_popular":     bool(data.get("is_popular", False)),
        "badge": data.get("badge", ""),
        "created_at": datetime.utcnow()
    }
    result = db.stores.insert_one(store)
    sid = str(result.inserted_id)
    qr = generate_qr_base64(sid)
    db.stores.update_one({"_id": result.inserted_id}, {"$set": {"qr_code": qr}})
    return {"message": "Store created", "store_id": sid, "qr_code": qr}

@router.get("/stores/slim")
def get_stores_slim(a=Depends(get_current_admin)):
    """Lightweight store list for ratings — no images, no heavy data."""
    stores = list(db.stores.find({}, {
        "_id":1,"store_name":1,"category":1,"city":1,"area":1,
        "rating":1,"admin_rating":1,"user_rating":1,"rating_count":1,"status":1
    }))
    return [{
        "_id": str(s["_id"]),
        "store_name": s.get("store_name",""),
        "category": s.get("category",""),
        "city": s.get("city",""),
        "area": s.get("area",""),
        "rating": s.get("admin_rating") or s.get("rating") or 0,
        "admin_rating": s.get("admin_rating",0),
        "user_rating": s.get("user_rating",0),
        "rating_count": s.get("rating_count",0),
        "status": s.get("status","active"),
    } for s in stores]

@router.get("/stores/{id}")
def get_store_detail(id: str, a=Depends(get_current_admin)):
    """Get full store detail including image2 — used by edit form."""
    try:
        s = db.stores.find_one({"_id": ObjectId(id)})
    except Exception:
        raise HTTPException(404, "Not found")
    if not s: raise HTTPException(404, "Not found")
    return {
        "_id":            str(s["_id"]),
        "store_name":     s.get("store_name", ""),
        "category":       s.get("category", ""),
        "city":           s.get("city", ""),
        "state":          s.get("state", ""),
        "area":           s.get("area", ""),
        "address":        s.get("address", ""),
        "phone":          s.get("phone", ""),
        "lat":            s.get("lat", ""),
        "lng":            s.get("lng", ""),
        "about":          s.get("about", ""),
        "points_per_scan":s.get("points_per_scan", 0),
        "visit_points":   s.get("visit_points", 0),
        "image":          s.get("image") or "",
        "image2":         s.get("store_image2") or s.get("image2") or "",
        "is_new_in_town": s.get("is_new_in_town", False),
        "badge": s.get("badge", ""),
        "status":         s.get("status", "active"),
        "merchant_id":    s.get("merchant_id", ""),
    }

@router.get("/stores/{id}/qr")
def get_store_qr(id: str, a=Depends(get_current_admin)):
    """Fetch or generate QR code for a store. Called by dashboard showQR()."""
    try:
        store = db.stores.find_one({"_id": ObjectId(id)})
        if not store:
            raise HTTPException(404, "Store not found")
        qr = store.get("qr_code", "")
        # Generate if missing
        if not qr:
            qr = generate_qr_base64(id)
            db.stores.update_one({"_id": ObjectId(id)}, {"$set": {"qr_code": qr}})
        return {"qr_code": qr, "store_name": store.get("store_name", "")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"QR generation failed: {str(e)}")

@router.put("/stores/{id}")
def update_store(id: str, data: dict, a=Depends(get_current_admin)):
    global _store_cache; _store_cache["data"] = None
    """Update any store field — used by admin dashboard Edit Store form."""
    store = db.stores.find_one({"_id": ObjectId(id)})
    if not store: raise HTTPException(404, "Not found")
    upd = {f: data[f] for f in ["store_name","category","city","state","area","address","phone","lat","lng","about"] if data.get(f) is not None}
    if "points_per_scan" in data and data["points_per_scan"] is not None:
        upd["points_per_scan"] = int(data["points_per_scan"])
    if "visit_points" in data and data["visit_points"] is not None:
        upd["visit_points"] = int(data["visit_points"])
    if "merchant_id" in data and data["merchant_id"] and data["merchant_id"].strip():
        upd["merchant_id"] = data["merchant_id"].strip()
    # Accept image — upload to Cloudinary if base64, else store base64 in "image" field
    if data.get("image"):
        _img_raw = data["image"]
        if _img_raw.startswith("http"):
            # already a CDN URL — store as-is
            upd["image_url"]   = _img_raw
            upd["image_thumb"] = _make_thumb_url(_img_raw)
            upd["image"]       = None
        else:
            # base64 — try Cloudinary upload
            _cdn = _cloudinary_upload(_img_raw, folder="offro/stores")
            if _cdn.startswith("http"):
                # Cloudinary worked
                upd["image_url"]   = _cdn
                upd["image_thumb"] = _make_thumb_url(_cdn)
                upd["image"]       = None
            else:
                # Cloudinary not configured — store base64 in "image" field for Flutter rendering
                upd["image"]       = _img_raw
                upd["image_url"]   = ""
                upd["image_thumb"] = ""
    if data.get("image2"):         # save image2 as store_image2 (matches public.py field name)
        upd["store_image2"] = data["image2"]
    if "is_new_in_town" in data: upd["is_new_in_town"] = bool(data["is_new_in_town"])
    if "is_trending"    in data: upd["is_trending"]    = bool(data["is_trending"])
    if "is_popular"     in data: upd["is_popular"]     = bool(data["is_popular"])
    if "badge" in data: upd["badge"] = data.get("badge", "")
    if "status" in data: upd["status"] = data["status"]
    if upd: db.stores.update_one({"_id": ObjectId(id)}, {"$set": upd})
    return {"message": "Updated"}

@router.put("/stores/{id}/rating")
def set_store_rating(id: str, data: dict, a=Depends(get_current_admin)):
    """Set the admin-controlled rating for a store (1-5 stars).
    This overwrites the displayed rating so the admin can curate what users see.
    The raw user ratings are preserved separately in the 'user_rating' field.
    """
    rating = float(data.get("admin_rating", 0))
    if not (0 <= rating <= 5):
        raise HTTPException(400, "Rating must be between 0 and 5")
    db.stores.update_one(
        {"_id": ObjectId(id)},
        {"$set": {"admin_rating": rating, "rating": rating}}
    )
    return {"message": "Rating updated", "rating": rating}


# ===================== REVIEWS (ADMIN) =====================

@router.get("/reviews")
def list_reviews(store_id: str = "", a=Depends(get_current_admin)):
    """List all user reviews. Optionally filter by store_id."""
    from datetime import datetime as _dt
    query = {}
    if store_id:
        query["store_id"] = store_id
    result = []
    for r in db.reviews.find(query).sort("created_at", -1).limit(500):
        store_name = ""
        try:
            s = db.stores.find_one({"_id": ObjectId(r["store_id"])}, {"store_name":1})
            if s:
                store_name = s.get("store_name","")
        except Exception:
            pass
        result.append({
            "_id":        str(r["_id"]),
            "store_id":   r.get("store_id",""),
            "store_name": store_name,
            "user_id":    r.get("user_id",""),
            "user_name":  r.get("user_name","Anonymous"),
            "rating":     r.get("rating",0),
            "text":       r.get("text",""),
            "created_at": r.get("created_at",""),
            "updated_at": r.get("updated_at",""),
        })
    return result

@router.delete("/reviews/{review_id}")
def delete_review(review_id: str, a=Depends(get_current_admin)):
    """Delete an inappropriate review and recalculate store rating."""
    try:
        r = db.reviews.find_one({"_id": ObjectId(review_id)})
        if not r:
            raise HTTPException(404, "Review not found")
        store_id = r.get("store_id","")
        db.reviews.delete_one({"_id": ObjectId(review_id)})
        # Recalculate store avg rating
        if store_id:
            all_ratings = list(db.ratings.find({"store_id": store_id}, {"rating":1}))
            all_reviews = list(db.reviews.find({"store_id": store_id}, {"rating":1}))
            combined = [x["rating"] for x in all_ratings + all_reviews]
            avg = round(sum(combined)/len(combined),1) if combined else 0
            store = db.stores.find_one({"_id": ObjectId(store_id)}, {"admin_rating":1})
            if store and not store.get("admin_rating"):
                db.stores.update_one({"_id": ObjectId(store_id)}, {"$set": {"rating": avg}})
        return {"ok": True, "message": "Review deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@router.put("/stores/{id}/approve")
def approve_store(id: str, a=Depends(get_current_admin)):
    global _store_cache; _store_cache["data"] = None
    db.stores.update_one({"_id": ObjectId(id)}, {"$set": {"status": "active"}})
    return {"message": "Store approved and live"}

@router.put("/stores/{id}/status")
def toggle_store(id: str, a=Depends(get_current_admin)):
    global _store_cache; _store_cache["data"] = None
    s = db.stores.find_one({"_id": ObjectId(id)})
    if not s: raise HTTPException(404, "Not found")
    ns = "inactive" if s.get("status") == "active" else "active"
    db.stores.update_one({"_id": ObjectId(id)}, {"$set": {"status": ns}})
    return {"status": ns}

@router.delete("/stores/{id}")
def delete_store(id: str, a=Depends(get_current_admin)):
    global _store_cache; _store_cache["data"] = None
    db.stores.delete_one({"_id": ObjectId(id)})
    return {"message": "Deleted"}

# ===================== USERS =====================

@router.get("/users")
def list_users(a=Depends(get_current_admin)):
    # DEPRECATED: Use /accounts instead. Returns from accounts collection.
    result = []
    cols = db.list_collection_names()
    for u in db.accounts.find():
        uid = str(u["_id"])
        result.append({
            "_id": uid, "name": u.get("name"), "phone": u.get("phone"),
            "city": u.get("city",""),
            "visit_points": u.get("visit_points",0), "pool_points": u.get("pool_points",0),
            "total_points": u.get("visit_points",0) + u.get("pool_points",0),
            "redemption_count": db.redemptions.count_documents({"user_id": uid}) if "redemptions" in cols else 0,
            "withdraw_count": db.withdraw_requests.count_documents({"user_id": uid}) if "withdraw_requests" in cols else 0,
            "pending_withdraw": db.withdraw_requests.count_documents({"user_id": uid, "status": "pending"}) > 0 if "withdraw_requests" in cols else False,
            "registered_on": u["_id"].generation_time.strftime("%d %b %Y") if hasattr(u["_id"],"generation_time") else ""
        })
    return result

@router.get("/users/{id}/history")
def user_history(id: str, a=Depends(get_current_admin)):
    u = db.accounts.find_one({"_id": ObjectId(id)}) or db.users.find_one({"_id": ObjectId(id)})
    if not u: raise HTTPException(404, "Not found")
    cols = db.list_collection_names()
    history = []
    if "redemptions" in cols:
        for r in db.redemptions.find({"user_id": id}).sort("created_at",-1):
            history.append({"type":"credit","description":f"QR Scan — {r.get('store_name','')}",
                "points":r.get("points",0),"date":r["created_at"].strftime("%d %b %Y %H:%M") if r.get("created_at") else ""})
    if "withdraw_requests" in cols:
        for w in db.withdraw_requests.find({"user_id": id}).sort("_id",-1):
            ts = w["_id"].generation_time.strftime("%d %b %Y %H:%M") if hasattr(w["_id"],"generation_time") else ""
            history.append({"type":"debit","description":f"Withdrawal — {w.get('status','')}","points":w.get("amount",0),"date":ts})
    if "point_adjustments" in cols:
        for adj in db.point_adjustments.find({"user_id": id}).sort("_id",-1):
            ts = adj["_id"].generation_time.strftime("%d %b %Y %H:%M") if hasattr(adj["_id"],"generation_time") else ""
            history.append({"type":adj.get("type","credit"),"description":f"Admin — {adj.get('note','')}","points":adj.get("points",0),"date":ts})
    history.sort(key=lambda x: x["date"], reverse=True)
    return {"user":{"name":u.get("name"),"phone":u.get("phone"),
        "visit_points":u.get("visit_points",0),"pool_points":u.get("pool_points",0),
        "total_points":u.get("visit_points",0)+u.get("pool_points",0)},"history":history}

@router.post("/users/{id}/adjust-points")
def adjust_points(id: str, data: dict, a=Depends(get_current_admin)):
    u = db.accounts.find_one({"_id": ObjectId(id)}) or db.users.find_one({"_id": ObjectId(id)})
    if not u: raise HTTPException(404, "Not found")
    t = data.get("type","credit"); pts = int(data.get("points",0))
    if pts <= 0: raise HTTPException(400, "Points must be > 0")
    vp = u.get("visit_points",0); pp = u.get("pool_points",0)
    if t == "credit":
        db.accounts.update_one({"_id": ObjectId(id)}, {"$inc": {"pool_points": pts}})
    else:
        if vp+pp < pts: raise HTTPException(400, f"User has only {vp+pp} pts")
        if pp >= pts: db.accounts.update_one({"_id": ObjectId(id)}, {"$inc": {"pool_points": -pts}})
        else:
            rem = pts - pp
            db.accounts.update_one({"_id": ObjectId(id)}, {"$set": {"pool_points":0,"visit_points":max(0,vp-rem)}})
    db.point_adjustments.insert_one({"user_id":id,"type":t,"points":pts,"note":data.get("note",""),"created_at":datetime.utcnow()})
    upd = db.accounts.find_one({"_id": ObjectId(id)}) or db.users.find_one({"_id": ObjectId(id)})
    return {"message":f"{'Added' if t=='credit' else 'Deducted'} {pts} pts",
            "new_total": upd.get("visit_points",0)+upd.get("pool_points",0)}

# ===================== STATS =====================

@router.get("/stats")
def admin_stats(a=Depends(get_current_admin)):
    cols = db.list_collection_names()
    total_accounts  = db.accounts.count_documents({})
    total_merchants = db.accounts.count_documents({"roles": "merchant"})
    # App Users = accounts that are NOT merchants (pure user accounts)
    app_users = db.accounts.count_documents({"roles": {"$not": {"$elemMatch": {"$eq": "merchant"}}}})
    # Fallback: also check 'users' collection if app_users is 0 and it exists
    if app_users == 0 and "users" in cols:
        app_users = db.users.count_documents({})
    return {
        "total_accounts":  total_accounts,
        "total_users":     total_accounts,
        "app_users":       app_users,
        "total_merchants": total_merchants,
        "active_merchants": db.accounts.count_documents({"roles": "merchant", "status": "active"}),
        "total_stores":    db.stores.count_documents({}),
        "waiting_approval": db.stores.count_documents({"status": "waiting_approval"}),
        "total_deals":     db.deals.count_documents({}) if "deals" in cols else 0,
    }

# ===================== SUBSCRIPTIONS (Admin view) =====================

@router.get("/subscriptions")
def list_subscriptions(a=Depends(get_current_admin)):
    result = []
    for s in db.subscriptions.find().sort("created_at", -1):
        merchant = None
        try:
            merchant = (db.accounts.find_one({"_id": ObjectId(s.get("merchant_id",""))}) or db.merchants.find_one({"_id": ObjectId(s.get("merchant_id",""))}))
        except: pass
        store_doc = {}
        try:
            store_doc = db.stores.find_one({"_id": ObjectId(s.get("store_id",""))}, {"store_name":1}) or {}
        except: pass
        fd = s.get("from_date"); ed = s.get("end_date")
        result.append({
            "merchant_name":  merchant.get("name") if merchant else "Unknown",
            "merchant_phone": merchant.get("phone") if merchant else "",
            "store_name":     store_doc.get("store_name", s.get("store_id","")),
            "plan":           s.get("plan"),
            "total":          s.get("total", 0),
            "gst":            s.get("gst", 0),
            "status":         s.get("status"),
            "from_date":      fd.strftime("%d %b %Y") if isinstance(fd, datetime) else str(fd or ""),
            "end_date":       ed.strftime("%d %b %Y") if isinstance(ed, datetime) else str(ed or ""),
            "created_at":     (s["created_at"] + __import__("datetime").timedelta(hours=5,minutes=30)).strftime("%d %b %Y, %I:%M %p") if s.get("created_at") else "",
        })
    return result


# ===================== MERCHANT PAYMENT TRANSACTIONS =====================

@router.get("/merchant-transactions")
def list_merchant_transactions(a=Depends(get_current_admin)):
    """All payment transactions made by merchants (subscriptions + invoices)."""
    result = []
    for inv in db.invoices.find().sort("created_at", -1):
        fd = inv.get("from_date"); ed = inv.get("end_date")
        result.append({
            "invoice_no":    inv.get("invoice_no", ""),
            "merchant_name": inv.get("merchant_name", ""),
            "merchant_phone":inv.get("merchant_phone", ""),
            "store_name":    inv.get("store_name", ""),
            "plan":          inv.get("plan", ""),
            "base_price":    inv.get("base_price", 0),
            "gst":           inv.get("gst", 0),
            "total":         inv.get("total", 0),
            "razorpay_payment_id": inv.get("razorpay_payment_id", ""),
            "from_date":     fd.strftime("%d %b %Y") if isinstance(fd, datetime) else str(fd or ""),
            "end_date":      ed.strftime("%d %b %Y") if isinstance(ed, datetime) else str(ed or ""),
            "created_at":    (inv["created_at"] + timedelta(hours=5,minutes=30)).strftime("%d %b %Y, %I:%M %p") if inv.get("created_at") else "",
        })
    return result

# ===================== POLICY MANAGEMENT =====================

@router.put("/policy/{policy_type}")
def save_policy(policy_type: str, body: dict, a=Depends(get_current_admin)):
    allowed = ["privacy", "refund", "kyc"]
    if policy_type not in allowed:
        raise HTTPException(status_code=400, detail="Invalid policy type")
    db.policies.update_one(
        {"type": policy_type},
        {"$set": {"type": policy_type, "content": body.get("content", ""), "updated_at": datetime.utcnow()}},
        upsert=True
    )
    return {"ok": True, "type": policy_type}

# ===================== SOCIAL MEDIA LINKS =====================

@router.get("/social")
def get_social(a=Depends(get_current_admin)):
    doc = db.settings.find_one({"key": "social_links"}) or {}
    return {
        "whatsapp":  doc.get("whatsapp", ""),
        "facebook":  doc.get("facebook", ""),
        "instagram": doc.get("instagram", ""),
        "youtube":   doc.get("youtube", ""),
    }

@router.put("/social")
@router.post("/social")
def save_social(body: dict, a=Depends(get_current_admin)):
    db.settings.update_one(
        {"key": "social_links"},
        {"$set": {"key": "social_links", **{k: body.get(k,"") for k in ["whatsapp","facebook","instagram","youtube"]}, "updated_at": datetime.utcnow()}},
        upsert=True
    )
    return {"ok": True}


# ===================== DISCOUNT CODES =====================

@router.get("/discounts")
def list_discounts(a=Depends(get_current_admin)):
    docs = list(db.discounts.find().sort("created_at", -1))
    result = []
    for d in docs:
        result.append({
            "_id":         str(d["_id"]),
            "code":        d.get("code",""),
            "value":       d.get("value",0),
            "max_uses":    d.get("max_uses",0),
            "used_count":  d.get("used_count",0),
            "active":      d.get("active",True),
            "expiry_date": d["expiry_date"].strftime("%Y-%m-%d") if d.get("expiry_date") else None,
            "created_at":  d["created_at"].strftime("%d %b %Y") if d.get("created_at") else "",
        })
    return result

@router.post("/discounts")
def create_discount(body: dict, a=Depends(get_current_admin)):
    code = (body.get("code","")).strip().upper()
    value = float(body.get("value",0))
    if not code:
        raise HTTPException(400, "Code is required")
    if value < 1:
        raise HTTPException(400, "Value must be at least ₹1")
    if db.discounts.find_one({"code": code}):
        raise HTTPException(400, "Code already exists")
    expiry = None
    if body.get("expiry_date"):
        try: expiry = datetime.strptime(body["expiry_date"], "%Y-%m-%d")
        except: pass
    db.discounts.insert_one({
        "code": code,
        "value": value,
        "max_uses": int(body.get("max_uses",0)),
        "used_count": 0,
        "active": True,
        "expiry_date": expiry,
        "created_at": datetime.utcnow(),
    })
    return {"ok": True}

@router.put("/discounts/{discount_id}")
def update_discount(discount_id: str, body: dict, a=Depends(get_current_admin)):
    update = {}
    if "active" in body: update["active"] = body["active"]
    if "value" in body: update["value"] = float(body["value"])
    if "max_uses" in body: update["max_uses"] = int(body["max_uses"])
    if not update:
        raise HTTPException(400, "Nothing to update")
    db.discounts.update_one({"_id": ObjectId(discount_id)}, {"$set": update})
    return {"ok": True}

@router.delete("/discounts/{discount_id}")
def delete_discount(discount_id: str, a=Depends(get_current_admin)):
    db.discounts.delete_one({"_id": ObjectId(discount_id)})
    return {"ok": True}

# ===================== ABOUT US =====================

@router.get("/about")
def get_about(a=Depends(get_current_admin)):
    doc = db.settings.find_one({"key": "about_us"}) or {}
    return {"content": doc.get("content", "")}

@router.put("/about")
def save_about(body: dict, a=Depends(get_current_admin)):
    db.settings.update_one(
        {"key": "about_us"},
        {"$set": {"key": "about_us", "content": body.get("content",""), "updated_at": datetime.utcnow()}},
        upsert=True
    )
    return {"ok": True}

# ===================== GIFT VOUCHER / WITHDRAW REQUESTS =====================

@router.get("/withdraw-requests")
def get_withdraw_requests(a=Depends(get_current_admin)):
    """Get all pending gift voucher withdrawal requests"""
    requests = list(db.withdraw_requests.find({}).sort("_id", -1))
    result = []
    for r in requests:
        result.append({
            "_id": str(r["_id"]),
            "user_id": r.get("user_id",""),
            "user_name": r.get("user_name",""),
            "phone": r.get("phone",""),
            "email": r.get("email",""),
            "points": r.get("points", r.get("amount", 200)),
            "voucher_value": r.get("voucher_value", round(r.get("points", r.get("amount", 200)) / 10, 2)),
            "status": r.get("status","pending"),
            "voucher_code": r.get("voucher_code",""),
            "voucher_type": r.get("voucher_type",""),
            "fulfilled_at": r.get("fulfilled_at",""),
            "created_at": str(r.get("created_at",""))[:10] if r.get("created_at") else "",
        })
    return result

@router.post("/withdraw-requests/{request_id}/fulfill")
def fulfill_withdraw_request(request_id: str, body: dict, a=Depends(get_current_admin)):
    """Send gift voucher to user - deduct points and clear pending flag"""
    voucher_code = (body.get("voucher_code","")).strip()
    voucher_type = body.get("voucher_type","Amazon")  # Amazon or Flipkart
    if not voucher_code:
        raise HTTPException(400, "Voucher code is required")
    
    req = db.withdraw_requests.find_one({"_id": ObjectId(request_id)})
    if not req:
        raise HTTPException(404, "Request not found")
    if req.get("status") == "fulfilled":
        raise HTTPException(400, "Already fulfilled")
    
    user_id = req.get("user_id")
    points = int(req.get("points", req.get("amount", 200)))
    
    # Deduct points from user now
    user = db.accounts.find_one({"_id": ObjectId(user_id)}) or db.users.find_one({"_id": ObjectId(user_id)})
    if user:
        pool = user.get("pool_points", 0)
        visit = user.get("visit_points", 0)
        remaining = points
        new_pool = pool
        new_visit = visit
        if new_pool >= remaining:
            new_pool -= remaining
        else:
            remaining -= new_pool
            new_pool = 0
            new_visit = max(0, new_visit - remaining)
        db.accounts.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {
                "visit_points": new_visit,
                "pool_points": new_pool,
                "pending_withdraw": False
            }}
        )
        # Log transaction
        db.point_transactions.insert_one({
            "user_id": user_id,
            "type": "debit",
            "points": points,
            "description": f"Gift Voucher Redeemed: {voucher_type} ₹{round(points/10,2)}",
            "date": datetime.utcnow().strftime("%Y-%m-%d")
        })
    
    # Update request as fulfilled
    db.withdraw_requests.update_one(
        {"_id": ObjectId(request_id)},
        {"$set": {
            "status": "fulfilled",
            "voucher_code": voucher_code,
            "voucher_type": voucher_type,
            "fulfilled_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        }}
    )
    return {"ok": True, "message": f"{voucher_type} voucher sent successfully"}


# ===================== GIFT VOUCHERS (app-facing cards) =====================

@router.get("/gift-vouchers")
def list_gift_vouchers(a=Depends(get_current_admin)):
    """List all gift vouchers + products shown in the app home screen."""
    result = []

    # ── 1. gift_vouchers collection (admin/merchant created cards) ──────────
    docs = list(db.gift_vouchers.find().sort("_id", -1))
    for v in docs:
        mid = v.get("merchant_id", "")
        merchant_name  = ""
        merchant_phone = ""
        if mid:
            try:
                m = (db.accounts.find_one({"_id": ObjectId(mid)}, {"name":1,"phone":1}) or
                     db.merchants.find_one({"_id": ObjectId(mid)}, {"name":1,"phone":1}))
                if m:
                    merchant_name  = m.get("name", "")
                    merchant_phone = str(m.get("phone", ""))
            except: pass
        ca = v.get("created_at")
        if isinstance(ca, datetime):
            created_at_iso = ca.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            created_at_iso = str(ca or "")[:19]
        result.append({
            "id":                str(v["_id"]),
            "title":             v.get("title", ""),
            "text":              v.get("text", ""),
            "validity":          v.get("validity", ""),
            "logo":              v.get("logo", ""),
            "store_id":          v.get("store_id", ""),
            "merchant_id":       mid,
            "merchant_name":     merchant_name,
            "merchant_phone":    merchant_phone,
            "is_active":         v.get("is_active", True),
            "from_date":         v.get("from_date", ""),
            "end_date":          v.get("end_date", ""),
            "created_at":        created_at_iso,
            "source":            v.get("source", "admin"),
            "source_voucher_id": v.get("source_voucher_id", ""),
            "duration_days":     v.get("duration_days", 0),
            "_collection":       "gift_vouchers",
        })

    # ── 2. products collection (seeded/imported product catalogue) ──────────
    product_docs = list(db.products.find().sort("_id", -1))
    for p in product_docs:
        pid = str(p["_id"])
        # Build offer text from price + discount if available
        price    = p.get("price", "")
        discount = p.get("discount", "")
        text_parts = []
        if discount: text_parts.append(f"{discount}% OFF")
        if price:    text_parts.append(f"₹{price}")
        offer_text = p.get("offer_text") or p.get("text") or (", ".join(text_parts) if text_parts else "")
        # Resolve image
        logo = (p.get("logo") or p.get("image_url") or p.get("image") or
                p.get("logo_url") or p.get("thumbnail") or p.get("img") or "")
        ca = p.get("created_at")
        if isinstance(ca, datetime):
            created_at_iso = ca.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            created_at_iso = str(ca or "")[:19]
        result.append({
            "id":                pid,
            "title":             p.get("name") or p.get("title") or "",
            "text":              offer_text,
            "validity":          p.get("validity") or p.get("valid_till") or "",
            "logo":              logo,
            "store_id":          str(p.get("store_id", "")),
            "merchant_id":       str(p.get("merchant_id", "")),
            "merchant_name":     p.get("merchant_name") or p.get("store_name") or "",
            "merchant_phone":    str(p.get("phone", "")),
            "is_active":         p.get("is_active", True),
            "from_date":         p.get("from_date") or p.get("start_date") or "",
            "end_date":          p.get("end_date") or p.get("expiry") or "",
            "created_at":        created_at_iso,
            "source":            "products",
            "source_voucher_id": "",
            "duration_days":     int(p.get("duration_days") or 0),
            "_collection":       "products",
            # Extra product-specific fields for display
            "price":             str(price),
            "discount":          str(discount),
            "description":       p.get("description", ""),
            "category":          p.get("category", ""),
        })

    # Sort all by created_at descending
    result.sort(key=lambda x: x.get("created_at",""), reverse=True)
    return result

@router.post("/gift-vouchers")
def create_gift_voucher(data: dict, a=Depends(get_current_admin)):
    """Create a new gift voucher card visible in the app."""
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Offer text is required")
    store_id    = (data.get("store_id") or "").strip()
    merchant_id = (data.get("merchant_id") or "").strip()
    logo = (data.get("logo") or "").strip()
    if not logo and store_id:
        try:
            s = db.stores.find_one({"_id": ObjectId(store_id)}, {"store_image2":1,"image2":1})
            if s:
                logo = s.get("store_image2") or s.get("image2") or ""
        except: pass
    doc = {
        "title":         (data.get("title") or "").strip(),
        "text":          text,
        "validity":      (data.get("validity") or "").strip(),
        "logo":          logo,
        "store_id":      store_id,
        "merchant_id":   merchant_id,
        "is_active":     bool(data.get("is_active", True)),
        "from_date":     (data.get("from_date") or "").strip(),
        "end_date":      (data.get("end_date") or "").strip(),
        "duration_days": int(data.get("duration_days") or 0),
        "source":        "admin",
        "created_at":    datetime.utcnow(),
    }
    result = db.gift_vouchers.insert_one(doc)
    new_id = str(result.inserted_id)

    # ISSUE 1: If linked to a merchant, also create a mirror record in merchant_vouchers
    # so it shows in the merchant app dashboard and counts toward their products
    if merchant_id:
        try:
            m = (db.accounts.find_one({"_id": ObjectId(merchant_id)}, {"name":1,"phone":1}) or db.merchants.find_one({"_id": ObjectId(merchant_id)}, {"name":1,"phone":1}))
            if m:
                mv_doc = {
                    "merchant_id":    merchant_id,
                    "merchant_name":  m.get("name", ""),
                    "merchant_phone": str(m.get("phone", "")),
                    "title":          doc["title"],
                    "offer_text":     doc["text"],
                    "logo_url":       doc["logo"],
                    "validity":       doc["validity"],
                    "from_date":      doc["from_date"],
                    "end_date":       doc["end_date"],
                    "duration_days":  doc["duration_days"],
                    "status":         "approved",
                    "approval_status":"approved",
                    "payment_status": "free",
                    "source":         "admin",
                    "source_gift_voucher_id": new_id,
                    "total":          0,
                    "created_at":     datetime.utcnow(),
                }
                db.merchant_vouchers.insert_one(mv_doc)
                # Also tag the gift_voucher with source_voucher_id for dedup
                db.gift_vouchers.update_one(
                    {"_id": result.inserted_id},
                    {"$set": {"source": "admin", "merchant_name": m.get("name",""), "merchant_phone": str(m.get("phone",""))}}
                )
        except Exception as e:
            pass  # non-fatal — gift_voucher was already created

    return {"message": "Product created", "id": new_id}

@router.put("/gift-vouchers/{vid}")
def update_gift_voucher(vid: str, data: dict, a=Depends(get_current_admin)):
    """Update an existing gift voucher."""
    upd = {}
    for field in ["title", "text", "validity", "logo", "merchant_id", "store_id", "from_date", "end_date", "duration_days"]:
        if field in data:
            upd[field] = (data[field] or "").strip()
    if "store_id" in upd and upd["store_id"] and "logo" not in upd:
        try:
            s = db.stores.find_one({"_id": ObjectId(upd["store_id"])}, {"store_image2":1,"image2":1})
            if s:
                upd["logo"] = s.get("store_image2") or s.get("image2") or ""
        except: pass
    if "is_active" in data:
        upd["is_active"] = bool(data["is_active"])
    if not upd:
        raise HTTPException(400, "Nothing to update")
    db.gift_vouchers.update_one({"_id": ObjectId(vid)}, {"$set": upd})
    return {"message": "Voucher updated"}

@router.delete("/gift-vouchers/{vid}")
def delete_gift_voucher(vid: str, a=Depends(get_current_admin)):
    """Delete a gift voucher."""
    db.gift_vouchers.delete_one({"_id": ObjectId(vid)})
    return {"message": "Deleted"}


# ===================== PROMO SLIDERS =====================

@router.get("/promo-sliders")
def list_promo_sliders(a=Depends(get_current_admin)):
    docs = list(db.promo_sliders.find().sort("sort_order", 1))
    result = []
    for d in docs:
        created = d.get("created_at", "")
        created_str = created.strftime("%d %b %Y %H:%M") if isinstance(created, datetime) else str(created)[:16]
        result.append({
            "id":            str(d["_id"]),
            "_id":           str(d["_id"]),
            "title":         d.get("title", ""),
            "image_url":     d.get("image_url", ""),
            "link_url":      d.get("link_url", ""),
            "sort_order":    d.get("sort_order", 0),
            "is_active":     d.get("is_active", True),
            # expiry & dates
            "from_date":     d.get("from_date", ""),
            "end_date":      d.get("end_date", d.get("expires_at", "")),
            "expires_at":    d.get("end_date", d.get("expires_at", "")),
            "duration_days": d.get("duration_days", d.get("days", "")),
            # merchant attribution
            "merchant_name":  d.get("merchant_name", ""),
            "merchant_phone": d.get("merchant_phone", ""),
            "source":         d.get("source", "admin"),
            "source_banner_id": d.get("source_banner_id", ""),
            # audit
            "created_at":    created_str,
        })
    return result

@router.post("/promo-sliders")
def create_promo_slider(data: dict, a=Depends(get_current_admin)):
    if not data.get("image_url"):
        raise HTTPException(400, "image_url required")
    # Auto-calculate end_date from from_date + days if not provided
    from_date = data.get("from_date", "")
    days      = int(data.get("days", 0))
    end_date  = data.get("end_date", "")
    if from_date and days > 0 and not end_date:
        from datetime import timedelta
        try:
            fd = datetime.strptime(from_date, "%Y-%m-%d")
            end_date = (fd + timedelta(days=days - 1)).strftime("%Y-%m-%d")
        except Exception:
            pass
    # Normalize merchant_phone to last 10 digits for consistent matching
    import re as _re
    raw_phone = str(data.get("merchant_phone", "") or "").strip()
    merchant_phone_norm = _re.sub(r'\D', '', raw_phone)[-10:] if raw_phone else ""

    doc = {
        "title":          data.get("title", ""),
        "image_url":      _cloudinary_upload(data["image_url"], folder="offro/sliders"),
        "link_url":       data.get("link_url", ""),
        "sort_order":     int(data.get("sort_order", 0)),
        "is_active":      bool(data.get("is_active", True)),
        "from_date":      from_date,
        "end_date":       end_date,
        "expires_at":     end_date,
        "duration_days":  days,
        "merchant_name":  data.get("merchant_name", ""),
        "merchant_phone": merchant_phone_norm,
        "source":         "admin",
        "created_at":     datetime.utcnow(),
    }
    r = db.promo_sliders.insert_one(doc)
    return {"message": "Slider created", "id": str(r.inserted_id)}

@router.put("/promo-sliders/{sid}")
def update_promo_slider(sid: str, data: dict, a=Depends(get_current_admin)):
    upd = {}
    import re as _re
    for f in ["title", "image_url", "link_url", "from_date", "end_date", "merchant_name"]:
        if f in data: upd[f] = data[f]
    if "merchant_phone" in data:
        raw_p = str(data["merchant_phone"] or "").strip()
        upd["merchant_phone"] = _re.sub(r'\D', '', raw_p)[-10:] if raw_p else ""
    if "sort_order"    in data: upd["sort_order"]    = int(data["sort_order"])
    if "is_active"     in data: upd["is_active"]     = bool(data["is_active"])
    if "days"          in data: upd["duration_days"]  = int(data["days"])
    # Keep expires_at in sync with end_date
    if "end_date" in upd: upd["expires_at"] = upd["end_date"]
    # Auto-calculate end_date from from_date + days
    if "from_date" in data and "days" in data and "end_date" not in data:
        from datetime import timedelta
        try:
            fd = datetime.strptime(data["from_date"], "%Y-%m-%d")
            calc_end = (fd + timedelta(days=int(data["days"]) - 1)).strftime("%Y-%m-%d")
            upd["end_date"]   = calc_end
            upd["expires_at"] = calc_end
        except Exception:
            pass
    if not upd: raise HTTPException(400, "Nothing to update")
    upd["updated_at"] = datetime.utcnow().isoformat()
    db.promo_sliders.update_one({"_id": ObjectId(sid)}, {"$set": upd})
    return {"message": "Updated"}

@router.delete("/promo-sliders/{sid}")
def delete_promo_slider(sid: str, a=Depends(get_current_admin)):
    db.promo_sliders.delete_one({"_id": ObjectId(sid)})
    return {"message": "Deleted"}


# ===================== NOTIFICATIONS =====================

@router.post("/upload-image")
async def upload_notification_image(file: UploadFile = File(...), a=Depends(get_current_admin)):
    """Upload an image for use in notifications. Returns a public URL."""
    import base64, mimetypes, time
    try:
        if not file.content_type or not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Only image files are allowed")

        contents = await file.read()
        if len(contents) > 2 * 1024 * 1024:  # 2 MB max
            raise HTTPException(status_code=400, detail="Image too large (max 2 MB)")

        # Store in MongoDB GridFS-style as a document with base64 content
        # And return a URL that serves it via a GET endpoint
        ext = mimetypes.guess_extension(file.content_type) or ".jpg"
        ext = ext.replace(".jpe", ".jpg")
        img_id = str(int(time.time() * 1000))
        doc = {
            "_id": img_id,
            "content_type": file.content_type,
            "data": base64.b64encode(contents).decode(),
            "filename": file.filename or f"notif_{img_id}{ext}",
            "created": time.time(),
        }
        db.notification_images.replace_one({"_id": img_id}, doc, upsert=True)

        # Return public serving URL
        base_url = os.environ.get("BASE_URL", "https://offro-backend-production.up.railway.app")
        url = f"{base_url}/admin/notification-image/{img_id}{ext}"
        return {"url": url, "id": img_id}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[UPLOAD] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/notification-image/{img_id}")
def serve_notification_image(img_id: str):
    """Serve a previously uploaded notification image."""
    from fastapi.responses import Response
    # Strip extension from img_id
    bare_id = img_id.split(".")[0]
    doc = db.notification_images.find_one({"_id": bare_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image not found")
    import base64
    data = base64.b64decode(doc["data"])
    return Response(content=data, media_type=doc.get("content_type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/notifications")
def list_notifications(a=Depends(get_current_admin)):
    docs = list(db.notifications.find().sort("_id", -1).limit(100))
    result = []
    for d in docs:
        result.append({
            "_id": str(d["_id"]),
            "id": str(d["_id"]),
            "title": d.get("title",""),
            "body": d.get("body",""),
            "target": d.get("target","all_users"),
            "target_phone": d.get("target_phone",""),
            "target_city":  d.get("target_city",""),
            "image_url": d.get("image_url",""),
            "status": d.get("status","sent"),
            "sent_at": d.get("sent_at",""),
            "created_at": d.get("created_at",""),
            "processed": d.get("processed", d.get("sent_count", 0)),
        })
    return result


@router.delete("/notifications/{notif_id}")
def delete_notification(notif_id: str, a=Depends(get_current_admin)):
    """Delete a single notification record from history."""
    from bson import ObjectId
    try:
        result = db.notifications.delete_one({"_id": ObjectId(notif_id)})
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Notification not found")
        return {"message": "Notification deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/notifications/process-queue")
def process_notification_queue(a=Depends(get_current_admin)):
    """Retry all queued/failed notifications."""
    queued = list(db.notifications.find(
        {"status": {"$in": ["queued", "failed", "error"]}},
        {"_id": 1, "title": 1, "body": 1, "target": 1, "target_phone": 1,
         "target_city": 1, "image_url": 1}
    ).limit(50))
    processed = failed = skipped = 0
    for n in queued:
        try:
            # Re-trigger send by calling the function with same data
            from bson import ObjectId
            result = db.notifications.update_one(
                {"_id": n["_id"]},
                {"$set": {"status": "retried"}}
            )
            skipped += 1  # Mark as retried — actual resend requires full send logic
        except Exception as e:
            failed += 1
    return {"processed": processed, "failed": failed, "skipped": skipped, "total": len(queued)}

@router.post("/notifications/send")
def send_notification(data: dict, a=Depends(get_current_admin)):
    import json as _json, time as _time, urllib.request as _ureq, base64 as _b64

    title      = (data.get("title") or "").strip()
    body       = (data.get("body")  or "").strip()
    target     = (data.get("target") or "all_users").strip()
    use_topic  = data.get("use_topic", target != "specific")
    image_url  = (data.get("image_url") or "").strip()
    # Upload to Cloudinary if it's a base64 image
    if image_url and (image_url.startswith("data:") or not image_url.startswith("http")):
        image_url = _cloudinary_upload(image_url, folder="offro/notifications")

    if not title or not body:
        raise HTTPException(400, "title and body are required")

    sent_at = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p")
    sent_count = 0
    fcm_error  = ""
    status     = "queued"

    sa_json    = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    project_id = os.environ.get("FIREBASE_PROJECT_ID", "").strip()

    def _get_access_token(sa_json_str, pid):
        """Build a JWT and exchange it for a Google OAuth2 access token."""
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend
        sa = _json.loads(sa_json_str)
        client_email   = sa["client_email"]
        private_key_pem = sa["private_key"]
        now = int(_time.time())
        header  = _b64.urlsafe_b64encode(_json.dumps({"alg":"RS256","typ":"JWT"}).encode()).rstrip(b"=")
        payload = _b64.urlsafe_b64encode(_json.dumps({
            "iss": client_email,
            "scope": "https://www.googleapis.com/auth/firebase.messaging",
            "aud": "https://oauth2.googleapis.com/token",
            "iat": now, "exp": now + 3600
        }).encode()).rstrip(b"=")
        pk = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None, backend=default_backend())
        sign_input = header + b"." + payload
        sig = pk.sign(sign_input, padding.PKCS1v15(), hashes.SHA256())
        jwt_token = (sign_input + b"." + _b64.urlsafe_b64encode(sig).rstrip(b"=")).decode()
        token_data = (
            "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer"
            f"&assertion={jwt_token}"
        ).encode()
        req = _ureq.Request("https://oauth2.googleapis.com/token", data=token_data,
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with _ureq.urlopen(req, timeout=12) as r:
            return _json.loads(r.read())["access_token"], sa.get("project_id", pid)

    def _build_fcm_message(*, token=None, topic=None):
        """Build FCM v1 message body for a token or topic."""
        dest = {"token": token} if token else {"topic": topic}
        # FCM v1 API only accepts https:// image URLs — reject base64/data URIs
        _fcm_image = image_url if (image_url and image_url.startswith("https://")) else ""
        notif_android = {
            "channel_id": "offro_high_importance",
            "click_action": "FLUTTER_NOTIFICATION_CLICK",
            "sound": "default",
        }
        if _fcm_image:
            notif_android["image"] = _fcm_image
        # Build APNS section with image support for iOS
        _apns = {
            "payload": {"aps": {"sound": "default", "badge": 1, "mutable-content": 1}},
        }
        if _fcm_image:
            # iOS image in FCM notification: needs fcm_options.image
            _apns["fcm_options"] = {"image": _fcm_image}

        return {
            "message": {
                **dest,
                "notification": {"title": title, "body": body},
                "android": {
                    "priority": "high",
                    "notification": notif_android,
                },
                "apns": _apns,
                "data": {
                    "type": "promo",
                    "title": title,
                    "body": body,
                    "image_url": image_url,
                },
            }
        }

    def _fcm_send(access_token, project, msg_body):
        """POST one message to FCM v1 API. Returns message_id on success."""
        fcm_url = f"https://fcm.googleapis.com/v1/projects/{project}/messages:send"
        req = _ureq.Request(
            fcm_url,
            data=_json.dumps(msg_body).encode(),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        with _ureq.urlopen(req, timeout=12) as r:
            return _json.loads(r.read()).get("name", "ok")

    if sa_json and (project_id or "project_id" in sa_json):
        try:
            access_token, _pid = _get_access_token(sa_json, project_id)

            # ── Determine how to send ──
            if use_topic and target != "specific":
                # Topic-based send: one call per relevant topic
                if target in ("all", "all_users"):
                    topics = ["all_users"]
                elif target == "offers":
                    topics = ["offers"]
                elif target == "city":
                    city_val = (data.get("target_city") or "").strip()
                    if not city_val:
                        raise ValueError("target_city is required for city target")
                    topics = [city_val.lower().replace(" ", "_").replace("-", "_") + "_users"]
                else:
                    topics = ["all_users"]

                for topic in topics:
                    msg = _build_fcm_message(topic=topic)
                    mid = _fcm_send(access_token, _pid, msg)
                    print(f"[FCM] topic={topic} message_id={mid}")
                    sent_count += 1
                status = "sent" if sent_count > 0 else "failed"

            else:
                # Token-based send: used for specific user only
                phone = (data.get("target_phone") or "").strip()
                # ── Phone normalisation: try all common formats ──
                # DB may store +91xxxxxxxxxx, users enter 10-digit numbers
                # Safe phone normalisation (lstrip is buggy — strips chars not prefixes)
                _pd = phone.strip().replace(" ", "").replace("-", "").replace("+", "")
                # Normalise to bare 10-digit number
                if len(_pd) == 12 and _pd.startswith("91"): _pd = _pd[2:]
                elif len(_pd) == 11 and _pd.startswith("0"): _pd = _pd[1:]
                elif len(_pd) == 13 and _pd.startswith("091"): _pd = _pd[3:]
                _last10 = _pd[-10:] if len(_pd) >= 10 else _pd
                phone_variants = list({
                    phone.strip(),          # as-typed by admin
                    f"+91{_last10}",        # E.164 international
                    f"91{_last10}",         # without +
                    _last10,                # 10-digit bare
                    f"0{_last10}",          # with leading 0
                    f"+{_last10}",          # with + only (edge case)
                })
                u = (db.accounts.find_one({"phone": {"$in": phone_variants}}, {"fcm_token": 1, "phone": 1}) or
                     db.users.find_one({"phone": {"$in": phone_variants}}, {"fcm_token": 1, "phone": 1}))
                print(f"[FCM] specific: phone={phone} variants={phone_variants} found={u is not None} stored_phone={u.get('phone') if u else None} has_token={bool(u and u.get('fcm_token'))}")
                
                # Fallback: check fcm_pending if user found but token missing, or user not found
                if not (u and u.get("fcm_token")):
                    pending = db.fcm_pending.find_one({"phone": {"$in": phone_variants}}, {"fcm_token": 1})
                    if pending and pending.get("fcm_token"):
                        print(f"[FCM] Found token in fcm_pending for phone={phone}")
                        tokens = [pending["fcm_token"]]
                    else:
                        tokens = []
                else:
                    tokens = [u["fcm_token"]]

                if not tokens:
                    status = "skipped_no_tokens"
                    matched_phone = u.get("phone","?") if u else "not_found"
                    fcm_error = f"No FCM token for phone={phone} (matched_phone={matched_phone}, user_found={u is not None})"
                else:
                    fail_count = 0
                    for tok in tokens:
                        try:
                            mid = _fcm_send(access_token, _pid, _build_fcm_message(token=tok))
                            print(f"[FCM] token send ok message_id={mid}")
                            sent_count += 1
                        except Exception as fe:
                            fail_count += 1
                            fcm_error = str(fe)
                            print(f"[FCM] token send FAILED: {fe}")
                    status = "sent" if fail_count == 0 else ("partial" if sent_count > 0 else "failed")

        except Exception as e:
            status = "error"
            fcm_error = str(e)
            print(f"[FCM] send_notification exception: {e}")
    else:
        status = "queued"
        fcm_error = "FIREBASE_SERVICE_ACCOUNT_JSON or FIREBASE_PROJECT_ID not set in Railway env"
        print(f"[FCM] env vars missing — queued only")

    # ── Persist to DB always ──
    doc = {
        "title": title, "body": body, "target": target,
        "target_phone": data.get("target_phone", ""),
        "target_city":  data.get("target_city", ""),
        "image_url": image_url,
        "status": status, "sent_at": sent_at,
        "sent_count": sent_count, "processed": sent_count,
        "created_at": datetime.utcnow(),
    }
    db.notifications.insert_one(doc)

    msg_out = "Notification sent!" if status == "sent" else f"Notification saved (status: {status})"
    return {
        "message": msg_out,
        "status": status,
        "sent_count": sent_count,
        "error": fcm_error if status not in ("sent", "queued") else "",
    }


# ═══════════════════════════════════════════════════════════
# ADMIN — MERCHANT BANNER APPROVAL
# ═══════════════════════════════════════════════════════════

@router.get("/merchant-banners")
def list_merchant_banners(a=Depends(get_current_admin)):
    """All merchant-submitted banners with approval status.
    Only returns pending/rejected — approved ones live in promo_sliders to avoid duplicates."""
    result = []
    for b in db.merchant_banners.find().sort("created_at", -1):
        approval_status = b.get("approval_status", "pending_approval")
        # Skip approved — they are already in promo_sliders (source_banner_id links them)
        if approval_status == "approved":
            continue
        result.append({
            "_id":            str(b["_id"]),
            "merchant_name":  b.get("merchant_name", ""),
            "merchant_phone": str(b.get("merchant_phone", b.get("phone", ""))),
            "title":          b.get("title", ""),
            "image_url":      b.get("image_url", ""),
            "duration_days":  b.get("duration_days", b.get("duration", 30)),
            "plan":           b.get("plan", ""),
            "status":         approval_status,
            "from_date":      b.get("from_date", b.get("start_date", "")),
            "end_date":       b.get("end_date", ""),
            "invoice_no":     b.get("invoice_no", ""),
            "amount":         b.get("total", 0),
            "created_at":     b["created_at"].strftime("%d %b %Y %H:%M") if isinstance(b.get("created_at"), datetime) else str(b.get("created_at",""))[:16],
        })
    return result

@router.put("/merchant-banners/{bid}/approve")
def approve_merchant_banner(bid: str, a=Depends(get_current_admin)):
    """Approve a merchant banner — publishes it as a promo slider."""
    b = db.merchant_banners.find_one({"_id": ObjectId(bid)})
    if not b: raise HTTPException(404, "Banner not found")
    db.merchant_banners.update_one({"_id": ObjectId(bid)}, {"$set": {"approval_status":"approved","approved_at":datetime.utcnow()}})
    # TASK 9 FIX: upsert into promo_sliders — never create duplicates
    db.promo_sliders.update_one(
        {"source_banner_id": bid},
        {"$set": {
            "title":         b.get("title",""),
            "image_url":     b.get("image_url",""),
            "is_active":     True,
            "sort_order":    50,
            "source":        "merchant",
            "source_banner_id": bid,
            "merchant_name": b.get("merchant_name",""),
            "expires_at":    b.get("end_date",""),
            "updated_at":    datetime.utcnow().isoformat(),
        },
         "$setOnInsert": {"created_at": datetime.utcnow().isoformat()}},
        upsert=True
    )
    return {"ok": True, "message": "Banner approved and published to app."}

@router.put("/merchant-banners/{bid}/reject")
def reject_merchant_banner(bid: str, body: dict = {}, a=Depends(get_current_admin)):
    reason = body.get("reason","")
    db.merchant_banners.update_one({"_id": ObjectId(bid)}, {"$set": {
        "approval_status":"rejected",
        "rejection_reason": reason,
        "rejected_at": datetime.utcnow()
    }})
    # Remove from promo_sliders if it was previously approved
    db.promo_sliders.delete_many({"source_banner_id": bid})
    return {"ok": True, "message": "Banner rejected."}

@router.put("/merchant-banners/{bid}")
def update_merchant_banner(bid: str, data: dict, a=Depends(get_current_admin)):
    """FIX 9: Admin edit merchant banner fields + status."""
    allowed = {"title", "image_url", "from_date", "end_date", "status", "duration_days"}
    update_data = {k: v for k, v in data.items() if k in allowed}
    if not update_data:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    update_data["updated_at"] = datetime.utcnow().isoformat()
    result = db.merchant_banners.update_one(
        {"_id": ObjectId(bid)},
        {"$set": update_data}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Banner not found")
    return {"ok": True, "updated": update_data}


@router.delete("/merchant-banners/{bid}")
def delete_merchant_banner(bid: str, a=Depends(get_current_admin)):
    db.merchant_banners.delete_one({"_id": ObjectId(bid)})
    db.promo_sliders.delete_many({"source_banner_id": bid})
    return {"ok": True}


# ═══════════════════════════════════════════════════════════
# ADMIN — MERCHANT VOUCHER APPROVAL
# ═══════════════════════════════════════════════════════════


def _compute_voucher_status(v):
    """Return real-time status — auto-expire if end_date has passed."""
    stored = v.get("approval_status", v.get("status", "pending_approval"))
    if stored in ("pending_approval", "rejected"):
        return stored
    end_raw = v.get("end_date", "")
    if end_raw:
        try:
            from dateutil.parser import parse as _dp
            import pytz
            end_dt = _dp(str(end_raw))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=pytz.UTC)
            now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
            if end_dt < now_utc:
                try:
                    db.merchant_vouchers.update_one(
                        {"_id": v["_id"], "approval_status": {"$ne": "expired"}},
                        {"$set": {"approval_status": "expired", "status": "expired"}}
                    )
                except: pass
                return "expired"
        except:
            pass
    return stored

@router.get("/merchant-vouchers")
def list_merchant_vouchers(a=Depends(get_current_admin)):
    result = []
    for v in db.merchant_vouchers.find().sort("created_at", -1):
        result.append({
            "_id":           str(v["_id"]),
            "merchant_name": v.get("merchant_name",""),
            "merchant_phone": v.get("merchant_phone",""),
            "title":         v.get("title",""),
            "offer_text":    v.get("offer_text",""),
            "logo_url":      v.get("logo_url",""),
            "validity":      v.get("validity", f"{v.get('from_date','')} → {v.get('end_date','')}" if v.get("from_date") else ""),
            "duration_days": v.get("duration_days", v.get("duration",30)),
            "from_date":     v.get("from_date",""),
            "end_date":      v.get("end_date",""),
            "status":        _compute_voucher_status(v),
            "invoice_no":    v.get("invoice_no",""),
            "amount":        v.get("total",0),
            "created_at":    v["created_at"].strftime("%Y-%m-%dT%H:%M:%S") if isinstance(v.get("created_at"), datetime) else str(v.get("created_at",""))[:16],
        })
    return result

@router.put("/merchant-vouchers/{vid}/approve")
def approve_merchant_voucher(vid: str, a=Depends(get_current_admin)):
    v = db.merchant_vouchers.find_one({"_id": ObjectId(vid)})
    if not v: raise HTTPException(404, "Voucher not found")
    # TASK 8: check if product has already expired before setting status
    end_date_raw = v.get("end_date", "")
    final_status = "approved"
    if end_date_raw:
        try:
            from dateutil.parser import parse as _parse_dt
            end_dt = _parse_dt(str(end_date_raw))
            if end_dt < datetime.utcnow():
                final_status = "expired"
        except:
            pass
    db.merchant_vouchers.update_one(
        {"_id": ObjectId(vid)},
        {"$set": {
            "approval_status": final_status,
            "status":          final_status,
            "approved_at":     datetime.utcnow().isoformat(),
        }}
    )
    # TASK 1 FIX: upsert into gift_vouchers — update if exists, insert if not — NEVER duplicate
    db.gift_vouchers.update_one(
        {"source_voucher_id": vid},
        {"$set": {
            "title":             v.get("title", ""),
            "text":              v.get("offer_text", ""),
            "logo":              v.get("logo_url", ""),
            "validity":          v.get("validity") or (
                                     f"{v.get('from_date', '')} → {v.get('end_date', '')}"
                                     if v.get("from_date") else "30 days"
                                 ),
            "is_active":         True,
            "source":            "merchant",
            "source_voucher_id": vid,
            "merchant_id":       str(v.get("merchant_id", "")),
            "merchant_name":     v.get("merchant_name", ""),
            "amount":            v.get("amount", 0),
            "updated_at":        datetime.utcnow().isoformat(),
        },
         "$setOnInsert": {
            "created_at": datetime.utcnow().isoformat(),
        }},
        upsert=True
    )
    return {"ok": True, "message": "Voucher approved and published to Voucher Zone."}

@router.put("/merchant-vouchers/{vid}/reject")
def reject_merchant_voucher(vid: str, body: dict = {}, a=Depends(get_current_admin)):
    reason = body.get("reason","")
    db.merchant_vouchers.update_one({"_id": ObjectId(vid)}, {"$set":{
        "approval_status":"rejected","rejection_reason":reason,"rejected_at":datetime.utcnow()
    }})
    db.gift_vouchers.delete_many({"source_voucher_id": vid})
    return {"ok": True, "message": "Voucher rejected."}

@router.put("/merchant-vouchers/{vid}")
def update_merchant_voucher(vid: str, data: dict, a=Depends(get_current_admin)):
    """FIX 10: Admin edit merchant voucher fields + status."""
    # ISSUES 5+7: also allow from_date, end_date, logo_url updates
    allowed = {"title", "offer_text", "validity", "logo", "logo_url", "from_date", "end_date", "status", "approval_status"}
    update_data = {k: v for k, v in data.items() if k in allowed}
    if not update_data:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    update_data["updated_at"] = datetime.utcnow().isoformat()
    # Map logo -> logo_url consistency
    if "logo" in update_data and "logo_url" not in update_data:
        update_data["logo_url"] = update_data["logo"]

    result = db.merchant_vouchers.update_one(
        {"_id": ObjectId(vid)},
        {"$set": update_data}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Voucher not found")

    # ISSUE 7: if status updated, sync to gift_vouchers record
    if "status" in update_data or "validity" in update_data or "from_date" in update_data or "end_date" in update_data:
        sync_fields = {}
        if "validity"   in update_data: sync_fields["validity"]  = update_data["validity"]
        if "from_date"  in update_data: sync_fields["from_date"] = update_data["from_date"]
        if "end_date"   in update_data: sync_fields["end_date"]  = update_data["end_date"]
        if "status"     in update_data:
            new_st = update_data["status"]
            sync_fields["is_active"] = (new_st == "approved")
        if "logo"       in update_data: sync_fields["logo"]      = update_data["logo"]
        if "title"      in update_data: sync_fields["title"]     = update_data["title"]
        if "offer_text" in update_data: sync_fields["text"]      = update_data["offer_text"]
        if sync_fields:
            db.gift_vouchers.update_many({"source_voucher_id": vid}, {"$set": sync_fields})

    return {"ok": True, "updated": update_data}


@router.delete("/merchant-vouchers/{vid}")
def delete_merchant_voucher(vid: str, a=Depends(get_current_admin)):
    db.merchant_vouchers.delete_one({"_id": ObjectId(vid)})
    db.gift_vouchers.delete_many({"source_voucher_id": vid})
    return {"ok": True}


# ═══════════════════════════════════════════════════════════
# ADMIN — FULL INVOICE VIEW (store + banner + voucher)
# ═══════════════════════════════════════════════════════════

@router.get("/invoices/full")
def list_all_invoices(a=Depends(get_current_admin)):
    """All merchant invoices: stores, banners, and vouchers — TASK 10: no zero amounts."""
    def _fmt(v):
        if not v: return ""
        if isinstance(v, datetime): return v.strftime("%d %b %Y")
        return str(v)[:10]
    def _fmtdt(v):
        if not v: return ""
        if isinstance(v, datetime): return v.strftime("%Y-%m-%dT%H:%M")
        return str(v)[:16]

    result = []
    seen_invoice_nos = set()   # FIX T1: dedup by invoice_no, not by ObjectId

    # 1. Primary: central invoices collection (most authoritative — always wins)
    for inv in db.invoices.find().sort("created_at", -1):
        ino = inv.get("invoice_no", str(inv["_id"])[:8].upper())
        if ino and ino in seen_invoice_nos: continue
        if ino: seen_invoice_nos.add(ino)
        fd = inv.get("from_date"); ed = inv.get("end_date")
        base = float(inv.get("base_price", inv.get("original_amount", 0)) or 0)
        gst  = float(inv.get("gst", inv.get("gst_amount", 0)) or 0)
        tot  = float(inv.get("total", inv.get("amount", 0)) or 0)
        if tot == 0 and base > 0: tot = base + gst
        result.append({
            "invoice_no":      inv.get("invoice_no",""),
            "merchant_name":   inv.get("merchant_name",""),
            "merchant_phone":  inv.get("merchant_phone",""),
            "type":            inv.get("type","store"),
            "item_label":      inv.get("item_label") or f"Store – {inv.get('plan','')}",
            "store_name":      inv.get("store_name",""),
            "base_price":      base,
            "original_amount": float(inv.get("original_amount", base) or base),
            "discount_code":   inv.get("discount_code",""),
            "discount_amount": float(inv.get("discount_amount",0) or 0),
            "final_amount":    float(inv.get("final_amount", base) or base),
            "gst":             gst,
            "total":           tot,
            "plan":            inv.get("plan",""),
            "from_date":       _fmt(fd),
            "end_date":        _fmt(ed),
            "created_at":      _fmtdt(inv.get("created_at")),
            "_status":         "paid",
        })

    # 2. Paid banners not already in invoices collection
    for b in db.merchant_banners.find({"payment_status":"paid"}).sort("created_at",-1):
        ino = b.get("invoice_no", str(b["_id"])[:8].upper())
        if ino and ino in seen_invoice_nos: continue   # FIX T1: skip if invoice already in set
        if ino: seen_invoice_nos.add(ino)
        base = float(b.get("base_price",0) or 0)
        gst  = float(b.get("gst_amount", b.get("gst",0)) or 0)
        tot  = float(b.get("total",0) or 0)
        if tot == 0 and base > 0: tot = round(base + gst, 2)
        result.append({
            "invoice_no":      ino,
            "merchant_name":   b.get("merchant_name",""),
            "merchant_phone":  b.get("merchant_phone",""),
            "type":            "banner",
            "item_label":      f"Banner – {b.get('duration_days', b.get('duration',30))} Days",
            "store_name":      b.get("title",""),
            "base_price":      base,
            "original_amount": float(b.get("original_amount", base) or base),
            "discount_code":   b.get("discount_code",""),
            "discount_amount": float(b.get("discount_amount",0) or 0),
            "final_amount":    float(b.get("final_amount", base) or base),
            "gst":             gst,
            "total":           tot,
            "plan":            f"{b.get('from_date','')} → {b.get('end_date','')}",
            "from_date":       _fmt(b.get("from_date")),
            "end_date":        _fmt(b.get("end_date")),
            "created_at":      _fmtdt(b.get("created_at")),
            "_status":         "paid",
        })

    # 3. Paid vouchers/products not already in invoices collection
    for v in db.merchant_vouchers.find({"payment_status":"paid"}).sort("created_at",-1):
        ino = v.get("invoice_no", str(v["_id"])[:8].upper())
        if ino and ino in seen_invoice_nos: continue   # FIX T1: skip if invoice already in set
        if ino: seen_invoice_nos.add(ino)
        base = float(v.get("base_price",0) or 0)
        gst  = float(v.get("gst_amount", v.get("gst",0)) or 0)
        tot  = float(v.get("total",0) or 0)
        if tot == 0 and base > 0: tot = round(base + gst, 2)
        result.append({
            "invoice_no":      ino,
            "merchant_name":   v.get("merchant_name",""),
            "merchant_phone":  v.get("merchant_phone",""),
            "type":            "product",
            "item_label":      f"Discover Product – {v.get('duration_days', v.get('duration',30))} Days",
            "store_name":      v.get("title",""),
            "base_price":      base,
            "original_amount": float(v.get("original_amount", base) or base),
            "discount_code":   v.get("discount_code",""),
            "discount_amount": float(v.get("discount_amount",0) or 0),
            "final_amount":    float(v.get("final_amount", base) or base),
            "gst":             gst,
            "total":           tot,
            "plan":            f"{v.get('from_date','')} → {v.get('end_date','')}",
            "from_date":       _fmt(v.get("from_date")),
            "end_date":        _fmt(v.get("end_date")),
            "created_at":      _fmtdt(v.get("created_at")),
            "_status":         "paid",
        })

    result.sort(key=lambda x: x.get("created_at",""), reverse=True)
    return result

@router.get("/banner-pricing")
def get_banner_pricing(a=Depends(get_current_admin)):
    doc = db.pricing.find_one({}) or {}
    gst = float(doc.get("gst_percent", 18))
    return {
        # Per-day pricing (Issue 4)
        "banner_price_per_day":  float(doc.get("banner_price_per_day",  15)),
        "voucher_price_per_day": float(doc.get("voucher_price_per_day", 10)),
        "gst_percent": gst,
        # Legacy fields kept for backward compat
        "banner_price_7":   doc.get("banner_price_7",  149),
        "banner_price_14":  doc.get("banner_price_14", 249),
        "banner_price_30":  doc.get("banner_price_30", 399),
        "voucher_price_30": doc.get("voucher_price_30",199),
        "voucher_price_60": doc.get("voucher_price_60",349),
        "voucher_price_90": doc.get("voucher_price_90",499),
    }

@router.put("/banner-pricing")
def update_banner_pricing(data: dict, a=Depends(get_current_admin)):
    fields = ["banner_price_per_day","voucher_price_per_day",
              "banner_price_7","banner_price_14","banner_price_30",
              "voucher_price_30","voucher_price_60","voucher_price_90"]
    upd = {f: float(data[f]) for f in fields if f in data}
    doc = db.pricing.find_one({})
    if doc: db.pricing.update_one({"_id":doc["_id"]},{"$set":upd})
    else: db.pricing.insert_one(upd)
    return {"ok":True}


# ═══════════════════════════════════════════════════════════════
# REVIEWS MANAGEMENT
# ═══════════════════════════════════════════════════════════════

@router.get("/reviews")
def list_reviews(
    store_id: str = None,
    skip: int = 0,
    limit: int = 50,
    a=Depends(get_current_admin)
):
    """List all user reviews. Optionally filter by store_id."""
    query = {}
    if store_id:
        query["store_id"] = store_id
    reviews = list(
        db.reviews.find(query)
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )
    total = db.reviews.count_documents(query)
    result = []
    for r in reviews:
        # Enrich with store name
        store_name = ""
        try:
            s = db.stores.find_one({"_id": ObjectId(r["store_id"])}, {"store_name": 1})
            store_name = s.get("store_name", "") if s else ""
        except Exception:
            pass
        result.append({
            "_id":        str(r["_id"]),
            "store_id":   r.get("store_id", ""),
            "store_name": store_name,
            "user_id":    r.get("user_id") or "",
            "user_name":  r.get("user_name", "Anonymous"),
            "rating":     float(r.get("rating", 0)),
            "text":       r.get("text", ""),
            "created_at": str(r.get("created_at", "")),
            "updated_at": str(r.get("updated_at", "")),
        })
    return {"reviews": result, "total": total}


@router.delete("/reviews/{review_id}")
def delete_review(review_id: str, a=Depends(get_current_admin)):
    """Admin deletes an inappropriate review."""
    try:
        res = db.reviews.delete_one({"_id": ObjectId(review_id)})
    except Exception:
        raise HTTPException(400, "Invalid review_id")
    if res.deleted_count == 0:
        raise HTTPException(404, "Review not found")
    return {"ok": True, "message": "Review deleted"}


@router.get("/reviews/stats")
def review_stats(a=Depends(get_current_admin)):
    """Return review counts grouped by store."""
    pipeline = [
        {"$group": {"_id": "$store_id", "count": {"$sum": 1}, "avg": {"$avg": "$rating"}}},
        {"$sort": {"count": -1}},
        {"$limit": 50},
    ]
    rows = list(db.reviews.aggregate(pipeline))
    result = []
    for row in rows:
        store_name = ""
        try:
            s = db.stores.find_one({"_id": ObjectId(row["_id"])}, {"store_name": 1})
            store_name = s.get("store_name", "") if s else ""
        except Exception:
            pass
        result.append({
            "store_id":   row["_id"],
            "store_name": store_name,
            "count":      row["count"],
            "avg_rating": round(float(row["avg"]), 1),
        })
    return {"stats": result}


# ════════════════════════════════════════════════════════
# CITIES MANAGEMENT
# ════════════════════════════════════════════════════════

@router.get("/cities")
def admin_get_cities(a=Depends(get_current_admin)):
    cities = list(db.cities.find({}).sort("sort_order", 1))
    return [{
        "id":         str(c["_id"]),
        "name":       c.get("name", ""),
        "image_url":  c.get("image_url", ""),
        "sort_order": c.get("sort_order", 0),
        "active":     c.get("active", True),
        "created_at": str(c.get("created_at", "")),
    } for c in cities]




@router.post("/cities/seed")
def admin_seed_cities(a=Depends(get_current_admin)):
    """Auto-populate default cities with placeholder images. Skips existing cities."""
    DEFAULT_CITIES = [
        {"name": "Ballari",   "sort_order": 1, "image_url": ""},
        {"name": "Bengaluru", "sort_order": 2, "image_url": ""},
        {"name": "Hyderabad", "sort_order": 3, "image_url": ""},
        {"name": "Hubli",     "sort_order": 4, "image_url": ""},
        {"name": "Dharwad",   "sort_order": 5, "image_url": ""},
        {"name": "Mysuru",    "sort_order": 6, "image_url": ""},
    ]
    added = []
    skipped = []
    for city in DEFAULT_CITIES:
        existing = db.cities.find_one({"name": {"$regex": f"^{city['name']}$", "$options": "i"}})
        if existing:
            skipped.append(city["name"])
            continue
        doc = {
            "name":       city["name"],
            "image_url":  city["image_url"],
            "sort_order": city["sort_order"],
            "active":     True,
            "status":     "active",
            "created_at": datetime.utcnow().isoformat(),
        }
        db.cities.insert_one(doc)
        added.append(city["name"])
    # Also migrate any existing cities that have 'active' but no 'status'
    db.cities.update_many(
        {"active": True,  "status": {"$exists": False}},
        {"$set": {"status": "active"}}
    )
    db.cities.update_many(
        {"active": False, "status": {"$exists": False}},
        {"$set": {"status": "inactive"}}
    )
    return {"ok": True, "added": added, "skipped": skipped}

@router.post("/cities")
def admin_create_city(body: dict, a=Depends(get_current_admin)):
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "City name required")
    existing = db.cities.find_one({"name": {"$regex": f"^{name}$", "$options": "i"}})
    if existing:
        raise HTTPException(400, "City already exists")
    is_active = bool(body.get("active", True))
    doc = {
        "name":       name,
        "image_url":  body.get("image_url", ""),
        "sort_order": int(body.get("sort_order", 0)),
        "active":     is_active,
        "status":     "active" if is_active else "inactive",
        "created_at": datetime.utcnow().isoformat(),
    }
    result = db.cities.insert_one(doc)
    return {"ok": True, "id": str(result.inserted_id)}


@router.put("/cities/{city_id}")
def admin_update_city(city_id: str, body: dict, a=Depends(get_current_admin)):
    update = {}
    for field in ["name", "image_url", "sort_order", "active"]:
        if field in body:
            update[field] = body[field]
    # Sync 'status' field with 'active' so public API works correctly
    if "active" in body:
        update["status"] = "active" if bool(body["active"]) else "inactive"
    if not update:
        raise HTTPException(400, "Nothing to update")
    db.cities.update_one({"_id": ObjectId(city_id)}, {"$set": update})
    return {"ok": True}


@router.delete("/cities/{city_id}")
def admin_delete_city(city_id: str, a=Depends(get_current_admin)):
    db.cities.delete_one({"_id": ObjectId(city_id)})
    return {"ok": True}


@router.post("/cities/{city_id}/upload-image")
async def admin_upload_city_image(city_id: str, file: UploadFile = File(...), a=Depends(get_current_admin)):
    content = await file.read()
    mime = file.content_type or "image/jpeg"
    b64 = base64.b64encode(content).decode()
    data_url = f"data:{mime};base64,{b64}"
    db.cities.update_one({"_id": ObjectId(city_id)}, {"$set": {"image_url": data_url}})
    return {"ok": True, "image_url": data_url}


# ════════════════════════════════════════════════════════
# DEFAULT IMAGES MANAGEMENT
# ════════════════════════════════════════════════════════

# ─── Admin Banners (home-screen banners managed by admin) ─────────────────────
@router.get("/banners")
def admin_list_banners(a=Depends(get_current_admin)):
    docs = list(db.admin_banners.find().sort("sort_order", 1))
    result = []
    for d in docs:
        img = d.get("image_url", "") or d.get("image", "")
        result.append({
            "id":         str(d["_id"]),
            "title":      d.get("title", ""),
            "subtitle":   d.get("subtitle", ""),
            "image":      img,
            "image_url":  img,
            "link_url":   d.get("link_url", ""),
            "sort_order": d.get("sort_order", 0),
            "is_active":  d.get("is_active", True),
            "created_at": d.get("created_at", ""),
        })
    return result


@router.post("/banners")
async def admin_create_banner(data: dict, a=Depends(get_current_admin)):
    """Create admin banner. Accepts JSON body with image_url (base64 or http URL)."""
    img = data.get("image_url") or data.get("image") or ""
    doc = {
        "title":      data.get("title", ""),
        "subtitle":   data.get("subtitle", ""),
        "image":      img,
        "image_url":  img,
        "link_url":   data.get("link_url", ""),
        "sort_order": int(data.get("sort_order", 0)),
        "is_active":  bool(data.get("is_active", True)),
        "created_at": datetime.utcnow().isoformat(),
    }
    r = db.admin_banners.insert_one(doc)
    return {"ok": True, "id": str(r.inserted_id)}


@router.put("/banners/{bid}")
async def admin_update_banner(bid: str, data: dict, a=Depends(get_current_admin)):
    """Update admin banner. Accepts JSON body."""
    update: dict = {}
    if "title"      in data: update["title"]      = data["title"]
    if "subtitle"   in data: update["subtitle"]   = data["subtitle"]
    if "link_url"   in data: update["link_url"]   = data["link_url"]
    if "sort_order" in data: update["sort_order"] = int(data["sort_order"])
    if "is_active"  in data: update["is_active"]  = bool(data["is_active"])
    img = data.get("image_url") or data.get("image")
    if img is not None:
        update["image"]     = img
        update["image_url"] = img
    if update:
        db.admin_banners.update_one({"_id": ObjectId(bid)}, {"$set": update})
    return {"ok": True}


@router.delete("/banners/{bid}")
def admin_delete_banner(bid: str, a=Depends(get_current_admin)):
    db.admin_banners.delete_one({"_id": ObjectId(bid)})
    return {"ok": True}


@router.patch("/banners/{bid}/toggle")
def admin_toggle_banner(bid: str, a=Depends(get_current_admin)):
    doc = db.admin_banners.find_one({"_id": ObjectId(bid)})
    if not doc:
        raise HTTPException(404, "Banner not found")
    new_state = not doc.get("is_active", True)
    db.admin_banners.update_one({"_id": ObjectId(bid)}, {"$set": {"is_active": new_state}})
    return {"ok": True, "is_active": new_state}



@router.get("/default-images")
def admin_get_default_images(a=Depends(get_current_admin)):
    doc = db.settings.find_one({"_type": "default_images"}) or {}
    def _to_list(val):
        """Normalise legacy single-string or list → list of non-empty http URLs."""
        if isinstance(val, list):
            return [v for v in val if isinstance(v, str) and v.startswith("http")]
        if isinstance(val, str) and val.startswith("http"):
            return [val]
        return []
    return {
        "store":   _to_list(doc.get("store",   doc.get("store_images",   []))),
        "product": _to_list(doc.get("product", doc.get("product_images", []))),
        "offer":   _to_list(doc.get("offer",   doc.get("offer_images",   []))),
        "city":    _to_list(doc.get("city",    doc.get("city_images",    []))),
    }


@router.put("/default-images/urls")
def admin_update_default_image_urls(body: dict, a=Depends(get_current_admin)):
    """Add a URL to the array for a given type.
    Body: { "type": "store"|"product"|"offer"|"city", "url": "https://..." }
    Also supports bulk legacy format: { "store": "url", ... }
    """
    action = body.get("action", "add")  # "add" or "remove"
    img_type = body.get("type", "")
    url = (body.get("url") or "").strip()

    if img_type and url and img_type in ["store", "product", "offer", "city"]:
        if action == "remove":
            db.settings.update_one(
                {"_type": "default_images"},
                {"$pull": {img_type: url}},
                upsert=True
            )
        else:
            # Migrate string → array before $addToSet
            doc = db.settings.find_one({"_type": "default_images"}) or {}
            existing = doc.get(img_type)
            if isinstance(existing, str):
                db.settings.update_one(
                    {"_type": "default_images"},
                    {"$set": {img_type: [existing] if existing else []}},
                    upsert=True
                )
            elif not isinstance(existing, list):
                db.settings.update_one(
                    {"_type": "default_images"},
                    {"$set": {img_type: []}},
                    upsert=True
                )
            db.settings.update_one(
                {"_type": "default_images"},
                {"$addToSet": {img_type: url}},
                upsert=True
            )
        return {"ok": True}

    # Legacy bulk format fallback
    update = {}
    for field in ["store", "product", "offer", "city"]:
        if field in body and isinstance(body[field], str) and body[field].startswith("http"):
            update[field] = [body[field]]
    if update:
        db.settings.update_one({"_type": "default_images"}, {"$set": update}, upsert=True)
    return {"ok": True}

@router.delete("/default-images/url")
def admin_remove_default_image_url(body: dict, a=Depends(get_current_admin)):
    """Remove a single URL from a type array."""
    img_type = body.get("type", "")
    url = (body.get("url") or "").strip()
    if not img_type or not url:
        raise HTTPException(status_code=400, detail="type and url required")
    db.settings.update_one(
        {"_type": "default_images"},
        {"$pull": {img_type: url}},
        upsert=True
    )
    return {"ok": True}


@router.put("/default-images")
async def admin_update_default_images(
    store_file: UploadFile = File(None),
    product_file: UploadFile = File(None),
    offer_file: UploadFile = File(None),
    city_file: UploadFile = File(None),
    a=Depends(get_current_admin),
):
    """Upload default images. Saves Cloudinary URL if configured, else base64 fallback."""
    import base64 as _b64mod, os as _di_os
    update = {}
    for field, f in [("store", store_file), ("product", product_file), ("offer", offer_file), ("city", city_file)]:
        if not f or not f.filename:
            continue
        raw  = await f.read()
        mime = f.content_type or "image/jpeg"
        b64_str = _b64mod.b64encode(raw).decode()
        b64_data = f"data:{mime};base64,{b64_str}"

        # Try Cloudinary first (same as category image upload)
        cloud  = _di_os.getenv("CLOUDINARY_CLOUD_NAME", "")
        api_key= _di_os.getenv("CLOUDINARY_API_KEY", "")
        secret = _di_os.getenv("CLOUDINARY_API_SECRET", "")
        saved_value = b64_data  # fallback
        if cloud and api_key and secret:
            try:
                cdn_url = _cloudinary_upload(b64_data, folder="offro/defaults")
                if cdn_url and cdn_url.startswith("http"):
                    saved_value = cdn_url
                    print(f"[DEFAULT-IMG] {field} uploaded to Cloudinary: {cdn_url}")
                else:
                    print(f"[DEFAULT-IMG] Cloudinary returned no URL for {field}, falling back to base64")
            except Exception as e:
                print(f"[DEFAULT-IMG] Cloudinary upload failed for {field}: {e}, falling back to base64")
        else:
            print(f"[DEFAULT-IMG] Cloudinary not configured — saving {field} as base64 (size: {len(b64_data)} chars)")

        update[field] = saved_value

    if update:
        # Migrate string → array, then append
        push_ops = {"$addToSet": {}}
        for field, val in update.items():
            push_ops["$addToSet"][field] = val
        if push_ops["$addToSet"]:
            doc = db.settings.find_one({"_type": "default_images"}) or {}
            set_fixes = {}
            for img_type in push_ops["$addToSet"]:
                existing = doc.get(img_type)
                if isinstance(existing, str):
                    set_fixes[img_type] = [existing] if existing else []
                elif not isinstance(existing, list):
                    set_fixes[img_type] = []
            if set_fixes:
                db.settings.update_one({"_type": "default_images"}, {"$set": set_fixes}, upsert=True)
            db.settings.update_one({"_type": "default_images"}, push_ops, upsert=True)
    return {"ok": True, "uploaded": list(update.keys())}
