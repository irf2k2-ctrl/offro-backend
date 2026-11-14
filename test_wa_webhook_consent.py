"""WhatsApp webhook Phase 1: Meta signature verification (off/log/enforce), log masking, STOP/START consent,
webhook de-duplication, and preservation of Live Chat + the welcome reply. No network: every outgoing send is faked.
Run:  python -m pytest test_wa_webhook_consent.py -q -W ignore"""
import hashlib
import hmac
import json
import logging

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.webhook as W
import routers.wa_chat as WC
import services.wa_consent as C
import services.wa_webhook_security as S
import services.whatsapp as WA

SECRET = "unit-test-app-secret-not-real"
SENDER = "919876543210"


# Private DB (same approach as test_wa_template_test.py): immune to other test modules that replace
# sys.modules["database"]; only the modules under test are pointed at it.
_DB = mongomock.MongoClient()["wa_webhook_consent_test"]


def _db():
    return _DB


@pytest.fixture
def sent(monkeypatch):
    """Fake the Graph API: record every outgoing reply instead of sending it."""
    calls = []

    def fake(phone, message):
        calls.append((phone, message))
        return {"ok": True, "outcome": "sent", "message_id": "wamid.OUT%d" % len(calls)}

    monkeypatch.setattr(W, "send_whatsapp_message_checked", fake)
    return calls


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in _DB.list_collection_names():
        _DB.drop_collection(name)
    monkeypatch.setattr(C, "db", _DB)
    monkeypatch.setattr(WC, "db", _DB)
    monkeypatch.delenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", raising=False)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    monkeypatch.setattr(W, "_MESSAGE_HANDLERS", [])
    monkeypatch.setattr(W, "_STATUS_HANDLERS", [])


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(W.router)
    return TestClient(app)


def payload(text="Hello", msg_id="wamid.IN1", sender=SENDER, name="Asha", mtype="text", extra=None, ts="1762843200"):
    msg = {"from": sender, "id": msg_id, "type": mtype, "timestamp": ts}
    if mtype == "text":
        msg["text"] = {"body": text}
    if extra:
        msg.update(extra)
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "PNID", "display_phone_number": "1"},
        "contacts": [{"wa_id": sender, "profile": {"name": name}}], "messages": [msg]}}]}]}


def status_payload(msg_id="wamid.OUT1", status="delivered", ts="1762843300"):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "PNID"},
        "statuses": [{"id": msg_id, "recipient_id": SENDER, "status": status, "timestamp": ts}]}}]}]}


def sign(raw: bytes, secret=SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def post(client, body, signature=None):
    raw = json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    if signature:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/webhook", content=raw, headers=headers)


def chats():
    return list(_db().whatsapp_chats.find({}))


# ───────────────────────────── signature primitives ─────────────────────────────

def test_valid_signature_verifies():
    raw = b'{"a":1}'
    assert S.verify_signature(raw, sign(raw), SECRET)
    assert S.verify_signature(raw, sign(raw).upper().replace("SHA256=", "sha256="), SECRET)  # hex case-insensitive


@pytest.mark.parametrize("hdr", [None, "", "sha256=", "sha1=" + "a" * 40, "deadbeef", "sha256=" + "0" * 64])
def test_invalid_or_malformed_signature_rejected(hdr):
    assert not S.verify_signature(b'{"a":1}', hdr, SECRET)


def test_tampered_body_and_wrong_secret_rejected():
    raw = b'{"a":1}'
    assert not S.verify_signature(b'{"a":2}', sign(raw), SECRET)
    assert not S.verify_signature(raw, sign(raw, "other-secret"), SECRET)


def test_empty_secret_never_verifies():
    raw = b"x"
    assert not S.verify_signature(raw, sign(raw, ""), "")


def test_secret_with_stray_whitespace_still_verifies(monkeypatch):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "enforce")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "  " + SECRET + "\n")
    raw = b'{"a":1}'
    assert S.check_signature(raw, sign(raw)) == (True, "ok")


def test_tokens_equal_constant_time_semantics():
    assert S.tokens_equal("abc", "abc")
    assert not S.tokens_equal("abc", "abd")
    assert not S.tokens_equal(None, "abc") and not S.tokens_equal("abc", None)
    assert S.tokens_equal("tökén", "tökén")          # non-ASCII must not raise


# ───────────────────────────── signature modes (end to end) ─────────────────────────────

def test_default_mode_is_off_and_ignores_signatures(client, sent):
    assert S.signature_mode() == "off"
    r = post(client, payload(), signature="sha256=" + "0" * 64)
    assert r.status_code == 200 and len(chats()) == 1


@pytest.mark.parametrize("val", ["enforced", "enfroce", "on", "true", "1", "strict", "of", "logging", "disabled", "off!"])
def test_unknown_mode_value_resolves_to_log_never_off(client, sent, monkeypatch, caplog, val):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", val)
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    assert S.signature_mode() == "log"                      # never "off"
    assert "Invalid WHATSAPP_WEBHOOK_SIGNATURE_MODE" in caplog.text and caplog.records[0].levelname == "ERROR"
    # still verifies (a bad signature is reported) but does not block
    caplog.clear()
    r = post(client, payload(), signature="sha256=" + "0" * 64)
    assert r.status_code == 200 and len(chats()) == 1 and "mismatch" in caplog.text


def test_invalid_mode_error_is_reported_once_per_value(monkeypatch, caplog):
    monkeypatch.setattr(S, "_reported_bad_modes", set())
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "enforcd")
    for _ in range(5):
        S.signature_mode()
    assert caplog.text.count("Invalid WHATSAPP_WEBHOOK_SIGNATURE_MODE") == 1


@pytest.mark.parametrize("val,expected", [("", "off"), ("   ", "off"), ("off", "off"), ("OFF", "off"), (" Off ", "off"),
                                          ("log", "log"), ("LOG", "log"), ("  Log\n", "log"),
                                          ("enforce", "enforce"), ("ENFORCE", "enforce"), (" Enforce ", "enforce")])
def test_valid_mode_values_case_insensitive_and_trimmed(monkeypatch, val, expected):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", val)
    assert S.signature_mode() == expected


def test_unset_mode_is_off(monkeypatch):
    monkeypatch.delenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", raising=False)
    assert S.signature_mode() == "off"


def test_off_mode_works_without_secret(client, sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "off")
    assert post(client, payload()).status_code == 200 and len(chats()) == 1


def test_log_mode_accepts_everything_but_logs_reason(client, sent, monkeypatch, caplog):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "log")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    caplog.set_level(logging.INFO)
    r = post(client, payload(msg_id="m-bad"), signature="sha256=" + "0" * 64)
    assert r.status_code == 200 and len(chats()) == 1
    assert "mismatch" in caplog.text and "accepted" in caplog.text
    caplog.clear()
    assert post(client, payload(msg_id="m-none")).status_code == 200            # header missing
    assert "header_missing" in caplog.text
    raw = json.dumps(payload(msg_id="m-ok")).encode()
    caplog.clear()
    assert client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": sign(raw)}).status_code == 200
    assert "REJECTED" not in caplog.text and "mismatch" not in caplog.text
    assert "Signature check (log mode): ok" in caplog.text
    assert len(chats()) == 3
    assert SECRET not in caplog.text


def test_log_mode_without_secret_accepts_and_says_so(client, sent, monkeypatch, caplog):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "log")
    r = post(client, payload())
    assert r.status_code == 200 and len(chats()) == 1
    assert "secret_missing" in caplog.text


def test_enforce_valid_signature_processes(client, sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "enforce")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    raw = json.dumps(payload()).encode()
    r = client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": sign(raw)})
    assert r.status_code == 200 and len(chats()) == 1 and len(sent) == 1


@pytest.mark.parametrize("hdr", [None, "sha256=" + "0" * 64, "garbage"])
def test_enforce_rejects_missing_or_invalid_signature(client, sent, monkeypatch, hdr):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "enforce")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    r = post(client, payload(), signature=hdr)
    assert r.status_code == 403
    assert chats() == [] and sent == [] and _db().wa_webhook_events.count_documents({}) == 0


def test_enforce_rejects_when_secret_missing_fail_closed(client, sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "enforce")
    r = post(client, payload(), signature=sign(b"whatever"))
    assert r.status_code == 403 and chats() == [] and sent == []


def test_enforce_signature_must_cover_exact_bytes(client, sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "enforce")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    signed = json.dumps(payload(text="Hello")).encode()
    forged = json.dumps(payload(text="STOP")).encode()
    r = client.post("/webhook", content=forged, headers={"X-Hub-Signature-256": sign(signed)})
    assert r.status_code == 403 and _db().wa_contacts.count_documents({}) == 0


# ───────────────────────────── verify-token handshake ─────────────────────────────

def test_verify_handshake_unchanged_behaviour(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": WA.WHATSAPP_VERIFY_TOKEN,
                                        "hub.challenge": "CH123"})
    assert ok.status_code == 200 and ok.text == "CH123"
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                          "hub.challenge": "x"}).status_code == 403
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.challenge": "x"}).status_code == 403
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "tökén",
                                          "hub.challenge": "x"}).status_code == 403


def test_verify_token_value_and_fallback_untouched():
    assert WA.WHATSAPP_VERIFY_TOKEN  # existing env/fallback logic still in services/whatsapp.py
    src = open(WA.__file__, encoding="utf-8").read()
    assert 'os.getenv("WHATSAPP_VERIFY_TOKEN", "offro_webhook_secret")' in src


# ───────────────────────────── ordinary messages: Live Chat + welcome unchanged ─────────────────────────────

def test_ordinary_message_goes_to_live_chat_and_gets_welcome(client, sent):
    r = post(client, payload(text="Hi, what offers do you have?"))
    assert r.status_code == 200
    cust = _db().customers_whatsapp.find_one({"phone_number": SENDER})
    assert cust and cust["name"] == "Asha" and cust["unread_count"] == 1
    msgs = chats()
    assert len(msgs) == 1 and msgs[0]["direction"] == "incoming" and msgs[0]["message"].startswith("Hi, what offers")
    assert len(sent) == 1 and sent[0][0] == SENDER
    assert sent[0][1] == ("Hello Asha! 👋 Welcome to OffrO.\n\n"
                          "We have received your message. Our team will get back to you shortly.\n\n"
                          "📲 _OffrO — Discover local stores & exclusive deals_")
    assert _db().wa_contacts.count_documents({}) == 0 and _db().wa_consent_events.count_documents({}) == 0


@pytest.mark.parametrize("text", ["please do not stop the offers", "start now", "stopping by", "Stop it please",
                                  "I want to cancel my order", "the end"])
def test_keyword_inside_a_sentence_is_ordinary(client, sent, text):
    post(client, payload(text=text))
    assert len(sent) == 1 and sent[0][1].startswith("Hello Asha! 👋 Welcome")
    assert _db().wa_contacts.count_documents({}) == 0


def test_non_text_message_stored_without_welcome(client, sent):
    post(client, payload(mtype="image", extra={"image": {"id": ""}}))
    assert len(chats()) == 1 and sent == []


def test_message_handlers_still_called(client, sent):
    seen = []
    W._MESSAGE_HANDLERS.append(lambda **kw: seen.append((kw["text"], kw["sender"])))
    post(client, payload(text="Hello"))
    assert seen == [("Hello", SENDER)]


def test_non_json_and_wrong_object_still_200(client, sent):
    assert client.post("/webhook", content=b"not json").status_code == 200
    assert client.post("/webhook", content=b"[1,2]").status_code == 200
    assert post(client, {"object": "page", "entry": []}).status_code == 200
    assert chats() == [] and sent == []


# ───────────────────────────── STOP ─────────────────────────────

@pytest.mark.parametrize("word", ["STOP", "stop", "Stop", " stop ", "UNSUBSCRIBE", "Unsubscribe!", "cancel", "END", "quit", "Stop."])
def test_stop_keywords_opt_out_confirm_and_no_welcome(client, sent, word):
    r = post(client, payload(text=word, msg_id="wamid.S-" + word))
    assert r.status_code == 200
    contact = _db().wa_contacts.find_one({"phone": SENDER})
    assert contact["status"] == "opted_out" and contact["phone_last4"] == "3210"
    ev = list(_db().wa_consent_events.find({"phone": SENDER}))
    assert len(ev) == 1 and ev[0]["event"] == "opt_out" and ev[0]["message_id"] == "wamid.S-" + word
    assert ev[0]["source"] == "whatsapp_keyword"
    # still visible in Live Chat
    assert len(chats()) == 1 and chats()[0]["message"] == word
    # exactly one reply, and it is the unsubscribe confirmation (never the welcome)
    assert sent == [(SENDER, C.STOP_CONFIRMATION)]
    assert "Welcome to OffrO" not in sent[0][1]
    assert C.is_opted_out(SENDER)


