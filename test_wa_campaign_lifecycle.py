"""WhatsApp Campaign Manager - Phase 2D: campaign lifecycle, recipient snapshot, schedule METADATA and the gated TEST send.
No worker, no bulk path. Meta (template listing) and the WhatsApp sender are faked; nothing touches the network.
Run:  python -m pytest test_wa_campaign_lifecycle.py -q -W ignore"""
import json
import pathlib
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

import routers.dashboard_auth as DA
import routers.wa_campaign_lifecycle as RL
import routers.wa_campaigns as RC
import services.wa_audience as AU
import services.wa_campaign_audit as AUD
import services.wa_campaign_manager as M
import services.wa_campaign_settings as ST
import services.wa_consent as C
import services.wa_templates as T
import services.whatsapp as WA
from test_wa_campaign_templates import FakeMeta, FakeResp, body_comp, catalogue, tpl
from test_wa_webhook_consent import _BrokenDb

_DB = mongomock.MongoClient()["wa_lifecycle_test"]
BASE = "/admin/whatsapp-campaigns/campaigns"
TPL = "/admin/whatsapp-campaigns/templates"
TOKEN = "EAAB-secret-token-123"
REAL_CHECKED_SENDER = WA.send_whatsapp_template_checked          # the real function, before the fixture stubs it
P1, P2, P3, P4 = "919876543210", "919812345678", "919700000003", "919700000004"
TESTNUM = "919876500001"
OFFER_MAP = {"body.1": {"source": "merchant_name", "fallback": "Valued merchant"}, "body.2": {"source": "static", "value": "20% off"}}


class Sender:
    """Stands in for services.whatsapp.send_whatsapp_template_checked and records every call."""
    def __init__(self):
        self.calls, self.result = [], {"ok": True, "outcome": "sent", "message_id": "wamid.TEST1"}

    def __call__(self, phone, name, language, components=None):
        self.calls.append({"phone": phone, "name": name, "language": language, "components": components})
        return dict(self.result)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for n in _DB.list_collection_names():
        _DB.drop_collection(n)
    for mod in (C, AUD, ST, DA, T, AU, M):
        monkeypatch.setattr(mod, "db", _DB)
    monkeypatch.setattr(WA, "WHATSAPP_ACCESS_TOKEN", TOKEN)
    monkeypatch.setattr(WA, "WHATSAPP_PHONE_NUMBER_ID", "111222333")
    monkeypatch.setattr(WA, "WHATSAPP_BUSINESS_ACCOUNT_ID", "999888777")
    monkeypatch.setattr(T, "SYNC_COOLDOWN", timedelta(0))
    monkeypatch.delenv("WA_CAMPAIGN_SENDING_ENABLED", raising=False)
    monkeypatch.delenv("WA_CAMPAIGN_TEST_NUMBERS", raising=False)
    meta = FakeMeta(catalogue())
    monkeypatch.setattr(T.requests, "get", meta)
    sender = Sender()
    monkeypatch.setattr(WA, "send_whatsapp_template_checked", sender)
    return {"meta": meta, "sender": sender, "mp": monkeypatch}


@pytest.fixture
def meta(_env):
    return _env["meta"]


@pytest.fixture
def sender(_env):
    return _env["sender"]


app = FastAPI()
app.include_router(RC.router, prefix="/admin")
client = TestClient(app)


def make_user(token, perms=None):
    rid = _DB.dashboard_roles.insert_one({"role_name": "Custom Role", "permissions": perms or {}}).inserted_id
    _DB.dashboard_users.insert_one({"token": token, "role_id": rid, "full_name": "Asha Admin", "status": "active",
                                    "last_active_at": datetime.utcnow(), "mobile": "9000000001", "assigned_cities": ["*"]})
    return token


def grant(*actions):
    return {RC.MODULE: {a: True for a in actions}}


def H(t):
    return {"Authorization": "Bearer " + t}


@pytest.fixture
def admin():
    return make_user("adm", grant("view", "add", "edit", "delete", "approve"))


def merchant(name, phone):
    mid = str(_DB.accounts.insert_one({"name": name, "phone": phone, "status": "active", "roles": ["merchant"]}).inserted_id)
    _DB.stores.insert_one({"merchant_id": mid, "store_name": name + " Store", "city": "Pune", "area": "Kothrud",
                           "state": "Maharashtra", "status": "active"})
    return mid


def start(phone, mid="wamid.S1"):
    return C.record_opt_in(phone, "START", mid, str(int(datetime.utcnow().timestamp()) - 60), whatsapp_id=True)


def stop(phone, mid="wamid.X1"):
    return C.record_opt_out(phone, "STOP", mid, str(int(datetime.utcnow().timestamp()) - 30), whatsapp_id=True)


def admin_consent(phone):
    return C.record_admin_consent(phone, "signup_form", "FORM-1", (datetime.utcnow() - timedelta(days=2)).isoformat() + "Z",
                                  {"id": "a1", "name": "Asha", "role": "Lead"}, True)


def sending_on(mp):
    mp.setenv("WA_CAMPAIGN_SENDING_ENABLED", "1")
    ST.set_sending_enabled(True, {"id": "a", "name": "A"})
    mp.setenv("WA_CAMPAIGN_TEST_NUMBERS", TESTNUM)
    start(TESTNUM, "wamid.TESTNUM")


def sync_and_enable(token="adm", name="offro_offer", variables=OFFER_MAP):
    assert client.post(TPL + "/sync", headers=H(token)).status_code == 200
    r = client.put("%s/%s/en/campaign-use" % (TPL, name), headers=H(token), json={"enabled": True, "variables": variables})
    assert r.status_code == 200, r.text


def mk(token="adm", **body):
    r = client.post(BASE, headers=H(token), json={"name": "Diwali offer", "description": "d", **body})
    assert r.status_code == 200, r.text
    return r.json()


def patch(cid, token="adm", **body):
    return client.patch("%s/%s" % (BASE, cid), headers=H(token), json=body)


def act(cid, action, token="adm", **body):
    return client.post("%s/%s/%s" % (BASE, cid, action), headers=H(token), json=body)


def future(**kw):
    return (datetime.now(timezone.utc) + timedelta(**(kw or {"hours": 3}))).astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()


def world():
    """Template synced+enabled; 4 merchants (START, admin evidence, STOP, unknown)."""
    sync_and_enable()
    ids = [merchant("M%d" % i, p) for i, p in enumerate((P1, P2, P3, P4))]
    start(P1)
    admin_consent(P2)
    stop(P3)
    return ids


def ready_campaign(**extra):
    ids = world()
    c = mk()
    r = patch(c["id"], template={"name": "offro_offer", "language": "en"}, audience={"merchants": {"ids": ids}, **extra})
    assert r.status_code == 200, r.text
    v = act(c["id"], "validate")
    assert v.status_code == 200, v.text
    return c["id"], ids


def confirm_body(cid, send_at=None, **over):
    c = client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()
    conf = {"acknowledge": True, "eligible_count": c["snapshot"]["eligible"], "template": "offro_offer/en", **over}
    return {"send_at": send_at or future(), "confirm": conf}


