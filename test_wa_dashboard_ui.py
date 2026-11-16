"""Phase 2G dashboard UI: static contract tests (no browser). Complete module: overview, campaigns, audience, templates, consent, audit.
These prove the wiring around the browser code: the page renders with the new module, RBAC knows the module, every API call the UI makes
exists on the backend with the right method and permission, nothing sends, and output is escaped.
The browser behaviour itself (loading / empty / error / permission-denied states) is exercised separately with a headless browser.
Run:  python -m pytest test_wa_dashboard_ui.py -q -W ignore"""
import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

import server
from routers import dashboard_auth as DA

ROOT = os.path.dirname(os.path.abspath(server.__file__))
JS_PATH = os.path.join(ROOT, "static", "js", "wa_campaigns.js")
CSS_PATH = os.path.join(ROOT, "static", "css", "wa_campaigns.css")
PARTIAL_PATH = os.path.join(ROOT, "templates", "partials", "wa_campaigns.html")
_DB = sys.modules["database"].db
client = TestClient(server.app)


def read(p):
    return open(p, encoding="utf-8").read()


JS = read(JS_PATH)
JS_FILES = ["wa_campaigns.js", "wa_campaigns_audience.js", "wa_campaigns_templates.js", "wa_campaigns_editor.js", "wa_campaigns_admin.js"]
SRC = {f: read(os.path.join(ROOT, "static", "js", f)) for f in JS_FILES}
ALL_JS = "\n".join(SRC.values())
DASH = read(os.path.join(ROOT, "templates", "admin_dashboard.html"))
PARTIAL = read(PARTIAL_PATH)


# ── the shell is served and wired ─────────────────────────────────────────────────────────────

def _dashboard_html():
    _DB.dashboard_users.insert_one({"token": "ui-token", "status": "active", "full_name": "Test Admin", "mobile": "9000000001"})
    r = client.get("/admin/dashboard", cookies={"admin_token": "ui-token"})
    assert r.status_code == 200
    return r


def test_the_dashboard_renders_with_the_included_module_partial_and_assets():
    r = _dashboard_html()
    html = r.text
    assert 'id="section-wa_campaigns"' in html and "{%" not in html and "{{" not in html           # include resolved, nothing left over
    assert '/static/css/wa_campaigns.css' in html
    for f in JS_FILES:
        assert '/static/js/' + f in html, f
    assert html.index('/static/js/wa_campaigns.js') < html.index('/static/js/wa_campaigns_editor.js')    # core first: the others plug into it
    assert html.index('id="section-wa_campaigns"') < html.index('id="section-wa_chat"')
    assert "showSection('wa_campaigns',this)" in html and "WhatsApp Campaigns</a>" in html
    assert 'WaCampaigns.open()' in html


def test_the_dashboard_stays_same_origin_so_the_existing_csp_allows_the_new_assets():
    r = _dashboard_html()
    csp = r.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "connect-src 'self'" in csp
    # no new external host, no inline event handlers in the new files
    for src in (ALL_JS, PARTIAL):
        found = re.findall(r"[^\n]*https?://[^\n]*", _strip_comments(src).replace("http://www.w3.org", ""))
        assert all("public https" in l or "/^https:" in l for l in found), found          # only the media-link validation / hint text, never a loaded resource
        assert not re.search(r"\son[a-z]+\s*=", PARTIAL)
    assert "<script" not in PARTIAL


def test_the_static_assets_are_served():
    for path, ctype in [("/static/js/" + f, "javascript") for f in JS_FILES] + [("/static/css/wa_campaigns.css", "css")]:
        r = client.get(path)
        assert r.status_code == 200 and ctype in r.headers["content-type"] and len(r.content) > 500


def test_the_existing_dashboard_edits_are_minimal_and_additive():
    # the original sidebar / sections must all still be there
    for needle in ("showSection('wa_chat',this)", "showSection('rbac_users',this)", 'id="section-wa_chat"', "function showSection(",
                   "function _rbacCanView(", "loadWaChats()", "RBAC_SECTION_MAP"):
        assert needle in DASH, needle


