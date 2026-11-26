"""Staging QA issues in the REAL admin dashboard (headless Chromium): Influencer permissions round trip, the Refresh button,
and the Edit-account dialog being typeable inside the account modal.
The Bootstrap stand-in used by the other browser tests has no focus trap, which is how issue 3 slipped through; FOCUS_TRAP below re-creates
Bootstrap's modal focus trap (focus that leaves the open modal is pulled back into it). With OFFRO_BOOTSTRAP_DIST=<bootstrap/dist dir> the same
test also runs against the REAL Bootstrap bundle.
Run: python -m pytest test_qa_staging_issues_browser.py -q -W ignore"""
import os
import sys
from datetime import datetime

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import server
from services import whatsapp as WA
from test_deals_admin_browser import BASE, _open, browser, world  # noqa: F401  (fixtures + helper)

fake = sys.modules["database"].db

FOCUS_TRAP = """
window.bootstrap.Modal.getInstance = (el) => ({hide(){ el.classList.remove('show'); el.style.display='none'; }});
document.addEventListener('focusin', e => {
  const m = document.querySelector('.modal.show');
  if (!m || e.target === document || m.contains(e.target)) return;
  const f = m.querySelector('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
  (f || m).focus();
});
"""
INFL = ["view", "add", "edit", "delete", "approve", "export"]


def _roles(page):
    page.evaluate("showSection('rbac_roles', null)")
    page.wait_for_selector("#rbac_roles_table tr td strong")


# ── Issue 1 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_super_admin_influencer_permissions_save_and_come_back_when_reopened(browser, world):
    page, log = _open(browser)
    page.evaluate(FOCUS_TRAP)
    _roles(page)
    rid = str(fake.dashboard_roles.find_one({"role_name": "Super Admin"})["_id"])
    page.evaluate(f"rbacEditRole('{rid}')")
    page.wait_for_selector('.perm-chk[data-mod="Influencers"][data-act="view"]')
    assert not any(page.is_checked(f'.perm-chk[data-mod="Influencers"][data-act="{a}"]') for a in INFL)
    page.check('.perm-all[data-mod="Influencers"]')                                       # "all" for the Influencers row
    assert all(page.is_checked(f'.perm-chk[data-mod="Influencers"][data-act="{a}"]') for a in INFL)
    page.evaluate("rbacSaveRole()")
    page.wait_for_function("document.getElementById('rbac_roles_msg').innerText.includes('updated')")
    assert fake.dashboard_roles.find_one({"_id": ObjectId(rid)})["permissions"]["Influencers"] == {a: True for a in INFL}
    page.evaluate(f"rbacEditRole('{rid}')")                                                # reopen
    page.wait_for_function("document.querySelectorAll('.perm-chk[data-mod=\"Influencers\"]:checked').length === 6")
    assert page.is_checked('.perm-all[data-mod="Influencers"]')                            # the row "all" box is restored as well
    assert not page.is_checked('.perm-all[data-mod="Accounts"]')                           # other rows are not
    assert log["errors"] == []


def test_matrix_has_a_row_for_every_module_the_backend_knows(browser, world):
    page, _ = _open(browser)
    page.evaluate("rbacShowRoleModal()")
    rows = page.eval_on_selector_all("#rbac_perm_matrix tr td.fw-bold", "e => e.map(x => x.innerText.trim())")
    from routers.dashboard_auth import ALL_MODULES
    assert rows == ALL_MODULES and "Influencers" in rows


# ── Issue 2 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_refresh_fetches_the_latest_roles_from_the_server_and_reports_it(browser, world):
    page, log = _open(browser)
    reqs = []
    page.on("request", lambda r: reqs.append(r.url) if "/admin/auth/roles" in r.url else None)
    _roles(page)
    n0 = len(reqs)
    assert "Night Shift" not in page.inner_text("#rbac_roles_table")
    fake.dashboard_roles.insert_one({"role_name": "Night Shift", "description": "added elsewhere", "permissions": {}, "is_system_role": False})
    page.click("#rbac_roles_refresh")
    page.wait_for_function("document.getElementById('rbac_roles_msg').innerText.startsWith('\\u2713 Refreshed')")
    assert len(reqs) == n0 + 1 and "Night Shift" in page.inner_text("#rbac_roles_table")
    assert "2 roles" in page.inner_text("#rbac_roles_msg")
    assert not page.is_disabled("#rbac_roles_refresh") and page.inner_text("#rbac_roles_refresh").strip() == "Refresh"
    # permissions changed in the database are picked up on the next click, not served from a cache
    fake.dashboard_roles.update_one({"role_name": "Night Shift"}, {"$set": {"description": "changed"}})
    page.click("#rbac_roles_refresh")
    page.wait_for_function("document.getElementById('rbac_roles_table').innerText.includes('changed')")
    assert any("_=" in u for u in reqs[-2:])                                            # cache-busted
    assert log["errors"] == []