def audit_log(action=None):
    q = {"action": action} if action else {}
    return list(_DB.wa_campaign_audit.find(q))


# ═════════════════════════════ drafts ═════════════════════════════

def test_create_draft_defaults_and_audit(admin):
    c = mk()
    assert c["status"] == "draft" and c["version"] == 1 and c["template"] is None and c["snapshot"] is None
    assert c["schedule"] is None and c["executes_automatically"] is False and c["created_by"] == "Asha Admin"
    assert [(a["action"], a["campaign_id"]) for a in audit_log("campaign.create")] == [("campaign.create", c["id"])]


@pytest.mark.parametrize("body", [{"name": "ab"}, {"name": ""}, {"name": 5}, {"name": "x" * 101}, {}, {"name": "ok name", "description": 5},
                                  {"name": "ok name", "description": "d" * 501}])
def test_create_rejects_bad_names_and_descriptions(admin, body):
    assert client.post(BASE, headers=H("adm"), json=body).status_code == 400


def test_name_is_sanitized(admin):
    assert mk(name="  Diwali \n\t offer  ")["name"] == "Diwali offer"


def test_list_filter_paging_and_404(admin):
    a, b = mk(name="First one"), mk(name="Second one")
    act(b["id"], "cancel")
    r = client.get(BASE, headers=H("adm")).json()
    assert r["total"] == 2 and [i["name"] for i in r["items"]] == ["Second one", "First one"]
    assert [i["id"] for i in client.get(BASE, headers=H("adm"), params={"status": "draft"}).json()["items"]] == [a["id"]]
    assert client.get(BASE, headers=H("adm"), params={"status": "bogus"}).status_code == 422
    assert client.get(BASE + "/nothex", headers=H("adm")).status_code == 404
    assert client.get(BASE + "/" + "0" * 24, headers=H("adm")).status_code == 404


def test_patch_draft_fields_and_cleaning(admin):
    c = mk()
    r = patch(c["id"], name="New name", description="New", template={"name": "offro_offer", "language": "en"},
              audience={"merchants": {"ids": ["a", "a", "b"], "filter": {"city": " Pune ", "x": "ignored"}}, "imports": ["i1", "i1"]})
    j = r.json()
    assert r.status_code == 200 and j["version"] == 2 and j["name"] == "New name"
    assert j["audience"] == {"merchants": {"ids": ["a", "b"], "filter": {"q": "", "city": "Pune", "area": "", "state": ""}}, "imports": ["i1"]}
    assert audit_log("campaign.update")[-1]["details"] == {"fields": ["audience", "description", "name", "template"]}


@pytest.mark.parametrize("body", [{}, {"bogus": 1}, {"template": "x"}, {"template": {"name": "Bad Name", "language": "en"}},
                                  {"template": {"name": "ok", "language": "english"}}, {"audience": []}, {"audience": {"imports": "x"}},
                                  {"audience": {"merchants": {"ids": "x"}}}, {"audience": {"merchants": {"filter": 5}}},
                                  {"header_media": {"type": "gif", "id": "12345"}}, {"header_media": {"type": "image"}},
                                  {"header_media": {"type": "image", "id": "12345", "link": "https://x.io/a.png"}},
                                  {"header_media": {"type": "image", "id": "abc"}}, {"header_media": {"type": "image", "link": "http://x.io/a.png"}},
                                  {"header_media": {"type": "image", "link": "https://u:p@x.io/a.png"}}, {"expected_version": "1", "name": "okay"}])
def test_patch_rejects_bad_input(admin, body):
    c = mk()
    assert patch(c["id"], **body).status_code == 400


def test_optimistic_concurrency_on_edit(admin):
    c = mk()
    assert patch(c["id"], name="Second", expected_version=1).status_code == 200
    r = patch(c["id"], name="Third", expected_version=1)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "conflict"


def test_changing_template_clears_header_media(admin):
    c = mk()
    patch(c["id"], template={"name": "offro_media", "language": "en"}, header_media={"type": "image", "id": "123456"})
    d = client.get("%s/%s" % (BASE, c["id"]), headers=H("adm")).json()
    assert d["header_media"] == {"type": "image", "kind": "id"} and "123456" not in json.dumps(d)
    assert patch(c["id"], template={"name": "offro_offer", "language": "en"}).json()["header_media"] is None


def test_only_drafts_are_editable_and_deletable_states(admin):
    cid, _ = ready_campaign()
    assert patch(cid, name="Changed").status_code == 409
    assert client.delete("%s/%s" % (BASE, cid), headers=H("adm")).status_code == 409
    act(cid, "cancel")
    assert patch(cid, name="Changed").status_code == 409
    assert client.delete("%s/%s" % (BASE, cid), headers=H("adm")).status_code == 200
    assert _DB[M.RECIPIENTS].count_documents({}) == 0 and _DB[M.CAMPAIGNS].count_documents({}) == 0


def test_delete_draft_is_audited_and_refused_if_audit_is_down(admin, _env):
    c = mk()
    _env["mp"].setattr(AUD, "db", _BrokenDb(_DB, {AUD.AUDIT: {"insert_one"}}))
    assert client.delete("%s/%s" % (BASE, c["id"]), headers=H("adm")).status_code == 503
    assert _DB[M.CAMPAIGNS].count_documents({}) == 1
    _env["mp"].setattr(AUD, "db", _DB)
    assert client.delete("%s/%s" % (BASE, c["id"]), headers=H("adm")).status_code == 200
    assert [a["outcome"] for a in audit_log("campaign.delete")] == ["attempt", "deleted"]


# ═════════════════════════════ preview ═════════════════════════════

def test_message_preview_with_fake_default_and_custom_sample(admin):
    sync_and_enable()
    c = mk()
    patch(c["id"], template={"name": "offro_offer", "language": "en"})
    j = act(c["id"], "preview").json()
    assert j["preview"]["body"] == "Hello Sample Merchant, offer 20% off" and j["sent"] is False and j["complete"]
    j = act(c["id"], "preview", recipient={"merchant_name": "Ravi", "bogus": "x"}).json()
    assert j["preview"]["body"] == "Hello Ravi, offer 20% off" and "bogus" not in j["sample_recipient"]
    j = act(c["id"], "preview", recipient={"merchant_name": "  "}).json()
    assert j["preview"]["body"] == "Hello Valued merchant, offer 20% off"          # blank value -> the mapping's fallback
    assert act(c["id"], "preview", recipient=[]).status_code == 400
    assert act(mk()["id"], "preview").status_code == 409                              # no template yet


def test_audience_preview_counts_and_reasons(admin):
    ids = world()
    c = mk()
    patch(c["id"], audience={"merchants": {"ids": ids}})
    j = act(c["id"], "audience-preview", page_size=2).json()
    assert j["summary"]["total"] == 4 and j["summary"]["eligible"] == 2
    assert j["summary"]["by_reason"] == {"opted_out": 1, "unknown_consent": 1} and len(j["recipients"]) == 2
    assert P1 not in json.dumps(j)
    assert act(c["id"], "audience-preview", status="excluded").json()["filtered_total"] == 2
    assert act(c["id"], "audience-preview", status="x").status_code == 400
    assert act(mk()["id"], "audience-preview").status_code == 409