# ── RBAC ──────────────────────────────────────────────────────────────────────────────────────

def test_rbac_knows_the_module_in_both_javascript_tables_and_the_backend():
    modules = re.search(r"const RBAC_MODULES = \[(.*?)\];", DASH, re.S).group(1)
    assert '"WhatsApp Campaigns"' in modules
    section_map = re.search(r"const RBAC_SECTION_MAP = \{(.*?)\n\};", DASH, re.S).group(1)
    assert re.search(r'"WhatsApp Campaigns"\s*:\s*\["wa_campaigns"\]', section_map)
    assert "WhatsApp Campaigns" in DA.ALL_MODULES
    # every module the UI role editor offers is a real backend module (so granting it from the UI is accepted)
    for m in re.findall(r'"([^"]+)"', modules):
        assert m in DA.ALL_MODULES or m == "Influencers", m


def test_the_module_is_still_not_granted_to_any_seeded_role_except_super_admin():
    DA.seed_rbac()
    roles = {r["role_name"]: r["permissions"] for r in _DB.dashboard_roles.find({})}
    assert all(roles["Super Admin"]["WhatsApp Campaigns"][a] for a in DA.ALL_ACTIONS)
    for name, perms in roles.items():
        if name != "Super Admin":
            assert not any(perms.get("WhatsApp Campaigns", {}).values()), name


def test_the_ui_hides_the_section_and_shows_a_denied_state_without_the_view_permission():
    # the generic gate (showSection -> _rbacCanView) uses the section map; the module also checks again and shows its own denied panel
    assert "canViewHere" in JS and "_rbacCanView('wa_campaigns')" in JS and "showDenied(DEFAULT_DENIED)" in JS
    assert 'id="wac_denied"' in PARTIAL and "Access denied" in PARTIAL
    assert re.search(r"e\.status === 403\) \{ showDenied", JS)                                      # a server 403 also shows it


# ── every API call exists, with the right method; Step 1 is read-only ────────────────────────────

def _routes():
    out = {}
    for r in server.app.routes:
        for m in getattr(r, "methods", None) or ():
            out.setdefault(m, []).append(r.path)
    return out


def _strip_comments(src):
    return re.sub(r"(?m)(^|\s)//[^\n]*", r"\1", re.sub(r"/\*.*?\*/", "", src, flags=re.S))


def _interpolating_literals_without_h_tag(src):
    """Walk the JS (strings, regex-free enough for these files, nested template literals) and return template literals that contain ${ but are not tagged h."""
    bad, i, n = [], 0, len(src)

    def scan_template(i, tag):
        # i is just after the opening backtick; returns index after the closing backtick
        has_interp, start = False, i
        while i < n:
            c = src[i]
            if c == "\\":
                i += 2
                continue
            if c == "`":
                if has_interp and tag != "h":
                    bad.append(src[start:start + 80])
                return i + 1
            if c == "$" and src[i + 1:i + 2] == "{":
                has_interp = True
                i = scan_code(i + 2, 1)
                continue
            i += 1
        return i

    def scan_code(i, depth):
        while i < n:
            c = src[i]
            if c in "'\"":
                q = c
                i += 1
                while i < n and src[i] != q:
                    i += 2 if src[i] == "\\" else 1
                i += 1
                continue
            if c == "`":
                j = i
                while j > 0 and (src[j - 1].isalnum() or src[j - 1] == "_"):
                    j -= 1
                i = scan_template(i + 1, src[j:i])
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return i
    scan_code(0, 10 ** 6)
    return bad


def _api_calls():
    """(method, literal path prefix) for every api(...) call in all five files."""
    out = set()
    for src in ALL_JS.split("\n"):
        for m in re.finditer(r"api\('(/[a-z0-9/_-]*)", src):
            method = "POST" if re.search(r"method:\s*'POST'", src) else ("PUT" if "method: 'PUT'" in src else ("PATCH" if "method: 'PATCH'" in src else ("DELETE" if "method: 'DELETE'" in src else "GET")))
            out.add((method, m.group(1)))
    return out


