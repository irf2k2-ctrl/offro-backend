"""WhatsApp Campaign Manager - Phase 2C template management: Meta sync (mocked), parsing, variables, validation,
preview, payload building, campaign-use mapping, permissions, audit. NO sending, NO network: requests.get is faked.
Run:  python -m pytest test_wa_campaign_templates.py -q -W ignore"""
import json
import pathlib
from datetime import datetime, timedelta

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.dashboard_auth as DA
import routers.wa_campaign_templates as RT
import routers.wa_campaigns as RC
import services.wa_campaign_audit as AUD
import services.wa_campaign_settings as ST
import services.wa_templates as T
import services.whatsapp as WA
from test_wa_webhook_consent import _BrokenDb

_DB = mongomock.MongoClient()["wa_templates_test"]
BASE = "/admin/whatsapp-campaigns/templates"
TOKEN = "EAAB-secret-token-123"


# ───────────────────────────── fake Meta ─────────────────────────────

def body_comp(text, examples=None):
    c = {"type": "BODY", "text": text}
    if examples:
        c["example"] = {"body_text": [examples]}
    return c


def tpl(name, status="APPROVED", category="MARKETING", language="en", comps=None, **kw):
    return {"id": "id_" + name, "name": name, "status": status, "category": category, "language": language,
            "components": comps if comps is not None else [body_comp("Hello {{1}}, offer {{2}}", ["Asha", "20%"])], **kw}


