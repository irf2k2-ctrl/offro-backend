"""Admin dashboard in headless Chromium: 'NOT IN USE' watermark + Accounts-tab action button colours (styling only).
Run:  python -m pytest test_dashboard_watermark_buttons.py -q -W ignore   (skips when Chromium is unavailable)"""
import sys

from test_deals_admin_browser import _open, browser, world  # noqa: F401

fake = sys.modules["database"].db


def _accounts(world):
    fake.accounts.insert_one({"name": "Del Request", "phone": "9444444444", "roles": ["user"], "status": "delete_requested", "created_at": "2026-05-01T00:00:00"})


def test_watermark_text_placement_and_it_never_blocks_the_dashboard(browser, world):
    page, log = _open(browser)
    assert page.inner_text(".nu-watermark") == "NOT IN USE"
    st = page.eval_on_selector(".nu-watermark", "e => { const s = getComputedStyle(e); return {pe: s.pointerEvents, pos: s.position, z: +s.zIndex, op: s.color}; }")
    assert st["pe"] == "none" and st["pos"] == "fixed" and 0 < st["z"] < 1055      # above content, below Bootstrap modals
    assert page.eval_on_selector(".nu-watermark", "e => e.getAttribute('aria-hidden')") == "true"
    # a click at the very centre of the screen reaches the real page, not the watermark
    hit = page.evaluate("(() => { const e = document.elementFromPoint(innerWidth/2, innerHeight/2); return e ? e.className : ''; })()")
    assert "nu-watermark" not in str(hit)
    assert page.evaluate("document.querySelectorAll('.nu-watermark').length") == 1
    assert log["errors"] == []


def test_sidebar_navigation_and_controls_still_work_with_the_watermark(browser, world):
    page, log = _open(browser)
    page.click("a[onclick*=\"showSection('accounts'\"]")
    page.wait_for_selector("#accounts_table tr td")
    assert page.evaluate("document.getElementById('section-accounts').classList.contains('active')")
    assert log["errors"] == []


def test_accounts_buttons_use_distinct_semantic_colours_and_keep_labels_and_actions(browser, world):
    _accounts(world)
    page, log = _open(browser)
    page.evaluate("showSection('accounts', null)")
    page.wait_for_selector("#accounts_table tr td")
    rgb = lambda sel: page.eval_on_selector(sel, "e => getComputedStyle(e).backgroundColor")
    assert rgb("#accounts_table button.acct-btn-view") == "rgb(31, 111, 235)"          # View    - blue
    assert rgb("#accounts_table button.acct-btn-approve") == "rgb(25, 135, 84)"        # Approve - green
    assert rgb("#accounts_table button.acct-btn-reject") == "rgb(230, 126, 34)"        # Reject  - orange
    labels = page.eval_on_selector_all("#accounts_table button", "bs => bs.map(b => b.innerText.trim())")
    assert any(l.endswith("View") for l in labels) and "🗑 Approve Delete" in labels and "↩ Reject" in labels
    assert any("Block" in l for l in labels)                                              # untouched action keeps its own colour
    attrs = page.eval_on_selector_all("#accounts_table button.acct-btn-approve, #accounts_table button.acct-btn-reject", "bs => bs.map(b => b.getAttribute('onclick'))")
    assert any("'approve'" in a for a in attrs) and any("'reject'" in a for a in attrs)     # same handlers as before
    page.evaluate("window.__confirmAnswer = false")
    page.click("#accounts_table button.acct-btn-reject")
    assert "restore" in page.evaluate("window.__confirms.slice(-1)[0]")
    assert fake.accounts.find_one({"name": "Del Request"})["status"] == "delete_requested"
    assert log["errors"] == []
