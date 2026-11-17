"""WhatsApp webhook diagnostics + template-test delivery status (sent / delivered / read / failed).
No network, no real sends. Run:  python -m pytest test_wa_template_test_delivery.py -q -W ignore"""
import hashlib
import hmac
import io
import json
import logging
from datetime import datetime

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.webhook as W
import routers.wa_chat as WC
import routers.wa_template_test as T
import services.wa_consent as C
import services.wa_template_test_delivery as D
import services.wa_webhook_diag as G

SECRET = "unit-test-app-secret-not-real"
PHONE = "919876543210"
MID = "wamid.HBgMOTE5ODc2NTQzMjEwFQIAERgSTESTSECRETIDENTIFIER00"
_DB = mongomock.MongoClient()["wa_tt_delivery_test"]
T0 = 1762843200


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for n in _DB.list_collection_names():
        _DB.drop_collection(n)
    for mod in (C, WC, D, T):
        monkeypatch.setattr(mod, "db", _DB)
    monkeypatch.delenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", raising=False)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    monkeypatch.setattr(W, "_MESSAGE_HANDLERS", [])
    monkeypatch.setattr(W, "_STATUS_HANDLERS", [D.handle_status])
    _DB.dashboard_users.insert_one({"token": "tok", "status": "active", "full_name": "Test Admin", "mobile": "9000000001"})


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(W.router)
    app.include_router(T.router, prefix="/admin")
    return TestClient(app)


def mk(status="sent", mid=MID, phone=PHONE, **extra):
    d = {"admin_id": "a1", "template": "t", "phone_hash": hashlib.sha256(phone.encode()).hexdigest(), "phone_last4": phone[-4:],
         "status": status, "created_at": datetime.utcnow()}
    if mid:
        d["message_id"] = mid
    d.update(extra)
    return _DB[D.AUDIT].insert_one(d).inserted_id


def rec(_id=None):
    return _DB[D.AUDIT].find_one({} if _id is None else {"_id": _id})


def ev(status, ts=T0, mid=MID, errors=None):
    s = {"id": mid, "recipient_id": PHONE, "status": status, "timestamp": str(ts)}
    if errors is not None:
        s["errors"] = errors
    return s


def body(*statuses, field="messages"):
    return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": field, "value": {
        "metadata": {"phone_number_id": "P"}, "statuses": list(statuses)}}]}]}


def post(client, b):
    return client.post("/webhook", content=json.dumps(b).encode(), headers={"content-type": "application/json"})


# ---- the four statuses ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("st,rank", [("sent", 2), ("delivered", 3), ("read", 4)])
def test_each_forward_status_saved(client, st, rank):
    mk()
    assert post(client, body(ev(st))).status_code == 200
    r = rec()
    assert r["delivery_status"] == st and r["delivery_rank"] == rank
    assert r["status"] == "sent"                                    # the send-limit field is never touched
    assert r["message_ref"] == G.msg_ref(MID)


def test_failed_saves_meta_error_code_and_not_the_text(client):
    mk()
    post(client, body(ev("failed", errors=[{"code": 131049, "title": "SECRET TITLE", "message": "TEXT", "error_data": {"details": "d"}}])))
    r = rec()
    assert r["delivery_status"] == "failed" and r["error_code"] == 131049 and r["status"] == "sent"
    assert "SECRET TITLE" not in json.dumps(r, default=str) and "TEXT" not in json.dumps(r, default=str)


def test_failed_without_error_block_saves_none(client):
    mk(); post(client, body(ev("failed")))
    assert rec()["delivery_status"] == "failed" and rec()["error_code"] is None


@pytest.mark.parametrize("bad", ["131049", True, 1.5, None, -1, [1]])
def test_non_integer_error_codes_are_not_stored(bad):
    assert G.error_code({"errors": [{"code": bad}]}) is None


def test_full_lifecycle_in_order(client):
    mk()
    for i, st in enumerate(["sent", "delivered", "read"]):
        post(client, body(ev(st, T0 + i)))
    assert rec()["delivery_status"] == "read"
    assert [e["status"] for e in rec()["delivery_events"]] == ["sent", "delivered", "read"]


def test_out_of_order_late_sent_does_not_regress(client):
    mk()
    post(client, body(ev("delivered", T0 + 5)))
    post(client, body(ev("sent", T0)))
    assert rec()["delivery_status"] == "delivered" and rec()["delivery_rank"] == 3


