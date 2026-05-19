import os
from fastapi import UploadFile, File, APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from database import db
from bson import ObjectId
from datetime import datetime, timedelta
import uuid, qrcode, io, base64
import time as _time

# In-memory cache for /stores list (15-second TTL)
_store_cache = {"data": None, "ts": 0.0}
_STORE_CACHE_TTL = 15

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
    if not db.categories.find_one({}):
        db.categories.insert_one({"categories": ["Grocery","Restaurant","Pharmacy","Electronics","Clothing","Bakery","Salon","Other"]})
    if not db.pricing.find_one({}):
        db.pricing.insert_one({"gst_percent": 18, "plans": [
            {"id": "1month",  "label": "1 Month",   "price": 499},
            {"id": "3months", "label": "3 Months",  "price": 1299},
            {"id": "6months", "label": "6 Months",  "price": 2299},
            {"id": "12months","label": "12 Months", "price": 3999},
        ]})

# ===================== AUTH =====================

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
    doc = db.categories.find_one({})
    return doc.get("categories", []) if doc else []

@router.post("/categories")
def add_category(data: dict, a=Depends(get_current_admin)):
    name = data.get("name", "").strip()
    if not name: raise HTTPException(400, "Name required")
    doc = db.categories.find_one({})
    cats = doc.get("categories", []) if doc else []
    if name not in cats: cats.append(name)
    if doc: db.categories.update_one({"_id": doc["_id"]}, {"$set": {"categories": cats}})
    else: db.categories.insert_one({"categories": cats})
    return {"categories": cats}

@router.delete("/categories/{name}")
def delete_category(name: str, a=Depends(get_current_admin)):
    doc = db.categories.find_one({})
    if doc:
        cats = [c for c in doc.get("categories", []) if c != name]
        db.categories.update_one({"_id": doc["_id"]}, {"$set": {"categories": cats}})
        return {"categories": cats}
    return {"categories": []}

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
    return [{"_id": str(m["_id"]), "name": m.get("name"), "phone": m.get("phone"),
             "city": m.get("city"), "area": m.get("area"), "status": m.get("status", "active"),
             "store_count": db.stores.count_documents({"merchant_id": str(m["_id"])})}
            for m in db.merchants.find()]

@router.put("/merchants/{id}")
def update_merchant(id: str, data: dict, a=Depends(get_current_admin)):
    upd = {f: data[f] for f in ["name","phone","city","area"] if data.get(f) is not None}
    if upd: db.merchants.update_one({"_id": ObjectId(id)}, {"$set": upd})
    return {"message": "Updated"}

@router.put("/merchants/{id}/status")
def toggle_merchant(id: str, a=Depends(get_current_admin)):
    m = db.merchants.find_one({"_id": ObjectId(id)})
    if not m: raise HTTPException(404, "Not found")
    ns = "inactive" if m.get("status") == "active" else "active"
    db.merchants.update_one({"_id": ObjectId(id)}, {"$set": {"status": ns}})
    return {"status": ns}

@router.delete("/merchants/{id}")
def delete_merchant(id: str, a=Depends(get_current_admin)):
    db.merchants.delete_one({"_id": ObjectId(id)})
    db.stores.delete_many({"merchant_id": id})
    return {"message": "Deleted"}

# ===================== STORES =====================

def _store_deal_status(store_id: str):
    """Check if store has any active/expired deals."""
    cols = db.list_collection_names()
    if "deals" not in cols: return "none"
    now = datetime.utcnow()
    active = db.deals.find_one({"store_id": store_id, "status": "active"})
    if active:
        end = active.get("end_date")
        if end and isinstance(end, datetime) and end < now: return "expired"
        return "active"
    return "inactive"

