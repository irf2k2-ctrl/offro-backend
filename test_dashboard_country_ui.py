"""Stage 4 Part A (UI): the Country field of the Dashboard user form + the Country column - the real page and JS in a headless browser
against mocked API responses. (Security is enforced by the backend; see test_dashboard_country_access.py.)
Run:  python -m pytest test_dashboard_country_ui.py -q -W ignore"""
import json

import pytest
from fastapi.testclient import TestClient

import server
from translation_test_support import DB

pw = pytest.importorskip("playwright.sync_api")
main_client = TestClient(server.app)
COUNTRIES = [{"iso": "IN", "name": "India", "dial": "91", "flag": "🇮🇳"}, {"iso": "JO", "name": "Jordan", "dial": "962", "flag": "🇯🇴"},
             {"iso": "NP", "name": "Nepal", "dial": "977", "flag": "🇳🇵"}, {"iso": "PH", "name": "Philippines", "dial": "63", "flag": "🇵🇭"},
             {"iso": "SA", "name": "Saudi Arabia", "dial": "966", "flag": "🇸🇦"}]
USERS = [{"id": "u1", "full_name": "Asha", "mobile": "9000000011", "designation": "", "role_id": "r1", "role_name": "Manager", "status": "active",
          "assigned_cities": ["Ballari"], "country": "SA", "country_name": "Saudi Arabia"},
         {"id": "u2", "full_name": "Legacy Lee", "mobile": "9000000012", "designation": "", "role_id": "r1", "role_name": "Manager", "status": "active",
          "assigned_cities": ["*"], "country": "", "country_name": ""}]


@pytest.fixture
def html():
    DB.dashboard_users.insert_one({"token": "ui-token", "status": "active", "full_name": "Test Admin", "mobile": "9000000009"})
    r = main_client.get("/admin/dashboard", cookies={"admin_token": "ui-token"})
    assert r.status_code == 200
    return r.text


def open_page(play, html, handler=None):
    b = play.chromium.launch(); pg = b.new_page(viewport={"width": 1280, "height": 900}); calls = []

    def route(rt):
        req = rt.request; url = req.url
        if url == "http://t.test/dash":
            return rt.fulfill(body=html, content_type="text/html")
        if not url.startswith("http://t.test/"):
            return rt.abort()
        path = url.split("http://t.test", 1)[1]
        if path.startswith("/static/"):
            try:
                body = open(server.__file__.rsplit("/", 1)[0] + path.split("?")[0], "rb").read()
            except OSError:
                return rt.fulfill(status=404, body="")
            return rt.fulfill(body=body, content_type="text/css" if path.endswith(".css") else "application/javascript")
        pd = req.post_data_buffer
        calls.append((req.method, path, pd.decode("utf-8", "replace") if pd else None))
        if handler:
            res = handler(req, path)
            if res is not None:
                return rt.fulfill(**res)
        js = lambda o: {"body": json.dumps(o), "content_type": "application/json"}
        if path == "/admin/auth/countries":
            return rt.fulfill(**js(COUNTRIES))
        if path == "/admin/auth/roles":
            return rt.fulfill(**js([{"id": "r1", "role_name": "Manager", "permissions": {}}]))
        if path == "/admin/auth/users" and req.method == "GET":
            return rt.fulfill(**js(USERS))
        if path.startswith("/admin/auth/users"):
            return rt.fulfill(**js({"message": "ok"}))
        return rt.fulfill(status=200, body="[]", content_type="application/json")
    pg.route("**/*", route)
    # Bootstrap comes from a CDN (blocked here): a minimal stand-in so the page's modal calls work; the form itself is the real one.
    pg.add_init_script("window.bootstrap = { Modal: class { show(){} hide(){} static getInstance(){ return { hide(){} }; } } };")
    pg.goto("http://t.test/dash")
    return b, pg, calls


def test_country_column_shows_each_users_country_or_all_countries(html):
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html)
        pg.evaluate("rbacLoadUsers()")
        pg.wait_for_function("document.querySelectorAll('#rbac_users_table tr').length === 2")
        rows = pg.locator("#rbac_users_table tr")
        assert "Saudi Arabia" in rows.nth(0).inner_text()
        assert "All countries" in rows.nth(1).inner_text()
        heads = pg.locator("#rbac_users_table").locator("xpath=ancestor::table//th").all_inner_texts()
        assert "Country" in [h.strip() for h in heads]
        b.close()


