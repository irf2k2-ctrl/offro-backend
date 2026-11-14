"""WhatsApp Campaign Manager - Phase 2B audience management: merchant retrieval, CSV / manual custom numbers,
normalization, duplicates, merchant matching, eligibility preview. NO sending, NO network. Private mongomock DB.
Run:  python -m pytest test_wa_campaign_audience.py -q -W ignore"""
import io
import json
import pathlib
from datetime import datetime, timedelta

import mongomock
import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.dashboard_auth as DA
import routers.wa_campaigns as RC
import routers.wa_campaign_audience as RA
import services.wa_audience as AU
import services.wa_campaign_audit as AUD
import services.wa_campaign_settings as ST
import services.wa_consent as C
import services.wa_eligibility as E
from test_wa_webhook_consent import _BrokenDb

_DB = mongomock.MongoClient()["wa_audience_test"]
BASE = "/admin/whatsapp-campaigns/audience"
P1, P2, P3, P4 = "919876543210", "919812345678", "919700000003", "919700000004"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in _DB.list_collection_names():
        _DB.drop_collection(name)
    for mod in (C, AUD, ST, DA, AU):
        monkeypatch.setattr(mod, "db", _DB)


app = FastAPI()
app.include_router(RC.router, prefix="/admin")
client = TestClient(app)


def make_user(token, perms=None, role_name="Custom Role", assigned=None):
    rid = _DB.dashboard_roles.insert_one({"role_name": role_name, "permissions": perms or {}}).inserted_id
    doc = {"token": token, "role_id": rid, "full_name": "Asha Admin", "status": "active",
           "last_active_at": datetime.utcnow(), "mobile": "9000000001"}
    if assigned is not None:
        doc["assigned_cities"] = assigned
    _DB.dashboard_users.insert_one(doc)
    return token


def grant(*actions):
    return {RC.MODULE: {a: True for a in actions}}


def H(t):
    return {"Authorization": "Bearer " + t}


@pytest.fixture
def admin():
    return make_user("adm", grant("view", "add", "delete", "edit", "approve"), assigned=["*"])


def merchant(name, phone, status="active", roles=("merchant",)):
    return str(_DB.accounts.insert_one({"name": name, "phone": phone, "status": status, "roles": list(roles)}).inserted_id)


def store(mid, name, city="Pune", area="Kothrud", state="Maharashtra", status="active"):
    return str(_DB.stores.insert_one({"merchant_id": mid, "store_name": name, "city": city, "area": area,
                                      "state": state, "status": status, "category": "Cafe"}).inserted_id)


def start(phone, mid="wamid.S1"):
    return C.record_opt_in(phone, "START", mid, str(int(datetime.utcnow().timestamp()) - 60), whatsapp_id=True)


def stop(phone, mid="wamid.X1"):
    return C.record_opt_out(phone, "STOP", mid, str(int(datetime.utcnow().timestamp()) - 30), whatsapp_id=True)


def admin_consent(phone):
    return C.record_admin_consent(phone, "signup_form", "FORM-1", (datetime.utcnow() - timedelta(days=2)).isoformat() + "Z",
                                  {"id": "a1", "name": "Asha", "role": "Lead"}, True)


def csv_up(text, name="nums.csv", token="adm"):
    return client.post(BASE + "/imports/csv", headers=H(token),
                       files={"file": (name, text if isinstance(text, bytes) else text.encode(), "text/csv")})


def preview(token="adm", **body):
    return client.post(BASE + "/preview", headers=H(token), json=body)


# ───────────────────────── normalization & invalid reasons ─────────────────────────

@pytest.mark.parametrize("raw", ["9876543210", "+91 98765 43210", "919876543210", "09876543210", "0091-98765-43210",
                                 "(+91) 98765.43210", "919876543210.0"])
def test_indian_forms_normalize_to_one_key(raw):
    res = AU.normalize_entries([(1, raw, "")])
    assert [r["phone"] for r in res["recipients"]] == [P1]


def test_international_needs_plus_and_is_kept():
    assert AU.normalize_entries([(1, "+65 8123 4567", "")])["recipients"][0]["phone"] == "6581234567"
    assert AU.normalize_entries([(1, "+1 415 555 2671", "")])["recipients"][0]["phone"] == "14155552671"


