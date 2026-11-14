"""WhatsApp Campaign Manager - Phase 2A foundations: permission module, audit trail, kill switch, test-number
allow-list, admin-recorded consent, centralized eligibility, and opt-out enforcement in the template-test endpoint.
NO network: every Meta call is mocked. Private mongomock DB patched only into the modules under test.
Run:  python -m pytest test_wa_campaigns_foundation.py -q -W ignore"""
import hashlib
import json
from datetime import datetime, timedelta

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

import routers.dashboard_auth as DA
import routers.wa_campaigns as RC
import routers.wa_chat as WCHAT
import routers.wa_template_test as T
import services.wa_campaign_audit as AUD
import services.wa_campaign_settings as ST
import services.wa_consent as C
import services.wa_eligibility as E
import services.whatsapp as WA

_DB = mongomock.MongoClient()["wa_campaigns_foundation_test"]
PHONE = "919876543210"
PHONE2 = "919812345678"
SECRET = "EAAB-secret-token-123"
CAMPAIGN_ENV = ("WA_CAMPAIGN_SENDING_ENABLED", "WA_CAMPAIGN_WORKER_ENABLED", "WA_CAMPAIGN_TEST_NUMBERS")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in _DB.list_collection_names():
        _DB.drop_collection(name)
    for mod in (C, AUD, ST, DA, T, WCHAT):
        monkeypatch.setattr(mod, "db", _DB)
    for v in CAMPAIGN_ENV:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)


app = FastAPI()
app.include_router(RC.router, prefix="/admin")
app.include_router(T.router, prefix="/admin")
client = TestClient(app)


def make_user(token, perms=None, role_name="Custom Role", status="active", full_name="Asha Admin"):
    rid = _DB.dashboard_roles.insert_one({"role_name": role_name, "permissions": perms or {}}).inserted_id
    _DB.dashboard_users.insert_one({"token": token, "role_id": rid, "full_name": full_name, "status": status,
                                    "last_active_at": datetime.utcnow(), "mobile": "9000000001"})
    return token


def grant(*actions):
    return {RC.MODULE: {a: True for a in ["view", "add", "edit", "delete", "approve", "export"] if a in actions}}


def H(token):
    return {"Authorization": "Bearer " + token}