# ═════════════════════════════ validate + snapshot ═════════════════════════════

def test_validate_blocks_incomplete_drafts(admin):
    sync_and_enable()
    c = mk()
    r = act(c["id"], "validate")
    assert r.status_code == 409 and set(r.json()["detail"]["errors"]) == {"template:template_not_selected", "audience:empty"}
    patch(c["id"], template={"name": "offro_utility", "language": "en"}, audience={"merchants": {"ids": ["x"]}})
    errs = act(c["id"], "validate").json()["detail"]["errors"]
    assert "template:category_not_allowed" in errs and "template:not_enabled" in errs
    patch(c["id"], template={"name": "offro_media", "language": "en"})
    assert "template:header_media_required" in act(c["id"], "validate").json()["detail"]["errors"]
    patch(c["id"], template={"name": "offro_offer", "language": "en"}, header_media={"type": "image", "id": "123456"})
    assert "template:header_media_not_needed" in act(c["id"], "validate").json()["detail"]["errors"]
    assert client.get("%s/%s" % (BASE, c["id"]), headers=H("adm")).json()["status"] == "draft"
    assert audit_log("campaign.validate")[-1]["outcome"] == "failed"


def test_validate_creates_snapshot_of_eligible_only(admin):
    cid, ids = ready_campaign()
    d = client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()
    assert d["status"] == "ready" and d["version"] == 3 and d["executes_automatically"] is False
    s = d["snapshot"]
    assert (s["total"], s["eligible"], s["excluded"]) == (4, 2, 2) and s["by_reason"] == {"opted_out": 1, "unknown_consent": 1}
    rows = list(_DB[M.RECIPIENTS].find({"campaign_id": cid}))
    assert sorted(r["phone"] for r in rows) == sorted([P1, P2])                                 # excluded people are NOT stored
    assert all(r["state"] == "snapshot" and r["snapshot_id"] == s["id"] for r in rows)
    assert {r["consent_basis"] for r in rows} == {"customer_start", "admin_recorded"}
    vals = {r["phone"]: r["values"] for r in rows}
    assert vals[P1] == {"body.1": "M0", "body.2": "20% off"}                            # values frozen at validation
    assert audit_log("campaign.validate")[-1]["details"]["eligible"] == 2
    assert P3 not in json.dumps(list(_DB.wa_campaign_audit.find()), default=str)


def test_snapshot_listing_is_masked_and_paged(admin):
    cid, _ = ready_campaign()
    r = client.get("%s/%s/recipients" % (BASE, cid), headers=H("adm"), params={"page_size": 1}).json()
    assert r["total"] == 2 and r["pages"] == 2 and r["items"][0]["phone_masked"].startswith("***") and r["items"][0]["consent_basis"]
    assert P1 not in json.dumps(r) and P2 not in json.dumps(r)
    assert client.get("%s/%s/recipients" % (BASE, mk()["id"]), headers=H("adm")).status_code == 409


def test_validate_fails_closed_when_consent_cannot_be_read(admin, _env):
    ids = world()
    c = mk()
    patch(c["id"], template={"name": "offro_offer", "language": "en"}, audience={"merchants": {"ids": ids}})
    _env["mp"].setattr(C, "db", _BrokenDb(_DB, {C.CONTACTS: {"find"}}))
    r = act(c["id"], "validate")
    assert r.status_code == 409 and r.json()["detail"]["errors"] == ["consent_check_failed"]
    assert _DB[M.RECIPIENTS].count_documents({}) == 0
    assert client.get("%s/%s" % (BASE, c["id"]), headers=H("adm")).json()["status"] == "draft"


def test_validate_with_no_eligible_recipient_is_refused(admin):
    sync_and_enable()
    m = merchant("Unknown", P4)
    c = mk()
    patch(c["id"], template={"name": "offro_offer", "language": "en"}, audience={"merchants": {"ids": [m]}})
    r = act(c["id"], "validate")
    assert r.status_code == 409 and r.json()["detail"]["errors"] == ["no_eligible_recipients"]
    assert _DB[M.RECIPIENTS].count_documents({}) == 0


def test_recipient_with_invalid_variable_value_is_excluded_not_sent_blank(admin):
    sync_and_enable(variables={"body.1": {"source": "merchant_name", "fallback": "Valued merchant"}, "body.2": {"source": "static", "value": "x"}})
    good, bad = merchant("Good", P1), merchant("B" * 1500, P2)
    start(P1); start(P2)
    c = mk()
    patch(c["id"], template={"name": "offro_offer", "language": "en"}, audience={"merchants": {"ids": [good, bad]}})
    v = act(c["id"], "validate").json()
    assert v["snapshot"]["eligible"] == 1 and v["snapshot"]["by_reason"] == {"invalid_variable_values": 1}
    assert [r["phone"] for r in _DB[M.RECIPIENTS].find({})] == [P1]


def test_custom_imports_and_duplicates_in_snapshot(admin):
    sync_and_enable()
    m = merchant("Asha", "9876543210")
    start(P1); start(P2)
    csv = client.post("/admin/whatsapp-campaigns/audience/imports/csv", headers=H("adm"),
                      files={"file": ("n.csv", b"phone,name\n9876543210,Same\n9812345678,Ravi\n")}).json()
    c = mk()
    patch(c["id"], template={"name": "offro_offer", "language": "en"}, audience={"merchants": {"ids": [m]}, "imports": [csv["import_id"]]})
    v = act(c["id"], "validate").json()
    assert v["snapshot"]["eligible"] == 2 and v["snapshot"]["by_reason"] == {"duplicate": 1}
    rows = {r["phone"]: r for r in _DB[M.RECIPIENTS].find({})}
    assert rows[P2]["source"] == "custom" and rows[P2]["values"]["body.1"] == "Valued merchant"     # no merchant name -> the mapping's fallback


def test_revalidation_replaces_the_snapshot_and_unique_index_protects_it(admin):
    cid, ids = ready_campaign()
    first = client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()["snapshot"]["id"]
    assert act(cid, "revert-to-draft").status_code == 200
    assert _DB[M.RECIPIENTS].count_documents({}) == 0 and client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()["snapshot"] is None
    stop(P1)
    assert act(cid, "validate").status_code == 200
    d = client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()
    assert d["snapshot"]["id"] != first and d["snapshot"]["eligible"] == 1
    assert [r["phone"] for r in _DB[M.RECIPIENTS].find({})] == [P2]
    M.ensure_indexes()
    with pytest.raises(DuplicateKeyError):
        _DB[M.RECIPIENTS].insert_one({"campaign_id": cid, "phone": P2, "snapshot_id": "x"})