@pytest.mark.parametrize("raw,reason", [
    ("", "empty_phone"), ("   ", "empty_phone"), ("9.19876543210E+11", "scientific_notation"),
    ("98765abc10", "invalid_characters"), ("12345", "too_short"), ("+1234567890123456789", "too_long"),
    ("+91 12345 67890", "invalid_indian_mobile"), ("919876543", "invalid_indian_mobile"), ("5876543210", "international_needs_plus"),
    ("+0123456789", "invalid_format"),
])
def test_invalid_numbers_get_a_clear_reason(raw, reason):
    res = AU.normalize_entries([(7, raw, "x")])
    assert res["recipients"] == [] and res["rejected"] == [{"row": 7, "reason": reason}]
    assert reason in AU.REASON_TEXT


def test_duplicates_detected_across_formats_first_row_wins():
    res = AU.normalize_entries([(2, "9876543210", "A"), (3, "+91 98765 43210", "B"), (4, "919812345678", "C"),
                                (5, "98765-43210", "D")])
    assert [r["row"] for r in res["recipients"]] == [2, 4]
    assert res["duplicates"] == [{"row": 3, "of_row": 2}, {"row": 5, "of_row": 2}]


def test_names_are_sanitized():
    assert AU.clean_name("=HYPERLINK(\"x\")") == "HYPERLINK(\"x\")"
    assert AU.clean_name("  A\x00\n  B  ") == "A B"
    assert len(AU.clean_name("x" * 500)) == 100


# ───────────────────────────────── CSV parsing ─────────────────────────────────

def test_csv_header_variants_bom_and_row_numbers():
    p = AU.parse_csv("﻿Mobile Number,Full Name\n9876543210,Asha\n,Nobody\n9812345678,Ravi\n".encode("utf-8"))
    assert p["entries"] == [(2, "9876543210", "Asha"), (3, "", "Nobody"), (4, "9812345678", "Ravi")]
    assert p["headerless"] is False


def test_csv_semicolon_and_tab_delimiters():
    assert len(AU.parse_csv(b"phone;name\n9876543210;A\n9812345678;B\n")["entries"]) == 2
    assert len(AU.parse_csv(b"phone\tname\n9876543210\tA\n")["entries"]) == 1


def test_csv_headerless_file_accepted_only_when_first_cell_is_a_number():
    p = AU.parse_csv(b"9876543210,Asha\n9812345678,Ravi\n")
    assert p["headerless"] and p["entries"][0] == (1, "9876543210", "Asha")
    with pytest.raises(AU.AudienceError) as ei:
        AU.parse_csv(b"foo,bar\n1,2\n")
    assert ei.value.code == "no_phone_column"


def test_csv_consent_columns_are_reported_and_ignored():
    p = AU.parse_csv(b"phone,name,whatsapp_consent,opt_in\n9876543210,A,yes,true\n")
    assert p["ignored_columns"] == ["whatsapp_consent", "opt_in"]
    assert p["entries"] == [(2, "9876543210", "A")]                      # consent values are not even carried


@pytest.mark.parametrize("data,code", [
    pytest.param(b"", "empty_file", id="zero_bytes"),
    pytest.param(b"\n\n,\n", "empty_file", id="blank_rows_only"),
    pytest.param(b"PK\x03\x04\x00\x00binary", "not_csv", id="zip_or_xlsx_binary"),
    pytest.param(b"\xff\xfe\x00bad", "not_csv", id="utf16_nul_bytes"),
    pytest.param(b"phone\n" + b"9" * (AU.MAX_CSV_BYTES + 1), "file_too_large", id="oversized_phone_field"),
])
def test_csv_unusable_files_are_rejected(data, code):
    with pytest.raises(AU.AudienceError) as ei:
        AU.parse_csv(data)
    assert ei.value.code == code


def test_csv_non_utf8_and_row_limit(monkeypatch):
    with pytest.raises(AU.AudienceError) as ei:
        AU.parse_csv("phone\n9876543210 é".encode("latin-1"))
    assert ei.value.code == "bad_encoding"
    monkeypatch.setattr(AU, "MAX_IMPORT_ROWS", 3)
    with pytest.raises(AU.AudienceError) as ei:
        AU.parse_csv(b"phone\n1\n2\n3\n4\n")
    assert ei.value.code == "too_many_rows"


# ───────────────────────────────── merchants ─────────────────────────────────

