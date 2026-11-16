"""Phase 2E: campaign worker (claims, leases, recovery, gates, retries, run controls). Meta and the sender are fake - nothing
touches the network. Run:  python -m pytest test_wa_campaign_worker.py -q -W ignore"""
import json
import threading
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.wa_campaigns as RC
import services.wa_campaign_audit as AUD
import services.wa_campaign_manager as M
import services.wa_campaign_settings as ST
import services.wa_campaign_worker as W
import services.wa_consent as C
import services.wa_templates as T
import test_wa_campaign_lifecycle as L
from test_wa_campaign_lifecycle import (P1, P2, TESTNUM, TOKEN, _env, admin, audit_log, make_user, grant, H, meta, ready_campaign,  # noqa: F401
                                        sender, start, stop)
from test_wa_webhook_consent import _BrokenDb

D = L._DB
BASE = "/admin/whatsapp-campaigns/campaigns"
WORKER = "/admin/whatsapp-campaigns/worker"
NOSLEEP = lambda s: None            # noqa: E731
ADMIN = {"id": "a1", "name": "Asha", "role": "Lead"}


def on(env, *numbers, worker=True, test_numbers=True):
    """All switches on, test numbers allow-listed."""
    mp = env["mp"]
    L.sending_on(mp)
    if worker:
        mp.setenv("WA_CAMPAIGN_WORKER_ENABLED", "1")
    mp.setenv("WA_CAMPAIGN_TEST_NUMBERS", ",".join((TESTNUM,) + numbers) if test_numbers else "")


def scheduled(env, extra=0, mode=None, allow=(P1, P2)):
    """A scheduled campaign whose send_at has passed, with its snapshot (P1, P2 + `extra` more consenting numbers)."""
    cid, _ = ready_campaign()
    c = M.get(cid)
    snap = c["snapshot"]["id"]
    extras = []
    for i in range(extra):
        ph = "9190000%05d" % i
        start(ph, "wamid.X%d" % i)
        D[M.RECIPIENTS].insert_one({"campaign_id": cid, "snapshot_id": snap, "phone": ph, "source": "custom", "ref": "x%d" % i,
                                    "consent_basis": "customer_start", "values": {"body.1": "A", "body.2": "B"},
                                    "state": "snapshot", "created_at": datetime.utcnow()})
        extras.append(ph)
    c = M.schedule(M.get(cid), datetime.utcnow() - timedelta(minutes=1), ADMIN)
    if mode:
        D[M.CAMPAIGNS].update_one({"_id": c["_id"]}, {"$set": {"run_config": {"mode": mode}}})
    on(env, *allow, *extras)
    return cid, extras


def tick(wid="w1", **kw):
    return W.run_once(wid, sleeper=NOSLEEP, **kw)


def drain(wid="w1", max_ticks=30):
    for _ in range(max_ticks):
        r = tick(wid)
        if r.get("idle") and not r.get("launched"):
            if M.get(r["batch"]["campaign"])["status"] != "running" if r.get("batch") else True:
                break
    return r


def states(cid):
    return {r["phone"]: r["state"] for r in D[M.RECIPIENTS].find({"campaign_id": cid})}


def camp(cid):
    return D[M.CAMPAIGNS].find_one({"_id": M.ObjectId(cid)})


class _LockedColl:
    """mongomock does NOT make find_one_and_update / update_one atomic across threads (a real MongoDB server does: each is one
    atomic single-document operation). This shim gives every collection call that atomicity so the thread tests exercise OUR logic -
    any read-then-write split into two calls in the application would still interleave and be caught. The genuine server-side
    guarantee is verified separately on Staging MongoDB (docs/WHATSAPP_PHASE2E2F_WORKER_DELIVERY.md, procedure)."""
    def __init__(self, coll, lock):
        self._c, self._l = coll, lock

    def __getattr__(self, name):
        fn = getattr(self._c, name)
        if name in ("find_one_and_update", "update_one", "update_many", "insert_one", "insert_many", "delete_one", "delete_many",
                    "find_one", "count_documents") and callable(fn):
            def wrapped(*a, **k):
                with self._l:
                    return fn(*a, **k)
            return wrapped
        return fn


