"""Phase 2 / Task 3: Support/Admin account edit - name + WhatsApp-OTP-verified number change.
(name sorts after test_banner_media.py on purpose.)  Run: python -m pytest test_phase2_task3_account_edit.py -q -W ignore"""
import copy
import sys
from datetime import datetime, timedelta

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import server
from services import account_change as AC
from services import whatsapp as WA
from test_dashboard_country_access import ALL_PERMS, H, mk_user

DB = sys.modules["database"].db
web = TestClient(server.app)


@pytest.fixture
def world(monkeypatch):
    for c in ("accounts", "users", "merchants", "stores", "merchant_banners", "influencers", "account_phone_changes", "security_audit",
              "dashboard_users", "dashboard_roles", "invoices"):
        DB[c].delete_many({})
    sent = []
    monkeypatch.setattr(WA, "send_otp_message", lambda phone, otp: (sent.append((phone, otp)) or {"ok": True}))
    w = {"sent": sent}
    u = DB.users.insert_one({"name": "Ravi", "phone": "9876543210", "token": "legacy"}).inserted_id
    w["in"] = str(DB.accounts.insert_one({"name": "Ravi", "phone": "9876543210", "roles": ["user"], "status": "active", "city": "Ballari",
                                          "user_id": str(u), "token": "tok-in"}).inserted_id)
    w["sa"] = str(DB.accounts.insert_one({"name": "Sami", "phone": "+966501234567", "phone_country": "SA", "phone_e164": "+966501234567",
                                          "roles": ["merchant"], "status": "active", "city": "Riyadh", "token": "tok-sa"}).inserted_id)
    DB.stores.insert_one({"merchant_id": w["sa"], "merchant_phone": "+966501234567", "store_name": "S", "status": "active"})
    DB.invoices.insert_one({"merchant_id": w["sa"], "merchant_phone": "+966501234567", "invoice_no": "I1", "total": 5})
    w["np"] = str(DB.accounts.insert_one({"name": "Nima", "phone": "+9779700000001", "roles": ["user"], "status": "active"}).inserted_id)
    mk_user("sup", role="Super Admin"); mk_user("sup2", role="Super Admin")
    mk_user("staff", role="Support"); mk_user("sa-staff", role="SA Support", country="SA")
    perms = copy.deepcopy(ALL_PERMS); perms["Accounts"]["edit"] = False
    mk_user("view", role="ViewOnly", perms=perms)
    return w


def audit(action=None):
    q = {"action": action} if action else {}
    return list(DB.security_audit.find(q))


def start(w, tok="staff", acct="in", phone="9812345678", country="NP", **extra):
    return web.post(f"/admin/accounts/{w[acct]}/phone-change", headers=H(tok), json={"new_phone": phone, "country": country, **extra})


def last_code(w):
    return w["sent"][-1][1]


# ── name ──────────────────────────────────────────────────────────────────────
def test_edit_name_audited_and_synced(world):
    r = web.put(f"/admin/accounts/{world['in']}/edit-name", headers=H("staff"), json={"name": "  Ravi   Kumar "})
    assert r.status_code == 200 and r.json()["name"] == "Ravi Kumar"
    assert DB.accounts.find_one({"_id": ObjectId(world["in"])})["name"] == "Ravi Kumar"
    assert DB.users.find_one({})["name"] == "Ravi Kumar"
    assert [e["outcome"] for e in audit("account.name_change")] == ["started", "ok"]
    assert audit("account.name_change")[0]["admin"]["name"] == "staff"


@pytest.mark.parametrize("bad", ["", "a", "x" * 81, "<b>", None])
def test_edit_name_validation(world, bad):
    assert web.put(f"/admin/accounts/{world['in']}/edit-name", headers=H("staff"), json={"name": bad}).status_code == 400


def test_edit_requires_accounts_edit_permission(world):
    assert web.put(f"/admin/accounts/{world['in']}/edit-name", headers=H("view"), json={"name": "Zed"}).status_code == 403
    assert start(world, "view").status_code == 403
    assert web.put(f"/admin/accounts/{world['in']}/edit-name", json={"name": "Zed"}).status_code in (401, 403)