def when(days=1):
    return (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"


def consent_body(**over):
    b = {"phone": PHONE, "source": "signup_form", "evidence_ref": "FORM-2026-000123", "consented_at": when(),
         "attested": True, "note": "Merchant ticked WhatsApp offers on the signup form"}
    b.update(over)
    return b


ADMIN = {"id": "adm1", "name": "Asha Admin", "role": "Marketing Lead"}


def start(phone=PHONE, mid="wamid.S1", ts=None):
    return C.record_opt_in(phone, "START", mid, str(ts or int(datetime.utcnow().timestamp()) - 60), whatsapp_id=True)


def stop(phone=PHONE, mid="wamid.P1", ts=None):
    return C.record_opt_out(phone, "STOP", mid, str(ts or int(datetime.utcnow().timestamp()) - 30), whatsapp_id=True)


def ledger(**q):
    return list(_DB.wa_consent_events.find(q))


# ═════════════════════ permission module ═════════════════════

def test_module_exists_and_is_not_in_any_seeded_non_super_role():
    assert RC.MODULE in DA.ALL_MODULES
    DA.seed_rbac()
    roles = {r["role_name"]: r["permissions"] for r in _DB.dashboard_roles.find({})}
    assert roles["Super Admin"][RC.MODULE] == {a: True for a in DA.ALL_ACTIONS}
    for name, perms in roles.items():
        if name != "Super Admin":
            assert not any(perms.get(RC.MODULE, {}).values()), name       # nobody else gets it automatically


def test_existing_role_documents_without_the_module_are_denied():
    make_user("old", perms={"Live Chat": {"view": True}, "Accounts": {"view": True, "edit": True, "approve": True}})
    for method, path in [("get", "/admin/whatsapp-campaigns/config"), ("get", "/admin/whatsapp-campaigns/audit")]:
        assert getattr(client, method)(path, headers=H("old")).status_code == 403


def test_every_route_requires_authentication():
    for method, path in [("get", "/config"), ("post", "/sending"), ("post", "/consent"), ("post", "/eligibility"),
                         ("get", "/audit")]:
        assert getattr(client, method)("/admin/whatsapp-campaigns" + path).status_code == 401


def test_permission_actions_are_enforced_per_route():
    make_user("viewer", grant("view"))
    make_user("editor", grant("view", "edit"))
    make_user("approver", grant("view", "edit", "approve"))
    base = "/admin/whatsapp-campaigns"
    assert client.get(base + "/config", headers=H("viewer")).status_code == 200
    assert client.get(base + "/audit", headers=H("viewer")).status_code == 200
    assert client.post(base + "/eligibility", json={"phones": [PHONE]}, headers=H("viewer")).status_code == 200
    assert client.post(base + "/consent", json=consent_body(), headers=H("viewer")).status_code == 403
    assert client.post(base + "/consent", json=consent_body(), headers=H("editor")).status_code == 403
    assert client.post(base + "/sending", json={"enabled": False}, headers=H("viewer")).status_code == 403
    assert client.post(base + "/sending", json={"enabled": False}, headers=H("editor")).status_code == 200
    assert client.post(base + "/consent", json=consent_body(), headers=H("approver")).status_code == 200


def test_disabled_or_suspended_admin_is_refused():
    make_user("sus", grant("view", "approve"), status="suspended")
    assert client.get("/admin/whatsapp-campaigns/config", headers=H("sus")).status_code == 403


def test_super_admin_and_legacy_admin_have_access():
    make_user("root", {}, role_name="Super Admin")
    _DB.admins.insert_one({"token": "legacy", "username": "boss"})
    assert client.get("/admin/whatsapp-campaigns/config", headers=H("root")).status_code == 200
    assert client.get("/admin/whatsapp-campaigns/config", headers=H("legacy")).status_code == 200


# ═════════════════════ audit infrastructure ═════════════════════

def test_audit_entry_has_identity_and_masks_phone_like_values():
    AUD.record("x.y", ADMIN, target=AUD.phone_ref(PHONE), details={"note": "call 919876543210 or +91 98765 43210",
                                                                    "nested": {"n": "9876543210"}, "count": 3})
    d = _DB.wa_campaign_audit.find_one({})
    assert d["admin"] == ADMIN and d["action"] == "x.y" and d["outcome"] == "ok"
    blob = json.dumps(d, default=str)
    assert PHONE not in blob and "9876543210" not in blob and d["target"]["phone_last4"] == "3210"
    assert d["target"]["phone_hash"] == hashlib.sha256(PHONE.encode()).hexdigest()
    assert d["details"]["count"] == 3


def test_audit_is_append_only_by_construction():
    public = {n for n in dir(AUD) if not n.startswith("_")}
    assert not any(w in n.lower() for n in public for w in ("delete", "update", "remove", "drop", "purge"))
    src = open(AUD.__file__, encoding="utf-8").read()
    assert "update_one" not in src and "delete_" not in src and ".drop(" not in src


def test_audit_strict_raises_best_effort_does_not(monkeypatch):
    class Boom:
        def __getitem__(self, n): raise RuntimeError("db down with secret 919876543210")
    monkeypatch.setattr(AUD, "db", Boom())
    assert AUD.record("a", ADMIN) is False
    with pytest.raises(AUD.AuditWriteError):
        AUD.record("a", ADMIN, strict=True)


def test_audit_unacknowledged_write_is_a_failure(monkeypatch):
    class Coll:
        def insert_one(self, d):
            class R: acknowledged = False
            return R()
    class Db:
        def __getitem__(self, n): return Coll()
    monkeypatch.setattr(AUD, "db", Db())
    with pytest.raises(AUD.AuditWriteError):
        AUD.record("a", ADMIN, strict=True)


def test_audit_indexes_idempotent_and_safe_startup(monkeypatch, caplog, capsys):
    assert AUD.ensure_indexes_safe() and AUD.ensure_indexes_safe()
    assert {"wa_campaign_audit_at", "wa_campaign_audit_action_at", "wa_campaign_audit_campaign_at"} <= \
        set(_DB.wa_campaign_audit.index_information())

    def boom():
        raise RuntimeError("mongodb://u:PASSWORD123@h 919876543210")
    monkeypatch.setattr(AUD, "ensure_indexes", boom)
    assert AUD.ensure_indexes_safe() is False
    assert "PASSWORD123" not in caplog.text + capsys.readouterr().out and "RuntimeError at" in caplog.text


def test_audit_listing_paging_and_filter():
    for i in range(5):
        AUD.record("a%d" % (i % 2), ADMIN)
    first = AUD.list_entries(limit=2)
    assert len(first) == 2
    rest = AUD.list_entries(limit=10, before=first[-1]["_id"])
    assert len(rest) == 3 and not {e["_id"] for e in first} & {e["_id"] for e in rest}
    assert all(e["action"] == "a0" for e in AUD.list_entries(action="a0"))


def test_admin_ref_comes_from_the_session_user():
    ref = AUD.admin_ref({"_id": "u1", "full_name": "Asha", "role": {"role_name": "Super Admin"}})
    assert ref == {"id": "u1", "name": "Asha", "role": "Super Admin"}


def test_audit_endpoint_returns_masked_entries():
    make_user("v", grant("view"))
    AUD.record("consent.admin_record", ADMIN, details={"ref": "919876543210"})
    r = client.get("/admin/whatsapp-campaigns/audit", headers=H("v"))
    assert r.status_code == 200 and PHONE not in r.text and r.json()["entries"][0]["action"] == "consent.admin_record"


# ═════════════════════ kill switch + safety settings ═════════════════════

def test_sending_is_off_by_default():
    assert ST.sending_allowed() == (False, "env_disabled")
    assert ST.worker_enabled() is False and ST.test_numbers() == frozenset()


@pytest.mark.parametrize("env,switch,expect", [
    (False, False, (False, "env_disabled")), (False, True, (False, "env_disabled")),
    (True, False, (False, "switch_off")), (True, True, (True, "ok"))])
def test_sending_needs_both_keys(monkeypatch, env, switch, expect):
    if env:
        monkeypatch.setenv("WA_CAMPAIGN_SENDING_ENABLED", "true")
    if switch:
        _DB.wa_campaign_settings.insert_one({"_id": "global", "sending_enabled": True})
    assert ST.sending_allowed() == expect


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", " ", "enabled?"])
def test_non_truthy_env_values_keep_sending_off(monkeypatch, val):
    monkeypatch.setenv("WA_CAMPAIGN_SENDING_ENABLED", val)
    assert ST.env_sending_enabled() is False


def test_unreadable_settings_fail_closed(monkeypatch):
    monkeypatch.setenv("WA_CAMPAIGN_SENDING_ENABLED", "1")
    class Boom:
        def __getitem__(self, n): raise RuntimeError("down")
    monkeypatch.setattr(ST, "db", Boom())
    assert ST.sending_allowed() == (False, "settings_unavailable")


def test_enable_requires_env_key_approve_and_is_audited(monkeypatch):
    make_user("approver", grant("view", "edit", "approve"))
    make_user("editor", grant("view", "edit"))
    url = "/admin/whatsapp-campaigns/sending"
    r = client.post(url, json={"enabled": True}, headers=H("approver"))
    assert r.status_code == 409 and ST.switch_on() is False                # env key off
    monkeypatch.setenv("WA_CAMPAIGN_SENDING_ENABLED", "1")
    assert client.post(url, json={"enabled": True}, headers=H("editor")).status_code == 403    # needs approve
    r = client.post(url, json={"enabled": True}, headers=H("approver"))
    assert r.status_code == 200 and r.json()["sending_allowed"] is True and ST.switch_on() is True
    acts = [(d["action"], d["outcome"]) for d in _DB.wa_campaign_audit.find({})]
    assert ("sending.enable", "attempt") in acts and ("sending.enable", "refused_env_disabled") in acts


def test_enabling_is_refused_if_the_audit_entry_cannot_be_written(monkeypatch):
    monkeypatch.setenv("WA_CAMPAIGN_SENDING_ENABLED", "1")
    make_user("approver", grant("view", "edit", "approve"))
    class Boom:
        def __getitem__(self, n): raise RuntimeError("down")
    monkeypatch.setattr(AUD, "db", Boom())
    r = client.post("/admin/whatsapp-campaigns/sending", json={"enabled": True}, headers=H("approver"))
    assert r.status_code == 503 and ST.switch_on() is False


def test_kill_switch_off_works_even_if_audit_is_down(monkeypatch):
    monkeypatch.setenv("WA_CAMPAIGN_SENDING_ENABLED", "1")
    _DB.wa_campaign_settings.insert_one({"_id": "global", "sending_enabled": True})
    make_user("editor", grant("view", "edit"))
    class Boom:
        def __getitem__(self, n): raise RuntimeError("down")
    monkeypatch.setattr(AUD, "db", Boom())
    r = client.post("/admin/whatsapp-campaigns/sending", json={"enabled": False}, headers=H("editor"))
    assert r.status_code == 200 and ST.switch_on() is False and ST.sending_allowed() == (False, "switch_off")


@pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}, {"enabled": None}])
def test_sending_endpoint_validates_body(body):
    make_user("approver", grant("view", "edit", "approve"))
    assert client.post("/admin/whatsapp-campaigns/sending", json=body, headers=H("approver")).status_code == 400