def _fmt_store_fast(s, sub_map, deal_map, merchants):
    """Format store using pre-loaded batch data - no extra DB calls."""
    store_id = str(s["_id"])
    sub = sub_map.get(store_id)
    now = datetime.utcnow()

    # ── Deal status (deals use status:"active" field + end_date field) ──
    deals = deal_map.get(store_id, [])
    deal_status = "none"
    deal_text = ""
    for d in deals:
        end = d.get("end_date", "")
        if end:
            try:
                end_dt = end if isinstance(end, datetime) else datetime.strptime(str(end)[:10], "%Y-%m-%d")
                deal_status = "active" if end_dt >= now else "expired"
            except Exception:
                deal_status = "active"
        else:
            deal_status = "active"
        # Display text: use discount% or title
        disc = d.get("discount", 0)
        title = d.get("title", "")
        deal_text = f"{disc}% OFF" if disc else title
        break

    # ── Subscription info ──
    # paid_status = "paid"/"unpaid"/"expired" (what the HTML template reads)
    if sub:
        fd = sub.get("from_date"); ed = sub.get("end_date")
        sub_from = fd.strftime("%d %b %Y") if isinstance(fd, datetime) else str(fd or "")[:10]
        sub_to   = ed.strftime("%d %b %Y") if isinstance(ed, datetime) else str(ed or "")[:10]
        sub_status = sub.get("status", "pending")
        if isinstance(ed, datetime) and ed < now:
            sub_status = "expired"
        # Map to paid_status that HTML template uses
        paid_status = "paid" if sub_status in ("paid", "active") else sub_status
    else:
        sub_from = sub_to = ""
        paid_status = "unpaid"
        sub_status = "none"

    mid = s.get("merchant_id", "")
    merchant = merchants.get(mid, {})

    return {
        "_id":            store_id,
        "store_name":     s.get("store_name", ""),
        "merchant_name":  merchant.get("name", "Unknown"),
        "merchant_phone": merchant.get("phone", ""),
        "category":       s.get("category", ""),
        "city":           s.get("city", ""),
        "area":           s.get("area", ""),
        "address":        s.get("address", ""),
        "phone":          s.get("phone", ""),
        "status":         s.get("status", "active"),
        "points_per_scan":s.get("points_per_scan", 0),
        "visit_points":   s.get("visit_points", 0),
        "is_new_in_town": s.get("is_new_in_town", False),
        "badge": s.get("badge", ""),
        "image":          s.get("image") or s.get("_thumb") or "",
        "_thumb":         s.get("_thumb") or "",
        "qr_code":        s.get("qr_code", ""),
        "lat":            s.get("lat", ""),
        "lng":            s.get("lng", ""),
        "deal_status":    deal_status,
        "deal_text":      deal_text,
        "paid_status":    paid_status,
        "sub_from":       sub_from,
        "sub_to":         sub_to,
        "sub_plan":       sub.get("plan", "") if sub else "",
        "merchant_id":    mid,
        "about":          s.get("about", ""),
        "logo":           s.get("logo") or "",
        "image2":         s.get("store_image2") or s.get("image2") or "",
        "rating":         round(float(s.get("rating") or 0), 1),
        "user_rating":    round(float(s.get("user_rating") or 0), 1),
        "rating_count":   int(s.get("rating_count") or 0),
        "admin_rating":   round(float(s.get("admin_rating") or 0), 1) if s.get("admin_rating") else None,
    }

