"""NOTE: sorts AFTER test_banner_media.py on purpose (that module swaps the shared fake `database` at import).
F1 subscription free activation, F2 banner free activation, F3 Razorpay-vs-manual verification (banner / product / renewal)."""
import hashlib
import hmac
import sys
from datetime import datetime, timedelta

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import routers.merchant_app as MA
import routers.merchant_products as MP
import server
from services import products as P

fake = sys.modules["database"].db
c = TestClient(server.app)
SECRET = "test_secret"
HA, HB = {"Authorization": "Bearer tokA"}, {"Authorization": "Bearer tokB"}


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    for col in ("accounts", "stores", "subscriptions", "banner_orders", "merchant_banners", "invoices", "product_orders",
                "merchant_products", "merchant_locks", "discounts", "transactions", "pricing"):
        fake[col].delete_many({})
    fake.accounts.insert_one({"token": "tokA", "merchant_id": "mA", "name": "A", "phone": "9990000001", "roles": ["merchant"]})
    fake.accounts.insert_one({"token": "tokB", "merchant_id": "mB", "name": "B", "phone": "9990000002", "roles": ["merchant"]})
    fake.pricing.insert_one({"gst_percent": 18, "free_product_limit": 0, "product_price_per_day": 10, "product_max_validity_days": 365})
    monkeypatch.setattr(MA, "RAZORPAY_KEY_SECRET", SECRET)
    monkeypatch.setattr(MP, "RAZORPAY_KEY_SECRET", SECRET)
    monkeypatch.setattr(MA, "_cloudinary_upload", lambda v, folder="": "http://img/x.png" if v else "")
    monkeypatch.setattr(MP, "_cloudinary_upload", MA._cloudinary_upload)
    monkeypatch.setattr(MA, "_make_thumb_url", lambda u: u)
    monkeypatch.setattr(MP, "_make_thumb_url", MA._make_thumb_url)
    yield


def store(mid="mA", status="waiting_approval"):
    return str(fake.stores.insert_one({"store_name": "S", "city": "Pune", "status": status, "merchant_id": mid}).inserted_id)


def sub(mid="mA", sid=None, total=0, **kw):
    doc = {"merchant_id": mid, "store_id": sid or store(mid), "plan": "monthly", "from_date": "2026-12-01", "end_date": "2026-12-31",
           "price": 100, "gst": 0 if not total else round(total - total / 1.18, 2), "total": total, "final_amount": 0 if not total else round(total / 1.18, 2),
           "status": "pending", "discount_code": "", **kw}
    return str(fake.subscriptions.insert_one(doc).inserted_id), doc["store_id"]


def activate_sub(sub_id, sid, h=HA):
    return c.post("/merchant/subscribe/free", json={"subscription_id": sub_id, "store_id": sid}, headers=h)


# ─────────────────────────── F1 ───────────────────────────
@pytest.mark.parametrize("discount_code", ["", "FREE100"])          # a zero-value plan and a 100 % discount both end at exactly zero
def test_f1_zero_value_subscription_activates(discount_code):
    sid_, st = sub(total=0, discount_code=discount_code, discount_amount=100 if discount_code else 0)
    r = activate_sub(sid_, st)
    assert r.status_code == 200, r.text
    d = fake.subscriptions.find_one({"_id": ObjectId(sid_)})
    assert d["status"] == "paid" and d["free_activation"] is True
    assert fake.stores.find_one({"_id": ObjectId(st)})["status"] == "waiting_approval"
    inv = list(fake.invoices.find({"store_id": st}))
    assert len(inv) == 1 and inv[0]["total"] == 0 and inv[0]["merchant_id"] == "mA"


@pytest.mark.parametrize("total", [0.01, 1, 588.82, 11.8])
def test_f1_non_zero_subscription_rejected_and_untouched(total):
    sid_, st = sub(total=total)
    r = activate_sub(sid_, st)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "payment_required"
    assert fake.subscriptions.find_one({"_id": ObjectId(sid_)})["status"] == "pending"
    assert fake.invoices.count_documents({}) == 0
    assert fake.stores.find_one({"_id": ObjectId(st)}).get("subscription_plan") is None


def test_f1_missing_total_is_not_free():
    sid_, st = sub(total=0)
    fake.subscriptions.update_one({"_id": ObjectId(sid_)}, {"$unset": {"total": ""}})
    assert activate_sub(sid_, st).status_code == 400
    assert fake.invoices.count_documents({}) == 0


def test_f1_other_merchants_subscription_rejected():
    sid_, st = sub(mid="mA", total=0)
    r = activate_sub(sid_, st, HB)                                   # merchant B tries to activate merchant A's zero subscription
    assert r.status_code == 404
    assert fake.subscriptions.find_one({"_id": ObjectId(sid_)})["status"] == "pending" and fake.invoices.count_documents({}) == 0


