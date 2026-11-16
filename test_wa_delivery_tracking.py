"""Phase 2F: delivery tracking (Meta status callbacks -> campaign recipients) and campaign report numbers.
Independent of the worker: recipients are written directly. Run:  python -m pytest test_wa_delivery_tracking.py -q -W ignore"""
import json
import logging
import threading
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.wa_campaigns as RC
import routers.webhook as WH
import services.wa_campaign_manager as M
import services.wa_delivery as D
import test_wa_campaign_lifecycle as L
from test_wa_campaign_lifecycle import H, P1, P2, _env, admin, meta, sender, start  # noqa: F401
from test_wa_campaign_worker import LockedDb

DB = L._DB
CID = "c0ffee000000000000000001"
MID = "wamid.OUT.1"
TS = 1762843200


def rec(phone=P1, state="sent", mid=MID, rank=2, **extra):
    d = {"campaign_id": CID, "snapshot_id": "s1", "phone": phone, "state": state, "attempts": 1, "created_at": datetime.utcnow(), **extra}
    if mid:
        d["message_id"] = mid
    if rank is not None:
        d["delivery_rank"] = rank
    DB[M.RECIPIENTS].insert_one(d)
    return d


def ev(status, ts=TS, mid=MID, phone=P1, **kw):
    return D.handle_status(msg_id=mid, recipient=phone, status=status, timestamp=str(ts), raw={"id": mid, "status": status, **kw})


def get(mid=MID):
    return DB[M.RECIPIENTS].find_one({"message_id": mid})


# ═════════ mapping and forward-only transitions ═════════

def test_status_events_map_to_the_recipient_by_message_id(admin, _env):
    rec()
    rec(P2, mid="wamid.OUT.2")
    assert ev("delivered") == "applied"
    assert get()["state"] == "delivered" and get()["delivery_rank"] == 3
    assert get("wamid.OUT.2")["state"] == "sent"                       # a different message is untouched
    assert ev("read", TS + 5) == "applied" and get()["state"] == "read" and get()["delivery_rank"] == 4


def test_api_acceptance_is_not_delivery(admin, _env):
    rec()
    s = D.campaign_stats(CID)
    assert s["sent"] == 1 and s["delivered"] == 0 and s["read"] == 0 and s["accepted"] == 1


def test_duplicate_events_change_nothing(admin, _env):
    rec()
    assert ev("delivered") == "applied"
    before = get()
    assert ev("delivered") == "stale" and ev("delivered") == "stale" and ev("sent") == "stale"
    assert get() == before


@pytest.mark.parametrize("order", [("sent", "delivered", "read"), ("read", "delivered", "sent"), ("delivered", "sent", "read"),
                                    ("read", "sent", "delivered"), ("delivered", "read", "delivered", "sent", "read")])
def test_out_of_order_events_never_move_status_backwards(admin, _env, order):
    rec()
    for i, st in enumerate(order):
        ev(st, TS + i)                                                    # timestamps follow arrival order here
    final = get()
    assert final["state"] == "read" or (final["state"] == "delivered" and "read" not in order)
    assert final["delivery_rank"] == max({"sent": 2, "delivered": 3, "read": 4}[o] for o in order)


def test_older_event_cannot_overwrite_a_newer_status(admin, _env):
    rec()
    ev("read", TS + 100)
    assert ev("delivered", TS + 1) == "stale" and get()["state"] == "read"


def test_failed_status_is_recorded_with_only_the_error_code(admin, _env):
    rec()
    r = ev("failed", errors=[{"code": 131026, "title": "Message undeliverable", "message": "to 919876543210", "error_data": {"details": "x"}}])
    assert r == "applied"
    d = get()
    assert d["state"] == "failed" and d["error_code"] == 131026 and d["error"] == "delivery_failed"
    blob = json.dumps(d, default=str)
    assert "Message undeliverable" not in blob and "to 9198765" not in blob and "details" not in blob       # error text is never stored


