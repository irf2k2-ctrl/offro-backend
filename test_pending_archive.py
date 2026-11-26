"""Archived Pending Transactions: archive job, retention purge, manual delete, permissions + audit, and that the existing Payments list,
filters, exports data and totals are untouched.  Run: python -m pytest test_pending_archive.py -q -W ignore"""
import sys
from datetime import datetime, timedelta

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import server
from services import pending_archive as PA

fake = sys.modules["database"].db
c = TestClient(server.app)
NOW = datetime(2026, 11, 26, 12, 0, 0)
OLD = NOW - timedelta(days=120)
YOUNG = NOW - timedelta(days=10)
EDGE_OLD = NOW - timedelta(days=90, seconds=1)
EDGE_YOUNG = NOW - timedelta(days=89, hours=23)
COLS = ("subscriptions", "subscriptions_archive", "invoices", "accounts", "stores", "dashboard_users", "dashboard_roles", "security_audit",
        "banner_orders", "product_orders")


def H(tok):
    return {"Authorization": "Bearer " + tok}


def user(tok, role="Super Admin", perms=None, country=None):
    rid = fake.dashboard_roles.insert_one({"role_name": role, "permissions": perms if perms is not None else {}}).inserted_id
    d = {"token": tok, "status": "active", "full_name": tok, "mobile": "9" + str(abs(hash(tok)))[:9], "role_id": rid, "assigned_cities": ["*"]}
    if country:
        d["country"] = country
    fake.dashboard_users.insert_one(d)
    return H(tok)


