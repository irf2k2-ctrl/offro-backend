"""Phase H: Translations dashboard tab. Static contract tests (wiring, RBAC, API calls exist, escaping, no production edit path) plus a
headless-browser check of the real JS against mocked API responses. Run:  python -m pytest test_translations_dashboard_ui.py -q -W ignore"""
import json
import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

import server
from routers import dashboard_auth as DA
from routers import translations_admin as TA
from translation_test_support import DB, add_ar, editor, publish, put, setup_env, super_admin, sync_fixture_keys, viewer, client as api

ROOT = os.path.dirname(os.path.abspath(server.__file__))
read = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()
JS, CSS, PARTIAL, DASH = read("static", "js", "translations.js"), read("static", "css", "translations.css"), read("templates", "partials", "translations.html"), read("templates", "admin_dashboard.html")
main_client = TestClient(server.app)


@pytest.fixture(autouse=True)
def staging(monkeypatch):
    setup_env(monkeypatch, "staging")
    sync_fixture_keys()


def _dash():
    DB.dashboard_users.insert_one({"token": "ui-token", "status": "active", "full_name": "Test Admin", "mobile": "9000000009"})
    r = main_client.get("/admin/dashboard", cookies={"admin_token": "ui-token"})
    assert r.status_code == 200
    return r.text


# ── wiring ───────────────────────────────────────────────────────────────────────────────────
def test_dashboard_renders_with_the_translations_module():
    html = _dash()
    assert 'id="section-translations"' in html and "{%" not in html and "{{" not in html
    assert "/static/css/translations.css" in html and "/static/js/translations.js" in html
    assert "showSection('translations',this)" in html and "Translations</a>" in html and "Translations.open()" in html


def test_static_assets_are_served_and_same_origin():
    for path, kind in (("/static/js/translations.js", "javascript"), ("/static/css/translations.css", "css")):
        r = main_client.get(path)
        assert r.status_code == 200 and kind in r.headers["content-type"]
    assert "<script" not in PARTIAL and not re.search(r"\son[a-z]+\s*=", PARTIAL)
    assert not re.findall(r"https?://", re.sub(r"http://www\.w3\.org", "", JS + CSS + PARTIAL))     # nothing external is loaded


def test_existing_dashboard_still_intact():
    for needle in ("showSection('wa_campaigns',this)", "showSection('rbac_users',this)", "function showSection(", "function _rbacCanView(", '{% include "partials/wa_campaigns.html" %}'):
        assert needle in DASH, needle


def test_rbac_knows_the_module_and_default_roles_get_no_access():
    assert '"Translations":      ["translations"]' in DASH and '"Deals","Translations"' in DASH
    assert "Translations" in DA.ALL_MODULES


def test_sidebar_entry_is_hidden_without_view_permission_by_the_existing_mechanism():
    # sidebar links are filtered by the section map, which now maps 'translations' to the Translations module's view permission
    assert re.search(r'"Translations":\s*\["translations"\]', DASH)
    m = re.search(r"function _rbacCanView\(.*?\n}\n", DASH, re.S)
    assert "perms[mod][\"view\"]" in m.group(0)


# ── every API call the UI makes exists on the backend with the right method ────────────────────
def _routes():
    out = {}
    for r in TA.router.routes:
        for m in r.methods:
            out[(m, "/admin" + r.path)] = r
    return out


def test_every_api_call_in_the_ui_exists_on_the_backend():
    routes = _routes()
    calls = []
    for m in re.finditer(r"api\('(/[a-z\-/]+)", JS):
        line = JS[m.start():JS.index("\n", m.start())]
        mm = re.search(r"method:\s*'([A-Z]+)'", line)
        calls.append((m.group(1), mm.group(1) if mm else None))
    assert len(calls) >= 12
    for path, method in calls:
        method = method or "GET"
        ok = any(m == method and (p == "/admin/translations" + path or p.startswith("/admin/translations" + path.rstrip("/") + "/{")) for (m, p) in routes)
        assert ok, (method, path)


