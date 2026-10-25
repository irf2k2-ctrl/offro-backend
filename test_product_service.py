"""Tests for services/products.py (shared mongomock DB from conftest.py).
Run:  python -m pytest test_product_service.py -q -W ignore"""
import sys, threading
from datetime import datetime, timedelta
from bson import ObjectId
from services import products as P
db = sys.modules["database"].db

def reset(limit=10, price=10):
    for c in db.list_collection_names(): db[c].drop()
    db.pricing.insert_one({"free_product_limit": limit, "product_price_per_day": price,
                           "product_max_validity_days": 365, "gst_percent": 18})
M = {"_id": ObjectId(), "name": "M", "phone": "1"}
END = (P.ist_today() + timedelta(days=29)).strftime("%Y-%m-%d")   # today..END = 30 days
S1, S2 = "store-1", "store-2"          # two stores of the SAME merchant M
def seed(n, status="approved", active=True, end_days=30, merchant=M, store=S1):
    for i in range(n):
        db.merchant_products.insert_one({"merchant_id": str(merchant["_id"]), "store_id": store, "status": status,
            "is_active": active, "end_date": P.ist_today() + timedelta(days=end_days)})
def f(t="x", store=S1): return {"title": t, "store_id": store}
def tier(store=S1): return P.decide(store)["tier"]

def test_product_service():
    reset(); seed(7); assert tier()=="free"; d=P.admin_create(merchant=M, fields=f(), end_raw=END); assert d["pricing_tier"]=="free"
    reset(); seed(10); assert tier()=="paid"
    try: P.admin_create(merchant=M, fields=f(), end_raw=END); assert 0
    except P.ProductError as e: assert e.status_code==409 and "₹10/day" in e.detail and "30 days" in e.detail, e.detail
    d=P.admin_create(merchant=M, fields=f(), end_raw=END, payment={"payment_mode":"cash","amount":354}); assert d["pricing_tier"]=="paid" and d["charged_amount"]==354
    reset(); seed(15); assert tier()=="paid"
    # statuses
    reset(); seed(3,"deleted"); seed(3,"expired"); seed(3,"rejected"); seed(2,"approved",end_days=-1); assert P.qualifying_count(S1)==0
    seed(4,active=False); assert P.qualifying_count(S1)==4   # deactivated counts
    # limit change 10->15 takes effect immediately, no code change
    reset(10); seed(10); assert tier()=="paid"; db.pricing.update_one({}, {"$set":{"free_product_limit":15}}); assert tier()=="free"
    db.pricing.update_one({}, {"$set":{"free_product_limit":5}}); assert tier()=="paid"
    # missing setting -> clear error, no silent default
    db.pricing.update_one({}, {"$unset":{"free_product_limit":""}})
    try: P.decide(S1); assert 0
    except P.ProductError as e: assert "free_product_limit" in e.detail
    assert P.ensure_settings()==["free_product_limit"]; assert db.pricing.find_one()["product_price_per_day"]==10
    # admin-owned: no quota
    reset(); seed(10); d=P.admin_create(merchant=None, fields=f(), end_raw=END); assert d["pricing_tier"]=="admin" and d["merchant_id"] is None and d["source"]=="admin"
    assert tier()=="paid" and P.qualifying_count(S1)==10
    # delete frees slot; renewal
    reset(); seed(10); first=db.merchant_products.find_one(); P.soft_delete(first); assert tier()=="free"
    first=db.merchant_products.find_one({"_id":first["_id"]})
    try: P.apply_renewal(first, END); assert 0
    except P.ProductError: pass
    reset(); seed(10); ex=db.merchant_products.find_one(); db.merchant_products.update_one({"_id":ex["_id"]},{"$set":{"status":"expired","end_date":P.ist_today()-timedelta(days=2)}})
    ex=db.merchant_products.find_one({"_id":ex["_id"]}); assert P.renew_decision(ex)["tier"]=="free"   # 9 other qualify
    r=P.apply_renewal(ex, END); assert r["status"]=="approved"
    seed(1); ex2=db.merchant_products.find_one({"_id":r["_id"]}); assert P.renew_decision(ex2)["tier"]=="paid"  # 10 OTHER qualifying products now
    # race: 12 concurrent free creations with 5 slots
    reset(5); res=[]
    def go():
        try: P.create_free(merchant=M, fields=f(), end_raw=END); res.append("ok")
        except P.ProductError as e: res.append(e.status_code)
    ts=[threading.Thread(target=go) for _ in range(12)]; [t.start() for t in ts]; [t.join() for t in ts]
    assert res.count("ok")==5, res; assert db.merchant_products.count_documents({})==5
    # Pending counts toward quota (no bypass by piling up pending products)
    reset(); seed(10,"pending"); assert tier()=="paid" and P.qualifying_count(S1)==10
    reset(); seed(6,"pending"); seed(4); assert tier()=="paid"
    # Historical pricing never changes: settings change + renewal leave original record intact
    reset(); seed(10); d=P.admin_create(merchant=M, fields=f(), end_raw=END, payment={"payment_mode":"cash","amount":354})
    db.pricing.update_one({},{"$set":{"product_price_per_day":99,"free_product_limit":50}})
    x=db.merchant_products.find_one({"_id":d["_id"]}); assert (x["pricing_tier"],x["unit_price"],x["charged_amount"],x["expected_amount"])==("paid",300.0,354.0,354.0)
    r=P.apply_renewal(x, (datetime.utcnow()+timedelta(days=60)).strftime("%Y-%m-%d"))
    assert (r["pricing_tier"],r["unit_price"],r["charged_amount"])==("paid",300.0,354.0) and len(r["renewals"])==1 and r["renewals"][0]["pricing_tier"]=="free"


