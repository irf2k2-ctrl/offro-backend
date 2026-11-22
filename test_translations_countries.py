"""Country <-> language mapping (many to many), account country display data, and the dashboard changes that go with them.

Covers: the single country master is reused (no second list), mapping API rules (Super Admin only, validation, audit, environment),
English as a mappable base language, the public manifest carrying the mapping (additive; versions/hashes untouched), the account
country fields, and the real dashboard JS in a headless browser (Publish Changes in the top toolbar, Live version column gone,
Countries column, multi-select on add and edit).
Run:  python -m pytest test_translations_countries.py -q -W ignore
"""
import json
import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

import server
from routers import translations_admin as TA
from services import translations as T
from translation_test_support import (DB, add_ar, audit_actions, client as api, editor, publish, put, setup_env, super_admin,
                                      sync_fixture_keys, viewer)
from utils import countries as C
from utils import phone as PH

main_client = TestClient(server.app)
ROOT = os.path.dirname(os.path.abspath(server.__file__))
read = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()
JS, CSS, PARTIAL = read("static", "js", "translations.js"), read("static", "css", "translations.css"), read("templates", "partials", "translations.html")
DASH = read("templates", "admin_dashboard.html")
MASTER_ISOS = [c.iso for c in PH.SUPPORTED_COUNTRIES]


@pytest.fixture(autouse=True)
def staging(monkeypatch):
    setup_env(monkeypatch, "staging")
    sync_fixture_keys()
    DB.drop_collection(T.LANG_COUNTRIES)
    T.ensure_indexes()


def _put_countries(headers, code, countries):
    return api.put(f"/admin/translations/languages/{code}/countries", headers=headers, json={"countries": countries})


# ── the one country master is reused ─────────────────────────────────────────────────────────
def test_country_choices_come_from_the_master_not_from_a_second_list():
    got = [c["iso"] for c in C.supported_countries()]
    assert got == MASTER_ISOS
    for c in C.supported_countries():
        m = PH.supported_country(c["iso"])
        assert (c["name"], c["dial"]) == (m.name, m.dial)
    # nothing in the new module or the dashboard JS spells out the countries
    src = read("utils", "countries.py") + JS
    for name in ("India", "Jordan", "Nepal", "Philippines", "Saudi Arabia"):
        assert f'"{name}"' not in src and f"'{name}'" not in src, name          # no quoted country names (comments may mention them)
    assert "SUPPORTED_COUNTRIES" in read("utils", "countries.py") and not re.search(r"Country\(", read("utils", "countries.py"))


def test_flag_is_computed_from_the_iso_code():
    assert C.flag_emoji("SA") == "\U0001F1F8\U0001F1E6" and C.flag_emoji("in") == "\U0001F1EE\U0001F1F3"
    for bad in ("", "S", "SAU", "1A", None, "é", "ÄB"):
        assert C.flag_emoji(bad) == ""
    assert C.country_info("XX") is None and C.country_info("966") is None and C.country_info("") is None


def test_languages_endpoint_lists_supported_countries_from_the_master():
    sa = super_admin()
    d = api.get("/admin/translations/languages", headers=sa).json()
    assert [c["iso"] for c in d["supported_countries"]] == MASTER_ISOS
    assert d["base_language"]["countries"] == []


# ── mapping API ──────────────────────────────────────────────────────────────────────────────
def test_many_to_many_mapping_is_saved_and_listed():
    sa = super_admin(); add_ar(sa)
    assert _put_countries(sa, "ar", ["SA", "JO"]).json()["countries"] == ["JO", "SA"]
    assert _put_countries(sa, "en", ["SA", "IN"]).status_code == 200            # English (built-in base language) can be mapped
    d = api.get("/admin/translations/languages", headers=sa).json()
    assert d["languages"][0]["countries"] == ["JO", "SA"] and d["base_language"]["countries"] == ["IN", "SA"]
    # one country, several languages (SA has both) and one language, several countries
    by_country = {}
    for code, cs in (("ar", d["languages"][0]["countries"]), ("en", d["base_language"]["countries"])):
        for c in cs:
            by_country.setdefault(c, []).append(code)
    assert sorted(by_country["SA"]) == ["ar", "en"] and by_country["IN"] == ["en"] and by_country["JO"] == ["ar"]