def test_snapshot_write_failure_rolls_back_and_stays_draft(admin, _env):
    ids = world()
    c = mk()
    patch(c["id"], template={"name": "offro_offer", "language": "en"}, audience={"merchants": {"ids": ids}})
    _env["mp"].setattr(M, "db", _BrokenDb(_DB, {M.RECIPIENTS: {"insert_many"}}))
    r = act(c["id"], "validate")
    assert r.status_code == 503 and _DB[M.RECIPIENTS].count_documents({}) == 0
    assert _DB[M.CAMPAIGNS].find_one()["status"] == "draft"


def test_lost_race_during_validation_removes_the_new_snapshot(admin, _env):
    ids = world()
    c = mk()
    patch(c["id"], template={"name": "offro_offer", "language": "en"}, audience={"merchants": {"ids": ids}})

    def lost(*a, **k):
        raise M.CampaignError("conflict", 409, "changed")
    _env["mp"].setattr(M, "_transition", lost)
    assert act(c["id"], "validate").status_code == 409
    assert _DB[M.RECIPIENTS].count_documents({}) == 0


def test_two_validations_cannot_both_win(admin):
    cid, _ = ready_campaign()
    assert act(cid, "validate").status_code == 409                           # already ready: not a draft any more


# ═════════════════════════════ confirmation + schedule ═════════════════════════════

def test_confirmation_report_has_clear_warnings(admin):
    cid, _ = ready_campaign()
    j = client.get("%s/%s/confirmation" % (BASE, cid), headers=H("adm")).json()
    codes = {w["code"] for w in j["warnings"]}
    assert {"sending_disabled", "no_worker_yet", "consent_rechecked", "recipients_excluded", "no_test_send"} <= codes
    assert j["required_confirmation"] == {"eligible_count": 2, "acknowledge": True, "template": "offro_offer/en"}


def test_schedule_happy_path_stores_metadata_only(admin, sender, meta):
    cid, _ = ready_campaign()
    n_meta = len(meta.calls)
    r = act(cid, "schedule", **confirm_body(cid, "2099-01-01T10:00:00+05:30"))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "send_at_too_far"            # beyond the 90-day horizon
    r = act(cid, "schedule", **confirm_body(cid))
    assert r.status_code == 200, r.text
    c = r.json()["campaign"]
    assert c["status"] == "scheduled" and c["schedule"]["send_at"].endswith("Z") and c["schedule"]["confirmed_by"] == "Asha Admin"
    assert c["executes_automatically"] is False and any(w["code"] == "no_worker_yet" for w in r.json()["warnings"])
    assert sender.calls == [] and len(meta.calls) > n_meta                                     # live template check only; no send
    assert [(a["action"], a["outcome"]) for a in audit_log("campaign.schedule")] == [("campaign.schedule", "refused:send_at_too_far"),
                                                                                    ("campaign.schedule", "attempt"), ("campaign.schedule", "ok")]


@pytest.mark.parametrize("send_at", [None, "", "soon", 5, "2030-01-01T10:00:00", "2020-01-01T10:00:00Z"])
def test_schedule_rejects_bad_times(admin, send_at):
    cid, _ = ready_campaign()
    body = confirm_body(cid)
    body["send_at"] = send_at
    r = act(cid, "schedule", **body)
    assert r.status_code in (400, 409) and client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()["status"] == "ready"


def test_schedule_lead_time_and_horizon(admin):
    cid, _ = ready_campaign()
    soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    assert act(cid, "schedule", **confirm_body(cid, soon)).json()["detail"]["code"] == "send_at_too_soon"
    far = (datetime.now(timezone.utc) + timedelta(days=91)).isoformat()
    assert act(cid, "schedule", **confirm_body(cid, far)).json()["detail"]["code"] == "send_at_too_far"
    ok = (datetime.now(timezone.utc) + timedelta(minutes=16)).isoformat()
    assert act(cid, "schedule", **confirm_body(cid, ok)).status_code == 200


def test_send_at_is_stored_as_utc(admin):
    cid, _ = ready_campaign()
    ist = datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(microsecond=0) + timedelta(hours=5)
    act(cid, "schedule", **confirm_body(cid, ist.isoformat()))
    stored = _DB[M.CAMPAIGNS].find_one()["schedule"]["send_at"]
    assert stored == ist.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.mark.parametrize("over,code", [({"acknowledge": False}, "confirmation_required"), ({"acknowledge": "yes"}, "confirmation_required"),
                                       ({"eligible_count": 3}, "confirmation_mismatch"), ({"eligible_count": None}, "confirmation_mismatch"),
                                       ({"template": "other/en"}, "confirmation_mismatch")])
def test_schedule_needs_the_exact_explicit_confirmation(admin, over, code):
    cid, _ = ready_campaign()
    r = act(cid, "schedule", **confirm_body(cid, **over))
    assert r.json()["detail"]["code"] == code and client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()["status"] == "ready"
    assert act(cid, "schedule", send_at=future()).json()["detail"]["code"] == "confirmation_required"


def test_cannot_schedule_unvalidated_or_stale(admin):
    c = mk()
    assert act(c["id"], "schedule", send_at=future(), confirm={"acknowledge": True}).json()["detail"]["code"] == "not_ready"
    cid, _ = ready_campaign()
    _DB[M.CAMPAIGNS].update_one({"_id": _DB[M.CAMPAIGNS].find_one({"status": "ready"})["_id"]},
                                {"$set": {"snapshot.at": datetime.utcnow() - timedelta(hours=25)}})
    assert act(cid, "schedule", **confirm_body(cid)).json()["detail"]["code"] == "snapshot_stale"
    assert any(w["code"] == "snapshot_stale" for w in client.get("%s/%s/confirmation" % (BASE, cid), headers=H("adm")).json()["warnings"])


def test_schedule_rechecks_the_template_live(admin, meta):
    cid, _ = ready_campaign()
    meta.templates = [dict(t, status="PAUSED") if t["name"] == "offro_offer" else t for t in catalogue()]
    r = act(cid, "schedule", **confirm_body(cid))
    assert r.status_code == 409 and "live_status_not_approved" in r.json()["detail"]["reasons"]
    meta.templates = [tpl("offro_offer", comps=[body_comp("CHANGED {{1}} {{2}}", ["a", "b"])]) if t["name"] == "offro_offer" else t for t in catalogue()]
    assert "template_changed_since_sync" in act(cid, "schedule", **confirm_body(cid)).json()["detail"]["reasons"]
    meta.templates = [t for t in catalogue() if t["name"] != "offro_offer"]
    r = act(cid, "schedule", **confirm_body(cid))
    assert r.status_code == 502 and r.json()["detail"]["code"] == "template_unverified"
    meta.templates = catalogue()
    meta.fail = FakeResp(401, {"error": {"code": 190, "message": TOKEN}})
    r = act(cid, "schedule", **confirm_body(cid))
    assert r.status_code == 502 and TOKEN not in r.text
    assert client.get("%s/%s" % (BASE, cid), headers=H("adm")).json()["status"] == "ready"


