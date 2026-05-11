from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from database import db
from bson import ObjectId
from datetime import datetime
import uuid, qrcode, io, base64

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
        "points_per_scan":s.get("points_per_scan", 10),
        "visit_points":   s.get("visit_points", 10),
        "is_new_in_town": s.get("is_new_in_town", False),
        "image":          s.get("_thumb", ""),   # FIX: pass thumbnail URL through
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
        "badge":          s.get("badge", ""),
        "logo":           "",
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
        "points_per_scan": s.get("points_per_scan", 10),
        "lat": s.get("lat",""), "lng": s.get("lng",""),
        "image": s.get("image") or "",
        "is_new_in_town": s.get("is_new_in_town", False),
        "deal_status": _store_deal_status(sid),
        "subscription_end": str(s.get("subscription_end","")),
        "paid_status": paid_status,
        "sub_from":    sub_from,
        "sub_to":      sub_to,
    }

@router.get("/stores")
def list_stores(a=Depends(get_current_admin)):
    # Images stored as base64 in MongoDB — pass them through directly
    stores = list(db.stores.find({}, {"logo": 0}))
    for s in stores:
        # Build _thumb: prefer base64 image, fallback to images array
        img = s.get("image", "") or ""
        imgs = s.get("images", []) or []
        if isinstance(imgs, str): imgs = [imgs] if imgs else []
        if img:
            s["_thumb"] = img  # base64 or URL — pass as-is
        elif imgs:
            s["_thumb"] = imgs[0] if imgs[0] else ""
        else:
            s["_thumb"] = ""
        # Remove large images array to save payload — _thumb has what we need
        s.pop("images", None)
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
    
    return [_fmt_store_fast(s, sub_map, deal_map, merchants) for s in stores]

@router.post("/stores")
def create_store(data: dict, a=Depends(get_current_admin)):
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
        "points_per_scan": int(data.get("points_per_scan", 10)),
        "lat": data.get("lat",""), "lng": data.get("lng",""),
        "image": data.get("image") or None,
        "badge": data.get("badge") or "",  # custom badge tag
        "is_new_in_town": bool(data.get("is_new_in_town", False)),
        "created_at": datetime.utcnow()
    }
    result = db.stores.insert_one(store)
    sid = str(result.inserted_id)
    qr = generate_qr_base64(sid)
    db.stores.update_one({"_id": result.inserted_id}, {"$set": {"qr_code": qr}})
    return {"message": "Store created", "store_id": sid, "qr_code": qr}

@router.put("/stores/{id}")
def update_store(id: str, data: dict, a=Depends(get_current_admin)):
    store = db.stores.find_one({"_id": ObjectId(id)})
    if not store: raise HTTPException(404, "Not found")
    upd = {f: data[f] for f in ["store_name","category","city","state","area","address","phone","lat","lng","about"] if data.get(f) is not None}
    if "points_per_scan" in data and data["points_per_scan"] is not None:
        upd["points_per_scan"] = int(data["points_per_scan"])
    if "merchant_id" in data and data["merchant_id"] and data["merchant_id"].strip():
        upd["merchant_id"] = data["merchant_id"].strip()
    if "image" in data and data["image"]: upd["image"] = data["image"]
    if "is_new_in_town" in data: upd["is_new_in_town"] = bool(data["is_new_in_town"])
    if "badge" in data: upd["badge"] = data["badge"] or ""   # custom store badge/tag
    if "status" in data: upd["status"] = data["status"]
    if upd: db.stores.update_one({"_id": ObjectId(id)}, {"$set": upd})
    return {"message": "Updated"}

@router.put("/stores/{id}/approve")
def approve_store(id: str, a=Depends(get_current_admin)):
    db.stores.update_one({"_id": ObjectId(id)}, {"$set": {"status": "active"}})
    return {"message": "Store approved and live"}

@router.put("/stores/{id}/status")
def toggle_store(id: str, a=Depends(get_current_admin)):
    s = db.stores.find_one({"_id": ObjectId(id)})
    if not s: raise HTTPException(404, "Not found")
    ns = "inactive" if s.get("status") == "active" else "active"
    db.stores.update_one({"_id": ObjectId(id)}, {"$set": {"status": ns}})
    return {"status": ns}

@router.delete("/stores/{id}")
def delete_store(id: str, a=Depends(get_current_admin)):
    db.stores.delete_one({"_id": ObjectId(id)})
    return {"message": "Deleted"}