def test_edit_replaces_the_previous_selection_and_empty_clears_it():
    sa = super_admin(); add_ar(sa)
    _put_countries(sa, "ar", ["SA", "JO", "NP"])
    assert _put_countries(sa, "ar", ["NP"]).json()["countries"] == ["NP"]
    assert api.get("/admin/translations/languages", headers=sa).json()["languages"][0]["countries"] == ["NP"]
    assert _put_countries(sa, "ar", []).json()["countries"] == []
    assert api.get("/admin/translations/languages", headers=sa).json()["languages"][0]["countries"] == []
    assert DB[T.LANG_COUNTRIES].count_documents({"lang": "ar"}) == 1               # replaced in place, never duplicated


def test_duplicates_and_case_are_normalised():
    sa = super_admin(); add_ar(sa)
    assert _put_countries(sa, "ar", ["sa", "SA", " jo "]).json()["countries"] == ["JO", "SA"]


@pytest.mark.parametrize("bad", [["ZZ"], ["SA", "XX"], ["966"], [""], [None], [5], "SA", {"SA": 1}, ["Saudi Arabia"]])
def test_unsupported_countries_are_refused_and_nothing_is_saved(bad):
    sa = super_admin(); add_ar(sa)
    _put_countries(sa, "ar", ["SA"])
    r = _put_countries(sa, "ar", bad)
    assert r.status_code == 422
    assert api.get("/admin/translations/languages", headers=sa).json()["languages"][0]["countries"] == ["SA"]


def test_only_super_admin_may_change_the_mapping():
    sa = super_admin(); add_ar(sa)
    for h in (editor(), viewer()):
        assert _put_countries(h, "ar", ["SA"]).status_code == 403
    from translation_test_support import fake_super
    assert _put_countries(fake_super(), "ar", ["SA"]).status_code == 403
    assert DB[T.LANG_COUNTRIES].count_documents({}) == 0
    assert api.put("/admin/translations/languages/ar/countries", json={"countries": ["SA"]}).status_code in (401, 403)


def test_unknown_language_is_404_and_unknown_environment_refuses(monkeypatch):
    sa = super_admin(); add_ar(sa)
    assert _put_countries(sa, "xx", ["SA"]).status_code == 404
    setup_env(monkeypatch, None)
    r = _put_countries(sa, "ar", ["SA"])
    assert r.status_code == 503 and DB[T.LANG_COUNTRIES].count_documents({}) == 0


def test_mapping_changes_are_audited_without_secrets():
    sa = super_admin(); add_ar(sa)
    _put_countries(sa, "ar", ["SA"])
    assert "set_language_countries" in audit_actions("started") and "set_language_countries" in audit_actions("ok")
    e = DB.translation_audit.find_one({"action": "set_language_countries", "outcome": "ok"})
    assert e["details"] == {"countries": ["SA"]} and e["lang"] == "ar"


def test_new_language_can_be_created_with_countries():
    sa = super_admin()
    r = api.post("/admin/translations/languages", headers=sa,
                 json={"code": "hi", "name_en": "Hindi", "name_native": "हिन्दी", "direction": "ltr", "countries": ["IN", "NP"]})
    assert r.status_code == 200 and r.json()["countries"] == ["IN", "NP"]
    langs = api.get("/admin/translations/languages", headers=sa).json()["languages"]
    assert langs[0]["code"] == "hi" and langs[0]["countries"] == ["IN", "NP"]


def test_new_language_without_countries_still_works_and_has_no_restriction():
    sa = super_admin()
    assert add_ar(sa).status_code == 200
    assert api.get("/admin/translations/languages", headers=sa).json()["languages"][0]["countries"] == []


