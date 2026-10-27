"""
Merchant Product routes  —  /merchant/products/...
Collections: merchant_products, product_orders.

All free/paid decisions live in services/products.py; this module only does
HTTP, Razorpay, discount codes, invoices and response shaping.
"""
from datetime import datetime, timedelta

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from database import db
from routers.merchant_app import (
    get_merchant, _mid, _log_tx, _resolve_discount, _mark_discount_used,
    _razorpay_request, _require_valid_razorpay_payment, _require_active_store,
    _cloudinary_upload, _make_thumb_url, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET,
)
from services import products as P

router = APIRouter(prefix="/merchant", tags=["Merchant Products"])

PRODUCTS, ORDERS = P.PRODUCTS, P.ORDERS


# ── helpers ────────────────────────────────────────────────────────────────────
def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except P.ProductError as e:
        raise HTTPException(e.status_code, e.detail)


def _oid(v, what="ID"):
    try:
        return ObjectId(str(v))
    except Exception:
        raise HTTPException(400, f"Invalid {what}")


def _own(pid, m, allow_deleted=False):
    doc = db[PRODUCTS].find_one({"_id": _oid(pid, "product ID"), "merchant_id": _mid(m)})
    if not doc:
        raise HTTPException(404, "Product not found")
    if doc.get("status") == "deleted" and not allow_deleted:
        raise HTTPException(410, "This product was deleted.")
    return doc


def _dstr(d):
    return d.strftime("%d %b %Y") if hasattr(d, "strftime") else str(d or "")


def _row(p: dict) -> dict:
    expired = p.get("status") == "expired" or P.is_expired_by_date(p)
    status = "expired" if expired else p.get("status", "pending")
    return {
        "_id": str(p["_id"]), "title": p.get("title", ""),
        "offer_text": p.get("description", ""), "logo_url": p.get("image_url", ""),
        "price": str(p.get("price", "") or ""), "original_price": str(p.get("original_price", "") or ""),
        "store_id": p.get("store_id", ""), "store_name": p.get("store_name", ""), "city": p.get("city", ""),
        "is_active": bool(p.get("is_active", True)),
        "from_date": _dstr(p.get("start_date")), "end_date": _dstr(p.get("end_date")),
        "duration_days": int(p.get("duration_days") or 0) or P.inclusive_days(
            P.parse_date(p.get("start_date")) or P.ist_today(), P.parse_date(p.get("end_date")) or P.ist_today()),
        "pricing_tier_label": "Free" if p.get("pricing_tier") == "free" else
                              (f"Paid ₹{float(p.get('charged_amount') or 0):g}" if p.get("pricing_tier") == "paid" else ""),
        "approval_status": status, "status": status,
        "amount": p.get("charged_amount", 0), "pricing_tier": p.get("pricing_tier", "free"),
        "source": p.get("source", "merchant"), "is_expired": expired,
        "created_at": str(p.get("created_at", ""))[:19],
    }


def _own_store(store_id: str, m: dict, active: bool = True):
    """The store must exist, (optionally) be active and belong to THIS merchant -
    the free-product quota is counted per store, so a merchant must not be able to
    create products under, or read the quota of, someone else's store."""
    store_id = (store_id or "").strip()
    store = _require_active_store(store_id, "product") if active else None
    if store is None:
        try:
            store = db.stores.find_one({"_id": ObjectId(store_id)})
        except Exception:
            raise HTTPException(400, "Invalid store ID")
        if not store:
            raise HTTPException(404, "Store not found")
    owner = str(store.get("merchant_id") or "")
    if owner and owner != _mid(m):
        raise HTTPException(403, "This store does not belong to your account.")
    return store


