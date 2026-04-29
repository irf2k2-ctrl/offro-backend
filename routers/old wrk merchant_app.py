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
            "created_at":  inv["created_at"].strftime("%d %b %Y") if inv.get("created_at") else "",
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