def test_f1_wrong_store_rejected():
    sid_, st = sub(mid="mA", total=0)
    own_other = store("mA")
    foreign = store("mB")
    assert activate_sub(sid_, own_other).status_code == 400          # own store, but not the subscription's store
    assert activate_sub(sid_, foreign).status_code == 400            # someone else's store
    assert activate_sub(sid_, "not-an-id").status_code == 400
    assert fake.subscriptions.find_one({"_id": ObjectId(sid_)})["status"] == "pending" and fake.invoices.count_documents({}) == 0
    # subscription pointing at a store that is not the caller's: rejected even if the client repeats that store id
    sid2, st2 = sub(mid="mA", sid=foreign, total=0)
    assert activate_sub(sid2, foreign).status_code == 404
    assert fake.stores.find_one({"_id": ObjectId(foreign)}).get("subscription_plan") is None


def test_f1_duplicate_activation_creates_one_invoice():
    sid_, st = sub(total=0)
    assert activate_sub(sid_, st).status_code == 200
    r2 = activate_sub(sid_, st)
    assert r2.status_code == 200 and r2.json().get("already_processed") is True
    assert fake.invoices.count_documents({"store_id": st}) == 1


def test_f1_already_paid_real_subscription_cannot_be_reused():
    sid_, st = sub(total=118, status="paid")
    assert activate_sub(sid_, st).status_code == 409
    assert fake.invoices.count_documents({}) == 0


# ─────────────────────────── F2 ───────────────────────────
def border(mid="mA", total=0, **kw):
    doc = {"merchant_id": mid, "type": "banner", "days": 7, "from_date": "01 Dec 2026", "end_date": "07 Dec 2026", "price_per_day": 10,
           "base_price": 70, "gst_percent": 18, "gst_amount": 0 if not total else round(total - total / 1.18, 2), "total": total,
           "amount_paise": int(round(total * 100)), "final_amount": 0 if not total else round(total / 1.18, 2),
           "discount_code": "", "status": "pending", **kw}
    return str(fake.banner_orders.insert_one(doc).inserted_id)


def free_banner(oid, sid, h=HA):
    return c.post("/merchant/banners/activate-free", json={"order_id": oid, "store_id": sid, "store_name": "S", "city": "Pune", "title": "T",
                                                          "image_url": "data:image/png;base64,xx"}, headers=h)


@pytest.mark.parametrize("discount_code", ["", "FREE100"])
def test_f2_zero_value_banner_activates(discount_code):
    oid, sid = border(discount_code=discount_code, discount_amount=70 if discount_code else 0), store(status="active")
    r = free_banner(oid, sid)
    assert r.status_code == 200 and r.json()["invoice_no"].startswith("BNR-FREE-"), r.text
    assert fake.banner_orders.find_one({"_id": ObjectId(oid)})["status"] == "submitted"
    inv = list(fake.invoices.find({"type": "banner"}))
    assert len(inv) == 1 and inv[0]["total"] == 0 and inv[0]["payment_status"] == "free"


@pytest.mark.parametrize("total", [0.01, 1, 82.6])
def test_f2_non_zero_banner_rejected_even_when_pending(total):
    oid, sid = border(total=total), store(status="active")
    r = free_banner(oid, sid)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "payment_required"
    o = fake.banner_orders.find_one({"_id": ObjectId(oid)})
    assert o["status"] == "pending" and "free_claim" not in o
    assert fake.invoices.count_documents({}) == 0 and fake.merchant_banners.count_documents({}) == 0


def test_f2_razorpay_mode_non_zero_banner_cannot_use_free_route():
    for extra in ({"pay_mode": "razorpay", "razorpay_order_id": "order_R1"}, {}):      # a Razorpay order, and a legacy order with no mode
        fake.banner_orders.delete_many({})
        oid, sid = border(total=82.6, **extra), store(status="active")
        assert free_banner(oid, sid).status_code == 400
        assert fake.merchant_banners.count_documents({}) == 0 and fake.invoices.count_documents({}) == 0


def test_f2_cross_merchant_and_foreign_store_rejected():
    oid = border(mid="mA")
    assert free_banner(oid, store("mA", "active"), HB).status_code == 404          # B uses A's order
    assert free_banner(oid, store("mB", "active"), HA).status_code == 404          # A's order, B's store
    assert free_banner(oid, store("mA", "active"), HA).status_code == 200          # the owner, own store still works
    assert fake.merchant_banners.count_documents({}) == 1


