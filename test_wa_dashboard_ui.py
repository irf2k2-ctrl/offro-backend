"""Phase 2G dashboard UI: static contract tests (no browser). Step 1 = shell, RBAC integration, overview and campaign list.
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
DASH = read(os.path.join(ROOT, "templates", "admin_dashboard.html"))
PARTIAL = read(PARTIAL_PATH)


# ── the shell is served and wired ─────────────────────────────────────────────────────────────

def _dashboard_html():
    _DB.admins.insert_one({"username": "root", "token": "ui-token"})
    r = client.get("/admin/dashboard", cookies={"admin_token": "ui-token"})
    assert r.status_code == 200
    return r


def test_the_dashboard_renders_with_the_included_module_partial_and_assets():
    r = _dashboard_html()
    html = r.text
    assert 'id="section-wa_campaigns"' in html and "{%" not in html and "{{" not in html           # include resolved, nothing left over
    assert '/static/css/wa_campaigns.css' in html and '/static/js/wa_campaigns.js' in html
    assert html.index('id="section-wa_campaigns"') < html.index('id="section-wa_chat"')
    assert "showSection('wa_campaigns',this)" in html and "WhatsApp Campaigns</a>" in html
    assert 'WaCampaigns.open()' in html


def test_the_dashboard_stays_same_origin_so_the_existing_csp_allows_the_new_assets():
    r = _dashboard_html()
    csp = r.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "connect-src 'self'" in csp
    # no new external host, no inline event handlers in the new files
    for src in (JS, PARTIAL):
        assert not re.search(r"https?://", src.replace("http://www.w3.org", ""))
        assert not re.search(r"\son[a-z]+\s*=", PARTIAL)
    assert "<script" not in PARTIAL


def test_the_static_assets_are_served():
    for path, ctype in (("/static/js/wa_campaigns.js", "javascript"), ("/static/css/wa_campaigns.css", "css")):
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


def _called_paths():
    paths = set()
    for m in re.finditer(r"api\('([^']*)'", JS):
        paths.add(m.group(1).split("?")[0])
    return paths


def test_every_api_call_in_the_ui_exists_on_the_backend_as_a_get():
    calls = _called_paths()
    assert calls >= {"/config", "/worker/status", "/campaigns"}
    routes = _routes()["GET"]
    for p in calls:
        assert "/admin/whatsapp-campaigns" + p in routes, p


def test_step_one_issues_no_state_changing_request():
    assert not re.search(r"method\s*:\s*['\"](POST|PUT|PATCH|DELETE)", JS, re.I)
    assert "FormData" not in JS and "XMLHttpRequest" not in JS and "sendBeacon" not in JS
    assert JS.count("fetch(") == 1                                          # one place only: api()
    assert "/sending" not in JS and "/test-send" not in JS and "/schedule" not in JS and "/consent" not in JS and "run-config" not in JS


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
    assert len(re.findall(r"\.innerHTML\s*=", JS)) == 1 and "function setHtml" in JS
    for m in re.finditer(r"(\w*)`([^`]*)`", JS):
        tag, body = m.group(1), m.group(2)
        if "${" in body:
            assert tag == "h", "template literal with interpolation must use h``: " + body[:80]
    assert "document.write" not in JS and "eval(" not in JS and "new Function" not in JS


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


def test_no_phone_number_or_secret_is_requested_displayed_or_stored_by_the_module():
    for src in (re.sub(r"/\*.*?\*/", "", JS, flags=re.S), PARTIAL):          # code and markup only; the header comment may say "never"
        assert not re.search(r"phone|mobile|msisdn|\+91|token|password|secret|access_token|localStorage|sessionStorage|document\.cookie", src, re.I), \
            re.search(r"phone|mobile|msisdn|\+91|token|password|secret|access_token|localStorage|sessionStorage|document\.cookie", src, re.I).group(0)


def test_config_fields_shown_are_only_the_non_secret_ones():
    shown = set(re.findall(r"\b[csw]\.(\w+)", JS)) | set(re.findall(r"\bs\.(\w+)", JS))
    assert not (shown & {"access_token_configured", "whatsapp", "webhook", "verify_token_is_default", "app_secret_configured"})


# ── presentation conventions ───────────────────────────────────────────────────────────────────

def test_the_module_uses_the_dashboards_design_tokens_and_namespaced_classes():
    css = read(CSS_PATH)
    for var in ("--primary", "--muted", "--border", "--danger", "--warning"):
        assert var in css
    assert not re.search(r"(^|\n)\s*(body|html|\.sidebar|\.main|\.table-wrap|\.btn-p|\.btn-o|\.section)\b[^{]*\{", css)   # never restyles existing UI
    for sel in re.findall(r"(?m)^([^\s@/*][^{]*)\{", css):
        assert all(part.strip().startswith(".wac") for part in sel.split(",")), sel
    assert 'class="section wac"' in PARTIAL and "page-title" in PARTIAL and "table-wrap" in PARTIAL and "btn-p" in PARTIAL
    for sid in ("wac_denied", "wac_body", "wac_cards", "wac_chips", "wac_list", "wac_prev", "wac_next", "wac_count", "wac_refresh", "wac_list_msg"):
        assert 'id="%s"' % sid in PARTIAL, sid
        assert sid in JS, sid


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
    _DB.admins.insert_one({"username": "root", "token": "dl-token"})
    r = client.get("/download/admin_dashboard", cookies={"admin_token": "dl-token"})
    assert r.status_code == 200
    assert r.headers["content-disposition"] == "attachment; filename=admin_dashboard.html"      # same attachment behaviour as before
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "{%" not in body and "{{" not in body and "include" not in re.findall(r"\{%.*?%\}", body)
    assert 'id="section-wa_campaigns"' in body and 'id="wac_list"' in body                       # the module markup is inlined
    assert "/static/css/wa_campaigns.css" in body and "/static/js/wa_campaigns.js" in body       # assets referenced by absolute path
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

def test_the_production_guard_wording_is_scoped_to_the_worker_and_discloses_the_test_send_gap():
    assert "Worker Production guard" in JS and "'Production guard'" not in JS
    assert "covers the worker only" in JS and "Single-number test sends have no Production guard of their own" in JS
    assert "not specific to Production" in JS
    assert "Blocking sending" not in JS                                                          # the old, over-broad label
    assert "worker only, not to single-number test sends" in PARTIAL
    assert not re.search(r"\ball sending|blocked in production|production is blocked|production blocks", JS + PARTIAL, re.I)


def test_the_test_send_production_guard_gap_is_documented_as_a_separate_backend_task():
    doc = read(os.path.join(ROOT, "docs", "WHATSAPP_PHASE2G_BACKEND_SAFETY_TASKS.md"))
    assert "send_test_message" in doc and "no such guard" in doc and "production_blocked()" in doc
    assert "before any Production sending is enabled" in doc
