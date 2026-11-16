"""Phase 2G read-only support endpoints + dedicated header-media upload. Meta is mocked; nothing is sent.
Run:  python -m pytest test_wa_phase2g_endpoints.py -q -W ignore"""
import json
from datetime import datetime, timedelta

import pytest

import services.wa_campaign_manager as M
import services.wa_consent as C
import services.whatsapp as WA
import test_wa_campaign_lifecycle as L
from test_wa_campaign_lifecycle import (BASE, H, P1, P2, TESTNUM, _env, admin, audit_log, client, grant, make_user, meta, sender,  # noqa: F401
                                        ready_campaign, start, stop, admin_consent)

ROOT = "/admin/whatsapp-campaigns"
D = L._DB


def recs(cid):
    return list(D[M.RECIPIENTS].find({"campaign_id": cid}).sort("_id", 1))


def details(cid, token="adm", **params):
    return client.get("%s/%s/recipient-details" % (BASE, cid), headers=H(token), params=params)


def dump(r):
    return json.dumps(r.json(), default=str)


# ═════════════ recipient delivery details ═════════════

def test_details_show_masked_numbers_and_every_requested_field(admin, _env):
    cid, _ids = ready_campaign()
    start(P1)
    rows = recs(cid)
    now = datetime.utcnow()
    D[M.RECIPIENTS].update_one({"_id": rows[0]["_id"]}, {"$set": {"state": "delivered", "message_id": "wamid.HBgMOTE5ODc2NTQzMjEw", "sent_at": now,
                                                                  "status_ts": now, "attempts": 1, "delivery_rank": 3, "queued_at": now}})
    r = details(cid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(rows) and body["pages"] == 1
    row = next(x for x in body["items"] if x["state"] == "delivered")
    assert set(row) == {"phone_masked", "source", "state", "eligibility", "message_ref", "has_message_id", "timestamps", "failure", "retry"}
    assert row["phone_masked"].startswith("***") and len(row["phone_masked"]) == 7
    assert row["eligibility"]["eligible_at_snapshot"] is True and row["eligibility"]["consent_now"] in ("opted_in", "unknown")
    assert row["has_message_id"] is True and len(row["message_ref"]) == 12
    assert row["timestamps"]["sent_at"].endswith("Z") and row["retry"]["attempts"] == 1 and row["failure"] is None


def test_details_never_return_a_full_number_a_wamid_or_an_admin_id(admin, _env):
    cid, _ids = ready_campaign()
    rows = recs(cid)
    D[M.RECIPIENTS].update_one({"_id": rows[0]["_id"]}, {"$set": {"state": "sent", "message_id": "wamid.HBgMOTE5ODc2NTQzMjEw"}})
    raw = dump(details(cid))
    for secret in (P1, P2, "wamid", "HBgM", "919", L.TOKEN):
        assert secret not in raw, secret
    assert "***" in raw


def test_failure_classification_and_retry_information(admin, _env):
    cid, _ids = ready_campaign()
    rows = recs(cid)
    nxt = datetime.utcnow() + timedelta(minutes=5)
    D[M.RECIPIENTS].update_one({"_id": rows[0]["_id"]}, {"$set": {"state": "failed", "error": "rejected", "error_code": 131026, "attempts": 3}})
    D[M.RECIPIENTS].update_one({"_id": rows[1]["_id"]}, {"$set": {"state": "uncertain", "uncertain_reason": "lease_expired_after_send_started"}})
    items = details(cid).json()["items"]
    failed = next(i for i in items if i["state"] == "failed")
    assert failed["failure"] == {"class": "rejected", "code": 131026} and failed["retry"]["attempts"] == 3
    unc = next(i for i in items if i["state"] == "uncertain")
    assert unc["failure"]["class"] == "lease_expired_after_send_started"
    D[M.RECIPIENTS].update_one({"_id": rows[0]["_id"]}, {"$set": {"state": "queued", "error_code": 130429, "attempts": 1, "next_attempt_at": nxt}})
    q = next(i for i in details(cid).json()["items"] if i["state"] == "queued")
    assert q["failure"]["class"] == "retry_scheduled" and q["retry"]["next_attempt_at"].endswith("Z")


def test_state_and_last4_filters_and_pagination(admin, _env):
    cid, _ids = ready_campaign()
    rows = recs(cid)
    D[M.RECIPIENTS].update_one({"_id": rows[0]["_id"]}, {"$set": {"state": "read"}})
    assert [i["state"] for i in details(cid, state="read").json()["items"]] == ["read"]
    last4 = rows[1]["phone"][-4:]
    only = details(cid, last4=last4).json()
    assert only["total"] >= 1 and all(i["phone_masked"] == "***" + last4 for i in only["items"])
    p = details(cid, page_size=1).json()
    assert len(p["items"]) == 1 and p["pages"] == len(rows)
    assert details(cid, page=99, page_size=1).json()["page"] == p["pages"]          # clamped, not an error


@pytest.mark.parametrize("params", [{"state": "bogus"}, {"last4": "12"}, {"last4": "12ab"}, {"last4": "12345"}, {"page_size": 101}, {"page": 0}])
def test_details_reject_bad_parameters(admin, _env, params):
    cid, _ids = ready_campaign()
    assert details(cid, **params).status_code in (400, 422)


def test_details_need_a_snapshot_and_a_real_campaign(admin, _env):
    c = L.mk()
    r = details(c["id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_snapshot"
    assert details("0" * 24).status_code == 404


def test_details_require_the_view_permission(admin, _env):
    cid, _ids = ready_campaign()
    assert client.get("%s/%s/recipient-details" % (BASE, cid)).status_code == 401
    make_user("noperm", {})
    make_user("addonly", grant("add", "edit", "approve", "delete"))
    assert details(cid, token="noperm").status_code == 403 and details(cid, token="addonly").status_code == 403
    make_user("viewer", grant("view"))
    assert details(cid, token="viewer").status_code == 200


def test_details_consent_is_live_and_stop_wins(admin, _env):
    cid, _ids = ready_campaign()
    p = recs(cid)[0]["phone"]
    stop(p, "wamid.STOPLIVE")
    row = next(i for i in details(cid).json()["items"] if i["phone_masked"] == "***" + p[-4:])
    assert row["eligibility"]["consent_now"] == "opted_out"


def test_details_consent_read_failure_shows_unavailable_not_a_guess(admin, _env):
    cid, _ids = ready_campaign()

    def boom(_p):
        raise RuntimeError("db down EAAB-secret-token-123")
    _env["mp"].setattr(C, "consent_status", boom)
    r = details(cid)
    assert r.status_code == 200 and {i["eligibility"]["consent_now"] for i in r.json()["items"]} == {"unavailable"}
    assert "EAAB" not in r.text


def test_details_change_nothing_and_send_nothing(admin, _env, sender):
    cid, _ids = ready_campaign()
    before = (list(D[M.RECIPIENTS].find({})), D[M.CAMPAIGNS].find_one({"_id": M.ObjectId(cid)}), D.wa_campaign_audit.count_documents({}))
    details(cid)
    details(cid, state="sent")
    assert before == (list(D[M.RECIPIENTS].find({})), D[M.CAMPAIGNS].find_one({"_id": M.ObjectId(cid)}), D.wa_campaign_audit.count_documents({}))
    assert sender.calls == []


# ═════════════ consent listing ═════════════

def consents(token="adm", **params):
    return client.get(ROOT + "/consents", headers=H(token), params=params)


def events(token="adm", **params):
    return client.get(ROOT + "/consent-events", headers=H(token), params=params)


def seed_consents():
    start(P1, "wamid.A1")
    admin_consent(P2)
    stop("919700000003", "wamid.B1")


def test_consent_listing_fields_and_masking(admin, _env):
    seed_consents()
    r = consents()
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    by = {i["phone_masked"]: i for i in body["items"]}
    assert set(by) == {"***3210", "***5678", "***0003"}
    assert by["***3210"]["status"] == "opted_in" and by["***3210"]["last_event"]["channel"] == "whatsapp_keyword"
    admin_row = by["***5678"]
    assert admin_row["status"] == "opted_in" and admin_row["last_event"]["evidence_ref"] == "FORM-1"
    assert admin_row["last_event"]["consent_source"] == "signup_form" and admin_row["last_event"]["recorded_by"] == "Asha"
    assert by["***0003"]["status"] == "opted_out" and by["***0003"]["opted_out_at"].endswith("Z")
    raw = r.text
    for secret in (P1, P2, "919700000003", "wamid", '"a1"', "admin_id"):
        assert secret not in raw, secret


def test_consent_filters_and_pagination(admin, _env):
    seed_consents()
    assert {i["status"] for i in consents(status="opted_out").json()["items"]} == {"opted_out"}
    assert consents(last4="5678").json()["total"] == 1
    p = consents(page_size=1).json()
    assert len(p["items"]) == 1 and p["pages"] == 3
    for bad in ({"status": "unknown"}, {"last4": "9"}, {"page_size": 500}):
        assert consents(**bad).status_code in (400, 422)


def test_consent_events_ledger_has_stop_start_and_admin_history(admin, _env):
    start(P1, "wamid.A1")
    stop(P1, "wamid.A2")
    admin_consent(P2)
    r = events()
    assert r.status_code == 200
    items = r.json()["items"]
    assert {i["event"] for i in items} == {"opt_in", "opt_out", "admin_opt_in"} and r.json()["total"] == 3
    assert next(i for i in items if i["event"] == "opt_out")["keyword"] == "STOP"
    adm = next(i for i in items if i["event"] == "admin_opt_in")
    assert adm["evidence_ref"] == "FORM-1" and adm["recorded_by"] == "Asha" and adm["recorded_by_role"] == "Lead"
    assert all(i["phone_masked"].startswith("***") for i in items)
    for secret in (P1, P2, "wamid", "a1\""):
        assert secret not in r.text, secret
    assert [i["event"] for i in events(event="opt_out").json()["items"]] == ["opt_out"]
    assert events(event="nonsense").status_code == 400 and events(last4="3210").json()["total"] == 2


@pytest.mark.parametrize("path", ["/consents", "/consent-events"])
def test_consent_endpoints_require_the_view_permission(admin, _env, path):
    assert client.get(ROOT + path).status_code == 401
    make_user("noperm", {})
    make_user("noview", grant("add", "edit", "delete", "approve"))
    assert client.get(ROOT + path, headers=H("noperm")).status_code == 403
    assert client.get(ROOT + path, headers=H("noview")).status_code == 403
    make_user("viewer", grant("view"))
    assert client.get(ROOT + path, headers=H("viewer")).status_code == 200


@pytest.mark.parametrize("path", ["/consents", "/consent-events"])
def test_consent_endpoints_are_read_only(path):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(ROOT + path, headers=H("adm")).status_code in (404, 405)


def test_consent_listing_fails_soft_with_a_friendly_503(admin, _env):
    from test_wa_webhook_consent import _BrokenDb
    _env["mp"].setattr(C, "db", _BrokenDb(D, {C.CONTACTS: {"count_documents", "find"}}))
    r = consents()
    assert r.status_code == 503 and "temporarily unavailable" in r.text and "Traceback" not in r.text


def test_consent_listing_changes_nothing(admin, _env):
    seed_consents()
    before = (list(D[C.CONTACTS].find({})), list(D[C.EVENTS].find({})), D.wa_campaign_audit.count_documents({}))
    consents(); events()
    assert before == (list(D[C.CONTACTS].find({})), list(D[C.EVENTS].find({})), D.wa_campaign_audit.count_documents({}))


# ═════════════ dedicated header-media upload ═════════════

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"\x00" * 64


class Uploader:
    def __init__(self):
        self.calls, self.result = [], {"ok": True, "media_id": "1234567890123"}

    def __call__(self, data, mime, filename="x"):
        self.calls.append({"size": len(data), "mime": mime, "filename": filename})
        return dict(self.result)


@pytest.fixture
def uploader(_env):
    u = Uploader()
    _env["mp"].setattr(WA, "upload_media_to_whatsapp", u)
    return u


def upload(data, mime, name="banner.jpg", token="adm"):
    return client.post(ROOT + "/media/header", headers=H(token), files={"file": (name, data, mime)})


@pytest.mark.parametrize("data,mime,kind", [(JPEG, "image/jpeg", "image"), (PNG, "image/png", "image"), (MP4, "video/mp4", "video"),
                                            (MP4, "video/3gpp", "video"), (PDF, "application/pdf", "document")])
def test_upload_accepts_each_allowed_format_and_returns_the_patchable_object(admin, uploader, data, mime, kind):
    r = upload(data, mime)
    assert r.status_code == 200, r.text
    assert r.json()["header_media"] == {"type": kind, "id": "1234567890123"} and r.json()["size_bytes"] == len(data)
    assert uploader.calls[0]["mime"] == mime
    assert M.clean_header_media(r.json()["header_media"]) == r.json()["header_media"]      # exactly what PATCH accepts


def test_upload_result_can_be_saved_on_a_campaign(admin, uploader, meta):
    r = upload(JPEG, "image/jpeg")
    c = L.mk()
    p = client.patch("%s/%s" % (BASE, c["id"]), headers=H("adm"), json={"header_media": r.json()["header_media"], "expected_version": c["version"]})
    assert p.status_code == 200
    assert D[M.CAMPAIGNS].find_one({"_id": M.ObjectId(c["id"])})["header_media"] == {"type": "image", "id": "1234567890123"}   # stored; API output redacts the id


@pytest.mark.parametrize("mime", ["image/gif", "image/svg+xml", "text/html", "application/x-msdownload", "application/zip", "video/webm", ""])
def test_upload_rejects_unsupported_types(admin, uploader, mime):
    r = upload(JPEG, mime)
    assert r.status_code == 415 and r.json()["detail"]["code"] == "unsupported_type" and uploader.calls == []


@pytest.mark.parametrize("data,mime", [(PNG, "image/jpeg"), (JPEG, "image/png"), (b"<script>alert(1)</script>", "image/jpeg"),
                                       (b"MZ\x90\x00" + b"\x00" * 60, "application/pdf"), (PDF, "video/mp4")])
def test_upload_checks_the_real_content_not_the_declared_type(admin, uploader, data, mime):
    r = upload(data, mime)
    assert r.status_code == 415 and r.json()["detail"]["code"] == "content_mismatch" and uploader.calls == []


def test_upload_enforces_size_limits_per_type(admin, uploader):
    assert upload(JPEG + b"\x00" * (5 * 1024 * 1024), "image/jpeg").status_code == 413
    assert upload(PDF + b"\x00" * (10 * 1024 * 1024), "application/pdf").status_code == 413
    assert upload(MP4 + b"\x00" * (16 * 1024 * 1024), "video/mp4").status_code == 413
    assert uploader.calls == []
    assert upload(JPEG + b"\x00" * (5 * 1024 * 1024 - 100), "image/jpeg").status_code == 200


def test_upload_rejects_an_empty_file(admin, uploader):
    r = upload(b"", "image/jpeg")
    assert r.status_code in (400, 415) and uploader.calls == []


def test_upload_filename_is_sanitised(admin, uploader):
    upload(JPEG, "image/jpeg", name="../../etc/pa<ss>wd;rm -rf.jpg")
    assert "/" not in uploader.calls[0]["filename"] and "<" not in uploader.calls[0]["filename"] and ";" not in uploader.calls[0]["filename"]


def test_upload_requires_the_edit_permission(admin, uploader):
    assert client.post(ROOT + "/media/header", files={"file": ("a.jpg", JPEG, "image/jpeg")}).status_code == 401
    make_user("viewer", grant("view"))
    make_user("noperm", {})
    assert upload(JPEG, "image/jpeg", token="viewer").status_code == 403
    assert upload(JPEG, "image/jpeg", token="noperm").status_code == 403
    assert uploader.calls == []
    make_user("editor", grant("view", "edit"))
    assert upload(JPEG, "image/jpeg", token="editor").status_code == 200


def test_the_old_general_upload_routes_are_not_reused():
    import routers.wa_campaign_media as MED
    src = open(MED.__file__, encoding="utf-8").read()
    assert "get_current_admin" not in src and "_cloudinary" not in src and "upload-image" not in src
    assert 'require_permission(MODULE, "edit")' in src


def test_meta_failure_is_a_friendly_502_without_leaking_provider_text(admin, uploader):
    uploader.result = {"ok": False, "error": "Invalid OAuth access token EAAB-secret-token-123"}
    r = upload(JPEG, "image/jpeg")
    assert r.status_code == 502 and r.json()["detail"]["code"] == "upload_failed"
    assert "EAAB" not in r.text and "OAuth" not in r.text


def test_an_exception_from_the_uploader_is_a_friendly_502(admin, _env):
    def boom(*a, **k):
        raise RuntimeError("EAAB-secret-token-123 trace")
    _env["mp"].setattr(WA, "upload_media_to_whatsapp", boom)
    r = upload(JPEG, "image/jpeg")
    assert r.status_code == 502 and "EAAB" not in r.text


def test_upload_is_audited_without_secrets_and_never_sends_a_message(admin, uploader, sender):
    upload(JPEG, "image/jpeg")
    upload(b"x" * 10, "image/gif")
    log = audit_log("campaign.media_upload")
    assert [e["outcome"] for e in log] == ["ok", "blocked:unsupported_type"]
    assert log[0]["details"]["media_id"] == "***0123" and log[0]["admin"]["id"]
    assert L.TOKEN not in json.dumps(log, default=str)
    assert sender.calls == []
