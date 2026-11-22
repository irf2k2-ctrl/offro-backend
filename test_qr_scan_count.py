"""Stage 4 Part B: the "QR Scans" count of Dashboard > Accounts (list + detail) follows the real scan flow (POST /user/redeem).

ROOT CAUSE (kept here as documentation): /user/redeem records a scan as a `redemptions` document and credits points, but the number
the dashboard shows is the stored `accounts.scans`, which nothing but the one-time migration ever set - so it never moved.
Fix: /user/redeem increments `scans` in the same update that credits the points (only after every check passed), and the dashboard
shows max(recorded redemptions, stored counter) so accounts whose counter fell behind are corrected too.
(file name sorts after test_banner_media.py on purpose: tests that import `server` must be collected after it.)
Run:  python -m pytest test_qr_scan_count.py -q -W ignore"""
import sys
from datetime import datetime, timedelta

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import server

DB = sys.modules["database"].db
web = TestClient(server.app)
ADMIN = {"Authorization": "Bearer admin-token"}


@pytest.fixture(autouse=True)
def world():
    rid = DB.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    DB.dashboard_users.insert_one({"token": "admin-token", "status": "active", "full_name": "A", "mobile": "9000000001", "role_id": rid,
                                   "assigned_cities": ["*"], "last_active_at": datetime.utcnow()})
    w = {}
    w["u1"] = DB.accounts.insert_one({"name": "Customer One", "phone": "9876543210", "roles": ["user"], "token": "tok1", "status": "active",
                                      "visit_points": 0}).inserted_id
    w["u2"] = DB.accounts.insert_one({"name": "Customer Two", "phone": "9876543211", "roles": ["user"], "token": "tok2", "status": "active",
                                      "visit_points": 0}).inserted_id
    w["m"] = DB.accounts.insert_one({"name": "Merchant", "phone": "9876543299", "roles": ["merchant"], "status": "active"}).inserted_id
    w["s1"] = DB.stores.insert_one({"store_name": "S1", "status": "active", "merchant_id": str(w["m"]), "points_per_scan": 10, "city": "X"}).inserted_id
    w["s2"] = DB.stores.insert_one({"store_name": "S2", "status": "active", "merchant_id": str(w["m"]), "points_per_scan": 25, "city": "X"}).inserted_id
    w["off"] = DB.stores.insert_one({"store_name": "Off", "status": "inactive", "merchant_id": str(w["m"]), "city": "X"}).inserted_id
    return w


def scan(token, store):
    return web.post("/user/redeem", json={"store_id": str(store), "user_token": token})


def shown(acct):
    """(count in the Accounts list, count in the account detail)"""
    lst = {r["account_id"]: r for r in web.get("/admin/accounts", headers=ADMIN).json()}
    paged = {r["account_id"]: r for r in web.get("/admin/accounts", headers=ADMIN, params={"page": 1, "page_size": 50}).json()["items"]}
    det = web.get(f"/admin/accounts/{acct}", headers=ADMIN).json()
    assert lst[str(acct)]["scans"] == paged[str(acct)]["scans"] == det["scans"]
    return det["scans"]


def test_empty_scan_history_shows_zero(world):
    assert shown(world["u1"]) == 0
    assert shown(world["m"]) == 0
    DB.accounts.update_one({"_id": world["u1"]}, {"$unset": {"scans": ""}})          # account that never had the field
    assert shown(world["u1"]) == 0


def test_successful_scan_is_recorded_and_the_count_increments(world):
    r = scan("tok1", world["s1"])
    assert r.status_code == 200, r.text
    assert shown(world["u1"]) == 1
    assert DB.accounts.find_one({"_id": world["u1"]})["scans"] == 1
    assert DB.redemptions.count_documents({"user_id": str(world["u1"]), "store_id": str(world["s1"])}) == 1


def test_multiple_successful_scans_accumulate(world):
    assert scan("tok1", world["s1"]).status_code == 200
    assert shown(world["u1"]) == 1
    assert scan("tok1", world["s2"]).status_code == 200
    assert shown(world["u1"]) == 2


