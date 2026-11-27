"""NOTE: sorts AFTER test_banner_media.py on purpose (that module swaps the shared fake `database` at import).
F5: /merchant/subscribe/verify security.  F2: the manual-payment banner workflow, merchant -> admin."""
import hashlib
import hmac
import sys
import threading

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import routers.merchant_app as MA
import server

fake = sys.modules["database"].db
c = TestClient(server.app)
SECRET = "test_secret"
HA, HB = {"Authorization": "Bearer tokA"}, {"Authorization": "Bearer tokB"}
HADM = {"Authorization": "Bearer at"}


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    for col in ("accounts", "stores", "subscriptions", "banner_orders", "merchant_banners", "invoices", "discounts", "transactions",
                "promo_sliders", "dashboard_users", "dashboard_roles"):
        fake[col].delete_many({})
    fake.accounts.insert_one({"token": "tokA", "merchant_id": "mA", "name": "A", "phone": "9990000001", "roles": ["merchant"]})
    fake.accounts.insert_one({"token": "tokB", "merchant_id": "mB", "name": "B", "phone": "9990000002", "roles": ["merchant"]})
    role = fake.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "at", "status": "active", "full_name": "Admin", "mobile": "9000000001", "role_id": role})
    monkeypatch.setattr(MA, "RAZORPAY_KEY_SECRET", SECRET)
    monkeypatch.setattr(MA, "_cloudinary_upload", lambda v, folder="": "http://img/x.png" if v else "")
    monkeypatch.setattr(MA, "_make_thumb_url", lambda u: u)
    yield


def sign(oid, pid="pay_1", secret=SECRET):
    return hmac.new(secret.encode(), f"{oid}|{pid}".encode(), hashlib.sha256).hexdigest()


def store(mid="mA", status="draft"):
    return str(fake.stores.insert_one({"store_name": "S", "city": "Pune", "status": status, "merchant_id": mid}).inserted_id)


def sub(mid="mA", sid=None, total=118.0, rid="order_S1", **kw):
    doc = {"merchant_id": mid, "store_id": sid or store(mid), "plan": "monthly", "from_date": "2026-12-01", "end_date": "2026-12-31", "price": 100,
           "gst": 18.0 if total else 0, "total": total, "final_amount": 100.0 if total else 0, "status": "pending", "discount_code": "",
           "razorpay_order_id": rid, "pay_mode": "razorpay", **kw}
    return str(fake.subscriptions.insert_one(doc).inserted_id), doc["store_id"]


def verify(rid, sid, pid="pay_1", sig=None, h=HA):
    return c.post("/merchant/subscribe/verify", json={"razorpay_order_id": rid, "razorpay_payment_id": pid,
                                                      "razorpay_signature": sign(rid, pid) if sig is None else sig, "store_id": sid}, headers=h)


def untouched(sub_id, st):
    assert fake.subscriptions.find_one({"_id": ObjectId(sub_id)})["status"] == "pending"
    assert fake.invoices.count_documents({}) == 0
    assert fake.stores.find_one({"_id": ObjectId(st)}).get("subscription_plan") is None


# ─────────────────────────── F5 ───────────────────────────
def test_f5_valid_payment_activates_once():
    sid_, st = sub()
    r = verify("order_S1", st)
    assert r.status_code == 200, r.text
    d = fake.subscriptions.find_one({"_id": ObjectId(sid_)})
    assert d["status"] == "paid" and d["razorpay_payment_id"] == "pay_1"
    assert fake.stores.find_one({"_id": ObjectId(st)})["status"] == "waiting_approval"
    assert fake.invoices.count_documents({"store_id": st}) == 1


def test_f5_other_merchant_cannot_verify_my_subscription():
    sid_, st = sub(mid="mA")
    assert verify("order_S1", st, h=HB).status_code == 404            # even with a genuine signature for A's order
    untouched(sid_, st)


def test_f5_store_must_be_the_subscriptions_store_and_the_callers():
    sid_, st = sub(mid="mA")
    own_other, foreign = store("mA"), store("mB")
    assert verify("order_S1", own_other).status_code == 400
    assert verify("order_S1", foreign).status_code == 400
    assert verify("order_S1", "nope").status_code == 400
    untouched(sid_, st)
    assert fake.stores.find_one({"_id": ObjectId(foreign)}).get("subscription_plan") is None
    # a subscription whose store belongs to someone else can never be activated by this merchant
    s2, _ = sub(mid="mA", sid=foreign, rid="order_S2")
    assert verify("order_S2", foreign).status_code == 404
    assert fake.stores.find_one({"_id": ObjectId(foreign)}).get("subscription_plan") is None