def test_country_restricted_staff_cannot_touch_other_country(world):
    assert web.put(f"/admin/accounts/{world['in']}/edit-name", headers=H("sa-staff"), json={"name": "Zed"}).status_code == 404
    assert start(world, "sa-staff", acct="in").status_code == 404
    assert web.put(f"/admin/accounts/{world['sa']}/edit-name", headers=H("sa-staff"), json={"name": "Sami B"}).status_code == 200


def test_country_restricted_staff_cannot_move_account_to_another_country(world):
    r = start(world, "sa-staff", acct="sa", phone="9812345678", country="NP")
    assert r.status_code == 403 and not world["sent"] and audit("account.phone_change_requested")[-1]["outcome"] == "denied"


def test_assigned_city_restriction(world):
    mk_user("city-staff", role="CityStaff", cities=("Ballari",))
    assert web.put(f"/admin/accounts/{world['sa']}/edit-name", headers=H("city-staff"), json={"name": "Zed"}).status_code == 404
    assert web.put(f"/admin/accounts/{world['in']}/edit-name", headers=H("city-staff"), json={"name": "Zed"}).status_code == 200


# ── number change: OTP is mandatory ─────────────────────────────────────────────
def test_request_sends_whatsapp_code_to_new_number_and_keeps_old(world):
    r = start(world)
    assert r.status_code == 200 and r.json()["request"]["status"] == "awaiting_otp"
    assert world["sent"][0][0] == "+9779812345678" and len(last_code(world)) == 4
    a = DB.accounts.find_one({"_id": ObjectId(world["in"])})
    assert a["phone"] == "9876543210" and a.get("token") == "tok-in"          # untouched until verified
    body = r.text
    assert last_code(world) not in body and "9812345678" not in body            # no code, no full number in the response
    c = DB.account_phone_changes.find_one({})
    assert "otp" in c and last_code(world) not in str(c)                          # only a salted hash is stored


def test_correct_code_applies_every_phone_field(world):
    cid = start(world).json()["request"]["id"]
    r = web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": last_code(world)})
    assert r.status_code == 200
    a = DB.accounts.find_one({"_id": ObjectId(world["in"])})
    assert (a["phone"], a["phone_country"], a["phone_e164"]) == ("+9779812345678", "NP", "+9779812345678")
    assert a["token"] is None and a["previous_phones"][0]["masked"] and "9876543210" not in str(a["previous_phones"])
    assert DB.users.find_one({})["phone"] == "+9779812345678"
    assert DB.account_phone_changes.find_one({})["status"] == "completed" and DB.account_phone_changes.find_one({})["otp"] is None
    acts = [(e["action"], e["outcome"]) for e in audit()]
    assert ("account.phone_change_applied", "started") in acts and ("account.phone_change_applied", "ok") in acts
    assert "9812345678" not in str(audit()) and last_code(world) not in str(audit())


def test_ownership_copies_follow_but_history_is_kept(world):
    cid = start(world, acct="sa", tok="sup", phone="501234568", country="SA").json()["request"]["id"]
    assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("sup"), json={"code": last_code(world)}).status_code == 200
    assert DB.stores.find_one({})["merchant_phone"] == "+966501234568"
    assert DB.invoices.find_one({})["merchant_phone"] == "+966501234567"           # historical invoice untouched


def test_wrong_code_does_not_change_and_locks_after_five(world):
    cid = start(world).json()["request"]["id"]
    good = last_code(world); bad = "0000" if good != "0000" else "1111"
    for _ in range(5):
        assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": bad}).status_code == 400
    assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": good}).status_code == 429
    assert DB.accounts.find_one({"_id": ObjectId(world["in"])})["phone"] == "9876543210"
    assert [e["outcome"] for e in audit("account.phone_change_verify")].count("failed") == 6