def test_refresh_shows_a_loading_state_then_gives_the_button_back(browser, world):
    page, _ = _open(browser)
    _roles(page)
    page.route("**/admin/auth/roles?*", lambda r: (page.wait_for_timeout(700), r.continue_()))
    page.click("#rbac_roles_refresh")
    assert page.is_disabled("#rbac_roles_refresh") and "Refreshing" in page.inner_text("#rbac_roles_refresh")
    assert "Refreshing" in page.inner_text("#rbac_roles_msg")
    page.wait_for_function("!document.getElementById('rbac_roles_refresh').disabled")
    assert page.inner_text("#rbac_roles_refresh").strip() == "Refresh"


def test_refresh_failure_is_reported_keeps_the_list_and_the_button_works_again(browser, world):
    page, _ = _open(browser)
    _roles(page)
    before = page.inner_text("#rbac_roles_table")
    page.route("**/admin/auth/roles?*", lambda r: r.fulfill(status=500, body='{"detail":"database busy"}', headers={"content-type": "application/json"}))
    page.click("#rbac_roles_refresh")
    page.wait_for_function("document.getElementById('rbac_roles_msg').innerText.includes('Could not refresh')")
    assert "database busy" in page.inner_text("#rbac_roles_msg") and page.inner_text("#rbac_roles_table") == before
    assert not page.is_disabled("#rbac_roles_refresh")
    page.unroute("**/admin/auth/roles?*")
    page.route("**/admin/auth/roles?*", lambda r: r.abort())                           # network failure
    page.click("#rbac_roles_refresh")
    page.wait_for_function("document.getElementById('rbac_roles_msg').innerText.includes('Could not refresh')")
    assert not page.is_disabled("#rbac_roles_refresh")


def test_refresh_also_rereads_the_signed_in_users_own_permissions(browser, world):
    page, _ = _open(browser)
    _roles(page)
    me = []
    page.on("request", lambda r: me.append(r.url) if r.url.endswith("/admin/auth/me") else None)
    page.click("#rbac_roles_refresh")
    page.wait_for_function("document.getElementById('rbac_roles_msg').innerText.startsWith('\\u2713')")
    page.wait_for_timeout(300)
    assert len(me) == 1


def test_access_denied_still_shows_the_denied_panel(browser, world):
    page, _ = _open(browser)
    _roles(page)
    page.route("**/admin/auth/roles?*", lambda r: r.fulfill(status=403, body='{"detail":"no"}', headers={"content-type": "application/json"}))
    page.click("#rbac_roles_refresh")
    page.wait_for_function("document.getElementById('rbac_roles_table').innerText.includes('Access Denied')")
    assert not page.is_disabled("#rbac_roles_refresh")


# ── Issue 3 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def acct(world, monkeypatch):
    sent = []
    monkeypatch.setattr(WA, "send_otp_message", lambda phone, otp: (sent.append((phone, otp)) or {"ok": True}))
    for c in ("account_phone_changes", "security_audit"):
        fake[c].delete_many({})
    aid = fake.accounts.insert_one({"name": "D'Souza", "phone": "9876543210", "roles": ["user"], "status": "active"}).inserted_id
    return {"id": str(aid), "sent": sent}


def _open_edit(page, acct):
    page.evaluate("showSection('accounts', null)")
    page.wait_for_selector("#accounts_table tr td")
    page.evaluate(f"openAccountDetail('{acct['id']}', 'x')")
    page.click("#acctDetailBody button:has-text('Edit')")
    page.wait_for_selector("#ae_name")


def test_edit_dialog_is_typeable_while_the_account_modal_is_open(browser, acct):
    page, log = _open(browser)
    page.evaluate(FOCUS_TRAP)
    _open_edit(page, acct)
    assert page.evaluate("document.getElementById('accountDetailModal').contains(document.getElementById('acctEditOverlay'))")
    assert page.input_value("#ae_name") == "D'Souza"                                      # no stray backslash from double escaping
    page.click("#ae_name", click_count=3)
    page.keyboard.type("Ravi Kumar")
    assert page.input_value("#ae_name") == "Ravi Kumar"
    page.click("#ae_name_btn")
    page.wait_for_function("document.getElementById('ae_msg').textContent === 'Name updated.'")
    assert fake.accounts.find_one({"_id": ObjectId(acct["id"])})["name"] == "Ravi Kumar"
    assert [a["outcome"] for a in fake.security_audit.find({"action": "account.name_change"})] == ["started", "ok"]
    assert log["errors"] == []


def test_name_with_an_apostrophe_is_saved_exactly_as_typed(browser, acct):
    page, _ = _open(browser)
    page.evaluate(FOCUS_TRAP)
    _open_edit(page, acct)
    page.click("#ae_name", click_count=3)
    page.keyboard.type("O'Brien Dsouza")
    page.click("#ae_name_btn")
    page.wait_for_function("document.getElementById('ae_msg').textContent === 'Name updated.'")
    assert fake.accounts.find_one({"_id": ObjectId(acct["id"])})["name"] == "O'Brien Dsouza"