def _fields(data: dict, m: dict) -> dict:
    store_id = (data.get("store_id") or "").strip()
    store = _own_store(store_id, m)
    price = str(data.get("price", "") or "").strip()
    orig = str(data.get("original_price", "") or "").strip()
    label = ""
    try:
        if price and orig and float(orig) > float(price) > 0:
            label = f"{round((float(orig) - float(price)) / float(orig) * 100)}% OFF"
    except Exception:
        pass
    img = _cloudinary_upload(data.get("logo_url", ""), folder="offro/products")
    if not str(img or "").strip():
        raise HTTPException(400, "Please add an image before saving.")
    title = (data.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Product title is required")
    return {
        "store_id": store_id, "store_name": (data.get("store_name") or store.get("store_name") or "").strip(),
        "city": (data.get("city") or store.get("city") or "").strip(),
        "title": title, "description": data.get("offer_text", ""),
        "image_url": img, "image_thumb": _make_thumb_url(img),
        "price": price, "original_price": orig, "discount_label": label,
    }


def _price_block(tier_decision: dict, discount_code):
    """Price for a PAID action from the service's decision (+ optional code)."""
    base = tier_decision["unit_price"]
    gst_pct = tier_decision["gst_percent"]
    disc = _resolve_discount(discount_code if base > 0 else None, "PRODUCTS", base)
    d_amt = disc["discount_amount"]
    sub = round(max(0.0, base - d_amt), 2)
    gst = round(sub * gst_pct / 100, 2)
    total = round(sub + gst, 2)
    return {"disc": disc, "base": base, "discount_amount": d_amt, "sub": sub,
            "gst_pct": gst_pct, "gst": gst, "total": total, "paise": int(round(total * 100))}


def _razorpay_order(paise, receipt, notes=None):
    if paise <= 0:
        return None, "manual"
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        return None, "manual"
    try:
        body = {"amount": paise, "currency": "INR", "receipt": receipt}
        if notes:
            body["notes"] = notes
        rp = _razorpay_request("POST", "/v1/orders", auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), json_data=body)
        rid = (rp.json() if hasattr(rp, "json") else {}).get("id")
        return rid, ("razorpay" if rid else "manual")
    except Exception:
        return None, "manual"


def _invoice(m, prod, order, paid: bool, rp_payment_id=""):
    no = f"PRD-{'' if paid else 'FREE-'}{datetime.utcnow().strftime('%Y%m%d')}-{str(prod['_id'])[-6:].upper()}"
    db.invoices.insert_one({
        "invoice_no": no, "type": "product", "merchant_id": _mid(m),
        "merchant_name": m.get("name"), "merchant_phone": str(m.get("phone", "")),
        "item_label": "Discover Product", "store_name": prod.get("title", ""),
        "plan": f"{_dstr(prod.get('start_date'))} → {_dstr(prod.get('end_date'))}",
        "base_price": order.get("base_price", 0), "original_amount": order.get("base_price", 0),
        "discount_code": order.get("discount_code") or "", "discount_type": order.get("discount_type"),
        "discount_scope": order.get("discount_scope"), "discount_value": order.get("discount_value", 0),
        "discount_amount": order.get("discount_amount", 0), "final_amount": order.get("final_amount", 0),
        "gst": order.get("gst_amount", 0), "gst_percent": order.get("gst_percent", 0),
        "total": order.get("total", 0), "razorpay_payment_id": rp_payment_id,
        "payment_status": "paid" if paid else "free", "created_at": datetime.utcnow(),
    })
    db[PRODUCTS].update_one({"_id": prod["_id"]}, {"$set": {"invoice_no": no}})
    return no


def _slot_payload(store_id, exclude_id=None, days=0):
    """Quota of ONE store."""
    s = _guard(P.slots_summary, store_id, exclude_id, days)
    st = P.get_settings()
    return {**s, "scope": "store", "store_id": str(store_id), "per_store_limit": s["free_limit"],
            "gst_pct": s["gst_percent"], "max_validity_days": st["product_max_validity_days"]}


def _merchant_overview(m, days=0):
    """No store chosen yet: one summary per active store of the merchant plus a combined
    total for the Products home banner. Each store still has its OWN quota - the combined
    numbers are display only."""
    mid = _mid(m)
    owners = [mid]
    try:
        owners.append(ObjectId(mid))
    except Exception:
        pass
    stores = list(db.stores.find({"merchant_id": {"$in": owners}, "status": "active"}))
    per = []
    for st in stores:
        sid = str(st["_id"])
        sm = _guard(P.slots_summary, sid, None, days)
        per.append({"store_id": sid, "store_name": st.get("store_name", ""), "used": sm["used"],
                    "free_limit": sm["free_limit"], "free_remaining": sm["free_remaining"],
                    "next_is_free": sm["next_is_free"]})
    base = _guard(P.slots_summary, "", None, days)       # settings only (price/gst), no store
    stg = P.get_settings()
    n = max(1, len(per))
    limit = stg["free_product_limit"]
    used = sum(x["used"] for x in per)
    nxt = True if not per else any(x["next_is_free"] for x in per)
    return {**base, "scope": "merchant", "stores": per, "per_store_limit": limit,
            "free_limit": limit * n, "used": used, "free_remaining": sum(x["free_remaining"] for x in per) if per else limit,
            "next_is_free": nxt, "tier": "free" if nxt else "paid",
            "gst_pct": stg["gst_percent"], "max_validity_days": stg["product_max_validity_days"]}


