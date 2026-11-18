"""Registration checkbox = Terms + Privacy + explicit WhatsApp promotional-message consent.
Run:  python -m pytest test_registration_whatsapp_consent.py -q -W ignore"""
import sys
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import server
import services.wa_consent as C
import services.wa_eligibility as E

fake = sys.modules["database"].db
c = TestClient(server.app)
A = {"Authorization": "Bearer at"}
VER = C.REGISTRATION_CONSENT_VERSION
TEXT = "I agree to the Terms & Conditions and Privacy Policy and consent to receive updates and promotional messages from OffrO via WhatsApp."
PHONE, CANON = "9876543210", "919876543210"


@pytest.fixture(autouse=True)
def _same_database(monkeypatch):
    """Other test modules may have bound services.wa_consent to a different fake; the suite-wide shared fake is the one
    the routers use, so the consent service must write to it too (restored automatically after each test)."""
    monkeypatch.setattr(C, "db", fake)


@pytest.fixture(autouse=True)
def _admin():
    role = fake.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "at", "status": "active", "full_name": "T", "mobile": "9000000001", "role_id": role, "assigned_cities": ["*"]})


def reg(phone=PHONE, **kw):
    body = {"name": "Asha", "phone": phone, "city": "", "consent": True, "whatsapp_consent": True, "consent_version": VER}
    body.update(kw)
    for k in [k for k, v in body.items() if v == "__drop__"]:
        del body[k]
    return c.post("/user/register", json=body)


def login(phone=PHONE):
    r = c.post("/user/account-login", json={"phone": phone})
    assert r.status_code == 200, r.text
    return r


def contact(canon=CANON):
    return fake[C.CONTACTS].find_one({"_id": canon})


def listing(**q):
    r = c.get("/admin/whatsapp-campaigns/consents", params=q, headers=A)
    assert r.status_code == 200, r.text
    return r.json()


def test_wording_constant_is_the_exact_checkbox_sentence():
    assert C.REGISTRATION_CONSENT_TEXT == TEXT and C.REGISTRATION_CONSENT_VERSIONS == {VER: TEXT}


# ── Scenario A: checkbox selected ───────────────────────────────────────────
def test_register_records_terms_and_holds_whatsapp_consent_pending_until_first_login():
    assert reg().status_code == 200
    a = fake.accounts.find_one({"name": "Asha"})
    assert a["consent_accepted"] is True and a["consent_accepted_at"] and a["terms_accepted_at"]
    assert a["wa_consent_pending"]["version"] == VER and a["wa_consent_pending"]["text"] == TEXT
    assert contact() is None                                 # not opted in for an unverified number
    assert "wa_consent_pending" not in fake.users.find_one({"name": "Asha"})


def test_first_login_writes_consent_with_full_evidence_and_it_appears_in_the_dashboard_list():
    reg()
    login()
    k = contact()
    assert k["status"] == "opted_in" and k["phone"] == CANON and k["phone_last4"] == "3210" and k["consent_seq"] == 0
    lc = k["last_consent"]
    assert lc["source"] == "registration_checkbox" and lc["consent_source"] == "customer_registration"
    assert lc["consent_text"] == TEXT and lc["consent_version"] == VER and lc["consented_at"] and lc["recorded_at"] and lc["account_id"]
    a = fake.accounts.find_one({"name": "Asha"})
    assert "wa_consent_pending" not in a and a["whatsapp_consent_version"] == VER and a["whatsapp_consent_result"] == "recorded"
    d = listing(last4="3210")
    row = d["items"][0]
    assert d["total"] == 1 and row["phone_masked"] == "***3210" and row["status"] == "opted_in"
    assert row["last_event"]["channel"] == "registration_checkbox" and row["last_event"]["consent_version"] == VER
    assert "9876543210" not in str(d) and "account_id" not in str(d)
    ev = c.get("/admin/whatsapp-campaigns/consent-events", params={"event": "registration_opt_in"}, headers=A).json()
    assert ev["total"] == 1 and ev["items"][0]["event"] == "registration_opt_in"


def test_campaign_eligibility_recognises_the_registration_consent():
    reg(); login()
    r = E.check(PHONE)
    assert r.eligible and r.basis == E.BASIS_CUSTOMER_REGISTRATION and r.canonical == CANON
    assert E.check("9000000099").eligible is False         # someone who never consented


def test_eligibility_rejects_tampered_or_incomplete_registration_evidence():
    reg(); login()
    for field, bad in (("consent_text", "I agree to everything"), ("consent_version", "other"), ("consent_source", "x"),
                       ("account_id", ""), ("consented_at", ""), ("source", "unknown")):
        fake[C.CONTACTS].update_one({"_id": CANON}, {"$set": {"last_consent." + field: bad}})
        assert E.check(PHONE).eligible is False, field
        fake[C.CONTACTS].update_one({"_id": CANON}, {"$set": {"last_consent": {**contact()["last_consent"], field: {"consent_text": TEXT, "consent_version": VER, "consent_source": "customer_registration", "account_id": "a", "consented_at": "t", "source": "registration_checkbox"}[field]}}})
    assert E.check(PHONE).eligible is True


# ── Scenario B: checkbox not selected ───────────────────────────────────────
@pytest.mark.parametrize("kw", [
    {"whatsapp_consent": False}, {"whatsapp_consent": "true"}, {"whatsapp_consent": 1}, {"whatsapp_consent": None},
    {"consent": False}, {"consent": "__drop__"}, {"consent": "true"},
])
def test_unticked_or_malformed_consent_cannot_register(kw):
    r = reg(**kw)
    assert r.status_code == 400 and "accept" in r.json()["detail"].lower()
    assert fake.accounts.count_documents({}) == 0 and fake.users.count_documents({}) == 0 and fake[C.CONTACTS].count_documents({}) == 0