def _fmt_store(s):
    sid = str(s["_id"])
    mid = s.get("merchant_id", "")
    merchant = None
    if mid:
        try: merchant = db.merchants.find_one({"_id": ObjectId(mid)})
        except: pass
    # Latest paid subscription for this store
    sub = db.subscriptions.find_one(
        {"store_id": sid, "status": {"$in": ["paid", "active"]}},
        sort=[("created_at", -1)]
    )
    paid_status = "paid" if sub else "unpaid"
    sub_from = ""
    sub_to   = ""
    if sub:
        fd = sub.get("from_date"); ed = sub.get("end_date")
        sub_from = fd.strftime("%d %b %Y") if isinstance(fd, datetime) else str(fd or "")
        sub_to   = ed.strftime("%d %b %Y") if isinstance(ed, datetime) else str(ed or "")
    return {
        "_id": sid, "store_name": s.get("store_name"), "category": s.get("category"),
        "city": s.get("city"), "area": s.get("area"), "address": s.get("address"),
        "phone": s.get("phone"), "status": s.get("status", "active"),
        "merchant_name": merchant.get("name") if merchant else "Unknown",
        "merchant_id": mid, "qr_code": s.get("qr_code",""),
        "points_per_scan": s.get("points_per_scan", 0),
        "lat": s.get("lat",""), "lng": s.get("lng",""),
        "image": s.get("image") or "",
        "is_new_in_town": s.get("is_new_in_town", False),
        "badge": s.get("badge", ""),
        "deal_status": _store_deal_status(sid),
        "subscription_end": str(s.get("subscription_end","")),
        "paid_status": paid_status,
        "sub_from":    sub_from,
        "sub_to":      sub_to,
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
    }))
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
    for m in db.merchants.find({"_id": {"$in": merch_obj_ids}}, {"name": 1, "phone": 1}):
        merchants[str(m["_id"])] = m
    
    result = [_fmt_store_fast(s, sub_map, deal_map, merchants) for s in stores]
    _store_cache["data"] = result
    _store_cache["ts"] = _time.time()
    return result

@router.post("/stores")
def create_store(data: dict, a=Depends(get_current_admin)):
    global _store_cache; _store_cache["data"] = None
    mid = data.get("merchant_id","").strip()
    name = data.get("store_name","").strip()
    if not mid: raise HTTPException(400, "merchant_id required")
    if not name: raise HTTPException(400, "store_name required")
    try: merchant = db.merchants.find_one({"_id": ObjectId(mid)})
    except: raise HTTPException(400, "Invalid merchant_id")
    if not merchant: raise HTTPException(404, "Merchant not found")

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
        "image": data.get("image") or None,
        "is_new_in_town": bool(data.get("is_new_in_town", False)),
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
    # Accept image — only update if a new value is explicitly provided (not null/empty)
    if data.get("image"):          # only overwrite if new image sent
        upd["image"] = data["image"]
    if data.get("image2"):         # save image2 as store_image2 (matches public.py field name)
        upd["store_image2"] = data["image2"]
    if "is_new_in_town" in data: upd["is_new_in_town"] = bool(data["is_new_in_town"])
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
    result = []
    cols = db.list_collection_names()
    for u in db.users.find():
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
    u = db.users.find_one({"_id": ObjectId(id)})
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
    u = db.users.find_one({"_id": ObjectId(id)})
    if not u: raise HTTPException(404, "Not found")
    t = data.get("type","credit"); pts = int(data.get("points",0))
    if pts <= 0: raise HTTPException(400, "Points must be > 0")
    vp = u.get("visit_points",0); pp = u.get("pool_points",0)
    if t == "credit":
        db.users.update_one({"_id": ObjectId(id)}, {"$inc": {"pool_points": pts}})
    else:
        if vp+pp < pts: raise HTTPException(400, f"User has only {vp+pp} pts")
        if pp >= pts: db.users.update_one({"_id": ObjectId(id)}, {"$inc": {"pool_points": -pts}})
        else:
            rem = pts - pp
            db.users.update_one({"_id": ObjectId(id)}, {"$set": {"pool_points":0,"visit_points":max(0,vp-rem)}})
    db.point_adjustments.insert_one({"user_id":id,"type":t,"points":pts,"note":data.get("note",""),"created_at":datetime.utcnow()})
    upd = db.users.find_one({"_id": ObjectId(id)})
    return {"message":f"{'Added' if t=='credit' else 'Deducted'} {pts} pts",
            "new_total": upd.get("visit_points",0)+upd.get("pool_points",0)}

