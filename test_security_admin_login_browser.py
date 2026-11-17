"""Regression tests that run the REAL login page in a headless Chromium against a stand-in for the MSG91 SDK.

Why: the page once defined its own global function `verifyOtp()`. The MSG91 SDK (exposeMethods) installs `window.verifyOtp`, so the "Verify & Login"
button called the SDK's verifyOtp() with NO argument -> "OTP not provided in verifyOtp() method" and nothing happened. Static text checks could not catch
that; running the page does. The page is served with the real Content-Security-Policy header so CSP violations show up as failures too.
Skipped automatically when Playwright / Chromium are not installed (the rest of the suite does not need them).
Run:  python -m pytest test_security_admin_login_browser.py -q -W ignore"""
import json
import time

import pytest
from fastapi.testclient import TestClient

playwright_sync = pytest.importorskip("playwright.sync_api")

import server  # noqa: E402

PAGE_URL = "http://admin.test/admin"
SDK_STUB = """
// Stand-in for https://verify.msg91.com/otp-provider.js. Like the real SDK it INSTALLS window.sendOtp / retryOtp / verifyOtp when initSendOTP runs,
// and like the real SDK its verifyOtp rejects with this exact error when no OTP is passed.
window.__sdk = {sent: [], retried: 0, verified: [], config: null};
window.initSendOTP = function (cfg) {
  window.__sdk.config = {widgetId: cfg.widgetId, tokenAuth: cfg.tokenAuth, exposeMethods: cfg.exposeMethods};
  window.sendOtp = function (id, ok, fail) { window.__sdk.sent.push(id); ok && ok({type: 'success', message: 'req-1'}); };
  window.retryOtp = function (ch, ok, fail) { window.__sdk.retried++; ok && ok({type: 'success'}); };
  window.verifyOtp = function (otp, ok, fail) {
    if (otp === undefined || otp === null || otp === '') { return Promise.reject(new Error('OTP not provided in verifyOtp() method.')); }
    window.__sdk.verified.push(String(otp));
    if (String(otp) === window.__goodOtp) ok && ok({type: 'success', message: 'eyJhbGciOiJIUzI1NiJ9.eyJpZGVudGlmaWVyIjoiOTE5MDAwMDAwMjAwIn0.sig'});
    else fail && fail({type: 'error', message: 'Invalid OTP'});
  };
};
"""


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                       # no browser binary in this environment
            pytest.skip("Chromium not available: %s" % e)
        yield b
        b.close()


def _open(browser, monkeypatch, good_otp="4821", verify_status=200):
    """Open the real page with API calls and the MSG91 SDK stubbed. Returns (page, log) where log collects requests, console problems and CSP violations."""
    monkeypatch.setenv("MSG91_WEB_WIDGET_ID", "webwid123")
    monkeypatch.setenv("MSG91_WEB_WIDGET_TOKEN_AUTH", "public-web-token")
    monkeypatch.setenv("MSG91_AUTH_KEY", "k")
    real = TestClient(server.app).get("/admin")           # the real HTML and the real CSP header
    assert real.status_code == 200
    html, csp = real.text, real.headers["content-security-policy"]
    log = {"posts": [], "console": [], "errors": []}
    ctx = browser.new_context()
    page = ctx.new_page()
    page.on("console", lambda m: log["console"].append(m.text))
    page.on("pageerror", lambda e: log["errors"].append(str(e)))

    def route(r):
        url, req = r.request.url, r.request
        if url == PAGE_URL:
            return r.fulfill(status=200, body=html, headers={"content-type": "text/html; charset=utf-8", "content-security-policy": csp})
        if url == "https://verify.msg91.com/otp-provider.js":
            return r.fulfill(status=200, body=SDK_STUB, content_type="application/javascript")
        if url.startswith("http://admin.test/admin/auth/"):
            name = url.rsplit("/", 1)[1]
            if req.method != "POST":                      # e.g. the page's device-status probe
                return r.fulfill(status=200, json={"trusted": False})
            body = json.loads(req.post_data or "{}")
            log["posts"].append((name, body))
            out = {"login": {"otp_required": True, "challenge_id": "cid-1", "otp_length": 4, "mobile_masked": "******0200", "otp_expires_in": 300,
                             "resend_available_in": 30, "provider": "widget"},
                   "otp-request": {"identifier": "919000000200", "widget": {"widget_id": "webwid123", "token_auth": "public-web-token"},
                                   "first": not any(n == "otp-request" for n, _ in log["posts"][:-1]), "otp_expires_in": 300, "resend_available_in": 30},
                   "verify-otp": {"success": True, "user": {"mobile": "9000000200"}, "device_trusted": False},
                   "otp-failed": {"remaining": 4}}.get(name, {})
            status = verify_status if name == "verify-otp" else 200
            return r.fulfill(status=status, json=out if status == 200 else {"detail": "x"})
        return r.abort()                                  # CDNs, logo: not needed
    ctx.route("**/*", route)
    page.goto(PAGE_URL)
    page.evaluate("g => { window.__goodOtp = g; }", good_otp)
    return page, log


def _wait(page, expr, seconds=5.0):
    """Poll a JS expression from Python. (page.wait_for_function evaluates a string in the page, which the page's own CSP, correctly, forbids.)"""
    end = time.time() + seconds
    while time.time() < end:
        if page.evaluate(expr):
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for: " + expr)