def seed_merchants():
    a = merchant("Asha Bakery", "9876543210")
    store(a, "Asha Cafe", city="Pune", area="Kothrud", state="Maharashtra")
    store(a, "Asha Cafe 2", city="Pune", area="Baner", state="Maharashtra")        # second store: same merchant
    b = merchant("Ravi Traders", "+91 98123 45678")
    store(b, "Ravi Mart", city="Mumbai", area="Andheri", state="Maharashtra")
    c = merchant("Closed Co", "9700000003", status="inactive")
    store(c, "Closed Store", city="Pune")
    d = merchant("No Store", "9700000004")
    store(d, "Old Store", status="inactive")
    e = merchant("Bad Phone", "12345")
    store(e, "BP Store", city="Delhi", area="Rohini", state="Delhi")
    merchant("Just A User", "9111111111", roles=("user",))
    return a, b, c, d, e


def test_merchant_list_qualifying_only_and_multi_store_is_one_recipient(admin):
    seed_merchants()
    r = client.get(BASE + "/merchants", headers=H(admin))
    assert r.status_code == 200
    names = [m["name"] for m in r.json()["items"]]
    assert names == ["Asha Bakery", "Bad Phone", "Ravi Traders"] or names == ["Asha Bakery", "Ravi Traders"]
    asha = next(m for m in r.json()["items"] if m["name"] == "Asha Bakery")
    assert asha["store_count"] == 2
    assert "Closed Co" not in names and "No Store" not in names and "Just A User" not in names


def test_merchant_list_masks_phone_and_shows_eligibility(admin):
    seed_merchants()
    start(P1)
    items = client.get(BASE + "/merchants", headers=H(admin)).json()["items"]
    asha = next(m for m in items if m["name"] == "Asha Bakery")
    assert asha["phone_masked"] == "***3210"
    assert asha["eligibility"] == {"eligible": True, "reason": "eligible", "consent_source": "customer_start", "opted_out": False}
    ravi = next(m for m in items if m["name"] == "Ravi Traders")
    assert ravi["eligibility"]["reason"] == "unknown_consent" and ravi["eligibility"]["consent_source"] == "none"
    assert P1 not in json.dumps(items)


def test_merchant_filters_city_area_state_and_search(admin):
    seed_merchants()
    def names(**q):
        return [m["name"] for m in client.get(BASE + "/merchants", headers=H(admin), params=q).json()["items"]]
    assert names(city="mumbai") == ["Ravi Traders"]                    # case-insensitive, exact city
    assert names(area="Baner") == ["Asha Bakery"]                      # matches via the merchant's second store
    assert names(state="Maharashtra") == ["Asha Bakery", "Ravi Traders"]
    assert names(city="Pun") == []                                      # exact, not substring
    assert names(q="asha cafe 2") == ["Asha Bakery"]                    # store name
    assert names(q="ravi") == ["Ravi Traders"]                          # merchant name
    assert names(q="98123") == ["Ravi Traders"]                         # phone digits, any stored format
    assert names(q="987") == ["Asha Bakery"] and names(q="98") == [] and names(q="zzz") == []
    assert names(city="Pune", area="Andheri") == []


def test_merchant_list_paging(admin):
    for i in range(7):
        m = merchant("M%d" % i, "98000000%02d" % i)
        store(m, "S%d" % i)
    r = client.get(BASE + "/merchants", headers=H(admin), params={"page": 2, "page_size": 3}).json()
    assert r["total"] == 7 and r["pages"] == 3 and len(r["items"]) == 3 and r["page"] == 2
    assert client.get(BASE + "/merchants", headers=H(admin), params={"page_size": 101}).status_code == 422


def test_filter_options_from_active_stores_only(admin):
    seed_merchants()
    f = client.get(BASE + "/filters", headers=H(admin)).json()
    assert f["cities"] == ["Delhi", "Mumbai", "Pune"] and "Rohini" in f["areas"] and f["states"] == ["Delhi", "Maharashtra"]


def test_city_scoped_admin_only_sees_assigned_cities():
    make_user("scoped", grant("view"), assigned=["Mumbai"])
    seed_merchants()
    items = client.get(BASE + "/merchants", headers=H("scoped")).json()["items"]
    assert [m["name"] for m in items] == ["Ravi Traders"]
    assert client.get(BASE + "/filters", headers=H("scoped")).json()["cities"] == ["Mumbai"]
    make_user("nocity", grant("view"), assigned=[])
    assert client.get(BASE + "/merchants", headers=H("nocity")).json()["items"] == []
    make_user("star", grant("view"), assigned=["*"])
    assert len(client.get(BASE + "/merchants", headers=H("star")).json()["items"]) >= 2


