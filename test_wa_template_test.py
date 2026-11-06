"""Tests for the single-recipient WhatsApp template test endpoint. NO network: every HTTP call is mocked,
and any un-mocked request fails the test.  Run:  python -m pytest test_wa_template_test.py -q -W ignore"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mongomock
import pytest
db = mongomock.MongoClient()["wa_tpl_test"]       # private fake DB: never touches the shared one / sys.modules["database"]
from fastapi import FastAPI
from fastapi.testclient import TestClient
from services import whatsapp as wa
import routers.wa_template_test as T
import routers.wa_chat as W

app = FastAPI()
app.include_router(T.router, prefix="/admin")
c = TestClient(app)
H = {"X-Admin-Token": "tok"}
SECRET = "EAAB-secret-token-123"
GOOD = {"phone": "+91 98765 43210", "consent_confirmed": True, "consent_note": "Opted in via signup form 2026-10-01",
        "header_media_id": "1234567890"}
calls = {"get": [], "post": []}
TPL = {"data": [{"name": T.TEMPLATE_NAME, "language": "en", "status": "APPROVED", "category": "MARKETING", "components": [
    {"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "Join OffrO"},
    {"type": "BUTTONS", "buttons": [{"type": "URL", "text": "Download OffrO", "url": "https://theoffro.com/"}]}]}]}


class R:
    def __init__(self, code, js): self.status_code, self._js = code, js
    def json(self): return self._js


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(T, "db", db)               # endpoint + admin auth use this private DB only
    monkeypatch.setattr(W, "db", db)
    for col in ("whatsapp_template_tests", "dashboard_users", "admins"):
        db[col].delete_many({})
    db.admins.insert_one({"token": "tok"})
    calls["get"].clear(); calls["post"].clear()
    monkeypatch.setenv("WHATSAPP_TEMPLATE_TEST_ENABLED", "1")
    monkeypatch.setattr(wa, "WHATSAPP_ACCESS_TOKEN", SECRET)
    monkeypatch.setattr(wa, "WHATSAPP_PHONE_NUMBER_ID", "111")
    monkeypatch.setattr(wa, "WHATSAPP_BUSINESS_ACCOUNT_ID", "222")
    state = {"tpl": TPL, "send": R(200, {"messages": [{"id": "wamid.X"}]})}
    def fget(url, **kw): calls["get"].append((url, kw)); return R(200, state["tpl"])
    def fpost(url, **kw): calls["post"].append((url, kw)); return state["send"]
    monkeypatch.setattr(T.requests, "get", fget)       # same module object as wa.requests
    monkeypatch.setattr(wa.requests, "post", fpost)
    return state


def post(body, headers=H): return c.post("/admin/whatsapp/template-test", json=body, headers=headers)


def test_requires_admin_auth():
    assert post(GOOD, headers={}).status_code == 401
    assert post(GOOD, headers={"X-Admin-Token": "bad"}).status_code == 403
    assert calls["post"] == [] and calls["get"] == []


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("WHATSAPP_TEMPLATE_TEST_ENABLED")
    assert post(GOOD).status_code == 403 and calls["post"] == []


@pytest.mark.parametrize("phone", ["", "123", "0919876543210", "98765abc4321", ["919876543210"],
                                   "919876543210,919876543211", "919876543210; 919876543211", 919876543210,
                                   "9198765432101234567"])
def test_phone_validation_and_no_bulk(phone):
    r = post({**GOOD, "phone": phone})
    assert r.status_code == 400 and calls["post"] == []


def test_consent_required():
    assert post({**GOOD, "consent_confirmed": False}).status_code == 400
    assert post({**GOOD, "consent_confirmed": "true"}).status_code == 400
    assert post({**GOOD, "consent_note": "ok"}).status_code == 400
    assert calls["post"] == []


def test_header_source_exactly_one():
    b = {k: v for k, v in GOOD.items() if k != "header_media_id"}
    assert post(b).status_code == 400
    assert post({**GOOD, "header_image_url": "https://x.com/a.png"}).status_code == 400
    assert post({**b, "header_image_url": "http://x.com/a.png"}).status_code == 400
    assert post({**b, "header_image_url": "https://u:p@x.com/a.png"}).status_code == 400
    assert post({**GOOD, "header_media_id": "abc"}).status_code == 400


def test_success_builds_correct_template_payload():
    r = post(GOOD)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["sent"] and j["message_id"] == "wamid.X" and j["to_last4"] == "3210"
    assert SECRET not in r.text
    url, kw = calls["post"][0]
    assert url.endswith("/111/messages")
    p = kw["json"]
    assert p["to"] == "919876543210" and p["type"] == "template"
    assert p["template"]["name"] == "offro_merchant_invitation" and p["template"]["language"] == {"code": "en"}
    assert p["template"]["components"] == [{"type": "header", "parameters": [{"type": "image", "image": {"id": "1234567890"}}]}]
    assert len(calls["post"]) == 1
    a = db.whatsapp_template_tests.find_one({})
    assert a["status"] == "sent" and a["phone_last4"] == "3210" and "phone" not in a and "919876543210" not in str(a)


def test_image_url_header_uses_link():
    b = {k: v for k, v in GOOD.items() if k != "header_media_id"}
    assert post({**b, "header_image_url": "https://cdn.example.com/offro.png"}).status_code == 200
    assert calls["post"][0][1]["json"]["template"]["components"][0]["parameters"][0]["image"] == {"link": "https://cdn.example.com/offro.png"}


def test_dry_run_sends_nothing_but_checks_status():
    r = post({**GOOD, "dry_run": True})
    assert r.status_code == 200 and r.json()["sent"] is False and r.json()["template_status"] == "APPROVED"
    assert calls["post"] == [] and len(calls["get"]) == 1
    assert calls["get"][0][1]["params"]["name"] == "offro_merchant_invitation"
    assert db.whatsapp_template_tests.count_documents({}) == 0


@pytest.mark.parametrize("status", ["PENDING", "REJECTED", "PAUSED", "DISABLED"])
def test_refuses_when_template_not_approved(_env, status):
    _env["tpl"] = {"data": [{**TPL["data"][0], "status": status}]}
    r = post(GOOD)
    assert r.status_code == 409 and status in r.text and calls["post"] == []


def test_refuses_when_template_missing_or_shape_changed(_env):
    _env["tpl"] = {"data": []}
    assert post(GOOD).status_code == 404
    _env["tpl"] = {"data": [{**TPL["data"][0], "components": [{"type": "HEADER", "format": "TEXT"}, {"type": "BODY", "text": "x"}]}]}
    assert post(GOOD).status_code == 409
    _env["tpl"] = {"data": [{**TPL["data"][0], "components": [{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "Hi {{1}}"}]}]}
    assert post(GOOD).status_code == 409
    assert calls["post"] == []


def test_meta_error_returned_without_secret(_env):
    _env["send"] = R(400, {"error": {"message": "Bad token " + SECRET + " Bearer " + SECRET}})
    r = post(GOOD)
    assert r.status_code == 502 and SECRET not in r.text
    assert db.whatsapp_template_tests.find_one({})["status"] == "failed"
    assert SECRET not in str(db.whatsapp_template_tests.find_one({}))


def test_same_recipient_cooldown_and_hourly_limit():
    assert post(GOOD).status_code == 200
    assert post(GOOD).status_code == 429                      # same number again within 24h
    assert len(calls["post"]) == 1
    for i in range(1, 5):                                      # 4 more distinct numbers -> 5 total this hour
        assert post({**GOOD, "phone": f"9199000{i}11111"}).status_code == 200
    assert post({**GOOD, "phone": "919876543299"}).status_code == 429
    assert len(calls["post"]) == 5


def test_missing_config_never_sends(monkeypatch):
    monkeypatch.setattr(wa, "WHATSAPP_BUSINESS_ACCOUNT_ID", "")
    assert post(GOOD).status_code == 503 and calls["post"] == []


def test_existing_routes_untouched():
    paths = {r.path for r in W.router.routes}
    assert "/whatsapp/send" in paths and "/whatsapp/upload" in paths
    assert "/whatsapp/template-test" not in paths