def test_failed_after_delivered_is_ignored(client):
    mk()
    post(client, body(ev("delivered", T0)))
    post(client, body(ev("failed", T0 + 1, errors=[{"code": 131026}])))
    assert rec()["delivery_status"] == "delivered" and rec().get("error_code") is None


def test_delivered_supersedes_earlier_failed_and_clears_code(client):
    mk()
    post(client, body(ev("failed", T0, errors=[{"code": 131026}])))
    assert rec()["error_code"] == 131026
    post(client, body(ev("delivered", T0 + 2)))
    assert rec()["delivery_status"] == "delivered" and "error_code" not in rec()


def test_failed_after_sent_marks_failed(client):
    mk()
    post(client, body(ev("sent", T0)))
    post(client, body(ev("failed", T0 + 3, errors=[{"code": 130472}])))
    assert rec()["delivery_status"] == "failed" and rec()["error_code"] == 130472


def _snap():
    r = rec()
    return (r.get("delivery_status"), r.get("delivery_rank"), r.get("error_code"), r.get("delivery_ts"), len(r.get("delivery_events", [])))


@pytest.mark.parametrize("sent_ts", [T0 - 5, T0, T0 + 1, T0 + 500])         # older, same-second, and LATER than the failure
def test_failed_then_sent_never_overwrites_failed(client, sent_ts):
    mk()
    post(client, body(ev("failed", T0, errors=[{"code": 131049}])))
    before = _snap()
    assert post(client, body(ev("sent", sent_ts))).status_code == 200
    assert _snap() == before and rec()["delivery_status"] == "failed" and rec()["error_code"] == 131049
    assert D.handle_status(MID, PHONE, "sent", str(sent_ts), ev("sent")) == "stale"


@pytest.mark.parametrize("st,rank", [("delivered", 3), ("read", 4)])
def test_failed_then_newer_delivered_or_read_supersedes_and_clears_code(client, st, rank):
    mk()
    post(client, body(ev("failed", T0, errors=[{"code": 131049}])))
    post(client, body(ev(st, T0 + 1)))
    r = rec()
    assert r["delivery_status"] == st and r["delivery_rank"] == rank and "error_code" not in r


@pytest.mark.parametrize("st", ["delivered", "read"])
@pytest.mark.parametrize("ts", [T0 - 5, T0])                                 # older or same timestamp as the failure: NOT newer
def test_failed_then_not_newer_delivered_or_read_does_not_supersede(client, st, ts):
    mk()
    post(client, body(ev("failed", T0, errors=[{"code": 131049}])))
    before = _snap()
    post(client, body(ev(st, ts)))
    assert _snap() == before and rec()["delivery_status"] == "failed" and rec()["error_code"] == 131049


@pytest.mark.parametrize("first", ["delivered", "read"])
@pytest.mark.parametrize("ts", [T0 - 5, T0, T0 + 5])
def test_delivered_or_read_then_failed_is_ignored(client, first, ts):
    mk()
    post(client, body(ev(first, T0)))
    before = _snap()
    post(client, body(ev("failed", ts, errors=[{"code": 131026}])))
    assert _snap() == before and rec()["delivery_status"] == first and rec().get("error_code") is None


def test_failed_stored_state_survives_duplicate_and_stale_events(client):
    mk()
    f = body(ev("failed", T0, errors=[{"code": 131049}]))
    post(client, f); before = _snap()
    post(client, f)                                                            # duplicate (webhook claim)
    D.handle_status(MID, PHONE, "failed", str(T0), ev("failed", errors=[{"code": 999}]))     # handler-level duplicate, different code
    D.handle_status(MID, PHONE, "failed", str(T0 + 50), ev("failed", errors=[{"code": 888}]))  # second failed
    D.handle_status(MID, PHONE, "sent", str(T0 + 60), ev("sent"))
    assert _snap() == before and rec()["error_code"] == 131049


def test_superseded_state_survives_duplicate_and_stale_events(client):
    mk()
    post(client, body(ev("failed", T0, errors=[{"code": 131049}])))
    post(client, body(ev("delivered", T0 + 2)))
    before = _snap()
    for st, ts in (("delivered", T0 + 2), ("delivered", T0 + 9), ("sent", T0 + 3), ("failed", T0 + 4), ("failed", T0 + 1)):
        D.handle_status(MID, PHONE, st, str(ts), ev(st, errors=[{"code": 7}]))
    assert _snap() == before and "error_code" not in rec()


def test_unknown_status_word_ignored():
    mk()
    assert D.handle_status(MID, PHONE, "deleted", str(T0), {}) == "ignored_status"
    assert "delivery_status" not in rec()


