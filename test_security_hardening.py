"""Security hardening: CORS, security headers/CSP, Secure cookies, protected download, staging-host lockdown,
and a regression check that Product / WhatsApp routes + startup wiring are still present.
Run:  python -m pytest test_security_hardening.py -q -W ignore"""
import os, re
import mongomock
import pytest
from fastapi.testclient import TestClient
import server
import routers.admin as RA
from services import security as S

db = mongomock.MongoClient()["sec_test"]
STAGING = "offro-backend-staging-d375.up.railway.app"
PROD = "offro-backend-production.up.railway.app"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for c in ("admins", "dashboard_users"):
        db[c].delete_many({})
    monkeypatch.setattr(server, "db", db)
    monkeypatch.setattr(RA, "db", db)
    monkeypatch.delenv("ENABLE_API_DOCS", raising=False)
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)


def client(host=None):
    return TestClient(server.app, headers={"host": host} if host else {}, follow_redirects=False)


# ── CORS ──
def test_cors_is_not_wildcard_and_untrusted_origin_is_refused():
    c = client()
    r = c.options("/home-data", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers
    r = c.options("/home-data", headers={"Origin": "https://admin.theoffro.com", "Access-Control-Request-Method": "GET"})
    assert r.headers["access-control-allow-origin"] == "https://admin.theoffro.com"
    assert r.headers["access-control-allow-credentials"] == "true"
    assert "*" not in S.cors_origins()


def test_cors_extra_origin_via_env_and_wildcard_ignored(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://extra.example/, *")
    o = S.cors_origins()
    assert "https://extra.example" in o and "*" not in o


# ── headers / CSP ──
def test_security_headers_on_json_and_csp_only_on_html():
    c = client()
    j = c.get("/openapi.json")
    for h in ("x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy"):
        assert h in j.headers
    assert j.headers["x-frame-options"] == "DENY" and j.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in j.headers
    h = c.get("/admin")
    assert h.status_code == 200
    csp = h.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "control.msg91.com" in csp
    assert "strict-transport-security" not in h.headers          # plain http
    assert "strict-transport-security" in c.get("/admin", headers={"x-forwarded-proto": "https"}).headers


# ── Secure cookie ──
def _login(c, extra=None):
    db.admins.insert_one({"username": "u", "password": "p"})
    return c.post("/admin/login", json={"username": "u", "password": "p"}, headers=extra or {})


def test_cookie_secure_only_over_https():
    r = _login(client(), {"x-forwarded-proto": "https"})
    ck = r.headers["set-cookie"]
    assert "admin_token=" in ck and "Secure" in ck and "HttpOnly" in ck
    db.admins.delete_many({})
    r = _login(client())
    assert "Secure" not in r.headers["set-cookie"]               # local http dev keeps working


# ── /download/admin_dashboard ──
def test_download_requires_admin_session():
    c = client()
    assert c.get("/download/admin_dashboard").status_code == 401
    assert c.get("/download/admin_dashboard", cookies={"admin_token": "bogus"}).status_code == 401
    db.admins.insert_one({"token": "good-token"})
    r = c.get("/download/admin_dashboard", cookies={"admin_token": "good-token"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]


# ── staging lockdown (production hosts unchanged) ──
def test_staging_host_is_locked_down():
    c = client(STAGING)
    for p in ("/docs", "/redoc", "/openapi.json", "/open"):
        assert c.get(p).status_code == 404, p
    rb = c.get("/robots.txt")
    assert rb.status_code == 200 and "Disallow: /" in rb.text
    assert c.get("/admin").headers["x-robots-tag"] == "noindex, nofollow"


def test_staging_crawl_flag_lifts_robots_block_for_review(monkeypatch):
    monkeypatch.setenv("STAGING_ALLOW_CRAWL", "1")
    c = client(STAGING)
    assert "x-robots-tag" not in c.get("/admin").headers
    assert "Disallow: /" not in c.get("/robots.txt").text


def test_staging_docs_can_be_re_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_API_DOCS", "1")
    assert client(STAGING).get("/openapi.json").status_code == 200


def test_production_host_behaviour_unchanged():
    c = client(PROD)
    assert c.get("/openapi.json").status_code == 200
    assert c.get("/open").status_code == 200
    assert "x-robots-tag" not in c.get("/admin").headers
    assert S.is_staging_host(PROD) is False and S.is_staging_host(STAGING) is True


# ── regression: Product + WhatsApp wiring intact ──
def test_product_whatsapp_routes_and_startup_intact():
    paths = {r.path for r in server.app.routes}
    for p in ("/admin/whatsapp/template-test", "/admin/whatsapp/send", "/admin/whatsapp/upload",
              "/merchant/products/order", "/merchant/products/verify", "/merchant/products/{pid}/renew/verify",
              "/products-public", "/admin/products/{vid}/extend", "/admin/pricing"):
        assert p in paths, p
    src = open(os.path.join(os.path.dirname(server.__file__), "server.py")).read()
    for needle in ("ensure_settings", "ensure_indexes", "include_router(merchant_products.router",
                   "include_router(admin_products.router", "include_router(public_products.router",
                   "include_router(wa_template_test.router"):
        assert needle in src, needle


# ── templates: helpers present, nothing obviously unsafe re-introduced ──
@pytest.mark.parametrize("f", ["admin_dashboard", "merchant_dashboard"])
def test_templates_define_escape_helpers_and_use_them(f):
    s = open(os.path.join(os.path.dirname(server.__file__), "templates", f + ".html")).read()
    assert s.count("function escHtml(") == 1 and "function _secJs(" in s and "function _secUrl(" in s
    assert s.count("escHtml(") > 10
    # no inline handler may interpolate a raw name/title/phone field
    for m in re.finditer(r"""on[a-z]+="[^"]*\$\{[^}]*\.(name|store_name|title|phone|email|city|full_name)\b[^}]*\}""", s):
        assert "_secJs(" in m.group(0) or "escHtml(" in m.group(0), m.group(0)[:100]
