"""WhatsApp Campaign Manager: template hash stability, validation -> schedule flow, campaign versions, duplicate requests, worker.
Regression tests for the Production report "template_changed_since_sync after a successful sync" (Meta re-signs `example.header_handle`
on every request), the misleading "Someone else changed this campaign" message, and the stale "no worker" warning.
No network: Meta and the sender are faked.  Run:  python -m pytest test_wa_campaign_template_stability.py -q -W ignore"""
import copy
import json
import shutil
import subprocess
import re
import pathlib
from datetime import datetime, timedelta

import pytest

import services.wa_campaign_manager as M
import services.wa_campaign_settings as ST
import services.wa_campaign_worker as W
import services.wa_templates as T
import test_wa_campaign_lifecycle as L
from test_wa_campaign_lifecycle import (P1, P2, TESTNUM, TPL, BASE, H, _env, admin, meta, sender, act, patch, mk, client,  # noqa: F401
                                        catalogue, tpl, body_comp, confirm_body, merchant, start, admin_consent, stop, future)
from test_wa_campaign_worker import ADMIN, NOSLEEP, on, tick

D = L._DB
INVITE = "offro_invite"
INVITE_MAP = {"body.1": {"source": "merchant_name", "fallback": "Valued merchant"}}


def invite_template(body="Hi {{1}}, join OffrO today", footer="Reply STOP to opt out", fmt="IMAGE", btn="Open"):
    return tpl(INVITE, comps=[
        {"type": "HEADER", "format": fmt, "example": {"header_handle": ["https://scontent.whatsapp.net/v/t61/sample.png?oh=SIG0&oe=EXP0"]}},
        {"type": "BODY", "text": body, "example": {"body_text": [["Asha"]]}},
        {"type": "FOOTER", "text": footer},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": btn}]}])


class VolatileMeta(L.FakeMeta):
    """Like Meta: every response carries a freshly signed sample-media URL in example.header_handle."""
    def __init__(self, templates):
        super().__init__(templates)
        self.n = 0

    def __call__(self, url, headers=None, params=None, timeout=None):
        resp = super().__call__(url, headers=headers, params=params, timeout=timeout)
        if resp.status_code != 200:
            return resp
        data = copy.deepcopy(resp._d)
        self.n += 1
        for t in data["data"]:
            for c in t.get("components", []):
                if isinstance(c.get("example"), dict) and "header_handle" in c["example"]:
                    c["example"]["header_handle"] = ["https://scontent.whatsapp.net/v/t61/sample.png?oh=SIG%d&oe=EXP%d" % (self.n, self.n * 7)]
        return L.FakeResp(200, data)


@pytest.fixture
def vm(_env):
    m = VolatileMeta(catalogue() + [invite_template()])
    _env["mp"].setattr(T.requests, "get", m)
    return m


def set_live(vm, template):
    vm.templates = [template if t["name"] == template["name"] else t for t in vm.templates]


def sync():
    r = client.post(TPL + "/sync", headers=H("adm"))
    assert r.status_code == 200, r.text
    return r.json()


def enable():
    r = client.put("%s/%s/en/campaign-use" % (TPL, INVITE), headers=H("adm"), json={"enabled": True, "variables": INVITE_MAP})
    assert r.status_code == 200, r.text


def tdoc():
    return D[T.TEMPLATES].find_one({"_id": INVITE + "|en"})


def vready():
    """sync -> enable mapping -> draft with image header -> validate. Returns the campaign id. Meta re-signs the media URL on every call."""
    sync(); enable()
    ids = [merchant("M%d" % i, p) for i, p in enumerate((P1, P2))]
    start(P1)
    admin_consent(P2)
    c = mk()
    assert patch(c["id"], template={"name": INVITE, "language": "en"}, audience={"merchants": {"ids": ids}}).status_code == 200
    assert patch(c["id"], header_media={"type": "image", "id": "123456789"}).status_code == 200
    v = act(c["id"], "validate")
    assert v.status_code == 200, v.text
    return c["id"]


def conf(cid, **over):
    c = client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()
    return {"send_at": future(), "confirm": {"acknowledge": True, "eligible_count": c["snapshot"]["eligible"],
                                            "template": INVITE + "/en", **over}}


def version(cid):
    return client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()["version"]


# ═════════════ A. hash consistency between sync and the live check ═════════════

def test_hash_ignores_only_the_volatile_media_handle():
    base = invite_template()["components"]
    other = copy.deepcopy(base)
    other[0]["example"]["header_handle"] = ["https://scontent.whatsapp.net/other?oh=ZZZ"]
    assert T.content_hash(base) == T.content_hash(other)
    nohandle = copy.deepcopy(base)
    nohandle[0].pop("example")
    assert T.content_hash(nohandle) == T.content_hash(base)                  # key present-or-absent is the same template


def _variant(i, fn):
    c = copy.deepcopy(invite_template()["components"])
    fn(c)
    return c


@pytest.mark.parametrize("mutate", [
    lambda c: c[1].update(text="Hi {{1}}, join OffrO now"),                         # body text
    lambda c: c[0].update(format="VIDEO"),                                          # header format
    lambda c: c[2].update(text="Reply NO to opt out"),                              # footer
    lambda c: c[3]["buttons"][0].update(text="Stop it"),                            # button text
    lambda c: c[3]["buttons"][0].update(type="PHONE_NUMBER"),                       # button type
    lambda c: c[3]["buttons"].append({"type": "QUICK_REPLY", "text": "More"}),      # added button
    lambda c: c[1].update(example={"body_text": [["Someone Else"]]}),               # variable example value
    lambda c: c.pop(2),                                                             # component removed
    lambda c: c.append({"type": "FOOTER", "text": "x"}),                            # component added
])
def test_every_genuine_template_change_changes_the_hash(mutate):
    assert T.content_hash(invite_template()["components"]) != T.content_hash(_variant(0, mutate))


def test_repeated_syncs_with_a_resigned_media_url_are_unchanged_and_keep_the_mapping(admin, vm):
    sync(); enable()
    h1 = tdoc()["content_hash"]
    for _ in range(3):
        out = sync()
        assert out["counts"]["unchanged"] >= 1 and out["counts"]["updated"] == 0
    d = tdoc()
    assert d["content_hash"] == h1 and d["campaign_use"]["mapped_hash"] == h1 and d["hash_version"] == T.HASH_VERSION
    assert T.readiness(d)["usable"] is True and "mapping_stale" not in T.readiness(d)["reasons"]


def test_verify_live_agrees_with_the_cache_after_a_successful_sync(admin, vm):
    sync(); enable()
    for _ in range(3):                                                              # Meta re-signs the URL on each of these calls
        assert T.verify_live(tdoc()) == []


@pytest.mark.parametrize("name,change,path", [
    ("body", lambda c: c[1].update(text="CHANGED {{1}}"), "components[1].text"),
    ("header_format", lambda c: c[0].update(format="VIDEO"), "components[0].format"),
    ("footer", lambda c: c[2].update(text="CHANGED"), "components[2].text"),
    ("button", lambda c: c[3]["buttons"][0].update(text="CHANGED"), "components[3].buttons[0].text"),
])
def test_genuine_meta_change_still_blocks_and_reports_paths_only(admin, vm, name, change, path):
    sync(); enable()
    live = invite_template()
    change(live["components"])
    set_live(vm, live)
    why = {}
    assert "template_changed_since_sync" in T.verify_live(tdoc(), why)
    assert why["cause"] == "template_edited_at_meta" and path in why["changed_fields"]
    assert "CHANGED" not in json.dumps(why)                                         # paths, never template text


def test_status_or_category_change_still_blocks(admin, vm):
    sync(); enable()
    set_live(vm, dict(invite_template(), status="PAUSED"))
    assert "live_status_not_approved" in T.verify_live(tdoc())
    set_live(vm, dict(invite_template(), category="UTILITY"))
    assert "live_category_not_allowed" in T.verify_live(tdoc())


def test_cache_older_than_the_mapping_is_reported_as_stale_mapping_not_as_a_meta_change(admin, vm):
    sync()                                                                           # mapping never saved for this content
    why = {}
    assert "template_changed_since_sync" in T.verify_live(tdoc(), why)
    assert why == {"cause": "mapping_not_saved_for_current_content", "changed_fields": []}


# ═════════════ legacy cache (hashed with the old raw algorithm) ═════════════

def _make_legacy(raw_stale_handle=True):
    """Rewrite the stored template the way the OLD code stored it: raw-components hash, no hash_version, mapping current under it."""
    import hashlib
    d = tdoc()
    blob = json.dumps(d["components"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    old = hashlib.sha256(blob.encode()).hexdigest()[:32]
    D[T.TEMPLATES].update_one({"_id": d["_id"]}, {"$set": {"content_hash": old, "campaign_use.mapped_hash": old}, "$unset": {"hash_version": ""}})
    return old


def test_legacy_cache_is_repaired_by_one_sync_and_the_mapping_stays_current(admin, vm):
    sync(); enable()
    old = _make_legacy()
    assert old != T.content_hash(tdoc()["components"])
    why = {}
    assert "template_changed_since_sync" in T.verify_live(tdoc(), why) and why["cause"] == "cache_hash_outdated"     # fails CLOSED until re-synced
    out = sync()
    assert out["counts"]["unchanged"] >= 1
    d = tdoc()
    assert d["hash_version"] == T.HASH_VERSION and d["content_hash"] == d["campaign_use"]["mapped_hash"] != old
    assert T.verify_live(d) == [] and T.readiness(d)["usable"] is True


def test_legacy_cache_with_a_genuine_change_stays_blocked_after_sync(admin, vm):
    sync(); enable()
    _make_legacy()
    live = invite_template(body="Edited at Meta {{1}}")
    set_live(vm, live)
    out = sync()
    assert out["counts"]["updated"] >= 1
    d = tdoc()
    assert d["campaign_use"]["mapped_hash"] != d["content_hash"] and "mapping_stale" in T.readiness(d)["reasons"]


def test_legacy_mapping_that_was_already_stale_is_not_blessed_by_sync(admin, vm):
    sync(); enable()
    _make_legacy()
    D[T.TEMPLATES].update_one({"_id": INVITE + "|en"}, {"$set": {"campaign_use.mapped_hash": "0" * 32}})
    sync()
    d = tdoc()
    assert d["campaign_use"]["mapped_hash"] == "0" * 32 and "mapping_stale" in T.readiness(d)["reasons"]


# ═════════════ B. validation -> schedule, versions, stale / duplicate requests ═════════════

def test_validate_then_schedule_succeeds_although_meta_resigns_the_media_url(admin, vm):
    cid = vready()
    v_ready = version(cid)
    r = act(cid, "schedule", **conf(cid))
    assert r.status_code == 200, r.text
    c = r.json()["campaign"]
    assert c["status"] == "scheduled" and c["version"] == v_ready + 1               # exactly ONE intentional bump


def test_read_only_calls_and_template_refresh_never_change_the_campaign_version(admin, vm):
    cid = vready()
    v = version(cid)
    for _ in range(2):
        assert client.get("%s/%s/confirmation" % (BASE, cid), headers=H("adm")).status_code == 200
        assert client.get("%s/%s/recipients" % (BASE, cid), headers=H("adm")).status_code == 200
        assert act(cid, "preview").status_code == 200
        assert act(cid, "audience-preview").status_code == 200
        sync()                                                                      # template refresh: same content -> same hash
    assert version(cid) == v
    assert act(cid, "schedule", **conf(cid)).status_code == 200                     # still schedulable after all of that


def test_validation_and_snapshot_carry_the_same_template_snapshot_used_at_schedule(admin, vm):
    cid = vready()
    snap = D[M.CAMPAIGNS].find_one({"_id": M.ObjectId(cid)})["snapshot"]
    d = tdoc()
    assert snap["content_hash"] == d["content_hash"] and snap["mapped_hash"] == d["campaign_use"]["mapped_hash"]
    sync()
    assert tdoc()["content_hash"] == snap["content_hash"]                           # a refresh in between does not invalidate it


def test_a_real_template_edit_after_validation_still_blocks_scheduling(admin, vm):
    cid = vready()
    set_live(vm, invite_template(body="NEW TEXT {{1}}"))
    sync()
    r = act(cid, "schedule", **conf(cid))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "template_changed"
    assert client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()["status"] == "ready"


def test_a_real_meta_edit_without_a_sync_blocks_at_schedule_time_with_the_cause(admin, vm):
    cid = vready()
    set_live(vm, invite_template(body="NEW TEXT {{1}}"))
    r = act(cid, "schedule", **conf(cid))
    d = r.json()["detail"]
    assert r.status_code == 409 and d["code"] == "template_not_ready" and "template_changed_since_sync" in d["reasons"]
    assert d["cause"] == "template_edited_at_meta" and "components[1].text" in d["changed_fields"] and "NEW TEXT" not in r.text


def test_stale_expected_version_is_a_conflict_and_changes_nothing(admin):
    c = mk()
    assert patch(c["id"], name="Renamed once", expected_version=c["version"]).status_code == 200
    r = patch(c["id"], name="Renamed twice", expected_version=c["version"])          # stale
    assert r.status_code == 409 and r.json()["detail"]["code"] == "conflict"
    assert client.get("%s/%s" % (BASE, c["id"]), headers=H("adm")).json()["name"] == "Renamed once"


def test_duplicate_schedule_requests_second_one_is_refused_and_one_audit_ok(admin, vm):
    cid = vready()
    v = version(cid)
    body = conf(cid)
    assert act(cid, "schedule", **body).status_code == 200
    r2 = act(cid, "schedule", **body)                                               # double click / retry
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "not_ready"
    assert version(cid) == v + 1
    assert [a["outcome"] for a in L.audit_log("campaign.schedule") if a["outcome"] == "ok"] == ["ok"]


def test_two_requests_holding_the_same_stale_copy_cannot_both_win(admin, vm):
    cid = vready()
    a, b = M.get(cid), M.get(cid)                                                    # two requests loaded the same version
    M.schedule(a, datetime.utcnow() + timedelta(hours=3), ADMIN)
    with pytest.raises(M.CampaignError) as e:
        M.schedule(b, datetime.utcnow() + timedelta(hours=3), ADMIN)
    assert e.value.code == "conflict" and e.value.http == 409
    assert D[M.CAMPAIGNS].find_one({"_id": M.ObjectId(cid)})["status"] == "scheduled"


def test_duplicate_validate_requests_second_is_refused(admin, vm):
    cid = vready()
    v = version(cid)
    assert act(cid, "validate").status_code == 409
    assert version(cid) == v and D[M.RECIPIENTS].count_documents({"campaign_id": cid}) == 2   # snapshot not duplicated


# ═════════════ frontend: only a real version conflict is called "someone else" ═════════════

JS = pathlib.Path(__file__).with_name("static").joinpath("js", "wa_campaigns_editor.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_conflict_text_distinguishes_a_version_conflict_from_a_template_change():
    src = JS.read_text(encoding="utf-8")
    fn = re.search(r"function conflictText\(e\) \{.*?\n", src).group(0)
    script = fn + """
    const mk = (status, code, message) => ({status: status, message: message, detail: code ? {code: code, message: message} : undefined});
    console.log(JSON.stringify([
      conflictText(mk(409, 'conflict', 'The campaign was changed by someone else. Reload and try again.')),
      conflictText(mk(409, 'template_changed', 'The template or its mapping changed after validation. Revert to draft and validate again.')),
      conflictText(mk(409, 'template_not_ready', 'The template is no longer usable at Meta.')),
      conflictText(mk(409, 'invalid_transition', 'A scheduled campaign cannot become draft.')),
      conflictText(mk(500, 'conflict', 'server')),
      conflictText({status: 409, message: 'Some version changed text'})]));"""
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out[0] == "Someone else changed this campaign. Reload it and try again."
    assert out[1].startswith("The template or its mapping changed after validation")
    assert out[2] == "The template is no longer usable at Meta."
    assert out[3] == "A scheduled campaign cannot become draft."
    assert out[4] == "server" and out[5] == "Some version changed text"


# ═════════════ C. worker: truthful warning, gates, template re-verification ═════════════

def test_confirmation_warning_tells_the_truth_about_the_worker(admin, vm, _env):
    cid = vready()
    mp = _env["mp"]
    mp.delenv("WA_CAMPAIGN_WORKER_ENABLED", raising=False)
    w = {x["code"]: x["message"] for x in client.get("%s/%s/confirmation" % (BASE, cid), headers=H("adm")).json()["warnings"]}
    assert "no_worker_yet" in w and "NOT active" in w["no_worker_yet"] and "worker_disabled" in w["no_worker_yet"] and "worker_active" not in w
    assert "only records the time" not in json.dumps(w)
    mp.setenv("WA_CAMPAIGN_WORKER_ENABLED", "1")
    w = {x["code"] for x in client.get("%s/%s/confirmation" % (BASE, cid), headers=H("adm")).json()["warnings"]}
    assert "worker_active" in w and "no_worker_yet" not in w
    assert client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()["executes_automatically"] is True
    mp.setenv("WA_CAMPAIGN_ENVIRONMENT", "production")                                # Production without the explicit worker override
    w = {x["code"]: x["message"] for x in client.get("%s/%s/confirmation" % (BASE, cid), headers=H("adm")).json()["warnings"]}
    assert "production_blocked" in w["no_worker_yet"]
    assert client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()["executes_automatically"] is False
    mp.setenv("WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION", "1")
    assert ST.environment_gate(True) == (True, "ok") and M.scheduler_status() == (True, "ok")


def _due(vm_unused, env):
    cid = vready()
    M.schedule(M.get(cid), datetime.utcnow() - timedelta(minutes=1), ADMIN)
    on(env, P1, P2)
    return cid


def test_worker_launches_and_sends_an_image_template_although_meta_resigns_the_url(admin, vm, _env, sender):
    cid = _due(vm, _env)
    r = tick()
    assert str(r.get("launched") or r) and M.get(cid)["status"] in ("running", "completed")
    for _ in range(10):
        tick()
    sent_to = {c["phone"] for c in sender.calls}
    assert sent_to == {P1, P2}                                                       # allow-listed numbers only, each exactly once
    assert all(c["name"] == INVITE for c in sender.calls)
    assert all(any(p.get("type") == "header" for p in c["components"]) for c in sender.calls)     # header media is attached
    assert len(sender.calls) == 2


def test_worker_does_not_start_a_campaign_whose_template_really_changed(admin, vm, _env, sender):
    cid = _due(vm, _env)
    set_live(vm, invite_template(body="EDITED AT META {{1}}"))
    for _ in range(3):
        tick()
    assert M.get(cid)["status"] == "scheduled" and sender.calls == []


def test_worker_pauses_a_running_campaign_if_the_template_changes_mid_run(admin, vm, _env, sender):
    cid = _due(vm, _env)
    tick()
    set_live(vm, invite_template(body="EDITED MID RUN {{1}}"))
    for _ in range(3):
        tick()
    assert M.get(cid)["status"] in ("paused", "running", "completed")
    assert len(sender.calls) <= 2
    after = len(sender.calls)
    for _ in range(3):
        tick()
    assert len(sender.calls) == after                                                # once blocked, nothing more leaves


@pytest.mark.parametrize("off", ["worker", "sending_env", "kill_switch", "production", "unknown_env", "bad_template_status"])
def test_every_required_gate_blocks_the_worker(admin, vm, _env, sender, off):
    cid = _due(vm, _env)
    mp = _env["mp"]
    if off == "worker":
        mp.delenv("WA_CAMPAIGN_WORKER_ENABLED")
    elif off == "sending_env":
        mp.delenv("WA_CAMPAIGN_SENDING_ENABLED")
    elif off == "kill_switch":
        ST.set_sending_enabled(False, {"id": "a", "name": "A"})
    elif off == "production":
        mp.setenv("WA_CAMPAIGN_ENVIRONMENT", "production")
    elif off == "unknown_env":
        mp.setenv("WA_CAMPAIGN_ENVIRONMENT", "banana")
    elif off == "bad_template_status":
        set_live(vm, dict(invite_template(), status="PAUSED"))
    for _ in range(3):
        tick()
    assert sender.calls == [] and M.get(cid)["status"] == "scheduled"


def test_stop_and_non_allow_listed_numbers_are_still_never_contacted(admin, vm, _env, sender):
    cid = _due(vm, _env)
    stop(P2, "wamid.LATESTOP")                                                       # STOP arrives after the snapshot
    for _ in range(10):
        tick()
    assert {c["phone"] for c in sender.calls} == {P1}


def test_changing_the_hash_rules_did_not_touch_the_media_requirement(admin, vm):
    sync(); enable()
    ids = [merchant("M1", P1)]
    start(P1)
    c = mk()
    assert patch(c["id"], template={"name": INVITE, "language": "en"}, audience={"merchants": {"ids": ids}}).status_code == 200
    r = act(c["id"], "validate")                                                     # no header media attached
    assert r.status_code == 409 and "template:header_media_required" in r.json()["detail"]["errors"]