def test_ui_never_offers_a_production_edit_or_direct_rollback():
    assert "isProd()" in JS
    for fn in ("canEdit() { return canWriteNonProd()", ):
        assert fn in JS
    # rollback / approve / add-language controls only render outside production
    assert "isSA() && canWriteNonProd()" in JS
    # publish in production only when an imported package is pending
    assert "(canWriteNonProd() || (isProd() && pend))" in JS


def test_output_is_escaped_and_no_dynamic_code():
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", JS, flags=re.S)
    assert "eval(" not in code and "document.write" not in code and "new Function" not in code
    assigns = re.findall(r"\.innerHTML\s*=", code)
    assert len(assigns) == 1                                                    # only inside setHtml
    assert "function esc(" in code and "&#39;" in code


def test_no_hardcoded_statistics():
    assert not re.search(r"\b\d{2,}\s*(%|translations|strings|languages)\b", PARTIAL)
    assert "sample" not in (JS + PARTIAL).lower()


# ── backend addition: last_updated ───────────────────────────────────────────────────────────
def test_languages_endpoint_reports_real_last_updated():
    sa = super_admin()
    add_ar(sa)
    r = api.get("/admin/translations/languages", headers=sa).json()["languages"][0]
    assert r["last_updated"] == {"app": None, "legal": None}
    put(sa, "loginTitle", "مرحبا")
    r = api.get("/admin/translations/languages", headers=sa).json()["languages"][0]
    assert r["last_updated"]["app"] and r["last_updated"]["legal"] is None
    publish(sa)
    r2 = api.get("/admin/translations/languages", headers=sa).json()["languages"][0]
    assert r2["last_updated"]["app"] >= r["last_updated"]["app"]


def test_viewer_can_read_but_not_see_super_admin_data():
    sa = super_admin(); add_ar(sa)
    v = viewer()
    env = api.get("/admin/translations/environment", headers=v).json()
    assert env["caller_is_system_super_admin"] is False and "package_config" not in env and "pending_packages" not in env
    assert api.get("/admin/translations/languages", headers=v).status_code == 200
    assert api.post("/admin/translations/publish", headers=v, json={"lang": "ar", "note": "x"}).status_code == 403
    assert api.get("/admin/translations/environment", headers=make_none()).status_code == 403


def make_none():
    from translation_test_support import make_user
    return make_user("none-token", perms={})


# ── real JS in a headless browser against mocked API data ──────────────────────────────────────
pw = pytest.importorskip("playwright.sync_api")


def _fixture(env="staging", sa=True, pending=None, empty=False, reason=None):
    ind = lambda pub, missing, draft, review: {"missing": missing, "draft_changes": draft, "waiting_review": review, "complete": missing == 0 and draft == 0, "published_version": pub}
    return {
        "environment": {"environment": {"classification": env, **({"reason": reason} if reason else {})}, "caller_is_system_super_admin": sa,
                        "kill_switch": {"enabled": False}, **({"package_config": {"own_secret": "ok", "own_fingerprint": "abcd1234", "ready_to_export": True}, "pending_packages": pending or {}} if sa else {})},
        "languages": {"base_language": {"code": "en", "name_en": "English", "name_native": "English", "direction": "ltr", "bundled": True}, "languages": [] if empty else [{"code": "ar", "name_en": "Arabic", "name_native": "العربية", "direction": "rtl", "status": "draft",
                                     "status_indicators": {"app": ind(2, 1, 3, 2), "legal": ind(None, 2, 0, 0)}, "last_updated": {"app": "2026-11-01T10:00:00Z", "legal": None}}]},
        "strings": {"lang": "ar", "track": "app", "total": 2, "page": 1, "page_size": 50, "rows": [
            {"key": "loginTitle", "screen_group": "login", "english": "Welcome <b>back</b>", "placeholders": [], "max_length": 60, "value": "مرحبا", "status": "published", "locked": False, "lock_reason": ""},
            {"key": "visitsCount", "screen_group": "home", "english": "{n, plural, one{# visit} other{# visits}}", "placeholders": [], "max_length": None, "value": None, "status": "locked", "locked": True, "lock_reason": "plural strings are bundled-only and read-only"}]},
        "versions": {"lang": "ar", "track": "app", "versions": [
            {"version": 2, "hash": "h2", "count": 5, "note": "n", "published_by": {"name": "Sam"}, "published_at": "2026-11-01T10:00:00", "restored_from": None, "promoted_from": None, "approved": False, "environment": env},
            {"version": 1, "hash": "h1", "count": 4, "note": "", "published_by": {"name": "Sam"}, "published_at": "2026-10-01T10:00:00", "restored_from": None, "promoted_from": None, "approved": True, "environment": env}]},
        "audit": {"entries": []},
    }