def test_bad_country_on_create_creates_nothing():
    sa = super_admin()
    r = api.post("/admin/translations/languages", headers=sa,
                 json={"code": "hi", "name_en": "Hindi", "name_native": "हिन्दी", "direction": "ltr", "countries": ["IN", "ZZ"]})
    assert r.status_code == 422
    assert DB[T.LANGS].count_documents({}) == 0 and DB[T.LANG_COUNTRIES].count_documents({}) == 0


def test_country_removed_from_the_master_is_not_shown(monkeypatch):
    sa = super_admin(); add_ar(sa)
    _put_countries(sa, "ar", ["SA", "JO"])
    monkeypatch.setattr(PH, "SUPPORTED_COUNTRIES", tuple(c for c in PH.SUPPORTED_COUNTRIES if c.iso != "JO"))
    assert api.get("/admin/translations/languages", headers=sa).json()["languages"][0]["countries"] == ["SA"]


# ── public manifest (what the app reads) ─────────────────────────────────────────────────────
def _publish_ar(sa):
    add_ar(sa); put(sa, "loginTitle", "مرحبا"); assert publish(sa).status_code == 200


def test_manifest_carries_the_mapping_and_stays_valid_for_older_apps():
    sa = super_admin(); _publish_ar(sa)
    api.patch("/admin/translations/languages/ar", headers=sa, json={"status": "active"})
    _put_countries(sa, "ar", ["SA", "JO"]); _put_countries(sa, "en", ["SA", "IN"])
    m = api.get("/translations/manifest").json()
    assert m["language_countries"] == {"ar": ["JO", "SA"], "en": ["IN", "SA"]}
    ar = [l for l in m["languages"] if l["code"] == "ar"][0]
    assert ar["countries"] == ["JO", "SA"] and set(ar["tracks"]) >= {"app"}
    assert m["schema"] == 1 and m["kill_switch"] is False and m["overrides_enabled"] is True      # fields an older app reads: unchanged


def test_unmapped_languages_are_listed_with_an_empty_list():
    sa = super_admin(); _publish_ar(sa)
    api.patch("/admin/translations/languages/ar", headers=sa, json={"status": "active"})
    assert api.get("/translations/manifest").json()["language_countries"] == {"ar": [], "en": []}


def test_draft_language_is_not_offered_through_the_manifest():
    sa = super_admin(); add_ar(sa); _put_countries(sa, "ar", ["SA"])
    assert "ar" not in api.get("/translations/manifest").json()["language_countries"]


def test_kill_switch_removes_the_mapping_so_the_app_shows_every_bundled_language():
    sa = super_admin(); _publish_ar(sa)
    api.patch("/admin/translations/languages/ar", headers=sa, json={"status": "active"})
    _put_countries(sa, "ar", ["SA"])
    assert api.post("/admin/translations/kill-switch", headers=sa, json={"enabled": True, "reason": "test"}).status_code == 200
    m = api.get("/translations/manifest").json()
    assert m["kill_switch"] is True and m["languages"] == [] and "language_countries" not in m


def test_mapping_never_touches_versions_hashes_or_the_language_file():
    sa = super_admin(); _publish_ar(sa)
    api.patch("/admin/translations/languages/ar", headers=sa, json={"status": "active"})
    before_v = DB[T.VERSIONS].count_documents({}); f1 = api.get("/translations/ar"); m1 = api.get("/translations/manifest")
    _put_countries(sa, "ar", ["SA"]); _put_countries(sa, "en", ["IN"])
    f2 = api.get("/translations/ar"); m2 = api.get("/translations/manifest")
    assert DB[T.VERSIONS].count_documents({}) == before_v
    assert f1.json() == f2.json() and f1.headers["etag"] == f2.headers["etag"]                 # language file + its ETag identical
    t1 = [l for l in m1.json()["languages"] if l["code"] == "ar"][0]["tracks"]; t2 = [l for l in m2.json()["languages"] if l["code"] == "ar"][0]["tracks"]
    assert t1 == t2
    assert m1.headers["etag"] != m2.headers["etag"]                                           # the manifest itself did change