def test_test_number_allow_list(monkeypatch):
    assert ST.is_test_number(PHONE) is False                                # unset -> nobody
    monkeypatch.setenv("WA_CAMPAIGN_TEST_NUMBERS", "+91 98765 43210; 9812345678,junk, +65 8123 4567")
    assert ST.test_numbers() == frozenset({"919876543210", "919812345678", "6581234567"})
    assert ST.is_test_number("9876543210") and ST.is_test_number("+919876543210") and ST.is_test_number("+6581234567")
    assert not ST.is_test_number("919000000001") and not ST.is_test_number("") and not ST.is_test_number(None)


def test_config_snapshot_has_no_secrets_and_identifies_the_account_safely(monkeypatch):
    monkeypatch.setattr(WA, "WHATSAPP_ACCESS_TOKEN", SECRET)
    monkeypatch.setattr(WA, "WHATSAPP_PHONE_NUMBER_ID", "123456789012345")
    monkeypatch.setattr(WA, "WHATSAPP_BUSINESS_ACCOUNT_ID", "998877665544332")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "appsecretvalue")
    monkeypatch.setenv("WA_CAMPAIGN_TEST_NUMBERS", PHONE)
    make_user("v", grant("view"))
    r = client.get("/admin/whatsapp-campaigns/config", headers=H("v"))
    body = r.text
    assert r.status_code == 200
    for leak in (SECRET, "appsecretvalue", "123456789012345", "998877665544332", PHONE):
        assert leak not in body
    j = r.json()
    assert j["whatsapp"]["phone_number_id_last4"] == "***2345" and j["whatsapp"]["business_account_id_last4"] == "***4332"
    assert j["whatsapp"]["access_token_configured"] is True and j["test_numbers_configured"] == 1
    assert j["sending"] == {"allowed": False, "reason": "env_disabled", "env_key_on": False, "switch_on": False}
    assert j["webhook"]["app_secret_configured"] is True and j["worker_env_enabled"] is False


