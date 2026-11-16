"""Phase 2D follow-up: (1) consent/STOP re-read immediately before the WhatsApp send, (2) atomic duplicate protection.
Nothing touches the network. Run:  python -m pytest test_wa_campaign_test_send_safety.py -q -W ignore"""
import json
import threading
from datetime import datetime, timedelta

import pytest

import services.wa_campaign_manager as M
import services.wa_consent as C
import services.wa_eligibility as E
import services.wa_templates as T
import test_wa_campaign_lifecycle as L
from test_wa_campaign_lifecycle import (TESTNUM, _env, admin, audit_log, meta, sender, start, stop, tsend,  # noqa: F401  (fixtures)
                                        test_ready_for_test as _ready)
from test_wa_webhook_consent import _BrokenDb


def ready(admin, env):
    return _ready(admin, env)


def last_outcome():
    return audit_log("campaign.test_send")[-1]["outcome"]


# ═════════════ Correction 1: consent / STOP re-read right before the send ═════════════

@pytest.mark.parametrize("hook", ["after_initial_check_at_live_verify", "after_attempt_audit"])
def test_stop_between_initial_check_and_send_blocks_and_sends_nothing(admin, _env, sender, hook):
    cid = ready(admin, _env)
    mp = _env["mp"]
    if hook == "after_initial_check_at_live_verify":
        real = T.verify_live

        def hooked(tdoc):
            stop(TESTNUM, "wamid.STOPMID")                  # the recipient replies STOP while we talk to Meta
            return real(tdoc)
        mp.setattr(T, "verify_live", hooked)
    else:
        real_rec = M.audit.record

        def hooked(action, admin_, **kw):
            res = real_rec(action, admin_, **kw)
            if kw.get("outcome") == "attempt":
                stop(TESTNUM, "wamid.STOPMID")              # STOP lands right after the attempt is recorded
            return res
        mp.setattr(M.audit, "record", hooked)
    r = tsend(cid)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "recipient_not_eligible"
    assert sender.calls == []                                # NOTHING was sent
    log = audit_log("campaign.test_send")
    assert log[-1]["outcome"] == "blocked:recipient_not_eligible"
    assert log[-1]["details"] == {"reason": "opted_out", "stage": "final_check"}
    assert TESTNUM not in json.dumps(log, default=str)
    t = _DB_sends()
    assert t["status"] == "blocked" and t["outcome"] == "blocked:recipient_not_eligible"
    lock = L._DB[M.TEST_LOCKS].find_one()
    assert lock["next_allowed_at"] <= datetime.utcnow()      # nothing was sent, so the claim is free again


def _DB_sends():
    return L._DB[M.TEST_SENDS].find_one()


def test_blocked_final_check_does_not_count_against_limits(admin, _env, sender):
    cid = ready(admin, _env)
    real = T.verify_live
    _env["mp"].setattr(T, "verify_live", lambda t: (stop(TESTNUM, "wamid.S"), real(t))[1])
    assert tsend(cid).status_code == 409
    assert sender.calls == []
    assert L._DB[M.TEST_SENDS].count_documents({"status": {"$ne": "blocked"}}) == 0      # the blocked attempt does not count


@pytest.mark.parametrize("change", ["unknown", "revoked"])
def test_unknown_or_revoked_consent_at_final_check_blocks(admin, _env, sender, change):
    cid = ready(admin, _env)
    real = T.verify_live

    def hooked(tdoc):
        if change == "unknown":
            L._DB[C.CONTACTS].delete_many({})                # no consent record at all
        else:
            L._DB[C.CONTACTS].update_many({}, {"$set": {"status": "opted_out"}})
        return real(tdoc)
    _env["mp"].setattr(T, "verify_live", hooked)
    r = tsend(cid)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "recipient_not_eligible" and sender.calls == []
    assert last_outcome() == "blocked:recipient_not_eligible"