# ===================== STATS =====================

@router.get("/stats")
def admin_stats(a=Depends(get_current_admin)):
    cols = db.list_collection_names()
    return {
        "total_merchants": db.merchants.count_documents({}),
        "active_merchants": db.merchants.count_documents({"status":"active"}),
        "total_stores": db.stores.count_documents({}),
        "waiting_approval": db.stores.count_documents({"status":"waiting_approval"}),
        "total_deals": db.deals.count_documents({}) if "deals" in cols else 0,
        "total_users": db.users.count_documents({}) if "users" in cols else 0,
    }

# ===================== SUBSCRIPTIONS (Admin view) =====================

@router.get("/subscriptions")
def list_subscriptions(a=Depends(get_current_admin)):
    result = []
    for s in db.subscriptions.find().sort("created_at", -1):
        merchant = None
        try:
            merchant = db.merchants.find_one({"_id": ObjectId(s.get("merchant_id",""))})
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
    user = db.users.find_one({"_id": ObjectId(user_id)})
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
        db.users.update_one(
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
    """List all gift vouchers shown in the app home screen."""
    docs = list(db.gift_vouchers.find().sort("_id", -1))
    result = []
    for v in docs:
        result.append({
            "id":          str(v["_id"]),
            "title":       v.get("title", ""),
            "text":        v.get("text", ""),
            "validity":    v.get("validity", ""),
            "logo":        v.get("logo", ""),
            "store_id":    v.get("store_id", ""),
            "merchant_id": v.get("merchant_id", ""),
            "is_active":   v.get("is_active", True),
            "created_at":  str(v.get("created_at", ""))[:10],
        })
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
        "title":       (data.get("title") or "").strip(),
        "text":        text,
        "validity":    (data.get("validity") or "").strip(),
        "logo":        logo,
        "store_id":    store_id,
        "merchant_id": merchant_id,
        "is_active":   bool(data.get("is_active", True)),
        "created_at":  datetime.utcnow(),
    }
    result = db.gift_vouchers.insert_one(doc)
    return {"message": "Voucher created", "id": str(result.inserted_id)}

@router.put("/gift-vouchers/{vid}")
def update_gift_voucher(vid: str, data: dict, a=Depends(get_current_admin)):
    """Update an existing gift voucher."""
    upd = {}
    for field in ["title", "text", "validity", "logo", "merchant_id", "store_id"]:
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
    return [{"id": str(d["_id"]), "title": d.get("title",""), "image_url": d.get("image_url",""),
             "link_url": d.get("link_url",""), "sort_order": d.get("sort_order",0),
             "is_active": d.get("is_active", True)} for d in docs]

@router.post("/promo-sliders")
def create_promo_slider(data: dict, a=Depends(get_current_admin)):
    if not data.get("image_url"):
        raise HTTPException(400, "image_url required")
    doc = {"title": data.get("title",""), "image_url": data["image_url"],
           "link_url": data.get("link_url",""), "sort_order": int(data.get("sort_order",0)),
           "is_active": bool(data.get("is_active", True)),
           "from_date": data.get("from_date",""),
           "days":      int(data.get("days",0)),
           "end_date":  data.get("end_date",""),
           "created_at": datetime.utcnow()}
    r = db.promo_sliders.insert_one(doc)
    return {"message": "Slider created", "id": str(r.inserted_id)}

