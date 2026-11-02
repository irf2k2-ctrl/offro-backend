"""HTTP-level tests for the Product architecture (shared mongomock DB from conftest.py + FastAPI TestClient).
Run from backend root:  python -m pytest test_products_api.py -q -W ignore"""
import sys, os, threading
import pytest
from datetime import datetime, timedelta
from bson import ObjectId
from fastapi.testclient import TestClient
import server
from services import products as P
import routers.merchant_app as MA
MA._cloudinary_upload = lambda v, folder="": "http://img/x.png" if v else ""
MA._make_thumb_url = lambda u: u
import routers.merchant_products as MP
MP._cloudinary_upload, MP._make_thumb_url = MA._cloudinary_upload, MA._make_thumb_url
import routers.admin_products as AP
AP._cloudinary_upload, AP._make_thumb_url = MA._cloudinary_upload, MA._make_thumb_url
MP.RAZORPAY_KEY_ID = MP.RAZORPAY_KEY_SECRET = ""
fake = sys.modules["database"].db
c = TestClient(server.app)

MID = "m1"
SID = ""
@pytest.fixture(autouse=True)
def _api_fixtures():
    """Merchant account, admin session and an active store (rebuilt for every test)."""
    global SID
    fake.accounts.insert_one({"token": "mt", "merchant_id": MID, "name": "Mer", "phone": "999", "roles": ["merchant"]})
    fake.admins.insert_one({"token": "at"})
    SID = str(fake.stores.insert_one({"store_name": "S", "city": "Pune", "status": "active", "merchant_id": MID}).inserted_id)
    yield

H = {"Authorization": "Bearer mt"}; A = {"Authorization": "Bearer at"}
END = (P.ist_today() + timedelta(days=29)).strftime("%Y-%m-%d")   # today..END = 30 days

def settings(**kw):
    fake.pricing.delete_many({})
    fake.pricing.insert_one({"gst_percent": 18, "free_product_limit": 10, "product_price_per_day": 10,
                             "product_max_validity_days": 365, **kw})
def wipe(): 
    for col in ("merchant_products","product_orders","merchant_locks","invoices"): fake[col].delete_many({})
def seed(n, status="approved"):
    for _ in range(n):
        fake.merchant_products.insert_one({"merchant_id": MID, "store_id": SID, "status": status, "is_active": True,
            "end_date": P.ist_today()+timedelta(days=20), "start_date": P.ist_today(), "title": "s"})
body = lambda t="P": {"title": t, "offer_text": "o", "price": "50", "original_price": "80", "store_id": SID,
                      "store_name": "S", "city": "Pune", "logo_url": "data:image/png;base64,xx"}

def merchant_create(t="P"):
    o = c.post("/merchant/products/order", json={"store_id": SID, "end_date": END}, headers=H)
    assert o.status_code == 200, o.text
    o = o.json(); pl = {**body(t), "order_id": o["order_id"], "end_date": END}
    if o["amount_paise"] == 0:
        return o, c.post("/merchant/products/activate-free", json=pl, headers=H)
    return o, c.post("/merchant/products/verify", json=pl, headers=H)