def test_f2_discount_not_consumed_when_rejected_but_consumed_once_on_a_real_free_activation():
    fake.discounts.insert_one({"code": "FREE100", "used_count": 0})
    free_banner(border(total=82.6, discount_code="FREE100"), store(status="active"))           # non-zero: rejected
    free_banner(border(discount_code="FREE100"), store("mB", "active"))                          # foreign store: rejected
    assert fake.discounts.find_one({"code": "FREE100"})["used_count"] == 0
    oid, sid = border(discount_code="FREE100"), store(status="active")
    assert free_banner(oid, sid).status_code == 200 and free_banner(oid, sid).status_code == 200
    assert fake.discounts.find_one({"code": "FREE100"})["used_count"] == 1


def test_f2_duplicate_activation_creates_one_banner_and_one_invoice():
    oid, sid = border(), store(status="active")
    r1, r2 = free_banner(oid, sid), free_banner(oid, sid)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["banner_id"] == r2.json()["banner_id"]
    assert fake.invoices.count_documents({"type": "banner"}) == 1 and fake.merchant_banners.count_documents({}) == 1


def test_f2_claim_blocks_a_concurrent_second_activation():
    oid, sid = border(), store(status="active")
    fake.banner_orders.update_one({"_id": ObjectId(oid)}, {"$set": {"free_claim": "now"}})     # another request already holds the claim
    free_banner(oid, sid)
    assert fake.invoices.count_documents({}) == 0 and fake.merchant_banners.count_documents({}) == 0


# ─────────────────────────── F3 ───────────────────────────
def sign(rid, pid="pay_1", secret=SECRET):
    return hmac.new(secret.encode(), f"{rid}|{pid}".encode(), hashlib.sha256).hexdigest()


def verify_banner(oid, sid, **ids):
    return c.post("/merchant/banners/verify", json={"order_id": oid, "store_id": sid, "store_name": "S", "city": "Pune", "title": "T",
                                                    "image_url": "data:image/png;base64,xx", **ids}, headers=HA)


def rz_banner(total=82.6):
    return border(total=total, razorpay_order_id="order_R1", pay_mode="razorpay")


def test_f3_valid_razorpay_verification_marks_paid():
    oid, sid = rz_banner(), store(status="active")
    r = verify_banner(oid, sid, razorpay_order_id="order_R1", razorpay_payment_id="pay_1", razorpay_signature=sign("order_R1"))
    assert r.status_code == 200, r.text
    assert fake.banner_orders.find_one({"_id": ObjectId(oid)})["status"] == "paid"


@pytest.mark.parametrize("ids", [
    {},                                                                                           # nothing
    {"razorpay_payment_id": "pay_1", "razorpay_signature": "x"},                                  # missing order id
    {"razorpay_order_id": "order_R1", "razorpay_signature": "x"},                                 # missing payment id
    {"razorpay_order_id": "order_R1", "razorpay_payment_id": "pay_1"},                            # missing signature
    {"razorpay_order_id": "order_R1", "razorpay_payment_id": "pay_1", "razorpay_signature": "bad"},      # invalid signature
    {"razorpay_order_id": "order_OTHER", "razorpay_payment_id": "pay_1", "razorpay_signature": sign("order_OTHER")},   # wrong order id (validly signed)
    {"razorpay_order_id": "order_R1", "razorpay_payment_id": "pay_1", "razorpay_signature": sign("order_R1", secret="other")},
])
def test_f3_razorpay_mode_rejects_missing_or_invalid_proof(ids):
    oid, sid = rz_banner(), store(status="active")
    r = verify_banner(oid, sid, **ids)
    assert r.status_code == 400, r.text
    assert fake.banner_orders.find_one({"_id": ObjectId(oid)})["status"] == "pending"
    assert fake.merchant_banners.count_documents({}) == 0 and fake.invoices.count_documents({}) == 0


def test_f3_manual_order_cannot_be_marked_paid_through_razorpay_verification():
    for extra in ({"pay_mode": "manual"}, {}):                      # explicit manual mode, and a non-zero order with no Razorpay order at all
        fake.banner_orders.delete_many({})
        oid, sid = border(total=82.6, **extra), store(status="active")
        for ids in ({}, {"razorpay_order_id": "order_R1", "razorpay_payment_id": "pay_1", "razorpay_signature": sign("order_R1")}):
            r = verify_banner(oid, sid, **ids)
            assert r.status_code == 409 and r.json()["detail"]["code"] == "manual_payment_pending", r.text
        assert fake.banner_orders.find_one({"_id": ObjectId(oid)})["status"] == "pending"
        assert fake.merchant_banners.count_documents({}) == 0 and fake.invoices.count_documents({}) == 0


def test_f3_manual_order_even_with_a_stored_razorpay_id_stays_pending():
    oid, sid = border(total=82.6, pay_mode="manual", razorpay_order_id="order_R1"), store(status="active")
    r = verify_banner(oid, sid, razorpay_order_id="order_R1", razorpay_payment_id="pay_1", razorpay_signature=sign("order_R1"))
    assert r.status_code == 409