@router.put("/promo-sliders/{sid}")
def update_promo_slider(sid: str, data: dict, a=Depends(get_current_admin)):
    upd = {}
    for f in ["title","image_url","link_url","from_date","end_date"]:
        if f in data: upd[f] = data[f]
    if "sort_order" in data: upd["sort_order"] = int(data["sort_order"])
    if "is_active"  in data: upd["is_active"]  = bool(data["is_active"])
    if "days"       in data: upd["days"]        = int(data["days"])
    if not upd: raise HTTPException(400, "Nothing to update")
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
                u = db.users.find_one({"phone": {"$in": phone_variants}}, {"fcm_token": 1, "phone": 1})
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
    """All merchant-submitted banners with approval status."""
    result = []
    for b in db.merchant_banners.find().sort("created_at", -1):
        result.append({
            "_id":          str(b["_id"]),
            "merchant_name": b.get("merchant_name",""),
            "title":        b.get("title",""),
            "image_url":    b.get("image_url",""),
            "duration_days": b.get("duration_days", b.get("duration", 30)),
            "plan":         b.get("plan",""),
            "status":       b.get("approval_status","pending_approval"),
            "from_date":    b.get("from_date", b.get("start_date","")),
            "end_date":     b.get("end_date",""),
            "invoice_no":   b.get("invoice_no",""),
            "amount":       b.get("total",0),
            "created_at":   b["created_at"].strftime("%d %b %Y %H:%M") if isinstance(b.get("created_at"), datetime) else str(b.get("created_at",""))[:16],
        })
    return result

@router.put("/merchant-banners/{bid}/approve")
def approve_merchant_banner(bid: str, a=Depends(get_current_admin)):
    """Approve a merchant banner — publishes it as a promo slider."""
    b = db.merchant_banners.find_one({"_id": ObjectId(bid)})
    if not b: raise HTTPException(404, "Banner not found")
    db.merchant_banners.update_one({"_id": ObjectId(bid)}, {"$set": {"approval_status":"approved","approved_at":datetime.utcnow()}})
    # Publish to promo_sliders so it appears in app
    existing = db.promo_sliders.find_one({"source_banner_id": bid})
    if not existing:
        db.promo_sliders.insert_one({
            "title":       b.get("title",""),
            "image_url":   b.get("image_url",""),
            "is_active":   True,
            "sort_order":  50,
            "source":      "merchant",
            "source_banner_id": bid,
            "merchant_name": b.get("merchant_name",""),
            "expires_at":  b.get("end_date",""),
            "created_at":  datetime.utcnow(),
        })
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
    allowed = {"title", "image_url", "from_date", "end_date", "status"}
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

@router.get("/merchant-vouchers")
def list_merchant_vouchers(a=Depends(get_current_admin)):
    result = []
    for v in db.merchant_vouchers.find().sort("created_at", -1):
        result.append({
            "_id":          str(v["_id"]),
            "merchant_name": v.get("merchant_name",""),
            "title":        v.get("title",""),
            "offer_text":   v.get("offer_text",""),
            "logo_url":     v.get("logo_url",""),
            "validity":     v.get("validity", f"{v.get('from_date','')} → {v.get('end_date','')}" if v.get("from_date") else ""),
            "duration_days": v.get("duration_days", v.get("duration",30)),
            "from_date":    v.get("from_date",""),
            "end_date":     v.get("end_date",""),
            "status":       v.get("approval_status","pending_approval"),
            "invoice_no":   v.get("invoice_no",""),
            "amount":       v.get("total",0),
            "created_at":   v["created_at"].strftime("%d %b %Y %H:%M") if isinstance(v.get("created_at"), datetime) else str(v.get("created_at",""))[:16],
        })
    return result