def test_products_api():
    # 1 settings endpoint exposes + validates free_product_limit
    settings(); r = c.get("/admin/pricing", headers=A); assert r.json()["free_product_limit"] == 10, r.text
    assert c.put("/admin/pricing", json={"free_product_limit": 12}, headers=A).status_code == 200
    assert fake.pricing.find_one()["free_product_limit"] == 12
    assert c.put("/admin/pricing", json={"free_product_limit": -1}, headers=A).status_code == 400
    settings()

    # 2 merchant: 7 qualifying -> free ; slots endpoint
    wipe(); seed(7)
    s = c.get("/merchant/products/slots", headers=H).json(); assert s["used"] == 7 and s["free_limit"] == 10 and s["next_is_free"]
    o, r = merchant_create(); assert o["pricing_tier"] == "free" and r.status_code == 200, r.text
    # 3 listing shows it
    rows = c.get("/merchant/products", headers=H).json(); assert len(rows) == 8 and rows[0]["pricing_tier"] in ("free",)
    # 4 10 qualifying -> paid, price from settings
    wipe(); seed(10)
    o = c.post("/merchant/products/order", json={"store_id": SID, "end_date": END}, headers=H).json()
    assert o["pricing_tier"] == "paid" and o["base_price"] == 300 and o["amount_display"] == 354 and o["days"] == 30 and o["price_per_day"] == 10, o
    # 5 Admin changes price / limit -> the very next decision follows, no code change
    settings(product_price_per_day=25)
    o = c.post("/merchant/products/order", json={"store_id": SID, "end_date": END}, headers=H).json()
    assert o["pricing_tier"] == "paid" and o["base_price"] == 750 and o["amount_display"] == 885, o
    settings(free_product_limit=15)
    o = c.post("/merchant/products/order", json={"store_id": SID, "end_date": END}, headers=H).json()
    assert o["pricing_tier"] == "free" and o["amount_paise"] == 0, o
    # 6 paid create records price at purchase and consumes a slot; later price change does not rewrite it
    settings(); wipe(); seed(10)
    o, r = merchant_create("Paid1"); assert r.status_code == 200, r.text
    p = fake.merchant_products.find_one({"title": "Paid1"})
    assert p["pricing_tier"] == "paid" and p["charged_amount"] == 354 and p["unit_price"] == 300 and p["duration_days"] == 30 and p["price_per_day"] == 10
    fake.pricing.update_one({}, {"$set": {"product_price_per_day": 99}}); assert fake.merchant_products.find_one({"title": "Paid1"})["charged_amount"] == 354
    settings()
    # 7 free activation of a PAID-tier order is refused
    o = c.post("/merchant/products/order", json={"store_id": SID, "end_date": END}, headers=H).json()
    r = c.post("/merchant/products/activate-free", json={**body(), "order_id": o["order_id"]}, headers=H); assert r.status_code == 400
    # 8 delete = soft, frees slot, cannot be edited/renewed
    wipe(); seed(9); o, r = merchant_create("D"); assert r.status_code == 200
    pid = str(fake.merchant_products.find_one({"title": "D"})["_id"])
    assert c.get("/merchant/products/slots", headers=H).json()["next_is_free"] is False
    d = c.delete(f"/merchant/products/{pid}", headers=H).json(); assert d["refund"] is False
    assert fake.merchant_products.find_one({"_id": ObjectId(pid)})["status"] == "deleted"
    assert c.get("/merchant/products/slots", headers=H).json()["next_is_free"] is True
    assert c.put(f"/merchant/products/{pid}", json={"title": "x"}, headers=H).status_code == 410
    assert c.post(f"/merchant/products/{pid}/renew", json={"end_date": END}, headers=H).status_code == 410
    assert all(r["_id"] != pid for r in c.get("/merchant/products", headers=H).json())
    # 9 renewal re-checks free/paid; expired product renewed free when slot available
    wipe(); seed(3)
    e = fake.merchant_products.insert_one({"merchant_id": MID, "store_id": SID, "status": "approved", "is_active": True, "title": "E",
        "start_date": P.ist_today()-timedelta(days=40), "end_date": P.ist_today()-timedelta(days=2)}).inserted_id
    rows = {r["_id"]: r for r in c.get("/merchant/products", headers=H).json()}; assert rows[str(e)]["status"] == "expired"
    assert c.get("/merchant/products/slots", params={"exclude_id": str(e)}, headers=H).json()["next_is_free"]
    ro = c.post(f"/merchant/products/{e}/renew", json={"end_date": END}, headers=H); assert ro.status_code == 200, ro.text
    assert ro.json()["pricing_tier"] == "free"
    rv = c.post(f"/merchant/products/{e}/renew/verify", json={}, headers=H); assert rv.status_code == 200, rv.text
    assert fake.merchant_products.find_one({"_id": e})["status"] == "approved"
    # paid renewal when merchant is full
    wipe(); seed(10)
    e = fake.merchant_products.insert_one({"merchant_id": MID, "store_id": SID, "status": "expired", "is_active": False, "title": "E2",
        "start_date": P.ist_today()-timedelta(days=40), "end_date": P.ist_today()-timedelta(days=2)}).inserted_id
    ro = c.post(f"/merchant/products/{e}/renew", json={"end_date": END}, headers=H).json(); assert ro["pricing_tier"] == "paid" and ro["amount_display"] == 354
    # 10 ADMIN create for merchant: 7 -> free, 10 -> 409 w/o payment, paid w/ payment, 15 -> paid
    ab = lambda **k: {"title": "AdminP", "text": "t", "price": 10, "logo": "data:image/png;base64,xx", "store_id": SID, "end_date": END, **k}
    wipe(); seed(7); r = c.post("/admin/products", json=ab(), headers=A); assert r.status_code == 200 and r.json()["pricing_tier"] == "free", r.text
    wipe(); seed(10); r = c.post("/admin/products", json=ab(), headers=A); assert r.status_code == 409 and "₹10/day" in r.json()["detail"], r.text
    r = c.post("/admin/products", json=ab(pay_mode="cash", pay_amount=354, pay_ref="R1"), headers=A); assert r.status_code == 200 and r.json()["pricing_tier"] == "paid" and r.json()["charged_amount"] == 354, r.text
    wipe(); seed(15); r = c.post("/admin/products", json=ab(), headers=A); assert r.status_code == 409
    # 10b admin-recorded paid product: validation, order, invoice, revenue, no Razorpay
    wipe(); seed(10); fake.invoices.delete_many({})
    for bad in (300, 50, 353.99):
        r = c.post("/admin/products", json=ab(pay_mode="UPI", pay_amount=bad), headers=A); assert r.status_code == 400 and "354" in r.json()["detail"], (bad, r.text)
    assert fake.merchant_products.count_documents({}) == 10 and fake.product_orders.count_documents({}) == 0
    r = c.post("/admin/products", json=ab(pay_mode="UPI", pay_amount=0), headers=A); assert r.status_code == 409
    r = c.post("/admin/products", json=ab(pay_amount=354), headers=A); assert r.status_code == 409          # mode missing
    r = c.post("/admin/products", json=ab(pay_mode="UPI", pay_amount=354, pay_ref="U1"), headers=A); assert r.status_code == 200, r.text
    r2 = c.post("/admin/products", json=ab(pay_mode="Cash", pay_amount=400.5, pay_ref="C2"), headers=A); assert r2.status_code == 200, r2.text
    assert r2.json()["charged_amount"] == 400.5 and r2.json()["expected_amount"] == 354
    pr = fake.merchant_products.find_one({"_id": ObjectId(r2.json()["id"])})
    assert (pr["pricing_tier"], pr["unit_price"], pr["charged_amount"], pr["expected_amount"], pr["payment_mode"], pr["payment_ref"], pr["merchant_id"]) == ("paid", 300.0, 400.5, 354.0, "Cash", "C2", MID)
    od = fake.product_orders.find_one({"product_id": str(pr["_id"])})
    assert od["kind"] == "create" and od["status"] == "paid" and od["pay_mode"] == "admin_recorded" and od["razorpay_order_id"] is None
    assert (od["unit_price"], od["gst_amount"], od["expected_amount"], od["amount_paid"], od["payment_mode"], od["payment_ref"], od["merchant_id"]) == (300.0, 54.0, 354.0, 400.5, "Cash", "C2", MID)
    inv = fake.invoices.find_one({"invoice_no": pr["invoice_no"]}); assert inv["type"] == "product" and inv["total"] == 400.5 and inv["gst"] == 54.0 and inv["expected_amount"] == 354
    assert fake.product_orders.count_documents({}) == 2 and fake.invoices.count_documents({}) == 2
    assert c.get("/admin/products/revenue", headers=A).json()["lifetime"] == 354 + 400.5
    assert any(i["invoice_no"] == pr["invoice_no"] for i in c.get("/admin/invoices/full", headers=A).json())
    # later settings change leaves the recorded payment untouched
    fake.pricing.update_one({}, {"$set": {"product_price_per_day": 99}})
    pr2 = fake.merchant_products.find_one({"_id": pr["_id"]}); assert (pr2["unit_price"], pr2["charged_amount"], pr2["expected_amount"]) == (300.0, 400.5, 354.0)
    settings()
    # free admin product: no order, no invoice
    wipe(); fake.invoices.delete_many({}); seed(3); assert c.post("/admin/products", json=ab(pay_mode="UPI", pay_amount=5), headers=A).status_code == 200
    assert fake.product_orders.count_documents({}) == 0 and fake.invoices.count_documents({}) == 0
    # is_active is not part of the quota
    wipe(); seed(10); fake.merchant_products.update_many({}, {"$set": {"is_active": False}}); assert c.get("/merchant/products/slots", headers=H).json()["used"] == 10
    # admin-created counts against merchant quota
    wipe(); seed(9); assert c.post("/admin/products", json=ab(), headers=A).status_code == 200
    assert c.get("/merchant/products/slots", headers=H).json()["next_is_free"] is False
    # admin-owned (no store/merchant) consumes no quota
    wipe(); seed(10); r = c.post("/admin/products", json={"title": "OffrO", "price": 1, "end_date": END, "logo": "data:image/png;base64,xx"}, headers=A); assert r.status_code == 200 and r.json()["pricing_tier"] == "admin", r.text
    assert c.get("/merchant/products/slots", headers=H).json()["used"] == 10
    # 11 admin delete = soft + frees slot; deactivate keeps slot; approve/reject
    wipe(); seed(10); pid = str(fake.merchant_products.find_one()["_id"])
    assert c.put(f"/admin/products/{pid}/deactivate", headers=A).status_code == 200
    assert c.get("/merchant/products/slots", headers=H).json()["used"] == 10
    assert c.delete(f"/admin/products/{pid}", headers=A).status_code == 200
    assert c.get("/merchant/products/slots", headers=H).json()["used"] == 9
    wipe(); seed(1, "pending"); pid = str(fake.merchant_products.find_one()["_id"])
    assert c.put(f"/admin/products/{pid}/approve", headers=A).status_code == 200 and fake.merchant_products.find_one()["status"] == "approved"
    assert c.put(f"/admin/products/{pid}/reject", json={"reason": "x"}, headers=A).status_code == 200
    assert c.get("/merchant/products/slots", headers=H).json()["used"] == 0           # rejected does not count
    # 12 admin list + reports
    assert isinstance(c.get("/admin/products", headers=A).json(), list)
    for u in ("summary", "revenue", "expiring", "top-merchants"): assert c.get(f"/admin/products/{u}", headers=A).status_code == 200, u
    # 13 customer visibility: approved+active+in-date only; no pricing leakage
    wipe()
    mk = lambda **k: fake.merchant_products.insert_one({"merchant_id": MID, "store_id": SID, "city": "Pune", "title": "v", "status": "approved",
        "is_active": True, "image_url": "http://i", "start_date": P.ist_today(), "end_date": P.ist_today()+timedelta(days=5), "pricing_tier": "paid", "charged_amount": 118, **k})
    live = mk(title="live").inserted_id; mk(title="pend", status="pending"); mk(title="off", is_active=False); mk(title="del", status="deleted")
    mk(title="old", end_date=P.ist_today()-timedelta(days=1)); mk(title="today", end_date=P.ist_today())
    pub = c.get("/products-public", params={"city": "pune"}).json(); titles = sorted(x["title"] for x in pub)
    assert titles == ["live", "today"], titles
    assert "pricing_tier" not in pub[0] and "charged_amount" not in pub[0]
    assert sorted(x["title"] for x in c.get(f"/products/by-store/{SID}").json()) == ["live", "today"]
    assert c.get("/products-public", params={"city": "delhi"}).json() == []
    assert c.post(f"/products/{live}/track", json={"event": "view"}).status_code == 200
    assert c.get(f"/merchant/products/{live}/analytics", headers=H).json()["views"] == 1
    assert c.get("/gift-vouchers-public").status_code == 404 and c.get("/merchant/vouchers", headers=H).status_code in (404, 405)
    # 14 concurrency over HTTP: 8 simultaneous creates, 3 free slots left
    settings(free_product_limit=10); wipe(); seed(7)
    orders = [c.post("/merchant/products/order", json={"store_id": SID, "end_date": END}, headers=H).json() for _ in range(8)]
    assert all(x["pricing_tier"] == "free" for x in orders)          # all quoted free (stale quotes)
    res = []
    def go(o):
        cc = TestClient(server.app); res.append(cc.post("/merchant/products/activate-free", json={**body(), "order_id": o["order_id"]}, headers=H).status_code)
    ts = [threading.Thread(target=go, args=(o,)) for o in orders]; [t.start() for t in ts]; [t.join() for t in ts]
    assert res.count(200) == 3 and res.count(409) == 5, res
    assert fake.merchant_products.count_documents({"merchant_id": MID}) == 10
    # 15 invalid end dates / missing settings
    r = c.post("/merchant/products/order", json={"store_id": SID, "end_date": "2020-01-01"}, headers=H); assert r.status_code == 400
    fake.pricing.update_one({}, {"$unset": {"free_product_limit": ""}})
    r = c.get("/merchant/products/slots", headers=H); assert r.status_code == 500 and "free_product_limit" in r.text
    # 16 startup seeding preserves legacy admin value & never overwrites
    fake.pricing.delete_many({}); fake.pricing.insert_one({"gst_percent": 18, "standard_product_limit": 12})
    assert "free_product_limit" in P.ensure_settings() and fake.pricing.find_one()["free_product_limit"] == 12
    assert P.ensure_settings() == []