def test_expired_code_rejected(world):
    cid = start(world).json()["request"]["id"]
    DB.account_phone_changes.update_one({}, {"$set": {"otp.expires_at": datetime.utcnow() - timedelta(seconds=1)}})
    assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": last_code(world)}).status_code == 410
    assert DB.accounts.find_one({"_id": ObjectId(world["in"])})["phone"] == "9876543210"


def test_code_cannot_be_reused(world):
    cid = start(world).json()["request"]["id"]
    code = last_code(world)
    assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": code}).status_code == 200
    assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": code}).status_code == 409


def test_no_endpoint_applies_a_number_without_the_code(world):
    cid = start(world).json()["request"]["id"]
    for body in ({}, {"code": ""}, {"code": None}, {"skip_otp": True, "code": ""}, {"force": True}):
        assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("sup"), json=body).status_code in (400, 429)
    assert DB.accounts.find_one({"_id": ObjectId(world["in"])})["phone"] == "9876543210"


def test_delivery_failure_changes_nothing(world, monkeypatch):
    monkeypatch.setattr(WA, "send_otp_message", lambda p, o: {"ok": False, "error": "x"})
    assert start(world).status_code == 502
    c = DB.account_phone_changes.find_one({})
    assert c["otp"] is None
    assert web.post(f"/admin/phone-changes/{c['_id']}/verify", headers=H("staff"), json={"code": "1234"}).status_code == 409
    assert DB.accounts.find_one({"_id": ObjectId(world["in"])})["phone"] == "9876543210"


def test_resend_cooldown_and_new_code_invalidates_old(world):
    cid = start(world).json()["request"]["id"]
    old = last_code(world)
    assert web.post(f"/admin/phone-changes/{cid}/resend", headers=H("staff")).status_code == 429       # cooldown
    DB.account_phone_changes.update_one({}, {"$set": {"otp.sent_at": datetime.utcnow() - timedelta(seconds=60)}})
    assert web.post(f"/admin/phone-changes/{cid}/resend", headers=H("staff")).status_code == 200
    new = last_code(world)
    if new != old:
        assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": old}).status_code == 400
    assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": new}).status_code == 200


# ── validation + duplicates ─────────────────────────────────────────────────────
@pytest.mark.parametrize("phone,country", [("123", "IN"), ("12345", "SA"), ("abc", "NP"), ("9812345678", "ZZ"), ("", "IN")])
def test_invalid_number_for_country(world, phone, country):
    assert start(world, phone=phone, country=country).status_code == 400 and not world["sent"]


@pytest.mark.parametrize("phone,country", [("9123456789", "IN"), ("0791234567", "JO"), ("9171234567", "PH"), ("501234569", "SA"), ("9841234567", "NP")])
def test_valid_numbers_for_each_supported_country(world, phone, country):
    r = start(world, phone=phone, country=country)
    assert r.status_code == 200, r.text
    assert r.json()["request"]["new_country"] == country


def test_duplicate_number_refused_in_all_formats(world):
    for phone, ctry in (("+9779700000001", "NP"), ("9700000001", "NP"), ("501234567", "SA")):
        r = start(world, phone=phone, country=ctry)
        assert r.status_code == 409 and "already assigned" in r.text, (phone, r.text)
    assert not world["sent"]


def test_indian_number_duplicate_in_legacy_formats(world):
    assert start(world, acct="sa", tok="sup", phone="+919876543210", country="IN").status_code == 409
    assert start(world, acct="sa", tok="sup", phone="09876543210", country="IN").status_code == 409


def test_duplicate_in_legacy_collections_and_pending_requests(world):
    DB.merchants.insert_one({"name": "Old", "phone": "9123456789"})
    assert start(world, phone="9123456789", country="IN").status_code == 409
    assert start(world, acct="in", phone="9841234567", country="NP").status_code == 200
    assert start(world, acct="np", phone="9841234567", country="NP").status_code == 409        # held by another open request
    assert start(world, acct="in", phone="9841234568", country="NP").status_code == 409        # one open request per account


def test_same_number_rejected(world):
    assert start(world, phone="9876543210", country="IN").status_code == 400