def test_stop_via_button_reply_also_opts_out(client, sent):
    post(client, payload(mtype="button", msg_id="wamid.B1", extra={"button": {"text": "Stop", "payload": "STOP"}}))
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_out"
    assert sent == [(SENDER, C.STOP_CONFIRMATION)] and len(chats()) == 1


def test_stop_handlers_still_called(client, sent):
    seen = []
    W._MESSAGE_HANDLERS.append(lambda **kw: seen.append(kw["text"]))
    post(client, payload(text="STOP"))
    assert seen == ["STOP"]


# ───────────────────────────── START ─────────────────────────────

def test_start_after_stop_records_consent_evidence(client, sent):
    post(client, payload(text="STOP", msg_id="wamid.S1", ts="1762843200"))
    sent.clear()
    post(client, payload(text="start", msg_id="wamid.ST1", ts="1762843260"))
    contact = _db().wa_contacts.find_one({"phone": SENDER})
    assert contact["status"] == "opted_in" and contact["last_consent"]["message_id"] == "wamid.ST1"
    events = list(_db().wa_consent_events.find({"phone": SENDER}).sort("at", 1))
    assert [e["event"] for e in events] == ["opt_out", "opt_in"]
    assert events[1]["message_id"] == "wamid.ST1" and events[1]["source"] == "whatsapp_keyword"
    assert events[1]["text"] == "start" and events[1]["wa_timestamp"] == "1762843260"
    assert sent == [(SENDER, C.START_CONFIRMATION)]
    assert not C.is_opted_out(SENDER)
    assert len(chats()) == 2            # both are in Live Chat


def test_start_from_unknown_number_is_explicit_opt_in_only_for_that_number(client, sent):
    _db().users.insert_one({"phone": "9111111111", "name": "Existing"})
    _db().merchants.insert_one({"phone": "9222222222", "name": "Merchant"})
    post(client, payload(text="START", msg_id="wamid.ST2"))
    assert _db().wa_contacts.count_documents({}) == 1
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_in"


# ───────────────────────────── no automatic opt-in of existing records ─────────────────────────────

def test_existing_records_are_never_opted_in(client, sent):
    _db().users.insert_many([{"phone": "9111111111"}, {"phone": "919222222222"}])
    _db().merchants.insert_one({"phone": "9333333333"})
    _db().accounts.insert_one({"phone": "9444444444", "roles": ["merchant"]})
    _db().customers_whatsapp.insert_one({"phone_number": "919555555555", "name": "Live chat contact"})
    C.ensure_indexes()
    post(client, payload(text="Hello"))
    assert _db().wa_contacts.count_documents({}) == 0
    assert _db().wa_consent_events.count_documents({}) == 0
    assert not C.is_opted_out("919555555555")      # unknown is neither opted out ...
    assert _db().wa_contacts.find_one({"phone": "919555555555"}) is None   # ... nor opted in


# ───────────────────────────── de-duplication ─────────────────────────────

def test_duplicate_ordinary_delivery_single_welcome_single_chat(client, sent):
    body = payload(text="Hello", msg_id="wamid.DUP1")
    assert post(client, body).status_code == 200
    assert post(client, body).status_code == 200
    assert len(sent) == 1 and len(chats()) == 1
    assert _db().customers_whatsapp.find_one({"phone_number": SENDER})["unread_count"] == 1


def test_duplicate_stop_delivery_single_confirmation_single_event(client, sent):
    body = payload(text="STOP", msg_id="wamid.DUP2")
    post(client, body)
    post(client, body)
    post(client, body)
    assert sent == [(SENDER, C.STOP_CONFIRMATION)]
    assert _db().wa_consent_events.count_documents({}) == 1 and len(chats()) == 1


def test_different_messages_are_not_deduplicated(client, sent):
    post(client, payload(msg_id="wamid.A"))
    post(client, payload(msg_id="wamid.B"))
    assert len(sent) == 2 and len(chats()) == 2


def test_duplicate_status_update_handled_once(client, sent):
    seen = []
    W._STATUS_HANDLERS.append(lambda **kw: seen.append(kw["status"]))
    post(client, status_payload())
    post(client, status_payload())
    post(client, status_payload(status="read"))
    assert seen == ["delivered", "read"]


def test_dedupe_store_down_still_stores_chat_but_sends_no_auto_reply(client, sent, monkeypatch):
    monkeypatch.setattr(C, "claim_event", lambda key: "unavailable")
    post(client, payload(text="Hello"))
    assert len(chats()) == 1 and sent == []
    post(client, payload(text="STOP", msg_id="wamid.S9"))
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_out"   # consent still recorded
    assert sent == []                                                                # but no confirmation that could double


def test_processing_crash_returns_500_and_meta_retry_is_processed_once(client, sent, monkeypatch):
    calls = {"n": 0}
    real = W._handle_one_message

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("unexpected")
        return real(*a, **k)

    monkeypatch.setattr(W, "_handle_one_message", boom)
    body = payload(msg_id="wamid.CRASH")
    assert post(client, body).status_code == 500                   # Meta must retry
    ev = _db().wa_webhook_events.find_one({"_id": "msg:wamid.CRASH"})
    assert ev["state"] == "failed" and ev["attempts"] == 1
    assert post(client, body).status_code == 200                   # retry is processed ...
    assert len(chats()) == 1 and len(sent) == 1
    assert post(client, body).status_code == 200                   # ... and a third delivery is a duplicate
    assert len(chats()) == 1 and len(sent) == 1
    assert _db().wa_webhook_events.find_one({"_id": "msg:wamid.CRASH"})["state"] == "done"


def test_retries_are_bounded_then_200(client, sent, monkeypatch):
    monkeypatch.setattr(W, "_handle_one_message", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("always")))
    body = payload(msg_id="wamid.POISON")
    codes = [post(client, body).status_code for _ in range(5)]
    assert codes == [500, 500, 500, 200, 200]                      # MAX_ATTEMPTS=3, then give up
    assert _db().wa_webhook_events.find_one({"_id": "msg:wamid.POISON"})["attempts"] == 3
    assert sent == []


def test_one_bad_message_does_not_discard_the_others(client, sent, monkeypatch):
    good1 = payload(msg_id="wamid.G1", text="one")["entry"][0]["changes"][0]["value"]["messages"][0]
    bad = payload(msg_id="wamid.BAD1", text="two")["entry"][0]["changes"][0]["value"]["messages"][0]
    good2 = payload(msg_id="wamid.G2", text="three")["entry"][0]["changes"][0]["value"]["messages"][0]
    body = payload()
    body["entry"][0]["changes"][0]["value"]["messages"] = [good1, "not-a-dict", None, 7, bad, good2]
    real_store = W.store_incoming_message

    def store(**kw):
        if kw["whatsapp_msg_id"] == "wamid.BAD1":
            raise RuntimeError("db exploded for 919876543210")
        return real_store(**kw)

    monkeypatch.setattr(W, "store_incoming_message", store)
    assert post(client, body).status_code == 500                   # the failed one needs a retry
    assert sorted(c["whatsapp_msg_id"] for c in chats()) == ["wamid.G1", "wamid.G2"]
    assert len(sent) == 2                                          # both good ones got their single welcome
    monkeypatch.setattr(W, "store_incoming_message", real_store)
    sent.clear()
    assert post(client, body).status_code == 200                   # retry: only the failed one is processed
    assert sorted(c["whatsapp_msg_id"] for c in chats()) == ["wamid.BAD1", "wamid.G1", "wamid.G2"]
    assert len(sent) == 1


def test_malformed_sections_do_not_break_other_sections(client, sent):
    body = {"object": "whatsapp_business_account", "entry": ["x", {"changes": "nope"}, {"changes": [
        {"field": "messages", "value": "bad"},
        {"field": "messages", "value": {"contacts": [{"nope": 1}, "x", {"wa_id": SENDER, "profile": "str"}],
                                        "messages": [payload(msg_id="wamid.M1")["entry"][0]["changes"][0]["value"]["messages"][0]],
                                        "statuses": ["x", {"id": "wamid.O", "status": "read", "timestamp": "1"}],
                                        "errors": ["x", {"code": 1, "error_data": None}]}}]}]}
    assert post(client, body).status_code == 200
    assert len(chats()) == 1 and len(sent) == 1


def test_live_chat_store_failure_requests_retry_and_replies_exactly_once(client, sent, monkeypatch):
    real = W.store_incoming_message
    calls = {"n": 0}

    def store(**kw):
        calls["n"] += 1
        return False if calls["n"] == 1 else real(**kw)

    monkeypatch.setattr(W, "store_incoming_message", store)
    body = payload(msg_id="wamid.LC1")
    assert post(client, body).status_code == 500
    assert chats() == [] and sent == []                            # nothing stored, no welcome yet
    assert post(client, body).status_code == 200
    assert len(chats()) == 1 and len(sent) == 1                    # retry stored it and welcomed once
    assert post(client, body).status_code == 200 and len(chats()) == 1 and len(sent) == 1


def test_retry_does_not_duplicate_a_message_the_failed_attempt_already_stored(client, sent, monkeypatch):
    real = W.store_incoming_message
    calls = {"n": 0}

    def store(**kw):
        calls["n"] += 1
        real(**kw)                                                 # row IS written ...
        return False if calls["n"] == 1 else True                  # ... but a later step reported failure

    monkeypatch.setattr(W, "store_incoming_message", store)
    body = payload(msg_id="wamid.LC2")
    assert post(client, body).status_code == 500
    assert post(client, body).status_code == 200
    assert len(chats()) == 1 and calls["n"] == 1 and len(sent) == 1


def test_stop_is_processed_even_when_live_chat_storage_fails(client, sent, monkeypatch):
    monkeypatch.setattr(W, "store_incoming_message", lambda **kw: False)
    body = payload(text="STOP", msg_id="wamid.LC3")
    assert post(client, body).status_code == 500                   # Live Chat needs a retry ...
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_out"   # ... but consent is saved now
    assert sent == [(SENDER, C.STOP_CONFIRMATION)]
    post(client, body)                                             # retry: still ONE confirmation, one ledger row
    assert sent == [(SENDER, C.STOP_CONFIRMATION)]
    assert _db().wa_consent_events.count_documents({}) == 1