def test_arabic_text_and_publishing_are_unchanged_by_countries():
    sa = super_admin(); add_ar(sa)
    r = put(sa, "loginTitle", "أهلا بعودتك")
    assert r.status_code == 200
    assert publish(sa).status_code == 200
    _put_countries(sa, "ar", ["SA"])
    assert publish(sa).status_code in (200, 409)                                              # nothing new to publish is fine; no side effect
    assert DB[T.VERSIONS].find_one({"lang": "ar"})["strings"]["loginTitle"] == "أهلا بعودتك"


# ── accounts: country after the name ────────────────────────────────────────────────────────
def test_account_country_from_stored_country_or_from_the_number():
    assert C.account_country({"phone_country": "SA", "phone": "+966501234567"})["iso"] == "SA"
    assert C.account_country({"phone": "+966501234567"})["name"] == "Saudi Arabia"          # older account: derived from the number
    assert C.account_country({"phone": "9876543210"})["iso"] == "IN"                        # legacy Indian number
    assert C.account_country({"phone": "+919876543210"})["flag"] == "\U0001F1EE\U0001F1F3"
    assert C.account_country({"phone_country": "ZZ", "phone": "+977 9812345678"})["iso"] == "NP"   # bad stored value: fall back to the number
    for none in ({}, {"phone": ""}, {"phone": "abc"}, {"phone": "+1 415 555 2671"}, None):
        assert C.account_country(none) is None


@pytest.fixture
def admin_headers():
    role = DB.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    DB.dashboard_users.insert_one({"token": "acct-token", "status": "active", "full_name": "T", "mobile": "9000000001", "role_id": role})
    return {"Authorization": "Bearer acct-token"}


def test_accounts_list_returns_country_fields_and_tolerates_missing_country(admin_headers):
    DB.accounts.delete_many({})
    DB.accounts.insert_one({"name": "Sara", "phone": "+966501234567", "phone_country": "SA", "roles": ["user"], "status": "active"})
    DB.accounts.insert_one({"name": "Ravi", "phone": "9876543210", "roles": ["user"], "status": "active"})
    DB.accounts.insert_one({"name": "Nobody", "phone": "", "roles": ["user"], "status": "active"})
    for params in ({"page": 1, "page_size": 50}, {}):
        r = main_client.get("/admin/accounts", params=params, headers=admin_headers)
        assert r.status_code == 200, r.text
        rows = r.json()["items"] if params else r.json()
        by = {i["full_name"]: i for i in rows}
        assert (by["Sara"]["country_iso"], by["Sara"]["country_name"], by["Sara"]["country_flag"]) == ("SA", "Saudi Arabia", "\U0001F1F8\U0001F1E6")
        assert (by["Ravi"]["country_iso"], by["Ravi"]["country_name"]) == ("IN", "India")
        assert (by["Nobody"]["country_iso"], by["Nobody"]["country_name"], by["Nobody"]["country_flag"]) == ("", "", "")


def test_accounts_dashboard_shows_country_in_its_own_column_and_hardcodes_nothing():
    assert "${escHtml(a.full_name||'—')}</strong></td>" in DASH and "<td>${acctCountryHtml(a).trim() || '—'}</td>" in DASH
    fn = re.search(r"function acctCountryHtml\(a\) \{.*?\n\}", DASH, re.S).group(0)
    assert "a.country_iso" in fn and "a.country_flag" in fn and "a.country_name" in fn and "escHtml(" in fn
    for name in ("Saudi", "India", "Jordan", "Nepal", "Philippines"):
        assert name not in fn
    assert "<th>Name</th><th>Phone</th><th>Country</th><th>Ref ID</th>" in DASH             # Country is its own column; Name and Phone stay where they were


