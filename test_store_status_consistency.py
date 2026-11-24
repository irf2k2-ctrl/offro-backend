"""Phase 5 / Issue 1 - admin, customer and merchant views agree on store status."""
from datetime import datetime, timedelta
from services import store_status as ss
from database import db


def test_valid_through_end_date_ist():
    now = datetime(2026, 11, 24, 20, 0)
    assert ss.is_expired("2026-11-23", now) is True
    assert ss.is_expired("2026-11-24", now) is False
    assert ss.is_expired("24 Nov 2026", now) is False
    assert ss.is_expired(None, now) is False and ss.is_expired("junk", now) is False


def test_effective_end_takes_latest():
    assert ss.effective_end("2026-01-01", [datetime(2027, 1, 1)]) == datetime(2027, 1, 1)
    assert ss.effective_end(None, []) is None


def test_unpaid_rows_do_not_count():
    assert ss.counts("paid") and ss.counts("active") and ss.counts(None)
    assert not ss.counts("pending") and not ss.counts("failed")


def _store(sub_end):
    r = db.stores.insert_one({"store_name": "test store stage", "status": "active",
                              "subscription_end": sub_end, "merchant_id": "m1", "city": "X"})
    return r.inserted_id


def test_stale_store_copy_but_paid_renewal_is_active():
    sid = _store(datetime.utcnow() - timedelta(days=30))
    db.subscriptions.insert_one({"store_id": str(sid), "status": "paid",
                                 "end_date": datetime.utcnow() + timedelta(days=20)})
    end = ss.store_effective_end(db, db.stores.find_one({"_id": sid}))
    assert ss.effective_status("active", end) == "active"


def test_abandoned_pending_renewal_does_not_extend():
    sid = _store(datetime.utcnow() - timedelta(days=30))
    db.subscriptions.insert_one({"store_id": str(sid), "status": "pending",
                                 "end_date": datetime.utcnow() + timedelta(days=300)})
    end = ss.store_effective_end(db, db.stores.find_one({"_id": sid}))
    assert ss.effective_status("active", end) == "expired"


def test_string_subscription_end_expires_too():
    sid = _store("2020-01-01")
    end = ss.store_effective_end(db, db.stores.find_one({"_id": sid}))
    assert ss.effective_status("active", end) == "expired"
