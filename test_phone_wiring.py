"""Targeted tests for Phase D: the shared phone helper wired into the user / merchant / admin routes and WhatsApp consent.
Run:  python -m pytest test_phone_wiring.py -q -W ignore
Uses the same in-memory fake database as the other route tests; no real database, no network, no OTP code is touched."""
import sys

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import server
from routers import users as U
from services import wa_consent as W
from utils import phone as P

fake = sys.modules["database"].db


@pytest.fixture(autouse=True)
def _login_proof_not_under_test(monkeypatch):
    """These tests are about phone-format matching / consent, not about the OTP proof (Phase 2 D2). The proof rule itself is tested for real in
    test_phase2_d2_login_proof.py; here it is stepped over so the account lookup underneath is still exercised exactly as before."""
    from services import login_proof
    monkeypatch.setattr(login_proof, "require_login_proof", lambda *a, **k: "otp")
c = TestClient(server.app)

IN_LOCAL = "9876543210"
NP = "+9779812345678"          # valid Nepal mobile
NP_LOCAL = "9812345678"        # same digits as a valid INDIAN mobile
SA = "+966501234567"
JO = "+962791234567"
PH = "+639171234567"


def _reg(phone, **kw):
    body = {"name": "T", "phone": phone, "city": ""}
    body.update(kw)
    return c.post("/user/register", json=body)


def _check(phone, **kw):
    return c.post("/user/check-phone", json={"phone": phone, **kw}).json()


# ── existing Indian behaviour is unchanged ─────────────────────────────────────
@pytest.mark.parametrize("fmt", [IN_LOCAL, "919876543210", "+919876543210", "09876543210"])
def test_indian_formats_all_find_an_existing_indian_account(fmt):
    fake.accounts.insert_one({"phone": "+919876543210", "roles": ["user"], "status": "active"})
    assert _check(fmt) == {"registered": True, "role": "user"}


def test_existing_indian_records_stored_in_any_legacy_shape_are_found():
    for i, stored in enumerate(["9876543210", "919876543210", "+919876543210", "09876543210"]):
        fake.accounts.delete_many({})
        fake.accounts.insert_one({"phone": stored, "roles": ["user"], "status": "active"})
        assert _check("+919876543210")["registered"] is True, stored
        assert _check(IN_LOCAL)["registered"] is True, stored


def test_indian_registration_is_unchanged_and_lenient():
    assert _reg(IN_LOCAL).status_code == 200
    a = fake.accounts.find_one({})
    assert a["phone"] == "+919876543210"
    assert set(a["phone_variants"]) == set(U._phone_variants(IN_LOCAL)) == {"+919876543210", "919876543210", IN_LOCAL, "09876543210"}
    assert _reg(IN_LOCAL).status_code == 400                          # duplicate still blocked
    fake.accounts.delete_many({}); fake.users.delete_many({})
    assert _reg("5551234567").status_code == 200                      # not a real mobile, but any 10 digits is accepted as before
    assert fake.accounts.find_one({})["phone"] == "+915551234567"


def test_indian_login_still_issues_a_token_for_every_format():
    fake.accounts.insert_one({"phone": "+919876543210", "name": "A", "roles": ["user"], "status": "active"})
    for fmt in [IN_LOCAL, "919876543210", "+919876543210"]:
        r = c.post("/user/account-login", json={"phone": fmt})
        assert r.status_code == 200 and r.json()["token"], fmt
        assert r.json()["phone"] == "+919876543210"


# ── international numbers ──────────────────────────────────────────────────────
@pytest.mark.parametrize("num,iso", [(SA, "SA"), (JO, "JO"), (NP, "NP"), (PH, "PH")])
def test_international_registration_check_and_login(num, iso):
    assert _reg(num, country=iso).status_code == 200
    a = fake.accounts.find_one({})
    assert a["phone"] == num and a["phone_country"] == iso and a["phone_e164"] == num
    assert _check(num, country=iso)["registered"] is True
    assert _check(num)["registered"] is True                          # a +E.164 number identifies itself, even with no country
    assert c.post("/user/account-login", json={"phone": num, "country": iso}).status_code == 200
    assert _reg(num, country=iso).status_code == 400                  # duplicate blocked


def test_nepal_and_india_same_digits_never_collide():
    assert _reg(NP_LOCAL).status_code == 200                           # old app: no country -> Indian +919812345678
    assert fake.accounts.find_one({})["phone"] == "+91" + NP_LOCAL
    assert _check(NP, country="NP")["registered"] is False             # the Nepal number is a different person
    assert _check(NP)["registered"] is False
    assert _reg(NP, country="NP").status_code == 200                   # and can register
    assert fake.accounts.count_documents({}) == 2
    assert _check(NP, country="NP")["registered"] is True and _check(NP_LOCAL)["registered"] is True


def test_philippines_never_matches_by_last_ten_digits():
    fake.accounts.insert_one({"phone": "+919171234567", "roles": ["user"], "status": "active"})
    assert _check(PH, country="PH")["registered"] is False
    assert U._phone_variants(PH) == [PH, PH[1:]] or set(U._phone_variants(PH)) == {PH, PH[1:]}


def test_registration_rejects_bad_international_input():
    assert _reg(SA, country="NP").status_code == 400                   # selected Nepal, typed a Saudi number
    assert _reg("+96650123", country="SA").status_code == 400          # too short for Saudi
    assert _reg("0112345678", country="SA").status_code == 400         # landline
    assert _reg("12345", country="US").status_code == 400              # unsupported country
    assert _reg("abc").status_code == 400
    assert fake.accounts.count_documents({}) == 0