def test_account_country_label_is_rendered_and_escaped_in_a_browser():
    pw = pytest.importorskip("playwright.sync_api")
    m = re.search(r"function escHtml\(.*?\n\}", DASH, re.S)
    esc = m.group(0) if m else "function escHtml(s){return String(s).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));}"
    fn = re.search(r"function acctCountryHtml\(a\) \{.*?\n\}", DASH, re.S).group(0)
    with pw.sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page()
        pg.set_content("<div id=o></div>")
        pg.add_script_tag(content=esc + "\n" + fn)
        out = pg.evaluate("""() => [acctCountryHtml({country_iso:'SA',country_name:'Saudi Arabia',country_flag:'\\u{1F1F8}\\u{1F1E6}'}),
                                    acctCountryHtml({country_iso:''}), acctCountryHtml({}), acctCountryHtml(null),
                                    acctCountryHtml({country_iso:'SA',country_name:'<img src=x onerror=alert(1)>',country_flag:''})]""")
        b.close()
    assert out[0].strip().startswith('<span class="acct-country"') and "Saudi Arabia – SA" in out[0] and "\U0001F1F8\U0001F1E6" in out[0]
    assert out[1] == "" and out[2] == "" and out[3] == ""
    assert "<img" not in out[4] and "&lt;img" in out[4]


# ── dashboard (real JS in a headless browser against mocked API data) ───────────────────────
pw_mod = pytest.importorskip("playwright.sync_api")
SUPPORTED = [{"iso": c["iso"], "name": c["name"], "dial": c["dial"], "flag": c["flag"]} for c in C.supported_countries()]


def _fx(env="staging", sa=True):
    ind = lambda pub, missing, draft: {"missing": missing, "draft_changes": draft, "waiting_review": 0, "complete": missing == 0 and draft == 0, "published_version": pub}
    return {
        "environment": {"environment": {"classification": env}, "caller_is_system_super_admin": sa, "kill_switch": {"enabled": False}},
        "languages": {"base_language": {"code": "en", "name_en": "English", "name_native": "English", "direction": "ltr", "bundled": True, "countries": ["SA", "IN"]},
                      "supported_countries": SUPPORTED,
                      "languages": [{"code": "ar", "name_en": "Arabic", "name_native": "العربية", "direction": "rtl", "status": "active", "countries": ["SA"],
                                     "status_indicators": {"app": ind(2, 1, 3), "legal": ind(None, 2, 0)}, "last_updated": {"app": "2026-11-01T10:00:00Z", "legal": None}}]},
        "strings": {"lang": "ar", "track": "app", "total": 1, "page": 1, "page_size": 50, "rows": [
            {"key": "loginTitle", "screen_group": "login", "english": "Welcome", "placeholders": [], "max_length": 60, "value": "مرحبا", "status": "published", "locked": False, "lock_reason": ""}]},
    }


def _open(play, fx, extra=None, width=1280):
    b = play.chromium.launch(); pg = b.new_page(viewport={"width": width, "height": 800}); calls = []
    html = ("<!doctype html><html><head><meta charset='utf-8'><link rel='stylesheet' href='/static/css/translations.css'></head><body><main>" + PARTIAL +
            "<script>window._rbacCanDo=function(){return true};</script><script src='/static/js/translations.js'></script></main></body></html>")

    def handle(route):
        url = route.request.url
        if url.endswith("/page"):
            return route.fulfill(body=html, content_type="text/html")
        if "/static/js/translations.js" in url:
            return route.fulfill(body=JS, content_type="application/javascript")
        if "/static/css/" in url:
            return route.fulfill(body=CSS, content_type="text/css")
        path = url.split("/admin/translations", 1)[1]
        calls.append((route.request.method, path, route.request.post_data_json if route.request.post_data else None))
        if extra and extra(route, path):
            return
        for k, v in (("/environment", fx["environment"]), ("/languages", fx["languages"]), ("/strings", fx["strings"])):
            if path.startswith(k) and route.request.method == "GET":
                return route.fulfill(body=json.dumps(v), content_type="application/json")
        route.fulfill(status=200, body="{}", content_type="application/json")
    pg.route("http://t.test/**", handle)
    pg.goto("http://t.test/page"); pg.evaluate("Translations.open()")
    pg.wait_for_selector("#trn_pane_overview table")
    return b, pg, calls