def test_explicit_ids_report_why_a_merchant_is_excluded(admin):
    a, b, c, d, e = seed_merchants()
    ghost = str(ObjectId())
    r = preview(merchants={"ids": [a, c, d, e, ghost, "not-an-id"]}).json()
    why = {x["ref"]: x["exclusion_reason"] for x in r["recipients"]}
    assert why[c] == "merchant_inactive" and why[d] == "merchant_no_active_store"
    assert why[e] == "invalid_number" and why[ghost] == "merchant_not_found" and why["not-an-id"] == "merchant_not_found"
    assert why[a] == "unknown_consent"


def test_explicit_id_outside_admin_city_scope_is_excluded():
    make_user("scoped", grant("view"), assigned=["Mumbai"])
    a, b, *_ = seed_merchants()
    r = preview("scoped", merchants={"ids": [a, b]}).json()
    why = {x["ref"]: x["exclusion_reason"] for x in r["recipients"]}
    assert why[a] == "merchant_out_of_scope" and why[b] == "unknown_consent"


# ───────────────────────── custom numbers: import endpoints ─────────────────────────

def test_manual_import_validates_and_reports(admin):
    r = client.post(BASE + "/imports", headers=H(admin), json={"entries": [
        {"phone": "9876543210", "name": "Asha"}, {"phone": "98765 43210"}, {"phone": "12"}, {"phone": "+65 8123 4567"}]})
    assert r.status_code == 200
    j = r.json()
    assert j["counts"] == {"rows": 4, "valid": 2, "invalid": 1, "duplicates": 1, "registered_merchants": 0}
    assert {p["reason"] for p in j["problems"]} == {"too_short", "duplicate_in_upload"}
    assert "9876543210" not in json.dumps(j)


def test_manual_phones_shortcut_and_bad_bodies(admin):
    assert client.post(BASE + "/imports", headers=H(admin), json={"phones": ["9876543210"]}).json()["counts"]["valid"] == 1
    for bad in ({}, {"entries": []}, {"entries": "x"}, {"entries": [5]}, []):
        assert client.post(BASE + "/imports", headers=H(admin), json=bad).status_code == 400
    assert client.post(BASE + "/imports", headers=H(admin), data="nope").status_code == 400
    big = {"phones": ["9876543210"] * (AU.MAX_MANUAL_ENTRIES + 1)}
    assert client.post(BASE + "/imports", headers=H(admin), json=big).status_code == 400


def test_csv_upload_end_to_end_and_import_detail(admin):
    r = csv_up("phone,name,consent\n9876543210,Asha,yes\nabc,Bad,yes\n9876543210,Dup,yes\n9812345678,Ravi,no\n")
    assert r.status_code == 200
    j = r.json()
    assert j["counts"]["valid"] == 2 and j["counts"]["invalid"] == 1 and j["counts"]["duplicates"] == 1
    assert j["warnings"] == ["consent_columns_ignored"] and j["ignored_columns"] == ["consent"]
    d = client.get(BASE + "/imports/" + j["import_id"], headers=H(admin)).json()
    assert [(p["row"], p["reason"]) for p in d["problems"]] == [(3, "invalid_characters"), (4, "duplicate_in_upload")]
    lst = client.get(BASE + "/imports", headers=H(admin)).json()["items"]
    assert len(lst) == 1 and lst[0]["import_id"] == j["import_id"]


def test_csv_upload_errors_are_clear_400s(admin):
    for body, code in ((b"", "empty_file"), (b"foo,bar\n1,2\n", "no_phone_column"), (b"PK\x03\x04\x00", "not_csv")):
        r = csv_up(body)
        assert r.status_code == 400 and r.json()["detail"]["code"] == code
    assert client.post(BASE + "/imports/csv", headers=H(admin)).status_code == 422


