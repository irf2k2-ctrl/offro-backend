"""Phase 2 / Task 5: CHARACTERISATION of the current country resolution and of every known India fallback.
These tests change no behaviour. They pin today's results so that a later, approved change must update them deliberately
(see docs/PHASE2_TASK5_COUNTRY_FALLBACK_REVIEW.md).  Run: python -m pytest test_phase2_task5_country_resolution.py -q -W ignore"""
import sys

import pytest
from fastapi.testclient import TestClient

import server
from services import country_diagnostics as D
from services import owner_country as OC
from test_dashboard_country_access import H, mk_user
from utils import countries as C

DB = sys.modules["database"].db
web = TestClient(server.app)

# (stored phone, stored phone_country) -> (iso, source)
RESOLUTION = [
    (("9876543210", ""), ("IN", "legacy_india")), (("919876543210", ""), ("IN", "legacy_india")), (("09876543210", ""), ("IN", "legacy_india")),
    (("+919876543210", ""), ("IN", "parsed")),
    (("+9779812345678", ""), ("NP", "parsed")), (("+966501234567", ""), ("SA", "parsed")), (("+639171234567", ""), ("PH", "parsed")),
    (("+962791234567", ""), ("JO", "parsed")),
    (("9812345678", "NP"), ("NP", "explicit")), (("+9779812345678", "IN"), ("IN", "explicit")),     # explicit valid country always wins
    (("+966501234567", "XX"), ("SA", "parsed")),                                                      # invalid stored country is ignored
    (("+966501234567", "sa"), ("SA", "explicit")),
    (("501234567", ""), ("", "unresolved")), (("791234567", ""), ("", "unresolved")), (("12345", ""), ("", "unresolved")),
    (("+15551234567", ""), ("", "unresolved")), (("", ""), ("", "unresolved")),
]


@pytest.mark.parametrize("stored,expected", RESOLUTION)
def test_current_resolution(stored, expected):
    r = C.resolve_account_country({"phone": stored[0], "phone_country": stored[1]})
    assert (r["iso"], r["source"]) == expected


def test_known_misclassification_is_visible_not_silent():
    """A BARE 10-digit non-Indian mobile with no phone_country (a Philippine or Nepal number saved without '+') is read as legacy India.
    This is the one resolution rule that can be wrong. It is always labelled `legacy_india`, so Country Diagnostics lists it."""
    for bare in ("9171234567", "9812345678"):
        r = C.resolve_account_country({"phone": bare})
        assert (r["iso"], r["source"]) == ("IN", "legacy_india")
        assert "legacy_india" in D.classify({"phone": bare})["issues"]


def test_diagnostics_flags_mismatch_and_invalid_country():
    assert "country_mismatch" in D.classify({"phone": "+9779812345678", "phone_country": "IN"})["issues"]
    assert "invalid_phone_country" in D.classify({"phone": "+966501234567", "phone_country": "XX"})["issues"]
    assert D.classify({"phone": "501234567"})["source"] == "unresolved"


def test_owner_country_fallbacks_today():
    """Silent India fallbacks that exist today in services.owner_country (public lists / display currency)."""
    own = OC.OwnerCountries()
    assert own.iso("000000000000000000000000") == "IN"             # owner not found            -> India
    assert own.iso("") == "IN" and own.iso(None) == "IN"           # no owner key at all        -> India
    a = DB.accounts.insert_one({"phone": "501234567", "name": "ghost", "roles": ["merchant"]}).inserted_id
    assert OC.OwnerCountries().iso(str(a)) == "IN"                 # owner exists, country unresolved -> India
    assert own.influencer_iso({"name": "x"}) == "IN"
    assert own.owner_iso_of_banner({"merchant_phone": "12345"}) == "IN"
    assert own.owner_iso_of_banner({}) is None                      # admin banner: no owner, no country
    assert own.money("SA")["currency_code"] in ("", "SAR") and own.money("IN") == {"currency_code": "INR", "currency_symbol": "₹"}


def test_new_task2_payment_country_never_assumes_india():
    from services.payment_country import PaymentCountries
    a = DB.accounts.insert_one({"phone": "501234567", "name": "ghost"}).inserted_id
    f = PaymentCountries().fields({"total": 5}, [str(a)], [])
    assert f["country"] == "" and f["currency_code"] == ""


def test_country_diagnostics_module_is_still_deployed():
    DB.dashboard_users.delete_many({}); DB.dashboard_roles.delete_many({})
    mk_user("sup", role="Super Admin")
    r = web.get("/admin/country-diagnostics", headers=H("sup"))
    assert r.status_code == 200, r.text