# ===================== STORE RATING (Admin Override) =====================

@router.put("/stores/{id}/rating")
def set_store_rating(id: str, data: dict, a=Depends(get_current_admin)):
    """Admin sets an override rating for a store. Shown in dashboard and app."""
    from bson import ObjectId as ObjId
    admin_rating = data.get("admin_rating")
    if admin_rating is None:
        raise HTTPException(400, "admin_rating required")
    val = float(admin_rating)
    db.stores.update_one({"_id": ObjId(id)}, {"$set": {"admin_rating": val, "rating": val}})
    return {"ok": True, "admin_rating": val}

@router.delete("/stores/{id}/rating")
def clear_store_rating(id: str, a=Depends(get_current_admin)):
    """Admin clears override rating so user average is used again."""
    from bson import ObjectId as ObjId
    db.stores.update_one({"_id": ObjId(id)}, {"$unset": {"admin_rating": ""}})
    return {"ok": True}

# ===================== GIFT VOUCHERS (Voucher Zone - App Display) =====================

@router.get("/gift-vouchers")
def list_gift_vouchers(a=Depends(get_current_admin)):
    """List all gift vouchers shown in Voucher Zone on the app home screen."""
    docs = list(db.gift_vouchers.find({}).sort("_id", -1))
    result = []
    for d in docs:
        d["id"] = str(d.pop("_id"))
        result.append(d)
    return result

@router.post("/gift-vouchers")
def create_gift_voucher(data: dict, a=Depends(get_current_admin)):
    """Create a new voucher card shown in the Voucher Zone section of the app."""
    data.setdefault("is_active", True)
    data.setdefault("created_at", datetime.utcnow().isoformat())
    res = db.gift_vouchers.insert_one(data)
    return {"ok": True, "id": str(res.inserted_id)}

@router.put("/gift-vouchers/{id}")
def update_gift_voucher(id: str, data: dict, a=Depends(get_current_admin)):
    """Update an existing Voucher Zone card."""
    data.pop("_id", None); data.pop("id", None)
    db.gift_vouchers.update_one({"_id": ObjectId(id)}, {"$set": data})
    return {"ok": True}

@router.delete("/gift-vouchers/{id}")
def delete_gift_voucher(id: str, a=Depends(get_current_admin)):
    """Delete a Voucher Zone card."""
    db.gift_vouchers.delete_one({"_id": ObjectId(id)})
    return {"ok": True}

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
    print("📊 Collections in DB:", cols)   # DEBUG
    users_count = db.users.count_documents({}) if "users" in cols else 0
    print(f"📊 Users count: {users_count}")  # DEBUG
    result = {
        "total_merchants": db.merchants.count_documents({}),
        "active_merchants": db.merchants.count_documents({"status":"active"}),
        "total_stores": db.stores.count_documents({}),
        "waiting_approval": db.stores.count_documents({"status":"waiting_approval"}),
        "total_deals": db.deals.count_documents({}) if "deals" in cols else 0,
        "total_users": users_count,
        "waiting_vouchers": db.withdraw_requests.count_documents({"status":"pending"}) if "withdraw_requests" in cols else 0,
    }
    print(f"📊 Stats response: {result}")  # DEBUG
    return result

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
            "created_at":     s["created_at"].strftime("%d %b %Y") if s.get("created_at") else "",
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
            "created_at":    inv["created_at"].strftime("%d %b %Y %H:%M") if inv.get("created_at") else "",
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


# ===================== NOTIFICATIONS (FIX 7) =====================

# ═══════════════════════════════════════════════════════════
# FIREBASE ADMIN SDK — initialized once at module load
# ═══════════════════════════════════════════════════════════
# Phone normalization helper — handles country code mismatches
# between what admin types and what is stored in MongoDB
# e.g. admin types "8105674906", DB has "+918105674906"
# ═══════════════════════════════════════════════════════════
def _phone_variants(raw: str) -> list:
    """Return all plausible phone string variants for MongoDB $in query."""
    p = raw.strip().replace(" ", "").replace("-", "")
    if not p:
        return []
    variants = {p}  # exact as typed
    # Strip leading + or 0
    digits = p.lstrip("+0")
    # Handle Indian numbers: last 10 digits
    last10 = digits[-10:] if len(digits) >= 10 else digits
    # Build all common formats
    variants.update([
        last10,              # 8105674906
        "+91" + last10,     # +918105674906
        "91" + last10,      # 918105674906
        "0"  + last10,      # 08105674906
    ])
    return list(variants)