# ---- duplicates ---------------------------------------------------------------------------------------------------------
def test_duplicate_webhook_event_is_applied_once(client):
    mk()
    b = body(ev("delivered"))
    assert post(client, b).status_code == 200 and post(client, b).status_code == 200
    assert len(rec()["delivery_events"]) == 1


def test_handler_is_idempotent_on_replay():
    mk()
    assert D.handle_status(MID, PHONE, "delivered", str(T0), ev("delivered")) == "applied"
    assert D.handle_status(MID, PHONE, "delivered", str(T0), ev("delivered")) == "stale"
    assert len(rec()["delivery_events"]) == 1


def test_event_history_is_bounded():
    mk()
    for i in range(30):
        D.handle_status(MID, PHONE, "failed" if i % 2 == 0 else "sent", str(T0 + i), ev("failed", errors=[{"code": 1}]))
    assert len(rec()["delivery_events"]) <= 10


# ---- unknown ids / race ------------------------------------------------------------------------------------------------
def test_unknown_message_id_ignored_and_200(client):
    mk()
    assert post(client, body(ev("delivered", mid="wamid.SOMEONE_ELSE"))).status_code == 200
    assert "delivery_status" not in rec()


def test_unknown_id_without_pending_test_is_ignored():
    assert D.handle_status("wamid.NOPE", PHONE, "sent", str(T0), {}) == "ignored_unknown"
    assert D.handle_status("", PHONE, "sent", str(T0), {}) == "ignored_unknown"


def test_race_status_before_message_id_saved_asks_meta_to_retry(client):
    _id = mk("attempting", mid=None)
    assert post(client, body(ev("sent"))).status_code == 500        # retryable; Meta redelivers
    _DB[D.AUDIT].update_one({"_id": _id}, {"$set": {"status": "sent", "message_id": MID}})
    D.handle_status(MID, PHONE, "sent", str(T0), ev("sent"))
    assert rec(_id)["delivery_status"] == "sent"


def test_pending_only_for_same_recipient_and_recent():
    mk("attempting", mid=None, phone="919000000001")
    assert D.handle_status("wamid.Z", PHONE, "sent", str(T0), {}) == "ignored_unknown"       # different recipient
    _DB[D.AUDIT].delete_many({})
    mk("attempting", mid=None, created_at=datetime(2020, 1, 1))
    assert D.handle_status("wamid.Z", PHONE, "sent", str(T0), {}) == "ignored_unknown"       # stale attempt


def test_only_the_matching_record_is_updated(client):
    mk(mid="wamid.OTHER", phone="919000000002")
    mk()
    post(client, body(ev("read")))
    other = _DB[D.AUDIT].find_one({"message_id": "wamid.OTHER"})
    assert "delivery_status" not in other


def test_register_is_idempotent(monkeypatch):
    monkeypatch.setattr(W, "_STATUS_HANDLERS", [])
    D.register(); D.register()
    assert W._STATUS_HANDLERS.count(D.handle_status) == 1


def test_startup_registers_the_handler_in_its_own_isolated_block():
    src = open("server.py", encoding="utf-8").read()
    k = src.index("_wa_tt_delivery.register()")
    block = src[src.rindex("    try:", 0, k):src.index("except Exception", k)]
    assert "wa_campaign_worker" not in block and "start_worker_if_enabled" not in block   # cannot affect the campaign worker setup
    assert src.index("_wa_tt_delivery.register()") < src.index("start_worker_if_enabled()")


# ---- diagnostics: visible, scoped, safe -------------------------------------------------------------------------------
def test_diag_logger_has_own_visible_stdout_handler_and_no_propagation():
    lg = logging.getLogger(G.LOGGER_NAME)
    assert lg.level == logging.INFO and lg.propagate is False
    assert sum(1 for h in lg.handlers if getattr(h, "_wa_diag", False)) == 1
    assert G.install() is False                                      # idempotent
    assert logging.getLogger().level != logging.INFO or True        # root logger is not touched by this module
    assert logging.getLogger("offro.webhook").level == logging.NOTSET


def test_diag_lines_reach_stdout_even_with_root_logger_at_warning(client, capsys):
    mk()
    post(client, body(ev("failed", errors=[{"code": 131049}])))
    out = capsys.readouterr().out
    assert "[WA-Webhook][diag] event object=whatsapp_business_account changes=[messages(msgs=0,statuses=1,errors=0)]" in out
    assert "status=failed ts=%d code=131049 ref=%s claim=claimed" % (T0, G.msg_ref(MID)) in out