class LockedDb:
    def __init__(self, real):
        self._r, self._l = real, threading.RLock()

    def __getitem__(self, name):
        return _LockedColl(self._r[name], self._l)

    def __getattr__(self, name):
        return self[name]


@pytest.fixture
def atomic(_env):
    _env["mp"].setattr(M, "db", LockedDb(D))
    return _env


@pytest.fixture
def small_batch(monkeypatch):
    monkeypatch.setenv("WA_CAMPAIGN_RATE_PER_SECOND", "80")
    monkeypatch.setenv("WA_CAMPAIGN_BACKOFF_BASE_SECONDS", "30")


# ═════════ default-off and gates ═════════

def test_everything_is_off_by_default(admin, _env, sender):
    cid, _x = scheduled(_env)
    for k in ("WA_CAMPAIGN_WORKER_ENABLED", "WA_CAMPAIGN_SENDING_ENABLED"):
        _env["mp"].delenv(k, raising=False)
    assert W.run_once("w1")["gate"] == "worker_disabled" and sender.calls == []
    assert camp(cid)["status"] == "scheduled" and W.start_worker_if_enabled() is False


@pytest.mark.parametrize("off", ["sending_env", "switch"])
def test_worker_needs_both_phase2a_keys(admin, _env, sender, off):
    cid, _x = scheduled(_env)
    if off == "sending_env":
        _env["mp"].delenv("WA_CAMPAIGN_SENDING_ENABLED")
    else:
        ST.set_sending_enabled(False, ADMIN)
    assert tick()["gate"] in ("env_disabled", "switch_off") and sender.calls == [] and camp(cid)["status"] == "scheduled"


def test_unreadable_switch_fails_closed(admin, _env, sender):
    cid, _x = scheduled(_env)
    _env["mp"].setattr(ST, "db", _BrokenDb(D, {ST.SETTINGS: {"find_one"}}))
    assert tick()["gate"] == "settings_unavailable" and sender.calls == []


def test_production_is_blocked_unless_explicitly_allowed(admin, _env, sender):
    cid, _x = scheduled(_env)
    _env["mp"].setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    assert tick()["gate"] == "production_blocked" and sender.calls == [] and W.start_worker_if_enabled() is False
    assert camp(cid)["status"] == "scheduled"
    _env["mp"].setenv("WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION", "1")
    assert tick()["gate"] == "ok"


def test_send_time_is_respected(admin, _env, sender):
    cid, _x = scheduled(_env)
    D[M.CAMPAIGNS].update_one({"_id": M.ObjectId(cid)}, {"$set": {"schedule.send_at": datetime.utcnow() + timedelta(hours=2)}})
    r = tick()
    assert r["launched"] == [] and camp(cid)["status"] == "scheduled" and sender.calls == []


def test_a_campaign_that_missed_its_slot_by_hours_is_not_sent_late(admin, _env, sender):
    cid, _x = scheduled(_env)
    D[M.CAMPAIGNS].update_one({"_id": M.ObjectId(cid)}, {"$set": {"schedule.send_at": datetime.utcnow() - timedelta(hours=7)}})
    assert tick()["launched"] == [] and camp(cid)["status"] == "scheduled" and sender.calls == []
    _env["mp"].setenv("WA_CAMPAIGN_MAX_LATE_MINUTES", "600")
    assert tick()["launched"] == [cid]


# ═════════ happy path, modes ═════════