def test_concurrent_duplicate_deliveries_single_reply(client, sent):
    import threading
    body = payload(msg_id="wamid.CONC1")
    codes = []

    def go():
        codes.append(post(client, body).status_code)

    ts = [threading.Thread(target=go) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    # A concurrent duplicate finds a live lease -> 500 (Meta retries); it never double-processes.
    assert set(codes) <= {200, 500} and len(sent) == 1 and len(chats()) == 1
    assert post(client, body).status_code == 200 and len(sent) == 1       # the retry sees the finished event


def test_concurrent_duplicate_stop_single_confirmation_single_ledger_row(client, sent):
    import threading
    body = payload(text="STOP", msg_id="wamid.CONC2")
    ts = [threading.Thread(target=lambda: post(client, body)) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sent == [(SENDER, C.STOP_CONFIRMATION)] and _db().wa_consent_events.count_documents({}) == 1


def test_concurrent_claims_of_a_failed_event_have_exactly_one_winner():
    import threading
    first = C.claim_event("msg:RACE")
    assert first == "claimed"
    C.set_event_state("msg:RACE", "failed", first.token)
    res = []
    ts = [threading.Thread(target=lambda: res.append(C.claim_event("msg:RACE"))) for _ in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert res.count("retry") == 1 and res.count("busy") == 29     # winner holds a live lease


# ───────────────────────────── consent database failures ─────────────────────────────

class _BrokenColl:
    def __init__(self, fail_on):
        self.fail_on = fail_on
        self.real = None

    def __getattr__(self, name):
        if name in self.fail_on:
            def boom(*a, **k):
                raise RuntimeError("db down for 919876543210 Asha EAAsecrettoken")
            return boom
        return getattr(self.real, name)


class _BrokenDb:
    def __init__(self, real, broken):
        self.real, self.broken = real, broken

    def __getitem__(self, name):
        if name in self.broken:
            b = _BrokenColl(self.broken[name])
            b.real = self.real[name]
            return b
        return self.real[name]

    def __getattr__(self, name):          # modules such as wa_chat use db.<collection>
        return self[name]


class _UnackColl:
    """Wraps a collection so that writes come back NOT acknowledged."""
    def __init__(self, real, ops):
        self.real, self.ops = real, ops

    def __getattr__(self, name):
        fn = getattr(self.real, name)
        if name in self.ops:
            def wrapped(*a, **k):
                res = fn(*a, **k)

                class R:
                    acknowledged = False
                    upserted_id = getattr(res, "upserted_id", None)
                return R()
            return wrapped
        return fn


class _UnackDb:
    def __init__(self, real, ops_by_coll):
        self.real, self.ops_by_coll = real, ops_by_coll

    def __getitem__(self, name):
        return _UnackColl(self.real[name], self.ops_by_coll[name]) if name in self.ops_by_coll else self.real[name]


@pytest.mark.parametrize("broken", [
    {C.CONTACTS: {"update_one"}},                                  # contact write fails
    {C.EVENTS: {"insert_one"}},                                    # ledger write fails
    {C.CONTACTS: {"update_one"}, C.EVENTS: {"insert_one"}},        # both fail
])
def test_failed_stop_write_sends_only_the_neutral_retry_notice(client, sent, monkeypatch, broken, caplog):
    monkeypatch.setattr(C, "db", _BrokenDb(_db(), broken))
    r = post(client, payload(text="STOP", msg_id="wamid.F1"))
    assert r.status_code == 200                                    # notice delivered -> nothing for Meta to retry
    assert sent == [(SENDER, "We couldn't process your request. Please reply STOP again.")]
    assert C.STOP_CONFIRMATION not in [m for _, m in sent] and "Welcome to OffrO" not in sent[0][1]
    assert len(chats()) == 1                                       # still visible in Live Chat
    assert "[WA-CONSENT-FAILURE]" in caplog.text and any(r.levelname == "ERROR" and "[WA-CONSENT-FAILURE]" in r.getMessage()
                                                         for r in caplog.records)
    assert SENDER not in caplog.text and "Asha" not in caplog.text and "EAAsecrettoken" not in caplog.text
    ev = _db().wa_webhook_events.find_one({"_id": "msg:wamid.F1"})
    assert ev["consent_failed"] is True and ev["notice_sent"] is True and ev["state"] == "done"


def test_failed_start_write_sends_neutral_start_notice_and_never_opts_in(client, sent, monkeypatch):
    monkeypatch.setattr(C, "db", _BrokenDb(_db(), {C.CONTACTS: {"update_one"}}))
    assert post(client, payload(text="START", msg_id="wamid.F2")).status_code == 200
    assert sent == [(SENDER, "We couldn't process your request. Please reply START again.")]
    assert _db().wa_contacts.find_one({"phone": SENDER}) is None


def test_user_can_resend_stop_after_a_failed_attempt(client, sent, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(C, "db", _BrokenDb(_db(), {C.CONTACTS: {"update_one"}}))
        post(client, payload(text="STOP", msg_id="wamid.R1", ts="1762843200"))
    sent.clear()
    post(client, payload(text="STOP", msg_id="wamid.R2", ts="1762843260"))
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_out"
    assert sent == [(SENDER, C.STOP_CONFIRMATION)]


def test_failure_notice_that_cannot_be_sent_is_recorded_and_meta_is_asked_to_retry(client, monkeypatch, caplog):
    sent_try = []
    monkeypatch.setattr(W, "send_whatsapp_message_checked",
                        lambda p, m: (sent_try.append(m) or {"ok": False, "outcome": "rejected_retryable", "error_code": 130429}))
    monkeypatch.setattr(C, "db", _BrokenDb(_db(), {C.CONTACTS: {"update_one"}}))
    r = post(client, payload(text="STOP", msg_id="wamid.F3"))
    assert r.status_code == 500                                    # nothing reached the user -> Meta retries
    assert sent_try == ["We couldn't process your request. Please reply STOP again."]
    ev = C.db["wa_webhook_events"].find_one({"_id": "msg:wamid.F3"})
    assert ev["consent_failed"] is True and ev["notice_sent"] is False and ev["state"] == "failed"
    assert "failure notice NOT sent" in caplog.text and SENDER not in caplog.text


def test_failure_and_marker_store_both_down_still_logs_monitored_error_and_never_raises(client, sent, monkeypatch, caplog):
    monkeypatch.setattr(W, "send_whatsapp_message_checked", lambda p, m: {"ok": False, "outcome": "rejected_retryable"})
    monkeypatch.setattr(C, "db", _BrokenDb(_db(), {C.CONTACTS: {"update_one"}, C.WEBHOOK_EVENTS: {"update_one"}}))
    r = post(client, payload(text="STOP", msg_id="wamid.F4"))
    assert r.status_code == 500 and "[WA-CONSENT-FAILURE]" in caplog.text
    assert "could not store consent-failure marker" in caplog.text


def test_retry_after_failed_notice_confirms_only_once_when_consent_now_succeeds(client, monkeypatch):
    out = []
    state = {"ok": False}

    def send(p, m):
        out.append(m)
        return {"ok": True, "outcome": "sent", "message_id": "x"} if state["ok"] else {"ok": False, "outcome": "rejected_retryable"}

    monkeypatch.setattr(W, "send_whatsapp_message_checked", send)
    body = payload(text="STOP", msg_id="wamid.F5")
    with monkeypatch.context() as m:
        m.setattr(C, "db", _BrokenDb(_db(), {C.CONTACTS: {"update_one"}}))
        assert post(client, body).status_code == 500
    state["ok"] = True
    assert post(client, body).status_code == 200                   # Meta's retry: DB is back, send works
    assert out[-1] == C.STOP_CONFIRMATION and out.count(C.STOP_CONFIRMATION) == 1
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_out"


@pytest.mark.parametrize("ops", [{C.CONTACTS: {"update_one"}}, {C.EVENTS: {"insert_one"}}])
def test_unacknowledged_stop_write_is_a_failure_never_a_confirmation(client, sent, monkeypatch, ops):
    monkeypatch.setattr(C, "db", _UnackDb(_db(), ops))
    post(client, payload(text="STOP", msg_id="wamid.U1"))
    assert [m for _, m in sent] == [C.STOP_FAILURE_NOTICE]
    with pytest.raises(C.ConsentWriteError):
        C.record_opt_out(SENDER, "STOP", "wamid.U2", "1762843200", whatsapp_id=True)


@pytest.mark.parametrize("ops", [{C.CONTACTS: {"update_one"}}, {C.EVENTS: {"insert_one"}}])
def test_unacknowledged_start_write_is_a_failure(client, sent, monkeypatch, ops):
    monkeypatch.setattr(C, "db", _UnackDb(_db(), ops))
    post(client, payload(text="START", msg_id="wamid.U3"))
    assert [m for _, m in sent] == [C.START_FAILURE_NOTICE]


def test_partial_opt_out_failure_leaves_person_opted_out_not_in(client, sent, monkeypatch):
    monkeypatch.setattr(C, "db", _BrokenDb(_db(), {C.EVENTS: {"insert_one"}}))
    post(client, payload(text="STOP", msg_id="wamid.F6"))
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_out"   # protected
    assert [m for _, m in sent] == [C.STOP_FAILURE_NOTICE]                          # but never a false confirmation


def test_partial_opt_in_failure_cannot_leave_person_opted_in(client, sent, monkeypatch):
    post(client, payload(text="STOP", msg_id="wamid.P1", ts="1762843200"))
    sent.clear()
    monkeypatch.setattr(C, "db", _BrokenDb(_db(), {C.CONTACTS: {"update_one"}}))
    post(client, payload(text="START", msg_id="wamid.P2", ts="1762843260"))
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_out"
    assert [m for _, m in sent] == [C.START_FAILURE_NOTICE]
    assert C.consent_status(SENDER) == "opted_out"


def test_invalid_sender_number_is_not_recorded_or_confirmed(client, sent):
    post(client, payload(text="STOP", sender="12", msg_id="wamid.BAD"))
    assert _db().wa_contacts.count_documents({}) == 0 and sent == []


# ───────────────────────────── consent ordering / concurrency / append-only ─────────────────────────────

def test_older_start_cannot_overwrite_newer_stop(client, sent):
    post(client, payload(text="STOP", msg_id="wamid.O2", ts="1762843300"))
    sent.clear()
    post(client, payload(text="START", msg_id="wamid.O1", ts="1762843200"))     # arrives late, is OLDER
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_out"
    assert sent == []                                                          # no false "you are subscribed"
    assert [e["event"] for e in _db().wa_consent_events.find({}).sort("at", 1)] == ["opt_out", "opt_in"]


def test_older_stop_cannot_overwrite_newer_start(client, sent):
    post(client, payload(text="START", msg_id="wamid.N2", ts="1762843300"))
    sent.clear()
    post(client, payload(text="STOP", msg_id="wamid.N1", ts="1762843200"))     # late and older
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_in"
    assert sent == []                                                          # no false "you are unsubscribed"


def test_same_second_stop_and_start_stop_wins(client, sent):
    post(client, payload(text="START", msg_id="wamid.T1", ts="1762843200"))
    post(client, payload(text="STOP", msg_id="wamid.T2", ts="1762843200"))
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_out"
    post(client, payload(text="START", msg_id="wamid.T3", ts="1762843200"))    # same second again: still out
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_out"


def test_any_arrival_order_ends_in_the_newest_state():
    import itertools
    events = [("stop", "1762843200"), ("start", "1762843260"), ("stop", "1762843320"), ("start", "1762843380")]
    for perm in itertools.permutations(range(len(events))):
        _DB.drop_collection(C.CONTACTS)
        _DB.drop_collection(C.EVENTS)
        for i in perm:
            kind, ts = events[i]
            (C.record_opt_out if kind == "stop" else C.record_opt_in)(SENDER, kind, "wamid.E%d" % i, ts, whatsapp_id=True)
        assert _DB[C.CONTACTS].find_one({"phone": SENDER})["status"] == "opted_in", perm   # newest is START
        assert _DB[C.CONTACTS].count_documents({}) == 1 and _DB[C.EVENTS].count_documents({}) == 4


def test_concurrent_stop_start_threads_converge_on_newest():
    import threading
    evs = [(C.record_opt_out, "STOP", "1762843200"), (C.record_opt_in, "START", "1762843260"),
           (C.record_opt_out, "STOP", "1762843320"), (C.record_opt_in, "START", "1762843380"),
           (C.record_opt_out, "STOP", "1762843440")]
    ts = [threading.Thread(target=lambda e=e, i=i: e[0](SENDER, e[1], "wamid.C%d" % i, e[2], whatsapp_id=True))
          for i, e in enumerate(evs * 4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert _DB[C.CONTACTS].count_documents({}) == 1
    assert _DB[C.CONTACTS].find_one({"phone": SENDER})["status"] == "opted_out"    # newest event is the STOP at ...440


def test_ledger_is_append_only_and_idempotent_per_message():
    C.record_opt_out(SENDER, "STOP", "wamid.L1", "1762843200", whatsapp_id=True)
    before = list(_DB[C.EVENTS].find({}))
    C.record_opt_in(SENDER, "START", "wamid.L2", "1762843260", whatsapp_id=True)
    C.record_opt_out(SENDER, "STOP", "wamid.L1", "1762843200", whatsapp_id=True)   # retry of the same message
    C.record_opt_in(SENDER, "START", "wamid.L0", "1762843100", whatsapp_id=True)   # older event, superseded
    after = list(_DB[C.EVENTS].find({}))
    assert len(before) == 1 and len(after) == 3                                    # grew; retry added nothing
    assert before[0] in after                                                      # earlier row byte-identical
    assert sorted(e["message_id"] for e in after) == ["wamid.L0", "wamid.L1", "wamid.L2"]
    src = open(C.__file__, encoding="utf-8").read()
    assert "update_one" not in src.split("def _append_event")[1].split("def record_opt_out")[0]
    assert not any(tok in src for tok in ("db[EVENTS].update", "db[EVENTS].delete", "db[EVENTS].replace"))


# ───────────────────────────── canonical phone numbers ─────────────────────────────

@pytest.mark.parametrize("raw", ["+919876543210", "919876543210", "9876543210", "+91 98765 43210", "+91-98765-43210",
                                 "(+91) 98765 43210", "91 9876543210", "09876543210", "0091 9876543210",
                                 "  9876543210  ", "98765.43210", 919876543210, "+91 (98765) 43210"])
def test_every_indian_format_resolves_to_the_same_key(raw):
    assert C.normalize_phone(raw) == "919876543210"


@pytest.mark.parametrize("first", ["6", "7", "8", "9"])
def test_ten_digit_mobile_prefixes_6_to_9_are_indian(first):
    assert C.normalize_phone(first + "123456789") == "91" + first + "123456789"


@pytest.mark.parametrize("raw", ["", None, "abc", "98765", "+91 98765", "5876543210x", "+91 5876543210", "915876543210",
                                 "91987654321", "9198765432101", "+9198765432", "0 0", "++919876543210", "+0919876543210",
                                 "9876543210 9876543210", "९८७६५४३२१०", "919876543210a", "+91 98765 43210 ext 4"])
def test_invalid_formats_are_rejected_not_stored_under_another_key(raw):
    assert C.normalize_phone(raw) == ""


def test_non_indian_international_numbers_are_preserved():
    assert C.normalize_phone("+1 415 555 2671") == "14155552671"
    assert C.normalize_phone("14155552671") == "14155552671"
    assert C.normalize_phone("+44 7911 123456") == "447911123456"
    assert C.normalize_phone("+65 8123 4567") == "6581234567"             # '+' means already international
    assert C.normalize_phone("6581234567", whatsapp_id=True) == "6581234567"   # a WhatsApp id is never re-prefixed
    assert C.normalize_phone("9876543210", whatsapp_id=True) == "9876543210"
    assert C.normalize_phone("919876543210", whatsapp_id=True) == "919876543210"
    assert C.normalize_phone("91987654321", whatsapp_id=True) == ""        # a 91 id must be a valid Indian mobile


def test_opt_out_with_plus91_cannot_be_bypassed_by_other_formats():
    C.record_opt_out("+919876543210", "STOP", "wamid.B1", "1762843200")
    for variant in ["9876543210", "919876543210", "+91 98765 43210", "09876543210", "0091 98765 43210", "98765-43210"]:
        assert C.is_opted_out(variant), variant
        assert C.consent_status(variant) == "opted_out", variant
        assert C.may_send_marketing(variant) is False, variant
    assert _DB[C.CONTACTS].count_documents({}) == 1
    assert _DB[C.CONTACTS].find_one({})["_id"] == "919876543210"


def test_opt_out_via_ten_digit_blocks_the_plus91_form_too_and_never_creates_a_second_contact():
    C.record_opt_out("9876543210", "STOP", "wamid.B2", "1762843200")
    C.record_opt_in("+91 98765 43210", "START", "wamid.B3", "1762843100")        # older -> superseded, same key
    assert _DB[C.CONTACTS].count_documents({}) == 1
    for variant in ["+919876543210", "919876543210", "9876543210"]:
        assert C.consent_status(variant) == "opted_out"


def test_webhook_opt_out_blocks_ten_digit_audience_number(client, sent):
    post(client, payload(text="STOP", sender="919876543210", msg_id="wamid.B4"))
    assert C.is_opted_out("9876543210") and not C.may_send_marketing("9876543210")


def test_ambiguous_foreign_ten_digit_opt_out_is_still_found():
    C.record_opt_out("6581234567", "STOP", "wamid.B5", "1762843200", whatsapp_id=True)   # Singapore wa_id, stored as is
    assert C.is_opted_out("6581234567")                                                    # lookup also tries the raw digits
    assert C.is_opted_out("+65 8123 4567")


def test_consent_status_unknown_invalid_and_fail_closed():
    assert C.consent_status("+919876543210") == "unknown" and C.may_send_marketing("+919876543210") is False
    assert C.consent_status("123") == "invalid" and C.may_send_marketing("123") is False
    C.record_opt_in("9876543210", "START", "wamid.K1", "1762843200")
    assert C.consent_status("+91 98765 43210") == "opted_in" and C.may_send_marketing("919876543210") is True
    C.db = _BrokenDb(_DB, {C.CONTACTS: {"find"}})                                       # restored by the autouse fixture
    with pytest.raises(RuntimeError):
        C.may_send_marketing("919876543210")                                            # raises -> Phase 2 must not send


def test_existing_phone_records_in_other_collections_are_not_consent():
    _db().users.insert_one({"phone": "9876543210"})
    _db().merchants.insert_one({"phone": "+919876543210"})
    assert C.consent_status("9876543210") == "unknown" and not C.may_send_marketing("9876543210")


def test_phase2_requirements_are_recorded():
    text = open(C.__file__, encoding="utf-8").read() + open("docs/WHATSAPP_WEBHOOK_STAGING_SETUP.md", encoding="utf-8").read()
    for needle in ("IMMEDIATELY BEFORE sending", "unknown-consent", "FAILS CLOSED", "/admin/whatsapp/template-test"):
        assert needle in text or needle.lower() in text.lower()
    req = open("docs/WHATSAPP_PHASE2_REQUIREMENTS.md", encoding="utf-8").read()
    for needle in ("wa_contacts", "immediately before", "unknown", "fail closed", "template-test"):
        assert needle in req.lower()


# ───────────────────────────── request size limit / exact raw bytes ─────────────────────────────

def _body_of_size(n):
    base = json.dumps({"object": "other", "pad": ""}).encode()
    return json.dumps({"object": "other", "pad": "x" * (n - len(base))}).encode()


def test_body_at_the_limit_is_accepted_and_one_byte_over_is_rejected(client, sent):
    ok = _body_of_size(S.MAX_BODY_BYTES)
    assert len(ok) == S.MAX_BODY_BYTES and client.post("/webhook", content=ok).status_code == 200
    big = _body_of_size(S.MAX_BODY_BYTES + 1)
    assert client.post("/webhook", content=big).status_code == 413
    assert chats() == [] and _db().wa_webhook_events.count_documents({}) == 0


def test_oversized_body_without_content_length_is_rejected_while_streaming(client):
    def gen():
        for _ in range(S.MAX_BODY_BYTES // 65536 + 2):
            yield b"x" * 65536                                   # chunked: no Content-Length
    assert client.post("/webhook", content=gen()).status_code == 413


def test_oversized_is_rejected_before_signature_and_in_every_mode(client, monkeypatch):
    big = _body_of_size(S.MAX_BODY_BYTES + 10)
    for mode in ("off", "log", "enforce"):
        monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", mode)
        monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
        assert client.post("/webhook", content=big, headers={"X-Hub-Signature-256": sign(big)}).status_code == 413


def test_lying_small_content_length_cannot_bypass_the_limit(client):
    big = _body_of_size(S.MAX_BODY_BYTES + 5)
    r = client.post("/webhook", content=big, headers={"content-length": "10"})
    assert r.status_code in (400, 413)          # the HTTP layer or our streaming check stops it; never processed


def test_signature_is_over_the_exact_raw_bytes_not_a_reserialisation(client, sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "enforce")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    body = payload(msg_id="wamid.RAW1")
    odd = json.dumps(body, indent=3, sort_keys=True, ensure_ascii=False).encode("utf-8")     # whitespace + key order
    compact = json.dumps(body, separators=(",", ":")).encode()
    assert odd != compact
    assert client.post("/webhook", content=odd, headers={"X-Hub-Signature-256": sign(odd)}).status_code == 200
    assert len(chats()) == 1
    assert client.post("/webhook", content=odd, headers={"X-Hub-Signature-256": sign(compact)}).status_code == 403


def test_signature_is_hmac_sha256_hex_of_body():
    raw = b'{"x":"y"}'
    expected = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert S.verify_signature(raw, "sha256=" + expected, SECRET)
    assert not S.verify_signature(raw, "sha256=" + hashlib.sha256(raw).hexdigest(), SECRET)      # plain hash is not HMAC
    assert not S.verify_signature(raw, "sha1=" + hmac.new(SECRET.encode(), raw, hashlib.sha1).hexdigest(), SECRET)


def test_default_mode_unchanged_by_size_limit_and_all_other_changes(client, sent):
    assert S.signature_mode() == "off"
    assert post(client, payload(), signature="sha256=" + "f" * 64).status_code == 200


# ───────────────────────────── logging: no payloads, phones or secrets ─────────────────────────────

def test_no_raw_payload_or_full_phone_in_logs_or_stdout(client, sent, monkeypatch, caplog, capsys):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "log")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    caplog.set_level(logging.DEBUG)
    post(client, payload(text="my secret order 4455 please", name="Asha Kumar"))
    post(client, payload(text="STOP", msg_id="wamid.L2"))
    post(client, status_payload())
    out = capsys.readouterr().out
    for blob in (caplog.text, out):
        assert SENDER not in blob
        assert "my secret order" not in blob and "Asha Kumar" not in blob
        assert "WHATSAPP PAYLOAD RECEIVED" not in blob and "Replying to" not in blob
        assert SECRET not in blob
    assert "***3210" in caplog.text


def test_meta_error_details_are_redacted(client, caplog):
    monkey = payload()
    monkey["entry"][0]["changes"][0]["value"] = {"errors": [{"code": 131047, "title": "Re-engagement for +919876543210",
                                                              "error_data": {"details": "Bearer abc.DEF-123 to 919876543210"}}]}
    post(client, monkey)
    assert "919876543210" not in caplog.text and "abc.DEF-123" not in caplog.text and "131047" in caplog.text


def test_redact_and_mask_helpers(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "EAAxxxxSECRETTOKEN123")
    out = S.redact("failed for 919876543210 token EAAxxxxSECRETTOKEN123 Bearer zzz.yyy")
    assert "919876543210" not in out and "SECRETTOKEN" not in out and "zzz.yyy" not in out
    assert S.mask_phone("+91 98765-43210") == "***3210" and S.mask_phone("") == "***" and S.mask_phone("12") == "***"
    assert S.payload_summary(payload()) == "messages=1 statuses=0 errors=0"
    assert S.payload_summary(None) == "unparseable"


def test_send_whatsapp_message_logs_masked_phone_and_keeps_return_values(monkeypatch, caplog):
    monkeypatch.setattr(WA, "WHATSAPP_ACCESS_TOKEN", "EAAtesttokentesttoken")
    monkeypatch.setattr(WA, "WHATSAPP_PHONE_NUMBER_ID", "PNID")
    caplog.set_level(logging.INFO)

    class R:
        def __init__(self, code, data):
            self.status_code, self._d = code, data

        def json(self):
            return self._d

    monkeypatch.setattr(WA.requests, "post", lambda *a, **k: R(200, {"messages": [{"id": "wamid.X"}]}))
    assert WA.send_whatsapp_message("+91 98765 43210", "hi") == {"ok": True, "message_id": "wamid.X"}
    monkeypatch.setattr(WA.requests, "post",
                        lambda *a, **k: R(400, {"error": {"message": "bad number 919876543210 EAAtesttokentesttoken"}}))
    res = WA.send_whatsapp_message("919876543210", "hi")
    assert res["ok"] is False and "919876543210" in res["error"]       # returned value unchanged (callers/audit)
    assert "919876543210" not in caplog.text and "9876543210" not in caplog.text
    assert "EAAtesttokentesttoken" not in caplog.text and "***3210" in caplog.text


def test_live_chat_store_log_is_masked(caplog):
    caplog.set_level(logging.INFO)
    WC.store_incoming_message(SENDER, "Asha Kumar", SENDER, "text", "hello", "wamid.Z", "1762843200", raw_msg={})
    assert "Stored text from ***3210" in caplog.text
    assert SENDER not in caplog.text and "Asha Kumar" not in caplog.text


# ───────────────────────────── indexes / collections ─────────────────────────────

def test_ensure_indexes_creates_expected_indexes_and_is_idempotent():
    C.ensure_indexes()
    C.ensure_indexes()
    d = _db()
    assert d.wa_contacts.index_information()["wa_contacts_phone"]["unique"] is True
    assert "wa_contacts_status" in d.wa_contacts.index_information()
    assert "wa_consent_events_phone_at" in d.wa_consent_events.index_information()
    info = d.wa_webhook_events.index_information()
    assert info["wa_webhook_events_key"]["unique"] is True
    assert info["wa_webhook_events_ttl"]["expireAfterSeconds"] == 30 * 24 * 3600
    first = C.claim_event("k1")
    assert first == "claimed" and C.claim_event("k1") == "busy"
    assert C.set_event_state("k1", "done", first.token) and C.claim_event("k1") == "duplicate"


def test_no_campaign_collections_or_bulk_endpoints_in_phase_1():
    import server
    paths = {getattr(r, "path", "") for r in server.app.routes}
    # Phase 2A (permission-gated admin foundations) legitimately adds /admin/whatsapp-campaigns/*; what must still be
    # true is that no sending / bulk endpoint exists. test_wa_campaigns_foundation.py pins the exact route set.
    camp = {p for p in paths if "campaign" in p.lower() and "whatsapp" in p.lower()}
    assert all(p.startswith("/admin/whatsapp-campaigns/") for p in camp)
    assert not any(w in p.lower() for p in camp for w in ("bulk", "launch", "run", "start", "execute", "send-"))
    src = open(C.__file__, encoding="utf-8").read()
    assert "wa_campaign" not in src


# ───────────────────────────── logging: exception text, signed media URLs, tokens ─────────────────────────────

LEAK_PHONE, LEAK_NAME, LEAK_BODY = "919876543210", "Asha Kumar", "my secret order 4455"
LEAK_TOKEN = "EAAleakytoken12345abc"
LEAK_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1&ext=2&hash=SIGNEDHASHVALUE"
LEAKY = "boom for %s %s %s token=%s secret=%s url=%s" % (LEAK_PHONE, LEAK_NAME, LEAK_BODY, LEAK_TOKEN, SECRET, LEAK_URL)


def _assert_clean(caplog, capsys=None):
    blobs = [caplog.text] + ([capsys.readouterr().out] if capsys else [])
    for blob in blobs:
        for needle in (LEAK_PHONE, "9876543210", LEAK_NAME, LEAK_BODY, LEAK_TOKEN, SECRET, "SIGNEDHASHVALUE",
                       "lookaside.fbsbx.com", "Traceback"):
            assert needle not in blob, needle
    # nothing may attach a traceback (its last line is the raw exception message)
    assert not [r for r in caplog.records if r.exc_info], "a log record carries exc_info"


@pytest.fixture
def leak_env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", LEAK_TOKEN)
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    monkeypatch.setattr(WA, "WHATSAPP_ACCESS_TOKEN", LEAK_TOKEN)
    monkeypatch.setattr(WA, "WHATSAPP_PHONE_NUMBER_ID", "PNID")


def test_exc_summary_has_type_and_location_but_never_the_message():
    try:
        raise ValueError(LEAKY)
    except ValueError as e:
        out = S.exc_summary(e)
    assert out.startswith("ValueError at test_wa_webhook_consent.py:") and "boom" not in out and LEAK_PHONE not in out


def test_redact_removes_signed_urls_tokens_secrets_and_short_numbers(leak_env):
    out = S.redact(LEAKY + " and 98765432", limit=2000)
    for needle in (LEAK_PHONE, LEAK_TOKEN, SECRET, "SIGNEDHASHVALUE", "lookaside", "98765432"):
        assert needle not in out
    assert "<url>" in out


def test_live_chat_store_failure_logs_type_only(caplog, leak_env):
    caplog.set_level(logging.DEBUG)
    real = WC.db
    WC.db = _BrokenDb(real, {"customers_whatsapp": {"find_one", "insert_one", "update_one"}})
    try:
        assert WC.store_incoming_message(LEAK_PHONE, LEAK_NAME, LEAK_PHONE, "text", LEAK_BODY, "wamid.X1", "1762843200",
                                         raw_msg={}) is False
    finally:
        WC.db = real
    assert "Failed to store incoming message (RuntimeError at" in caplog.text
    _assert_clean(caplog)


def test_media_pipeline_logs_never_contain_signed_urls(monkeypatch, caplog, leak_env):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(WC, "fetch_media_url", lambda mid: {"ok": False, "error": "failed " + LEAKY})
    WC._fetch_and_store_media("507f1f77bcf86cd799439011", "MEDIA1")
    monkeypatch.setattr(WC, "fetch_media_url", lambda mid: {"ok": True, "url": LEAK_URL, "mime_type": "image/jpeg"})
    monkeypatch.setattr(WC, "download_media", lambda u: {"ok": False, "error": "ConnectionError for " + LEAK_URL + " " + LEAKY})
    WC._fetch_and_store_media("507f1f77bcf86cd799439011", "MEDIA1")
    monkeypatch.setattr(WC, "download_media", lambda u: (_ for _ in ()).throw(RuntimeError(LEAKY)))
    WC._fetch_and_store_media("507f1f77bcf86cd799439011", "MEDIA1")
    assert "_fetch_and_store_media error for id=MEDIA1 (RuntimeError at" in caplog.text
    _assert_clean(caplog)


@pytest.mark.parametrize("fn,args", [
    ("send_whatsapp_message", (LEAK_PHONE, LEAK_BODY)),
    ("fetch_media_url", ("MEDIA1",)),
    ("download_media", (LEAK_URL,)),
    ("upload_media_to_whatsapp", (b"abc", "image/jpeg")),
    ("send_whatsapp_image", (LEAK_PHONE, "MEDIA1", LEAK_BODY)),
])
def test_whatsapp_service_exceptions_log_type_only_but_return_values_unchanged(monkeypatch, caplog, leak_env, fn, args):
    caplog.set_level(logging.DEBUG)

    def boom(*a, **k):
        raise RuntimeError(LEAKY)

    monkeypatch.setattr(WA.requests, "post", boom)
    monkeypatch.setattr(WA.requests, "get", boom)
    res = getattr(WA, fn)(*args)
    assert res["ok"] is False and LEAK_PHONE in res["error"]        # return value unchanged (callers/admin API)
    assert "RuntimeError at" in caplog.text
    _assert_clean(caplog)


def test_webhook_handler_and_processing_exceptions_log_type_only(client, sent, monkeypatch, caplog, leak_env, capsys):
    caplog.set_level(logging.DEBUG)

    def leaky_handler(**kw):
        raise RuntimeError(LEAKY)

    W._MESSAGE_HANDLERS.append(leaky_handler)
    W._STATUS_HANDLERS.append(leaky_handler)
    post(client, payload(text=LEAK_BODY, name=LEAK_NAME, sender=LEAK_PHONE, msg_id="wamid.H1"))
    post(client, status_payload())
    monkeypatch.setattr(W, "_process_webhook", lambda b: (_ for _ in ()).throw(RuntimeError(LEAKY)))
    assert post(client, payload(msg_id="wamid.H2")).status_code == 200      # structural bug: not retryable
    assert "Handler leaky_handler error (RuntimeError at" in caplog.text
    _assert_clean(caplog, capsys)


def test_message_processing_exception_does_not_leak(client, monkeypatch, caplog, leak_env):
    monkeypatch.setattr(W, "store_incoming_message", lambda **kw: (_ for _ in ()).throw(RuntimeError(LEAKY)))
    assert post(client, payload(text=LEAK_BODY, name=LEAK_NAME, sender=LEAK_PHONE)).status_code == 500
    _assert_clean(caplog)


def test_reply_failure_and_meta_error_text_are_redacted(client, monkeypatch, caplog, leak_env):
    monkeypatch.setattr(W, "send_whatsapp_message_checked", lambda p, m: {"ok": False, "outcome": "uncertain", "error_code": None})
    post(client, payload(text="Hello", sender=LEAK_PHONE, name=LEAK_NAME))
    body = payload()
    body["entry"][0]["changes"][0]["value"] = {"errors": [{"code": 1, "title": LEAKY, "error_data": {"details": LEAKY}}]}
    post(client, body)
    _assert_clean(caplog)


def test_full_message_lifecycle_logs_are_clean(client, sent, monkeypatch, caplog, capsys, leak_env):
    monkeypatch.setenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "log")
    caplog.set_level(logging.DEBUG)
    for i, t in enumerate([LEAK_BODY, "STOP", "START"]):
        post(client, payload(text=t, name=LEAK_NAME, sender=LEAK_PHONE, msg_id="wamid.LC%d" % i, ts=str(1762843200 + i * 60)))
    post(client, status_payload())
    _assert_clean(caplog, capsys)


# ═════════════════════════ Review corrections (status retries, lease, startup log, reply outcomes, access log,
# ═════════════════════════ duplicate-key handling) ═════════════════════════════════════════════════════════
import threading
from datetime import datetime, timedelta

from pymongo.errors import DuplicateKeyError

OUT_SENT = {"ok": True, "outcome": "sent", "message_id": "wamid.X"}
OUT_RETRYABLE = {"ok": False, "outcome": "rejected_retryable", "error_code": 130429}
OUT_PERMANENT = {"ok": False, "outcome": "rejected_permanent", "error_code": 131009}
OUT_UNCERTAIN = {"ok": False, "outcome": "uncertain", "error_code": None}


@pytest.fixture
def scripted(monkeypatch):
    """Checked sender whose results are scripted in order (then 'sent'); records every attempted send."""
    calls, outcomes = [], []

    def fake(phone, message):
        calls.append(message)
        return dict(outcomes.pop(0)) if outcomes else dict(OUT_SENT)

    monkeypatch.setattr(W, "send_whatsapp_message_checked", fake)
    return calls, outcomes


def ev(key):
    return _db().wa_webhook_events.find_one({"_id": key})


def expire_lease(key):
    _db().wa_webhook_events.update_one({"_id": key}, {"$set": {"lease_until": datetime.utcnow() - timedelta(seconds=1)}})


# ── 1. status callback retry safety ─────────────────────────────────────────────

def test_failing_status_handler_returns_500_and_marks_event_failed(client):
    W._STATUS_HANDLERS.append(lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert post(client, status_payload()).status_code == 500
    assert ev("status:wamid.OUT1:delivered:1762843300")["state"] == "failed"


def test_status_retry_runs_only_failed_handlers_and_finishes_done(client):
    ran = {"good": 0, "flaky": 0}
    fail = {"on": True}

    def good(**kw):
        ran["good"] += 1

    def flaky(**kw):
        ran["flaky"] += 1
        if fail["on"]:
            raise RuntimeError("temporary")

    W._STATUS_HANDLERS.extend([good, flaky])
    assert post(client, status_payload()).status_code == 500
    assert ran == {"good": 1, "flaky": 1}
    fail["on"] = False
    assert post(client, status_payload()).status_code == 200            # Meta's retry
    assert ran == {"good": 1, "flaky": 2}                                # the successful handler is NOT repeated
    assert ev("status:wamid.OUT1:delivered:1762843300")["state"] == "done"
    assert post(client, status_payload()).status_code == 200            # finished events are never repeated
    assert ran == {"good": 1, "flaky": 2}


def test_status_handler_progress_survives_identically_named_lambdas(client):
    ran = []
    W._STATUS_HANDLERS.append(lambda **kw: ran.append("a"))
    W._STATUS_HANDLERS.append(lambda **kw: (ran.append("b"), (_ for _ in ()).throw(RuntimeError("x"))))
    assert post(client, status_payload()).status_code == 500
    assert post(client, status_payload()).status_code == 500
    assert ran.count("a") == 1 and ran.count("b") == 2


def test_status_retries_are_bounded(client):
    calls = []
    W._STATUS_HANDLERS.append(lambda **kw: (calls.append(1), (_ for _ in ()).throw(RuntimeError("x"))))
    codes = [post(client, status_payload()).status_code for _ in range(5)]
    assert codes == [500, 500, 500, 200, 200] and len(calls) == C.MAX_ATTEMPTS


def test_status_handler_failure_with_claim_store_down_asks_for_retry_and_reruns_all(client, monkeypatch):
    monkeypatch.setattr(C, "claim_event", lambda key: C.Claim("unavailable"))
    calls = []
    W._STATUS_HANDLERS.append(lambda **kw: (calls.append(1), (_ for _ in ()).throw(RuntimeError("x"))))
    assert post(client, status_payload()).status_code == 500
    assert post(client, status_payload()).status_code == 500
    assert len(calls) == 2


def test_status_without_message_id_never_asks_for_a_retry(client):
    W._STATUS_HANDLERS.append(lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    assert post(client, status_payload(msg_id="")).status_code == 200


def test_successful_status_handlers_mark_done_and_duplicates_skipped(client):
    seen = []
    W._STATUS_HANDLERS.append(lambda **kw: seen.append(kw["status"]))
    assert post(client, status_payload()).status_code == 200
    assert post(client, status_payload()).status_code == 200
    assert seen == ["delivered"] and ev("status:wamid.OUT1:delivered:1762843300")["state"] == "done"


def test_busy_status_event_asks_meta_to_retry(client):
    seen = []
    W._STATUS_HANDLERS.append(lambda **kw: seen.append(1))
    C.claim_event("status:wamid.OUT1:delivered:1762843300")              # another worker, live lease
    assert post(client, status_payload()).status_code == 500 and seen == []


# ── 2. lease, stuck 'processing' events, claim token ────────────────────────────

def test_expired_lease_is_reclaimed_and_counts_an_attempt():
    first = C.claim_event("msg:L1")
    expire_lease("msg:L1")
    second = C.claim_event("msg:L1")
    assert second == "retry" and second.token and second.token != first.token
    assert ev("msg:L1")["attempts"] == 2 and ev("msg:L1")["state"] == "processing"


def test_live_lease_is_busy_not_reclaimed():
    C.claim_event("msg:L2")
    assert C.claim_event("msg:L2") == "busy" and ev("msg:L2")["attempts"] == 1


def test_lease_length_is_120_seconds():
    assert C.LEASE_SECONDS == 120
    C.claim_event("msg:L3")
    d = ev("msg:L3")
    assert 115 <= (d["lease_until"] - d["received_at"]).total_seconds() <= 125


def test_legacy_processing_claim_without_lease_is_recoverable():
    _db().wa_webhook_events.insert_one({"_id": "msg:OLD", "event_key": "msg:OLD", "state": "processing", "attempts": 1,
                                        "received_at": datetime.utcnow(), "updated_at": datetime.utcnow()})
    assert C.claim_event("msg:OLD") == "retry"


def test_concurrent_reclaim_of_an_expired_lease_has_exactly_one_winner():
    C.claim_event("msg:L4")
    expire_lease("msg:L4")
    res = []
    ts = [threading.Thread(target=lambda: res.append(str(C.claim_event("msg:L4")))) for _ in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert res.count("retry") == 1 and res.count("busy") == 29 and ev("msg:L4")["attempts"] == 2


def test_exhausted_attempts_on_a_stalled_event_are_abandoned_with_monitored_log(caplog):
    C.claim_event("msg:L5")
    for _ in range(C.MAX_ATTEMPTS - 1):
        expire_lease("msg:L5")
        assert C.claim_event("msg:L5") == "retry"
    expire_lease("msg:L5")
    assert C.claim_event("msg:L5") == "abandoned"
    assert ev("msg:L5")["state"] == "abandoned"
    assert any(r.levelname == "ERROR" and "[WA-EVENT-ABANDONED]" in r.getMessage() for r in caplog.records)
    assert C.claim_event("msg:L5") == "duplicate"


def test_webhook_busy_message_returns_500_and_does_nothing(client, sent):
    C.claim_event("msg:wamid.BUSY")                                      # live lease held by another worker
    assert post(client, payload(msg_id="wamid.BUSY")).status_code == 500
    assert chats() == [] and sent == []


def test_webhook_recovers_a_crashed_worker_after_lease_expiry(client, sent):
    C.claim_event("msg:wamid.CRASH")                                     # worker died after claiming
    assert post(client, payload(msg_id="wamid.CRASH")).status_code == 500
    expire_lease("msg:wamid.CRASH")
    assert post(client, payload(msg_id="wamid.CRASH")).status_code == 200
    assert len(chats()) == 1 and len(sent) == 1 and ev("msg:wamid.CRASH")["state"] == "done"


def test_webhook_abandoned_message_is_acknowledged_not_retried(client, sent):
    C.claim_event("msg:wamid.AB")
    for _ in range(C.MAX_ATTEMPTS - 1):
        expire_lease("msg:wamid.AB")
        C.claim_event("msg:wamid.AB")
    expire_lease("msg:wamid.AB")
    assert post(client, payload(msg_id="wamid.AB")).status_code == 200 and sent == [] and chats() == []


def test_stale_claim_token_cannot_change_any_event_or_reply_state():
    old = C.claim_event("msg:Z1")
    expire_lease("msg:Z1")
    new = C.claim_event("msg:Z1")
    assert new == "retry"
    assert C.set_event_state("msg:Z1", "done", old.token) is False
    assert C.set_event_state("msg:Z1", "failed", "") is False
    assert C.claim_reply_slot("msg:Z1", old.token) is False
    assert C.mark_handler_done("msg:Z1", old.token, "0:h") is False
    assert C.note_consent_failure("msg:Z1", "stop", True, old.token) is False
    d = ev("msg:Z1")
    assert d["state"] == "processing" and d["claim_token"] == new.token and "reply_state" not in d
    assert "handlers_done" not in d and "consent_failed" not in d
    assert C.claim_reply_slot("msg:Z1", new.token) is True               # the owner still can
    assert C.finish_reply_slot("msg:Z1", old.token, "sent") is False
    assert C.release_reply_slot("msg:Z1", old.token) is False
    assert C.set_event_state("msg:Z1", "done", new.token) is True


def test_zombie_worker_cannot_send_a_reply(monkeypatch, scripted):
    calls, _ = scripted
    old = C.claim_event("msg:Z2")
    expire_lease("msg:Z2")
    new = C.claim_event("msg:Z2")
    assert W._auto_reply("msg:Z2", old.token, SENDER, "hi") == "blocked" and calls == []
    assert W._auto_reply("msg:Z2", new.token, SENDER, "hi") == "sent" and calls == ["hi"]


def test_reply_slot_has_exactly_one_winner_under_concurrency(scripted):
    calls, _ = scripted
    c = C.claim_event("msg:RS")
    res = []
    ts = [threading.Thread(target=lambda: res.append(W._auto_reply("msg:RS", c.token, SENDER, "hi"))) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1 and res.count("sent") == 1 and ev("msg:RS")["reply_state"] == "sent"


def test_crash_after_slot_taken_never_resends(client, sent):
    c = C.claim_event("msg:wamid.RS2")
    assert C.claim_reply_slot("msg:wamid.RS2", c.token)                   # worker died while 'sending'
    expire_lease("msg:wamid.RS2")
    assert post(client, payload(msg_id="wamid.RS2")).status_code == 200
    assert sent == [] and ev("msg:wamid.RS2")["reply_state"] == "sending" and len(chats()) == 1


def test_reply_slot_state_is_per_message(client, sent):
    post(client, payload(msg_id="wamid.M1"))
    post(client, payload(msg_id="wamid.M2"))
    assert len(sent) == 2


# ── 3. safe startup logging ─────────────────────────────────────────────────────

def test_ensure_indexes_safe_logs_type_and_location_only(monkeypatch, caplog, capsys):
    secret = "mongodb://user:PASSWORD123@host/db 919876543210"

    def boom():
        raise RuntimeError(secret)

    monkeypatch.setattr(C, "ensure_indexes", boom)
    assert C.ensure_indexes_safe() is False
    out = capsys.readouterr()
    assert "PASSWORD123" not in caplog.text + out.out + out.err and "9876543210" not in caplog.text
    assert "index creation skipped (RuntimeError at" in caplog.text


def test_ensure_indexes_safe_true_when_ok():
    assert C.ensure_indexes_safe() is True


def test_server_startup_block_no_longer_prints_the_raw_exception():
    src = open("server.py", encoding="utf-8").read()
    block = src[src.index("_wa_consent.ensure_indexes_safe"):]
    block = block[:block.index("# Ensure OTP TTL")]
    assert "{e}" not in block and "type(e).__name__" in block


# ── 4. outcome-classified sender + reply outcomes ──────────────────────────────

@pytest.mark.parametrize("status,data,expected", [
    (200, {"messages": [{"id": "wamid.1"}]}, "sent"),
    (200, {"messages": []}, "uncertain"),
    (200, {}, "uncertain"),
    (200, None, "uncertain"),
    (429, None, "rejected_retryable"),
    (429, {"error": {"code": 130429}}, "rejected_retryable"),
    (400, {"error": {"code": 130429}}, "rejected_retryable"),
    (400, {"error": {"code": 131056}}, "rejected_retryable"),
    (400, {"error": {"code": 4}}, "rejected_retryable"),
    (400, {"error": {"code": 80007}}, "rejected_retryable"),
    (400, {"error": {"code": 131009}}, "rejected_permanent"),
    (401, {"error": {"code": 190}}, "rejected_permanent"),
    (400, {"error": {}}, "rejected_permanent"),
    (400, None, "uncertain"),
    (500, {"error": {"code": 131016}}, "uncertain"),
    (503, None, "uncertain"),
    (502, {"error": {"code": 130429}}, "uncertain"),          # 5xx is never 'definitively rejected'
    (302, None, "uncertain"),
])
def test_classify_send_response(status, data, expected):
    assert WA.classify_send_response(status, data)[0] == expected


class _Resp:
    def __init__(self, status, data=None, bad_json=False):
        self.status_code, self._d, self._bad = status, data, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._d


@pytest.fixture
def wa_env(monkeypatch):
    monkeypatch.setattr(WA, "WHATSAPP_ACCESS_TOKEN", "EAAsecrettoken")
    monkeypatch.setattr(WA, "WHATSAPP_PHONE_NUMBER_ID", "PNID")


def test_checked_sender_maps_http_results(monkeypatch, wa_env):
    mk = lambda r: monkeypatch.setattr(WA.requests, "post", lambda *a, **k: r)
    mk(_Resp(200, {"messages": [{"id": "wamid.9"}]}))
    assert WA.send_whatsapp_message_checked("+91 98765 43210", "hi") == {"ok": True, "outcome": "sent", "message_id": "wamid.9"}
    mk(_Resp(429, {"error": {"code": 130429, "message": "x"}}))
    assert WA.send_whatsapp_message_checked(SENDER, "hi")["outcome"] == "rejected_retryable"
    mk(_Resp(400, {"error": {"code": 131009}}))
    assert WA.send_whatsapp_message_checked(SENDER, "hi")["outcome"] == "rejected_permanent"
    mk(_Resp(500, {"error": {}}))
    assert WA.send_whatsapp_message_checked(SENDER, "hi")["outcome"] == "uncertain"
    mk(_Resp(200, bad_json=True))
    assert WA.send_whatsapp_message_checked(SENDER, "hi")["outcome"] == "uncertain"


@pytest.mark.parametrize("exc", [WA.requests.exceptions.Timeout("t"), WA.requests.exceptions.ConnectionError("c"),
                                 RuntimeError("anything")])
def test_checked_sender_timeouts_and_exceptions_are_uncertain_and_do_not_leak(monkeypatch, wa_env, caplog, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(WA.requests, "post", boom)
    res = WA.send_whatsapp_message_checked("+91 98765 43210", "secret body")
    assert res == {"ok": False, "outcome": "uncertain", "error_code": None}
    assert "9876543210" not in caplog.text and "secret body" not in caplog.text and "EAAsecrettoken" not in caplog.text


def test_checked_sender_not_configured_is_a_definite_non_send(monkeypatch):
    monkeypatch.setattr(WA, "WHATSAPP_ACCESS_TOKEN", "")
    called = []
    monkeypatch.setattr(WA.requests, "post", lambda *a, **k: called.append(1))
    assert WA.send_whatsapp_message_checked(SENDER, "hi")["outcome"] == "rejected_permanent" and called == []


def test_checked_sender_does_not_leak_error_text_or_body(monkeypatch, wa_env, caplog):
    monkeypatch.setattr(WA.requests, "post", lambda *a, **k: _Resp(400, {"error": {"code": 131009, "message": "Asha 919876543210 secret"}}))
    res = WA.send_whatsapp_message_checked("919876543210", "hi")
    assert "Asha" not in str(res) and "Asha" not in caplog.text and "919876543210" not in caplog.text


def test_existing_sender_is_unchanged(monkeypatch, wa_env):
    monkeypatch.setattr(WA.requests, "post", lambda *a, **k: _Resp(400, {"error": {"message": "Bad thing"}}))
    assert WA.send_whatsapp_message(SENDER, "hi") == {"ok": False, "error": "Bad thing"}
    assert W.reply_to_sender(SENDER, "hi") == {"ok": False, "error": "Bad thing"}


def test_welcome_reply_sent_marks_slot_sent_permanently(client, scripted):
    calls, _ = scripted
    assert post(client, payload(msg_id="wamid.W1")).status_code == 200
    assert ev("msg:wamid.W1")["reply_state"] == "sent" and len(calls) == 1
    assert "Welcome to OffrO" in calls[0]                                # wording unchanged


def test_welcome_retryable_rejection_releases_slot_and_retry_sends_once(client, scripted):
    calls, outcomes = scripted
    outcomes.append(OUT_RETRYABLE)
    body = payload(msg_id="wamid.W2")
    assert post(client, body).status_code == 500
    assert "reply_state" not in ev("msg:wamid.W2") and ev("msg:wamid.W2")["state"] == "failed"
    assert post(client, body).status_code == 200                         # Meta retry: slot free, sends now
    assert len(calls) == 2 and ev("msg:wamid.W2")["reply_state"] == "sent" and ev("msg:wamid.W2")["state"] == "done"
    assert post(client, body).status_code == 200 and len(calls) == 2     # never again
    assert len(chats()) == 1                                             # Live Chat stored once across the retry


def test_message_handlers_run_once_across_a_reply_retry(client, scripted):
    calls, outcomes = scripted
    outcomes.append(OUT_RETRYABLE)
    ran = []
    W._MESSAGE_HANDLERS.append(lambda **kw: ran.append(kw["msg_id"]))
    body = payload(msg_id="wamid.W3")
    post(client, body)
    post(client, body)
    assert ran == ["wamid.W3"]


def test_welcome_uncertain_outcome_is_kept_never_resent_and_not_retried(client, scripted, caplog):
    calls, outcomes = scripted
    outcomes.append(OUT_UNCERTAIN)
    body = payload(msg_id="wamid.W4")
    assert post(client, body).status_code == 200                         # nothing for Meta to retry
    assert ev("msg:wamid.W4")["reply_state"] == "uncertain" and len(calls) == 1
    assert "[WA-REPLY-UNCERTAIN]" in caplog.text and SENDER not in caplog.text
    expire_lease("msg:wamid.W4")                                         # even a forced reclaim never resends
    _db().wa_webhook_events.update_one({"_id": "msg:wamid.W4"}, {"$set": {"state": "failed"}})
    assert post(client, body).status_code == 200 and len(calls) == 1 and ev("msg:wamid.W4")["reply_state"] == "uncertain"


def test_welcome_permanent_rejection_is_recorded_and_not_retried(client, scripted, caplog):
    calls, outcomes = scripted
    outcomes.append(OUT_PERMANENT)
    assert post(client, payload(msg_id="wamid.W5")).status_code == 200
    assert ev("msg:wamid.W5")["reply_state"] == "rejected" and len(calls) == 1
    assert "[WA-REPLY-REJECTED]" in caplog.text and ev("msg:wamid.W5")["state"] == "done"


def test_unreleasable_retryable_slot_degrades_to_uncertain_never_resent(client, scripted, monkeypatch):
    calls, outcomes = scripted
    outcomes.append(OUT_RETRYABLE)
    monkeypatch.setattr(C, "release_reply_slot", lambda k, t: False)
    assert post(client, payload(msg_id="wamid.W6")).status_code == 200
    assert ev("msg:wamid.W6")["reply_state"] == "uncertain"


def test_reply_slot_store_error_sends_nothing(client, scripted, monkeypatch):
    calls, _ = scripted
    monkeypatch.setattr(C, "claim_reply_slot", lambda k, t: (_ for _ in ()).throw(RuntimeError("db down")))
    assert post(client, payload(msg_id="wamid.W7")).status_code == 200 and calls == []


@pytest.mark.parametrize("word,confirmation", [("STOP", C.STOP_CONFIRMATION), ("START", C.START_CONFIRMATION)])
def test_confirmation_retryable_rejection_retries_and_confirms_once(client, scripted, word, confirmation):
    calls, outcomes = scripted
    outcomes.append(OUT_RETRYABLE)
    body = payload(text=word, msg_id="wamid.C1")
    assert post(client, body).status_code == 500
    assert _db().wa_contacts.find_one({"phone": SENDER}) is not None     # consent already saved
    assert post(client, body).status_code == 200
    assert calls == [confirmation, confirmation] and ev("msg:wamid.C1")["reply_state"] == "sent"
    assert _db().wa_consent_events.count_documents({}) == 1              # ledger idempotent
    assert post(client, body).status_code == 200 and len(calls) == 2


@pytest.mark.parametrize("outcome", [OUT_UNCERTAIN, OUT_PERMANENT])
def test_confirmation_uncertain_or_permanent_is_not_retried_and_optout_stands(client, scripted, outcome):
    calls, outcomes = scripted
    outcomes.append(outcome)
    assert post(client, payload(text="STOP", msg_id="wamid.C2")).status_code == 200
    assert len(calls) == 1 and _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_out"


def test_retried_confirmation_is_not_sent_if_a_newer_event_superseded_it(client, scripted):
    calls, outcomes = scripted
    outcomes.append(OUT_RETRYABLE)
    assert post(client, payload(text="STOP", msg_id="wamid.C3", ts="1762843200")).status_code == 500
    post(client, payload(text="START", msg_id="wamid.C4", ts="1762843260"))       # newer decision
    calls.clear()
    assert post(client, payload(text="STOP", msg_id="wamid.C3", ts="1762843200")).status_code == 200
    assert calls == [] and _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_in"


def test_consent_failure_notice_uncertain_retries_the_write_but_never_resends(client, scripted, monkeypatch):
    calls, outcomes = scripted
    outcomes.append(OUT_UNCERTAIN)
    body = payload(text="STOP", msg_id="wamid.C5")
    with monkeypatch.context() as m:
        m.setattr(C, "db", _BrokenDb(_db(), {C.CONTACTS: {"update_one"}}))
        assert post(client, body).status_code == 500                    # opt-out NOT saved -> retry the write
    assert calls == [C.STOP_FAILURE_NOTICE]
    assert post(client, body).status_code == 200                        # DB back: write succeeds on the retry
    assert _db().wa_contacts.find_one({"phone": SENDER})["status"] == "opted_out"
    assert calls == [C.STOP_FAILURE_NOTICE]                             # no second message of any kind


def test_consent_failure_notice_retryable_then_retry_saves_and_does_not_send_a_false_confirmation(client, scripted, monkeypatch):
    calls, outcomes = scripted
    outcomes.append(OUT_RETRYABLE)
    body = payload(text="STOP", msg_id="wamid.C6")
    with monkeypatch.context() as m:
        m.setattr(C, "db", _BrokenDb(_db(), {C.CONTACTS: {"update_one"}}))
        assert post(client, body).status_code == 500
    assert post(client, body).status_code == 200
    assert calls == [C.STOP_FAILURE_NOTICE, C.STOP_CONFIRMATION]        # notice never delivered -> confirmation is true now
    assert [c for c in calls if c == C.STOP_CONFIRMATION] == [C.STOP_CONFIRMATION]


def test_no_confirmation_when_consent_write_failed_even_if_sender_works(client, scripted, monkeypatch):
    calls, _ = scripted
    monkeypatch.setattr(C, "db", _BrokenDb(_db(), {C.CONTACTS: {"update_one"}}))
    post(client, payload(text="STOP", msg_id="wamid.C7"))
    assert C.STOP_CONFIRMATION not in calls


# ── 5. access-log redaction of the webhook verify token ────────────────────────

def _access_record(path, args=None):
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             args or ("1.2.3.4:5", "GET", path, "1.1", 200), None)


@pytest.mark.parametrize("path,expect", [
    ("/webhook?hub.mode=subscribe&hub.verify_token=SECRETVALUE&hub.challenge=99",
     "/webhook?hub.mode=subscribe&hub.verify_token=***&hub.challenge=99"),
    ("/webhook?hub.verify_token=SECRETVALUE", "/webhook?hub.verify_token=***"),
    ("/webhook?hub.challenge=1&hub.verify_token=SECRETVALUE", "/webhook?hub.challenge=1&hub.verify_token=***"),
    ("/webhook?hub%2Everify_token=SECRETVALUE&x=1", "/webhook?hub%2Everify_token=***&x=1"),
    ("/webhook?HUB.VERIFY_TOKEN=SECRETVALUE", "/webhook?HUB.VERIFY_TOKEN=***"),
    ("/webhook?hub.verify_token=SEC%26RET%3D&hub.mode=subscribe", "/webhook?hub.verify_token=***&hub.mode=subscribe"),
    ("/webhook", "/webhook"),
    ("/admin/whatsapp?token=keepme&mode=a", "/admin/whatsapp?token=keepme&mode=a"),
])
def test_access_log_filter_redacts_only_the_verify_token(path, expect):
    rec = _access_record(path)
    assert S.VerifyTokenRedactingFilter().filter(rec) is True
    line = rec.getMessage()
    assert expect in line and "SECRETVALUE" not in line and "SEC%26RET" not in line
    assert rec.args[0] == "1.2.3.4:5" and rec.args[3] == "1.1" and rec.args[4] == 200   # other args intact


@pytest.mark.parametrize("record", [
    logging.LogRecord("u", logging.INFO, "f", 1, "plain message", None, None),
    logging.LogRecord("u", logging.INFO, "f", 1, "%(a)s", ({"a": "hub.verify_token=SECRETVALUE"},), None),
    logging.LogRecord("u", logging.INFO, "f", 1, "no args hub.verify_token=SECRETVALUE", None, None),
    logging.LogRecord("u", logging.INFO, "f", 1, "%s %s", (None, 3.5), None),
])
def test_access_log_filter_never_raises_or_drops_records(record):
    assert S.VerifyTokenRedactingFilter().filter(record) is True
    record.getMessage()


def test_access_log_redaction_is_installed_idempotent_and_survives_uvicorn_reconfig():
    import io
    import logging.config
    from uvicorn.config import LOGGING_CONFIG
    lg = logging.getLogger("uvicorn.access")
    assert S.install_access_log_redaction() in (True, False)
    assert S.install_access_log_redaction() is False
    assert sum(isinstance(f, S.VerifyTokenRedactingFilter) for f in lg.filters) == 1
    logging.config.dictConfig(LOGGING_CONFIG)                           # what uvicorn does at start-up
    assert sum(isinstance(f, S.VerifyTokenRedactingFilter) for f in lg.filters) == 1
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg.addHandler(h)
    try:
        lg.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/webhook?hub.verify_token=SECRETVALUE&hub.challenge=9", "1.1", 200)
    finally:
        lg.removeHandler(h)
    assert "SECRETVALUE" not in buf.getvalue() and "hub.verify_token=***" in buf.getvalue()


def test_verify_handshake_still_works_with_custom_token(client, monkeypatch):
    monkeypatch.setattr(W, "WHATSAPP_VERIFY_TOKEN", "my-token")
    r = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "my-token", "hub.challenge": "777"})
    assert r.status_code == 200 and r.text == "777"


# ── 6. duplicate-key handling in the contact write ─────────────────────────────

class _DupColl:
    """Wraps wa_contacts: update_one raises DuplicateKeyError for the first `times` calls (None = always)."""
    def __init__(self, real, times):
        self.real, self.times, self.n = real, times, 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    def update_one(self, *a, **k):
        self.n += 1
        if self.times is None or self.n <= self.times:
            raise DuplicateKeyError("E11000 duplicate key")
        return self.real.update_one(*a, **k)


class _DupDb:
    def __init__(self, real, times):
        self.real, self.coll = real, _DupColl(real[C.CONTACTS], times)

    def __getitem__(self, name):
        return self.coll if name == C.CONTACTS else self.real[name]

    def __getattr__(self, name):
        return self[name]


def _seed_contact(seq, with_seq=True):
    d = {"_id": SENDER, "phone": SENDER, "status": "opted_out"}
    if with_seq:
        d["consent_seq"] = seq
    _db().wa_contacts.insert_one(d)


def test_duplicate_key_with_a_genuinely_newer_stored_event_is_superseded(monkeypatch):
    _seed_contact(1762843300 * 2 + 1)
    monkeypatch.setattr(C, "db", _DupDb(_db(), None))
    assert C.record_opt_in(SENDER, "START", "wamid.D1", "1762843200", whatsapp_id=True) == "superseded"


def test_duplicate_key_from_another_unique_index_is_a_failure_not_superseded(monkeypatch):
    monkeypatch.setattr(C, "db", _DupDb(_db(), None))                    # no contact under this _id
    with pytest.raises(C.ConsentWriteError):
        C.record_opt_out(SENDER, "STOP", "wamid.D2", "1762843200", whatsapp_id=True)


def test_real_phone_index_conflict_is_a_failure(monkeypatch):
    C.ensure_indexes()
    _db().wa_contacts.insert_one({"_id": "someone-else", "phone": SENDER, "status": "opted_in", "consent_seq": 1})
    with pytest.raises(Exception) as ei:
        C.record_opt_out(SENDER, "STOP", "wamid.D3", "1762843200", whatsapp_id=True)
    assert not isinstance(ei.value, ValueError)


def test_lost_insert_race_is_retried_once_and_applies(monkeypatch):
    _seed_contact(1762843100 * 2)                                        # older than the incoming event
    d = _DupDb(_db(), 1)
    monkeypatch.setattr(C, "db", d)
    assert C.record_opt_out(SENDER, "STOP", "wamid.D4", "1762843200", whatsapp_id=True) == "applied"
    assert d.coll.n == 2 and _db().wa_contacts.find_one({"_id": SENDER})["consent_seq"] == 1762843200 * 2 + 1


@pytest.mark.parametrize("stored", [1762843100 * 2, 1762843200 * 2 + 1, None])
def test_persistent_duplicate_with_older_equal_or_missing_seq_is_a_failure(monkeypatch, stored):
    _seed_contact(stored, with_seq=stored is not None)
    d = _DupDb(_db(), None)
    monkeypatch.setattr(C, "db", d)
    with pytest.raises(C.ConsentWriteError):
        C.record_opt_out(SENDER, "STOP", "wamid.D5", "1762843200", whatsapp_id=True)
    assert d.coll.n == 2                                                 # exactly one retry


def test_non_integer_stored_seq_is_not_treated_as_newer(monkeypatch):
    _db().wa_contacts.insert_one({"_id": SENDER, "phone": SENDER, "consent_seq": "9999999999999"})
    monkeypatch.setattr(C, "db", _DupDb(_db(), None))
    with pytest.raises(C.ConsentWriteError):
        C.record_opt_out(SENDER, "STOP", "wamid.D6", "1762843200", whatsapp_id=True)


def test_webhook_duplicate_key_failure_sends_only_the_failure_notice(client, scripted, monkeypatch, caplog):
    calls, _ = scripted
    monkeypatch.setattr(C, "db", _DupDb(_db(), None))
    assert post(client, payload(text="STOP", msg_id="wamid.D7")).status_code == 200
    assert calls == [C.STOP_FAILURE_NOTICE] and "[WA-CONSENT-FAILURE]" in caplog.text


def test_webhook_legitimately_superseded_event_sends_no_confirmation(client, scripted, monkeypatch):
    calls, _ = scripted
    _seed_contact(1762843900 * 2 + 1)
    monkeypatch.setattr(C, "db", _DupDb(_db(), None))
    assert post(client, payload(text="START", msg_id="wamid.D8", ts="1762843200")).status_code == 200
    assert calls == []


# ═════════════ Event timestamp validation (future timestamps are rejected for consent ordering) ═════════════
import time as _time
import types as _types

NOW = 1_800_000_000          # frozen server clock for the unit tests below


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(C, "time", _types.SimpleNamespace(time=lambda: float(NOW)))
    return NOW


def _real_future(seconds):
    return str(int(_time.time()) + seconds)


def _contact():
    return _db().wa_contacts.find_one({"_id": SENDER})


def _ledger():
    return _db().wa_consent_events.count_documents({})


def test_tolerance_constant_is_five_minutes():
    assert C.FUTURE_TOLERANCE_SECONDS == 300


@pytest.mark.parametrize("offset", [-86400 * 30, -3600, -1, 0])
def test_legitimate_past_and_current_timestamps_are_accepted(frozen_now, offset):
    assert C.record_opt_out(SENDER, "STOP", "wamid.T%d" % (-offset), str(NOW + offset), whatsapp_id=True) == "applied"
    assert _contact()["consent_seq"] == (NOW + offset) * 2 + 1


@pytest.mark.parametrize("offset", [1, 60, 299, 300])
def test_timestamps_within_the_five_minute_tolerance_are_accepted(frozen_now, offset):
    assert C.record_opt_in(SENDER, "START", "wamid.W%d" % offset, str(NOW + offset), whatsapp_id=True) == "applied"
    assert _contact()["status"] == "opted_in" and _contact()["consent_seq"] == (NOW + offset) * 2


@pytest.mark.parametrize("offset", [301, 600, 3600, 86400 * 365])
@pytest.mark.parametrize("fn", ["record_opt_out", "record_opt_in"])
def test_timestamps_beyond_tolerance_are_rejected_before_any_write(frozen_now, offset, fn):
    with pytest.raises(C.InvalidTimestampError):
        getattr(C, fn)(SENDER, "x", "wamid.X%d" % offset, str(NOW + offset), whatsapp_id=True)
    assert _contact() is None and _ledger() == 0                        # nothing created, nothing recorded
    assert not issubclass(C.InvalidTimestampError, ValueError)          # never confused with 'invalid phone'


def test_rejected_timestamp_is_not_clamped_to_now(frozen_now):
    with pytest.raises(C.InvalidTimestampError):
        C.record_opt_out(SENDER, "STOP", "wamid.NC", str(NOW + 10_000), whatsapp_id=True)
    assert _contact() is None and C.consent_status(SENDER) == "unknown"


def test_far_future_12_digit_timestamp_is_rejected(frozen_now):
    with pytest.raises(C.InvalidTimestampError):
        C.record_opt_in(SENDER, "START", "wamid.FF", "999999999999", whatsapp_id=True)
    assert C.may_send_marketing(SENDER) is False


def test_forged_far_future_start_cannot_create_opt_in(frozen_now):
    with pytest.raises(C.InvalidTimestampError):
        C.record_opt_in(SENDER, "START", "wamid.FS", "999999999999", whatsapp_id=True)
    assert C.consent_status(SENDER) == "unknown" and C.may_send_marketing(SENDER) is False and _ledger() == 0


def test_forged_far_future_stop_cannot_lock_out_a_legitimate_later_start(frozen_now):
    with pytest.raises(C.InvalidTimestampError):
        C.record_opt_out(SENDER, "STOP", "wamid.FL", "999999999999", whatsapp_id=True)
    assert C.record_opt_in(SENDER, "START", "wamid.LS", str(NOW + 5), whatsapp_id=True) == "applied"
    assert C.consent_status(SENDER) == "opted_in"                       # the genuine START is not 'superseded'


@pytest.mark.parametrize("existing,forged_fn,forged_status", [
    ("record_opt_in", "record_opt_out", "opted_in"),
    ("record_opt_out", "record_opt_in", "opted_out"),
])
def test_existing_consent_is_unchanged_by_an_invalid_timestamp_event(frozen_now, existing, forged_fn, forged_status):
    getattr(C, existing)(SENDER, "x", "wamid.E1", str(NOW - 100), whatsapp_id=True)
    before, n = _contact(), _ledger()
    with pytest.raises(C.InvalidTimestampError):
        getattr(C, forged_fn)(SENDER, "x", "wamid.E2", str(NOW + 10_000), whatsapp_id=True)
    after = _contact()
    assert after == before and after["status"] == forged_status and _ledger() == n


def test_legitimate_ordering_idempotency_and_same_second_stop_priority_are_preserved(frozen_now):
    t = str(NOW - 50)
    C.record_opt_in(SENDER, "START", "wamid.O1", t, whatsapp_id=True)
    C.record_opt_out(SENDER, "STOP", "wamid.O2", t, whatsapp_id=True)           # same second: STOP wins
    assert C.consent_status(SENDER) == "opted_out"
    assert C.record_opt_in(SENDER, "START", "wamid.O3", t, whatsapp_id=True) == "superseded"
    assert C.record_opt_out(SENDER, "STOP", "wamid.O2", t, whatsapp_id=True) == "applied"      # idempotent retry
    assert _db().wa_consent_events.count_documents({"_id": "opt_out:wamid.O2"}) == 1
    assert C.record_opt_in(SENDER, "START", "wamid.O4", str(NOW - 10), whatsapp_id=True) == "applied"
    assert C.record_opt_out(SENDER, "STOP", "wamid.O5", str(NOW - 20), whatsapp_id=True) == "superseded"
    assert C.consent_status(SENDER) == "opted_in"


@pytest.mark.parametrize("ts", ["", None, "abc", "12.5", "1234567890123", "-5"])
def test_missing_or_malformed_timestamp_keeps_existing_receive_time_behaviour(frozen_now, ts):
    assert C.record_opt_out(SENDER, "STOP", "wamid.M%s" % abs(hash(str(ts))), ts, whatsapp_id=True) == "applied"
    assert _contact()["consent_seq"] == NOW * 2 + 1


# ── webhook level: nothing is silently mishandled ──────────────────────────────

def test_webhook_forged_future_start_changes_nothing_sends_nothing_and_is_monitored(client, sent, caplog):
    r = post(client, payload(text="START", msg_id="wamid.WF1", ts=_real_future(3600)))
    assert r.status_code == 200                                         # retrying identical bytes cannot help
    assert _contact() is None and _ledger() == 0 and sent == []         # no consent, no confirmation, no reply
    assert len(chats()) == 1                                            # still visible in Live Chat
    assert any(rec.levelname == "ERROR" and "[WA-CONSENT-INVALID-TIMESTAMP]" in rec.getMessage() for rec in caplog.records)
    assert SENDER not in caplog.text and ev("msg:wamid.WF1")["state"] == "done"


def test_webhook_future_stop_is_never_silent_never_a_confirmation_and_never_opt_in(client, sent, caplog):
    r = post(client, payload(text="STOP", msg_id="wamid.WF2", ts=_real_future(3600)))
    assert r.status_code == 200
    assert sent == [(SENDER, C.STOP_FAILURE_NOTICE)]                    # told it was NOT processed
    assert C.STOP_CONFIRMATION not in [m for _, m in sent] and C.START_CONFIRMATION not in [m for _, m in sent]
    assert _contact() is None and C.may_send_marketing(SENDER) is False and _ledger() == 0
    assert "[WA-CONSENT-INVALID-TIMESTAMP]" in caplog.text and "[WA-CONSENT-FAILURE]" not in caplog.text
    assert ev("msg:wamid.WF2")["consent_failed"] is True and ev("msg:wamid.WF2")["notice_sent"] is True


def test_webhook_future_stop_does_not_clear_existing_opt_in_but_user_is_told(client, sent):
    post(client, payload(text="START", msg_id="wamid.WF3", ts=str(int(_time.time()) - 60)))
    sent.clear()
    before = _contact()
    post(client, payload(text="STOP", msg_id="wamid.WF4", ts=_real_future(3600)))
    assert _contact() == before and sent == [(SENDER, C.STOP_FAILURE_NOTICE)]


def test_webhook_forged_far_future_stop_cannot_block_the_real_start(client, sent):
    post(client, payload(text="STOP", msg_id="wamid.WF5", ts="4102444800"))              # forged, year 2100
    assert _contact() is None
    post(client, payload(text="START", msg_id="wamid.WF6", ts=str(int(_time.time()))))
    assert _contact()["status"] == "opted_in" and sent[-1] == (SENDER, C.START_CONFIRMATION)


def test_webhook_within_tolerance_future_timestamp_is_processed_normally(client, sent):
    post(client, payload(text="STOP", msg_id="wamid.WF7", ts=_real_future(120)))
    assert _contact()["status"] == "opted_out" and sent == [(SENDER, C.STOP_CONFIRMATION)]


def test_webhook_future_stop_notice_retryable_asks_meta_to_retry_uncertain_does_not(client, scripted):
    calls, outcomes = scripted
    outcomes.append(OUT_RETRYABLE)
    body = payload(text="STOP", msg_id="wamid.WF8", ts=_real_future(3600))
    assert post(client, body).status_code == 500
    assert post(client, body).status_code == 200 and calls == [C.STOP_FAILURE_NOTICE] * 2
    outcomes.append(OUT_UNCERTAIN)
    assert post(client, payload(text="STOP", msg_id="wamid.WF9", ts=_real_future(3600))).status_code == 200


def test_webhook_duplicate_future_stop_is_still_one_notice(client, sent):
    body = payload(text="STOP", msg_id="wamid.WF10", ts=_real_future(3600))
    post(client, body)
    post(client, body)
    assert sent == [(SENDER, C.STOP_FAILURE_NOTICE)]


@pytest.mark.parametrize("word", ["UNSUBSCRIBE", "cancel", "End", "QUIT"])
def test_webhook_every_stop_keyword_with_future_timestamp_is_handled_the_same_way(client, sent, word):
    post(client, payload(text=word, msg_id="wamid.K" + word, ts=_real_future(3600)))
    assert sent == [(SENDER, C.STOP_FAILURE_NOTICE)] and _contact() is None