def catalogue():
    return [
        tpl("offro_offer"),
        tpl("offro_media", comps=[{"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["https://x/y"]}},
                                  body_comp("Hi {{1}}", ["Asha"]),
                                  {"type": "FOOTER", "text": "Reply STOP to opt out"},
                                  {"type": "BUTTONS", "buttons": [
                                      {"type": "URL", "text": "Open", "url": "https://offro.in/m/{{1}}", "example": ["https://offro.in/m/abc"]},
                                      {"type": "QUICK_REPLY", "text": "Stop"}]}]),
        tpl("offro_utility", category="UTILITY"),
        tpl("offro_pending", status="PENDING"),
        tpl("offro_rejected", status="REJECTED", rejected_reason="INVALID_FORMAT"),
        tpl("offro_otp", category="AUTHENTICATION", comps=[body_comp("{{1}} is your code"),
                                                          {"type": "BUTTONS", "buttons": [{"type": "OTP", "otp_type": "COPY_CODE"}]}]),
        tpl("offro_hindi", language="hi"),
        tpl("offro_named", parameter_format="NAMED",
            comps=[{"type": "BODY", "text": "Hi {{first_name}}, {{offer}}",
                    "example": {"body_text_named_params": [{"param_name": "first_name", "example": "Asha"},
                                                           {"param_name": "offer", "example": "20% off"}]}}]),
        tpl("offro_novars", comps=[body_comp("Plain announcement")]),
    ]


class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code, self._d = status, data

    def json(self):
        if self._d is None:
            raise ValueError("no json")
        return self._d


class FakeMeta:
    """Serves `templates` in pages of `page_size`; records every request. `fail` = FakeResp/Exception to return."""
    def __init__(self, templates, page_size=100):
        self.templates, self.page_size, self.calls, self.fail = templates, page_size, [], None

    def __call__(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": dict(params or {}), "timeout": timeout})
        if isinstance(self.fail, Exception):
            raise self.fail
        if self.fail is not None:
            return self.fail
        start = int((params or {}).get("after") or 0)
        chunk = self.templates[start:start + self.page_size]
        data = {"data": chunk}
        if start + self.page_size < len(self.templates):
            data["paging"] = {"cursors": {"after": str(start + self.page_size)},
                              "next": "https://graph.facebook.com/next?access_token=" + TOKEN}
        return FakeResp(200, data)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in _DB.list_collection_names():
        _DB.drop_collection(name)
    for mod in (AUD, ST, DA, T):
        monkeypatch.setattr(mod, "db", _DB)
    monkeypatch.setattr(WA, "WHATSAPP_ACCESS_TOKEN", TOKEN)
    monkeypatch.setattr(WA, "WHATSAPP_PHONE_NUMBER_ID", "111222333")
    monkeypatch.setattr(WA, "WHATSAPP_BUSINESS_ACCOUNT_ID", "999888777")
    monkeypatch.setattr(T, "SYNC_COOLDOWN", timedelta(0))
    meta = FakeMeta(catalogue())
    monkeypatch.setattr(T.requests, "get", meta)
    return meta


@pytest.fixture
def meta(_env):
    return _env


app = FastAPI()
app.include_router(RC.router, prefix="/admin")
client = TestClient(app)


def make_user(token, perms=None):
    rid = _DB.dashboard_roles.insert_one({"role_name": "Custom Role", "permissions": perms or {}}).inserted_id
    _DB.dashboard_users.insert_one({"token": token, "role_id": rid, "full_name": "Asha Admin", "status": "active",
                                    "last_active_at": datetime.utcnow(), "mobile": "9000000001"})
    return token


def grant(*actions):
    return {RC.MODULE: {a: True for a in actions}}


def H(t):
    return {"Authorization": "Bearer " + t}


@pytest.fixture
def admin():
    return make_user("adm", grant("view", "add", "edit", "delete", "approve"))


def synced(admin):
    r = client.post(BASE + "/sync", headers=H(admin))
    assert r.status_code == 200, r.text
    return r.json()


def use(name, lang="en", token="adm", **body):
    return client.put("%s/%s/%s/campaign-use" % (BASE, name, lang), headers=H(token), json=body)


OFFER_MAP = {"body.1": {"source": "merchant_name", "fallback": "Valued merchant"}, "body.2": {"source": "static", "value": "20% off"}}


# ───────────────────────────── parsing ─────────────────────────────

def parse(name):
    return T.parse_template(next(t for t in catalogue() if t["name"] == name))


def test_positional_variables_and_meta_examples():
    p = parse("offro_offer")
    assert [(v["key"], v["example"]) for v in p["variables"]] == [("body.1", "Asha"), ("body.2", "20%")]
    assert p["campaign_supported"] and p["unsupported"] == [] and p["parameter_format"] == "POSITIONAL"


def test_named_variables_and_examples():
    p = parse("offro_named")
    assert p["parameter_format"] == "NAMED"
    assert [(v["key"], v["example"]) for v in p["variables"]] == [("body.first_name", "Asha"), ("body.offer", "20% off")]


def test_media_header_footer_and_button_variables():
    p = parse("offro_media")
    assert p["requires_header_media"] and p["header"]["format"] == "IMAGE"
    assert p["footer"]["text"] == "Reply STOP to opt out"
    assert [(v["key"], v["example"]) for v in p["variables"]] == [("body.1", "Asha"), ("button.0", "abc")]
    assert [b["type"] for b in p["buttons"]] == ["URL", "QUICK_REPLY"] and p["campaign_supported"]


def test_header_text_variable():
    raw = tpl("h", comps=[{"type": "HEADER", "format": "TEXT", "text": "Sale for {{1}}", "example": {"header_text": ["Asha"]}},
                          body_comp("Body")])
    p = T.parse_template(raw)
    assert p["variables"] == [{"key": "header.1", "component": "header", "name": "1", "example": "Asha"}]


@pytest.mark.parametrize("comps,flag", [
    ([body_comp("x"), {"type": "BUTTONS", "buttons": [{"type": "OTP"}]}], "button_otp"),
    ([body_comp("x"), {"type": "BUTTONS", "buttons": [{"type": "COPY_CODE", "example": "A"}]}], "button_copy_code"),
    ([body_comp("x"), {"type": "BUTTONS", "buttons": [{"type": "FLOW"}]}], "button_flow"),
    ([{"type": "HEADER", "format": "LOCATION"}, body_comp("x")], "header_format_location"),
    ([body_comp("x"), {"type": "CAROUSEL", "cards": []}], "component_carousel"),
    ([{"type": "FOOTER", "text": "f"}], "no_body"),
    ([body_comp("{{1}} {{3}}")], "non_sequential_variables_body"),
])
def test_unsupported_shapes_are_flagged_not_fatal(comps, flag):
    p = T.parse_template(tpl("u", comps=comps))
    assert flag in p["unsupported"] and p["campaign_supported"] is False


def test_garbage_components_do_not_crash():
    p = T.parse_template({"name": "g", "components": ["x", None, {"type": 5}, {"type": "BODY"}]})
    assert isinstance(p, dict)
    assert T.parse_template({"name": "g", "components": "nope"})["campaign_supported"] is False


def test_phone_button_number_is_masked():
    p = T.parse_template(tpl("p", comps=[body_comp("x"), {"type": "BUTTONS", "buttons": [
        {"type": "PHONE_NUMBER", "text": "Call", "phone_number": "+919876543210"}]}]))
    assert p["buttons"][0]["phone_masked"] == "***3210" and "9876543210" not in json.dumps(p)


@pytest.mark.parametrize("value,bad", [
    ("Asha", ""), ("", "empty"), ("   ", "empty"), (None, "empty"), (5, "empty"), ("a\nb", "contains_newline_or_tab"),
    ("a\tb", "contains_newline_or_tab"), ("a    b", "four_consecutive_spaces"), ("a   b", ""), ("x" * 1025, "too_long"),
])
def test_value_rules(value, bad):
    assert T.validate_value(value) == bad


def test_component_specific_length_limits():
    assert T.validate_value("x" * 61, "header") == "too_long" and T.validate_value("x" * 60, "header") == ""
    assert T.validate_value("x" * 1500, "button") == ""


# ───────────────────────────── preview / payload / resolution ─────────────────────────────

def test_preview_with_values_samples_and_missing():
    p = parse("offro_offer")
    r = T.render_preview(p, {"body.1": "Ravi"})
    assert r["preview"]["body"] == "Hello Ravi, offer 20%" and r["complete"] and r["missing_variables"] == []
    r = T.render_preview(p, {"body.1": "Ravi"}, use_samples=False)
    assert r["preview"]["body"] == "Hello Ravi, offer {{2}}" and r["missing_variables"] == ["body.2"] and not r["complete"]


def test_preview_flags_invalid_values_and_never_renders_them():
    r = T.render_preview(parse("offro_offer"), {"body.1": "a\nb", "body.2": "ok"})
    assert r["invalid_values"] == {"body.1": "contains_newline_or_tab"} and not r["complete"]
    assert "a\nb" not in json.dumps(r["preview"])


def test_preview_media_button_and_footer():
    r = T.render_preview(parse("offro_media"))
    assert r["preview"]["header"] == {"format": "IMAGE", "requires_media": True}
    assert r["preview"]["buttons"][0] == {"type": "URL", "text": "Open", "url": "https://offro.in/m/abc"}
    assert r["preview"]["footer"] == "Reply STOP to opt out" and r["requires_header_media"]


def test_preview_of_unsupported_template_is_never_complete():
    p = T.parse_template(tpl("u", comps=[body_comp("x"), {"type": "BUTTONS", "buttons": [{"type": "FLOW"}]}]))
    assert T.render_preview(p)["complete"] is False


def test_build_components_positional_named_and_url_button():
    assert T.build_send_components(parse("offro_offer"), {"body.1": "Asha", "body.2": "20%"}) == [
        {"type": "body", "parameters": [{"type": "text", "text": "Asha"}, {"type": "text", "text": "20%"}]}]
    named = T.build_send_components(parse("offro_named"), {"body.first_name": "Asha", "body.offer": "x"})
    assert named[0]["parameters"][0] == {"type": "text", "text": "Asha", "parameter_name": "first_name"}
    comps = T.build_send_components(parse("offro_media"), {"body.1": "Asha", "button.0": "abc"},
                                    header_media={"type": "image", "id": "12345"})
    assert comps[0] == {"type": "header", "parameters": [{"type": "image", "image": {"id": "12345"}}]}
    assert comps[2] == {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "abc"}]}
    assert T.build_send_components(parse("offro_novars"), {}) == []