def test_consent_db_failure_at_final_check_fails_closed(admin, _env, sender):
    cid = ready(admin, _env)
    real = T.verify_live

    def hooked(tdoc):
        out = real(tdoc)
        _env["mp"].setattr(C, "db", _BrokenDb(L._DB, {C.CONTACTS: {"find_one", "find"}}))      # consent store goes down
        return out
    _env["mp"].setattr(T, "verify_live", hooked)
    r = tsend(cid)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "recipient_not_eligible"
    assert sender.calls == [] and last_outcome() == "blocked:recipient_not_eligible"


def test_unexpected_exception_in_final_check_fails_closed(admin, _env, sender):
    cid = ready(admin, _env)
    real, calls = E.check, {"n": 0}

    def flaky(phone):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("boom 919876500001")
        return real(phone)
    _env["mp"].setattr(E, "check", flaky)
    r = tsend(cid)
    assert r.status_code == 409 and sender.calls == [] and calls["n"] == 2
    log = audit_log("campaign.test_send")
    assert log[-1]["outcome"] == "blocked:recipient_not_eligible" and log[-1]["details"]["reason"] == "consent_lookup_failed"
    assert TESTNUM not in json.dumps(log, default=str) and TESTNUM not in r.text


def test_final_check_is_a_fresh_read_not_the_earlier_result(admin, _env, sender):
    cid = ready(admin, _env)
    real, seen = E.check, []
    _env["mp"].setattr(E, "check", lambda p: (seen.append(p), real(p))[1])
    assert tsend(cid).status_code == 200
    assert len(seen) == 2 and sender.calls                  # initial gate + the final re-read right before the send


# ═════════════ Correction 2: atomic claim ═════════════

def claim(cid="c1", h="hash1", admin_id="a"):
    return M._claim_test_send(cid, h, "0001", admin_id)


def test_second_claim_for_same_campaign_and_number_is_refused():
    claim()
    with pytest.raises(M.CampaignError) as ei:
        claim()
    assert ei.value.code == "cooldown"
    assert L._DB[M.TEST_LOCKS].count_documents({}) == 1


def test_different_campaign_or_number_can_claim_independently():
    claim("c1", "h1")
    claim("c2", "h1")
    claim("c1", "h2")
    assert L._DB[M.TEST_LOCKS].count_documents({}) == 3


def test_claim_is_one_atomic_upsert_not_find_then_insert():
    import inspect
    src = inspect.getsource(M._claim_test_send)
    assert "update_one(" in src and "upsert=True" in src and "find_one" not in src and "insert_one" not in src


def test_expired_claim_can_be_reclaimed():
    claim()
    L._DB[M.TEST_LOCKS].update_many({}, {"$set": {"next_allowed_at": datetime.utcnow() - timedelta(seconds=1)}})
    assert claim()[1]


@pytest.mark.parametrize("n", [2, 8, 32])
def test_many_simultaneous_claims_exactly_one_wins(n):
    gate, wins, losses, errs = threading.Barrier(n), [], [], []

    def worker():
        gate.wait()
        try:
            wins.append(claim())
        except M.CampaignError as e:
            losses.append(e.code)
        except Exception as e:                               # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(wins) == 1 and losses == ["cooldown"] * (n - 1)


def test_interleaved_requests_cannot_both_pass_a_stale_check():
    """Deterministic interleaving: B's claim happens while A is mid-flight (between A's claim and A's send)."""
    a = claim()
    with pytest.raises(M.CampaignError):
        claim()                                              # B arrives after A claimed but before A sent
    M._release_test_claim(*a)
    assert claim()                                           # only after A gave it back