def test_quota_is_per_store():
    """The free quota is counted per STORE (store_id), never per merchant. Same merchant M, two stores."""
    reset(limit=10)
    # 1. Store 1: first 10 free (merchant creates through the same service path)
    for i in range(10):
        assert tier(S1) == "free", i
        d = P.create_free(merchant=M, fields=f(store=S1), end_raw=END); assert d["store_id"] == S1 and d["merchant_id"] == P.merchant_key(M)
    # 3. Store 1 product #11 is paid
    assert tier(S1) == "paid"
    try: P.create_free(merchant=M, fields=f(store=S1), end_raw=END); assert 0
    except P.ProductError as e: assert e.status_code == 409
    # 2/5. Store 2 (same merchant) is untouched: its own 10 free
    assert P.decide(S2)["used"] == 0
    for i in range(10):
        assert tier(S2) == "free", i
        P.create_free(merchant=M, fields=f(store=S2), end_raw=END)
    # 4. Store 2 product #11 is paid independently
    assert tier(S2) == "paid" and P.decide(S2)["used"] == 10 and P.decide(S1)["used"] == 10
    assert P.decide(S2, days=30)["total"] == 354.0
    # a third store of the same merchant still has a full free quota
    assert P.decide("store-3")["tier"] == "free" and P.decide("store-3")["used"] == 0
    # freeing a slot in store 1 does not free one in store 2
    P.soft_delete(db.merchant_products.find_one({"store_id": S1})); assert tier(S1) == "free" and tier(S2) == "paid"
    # a product with no store id can never be created through the merchant path
    try: P.create_free(merchant=M, fields={"title": "x"}, end_raw=END); assert 0
    except P.ProductError as e: assert e.status_code == 400 and "store" in e.detail.lower()


def test_admin_created_follows_store_quota():
    """Admin-created pricing/quota follows the STORE (and its merchant), never who created it."""
    reset(limit=10); seed(9, store=S1); seed(10, store=S2)
    # 6. store 1 has 9 -> admin product #10 FREE, #11 PAID; store 2 (full) is unaffected by it
    d10 = P.admin_create(merchant=M, fields=f(store=S1), end_raw=END)
    assert d10["pricing_tier"] == "free" and d10["source"] == "admin" and d10["charged_amount"] == 0 and d10["store_id"] == S1
    assert tier(S1) == "paid"
    try: P.admin_create(merchant=M, fields=f(store=S1), end_raw=END); assert 0
    except P.ProductError as e: assert e.status_code == 409
    try: P.admin_create(merchant=M, fields=f(store=S1), end_raw=END, payment={"payment_mode": "cash", "amount": 100}); assert 0
    except P.ProductError as e: assert e.status_code == 400                  # below the expected 354
    d11 = P.admin_create(merchant=M, fields=f(store=S1), end_raw=END, payment={"payment_mode": "cash", "amount": 354})
    assert d11["pricing_tier"] == "paid" and d11["source"] == "admin" and d11["charged_amount"] == 354
    assert d11["merchant_id"] == P.merchant_key(M) and d11["store_id"] == S1
    assert P.decide(S1)["used"] == 11 and P.decide(S2)["used"] == 10
    # store 2 is full on its own: an admin product there is paid, though the admin made it
    try: P.admin_create(merchant=M, fields=f(store=S2), end_raw=END); assert 0
    except P.ProductError as e: assert e.status_code == 409
    # a merchant but no store -> cannot be priced, must be rejected clearly
    try: P.admin_create(merchant=M, fields={"title": "x"}, end_raw=END); assert 0
    except P.ProductError as e: assert e.status_code == 400
    # 7. no merchant (OffrO-owned): no quota, no charge, source stays admin - even if it names a store
    before = (P.decide(S1)["used"], P.decide(S2)["used"])
    own = P.admin_create(merchant=None, fields=f(store=S1), end_raw=END)
    own2 = P.admin_create(merchant=None, fields={"title": "o"}, end_raw=END)
    for o in (own, own2):
        assert o["pricing_tier"] == "admin" and o["merchant_id"] is None and o["source"] == "admin" and o["charged_amount"] == 0
    assert (P.decide(S1)["used"], P.decide(S2)["used"]) == before and tier(S1) == "paid"