# ═════════════════════ admin-recorded consent ═════════════════════

def rec(**over):
    a = dict(phone=PHONE, source="signup_form", evidence_ref="FORM-2026-000123", consented_at=when(), admin=ADMIN,
             attested=True, note="")
    a.update(over)
    return C.record_admin_consent(**a)


def test_admin_consent_creates_opted_in_contact_with_full_evidence():
    res = rec()
    assert res["result"] == "recorded" and res["phone_last4"] == "3210"
    c = _DB.wa_contacts.find_one({"_id": PHONE})
    assert c["status"] == "opted_in" and c["consent_seq"] == 0
    lc = c["last_consent"]
    assert lc["source"] == "admin_recorded" and lc["consent_source"] == "signup_form" and lc["attested"] is True
    assert lc["evidence_ref"] == "FORM-2026-000123" and lc["admin_id"] == "adm1" and lc["admin_name"] == "Asha Admin"
    assert lc["consented_at"] and lc["recorded_at"]
    ev = ledger(event="admin_opt_in")
    assert len(ev) == 1 and ev[0]["phone"] == PHONE and ev[0]["admin_id"] == "adm1" and ev[0]["evidence_ref"] == lc["evidence_ref"]


@pytest.mark.parametrize("over,code", [
    ({"phone": "12345"}, "invalid_phone"), ({"phone": ""}, "invalid_phone"), ({"phone": None}, "invalid_phone"),
    ({"source": "telepathy"}, "invalid_source"), ({"source": ""}, "invalid_source"), ({"source": None}, "invalid_source"),
    ({"evidence_ref": ""}, "evidence_required"), ({"evidence_ref": "abc"}, "evidence_required"), ({"evidence_ref": None}, "evidence_required"),
    ({"consented_at": ""}, "invalid_timestamp"), ({"consented_at": "yesterday"}, "invalid_timestamp"),
    ({"consented_at": None}, "invalid_timestamp"), ({"consented_at": "1999-01-01T00:00:00Z"}, "invalid_timestamp"),
    ({"attested": False}, "attestation_required"), ({"attested": "true"}, "attestation_required"), ({"attested": None}, "attestation_required"),
    ({"admin": {}}, "admin_required"), ({"admin": {"id": " "}}, "admin_required"), ({"admin": None}, "admin_required"),
])
def test_admin_consent_requires_every_piece_of_evidence(over, code):
    with pytest.raises(C.AdminConsentError) as e:
        rec(**over)
    assert e.value.code == code
    assert _DB.wa_contacts.count_documents({}) == 0 and ledger() == []        # nothing written


def test_future_consent_time_is_rejected_but_tolerance_is_allowed():
    with pytest.raises(C.AdminConsentError):
        rec(consented_at=(datetime.utcnow() + timedelta(hours=2)).isoformat())
    assert rec(consented_at=(datetime.utcnow() + timedelta(minutes=2)).isoformat())["result"] == "recorded"