def test_simultaneous_full_requests_send_exactly_once(admin, _env, sender):
    cid = ready(admin, _env)
    c = M.get(cid)
    n, gate, out = 6, threading.Barrier(6), []
    real_call = sender.__call__

    def slow(phone, name, language, components=None):
        import time
        time.sleep(0.05)
        return real_call(phone, name, language, components)
    _env["mp"].setattr(M.WA, "send_whatsapp_template_checked", slow)

    def worker():
        gate.wait()
        try:
            out.append(("ok", M.send_test_message(c, TESTNUM, {"id": "a1", "name": "A"}, True)))
        except M.CampaignError as e:
            out.append((e.code, None))
    ts = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(sender.calls) == 1
    assert [o for o, _ in out].count("ok") == 1
    assert {o for o, _ in out} <= {"ok", "cooldown", "rate_limited"}


def test_failed_send_keeps_the_cooldown_no_immediate_retry(admin, _env, sender):
    cid = ready(admin, _env)
    sender.result = {"ok": False, "outcome": "failed", "error_code": 131026}
    assert tsend(cid).status_code == 502 and len(sender.calls) == 1            # router reports a failed send as 502
    r = tsend(cid)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "cooldown" and len(sender.calls) == 1


def test_uncertain_send_is_held_well_beyond_the_normal_cooldown(admin, _env, sender):
    cid = ready(admin, _env)
    sender.result = {"ok": False, "outcome": "uncertain"}
    r = tsend(cid)
    j = r.json()["detail"]
    assert r.status_code == 202 and j["outcome"] == "uncertain" and "UNKNOWN" in j["warning"]
    lock = L._DB[M.TEST_LOCKS].find_one()
    assert lock["next_allowed_at"] > datetime.utcnow() + M.TEST_COOLDOWN + timedelta(minutes=5)
    assert tsend(cid).status_code == 429 and len(sender.calls) == 1


def test_sender_exception_never_releases_the_claim(admin, _env, sender):
    cid = ready(admin, _env)

    def boom(*a, **k):
        raise RuntimeError("network died mid-call")
    _env["mp"].setattr(M.WA, "send_whatsapp_template_checked", boom)
    assert tsend(cid).status_code == 503                    # reported as unavailable, never as "not sent"
    assert L._DB[M.TEST_LOCKS].find_one()["next_allowed_at"] > datetime.utcnow()
    _env["mp"].setattr(M.WA, "send_whatsapp_template_checked", sender)
    assert tsend(cid).status_code == 429 and sender.calls == []


