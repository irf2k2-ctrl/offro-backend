"""Runs the REAL admin dashboard in headless Chromium against the real backend (mongomock) for the Deals module and the Accounts "Created" column.
CDN assets (Bootstrap/FontAwesome/jsPDF) are not fetched: a tiny Bootstrap-Modal stand-in is injected. Skipped when Chromium is unavailable.
Run:  python -m pytest test_deals_admin_browser.py -q -W ignore"""
import sys
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient

playwright_sync = pytest.importorskip("playwright.sync_api")
import server  # noqa: E402

fake = sys.modules["database"].db
BASE = "http://admin.test"
STUB = """
window.bootstrap = {Modal: class { constructor(el){this.el=el;} show(){this.el.classList.add('show');this.el.style.display='block';} hide(){this.el.classList.remove('show');this.el.style.display='none';} },
                    Tooltip: class {}, Toast: class {}, Collapse: class {}};
window.confirm = (m) => { window.__confirms = (window.__confirms||[]).concat([m]); return window.__confirmAnswer !== false; };
"""


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip("Chromium not available: %s" % e)
        yield b
        b.close()


@pytest.fixture
def world():
    role = fake.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "tk", "status": "active", "full_name": "Admin", "mobile": "9000000001", "role_id": role, "assigned_cities": ["*"]})
    m = fake.accounts.insert_one({"name": "Ravi Merchant", "phone": "9111111111", "roles": ["merchant"], "status": "active",
                                  "created_at": "2026-03-05T10:20:30"}).inserted_id
    fake.accounts.insert_one({"name": "Old Timer", "phone": "9333333333", "roles": ["user"], "status": "active"})   # no timestamp
    s = fake.stores.insert_one({"store_name": "Ravi Cafe", "city": "Pune", "status": "active", "merchant_id": str(m)}).inserted_id
    d = {"merchant_id": str(m), "store_id": str(s), "discount": 20, "category": "Food", "image_url": "",
         "start_date": "2026-01-01", "end_date": (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d"),
         "status": "active", "created_at": datetime(2026, 4, 1, 6, 30, 0)}
    fake.deals.insert_one({**d, "title": "Coffee 20", "description": "<b>x</b> & more"})
    fake.deals.insert_one({**d, "title": "Pizza", "description": "cheesy", "status": "paused"})
    return str(m)


def _open(browser):
    tc = TestClient(server.app)
    tc.cookies.set("admin_token", "tk")
    html = tc.get("/admin/dashboard").text
    log = {"errors": [], "console": []}
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    ctx.add_init_script(STUB)
    page = ctx.new_page()
    page.on("pageerror", lambda e: log["errors"].append(str(e)))
    page.on("console", lambda m: log["console"].append(m.text) if m.type == "error" else None)

    def route(r):
        req, url = r.request, r.request.url
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
    return page, log


def test_accounts_tab_shows_created_column_in_ist_with_safe_fallback_and_sorts(browser, world):
    page, log = _open(browser)
    page.evaluate("showSection('accounts', null)")
    page.wait_for_selector("#accounts_table tr td")
    heads = page.eval_on_selector_all("#section-accounts thead th", "e => e.map(x => x.innerText.trim())")
    assert heads[:1] == ["Name"] and any(h.startswith("Created") for h in heads) and len(heads) == 12
    rows = page.eval_on_selector_all("#accounts_table tr", "rs => rs.map(r => Array.from(r.cells).map(c => c.innerText.trim()))")
    assert all(len(r) == 12 for r in rows)
    ci = [h.startswith("Created") for h in heads].index(True)
    by = {r[0]: r[ci] for r in rows}
    assert by["Ravi Merchant"] == "05 Mar 2026, 03:50 pm".replace("pm", "pm") or by["Ravi Merchant"].lower() == "05 mar 2026, 03:50 pm"   # 10:20 UTC = 15:50 IST
    assert by["Old Timer"] == "—"
    page.click("#acct_created_th")
    page.wait_for_function("document.getElementById('acct_sort_ind').textContent === '▼'")
    page.wait_for_timeout(300)
    first = page.eval_on_selector_all("#accounts_table tr", "rs => rs.map(r => r.cells[0].innerText.trim())")
    assert first[0] == "Ravi Merchant" and first[-1] == "Old Timer"            # newest first, undated last
    page.click("#acct_created_th")
    page.wait_for_function("document.getElementById('acct_sort_ind').textContent === '▲'")
    assert log["errors"] == []


def test_deals_section_lists_filters_views_edits_toggles_and_deletes(browser, world):
    page, log = _open(browser)
    assert page.is_visible("a[onclick*=\"showSection('deals'\"]")
    page.click("a[onclick*=\"showSection('deals'\"]")
    page.wait_for_selector("#deals_table tr td")
    heads = page.eval_on_selector_all("#section-deals thead th", "e => e.map(x => x.innerText.trim())")
    assert heads == ["Image", "Deal", "Merchant", "Store", "Offer", "Start", "Expiry", "Status", "Created", "Actions"]
    text = page.inner_text("#deals_table")
    assert "Coffee 20" in text and "Pizza" in text and "Ravi Merchant" in text and "Ravi Cafe" in text and "20% off" in text
    assert "<b>" not in page.inner_html("#deals_table").replace("&lt;b&gt;", "")      # description is escaped, not injected
    # filter by status
    page.select_option("#deal_status_filter", "inactive")
    page.wait_for_function("document.querySelectorAll('#deals_table tr').length === 1")
    assert "Pizza" in page.inner_text("#deals_table")
    page.select_option("#deal_status_filter", "")
    page.fill("#deal_search", "coffee")
    page.wait_for_function("document.querySelectorAll('#deals_table tr').length === 1 && document.getElementById('deals_table').innerText.includes('Coffee')")
    # view
    page.click("#deals_table button:has-text('View')")
    page.wait_for_selector("#dealModalBody table")
    assert "Ravi Merchant" in page.inner_text("#dealModalBody") and "Coffee 20" in page.inner_text("#dealModalBody")
    # edit
    page.click("#deals_table button:has-text('Edit')")
    page.wait_for_selector("#de_title")
    page.fill("#de_title", "Coffee 35")
    page.fill("#de_disc", "35")
    page.click("#de_save")
    page.wait_for_function("document.getElementById('deals_table').innerText.includes('Coffee 35')")
    d = fake.deals.find_one({"title": "Coffee 35"})
    assert d and d["discount"] == 35 and fake.deals.count_documents({}) == 2
    # invalid edit shows the message and leaves the modal open
    page.click("#deals_table button:has-text('Edit')")
    page.wait_for_selector("#de_title")
    page.fill("#de_disc", "500")
    page.click("#de_save")
    page.wait_for_function("document.getElementById('de_err').textContent.length > 0")
    assert fake.deals.find_one({"title": "Coffee 35"})["discount"] == 35
    page.evaluate("document.getElementById('dealModal').style.display='none'")
    # deactivate (with confirmation)
    page.click("#deals_table button:has-text('Deactivate')")
    page.wait_for_function("document.getElementById('deals_table').innerText.includes('Inactive')")
    assert fake.deals.find_one({"title": "Coffee 35"})["status"] == "paused"
    # delete: cancelled -> nothing happens; confirmed -> gone
    page.evaluate("window.__confirmAnswer = false")
    page.click("#deals_table button:has-text('Delete')")
    page.wait_for_timeout(300)
    assert fake.deals.count_documents({}) == 2
    assert "cannot be undone" in page.evaluate("window.__confirms.slice(-1)[0]")
    page.evaluate("window.__confirmAnswer = true")
    page.click("#deals_table button:has-text('Delete')")
    page.wait_for_function("document.getElementById('deals_table').innerText.includes('No deals found')")
    assert fake.deals.count_documents({"title": "Coffee 35"}) == 0 and fake.deals.count_documents({}) == 1
    assert log["errors"] == []


def test_deals_section_hidden_and_denied_without_the_module(browser, world):
    rid = fake.dashboard_roles.insert_one({"role_name": "Limited", "permissions": {"Stores": {"view": True}}}).inserted_id
    fake.dashboard_users.update_one({"token": "tk"}, {"$set": {"role_id": rid}})
    page, log = _open(browser)
    page.wait_for_timeout(500)
    assert not page.is_visible("a[onclick*=\"showSection('deals'\"]")
    assert not page.evaluate("document.getElementById('section-deals').classList.contains('active')")
    # even if the section is forced open, the API refuses and the table shows the denied state, never data
    page.evaluate("loadDeals()")
    page.wait_for_function("document.getElementById('deals_table').innerText.length > 0")
    assert "Coffee 20" not in page.inner_text("#deals_table")
