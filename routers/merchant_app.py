"""
Merchant App Router — self-service portal
Place at: routers/merchant_app.py
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from database import db
from bson import ObjectId
from datetime import datetime, timedelta
import uuid, qrcode, io, base64, hmac, hashlib, os, requests

router = APIRouter(tags=["MerchantApp"])

RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID",     "rzp_live_SdiI6kcuZzZjsl")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "3JzhKnKuGkhCrelaUgCaFfQr")

# ───────────── helpers ─────────────

def _qr(store_id: str) -> str:
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(f"offro://redeem?store_id={store_id}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="#3E5F55", back_color="white")
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode()

def get_merchant(request: Request):
    token = (request.cookies.get("merchant_token") or
             request.headers.get("Authorization", "").replace("Bearer ", ""))
    if not token: raise HTTPException(401, "Not authenticated")
    m = db.merchants.find_one({"token": token})
    if not m: raise HTTPException(403, "Invalid session")
    return m

def plan_days(plan: str) -> int:
    return {"1month": 30, "3months": 90, "6months": 180, "12months": 365}.get(plan, 30)

def _log_tx(merchant_id: str, tx_type: str, description: str, amount: float = 0, meta: dict = None):
    db.merchant_transactions.insert_one({
        "merchant_id": merchant_id,
        "type": tx_type,
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

@router.put("/profile")
def update_merchant_profile(data: dict, m=Depends(get_merchant)):
    allowed = ["name", "city", "area", "phone"]
    upd = {f: data[f] for f in allowed if data.get(f) is not None}
    if upd:
        db.merchants.update_one({"_id": m["_id"]}, {"$set": upd})
    return {"message": "Profile updated"}

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
        result.append({
            "_id": str(s["_id"]),
            "store_name":        s.get("store_name"),
            "category":          s.get("category", ""),
            "city":              s.get("city", ""),
            "area":              s.get("area", ""),
            "address":           s.get("address", ""),
            "phone":             s.get("phone", ""),
            "status":            s.get("status", "draft"),
            "subscription_end":  sub_end_str,
            "subscription_plan": s.get("subscription_plan", ""),
            "visit_points":      s.get("points_per_scan", 10),
            "is_new_in_town":    s.get("is_new_in_town", False),
            "qr_code":           s.get("qr_code", ""),
            "image":             s.get("image") or "",
        })
    return result

@router.post("/stores")
def create_merchant_store(data: dict, m=Depends(get_merchant)):
    store_name = data.get("store_name", "").strip()
    if not store_name: raise HTTPException(400, "Store name required")
    store = {
        "merchant_id":    str(m["_id"]),
        "merchant_name":  m.get("name"),
        "store_name":     store_name,
        "category":       data.get("category", ""),
        "city":           data.get("city") or m.get("city", ""),
        "area":           data.get("area") or m.get("area", ""),
        "address":        data.get("address", ""),
        "phone":          data.get("phone") or m.get("phone", ""),
        "status":         "draft",
        "points_per_scan": 10,
        "lat":            data.get("lat", ""),
        "lng":            data.get("lng", ""),
        "image":          data.get("image") or None,
        "is_new_in_town": False,
        "created_at":     datetime.utcnow(),
    }
    result = db.stores.insert_one(store)
    sid = str(result.inserted_id)
    qr_b64 = _qr(sid)
    db.stores.update_one({"_id": result.inserted_id}, {"$set": {"qr_code": qr_b64}})
    _log_tx(str(m["_id"]), "store_created", f"Store '{store_name}' created", meta={"store_id": sid})
    return {"store_id": sid, "qr_code": qr_b64, "message": "Store created. Subscribe to go live."}

@router.put("/stores/{sid}")
def update_merchant_store(sid: str, data: dict, m=Depends(get_merchant)):
    store = db.stores.find_one({"_id": ObjectId(sid), "merchant_id": str(m["_id"])})
    if not store: raise HTTPException(404, "Store not found")
    upd = {f: data[f] for f in ["store_name","category","city","area","address","phone","lat","lng"] if data.get(f) is not None}
    if data.get("image"): upd["image"] = data["image"]
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
        price   = p["price"]
        gst_amt = round(price * gst / 100, 2)
        total   = round(price + gst_amt, 2)
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

    # ── Apply discount code ──
    discount_code  = data.get("discount_code", "")
    discount_value = 0.0
    if discount_code:
        disc_doc = db.discounts.find_one({"code": discount_code.upper(), "active": True})
        if disc_doc:
            max_u   = disc_doc.get("max_uses", 0)
            used    = disc_doc.get("used_count", 0)
            expired = disc_doc.get("expiry_date") and datetime.utcnow() > disc_doc["expiry_date"]
            if not expired and (max_u == 0 or used < max_u):
                discount_value = float(disc_doc.get("value", 0))
                db.discounts.update_one({"_id": disc_doc["_id"]}, {"$inc": {"used_count": 1}})

    total       = max(1.0, round(total - discount_value, 2))  # min ₹1
    total_paise = int(total * 100)

    from_date = datetime.strptime(from_date_str, "%Y-%m-%d")
    end_date  = from_date + timedelta(days=plan_days(plan))

    # ── Create Razorpay order ──
    rp_order_id = None
    pay_mode    = "manual"

    try:
        rp_res = requests.post(
            "https://api.razorpay.com/v1/orders",
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
            json={
                "amount":   total_paise,
                "currency": "INR",
                "receipt":  f"OFFRO_{store_id[:8]}_{plan}",
                "notes":    {"store_id": store_id, "plan": plan, "merchant_id": str(m["_id"])}
            },
            timeout=15,
        )
        rp_data = rp_res.json()
        if "id" in rp_data:
            rp_order_id = rp_data["id"]
            pay_mode    = "razorpay"
        else:
            err_desc = rp_data.get("error", {}).get("description", str(rp_data))
            raise HTTPException(502, f"Razorpay error: {err_desc}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Payment gateway unreachable: {str(e)}")

    # ── Save pending subscription record ──
    db.subscriptions.insert_one({
        "merchant_id":    str(m["_id"]),
        "store_id":       store_id,
        "plan":           plan,
        "amount":         total,
        "from_date":      from_date,
        "end_date":       end_date,
        "status":         "pending",
        "razorpay_order_id": rp_order_id,
        "pay_mode":       pay_mode,
        "discount_code":  discount_code,
        "discount_value": discount_value,
        "created_at":     datetime.utcnow(),
    })

    return {
        "order_id":    rp_order_id,
        "amount":      total_paise,
        "currency":    "INR",
        "key_id":      RAZORPAY_KEY_ID,
        "pay_mode":    pay_mode,
        "plan":        plan,
        "total":       total,
        "store_id":    store_id,
        "from_date":   from_date_str,
        "end_date":    end_date.strftime("%Y-%m-%d"),
    }

@router.post("/subscribe/verify")
def verify_payment(data: dict, m=Depends(get_merchant)):
    order_id   = data.get("razorpay_order_id")
    payment_id = data.get("razorpay_payment_id")
    signature  = data.get("razorpay_signature")
    store_id   = data.get("store_id")
    plan       = data.get("plan")
    from_date_str = data.get("from_date")

    if not all([order_id, payment_id, signature, store_id, plan, from_date_str]):
        raise HTTPException(400, "Missing payment verification fields")

    # ── Verify HMAC signature ──
    body    = f"{order_id}|{payment_id}"
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        body.encode(),
        hashlib.sha256
    ).hexdigest()

    if expected != signature:
        raise HTTPException(400, "Payment signature invalid")

    from_date = datetime.strptime(from_date_str, "%Y-%m-%d")
    end_date  = from_date + timedelta(days=plan_days(plan))

    # ── Activate subscription ──
    db.subscriptions.update_one(
        {"razorpay_order_id": order_id},
        {"$set": {
            "status":            "paid",
            "razorpay_payment_id": payment_id,
            "razorpay_signature":  signature,
            "paid_at":           datetime.utcnow(),
        }}
    )

    # ── Update store status → waiting_approval ──
    db.stores.update_one(
        {"_id": ObjectId(store_id)},
        {"$set": {
            "status":            "waiting_approval",
            "subscription_plan": plan,
            "subscription_end":  end_date,
            "from_date":         from_date,
        }}
    )

    sub = db.subscriptions.find_one({"razorpay_order_id": order_id}) or {}
    amount = sub.get("amount", 0)
    _log_tx(str(m["_id"]), "subscription", f"Subscribed plan {plan} for store {store_id}",
            amount=amount, meta={"store_id": store_id, "plan": plan, "order_id": order_id})

    return {"message": "Payment verified. Store is under review.", "status": "waiting_approval"}

# ───────────── invoices ─────────────

@router.get("/invoices")
def get_invoices(m=Depends(get_merchant)):
    mid = str(m["_id"])
    result = []
    for sub in db.subscriptions.find({"merchant_id": mid, "status": {"$in": ["paid", "active"]}},
                                      sort=[("created_at", -1)]):
        store = db.stores.find_one({"_id": ObjectId(sub.get("store_id", ""))}) if sub.get("store_id") else None
        result.append({
            "_id":         str(sub["_id"]),
            "store_name":  store.get("store_name") if store else "Unknown",
            "plan":        sub.get("plan"),
            "amount":      sub.get("amount"),
            "status":      sub.get("status"),
            "from_date":   sub.get("from_date", "").strftime("%d %b %Y") if isinstance(sub.get("from_date"), datetime) else str(sub.get("from_date", "")),
            "end_date":    sub.get("end_date", "").strftime("%d %b %Y") if isinstance(sub.get("end_date"), datetime) else str(sub.get("end_date", "")),
            "paid_at":     sub.get("paid_at", "").strftime("%d %b %Y %H:%M") if isinstance(sub.get("paid_at"), datetime) else "",
            "order_id":    sub.get("razorpay_order_id", ""),
            "payment_id":  sub.get("razorpay_payment_id", ""),
        })
    return result

# ───────────── transactions ─────────────

@router.get("/transactions")
def get_transactions(m=Depends(get_merchant)):
    mid = str(m["_id"])
    result = []
    for tx in db.merchant_transactions.find({"merchant_id": mid}, sort=[("created_at", -1)]):
        result.append({
            "_id":         str(tx["_id"]),
            "type":        tx.get("type"),
            "description": tx.get("description"),
            "amount":      tx.get("amount", 0),
            "created_at":  tx.get("created_at", "").strftime("%d %b %Y %H:%M") if isinstance(tx.get("created_at"), datetime) else "",
        })
    return result

# ───────────── terms ─────────────

@router.get("/terms")
def get_merchant_terms():
    doc = db.terms.find_one({"type": "merchant"}) or {}
    return {"content": doc.get("content", "")}

# ───────────── deals ─────────────

@router.get("/deals")
def get_deals(m=Depends(get_merchant)):
    mid = str(m["_id"])
    store_ids = [str(s["_id"]) for s in db.stores.find({"merchant_id": mid})]
    result = []
    for d in db.deals.find({"store_id": {"$in": store_ids}}):
        result.append({
            "_id":         str(d["_id"]),
            "store_id":    d.get("store_id"),
            "title":       d.get("title"),
            "description": d.get("description", ""),
            "discount":    d.get("discount", 0),
            "category":    d.get("category", ""),
            "start_date":  d.get("start_date", "").strftime("%Y-%m-%d") if isinstance(d.get("start_date"), datetime) else str(d.get("start_date", "")),
            "end_date":    d.get("end_date", "").strftime("%Y-%m-%d") if isinstance(d.get("end_date"), datetime) else str(d.get("end_date", "")),
            "status":      d.get("status", "active"),
        })
    return result

@router.post("/deals")
def add_deal(data: dict, m=Depends(get_merchant)):
    store_id = data.get("store_id")
    if not store_id: raise HTTPException(400, "store_id required")
    store = db.stores.find_one({"_id": ObjectId(store_id), "merchant_id": str(m["_id"])})
    if not store: raise HTTPException(404, "Store not found or not yours")

    start_str = data.get("start_date", datetime.utcnow().strftime("%Y-%m-%d"))
    end_str   = data.get("end_date",   (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d"))
    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d")
        end_dt   = datetime.strptime(end_str,   "%Y-%m-%d")
    except Exception:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD")

    deal = {
        "store_id":    store_id,
        "merchant_id": str(m["_id"]),
        "title":       data.get("title", ""),
        "description": data.get("description", ""),
        "discount":    float(data.get("discount", 0)),
        "category":    data.get("category", ""),
        "start_date":  start_dt,
        "end_date":    end_dt,
        "status":      "active",
        "created_at":  datetime.utcnow(),
    }
    result = db.deals.insert_one(deal)
    db.stores.update_one({"_id": ObjectId(store_id)}, {"$set": {"has_deals": True}})
    return {"deal_id": str(result.inserted_id), "message": "Deal added"}

@router.delete("/deals/{deal_id}")
def delete_deal(deal_id: str, m=Depends(get_merchant)):
    try:
        deal = db.deals.find_one({"_id": ObjectId(deal_id), "merchant_id": str(m["_id"])})
        if not deal: raise HTTPException(404, "Deal not found")
        db.deals.delete_one({"_id": ObjectId(deal_id)})
        return {"message": "Deal deleted"}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Invalid deal ID")