def _to_otp_screen(page):
    page.fill("#mobile", "9000000200")
    page.fill("#pin", "4321")
    page.click("#loginBtn")
    page.wait_for_selector(".otp-box", state="visible")
    _wait(page, "window.__sdk && window.__sdk.sent.length === 1")


def _type_without_autosubmit(page, digits):
    page.evaluate("d => document.querySelectorAll('.otp-box').forEach((b, i) => { b.value = d[i] || ''; })", digits)


def _csp_problems(log):
    return [m for m in log["console"] if "Content Security Policy" in m or "Refused to" in m]


def test_the_page_does_not_define_functions_that_collide_with_the_sdk_methods(browser, monkeypatch):
    page, log = _open(browser, monkeypatch)
    for name in ("sendOtp", "retryOtp", "verifyOtp", "getWidgetData", "isCaptchaVerified"):
        assert page.evaluate("n => typeof window[n]", name) == "undefined", name       # before the SDK loads, none of these names exists on the page


def test_the_verify_button_hands_the_typed_digits_to_the_sdk_verify_otp(browser, monkeypatch):
    page, log = _open(browser, monkeypatch)
    _to_otp_screen(page)
    _type_without_autosubmit(page, "4821")
    page.click("#verifyBtn")
    _wait(page, "window.__sdk.verified.length === 1")
    assert page.evaluate("window.__sdk.verified") == ["4821"]                          # a non-empty STRING of the four digits
    assert not [e for e in log["errors"] if "OTP not provided" in e]
    verify_posts = [b for n, b in log["posts"] if n == "verify-otp"]
    assert len(verify_posts) == 1
    assert verify_posts[0]["challenge_id"] == "cid-1" and verify_posts[0]["access_token"].startswith("eyJ")
    assert "otp" not in verify_posts[0] and "verified" not in verify_posts[0]           # the server gets the MSG91 token only, never the code


def test_entering_the_fourth_digit_submits_once_with_the_code(browser, monkeypatch):
    page, log = _open(browser, monkeypatch)
    _to_otp_screen(page)
    for i, d in enumerate("4821"):
        page.fill(".otp-box[data-index='%d']" % i, d)
    _wait(page, "window.__sdk.verified.length >= 1")
    assert page.evaluate("window.__sdk.verified") == ["4821"] and not log["errors"]


def test_pressing_enter_in_a_box_submits_the_code(browser, monkeypatch):
    page, log = _open(browser, monkeypatch)
    _to_otp_screen(page)
    _type_without_autosubmit(page, "4821")
    page.focus(".otp-box[data-index='3']")
    page.keyboard.press("Enter")
    _wait(page, "window.__sdk.verified.length === 1")
    assert page.evaluate("window.__sdk.verified") == ["4821"]


def test_an_incomplete_code_never_reaches_the_sdk(browser, monkeypatch):
    page, log = _open(browser, monkeypatch)
    _to_otp_screen(page)
    _type_without_autosubmit(page, "48")
    page.click("#verifyBtn")
    assert "Please enter all 4 digits" in page.inner_text("#otpMsg")
    assert page.evaluate("window.__sdk.verified") == [] and not [n for n, _ in log["posts"] if n == "verify-otp"]


def test_a_wrong_code_is_reported_for_attempt_counting_and_sends_no_token(browser, monkeypatch):
    page, log = _open(browser, monkeypatch, good_otp="9999")
    _to_otp_screen(page)
    _type_without_autosubmit(page, "4821")
    page.click("#verifyBtn")
    _wait(page, "document.getElementById('otpMsg').innerText.includes('incorrect')")
    assert "Attempts left: 4" in page.inner_text("#otpMsg")
    names = [n for n, _ in log["posts"]]
    assert "otp-failed" in names and "verify-otp" not in names


def test_resend_uses_the_sdk_retry_otp_with_the_null_channel(browser, monkeypatch):
    page, log = _open(browser, monkeypatch)
    _to_otp_screen(page)
    page.evaluate("document.getElementById('resendBtn').disabled = false")
    page.click("#resendBtn")
    _wait(page, "window.__sdk.retried === 1")
    assert [b for n, b in log["posts"] if n == "otp-request"] == [{"challenge_id": "cid-1"}] * 2


def test_the_sdk_is_configured_from_the_servers_public_settings_only(browser, monkeypatch):
    page, log = _open(browser, monkeypatch)
    _to_otp_screen(page)
    cfg = page.evaluate("window.__sdk.config")
    assert cfg == {"widgetId": "webwid123", "tokenAuth": "public-web-token", "exposeMethods": True}
    assert page.evaluate("window.__sdk.sent") == ["919000000200"]


def test_the_page_runs_under_its_real_csp_without_violations(browser, monkeypatch):
    page, log = _open(browser, monkeypatch)
    _to_otp_screen(page)
    _type_without_autosubmit(page, "4821")
    page.click("#verifyBtn")
    _wait(page, "window.__sdk.verified.length === 1")
    own = [m for m in _csp_problems(log) if "msg91" in m.lower() or "inline" in m.lower() or "eval" in m.lower()]
    assert own == [], own                                  # CDN/logo requests are aborted by this harness and are not counted