def test_import_stores_no_raw_data_or_consent_values(admin):
    j = csv_up("phone,name,consent\n9876543210,Asha,yes\n+91 xx 123,Weird Raw,yes\n", name="../../evil name.csv").json()
    doc = _DB[AU.IMPORTS].find_one()
    blob = json.dumps(doc, default=str)
    assert "Weird Raw" not in blob and "xx 123" not in blob and "yes" not in blob
    assert doc["filename"] == ".._.._evil name.csv" or "/" not in doc["filename"]
    assert doc["rejected"] == [{"row": 3, "reason": "invalid_characters"}]
    assert set(_DB.list_collection_names()) <= {AU.IMPORTS, "wa_campaign_audit", "dashboard_users", "dashboard_roles"}
    assert j["import_id"] == str(doc["_id"])


def test_import_and_delete_are_audited_without_numbers(admin):
    j = csv_up("phone\n9876543210\n").json()
    assert client.delete(BASE + "/imports/" + j["import_id"], headers=H(admin)).status_code == 200
    acts = [(a["action"], a["outcome"]) for a in _DB.wa_campaign_audit.find()]
    assert ("audience.import", "attempt") in acts and ("audience.import", "stored") in acts
    assert ("audience.import_delete", "deleted") in acts
    assert "9876543210" not in json.dumps(list(_DB.wa_campaign_audit.find()), default=str)
    assert _DB[AU.IMPORTS].count_documents({}) == 0
    assert client.delete(BASE + "/imports/" + j["import_id"], headers=H(admin)).status_code == 404
    assert client.delete(BASE + "/imports/xyz", headers=H(admin)).status_code == 404


def test_import_refused_when_audit_cannot_be_written(admin, monkeypatch):
    monkeypatch.setattr(AUD, "db", _BrokenDb(_DB, {AUD.AUDIT: {"insert_one"}}))
    r = csv_up("phone\n9876543210\n")
    assert r.status_code == 503 and _DB[AU.IMPORTS].count_documents({}) == 0


def test_import_storage_failure_is_503_without_leaking(admin, monkeypatch):
    monkeypatch.setattr(AU, "db", _BrokenDb(_DB, {AU.IMPORTS: {"insert_one"}}))
    r = csv_up("phone\n9876543210\n")
    assert r.status_code == 503 and "9198" not in r.text and "EAAsecret" not in r.text


# ───────────────────────────── preview & eligibility ─────────────────────────────

def test_preview_requires_a_selection_and_valid_shapes(admin):
    assert preview().status_code == 400
    assert preview(imports="x").status_code == 400
    assert preview(imports=[str(ObjectId())]).status_code == 404
    assert preview(merchants={"filter": "x"}).status_code == 400
    assert preview(merchants={"ids": [1]}).status_code == 400
    assert preview(merchants={"ids": []}, status="weird").status_code == 400
    assert preview(merchants={"ids": []}, page="a").status_code == 400


def test_preview_eligibility_matrix_and_privacy(admin):
    ids = [merchant("M%d" % i, p) for i, p in enumerate((P1, P2, P3, P4))]
    for m in ids:
        store(m, "S")
    start(P1)                                   # customer START -> eligible
    admin_consent(P2)                           # admin evidence -> eligible
    stop(P3)                                    # opted out
    # P4 unknown
    r = preview(merchants={"ids": ids}).json()
    by = {x["ref"]: x for x in r["recipients"]}
    assert by[ids[0]]["eligible"] and by[ids[0]]["consent_source"] == "customer_start"
    assert by[ids[1]]["eligible"] and by[ids[1]]["consent_source"] == "admin_recorded"
    assert by[ids[2]]["exclusion_reason"] == "opted_out" and by[ids[2]]["opted_out"] is True
    assert by[ids[3]]["exclusion_reason"] == "unknown_consent" and by[ids[3]]["consent_source"] == "none"
    s = r["summary"]
    assert (s["total"], s["eligible"], s["excluded"]) == (4, 2, 2)
    assert s["by_reason"] == {"opted_out": 1, "unknown_consent": 1} and s["consent_check_ok"] is True
    assert s["by_source"]["merchant"] == {"total": 4, "eligible": 2}
    for p in (P1, P2, P3, P4):
        assert p not in json.dumps(r)


def test_opt_out_beats_admin_consent_and_start_in_preview(admin):
    m = merchant("M", P1)
    store(m, "S")
    admin_consent(P1)
    stop(P1)
    r = preview(merchants={"ids": [m]}).json()
    assert r["recipients"][0]["exclusion_reason"] == "opted_out" and r["summary"]["eligible"] == 0