def test_count_refreshes_on_every_read_without_a_restart(world):
    seen = []
    for st in (world["s1"], world["s2"]):
        seen.append(shown(world["u1"]))
        assert scan("tok1", st).status_code == 200
    seen.append(shown(world["u1"]))
    assert seen == [0, 1, 2]


@pytest.mark.parametrize("kind", ["unknown_token", "no_token", "bad_store_id", "missing_store", "inactive_store", "no_store"])
def test_invalid_or_failed_scans_are_not_counted(world, kind):
    payload = {"unknown_token": ("nope", world["s1"]), "bad_store_id": ("tok1", "not-an-id"), "missing_store": ("tok1", ObjectId()),
               "inactive_store": ("tok1", world["off"])}
    if kind == "no_token":
        r = web.post("/user/redeem", json={"store_id": str(world["s1"])})
    elif kind == "no_store":
        r = web.post("/user/redeem", json={"user_token": "tok1"})
    else:
        r = scan(*payload[kind])
    assert r.status_code >= 400
    assert shown(world["u1"]) == 0
    assert DB.redemptions.count_documents({}) == 0
    assert DB.accounts.find_one({"_id": world["u1"]})["visit_points"] == 0


def test_a_retry_or_duplicate_request_is_not_counted_twice(world):
    assert scan("tok1", world["s1"]).status_code == 200
    for _ in range(3):                                                            # retries / network re-sends / double taps
        assert scan("tok1", world["s1"]).status_code == 429
    assert shown(world["u1"]) == 1
    assert DB.redemptions.count_documents({}) == 1
    assert DB.accounts.find_one({"_id": world["u1"]})["visit_points"] == 10         # points were not doubled either


def test_the_same_store_again_after_24_hours_counts(world):
    assert scan("tok1", world["s1"]).status_code == 200
    DB.redemptions.update_many({}, {"$set": {"created_at": datetime.utcnow() - timedelta(hours=25)}})
    assert scan("tok1", world["s1"]).status_code == 200
    assert shown(world["u1"]) == 2


def test_the_count_belongs_to_the_scanning_account_only(world):
    scan("tok1", world["s1"]); scan("tok1", world["s2"]); scan("tok2", world["s1"])
    assert shown(world["u1"]) == 2
    assert shown(world["u2"]) == 1
    assert shown(world["m"]) == 0                                                   # scanning the merchant's stores is not the merchant's scan


def test_unrelated_redemptions_do_not_leak_into_the_count(world):
    DB.redemptions.insert_one({"user_id": str(world["u2"]), "store_id": str(world["s1"]), "points": 10, "created_at": datetime.utcnow()})
    DB.redemptions.insert_one({"user_id": "somebody-else", "store_id": str(world["s1"]), "points": 10, "created_at": datetime.utcnow()})
    assert shown(world["u1"]) == 0
    assert shown(world["u2"]) == 1


def test_legacy_counter_from_the_old_app_is_kept_and_continues(world):
    DB.accounts.update_one({"_id": world["u1"]}, {"$set": {"scans": 5}})            # migrated value, no redemptions behind it
    assert shown(world["u1"]) == 5                                                  # never lowered
    assert scan("tok1", world["s1"]).status_code == 200
    assert shown(world["u1"]) == 6                                                  # and it moves again


def test_account_whose_counter_fell_behind_the_records_is_corrected(world):
    for st in (world["s1"], world["s2"], world["s1"]):                              # three recorded scans, counter never incremented
        DB.redemptions.insert_one({"user_id": str(world["u1"]), "store_id": str(st), "points": 1, "created_at": datetime.utcnow() - timedelta(days=3)})
    DB.accounts.update_one({"_id": world["u1"]}, {"$set": {"scans": 0}})
    assert shown(world["u1"]) == 3


def test_garbage_counter_values_do_not_break_the_list(world):
    for bad, expected in (("x", 0), (None, 0), (-4, 0), (2.7, 2), ({}, 0), ("7", 7)):
        DB.accounts.update_one({"_id": world["u1"]}, {"$set": {"scans": bad}})
        r = web.get("/admin/accounts", headers=ADMIN)
        assert r.status_code == 200
        assert [x for x in r.json() if x["account_id"] == str(world["u1"])][0]["scans"] == expected, bad