def test_overview_has_countries_column_and_no_live_version():
    with pw_mod.sync_playwright() as p:
        b, pg, _ = _open(p, _fx())
        heads = [t.strip().lower() for t in pg.locator("#trn_pane_overview thead th").all_inner_texts()]
        assert "countries" in heads and "live version" not in heads and "live" not in " ".join(heads)
        n = len(heads)
        for row in pg.locator("#trn_pane_overview tbody tr").all():                       # no row is wider than the header (no empty gaps)
            assert sum(int(c.get_attribute("colspan") or 1) for c in row.locator("td").all()) == n
        txt = pg.inner_text("#trn_pane_overview")
        assert "Saudi Arabia" in txt and "India" in txt and "\U0001F1F8\U0001F1E6" in txt
        assert pg.locator("#trn_pane_overview tbody tr").first.inner_text().count("Saudi Arabia") == 1       # English row shows its own mapping
        b.close()


def test_unmapped_language_shows_all_countries():
    fx = _fx(); fx["languages"]["languages"][0]["countries"] = []
    with pw_mod.sync_playwright() as p:
        b, pg, _ = _open(p, fx)
        assert "All countries" in pg.inner_text("#trn_pane_overview")
        b.close()


def test_add_language_form_has_country_multiselect_from_the_server_list_and_posts_it():
    sent = []
    def extra(route, path):
        if route.request.method == "POST" and path == "/languages":
            sent.append(route.request.post_data_json); route.fulfill(body='{"code":"hi","status":"draft","direction":"ltr"}', content_type="application/json"); return True
    with pw_mod.sync_playwright() as p:
        b, pg, _ = _open(p, _fx(), extra)
        boxes = pg.locator("#trn_nl_countries input[type=checkbox]")
        assert [boxes.nth(i).get_attribute("value") for i in range(boxes.count())] == MASTER_ISOS      # exactly the server's list, in its order
        pg.fill("#trn_nl_code", "hi"); pg.fill("#trn_nl_en", "Hindi"); pg.fill("#trn_nl_native", "हिन्दी")
        pg.check("#trn_nl_countries input[value=IN]"); pg.check("#trn_nl_countries input[value=NP]")
        pg.click("[data-act=addlang]"); pg.wait_for_selector("#trn_alert:not([hidden])")
        assert sent == [{"code": "hi", "name_en": "Hindi", "name_native": "हिन्दी", "direction": "ltr", "countries": ["IN", "NP"]}]
        b.close()


def test_edit_countries_dialog_prefills_saves_and_refreshes_the_overview():
    saved = []
    fx = _fx()
    def extra(route, path):
        if route.request.method == "PUT" and path == "/languages/ar/countries":
            body = route.request.post_data_json; saved.append(body)
            fx["languages"]["languages"][0]["countries"] = body["countries"]               # the server now returns the saved mapping
            route.fulfill(body=json.dumps({"code": "ar", "countries": body["countries"]}), content_type="application/json"); return True
    with pw_mod.sync_playwright() as p:
        b, pg, calls = _open(p, fx, extra)
        pg.locator("[data-act=editcountries][data-lang=ar]").first.click()
        assert pg.locator("#trn_modal").is_visible()
        assert pg.locator("#trn_f_countries input[value=SA]").is_checked() and not pg.locator("#trn_f_countries input[value=JO]").is_checked()
        pg.check("#trn_f_countries input[value=JO]"); pg.uncheck("#trn_f_countries input[value=SA]")
        pg.click("#trn_modal_ok"); pg.wait_for_selector("#trn_modal", state="hidden")
        assert saved == [{"countries": ["JO"]}]
        pg.wait_for_function("document.querySelector('#trn_pane_overview').innerText.includes('Jordan')")
        txt = pg.inner_text("#trn_pane_overview")
        assert "Jordan" in txt
        assert sum(1 for c in calls if c[0] == "GET" and c[1].startswith("/languages")) >= 2    # re-read after saving (latest mapping shown)
        b.close()


def test_english_mapping_can_be_edited_too():
    saved = []
    def extra(route, path):
        if route.request.method == "PUT" and path == "/languages/en/countries":
            saved.append(route.request.post_data_json); route.fulfill(body='{"code":"en","countries":["IN"]}', content_type="application/json"); return True
    with pw_mod.sync_playwright() as p:
        b, pg, _ = _open(p, _fx(), extra)
        pg.locator("[data-act=editcountries][data-lang=en]").click()
        pg.uncheck("#trn_f_countries input[value=SA]"); pg.click("#trn_modal_ok"); pg.wait_for_selector("#trn_modal", state="hidden")
        assert saved == [{"countries": ["IN"]}]
        b.close()