def _page(play, fx, extra_route=None, can_edit=True):
    b = play.chromium.launch(); pg = b.new_page(); calls = []
    html = ("<!doctype html><html><head><meta charset='utf-8'><link rel='stylesheet' href='/static/css/translations.css'></head><body><main>" + PARTIAL +
            "<script>window._rbacCanDo=function(){return %s};</script><script src='/static/js/translations.js'></script></main></body></html>" % ("true" if can_edit else "false"))

    def handle(route):
        url = route.request.url
        if url.endswith("/page"):
            return route.fulfill(body=html, content_type="text/html")
        if "/static/js/translations.js" in url:
            return route.fulfill(body=JS, content_type="application/javascript")
        if "/static/css/" in url:
            return route.fulfill(body=CSS, content_type="text/css")
        path = url.split("/admin/translations", 1)[1]
        calls.append((route.request.method, path))
        if extra_route and extra_route(route, path):
            return
        for k, v in (("/environment", fx["environment"]), ("/languages", fx["languages"]), ("/strings", fx["strings"]), ("/versions", fx["versions"]), ("/audit", fx["audit"])):
            if path.startswith(k) and route.request.method == "GET":
                return route.fulfill(body=json.dumps(v), content_type="application/json")
        route.fulfill(status=200, body="{}", content_type="application/json")
    pg.route("http://t.test/**", handle)
    pg.goto("http://t.test/page")
    pg.evaluate("Translations.open()")
    pg.wait_for_selector("#trn_pane_overview table, #trn_pane_overview .trn-empty")
    return b, pg, calls


def test_browser_overview_shows_real_data_and_super_admin_controls():
    with pw.sync_playwright() as p:
        b, pg, calls = _page(p, _fixture())
        txt = pg.inner_text("#trn_pane_overview")
        assert "Arabic" in txt and "v2" in txt and "v3 (3 changes)" in txt and "1 missing" in txt and "2 awaiting review" in txt and "01 Nov 2026" in txt
        assert pg.locator("#trn_tabs").inner_text().count("Promotion") == 1 and pg.locator("#trn_nl_code").count() == 1
        assert "STAGING" in pg.inner_text("#trn_banner")
        b.close()


def test_browser_edit_save_draft_and_escaping():
    saved = []
    def extra(route, path):
        if route.request.method == "PUT":
            saved.append((path, route.request.post_data_json)); route.fulfill(body='{"track":"app"}', content_type="application/json"); return True
    with pw.sync_playwright() as p:
        b, pg, calls = _page(p, _fixture(), extra)
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        assert "<b>" not in pg.inner_html(".trn-en") and "Welcome <b>back</b>" == pg.inner_text(".trn-en").strip().splitlines()[0]
        assert pg.locator("tr[data-row=visitsCount] textarea").is_disabled()                 # plural is locked
        assert not pg.locator("tr[data-row=loginTitle] [data-act=save]").is_visible()
        pg.fill("tr[data-row=loginTitle] textarea", "أهلا بعودتك"); pg.click("tr[data-row=loginTitle] [data-act=save]")
        pg.wait_for_selector("#trn_alert:not([hidden])")
        assert saved == [("/strings/ar/loginTitle", {"value": "أهلا بعودتك"})]
        assert pg.locator("tr[data-row=loginTitle] textarea").get_attribute("dir") == "rtl"
        b.close()


