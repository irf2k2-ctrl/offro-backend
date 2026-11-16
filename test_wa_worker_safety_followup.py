"""Phase 2E follow-up safety corrections: per-message template check, worker key in the final gate, campaign lease ownership /
renewal, cancellation race ordering, and the staging concurrency script helpers. Fake Meta/sender only.
Run:  python -m pytest test_wa_worker_safety_followup.py -q -W ignore"""
import inspect
import json
import time
from datetime import datetime, timedelta

import pytest

import services.wa_campaign_manager as M
import services.wa_campaign_worker as W
import services.wa_templates as T
import test_wa_campaign_lifecycle as L
from test_wa_campaign_lifecycle import P1, P2, TOKEN, _env, admin, audit_log, meta, sender  # noqa: F401
from test_wa_campaign_worker import (ADMIN, D, LockedDb, NOSLEEP, atomic, camp, scheduled, small_batch, states, tick)  # noqa: F401
from test_wa_webhook_consent import _BrokenDb


def after_first_send(env, sender, action):
    """Run `action()` once, right after the first message left (i.e. while the batch is in flight)."""
    real, done = sender.__call__, {"n": 0}

    def hooked(phone, name, language, components=None):
        out = real(phone, name, language, components)
        if not done["n"]:
            done["n"] = 1
            action()
        return out
    env["mp"].setattr(L.WA, "send_whatsapp_template_checked", hooked)


def queued_clean(cid):
    return [r for r in D[M.RECIPIENTS].find({"campaign_id": cid, "state": "queued"})]


# ═════════ 1. live template check before EVERY message ═════════

def test_template_is_verified_with_meta_before_every_message(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3)
    real, n = T.verify_live, {"n": 0}
    _env["mp"].setattr(T, "verify_live", lambda t: (n.__setitem__("n", n["n"] + 1), real(t))[1])
    tick()
    assert len(sender.calls) == 5 and n["n"] >= 1 + 5          # launch + batch start + one per message (>= each send was preceded by a check)


def test_template_pulled_mid_batch_stops_before_the_next_message_and_pauses(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3)
    after_first_send(_env, sender, lambda: _env["mp"].setattr(T, "verify_live", lambda t: ["template_paused"]))
    tick()
    assert len(sender.calls) == 1
    c = camp(cid)
    assert c["status"] == "paused" and c["run"]["pause_reason"] == "template_not_ready"
    assert all(r["attempts"] == 0 and r["send_started"] is False for r in queued_clean(cid)) and len(queued_clean(cid)) == 4


@pytest.mark.parametrize("failure", ["meta_down", "unexpected_error"])
def test_meta_unavailable_at_the_per_message_check_fails_closed_and_backs_off(admin, _env, sender, small_batch, failure):
    cid, _x = scheduled(_env, extra=3)

    def broken(t):
        if failure == "meta_down":
            raise T.TemplateSyncError("meta_unavailable", "down")
        raise RuntimeError("boom " + TOKEN)

    after_first_send(_env, sender, lambda: _env["mp"].setattr(T, "verify_live", broken))
    r = tick()
    assert len(sender.calls) == 1 and r["batch"]["halt"] == "template_unverified"
    c = camp(cid)
    assert c["status"] == "running" and c["run"]["backoff_until"] > datetime.utcnow() + timedelta(seconds=30)
    assert len(queued_clean(cid)) == 4 and all(r_["attempts"] == 0 for r_ in queued_clean(cid))
    assert tick()["batch"] is None and len(sender.calls) == 1                 # backing off: not even claimed again
    assert TOKEN not in json.dumps(audit_log(), default=str)


# ═════════ 2. worker key (and the other final-gate keys) stop the very next message ═════════

def test_worker_key_switched_off_mid_batch_stops_the_next_message(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3)
    after_first_send(_env, sender, lambda: _env["mp"].delenv("WA_CAMPAIGN_WORKER_ENABLED"))
    r = tick()
    assert len(sender.calls) == 1 and r["batch"]["halt"] == "kill_switch:worker_disabled"
    assert camp(cid)["status"] == "paused" and camp(cid)["run"]["pause_reason"] == "kill_switch:worker_disabled"
    assert tick()["gate"] == "worker_disabled" and len(sender.calls) == 1


def test_production_guard_and_live_key_are_also_in_the_final_gate(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3)
    after_first_send(_env, sender, lambda: _env["mp"].setenv("RAILWAY_ENVIRONMENT_NAME", "production"))
    tick()
    assert len(sender.calls) == 1 and camp(cid)["run"]["pause_reason"] == "kill_switch:production_blocked"