def test_uploaded_consent_column_never_makes_anyone_eligible(admin):
    j = csv_up("phone,name,consent,opt_in\n9876543210,Asha,yes,true\n9812345678,Ravi,YES,1\n").json()
    r = preview(imports=[j["import_id"]]).json()
    assert r["summary"]["eligible"] == 0 and r["summary"]["by_reason"] == {"unknown_consent": 2}
    assert all(x["consent_source"] == "none" for x in r["recipients"])


def test_custom_number_with_recorded_consent_or_stop(admin):
    start(P1)
    stop(P2)
    j = csv_up("phone\n9876543210\n9812345678\n9700000003\n").json()
    rec = {x["phone_masked"]: x for x in preview(imports=[j["import_id"]]).json()["recipients"]}
    assert rec["***3210"]["eligible"] and rec["***5678"]["exclusion_reason"] == "opted_out"
    assert rec["***0003"]["exclusion_reason"] == "unknown_consent"


def test_custom_number_duplicates_a_selected_merchant(admin):
    m = merchant("Asha", "9876543210")
    store(m, "S")
    start(P1)
    j = csv_up("phone,name\n+91 98765 43210,Same person\n9812345678,Other\n").json()
    assert j["counts"]["registered_merchants"] == 1
    r = preview(merchants={"ids": [m]}, imports=[j["import_id"]]).json()
    assert r["summary"]["total"] == 3 and r["summary"]["eligible"] == 1
    dup = next(x for x in r["recipients"] if x["source"] == "custom" and x["duplicate_status"] == "duplicate_of_merchant")
    assert not dup["eligible"] and dup["exclusion_reason"] == "duplicate"
    assert dup["registered_merchant"]["merchant_id"] == m
    assert r["summary"]["by_reason"]["duplicate"] == 1


def test_registered_merchant_not_selected_is_flagged_not_excluded(admin):
    m = merchant("Asha", "9876543210")
    store(m, "S")
    start(P1)
    j = csv_up("phone\n9876543210\n").json()
    row = preview(imports=[j["import_id"]]).json()["recipients"][0]
    assert row["eligible"] and row["registered_merchant"] == {"merchant_id": m, "name": "Asha"}


def test_duplicates_across_two_imports_and_two_merchant_accounts(admin):
    j1 = csv_up("phone\n9876543210\n").json()
    j2 = csv_up("phone\n+919876543210\n9812345678\n").json()
    r = preview(imports=[j1["import_id"], j2["import_id"], j1["import_id"]]).json()
    assert r["summary"]["total"] == 3                                   # the repeated import id is read once
    assert sorted(x["duplicate_status"] for x in r["recipients"]) == ["duplicate_in_upload", "unique", "unique"]
    assert r["summary"]["by_reason"].get("duplicate") == 1
    m1, m2 = merchant("A", "9812300000"), merchant("B", "+91 98123 00000")
    store(m1, "S"); store(m2, "S")
    r2 = preview(merchants={"ids": [m1, m2]}).json()
    assert sorted(x["duplicate_status"] for x in r2["recipients"]) == ["duplicate_recipient", "unique"]


def test_group_selection_by_filter_and_union_with_ids(admin):
    a, b, *_ = seed_merchants()
    r = preview(merchants={"filter": {"city": "Mumbai"}}).json()
    assert r["summary"]["total"] == 1 and r["recipients"][0]["ref"] == b
    r = preview(merchants={"filter": {"state": "Maharashtra"}, "ids": [a]}).json()
    assert r["summary"]["total"] == 2                                   # a is not counted twice
    r = preview(merchants={"filter": {}}).json()
    assert r["summary"]["total"] >= 2


def test_preview_status_filter_and_paging(admin):
    ms = []
    for i in range(5):
        m = merchant("M%d" % i, "98000000%02d" % i)
        store(m, "S")
        ms.append(m)
    start("919800000001"); start("919800000003")
    r = preview(merchants={"ids": ms}, status="eligible").json()
    assert r["filtered_total"] == 2 and r["summary"]["total"] == 5
    r = preview(merchants={"ids": ms}, status="excluded", page=2, page_size=2).json()
    assert r["filtered_total"] == 3 and len(r["recipients"]) == 1 and r["pages"] == 2