@pytest.mark.parametrize("values,media,code", [
    ({"body.1": "Asha"}, None, "invalid_or_missing_values"),
    ({"body.1": "Asha", "body.2": "a\nb"}, None, "invalid_or_missing_values"),
])
def test_build_components_refuses_to_guess(values, media, code):
    with pytest.raises(T.ComponentError) as ei:
        T.build_send_components(parse("offro_offer"), values)
    assert str(ei.value) == code


def test_build_components_media_and_unsupported_rules():
    good = {"body.1": "A", "button.0": "x"}
    for media in (None, {}, {"type": "video", "id": "1"}, {"type": "image"}):
        with pytest.raises(T.ComponentError) as ei:
            T.build_send_components(parse("offro_media"), good, header_media=media)
        assert str(ei.value) == "header_media_required"
    with pytest.raises(T.ComponentError):
        T.build_send_components(parse("offro_otp"), {"body.1": "1"})


def test_resolve_values_sources_fallbacks_and_blanks():
    cu = {"variables": {"body.1": {"source": "merchant_name", "fallback": "Valued merchant"},
                        "body.2": {"source": "static", "value": "20%  off\n today"},
                        "body.3": {"source": "store_city"}, "bad": "x"}}
    assert T.resolve_values(cu, {"merchant_name": "Asha"}) == {"body.1": "Asha", "body.2": "20% off today"}
    assert T.resolve_values(cu, {"merchant_name": "  "})["body.1"] == "Valued merchant"
    assert T.resolve_values(cu, {})["body.1"] == "Valued merchant" and "body.3" not in T.resolve_values(cu, {})
    assert T.resolve_values(None, None) == {}


