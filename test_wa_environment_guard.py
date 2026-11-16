"""Phase 2G safety: explicit environment identification, applied to BOTH the bulk worker and the single-number test send.
Nothing touches the network. Run:  python -m pytest test_wa_environment_guard.py -q -W ignore"""
import pytest

import services.wa_campaign_settings as ST
import services.wa_campaign_worker as W
import test_wa_campaign_lifecycle as L
from test_wa_campaign_lifecycle import _env, admin, audit_log, sender, tsend  # noqa: F401  (fixtures)
from test_wa_campaign_test_send_safety import ready
from test_wa_campaign_worker import scheduled, tick, camp  # noqa: F401

ALL = ("WA_CAMPAIGN_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT", "APP_ENV", "ENVIRONMENT", "ENV",
       "WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION")


def setenv(mp, **kw):
    for n in ALL:
        mp.delenv(n, raising=False)
    for k, v in kw.items():
        mp.setenv(k, v)


# ── classification ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env,expected,source", [
    ({"WA_CAMPAIGN_ENVIRONMENT": "staging"}, "staging", "explicit"),
    ({"WA_CAMPAIGN_ENVIRONMENT": "Development"}, "development", "explicit"),
    ({"WA_CAMPAIGN_ENVIRONMENT": " test "}, "test", "explicit"),
    ({"WA_CAMPAIGN_ENVIRONMENT": "production"}, "production", "explicit"),
    ({"WA_CAMPAIGN_ENVIRONMENT": "production", "RAILWAY_ENVIRONMENT_NAME": "staging"}, "production", "explicit"),
    ({}, "unknown", "none"),
    ({"WA_CAMPAIGN_ENVIRONMENT": "stagin"}, "unknown", "explicit_invalid"),                 # typo / unrecognised value
    ({"WA_CAMPAIGN_ENVIRONMENT": "prod"}, "unknown", "explicit_invalid"),                   # explicit values are exact words
    ({"RAILWAY_ENVIRONMENT_NAME": "staging"}, "unknown", "none"),                           # a "staging" signal alone is NOT trusted
    ({"RAILWAY_ENVIRONMENT_NAME": "Production"}, "production", "signal"),
    ({"RAILWAY_ENVIRONMENT": "prod"}, "production", "signal"),
    ({"APP_ENV": "production"}, "production", "signal"),
    ({"ENVIRONMENT": "prod"}, "production", "signal"),
    ({"ENV": "production"}, "production", "signal"),
])
def test_classification(monkeypatch, env, expected, source):
    setenv(monkeypatch, **env)
    st = ST.environment_status()
    assert st["classification"] == expected and st["source"] == source


def test_a_copied_generic_variable_cannot_make_an_explicit_staging_look_like_production(monkeypatch):
    # the likely real-world mistake: APP_ENV=production copied from the Production service into Staging
    setenv(monkeypatch, WA_CAMPAIGN_ENVIRONMENT="staging", APP_ENV="production", ENV="prod")
    st = ST.environment_status()
    assert st["classification"] == "staging" and st["overridden_signals"] == ["APP_ENV", "ENV"] and not st["conflict"]
    assert ST.environment_gate() == (True, "ok")


@pytest.mark.parametrize("var", ["RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT"])
def test_a_platform_production_name_always_wins_over_an_explicit_non_production_value(monkeypatch, var):
    setenv(monkeypatch, WA_CAMPAIGN_ENVIRONMENT="staging", **{var: "production"})
    st = ST.environment_status()
    assert st["classification"] == "production" and st["conflict"] is True and st["source"] == "platform_conflict"
    assert st["production_signals"] == [var]
    assert ST.environment_gate() == (False, "production_blocked")


def test_the_status_never_contains_anything_but_names_and_environment_words(monkeypatch):
    setenv(monkeypatch, WA_CAMPAIGN_ENVIRONMENT="staging", APP_ENV="production")
    assert set(ST.environment_status()) == {"classification", "source", "explicit", "conflict", "production_signals", "overridden_signals"}
    assert set(ST.config_snapshot()["environment"]) == set(ST.environment_status())


# ── gate ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env,ok,why", [
    ({"WA_CAMPAIGN_ENVIRONMENT": "staging"}, True, "ok"),
    ({"WA_CAMPAIGN_ENVIRONMENT": "development"}, True, "ok"),
    ({"WA_CAMPAIGN_ENVIRONMENT": "production"}, False, "production_blocked"),
    ({"RAILWAY_ENVIRONMENT_NAME": "production"}, False, "production_blocked"),
    ({}, False, "environment_unknown"),
    ({"WA_CAMPAIGN_ENVIRONMENT": "nonsense"}, False, "environment_unknown"),
])
def test_gate(monkeypatch, env, ok, why):
    setenv(monkeypatch, **env)
    assert ST.environment_gate() == (ok, why)


def test_only_a_classified_production_can_be_allowed_and_never_an_unknown_environment(monkeypatch):
    setenv(monkeypatch, WA_CAMPAIGN_ENVIRONMENT="production")
    assert ST.environment_gate(allow_production=True) == (True, "ok")
    setenv(monkeypatch)
    assert ST.environment_gate(allow_production=True) == (False, "environment_unknown")
    setenv(monkeypatch, WA_CAMPAIGN_ENVIRONMENT="bogus")
    assert ST.environment_gate(allow_production=True) == (False, "environment_unknown")