def test_preview_fails_closed_when_consent_lookup_fails(admin, monkeypatch):
    m = merchant("M", P1)
    store(m, "S")
    start(P1)
    monkeypatch.setattr(C, "db", _BrokenDb(_DB, {C.CONTACTS: {"find"}}))
    r = preview(merchants={"ids": [m]})
    assert r.status_code == 200
    j = r.json()
    assert j["summary"]["eligible"] == 0 and j["recipients"][0]["exclusion_reason"] == "consent_lookup_failed"
    assert j["summary"]["consent_check_ok"] is False and "9198" not in r.text and "EAAsecret" not in r.text


def test_merchant_database_failure_is_503_not_a_partial_audience(admin, monkeypatch):
    seed_merchants()
    monkeypatch.setattr(AU, "db", _BrokenDb(_DB, {"accounts": {"find"}}))
    assert preview(merchants={"filter": {}}).status_code == 503
    assert client.get(BASE + "/merchants", headers=H(admin)).status_code == 503
    assert preview(merchants={"ids": [str(ObjectId())]}).status_code == 503


def test_audience_size_cap(admin, monkeypatch):
    monkeypatch.setattr(AU, "MAX_AUDIENCE", 2)
    j = csv_up("phone\n9876543210\n9812345678\n9700000003\n").json()
    r = preview(imports=[j["import_id"]])
    assert r.status_code == 400 and r.json()["detail"]["code"] == "audience_too_large"


# ───────────────────────────── permissions ─────────────────────────────

def test_every_route_needs_the_campaign_module():
    make_user("none", {"Merchants": {"view": True, "add": True, "delete": True}})
    make_user("viewer", grant("view"), assigned=["*"])
    iid = str(ObjectId())
    calls = [("get", "/filters"), ("get", "/merchants"), ("get", "/imports"), ("get", "/imports/" + iid),
             ("post", "/preview"), ("post", "/imports"), ("delete", "/imports/" + iid)]
    for method, path in calls:
        assert getattr(client, method)(BASE + path).status_code in (401, 403)
        r = getattr(client, method)(BASE + path, headers=H("none"), json={"phones": ["9876543210"]}) if method == "post" \
            else getattr(client, method)(BASE + path, headers=H("none"))
        assert r.status_code == 403, (method, path)
    assert client.post(BASE + "/imports", headers=H("viewer"), json={"phones": ["9876543210"]}).status_code == 403
    assert client.post(BASE + "/imports/csv", headers=H("viewer"), files={"file": ("a.csv", b"phone\n1")}).status_code == 403
    assert client.delete(BASE + "/imports/" + iid, headers=H("viewer")).status_code == 403
    assert client.get(BASE + "/merchants", headers=H("viewer")).status_code == 200
    assert preview("viewer", merchants={"ids": []}).status_code == 200


def test_seeded_admin_role_has_no_audience_access():
    DA.seed_rbac()
    role = _DB.dashboard_roles.find_one({"role_name": "Admin"})
    if role:
        assert not any(role["permissions"].get(RC.MODULE, {}).values())


# ───────────────────────────── safety boundaries ─────────────────────────────

def test_audience_code_has_no_way_to_send():
    for path in ("services/wa_audience.py", "routers/wa_campaign_audience.py"):
        src = pathlib.Path(path).read_text()
        for forbidden in ("import requests", "import httpx", "services.whatsapp", "services import whatsapp", "graph.facebook",
                          "send_template", "send_message", "BackgroundTasks", "asyncio.create_task", "threading", "schedule"):
            assert forbidden not in src, (path, forbidden)


def test_audience_routes_are_namespaced_and_read_or_store_only():
    paths = {(m, r.path) for r in RA.router.routes for m in r.methods}
    assert paths == {("GET", "/audience/filters"), ("GET", "/audience/merchants"), ("POST", "/audience/imports"),
                     ("POST", "/audience/imports/csv"), ("GET", "/audience/imports"), ("GET", "/audience/imports/{import_id}"),
                     ("DELETE", "/audience/imports/{import_id}"), ("POST", "/audience/preview")}
    full = {r.path for r in app.routes if "audience" in r.path}
    assert full and all(p.startswith("/admin/whatsapp-campaigns/audience/") for p in full)


def test_phase_2a_eligibility_service_is_the_only_consent_decision():
    src = pathlib.Path("services/wa_audience.py").read_text()
    assert "E.check_many" in src and "wa_contacts" not in src and "status\") == \"opted" not in src
