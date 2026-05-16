"""
Merchant App Router — self-service portal
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from database import db
from bson import ObjectId
from datetime import datetime, timedelta
import uuid, qrcode, io, base64, hmac, hashlib

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

RAZORPAY_KEY_ID     = _os.getenv("RAZORPAY_KEY_ID",     "rzp_live_SdiI6kcuZzZjsl")
RAZORPAY_KEY_SECRET = _os.getenv("RAZORPAY_KEY_SECRET", "3JzhKnKuGkhCrelaUgCaFfQr")

# ───────────── helpers ─────────────

def _qr(store_id: str) -> str:
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(f"localsaver://redeem?store_id={store_id}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="#3E5F55", back_color="white")
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode()

def get_merchant(request: Request):
    token = (request.cookies.get("merchant_token") or
             request.headers.get("Authorization", "").replace("Bearer ", ""))
    if not token: raise HTTPException(401, "Not authenticated")
    m = db.merchants.find_one({"token": token})
    if not m:    raise HTTPException(403, "Invalid session")
    return m

def plan_days(plan: str) -> int:
    return {"1month": 30, "3months": 90, "6months": 180, "12months": 365}.get(plan, 30)

def _log_tx(merchant_id: str, tx_type: str, description: str, amount: float = 0, meta: dict = None):
    """Write a transaction record for a merchant."""
    db.merchant_transactions.insert_one({
        "merchant_id": merchant_id,
        "type": tx_type,          # "subscription" | "store_created" | "store_approved" | "subscription_expired"
        "description": description,
        "amount": amount,
        "meta": meta or {},
        "created_at": datetime.utcnow(),
    })

# ───────────── auth ─────────────

@router.post("/register")
def merchant_register(data: dict):
    name  = data.get("name", "").strip()
    phone = str(data.get("phone", "")).strip()
    city  = data.get("city", "").strip()
    area  = data.get("area", "").strip()
    if not name or not phone:
        raise HTTPException(400, "Name and phone are required")
    if db.merchants.find_one({"phone": phone}):
        raise HTTPException(400, "Phone already registered. Please login.")
    merchant = {
        "name": name, "phone": phone,
        "city": city, "area": area,
        "status": "active", "token": None,
        "registered_at": datetime.utcnow(),
    }
    result = db.merchants.insert_one(merchant)
    _log_tx(str(result.inserted_id), "account_created", f"Merchant account created for {name}")
    return {"message": "Registered successfully. You can now login.", "merchant_id": str(result.inserted_id)}

@router.post("/login")
def merchant_login(data: dict):
    phone = str(data.get("phone", "")).strip()
    m = db.merchants.find_one({"phone": phone})
    if not m: raise HTTPException(401, "Phone not registered. Please register first.")
    token = str(uuid.uuid4())
    db.merchants.update_one({"_id": m["_id"]}, {"$set": {"token": token}})
    res = JSONResponse({"merchant_id": str(m["_id"]), "name": m.get("name"),
                        "phone": m.get("phone"), "token": token})
    res.set_cookie("merchant_token", token, httponly=True, samesite="Lax", max_age=3600 * 24)
    return res

@router.post("/logout")
def merchant_logout():
    res = JSONResponse({"message": "Logged out"})
    res.delete_cookie("merchant_token")
    return res

# ───────────── profile ─────────────

@router.get("/me")
def merchant_me(m=Depends(get_merchant)):
    return {
        "merchant_id": str(m["_id"]), "name": m.get("name"),
        "phone": m.get("phone"),       "city": m.get("city", ""),
        "area": m.get("area", ""),     "status": m.get("status", "active"),
    }

# ───────────── stores ─────────────

@router.get("/stores")
def my_stores(m=Depends(get_merchant)):
    mid = str(m["_id"])
    result = []
    for s in db.stores.find({"merchant_id": mid}):
        sub_end = s.get("subscription_end")
        sub_end_str = ""
        if sub_end:
            if isinstance(sub_end, datetime):
                sub_end_str = sub_end.strftime("%d %b %Y")
            else:
                sub_end_str = str(sub_end)
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
            "city":            s.get("city", ""),
            "area":            s.get("area", ""),
            "address":         s.get("address", ""),
            "phone":           s.get("phone", ""),
            "status":          s.get("status", "draft"),
            "subscription_end": sub_end_str,
            "subscription_plan": s.get("subscription_plan", ""),
            "visit_points":    s.get("points_per_scan", 10),
            "is_new_in_town":  s.get("is_new_in_town", False),
            "qr_code":         s.get("qr_code", ""),
            "image":           s.get("image") or "",
            "image2":          s.get("image2") or "",
            "deal_count":      deal_count,
            "has_paid_sub":    has_paid_sub,
        })
    return result

@router.post("/stores")
def create_merchant_store(data: dict, m=Depends(get_merchant)):
    store_name = data.get("store_name", "").strip()
    if not store_name: raise HTTPException(400, "Store name required")
    store = {
        "merchant_id":   str(m["_id"]),
        "merchant_name": m.get("name"),
        "store_name":    store_name,
        "category":      data.get("category", ""),
        "city":          data.get("city") or m.get("city", ""),
        "area":          data.get("area") or m.get("area", ""),
        "address":       data.get("address", ""),
        "phone":         data.get("phone") or m.get("phone", ""),
        "about":         data.get("about", ""),
        "status":        "draft",
        "points_per_scan": 0,
        "lat":  data.get("lat", ""),   "lng": data.get("lng", ""),
        "image":        data.get("image") or None,
        "is_new_in_town": False,
        "created_at":   datetime.utcnow(),
    }
    result = db.stores.insert_one(store)
    sid = str(result.inserted_id)
    qr_b64 = _qr(sid)
    db.stores.update_one({"_id": result.inserted_id}, {"$set": {"qr_code": qr_b64}})
    _log_tx(str(m["_id"]), "store_created", f"Store '{store_name}' created", meta={"store_id": sid})
    return {"store_id": sid, "qr_code": qr_b64, "message": "Store created. Subscribe to go live."}

@router.get("/stores/{sid}")
def get_merchant_store(sid: str, m=Depends(get_merchant)):
    """Return full store detail including image2 — used by edit store screen."""
    store = db.stores.find_one({"_id": ObjectId(sid), "merchant_id": str(m["_id"])})
    if not store: raise HTTPException(404, "Store not found")
    sub_end = store.get("subscription_end")
    sub_end_str = sub_end.strftime("%d %b %Y") if isinstance(sub_end, datetime) else (str(sub_end) if sub_end else "")
    deal_count = db.deals.count_documents({"store_id": sid, "status": "active"}) \
        if "deals" in db.list_collection_names() else 0
    paid_sub = db.subscriptions.find_one({"store_id": sid, "status": {"$in": ["paid", "active"]}})
    return {
        "_id":              sid,
        "store_name":       store.get("store_name", ""),
        "category":         store.get("category", ""),
        "city":             store.get("city", ""),
        "area":             store.get("area", ""),
        "address":          store.get("address", ""),
        "phone":            store.get("phone", ""),
        "lat":              store.get("lat", ""),
        "lng":              store.get("lng", ""),
        "status":           store.get("status", "draft"),
        "subscription_end": sub_end_str,
        "subscription_plan": store.get("subscription_plan", ""),
        "visit_points":     store.get("points_per_scan", 10),
        "is_new_in_town":   store.get("is_new_in_town", False),
        "qr_code":          store.get("qr_code", ""),
        "image":            store.get("image") or "",
        "image2":           store.get("image2") or "",
        "about":            store.get("about") or "",
        "deal_count":       deal_count,
        "has_paid_sub":     paid_sub is not None,
    }

@router.put("/stores/{sid}")
def update_merchant_store(sid: str, data: dict, m=Depends(get_merchant)):
    store = db.stores.find_one({"_id": ObjectId(sid), "merchant_id": str(m["_id"])})
    if not store: raise HTTPException(404, "Store not found")
    upd = {f: data[f] for f in ["store_name","category","city","area","address","phone","lat","lng","about"] if data.get(f) is not None}
    if data.get("image"): upd["image"] = data["image"]
    if data.get("image2") is not None: upd["image2"] = data["image2"]  # image2 save support
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

    store = db.stores.find_one({"_id": ObjectId(store_id), "merchant_id": str(m["_id"])})
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
        "merchant_id":        str(m["_id"]),
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
    msg      = f"{order_id}|{payment_id}"
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(400, "Payment signature mismatch")

    sub = db.subscriptions.find_one({"razorpay_order_id": order_id, "status": "pending"})
    if not sub: raise HTTPException(404, "Subscription record not found")

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
    store_doc  = db.stores.find_one({"_id": ObjectId(store_id)}, {"store_name": 1}) or {}
    db.invoices.insert_one({
        "invoice_no":         invoice_no,
        "merchant_id":        str(m["_id"]),
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

    _log_tx(str(m["_id"]), "subscription",
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
    store_doc  = db.stores.find_one({"_id": ObjectId(store_id)}, {"store_name": 1}) or {}
    db.invoices.insert_one({
        "invoice_no":    invoice_no,
        "merchant_id":   str(m["_id"]),
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
    _log_tx(str(m["_id"]), "subscription",
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
    for inv in db.invoices.find({"merchant_id": str(m["_id"])}).sort("created_at", -1):
        fd = inv.get("from_date"); ed = inv.get("end_date")
        result.append({
            "invoice_no":  inv.get("invoice_no"),
            "store_name":  inv.get("store_name"),
            "plan":        inv.get("plan"),
            "total":       inv.get("total"),
            "gst":         inv.get("gst"),
            "base_price":  inv.get("base_price"),
            "from_date":   fd.strftime("%d %b %Y") if isinstance(fd, datetime) else str(fd or ""),
            "end_date":    ed.strftime("%d %b %Y") if isinstance(ed, datetime) else str(ed or ""),
            "created_at":  inv["created_at"].strftime("%d %b %Y %H:%M") if inv.get("created_at") else "",
        })
    return result

# ───────────── transaction history ─────────────

@router.get("/transactions")
def my_transactions(m=Depends(get_merchant)):
    result = []
    for tx in db.merchant_transactions.find({"merchant_id": str(m["_id"])}).sort("created_at", -1).limit(100):
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
    merchant_id = str(m["_id"])
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
    merchant_id = str(m["_id"])
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

@router.delete("/deals/{deal_id}")
def delete_deal(deal_id: str, m=Depends(get_merchant)):
    merchant_id = str(m["_id"])
    db.deals.delete_one({"_id": ObjectId(deal_id), "merchant_id": merchant_id})
    return {"message": "Deal deleted"}


# ───────────── terms (public read) ─────────────

@router.get("/terms")
def merchant_terms():
    doc = db.terms.find_one({"type": "merchant"}) or {}
    return {"content": doc.get("content", "Merchant terms and conditions will be posted here.")}

# ───────────── subscriptions list ─────────────

@router.get("/subscriptions")
def my_subscriptions(m=Depends(get_merchant)):
    result = []
    for s in db.subscriptions.find({"merchant_id": str(m["_id"])}).sort("created_at", -1):
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
    db.merchants.update_one({"_id": m["_id"]}, {"$set": update})
    return {"ok": True}


# ═══════════════════════════════════════════════════════════
# MERCHANT BANNERS — paid promotional sliders in user app
# ═══════════════════════════════════════════════════════════

def _get_banner_pricing():
    """Pull banner pricing from admin pricing doc. Default ₹299/30days."""
    doc = db.pricing.find_one({}) or {}
    return {
        "price_7":  doc.get("banner_price_7",  149),
        "price_14": doc.get("banner_price_14", 249),
        "price_30": doc.get("banner_price_30", 399),
        "gst":      doc.get("gst_percent", 18),
    }

def _get_voucher_pricing():
    """Pull voucher pricing from admin pricing doc. Default ₹199/voucher."""
    doc = db.pricing.find_one({}) or {}
    return {
        "price_30": doc.get("voucher_price_30", 199),
        "price_60": doc.get("voucher_price_60", 349),
        "price_90": doc.get("voucher_price_90", 499),
        "gst":      doc.get("gst_percent", 18),
    }

def _has_eligible_store(merchant_id: str) -> bool:
    """Eligibility: merchant has at least one active or expired store."""
    store = db.stores.find_one({
        "merchant_id": merchant_id,
        "status": {"$in": ["active", "inactive", "waiting_approval", "paid"]}
    })
    return store is not None

def _gen_invoice_no(prefix: str) -> str:
    now = datetime.utcnow()
    uid = str(uuid.uuid4())[:6].upper()
    return f"{prefix}-{now.strftime('%Y%m%d')}-{uid}"

def _compute_gst(price: float, gst_pct: float):
    gst = round(price * gst_pct / 100, 2)
    total = round(price + gst, 2)
    return gst, total

# ─── List my banners ───
@router.get("/banners")
def my_banners(m=Depends(get_merchant)):
    result = []
    for b in db.merchant_banners.find({"merchant_id": str(m["_id"])}).sort("created_at", -1):
        result.append({
            "_id":        str(b["_id"]),
            "title":      b.get("title",""),
            "image_url":  b.get("image_url",""),
            "duration":   b.get("duration", 30),
            "status":     b.get("approval_status","pending_approval"),
            "start_date": b.get("start_date",""),
            "end_date":   b.get("end_date",""),
            "invoice_no": b.get("invoice_no",""),
            "amount":     b.get("total",0),
            "created_at": b["created_at"].strftime("%d %b %Y %H:%M") if b.get("created_at") else "",
        })
    return result

# ─── Get banner pricing ───
@router.get("/banners/pricing")
def banner_pricing(m=Depends(get_merchant)):
    p = _get_banner_pricing()
    return {
        "plans": [
            {"id":"7days",  "label":"7 Days",  "price":p["price_7"],  "gst_pct":p["gst"]},
            {"id":"14days", "label":"14 Days", "price":p["price_14"], "gst_pct":p["gst"]},
            {"id":"30days", "label":"30 Days", "price":p["price_30"], "gst_pct":p["gst"]},
        ],
        "max_size_mb": 2,
        "recommended_px": "1200 x 400",
    }

# ─── Initiate banner purchase order ───
@router.post("/banners/order")
def initiate_banner_order(data: dict, m=Depends(get_merchant)):
    merchant_id = str(m["_id"])
    if not _has_eligible_store(merchant_id):
        raise HTTPException(403, "You need an active or previously subscribed store to create banners.")

    plan = data.get("plan","30days")  # 7days / 14days / 30days
    p = _get_banner_pricing()
    gst_pct = p["gst"]

    plan_map = {
        "7days":  (7,  p["price_7"]),
        "14days": (14, p["price_14"]),
        "30days": (30, p["price_30"]),
    }
    days, base_price = plan_map.get(plan, (30, p["price_30"]))
    gst_amt, total = _compute_gst(base_price, gst_pct)

    from_date_str = data.get("from_date", datetime.utcnow().strftime("%Y-%m-%d"))
    try:
        from_dt = datetime.strptime(from_date_str, "%Y-%m-%d")
    except Exception:
        from_dt = datetime.utcnow()
    end_dt = from_dt + timedelta(days=days)

    # Create pending subscription record
    sub_id = db.banner_orders.insert_one({
        "merchant_id": merchant_id,
        "plan":        plan,
        "days":        days,
        "base_price":  base_price,
        "gst_percent": gst_pct,
        "gst_amount":  gst_amt,
        "total":       total,
        "from_date":   from_dt,
        "end_date":    end_dt,
        "status":      "pending",
        "created_at":  datetime.utcnow(),
    }).inserted_id

    # Fetch Razorpay keys
    doc = db.pricing.find_one({}) or {}
    rzp_key = doc.get("razorpay_key_id") or os.environ.get("RAZORPAY_KEY_ID","")
    rzp_secret = doc.get("razorpay_key_secret") or os.environ.get("RAZORPAY_KEY_SECRET","")

    amount_paise = int(round(total * 100))
    pay_mode = "manual"
    rzp_order_id = None

    if rzp_key and rzp_secret and amount_paise > 0:
        try:
            resp = _razorpay_request("POST", "/orders", (rzp_key, rzp_secret), {
                "amount": amount_paise, "currency": "INR",
                "receipt": f"banner_{str(sub_id)[-8:]}",
                "notes": {"merchant_id": merchant_id, "plan": plan}
            })
            rzp_order_id = resp.get("id")
            pay_mode = "razorpay"
        except Exception as e:
            print(f"[Razorpay] Banner order error: {e}")

    return {
        "order_id":      str(sub_id),
        "plan":          plan,
        "plan_label":    f"{days} Days",
        "base_price":    base_price,
        "gst_percent":   gst_pct,
        "gst_amount":    gst_amt,
        "amount_display":total,
        "from_date":     from_dt.strftime("%d %b %Y"),
        "end_date":      end_dt.strftime("%d %b %Y"),
        "razorpay_order_id": rzp_order_id,
        "razorpay_key":  rzp_key,
        "pay_mode":      pay_mode,
        "amount_paise":  amount_paise,
    }

# ─── Verify banner payment + create banner ───
@router.post("/banners/verify")
def verify_banner_payment(data: dict, m=Depends(get_merchant)):
    merchant_id = str(m["_id"])
    order_id    = data.get("order_id","")
    title       = data.get("title","").strip()
    image_url   = data.get("image_url","").strip()  # base64 or URL
    rzp_payment_id   = data.get("razorpay_payment_id","")
    rzp_order_id     = data.get("razorpay_order_id","")
    rzp_signature    = data.get("razorpay_signature","")

    if not title:   raise HTTPException(400, "Banner title required")
    if not image_url: raise HTTPException(400, "Banner image required")

    # Validate image size (base64 → approx bytes)
    if image_url.startswith("data:image"):
        b64_data = image_url.split(",")[-1]
        approx_bytes = len(b64_data) * 3 // 4
        if approx_bytes > 2 * 1024 * 1024:
            raise HTTPException(400, "Image exceeds 2MB limit. Please compress and re-upload.")

    try:
        order = db.banner_orders.find_one({"_id": ObjectId(order_id), "merchant_id": merchant_id, "status": "pending"})
    except Exception:
        raise HTTPException(400, "Invalid order")
    if not order: raise HTTPException(404, "Order not found")

    # Verify Razorpay signature if online payment
    if rzp_payment_id and rzp_order_id:
        import hmac, hashlib
        doc = db.pricing.find_one({}) or {}
        secret = doc.get("razorpay_key_secret") or os.environ.get("RAZORPAY_KEY_SECRET","")
        sig_body = f"{rzp_order_id}|{rzp_payment_id}"
        expected = hmac.new(secret.encode(), sig_body.encode(), hashlib.sha256).hexdigest()
        if expected != rzp_signature:
            raise HTTPException(400, "Payment signature invalid")

    now = datetime.utcnow()
    invoice_no = _gen_invoice_no("BNR")

    # Create banner
    banner_id = db.merchant_banners.insert_one({
        "merchant_id":     merchant_id,
        "merchant_name":   m.get("name",""),
        "title":           title,
        "image_url":       image_url,
        "duration":        order["days"],
        "plan":            order["plan"],
        "start_date":      order["from_date"].strftime("%d %b %Y"),
        "end_date":        order["end_date"].strftime("%d %b %Y"),
        "approval_status": "pending_approval",
        "base_price":      order["base_price"],
        "gst_amount":      order["gst_amount"],
        "total":           order["total"],
        "invoice_no":      invoice_no,
        "razorpay_payment_id": rzp_payment_id,
        "created_at":      now,
    }).inserted_id

    # Mark order paid
    db.banner_orders.update_one({"_id": order["_id"]}, {"$set": {"status":"paid","paid_at":now}})

    # Save invoice
    db.invoices.insert_one({
        "invoice_no":    invoice_no,
        "merchant_id":   merchant_id,
        "merchant_name": m.get("name",""),
        "merchant_phone": m.get("phone",""),
        "type":          "banner",
        "item_label":    f"Banner – {order['days']} Days",
        "banner_id":     str(banner_id),
        "base_price":    order["base_price"],
        "gst":           order["gst_amount"],
        "total":         order["total"],
        "from_date":     order["from_date"],
        "end_date":      order["end_date"],
        "created_at":    now,
    })

    _log_tx(merchant_id,"banner", f"Banner created: {title}",
            amount=order["total"], meta={"banner_id":str(banner_id),"invoice":invoice_no})

    return {"ok":True, "banner_id":str(banner_id), "invoice_no":invoice_no,
            "message":"Banner submitted for admin approval."}

# ─── Manual/free banner activation ───
@router.post("/banners/activate-free")
def activate_free_banner(data: dict, m=Depends(get_merchant)):
    merchant_id = str(m["_id"])
    order_id    = data.get("order_id","")
    title       = data.get("title","").strip()
    image_url   = data.get("image_url","").strip()
    if not title or not image_url: raise HTTPException(400, "Title and image required")
    try:
        order = db.banner_orders.find_one({"_id": ObjectId(order_id), "merchant_id": merchant_id})
    except Exception:
        raise HTTPException(400, "Invalid order")
    if not order: raise HTTPException(404, "Order not found")

    now = datetime.utcnow()
    invoice_no = _gen_invoice_no("BNR-FREE")
    banner_id = db.merchant_banners.insert_one({
        "merchant_id":     merchant_id,
        "merchant_name":   m.get("name",""),
        "title":           title, "image_url": image_url,
        "duration":        order.get("days",30), "plan": order.get("plan","30days"),
        "start_date":      order["from_date"].strftime("%d %b %Y") if isinstance(order.get("from_date"),datetime) else "",
        "end_date":        order["end_date"].strftime("%d %b %Y") if isinstance(order.get("end_date"),datetime) else "",
        "approval_status": "pending_approval",
        "base_price":0,"gst_amount":0,"total":0,"invoice_no":invoice_no,"created_at":now,
    }).inserted_id
    db.banner_orders.update_one({"_id":order["_id"]},{"$set":{"status":"paid","paid_at":now}})
    return {"ok":True,"banner_id":str(banner_id),"invoice_no":invoice_no,"message":"Banner submitted for admin approval."}


# ═══════════════════════════════════════════════════════════
# MERCHANT VOUCHERS — paid voucher cards in Voucher Zone
# ═══════════════════════════════════════════════════════════

@router.get("/vouchers")
def my_vouchers(m=Depends(get_merchant)):
    result = []
    for v in db.merchant_vouchers.find({"merchant_id": str(m["_id"])}).sort("created_at", -1):
        result.append({
            "_id":        str(v["_id"]),
            "title":      v.get("title",""),
            "offer_text": v.get("offer_text",""),
            "logo_url":   v.get("logo_url",""),
            "validity":   v.get("validity","30 days"),
            "duration":   v.get("duration",30),
            "status":     v.get("approval_status","pending_approval"),
            "invoice_no": v.get("invoice_no",""),
            "amount":     v.get("total",0),
            "created_at": v["created_at"].strftime("%d %b %Y %H:%M") if v.get("created_at") else "",
        })
    return result

@router.get("/vouchers/pricing")
def voucher_pricing(m=Depends(get_merchant)):
    p = _get_voucher_pricing()
    return {
        "plans": [
            {"id":"30days","label":"30 Days","price":p["price_30"],"gst_pct":p["gst"]},
            {"id":"60days","label":"60 Days","price":p["price_60"],"gst_pct":p["gst"]},
            {"id":"90days","label":"90 Days","price":p["price_90"],"gst_pct":p["gst"]},
        ],
        "max_size_mb": 1,
        "recommended_px": "400 x 400",
    }

@router.post("/vouchers/order")
def initiate_voucher_order(data: dict, m=Depends(get_merchant)):
    merchant_id = str(m["_id"])
    if not _has_eligible_store(merchant_id):
        raise HTTPException(403, "You need an active or previously subscribed store to create vouchers.")

    plan = data.get("plan","30days")
    p = _get_voucher_pricing()
    gst_pct = p["gst"]
    plan_map = {"30days":(30,p["price_30"]),"60days":(60,p["price_60"]),"90days":(90,p["price_90"])}
    days, base_price = plan_map.get(plan, (30, p["price_30"]))
    gst_amt, total = _compute_gst(base_price, gst_pct)

    from_date_str = data.get("from_date", datetime.utcnow().strftime("%Y-%m-%d"))
    try: from_dt = datetime.strptime(from_date_str, "%Y-%m-%d")
    except Exception: from_dt = datetime.utcnow()
    end_dt = from_dt + timedelta(days=days)

    sub_id = db.voucher_orders.insert_one({
        "merchant_id": merchant_id,"plan":plan,"days":days,
        "base_price":base_price,"gst_percent":gst_pct,"gst_amount":gst_amt,
        "total":total,"from_date":from_dt,"end_date":end_dt,
        "status":"pending","created_at":datetime.utcnow(),
    }).inserted_id

    doc = db.pricing.find_one({}) or {}
    rzp_key = doc.get("razorpay_key_id") or os.environ.get("RAZORPAY_KEY_ID","")
    rzp_secret = doc.get("razorpay_key_secret") or os.environ.get("RAZORPAY_KEY_SECRET","")
    amount_paise = int(round(total * 100))
    pay_mode = "manual"; rzp_order_id = None

    if rzp_key and rzp_secret and amount_paise > 0:
        try:
            resp = _razorpay_request("POST","/orders",(rzp_key,rzp_secret),{
                "amount":amount_paise,"currency":"INR",
                "receipt":f"vouch_{str(sub_id)[-8:]}",
                "notes":{"merchant_id":merchant_id,"plan":plan}
            })
            rzp_order_id = resp.get("id"); pay_mode = "razorpay"
        except Exception as e: print(f"[Razorpay] Voucher order error: {e}")

    return {
        "order_id":str(sub_id),"plan":plan,"plan_label":f"{days} Days",
        "base_price":base_price,"gst_percent":gst_pct,"gst_amount":gst_amt,
        "amount_display":total,"from_date":from_dt.strftime("%d %b %Y"),"end_date":end_dt.strftime("%d %b %Y"),
        "razorpay_order_id":rzp_order_id,"razorpay_key":rzp_key,
        "pay_mode":pay_mode,"amount_paise":amount_paise,
    }

@router.post("/vouchers/verify")
def verify_voucher_payment(data: dict, m=Depends(get_merchant)):
    merchant_id = str(m["_id"])
    order_id    = data.get("order_id","")
    title       = data.get("title","").strip()
    offer_text  = data.get("offer_text","").strip()
    logo_url    = data.get("logo_url","").strip()
    validity    = data.get("validity","30 days").strip()
    rzp_payment_id  = data.get("razorpay_payment_id","")
    rzp_order_id    = data.get("razorpay_order_id","")
    rzp_signature   = data.get("razorpay_signature","")

    if not title: raise HTTPException(400, "Voucher title required")

    if logo_url.startswith("data:image"):
        b64_data = logo_url.split(",")[-1]
        approx_bytes = len(b64_data) * 3 // 4
        if approx_bytes > 1 * 1024 * 1024:
            raise HTTPException(400, "Logo image exceeds 1MB limit.")

    try:
        order = db.voucher_orders.find_one({"_id":ObjectId(order_id),"merchant_id":merchant_id,"status":"pending"})
    except Exception: raise HTTPException(400,"Invalid order")
    if not order: raise HTTPException(404,"Order not found")

    if rzp_payment_id and rzp_order_id:
        import hmac, hashlib
        doc = db.pricing.find_one({}) or {}
        secret = doc.get("razorpay_key_secret") or os.environ.get("RAZORPAY_KEY_SECRET","")
        sig_body = f"{rzp_order_id}|{rzp_payment_id}"
        expected = hmac.new(secret.encode(), sig_body.encode(), hashlib.sha256).hexdigest()
        if expected != rzp_signature: raise HTTPException(400,"Payment signature invalid")

    now = datetime.utcnow()
    invoice_no = _gen_invoice_no("VCH")

    voucher_id = db.merchant_vouchers.insert_one({
        "merchant_id":     merchant_id,"merchant_name":m.get("name",""),
        "title":title,"offer_text":offer_text,"logo_url":logo_url,"validity":validity,
        "duration":order["days"],"plan":order["plan"],
        "start_date":order["from_date"].strftime("%d %b %Y"),"end_date":order["end_date"].strftime("%d %b %Y"),
        "approval_status":"pending_approval",
        "base_price":order["base_price"],"gst_amount":order["gst_amount"],"total":order["total"],
        "invoice_no":invoice_no,"razorpay_payment_id":rzp_payment_id,"created_at":now,
    }).inserted_id

    db.voucher_orders.update_one({"_id":order["_id"]},{"$set":{"status":"paid","paid_at":now}})
    db.invoices.insert_one({
        "invoice_no":invoice_no,"merchant_id":merchant_id,
        "merchant_name":m.get("name",""),"merchant_phone":m.get("phone",""),
        "type":"voucher","item_label":f"Voucher Zone – {order['days']} Days",
        "voucher_id":str(voucher_id),"base_price":order["base_price"],
        "gst":order["gst_amount"],"total":order["total"],
        "from_date":order["from_date"],"end_date":order["end_date"],"created_at":now,
    })
    _log_tx(merchant_id,"voucher",f"Voucher created: {title}",amount=order["total"],meta={"voucher_id":str(voucher_id),"invoice":invoice_no})
    return {"ok":True,"voucher_id":str(voucher_id),"invoice_no":invoice_no,"message":"Voucher submitted for admin approval."}

@router.post("/vouchers/activate-free")
def activate_free_voucher(data: dict, m=Depends(get_merchant)):
    merchant_id = str(m["_id"])
    order_id = data.get("order_id",""); title = data.get("title","").strip()
    offer_text=data.get("offer_text",""); logo_url=data.get("logo_url",""); validity=data.get("validity","30 days")
    if not title: raise HTTPException(400,"Title required")
    try: order = db.voucher_orders.find_one({"_id":ObjectId(order_id),"merchant_id":merchant_id})
    except Exception: raise HTTPException(400,"Invalid order")
    if not order: raise HTTPException(404,"Order not found")
    now = datetime.utcnow(); invoice_no = _gen_invoice_no("VCH-FREE")
    voucher_id = db.merchant_vouchers.insert_one({
        "merchant_id":merchant_id,"merchant_name":m.get("name",""),
        "title":title,"offer_text":offer_text,"logo_url":logo_url,"validity":validity,
        "duration":order.get("days",30),"plan":order.get("plan","30days"),
        "approval_status":"pending_approval","base_price":0,"gst_amount":0,"total":0,
        "invoice_no":invoice_no,"created_at":now,
    }).inserted_id
    db.voucher_orders.update_one({"_id":order["_id"]},{"$set":{"status":"paid","paid_at":now}})
    return {"ok":True,"voucher_id":str(voucher_id),"invoice_no":invoice_no,"message":"Voucher submitted for admin approval."}

# ═══════════════════════════════════════════════════════════
# MERCHANT INVOICES — full line-item view
# ═══════════════════════════════════════════════════════════

@router.get("/invoices/full")
def my_invoices_full(m=Depends(get_merchant)):
    """Returns all invoices with type (store/banner/voucher) for line-item display."""
    result = []
    for inv in db.invoices.find({"merchant_id": str(m["_id"])}).sort("created_at", -1):
        fd = inv.get("from_date"); ed = inv.get("end_date")
        result.append({
            "invoice_no":  inv.get("invoice_no",""),
            "type":        inv.get("type","store"),
            "item_label":  inv.get("item_label") or f"Store – {inv.get('plan','')}",
            "store_name":  inv.get("store_name",""),
            "base_price":  inv.get("base_price",0),
            "gst":         inv.get("gst",0),
            "total":       inv.get("total",0),
            "from_date":   fd.strftime("%d %b %Y") if isinstance(fd,datetime) else str(fd or ""),
            "end_date":    ed.strftime("%d %b %Y") if isinstance(ed,datetime) else str(ed or ""),
            "created_at":  inv["created_at"].strftime("%d %b %Y %H:%M") if inv.get("created_at") else "",
        })
    return result
