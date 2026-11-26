"""Payments > Archived Pending Transactions tab, in the REAL admin dashboard (headless Chromium).
Run: python -m pytest test_pending_archive_browser.py -q -W ignore"""
import sys
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from test_deals_admin_browser import _open, browser  # noqa: F401  (fixture + helper)

fake = sys.modules["database"].db
OLD = datetime.utcnow() - timedelta(days=120)


def _seed(perms=None, role="Super Admin"):
    for n in ("dashboard_users", "dashboard_roles", "subscriptions", "subscriptions_archive", "accounts", "stores", "security_audit", "invoices"):
        fake[n].delete_many({})
    rid = fake.dashboard_roles.insert_one({"role_name": role, "permissions": perms or {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "tk", "status": "active", "full_name": "Admin", "mobile": "9000000001", "role_id": rid, "assigned_cities": ["*"]})
    m1 = fake.accounts.insert_one({"name": "Ravi", "phone": "+919812345678", "phone_country": "IN", "roles": ["merchant"]}).inserted_id
    m2 = fake.accounts.insert_one({"name": "Sara", "phone": "+966501234567", "phone_country": "SA", "roles": ["merchant"]}).inserted_id
    base = {"plan": "monthly", "total": 100, "status": "pending", "pay_mode": "manual", "created_at": OLD}
    ids = [fake.subscriptions_archive.insert_one({**base, "merchant_id": str(m), "total": t, "archived_at": datetime.utcnow() - timedelta(days=i)}).inserted_id
           for i, (m, t) in enumerate([(m1, 11), (m1, 22), (m2, 33)])]
    live = fake.subscriptions.insert_one({**base, "merchant_id": str(m1), "created_at": datetime.utcnow(), "total": 77}).inserted_id
    return ids, live


@pytest.fixture
def world():
    return _seed()


def _tab(page):
    page.evaluate("showSection('merchant_txns', null)")
    page.wait_for_selector("#pay_tabs")
    page.click("#pay_tab_btn_archive")
    page.wait_for_selector("#arch_body .arch-cb")


def test_existing_payments_view_is_kept_and_archive_tab_is_separate(browser, world):
    page, log = _open(browser)
    page.evaluate("showSection('merchant_txns', null)")
    assert page.is_visible("#pay_pane_active") and not page.is_visible("#pay_pane_archive")
    for sel in ("#txn_search", "#txn_status_filter", "#txn_type_filter", "#txn_country_filter", "#txn_total_revenue", "#txn_body"):
        assert page.locator(sel).count() == 1, sel
    assert page.locator("button[onclick='exportPaymentsPDF()']").count() == 1 and page.locator("button[onclick='exportPaymentsCSV()']").count() == 1
    page.wait_for_function("document.getElementById('txn_total_revenue').textContent !== '--'")
    assert page.inner_text("#pay_tab_btn_archive").startswith("Archived Pending Transactions")
    _tab(page)
    assert page.is_visible("#pay_pane_archive") and not page.is_visible("#pay_pane_active")
    page.click("#pay_tab_btn_active")
    assert page.is_visible("#pay_pane_active") and not page.is_visible("#pay_pane_archive")
    assert [e for e in log["errors"] if "Failed to fetch" not in e] == []


def test_archive_lists_rows_with_count_and_checkboxes(browser, world):
    page, _ = _open(browser)
    _tab(page)
    assert page.locator("#arch_body .arch-cb").count() == 3
    assert page.inner_text("#arch_count").strip() == "3 archived" and "(3)" in page.inner_text("#arch_tab_count")
    assert "90 days" in page.inner_text("#arch_info") and "Razorpay" in page.inner_text("#arch_info")
    assert page.inner_text("#arch_body").count("Pending (archived)") == 3
    assert page.is_disabled("#arch_delete_btn") and page.inner_text("#arch_selected").strip() == "0 selected"


def test_selection_select_all_and_delete_button_state(browser, world):
    page, _ = _open(browser)
    _tab(page)
    boxes = page.locator("#arch_body .arch-cb")
    boxes.nth(0).check()
    assert page.inner_text("#arch_selected").strip() == "1 selected" and not page.is_disabled("#arch_delete_btn")
    assert page.evaluate("document.getElementById('arch_select_all').indeterminate") is True
    page.check("#arch_select_all")
    assert page.inner_text("#arch_selected").strip() == "3 selected" and all(boxes.nth(i).is_checked() for i in range(3))
    page.uncheck("#arch_select_all")
    assert page.inner_text("#arch_selected").strip() == "0 selected" and page.is_disabled("#arch_delete_btn")
    boxes.nth(1).check(); boxes.nth(1).uncheck()
    assert page.is_disabled("#arch_delete_btn")                                      # empty selection disables Delete Selected


def test_delete_asks_for_confirmation_and_cancel_deletes_nothing(browser, world):
    ids, live = world
    page, _ = _open(browser)
    _tab(page)
    page.evaluate("window.__confirmAnswer = false")
    page.locator("#arch_body .arch-cb").nth(0).check(); page.locator("#arch_body .arch-cb").nth(1).check()
    page.click("#arch_delete_btn")
    msg = page.evaluate("window.__confirms")[-1]
    assert "Permanently delete 2 archived transactions" in msg and "cannot be undone" in msg
    assert fake.subscriptions_archive.count_documents({}) == 3 and page.inner_text("#arch_selected").strip() == "2 selected"


def test_confirmed_delete_removes_only_selected_archived_rows_and_refreshes(browser, world):
    ids, live = world
    page, log = _open(browser)
    _tab(page)
    page.locator("#arch_body .arch-cb").nth(0).check()
    page.click("#arch_delete_btn")
    assert "Permanently delete 1 archived transaction?" in page.evaluate("window.__confirms")[-1]
    page.wait_for_function("document.querySelectorAll('#arch_body .arch-cb').length === 2")
    assert fake.subscriptions_archive.count_documents({}) == 2 and fake.subscriptions.count_documents({"_id": live}) == 1
    assert page.inner_text("#arch_count").strip() == "2 archived" and page.inner_text("#arch_selected").strip() == "0 selected"
    assert page.is_disabled("#arch_delete_btn")
    assert fake.security_audit.count_documents({"action": "payments.archive.delete", "outcome": "ok"}) == 1
    page.check("#arch_select_all"); page.click("#arch_delete_btn")                   # delete all that is left
    page.wait_for_function("document.querySelectorAll('#arch_body .arch-cb').length === 0")
    assert fake.subscriptions_archive.count_documents({}) == 0 and "No archived transactions" in page.inner_text("#arch_body")
    assert page.is_disabled("#arch_select_all") and fake.subscriptions.count_documents({}) == 1
    assert [e for e in log["errors"] if "Failed to fetch" not in e] == []


def test_search_date_and_country_filters(browser, world):
    page, _ = _open(browser)
    _tab(page)
    page.select_option("#arch_country", "SA")
    page.wait_for_function("document.querySelectorAll('#arch_body .arch-cb').length === 1")
    assert page.inner_text("#arch_count").strip() == "1 of 3 archived"
    page.select_option("#arch_country", "")
    page.wait_for_function("document.querySelectorAll('#arch_body .arch-cb').length === 3")
    page.fill("#arch_search", "ravi")
    page.wait_for_function("document.querySelectorAll('#arch_body .arch-cb').length === 2")
    page.fill("#arch_search", "")
    page.fill("#arch_from", "2099-01-01"); page.dispatch_event("#arch_from", "change")
    page.wait_for_function("document.querySelectorAll('#arch_body .arch-cb').length === 0")
    assert "No archived transactions" in page.inner_text("#arch_body")


def test_user_without_delete_permission_cannot_see_or_use_delete(browser):
    _seed({"Payments": {"view": True}}, role="Finance")
    page, _ = _open(browser)
    _tab(page)
    assert not page.is_visible("#arch_delete_btn")
    status = page.evaluate("""async () => (await fetch('/admin/payments/archive/delete', {method:'POST', credentials:'include',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify({ids: []})})).status""")
    assert status == 403 and fake.subscriptions_archive.count_documents({}) == 3