def test_consent_time_with_offset_is_converted_to_utc():
    rec(consented_at="2026-10-01T10:00:00+05:30")
    assert _DB.wa_contacts.find_one({"_id": PHONE})["last_consent"]["consented_at"].startswith("2026-10-01T04:30")


@pytest.mark.parametrize("variant", ["919876543210", "9876543210", "+91 98765 43210", "0919876543210"[1:]])
def test_admin_consent_never_overrides_an_opt_out_under_any_number_format(variant):
    stop()
    before, n = _DB.wa_contacts.find_one({"_id": PHONE}), len(ledger())
    with pytest.raises(C.AdminConsentError) as e:
        rec(phone=variant)
    assert e.value.code == "opted_out"
    assert _DB.wa_contacts.find_one({"_id": PHONE}) == before and len(ledger()) == n
    assert C.consent_status(PHONE) == "opted_out"


def test_opt_out_that_wins_a_race_is_never_overwritten(monkeypatch):
    real = _DB[C.CONTACTS]

    class Racing:
        def __getattr__(self, n): return getattr(real, n)
        def update_one(self, *a, **k):                       # a STOP lands between the check and the write
            real.insert_one({"_id": PHONE, "phone": PHONE, "status": "opted_out", "consent_seq": 9})
            raise DuplicateKeyError("E11000")

    class Db:
        def __getitem__(self, n): return Racing() if n == C.CONTACTS else _DB[n]
    monkeypatch.setattr(C, "db", Db())
    with pytest.raises(C.AdminConsentError) as e:
        rec()
    assert e.value.code == "opted_out" and _DB.wa_contacts.find_one({"_id": PHONE})["status"] == "opted_out"


def test_only_the_customers_own_start_restores_consent_after_stop():
    stop()
    with pytest.raises(C.AdminConsentError):
        rec()
    start(mid="wamid.S9", ts=int(datetime.utcnow().timestamp()))
    assert C.consent_status(PHONE) == "opted_in"
    assert _DB.wa_contacts.find_one({"_id": PHONE})["last_consent"]["source"] == "whatsapp_keyword"


def test_a_customer_stop_after_admin_consent_always_applies_even_with_an_older_timestamp():
    rec()
    r = C.record_opt_out(PHONE, "STOP", "wamid.LATE", "1000000000", whatsapp_id=True)   # very old Meta timestamp
    assert r == "applied" and C.consent_status(PHONE) == "opted_out"


def test_customer_start_after_admin_consent_replaces_the_evidence():
    rec()
    start()
    assert _DB.wa_contacts.find_one({"_id": PHONE})["last_consent"]["source"] == "whatsapp_keyword"


def test_existing_opt_in_is_left_untouched_and_only_evidence_is_appended():
    start()
    before = _DB.wa_contacts.find_one({"_id": PHONE})
    res = rec()
    assert res["result"] == "already_opted_in" and _DB.wa_contacts.find_one({"_id": PHONE}) == before
    assert len(ledger(event="admin_opt_in")) == 1


def test_recording_the_same_evidence_twice_is_idempotent():
    fixed = when()
    a, b = rec(consented_at=fixed), rec(consented_at=fixed)
    assert a["event_id"] == b["event_id"] and len(ledger(event="admin_opt_in")) == 1
    assert _DB.wa_contacts.count_documents({}) == 1


def test_different_evidence_is_a_separate_ledger_row():
    rec()
    rec(evidence_ref="FORM-2026-000999")
    assert len(ledger(event="admin_opt_in")) == 2


def test_ledger_failure_leaves_no_consent(monkeypatch):
    from test_wa_webhook_consent import _BrokenDb
    monkeypatch.setattr(C, "db", _BrokenDb(_DB, {C.EVENTS: {"insert_one"}}))
    with pytest.raises(Exception):
        rec()
    assert _DB.wa_contacts.count_documents({}) == 0


def test_contact_write_failure_never_reports_success(monkeypatch):
    from test_wa_webhook_consent import _BrokenDb
    monkeypatch.setattr(C, "db", _BrokenDb(_DB, {C.CONTACTS: {"update_one"}}))
    with pytest.raises(Exception):
        rec()
    assert E.check(PHONE).eligible is False


def test_unacknowledged_contact_write_is_an_error(monkeypatch):
    from test_wa_webhook_consent import _UnackDb
    monkeypatch.setattr(C, "db", _UnackDb(_DB, {C.CONTACTS: {"update_one"}}))
    with pytest.raises(C.ConsentWriteError):
        rec()


def test_consent_lookup_failure_blocks_admin_consent_fail_closed(monkeypatch):
    class Boom:
        def __getitem__(self, n): raise RuntimeError("down")
    monkeypatch.setattr(C, "db", Boom())
    with pytest.raises(RuntimeError):
        rec()


