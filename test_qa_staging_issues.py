"""Staging QA issues - backend regressions.
 1. Roles & Permissions: "Influencers" was missing from the backend module list, so a saved Influencers grant was silently dropped.
 3. Super Admin account edit: the backend path (name + OTP-verified number change) must keep working with every safeguard.
 (2 = the Refresh button, 3 = the edit dialog focus: browser tests in test_qa_staging_issues_browser.py.)
Run: python -m pytest test_qa_staging_issues.py -q -W ignore   (name sorts after test_banner_media.py on purpose)"""
import re
import sys
from pathlib import Path

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import server
from routers import dashboard_auth as DA
from test_dashboard_country_access import H, mk_user
from test_phase2_task3_account_edit import audit, last_code, start, world  # noqa: F401  (world = the account-edit fixture)

DB = sys.modules["database"].db
web = TestClient(server.app)
HERE = Path(__file__).resolve().parent


def dashboard_modules():
    html = (HERE / "templates" / "admin_dashboard.html").read_text(encoding="utf-8")
    block = re.search(r"const RBAC_MODULES = \[(.*?)\];", html, re.S).group(1)
    return re.findall(r'"([^"]+)"', block)


def full(value=True):
    return {m: {a: value for a in DA.ALL_ACTIONS} for m in DA.ALL_MODULES}


@pytest.fixture
def roles():
    for c in ("dashboard_roles", "dashboard_users", "activity_logs"):
        DB[c].delete_many({})
    mk_user("sup", role="Super Admin", perms={m: {a: False for a in DA.ALL_ACTIONS} for m in DA.ALL_MODULES})
    return DB.dashboard_roles.find_one({"role_name": "Super Admin"})["_id"]


# ── Issue 1: Influencers permissions ──────────────────────────────────────────────────────────────────────────────────
def test_every_module_the_dashboard_matrix_sends_is_known_to_the_backend():
    """The matrix and the backend list must never drift apart again (a module the backend does not list is silently discarded on save)."""
    ui = dashboard_modules()
    assert "Influencers" in ui and "Influencers" in DA.ALL_MODULES
    assert [m for m in ui if m not in DA.ALL_MODULES] == []
    assert [m for m in DA.ALL_MODULES if m not in ui] == []


def test_super_admin_role_keeps_all_influencer_permissions_after_save_and_reopen(roles):
    payload = {m: {a: False for a in DA.ALL_ACTIONS} for m in dashboard_modules()}
    payload["Influencers"] = {a: True for a in DA.ALL_ACTIONS}
    r = web.put(f"/admin/auth/roles/{roles}", headers=H("sup"), json={"role_name": "Super Admin", "description": "Full", "permissions": payload})
    assert r.status_code == 200
    got = {x["role_name"]: x for x in web.get("/admin/auth/roles", headers=H("sup")).json()}["Super Admin"]["permissions"]
    assert got["Influencers"] == {a: True for a in DA.ALL_ACTIONS}                      # restored when the role is reopened
    assert DB.dashboard_roles.find_one({"_id": roles})["permissions"]["Influencers"]["delete"] is True      # and really stored


def test_partial_influencer_grants_round_trip_and_other_modules_are_unaffected(roles):
    perms = {m: {a: False for a in DA.ALL_ACTIONS} for m in dashboard_modules()}
    perms["Influencers"].update(view=True, edit=True)
    perms["Accounts"].update(view=True)
    web.put(f"/admin/auth/roles/{roles}", headers=H("sup"), json={"permissions": perms})
    got = DB.dashboard_roles.find_one({"_id": roles})["permissions"]
    assert got["Influencers"] == {"view": True, "add": False, "edit": True, "delete": False, "approve": False, "export": False}
    assert got["Accounts"]["view"] is True and got["Stores"]["view"] is False and set(got) == set(DA.ALL_MODULES)