def test_f3_zero_payable_needs_no_payment_proof():
    MA._require_valid_razorpay_payment({"total": 0, "amount_paise": 0, "final_amount": 0, "pay_mode": "free"}, "", "", "")
    with pytest.raises(Exception):
        MA._require_valid_razorpay_payment({"total": 5, "amount_paise": 0}, "", "", "")           # inconsistent figures: not free
    with pytest.raises(Exception):
        MA._require_valid_razorpay_payment({"amount_paise": 0}, "", "", "")                       # no total: not free


def test_f3_verify_checks_store_ownership():
    oid, foreign = rz_banner(), store("mB", "active")
    r = verify_banner(oid, foreign, razorpay_order_id="order_R1", razorpay_payment_id="pay_1", razorpay_signature=sign("order_R1"))
    assert r.status_code == 404 and fake.merchant_banners.count_documents({}) == 0


def _product_order(**kw):
    sid = store(status="active")
    doc = {"kind": "create", "merchant_id": "mA", "store_id": sid, "status": "created", "total": 118.0, "amount_paise": 11800,
           "base_price": 100, "gst_amount": 18, "days": 30, "from_date": P.ist_today().strftime("%Y-%m-%d"), "end_date": (P.ist_today() + timedelta(days=29)).strftime("%Y-%m-%d"), "created_at": datetime.utcnow(), "pricing_tier": "paid", **kw}
    return str(fake[P.ORDERS].insert_one(doc).inserted_id), sid


def _verify_product(oid, sid, **ids):
    end = (P.ist_today() + timedelta(days=29)).strftime("%Y-%m-%d")
    return c.post("/merchant/products/verify", json={"order_id": oid, "store_id": sid, "title": "P", "offer_text": "o", "price": "50", "original_price": "80",
                                                     "store_name": "S", "city": "Pune", "logo_url": "data:image/png;base64,xx", "end_date": end, **ids}, headers=HA)


def test_f3_product_verification_modes():
    oid, sid = _product_order(pay_mode="manual")
    r = _verify_product(oid, sid, razorpay_order_id="o1", razorpay_payment_id="p1", razorpay_signature=sign("o1", "p1"))
    assert r.status_code == 409 and fake.merchant_products.count_documents({}) == 0
    oid, sid = _product_order(pay_mode="razorpay", razorpay_order_id="order_P1")
    assert _verify_product(oid, sid).status_code == 400
    assert _verify_product(oid, sid, razorpay_order_id="order_P1", razorpay_payment_id="pay_1", razorpay_signature="bad").status_code == 400
    assert fake.merchant_products.count_documents({}) == 0
    ok = _verify_product(oid, sid, razorpay_order_id="order_P1", razorpay_payment_id="pay_1", razorpay_signature=sign("order_P1"))
    assert ok.status_code == 200, ok.text


def test_f3_renewal_verification_modes():
    sid = store(status="active")
    pid = fake.merchant_products.insert_one({"merchant_id": "mA", "store_id": sid, "status": "approved", "is_active": True, "title": "L",
                                             "start_date": P.ist_today(), "end_date": P.ist_today() + timedelta(days=9), "duration_days": 10}).inserted_id
    end = (P.ist_today() + timedelta(days=19)).strftime("%Y-%m-%d")
    ro = c.post(f"/merchant/products/{pid}/renew", json={"end_date": end}, headers=HA)
    assert ro.status_code == 200, ro.text
    # no Razorpay keys in the test world -> the renewal order is a manual order: it must stay pending whatever the client sends
    order = fake[P.ORDERS].find_one({"kind": "renew", "product_id": str(pid)})
    assert order["pay_mode"] == "manual"
    for ids in ({}, {"razorpay_order_id": "o1", "razorpay_payment_id": "p1", "razorpay_signature": sign("o1", "p1")}):
        r = c.post(f"/merchant/products/{pid}/renew/verify", json=ids, headers=HA)
        assert r.status_code == 409, r.text
    assert not fake.merchant_products.find_one({"_id": pid}).get("renewals")                  # nothing was renewed
    assert fake[P.ORDERS].find_one({"_id": order["_id"]})["status"] == "created"


def test_f3_manual_approval_workflow_is_untouched():
    """The admin / manual approval path does not go through the Razorpay helper: an admin can still approve a banner created for a
    manual order, and the order itself stays pending until then."""
    import inspect
    import routers.admin as AD
    src = inspect.getsource(AD)
    assert "_require_valid_razorpay_payment" not in src
    oid, sid = border(total=82.6, pay_mode="manual"), store(status="active")
    assert verify_banner(oid, sid).status_code == 409
    assert fake.banner_orders.find_one({"_id": ObjectId(oid)})["status"] == "pending"