def test_live_key_switched_off_mid_batch_stops_a_live_run(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3, mode="live")
    _env["mp"].setenv("WA_CAMPAIGN_BULK_LIVE_ENABLED", "1")
    after_first_send(_env, sender, lambda: _env["mp"].delenv("WA_CAMPAIGN_BULK_LIVE_ENABLED"))
    tick()
    assert len(sender.calls) == 1 and camp(cid)["run"]["pause_reason"] == "kill_switch:live_disabled"


def test_final_gate_source_lists_every_key(admin):
    src = inspect.getsource(W.process_recipient)
    gate = src[src.index("FINAL gate"):src.index("(a) record send_started")]
    for needle in ("ST.sending_allowed()", "ST.worker_enabled()", "live_allowed()", "production_blocked()"):
        assert needle in gate


# ═════════ 3. campaign lease: ownership, renewal, expiry ═════════

def test_renewal_extends_only_our_own_unexpired_lease(admin, _env):
    cid, _x = scheduled(_env)
    W.launch_due()
    cfg = W.config()
    c = W._claim_campaign("w1", cfg, datetime.utcnow())
    b = W._Batch(c, None, "test", cfg, "w1")
    before = camp(cid)["run"]["lease_until"]
    time.sleep(0.01)
    assert W._renew_campaign_lease(b) == (True, "ok") and camp(cid)["run"]["lease_until"] > before


def test_renewal_cannot_overwrite_another_workers_lease(admin, _env):
    cid, _x = scheduled(_env)
    W.launch_due()
    cfg = W.config()
    c = W._claim_campaign("w1", cfg, datetime.utcnow())
    b1 = W._Batch(c, None, "test", cfg, "w1")
    D[M.CAMPAIGNS].update_one({"_id": c["_id"]}, {"$set": {"run.lease_owner": "w2", "run.lease_until": datetime.utcnow() + timedelta(seconds=90)}})
    snap = camp(cid)["run"]
    assert W._renew_campaign_lease(b1) == (False, "lease_lost")
    assert camp(cid)["run"] == snap                                           # untouched: owner w2, same expiry


def test_an_expired_lease_is_not_resurrected_by_its_old_owner(admin, _env):
    cid, _x = scheduled(_env)
    W.launch_due()
    cfg = W.config()
    c = W._claim_campaign("w1", cfg, datetime.utcnow())
    D[M.CAMPAIGNS].update_one({"_id": c["_id"]}, {"$set": {"run.lease_until": datetime.utcnow() - timedelta(seconds=1)}})
    assert W._renew_campaign_lease(W._Batch(c, None, "test", cfg, "w1")) == (False, "lease_lost")
    assert W._claim_campaign("w2", cfg, datetime.utcnow())["run"]["lease_owner"] == "w2"      # a new worker takes it normally


@pytest.mark.parametrize("status,reason", [("paused", "paused"), ("cancelled", "cancelled")])
def test_renewal_is_refused_after_pause_or_cancel(admin, _env, status, reason):
    cid, _x = scheduled(_env)
    W.launch_due()
    cfg = W.config()
    c = W._claim_campaign("w1", cfg, datetime.utcnow())
    D[M.CAMPAIGNS].update_one({"_id": c["_id"]}, {"$set": {"status": status}})
    assert W._renew_campaign_lease(W._Batch(c, None, "test", cfg, "w1")) == (False, reason)


def test_lease_expiring_mid_batch_stops_that_worker_before_the_next_message(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3)
    after_first_send(_env, sender, lambda: D[M.CAMPAIGNS].update_one(
        {"_id": M.ObjectId(cid)}, {"$set": {"run.lease_until": datetime.utcnow() - timedelta(seconds=1)}}))
    r = tick()
    assert len(sender.calls) == 1 and r["batch"]["halt"] == "lease_lost"
    assert camp(cid)["status"] == "running"                                  # no auto-pause: another worker may simply take over
    assert len(queued_clean(cid)) == 4 and all(x["attempts"] == 0 for x in queued_clean(cid))