def test_creating_a_custom_role_stores_influencer_permissions_and_they_grant_access(roles):
    perms = {m: {a: False for a in DA.ALL_ACTIONS} for m in dashboard_modules()}
    perms["Influencers"].update(edit=True)
    r = web.post("/admin/auth/roles", headers=H("sup"), json={"role_name": "Influencer Desk", "description": "x", "permissions": perms})
    assert r.status_code == 200
    stored = DB.dashboard_roles.find_one({"role_name": "Influencer Desk"})["permissions"]["Influencers"]
    assert stored["edit"] is True and stored["delete"] is False
    mk_user("desk", role="Influencer Desk")
    from routers.admin import _check_perm
    adm = web.get("/admin/auth/me", headers=H("desk")).json()
    assert adm["permissions"]["Influencers"]["edit"] is True
    with pytest.raises(Exception):
        _check_perm({"role_name": "Influencer Desk", "permissions": stored}, "Influencers", "delete")


def test_unknown_module_names_are_still_discarded(roles):
    perms = full(False)
    perms["Not A Module"] = {"view": True}
    web.put(f"/admin/auth/roles/{roles}", headers=H("sup"), json={"permissions": perms})
    assert "Not A Module" not in DB.dashboard_roles.find_one({"_id": roles})["permissions"]


def test_seeded_roles_keep_their_previous_effective_influencer_access():
    """A fresh database: Super Admin has everything; no other seeded role gains Influencers access just because the module is now listed."""
    DB.dashboard_roles.delete_many({})
    DA.seed_rbac()
    seeded = {r["role_name"]: r["permissions"] for r in DB.dashboard_roles.find()}
    assert all(seeded["Super Admin"]["Influencers"].values())
    for name, perms in seeded.items():
        if name != "Super Admin":
            assert not any(perms["Influencers"].values()), name
    assert set(seeded["Administrator"]) == set(DA.ALL_MODULES)


# ── Issue 3 (backend half): Super Admin account edit with every safeguard ─────────────────────────────────────────────
def test_super_admin_can_edit_the_account_name_and_it_is_audited(world):
    r = web.put(f"/admin/accounts/{world['in']}/edit-name", headers=H("sup"), json={"name": "D'Souza  Kumar"})
    assert r.status_code == 200 and r.json() == {"ok": True, "name": "D'Souza Kumar", "changed": True}
    assert DB.accounts.find_one({"_id": ObjectId(world["in"])})["name"] == "D'Souza Kumar"
    assert [a["outcome"] for a in audit("account.name_change")] == ["started", "ok"]


def test_super_admin_number_change_keeps_old_number_until_whatsapp_code_is_verified(world):
    r = start(world, tok="sup", acct="in", phone="9812345678", country="NP")
    assert r.status_code == 200 and r.json()["request"]["status"] == "awaiting_otp"
    assert len(world["sent"]) == 1 and world["sent"][0][0].endswith("9812345678")                  # code went to the NEW number
    acct = DB.accounts.find_one({"_id": ObjectId(world["in"])})
    assert acct["phone"] == "9876543210"                                                           # old number still active
    cid = r.json()["request"]["id"]
    bad = web.post(f"/admin/phone-changes/{cid}/verify", headers=H("sup"), json={"code": "000000"})
    assert bad.status_code in (400, 429) and DB.accounts.find_one({"_id": ObjectId(world["in"])})["phone"] == "9876543210"
    ok = web.post(f"/admin/phone-changes/{cid}/verify", headers=H("sup"), json={"code": last_code(world)})
    assert ok.status_code == 200
    new = DB.accounts.find_one({"_id": ObjectId(world["in"])})
    assert new["phone"] != "9876543210" and new["phone"].endswith("9812345678")
    assert "account.phone_change_requested" in {a["action"] for a in audit()}


def test_super_admin_still_gets_duplicate_and_country_validation(world):
    assert start(world, tok="sup", acct="sa", phone="+919876543210", country="IN").status_code == 409      # number belongs to another account
    assert start(world, tok="sup", acct="in", phone="12345", country="NP").status_code == 400             # not a valid mobile of that country
    assert start(world, tok="sup", acct="in", phone="9812345678", country="XX").status_code == 400        # unsupported country
    assert world["sent"] == []