@router.put("/merchant-vouchers/{vid}/approve")
def approve_merchant_voucher(vid: str, a=Depends(get_current_admin)):
    v = db.merchant_vouchers.find_one({"_id": ObjectId(vid)})
    if not v: raise HTTPException(404, "Voucher not found")
    db.merchant_vouchers.update_one({"_id": ObjectId(vid)}, {"$set":{"approval_status":"approved","approved_at":datetime.utcnow()}})
    # Publish to gift_vouchers so it appears in Voucher Zone
    existing = db.gift_vouchers.find_one({"source_voucher_id": vid})
    if not existing:
        db.gift_vouchers.insert_one({
            "title":      v.get("title",""),
            "text":       v.get("offer_text",""),
            "logo":       v.get("logo_url",""),
            "validity":   v.get("validity") or (f"{v.get('from_date','')} → {v.get('end_date','')}" if v.get("from_date") else "30 days"),
            "is_active":  True,
            "price":      "",
            "source":     "merchant",
            "source_voucher_id": vid,
            "merchant_name": v.get("merchant_name",""),
            "created_at": datetime.utcnow(),
        })
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
    allowed = {"title", "offer_text", "validity", "logo", "status"}
    update_data = {k: v for k, v in data.items() if k in allowed}
    if not update_data:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    update_data["updated_at"] = datetime.utcnow().isoformat()
    result = db.merchant_vouchers.update_one(
        {"_id": ObjectId(vid)},
        {"$set": update_data}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Voucher not found")
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
    """All merchant invoices: store subscriptions + banner + product orders."""
    result = []
    def _fd(v, full=False):
        if isinstance(v, datetime):
            return v.strftime("%d %b %Y %H:%M") if full else v.strftime("%d %b %Y")
        return str(v or "")

    # 1. Store invoices
    for inv in db.invoices.find().sort("created_at", -1):
        result.append({
            "invoice_no":    inv.get("invoice_no",""),
            "merchant_name": inv.get("merchant_name",""),
            "merchant_phone":inv.get("merchant_phone",""),
            "type":          inv.get("type","store"),
            "item_label":    inv.get("item_label") or f"Store – {inv.get('plan','')}",
            "store_name":    inv.get("store_name",""),
            "plan":          inv.get("plan",""),
            "base_price":    inv.get("base_price",0),
            "gst":           inv.get("gst",0),
            "gst_percent":   inv.get("gst_percent",0),
            "total":         inv.get("total",0),
            "from_date":     _fd(inv.get("from_date")),
            "end_date":      _fd(inv.get("end_date")),
            "created_at":    _fd(inv.get("created_at"), full=True),
            "_status":       inv.get("status","paid"),
        })

    # 2. Banner orders
    for b in db.merchant_banners.find().sort("created_at", -1):
        ca = b.get("created_at","")
        result.append({
            "invoice_no":    str(b["_id"])[:8].upper(),
            "merchant_name": b.get("merchant_name",""),
            "merchant_phone":b.get("merchant_phone",""),
            "type":          "banner",
            "item_label":    f"{b.get('duration_days','')} Day Banner",
            "store_name":    "",
            "plan":          f"{b.get('from_date','')} → {b.get('end_date','')}",
            "base_price":    b.get("base_price",0),
            "gst":           b.get("gst_amount",0),
            "gst_percent":   b.get("gst_percent",18),
            "total":         b.get("total",0),
            "from_date":     b.get("from_date",""),
            "end_date":      b.get("end_date",""),
            "created_at":    ca.strftime("%d %b %Y %H:%M") if isinstance(ca,datetime) else str(ca),
            "_status":       b.get("payment_status","free"),
        })

    # 3. Product (voucher) orders
    for v in db.merchant_vouchers.find().sort("created_at", -1):
        ca = v.get("created_at","")
        result.append({
            "invoice_no":    str(v["_id"])[:8].upper(),
            "merchant_name": v.get("merchant_name",""),
            "merchant_phone":v.get("merchant_phone",""),
            "type":          "product",
            "item_label":    f"{v.get('duration_days','')} Day Product",
            "store_name":    "",
            "plan":          f"{v.get('from_date','')} → {v.get('end_date','')}",
            "base_price":    v.get("base_price",0),
            "gst":           v.get("gst_amount",0),
            "gst_percent":   v.get("gst_percent",18),
            "total":         v.get("total",0),
            "from_date":     v.get("from_date",""),
            "end_date":      v.get("end_date",""),
            "created_at":    ca.strftime("%d %b %Y %H:%M") if isinstance(ca,datetime) else str(ca),
            "_status":       v.get("payment_status","free"),
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