def test_duration_pricing_and_dashboard_fields():
    fake = sys.modules["database"].db
    ymd = lambda n: (P.ist_today() + timedelta(days=n)).strftime("%Y-%m-%d")
    settings(); wipe(); seed(10)
    # price = price_per_day x days (inclusive), GST on top
    for end_off, days, base, total in ((0, 1, 10, 11.8), (6, 7, 70, 82.6), (44, 45, 450, 531)):
        o = c.post("/merchant/products/order", json={"store_id": SID, "end_date": ymd(end_off)}, headers=H).json()
        assert (o["days"], o["pricing_tier"], o["base_price"], o["amount_display"]) == (days, "paid", base, total), o
    # slots endpoint previews price for N days
    s = c.get("/merchant/products/slots", params={"days": 10, "store_id": SID}, headers=H).json()
    assert s["price_per_day"] == 10 and s["total"] == 118 and s["days"] == 10 and "additional_price" not in s
    # max validity enforced (366 days > 365)
    assert c.post("/merchant/products/order", json={"store_id": SID, "end_date": ymd(365)}, headers=H).status_code == 400
    assert c.post("/merchant/products/order", json={"store_id": SID, "end_date": ymd(364)}, headers=H).status_code == 200
    # free product: price 0 whatever the duration
    wipe(); seed(2)
    o = c.post("/merchant/products/order", json={"store_id": SID, "end_date": ymd(100)}, headers=H).json()
    assert o["pricing_tier"] == "free" and o["amount_paise"] == 0 and o["days"] == 101
    # future start date is honoured: days counted from the start, product hidden until it starts
    o = c.post("/merchant/products/order", json={"store_id": SID, "from_date": ymd(5), "end_date": ymd(14)}, headers=H).json()
    assert o["days"] == 10
    r = c.post("/merchant/products/activate-free", json={**body("Future"), "order_id": o["order_id"]}, headers=H); assert r.status_code == 200
    pf = fake.merchant_products.find_one({"title": "Future"}); assert pf["duration_days"] == 10 and pf["start_date"] == P.ist_today() + timedelta(days=5)
    assert c.post("/merchant/products/order", json={"store_id": SID, "from_date": ymd(-1), "end_date": ymd(5)}, headers=H).status_code == 400
    # renewal bills only the ADDED days
    wipe(); seed(10)
    live = fake.merchant_products.insert_one({"merchant_id": MID, "store_id": SID, "status": "approved", "is_active": True, "title": "L",
        "start_date": P.ist_today(), "end_date": P.ist_today() + timedelta(days=9), "duration_days": 10}).inserted_id
    ro = c.post(f"/merchant/products/{live}/renew", json={"end_date": ymd(19)}, headers=H).json()
    assert ro["days"] == 10 and ro["base_price"] == 100 and ro["amount_display"] == 118, ro                   # +10 days
    assert c.post(f"/merchant/products/{live}/renew", json={"end_date": ymd(9)}, headers=H).status_code == 400  # not after current end
    exp = fake.merchant_products.insert_one({"merchant_id": MID, "store_id": SID, "status": "expired", "is_active": False, "title": "X",
        "start_date": P.ist_today() - timedelta(days=40), "end_date": P.ist_today() - timedelta(days=3)}).inserted_id
    ro = c.post(f"/merchant/products/{exp}/renew", json={"end_date": ymd(6)}, headers=H).json()
    assert ro["days"] == 7 and ro["base_price"] == 70, ro                                                      # today..+6 = 7 days
    # rate stored per renewal; later rate change does not touch history
    rv = c.post(f"/merchant/products/{live}/renew/verify", json={}, headers=H); assert rv.status_code == 200, rv.text
    pl = fake.merchant_products.find_one({"_id": live}); assert pl["renewals"][0]["unit_price"] == 100 and pl["renewals"][0]["added_days"] == 10
    assert pl["duration_days"] == 20
    # admin dashboard row: duration + paid status/amount
    wipe(); seed(10)
    ab = lambda **k: {"title": "A", "text": "t", "price": 5, "logo": "data:image/png;base64,xx", "store_id": SID, "from_date": ymd(0), "end_date": ymd(29), **k}
    r = c.post("/admin/products", json=ab(pay_mode="UPI", pay_amount=354, pay_ref="U"), headers=A); assert r.status_code == 200, r.text
    wipe(); seed(3); assert c.post("/admin/products", json=ab(), headers=A).status_code == 200
    rows = {x["pricing_tier"]: x for x in c.get("/admin/products", headers=A).json() if x["title"] == "A"}
    assert rows["free"]["duration_days"] == 30 and rows["free"]["paid_status"] == "free" and rows["free"]["paid_amount"] == 0
    wipe(); seed(10); c.post("/admin/products", json=ab(pay_mode="UPI", pay_amount=400, pay_ref="U"), headers=A)
    row = [x for x in c.get("/admin/products", headers=A).json() if x["title"] == "A"][0]
    assert (row["paid_status"], row["paid_amount"], row["expected_amount"], row["duration_days"]) == ("paid", 400.0, 354.0, 30)
    fake.pricing.update_one({}, {"$set": {"product_price_per_day": 77}})                                       # history unchanged
    row = [x for x in c.get("/admin/products", headers=A).json() if x["title"] == "A"][0]; assert row["paid_amount"] == 400.0
    # admin edit: changing dates recomputes duration; unchanged past dates on an expired product do not block edits
    pid = row["_id"]
    assert c.put(f"/admin/products/{pid}", json={"from_date": ymd(0), "end_date": ymd(9)}, headers=A).status_code == 200
    assert fake.merchant_products.find_one({"_id": ObjectId(pid)})["duration_days"] == 10
    ex = str(fake.merchant_products.insert_one({"merchant_id": MID, "store_id": SID, "status": "expired", "title": "E", "image_url": "http://img/e.png", "start_date": P.ist_today() - timedelta(days=20),
        "end_date": P.ist_today() - timedelta(days=5), "duration_days": 16}).inserted_id)
    assert c.put(f"/admin/products/{ex}", json={"title": "E2", "from_date": ymd(-20), "end_date": ymd(-5)}, headers=A).status_code == 200