def test_api_response_shape(world):
    scan("tok1", world["s1"])
    d = web.get(f"/admin/accounts/{world['u1']}", headers=ADMIN).json()
    assert isinstance(d["scans"], int) and d["scans"] == 1
    row = [x for x in web.get("/admin/accounts", headers=ADMIN).json() if x["account_id"] == str(world["u1"])][0]
    assert isinstance(row["scans"], int) and row["scans"] == 1


# ── the reward / redemption behaviour must be exactly what it was ──
def test_reward_behaviour_is_unchanged(world):
    r = scan("tok1", world["s2"])
    assert r.status_code == 200
    j = r.json()
    assert j["points_earned"] == 25 and j["store_name"] == "S2" and j["total_points"] == 25 and "25" in j["message"]
    a = DB.accounts.find_one({"_id": world["u1"]})
    assert a["visit_points"] == 25 and a["visit_pts"] == 25
    red = DB.redemptions.find_one({"user_id": str(world["u1"])})
    assert red["points"] == 25 and red["store_name"] == "S2" and red["merchant_id"] == str(world["m"])


def test_second_scan_message_and_status_are_unchanged(world):
    scan("tok1", world["s1"])
    r = scan("tok1", world["s1"])
    assert r.status_code == 429 and "Already redeemed" in r.json()["detail"]


def test_inactive_store_message_is_unchanged(world):
    r = scan("tok1", world["off"])
    assert r.status_code == 400 and r.json()["detail"] == "Store is not active"


# ═══════════════ atomicity: points and `scans` exist only for a recorded redemption ═══════════════
import routers.users as U  # noqa: E402