# ── list / slots ───────────────────────────────────────────────────────────────
@router.get("/products")
def list_products(m=Depends(get_merchant)):
    mid = _mid(m)
    P.expire_due(mid)
    docs = db[PRODUCTS].find({"merchant_id": mid, "status": {"$ne": "deleted"}}).sort("created_at", -1)
    return [_row(p) for p in docs]


@router.get("/products/slots")
def product_slots(exclude_id: str = "", store_id: str = "", days: int = 0, m=Depends(get_merchant)):
    """'N / limit free product slots used' + the current price per day (x days = price).
    The quota is PER STORE: pass `store_id` for that store's numbers, or `exclude_id`
    (renewal preview - the store is taken from that product). With neither, a combined
    overview of the merchant's stores is returned. Display only — the decision is
    re-made server-side on save / renew."""
    days = max(0, int(days or 0))
    if exclude_id:
        prod = _own(exclude_id, m)
        return _slot_payload(prod.get("store_id"), exclude_id, days)
    if (store_id or "").strip():
        _own_store(store_id, m, active=False)
        return _slot_payload(store_id.strip(), None, days)
    return _merchant_overview(m, days)


# ── create ─────────────────────────────────────────────────────────────────────
@router.post("/products/order")
def create_product_order(data: dict, m=Depends(get_merchant)):
    mid = _mid(m)
    store_id = (data.get("store_id") or "").strip()
    _own_store(store_id, m)                    # required: the free quota is counted per store
    start, end, days = _guard(P.validate_period, data.get("from_date"), data.get("end_date"))
    d = _guard(P.decide, store_id, None, days)
    pb = _price_block(d, data.get("discount_code") if d["tier"] == "paid" else None)
    disc = pb["disc"]
    order = {
        "kind": "create", "merchant_id": mid, "store_id": store_id, "merchant_phone": str(m.get("phone", "")),
        "merchant_name": str(m.get("name", "")), "pricing_tier": d["tier"], "unit_price": d["unit_price"],
        "from_date": start, "end_date": end, "days": days, "price_per_day": d["price_per_day"],
        "base_price": pb["base"], "discount_code": disc["code"] or None, "discount_type": disc["type"],
        "discount_scope": disc["applies_to"], "discount_value": disc["discount_value"],
        "discount_amount": pb["discount_amount"], "final_amount": pb["sub"],
        "gst_percent": pb["gst_pct"], "gst_amount": pb["gst"], "total": pb["total"],
        "amount_paise": pb["paise"], "status": "created", "created_at": datetime.utcnow(),
    }
    oid = db[ORDERS].insert_one(order).inserted_id
    rp_id, pay_mode = _razorpay_order(pb["paise"], f"prd_{str(oid)[:8]}")
    db[ORDERS].update_one({"_id": oid}, {"$set": {"razorpay_order_id": rp_id, "pay_mode": pay_mode}})
    return {
        "order_id": str(oid), "plan_label": "Free product" if d["tier"] == "free" else "Additional product",
        "pricing_tier": d["tier"], "store_id": store_id, "from_date": _dstr(start), "end_date": _dstr(end),
        "days": days, "price_per_day": d["price_per_day"], "base_price": pb["base"], "gst_percent": pb["gst_pct"],
        "gst_amount": pb["gst"], "discount_code": disc["code"], "discount_type": disc["type"],
        "discount_value": disc["discount_value"], "discount_amount": pb["discount_amount"],
        "discount_msg": disc["message"], "amount_display": pb["total"], "amount_paise": pb["paise"],
        "pay_mode": pay_mode, "razorpay_key": RAZORPAY_KEY_ID, "razorpay_order_id": rp_id,
        "free_limit": d["free_limit"], "free_remaining": max(0, d["free_limit"] - d["used"]),
    }


def _order(data, m, kind="create"):
    o = db[ORDERS].find_one({"_id": _oid(data.get("order_id", ""), "order ID"),
                             "merchant_id": _mid(m), "kind": kind})
    if not o:
        raise HTTPException(404, "Order not found")
    return o


def _order_price(order: dict) -> dict:
    """The price the order QUOTED - recorded on the product so a later rate change never rewrites it."""
    return {"price_per_day": order.get("price_per_day", 0), "unit_price": order.get("unit_price", 0),
            "gst_percent": order.get("gst_percent", 0), "gst_amount": order.get("gst_amount", 0)}