def test_local_number_with_selected_country_is_accepted():
    assert _reg("0501234567", country="SA").status_code == 200
    assert fake.accounts.find_one({})["phone"] == SA


# ── merchant routes ────────────────────────────────────────────────────────────
def test_merchant_indian_registration_is_unchanged():
    r = c.post("/merchant/register", json={"name": "M", "phone": IN_LOCAL, "city": "x", "area": "y"})
    assert r.status_code == 200
    a = fake.accounts.find_one({})
    assert a["phone"] == IN_LOCAL and "merchant" in a["roles"]          # bare last-10 digits, exactly as before
    assert c.post("/merchant/register", json={"name": "M", "phone": IN_LOCAL}).status_code == 400
    assert c.post("/merchant/login", json={"phone": "+919876543210"}).status_code == 200
    assert c.post("/merchant/check-phone", json={"phone": "919876543210"}).json()["registered"] is True


def test_merchant_international_registration_and_login():
    r = c.post("/merchant/register", json={"name": "M", "phone": SA, "country": "SA"})
    assert r.status_code == 200
    a = fake.accounts.find_one({})
    assert a["phone"] == SA and "merchant" in a["roles"]
    assert fake.accounts.count_documents({}) == 1
    assert c.post("/merchant/login", json={"phone": SA, "country": "SA"}).status_code == 200
    assert c.post("/merchant/check-phone", json={"phone": SA}).json()["role"] == "merchant"
    assert c.post("/merchant/register", json={"name": "M", "phone": SA, "country": "SA"}).status_code == 400


def test_merchant_international_does_not_match_indian_merchant():
    c.post("/merchant/register", json={"name": "M", "phone": NP_LOCAL})            # Indian merchant 9812345678
    assert c.post("/merchant/check-phone", json={"phone": NP, "country": "NP"}).json()["registered"] is False
    assert c.post("/merchant/login", json={"phone": NP, "country": "NP"}).status_code == 401


# ── ownership keys (banners, stores, promo sliders) ────────────────────────────
def test_owner_key_keeps_indian_last10_and_separates_countries():
    for fmt in [IN_LOCAL, "919876543210", "+919876543210", "09876543210"]:
        assert P.owner_key(fmt) == IN_LOCAL
    assert P.same_number("+919876543210", IN_LOCAL)
    assert P.owner_key(NP) == "9779812345678" and P.owner_key(SA) == "966501234567"
    assert not P.same_number(NP, NP_LOCAL)                              # Nepal vs Indian with the same last 10 digits
    assert not P.same_number(PH, "+919171234567")
    assert P.same_number(SA, "966501234567")
    assert not P.same_number("", "")
    assert P.slider_phone_key("+91 98765-43210") == IN_LOCAL and P.slider_phone_key(JO) == "962791234567"


# ── influencer phone ───────────────────────────────────────────────────────────
def test_influencer_phone_accepts_old_ten_digits_and_valid_e164_only():
    assert U._influencer_phone(IN_LOCAL) == IN_LOCAL
    assert U._influencer_phone("5551234567") == "5551234567"           # unchanged: any 10 digits
    assert U._influencer_phone("+966 50 123 4567") == SA
    for bad in ["123", "98765432101", "+966123", "abc", "+1 650 253 0000", "9876 54321"]:
        with pytest.raises(HTTPException) as e:
            U._influencer_phone(bad)
        assert e.value.status_code == 400 and "10-digit" in e.value.detail


# ── WhatsApp consent keys ──────────────────────────────────────────────────────
def test_wa_consent_defaults_unchanged_and_country_aware_when_given():
    assert W.normalize_phone("9876543210") == "919876543210"
    assert W.normalize_phone("+919876543210") == "919876543210"
    assert W.normalize_phone(NP_LOCAL) == "919812345678"                # no country: old behaviour (Indian)
    assert W.normalize_phone(NP_LOCAL, country="NP") == "9779812345678"
    assert W.normalize_phone("0501234567", country="SA") == "966501234567"
    assert W.normalize_phone("12345", country="SA") == ""
    assert W.phone_lookup_keys(NP_LOCAL, country="NP") == ["9779812345678"]
    assert W.phone_lookup_keys(IN_LOCAL) == ["919876543210", IN_LOCAL]


# ── PHONE_INTL_MODE=legacy rollback switch ─────────────────────────────────────
def test_legacy_switch_restores_the_old_code_exactly(monkeypatch):
    monkeypatch.setenv("PHONE_INTL_MODE", "legacy")
    for raw in [IN_LOCAL, "+919876543210", "09876543210", NP, PH, "12345", "+9665012345678"]:
        assert set(U._phone_variants(raw)) == set(P.legacy_variants(raw))
        assert U._normalise_phone(raw) == P.legacy_normalise(raw)
        assert P.owner_key(raw) == "".join(ch for ch in raw if ch.isdigit())[-10:]
    assert P.phone_fields("+966501234567") == {}
    assert U._phone_variants(NP)[0] is not None
    with pytest.raises(HTTPException):
        U._influencer_phone(SA)                                         # only the old 10 digits in legacy mode
    assert _reg(IN_LOCAL).status_code == 200
    assert "phone_country" not in fake.accounts.find_one({})