def test_template_resync_after_validation_blocks_scheduling(admin, meta):
    cid, _ = ready_campaign()
    meta.templates = [tpl("offro_offer", comps=[body_comp("NEW {{1}} {{2}}", ["a", "b"])]) if t["name"] == "offro_offer" else t for t in catalogue()]
    assert client.post(TPL + "/sync", headers=H("adm")).status_code == 200
    assert act(cid, "schedule", **confirm_body(cid)).json()["detail"]["code"] == "template_changed"


def test_schedule_is_refused_if_audit_cannot_be_written(admin, _env):
    cid, _ = ready_campaign()
    body = confirm_body(cid)
    _env["mp"].setattr(AUD, "db", _BrokenDb(_DB, {AUD.AUDIT: {"insert_one"}}))
    assert act(cid, "schedule", **body).status_code == 503
    assert _DB[M.CAMPAIGNS].find_one()["status"] == "ready"


def test_schedule_twice_and_concurrent_attempt(admin):
    cid, _ = ready_campaign()
    body = confirm_body(cid)
    assert act(cid, "schedule", **body).status_code == 200
    assert act(cid, "schedule", **body).json()["detail"]["code"] == "not_ready"
    assert _DB[M.CAMPAIGNS].find_one()["status"] == "scheduled"


def test_unschedule_revert_cancel_transitions(admin):
    cid, _ = ready_campaign()
    act(cid, "schedule", **confirm_body(cid))
    assert act(cid, "revert-to-draft").status_code == 409                       # must unschedule first
    u = act(cid, "unschedule").json()
    assert u["status"] == "ready" and u["schedule"] is None and u["snapshot"]["eligible"] == 2
    assert act(cid, "unschedule").status_code == 409
    assert act(cid, "revert-to-draft").json()["status"] == "draft"
    assert act(cid, "revert-to-draft").status_code == 409
    c = act(cid, "cancel").json()
    assert c["status"] == "cancelled"
    for a in ("validate", "revert-to-draft", "unschedule", "cancel"):
        assert act(cid, a).status_code == 409
    assert act(cid, "schedule", send_at=future(), confirm={"acknowledge": True}).status_code == 409
    assert patch(cid, name="Zzz").status_code == 409


def test_cancel_works_from_every_open_state_and_drops_the_snapshot_even_if_audit_is_down(admin, _env):
    d = mk(name="Draft one")
    assert act(d["id"], "cancel").json()["status"] == "cancelled"
    cid, _ = ready_campaign()
    act(cid, "schedule", **confirm_body(cid))
    _env["mp"].setattr(AUD, "db", _BrokenDb(_DB, {AUD.AUDIT: {"insert_one"}}))
    assert act(cid, "cancel").json()["status"] == "cancelled"                  # safety direction never blocked by the audit log
    assert _DB[M.RECIPIENTS].count_documents({}) == 0


def test_transitions_are_guarded_by_status_and_version_so_two_admins_cannot_both_win(admin):
    cid, _ = ready_campaign()
    stale = M.get(cid)                                           # what a slow second admin / replica is holding
    assert act(cid, "revert-to-draft").status_code == 200       # the first admin wins
    with pytest.raises(M.CampaignError) as ei:
        M.cancel(stale, {"id": "x", "name": "Slow admin"})
    assert ei.value.code == "conflict"
    assert M.get(cid)["status"] == "draft" and _DB[M.CAMPAIGNS].find_one()["version"] == stale["version"] + 1
    stale_ready = dict(stale, status="draft")                    # right status, wrong version
    with pytest.raises(M.CampaignError):
        M.update_draft(dict(stale_ready, version=stale["version"]), {"name": "Lost update"}, None, {"id": "x"})
    assert M.get(cid)["name"] == "Diwali offer"


def test_completed_is_reserved_and_unreachable():
    assert all(M.COMPLETED not in t for t in M.TRANSITIONS.values()) and M.TRANSITIONS[M.COMPLETED] == set()


def test_edits_after_ready_require_going_back_through_validation(admin):
    cid, _ = ready_campaign()
    assert patch(cid, audience={"imports": []}).json()["detail"]["code"] == "not_editable"
    act(cid, "revert-to-draft")
    assert patch(cid, name="Edited again").status_code == 200
    assert act(cid, "schedule", send_at=future(), confirm={"acknowledge": True}).json()["detail"]["code"] == "not_ready"


# ═════════════════════════════ TEST send ═════════════════════════════

def tsend(cid, phone=TESTNUM, token="adm", **over):
    return client.post("%s/%s/test-send" % (BASE, cid), headers=H(token), json={"phone": phone, "confirm_test_send": True, **over})


def test_ready_for_test(admin, _env):
    cid, _ = ready_campaign()
    sending_on(_env["mp"])
    return cid


def test_test_send_happy_path_sends_exactly_one_message(admin, _env, sender):
    cid = test_ready_for_test(admin, _env)
    before = _DB[M.CAMPAIGNS].find_one()
    r = tsend(cid)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["sent"] is True and j["outcome"] == "sent" and j["message_id"] == "wamid.TEST1" and j["to_last4"] == TESTNUM[-4:]
    assert sender.calls == [{"phone": TESTNUM, "name": "offro_offer", "language": "en", "components": [
        {"type": "body", "parameters": [{"type": "text", "text": "Sample Merchant"}, {"type": "text", "text": "20% off"}]}]}]
    t = _DB[M.TEST_SENDS].find_one()
    assert t["status"] == "sent" and t["phone_last4"] == TESTNUM[-4:] and TESTNUM not in json.dumps(t, default=str)
    assert [(a["action"], a["outcome"]) for a in audit_log("campaign.test_send")] == [("campaign.test_send", "attempt"), ("campaign.test_send", "sent")]
    assert TESTNUM not in json.dumps(audit_log(), default=str) and TOKEN not in r.text
    after = _DB[M.CAMPAIGNS].find_one()
    assert (after["status"], after["version"], after["snapshot"]) == (before["status"], before["version"], before["snapshot"])
    assert _DB[M.RECIPIENTS].count_documents({"state": {"$ne": "snapshot"}}) == 0        # a test never touches recipients


def test_test_send_uses_custom_sample_data_and_works_on_a_draft(admin, _env, sender):
    sync_and_enable()
    sending_on(_env["mp"])
    c = mk()
    patch(c["id"], template={"name": "offro_offer", "language": "en"})
    assert tsend(c["id"], recipient={"merchant_name": "Ravi"}).status_code == 200
    assert sender.calls[0]["components"][0]["parameters"][0]["text"] == "Ravi"


def test_gate_confirmation_flag_required(admin, _env, sender):
    cid = test_ready_for_test(admin, _env)
    for over in ({"confirm_test_send": False}, {"confirm_test_send": "yes"}, {"confirm_test_send": None}):
        assert tsend(cid, **over).status_code == 400
    assert client.post("%s/%s/test-send" % (BASE, cid), headers=H("adm"), json={"phone": TESTNUM}).status_code == 400
    assert sender.calls == []