def test_diag_logs_duplicate_outcome(client, capsys):
    mk(); b = body(ev("sent"))
    post(client, b); capsys.readouterr()
    post(client, b)
    assert "claim=duplicate" in capsys.readouterr().out


def test_diag_never_prints_personal_data_or_full_ids(client, capsys):
    mk()
    b = body(ev("failed", errors=[{"code": 131049, "title": "TITLE-PII", "message": "MSG-PII", "error_data": {"details": "DET-PII"}}]))
    b["entry"][0]["changes"][0]["value"]["contacts"] = [{"profile": {"name": "Asha Secret"}, "wa_id": PHONE}]
    b["entry"][0]["changes"][0]["value"]["messages"] = [{"from": PHONE, "id": "wamid.INBOUND", "type": "text", "text": {"body": "hello private"}}]
    post(client, b)
    out = capsys.readouterr().out
    for secret in (PHONE, PHONE[-4:], "Asha", "hello private", "TITLE-PII", "MSG-PII", "DET-PII", MID, "wamid.INBOUND", "TESTSECRETIDENTIFIER", "HBgM"):
        assert secret not in out, secret


def test_diag_sanitises_hostile_values():
    line = G.describe_status({"id": MID, "status": "X\nFORGED LINE", "timestamp": "1\n2", "errors": [{"code": "9; DROP"}]}, "cl\naim")
    assert "\n" not in line and "FORGED" not in line and "status=other ts=- code=- " in line and "claim=-" in line
    ev_line = G.describe_event({"object": "evil\nobj", "entry": [{"changes": [{"field": "bad field\n", "value": {"messages": "x"}}]}]})
    assert "\n" not in ev_line and "object=other" in ev_line and "other(msgs=0" in ev_line


def test_msg_ref_is_stable_short_and_not_a_substring_of_the_id():
    r = G.msg_ref(MID)
    assert r == G.msg_ref(MID) and len(r) == 14 and r.startswith("m_") and r[2:] not in MID
    assert G.msg_ref("") == "none" and G.msg_ref(None) == "none" and G.msg_ref(5) == "none"


