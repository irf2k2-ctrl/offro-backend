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
def seed(n, status="approved", active=True, end_days=30, merchant=M):
    for i in range(n):
        db.merchant_products.insert_one({"merchant_id": str(merchant["_id"]), "status": status,
            "is_active": active, "end_date": P.ist_today() + timedelta(days=end_days)})
def f(t="x"): return {"title": t}
def tier(): return P.decide(str(M["_id"]))["tier"]

def test_product_service():
    reset(); seed(7); assert tier()=="free"; d=P.admin_create(merchant=M, fields=f(), end_raw=END); assert d["pricing_tier"]=="free"
    reset(); seed(10); assert tier()=="paid"
    try: P.admin_create(merchant=M, fields=f(), end_raw=END); assert 0
    except P.ProductError as e: assert e.status_code==409 and "₹10/day" in e.detail and "30 days" in e.detail, e.detail
    d=P.admin_create(merchant=M, fields=f(), end_raw=END, payment={"payment_mode":"cash","amount":354}); assert d["pricing_tier"]=="paid" and d["charged_amount"]==354
    reset(); seed(15); assert tier()=="paid"
    # statuses
    reset(); seed(3,"deleted"); seed(3,"expired"); seed(3,"rejected"); seed(2,"approved",end_days=-1); assert P.qualifying_count(str(M["_id"]))==0
    seed(4,active=False); assert P.qualifying_count(str(M["_id"]))==4   # deactivated counts
    # limit change 10->15 takes effect immediately, no code change
    reset(10); seed(10); assert tier()=="paid"; db.pricing.update_one({}, {"$set":{"free_product_limit":15}}); assert tier()=="free"
    db.pricing.update_one({}, {"$set":{"free_product_limit":5}}); assert tier()=="paid"
    # missing setting -> clear error, no silent default
    db.pricing.update_one({}, {"$unset":{"free_product_limit":""}})
    try: P.decide(str(M["_id"])); assert 0
    except P.ProductError as e: assert "free_product_limit" in e.detail
    assert P.ensure_settings()==["free_product_limit"]; assert db.pricing.find_one()["product_price_per_day"]==10
    # admin-owned: no quota
    reset(); seed(10); d=P.admin_create(merchant=None, fields=f(), end_raw=END); assert d["pricing_tier"]=="admin" and d["merchant_id"] is None and d["source"]=="admin"
    assert tier()=="paid" and P.qualifying_count(str(M["_id"]))==10
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
    reset(); seed(10,"pending"); assert tier()=="paid" and P.qualifying_count(str(M["_id"]))==10
    reset(); seed(6,"pending"); seed(4); assert tier()=="paid"
    # Historical pricing never changes: settings change + renewal leave original record intact
    reset(); seed(10); d=P.admin_create(merchant=M, fields=f(), end_raw=END, payment={"payment_mode":"cash","amount":354})
    db.pricing.update_one({},{"$set":{"product_price_per_day":99,"free_product_limit":50}})
    x=db.merchant_products.find_one({"_id":d["_id"]}); assert (x["pricing_tier"],x["unit_price"],x["charged_amount"],x["expected_amount"])==("paid",300.0,354.0,354.0)
    r=P.apply_renewal(x, (datetime.utcnow()+timedelta(days=60)).strftime("%Y-%m-%d"))
    assert (r["pricing_tier"],r["unit_price"],r["charged_amount"])==("paid",300.0,354.0) and len(r["renewals"])==1 and r["renewals"][0]["pricing_tier"]=="free"


def test_admin_created_follows_merchant_quota():
    """Pricing/quota is decided by merchant_id, never by who created the product."""
    reset(limit=10); seed(9)
    d10 = P.admin_create(merchant=M, fields=f(), end_raw=END)             # #10 -> FREE
    assert d10["pricing_tier"] == "free" and d10["source"] == "admin" and d10["charged_amount"] == 0
    assert tier() == "paid"                                               # slots now used up
    try:
        P.admin_create(merchant=M, fields=f(), end_raw=END); assert 0     # #11 needs payment
    except P.ProductError as e:
        assert e.status_code == 409
    d11 = P.admin_create(merchant=M, fields=f(), end_raw=END, payment={"payment_mode": "cash", "amount": 354})
    assert d11["pricing_tier"] == "paid" and d11["merchant_id"] == P.merchant_key(M) and d11["source"] == "admin"
    assert d11["charged_amount"] == 354
    # OffrO-owned (no merchant): outside every merchant's quota, no charge
    used_before = P.decide(P.merchant_key(M))["used"]
    own = P.admin_create(merchant=None, fields=f(), end_raw=END)
    assert own["pricing_tier"] == "admin" and own["merchant_id"] is None and own["source"] == "admin"
    assert own["charged_amount"] == 0 and P.decide(P.merchant_key(M))["used"] == used_before