# ───────────────────────────── fetch from Meta ─────────────────────────────

def test_fetch_pages_with_cursor_and_never_follows_next_url(meta):
    meta.templates = [tpl("t%d" % i) for i in range(5)]
    meta.page_size = 2
    got = T.fetch_from_meta()
    assert [t["name"] for t in got] == ["t0", "t1", "t2", "t3", "t4"]
    assert [c["params"].get("after") for c in meta.calls] == [None, "2", "4"]
    assert all(c["url"].endswith("/999888777/message_templates") and "access_token" not in json.dumps(c["params"]) for c in meta.calls)
    assert all(c["headers"] == {"Authorization": "Bearer " + TOKEN} and c["timeout"] == (5, 15) for c in meta.calls)
    assert "components" in meta.calls[0]["params"]["fields"]


@pytest.mark.parametrize("missing", ["WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_BUSINESS_ACCOUNT_ID"])
def test_fetch_requires_full_configuration(monkeypatch, meta, missing):
    monkeypatch.setattr(WA, missing, "")
    with pytest.raises(T.TemplateSyncError) as ei:
        T.fetch_from_meta()
    assert ei.value.code == "not_configured" and meta.calls == []


@pytest.mark.parametrize("status,data,code", [
    (401, {"error": {"code": 190, "message": "Invalid OAuth access token EAAB-secret-token-123"}}, "meta_auth_failed"),
    (400, {"error": {"code": 190}}, "meta_auth_failed"),
    (429, {"error": {"code": 4}}, "meta_rate_limited"),
    (400, {"error": {"code": 80008}}, "meta_rate_limited"),
    (403, {"error": {"code": 200}}, "meta_permission_denied"),
    (400, {"error": {"code": 10}}, "meta_permission_denied"),
    (500, {"error": {"code": 2}}, "meta_error"),
    (400, {"error": {"code": 100}}, "meta_error"),
    (500, None, "meta_error"),
])
def test_meta_http_errors_are_classified(meta, status, data, code):
    meta.fail = FakeResp(status, data)
    with pytest.raises(T.TemplateSyncError) as ei:
        T.fetch_from_meta()
    assert ei.value.code == code and TOKEN not in ei.value.message


@pytest.mark.parametrize("exc", [TimeoutError("read timed out " + TOKEN), ConnectionError("down"), RuntimeError("boom")])
def test_network_failures_are_unreachable_and_leak_nothing(meta, exc, caplog):
    meta.fail = exc
    with pytest.raises(T.TemplateSyncError) as ei:
        T.fetch_from_meta()
    assert ei.value.code == "meta_unreachable" and TOKEN not in caplog.text and TOKEN not in str(ei.value)


@pytest.mark.parametrize("payload", [None, [], {"data": "x"}, {"nodata": 1}])
def test_unexpected_200_bodies_are_rejected(meta, payload):
    meta.fail = FakeResp(200, payload)
    with pytest.raises(T.TemplateSyncError) as ei:
        T.fetch_from_meta()
    assert ei.value.code == "meta_bad_response"


def test_runaway_pagination_is_bounded(monkeypatch, meta):
    monkeypatch.setattr(T, "MAX_PAGES", 3)
    meta.templates = [tpl("t%d" % i) for i in range(10)]
    meta.page_size = 1
    with pytest.raises(T.TemplateSyncError) as ei:
        T.fetch_from_meta()
    assert ei.value.code == "too_many_templates" and len(meta.calls) == 3


# ───────────────────────────── sync ─────────────────────────────

def test_first_sync_adds_everything_and_parses(admin):
    r = synced(admin)
    assert r["counts"]["fetched"] == r["counts"]["added"] == len(catalogue()) and r["counts"]["updated"] == 0
    d = _DB[T.TEMPLATES].find_one({"_id": "offro_offer|en"})
    assert d["status"] == "APPROVED" and d["category"] == "MARKETING" and d["parsed"]["campaign_supported"]
    assert d["campaign_use"] is None and d["content_hash"] and d["first_seen_at"]
    assert _DB[T.TEMPLATES].find_one({"_id": "offro_hindi|hi"})["language"] == "hi"
    assert _DB[T.TEMPLATES].find_one({"_id": "offro_rejected|en"})["rejected_reason"] == "INVALID_FORMAT"


def test_second_sync_is_idempotent(admin):
    synced(admin)
    r = synced(admin)
    assert r["counts"]["unchanged"] == len(catalogue()) and r["counts"]["added"] == 0 and r["counts"]["updated"] == 0
    assert _DB[T.TEMPLATES].count_documents({}) == len(catalogue())