# Uses FIREBASE_SERVICE_ACCOUNT env var (JSON string)
# ═══════════════════════════════════════════════════════════
def _get_fcm_app():
    """Return initialized firebase_admin app (singleton).
    Reads FIREBASE_SERVICE_ACCOUNT_JSON (Railway) or FIREBASE_SERVICE_ACCOUNT as fallback.
    """
    import firebase_admin
    from firebase_admin import credentials as fb_cred
    import os, json as _j

    # Return existing app if already initialized
    try:
        return firebase_admin.get_app("offro")
    except ValueError:
        pass  # not initialized yet — proceed below

    # Read env var — support both names (Railway uses _JSON suffix)
    sa_json = (
        os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
        or os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
    ).strip()

    if not sa_json:
        print("[FCM] ❌ FIREBASE_SERVICE_ACCOUNT_JSON env var not set — cannot init Firebase Admin")
        return None

    try:
        sa_dict = _j.loads(sa_json)
        cred = fb_cred.Certificate(sa_dict)
        app = firebase_admin.initialize_app(cred, name="offro")
        print(f"[FCM] ✅ Firebase Admin initialized successfully (project={sa_dict.get('project_id', '?')})")
        return app
    except _j.JSONDecodeError as e:
        print(f"[FCM] ❌ Firebase service account JSON is malformed: {e}")
        return None
    except Exception as e:
        print(f"[FCM] ❌ Firebase Admin init failed: {e}")
        return None


def _send_via_firebase_admin(tokens: list, title: str, body: str, image_url: str = "", data: dict = None) -> tuple:
    """Send individual FCM messages via Firebase Admin SDK (HTTP v1 API).
    Returns (sent_count, failed_count).
    """
    from firebase_admin import messaging as fb_msg
    app = _get_fcm_app()
    if app is None:
        print("[FCM] No app initialized — check FIREBASE_SERVICE_ACCOUNT_JSON env var on Railway")
        return 0, 0

    sent = 0; failed = 0
    extra_data = data or {}

    # Build multicast messages in batches of 500
    for i in range(0, len(tokens), 500):
        batch = tokens[i:i+500]
        reg_ids = [t["token"] for t in batch]
        try:
            notif = fb_msg.Notification(title=title, body=body, image=image_url or None)
            android_notif = fb_msg.AndroidNotification(
                title=title, body=body,
                image=image_url or None,
                sound="default",
                notification_count=1,
            )
            android_config = fb_msg.AndroidConfig(
                priority="high",
                notification=android_notif,
            )
            apns_config = fb_msg.APNSConfig(
                payload=fb_msg.APNSPayload(
                    aps=fb_msg.Aps(sound="default", badge=1)
                )
            )
            mm = fb_msg.MulticastMessage(
                tokens=reg_ids,
                notification=notif,
                android=android_config,
                apns=apns_config,
                data={**{k: str(v) for k, v in extra_data.items()},
                      "image_url": image_url or "",
                      "title": title, "body": body},
            )
            resp = fb_msg.send_each_for_multicast(mm, app=app)
            sent   += resp.success_count
            failed += resp.failure_count
            print(f"[FCM] Batch {i//500 + 1}: sent={resp.success_count} failed={resp.failure_count} total_tokens={len(reg_ids)}")
            # Log failures and successes with message IDs
            for j, r in enumerate(resp.responses):
                if r.success:
                    print(f"[FCM] ✅ sent successfully message_id={r.message_id}")
                else:
                    print(f"[FCM] ❌ Token {j} failed: {r.exception}")
        except Exception as e:
            print(f"[FCM] Batch {i//500 + 1} error: {e}")
            failed += len(batch)

    print(f"[FCM] Direct send complete: total_sent={sent} total_failed={failed}")
    return sent, failed