@pytest.mark.parametrize("stage", ["verify_live", "audit_attempt"])
def test_failure_before_the_send_releases_the_claim_so_retry_works(admin, _env, sender, stage):
    cid = ready(admin, _env)
    mp = _env["mp"]
    if stage == "verify_live":
        real = T.verify_live
        mp.setattr(T, "verify_live", lambda t: (_ for _ in ()).throw(T.TemplateSyncError("meta_unavailable", "down")))
    else:
        mp.setattr(M.audit, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down")) if k.get("strict") else None)
    r = tsend(cid)
    assert r.status_code in (502, 503) and sender.calls == []
    assert L._DB[M.TEST_LOCKS].find_one()["next_allowed_at"] <= datetime.utcnow()


def test_claim_database_error_is_503_and_nothing_is_sent(admin, _env, sender):
    cid = ready(admin, _env)
    _env["mp"].setattr(M, "db", _BrokenDb(L._DB, {M.TEST_LOCKS: {"update_one"}}))
    r = tsend(cid)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "claim_unavailable" and sender.calls == []
    assert last_outcome() == "blocked:claim_unavailable"


def test_stale_token_cannot_release_someone_elses_claim():
    a = claim()
    M._release_test_claim(a[0], a[1])
    b = claim()
    M._release_test_claim(a[0], a[1])                        # A's late release must not free B's newer claim
    with pytest.raises(M.CampaignError):
        claim()
    M._release_test_claim(*b)
    assert claim()


def test_limit_race_a_request_that_slips_past_the_precheck_is_stopped_by_the_recheck(admin, _env, sender, monkeypatch):
    cid = ready(admin, _env)
    monkeypatch.setattr(M, "TEST_MAX_GLOBAL_HOUR", 1)
    real_claim = M._claim_test_send

    def claim_then_rival(*a, **k):
        out = real_claim(*a, **k)                            # our pre-check passed (budget was empty) ...
        L._DB[M.TEST_SENDS].insert_one({"campaign_id": "other", "admin_id": "zz", "status": "attempting",
                                        "created_at": datetime.utcnow()})        # ... a rival request records its attempt now
        return out
    monkeypatch.setattr(M, "_claim_test_send", claim_then_rival)
    r = tsend(cid)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited" and sender.calls == []
    assert last_outcome() == "blocked:rate_limited"
    assert L._DB[M.TEST_LOCKS].find_one()["next_allowed_at"] <= datetime.utcnow()      # nothing sent -> claim released


def test_blocked_attempts_are_audited_when_claim_denied(admin, _env, sender):
    cid = ready(admin, _env)
    assert tsend(cid).status_code == 200
    assert tsend(cid).status_code == 429
    assert last_outcome() == "blocked:cooldown" and len(sender.calls) == 1


# ═════════════ Correction 3: kill switch re-read immediately before the WhatsApp call ═════════════

def _during_verify(env, action):
    """Run `action()` while the request is in flight (after the first kill-switch / consent gates, before the send)."""
    real = T.verify_live

    def hooked(tdoc):
        action()
        return real(tdoc)
    env["mp"].setattr(T, "verify_live", hooked)


def _assert_blocked_clean(r, sender, why):
    assert r.status_code == 409 and r.json()["detail"]["code"] == "sending_disabled"
    assert sender.calls == []                                           # no WhatsApp sender invocation
    log = audit_log("campaign.test_send")
    assert log[-1]["outcome"] == "blocked:sending_disabled" and log[-1]["details"] == {"reason": why, "stage": "final_check"}
    assert TESTNUM not in json.dumps(log, default=str) and L.TOKEN not in json.dumps(log, default=str)
    assert TESTNUM not in r.text and L.TOKEN not in r.text
    t = _DB_sends()
    assert t["status"] == "blocked" and t["outcome"] == "blocked:sending_disabled"        # not "sent", not "failed"
    assert L._DB[M.TEST_SENDS].count_documents({"status": {"$in": ["sent", "failed", "uncertain"]}}) == 0
    assert L._DB[M.TEST_LOCKS].find_one()["next_allowed_at"] <= datetime.utcnow()         # own claim released


def test_env_switch_turned_off_mid_request_blocks_the_send(admin, _env, sender):
    cid = ready(admin, _env)
    _during_verify(_env, lambda: _env["mp"].delenv("WA_CAMPAIGN_SENDING_ENABLED"))
    _assert_blocked_clean(tsend(cid), sender, "env_disabled")


def test_database_switch_turned_off_mid_request_blocks_the_send(admin, _env, sender):
    cid = ready(admin, _env)
    _during_verify(_env, lambda: L.ST.set_sending_enabled(False, {"id": "a", "name": "A"}))
    _assert_blocked_clean(tsend(cid), sender, "switch_off")


def test_switch_lookup_failure_at_the_final_check_fails_closed(admin, _env, sender):
    cid = ready(admin, _env)
    _during_verify(_env, lambda: _env["mp"].setattr(L.ST, "db", _BrokenDb(L._DB, {L.ST.SETTINGS: {"find_one"}})))
    _assert_blocked_clean(tsend(cid), sender, "settings_unavailable")


def test_unexpected_exception_from_the_final_switch_check_fails_closed(admin, _env, sender):
    cid = ready(admin, _env)
    real, n = L.ST.sending_allowed, {"n": 0}

    def flaky():
        n["n"] += 1
        if n["n"] >= 2:
            raise RuntimeError("config exploded 919876500001 " + L.TOKEN)
        return real()
    _env["mp"].setattr(L.ST, "sending_allowed", flaky)
    r = tsend(cid)
    _assert_blocked_clean(r, sender, "settings_unavailable")
    assert n["n"] == 2


def test_both_switches_on_the_test_send_proceeds_normally(admin, _env, sender):
    cid = ready(admin, _env)
    r = tsend(cid)
    assert r.status_code == 200 and r.json()["outcome"] == "sent" and len(sender.calls) == 1
    assert [a["outcome"] for a in audit_log("campaign.test_send")] == ["attempt", "sent"]
    assert L._DB[M.TEST_LOCKS].find_one()["next_allowed_at"] > datetime.utcnow()           # cooldown still applies after a send


def test_switch_is_read_again_right_before_the_send_not_reused(admin, _env, sender):
    cid = ready(admin, _env)
    real, n = L.ST.sending_allowed, {"n": 0}
    _env["mp"].setattr(L.ST, "sending_allowed", lambda: (n.__setitem__("n", n["n"] + 1), real())[1])
    assert tsend(cid).status_code == 200 and n["n"] == 2


def test_blocked_by_final_switch_check_then_retry_works_once_sending_is_back_on(admin, _env, sender):
    cid = ready(admin, _env)
    real = T.verify_live
    _during_verify(_env, lambda: L.ST.set_sending_enabled(False, {"id": "a", "name": "A"}))
    assert tsend(cid).status_code == 409 and sender.calls == []
    _env["mp"].setattr(T, "verify_live", real)
    L.ST.set_sending_enabled(True, {"id": "a", "name": "A"})
    assert tsend(cid).status_code == 200 and len(sender.calls) == 1          # claim had been released; no cooldown penalty


def test_final_switch_block_never_releases_another_requests_claim(admin, _env, sender):
    cid = ready(admin, _env)

    def steal():                                                            # another replica now holds the claim
        L._DB[M.TEST_LOCKS].update_many({}, {"$set": {"claim_token": "OTHER-REQUEST", "next_allowed_at": datetime.utcnow() + timedelta(seconds=50)}})
        L.ST.set_sending_enabled(False, {"id": "a", "name": "A"})
    _during_verify(_env, steal)
    r = tsend(cid)
    assert r.status_code == 409 and sender.calls == []
    lock = L._DB[M.TEST_LOCKS].find_one()
    assert lock["claim_token"] == "OTHER-REQUEST" and lock["next_allowed_at"] > datetime.utcnow()      # untouched


def test_all_three_safeguards_work_together(admin, _env, sender):
    """Consent re-check, atomic claim and kill-switch re-check on one request path, each independently decisive."""
    cid = ready(admin, _env)
    real_verify = T.verify_live
    gate = {"stop": False, "off": False}

    def hooked(tdoc):
        if gate["stop"]:
            stop(TESTNUM, "wamid.S")
        if gate["off"]:
            L.ST.set_sending_enabled(False, {"id": "a", "name": "A"})
        return real_verify(tdoc)
    _env["mp"].setattr(T, "verify_live", hooked)
    gate["off"] = True
    assert tsend(cid).json()["detail"]["code"] == "sending_disabled"
    L.ST.set_sending_enabled(True, {"id": "a", "name": "A"})
    gate["off"], gate["stop"] = False, True
    assert tsend(cid).json()["detail"]["code"] == "recipient_not_eligible"
    assert sender.calls == []
    assert L._DB[M.TEST_SENDS].count_documents({"status": {"$ne": "blocked"}}) == 0      # blocked attempts never counted as sends
    assert audit_log("campaign.test_send")[-1]["outcome"] == "blocked:recipient_not_eligible"


def test_uncertain_send_protection_unchanged_with_the_final_switch_check(admin, _env, sender):
    cid = ready(admin, _env)
    sender.result = {"ok": False, "outcome": "uncertain"}
    assert tsend(cid).status_code == 202 and len(sender.calls) == 1
    assert L._DB[M.TEST_LOCKS].find_one()["next_allowed_at"] > datetime.utcnow() + timedelta(minutes=10)
    assert tsend(cid).status_code == 429 and len(sender.calls) == 1