def test_status_change_is_recorded_and_reported(admin, meta):
    synced(admin)
    meta.templates = [dict(t, status="PAUSED") if t["name"] == "offro_offer" else t for t in catalogue()]
    r = synced(admin)
    assert r["counts"]["status_changed"] == 1 and r["status_changes"] == [
        {"name": "offro_offer", "language": "en", "from": "APPROVED", "to": "PAUSED"}]
    d = _DB[T.TEMPLATES].find_one({"_id": "offro_offer|en"})
    assert d["status"] == "PAUSED" and d["previous_status"] == "APPROVED" and d["status_changed_at"]


def test_sync_never_overwrites_campaign_use_but_content_change_makes_it_stale(admin, meta):
    synced(admin)
    assert use("offro_offer", enabled=True, variables=OFFER_MAP).status_code == 200
    synced(admin)
    cu = _DB[T.TEMPLATES].find_one({"_id": "offro_offer|en"})["campaign_use"]
    assert cu["enabled"] is True and cu["variables"] == OFFER_MAP
    assert client.get(BASE + "/offro_offer/en", headers=H(admin)).json()["usable_for_campaign"] is True
    meta.templates = [tpl("offro_offer", comps=[body_comp("NEW TEXT {{1}} {{2}}", ["a", "b"])]) if t["name"] == "offro_offer" else t
                      for t in catalogue()]
    synced(admin)
    d = client.get(BASE + "/offro_offer/en", headers=H(admin)).json()
    assert d["campaign_use"]["enabled"] is True and d["campaign_use"]["stale"] is True
    assert d["usable_for_campaign"] is False and "mapping_stale" in d["readiness"]


def test_template_removed_at_meta_is_marked_missing_and_unusable_then_restored(admin, meta):
    synced(admin)
    use("offro_offer", enabled=True, variables=OFFER_MAP)
    meta.templates = [t for t in catalogue() if t["name"] != "offro_offer"]
    r = synced(admin)
    assert r["counts"]["marked_missing"] == 1
    d = client.get(BASE + "/offro_offer/en", headers=H(admin)).json()
    assert d["missing_from_meta"] and not d["usable_for_campaign"] and "missing_from_meta" in d["readiness"]
    meta.templates = catalogue()
    assert synced(admin)["counts"]["restored"] == 1
    assert client.get(BASE + "/offro_offer/en", headers=H(admin)).json()["usable_for_campaign"] is True


def test_failed_sync_changes_nothing_and_is_audited(admin, meta):
    synced(admin)
    before = list(_DB[T.TEMPLATES].find({}))
    meta.fail = FakeResp(401, {"error": {"code": 190, "message": "bad token " + TOKEN}})
    r = client.post(BASE + "/sync", headers=H(admin))
    assert r.status_code == 502 and r.json()["detail"]["code"] == "meta_auth_failed" and TOKEN not in r.text
    assert list(_DB[T.TEMPLATES].find({})) == before                    # nothing deleted, nothing marked missing
    a = [x for x in _DB.wa_campaign_audit.find({"action": "templates.sync"})]
    assert a[-1]["outcome"] == "failed:meta_auth_failed" and a[-1]["details"] == {"meta_code": 190}


def test_failure_midway_through_pages_writes_nothing(admin, meta):
    meta.templates = [tpl("t%d" % i) for i in range(4)]
    meta.page_size = 2
    real = meta.__call__
    state = {"n": 0}

    def flaky(url, headers=None, params=None, timeout=None):
        state["n"] += 1
        if state["n"] == 2:
            return FakeResp(500, {"error": {"code": 2}})
        return real(url, headers=headers, params=params, timeout=timeout)
    T.requests.get = flaky
    assert client.post(BASE + "/sync", headers=H(admin)).status_code == 502
    assert _DB[T.TEMPLATES].count_documents({}) == 0


def test_invalid_entries_are_skipped_not_fatal(admin, meta):
    meta.templates = [{"name": "", "language": "en"}, {"name": "x"}, tpl("ok")]
    assert synced(admin)["counts"] == {"fetched": 3, "added": 1, "updated": 0, "unchanged": 0, "status_changed": 0,
                                       "marked_missing": 0, "restored": 0, "skipped_invalid": 2}


def test_sync_cooldown_blocks_rapid_repeats_and_a_concurrent_claim(admin, monkeypatch, meta):
    monkeypatch.setattr(T, "SYNC_COOLDOWN", timedelta(seconds=30))
    synced(admin)
    n = len(meta.calls)
    r = client.post(BASE + "/sync", headers=H(admin))
    assert r.status_code == 429 and r.json()["detail"]["code"] == "sync_busy" and len(meta.calls) == n
    _DB[T.SETTINGS].update_one({"_id": T.SYNC_STATE_ID}, {"$set": {"next_allowed_at": datetime.utcnow() - timedelta(seconds=1)}})
    assert client.post(BASE + "/sync", headers=H(admin)).status_code == 200