def _send_fcm_topic(topic: str, title: str, body: str, image_url: str = "", data: dict = None) -> bool:
    """Send FCM notification to a topic (e.g. all_users, ballari_users, offers).
    Returns True on success.
    """
    from firebase_admin import messaging as fb_msg
    app = _get_fcm_app()
    if app is None:
        print("[FCM] No app for topic send — check FIREBASE_SERVICE_ACCOUNT_JSON env var on Railway")
        return False

    try:
        extra_data = data or {}
        msg = fb_msg.Message(
            topic=topic,
            notification=fb_msg.Notification(title=title, body=body, image=image_url or None),
            android=fb_msg.AndroidConfig(
                priority="high",
                notification=fb_msg.AndroidNotification(
                    title=title, body=body,
                    image=image_url or None,
                    sound="default",
                ),
            ),
            apns=fb_msg.APNSConfig(
                payload=fb_msg.APNSPayload(aps=fb_msg.Aps(sound="default", badge=1))
            ),
            data={**{k: str(v) for k, v in extra_data.items()},
                  "image_url": image_url or "",
                  "title": title, "body": body},
        )
        msg_id = fb_msg.send(msg, app=app)
        print(f"[FCM] Topic '{topic}' message sent: {msg_id}")
        return True
    except Exception as e:
        print(f"[FCM] Topic '{topic}' send failed: {e}")
        return False


@router.get("/notifications")
def get_notifications(a=Depends(get_current_admin)):
    """Get notification history — sorted newest first."""
    docs = list(db.notifications.find({}).sort("created_at", -1).limit(200))
    result = []
    for d in docs:
        d["_id"] = str(d["_id"])
        result.append(d)
    return result


@router.delete("/notifications/{notif_id}")
def delete_notification(notif_id: str, a=Depends(get_current_admin)):
    """Delete a notification record by ID."""
    from bson import ObjectId
    try:
        oid = ObjectId(notif_id)
    except Exception:
        raise HTTPException(400, "Invalid notification ID")
    result = db.notifications.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(404, "Notification not found")
    print(f"[ADMIN] Deleted notification {notif_id}")
    return {"ok": True, "deleted": notif_id}


@router.post("/notifications/send")
def send_notification(body: dict, a=Depends(get_current_admin)):
    """Send push notification.
    target: "all_users" | "city" | "offers" | "specific"
    target_city: city name when target="city" (e.g. "Ballari")
    """
    title      = body.get("title", "").strip()
    msg        = body.get("body", "").strip()
    target     = body.get("target", "all_users")
    phone      = body.get("target_phone", "").strip()
    city_raw   = body.get("target_city", "").strip()
    img_url    = body.get("image_url", "").strip()
    use_topic  = body.get("use_topic", True)   # default: use FCM topics

    if not title or not msg:
        raise HTTPException(400, "Title and body are required")
    if target == "specific" and not phone:
        raise HTTPException(400, "Phone required for specific target")
    if target == "city" and not city_raw:
        raise HTTPException(400, "City name required for city target")

    # Determine FCM topic name
    topic_map = {
        "all_users": "all_users",
        "offers":    "offers",
        "city":      (city_raw.lower().strip().replace(" ", "_") + "_users") if city_raw else None,
    }
    topic = topic_map.get(target)

    # Save to DB first
    notif_doc = {
        "title": title, "body": msg, "target": target,
        "target_phone": phone if target == "specific" else "",
        "target_city": city_raw if target == "city" else "",
        "image_url": img_url,
        "topic": topic or "",
        "status": "queued",
        "processed": 0, "failed": 0,
        "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "sent_at": None,
    }
    inserted = db.notifications.insert_one(notif_doc)
    notif_id = inserted.inserted_id

    sent = 0; failed = 0

    # Debug: count total users + users with tokens in DB
    total_users   = db.users.count_documents({})
    users_w_token = db.users.count_documents({"fcm_token": {"$exists": True, "$ne": ""}})
    print(f"[NOTIF] target={target} | total_users={total_users} | users_with_fcm_token={users_w_token}")

    if target == "specific":
        # Direct token send to individual user
        # Normalize phone: admin may type "8105674906", DB may store "+918105674906" or "918105674906"
        phone_variants = _phone_variants(phone)
        print(f"[NOTIF] specific lookup phone={phone!r} variants={phone_variants}")
        user = db.users.find_one({"phone": {"$in": phone_variants}}, {"fcm_token": 1, "name": 1, "phone": 1})
        print(f"[NOTIF] specific user found: {bool(user)} | has_token: {bool(user and user.get('fcm_token'))} | db_phone={user.get('phone') if user else 'N/A'}")
        tokens = [{"token": user["fcm_token"], "user_id": str(user["_id"])}] if user and user.get("fcm_token") else []
        if tokens:
            sent, failed = _send_via_firebase_admin(tokens, title, msg, img_url)
        else:
            failed = 1
    elif topic and use_topic:
        # Topic-based broadcast — sends to ALL subscribers of the topic
        print(f"[NOTIF] Sending to topic: {topic}")
        ok = _send_fcm_topic(topic, title, msg, img_url, data={"target": target, "city": city_raw})
        if ok:
            sent = 1; failed = 0   # topic sends don't have per-token counts
        else:
            failed = 1
        # Also estimate reach from DB
        if target == "city" and city_raw:
            q = {"city": {"$regex": city_raw, "$options": "i"}, "fcm_token": {"$exists": True, "$ne": ""}}
        else:
            q = {"fcm_token": {"$exists": True, "$ne": ""}}
        notif_doc["token_count"] = db.users.count_documents(q)
        db.notifications.update_one({"_id": notif_id}, {"$set": {"token_count": notif_doc["token_count"]}})
        print(f"[NOTIF] Topic send ok={ok} estimated_reach={notif_doc['token_count']}")
    else:
        # Fallback: direct token send to matching users
        if target == "city" and city_raw:
            query = {"city": {"$regex": city_raw, "$options": "i"}, "fcm_token": {"$exists": True, "$ne": ""}}
        else:
            query = {"fcm_token": {"$exists": True, "$ne": ""}}
        users = list(db.users.find(query, {"fcm_token": 1}))
        tokens = [{"token": u["fcm_token"], "user_id": str(u["_id"])} for u in users if u.get("fcm_token")]
        print(f"[NOTIF] Direct send: found {len(tokens)} tokens from query={query}")
        if tokens:
            sent, failed = _send_via_firebase_admin(tokens, title, msg, img_url)

    # Update status
    status = "sent" if (sent > 0 and failed == 0) else ("partial" if sent > 0 else ("failed" if failed > 0 else "queued"))
    db.notifications.update_one({"_id": notif_id}, {"$set": {
        "status": status, "processed": sent, "failed": failed,
        "sent_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
    }})

    return {
        "ok": True, "sent": sent, "failed": failed,
        "status": status, "topic": topic or "",
        "message": f"Notification {status} — topic: {topic or 'direct'}, delivered: {sent}, failed: {failed}"
    }