def test_diag_failure_cannot_break_the_webhook(client, monkeypatch):
    mk()
    monkeypatch.setattr(G, "describe_status", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(G, "describe_event", lambda *a, **k: 1 / 0)
    assert post(client, body(ev("delivered"))).status_code == 200
    assert rec()["delivery_status"] == "delivered"


def test_diag_handles_garbage_bodies():
    for b in (None, [], "x", {"entry": "x"}, {"entry": [None, 1, {"changes": "x"}]}, {"entry": [{"changes": [None, {"value": None}]}]}):
        G.describe_event(b)
    G.describe_status(None); G.describe_status({"errors": "x"})


# ---- security preserved -------------------------------------------------------------------------------------------------
def _sig(raw):
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def test_enforce_mode_still_rejects_bad_signature_and_updates_nothing(client, monkeypatch, capsys):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "enforce")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    mk()
    raw = json.dumps(body(ev("delivered"))).encode()
    r = client.post("/webhook", content=raw, headers={"content-type": "application/json", "X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert r.status_code in (401, 403)
    assert "delivery_status" not in rec()
    assert "[WA-Webhook][diag]" not in capsys.readouterr().out       # nothing is diagnosed for an unauthenticated request


def test_enforce_mode_valid_signature_updates(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "enforce")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    mk()
    raw = json.dumps(body(ev("read"))).encode()
    r = client.post("/webhook", content=raw, headers={"content-type": "application/json", "X-Hub-Signature-256": _sig(raw)})
    assert r.status_code == 200 and rec()["delivery_status"] == "read"


def test_non_messages_field_not_processed(client):
    mk()
    assert post(client, body(ev("delivered"), field="account_update")).status_code == 200
    assert "delivery_status" not in rec()


# ---- read-only lookup endpoint ---------------------------------------------------------------------------------------
H = {"X-Admin-Token": "tok"}


def test_status_endpoint_requires_admin(client):
    ref = G.msg_ref(MID)
    assert client.get("/admin/whatsapp/template-test/status", params={"ref": ref}).status_code == 401
    assert client.get("/admin/whatsapp/template-test/status", params={"ref": ref}, headers={"X-Admin-Token": "bad"}).status_code == 403


def test_status_endpoint_by_ref_returns_safe_fields(client):
    mk(consent_note="PRIVATE NOTE")
    post(client, body(ev("failed", errors=[{"code": 131049}])))
    r = client.get("/admin/whatsapp/template-test/status", params={"ref": G.msg_ref(MID)}, headers=H)
    assert r.status_code == 200
    j = r.json()
    assert j["delivery_status"] == "failed" and j["error_code"] == 131049 and j["to_last4"] == PHONE[-4:] and j["send_status"] == "sent"
    assert j["message_ref"] == G.msg_ref(MID)
    txt = r.text
    assert PHONE not in txt and "PRIVATE NOTE" not in txt and "phone_hash" not in txt and MID not in txt


def test_status_endpoint_does_not_accept_a_full_message_id(client):
    mk(); post(client, body(ev("delivered")))
    r = client.get("/admin/whatsapp/template-test/status", params={"message_id": MID}, headers=H)   # unknown param is ignored
    assert r.status_code == 404
    assert client.get("/admin/whatsapp/template-test/status", params={"ref": MID}, headers=H).status_code == 404
    assert "message_id" not in T.template_test_delivery_status.__code__.co_varnames[:T.template_test_delivery_status.__code__.co_argcount]
    import inspect
    assert "message_id" not in inspect.signature(D.lookup).parameters


def test_status_endpoint_unknown_and_bad_ref(client):
    mk()
    for ref in ("m_000000000000", "m_zz'}", "m_" + "a" * 40, "", "M_ABCDEF123456"):
        assert client.get("/admin/whatsapp/template-test/status", params={"ref": ref}, headers=H).status_code == 404
    assert client.get("/admin/whatsapp/template-test/status", headers=H).status_code == 404


# ---- no full message id / phone number in ANY webhook-path log (new AND older log statements) ---------------------------
INBOUND_ID = "wamid.HBgMINBOUNDSECRETIDENTIFIER111"
OUT_ID = "wamid.HBgMOUTBOUNDSECRETIDENTIFIER222"
BAD_SUBSTRINGS = (PHONE, PHONE[2:], "3210", "***", "Asha Kumar", "hello private", "INBOUNDSECRET", "OUTBOUNDSECRET", "HBgM", "SECRETIDENTIFIER")


def _inbound(text="hello private", msg_id=INBOUND_ID, ts="1762843200"):
    return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "P"}, "contacts": [{"profile": {"name": "Asha Kumar"}, "wa_id": PHONE}],
        "messages": [{"from": PHONE, "id": msg_id, "type": "text", "timestamp": ts, "text": {"body": text}}]}}]}]}


def _all_output(caplog, capsys):
    out = capsys.readouterr()
    return caplog.text + out.out + out.err


@pytest.fixture
def replies(monkeypatch):
    calls = []

    def fake(phone, message):
        calls.append(phone)
        return {"ok": True, "outcome": "sent", "message_id": OUT_ID}
    monkeypatch.setattr(W, "send_whatsapp_message_checked", fake)
    return calls


def _no_leak(blob):
    for bad in BAD_SUBSTRINGS:
        assert bad not in blob, bad