def test_product_price_per_day_setting():
    fake = sys.modules["database"].db
    settings(); fake.pricing.update_one({}, {"$unset": {"product_price_per_day": ""}, "$set": {"voucher_price_per_day": 12}})
    assert P.get_settings()["product_price_per_day"] == 12                       # existing configured value is reused
    assert P.ensure_settings() == ["product_price_per_day"] and fake.pricing.find_one()["product_price_per_day"] == 12
    # the dashboard's "Product Price per Day" now actually persists (it used to be silently dropped)
    assert c.put("/admin/banner-pricing", json={"banner_price_per_day": 15, "product_price_per_day": 8}, headers=A).status_code == 200
    assert c.get("/admin/banner-pricing", headers=A).json()["product_price_per_day"] == 8
    assert P.get_settings()["product_price_per_day"] == 8
    assert c.put("/admin/banner-pricing", json={"product_price_per_day": -1}, headers=A).status_code == 400
    # additional_product_price is gone from the settings API
    g = c.get("/admin/pricing", headers=A).json(); assert "additional_product_price" not in g and "free_product_limit" in g
    c.put("/admin/pricing", json={"additional_product_price": 500}, headers=A); assert "additional_product_price" not in fake.pricing.find_one()


def test_store_quota_via_api():
    """Per-store quota end to end: two stores of one merchant, ownership checks, admin create for a store."""
    settings(); wipe()
    S2 = str(fake.stores.insert_one({"store_name": "S2", "city": "Pune", "status": "active", "merchant_id": MID}).inserted_id)
    OTHER = str(fake.stores.insert_one({"store_name": "X", "city": "Pune", "status": "active", "merchant_id": "someone-else"}).inserted_id)
    for _ in range(10):
        fake.merchant_products.insert_one({"merchant_id": MID, "store_id": SID, "status": "approved", "is_active": True,
            "end_date": P.ist_today()+timedelta(days=20), "start_date": P.ist_today(), "title": "s"})
    g = lambda **q: c.get("/merchant/products/slots", params=q, headers=H).json()
    s1, s2 = g(store_id=SID), g(store_id=S2)
    assert (s1["used"], s1["next_is_free"], s1["scope"]) == (10, False, "store")      # store 1 full
    assert (s2["used"], s2["next_is_free"], s2["free_remaining"]) == (0, True, 10)    # store 2 untouched
    ov = g()                                                                          # no store -> combined overview
    assert ov["scope"] == "merchant" and ov["used"] == 10 and ov["free_limit"] == 20 and ov["per_store_limit"] == 10
    assert {x["store_id"]: x["used"] for x in ov["stores"]} == {SID: 10, S2: 0}
    # orders: store 1 #11 paid, store 2 free; a store is mandatory; someone else's store is refused
    o1 = c.post("/merchant/products/order", json={"store_id": SID, "end_date": END}, headers=H).json()
    o2 = c.post("/merchant/products/order", json={"store_id": S2, "end_date": END}, headers=H).json()
    assert (o1["pricing_tier"], o1["amount_display"], o1["store_id"]) == ("paid", 354, SID)
    assert (o2["pricing_tier"], o2["amount_display"], o2["store_id"]) == ("free", 0, S2)
    assert c.post("/merchant/products/order", json={"end_date": END}, headers=H).status_code == 400
    assert c.post("/merchant/products/order", json={"store_id": OTHER, "end_date": END}, headers=H).status_code == 403
    assert c.get("/merchant/products/slots", params={"store_id": OTHER}, headers=H).status_code == 403
    # an order quoted for store 2 cannot be used to create a product in store 1 (and vice versa)
    pl = {**body("mix"), "order_id": o2["order_id"], "end_date": END}              # body() carries store 1
    assert c.post("/merchant/products/activate-free", json=pl, headers=H).status_code == 400
    # creating in store 2 through the normal flow stores both merchant_id and store_id
    pl2 = {**body("ok"), "store_id": S2, "order_id": o2["order_id"], "end_date": END}
    r = c.post("/merchant/products/activate-free", json=pl2, headers=H); assert r.status_code == 200, r.text
    doc = fake.merchant_products.find_one({"_id": ObjectId(r.json()["product_id"])})
    assert doc["store_id"] == S2 and doc["merchant_id"] == MID and doc["pricing_tier"] == "free"
    # admin: for store 2 (1 used) -> free; for store 1 (full) -> needs payment; no merchant/store -> OffrO-owned
    ab = lambda **k: {"title": "A", "text": "t", "end_date": END, "logo": "data:image/png;base64,xx", **k}
    r = c.post("/admin/products", json=ab(store_id=S2), headers=A); assert r.status_code == 200, r.text
    assert r.json()["pricing_tier"] == "free"
    assert c.post("/admin/products", json=ab(store_id=SID), headers=A).status_code == 409
    r = c.post("/admin/products", json=ab(store_id=SID, pay_mode="cash", pay_amount=354), headers=A); assert r.status_code == 200, r.text
    assert r.json()["pricing_tier"] == "paid" and r.json()["charged_amount"] == 354
    assert c.post("/admin/products", json=ab(merchant_id=MID), headers=A).status_code == 400   # merchant but no store
    r = c.post("/admin/products", json=ab(), headers=A); assert r.status_code == 200, r.text
    own = fake.merchant_products.find_one({"_id": ObjectId(r.json()["id"])})
    assert own["pricing_tier"] == "admin" and own["merchant_id"] is None and own["source"] == "admin"
    assert g(store_id=SID)["used"] == 11 and g(store_id=S2)["used"] == 2             # OffrO-owned consumed nothing