def test_non_indian_international_number_is_supported():
    assert rec(phone="+65 8123 4567")["phone_last4"] == "4567"
    assert _DB.wa_contacts.find_one({"_id": "6581234567"})["status"] == "opted_in"


# ═════════════════════ consent endpoint ═════════════════════

def test_consent_endpoint_records_with_identity_from_the_session_not_the_body():
    make_user("approver", grant("approve"), full_name="Real Admin")
    body = consent_body(admin={"id": "forged"}, admin_id="forged", admin_name="Forged Name")
    r = client.post("/admin/whatsapp-campaigns/consent", json=body, headers=H("approver"))
    assert r.status_code == 200 and r.json() == {"ok": True, "result": "recorded", "phone_last4": "3210"}
    lc = _DB.wa_contacts.find_one({"_id": PHONE})["last_consent"]
    assert lc["admin_name"] == "Real Admin" and lc["admin_id"] != "forged" and lc["admin_role"] == "Custom Role"
    assert PHONE not in r.text


def test_consent_endpoint_audits_attempt_before_and_outcome_after_without_phone():
    make_user("approver", grant("approve"))
    client.post("/admin/whatsapp-campaigns/consent", json=consent_body(), headers=H("approver"))
    entries = list(_DB.wa_campaign_audit.find({"action": "consent.admin_record"}).sort("_id", 1))
    assert [e["outcome"] for e in entries] == ["attempt", "recorded"]
    assert entries[0]["target"]["phone_last4"] == "3210" and PHONE not in json.dumps(entries, default=str)
    assert entries[0]["admin"]["name"] == "Asha Admin"


def test_consent_endpoint_is_refused_if_audit_cannot_be_written(monkeypatch):
    make_user("approver", grant("approve"))
    class Boom:
        def __getitem__(self, n): raise RuntimeError("down")
    monkeypatch.setattr(AUD, "db", Boom())
    r = client.post("/admin/whatsapp-campaigns/consent", json=consent_body(), headers=H("approver"))
    assert r.status_code == 503 and _DB.wa_contacts.count_documents({}) == 0 and ledger() == []


@pytest.mark.parametrize("over,status", [
    ({"phone": "bad"}, 400), ({"source": "x"}, 400), ({"evidence_ref": ""}, 400), ({"consented_at": "x"}, 400),
    ({"attested": False}, 400), ({"attested": None}, 400)])
def test_consent_endpoint_validation_errors(over, status):
    make_user("approver", grant("approve"))
    r = client.post("/admin/whatsapp-campaigns/consent", json=consent_body(**over), headers=H("approver"))
    assert r.status_code == status and _DB.wa_contacts.count_documents({}) == 0
    assert any(e["outcome"].startswith("refused:") for e in _DB.wa_campaign_audit.find({}))


def test_consent_endpoint_conflicts_with_an_opt_out_with_409():
    make_user("approver", grant("approve"))
    stop()
    r = client.post("/admin/whatsapp-campaigns/consent", json=consent_body(), headers=H("approver"))
    assert r.status_code == 409 and "cannot override" in r.json()["detail"]
    assert C.consent_status(PHONE) == "opted_out"


def test_consent_endpoint_database_failure_is_503_and_not_consented(monkeypatch):
    from test_wa_webhook_consent import _BrokenDb
    make_user("approver", grant("approve"))
    monkeypatch.setattr(C, "db", _BrokenDb(_DB, {C.CONTACTS: {"update_one"}}))
    r = client.post("/admin/whatsapp-campaigns/consent", json=consent_body(), headers=H("approver"))
    assert r.status_code == 503 and _DB.wa_contacts.count_documents({}) == 0


@pytest.mark.parametrize("raw", [b"[]", b"not json", b'"x"'])
def test_endpoints_reject_non_object_json(raw):
    make_user("approver", grant("view", "edit", "approve"))
    for path in ("consent", "eligibility", "sending"):
        r = client.post("/admin/whatsapp-campaigns/" + path, content=raw, headers={**H("approver"), "content-type": "application/json"})
        assert r.status_code == 400


# ═════════════════════ centralized eligibility ═════════════════════

def test_eligibility_reasons():
    assert E.check("nope").reason == E.INVALID_NUMBER and E.check("nope").eligible is False
    assert E.check(PHONE).reason == E.UNKNOWN_CONSENT
    start(PHONE2)
    r = E.check(PHONE2)
    assert r.eligible and r.reason == E.ELIGIBLE and r.basis == E.BASIS_CUSTOMER_START and r.phone_last4 == "5678"
    rec()
    r = E.check(PHONE)
    assert r.eligible and r.basis == E.BASIS_ADMIN_RECORDED
    stop(PHONE)
    assert E.check(PHONE).reason == E.OPTED_OUT and E.check(PHONE).eligible is False