def test_failed_after_delivered_is_ignored_but_a_later_delivered_supersedes_an_earlier_failure(admin, _env):
    rec()
    ev("delivered", TS + 10)
    assert ev("failed", TS + 11) == "stale" and get()["state"] == "delivered"
    rec(P2, mid="wamid.OUT.2")
    ev("failed", TS + 5, mid="wamid.OUT.2", phone=P2)
    assert get("wamid.OUT.2")["state"] == "failed"
    assert ev("delivered", TS + 1, mid="wamid.OUT.2", phone=P2) == "stale" and get("wamid.OUT.2")["state"] == "failed"      # older
    assert ev("delivered", TS + 9, mid="wamid.OUT.2", phone=P2) == "applied" and get("wamid.OUT.2")["state"] == "delivered"
    assert "error_code" not in get("wamid.OUT.2")


def test_unknown_message_ids_are_ignored_safely(admin, _env, caplog):
    rec()
    with caplog.at_level(logging.DEBUG):
        assert ev("delivered", mid="wamid.NOT.OURS", phone="919111111111") == "ignored_unknown"
    assert get()["state"] == "sent"
    assert "919111111111" not in caplog.text and "wamid.NOT.OURS" not in caplog.text


@pytest.mark.parametrize("bad", [dict(msg_id=""), dict(msg_id=None), dict(msg_id=123), dict(status="deleted"), dict(status=""),
                                 dict(status=None), dict(timestamp="not-a-number"), dict(timestamp=None), dict(raw=None), dict(raw="x")])
def test_malformed_events_never_raise_or_corrupt(admin, _env, bad):
    rec()
    args = dict(msg_id=MID, recipient=P1, status="delivered", timestamp=str(TS), raw={})
    args.update(bad)
    D.handle_status(**args)
    assert get()["state"] in ("sent", "delivered")


def test_status_arriving_before_the_worker_saved_the_id_asks_for_a_redelivery(admin, _env):
    rec(state="sending", mid=None, rank=None, send_started=True)
    with pytest.raises(D.DeliveryPending):
        ev("delivered", mid="wamid.NOT.SAVED.YET")
    rec(P2, state="queued", mid=None, rank=None)                         # a merely queued recipient does not hold events back
    assert ev("delivered", mid="wamid.X", phone=P2) == "ignored_unknown"


def test_uncertain_and_cancelled_recipients_are_not_touched_by_status_events(admin, _env):
    rec(state="uncertain", mid="wamid.U", rank=None)
    assert ev("delivered", mid="wamid.U") == "stale" and get("wamid.U")["state"] == "uncertain"