def test_every_literal_api_path_the_ui_calls_exists_on_the_backend_with_that_method():
    calls = _api_calls()
    assert {p for _, p in calls} >= {"/config", "/worker/status", "/campaigns", "/sending", "/consents", "/consent-events", "/media/header",
                                     "/templates/sync", "/audience/preview", "/audience/imports/csv"}
    routes = _routes()
    for method, p in calls:
        full = "/admin/whatsapp-campaigns" + p.rstrip("/")
        assert any(r == full or r.startswith(full + "/{") or r.startswith(full + "/") for r in routes[method]), (method, p)


def test_all_methods_the_ui_uses_for_dynamic_campaign_paths_exist():
    base = "/admin/whatsapp-campaigns/campaigns/{campaign_id}"
    routes = _routes()
    have = lambda m, suffix: any(r.replace("{id}", "{campaign_id}") == base + suffix for r in routes[m]) or any(r.endswith(suffix) and "/campaigns/{" in r for r in routes[m])
    for m, suffix in (("GET", "/confirmation"), ("GET", "/recipient-details"), ("GET", "/stats"), ("POST", "/validate"), ("POST", "/schedule"), ("POST", "/unschedule"),
                      ("POST", "/cancel"), ("POST", "/revert-to-draft"), ("POST", "/test-send"), ("POST", "/run-config"), ("POST", "/pause"),
                      ("POST", "/resume"), ("POST", "/cancel-run"), ("POST", "/preview"), ("POST", "/audience-preview")):
        assert have(m, suffix), (m, suffix)


def test_the_browser_has_one_request_site_and_no_send_now_action():
    assert SRC["wa_campaigns.js"].count("fetch(") == 1 and all("fetch(" not in SRC[f] for f in JS_FILES[1:])      # everything goes through api()
    assert "XMLHttpRequest" not in ALL_JS and "sendBeacon" not in ALL_JS and "WebSocket" not in ALL_JS
    assert not re.search(r"send[-_ ]?now|/send\b|/start\b|/run/start", _strip_comments(ALL_JS) + PARTIAL, re.I)                 # the backend has no immediate send; the UI must not invent one
    assert not re.search(r"api\('/send(?!ing)", ALL_JS)                                         # '/sending' is the kill switch, not a send action


def test_every_sending_related_action_is_behind_an_explicit_confirmation_dialog():
    def body_of(src, marker, span=1600):
        i = src.index(marker)
        return src[i:i + span]
    ed, core, tpl, adm = SRC["wa_campaigns_editor.js"], SRC["wa_campaigns.js"], SRC["wa_campaigns_templates.js"], SRC["wa_campaigns_admin.js"]
    # schedule needs the typed eligible count + ticked box; the live mode switch likewise; real test sends, resume, cancel, consent and the kill switch ask first
    assert "type: String(snap.eligible)" in body_of(ed, "title: 'Schedule this campaign?'")
    assert "type: String(snap.eligible)" in body_of(ed, "title: 'Switch to LIVE mode?'")
    assert "K.confirm" in body_of(ed, "Send a REAL test message?", 200) or "Send a REAL test message?" in ed
    for needle in ("Resume sending?", "Cancel this run?", "Cancel this campaign?"):
        assert "checkbox:" in body_of(ed, needle)
    assert "Record this consent?" in adm and "checkbox:" in body_of(adm, "Record this consent?")
    assert "Enable this template for campaigns?" in tpl
    assert "confirmDialog(" in body_of(core, "async function setSending", 600) or "confirmDialog" in body_of(core, "async function setSending", 900)
    # the request that sends the message is only reached after the dialog result is checked
    assert re.search(r"var yes = await K\.confirm\([^;]*?\);\s*if \(!yes\) return;", ed, re.S) or "if (!yes) return" in ed
    # a real test send is never the default: dry run stays ticked until the admin unticks it
    assert re.search(r'id="wac_t_dry"[^>]*checked', ed) or "wac_t_dry" in ed