@pytest.fixture(autouse=True)
def world(monkeypatch):
    for n in COLS:
        fake[n].delete_many({})
    for k in ("PENDING_ARCHIVE_AFTER_DAYS", "PENDING_ARCHIVE_RETENTION_DAYS", "PENDING_ARCHIVE_INTERVAL_HOURS", "PENDING_ARCHIVE_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    yield


def sub(**kw):
    d = {"merchant_id": str(ObjectId()), "store_id": str(ObjectId()), "plan": "monthly", "total": 100, "gst": 18, "status": "pending",
         "pay_mode": "manual", "created_at": OLD}
    d.update(kw)
    return fake.subscriptions.insert_one(d).inserted_id


def in_active(i): return fake.subscriptions.count_documents({"_id": i}) == 1
def in_archive(i): return fake.subscriptions_archive.count_documents({"_id": i}) == 1


# ── 1-5: what gets archived ───────────────────────────────────────────────────────────────────────────────────────────
def test_old_pending_without_razorpay_ids_is_archived_with_every_field_kept():
    i = sub(discount_code="X1", note="keep me", razorpay_order_id=None, razorpay_payment_id="")
    before = fake.subscriptions.find_one({"_id": i})
    r = PA.run_cleanup(NOW)
    assert r["archived"] == 1 and not in_active(i) and in_archive(i)
    a = fake.subscriptions_archive.find_one({"_id": i})
    assert a.pop("archived_at") == NOW
    assert a == before and a["status"] == "pending"                 # status and every original field unchanged


def test_pending_with_razorpay_order_id_stays_active():
    i = sub(razorpay_order_id="order_abc")
    assert PA.run_cleanup(NOW)["archived"] == 0 and in_active(i) and not in_archive(i)


def test_pending_with_razorpay_payment_id_stays_active():
    i = sub(razorpay_payment_id="pay_abc")
    j = sub(razorpay_order_id="order_x", razorpay_payment_id="pay_x")
    PA.run_cleanup(NOW)
    assert in_active(i) and in_active(j) and fake.subscriptions_archive.count_documents({}) == 0


def test_young_pending_stays_active_and_the_90_day_boundary():
    y, e1, e2 = sub(created_at=YOUNG), sub(created_at=EDGE_YOUNG), sub(created_at=EDGE_OLD)
    PA.run_cleanup(NOW)
    assert in_active(y) and in_active(e1) and in_archive(e2)


@pytest.mark.parametrize("status", ["paid", "active", "failed", "cancelled", "canceled", "created", "expired", "refunded"])
def test_other_statuses_are_never_touched(status):
    i = sub(status=status)
    before = fake.subscriptions.find_one({"_id": i})
    PA.run_cleanup(NOW)
    assert fake.subscriptions.find_one({"_id": i}) == before and not in_archive(i)


def test_paid_at_set_is_never_archived_and_unreadable_age_is_never_archived():
    a = sub(paid_at=OLD)
    b = sub(created_at=None)
    d = sub(created_at="garbage")
    PA.run_cleanup(NOW)
    assert in_active(a) and in_active(b) and in_active(d)


def test_age_uses_updated_at_when_present_else_created_at_and_accepts_iso_strings():
    reused = sub(created_at=OLD, updated_at=YOUNG)                      # a reused order touched recently stays active
    iso = sub(created_at=OLD.isoformat())
    PA.run_cleanup(NOW)
    assert in_active(reused) and in_archive(iso)


def test_other_collections_are_never_touched():
    fake.invoices.insert_one({"invoice_no": "LS-1", "total": 5})
    fake.banner_orders.insert_one({"status": "pending", "created_at": OLD.isoformat()})
    fake.product_orders.insert_one({"status": "pending", "created_at": OLD})
    sub()
    PA.run_cleanup(NOW)
    assert fake.invoices.count_documents({}) == 1 and fake.banner_orders.count_documents({}) == 1 and fake.product_orders.count_documents({}) == 1
    assert fake.banner_orders.find_one()["status"] == "pending" and fake.product_orders.find_one()["status"] == "pending"


def test_run_is_repeatable_and_does_not_reset_archived_at():
    i = sub()
    PA.run_cleanup(NOW)
    PA.run_cleanup(NOW + timedelta(days=5))
    PA.run_cleanup(NOW + timedelta(days=6))
    assert fake.subscriptions_archive.count_documents({}) == 1 and fake.subscriptions_archive.find_one({"_id": i})["archived_at"] == NOW


def test_crash_between_copy_and_delete_is_finished_by_the_next_run():
    i = sub()
    doc = fake.subscriptions.find_one({"_id": i})
    fake.subscriptions_archive.insert_one({**doc, "archived_at": NOW - timedelta(days=1)})        # copied, original not yet removed
    PA.run_cleanup(NOW)
    assert not in_active(i) and fake.subscriptions_archive.count_documents({}) == 1
    assert fake.subscriptions_archive.find_one({"_id": i})["archived_at"] == NOW - timedelta(days=1)


def test_payment_completed_during_the_run_is_left_alone(monkeypatch):
    i = sub()
    real = PA.db[PA.ARCHIVE].update_one
    def paid_meanwhile(*a, **k):
        fake.subscriptions.update_one({"_id": i}, {"$set": {"status": "paid", "razorpay_payment_id": "pay_late"}})
        return real(*a, **k)
    class Wrap:                                   # intercept only the archive-copy write
        def __init__(s, col): s.col = col
        def __getattr__(s, n): return paid_meanwhile if n == "update_one" else getattr(s.col, n)
    orig = PA.db.__class__.__getitem__
    monkeypatch.setattr(PA.db.__class__, "__getitem__", lambda self, name: Wrap(orig(self, name)) if name == PA.ARCHIVE else orig(self, name))
    r = PA.archive_old_pending(NOW)
    assert r["archived"] == 0 and r["skipped_changed"] == 1
    assert in_active(i) and fake.subscriptions.find_one({"_id": i})["status"] == "paid" and not in_archive(i)


# ── configuration ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_periods_are_configurable_and_bad_values_fall_back(monkeypatch):
    assert PA.archive_after_days() == 90 and PA.retention_days() == 90 and PA.interval_hours() == 24 and PA.enabled()
    monkeypatch.setenv("PENDING_ARCHIVE_AFTER_DAYS", "30"); monkeypatch.setenv("PENDING_ARCHIVE_RETENTION_DAYS", "7")
    assert PA.archive_after_days() == 30 and PA.retention_days() == 7
    for bad in ("0", "-5", "abc", ""):
        monkeypatch.setenv("PENDING_ARCHIVE_AFTER_DAYS", bad)
        assert PA.archive_after_days() == 90                          # 0 can never archive everything
    monkeypatch.setenv("PENDING_ARCHIVE_ENABLED", "0")
    assert not PA.enabled() and PA.start_if_enabled() is False


def test_configured_archive_age_is_used(monkeypatch):
    monkeypatch.setenv("PENDING_ARCHIVE_AFTER_DAYS", "5")
    i = sub(created_at=NOW - timedelta(days=6)); j = sub(created_at=NOW - timedelta(days=4))
    PA.run_cleanup(NOW)
    assert in_archive(i) and in_active(j)


# ── 6-7: retention in the archive ─────────────────────────────────────────────────────────────────────────────────────
def test_archived_records_are_kept_90_days_then_deleted(monkeypatch):
    i = sub()
    PA.run_cleanup(NOW)
    assert PA.purge_expired_archive(NOW + timedelta(days=89)) == 0 and in_archive(i)
    assert PA.purge_expired_archive(NOW + timedelta(days=90)) == 1 and not in_archive(i) and not in_active(i)
    assert PA.purge_expired_archive(NOW + timedelta(days=200)) == 0                       # repeatable


def test_purge_uses_the_configured_retention_and_touches_only_the_archive(monkeypatch):
    monkeypatch.setenv("PENDING_ARCHIVE_RETENTION_DAYS", "10")
    old_arch = fake.subscriptions_archive.insert_one({"status": "pending", "archived_at": NOW - timedelta(days=11)}).inserted_id
    new_arch = fake.subscriptions_archive.insert_one({"status": "pending", "archived_at": NOW - timedelta(days=9)}).inserted_id
    active = sub(created_at=YOUNG); paid = sub(status="paid", created_at=OLD)
    r = PA.run_cleanup(NOW)
    assert r["purged"] == 1 and not in_archive(old_arch) and in_archive(new_arch) and in_active(active) and in_active(paid)


# ── the active Payments list / reports are unchanged ─────────────────────────────────────────────────────────────────
def test_active_payments_list_loses_only_the_archived_rows_and_totals_follow():
    adm = user("sa")
    keep = sub(created_at=YOUNG, total=50); razor = sub(razorpay_order_id="order_1", total=70); paid = sub(status="paid", total=200)
    gone = sub(total=999)
    before = c.get("/admin/subscriptions", headers=adm).json()
    assert len(before) == 4
    PA.run_cleanup(NOW)
    after = c.get("/admin/subscriptions", headers=adm).json()
    assert sorted(r["total"] for r in after) == [50, 70, 200] and sorted(r["total"] for r in before) == [50, 70, 200, 999]
    assert c.get("/admin/subscriptions?country=IN", headers=adm).status_code == 200
    assert c.get("/admin/payments/country-report", headers=adm).status_code == 200
    assert c.get("/admin/merchant-transactions", headers=adm).status_code == 200


# ── API: list ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def seed_archive():
    m1 = fake.accounts.insert_one({"name": "Ravi", "phone": "+919812345678", "phone_country": "IN", "roles": ["merchant"]}).inserted_id
    m2 = fake.accounts.insert_one({"name": "Sara", "phone": "+966501234567", "phone_country": "SA", "roles": ["merchant"]}).inserted_id
    st = fake.stores.insert_one({"store_name": "Ravi Cafe"}).inserted_id
    a = sub(merchant_id=str(m1), store_id=str(st), created_at=datetime(2026, 1, 10, 6, 0), total=10)
    b = sub(merchant_id=str(m2), created_at=datetime(2026, 2, 10, 6, 0), total=20, country="SA", currency_code="SAR")
    d = sub(merchant_id=str(m1), created_at=datetime(2026, 3, 10, 6, 0), total=30)
    PA.run_cleanup(NOW)
    assert fake.subscriptions_archive.count_documents({}) == 3
    return a, b, d


def test_list_shape_count_search_date_and_country_filters():
    adm = user("sa")
    a, b, d = seed_archive()
    j = c.get("/admin/payments/archive", headers=adm).json()
    assert j["total"] == 3 and j["archived_total"] == 3 and j["retention"] == {"archive_after_days": 90, "retention_days": 90}
    r = {x["id"]: x for x in j["items"]}
    assert r[str(a)]["merchant_name"] == "Ravi" and r[str(a)]["store_name"] == "Ravi Cafe" and r[str(a)]["status"] == "pending"
    assert r[str(b)]["country"] == "SA" and r[str(b)]["currency_code"] == "SAR" and r[str(a)]["delete_on"]
    names = lambda **q: sorted(x["merchant_name"] for x in c.get("/admin/payments/archive", params=q, headers=adm).json()["items"])
    assert names(search="sara") == ["Sara"] and names(search="cafe") == ["Ravi"] and names(search="zzz") == []
    assert names(country="SA") == ["Sara"] and names(country="IN") == ["Ravi", "Ravi"]
    assert names(date_from="2026-02-01", date_to="2026-02-28") == ["Sara"]
    assert names(date_from="2026-03-01") == ["Ravi"] and names(date_to="2026-01-31") == ["Ravi"]
    cc = c.get("/admin/payments/archive", params={"country": "SA"}, headers=adm).json()
    assert cc["total"] == 1 and cc["archived_total"] == 3                      # the count shows both
    assert c.get("/admin/payments/archive", params={"country": "ZZ"}, headers=adm).status_code in (400, 422)
    assert c.get("/admin/payments/archive", params={"date_from": "x"}, headers=adm).status_code == 422
    assert c.get("/admin/payments/archive", params={"date_from": "2026-05-01", "date_to": "2026-01-01"}, headers=adm).status_code == 422


def test_list_paging():
    adm = user("sa")
    for _ in range(7):
        sub()
    PA.run_cleanup(NOW)
    j = c.get("/admin/payments/archive", params={"page_size": 3, "page": 3}, headers=adm).json()
    assert j["pages"] == 3 and len(j["items"]) == 1 and j["total"] == 7


def test_list_needs_payments_view_and_login():
    seed_archive()
    assert c.get("/admin/payments/archive").status_code in (401, 403)
    assert c.get("/admin/payments/archive", headers=user("none", "Nobody", {})).status_code == 403
    assert c.get("/admin/payments/archive", headers=user("acc", "Accounts", {"Accounts": {"view": True}})).status_code == 403
    assert c.get("/admin/payments/archive", headers=user("fin", "Finance", {"Payments": {"view": True}})).status_code == 200


def test_country_restricted_admin_sees_only_own_country():
    seed_archive()
    j = c.get("/admin/payments/archive", headers=user("sa_adm", "Finance", {"Payments": {"view": True}}, country="SA")).json()
    assert [x["merchant_name"] for x in j["items"]] == ["Sara"] and j["archived_total"] == 1


# ── 8-9, 12-13: manual delete ─────────────────────────────────────────────────────────────────────────────────────────
def audit(outcome=None):
    q = {"action": "payments.archive.delete"}
    if outcome:
        q["outcome"] = outcome
    return list(fake.security_audit.find(q))


def test_delete_one_selected_archived_record():
    adm = user("sa")
    a, b, d = seed_archive()
    r = c.post("/admin/payments/archive/delete", json={"ids": [str(a)]}, headers=adm)
    assert r.status_code == 200 and r.json() == {"deleted": 1, "requested": 1, "not_deleted": 0}
    assert not in_archive(a) and in_archive(b) and in_archive(d)
    assert c.get("/admin/payments/archive", headers=adm).json()["archived_total"] == 2


def test_bulk_delete_several_selected_and_select_all():
    adm = user("sa")
    a, b, d = seed_archive()
    r = c.post("/admin/payments/archive/delete", json={"ids": [str(a), str(b), str(a), "not-an-id"]}, headers=adm)
    assert r.json()["deleted"] == 2 and in_archive(d) and not in_archive(a) and not in_archive(b)
    every = [x["id"] for x in c.get("/admin/payments/archive", headers=adm).json()["items"]]          # "select all"
    assert c.post("/admin/payments/archive/delete", json={"ids": every}, headers=adm).json()["deleted"] == 1
    assert fake.subscriptions_archive.count_documents({}) == 0


def test_manual_delete_can_never_reach_active_payments():
    adm = user("sa")
    live_pending, live_paid, live_razor = sub(created_at=YOUNG), sub(status="paid"), sub(razorpay_order_id="o1")
    a, b, d = seed_archive()
    ids = [str(live_pending), str(live_paid), str(live_razor), str(ObjectId())]
    r = c.post("/admin/payments/archive/delete", json={"ids": ids}, headers=adm)
    assert r.status_code == 200 and r.json()["deleted"] == 0 and r.json()["not_deleted"] == 4
    assert in_active(live_pending) and in_active(live_paid) and in_active(live_razor) and fake.subscriptions_archive.count_documents({}) == 3
    mixed = c.post("/admin/payments/archive/delete", json={"ids": [str(live_paid), str(a)]}, headers=adm).json()
    assert mixed["deleted"] == 1 and in_active(live_paid) and not in_archive(a)


def test_empty_and_oversized_selection_rejected():
    adm = user("sa")
    assert c.post("/admin/payments/archive/delete", json={"ids": []}, headers=adm).status_code == 422
    assert c.post("/admin/payments/archive/delete", json={"ids": ["bad"]}, headers=adm).status_code == 422
    assert c.post("/admin/payments/archive/delete", json={"ids": [str(ObjectId()) for _ in range(PA.MAX_MANUAL_DELETE + 1)]}, headers=adm).status_code == 422


def test_delete_needs_payments_delete_permission_and_denial_is_audited():
    a, b, d = seed_archive()
    assert c.post("/admin/payments/archive/delete", json={"ids": [str(a)]}).status_code in (401, 403)
    viewer = user("viewer", "Finance", {"Payments": {"view": True, "edit": True}})
    r = c.post("/admin/payments/archive/delete", json={"ids": [str(a)]}, headers=viewer)
    assert r.status_code == 403 and in_archive(a)
    den = audit("denied")
    assert len(den) == 1 and den[0]["admin"]["name"] == "viewer"
    ok = user("deleter", "Finance", {"Payments": {"view": True, "delete": True}})
    assert c.post("/admin/payments/archive/delete", json={"ids": [str(a)]}, headers=ok).status_code == 200 and not in_archive(a)


def test_country_restricted_admin_cannot_delete_other_countries():
    a, b, d = seed_archive()
    sa = user("sa_adm", "Finance", {"Payments": {"view": True, "delete": True}}, country="SA")
    r = c.post("/admin/payments/archive/delete", json={"ids": [str(a), str(b)]}, headers=sa)
    assert r.json()["deleted"] == 1 and in_archive(a) and not in_archive(b)


def test_delete_is_audited_started_and_ok_with_counts_and_no_payment_details():
    adm = user("sa")
    a, b, d = seed_archive()
    c.post("/admin/payments/archive/delete", json={"ids": [str(a), str(b)]}, headers=adm)
    started, ok = audit("started"), audit("ok")
    assert len(started) == 1 and len(ok) == 1
    assert started[0]["details"]["requested"] == 2 and ok[0]["details"]["deleted"] == 2 and ok[0]["module"] == "Payments"
    assert ok[0]["admin"]["name"] == "sa" and "total" not in str(ok[0]["details"])


def test_delete_is_refused_when_the_audit_entry_cannot_be_written(monkeypatch):
    adm = user("sa")
    a, b, d = seed_archive()
    from services import security_audit as SA
    class Boom:
        def insert_one(self, *_a, **_k): raise RuntimeError("down")
    monkeypatch.setattr(SA, "db", type("D", (), {"__getitem__": lambda s, n: Boom()})())
    r = c.post("/admin/payments/archive/delete", json={"ids": [str(a)]}, headers=adm)
    assert r.status_code == 503 and in_archive(a)


def test_no_route_can_delete_or_change_active_subscriptions():
    paths = {r.path for r in server.app.routes if "archive" in getattr(r, "path", "")}
    assert paths == {"/admin/payments/archive", "/admin/payments/archive/delete"}


# ══ concurrency: several workers / instances processing the same subscription ═════════════════════════════════════════════
import threading


class _Hook:
    """Runs `fn` once, right BEFORE the named method of the named collection - to interleave a second worker or a payment at an exact point."""
    def __init__(self, monkeypatch, coll, method, fn):
        self.fn, self.done = fn, False
        orig = PA.db.__class__.__getitem__
        me = self

        class Wrap:
            def __init__(s, col): s.col = col
            def __getattr__(s, n):
                real = getattr(s.col, n)
                if n != method:
                    return real
                def call(*a, **k):
                    if not me.done:
                        me.done = True
                        me.fn()
                    return real(*a, **k)
                return call
        monkeypatch.setattr(PA.db.__class__, "__getitem__", lambda self, name: Wrap(orig(self, name)) if name == coll else orig(self, name))


def snap(i):
    return fake.subscriptions.find_one({"_id": i})


def test_two_workers_loser_never_deletes_the_winners_archive_record(monkeypatch):
    """The reported sequence: A copies; B (also copied/saw it) removes the original; A's delete then finds nothing -> A must KEEP the record."""
    i = sub(note="orig")
    a_view = snap(i)
    outcome = {}
    _Hook(monkeypatch, "subscriptions", "find_one_and_delete", lambda: outcome.setdefault("B", PA.archive_one(snap(i), NOW, 90)))
    assert PA.archive_one(a_view, NOW, 90) == "skipped_concurrent"          # A lost the delete
    assert outcome["B"] == "archived"
    assert in_archive(i) and not in_active(i) and fake.subscriptions_archive.count_documents({}) == 1
    assert fake.subscriptions_archive.find_one({"_id": i})["note"] == "orig"


def test_late_worker_with_a_stale_snapshot_cannot_delete_the_record():
    i = sub()
    stale = snap(i)
    assert PA.archive_one(snap(i), NOW, 90) == "archived"                      # worker A finished the whole move
    assert PA.archive_one(stale, NOW + timedelta(hours=1), 90) == "skipped_concurrent"      # worker B, slower, stale view
    assert in_archive(i) and not in_active(i) and fake.subscriptions_archive.count_documents({}) == 1
    assert fake.subscriptions_archive.find_one({"_id": i})["archived_at"] == NOW             # first archive date kept


def test_creator_does_not_delete_when_the_other_worker_already_removed_the_original(monkeypatch):
    """A created the copy, B removed the original and finalised: A (the creator!) still must not delete, because the original is gone."""
    i = sub()
    _Hook(monkeypatch, "subscriptions", "find_one_and_delete", lambda: PA.archive_one(snap(i), NOW, 90))
    assert PA.archive_one(snap(i), NOW, 90) == "skipped_concurrent"
    assert in_archive(i) and not in_active(i)


@pytest.mark.parametrize("change", [
    {"status": "paid"}, {"status": "active"}, {"status": "failed"}, {"status": "cancelled"},
    {"razorpay_order_id": "order_late"}, {"razorpay_payment_id": "pay_late"}, {"paid_at": NOW},
    {"status": "paid", "razorpay_payment_id": "pay_late"},
])
def test_subscription_that_changes_during_processing_is_not_archived(monkeypatch, change):
    i = sub()
    _Hook(monkeypatch, "subscriptions", "find_one_and_delete", lambda: fake.subscriptions.update_one({"_id": i}, {"$set": change}))
    assert PA.archive_one(snap(i), NOW, 90) == "skipped_changed"
    live = snap(i)
    assert live is not None and not in_archive(i)
    assert all(live[k] == v for k, v in change.items())                       # the payment's new state is untouched


@pytest.mark.parametrize("change", [{"status": "paid"}, {"razorpay_order_id": "order_late"}])
def test_change_between_two_workers_nothing_is_archived_by_either(monkeypatch, change):
    i = sub()
    _Hook(monkeypatch, "subscriptions", "find_one_and_delete",
          lambda: (fake.subscriptions.update_one({"_id": i}, {"$set": change}), PA.archive_one(snap(i), NOW, 90)))      # payment lands, then B runs
    assert PA.archive_one(snap(i), NOW, 90) == "skipped_changed"
    assert in_active(i) and not in_archive(i)


def test_non_creator_does_not_withdraw_but_the_orphan_is_reconciled_next_run():
    """Worker A died after copying; the payment then completed. B (not the creator) leaves the copy; the next cleanup run removes that
    orphan because the live original proves nothing is lost."""
    i = sub()
    d = snap(i)
    fake.subscriptions_archive.insert_one({**d, "archived_at": NOW})                          # A's copy
    fake.subscriptions.update_one({"_id": i}, {"$set": {"status": "paid", "razorpay_payment_id": "pay_1"}})
    assert PA.archive_one(d, NOW, 90) == "skipped_changed" and in_archive(i)                  # B (not the creator) did not delete
    r = PA.run_cleanup(NOW)
    assert r["reconciled"] == 1 and not in_archive(i) and in_active(i) and snap(i)["status"] == "paid"


def test_reconcile_never_touches_a_record_whose_original_is_gone_or_still_pending():
    gone = sub(); mid = sub()
    PA.archive_one(snap(gone), NOW, 90)                                                        # fully archived, original gone
    fake.subscriptions_archive.insert_one({**snap(mid), "archived_at": NOW})                   # copy written, original still pending
    assert PA.reconcile_orphans() == 0 and in_archive(gone) and in_archive(mid)
    PA.run_cleanup(NOW)                                                                        # the normal pass finishes the second move
    assert in_archive(mid) and not in_active(mid) and fake.subscriptions_archive.count_documents({}) == 2


def test_reused_order_touched_during_processing_is_not_archived(monkeypatch):
    i = sub(created_at=OLD, updated_at=OLD)
    _Hook(monkeypatch, "subscriptions", "find_one_and_delete", lambda: fake.subscriptions.update_one({"_id": i}, {"$set": {"updated_at": NOW}}))
    assert PA.archive_one(snap(i), NOW, 90) == "skipped_changed"
    assert in_active(i) and not in_archive(i)


def test_archive_holds_the_final_original_fields_plus_only_archived_at(monkeypatch):
    i = sub(note="v1", discount_code="D1")
    _Hook(monkeypatch, "subscriptions", "find_one_and_delete", lambda: fake.subscriptions.update_one({"_id": i}, {"$set": {"note": "v2"}}))
    assert PA.archive_one(snap(i), NOW, 90) == "archived"
    a = fake.subscriptions_archive.find_one({"_id": i})
    assert a["note"] == "v2" and a["discount_code"] == "D1" and a["status"] == "pending"      # the edit made meanwhile is not lost
    assert set(a) - {"archived_at"} == {"_id", "merchant_id", "store_id", "plan", "total", "gst", "status", "pay_mode", "created_at", "note", "discount_code"}


def test_copy_removed_by_someone_while_we_hold_the_only_original_is_restored(monkeypatch):
    i = sub()
    _Hook(monkeypatch, "subscriptions", "find_one_and_delete", lambda: fake.subscriptions_archive.delete_one({"_id": i}))
    assert PA.archive_one(snap(i), NOW, 90) == "archived" and in_archive(i) and not in_active(i)


def test_run_cleanup_counts_and_a_second_instance_run_changes_nothing():
    for _ in range(5):
        sub()
    sub(razorpay_order_id="o")
    r1 = PA.run_cleanup(NOW)
    before = list(fake.subscriptions_archive.find())
    r2 = PA.run_cleanup(NOW + timedelta(minutes=1))
    assert r1["archived"] == 5 and r2["archived"] == 0 and r2["skipped_concurrent"] == 0 and r2["reconciled"] == 0
    assert list(fake.subscriptions_archive.find()) == before and fake.subscriptions.count_documents({}) == 1


def test_many_threads_running_the_whole_cleanup_at_once_lose_nothing():
    ids = [sub(note="n%d" % k) for k in range(40)]
    keep = [sub(razorpay_order_id="o%d" % k) for k in range(5)] + [sub(status="paid") for _ in range(5)]
    originals = {i: snap(i) for i in ids}
    errors, barrier = [], threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            PA.run_cleanup(NOW)
        except Exception as e:                                                                 # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    assert fake.subscriptions_archive.count_documents({}) == 40 and fake.subscriptions.count_documents({}) == 10
    for i in ids:
        a = fake.subscriptions_archive.find_one({"_id": i})
        assert a is not None and not in_active(i)
        assert {k: v for k, v in a.items() if k != "archived_at"} == originals[i]
    assert all(in_active(k) for k in keep)


def test_many_threads_with_a_payment_landing_midway_never_archive_the_paid_one(monkeypatch):
    ids = [sub() for _ in range(20)]
    target = ids[7]
    _Hook(monkeypatch, "subscriptions", "find_one_and_delete",
          lambda: fake.subscriptions.update_one({"_id": target}, {"$set": {"status": "paid"}}))
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        PA.run_cleanup(NOW)
    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    # whichever worker reached the hook, the payment's state wins: still active, paid, and never in the archive
    assert in_active(target) and snap(target)["status"] == "paid" and not in_archive(target)
    assert all(in_archive(i) and not in_active(i) for i in ids if i != target)


def test_cleanup_log_line_reports_every_counter(caplog):
    import logging
    sub(); sub(created_at=YOUNG)
    with caplog.at_level(logging.INFO, logger="offro.payments.archive"):
        r = PA.run_cleanup(NOW)
    line = [m for m in caplog.messages if m.startswith("pending archive run:")][-1]
    for k in ("scanned", "archived", "skipped_not_old", "skipped_changed", "skipped_concurrent", "reconciled", "errors", "purged"):
        assert "%s=%s" % (k, r[k]) in line, k
    assert "archived=1" in line and "skipped_not_old=1" in line and "archive after 90d, keep 90d" in line