def test_sync_storage_failure_is_503(admin, monkeypatch):
    monkeypatch.setattr(T, "db", _BrokenDb(_DB, {T.TEMPLATES: {"update_one"}}))
    r = client.post(BASE + "/sync", headers=H(admin))
    assert r.status_code == 503 and "9198" not in r.text


# ───────────────────────────── list / detail ─────────────────────────────

def test_list_filters_and_facets(admin):
    synced(admin)
    def names(**q):
        return [i["name"] + "|" + i["language"] for i in client.get(BASE, headers=H(admin), params=q).json()["items"]]
    assert len(names()) == len(catalogue())
    assert names(status="approved", category="marketing", language="en", q="OFFRO_O") == ["offro_offer|en"]
    assert names(status="REJECTED") == ["offro_rejected|en"] and names(language="hi") == ["offro_hindi|hi"]
    assert names(category="AUTHENTICATION") == ["offro_otp|en"] and names(q="zzz") == []
    assert names(campaign="usable") == [] and names(campaign="enabled") == []
    use("offro_offer", enabled=True, variables=OFFER_MAP)
    assert names(campaign="usable") == ["offro_offer|en"] == names(campaign="enabled")
    assert "offro_offer|en" not in names(campaign="not_ready")
    f = client.get(BASE, headers=H(admin)).json()["facets"]
    assert "PENDING" in f["statuses"] and "UTILITY" in f["categories"] and f["languages"] == ["en", "hi"]
    assert client.get(BASE, headers=H(admin), params={"campaign": "bogus"}).status_code == 422
    p = client.get(BASE, headers=H(admin), params={"page_size": 4, "page": 2}).json()
    assert len(p["items"]) == 4 and p["pages"] == 3 and p["total"] == len(catalogue())


def test_detail_shows_variables_readiness_and_unsupported(admin):
    synced(admin)
    d = client.get(BASE + "/offro_otp/en", headers=H(admin)).json()
    assert not d["campaign_supported"] and "button_otp" in d["unsupported"]
    assert {"category_not_allowed", "unsupported_components", "not_configured"} <= set(d["readiness"])
    d = client.get(BASE + "/offro_media/en", headers=H(admin)).json()
    assert d["requires_header_media"] and [v["key"] for v in d["parsed"]["variables"]] == ["body.1", "button.0"]
    assert set(d["variable_sources"]) == set(T.SOURCES)


@pytest.mark.parametrize("path", ["/nope/en", "/offro_offer/xx", "/OFFRO/en", "/offro_offer/en_US_extra_long", "/a.b/en"])
def test_unknown_or_malformed_template_is_404(admin, path):
    synced(admin)
    assert client.get(BASE + path, headers=H(admin)).status_code == 404


# ───────────────────────────── preview endpoint ─────────────────────────────

def test_preview_endpoint(admin):
    synced(admin)
    r = client.post(BASE + "/offro_offer/en/preview", headers=H(admin), json={"values": {"body.1": "Ravi"}})
    j = r.json()
    assert r.status_code == 200 and j["preview"]["body"] == "Hello Ravi, offer 20%" and j["sent"] is False and j["complete"]
    j = client.post(BASE + "/offro_offer/en/preview", headers=H(admin), json={"use_samples": False, "values": {"nope.1": "x"}}).json()
    assert j["missing_variables"] == ["body.1", "body.2"] and j["unknown_variables"] == ["nope.1"]
    assert client.post(BASE + "/offro_offer/en/preview", headers=H(admin)).json()["preview"]["body"] == "Hello Asha, offer 20%"
    for bad in ({"values": []}, {"values": {"body.1": 5}}, {"use_samples": "yes"}, []):
        assert client.post(BASE + "/offro_offer/en/preview", headers=H(admin), json=bad).status_code == 400


def test_preview_cannot_send_or_call_meta_again(admin, meta):
    synced(admin)
    n = len(meta.calls)
    client.post(BASE + "/offro_offer/en/preview", headers=H(admin), json={})
    assert len(meta.calls) == n


# ───────────────────────────── campaign use ─────────────────────────────