@pytest.mark.parametrize("how", ["env_off", "switch_off", "unreadable"])
def test_gate_global_kill_switch_cannot_be_bypassed(admin, _env, sender, meta, how):
    cid = test_ready_for_test(admin, _env)
    mp = _env["mp"]
    if how == "env_off":
        mp.delenv("WA_CAMPAIGN_SENDING_ENABLED")
    elif how == "switch_off":
        ST.set_sending_enabled(False, {"id": "a", "name": "A"})
    else:
        mp.setattr(ST, "db", _BrokenDb(_DB, {ST.SETTINGS: {"find_one"}}))
    n = len(meta.calls)
    r = tsend(cid)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "sending_disabled"
    assert sender.calls == [] and len(meta.calls) == n and audit_log("campaign.test_send")[-1]["outcome"] == "blocked:sending_disabled"
    assert tsend(cid, dry_run=True).status_code == 409                          # dry runs obey the same gates


def test_gate_allow_list_empty_or_other_number(admin, _env, sender, meta):
    cid = test_ready_for_test(admin, _env)
    other = "919876500002"
    start(other)
    n = len(meta.calls)
    r = tsend(cid, other)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_test_number"
    _env["mp"].delenv("WA_CAMPAIGN_TEST_NUMBERS")
    assert tsend(cid).status_code == 403                                           # empty allow-list = nobody
    _env["mp"].setenv("WA_CAMPAIGN_TEST_NUMBERS", "garbage, 123")
    assert tsend(cid).status_code == 403
    assert sender.calls == [] and len(meta.calls) == n


def test_gate_merchants_and_snapshot_members_are_not_test_numbers(admin, _env, sender):
    cid = test_ready_for_test(admin, _env)
    assert tsend(cid, P1).status_code == 403 and sender.calls == []                # a real consenting recipient, not allow-listed


@pytest.mark.parametrize("bad", ["", "12", "abc", "+1234567890123456789"])
def test_invalid_phone_is_400(admin, _env, sender, bad):
    cid = test_ready_for_test(admin, _env)
    assert tsend(cid, bad).status_code == 400 and sender.calls == []


def test_gate_consent_opt_out_unknown_and_lookup_failure(admin, _env, sender, meta):
    cid = test_ready_for_test(admin, _env)
    n = len(meta.calls)
    stop(TESTNUM, "wamid.STOPTEST")
    r = tsend(cid)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "opted_out"
    _DB.wa_contacts.delete_many({})
    assert tsend(cid).json()["detail"]["reason"] == "unknown_consent"
    start(TESTNUM, "wamid.AGAIN")
    _env["mp"].setattr(C, "db", _BrokenDb(_DB, {C.CONTACTS: {"find"}}))
    assert tsend(cid).json()["detail"]["reason"] == "consent_lookup_failed"
    assert sender.calls == [] and len(meta.calls) == n
    assert [a["outcome"] for a in audit_log("campaign.test_send")] == ["blocked:recipient_not_eligible"] * 3


def test_admin_recorded_consent_is_enough_for_a_test_number(admin, _env, sender):
    cid = test_ready_for_test(admin, _env)
    _DB.wa_contacts.delete_many({})
    admin_consent(TESTNUM)
    assert tsend(cid).status_code == 200 and len(sender.calls) == 1


def test_stop_arriving_right_before_the_send_blocks_it(admin, _env, sender):
    cid = test_ready_for_test(admin, _env)
    assert tsend(cid, dry_run=True).status_code == 200                              # fine a moment ago ...
    stop(TESTNUM, "wamid.LATE")
    assert tsend(cid).status_code == 409 and sender.calls == []                     # ... consent is re-read every time


@pytest.mark.parametrize("mutate,reason", [
    (lambda ts: [dict(t, status="PAUSED") if t["name"] == "offro_offer" else t for t in ts], "live_status_not_approved"),
    (lambda ts: [dict(t, status="REJECTED") if t["name"] == "offro_offer" else t for t in ts], "live_status_not_approved"),
    (lambda ts: [dict(t, category="UTILITY") if t["name"] == "offro_offer" else t for t in ts], "live_category_not_allowed"),
    (lambda ts: [tpl("offro_offer", comps=[body_comp("EDITED {{1}} {{2}}", ["a", "b"])]) if t["name"] == "offro_offer" else t for t in ts],
     "template_changed_since_sync"),
])
def test_live_template_recheck_blocks_a_changed_template(admin, _env, sender, meta, mutate, reason):
    cid = test_ready_for_test(admin, _env)
    meta.templates = mutate(catalogue())
    r = tsend(cid)
    assert r.status_code == 409 and reason in r.json()["detail"]["reasons"] and sender.calls == []
    assert _DB[M.TEST_SENDS].count_documents({}) == 0


def test_live_template_recheck_fails_closed_on_meta_problems(admin, _env, sender, meta):
    cid = test_ready_for_test(admin, _env)
    meta.templates = [t for t in catalogue() if t["name"] != "offro_offer"]
    r = tsend(cid)
    assert r.status_code == 502 and r.json()["detail"]["template_error"] == "template_not_found"
    meta.templates = catalogue()
    for fail in (FakeResp(500, {"error": {"code": 2}}), FakeResp(429, {"error": {"code": 4}}), TimeoutError("t " + TOKEN)):
        meta.fail = fail
        r = tsend(cid)
        assert r.status_code == 502 and r.json()["detail"]["code"] == "template_unverified" and TOKEN not in r.text
    assert sender.calls == []


def test_cached_readiness_is_checked_too(admin, _env, sender):
    cid = test_ready_for_test(admin, _env)
    client.put(TPL + "/offro_offer/en/campaign-use", headers=H("adm"), json={"enabled": False, "variables": OFFER_MAP})
    r = tsend(cid)
    assert r.status_code == 409 and "not_enabled" in r.json()["detail"]["reasons"] and sender.calls == []
    assert tsend(mk()["id"]).json()["detail"]["reasons"] == ["template_not_selected"]


def test_media_template_needs_header_media_and_passes_it(admin, _env, sender):
    sync_and_enable()
    client.put(TPL + "/offro_media/en/campaign-use", headers=H("adm"), json={"enabled": True, "variables": {
        "body.1": {"source": "store_name", "fallback": "your store"}, "button.0": {"source": "static", "value": "abc"}}})
    sending_on(_env["mp"])
    c = mk()
    patch(c["id"], template={"name": "offro_media", "language": "en"})
    assert tsend(c["id"]).json()["detail"]["reasons"] == ["header_media_required"] and sender.calls == []
    patch(c["id"], header_media={"type": "image", "id": "123456"})
    assert tsend(c["id"]).status_code == 200
    comps = sender.calls[0]["components"]
    assert comps[0] == {"type": "header", "parameters": [{"type": "image", "image": {"id": "123456"}}]} and comps[2]["sub_type"] == "url"