def _same_store(order: dict, fields: dict):
    """The order was quoted for one store's quota; it can only create a product in that store."""
    if order.get("store_id") and order["store_id"] != fields.get("store_id"):
        raise HTTPException(400, "This order was created for a different store. Please start again.")


@router.post("/products/activate-free")
def activate_free_product(data: dict, m=Depends(get_merchant)):
    """Submit a product that needs no payment (free slot, or fully discounted)."""
    order = _order(data, m)
    if order.get("status") in ("submitted", "paid"):                   # idempotent retry
        return {"message": "Product already activated.", "product_id": order.get("product_id", ""),
                "invoice_no": order.get("invoice_no", "")}
    if float(order.get("total", 0) or 0) > 0:
        raise HTTPException(400, "Payment is required for this product.")
    fields = _fields(data, m)
    _same_store(order, fields)
    if order["pricing_tier"] == "free":
        prod = _guard(P.create_free, merchant=m, fields=fields, end_raw=order["end_date"],
                      from_raw=order.get("from_date"))
    else:   # paid tier, discounted to 0
        prod = _guard(P.create_paid, merchant=m, fields=fields, end_raw=order["end_date"],
                      from_raw=order.get("from_date"), source="merchant", paid_total=0.0,
                      payment={"payment_status": "free_after_discount"}, price=_order_price(order))
    _mark_discount_used(order.get("discount_code"))
    inv = _invoice(m, prod, order, paid=False)
    db[ORDERS].update_one({"_id": order["_id"]}, {"$set": {"status": "submitted", "product_id": str(prod["_id"]),
                                                          "invoice_no": inv}})
    _log_tx(_mid(m), "product", f"Free product '{prod['title']}'", amount=0, meta={"product_id": str(prod["_id"])})
    return {"message": "Product submitted for review", "product_id": str(prod["_id"]), "invoice_no": inv}


@router.post("/products/verify")
def verify_product_payment(data: dict, m=Depends(get_merchant)):
    """Verify the Razorpay payment for an additional (paid) product, then create it."""
    order = _order(data, m)
    if order.get("status") == "paid":
        return {"message": "Payment already verified.", "product_id": order.get("product_id", ""),
                "invoice_no": order.get("invoice_no", "")}
    if float(order.get("total", 0) or 0) <= 0:
        raise HTTPException(400, "This product does not need payment.")
    rp_oid, rp_pid, sig = (data.get("razorpay_order_id", ""), data.get("razorpay_payment_id", ""),
                           data.get("razorpay_signature", ""))
    _require_valid_razorpay_payment(order, rp_oid, rp_pid, sig)
    fields = _fields(data, m)
    _same_store(order, fields)
    prod = _guard(P.create_paid, merchant=m, fields=fields, end_raw=order["end_date"],
                  from_raw=order.get("from_date"), source="merchant", price=_order_price(order),
                  paid_total=float(order["total"]),
                  payment={"razorpay_payment_id": rp_pid, "razorpay_order_id": rp_oid, "payment_status": "paid",
                           "discount_code": order.get("discount_code") or "",
                           "discount_amount": order.get("discount_amount", 0)})
    _mark_discount_used(order.get("discount_code"))
    inv = _invoice(m, prod, order, paid=True, rp_payment_id=rp_pid)
    db[ORDERS].update_one({"_id": order["_id"]}, {"$set": {"status": "paid", "product_id": str(prod["_id"]),
                                                          "invoice_no": inv}})
    _log_tx(_mid(m), "product", f"Product '{prod['title']}'", amount=order["total"],
            meta={"product_id": str(prod["_id"]), "invoice_no": inv})
    return {"message": "Payment verified. Product pending admin approval.",
            "product_id": str(prod["_id"]), "invoice_no": inv}


# ── edit / delete / availability ───────────────────────────────────────────────
@router.put("/products/{pid}")
def update_product(pid: str, data: dict, m=Depends(get_merchant)):
    doc = _own(pid, m)
    upd = {}
    if "title" in data: upd["title"] = str(data["title"]).strip()
    if "offer_text" in data: upd["description"] = data["offer_text"]
    if "price" in data: upd["price"] = str(data["price"] or "")
    if "original_price" in data: upd["original_price"] = str(data["original_price"] or "")
    if upd:
        upd["updated_at"] = datetime.utcnow()
        db[PRODUCTS].update_one({"_id": doc["_id"]}, {"$set": upd})
    return {"ok": True}