@pytest.mark.parametrize("variant", ["9876543210", "+91 98765 43210", "919876543210"])
def test_opt_out_wins_under_any_number_format(variant):
    start()
    stop()
    assert E.check(variant).eligible is False and E.check(variant).reason == E.OPTED_OUT


def test_opt_out_stored_under_a_variant_key_wins():
    # A Singapore number that opted out through the webhook is stored as '6581234567'. Typed WITHOUT a '+', that
    # looks like a bare Indian mobile ('916581234567'); both keys are checked and the opt-out wins.
    C.record_opt_out("6581234567", "STOP", "wamid.SG", "1762843200", whatsapp_id=True)
    _DB.wa_contacts.insert_one({"_id": "916581234567", "status": "opted_in", "last_consent": {
        "source": "whatsapp_keyword", "message_id": "wamid.X"}})
    assert E.check("6581234567").reason == E.OPTED_OUT
    assert E.check("+65 8123 4567").reason == E.OPTED_OUT


@pytest.mark.parametrize("last_consent", [
    None, {}, "text", {"source": "whatsapp_keyword", "message_id": ""}, {"source": "unknown", "message_id": "m"},
    {"source": "admin_recorded"}, {"source": "admin_recorded", "consent_source": "email", "evidence_ref": "REF-1234",
                                   "consented_at": "2026-01-01", "admin_id": "a", "attested": False},
    {"source": "admin_recorded", "consent_source": "email", "evidence_ref": "", "consented_at": "2026-01-01",
     "admin_id": "a", "attested": True}])
def test_opted_in_without_complete_evidence_is_not_eligible(last_consent):
    doc = {"_id": PHONE, "status": "opted_in"}
    if last_consent is not None:
        doc["last_consent"] = last_consent
    _DB.wa_contacts.insert_one(doc)
    r = E.check(PHONE)
    assert r.eligible is False and r.reason == E.EVIDENCE_MISSING


def test_database_failure_is_never_eligible(monkeypatch):
    start()
    class Boom:
        def __getitem__(self, n): raise RuntimeError("down")
    monkeypatch.setattr(C, "db", Boom())
    r = E.check(PHONE)
    assert r.eligible is False and r.reason == E.LOOKUP_FAILED
    assert E.check("nope").reason == E.INVALID_NUMBER


def test_bulk_matches_single_checks_keeps_order_and_duplicates():
    start(PHONE2)
    stop("919000000001", mid="wamid.Z")
    inputs = [PHONE2, "junk", PHONE, "9000000001", PHONE2, "+91 98123 45678"]
    bulk = E.check_many(inputs)
    assert [b.reason for b in bulk] == [E.check(i).reason for i in inputs]
    assert [b.eligible for b in bulk] == [True, False, False, False, True, True]


def test_bulk_handles_more_than_one_lookup_batch(monkeypatch):
    monkeypatch.setattr(E, "_BATCH", 3)
    numbers = ["9198%08d" % i for i in range(7)]
    for n in numbers[:4]:
        C.record_opt_in(n, "START", "wamid." + n, "1762843200", whatsapp_id=True)
    assert [r.eligible for r in E.check_many(numbers)] == [True] * 4 + [False] * 3


def test_eligibility_endpoint_masks_numbers_and_summarises():
    make_user("v", grant("view"))
    start(PHONE2)
    r = client.post("/admin/whatsapp-campaigns/eligibility", json={"phones": [PHONE2, PHONE, "bad"]}, headers=H("v"))
    j = r.json()
    assert r.status_code == 200 and PHONE not in r.text and PHONE2 not in r.text
    assert [x["eligible"] for x in j["results"]] == [True, False, False]
    assert j["results"][0]["basis"] == "customer_start" and j["results"][0]["phone_last4"] == "5678"
    assert j["summary"] == {"eligible": 1, "unknown_consent": 1, "invalid_number": 1}


@pytest.mark.parametrize("phones", [[], "x", [1], ["a"] * 201, None])
def test_eligibility_endpoint_limits(phones):
    make_user("v", grant("view"))
    assert client.post("/admin/whatsapp-campaigns/eligibility", json={"phones": phones}, headers=H("v")).status_code == 400


# ═════════════════════ template-test endpoint: opt-out enforcement ═════════════════════

