"""Sidebar grouping: runs the REAL admin dashboard in headless Chromium. UI only - every link, handler and permission rule is unchanged.
Run:  python -m pytest test_sidebar_groups_browser.py -q -W ignore"""
import re
import sys

import pytest
from fastapi.testclient import TestClient

import server
from test_deals_admin_browser import BASE, STUB, _open, browser  # noqa: F401  (fixture + helper)

fake = sys.modules["database"].db
ORIG = "templates/admin_dashboard.html"
ALL = ["overview", "accounts", "stores", "influencers", "product_cards", "categories", "deals", "cities", "promo_sliders", "admin_banners",
       "popup_campaigns", "social", "wa_campaigns", "notifications", "merchant_txns", "gift_product_requests", "pricing_plans", "discounts",
       "reviews", "wa_chat", "terms", "policies", "about", "translations", "default_images", "rbac_users", "rbac_roles", "rbac_logs"]
GROUPS = {"store": ["accounts", "stores", "influencers", "product_cards", "categories", "deals", "cities"],
          "marketing": ["promo_sliders", "admin_banners", "popup_campaigns", "social", "wa_campaigns", "notifications"],
          "finance": ["merchant_txns", "gift_product_requests", "pricing_plans", "discounts"],
          "engagement": ["reviews", "wa_chat"],
          "content": ["terms", "policies", "about", "translations", "default_images"],
          "admin": ["rbac_users", "rbac_roles", "rbac_logs"]}


@pytest.fixture
def world():
    for c in ("dashboard_users", "dashboard_roles"):
        fake[c].delete_many({})
    role = fake.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    fake.dashboard_users.insert_one({"token": "tk", "status": "active", "full_name": "Admin", "mobile": "9000000001", "role_id": role, "assigned_cities": ["*"]})


def _html():
    tc = TestClient(server.app); tc.cookies.set("admin_token", "tk")
    return tc.get("/admin/dashboard").text


def _links(html):
    side = html[html.index('<div class="sidebar"'):html.index('<div class="main">')]
    return re.findall(r'<a [^>]*onclick="([^"]+)"[^>]*>(.*?)</a>', side, re.S)


# ── static: nothing removed, renamed or re-wired ─────────────────────────────────────────────────────────────────────
def test_every_original_link_is_kept_unchanged_in_a_group(world):
    import os
    path = os.environ.get("SIDEBAR_ORIG_HTML", "")        # the pre-change template (git show origin/staging:templates/admin_dashboard.html > file)
    if not path or not os.path.exists(path):
        pytest.skip("set SIDEBAR_ORIG_HTML to the pre-change template to run this comparison")
    orig = open(path, encoding="utf-8").read()
    assert sorted(_links(orig)) == sorted(_links(_html()))              # same handler, same icon, same label, nothing added or dropped
    names = [re.search(r"showSection\('([^']+)'", o).group(1) for o, _ in _links(_html()) if "showSection" in o]
    assert sorted(names) == sorted(ALL)
    assert any("logout()" in o for o, _ in _links(_html()))


def test_markup_structure(world):
    h = _html()
    assert h.count('class="nav-group"') == 6 and h.count('class="nav-group-head"') == 6 and 'id="sidebar"' in h
    for key, names in GROUPS.items():
        body = h[h.index('id="nav-body-%s"' % key):]
        body = body[:body.index("</div></div>")]
        assert [re.search(r"showSection\('([^']+)'", x).group(1) for x in re.findall(r'onclick="(showSection[^"]+)"', body)] == names
    side = h[h.index('<div class="sidebar"'):h.index('<div class="main">')]
    assert side.index("showSection('overview'") < side.index('data-group="store"') and side.rindex('data-group=') < side.index('class="logout"')


# ── behaviour ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_super_admin_sees_everything_and_every_link_opens_its_page(browser, world):
    page, log = _open(browser)
    for key in GROUPS:
        if page.get_attribute(f'.nav-group[data-group="{key}"] .nav-group-head', "aria-expanded") != "true":
            page.click(f'.nav-group[data-group="{key}"] .nav-group-head')
    for name in ALL:
        a = page.locator(f'.sidebar a[onclick*="showSection(\'{name}\',"]')
        assert a.is_visible(), name
        a.click()
        assert page.evaluate(f"document.getElementById('section-{name}').classList.contains('active')"), name
        assert "active" in (a.get_attribute("class") or ""), name                       # existing active highlight kept
    assert [e for e in log["errors"] if "Failed to fetch" not in e] == [], log["errors"]


def test_toggle_and_only_the_active_group_is_forced_open(browser, world):
    page, _ = _open(browser)
    head = '.nav-group[data-group="finance"] .nav-group-head'
    assert page.get_attribute(head, "aria-expanded") == "false"
    assert not page.locator('.sidebar a[onclick*="merchant_txns"]').is_visible()
    page.click(head)
    assert page.get_attribute(head, "aria-expanded") == "true"
    page.wait_for_timeout(300)
    assert page.locator('.sidebar a[onclick*="merchant_txns"]').is_visible()
    page.click(head)
    assert page.get_attribute(head, "aria-expanded") == "false"
    page.evaluate("showSection('reviews', null)")                                       # no link element passed: group still opens
    assert page.get_attribute('.nav-group[data-group="engagement"] .nav-group-head', "aria-expanded") == "true"
    assert page.get_attribute('.nav-group[data-group="marketing"] .nav-group-head', "aria-expanded") == "false"


