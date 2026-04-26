from utils.image_utils import process_store_image
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
        "image":          "",  # excluded from list query for performance
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
        "logo":           "",  # excluded from list for performance
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
    # Exclude large base64 image fields from list for performance
    projection = {"image": 0, "images": 0, "logo": 0}
    stores = list(db.stores.find({}, projection))
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
        "image_thumb": None,
        "is_new_in_town": bool(data.get("is_new_in_town", False)),
        "created_at": datetime.utcnow()
    }
    if store.get("image"):
        imgs = process_store_image(store["image"])
        store.update(imgs)
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
    if "image" in data and data["image"]:
        imgs = process_store_image(data["image"])
        upd.update(imgs)
    if "image2" in data and data.get("image2") is not None: upd["image2"] = data["image2"]
    if "is_new_in_town" in data: upd["is_new_in_town"] = bool(data["is_new_in_town"])
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

# =================== STORE RATING (Admin) ===================
@router.put("/stores/{store_id}/rating")
def set_store_rating(store_id: str, data: dict, a=Depends(get_current_admin)):
    """Admin can set/override a store's admin_rating."""
    try:
        sid = ObjectId(store_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid store_id")
    admin_rating = float(data.get("admin_rating", 0))
    db.stores.update_one({"_id": sid}, {"$set": {"admin_rating": admin_rating}})
    return {"ok": True, "admin_rating": admin_rating}


# =================== ALL RATINGS LIST ===================
@router.get("/ratings")
def list_all_ratings(a=Depends(get_current_admin)):
    """Return all user ratings with store and user info."""
    ratings = list(db.ratings.find().sort("created_at", -1).limit(500))
    result = []
    for r in ratings:
        store = db.stores.find_one({"_id": ObjectId(r["store_id"])}) if r.get("store_id") else None
        user  = db.users.find_one({"_id": ObjectId(r["user_id"])}) if r.get("user_id") else None
        result.append({
            "_id":        str(r["_id"]),
            "store_id":   r.get("store_id"),
            "store_name": store.get("store_name") if store else "—",
            "user_id":    r.get("user_id"),
            "user_name":  user.get("name") if user else "—",
            "user_phone": user.get("phone") if user else "—",
            "rating":     r.get("rating"),
            "created_at": r["created_at"].strftime("%d %b %Y %H:%M") if r.get("created_at") else "",
        })
    return result