def test_duplicate_rechecked_at_apply_time(world):
    cid = start(world).json()["request"]["id"]
    DB.accounts.insert_one({"name": "Late", "phone": "+9779812345678"})
    assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": last_code(world)}).status_code == 409
    assert DB.accounts.find_one({"_id": ObjectId(world["in"])})["phone"] == "9876543210"


# ── old number unavailable => Super Admin ───────────────────────────────────────
def test_unavailable_old_number_needs_reason_and_super_admin(world):
    assert start(world, old_number_unavailable=True).status_code == 400                       # reason missing
    r = start(world, old_number_unavailable=True, reason="Customer lost the SIM")
    assert r.status_code == 200 and r.json()["request"]["status"] == "pending_approval" and not world["sent"]
    cid = r.json()["request"]["id"]
    assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": "1234"}).status_code == 409   # no code exists
    assert web.post(f"/admin/phone-changes/{cid}/approve", headers=H("staff")).status_code == 403
    assert not world["sent"]
    assert web.post(f"/admin/phone-changes/{cid}/approve", headers=H("sup")).status_code == 200
    assert world["sent"] and world["sent"][0][0] == "+9779812345678"
    assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": last_code(world)}).status_code == 200
    assert DB.accounts.find_one({"_id": ObjectId(world["in"])})["phone"] == "+9779812345678"
    acts = [e["action"] for e in audit()]
    assert "account.phone_change_approved" in acts
    done = [e for e in audit("account.phone_change_applied") if e["outcome"] == "started"][0]
    assert done["details"]["old_unavailable"] is True and done["details"]["approved_by"] == "sup"


def test_super_admin_cannot_approve_own_request_and_reject_works(world):
    cid = start(world, tok="sup", old_number_unavailable=True, reason="SIM lost for good").json()["request"]["id"]
    assert web.post(f"/admin/phone-changes/{cid}/approve", headers=H("sup")).status_code == 403
    assert web.post(f"/admin/phone-changes/{cid}/reject", headers=H("sup2"), json={"note": "no"}).status_code == 200
    assert DB.account_phone_changes.find_one({})["status"] == "rejected"
    assert web.post(f"/admin/phone-changes/{cid}/approve", headers=H("sup2")).status_code == 409


def test_stale_approval_expires(world):
    cid = start(world, old_number_unavailable=True, reason="SIM lost for good").json()["request"]["id"]
    DB.account_phone_changes.update_one({}, {"$set": {"requested_at": datetime.utcnow() - timedelta(hours=25)}})
    assert web.post(f"/admin/phone-changes/{cid}/approve", headers=H("sup")).status_code == 410


def test_cancel_closes_request_and_code(world):
    cid = start(world).json()["request"]["id"]
    assert web.post(f"/admin/phone-changes/{cid}/cancel", headers=H("staff")).status_code == 200
    assert web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": last_code(world)}).status_code == 409
    assert DB.accounts.find_one({"_id": ObjectId(world["in"])})["phone"] == "9876543210"


def test_other_country_staff_cannot_use_a_request_id(world):
    cid = start(world).json()["request"]["id"]
    for ep in ("verify", "resend", "cancel", "approve"):
        assert web.post(f"/admin/phone-changes/{cid}/{ep}", headers=H("sa-staff"), json={"code": "1234"}).status_code == 404


def test_new_number_can_sign_in_flow_lookup(world):
    from utils import phone as ph
    cid = start(world).json()["request"]["id"]
    web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": last_code(world)})
    keys = ph.account_lookup_keys("+9779812345678")
    assert DB.accounts.find_one({"phone": {"$in": keys}})["name"] == "Ravi"
    assert DB.accounts.find_one({"phone": {"$in": ph.account_lookup_keys("9876543210")}}) is None


def test_audit_is_append_only_and_has_no_secrets(world):
    cid = start(world).json()["request"]["id"]
    web.post(f"/admin/phone-changes/{cid}/verify", headers=H("staff"), json={"code": last_code(world)})
    txt = str(audit())
    assert last_code(world) not in txt and "tok-in" not in txt
    assert all(e["at"] for e in audit())