def test_dry_run_returns_the_payload_but_sends_and_records_nothing(admin, _env, sender):
    cid = test_ready_for_test(admin, _env)
    r = tsend(cid, dry_run=True)
    j = r.json()
    assert r.status_code == 200 and j["dry_run"] is True and j["sent"] is False and j["components"][0]["type"] == "body"
    assert sender.calls == [] and _DB[M.TEST_SENDS].count_documents({}) == 0 and audit_log("campaign.test_send") == []
    for _ in range(12):                                                              # dry runs never consume the send limits
        assert tsend(cid, dry_run=True).status_code == 200
    assert tsend(cid).status_code == 200


@pytest.mark.parametrize("result,http,status", [
    ({"ok": False, "outcome": "uncertain", "error_code": None}, 202, "uncertain"),
    ({"ok": False, "outcome": "rejected_permanent", "error_code": 131026}, 502, "failed"),
    ({"ok": False, "outcome": "rejected_retryable", "error_code": 131048}, 502, "failed"),
])
def test_failed_or_unknown_outcomes_are_recorded_and_never_retried(admin, _env, sender, result, http, status):
    cid = test_ready_for_test(admin, _env)
    sender.result = result
    r = tsend(cid)
    assert r.status_code == http and r.json()["detail"]["outcome"] == result["outcome"]
    assert len(sender.calls) == 1                                                    # exactly one attempt, no automatic retry
    assert _DB[M.TEST_SENDS].find_one()["status"] == status
    assert audit_log("campaign.test_send")[-1]["outcome"] == result["outcome"]
    if status == "uncertain":
        assert "UNKNOWN" in r.json()["detail"]["warning"]


def test_rate_limits_cooldown_per_admin_and_global(admin, _env, sender):
    cid = test_ready_for_test(admin, _env)
    assert tsend(cid).status_code == 200
    r = tsend(cid)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "cooldown" and len(sender.calls) == 1
    _DB[M.TEST_SENDS].update_many({}, {"$set": {"created_at": datetime.utcnow() - timedelta(minutes=5)}})
    _DB[M.TEST_LOCKS].update_many({}, {"$set": {"next_allowed_at": datetime.utcnow() - timedelta(minutes=4)}})      # cooldown over
    assert tsend(cid).status_code == 200
    for i in range(M.TEST_MAX_PER_ADMIN_HOUR):
        _DB[M.TEST_SENDS].insert_one({"campaign_id": "x", "admin_id": audit_log("campaign.test_send")[0]["admin"]["id"],
                                      "phone_hash": "h%d" % i, "created_at": datetime.utcnow() - timedelta(minutes=10), "status": "sent"})
    r = tsend(cid)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited" and len(sender.calls) == 2


def test_global_hourly_cap(admin, _env, sender):
    cid = test_ready_for_test(admin, _env)
    for i in range(M.TEST_MAX_GLOBAL_HOUR):
        _DB[M.TEST_SENDS].insert_one({"campaign_id": "x", "admin_id": "someone-%d" % i, "phone_hash": "h%d" % i,
                                      "created_at": datetime.utcnow() - timedelta(minutes=10), "status": "sent"})
    assert tsend(cid).json()["detail"]["code"] == "rate_limited" and sender.calls == []


def test_test_send_is_refused_if_the_attempt_cannot_be_recorded(admin, _env, sender):
    cid = test_ready_for_test(admin, _env)
    _env["mp"].setattr(AUD, "db", _BrokenDb(_DB, {AUD.AUDIT: {"insert_one"}}))
    r = tsend(cid)
    assert r.status_code == 503 and sender.calls == [] and _DB[M.TEST_SENDS].count_documents({}) == 0
    _env["mp"].setattr(AUD, "db", _DB)
    _env["mp"].setattr(M, "db", _BrokenDb(_DB, {M.TEST_SENDS: {"insert_one"}}))
    assert tsend(cid).status_code == 503 and sender.calls == []


def test_closed_campaigns_cannot_test_send(admin, _env, sender):
    cid = test_ready_for_test(admin, _env)
    act(cid, "cancel")
    assert tsend(cid).json()["detail"]["code"] == "campaign_closed" and sender.calls == []


def test_successful_test_removes_the_no_test_send_warning(admin, _env):
    cid = test_ready_for_test(admin, _env)
    assert tsend(cid).status_code == 200
    codes = {w["code"] for w in client.get("%s/%s/confirmation" % (BASE, cid), headers=H("adm")).json()["warnings"]}
    assert "no_test_send" not in codes and "sending_disabled" not in codes


def test_wa_checked_template_sender_classification_and_payload(monkeypatch):
    sent = []

    class R:
        def __init__(self, status, data):
            self.status_code, self._d = status, data

        def json(self):
            if self._d is None:
                raise ValueError
            return self._d

    def post_ok(url, headers=None, json=None, timeout=None):
        sent.append({"url": url, "json": json, "timeout": timeout})
        return R(200, {"messages": [{"id": "wamid.X"}]})
    monkeypatch.setattr(WA.requests, "post", post_ok)
    out = REAL_CHECKED_SENDER("+91 98765-00001", "offro_offer", "en", [{"type": "body", "parameters": []}])
    assert out == {"ok": True, "outcome": "sent", "message_id": "wamid.X"} and len(sent) == 1
    p = sent[0]["json"]
    assert p["to"] == "919876500001" and p["type"] == "template" and sent[0]["timeout"] == 10
    assert p["template"] == {"name": "offro_offer", "language": {"code": "en"}, "components": [{"type": "body", "parameters": []}]}
    for status, data, outcome in ((500, {"error": {"code": 1}}, "uncertain"), (200, {"x": 1}, "uncertain"),
                                  (400, {"error": {"code": 131026}}, "rejected_permanent"), (429, {}, "rejected_retryable")):
        monkeypatch.setattr(WA.requests, "post", lambda *a, _s=status, _d=data, **k: R(_s, _d))
        assert REAL_CHECKED_SENDER("919876500001", "t", "en")["outcome"] == outcome
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise TimeoutError("boom " + TOKEN)
    monkeypatch.setattr(WA.requests, "post", boom)
    assert REAL_CHECKED_SENDER("919876500001", "t", "en") == {"ok": False, "outcome": "uncertain", "error_code": None}
    assert calls == [1]                                                              # a timeout is NOT retried
    monkeypatch.setattr(WA, "WHATSAPP_ACCESS_TOKEN", "")
    assert REAL_CHECKED_SENDER("919876500001", "t", "en")["outcome"] == "rejected_permanent"
    assert callable(WA.send_whatsapp_template) and callable(WA.send_whatsapp_message_checked)      # originals still there


# ═════════════════════════════ permissions & safety review ═════════════════════════════