def test_worker_that_lost_the_lease_to_another_worker_sends_nothing_more_and_the_other_continues(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=5)
    after_first_send(_env, sender, lambda: D[M.CAMPAIGNS].update_one(
        {"_id": M.ObjectId(cid)}, {"$set": {"run.lease_owner": "w2", "run.lease_until": datetime.utcnow() + timedelta(seconds=100)}}))
    r = tick("w1")
    assert len(sender.calls) == 1 and r["batch"]["halt"] == "lease_lost"
    assert camp(cid)["run"]["lease_owner"] == "w2" and camp(cid)["run"]["lease_until"] > datetime.utcnow() + timedelta(seconds=60)   # w1 did not free it
    _env["mp"].setattr(L.WA, "send_whatsapp_template_checked", sender)
    D[M.CAMPAIGNS].update_one({"_id": M.ObjectId(cid)}, {"$set": {"run.lease_until": datetime.utcnow() - timedelta(seconds=1)}})
    for _ in range(3):
        tick("w2")
    phones = [c["phone"] for c in sender.calls]
    assert len(phones) == len(set(phones)) == 7 and camp(cid)["status"] == "completed"


def test_long_batch_keeps_its_lease_alive_by_renewal(admin, _env, sender, small_batch, monkeypatch):
    """10 messages, each 'taking' 20 s on a fake clock, lease 30 s: without renewal the lease would die after the 2nd message."""
    monkeypatch.setenv("WA_CAMPAIGN_LEASE_SECONDS", "30")
    clock = {"t": datetime.utcnow()}
    monkeypatch.setattr(W, "_now", lambda: clock["t"])
    cid, _x = scheduled(_env, extra=8)
    real = sender.__call__

    def slow(phone, name, language, components=None):
        clock["t"] += timedelta(seconds=20)
        return real(phone, name, language, components)
    monkeypatch.setattr(L.WA, "send_whatsapp_template_checked", slow)
    r = tick()
    assert len(sender.calls) == 10 and r["batch"]["halt"] == "" and camp(cid)["status"] == "completed"


def test_recipients_are_claimed_one_at_a_time_so_their_leases_are_fresh(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3)
    seen = []
    real = W.claim_recipient
    _env["mp"].setattr(W, "claim_recipient", lambda *a, **k: (seen.append(D[M.RECIPIENTS].count_documents({"state": "sending"})), real(*a, **k))[1])
    tick()
    assert seen and max(seen) == 0                       # never more than the one in hand: nothing is claimed ahead of time


def test_recovery_after_a_lost_lease_never_resends(admin, _env, sender, small_batch):
    """A recipient whose lease expired before the send is requeued and sent once by whoever is the owner; the stale worker cannot."""
    cid, _x = scheduled(_env)
    W.launch_due()
    W._enqueue_snapshot(M.get(cid))
    cfg = W.config()
    c = W._claim_campaign("w1", cfg, datetime.utcnow())
    rec = W.claim_recipient(cid, "w1", cfg, datetime.utcnow())
    D[M.RECIPIENTS].update_one({"_id": rec["_id"]}, {"$set": {"lease_until": datetime.utcnow() - timedelta(seconds=1)}})
    D[M.CAMPAIGNS].update_one({"_id": c["_id"]}, {"$set": {"run.lease_until": datetime.utcnow() - timedelta(seconds=1)}})
    b = W._Batch(camp(cid), M.load_template(camp(cid)["template"]), "test", cfg, "w1")
    assert W.process_recipient(rec, b, NOSLEEP) in ("released", "lost_lease") and sender.calls == []
    W.recover_abandoned()
    for _ in range(3):
        tick("w2")
    phones = [c_["phone"] for c_ in sender.calls]
    assert sorted(phones) == sorted([P1, P2]) and states(cid) == {P1: "sent", P2: "sent"}


# ═════════ 5. cancellation / pause race around send_started ═════════

def _hook_template_check(env, action):
    """`verify_live` per message runs AFTER the status check and BEFORE send_started: perfect place for a rival to win the race."""
    real, n = T.verify_live, {"n": 0}

    def hooked(t):
        n["n"] += 1
        if n["n"] == 3:                                   # 1 = launch, 2 = batch start, 3 = first message
            action()
        return real(t)
    env["mp"].setattr(T, "verify_live", hooked)