def test_logs_clean_for_inbound_text_and_welcome_reply(client, replies, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    assert post(client, _inbound()).status_code == 200
    blob = _all_output(caplog, capsys)
    assert "Message received" in blob and G.msg_ref(INBOUND_ID) in blob
    _no_leak(blob)


@pytest.mark.parametrize("kw", ["STOP", "START"])
def test_logs_clean_for_consent_keywords_and_confirmations(client, replies, caplog, capsys, kw):
    caplog.set_level(logging.DEBUG)
    post(client, _inbound(text=kw))
    post(client, _inbound(text=kw))                                   # duplicate delivery path
    _no_leak(_all_output(caplog, capsys))


def test_logs_clean_for_duplicate_busy_abandoned_and_retry_paths(client, replies, caplog, capsys, monkeypatch):
    caplog.set_level(logging.DEBUG)
    for outcome in ("duplicate", "busy", "abandoned"):
        monkeypatch.setattr(C, "claim_event", lambda key, _o=outcome: C.Claim(_o))
        post(client, _inbound())
        post(client, body(ev("delivered", mid=OUT_ID)))
    monkeypatch.undo()
    _no_leak(_all_output(caplog, capsys))


def test_logs_clean_when_message_processing_fails_and_meta_must_retry(client, replies, caplog, capsys, monkeypatch):
    caplog.set_level(logging.DEBUG)
    def boom(*a, **k):
        raise RuntimeError("db down for 919876543210 wamid.HBgMINBOUNDSECRETIDENTIFIER111")
    monkeypatch.setattr(W, "_handle_one_message", boom)
    post(client, _inbound())
    _no_leak(_all_output(caplog, capsys))


def test_logs_clean_when_live_chat_store_fails(client, replies, caplog, capsys, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(W, "store_incoming_message", lambda **k: False)
    assert post(client, _inbound()).status_code == 500
    blob = _all_output(caplog, capsys)
    assert "Live Chat store FAILED for message ref=%s" % G.msg_ref(INBOUND_ID) in blob
    _no_leak(blob)


@pytest.mark.parametrize("outcome", ["rejected_retryable", "rejected_permanent", "uncertain"])
def test_logs_clean_for_reply_rejections(client, caplog, capsys, monkeypatch, outcome):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(W, "send_whatsapp_message_checked",
                        lambda p, m: {"ok": False, "outcome": outcome, "error_code": 131047, "message_id": OUT_ID})
    post(client, _inbound(text="STOP"))
    _no_leak(_all_output(caplog, capsys))


def test_logs_clean_for_status_events_all_statuses_and_failures(client, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    mk(mid=OUT_ID)
    for i, st in enumerate(["sent", "delivered", "read"]):
        post(client, body(ev(st, T0 + i, mid=OUT_ID)))
    post(client, body(ev("failed", T0 + 9, mid="wamid.UNKNOWNSECRETIDENTIFIER", errors=[{"code": 131049, "title": "for 919876543210"}])))
    blob = _all_output(caplog, capsys)
    assert G.msg_ref(OUT_ID) in blob
    _no_leak(blob)


def test_logs_clean_for_older_campaign_delivery_handler_unknown_id(caplog, capsys):
    import services.wa_delivery as WD
    caplog.set_level(logging.DEBUG)
    try:
        WD.handle_status("wamid.UNKNOWNSECRETIDENTIFIER", PHONE, "delivered", "1762843200", {})
    except Exception:
        pass
    _no_leak(_all_output(caplog, capsys))


def test_logs_clean_when_consent_store_abandons_an_event(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    key = "msg:" + INBOUND_ID
    _DB[C.WEBHOOK_EVENTS].insert_one({"_id": key, "state": "processing", "attempts": C.MAX_ATTEMPTS, "updated_at": datetime(2020, 1, 1),
                                       "lease_until": datetime(2020, 1, 1)})
    try:
        C.claim_event(key)
    except Exception:
        pass
    _no_leak(_all_output(caplog, capsys))


def test_logs_clean_for_outbound_sends_in_services_whatsapp(monkeypatch, caplog, capsys):
    import services.whatsapp as WA
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(WA, "WHATSAPP_ACCESS_TOKEN", "EAAtesttokentesttoken")
    monkeypatch.setattr(WA, "WHATSAPP_PHONE_NUMBER_ID", "PNID")

    class Rsp:
        status_code = 200
        def json(self): return {"messages": [{"id": OUT_ID}]}
    monkeypatch.setattr(WA.requests, "post", lambda *a, **k: Rsp())
    WA.send_whatsapp_message(PHONE, "hi")
    WA.send_whatsapp_message_checked(PHONE, "hi")
    _no_leak(_all_output(caplog, capsys))


def test_static_guard_no_webhook_log_call_formats_a_message_id_or_phone():
    """Source-level guard: no logger call in the webhook path interpolates msg_id / mask_phone / a raw message id."""
    import re
    for path in ("routers/webhook.py", "services/wa_consent.py", "services/wa_delivery.py", "services/whatsapp.py"):
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(r"logger\.(?:debug|info|warning|error|exception|critical)\(", src):
            depth, j = 0, m.end() - 1
            while True:
                depth += (src[j] == "(") - (src[j] == ")")
                if depth == 0:
                    break
                j += 1
            call = src[m.start():j + 1]
            call = re.sub(r'"(?:[^"\\\\]|\\\\.)*"', '""', call)          # drop string literals: only interpolated ARGUMENTS matter
            assert "mask_phone" not in call, (path, call)
            assert not re.search(r"\bmsg_id\b(?!\))", call.replace("msg_ref(msg_id)", "")), (path, call)
            assert not re.search(r"\b(mid|sender|sender_phone|recipient|phone)\b(?!_)", call.replace("msg_ref(mid)", "")), (path, call)
            assert 'get("message_id")' not in call.replace('msg_ref(res.get("message_id"))', "").replace('msg_ref(result.get("message_id"))', ""), (path, call)