def test_f5_attacker_cannot_use_own_payment_to_activate_a_victims_store():
    """The classic bypass: pay for my own cheap subscription, then send the VICTIM's store_id with my genuine payment ids."""
    mine, my_store = sub(mid="mB", rid="order_MINE")
    victim = store("mA")
    r = verify("order_MINE", victim, h=HB)
    assert r.status_code == 400
    assert fake.stores.find_one({"_id": ObjectId(victim)}).get("subscription_plan") is None
    assert fake.subscriptions.find_one({"_id": ObjectId(mine)})["status"] == "pending" and fake.invoices.count_documents({}) == 0


@pytest.mark.parametrize("pid,sig", [("pay_1", "bad"), ("pay_1", ""), ("", "x"), ("pay_1", sign("order_S1", "pay_OTHER")), ("pay_1", sign("order_S1", "pay_1", "otherkey"))])
def test_f5_missing_or_invalid_proof_rejected(pid, sig):
    sid_, st = sub()
    assert verify("order_S1", st, pid=pid, sig=sig).status_code == 400
    untouched(sid_, st)


def test_f5_wrong_or_unknown_order_id_rejected():
    sid_, st = sub()
    assert verify("order_OTHER", st).status_code == 404               # validly signed, but no subscription has that order id
    assert verify("", st).status_code == 400
    untouched(sid_, st)


def test_f5_manual_mode_subscription_cannot_be_activated_by_razorpay_verification():
    for extra in ({"pay_mode": "manual"}, {"pay_mode": "manual", "razorpay_order_id": "order_S1"}):
        fake.subscriptions.delete_many({})
        sid_, st = sub(**extra) if extra.get("razorpay_order_id") else sub(rid="order_S1", **extra)
        r = verify("order_S1", st)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "manual_payment_pending", r.text
        untouched(sid_, st)


def test_f5_zero_payable_subscription_is_not_verified_through_razorpay():
    sid_, st = sub(total=0, pay_mode="free")
    assert verify("order_S1", st).status_code == 400
    untouched(sid_, st)
    # ...and it still activates through the free flow, without Razorpay
    assert c.post("/merchant/subscribe/free", json={"subscription_id": sid_, "store_id": st}, headers=HA).status_code == 200


def test_f5_repeat_verification_creates_one_invoice():
    sid_, st = sub()
    assert verify("order_S1", st).status_code == 200
    r2 = verify("order_S1", st)
    assert r2.status_code == 200 and "already" in r2.json()["message"]
    assert fake.invoices.count_documents({"store_id": st}) == 1