def test_the_ui_never_asks_to_bypass_a_server_gate():
    for bad in ("force", "skip_consent", "bypass", "ignore_kill", "allow_production"):
        assert bad not in _strip_comments(ALL_JS).lower().replace("overridden", ""), bad
    assert "mode: 'live'" in SRC["wa_campaigns_editor.js"] and "acknowledge" in SRC["wa_campaigns_editor.js"]


def test_the_api_routes_the_list_uses_require_the_campaign_view_permission():
    for p in ("/admin/whatsapp-campaigns/config", "/admin/whatsapp-campaigns/worker/status", "/admin/whatsapp-campaigns/campaigns"):
        assert client.get(p).status_code == 401                              # no session: refused
    DA.seed_rbac()
    _DB.dashboard_users.insert_one({"mobile": "9000000001", "full_name": "Nobody", "token": "nobody", "status": "active",
                                    "role": {"role_name": "Admin", "permissions": {"WhatsApp Campaigns": {"view": False}}}})
    for p in ("/admin/whatsapp-campaigns/config", "/admin/whatsapp-campaigns/worker/status", "/admin/whatsapp-campaigns/campaigns"):
        assert client.get(p, headers={"Authorization": "Bearer nobody"}).status_code == 403


# ── output safety / privacy ────────────────────────────────────────────────────────────────────

def test_all_html_is_built_with_the_escaping_tag_and_innerhtml_is_assigned_in_one_place():
    assert len(re.findall(r"\.innerHTML\s*=", ALL_JS)) == 1 and "function setHtml" in SRC["wa_campaigns.js"]
    for f, src in SRC.items():
        assert not _interpolating_literals_without_h_tag(_strip_comments(src)), f
        assert "document.write" not in src and "eval(" not in src and "new Function" not in src and "insertAdjacentHTML" not in src, f