def test_pause_that_wins_before_send_started_prevents_the_send(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    _hook_template_check(_env, lambda: W.pause(M.get(cid), ADMIN))
    tick()
    assert sender.calls == [] and camp(cid)["status"] == "paused"
    assert set(states(cid).values()) == {"queued"} and all(r["send_started"] is False and r["attempts"] == 0 for r in D[M.RECIPIENTS].find({}))


def test_cancel_that_wins_before_send_started_prevents_the_send(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    _hook_template_check(_env, lambda: W.cancel_run(M.get(cid), ADMIN))
    tick()
    assert sender.calls == [] and camp(cid)["status"] == "cancelled" and set(states(cid).values()) == {"cancelled"}
    assert all(r["send_started"] is False for r in D[M.RECIPIENTS].find({}))


@pytest.mark.parametrize("rival", ["pause", "cancel"])
def test_pause_or_cancel_between_send_started_and_the_final_lease_check_still_prevents_the_send(admin, _env, sender, small_batch, rival):
    cid, _x = scheduled(_env)
    real = W._renew_campaign_lease
    n = {"n": 0}

    def rival_first(b):
        n["n"] += 1
        if n["n"] == 2:                                                       # 1 = heartbeat, 2 = the final check after send_started
            assert D[M.RECIPIENTS].find_one({"state": "sending"})["send_started"] is True
            (W.pause if rival == "pause" else W.cancel_run)(M.get(cid), ADMIN)
        return real(b)
    _env["mp"].setattr(W, "_renew_campaign_lease", rival_first)
    tick()
    assert sender.calls == []
    first = D[M.RECIPIENTS].find_one({"phone": {"$in": [P1, P2]}}, sort=[("_id", 1)])
    assert first["send_started"] is False and first["state"] in ("queued", "cancelled")      # flag taken back: nothing was sent


def test_if_send_started_cannot_be_taken_back_the_recipient_is_never_resent(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env)
    real = W._renew_campaign_lease
    n = {"n": 0}

    def rival(b):
        n["n"] += 1
        if n["n"] == 2:
            W.pause(M.get(cid), ADMIN)
        return real(b)
    _env["mp"].setattr(W, "_renew_campaign_lease", rival)
    _env["mp"].setattr(W, "_unstart", lambda rec, wid: False)               # the revert fails
    tick()
    assert sender.calls == []
    stuck = D[M.RECIPIENTS].find_one({"state": "sending"})
    assert stuck["send_started"] is True
    D[M.RECIPIENTS].update_one({"_id": stuck["_id"]}, {"$set": {"lease_until": datetime.utcnow() - timedelta(seconds=1)}})
    assert W.recover_abandoned()["uncertain"] == 1
    W.resume(M.get(cid), ADMIN)
    for _ in range(3):
        tick()
    assert stuck["phone"] not in [c["phone"] for c in sender.calls]          # uncertain: never resent


def test_pause_after_the_final_check_lets_only_that_one_message_through(admin, _env, sender, small_batch):
    cid, _x = scheduled(_env, extra=3)
    after_first_send(_env, sender, lambda: W.pause(M.get(cid), ADMIN))
    tick()
    assert len(sender.calls) == 1 and camp(cid)["status"] == "paused"


def test_send_started_and_lease_check_are_conditional_and_adjacent_in_the_code(admin):
    src = inspect.getsource(W.process_recipient)
    a, b, c = src.index("(a) record send_started"), src.index("_renew_campaign_lease(b)", src.index("(b)")), src.index("WA.send_whatsapp_template_checked(")
    assert a < b < c
    between = src[a:c]
    assert "find_one" not in between                                      # no separate status read between the check and the call
    for needle in ('"lease_owner": b.wid', '"send_started": False', '"lease_until": {"$gt": now}', '"state": "sending"'):
        assert needle in between
    lease_src = inspect.getsource(W._renew_campaign_lease)
    for needle in ('"status": RUNNING', '"run.lease_owner": b.wid', '"run.lease_until": {"$gt": now}'):
        assert needle in lease_src


def test_concurrent_pause_and_send_never_both_win_on_the_same_message(admin, _env, sender, small_batch, atomic):
    import threading
    cid, _x = scheduled(_env, extra=20)
    stop_flag = threading.Event()

    def pauser():
        time.sleep(0.01)
        try:
            W.pause(M.get(cid), ADMIN)
        except M.CampaignError:
            pass
        stop_flag.set()
    t = threading.Thread(target=pauser)
    t.start()
    tick()
    t.join()
    sent = {c["phone"] for c in sender.calls}
    started = {r["phone"] for r in D[M.RECIPIENTS].find({"send_started": True})}
    assert sent <= started and len(sent) == len(sender.calls)               # every message sent had send_started recorded first
    assert all(r["send_started"] is False for r in D[M.RECIPIENTS].find({"state": "queued"}))


# ═════════ 4. staging concurrency script helpers ═════════

def _script():
    import tools.wa_staging_concurrency_check as S
    return S


def _ok_child(q):
    q.put(7)


def _crash_child(q):
    raise SystemExit(3)


def _hang_child(q):
    time.sleep(120)


def test_rate_window_and_bound_are_consistent():
    S = _script()
    assert S.RATE_WINDOW_SECONDS == 3 and S.RATE_PER_SECOND == 5
    assert S.rate_upper_bound(5, 3) == 20                      # a 3 s window can touch 4 one-second buckets
    assert S.rate_lower_bound(5, 3) == 10                      # and must contain at least 2 FULL buckets (sanity: tokens do flow)
    src = inspect.getsource(S._rate_proc)
    assert "sec_start + 3" not in src and "RATE_WINDOW_SECONDS" in src and "start_at" in src
    assert "start_at + 2" not in inspect.getsource(S.run_checks) and "RATE_START_DELAY" in inspect.getsource(S.run_checks)


def test_runner_collects_results_from_healthy_children():
    S = _script()
    assert S._run(3, _ok_child, (), timeout=20) == [7, 7, 7]


def test_runner_reports_a_crashed_child_quickly_instead_of_hanging():
    S = _script()
    t0 = time.time()
    with pytest.raises(RuntimeError, match="child"):
        S._run(2, _crash_child, (), timeout=60)
    assert time.time() - t0 < 30


def test_runner_times_out_and_terminates_a_hung_child():
    S = _script()
    t0 = time.time()
    with pytest.raises(RuntimeError, match="timed out|child"):
        S._run(1, _hang_child, (), timeout=2)
    assert time.time() - t0 < 20
    import multiprocessing as mp
    assert not [p for p in mp.active_children() if p.is_alive()]            # nothing left running


def _quick_exit_child(q):
    q.put(list(range(20000)))                  # a large result, then exit immediately: the old Queue.empty() race window


def _silent_exit_child(q):
    return                                     # exits 0 without ever sending a result


def _late_result_child(q):
    time.sleep(0.3)
    q.put("late")


def test_results_sent_just_before_a_fast_exit_are_never_lost():
    S = _script()
    for _ in range(25):                        # repeat: a flaky Queue.empty() would fail some of these runs
        out = S._run(6, _quick_exit_child, (), timeout=30)
        assert len(out) == 6 and all(len(r) == 20000 for r in out)


def test_results_are_returned_in_child_order_and_slow_children_are_waited_for():
    S = _script()
    assert S._run(3, _late_result_child, (), timeout=20) == ["late"] * 3


def test_child_that_exits_zero_without_a_result_is_detected_quickly():
    S = _script()
    t0 = time.time()
    with pytest.raises(RuntimeError, match="without a result"):
        S._run(3, _silent_exit_child, (), timeout=60)
    assert time.time() - t0 < 15


def _crash_then_hang_child(flag, q):
    import os
    try:
        os.close(os.open(flag, os.O_CREAT | os.O_EXCL))     # exactly one child wins the file: it crashes, the other hangs
    except FileExistsError:
        time.sleep(120)
        return
    raise SystemExit(4)


def test_crash_in_one_child_fails_fast_even_while_another_child_hangs(tmp_path):
    S = _script()
    import multiprocessing as mp
    t0 = time.time()
    with pytest.raises(RuntimeError, match="child"):
        S._run(2, _crash_then_hang_child, (str(tmp_path / "flag"),), timeout=60)
    assert time.time() - t0 < 15                            # did not wait for the 60 s timeout
    assert not [p for p in mp.active_children() if p.is_alive()]


def test_main_turns_a_runner_failure_into_a_failed_check_not_a_hang(monkeypatch, capsys, tmp_path):
    S = _script()
    # run_checks() creates a local ownership record first. Keep it in a temp folder and out of the host's real Windows ACL policy
    # (and out of the real %USERPROFILE%); the Windows policy has its own tests in test_wa_staging_concurrency_script.py.
    monkeypatch.setattr(S, "_is_windows", lambda: False)
    monkeypatch.setenv("CONC_OWNER_DIR", str(tmp_path / "own"))
    monkeypatch.setattr(S, "_icacls", lambda *a, **k: (_ for _ in ()).throw(AssertionError("real icacls")))
    monkeypatch.setattr(S, "_whoami", lambda *a, **k: (_ for _ in ()).throw(AssertionError("real whoami")))
    import argparse
    import mongomock
    client = mongomock.MongoClient()

    def fake_setup(name):
        db = client[name]
        M.db = db
        return db, M, W
    monkeypatch.setattr(S, "_setup", fake_setup)
    monkeypatch.setattr(S, "_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("child exited with code 1")))
    code = S.run_checks(argparse.Namespace(processes=2, recipients=5))
    out = capsys.readouterr().out
    assert code == 1 and "FAIL" in out and "child exited with code 1" in out
    assert list((tmp_path / "own").glob("*.json")) == []                       # the run cleaned up after itself (record removed after the verified drop)