def test_a_failure_while_identifying_the_environment_fails_closed(monkeypatch):
    def boom():
        raise RuntimeError("secret detail")
    monkeypatch.setattr(ST, "environment_status", boom)
    assert ST.environment_gate() == (False, "environment_unknown")


# ── the bulk worker ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env,gate", [
    ({"WA_CAMPAIGN_ENVIRONMENT": "production"}, "production_blocked"),
    ({"WA_CAMPAIGN_ENVIRONMENT": "staging", "RAILWAY_ENVIRONMENT_NAME": "production"}, "production_blocked"),
    ({}, "environment_unknown"),
    ({"WA_CAMPAIGN_ENVIRONMENT": "oops"}, "environment_unknown"),
])
def test_the_worker_sends_nothing_in_production_or_an_unknown_environment(admin, _env, sender, env, gate):
    cid, _x = scheduled(_env)
    setenv(_env["mp"], **env)
    assert tick()["gate"] == gate and sender.calls == [] and camp(cid)["status"] == "scheduled"
    assert W.production_blocked() is True and W.start_worker_if_enabled() is False
    assert W.status()["production_blocked"] is True and W.status()["environment"]["classification"] in ("production", "unknown")


def test_the_worker_runs_on_an_explicit_staging_even_with_a_copied_production_app_env(admin, _env, sender):
    cid, _x = scheduled(_env)
    setenv(_env["mp"], WA_CAMPAIGN_ENVIRONMENT="staging", APP_ENV="production")
    assert tick()["gate"] == "ok" and W.production_blocked() is False


def test_the_worker_override_still_works_for_production_only(admin, _env, sender):
    cid, _x = scheduled(_env)
    setenv(_env["mp"], WA_CAMPAIGN_ENVIRONMENT="production", WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION="1")
    assert tick()["gate"] == "ok"
    setenv(_env["mp"], WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION="1")                      # override does NOT rescue an unknown environment
    assert tick()["gate"] == "environment_unknown"


def test_the_kill_switch_and_keys_are_still_required_on_staging(admin, _env, sender):
    cid, _x = scheduled(_env)
    setenv(_env["mp"], WA_CAMPAIGN_ENVIRONMENT="staging")
    _env["mp"].delenv("WA_CAMPAIGN_SENDING_ENABLED", raising=False)
    assert tick()["gate"] == "env_disabled" and sender.calls == []
    _env["mp"].setenv("WA_CAMPAIGN_SENDING_ENABLED", "1")
    _env["mp"].delenv("WA_CAMPAIGN_WORKER_ENABLED", raising=False)
    assert tick()["gate"] == "worker_disabled" and sender.calls == []


# ── the single-number test send (previously unguarded) ───────────────────────────────────────────

def test_test_send_works_on_an_explicit_staging(admin, _env, sender):
    cid = ready(admin, _env)
    setenv(_env["mp"], WA_CAMPAIGN_ENVIRONMENT="staging")
    r = tsend(cid)
    assert r.status_code == 200 and len(sender.calls) == 1


@pytest.mark.parametrize("env,code", [
    ({"WA_CAMPAIGN_ENVIRONMENT": "production"}, "production_blocked"),
    ({"WA_CAMPAIGN_ENVIRONMENT": "staging", "RAILWAY_ENVIRONMENT_NAME": "production"}, "production_blocked"),
    ({"APP_ENV": "production"}, "production_blocked"),
    ({}, "environment_unknown"),
    ({"WA_CAMPAIGN_ENVIRONMENT": "x"}, "environment_unknown"),
])
def test_test_send_is_refused_in_production_or_an_unknown_environment_and_sends_nothing(admin, _env, sender, env, code):
    cid = ready(admin, _env)
    setenv(_env["mp"], **env)
    r = tsend(cid)
    assert r.status_code == 409 and r.json()["detail"]["code"] == code and sender.calls == []
    assert audit_log("campaign.test_send")[-1]["outcome"] == "blocked:" + code


def test_test_send_has_no_production_override_even_with_the_worker_override_set(admin, _env, sender):
    cid = ready(admin, _env)
    setenv(_env["mp"], WA_CAMPAIGN_ENVIRONMENT="production", WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION="1")
    r = tsend(cid)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "production_blocked" and sender.calls == []


def test_test_send_dry_run_is_also_refused_in_production(admin, _env, sender):
    cid = ready(admin, _env)
    setenv(_env["mp"], WA_CAMPAIGN_ENVIRONMENT="production")
    r = tsend(cid, dry_run=True)
    assert r.status_code == 409 and sender.calls == []


def test_the_existing_test_send_gates_still_apply_on_staging(admin, _env, sender):
    cid = ready(admin, _env)
    setenv(_env["mp"], WA_CAMPAIGN_ENVIRONMENT="staging")
    _env["mp"].delenv("WA_CAMPAIGN_SENDING_ENABLED", raising=False)
    r = tsend(cid)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "sending_disabled" and sender.calls == []
    _env["mp"].setenv("WA_CAMPAIGN_SENDING_ENABLED", "1")
    _env["mp"].setenv("WA_CAMPAIGN_TEST_NUMBERS", "")
    r = tsend(cid)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_test_number" and sender.calls == []