@router.delete("/products/{pid}")
def delete_product(pid: str, m=Depends(get_merchant)):
    """Soft delete (status=deleted). No refund. The slot is freed because deleted
    products no longer qualify in the count."""
    doc = _own(pid, m, allow_deleted=True)
    _guard(P.soft_delete, doc, "merchant")
    return {"ok": True, "deleted": True, "refund": False}


@router.put("/products/{pid}/availability")
def toggle_availability(pid: str, data: dict, m=Depends(get_merchant)):
    doc = _own(pid, m)
    available = bool(data.get("available", True))
    if available and (doc.get("status") == "expired" or P.is_expired_by_date(doc)):
        raise HTTPException(400, "This product has expired. Renew it to make it live again.")
    db[PRODUCTS].update_one({"_id": doc["_id"]}, {"$set": {"is_active": available, "updated_at": datetime.utcnow()}})
    return {"ok": True, "available": available}


@router.get("/products/search")
def search_products(q: str = "", status: str = "", store_id: str = "", m=Depends(get_merchant)):
    flt = {"merchant_id": _mid(m), "status": {"$ne": "deleted"}}
    if store_id: flt["store_id"] = store_id
    if status: flt["status"] = status
    out = []
    for p in db[PRODUCTS].find(flt).limit(200):
        hay = f"{p.get('title','')} {p.get('description','')}".lower()
        if q and q.lower() not in hay:
            continue
        out.append(_row(p))
    return out


@router.post("/products/bulk-action")
def bulk_action(data: dict, m=Depends(get_merchant)):
    mid = _mid(m)
    ids, action = [i for i in data.get("ids", []) if i], data.get("action", "")
    if not ids or action not in ("activate", "deactivate", "delete"):
        raise HTTPException(400, "ids and valid action required")
    oids = [_oid(i, "product ID") for i in ids]
    n = 0
    for d in db[PRODUCTS].find({"_id": {"$in": oids}, "merchant_id": mid, "status": {"$ne": "deleted"}}):
        if action == "delete":
            P.soft_delete(d, "merchant"); n += 1
        elif action == "activate":
            if d.get("status") == "expired" or P.is_expired_by_date(d):
                continue                                  # expired → must be renewed
            n += db[PRODUCTS].update_one({"_id": d["_id"]}, {"$set": {"is_active": True}}).modified_count
        else:
            n += db[PRODUCTS].update_one({"_id": d["_id"]}, {"$set": {"is_active": False}}).modified_count
    return {"ok": True, "affected": n, "action": action}


# ── renewal ────────────────────────────────────────────────────────────────────
@router.post("/products/{pid}/renew")
def renew_order(pid: str, data: dict, m=Depends(get_merchant)):
    """Re-checks free/paid NOW through the same service decision; the product
    itself is excluded from the count."""
    mid = _mid(m)
    prod = _own(pid, m)
    end, days = _guard(P.renewal_period, prod, data.get("end_date"))
    d = _guard(P.renew_decision, prod, days)
    pb = _price_block(d, data.get("discount_code") if d["tier"] == "paid" else None)
    disc = pb["disc"]
    order = {
        "kind": "renew", "product_id": pid, "merchant_id": mid, "store_id": prod.get("store_id", ""), "pricing_tier": d["tier"],
        "unit_price": d["unit_price"], "end_date": end, "days": days, "price_per_day": d["price_per_day"],
        "base_price": pb["base"],
        "discount_code": disc["code"] or None, "discount_type": disc["type"],
        "discount_scope": disc["applies_to"], "discount_value": disc["discount_value"],
        "discount_amount": pb["discount_amount"], "final_amount": pb["sub"],
        "gst_percent": pb["gst_pct"], "gst_amount": pb["gst"], "total": pb["total"],
        "amount_paise": pb["paise"], "status": "created", "created_at": datetime.utcnow(),
    }
    oid = db[ORDERS].insert_one(order).inserted_id
    rp_id, pay_mode = _razorpay_order(pb["paise"], f"ren_{pid[:8]}", {"product_id": pid, "type": "renew"})
    db[ORDERS].update_one({"_id": oid}, {"$set": {"razorpay_order_id": rp_id, "pay_mode": pay_mode}})
    return {"order_id": rp_id or "", "product_order_id": str(oid), "amount": pb["total"], "currency": "INR",
            "key": RAZORPAY_KEY_ID, "gst": pb["gst"], "end_date": _dstr(end), "days": days, "price_per_day": d["price_per_day"], "pricing_tier": d["tier"],
            "free_remaining": max(0, d["free_limit"] - d["used"]), "base_price": pb["base"],
            "gst_percent": pb["gst_pct"], "gst_amount": pb["gst"], "discount_code": disc["code"],
            "discount_type": disc["type"], "discount_value": disc["discount_value"],
            "discount_amount": pb["discount_amount"], "discount_msg": disc["message"],
            "amount_display": pb["total"], "amount_paise": pb["paise"], "pay_mode": pay_mode,
            "razorpay_key": RAZORPAY_KEY_ID, "razorpay_order_id": rp_id}