def test_one_day_validity_and_admin_image_api():
    settings(); wipe()
    ymd = lambda n: (P.ist_today() + timedelta(days=n)).strftime("%Y-%m-%d")
    # Add Product (merchant): 1 / 2 / 3 days
    for off, days in ((0, 1), (1, 2), (2, 3)):
        o = c.post("/merchant/products/order", json={"store_id": SID, "end_date": ymd(off)}, headers=H)
        assert o.status_code == 200 and o.json()["days"] == days, o.text
    o = c.post("/merchant/products/order", json={"store_id": SID, "from_date": ymd(0), "end_date": ymd(0)}, headers=H).json()
    r = c.post("/merchant/products/activate-free", json={**body("One"), "order_id": o["order_id"], "end_date": ymd(0)}, headers=H)
    assert r.status_code == 200, r.text
    one = fake.merchant_products.find_one({"title": "One"}); assert one["duration_days"] == 1 and one["start_date"] == one["end_date"]
    # Renewal: exactly 1 day added
    ro = c.post(f"/merchant/products/{one['_id']}/renew", json={"end_date": ymd(1)}, headers=H); assert ro.status_code == 200, ro.text
    assert ro.json()["days"] == 1
    # Admin Add with a 1-day validity, image required
    ab = lambda **k: {"title": "A", "text": "t", "store_id": SID, "from_date": ymd(0), "end_date": ymd(0), "logo": "data:image/png;base64,xx", **k}
    r = c.post("/admin/products", json=ab(), headers=A); assert r.status_code == 200, r.text
    pid = r.json()["id"]; assert fake.merchant_products.find_one({"_id": ObjectId(pid)})["duration_days"] == 1
    r = c.post("/admin/products", json=ab(logo=""), headers=A); assert r.status_code == 400 and "image is required" in r.json()["detail"]
    r = c.post("/admin/products", json={k: v for k, v in ab().items() if k not in ("logo",)}, headers=A); assert r.status_code == 400
    # Admin Edit: existing image counts; dates can be changed to 1 day / 2 days / 3 days
    for end, days in ((ymd(1), 2), (ymd(2), 3), (ymd(0), 1)):
        r = c.put(f"/admin/products/{pid}", json={"title": "A2", "end_date": end}, headers=A); assert r.status_code == 200, r.text
        assert fake.merchant_products.find_one({"_id": ObjectId(pid)})["duration_days"] == days
    assert c.put(f"/admin/products/{pid}", json={"end_date": ymd(-1)}, headers=A).status_code == 400   # before start
    # Edit a product that has no image at all: refused until one is provided
    bare = fake.merchant_products.insert_one({"merchant_id": MID, "store_id": SID, "status": "approved", "is_active": True, "title": "B",
        "image_url": "", "pricing_tier": "free", "start_date": P.ist_today(), "end_date": P.ist_today() + timedelta(days=5)}).inserted_id
    r = c.put(f"/admin/products/{bare}", json={"title": "B2"}, headers=A); assert r.status_code == 400 and "image is required" in r.json()["detail"]
    assert c.put(f"/admin/products/{bare}", json={"title": "B2", "logo": "data:image/png;base64,xx"}, headers=A).status_code == 200