TPL = {"data": [{"name": T.TEMPLATE_NAME, "language": "en", "status": "APPROVED", "category": "MARKETING", "components": [
    {"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "Join OffrO"}]}]}
TT_GOOD = {"phone": "+91 98765 43210", "consent_confirmed": True, "consent_note": "Opted in via signup form 2026-10-01",
           "header_media_id": "1234567890"}


@pytest.fixture
def tt(monkeypatch):
    calls = {"get": [], "post": []}

    class R:
        def __init__(self, code, js): self.status_code, self._js = code, js
        def json(self): return self._js
    _DB.admins.insert_one({"token": "tok"})
    monkeypatch.setenv("WHATSAPP_TEMPLATE_TEST_ENABLED", "1")
    monkeypatch.setattr(WA, "WHATSAPP_ACCESS_TOKEN", SECRET)
    monkeypatch.setattr(WA, "WHATSAPP_PHONE_NUMBER_ID", "111")
    monkeypatch.setattr(WA, "WHATSAPP_BUSINESS_ACCOUNT_ID", "222")
    monkeypatch.setattr(T.requests, "get", lambda url, **kw: (calls["get"].append(url), R(200, TPL))[1])
    monkeypatch.setattr(WA.requests, "post", lambda url, **kw: (calls["post"].append(url), R(200, {"messages": [{"id": "wamid.X"}]}))[1])
    return calls


def tt_post(body=TT_GOOD):
    return client.post("/admin/whatsapp/template-test", json=body, headers={"X-Admin-Token": "tok"})


def test_template_test_blocks_an_opted_out_number_before_any_network_call(tt):
    stop()
    r = tt_post()
    assert r.status_code == 409 and "opted out" in r.json()["detail"]
    assert tt == {"get": [], "post": []}
    assert _DB.whatsapp_template_tests.count_documents({}) == 0               # consumed no send quota
    a = _DB.wa_campaign_audit.find_one({"action": "template_test.blocked_opted_out"})
    assert a["outcome"] == "blocked" and a["target"]["phone_last4"] == "3210" and PHONE not in json.dumps(a, default=str)


@pytest.mark.parametrize("phone", ["9876543210", "919876543210", "+91 98765 43210", "+919876543210"])
def test_template_test_opt_out_cannot_be_bypassed_by_number_format(tt, phone):
    stop()
    assert tt_post({**TT_GOOD, "phone": phone}).status_code in (409, 400) and tt["post"] == []


def test_template_test_dry_run_is_also_blocked_for_opted_out(tt):
    stop()
    assert tt_post({**TT_GOOD, "dry_run": True}).status_code == 409 and tt["get"] == []


def test_template_test_fails_closed_when_the_consent_store_is_unreadable(tt, monkeypatch):
    class Boom:
        def __getitem__(self, n): raise RuntimeError("down")
    monkeypatch.setattr(C, "db", Boom())
    r = tt_post()
    assert r.status_code == 503 and tt == {"get": [], "post": []}


def test_template_test_unchanged_for_opted_in_and_unknown_consent(tt):
    assert tt_post().status_code == 200 and len(tt["post"]) == 1               # unknown: admin attestation (existing rule)
    start()
    _DB.whatsapp_template_tests.delete_many({})
    assert tt_post({**TT_GOOD, "phone": PHONE}).status_code == 200


def test_template_test_after_customer_start_following_stop_is_allowed(tt):
    stop(ts=int(datetime.utcnow().timestamp()) - 120)
    assert tt_post().status_code == 409
    start(mid="wamid.S77", ts=int(datetime.utcnow().timestamp()))
    assert tt_post().status_code == 200


# ═════════════════════ wiring / no sending ═════════════════════

def test_phase_2a_modules_import_no_sender_and_call_none():
    for mod in (ST, AUD, E, RC):
        src = open(mod.__file__, encoding="utf-8").read()
        for banned in ("send_whatsapp_message", "send_whatsapp_template", "send_whatsapp_image", "requests.post"):
            assert banned not in src, (mod.__name__, banned)


def test_server_registers_the_router_and_startup_index_creation():
    import server
    paths = {getattr(r, "path", "") for r in server.app.routes}
    assert "/admin/whatsapp-campaigns/consent" in paths and "/admin/whatsapp-campaigns/config" in paths
    mine = {p for p in paths if p.startswith("/admin/whatsapp-campaigns")}
    assert mine == {"/admin/whatsapp-campaigns/" + x for x in ("config", "sending", "consent", "eligibility", "audit")}
    src = open("server.py", encoding="utf-8").read()
    assert "_wa_camp_audit.ensure_indexes_safe()" in src and "WhatsApp campaign audit indexes skipped ({type(e).__name__})" in src


def test_phase_1_routes_and_existing_whatsapp_routes_still_present():
    import server
    paths = {getattr(r, "path", "") for r in server.app.routes}
    assert {"/webhook", "/admin/whatsapp/template-test", "/gift-vouchers", "/home-data"} <= paths