def test_browser_production_has_no_edit_rollback_approve_or_add_language():
    pend = {"app:ar": {"lang": "ar", "track": "app", "source_version": 3, "imported_at": "2026-11-02T00:00:00"}}
    with pw.sync_playwright() as p:
        b, pg, calls = _page(p, _fixture("production", True, pend))
        assert pg.locator("#trn_nl_code").count() == 0 and "PRODUCTION" in pg.inner_text("#trn_banner")
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        assert pg.locator("tr[data-row=loginTitle] textarea").is_disabled() and pg.locator("[data-act=save]").count() == 0
        assert pg.locator("[data-act=submit]").count() == 0
        assert "Publish imported package" in pg.inner_text("#trn_pane_strings")              # only because a package is pending
        pg.click("[data-act=tab][data-tab=versions]"); pg.wait_for_selector("#trn_pane_versions table")
        for act in ("rollback", "approve", "export"):
            assert pg.locator("[data-act=%s]" % act).count() == 0, act
        pg.click("[data-act=tab][data-tab=promotion]"); assert pg.locator("[data-act=import]").count() == 1
        b.close()


def test_browser_production_without_pending_package_has_no_publish_button():
    with pw.sync_playwright() as p:
        b, pg, calls = _page(p, _fixture("production", True, {}))
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        assert pg.locator("[data-act=publish]").count() == 0
        b.close()


def test_browser_non_super_admin_sees_no_privileged_controls():
    with pw.sync_playwright() as p:
        b, pg, calls = _page(p, _fixture("staging", False))
        assert pg.locator("#trn_tabs").inner_text().count("Promotion") == 0 and pg.locator("#trn_nl_code").count() == 0
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        assert pg.locator("[data-act=publish]").count() == 0 and pg.locator("[data-act=submit]").count() == 1
        pg.click("[data-act=tab][data-tab=versions]"); pg.wait_for_selector("#trn_pane_versions table")
        assert pg.locator("[data-act=approve],[data-act=rollback],[data-act=export]").count() == 0
        b.close()


def test_browser_view_only_user_cannot_edit():
    with pw.sync_playwright() as p:
        b, pg, calls = _page(p, _fixture("staging", False), can_edit=False)
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        assert pg.locator("tr[data-row=loginTitle] textarea").is_disabled() and pg.locator("[data-act=submit]").count() == 0
        b.close()


def test_browser_permission_denied_state_and_server_errors_shown():
    def extra(route, path):
        if path.startswith("/environment"):
            route.fulfill(status=403, body='{"detail":{"code":"forbidden"}}', content_type="application/json"); return True
    with pw.sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page()
        html = "<!doctype html><meta charset='utf-8'><body>" + PARTIAL + "<script>window._rbacCanDo=function(){return true};</script><script src='/static/js/translations.js'></script></body>"
        pg.route("http://t.test/**", lambda r: r.fulfill(body=html, content_type="text/html") if r.request.url.endswith("/page") else
                 r.fulfill(body=JS, content_type="application/javascript") if "translations.js" in r.request.url else
                 r.fulfill(status=403, body='{"detail":{"code":"forbidden"}}', content_type="application/json"))
        pg.goto("http://t.test/page"); pg.evaluate("Translations.open()")
        pg.wait_for_selector("#trn_denied:not([hidden])"); assert pg.locator("#trn_body").is_hidden()
        b.close()


def test_browser_publish_requires_a_note_and_sends_it():
    sent = []
    def extra(route, path):
        if route.request.method == "POST" and path == "/publish":
            sent.append(route.request.post_data_json); route.fulfill(body='{"version":3}', content_type="application/json"); return True
    with pw.sync_playwright() as p:
        b, pg, calls = _page(p, _fixture(), extra)
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        pg.click("[data-act=publish]"); pg.click("#trn_modal_ok")
        assert "required" in pg.inner_text("#trn_modal_err") and sent == []                  # blocked client side, and the server also requires it
        pg.fill("#trn_f_note", "tested on device"); pg.click("#trn_modal_ok"); pg.wait_for_selector("#trn_modal", state="hidden")
        assert sent == [{"lang": "ar", "track": "app", "note": "tested on device"}]
        b.close()