def test_every_route_needs_the_campaign_module_and_the_right_action(admin):
    cid, _ = ready_campaign()
    make_user("none", {"Merchants": {"view": True, "edit": True, "approve": True, "add": True, "delete": True}})
    make_user("viewer", grant("view"))
    make_user("editor", grant("view", "edit"))
    make_user("adder", grant("view", "add"))
    calls = [("post", "", {}), ("get", "", None), ("get", "/" + cid, None), ("patch", "/" + cid, {"name": "zzz zzz"}),
             ("delete", "/" + cid, None), ("post", "/%s/preview" % cid, {}), ("post", "/%s/audience-preview" % cid, {}),
             ("post", "/%s/validate" % cid, {}), ("post", "/%s/revert-to-draft" % cid, {}), ("get", "/%s/recipients" % cid, None),
             ("get", "/%s/confirmation" % cid, None), ("post", "/%s/test-send" % cid, {"phone": TESTNUM, "confirm_test_send": True}),
             ("post", "/%s/schedule" % cid, {}), ("post", "/%s/unschedule" % cid, {}), ("post", "/%s/cancel" % cid, {})]
    for method, path, body in calls:
        assert client.request(method.upper(), BASE + path).status_code in (401, 403), (method, path)
        assert client.request(method.upper(), BASE + path, headers=H("none"), json=body).status_code == 403, (method, path)
    # view-only: reads work, nothing else does
    for method, path, body in calls:
        want_ok = method == "get" or path.endswith(("/preview", "/audience-preview"))
        r = client.request(method.upper(), BASE + path, headers=H("viewer"), json=body)
        assert (r.status_code != 403) == want_ok, (method, path, r.status_code)
    # edit but not approve: cannot test-send or schedule
    for path in ("/%s/test-send" % cid, "/%s/schedule" % cid):
        assert client.post(BASE + path, headers=H("editor"), json={"phone": TESTNUM, "confirm_test_send": True}).status_code == 403
    assert client.delete(BASE + "/" + cid, headers=H("editor")).status_code == 403
    assert client.post(BASE, headers=H("editor"), json={"name": "abc def"}).status_code == 403
    assert client.post(BASE, headers=H("adder"), json={"name": "abc def"}).status_code == 200
    assert client.patch(BASE + "/" + cid, headers=H("adder"), json={"name": "abc def"}).status_code == 403


def test_the_seeded_roles_get_no_campaign_access_automatically():
    DA.seed_rbac()
    for role in _DB.dashboard_roles.find({}):
        if role["role_name"] != "Super Admin":
            assert not any(role["permissions"].get(RC.MODULE, {}).values())


def test_lifecycle_routes_are_exactly_these_and_namespaced(admin):
    paths = {(m, r.path) for r in RL.router.routes for m in r.methods}
    assert paths == {("POST", "/campaigns"), ("GET", "/campaigns"), ("GET", "/campaigns/{campaign_id}"),
                     ("PATCH", "/campaigns/{campaign_id}"), ("DELETE", "/campaigns/{campaign_id}"),
                     ("POST", "/campaigns/{campaign_id}/preview"), ("POST", "/campaigns/{campaign_id}/audience-preview"),
                     ("POST", "/campaigns/{campaign_id}/validate"), ("POST", "/campaigns/{campaign_id}/revert-to-draft"),
                     ("GET", "/campaigns/{campaign_id}/recipients"), ("GET", "/campaigns/{campaign_id}/confirmation"),
                     ("POST", "/campaigns/{campaign_id}/test-send"), ("POST", "/campaigns/{campaign_id}/schedule"),
                     ("POST", "/campaigns/{campaign_id}/unschedule"), ("POST", "/campaigns/{campaign_id}/cancel")}
    assert not any(w in p for _m, p in paths for w in ("bulk", "launch", "execute", "run", "send-now", "start"))


def test_no_worker_scheduler_or_second_sender_exists():
    for path in ("services/wa_campaign_manager.py", "routers/wa_campaign_lifecycle.py"):
        src = pathlib.Path(path).read_text()
        for forbidden in ("BackgroundTasks", "threading", "asyncio.create_task", "apscheduler", "sched.", "Timer(", "while True",
                          "send_whatsapp_message", "requests.post", "wa.send_whatsapp_template(", "WA.send_whatsapp_template("):
            assert forbidden not in src, (path, forbidden)
    mgr = pathlib.Path("services/wa_campaign_manager.py").read_text()
    assert mgr.count("WA.send_whatsapp_template_checked(") == 1                      # the single call site, inside send_test_message
    assert mgr.index("WA.send_whatsapp_template_checked(") > mgr.index("def send_test_message")
    assert "WA_CAMPAIGN_WORKER_ENABLED" not in mgr


def test_sender_function_is_called_only_after_every_local_gate():
    src = pathlib.Path("services/wa_campaign_manager.py").read_text()
    src = src[src.index("def send_test_message"):]
    order = [src.index(x) for x in ("confirm is not True", "template_problems(campaign)", "ST.sending_allowed()", "ST.is_test_number(",
                                    "E.check(canonical)", "_over_test_limits(admin_id, now)", "_claim_test_send(str(", "T.verify_live(",
                                    "_components_for(tdoc", "strict=True", "_over_test_limits(admin_id, now, own=1)",
                                    "final = E.check(canonical)", "# fails closed itself", "WA.send_whatsapp_template_checked(")]
    assert order == sorted(order)


def test_nothing_at_startup_starts_processing_and_indexes_are_new_collections_only():
    server = pathlib.Path("server.py").read_text()
    assert "wa_campaign_manager" in server and "ensure_indexes_safe" in server
    assert M.ensure_indexes_safe() is True
    names = {n for coll in (M.CAMPAIGNS, M.RECIPIENTS, M.TEST_SENDS) for n in _DB[coll].index_information()}
    assert {"wa_campaign_recipients_unique", "wa_campaigns_status_sendat"} <= names


def test_index_creation_failure_never_breaks_startup(_env):
    _env["mp"].setattr(M, "db", _BrokenDb(_DB, {M.CAMPAIGNS: {"create_index"}}))
    assert M.ensure_indexes_safe() is False


def test_no_secret_or_full_number_is_ever_returned_stored_or_audited(admin, _env, sender):
    cid = test_ready_for_test(admin, _env)
    tsend(cid)
    dry = tsend(cid, dry_run=True)
    assert dry.json()["to_last4"] == TESTNUM[-4:]
    texts = [client.get(BASE + "/" + cid, headers=H("adm")).text, client.get(BASE + "/" + cid + "/recipients", headers=H("adm")).text,
             client.get(BASE + "/" + cid + "/confirmation", headers=H("adm")).text, dry.text,
             json.dumps(list(_DB.wa_campaign_audit.find()), default=str), json.dumps(list(_DB[M.TEST_SENDS].find()), default=str)]
    for t in texts:
        assert TOKEN not in t and "999888777" not in t and TESTNUM not in t
    for p in (P1, P2):
        assert not any(p in t for t in texts)


def test_database_failures_are_503_without_leaks(admin, _env):
    _env["mp"].setattr(M, "db", _BrokenDb(_DB, {M.CAMPAIGNS: {"find_one", "insert_one", "find", "count_documents"}}))
    for r in (client.post(BASE, headers=H("adm"), json={"name": "abc def"}), client.get(BASE, headers=H("adm")),
              client.get(BASE + "/" + "0" * 24, headers=H("adm"))):
        assert r.status_code == 503 and "9198" not in r.text and "Traceback" not in r.text