def _fail_once(monkeypatch, coll, method, apply_first=False):
    """Make the FIRST call of coll.method raise. apply_first=True: the write is applied and THEN the call raises (what a timeout after a
    successful write looks like) - the harder case for a rollback. Later calls (the rollback's own) behave normally."""
    real, calls = getattr(coll, method), []

    def wrapper(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            if apply_first:
                real(*a, **k)
            raise RuntimeError("simulated database failure")
        return real(*a, **k)
    monkeypatch.setattr(coll, method, wrapper)
    return calls


def _state(uid):
    a = DB.accounts.find_one({"_id": uid})
    return (a.get("visit_points", 0), a.get("visit_pts", 0), a.get("scans", 0), DB.redemptions.count_documents({}))


def _api(token, store):
    return TestClient(server.app, raise_server_exceptions=False).post("/user/redeem", json={"store_id": str(store), "user_token": token})


def test_redemption_insert_failure_changes_nothing(world, monkeypatch):
    before = _state(world["u1"])
    _fail_once(monkeypatch, U.db.redemptions, "insert_one")
    r = _api("tok1", world["s1"])
    assert r.status_code == 500
    assert _state(world["u1"]) == before == (0, 0, 0, 0)
    assert shown(world["u1"]) == 0
    assert scan("tok1", world["s1"]).status_code == 200                      # the user can simply try again
    assert _state(world["u1"]) == (10, 10, 1, 1)


@pytest.mark.parametrize("apply_first", [False, True])
def test_account_update_failure_leaves_no_points_scan_or_redemption(world, monkeypatch, apply_first):
    _fail_once(monkeypatch, U.db.accounts, "update_one", apply_first=apply_first)
    r = _api("tok1", world["s1"])
    assert r.status_code == 500
    assert _state(world["u1"]) == (0, 0, 0, 0)
    assert not DB.accounts.find_one({"_id": world["u1"]}).get("redemption_credits")
    assert shown(world["u1"]) == 0
    assert scan("tok1", world["s1"]).status_code == 200                      # not blocked by the 24h rule: the failed attempt left no record
    assert _state(world["u1"]) == (10, 10, 1, 1)


@pytest.mark.parametrize("apply_first", [False, True])
def test_legacy_user_sync_failure_rolls_back_the_account_credit_too(world, monkeypatch, apply_first):
    DB.accounts.update_one({"_id": world["u1"]}, {"$set": {"user_id": "legacy-1", "visit_points": 5, "visit_pts": 5, "scans": 2}})
    DB.users.insert_one({"token": "tok1", "visit_points": 5})
    _fail_once(monkeypatch, U.db.users, "update_one", apply_first=apply_first)
    r = _api("tok1", world["s1"])
    assert r.status_code == 500
    assert _state(world["u1"]) == (5, 5, 2, 0)
    assert DB.users.find_one({"token": "tok1"})["visit_points"] == 5
    assert not DB.users.find_one({"token": "tok1"}).get("redemption_credits")


def test_failed_cleanup_of_the_redemption_record_still_reverses_the_points(world, monkeypatch):
    _fail_once(monkeypatch, U.db.accounts, "update_one", apply_first=True)
    real_delete = U.db.redemptions.delete_one
    monkeypatch.setattr(U.db.redemptions, "delete_one", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert _api("tok1", world["s1"]).status_code == 500
    a = DB.accounts.find_one({"_id": world["u1"]})
    assert (a["visit_points"], a["visit_pts"], a["scans"]) == (0, 0, 0)       # no points / scan without a successful redemption
    monkeypatch.setattr(U.db.redemptions, "delete_one", real_delete)


def test_rollback_only_reverses_this_redemption_not_other_credits(world, monkeypatch):
    assert scan("tok1", world["s1"]).status_code == 200                      # an earlier, successful scan: 10 points, 1 scan
    _fail_once(monkeypatch, U.db.accounts, "update_one", apply_first=True)
    assert _api("tok1", world["s2"]).status_code == 500                      # second store fails
    assert _state(world["u1"]) == (10, 10, 1, 1)


def test_legacy_sync_and_response_still_work_when_nothing_fails(world):
    DB.accounts.update_one({"_id": world["u1"]}, {"$set": {"user_id": "legacy-1"}})
    DB.users.insert_one({"token": "tok1", "visit_points": 0})
    r = scan("tok1", world["s2"])
    assert r.status_code == 200
    assert r.json() == {"message": "✅ 25 points added!", "store_name": "S2", "points_earned": 25, "total_points": 25}
    assert DB.users.find_one({"token": "tok1"})["visit_points"] == 25
    assert _state(world["u1"]) == (25, 25, 1, 1)
    assert scan("tok1", world["s2"]).status_code == 429                      # duplicate-scan protection unchanged
    assert _state(world["u1"]) == (25, 25, 1, 1)


# ═══════════════ concurrent redemptions: one marker PER redemption ═══════════════
def _legacy(world):
    DB.accounts.update_one({"_id": world["u1"]}, {"$set": {"user_id": "legacy-1"}})
    DB.users.insert_one({"token": "tok1", "visit_points": 0})


def _no_markers(world):
    assert not DB.accounts.find_one({"_id": world["u1"]}).get("redemption_credits")
    assert not DB.users.find_one({"token": "tok1"}).get("redemption_credits")


def test_two_concurrent_redemptions_for_different_stores_are_both_credited(world):
    from concurrent.futures import ThreadPoolExecutor
    _legacy(world)
    with ThreadPoolExecutor(2) as ex:
        rs = list(ex.map(lambda st: scan("tok1", st), (world["s1"], world["s2"])))
    assert [r.status_code for r in rs] == [200, 200]
    assert _state(world["u1"]) == (35, 35, 2, 2)
    assert DB.users.find_one({"token": "tok1"})["visit_points"] == 35
    _no_markers(world)


@pytest.mark.parametrize("fail_target", ["users", "accounts"])
def test_failure_of_a_redemption_interleaved_with_another_successful_one_reverses_only_the_failed_one(world, monkeypatch, fail_target):
    """A (store S1) credits the account and then FAILS after its write was applied; while it is still in flight, B (store S2) runs to
    completion for the same account. A's rollback must find ITS OWN credit (a single shared marker would have been overwritten by B)
    and reverse only that: B's 25 points and its scan remain, A's 10 points and its scan are gone."""
    _legacy(world)
    coll = getattr(U.db, fail_target)
    real, state = coll.update_one, {"n": 0}

    def wrapper(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            real(*a, **k)                                                       # A's credit on this collection IS applied ...
            assert scan("tok1", world["s2"]).status_code == 200                # ... B completes meanwhile ...
            raise RuntimeError("simulated database failure")                   # ... and then A fails
        return real(*a, **k)
    monkeypatch.setattr(coll, "update_one", wrapper)
    assert _api("tok1", world["s1"]).status_code == 500
    assert _state(world["u1"]) == (25, 25, 1, 1)                               # only B remains: points, scan and record
    assert DB.users.find_one({"token": "tok1"})["visit_points"] == 25
    assert DB.redemptions.count_documents({"store_id": str(world["s2"])}) == 1
    assert DB.redemptions.count_documents({"store_id": str(world["s1"])}) == 0
    _no_markers(world)
    assert shown(world["u1"]) == 1


def test_concurrent_threads_one_failing_one_succeeding(world, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    _legacy(world)
    real = U.db.users.update_one

    def wrapper(flt, upd, *a, **k):
        r = real(flt, upd, *a, **k)
        if upd.get("$inc", {}).get("visit_points") == 10:                      # only store S1's credit (10 points) fails, after being applied
            raise RuntimeError("simulated database failure")
        return r
    monkeypatch.setattr(U.db.users, "update_one", wrapper)
    with ThreadPoolExecutor(2) as ex:
        rs = list(ex.map(lambda st: _api("tok1", st), (world["s1"], world["s2"])))
    assert sorted(r.status_code for r in rs) == [200, 500]
    assert _state(world["u1"]) == (25, 25, 1, 1)
    assert DB.users.find_one({"token": "tok1"})["visit_points"] == 25
    _no_markers(world)


def test_the_credit_is_idempotent_per_redemption(world):
    _legacy(world)
    user = DB.accounts.find_one({"_id": world["u1"]})
    U._credit_redemption(user, "tok1", "r-1", 10)
    U._credit_redemption(user, "tok1", "r-1", 10)                               # the same redemption applied again: no second credit
    U._credit_redemption(user, "tok1", "r-2", 25)                               # another redemption: independent
    a = DB.accounts.find_one({"_id": world["u1"]})
    assert (a["visit_points"], a["visit_pts"], a["scans"]) == (35, 35, 2) and sorted(a["redemption_credits"]) == ["r-1", "r-2"]
    assert DB.users.find_one({"token": "tok1"})["visit_points"] == 35


def test_duplicate_rollback_attempts_do_not_reverse_a_credit_twice(world):
    _legacy(world)
    user = DB.accounts.find_one({"_id": world["u1"]})
    U._credit_redemption(user, "tok1", "r-1", 10)
    U._credit_redemption(user, "tok1", "r-2", 25)
    rec = DB.redemptions.insert_one({"_id": ObjectId(), "user_id": str(world["u1"]), "store_id": "x", "points": 10, "created_at": datetime.utcnow()}).inserted_id
    DB.redemptions.update_one({"_id": rec}, {"$set": {"points": 10}})
    # r-1 plays the failed redemption: its id is the redemption's id
    DB.accounts.update_one({"_id": world["u1"]}, {"$set": {"redemption_credits": [str(rec), "r-2"]}})
    DB.users.update_one({"token": "tok1"}, {"$set": {"redemption_credits": [str(rec), "r-2"]}})
    for _ in range(3):
        U._undo_redemption(user, "tok1", rec, 10)
    a = DB.accounts.find_one({"_id": world["u1"]})
    # 35 credited, ONE reversal of 10 -> 25; r-2's marker and credit untouched
    assert (a["visit_points"], a["visit_pts"], a["scans"]) == (25, 25, 1) and a["redemption_credits"] == ["r-2"]
    assert DB.users.find_one({"token": "tok1"})["visit_points"] == 25
    assert DB.redemptions.count_documents({"_id": rec}) == 0