@pytest.mark.parametrize("ver", ["", "bogus", None, 7, "reg-wa-1999"])
def test_unknown_wording_version_is_refused(ver):
    r = reg(consent_version=ver)
    assert r.status_code == 400 and "update" in r.json()["detail"].lower()
    assert fake.accounts.count_documents({}) == 0


def test_client_supplied_wording_is_ignored_server_keeps_its_own_copy():
    reg(consent_text="I agree to anything"); login()
    assert contact()["last_consent"]["consent_text"] == TEXT


# ── compatibility: older builds, V2 builds, existing customers ──────────────
def test_older_builds_and_v2_builds_register_without_any_whatsapp_consent():
    assert c.post("/user/register", json={"name": "Old", "phone": "9111111111", "city": ""}).status_code == 200            # pre-V2
    assert c.post("/user/register", json={"name": "V2", "phone": "9222222222", "city": "", "consent": True}).status_code == 200  # V2: Terms/Privacy only
    for p in ("9111111111", "9222222222"):
        login(p)
    assert fake[C.CONTACTS].count_documents({}) == 0       # consent_accepted alone never means WhatsApp marketing consent
    assert fake.accounts.find_one({"name": "V2"})["consent_accepted"] is True
    assert E.check("9222222222").eligible is False


def test_existing_customer_login_is_unchanged_and_creates_no_consent():
    fake.accounts.insert_one({"name": "Old", "phone": "+919333333333", "roles": ["user"], "status": "active", "created_at": "2025-01-01T00:00:00"})
    r = login("9333333333")
    assert r.json()["name"] == "Old" and fake[C.CONTACTS].count_documents({}) == 0


def test_duplicate_registration_still_blocked_and_no_second_consent_record():
    assert reg().status_code == 200
    assert reg().status_code == 400
    login(); login(); login()                                 # repeated logins never duplicate anything
    assert fake[C.CONTACTS].count_documents({}) == 1 and fake[C.EVENTS].count_documents({"event": "registration_opt_in"}) == 1


@pytest.mark.parametrize("raw", ["9876543210", "+919876543210", "919876543210", "09876543210", "+91 98765-43210"])
def test_phone_formats_all_normalise_to_one_consent_key(raw):
    assert reg(raw).status_code == 200
    login(raw)
    assert [d["_id"] for d in fake[C.CONTACTS].find()] == [CANON]
    assert E.check(raw).eligible and E.check("98765 43210").eligible


def test_login_never_fails_because_the_consent_write_fails_and_is_retried_next_login(monkeypatch):
    reg()
    real = C.record_registration_consent
    monkeypatch.setattr(C, "record_registration_consent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert login().json()["token"]
    assert contact() is None and "wa_consent_pending" in fake.accounts.find_one({"name": "Asha"})     # nothing claimed as saved
    monkeypatch.setattr(C, "record_registration_consent", real)
    login()
    assert contact()["status"] == "opted_in" and "wa_consent_pending" not in fake.accounts.find_one({"name": "Asha"})


# ── withdrawal / opt-out protection ─────────────────────────────────────────
def test_stop_withdraws_registration_consent_and_start_restores_it():
    reg(); login()
    C.record_opt_out(PHONE, "STOP", "wamid.X1", str(int(datetime.utcnow().timestamp()) + 5))
    assert contact()["status"] == "opted_out" and E.check(PHONE).eligible is False and E.check(PHONE).reason == E.OPTED_OUT
    C.record_opt_in(PHONE, "START", "wamid.X2", str(int(datetime.utcnow().timestamp()) + 60))
    assert E.check(PHONE).eligible is True


def test_registration_never_overrides_an_existing_stop():
    C.record_opt_out(PHONE, "STOP", "wamid.S1", str(int(datetime.utcnow().timestamp()) - 100))
    reg(); login()
    k = contact()
    assert k["status"] == "opted_out" and k["last_consent"]["source"] == "whatsapp_keyword"
    assert E.check(PHONE).eligible is False
    assert fake.accounts.find_one({"name": "Asha"})["whatsapp_consent_result"] == "opted_out_kept"
    assert fake[C.EVENTS].count_documents({"event": "registration_opt_in"}) == 0


def test_re_registration_after_stop_and_deletion_does_not_reopt_in():
    reg(); login()
    C.record_opt_out(PHONE, "STOP", "wamid.S2", str(int(datetime.utcnow().timestamp()) + 5))
    fake.accounts.update_one({"name": "Asha"}, {"$set": {"status": "deleted"}})
    assert reg(name="Asha2").status_code == 200
    login()
    assert contact()["status"] == "opted_out" and E.check(PHONE).eligible is False


def test_existing_admin_recorded_consent_is_left_untouched():
    C.record_admin_consent(PHONE, "signup_form", "FORM-123", (datetime.utcnow()).isoformat(), {"id": "a1", "name": "Asha", "role": "Lead"}, True)
    before = contact()
    reg(); login()
    after = contact()
    assert after["last_consent"] == before["last_consent"] and fake[C.CONTACTS].count_documents({}) == 1
    assert E.check(PHONE).basis == E.BASIS_ADMIN_RECORDED
    assert fake.accounts.find_one({"name": "Asha"})["whatsapp_consent_result"] == "already_opted_in"


def test_re_registration_with_a_new_account_carries_no_stale_pending_consent():
    reg()                                                     # pending, never logged in
    fake.accounts.update_one({"name": "Asha"}, {"$set": {"status": "deleted"}})
    assert c.post("/user/register", json={"name": "N", "phone": PHONE, "city": ""}).status_code == 200      # old build, no consent
    a = fake.accounts.find_one({"name": "N"})
    assert "wa_consent_pending" not in a
    login()
    assert contact() is None