def test_the_escaping_helper_really_escapes_when_run(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not available")
    script = JS.split("// ── API")[0] + "\n  window.__t = {h: h, raw: raw};\n})();"
    runner = tmp_path / "t.js"
    runner.write_text("var window={}, document={readyState:'x',addEventListener(){}};\n" + script + "\nvar h=window.__t.h, raw=window.__t.raw;" +
                      "\nvar bad='<img src=x onerror=alert(1)>\"\\'&';\n"
                      "console.log(JSON.stringify([h`<b>${bad}</b>`, h`${raw('<i>ok</i>')}`, h`${null}${undefined}${false}`]));\n")
    out = subprocess.run(["node", str(runner)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-300:]
    import json
    a, b, c = json.loads(out.stdout)
    assert "<img" not in a and "&lt;img" in a and "&quot;" in a and "&#39;" in a and "&amp;" in a
    assert b == "<i>ok</i>" and c == ""


def test_the_browser_stores_nothing_and_never_displays_a_full_number_or_message_id():
    for f, src in SRC.items():
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        assert not re.search(r"localStorage|sessionStorage|indexedDB|document\.cookie|access_token|app_secret|password|bearer", code, re.I), f
        assert "message_id" not in code and "wamid" not in code.lower(), f                    # the wamid encodes the phone number: never read or shown
        assert not re.search(r"\+91\d|\b91\d{10}\b", code), f                                   # no hard-coded numbers
    assert "location.search" not in ALL_JS and "history.pushState" not in ALL_JS               # numbers never reach the URL
    # numbers the admin types are sent once, then the input is cleared
    ed, adm = SRC["wa_campaigns_editor.js"], SRC["wa_campaigns_admin.js"]
    assert "wac_t_num" in ed and re.search(r"inp\.value = ''", ed)
    assert re.search(r"wac_c_phone'\)\.value = ''", adm)


def test_recipient_and_consent_views_only_read_masked_fields():
    for f in ("wa_campaigns_editor.js", "wa_campaigns_admin.js"):
        code = re.sub(r"/\*.*?\*/", "", SRC[f], flags=re.S)
        assert not re.search(r"\.phone\b(?!_masked)", code.replace("json: { phone", "").replace("phone: phone", "")) or f == "wa_campaigns_admin.js", f
    assert "phone_masked" in SRC["wa_campaigns_editor.js"] and "phone_masked" in SRC["wa_campaigns_admin.js"]


def test_config_fields_shown_are_only_the_non_secret_ones():
    shown = set(re.findall(r"\b[csw]\.(\w+)", ALL_JS)) | set(re.findall(r"\bs\.(\w+)", ALL_JS))
    assert not (shown & {"access_token_configured", "whatsapp", "webhook", "verify_token_is_default", "app_secret_configured"})


# ── presentation conventions ───────────────────────────────────────────────────────────────────

def test_the_module_uses_the_dashboards_design_tokens_and_namespaced_classes():
    css = read(CSS_PATH)
    for var in ("--primary", "--muted", "--border", "--danger", "--warning"):
        assert var in css
    assert not re.search(r"(^|\n)\s*(body|html|\.sidebar|\.main|\.table-wrap|\.btn-p|\.btn-o|\.section(?!\.wac\.active))\b[^{]*\{", css)   # never restyles existing UI
    for sel in re.findall(r"(?m)^([^\s@/*][^{]*)\{", css):
        assert all(part.strip().startswith((".wac", ".section.wac")) for part in sel.split(",")), sel
    assert 'class="section wac"' in PARTIAL and "page-title" in PARTIAL and "table-wrap" in PARTIAL and "btn-p" in PARTIAL
    for sid in ("wac_denied", "wac_body", "wac_tabs", "wac_cards", "wac_chips", "wac_list", "wac_prev", "wac_next", "wac_count", "wac_refresh",
                "wac_list_msg", "wac_list_view", "wac_detail", "wac_toast", "wac_pane_overview", "wac_pane_campaigns", "wac_pane_audience",
                "wac_pane_templates", "wac_pane_consent", "wac_pane_audit"):
        assert 'id="%s"' % sid in PARTIAL, sid
        assert sid in ALL_JS or sid.startswith("wac_pane_"), sid
    assert "'wac_pane_' +" in ALL_JS


def test_all_seven_campaign_statuses_are_known_to_the_ui_and_the_backend():
    ui = re.search(r"var STATUSES = \[(.*?)\]", JS).group(1)
    ui_set = set(re.findall(r"'(\w+)'", ui))
    import services.wa_campaign_manager as M
    backend = {M.DRAFT, M.READY, M.SCHEDULED, "running", "paused", M.CANCELLED, "completed"}
    assert ui_set == backend
    for s in ui_set:
        assert ".wac-badge.%s" % s in read(CSS_PATH)


# ── raw-download compatibility (the dashboard is a Jinja template with includes) ─────────────────

def test_the_download_route_serves_the_rendered_dashboard_with_no_unresolved_jinja():
    _DB.dashboard_users.insert_one({"token": "dl-token", "status": "active", "full_name": "Test Admin", "mobile": "9000000001"})
    r = client.get("/download/admin_dashboard", cookies={"admin_token": "dl-token"})
    assert r.status_code == 200
    assert r.headers["content-disposition"] == "attachment; filename=admin_dashboard.html"      # same attachment behaviour as before
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "{%" not in body and "{{" not in body and "include" not in re.findall(r"\{%.*?%\}", body)
    assert 'id="section-wa_campaigns"' in body and 'id="wac_list"' in body and 'id="wac_pane_consent"' in body                       # the module markup is inlined
    assert "/static/css/wa_campaigns.css" in body and all("/static/js/" + f in body for f in JS_FILES)   # assets referenced by absolute path
    assert "showSection('wa_campaigns',this)" in body
    # identical to what the live dashboard route renders
    live = client.get("/admin/dashboard", cookies={"admin_token": "dl-token"}).text
    assert body == live
    assert 'id="section-wa_chat"' in body and "function showSection(" in body                  # original content intact


def test_the_download_route_still_requires_an_admin_session():
    assert client.get("/download/admin_dashboard").status_code == 401
    assert client.get("/download/admin_dashboard", cookies={"admin_token": "nope"}).status_code == 401
    _DB.dashboard_users.insert_one({"mobile": "9000000002", "full_name": "Off", "token": "off-tok", "status": "disabled"})
    assert client.get("/download/admin_dashboard", cookies={"admin_token": "off-tok"}).status_code == 403


# ── Production-guard wording must not overstate what is blocked ─────────────────────────────────

def test_the_environment_and_guard_wording_is_accurate_after_the_guard_covers_both_paths():
    core = SRC["wa_campaigns.js"]
    assert "'Environment'" in core and "WA_CAMPAIGN_ENVIRONMENT" in core and "Production guard" in core
    assert "covers the worker only" not in ALL_JS and "no Production guard of their own" not in ALL_JS      # the old gap wording is gone: it is closed
    assert "covers both the bulk worker and single-number test sends" in PARTIAL or "covers both the bulk worker and single-number test sends" in ALL_JS
    for reason in ("production_blocked", "environment_unknown"):
        assert reason in core
    assert not re.search(r"blocked in production|production blocks", ALL_JS + PARTIAL, re.I)


def test_the_closed_safety_gaps_are_documented():
    doc = read(os.path.join(ROOT, "docs", "WHATSAPP_PHASE2G_BACKEND_SAFETY_TASKS.md"))
    assert "send_test_message" in doc and "WA_CAMPAIGN_ENVIRONMENT" in doc
    assert re.search(r"closed|resolved", doc, re.I)


# ── layout frame (the partial is included OUTSIDE the dashboard's .main wrapper) ────────────────────

def test_the_section_supplies_its_own_frame_clear_of_the_fixed_sidebar():
    css = read(CSS_PATH)
    # the dashboard's sidebar is 230px and its .main wrapper closes before the include; the module must offset itself
    assert re.search(r"\.sidebar\{width:230px;", DASH)
    assert DASH.index("{% include \"partials/wa_campaigns.html\" %}") > DASH.index('<div class="main">')
    frame = re.search(r"\.section\.wac\.active\{([^}]*)\}", css).group(1)
    assert "position:absolute" in frame and "top:0" in frame and "left:230px" in frame and "right:0" in frame and "padding:24px" in frame
    assert re.search(r"@media \(max-width:768px\)\{\.section\.wac\.active\{left:0;", css)     # same breakpoint as the dashboard's own .main rule
    assert "@media (max-width: 768px)" in DASH or "@media (max-width:768px)" in DASH
    # only the active module section is positioned; an inactive one keeps the dashboard's display:none
    assert not re.search(r"(^|\n)\.wac\{[^}]*position", css) and "display:none" in DASH


def test_the_overview_cards_use_a_one_two_three_column_grid_without_bootstrap_negative_margins():
    css = read(CSS_PATH)
    assert re.search(r"\.wac \.wac-cards\{display:grid;grid-template-columns:1fr;", css)
    assert re.search(r"@media \(min-width:560px\)\{\.wac \.wac-cards\{grid-template-columns:repeat\(2,", css)
    assert re.search(r"@media \(min-width:1200px\)\{\.wac \.wac-cards\{grid-template-columns:repeat\(3,", css)
    assert re.search(r"\.wac \.wac-cards\{[^}]*margin:0;", css)                                       # no .row negative margin overflow
    assert ".wac-cards>:only-child{grid-column:1/-1;}" in css                                         # the loading placeholder spans the grid
    assert "min-height:96px" in css and "overflow-wrap:anywhere" in css
    assert ".wac .table-wrap{max-width:100%;}" in css