@router.post("/notifications/process-queue")
def process_notification_queue(a=Depends(get_current_admin)):
    """Retry all queued or failed notifications."""
    pending = list(db.notifications.find({"status": {"$in": ["queued", "failed", "partial"]}}))
    total_sent = 0; total_failed = 0; total_skipped = 0

    for notif in pending:
        title   = notif.get("title", "")
        msg     = notif.get("body", "")
        target  = notif.get("target", "all_users")
        phone   = notif.get("target_phone", "")
        city_raw= notif.get("target_city", "")
        img     = notif.get("image_url", "")
        topic   = notif.get("topic", "")

        sent = 0; failed = 0

        if target == "specific":
            user = db.users.find_one({"phone": phone}, {"fcm_token": 1})
            tokens = [{"token": user["fcm_token"], "user_id": str(user["_id"])}] if user and user.get("fcm_token") else []
            if tokens:
                sent, failed = _send_via_firebase_admin(tokens, title, msg, img)
            else:
                total_skipped += 1
                db.notifications.update_one({"_id": notif["_id"]}, {"$set": {"status": "skipped_no_tokens"}})
                continue
        elif topic:
            ok = _send_fcm_topic(topic, title, msg, img)
            sent = 1 if ok else 0; failed = 0 if ok else 1
        else:
            if city_raw:
                query = {"city": {"$regex": city_raw, "$options": "i"}, "fcm_token": {"$exists": True, "$ne": ""}}
            else:
                query = {"fcm_token": {"$exists": True, "$ne": ""}}
            users = list(db.users.find(query, {"fcm_token": 1}))
            tokens = [{"token": u["fcm_token"], "user_id": str(u["_id"])} for u in users if u.get("fcm_token")]
            if not tokens:
                total_skipped += 1
                db.notifications.update_one({"_id": notif["_id"]}, {"$set": {"status": "skipped_no_tokens"}})
                continue
            sent, failed = _send_via_firebase_admin(tokens, title, msg, img)

        total_sent += sent; total_failed += failed
        new_status = "sent" if (sent > 0 and failed == 0) else ("partial" if sent > 0 else "failed")
        db.notifications.update_one({"_id": notif["_id"]}, {"$set": {
            "status": new_status, "processed": sent, "failed": failed,
            "sent_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        }})

    return {"ok": True, "processed": total_sent, "failed": total_failed, "skipped": total_skipped}