def test_selector_offers_the_country_master_and_no_restriction(html):
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html)
        pg.evaluate("rbacShowUserModal('')")
        pg.wait_for_function("document.querySelectorAll('#rbac_user_country option').length === 6")
        opts = pg.locator("#rbac_user_country option").all_inner_texts()
        assert opts[0].startswith("All countries") and [o.split(" ", 1)[1] for o in opts[1:]] == ["India", "Jordan", "Nepal", "Philippines", "Saudi Arabia"]
        assert any(c[1] == "/admin/auth/countries" for c in calls)                         # served by the backend, not hard-coded in the page
        assert pg.input_value("#rbac_user_country") == ""
        b.close()


def test_creating_a_user_sends_the_selected_country(html):
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html)
        pg.evaluate("rbacShowUserModal('')")
        pg.wait_for_function("document.querySelectorAll('#rbac_user_country option').length === 6")
        pg.fill("#rbac_user_name", "New Person"); pg.fill("#rbac_user_mobile", "9111122222"); pg.fill("#rbac_user_pin", "1234")
        pg.select_option("#rbac_user_role", "r1"); pg.select_option("#rbac_user_country", "NP")
        pg.evaluate("rbacSaveUser()")
        pg.wait_for_function("window.__sent = 1; true")
        pg.wait_for_timeout(300)
        post = [c for c in calls if c[0] == "POST" and c[1] == "/admin/auth/users"][-1]
        body = json.loads(post[2])
        assert body["country"] == "NP" and body["full_name"] == "New Person" and "assigned_cities" in body     # city field still sent
        b.close()


def test_editing_preselects_the_users_country_and_sends_changes(html):
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html)
        pg.evaluate("rbacShowUserModal('u1')")
        pg.wait_for_function("document.querySelector('#rbac_user_name').value === 'Asha'")
        assert pg.input_value("#rbac_user_country") == "SA"
        pg.select_option("#rbac_user_country", "JO")
        pg.evaluate("rbacSaveUser()")
        pg.wait_for_timeout(300)
        put = [c for c in calls if c[0] == "PUT" and c[1] == "/admin/auth/users/u1"][-1]
        assert json.loads(put[2])["country"] == "JO"
        pg.evaluate("rbacShowUserModal('u2')")
        pg.wait_for_function("document.querySelector('#rbac_user_name').value === 'Legacy Lee'")
        assert pg.input_value("#rbac_user_country") == ""                              # legacy user: no restriction shown, not a wrong country
        b.close()


def test_server_refusal_is_shown(html):
    def handler(req, path):
        if req.method == "PUT" and path == "/admin/auth/users/u1":
            return {"status": 403, "body": json.dumps({"detail": "You cannot change your own country assignment"}), "content_type": "application/json"}
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html, handler)
        msgs = []
        pg.on("dialog", lambda d: (msgs.append(d.message), d.accept()))
        pg.evaluate("rbacShowUserModal('u1')")
        pg.wait_for_function("document.querySelector('#rbac_user_name').value === 'Asha'")
        pg.evaluate("rbacSaveUser()")
        pg.wait_for_function("document.querySelector('#rbac_user_country_err').innerText.includes('own country')")
        assert msgs and "own country" in msgs[0]
        b.close()


def test_country_names_from_the_server_are_escaped(html):
    evil = "<img src=x onerror=window.__pwned=1>"
    def handler(req, path):
        if path == "/admin/auth/countries":
            return {"body": json.dumps([{"iso": "IN", "name": evil, "dial": "91", "flag": ""}]), "content_type": "application/json"}
        if path == "/admin/auth/users" and req.method == "GET":
            return {"body": json.dumps([{**USERS[0], "country": "IN", "country_name": evil}]), "content_type": "application/json"}
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html, handler)
        pg.evaluate("rbacLoadUsers()")
        pg.wait_for_function("document.querySelectorAll('#rbac_users_table tr').length === 1")
        pg.evaluate("rbacShowUserModal('')")
        pg.wait_for_function("document.querySelectorAll('#rbac_user_country option').length === 2")
        assert pg.evaluate("window.__pwned") is None
        assert evil in pg.inner_text("#rbac_users_table")
        b.close()
