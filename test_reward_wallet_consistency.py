"""Reward points: a QR scan credit is visible on the very next /user/wallet read (what Home and the Rewards screen both call).
Run:  python -m pytest test_reward_wallet_consistency.py -q -W ignore"""
import sys
from datetime import datetime, timedelta

from bson import ObjectId
from fastapi.testclient import TestClient

import server

fake = sys.modules["database"].db
c = TestClient(server.app)
TOKEN = "tok-reward"
H = {"Authorization": "Bearer " + TOKEN}


def setup(visit=100, pool=7, per_scan=10, stores=1):
    uid = fake.accounts.insert_one({"token": TOKEN, "phone": "9876543210", "name": "A", "visit_points": visit, "pool_points": pool}).inserted_id
    sids = [str(fake.stores.insert_one({"store_name": "S%d" % i, "status": "active", "points_per_scan": per_scan, "merchant_id": "m"}).inserted_id)
            for i in range(stores)]
    return uid, sids


def scan(sid):
    return c.post("/user/redeem", json={"store_id": sid, "user_token": TOKEN})


def wallet():
    r = c.get("/user/wallet", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_home_balance_call_returns_the_new_balance_immediately_after_a_scan():
    _, (sid,) = setup(visit=100, pool=7, per_scan=10)
    assert wallet()["visit_points"] == 100
    r = scan(sid)
    assert r.status_code == 200 and r.json()["points_earned"] == 10
    assert wallet()["visit_points"] == 110          # no second screen / refresh trick needed


def test_repeated_reads_are_not_cached_and_follow_each_credit():
    _, sids = setup(visit=0, pool=0, per_scan=5, stores=3)
    seen = []
    for sid in sids:
        assert scan(sid).status_code == 200
        seen.append(wallet()["visit_points"])
    assert seen == [5, 10, 15]


def test_scan_response_total_matches_wallet_total_and_both_screens_agree():
    _, (sid,) = setup(visit=40, pool=2, per_scan=10)
    r = scan(sid).json()
    w = wallet()
    assert r["total_points"] == w["total_points"] == 52
    assert w["visit_points"] == 50 and w["pool_points"] == 2        # Home shows visit_points, Rewards shows the same fields


def test_scan_is_recorded_in_history_and_second_scan_same_day_does_not_credit():
    _, (sid,) = setup(visit=0, pool=0, per_scan=10)
    assert scan(sid).status_code == 200
    again = scan(sid)
    assert again.status_code == 429
    assert wallet()["visit_points"] == 10                            # reward rule unchanged: one credit per store per 24h
    h = c.get("/user/redemptions", headers=H).json()
    assert len(h) == 1 and h[0]["points"] == 10 and h[0]["store_name"] == "S0"


def test_scan_after_24_hours_credits_again_and_wallet_follows():
    uid, (sid,) = setup(visit=0, pool=0, per_scan=10)
    scan(sid)
    fake.redemptions.update_many({}, {"$set": {"created_at": datetime.utcnow() - timedelta(hours=25)}})
    assert scan(sid).status_code == 200
    assert wallet()["visit_points"] == 20


def test_failed_scan_leaves_the_balance_unchanged():
    setup(visit=30, pool=0)
    assert scan(str(ObjectId())).status_code == 404
    assert c.post("/user/redeem", json={"store_id": "nope", "user_token": TOKEN}).status_code == 400
    assert wallet()["visit_points"] == 30