# ── unsafe / unidentified environment and an empty registry ──────────────────────────────────────────────────
def test_browser_unknown_environment_disables_every_change_and_explains_why():
    with pw.sync_playwright() as p:
        b, pg, calls = _page(p, _fixture("unknown", True, None, empty=True, reason="environment_not_set"))
        banner = pg.inner_text("#trn_banner")
        assert "Environment not identified" in banner and "OFFRO_ENVIRONMENT is not set" in banner
        assert "every change is disabled" in pg.inner_text("#trn_pane_overview")
        assert pg.locator("#trn_nl_code").count() == 0 and pg.locator("[data-act=fillar]").count() == 0
        assert "English" in pg.inner_text("#trn_pane_overview")                              # base language row is still shown
        pg.click("[data-act=tab][data-tab=promotion]"); assert pg.locator("[data-act=killswitch]").is_disabled()
        b.close()


def test_browser_unknown_environment_hides_edit_and_publish_for_existing_languages():
    with pw.sync_playwright() as p:
        b, pg, calls = _page(p, _fixture("unknown", True, None, reason="environment_value_invalid"))
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        assert pg.locator("tr[data-row=loginTitle] textarea").is_disabled()
        assert pg.locator("[data-act=publish]").count() == 0 and pg.locator("[data-act=submit]").count() == 0
        assert "Changes are disabled" in pg.inner_text("#trn_pane_strings")
        pg.click("[data-act=tab][data-tab=versions]"); pg.wait_for_selector("#trn_pane_versions table")
        assert pg.locator("[data-act=approve],[data-act=rollback],[data-act=export]").count() == 0
        b.close()


def test_browser_empty_staging_shows_english_base_and_lets_super_admin_add_arabic_once():
    sent = []
    def extra(route, path):
        if route.request.method == "POST" and path == "/languages":
            sent.append(route.request.post_data_json); route.fulfill(body='{"code":"ar","status":"draft","direction":"rtl"}', content_type="application/json"); return True
    with pw.sync_playwright() as p:
        b, pg, calls = _page(p, _fixture("staging", True, None, empty=True), extra)
        txt = pg.inner_text("#trn_pane_overview")
        assert "English" in txt and "built-in" in txt and "No translation languages added yet" in txt
        pg.click("[data-act=fillar]")
        assert pg.input_value("#trn_nl_code") == "ar" and pg.input_value("#trn_nl_dir") == "rtl" and pg.input_value("#trn_nl_en") == "Arabic"
        pg.click("[data-act=addlang]"); pg.wait_for_selector("#trn_alert:not([hidden])")
        assert sent == [{"code": "ar", "name_en": "Arabic", "name_native": "العربية", "direction": "rtl"}]
        b.close()


def test_browser_no_arabic_shortcut_when_arabic_already_exists():
    with pw.sync_playwright() as p:
        b, pg, calls = _page(p, _fixture("staging", True))
        assert pg.locator("[data-act=fillar]").count() == 0 and "Arabic" in pg.inner_text("#trn_pane_overview")
        b.close()


def test_browser_duplicate_language_error_is_shown_plainly():
    def extra(route, path):
        if route.request.method == "POST" and path == "/languages":
            route.fulfill(status=409, body='{"detail":{"code":"language_exists"}}', content_type="application/json"); return True
    with pw.sync_playwright() as p:
        b, pg, calls = _page(p, _fixture("staging", True, None, empty=True), extra)
        pg.click("[data-act=fillar]"); pg.click("[data-act=addlang]"); pg.wait_for_selector("#trn_alert:not([hidden])")
        assert "already exists" in pg.inner_text("#trn_alert")
        b.close()