def test_f5_concurrent_verification_creates_one_invoice_and_counts_discount_once():
    fake.discounts.insert_one({"code": "SAVE", "used_count": 0, "max_uses": 0})
    sid_, st = sub(discount_code="SAVE")
    out = []
    ts = [threading.Thread(target=lambda: out.append(verify("order_S1", st).status_code)) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out and all(x == 200 for x in out)
    assert fake.invoices.count_documents({"store_id": st}) == 1
    assert fake.discounts.find_one({"code": "SAVE"})["used_count"] == 1


def test_f5_rejected_verification_does_not_consume_discount():
    fake.discounts.insert_one({"code": "SAVE", "used_count": 0, "max_uses": 0})
    sid_, st = sub(discount_code="SAVE")
    verify("order_S1", st, sig="bad")
    verify("order_S1", store("mB"), h=HB)
    assert fake.discounts.find_one({"code": "SAVE"})["used_count"] == 0


def test_f5_cancelled_or_failed_subscription_is_not_activated():
    sid_, st = sub(status="cancelled")
    assert verify("order_S1", st).status_code == 409
    assert fake.invoices.count_documents({}) == 0


# ─────────────────────────── F2: manual banner workflow ───────────────────────────
def border(mid="mA", total=82.6, **kw):
    doc = {"merchant_id": mid, "type": "banner", "days": 7, "from_date": "01 Dec 2026", "end_date": "07 Dec 2026", "price_per_day": 10, "base_price": 70,
           "gst_percent": 18, "gst_amount": 12.6, "total": total, "amount_paise": int(round(total * 100)), "final_amount": 70, "discount_code": "",
           "status": "pending", "pay_mode": "manual", **kw}
    return str(fake.banner_orders.insert_one(doc).inserted_id)


def submit(oid, sid, h=HA):
    return c.post("/merchant/banners/activate-free", json={"order_id": oid, "store_id": sid, "store_name": "S", "city": "Pune", "title": "T",
                                                          "image_url": "data:image/png;base64,xx"}, headers=h)


def test_manual_paid_banner_is_submitted_and_stays_pending_unpaid():
    oid, sid = border(), store(status="active")
    r = submit(oid, sid)
    assert r.status_code == 200 and r.json()["manual_payment_pending"] is True and r.json()["invoice_no"] == "", r.text
    b = fake.merchant_banners.find_one({"_id": ObjectId(r.json()["banner_id"])})
    assert b["payment_status"] == "manual_pending" and b["status"] == "pending" and b["approval_status"] == "pending"
    assert fake.invoices.count_documents({}) == 0                          # no free invoice, nothing marked paid
    o = fake.banner_orders.find_one({"_id": ObjectId(oid)})
    assert o["status"] == "submitted" and o["manual_payment"] is True and o["total"] == 82.6
    assert fake.promo_sliders.count_documents({}) == 0                     # not live


def test_manual_submit_is_idempotent_and_owner_only():
    oid, sid = border(), store(status="active")
    r1, r2 = submit(oid, sid), submit(oid, sid)
    assert r1.json()["banner_id"] == r2.json()["banner_id"] and fake.merchant_banners.count_documents({}) == 1
    oid2 = border()
    assert submit(oid2, store("mA", "active"), HB).status_code == 404       # not the owner
    assert submit(oid2, store("mB", "active"), HA).status_code == 404       # owner, someone else's store
    assert fake.merchant_banners.count_documents({}) == 1


def test_manual_order_cannot_be_turned_into_paid_by_razorpay_verification():
    oid, sid = border(), store(status="active")
    r = c.post("/merchant/banners/verify", json={"order_id": oid, "store_id": sid, "title": "T", "razorpay_order_id": "o", "razorpay_payment_id": "p",
                                                 "razorpay_signature": sign("o", "p")}, headers=HA)
    assert r.status_code == 409 and fake.merchant_banners.count_documents({}) == 0
    submit(oid, sid)
    assert fake.banner_orders.find_one({"_id": ObjectId(oid)})["status"] == "submitted"       # still not "paid"


def test_razorpay_mode_paid_banner_cannot_be_submitted_as_manual():
    oid, sid = border(pay_mode="razorpay", razorpay_order_id="order_R1"), store(status="active")
    r = submit(oid, sid)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "payment_required"
    assert fake.merchant_banners.count_documents({}) == 0


def test_rejected_requests_do_not_consume_discount_usage_but_a_submission_does_once():
    fake.discounts.insert_one({"code": "SAVE", "used_count": 0, "max_uses": 0})
    bad = border(pay_mode="razorpay", razorpay_order_id="o", discount_code="SAVE")
    submit(bad, store("mA", "active"))                                        # rejected: razorpay-mode
    submit(border(discount_code="SAVE"), store("mA", "draft"))               # rejected: store not active
    submit(border(discount_code="SAVE"), store("mB", "active"))              # rejected: not the merchant's store
    assert fake.discounts.find_one({"code": "SAVE"})["used_count"] == 0
    oid, sid = border(discount_code="SAVE"), store("mA", "active")
    assert submit(oid, sid).status_code == 200 and submit(oid, sid).status_code == 200
    assert fake.discounts.find_one({"code": "SAVE"})["used_count"] == 1


def test_admin_approves_manual_banner_without_razorpay_verification(monkeypatch):
    """The existing admin approval publishes the pending banner; it never calls the Razorpay verification helper."""
    called = []
    monkeypatch.setattr(MA, "_require_valid_razorpay_payment", lambda *a, **k: called.append(1))
    oid, sid = border(), store(status="active")
    bid = submit(oid, sid).json()["banner_id"]
    r = c.put(f"/admin/merchant-banners/{bid}/approve", headers=HADM)
    assert r.status_code == 200, r.text
    assert fake.merchant_banners.find_one({"_id": ObjectId(bid)})["approval_status"] == "approved"
    assert called == [] and fake.promo_sliders.count_documents({}) == 1
    assert fake.invoices.count_documents({}) == 0


def test_admin_approval_still_requires_admin_authority():
    oid, sid = border(), store(status="active")
    bid = submit(oid, sid).json()["banner_id"]
    assert c.put(f"/admin/merchant-banners/{bid}/approve", headers=HA).status_code in (401, 403)       # a merchant token is not an admin
    assert c.put(f"/admin/merchant-banners/{bid}/approve").status_code in (401, 403)
    assert fake.merchant_banners.find_one({"_id": ObjectId(bid)})["approval_status"] == "pending"