@router.post("/products/{pid}/renew/verify")
def verify_renewal(pid: str, data: dict, m=Depends(get_merchant)):
    mid = _mid(m)
    prod = _own(pid, m)
    rp_oid, rp_pid, sig = (data.get("razorpay_order_id", ""), data.get("razorpay_payment_id", ""),
                           data.get("razorpay_signature", ""))
    q = {"kind": "renew", "product_id": pid, "merchant_id": mid}
    order = db[ORDERS].find_one({**q, "razorpay_order_id": rp_oid}) if rp_oid else None
    order = order or db[ORDERS].find_one({**q, "status": "created"}, sort=[("created_at", -1)])
    if not order:
        raise HTTPException(404, "Renewal order not found")
    if order.get("status") == "paid":
        return {"ok": True, "message": "Product already renewed."}
    _require_valid_razorpay_payment(order, rp_oid, rp_pid, sig)
    total = float(order.get("total", 0) or 0)
    payment = {"razorpay_renewal_order_id": rp_oid, "last_renewal_payment_id": rp_pid,
               "discount_code": order.get("discount_code") or ""} if total > 0 else None
    # Free renewals are re-validated inside apply_renewal (lock + fresh decision).
    new = _guard(P.apply_renewal, prod, order["end_date"], charged_amount=total, payment=payment,
                 price=_order_price(order))
    db[ORDERS].update_one({"_id": order["_id"]}, {"$set": {"status": "paid", "payment_id": rp_pid,
                                                          "paid_at": datetime.utcnow()}})
    _mark_discount_used(order.get("discount_code"))
    _log_tx(mid, "product_renew", f"Product '{prod.get('title','')}' renewed", amount=total,
            meta={"product_id": pid})
    return {"ok": True, "new_end_date": _dstr(new["end_date"]),
            "message": f"Product renewed until {_dstr(new['end_date'])}."}


# ── expiry / analytics / history ───────────────────────────────────────────────
@router.get("/products/expiry-check")
def expiry_check(m=Depends(get_merchant)):
    today, soon = P.ist_today(), P.ist_today() + timedelta(days=7)
    docs = db[PRODUCTS].find({"merchant_id": _mid(m), "status": "approved",
                              "end_date": {"$gte": today, "$lte": soon}}).limit(50)
    return [{"id": str(d["_id"]), "title": d.get("title", ""), "end_date": _dstr(d["end_date"]),
             "days_left": max(0, (d["end_date"] - today).days)} for d in docs]


@router.get("/products/{pid}/analytics")
def product_analytics(pid: str, m=Depends(get_merchant)):
    _own(pid, m)
    mid = _mid(m)
    pipeline = [{"$match": {"product_id": pid, "merchant_id": {"$in": [mid, "", None]}}},
                {"$group": {"_id": "$event", "count": {"$sum": 1}, "last": {"$max": "$created_at"}}}]
    counts, last = {"view": 0, "share": 0, "open": 0}, None
    for row in db.product_events.aggregate(pipeline):
        if row.get("_id") in counts:
            counts[row["_id"]] = int(row.get("count", 0))
        ts = row.get("last")
        if isinstance(ts, datetime) and (last is None or ts > last):
            last = ts
    iso = last.strftime("%Y-%m-%dT%H:%M:%SZ") if last else ""
    return {"product_id": pid, "views": counts["view"], "shares": counts["share"], "opens": counts["open"],
            **counts, "total": sum(counts.values()), "last_viewed": iso, "last_seen": iso}


@router.get("/products/{pid}/history")
def product_history(pid: str, m=Depends(get_merchant)):
    _own(pid, m, allow_deleted=True)
    logs = list(db.product_history.find({"product_id": pid, "merchant_id": _mid(m)}, {"_id": 0})
                .sort("created_at", -1).limit(50))
    for log in logs:
        if "created_at" in log:
            log["created_at"] = str(log["created_at"])[:19]
    return logs
