"""Registration consent (Terms & Conditions + Privacy Policy + data processing).
Run:  python -m pytest test_register_consent.py -q -W ignore"""
import sys
from fastapi.testclient import TestClient
import server

fake = sys.modules["database"].db
c = TestClient(server.app)


def _reg(**kw):
    body = {"name": "Asha", "phone": "9876543210", "city": ""}
    body.update(kw)
    return c.post("/user/register", json=body)


def test_consent_true_is_recorded_with_timestamp():
    r = _reg(consent=True)
    assert r.status_code == 200
    a = fake.accounts.find_one({"name": "Asha"})
    assert a["consent_accepted"] is True
    assert a["consent_accepted_at"] and a["consent_accepted_at"] == a["terms_accepted_at"]
    assert a["created_at"]


def test_consent_also_mirrored_to_legacy_users():
    _reg(consent=True)
    assert fake.users.find_one({"name": "Asha"})["consent_accepted"] is True


def test_explicit_false_or_non_boolean_is_rejected_and_nothing_created():
    for bad in (False, "true", 1, None, "yes"):
        r = _reg(consent=bad)
        assert r.status_code == 400, bad
        assert "Terms" in r.json()["detail"]
    assert fake.accounts.count_documents({}) == 0


def test_old_app_without_consent_field_still_registers_without_claiming_consent():
    r = _reg()
    assert r.status_code == 200
    a = fake.accounts.find_one({"name": "Asha"})
    assert "consent_accepted" not in a and "consent_accepted_at" not in a


def test_duplicate_registration_still_blocked():
    assert _reg(consent=True).status_code == 200
    assert _reg(consent=True).status_code == 400


def test_re_registration_after_deletion_records_fresh_consent():
    _reg()
    fake.accounts.update_one({"name": "Asha"}, {"$set": {"status": "deleted"}})
    r = _reg(name="Asha2", consent=True)
    assert r.status_code == 200
    a = fake.accounts.find_one({"phone": {"$exists": True}})
    assert a["status"] == "active" and a["consent_accepted"] is True and a["consent_accepted_at"]


def test_me_exposes_consent_fields_additively():
    from routers import users
    _reg(consent=True)
    a = fake.accounts.find_one({"name": "Asha"})
    out = users.get_profile(user=a)
    assert out["consent_accepted"] is True and out["consent_accepted_at"]
    legacy = users.get_profile(user={"_id": a["_id"], "name": "Old"})
    assert legacy["consent_accepted"] is False and legacy["consent_accepted_at"] == ""