def test_test_mode_sends_to_allow_listed_numbers_and_completes(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    r = tick()
    assert r["launched"] == [cid] and r["batch"]["claimed"] == 2
    assert sorted(c["phone"] for c in sender.calls) == sorted([P1, P2])
    assert all(c["name"] == "offro_offer" and c["language"] == "en" for c in sender.calls)
    c = camp(cid)
    assert c["status"] == "completed" and c["run"]["mode"] == "test"
    recs = list(D[M.RECIPIENTS].find({"campaign_id": cid}))
    assert {r_["state"] for r_ in recs} == {"sent"} and all(r_["message_id"] == "wamid.TEST1" and r_["delivery_rank"] == 2 for r_ in recs)
    assert all(r_["attempts"] == 1 and r_["send_started"] is True for r_ in recs)
    assert [a["action"] for a in audit_log() if a["action"].startswith("campaign.run_")] == ["campaign.run_started", "campaign.run_completed"]


def test_test_mode_never_contacts_a_number_outside_the_allow_list(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, allow=(P1,))
    tick()
    assert [c["phone"] for c in sender.calls] == [P1]
    st = states(cid)
    assert st[P1] == "sent" and st[P2] == "cancelled"
    assert D[M.RECIPIENTS].find_one({"phone": P2})["cancel_reason"] == "not_a_test_number"


def test_empty_allow_list_means_nobody_in_test_mode(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    _env["mp"].delenv("WA_CAMPAIGN_TEST_NUMBERS")
    tick()
    assert sender.calls == [] and set(states(cid).values()) == {"cancelled"}


def test_live_mode_needs_its_own_key_and_a_confirmation(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, mode="live")
    assert tick()["launched"] == [] and camp(cid)["status"] == "scheduled" and sender.calls == []      # third key off
    _env["mp"].setenv("WA_CAMPAIGN_BULK_LIVE_ENABLED", "1")
    _env["mp"].delenv("WA_CAMPAIGN_TEST_NUMBERS")                                                         # allow-list is irrelevant in live
    tick()
    assert sorted(c["phone"] for c in sender.calls) == sorted([P1, P2]) and camp(cid)["run"]["mode"] == "live"


def test_run_config_requires_confirmation_for_live_and_only_when_scheduled(admin, _env):
    cid, _x = scheduled(_env)
    c = M.get(cid)
    for bad in (None, {}, {"acknowledge": True}, {"acknowledge": True, "mode": "live", "eligible_count": 99}):
        with pytest.raises(M.CampaignError) as ei:
            W.set_run_config(c, "live", bad, ADMIN)
        assert ei.value.code == "confirmation_required"
    with pytest.raises(M.CampaignError):
        W.set_run_config(c, "bulk", None, ADMIN)
    out = W.set_run_config(c, "live", {"acknowledge": True, "mode": "live", "eligible_count": c["snapshot"]["eligible"]}, ADMIN)
    assert out["run_config"]["mode"] == "live"
    with pytest.raises(M.CampaignError):
        W.set_run_config(D[M.CAMPAIGNS].find_one({"_id": c["_id"]}) | {"status": "draft"}, "test", None, ADMIN)


# ═════════ concurrency, leases, duplicate protection ═════════

def test_recipient_claims_are_exclusive_across_threads(admin, _env, atomic):
    cid, _x = scheduled(_env, extra=40)
    W._enqueue_snapshot(M.get(cid))
    cfg, got, gate = W.config(), [], threading.Barrier(8)

    def worker(i):
        gate.wait()
        while True:
            r = W.claim_recipient(cid, "w%d" % i, cfg, datetime.utcnow())
            if not r:
                return
            got.append((r["phone"], "w%d" % i))
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    phones = [p for p, _w in got]
    assert len(phones) == 42 and len(set(phones)) == 42                    # every recipient exactly once, none twice


def test_campaign_lease_is_exclusive_and_expires(admin, _env):
    cid, _x = scheduled(_env)
    W.launch_due()
    cfg, now = W.config(), datetime.utcnow()
    a = W._claim_campaign("w1", cfg, now)
    assert a and W._claim_campaign("w2", cfg, now) is None                  # held
    assert W._claim_campaign("w2", cfg, now + timedelta(seconds=cfg["lease_seconds"] + 1))["run"]["lease_owner"] == "w2"   # expired


def test_two_replicas_ticking_together_send_each_recipient_once(admin, _env, sender, small_batch, atomic):
    cid, _x = scheduled(_env, extra=30)
    gate = threading.Barrier(4)

    def replica(i):
        gate.wait()
        for _ in range(12):
            W.run_once("rep%d" % i, sleeper=NOSLEEP)
    ts = [threading.Thread(target=replica, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    phones = [c["phone"] for c in sender.calls]
    assert len(phones) == len(set(phones))                                  # never the same number twice
    assert set(phones) <= set(D[M.RECIPIENTS].distinct("phone"))
    assert camp(cid)["status"] == "completed" and set(states(cid).values()) == {"sent"} and len(phones) == 32


def test_only_one_replica_launches_a_due_campaign(admin, _env, atomic):
    cid, _x = scheduled(_env)
    res, gate = [], threading.Barrier(6)

    def go():
        gate.wait()
        res.extend(W.launch_due())
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert res == [cid] and len(audit_log("campaign.run_started")) == 1


def test_expired_lease_before_the_send_returns_recipient_to_the_queue(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    W.launch_due()
    W._enqueue_snapshot(M.get(cid))
    rec = W.claim_recipient(cid, "dead", W.config(), datetime.utcnow())           # a worker claims, then dies BEFORE the send
    D[M.RECIPIENTS].update_one({"_id": rec["_id"]}, {"$set": {"lease_until": datetime.utcnow() - timedelta(seconds=5)}})
    assert W.recover_abandoned() == {"requeued": 1, "uncertain": 0}
    r = D[M.RECIPIENTS].find_one({"_id": rec["_id"]})
    assert r["state"] == "queued" and r["attempts"] == 0
    tick()
    assert sorted(c["phone"] for c in sender.calls) == sorted([P1, P2])


def test_expired_lease_after_send_started_becomes_uncertain_and_is_never_resent(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    W.launch_due()
    W._enqueue_snapshot(M.get(cid))
    rec = W.claim_recipient(cid, "dead", W.config(), datetime.utcnow())
    D[M.RECIPIENTS].update_one({"_id": rec["_id"]}, {"$set": {"send_started": True, "lease_until": datetime.utcnow() - timedelta(seconds=5)}})
    assert W.recover_abandoned() == {"requeued": 0, "uncertain": 1}
    tick()
    tick()
    assert D[M.RECIPIENTS].find_one({"_id": rec["_id"]})["state"] == "uncertain"
    assert rec["phone"] not in [c["phone"] for c in sender.calls] and len(sender.calls) == 1


def test_live_lease_is_not_recovered(admin, _env):
    cid, _x = scheduled(_env)
    W.launch_due()
    W._enqueue_snapshot(M.get(cid))
    W.claim_recipient(cid, "alive", W.config(), datetime.utcnow())
    assert W.recover_abandoned() == {"requeued": 0, "uncertain": 0}


def test_recipient_recovered_by_another_replica_is_not_sent_by_the_slow_owner(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    W.launch_due()
    W._enqueue_snapshot(M.get(cid))
    cfg = W.config()
    rec = W.claim_recipient(cid, "slow", cfg, datetime.utcnow())
    D[M.RECIPIENTS].update_one({"_id": rec["_id"]}, {"$set": {"lease_until": datetime.utcnow() - timedelta(seconds=1)}})
    W.recover_abandoned()                                                       # requeued
    b = W._Batch(camp(cid), M.load_template(camp(cid)["template"]), "test", cfg, "slow")
    assert W.process_recipient(rec, b, NOSLEEP) in ("lost_lease", "released", "cancelled", "sent") and \
        D[M.RECIPIENTS].find_one({"_id": rec["_id"]}).get("lease_owner") != "slow"
    assert sender.calls == [] or all(c["phone"] != rec["phone"] for c in sender.calls)


def test_late_owner_can_upgrade_uncertain_to_sent(admin, _env, sender):
    cid, _x = scheduled(_env)
    W.launch_due()
    W._enqueue_snapshot(M.get(cid))
    cfg = W.config()
    rec = W.claim_recipient(cid, "w1", cfg, datetime.utcnow())
    D[M.RECIPIENTS].update_one({"_id": rec["_id"]}, {"$set": {"send_started": True, "lease_until": datetime.utcnow() - timedelta(seconds=1)}})
    W.recover_abandoned()
    b = W._Batch(camp(cid), None, "test", cfg, "w1")
    assert W._record_outcome(rec, b, {"ok": True, "outcome": "sent", "message_id": "wamid.LATE"}) == "sent"
    r = D[M.RECIPIENTS].find_one({"_id": rec["_id"]})
    assert r["state"] == "sent" and r["message_id"] == "wamid.LATE"


def test_rate_limiter_is_atomic_and_cluster_wide(admin, _env, atomic):
    cfg = {"rate_per_second": 3}
    now = datetime(2026, 1, 1, 12, 0, 0, 500000)
    assert [W.take_rate_token(cfg, now) for _ in range(5)] == [True, True, True, False, False]
    assert W.take_rate_token(cfg, now + timedelta(seconds=1)) is True
    wins, gate = [], threading.Barrier(16)
    later = now + timedelta(seconds=10)

    def go():
        gate.wait()
        wins.append(W.take_rate_token(cfg, later))
    ts = [threading.Thread(target=go) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 3


def test_rate_limiter_database_failure_means_no_send(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    _env["mp"].setattr(M, "db", _BrokenDb(D, {W.RATE: {"update_one"}}))
    tick()
    assert sender.calls == []


# ═════════ re-checks during processing ═════════

def test_kill_switch_off_during_processing_stops_the_run_and_pauses(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3)
    real = sender.__call__

    def flip(phone, name, language, components=None):
        out = real(phone, name, language, components)
        ST.set_sending_enabled(False, ADMIN)                       # operator hits the kill switch after the first message
        return out
    _env["mp"].setattr(L.WA, "send_whatsapp_template_checked", flip)
    tick()
    assert len(sender.calls) == 1
    c = camp(cid)
    assert c["status"] == "paused" and c["run"]["pause_reason"].startswith("kill_switch:")
    st = list(states(cid).values())
    assert st.count("sent") == 1 and st.count("queued") == len(st) - 1 and "sending" not in st
    assert all(r["attempts"] == 0 for r in D[M.RECIPIENTS].find({"state": "queued"}))        # released claims are not counted
    assert audit_log("campaign.run_auto_paused")[0]["details"]["reason"].startswith("kill_switch:")
    tick()
    assert len(sender.calls) == 1                                   # nothing more while off / paused


def test_env_key_off_during_processing_stops_the_run(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3)
    real = sender.__call__

    def flip(phone, name, language, components=None):
        out = real(phone, name, language, components)
        _env["mp"].delenv("WA_CAMPAIGN_SENDING_ENABLED")
        return out
    _env["mp"].setattr(L.WA, "send_whatsapp_template_checked", flip)
    tick()
    assert len(sender.calls) == 1 and camp(cid)["status"] == "paused"


def test_resume_is_refused_while_sending_is_off_and_continues_when_on(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=2)
    real = sender.__call__
    _env["mp"].setattr(L.WA, "send_whatsapp_template_checked",
                       lambda *a, **k: (ST.set_sending_enabled(False, ADMIN), real(*a, **k))[1])
    tick()
    assert camp(cid)["status"] == "paused"
    with pytest.raises(M.CampaignError) as ei:
        W.resume(M.get(cid), ADMIN)
    assert ei.value.code == "sending_disabled"
    _env["mp"].setattr(L.WA, "send_whatsapp_template_checked", sender)
    ST.set_sending_enabled(True, ADMIN)
    W.resume(M.get(cid), ADMIN)
    for _ in range(4):
        tick()
    assert camp(cid)["status"] == "completed" and set(states(cid).values()) == {"sent"}
    phones = [c["phone"] for c in sender.calls]
    assert len(phones) == len(set(phones)) == 4


def test_stop_received_during_processing_overrides_eligibility(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    first = {"done": False}
    real = sender.__call__

    def hook(phone, name, language, components=None):
        out = real(phone, name, language, components)
        if not first["done"]:
            first["done"] = True
            stop(P2 if phone == P1 else P1, "wamid.STOPNOW")        # the OTHER recipient replies STOP while we are mid-run
        return out
    _env["mp"].setattr(L.WA, "send_whatsapp_template_checked", hook)
    tick()
    assert len(sender.calls) == 1
    other = P2 if sender.calls[0]["phone"] == P1 else P1
    r = D[M.RECIPIENTS].find_one({"phone": other})
    assert r["state"] == "cancelled" and r["cancel_reason"] == "opted_out"
    assert camp(cid)["status"] == "completed"


def test_recipient_who_was_eligible_at_snapshot_but_is_now_unknown_is_not_sent(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    D[C.CONTACTS].delete_many({"phone": P1})
    tick()
    assert [c["phone"] for c in sender.calls] == [P2] and states(cid)[P1] == "cancelled"


def test_consent_lookup_failure_sends_nothing_and_requeues(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    _env["mp"].setattr(C, "db", _BrokenDb(D, {C.CONTACTS: {"find_one", "find"}}))
    tick()
    assert sender.calls == []
    assert set(states(cid).values()) == {"queued"} and all(r["attempts"] == 0 for r in D[M.RECIPIENTS].find({"campaign_id": cid}))
    assert camp(cid)["run"]["backoff_until"] > datetime.utcnow()           # slows down instead of spinning
    assert tick()["batch"] is None and sender.calls == []                  # not even claimed again until the backoff ends


def test_template_no_longer_ready_pauses_before_any_message(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    W.launch_due()
    _env["mp"].setattr(T, "verify_live", lambda t: ["template_paused"])
    r = tick()
    assert sender.calls == [] and camp(cid)["status"] == "paused" and r["batch"]["halt"] == "template_not_ready"


def test_meta_unreachable_for_template_check_waits_and_sends_nothing(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    W.launch_due()

    def down(t):
        raise T.TemplateSyncError("meta_unavailable", "down")
    _env["mp"].setattr(T, "verify_live", down)
    r = tick()
    assert sender.calls == [] and camp(cid)["status"] == "running" and r["batch"]["halt"] == "template_unverified"
    assert camp(cid)["run"]["backoff_until"] > datetime.utcnow()


def test_launch_is_refused_when_template_cannot_be_verified(admin, _env, sender):
    cid, _x = scheduled(_env)
    _env["mp"].setattr(T, "verify_live", lambda t: (_ for _ in ()).throw(T.TemplateSyncError("meta_unavailable", "down")))
    assert tick()["launched"] == [] and camp(cid)["status"] == "scheduled"


def test_audit_failure_at_launch_pauses_the_campaign(admin, _env, sender):
    cid, _x = scheduled(_env)
    _env["mp"].setattr(AUD, "db", _BrokenDb(D, {AUD.AUDIT: {"insert_one"}}))
    W.launch_due()
    assert camp(cid)["status"] == "paused" and camp(cid)["run"]["pause_reason"] == "audit_unavailable" and sender.calls == []


# ═════════ outcomes, retries, backoff ═════════

def test_uncertain_outcome_is_never_retried(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    sender.result = {"ok": False, "outcome": "uncertain", "error_code": None}
    tick()
    assert len(sender.calls) == 2 and set(states(cid).values()) == {"uncertain"}
    sender.result = {"ok": True, "outcome": "sent", "message_id": "wamid.NO"}
    for _ in range(3):
        tick()
    assert len(sender.calls) == 2 and camp(cid)["status"] == "completed"
    assert all(r["attempts"] == 1 and "message_id" not in r for r in D[M.RECIPIENTS].find({"campaign_id": cid}))


def test_sender_exception_is_treated_as_uncertain_not_failed_or_retried(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)

    def boom(*a, **k):
        raise RuntimeError("socket died 919876543210 " + TOKEN)
    _env["mp"].setattr(L.WA, "send_whatsapp_template_checked", boom)
    tick()
    assert set(states(cid).values()) == {"uncertain"}
    assert TOKEN not in json.dumps(list(D[M.RECIPIENTS].find({"campaign_id": cid})), default=str)


def test_permanent_rejection_fails_without_retry(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    sender.result = {"ok": False, "outcome": "rejected_permanent", "error_code": 131026}
    tick()
    assert len(sender.calls) == 2 and set(states(cid).values()) == {"failed"}
    assert {r["error_code"] for r in D[M.RECIPIENTS].find({"campaign_id": cid})} == {131026}


def test_retryable_failure_is_requeued_with_exponential_backoff_then_fails_at_the_limit(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, allow=(P1,))
    sender.result = {"ok": False, "outcome": "rejected_retryable", "error_code": 131000}
    prev = None
    for attempt in (1, 2, 3):
        tick()
        r = D[M.RECIPIENTS].find_one({"phone": P1})
        assert r["attempts"] == attempt
        if attempt < 3:
            assert r["state"] == "queued" and r["next_attempt_at"] > datetime.utcnow()
            if prev:
                assert (r["next_attempt_at"] - datetime.utcnow()).total_seconds() > (prev - datetime.utcnow()).total_seconds() - 1
            prev = r["next_attempt_at"]
            D[M.RECIPIENTS].update_one({"_id": r["_id"]}, {"$set": {"next_attempt_at": datetime.utcnow() - timedelta(seconds=1)}})
            D[M.CAMPAIGNS].update_one({"_id": M.ObjectId(cid)}, {"$set": {"run.backoff_until": W.EPOCH}})
    r = D[M.RECIPIENTS].find_one({"phone": P1})
    assert r["state"] == "failed" and r["error"] == "retries_exhausted" and r["attempts"] == 3
    tick()
    assert len([c for c in sender.calls if c["phone"] == P1]) == 3


def test_retry_not_attempted_before_backoff_elapses(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, allow=(P1,))
    sender.result = {"ok": False, "outcome": "rejected_retryable", "error_code": 131000}
    tick()
    n = len(sender.calls)
    for _ in range(3):
        tick()
    assert len(sender.calls) == n


def test_backoff_function_grows_is_capped_and_jittered():
    cfg = {"backoff_base_seconds": 30, "backoff_max_seconds": 300}
    mid = lambda: 0.5      # noqa: E731
    vals = [W.backoff_seconds(n, cfg, mid) for n in (1, 2, 3, 4, 5, 6, 7)]
    assert vals[:4] == [30, 60, 120, 240] and vals[4:] == [300, 300, 300]
    assert W.backoff_seconds(1, cfg, lambda: 0.0) == pytest.approx(24) and W.backoff_seconds(1, cfg, lambda: 1.0) == pytest.approx(36)


def test_meta_rate_limit_slows_the_whole_campaign(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3)
    sender.result = {"ok": False, "outcome": "rejected_retryable", "error_code": 130429}
    tick()
    assert len(sender.calls) == 1                                                    # stops at the first throttle, not 5 more calls
    assert camp(cid)["run"]["backoff_until"] > datetime.utcnow() + timedelta(seconds=30)
    assert tick()["batch"] is None


def test_config_is_clamped_and_bad_values_fall_back(monkeypatch):
    monkeypatch.setenv("WA_CAMPAIGN_BATCH_SIZE", "99999")
    monkeypatch.setenv("WA_CAMPAIGN_CONCURRENCY", "abc")
    monkeypatch.setenv("WA_CAMPAIGN_MAX_ATTEMPTS", "-4")
    monkeypatch.setenv("WA_CAMPAIGN_RATE_PER_SECOND", "0")
    c = W.config()
    assert (c["batch_size"], c["concurrency"], c["max_attempts"], c["rate_per_second"]) == (200, 1, 1, 1)


def test_concurrent_batch_processing_sends_each_once(admin, _env, sender, small_batch, atomic):
    _env["mp"].setenv("WA_CAMPAIGN_CONCURRENCY", "4")
    cid, _x = scheduled(_env, extra=18)
    for _ in range(3):
        tick()
    phones = [c["phone"] for c in sender.calls]
    assert len(phones) == len(set(phones)) == 20 and camp(cid)["status"] == "completed"


# ═════════ run controls (API) ═════════

app = FastAPI()
app.include_router(RC.router, prefix="/admin")
client = TestClient(app)


def post(cid, action, token="adm", **body):
    return client.post("%s/%s/%s" % (BASE, cid, action), headers=H(token), json=body)


def test_pause_stops_processing_and_resume_continues(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3)
    W.launch_due()
    r = post(cid, "pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    for _ in range(3):
        tick()
    assert sender.calls == []
    assert post(cid, "pause").status_code == 409                       # already paused
    assert post(cid, "resume").status_code == 200
    for _ in range(3):
        tick()
    assert len(sender.calls) == 5 and camp(cid)["status"] == "completed"


def test_cancel_run_stops_sending_cancels_queued_and_keeps_the_records(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3)
    real = sender.__call__

    def cancel_after_first(phone, name, language, components=None):
        out = real(phone, name, language, components)
        if len(sender.calls) == 1:
            assert post(cid, "cancel-run").status_code == 200       # an admin cancels while the batch is in flight
        return out
    _env["mp"].setattr(L.WA, "send_whatsapp_template_checked", cancel_after_first)
    tick()
    assert len(sender.calls) == 1 and camp(cid)["status"] == "cancelled"
    st = list(states(cid).values())
    assert st.count("sent") == 1 and st.count("cancelled") == 4 and D[M.RECIPIENTS].count_documents({"campaign_id": cid}) == 5
    tick()
    tick()
    assert len(sender.calls) == 1
    s = client.get("%s/%s/stats" % (BASE, cid), headers=H("adm")).json()
    assert s["total"] == 5 and s["sent"] == 1 and s["cancelled"] == 4 and s["cancelled_by_reason"] == {"campaign_cancelled": 4}


def test_interrupted_cancel_is_completed_by_the_sweep(admin, _env, sender):
    cid, _x = scheduled(_env)
    W.launch_due()
    W._enqueue_snapshot(M.get(cid))
    c = camp(cid)
    W._run_transition(c, ("running",), "cancelled", ADMIN, {"run.swept": False})      # crashed before the recipient update
    assert set(states(cid).values()) == {"queued"}
    tick()
    assert set(states(cid).values()) == {"cancelled"} and sender.calls == []


def test_permissions_for_run_controls(admin, _env, sender):
    cid, _x = scheduled(_env)
    W.launch_due()
    make_user("viewer", grant("view"))
    make_user("editor", grant("view", "edit"))
    assert post(cid, "pause", "viewer").status_code == 403 and post(cid, "cancel-run", "viewer").status_code == 403
    assert post(cid, "resume", "editor").status_code == 403                          # starting again needs approve
    assert post(cid, "run-config", "editor", mode="test").status_code == 403
    assert post(cid, "pause", "editor").status_code == 200                           # stopping needs only edit
    assert client.get("%s/%s/stats" % (BASE, cid), headers=H("viewer")).status_code == 200
    assert client.get(WORKER + "/status", headers=H("viewer")).status_code == 200
    assert client.get(WORKER + "/status").status_code in (401, 403)


def test_stopping_is_never_blocked_by_the_audit_log(admin, _env, sender):
    cid, _x = scheduled(_env)
    W.launch_due()
    _env["mp"].setattr(AUD, "db", _BrokenDb(D, {AUD.AUDIT: {"insert_one"}}))
    assert post(cid, "pause").status_code == 200
    assert post(cid, "resume").status_code == 503                                    # starting IS blocked (audit-first)


def test_run_config_endpoint_is_audited_and_sends_nothing(admin, _env, sender):
    cid, _x = scheduled(_env)
    n = M.get(cid)["snapshot"]["eligible"]
    assert post(cid, "run-config", mode="live").status_code == 400
    r = post(cid, "run-config", mode="live", confirm={"acknowledge": True, "mode": "live", "eligible_count": n})
    assert r.status_code == 200 and r.json()["mode"] == "live" and sender.calls == []
    assert audit_log("campaign.run_config")


def test_worker_status_endpoint_has_no_secrets(admin, _env):
    on(_env)
    j = client.get(WORKER + "/status", headers=H("adm")).json()
    assert j["worker_env_enabled"] is True and j["gate"]["ok"] is True and j["live_key_on"] is False
    assert TOKEN not in json.dumps(j) and TESTNUM not in json.dumps(j)


def test_run_endpoints_never_send_anything_themselves(admin, _env, sender):
    cid, _x = scheduled(_env)
    W.launch_due()
    for a in ("pause", "resume", "pause", "cancel-run"):
        post(cid, a)
    client.get("%s/%s/run" % (BASE, cid), headers=H("adm"))
    assert sender.calls == []


def test_phone_numbers_and_tokens_never_reach_audit_or_logs(admin, _env, sender, small_batch, caplog):
    cid, _x = scheduled(_env)
    with caplog.at_level("DEBUG"):
        tick()
    blob = json.dumps(audit_log(), default=str) + caplog.text
    assert P1 not in blob and P2 not in blob and TOKEN not in blob


def test_indexes_are_created(admin, _env):
    W.ensure_indexes()
    ix = D[M.RECIPIENTS].index_information()
    assert any("message_id" in k for k in ix) and any("lease" in k for k in ix)
    assert W.ensure_indexes_safe() is True