def test_state_is_remembered_across_reload(browser, world):
    page, _ = _open(browser)
    page.click('.nav-group[data-group="store"] .nav-group-head')                       # default open -> closed
    page.click('.nav-group[data-group="marketing"] .nav-group-head')                   # closed -> open
    page.reload(); page.wait_for_function("window._rbacMe !== null && window._rbacMe !== undefined")
    assert page.get_attribute('.nav-group[data-group="store"] .nav-group-head', "aria-expanded") == "false"
    assert page.get_attribute('.nav-group[data-group="marketing"] .nav-group-head', "aria-expanded") == "true"


def test_typography_and_colours(browser, world):
    page, _ = _open(browser)
    head = page.eval_on_selector(".nav-group-head", "e => {const s=getComputedStyle(e); return [s.fontWeight, s.fontSize, s.color]}")
    item = page.eval_on_selector(".nav-group-inner a", "e => getComputedStyle(e).fontSize")
    assert head == ["700", "14.5px", "rgb(255, 255, 255)"] and item == "12.5px"
    assert page.eval_on_selector(".sidebar", "e => getComputedStyle(e).backgroundColor") == "rgb(62, 95, 85)"
    assert page.get_attribute(".sidebar-logo img", "style").startswith("height:38px")


def test_sidebar_scrolls_on_its_own_and_works_on_a_phone(browser, world):
    page, _ = _open(browser)
    for key in GROUPS:
        if page.get_attribute(f'.nav-group[data-group="{key}"] .nav-group-head', "aria-expanded") != "true":
            page.click(f'.nav-group[data-group="{key}"] .nav-group-head')
    page.set_viewport_size({"width": 1400, "height": 420})
    page.wait_for_timeout(300)
    m = page.eval_on_selector(".sidebar", "e => [e.scrollHeight > e.clientHeight, getComputedStyle(e).overflowY, getComputedStyle(e).position]")
    assert m == [True, "auto", "fixed"]
    page.eval_on_selector(".sidebar", "e => e.scrollTop = e.scrollHeight")
    assert page.eval_on_selector(".sidebar", "e => e.scrollTop") > 0 and page.evaluate("window.scrollY") == 0
    assert page.locator(".sidebar a.logout").is_visible() or page.eval_on_selector(".sidebar a.logout", "e => e.getBoundingClientRect().bottom") > 0
    page.set_viewport_size({"width": 390, "height": 800})
    page.wait_for_timeout(300)
    assert page.eval_on_selector(".sidebar", "e => e.getBoundingClientRect().right") <= 1
    page.click(".menu-toggle")
    assert "open" in page.get_attribute(".sidebar", "class")
    assert page.get_attribute('.nav-group[data-group="finance"] .nav-group-head', "aria-expanded") == "true"   # still open from above
    page.click('.sidebar a[onclick*="merchant_txns"]')
    assert "open" not in page.get_attribute(".sidebar", "class")                      # drawer closes after choosing a page
    assert page.evaluate("document.getElementById('section-merchant_txns').classList.contains('active')")


def test_restricted_role_sees_only_permitted_links_and_empty_groups_disappear(browser):
    for c in ("dashboard_users", "dashboard_roles"):
        fake[c].delete_many({})
    role = fake.dashboard_roles.insert_one({"role_name": "Limited", "permissions": {
        "Accounts": {"view": True}, "Stores": {"view": True}, "Payments": {"view": True}}}).inserted_id
    fake.dashboard_users.insert_one({"token": "tk", "status": "active", "full_name": "Lim", "mobile": "9000000002", "role_id": role, "assigned_cities": ["*"]})
    page, _ = _open(browser)
    for key in GROUPS:
        if page.get_attribute(f'.nav-group[data-group="{key}"] .nav-group-head', "aria-expanded") != "true" and page.locator(f'.nav-group[data-group="{key}"]').is_visible():
            page.click(f'.nav-group[data-group="{key}"] .nav-group-head')
    page.wait_for_timeout(300)
    vis = page.eval_on_selector_all(".sidebar a[onclick]", "e => e.filter(a => a.style.display !== 'none').map(a => a.getAttribute('onclick'))")
    names = sorted(re.search(r"showSection\('([^']+)'", v).group(1) for v in vis if "showSection" in v)
    shown = page.eval_on_selector_all(".nav-group", "e => e.filter(g => !g.classList.contains('nav-empty')).map(g => g.dataset.group)")
    assert "accounts" in names and "stores" in names and "merchant_txns" in names
    assert not {"rbac_users", "rbac_roles", "rbac_logs", "notifications", "wa_campaigns", "reviews"} & set(names)
    assert set(shown) == {"store", "finance"}                                          # groups with no permitted link are gone
    assert page.locator(".sidebar a.logout").is_visible()