def test_country_editing_is_hidden_from_non_super_admins():
    with pw_mod.sync_playwright() as p:
        b, pg, _ = _open(p, _fx(sa=False))
        assert pg.locator("[data-act=editcountries]").count() == 0 and pg.locator("#trn_nl_countries").count() == 0
        assert "Saudi Arabia" in pg.inner_text("#trn_pane_overview")                          # still visible, just read-only
        b.close()


def test_country_names_from_the_server_are_escaped():
    fx = _fx(); fx["languages"]["supported_countries"][0]["name"] = "<img src=x onerror=window.__x=1>"
    fx["languages"]["languages"][0]["countries"] = [fx["languages"]["supported_countries"][0]["iso"]]
    with pw_mod.sync_playwright() as p:
        b, pg, _ = _open(p, fx)
        assert pg.locator("#trn_pane_overview img").count() == 0 and pg.evaluate("window.__x") is None
        b.close()


# ── Publish Changes: top toolbar, next to the search box, once ────────────────────────────────
@pytest.mark.parametrize("width", [1280, 390])
def test_publish_button_is_in_the_top_toolbar_next_to_search_and_not_duplicated(width):
    with pw_mod.sync_playwright() as p:
        b, pg, calls = _open(p, _fx(), width=width)
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        assert pg.locator("[data-act=publish]").count() == 1 and pg.locator(".trn-actions").count() == 0
        first_toolbar = pg.locator("#trn_pane_strings .trn-toolbar").first
        assert first_toolbar.locator("input[type=search]").count() == 1 and first_toolbar.locator("[data-act=publish]").count() == 1
        # right after the search box in the same toolbar, above the table
        assert pg.evaluate("""() => { const s=document.querySelector('#trn_pane_strings input[type=search]'); return s.nextElementSibling && s.nextElementSibling.getAttribute('data-act')==='publish'; }""")
        assert pg.evaluate("""() => document.querySelector('[data-act=publish]').getBoundingClientRect().top < document.querySelector('#trn_pane_strings table').getBoundingClientRect().top""")
        btn = pg.locator("[data-act=publish]")
        assert btn.is_visible() and btn.bounding_box()["x"] >= 0 and btn.bounding_box()["x"] + btn.bounding_box()["width"] <= width + 1
        # the toolbar itself fits the viewport (it wraps); the wide key table scrolls inside .table-wrap in the real dashboard
        assert pg.evaluate("""() => { const t=document.querySelector('#trn_pane_strings .trn-toolbar'); return t.scrollWidth <= t.clientWidth + 1 && t.getBoundingClientRect().right <= window.innerWidth + 1; }""")
        b.close()


def test_publish_action_is_unchanged_when_pressed_from_the_toolbar():
    posts = []
    def extra(route, path):
        if route.request.method == "POST" and path == "/publish":
            posts.append(route.request.post_data_json); route.fulfill(body='{"version":3}', content_type="application/json"); return True
    with pw_mod.sync_playwright() as p:
        b, pg, _ = _open(p, _fx(), extra)
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        pg.click("[data-act=publish]"); assert pg.locator("#trn_modal_title").inner_text() == "Publish Changes"
        pg.fill("#trn_f_note", "n1"); pg.click("#trn_modal_ok"); pg.wait_for_selector("#trn_modal", state="hidden")
        assert posts == [{"lang": "ar", "track": "app", "note": "n1"}]
        b.close()


def test_publish_button_still_hidden_without_super_admin_or_known_environment():
    with pw_mod.sync_playwright() as p:
        for fx in (_fx(sa=False), _fx(env="unknown")):
            b, pg, _ = _open(p, fx)
            pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
            assert pg.locator("[data-act=publish]").count() == 0
            b.close()