def test_enable_happy_path_stores_config_and_audits(admin):
    synced(admin)
    r = use("offro_offer", enabled=True, variables=OFFER_MAP, sample_values={"body.1": "Asha"})
    assert r.status_code == 200
    j = r.json()
    assert j["campaign_enabled"] and j["usable_for_campaign"] and j["readiness"] == [] and j["campaign_use"]["stale"] is False
    acts = [(a["action"], a["outcome"]) for a in _DB.wa_campaign_audit.find()]
    assert ("template.campaign_enable", "attempt") in acts and ("template.campaign_enable", "ok") in acts


def test_enable_needs_approve_but_saving_a_draft_and_disabling_do_not(admin):
    synced(admin)
    make_user("editor", grant("view", "edit"))
    assert use("offro_offer", token="editor", enabled=True, variables=OFFER_MAP).status_code == 403
    assert _DB[T.TEMPLATES].find_one({"_id": "offro_offer|en"})["campaign_use"] is None
    r = use("offro_offer", token="editor", enabled=False, variables={"body.1": {"source": "merchant_name", "fallback": "Hi"}})
    assert r.status_code == 200 and r.json()["campaign_enabled"] is False and not r.json()["usable_for_campaign"]
    assert use("offro_offer", enabled=True, variables=OFFER_MAP).status_code == 200
    assert use("offro_offer", token="editor", enabled=False, variables=OFFER_MAP).json()["campaign_enabled"] is False


@pytest.mark.parametrize("name,vars_,reason", [
    ("offro_utility", OFFER_MAP, "category_not_allowed"), ("offro_otp", {}, "category_not_allowed"),
    ("offro_pending", OFFER_MAP, "status_not_approved"), ("offro_rejected", OFFER_MAP, "status_not_approved"),
])
def test_cannot_enable_wrong_category_or_status(admin, name, vars_, reason):
    synced(admin)
    r = use(name, enabled=True, variables=vars_)
    assert r.status_code == 409 and reason in r.json()["detail"]["reasons"]
    assert _DB[T.TEMPLATES].find_one({"_id": name + "|en"})["campaign_use"] is None


def test_cannot_enable_with_unsupported_components(admin, meta):
    meta.templates = [tpl("flowy", comps=[body_comp("x"), {"type": "BUTTONS", "buttons": [{"type": "FLOW", "text": "Go"}]}])]
    synced(admin)
    r = use("flowy", enabled=True, variables={})
    assert r.status_code == 409 and "unsupported_components" in r.json()["detail"]["reasons"]


@pytest.mark.parametrize("variables,problem", [
    ({"body.1": OFFER_MAP["body.1"]}, "unmapped:body.2"),
    ({**OFFER_MAP, "body.9": {"source": "static", "value": "x"}}, "unknown_variable:body.9"),
    ({**OFFER_MAP, "body.2": {"source": "magic"}}, "bad_source:body.2"),
    ({**OFFER_MAP, "body.2": {"source": "static", "value": "a\nb"}}, "bad_static_value:body.2"),
    ({**OFFER_MAP, "body.2": {"source": "static"}}, "bad_static_value:body.2"),
    ({**OFFER_MAP, "body.1": {"source": "merchant_name"}}, "fallback_required:body.1"),
    ({**OFFER_MAP, "body.1": {"source": "merchant_name", "fallback": "  "}}, "fallback_required:body.1"),
])
def test_cannot_enable_with_incomplete_or_invalid_mapping(admin, variables, problem):
    synced(admin)
    r = use("offro_offer", enabled=True, variables=variables)
    assert r.status_code == 409 and problem in r.json()["detail"]["mapping_problems"]


def test_template_without_variables_can_be_enabled(admin):
    synced(admin)
    assert use("offro_novars", enabled=True, variables={}).json()["usable_for_campaign"] is True


def test_media_template_enable_flags_header_media_for_later_phase(admin):
    synced(admin)
    r = use("offro_media", enabled=True, variables={"body.1": {"source": "store_name", "fallback": "your store"},
                                                     "button.0": {"source": "static", "value": "abc"}})
    assert r.status_code == 200 and r.json()["requires_header_media"] is True


def test_bad_request_bodies(admin):
    synced(admin)
    for body in ({}, {"enabled": "yes"}, {"enabled": False, "variables": []}, {"enabled": False, "variables": {"a": 1}},
                 {"enabled": False, "sample_values": {"body.1": 5}}):
        assert client.put(BASE + "/offro_offer/en/campaign-use", headers=H(admin), json=body).status_code == 400
    assert client.put(BASE + "/offro_offer/en/campaign-use", headers=H(admin), content=b"x").status_code == 400
    r = use("offro_offer", enabled=False, variables={}, sample_values={"body.1": "a\nb"})
    assert r.status_code == 400 and r.json()["detail"]["invalid"] == {"body.1": "contains_newline_or_tab"}
    assert use("nope", enabled=False).status_code == 404