def test_renewal_uses_store_quota_and_keeps_history():
    reset(limit=10); seed(10, store=S2)
    d = P.create_free(merchant=M, fields=f(store=S1), end_raw=END)                 # store 1 has room
    doc = db.merchant_products.find_one({"_id": d["_id"]})
    assert P.renew_decision(doc, 5)["tier"] == "free"                              # store 2 being full does not matter
    seed(10, store=S1)                                                             # 10 OTHER products in store 1 -> a renewal is paid
    rd = P.renew_decision(doc, 5); assert rd["tier"] == "paid" and rd["used"] == 10
    new_end = (P.parse_date(END) + timedelta(days=5)).strftime("%Y-%m-%d")
    r = P.apply_renewal(doc, new_end, charged_amount=59.0, price={"price_per_day": 10, "unit_price": 50.0, "gst_percent": 18})
    assert (r["pricing_tier"], r["unit_price"], r["charged_amount"]) == ("free", 0.0, 0.0)   # original purchase untouched
    assert len(r["renewals"]) == 1 and r["renewals"][0]["charged_amount"] == 59.0 and r["store_id"] == S1


def test_renewal_without_store_fails_closed():
    reset(limit=10)
    d = db.merchant_products.insert_one({"merchant_id": str(M["_id"]), "store_id": "", "status": "approved", "is_active": True,
        "pricing_tier": "free", "start_date": P.ist_today(), "end_date": P.ist_today() + timedelta(days=5)}).inserted_id
    try: P.renew_decision(db.merchant_products.find_one({"_id": d}), 3); assert 0
    except P.ProductError as e: assert e.status_code == 409 and "store" in e.detail.lower()
    # OffrO-owned products are exempt (no merchant, no quota)
    o = P.admin_create(merchant=None, fields={"title": "o"}, end_raw=END)
    assert P.renew_decision(db.merchant_products.find_one({"_id": o["_id"]}), 3)["tier"] == "admin"


def test_one_day_validity():
    """1 day = start and end on the same date; 2 days = start+1; 3 days = start+2 (inclusive)."""
    reset(limit=10)
    today = P.ist_today()
    ymd = lambda n: (today + timedelta(days=n)).strftime("%Y-%m-%d")
    for off, days in ((0, 1), (1, 2), (2, 3), (29, 30)):
        s_, e_, n = P.validate_period(None, ymd(off)); assert (s_, e_, n) == (today, today + timedelta(days=off), days)
    # explicit start == end is a valid 1-day product, also for a future start date
    s_, e_, n = P.validate_period(ymd(5), ymd(5)); assert n == 1 and s_ == e_
    # end BEFORE start is still rejected
    try: P.validate_period(ymd(5), ymd(4)); assert 0
    except P.ProductError as e: assert e.status_code == 400
    # free 1-day product, then a paid 1-day product (price = per-day x 1 + GST)
    d = P.create_free(merchant=M, fields=f(), end_raw=ymd(0), from_raw=ymd(0))
    assert d["duration_days"] == 1 and d["start_date"] == d["end_date"] == today
    reset(limit=10); seed(10)
    assert P.decide(S1, days=1)["total"] == 11.8
    p = P.admin_create(merchant=M, fields=f(), end_raw=ymd(0), payment={"payment_mode": "cash", "amount": 11.8})
    assert p["pricing_tier"] == "paid" and p["duration_days"] == 1 and p["charged_amount"] == 11.8
    # renewal of exactly 1 day: live product ends today -> new end tomorrow bills 1 day; expired -> today..today
    live = db.merchant_products.find_one({"_id": p["_id"]})
    assert P.renewal_period(live, ymd(1))[1] == 1
    live["end_date"], live["status"] = today - timedelta(days=3), "expired"
    assert P.renewal_period(live, ymd(0))[1] == 1                      # expired: restart today, 1 day
    try: P.renewal_period(live, ymd(-1)); assert 0
    except P.ProductError as e: assert e.status_code == 400