def test_mobile_change_can_be_started_typed_and_the_old_number_stays_active(browser, acct):
    page, _ = _open(browser)
    page.evaluate(FOCUS_TRAP)
    _open_edit(page, acct)
    page.select_option("#ae_country", "NP")
    page.click("#ae_phone")
    page.keyboard.type("9812345678")
    assert page.input_value("#ae_phone") == "9812345678"
    page.click("#ae_req_btn")
    page.wait_for_selector("#ae_code")                                                    # now waiting for the WhatsApp code
    assert "Code sent on WhatsApp" in page.inner_text("#ae_msg") and len(acct["sent"]) == 1
    assert fake.accounts.find_one({"_id": ObjectId(acct["id"])})["phone"] == "9876543210"  # old number untouched until verified
    page.click("#ae_code")
    page.keyboard.type(acct["sent"][-1][1])
    page.click("#ae_verify")
    page.wait_for_function("!document.getElementById('acctEditOverlay')")
    assert fake.accounts.find_one({"_id": ObjectId(acct["id"])})["phone"].endswith("9812345678")


def test_invalid_and_duplicate_numbers_are_still_refused_from_the_dialog(browser, acct):
    fake.accounts.insert_one({"name": "Other", "phone": "9000011111", "roles": ["user"], "status": "active"})
    page, _ = _open(browser)
    page.evaluate(FOCUS_TRAP)
    _open_edit(page, acct)
    page.select_option("#ae_country", "IN")
    page.click("#ae_phone"); page.keyboard.type("9000011111")
    page.click("#ae_req_btn")
    page.wait_for_function("document.getElementById('ae_msg').textContent.includes('already assigned')")
    page.fill("#ae_phone", "123")
    page.click("#ae_req_btn")
    page.wait_for_function("document.getElementById('ae_msg').textContent.includes('valid mobile')")
    assert acct["sent"] == [] and fake.accounts.find_one({"_id": ObjectId(acct["id"])})["phone"] == "9876543210"


def test_edit_dialog_opened_without_the_modal_still_works_on_the_page_body(browser, acct):
    page, _ = _open(browser)
    page.evaluate(FOCUS_TRAP)
    page.evaluate(f"openAccountEdit('{acct['id']}', 'Ravi')")
    page.wait_for_selector("#ae_name")
    assert page.evaluate("document.getElementById('acctEditOverlay').parentElement === document.body")
    page.click("#ae_name", click_count=3); page.keyboard.type("Ravi K")
    assert page.input_value("#ae_name") == "Ravi K"


DIST = os.environ.get("OFFRO_BOOTSTRAP_DIST", "")


@pytest.mark.skipif(not DIST or not os.path.exists(os.path.join(DIST, "js", "bootstrap.bundle.min.js")), reason="set OFFRO_BOOTSTRAP_DIST to a bootstrap 5 dist folder")
def test_edit_dialog_is_typeable_with_the_real_bootstrap_bundle(browser, acct):
    tc = TestClient(server.app); tc.cookies.set("admin_token", "tk")
    html = tc.get("/admin/dashboard").text
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    page = ctx.new_page()

    def route(r):
        req, url = r.request, r.request.url
        if "bootstrap.bundle.min.js" in url:
            return r.fulfill(status=200, body=open(os.path.join(DIST, "js", "bootstrap.bundle.min.js")).read(), content_type="application/javascript")
        if "bootstrap.min.css" in url:
            return r.fulfill(status=200, body=open(os.path.join(DIST, "css", "bootstrap.min.css")).read(), content_type="text/css")
        if not url.startswith(BASE):
            return r.fulfill(status=200, body="", content_type="text/css" if url.endswith(".css") else "application/javascript")
        path = url[len(BASE):]
        if path.split("?")[0] == "/admin/dashboard":
            return r.fulfill(status=200, body=html, headers={"content-type": "text/html; charset=utf-8"})
        body = req.post_data
        resp = tc.request(req.method, path, content=body, headers={"content-type": req.headers.get("content-type", "application/json")} if body else None)
        return r.fulfill(status=resp.status_code, body=resp.content, headers={"content-type": resp.headers.get("content-type", "application/json")})
    ctx.route("**/*", route)
    page.goto(BASE + "/admin/dashboard")
    page.wait_for_function("window._rbacMe !== null && window._rbacMe !== undefined", timeout=15000)
    _open_edit(page, acct)
    page.click("#ae_name", click_count=3)
    page.keyboard.type("Ravi Kumar")
    assert page.input_value("#ae_name") == "Ravi Kumar"
    page.click("#ae_name_btn")
    page.wait_for_function("document.getElementById('ae_msg').textContent === 'Name updated.'")
    ctx.close()