def test_concurrent_duplicate_events_apply_once(admin, _env):
    _env["mp"].setattr(M, "db", LockedDb(DB))
    rec()
    out, gate = [], threading.Barrier(10)

    def go():
        gate.wait()
        out.append(ev("delivered"))
    ts = [threading.Thread(target=go) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out.count("applied") == 1 and out.count("stale") == 9 and get()["state"] == "delivered"


# ═════════ through the Phase 1 webhook handler ═════════

def _status_value(mid, status, ts=TS, phone=P1):
    return {"statuses": [{"id": mid, "recipient_id": phone, "status": status, "timestamp": str(ts)}]}


def test_handler_is_registered_once_in_the_phase1_webhook(admin, _env):
    n = len(WH._STATUS_HANDLERS)
    D.register()
    D.register()
    assert WH._STATUS_HANDLERS.count(D.handle_status) == 1 and len(WH._STATUS_HANDLERS) in (n, n + 1)
    WH._STATUS_HANDLERS.remove(D.handle_status)


def test_phase1_webhook_dedupes_and_applies_events_end_to_end(admin, _env):
    rec()
    D.register()
    try:
        assert WH._handle_statuses(_status_value(MID, "delivered")) is False
        assert WH._handle_statuses(_status_value(MID, "delivered")) is False        # Phase 1 duplicate claim: handler not even called
        assert get()["state"] == "delivered"
        assert WH._handle_statuses(_status_value(MID, "read", TS + 9)) is False and get()["state"] == "read"
        assert WH._handle_statuses(_status_value(MID, "sent", TS - 9)) is False and get()["state"] == "read"
    finally:
        WH._STATUS_HANDLERS.remove(D.handle_status)


def test_pending_mapping_makes_the_webhook_request_a_retry_and_later_succeeds(admin, _env):
    rec(state="sending", mid=None, rank=None, send_started=True)
    D.register()
    try:
        assert WH._handle_statuses(_status_value("wamid.LATE", "delivered")) is True      # -> HTTP 500 -> Meta redelivers
        DB[M.RECIPIENTS].update_one({"phone": P1}, {"$set": {"state": "sent", "message_id": "wamid.LATE", "delivery_rank": 2}})
        assert WH._handle_statuses(_status_value("wamid.LATE", "delivered")) is False
        assert get("wamid.LATE")["state"] == "delivered"
    finally:
        WH._STATUS_HANDLERS.remove(D.handle_status)


def test_other_handlers_keep_working_when_ours_is_registered(admin, _env):
    seen = []
    other = lambda **kw: seen.append(kw["status"])   # noqa: E731
    WH._STATUS_HANDLERS.append(other)
    D.register()
    try:
        rec()
        WH._handle_statuses(_status_value(MID, "delivered"))
        assert seen == ["delivered"] and get()["state"] == "delivered"
    finally:
        WH._STATUS_HANDLERS.remove(other)
        WH._STATUS_HANDLERS.remove(D.handle_status)


# ═════════ campaign statistics ═════════

def test_stats_buckets_sum_to_total_and_match_recipient_records(admin, _env):
    plan = {"snapshot": 1, "queued": 3, "sending": 2, "sent": 4, "delivered": 5, "read": 6, "failed": 2, "cancelled": 3, "uncertain": 2}
    n = 0
    for state, k in plan.items():
        for _ in range(k):
            n += 1
            rec("9190%07d" % n, state=state, mid="wamid.S%d" % n if state in ("sent", "delivered", "read", "failed") else None, rank=None,
                **({"cancel_reason": "opted_out"} if state == "cancelled" else {}))
    s = D.campaign_stats(CID)
    assert s["total"] == sum(plan.values()) == DB[M.RECIPIENTS].count_documents({"campaign_id": CID})
    for state, k in plan.items():
        assert s[state] == k, state
    assert s["consistent"] is True and s["other"] == 0 and s["accepted"] == 4 + 5 + 6
    assert s["cancelled_by_reason"] == {"opted_out": 3}
    assert sum(s[k] for k in D.REPORT_STATES) + s["snapshot"] == s["total"]


def test_stats_follow_status_events_and_never_double_count(admin, _env):
    for i in range(3):
        rec("9190%07d" % i, mid="wamid.S%d" % i)
    ev("delivered", mid="wamid.S0")
    ev("read", mid="wamid.S1")
    ev("failed", mid="wamid.S2")
    s = D.campaign_stats(CID)
    assert (s["sent"], s["delivered"], s["read"], s["failed"], s["total"]) == (0, 1, 1, 1, 3) and s["consistent"]
    ev("delivered", mid="wamid.S1")
    ev("read", mid="wamid.S0", ts=TS + 5)
    s = D.campaign_stats(CID)
    assert (s["delivered"], s["read"], s["total"]) == (0, 2, 3)


def test_unexpected_state_values_are_visible_not_hidden(admin, _env):
    rec(state="mystery", mid=None, rank=None)
    s = D.campaign_stats(CID)
    assert s["other"] == 1 and s["total"] == 1 and s["consistent"] is True


def test_stats_for_unknown_campaign_are_all_zero(admin, _env):
    s = D.campaign_stats("ffffffffffffffffffffffff")
    assert s["total"] == 0 and all(s[k] == 0 for k in D.REPORT_STATES)


def test_stats_and_run_endpoints(admin, _env):
    app = FastAPI()
    app.include_router(RC.router, prefix="/admin")
    client = TestClient(app)
    L.sync_and_enable()
    c = L.mk()
    cid = c["id"]
    for i, st in enumerate(("sent", "delivered", "read", "failed", "uncertain")):
        DB[M.RECIPIENTS].insert_one({"campaign_id": cid, "snapshot_id": "s", "phone": "9190%07d" % i, "state": st, "created_at": datetime.utcnow()})
    base = "/admin/whatsapp-campaigns/campaigns/%s" % cid
    s = client.get(base + "/stats", headers=H("adm")).json()
    assert s["total"] == 5 and s["sent"] == s["delivered"] == s["read"] == s["failed"] == s["uncertain"] == 1
    r = client.get(base + "/run", headers=H("adm")).json()
    assert r["status"] == "draft" and r["stats"]["total"] == 5 and r["mode"] == "test"
    assert client.get(base + "/stats").status_code in (401, 403)
    assert "919" not in json.dumps(s) and "919" not in json.dumps(r)