def test_enable_refused_when_audit_cannot_be_written(admin, monkeypatch):
    synced(admin)
    monkeypatch.setattr(AUD, "db", _BrokenDb(_DB, {AUD.AUDIT: {"insert_one"}}))
    r = use("offro_offer", enabled=True, variables=OFFER_MAP)
    assert r.status_code == 503 and _DB[T.TEMPLATES].find_one({"_id": "offro_offer|en"})["campaign_use"] is None


def test_save_failure_is_503(admin, monkeypatch):
    synced(admin)
    monkeypatch.setattr(T, "db", _BrokenDb(_DB, {T.TEMPLATES: {"update_one"}}))
    assert use("offro_offer", enabled=False, variables={}).status_code == 503


def test_enabled_template_goes_stale_and_unusable_when_status_changes(admin, meta):
    synced(admin)
    use("offro_offer", enabled=True, variables=OFFER_MAP)
    meta.templates = [dict(t, status="DISABLED") if t["name"] == "offro_offer" else t for t in catalogue()]
    synced(admin)
    d = client.get(BASE + "/offro_offer/en", headers=H(admin)).json()
    assert d["campaign_enabled"] and not d["usable_for_campaign"] and "status_not_approved" in d["readiness"]


# ───────────────────────────── permissions / privacy / safety ─────────────────────────────

def test_every_route_needs_the_campaign_module(admin):
    synced(admin)
    make_user("none", {"Merchants": {"view": True, "edit": True, "approve": True}})
    make_user("viewer", grant("view"))
    calls = [("post", "/sync"), ("get", ""), ("get", "/offro_offer/en"), ("post", "/offro_offer/en/preview"),
             ("put", "/offro_offer/en/campaign-use")]
    for method, path in calls:
        assert getattr(client, method)(BASE + path).status_code in (401, 403)
        assert client.request(method.upper(), BASE + path, headers=H("none"), json={}).status_code == 403, (method, path)
    assert client.post(BASE + "/sync", headers=H("viewer")).status_code == 403
    assert use("offro_offer", token="viewer", enabled=False, variables={}).status_code == 403
    for ok in (client.get(BASE, headers=H("viewer")), client.get(BASE + "/offro_offer/en", headers=H("viewer")),
               client.post(BASE + "/offro_offer/en/preview", headers=H("viewer"), json={})):
        assert ok.status_code == 200


def test_no_secret_or_full_number_is_ever_returned_or_stored(admin, meta):
    meta.templates = catalogue() + [tpl("callme", comps=[body_comp("x"), {"type": "BUTTONS", "buttons": [
        {"type": "PHONE_NUMBER", "text": "Call", "phone_number": "+919876543210"}]}])]
    synced(admin)
    texts = [client.get(BASE, headers=H(admin)).text, client.get(BASE + "/callme/en", headers=H(admin)).text,
             json.dumps(list(_DB.wa_campaign_audit.find()), default=str)]
    assert all(TOKEN not in t and "9876543210" not in t and "999888777" not in t for t in texts)


def test_phase_2c_code_cannot_send_anything():
    for path in ("services/wa_templates.py", "routers/wa_campaign_templates.py"):
        src = pathlib.Path(path).read_text()
        for forbidden in ("requests.post", "send_whatsapp", "wa.send_", "/messages", "BackgroundTasks", "threading",
                          "asyncio.create_task", "services.wa_chat", "wa_template_test"):
            assert forbidden not in src, (path, forbidden)
    assert pathlib.Path("services/wa_templates.py").read_text().count("requests.get(") == 1


def test_template_routes_are_namespaced_and_listed_exactly():
    paths = {(m, r.path) for r in RT.router.routes for m in r.methods}
    assert paths == {("POST", "/templates/sync"), ("GET", "/templates"), ("GET", "/templates/{name}/{language}"),
                     ("POST", "/templates/{name}/{language}/preview"), ("PUT", "/templates/{name}/{language}/campaign-use")}
    full = {r.path for r in app.routes if "/templates" in r.path}
    assert full and all(p.startswith("/admin/whatsapp-campaigns/templates") for p in full)


def test_no_startup_or_index_side_effects():
    src = pathlib.Path("services/wa_templates.py").read_text() + pathlib.Path("routers/wa_campaign_templates.py").read_text()
    assert "create_index" not in src and "ensure_indexes" not in src


def test_existing_template_test_helper_is_untouched_and_not_reused_for_sending():
    import routers.wa_template_test as TT
    assert TT.TEMPLATE_NAME == "offro_merchant_invitation" and hasattr(TT, "fetch_template_status")
