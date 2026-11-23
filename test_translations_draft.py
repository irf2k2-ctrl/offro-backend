"""Stage 1: (2) the language-independent Modified/Published rule and (1) Download Draft / Import (XLSX).
Run:  python -m pytest test_translations_draft.py -q -W ignore
"""
import builtins
import contextlib
import io
import json
import os
import re
import zipfile

import pytest
from openpyxl import Workbook, load_workbook

from services import translation_draft as D
from services import translations as T
from translation_test_support import (DB, add_ar, audit_actions, client as api, editor, publish, put, setup_env, super_admin,
                                      sync_fixture_keys, viewer, fake_super)

APP_KEYS = ["loginTitle", "otpSent", "resendOtp", "smsInstead", "supportLine"]     # editable app-track keys in the fixture
LANGS = [("ar", "Arabic", "العربية", "rtl"), ("hi", "Hindi", "हिन्दी", "ltr"), ("fr", "French", "Français", "ltr")]


@pytest.fixture(autouse=True)
def staging(monkeypatch):
    setup_env(monkeypatch, "staging")
    sync_fixture_keys()


def mk_lang(sa, code):
    _, en, nat, d = [l for l in LANGS if l[0] == code][0]
    r = api.post("/admin/translations/languages", headers=sa, json={"code": code, "name_en": en, "name_native": nat, "direction": d})
    assert r.status_code == 200, r.text


def status_of(sa, lang, key, track="app"):
    rows = api.get("/admin/translations/strings", headers=sa, params={"lang": lang, "track": track, "q": key}).json()["rows"]
    return [r for r in rows if r["key"] == key][0]["status"]


def download(headers, lang="ar", track="app"):
    r = api.get("/admin/translations/draft", headers=headers, params={"lang": lang, "track": track})
    assert r.status_code == 200, r.text
    return r.content


def sheet_rows(data):
    ws = load_workbook(io.BytesIO(data))["Translations"]
    return [[c.value for c in row] for row in ws.iter_rows(min_row=2, max_col=5)]


@contextlib.contextmanager
def _no_newline_translation():
    r"""Make openpyxl write the workbook byte-for-byte the same on every OS.

    openpyxl's XML writer (et_xmlfile) opens the sheet file in TEXT mode, so on Windows every "\n" is written as "\r\n": a cell holding
    "\r\n" is stored as "\r\r\n" and reads back as TWO line breaks. Passing newline="" turns that translation off, so the file contains
    exactly the characters the test put in the cell. Linux/macOS output is unchanged (they never translated)."""
    try:
        from et_xmlfile import incremental_tree as it
    except ImportError:                                   # a different openpyxl layout: nothing to adjust
        yield
        return
    real_open, had_own = builtins.open, "open" in vars(it)
    previous = vars(it).get("open")

    def _open(file, mode="r", *a, **k):
        if "w" in mode and "b" not in mode:
            k.setdefault("newline", "")
        return real_open(file, mode, *a, **k)

    it.open = _open
    try:
        yield
    finally:
        if had_own:
            it.open = previous
        else:
            del it.open


def edit_file(data, changes=None, *, english=None, drop=(), add=(), blank=(), set_type=None):
    """Return a modified copy of a downloaded draft. changes: key -> new translation. add: extra [key, feature, english, translation] rows."""
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Translations"]
    for row in list(ws.iter_rows(min_row=2)):
        key = row[0].value
        if key in drop:
            ws.delete_rows(row[0].row)
    for row in ws.iter_rows(min_row=2):
        key = row[0].value
        if changes and key in changes:
            row[3].value = changes[key]
        if key in blank:
            row[3].value = None
        if english and key in english:
            row[2].value = english[key]
    for extra in add:
        ws.append(list(extra))
    if set_type:
        for ref, v in set_type.items():
            ws[ref] = v
    out = io.BytesIO()
    with _no_newline_translation():
        wb.save(out)
    return out.getvalue()


def edit_info(data, label, value):
    """Copy of a draft whose Info sheet row [label] has a new value (None removes the value)."""
    wb = load_workbook(io.BytesIO(data)); ws = wb["Info"]
    for row in ws.iter_rows(min_row=1, max_col=2):
        if row[0].value == label:
            row[1].value = value
    out = io.BytesIO(); wb.save(out)
    return out.getvalue()


def do_import(headers, data, lang="ar", track="app", apply=False, name="draft.xlsx"):
    return api.post("/admin/translations/import", headers=headers, files={"file": (name, data, D.XLSX_MIME)},
                    data={"lang": lang, "track": track, "apply": "1" if apply else ""})


def values(lang="ar"):
    return {v["key"]: v for v in DB[T.VALUES].find({"lang": lang})}


# ═══ Item 2: Modified / Published against the PUBLISHED baseline, for every language ═════════════════════════════════════════
@pytest.mark.parametrize("code", [l[0] for l in LANGS])
def test_status_follows_the_published_baseline_exactly(code):
    sa = super_admin(); mk_lang(sa, code)
    assert put(sa, "loginTitle", "Welcome", code).json()["status"] == "modified"        # nothing published yet
    assert publish(sa, code).status_code == 200
    seq = [("Welcome back", "modified"), ("Hello", "modified"), ("Welcome", "published"), ("Welcome again", "modified"), ("Welcome", "published")]
    for text, expected in seq:
        r = put(sa, "loginTitle", text, code)
        assert r.json()["status"] == expected, (code, text)
        assert status_of(sa, code, "loginTitle") == expected


@pytest.mark.parametrize("code", ["ar", "hi"])
def test_restoring_published_text_clears_the_languages_draft_count(code):
    sa = super_admin(); mk_lang(sa, code)
    put(sa, "loginTitle", "A", code); publish(sa, code)
    put(sa, "loginTitle", "B", code)
    assert T.language_status(code)["app"]["draft_changes"] == 1
    put(sa, "loginTitle", "A", code)
    assert T.language_status(code)["app"]["draft_changes"] == 0 and T.diff(T.published_strings("app", code), T.working_strings("app", code)) == {"added": [], "changed": [], "removed": []}


def test_comparison_is_with_the_published_text_not_the_previous_draft():
    sa = super_admin(); mk_lang(sa, "ar")
    put(sa, "loginTitle", "P", "ar"); publish(sa, "ar")
    put(sa, "loginTitle", "X", "ar")                                    # draft X ...
    assert put(sa, "loginTitle", "X", "ar").json()["status"] == "modified"   # ... saving X again is still modified (X differs from P)
    assert put(sa, "loginTitle", "P", "ar").json()["status"] == "published"
    assert put(sa, "loginTitle", "Y", "ar").json()["status"] == "modified"


def test_a_new_publish_moves_the_baseline():
    sa = super_admin(); mk_lang(sa, "ar")
    put(sa, "loginTitle", "One", "ar"); publish(sa, "ar")
    put(sa, "loginTitle", "Two", "ar"); assert publish(sa, "ar").status_code == 200
    assert put(sa, "loginTitle", "One", "ar").json()["status"] == "modified"      # the old baseline no longer counts
    assert put(sa, "loginTitle", "Two", "ar").json()["status"] == "published"


def test_value_never_published_is_modified_even_if_other_keys_are():
    sa = super_admin(); mk_lang(sa, "ar")
    put(sa, "loginTitle", "A", "ar"); publish(sa, "ar")
    assert put(sa, "resendOtp", "B", "ar").json()["status"] == "modified"


def test_list_status_is_derived_from_the_baseline_not_from_a_stale_flag():
    sa = super_admin(); mk_lang(sa, "hi")
    put(sa, "loginTitle", "A", "hi"); publish(sa, "hi")
    DB[T.VALUES].update_one({"lang": "hi", "key": "loginTitle"}, {"$set": {"status": "modified"}})     # stale flag, value == published
    assert status_of(sa, "hi", "loginTitle") == "published"
    DB[T.VALUES].update_one({"lang": "hi", "key": "loginTitle"}, {"$set": {"value": "Z", "status": "published"}})
    assert status_of(sa, "hi", "loginTitle") == "modified"
    rows = api.get("/admin/translations/strings", headers=sa, params={"lang": "hi", "filter": "modified"}).json()["rows"]
    assert [r["key"] for r in rows] == ["loginTitle"]


def test_languages_do_not_affect_each_other():
    sa = super_admin(); mk_lang(sa, "ar"); mk_lang(sa, "hi")
    put(sa, "loginTitle", "Same", "ar"); publish(sa, "ar")
    assert put(sa, "loginTitle", "Same", "hi").json()["status"] == "modified"       # hi has no published baseline of its own
    assert status_of(sa, "ar", "loginTitle") == "published"


def test_legal_track_uses_the_same_rule():
    sa = super_admin(); mk_lang(sa, "hi")
    put(sa, "privacyIntro", "L1", "hi"); publish(sa, "hi", "legal")
    assert put(sa, "privacyIntro", "L2", "hi").json()["status"] == "modified"
    assert put(sa, "privacyIntro", "L1", "hi").json()["status"] == "published"


def test_rule_is_common_code_with_no_language_special_case():
    import inspect
    src = inspect.getsource(T.effective_status) + inspect.getsource(T.set_value)
    assert not re.search(r"""["']ar["']|arabic|\blang\s*==""", src, re.I)
    assert T.effective_status({"k": "v"}, "k", "v") == "published" and T.effective_status({"k": "v"}, "k", "w") == "modified" and T.effective_status({}, "k", "v") == "modified"


def test_english_stays_the_read_only_reference():
    sa = super_admin()
    assert put(sa, "loginTitle", "x", "en").status_code == 404
    assert DB[T.VALUES].count_documents({"lang": "en"}) == 0


def test_publishing_safeguards_are_unchanged_by_the_rule():
    sa = super_admin(); mk_lang(sa, "ar")
    put(sa, "loginTitle", "A", "ar")
    r = publish(sa, "ar"); assert r.status_code == 200
    v = DB[T.VERSIONS].find_one({"lang": "ar"})
    assert v["hash"] == T.hash_strings(v["strings"]) and v["version"] == 1
    assert publish(sa, "ar").status_code == 409                                      # nothing to publish
    put(sa, "loginTitle", "B", "ar"); put(sa, "loginTitle", "A", "ar")
    assert publish(sa, "ar").status_code == 409                                      # restored: no phantom change to publish
    assert DB[T.VERSIONS].count_documents({"lang": "ar"}) == 1
    assert "edit" in audit_actions("ok") and "publish" in audit_actions("ok")


# ═══ Item 1: Download Draft ══════════════════════════════════════════════════════════════════════════════════════════════════
def test_draft_has_key_feature_english_translation_and_status():
    sa = super_admin(); mk_lang(sa, "ar")
    put(sa, "loginTitle", "مرحبا بك في عرض", "ar"); put(sa, "resendOtp", "إعادة إرسال", "ar")
    data = download(sa)
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Translations", "Info"]
    ws = wb["Translations"]
    assert [c.value for c in ws[1]][0].startswith("Key") and "English" in ws["C1"].value and "Arabic" in ws["D1"].value
    rows = {r[0]: r for r in sheet_rows(data)}
    assert sorted(rows) == sorted(APP_KEYS)                                          # all editable app keys; no plural, no legal
    assert rows["loginTitle"][1:] == ["login", "Welcome to OffrO", "مرحبا بك في عرض", "modified"]
    assert rows["smsInstead"][3] in ("", None) and rows["smsInstead"][4] == "missing"          # empty translations stay empty and editable
    info = {a.value: b.value for a, b in wb["Info"].iter_rows(min_col=1, max_col=2)}
    assert info["File"] == D.MARKER and info["Language"] == "ar" and info["Track"] == "app" and info["Environment"] == "staging"


def test_draft_is_stable_identifier_based_and_deterministic():
    sa = super_admin(); mk_lang(sa, "ar")
    a, b = sheet_rows(download(sa)), sheet_rows(download(sa))
    assert a == b and [r[0] for r in a] == sorted(APP_KEYS)


def test_draft_translation_column_is_unlocked_and_the_rest_locked():
    sa = super_admin(); mk_lang(sa, "ar")
    ws = load_workbook(io.BytesIO(download(sa)))["Translations"]
    assert ws.protection.sheet is True
    assert ws["D2"].protection.locked is False and ws["A2"].protection.locked and ws["C2"].protection.locked and ws["E2"].protection.locked


def test_formula_looking_text_is_exported_as_plain_text():
    sa = super_admin(); mk_lang(sa, "ar")
    assert put(sa, "resendOtp", "=مرحبا", "ar").status_code == 200
    data = download(sa)
    wb = load_workbook(io.BytesIO(data))
    cell = [r[3] for r in wb["Translations"].iter_rows(min_row=2) if r[0].value == "resendOtp"][0]
    assert cell.data_type == "s" and cell.value == "=مرحبا"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert b"<f>" not in z.read("xl/worksheets/sheet1.xml")


def test_legal_draft_contains_only_legal_keys():
    sa = super_admin(); mk_lang(sa, "hi")
    keys = [r[0] for r in sheet_rows(download(sa, "hi", "legal"))]
    assert sorted(keys) == ["privacyIntro", "whatsappConsent"]


@pytest.mark.parametrize("code", ["hi", "fr"])
def test_draft_works_for_any_language(code):
    sa = super_admin(); mk_lang(sa, code)
    put(sa, "loginTitle", "Bonjour", code)
    rows = {r[0]: r for r in sheet_rows(download(sa, code))}
    assert rows["loginTitle"][3] == "Bonjour"
    assert load_workbook(io.BytesIO(download(sa, code)))["Translations"]["D1"].value.startswith({"hi": "Hindi", "fr": "French"}[code])


def test_draft_permissions_and_errors():
    sa = super_admin(); mk_lang(sa, "ar")
    assert api.get("/admin/translations/draft", params={"lang": "ar"}).status_code in (401, 403)
    assert api.get("/admin/translations/draft", headers=viewer(), params={"lang": "ar"}).status_code == 200
    assert api.get("/admin/translations/draft", headers=sa, params={"lang": "zz"}).status_code == 404
    assert api.get("/admin/translations/draft", headers=sa, params={"lang": "ar", "track": "x"}).status_code in (400, 404, 422)
    assert api.get("/admin/translations/draft", headers=sa, params={"lang": "en"}).status_code == 404       # English is not a translation target
    r = api.get("/admin/translations/draft", headers=sa, params={"lang": "ar"})
    assert r.headers["content-type"].startswith(D.XLSX_MIME) and "attachment" in r.headers["content-disposition"] and ".xlsx" in r.headers["content-disposition"]
    assert "export_draft" in audit_actions("ok")


def test_download_changes_nothing():
    sa = super_admin(); mk_lang(sa, "ar"); put(sa, "loginTitle", "A", "ar")
    before = (DB[T.VALUES].count_documents({}), DB[T.VERSIONS].count_documents({}))
    download(sa)
    assert (DB[T.VALUES].count_documents({}), DB[T.VERSIONS].count_documents({})) == before


# ═══ Item 1: Import ══════════════════════════════════════════════════════════════════════════════════════════════════════════
def base_setup(code="ar"):
    sa = super_admin(); mk_lang(sa, code)
    put(sa, "loginTitle", "Welcome", code); put(sa, "resendOtp", "Resend", code)
    assert publish(sa, code).status_code == 200
    return sa


def test_round_trip_of_an_untouched_file_changes_nothing():
    sa = base_setup()
    before = values()
    r = do_import(sa, download(sa), apply=True).json()
    assert (r["total"], r["updated"], r["unchanged"], r["invalid"], r["missing"]) == (5, 0, 5, 0, 0)
    assert values() == before and r["published"] is False


def test_only_genuine_changes_become_modified_and_the_rest_keep_their_status():
    sa = base_setup()
    put(sa, "smsInstead", "Draft only", "ar")                      # a Modified draft that the file does not touch
    data = edit_file(download(sa), {"loginTitle": "Welcome back", "resendOtp": "Resend", "smsInstead": "Draft only", "supportLine": "للدعم support@theoffro.com"})
    r = do_import(sa, data, apply=True).json()
    assert (r["total"], r["updated"], r["unchanged"], r["invalid"], r["missing"]) == (5, 2, 3, 0, 0)
    assert r["updated_keys"] == ["loginTitle", "supportLine"]
    assert status_of(sa, "ar", "loginTitle") == "modified" and status_of(sa, "ar", "supportLine") == "modified"
    assert status_of(sa, "ar", "resendOtp") == "published"         # unchanged keeps Published
    assert status_of(sa, "ar", "smsInstead") == "modified"         # unchanged keeps Modified
    assert values()["loginTitle"]["source"] == "import"


def test_importing_the_published_wording_restores_published():
    sa = base_setup()
    put(sa, "loginTitle", "Something else", "ar")
    assert status_of(sa, "ar", "loginTitle") == "modified"
    r = do_import(sa, edit_file(download(sa), {"loginTitle": "Welcome"}), apply=True).json()
    assert r["updated"] == 1 and status_of(sa, "ar", "loginTitle") == "published"


def test_import_never_publishes_and_keeps_version_and_hash_safeguards():
    sa = base_setup()
    v0 = list(DB[T.VERSIONS].find({"lang": "ar"}))
    live_before = api.get("/translations/ar", params={"track": "app"})
    do_import(sa, edit_file(download(sa), {"loginTitle": "Welcome back"}), apply=True)
    assert list(DB[T.VERSIONS].find({"lang": "ar"})) == v0                                    # no version created
    assert api.get("/translations/ar", params={"track": "app"}).json() == live_before.json()  # customers still get the published text
    assert T.language_status("ar")["app"]["draft_changes"] == 1
    assert publish(sa, "ar").status_code == 200                                                # the normal workflow publishes it
    v = DB[T.VERSIONS].find_one({"lang": "ar", "version": 2})
    assert v["strings"]["loginTitle"] == "Welcome back" and v["hash"] == T.hash_strings(v["strings"])


def test_check_only_writes_nothing_and_apply_writes():
    sa = base_setup()
    data = edit_file(download(sa), {"loginTitle": "Welcome back"})
    r = do_import(sa, data, apply=False).json()
    assert r["applied"] is False and r["updated"] == 1 and values()["loginTitle"]["value"] == "Welcome"
    assert "import_draft" not in audit_actions()
    r = do_import(sa, data, apply=True).json()
    assert r["applied"] is True and values()["loginTitle"]["value"] == "Welcome back"
    assert "import_draft" in audit_actions("started") and "import_draft" in audit_actions("ok")
    e = DB.translation_audit.find_one({"action": "import_draft", "outcome": "ok"})
    assert e["details"] == {"total": 5, "updated": 1, "unchanged": 4, "invalid": 0, "missing": 0}
    assert "Welcome back" not in json.dumps(e, default=str)                                    # no translation text in the audit log


def test_english_column_is_ignored_and_cannot_be_edited_through_import():
    sa = base_setup()
    before = DB[T.KEYS].find_one({"key": "loginTitle"})["english"]
    r = do_import(sa, edit_file(download(sa), english={"loginTitle": "HACKED ENGLISH"}), apply=True).json()
    assert r["english_edits_ignored"] == 1 and r["updated"] == 0
    assert DB[T.KEYS].find_one({"key": "loginTitle"})["english"] == before
    assert api.get("/admin/translations/strings", headers=sa, params={"lang": "ar", "q": "loginTitle"}).json()["rows"][0]["english"] == before


def test_invalid_and_duplicate_rows_are_rejected_safely_and_do_not_block_valid_ones():
    sa = base_setup()
    long = "x" * 80
    data = edit_file(download(sa), {"loginTitle": long, "otpSent": "no placeholder here", "resendOtp": "OK value"},
                     add=[["noSuchKey", "x", "x", "t"], ["visitsCount", "w", "plural", "t"], ["privacyIntro", "legal", "x", "wrong track"],
                          ["smsInstead", "otp", "x", "dup one"], ["smsInstead", "otp", "x", "dup two"], [None, "x", "x", "no key"]])
    r = do_import(sa, data, apply=True).json()
    codes = {(i["key"], i["code"]) for i in r["invalid_rows"]}
    assert ("loginTitle", "too_long") in codes and ("otpSent", "placeholder_mismatch") in codes
    assert ("noSuchKey", "unknown_key") in codes and ("visitsCount", "plural_read_only") in codes and ("privacyIntro", "wrong_track") in codes
    assert ("smsInstead", "duplicate_key") in codes and ("", "missing_key") in codes
    assert [i["code"] for i in r["invalid_rows"]].count("duplicate_key") == 3                    # the exported row + both added rows: ALL occurrences are rejected
    assert r["updated"] == 1 and values()["resendOtp"]["value"] == "OK value"                    # the valid row was still applied
    assert "smsInstead" not in values() and values()["loginTitle"]["value"] == "Welcome"
    assert r["total"] == r["updated"] + r["unchanged"] + r["invalid"]


def test_missing_records_are_reported_and_not_deleted():
    sa = base_setup()
    r = do_import(sa, edit_file(download(sa), drop=("otpSent", "supportLine")), apply=True).json()
    assert r["missing"] == 2 and r["missing_keys"] == ["otpSent", "supportLine"] and r["total"] == 3
    assert values()["resendOtp"]["value"] == "Resend"


def test_blank_cell_never_deletes_an_existing_translation():
    sa = base_setup()
    r = do_import(sa, edit_file(download(sa), blank=("loginTitle", "smsInstead")), apply=True).json()
    assert r["empty_cells"] == 4 and r["updated"] == 0                                           # loginTitle (blanked) + the 3 keys that never had text and values()["loginTitle"]["value"] == "Welcome" and "smsInstead" not in values()


def test_windows_line_breaks_are_normalised():
    sa = base_setup()
    do_import(sa, edit_file(download(sa), {"supportLine": "سطر\r\nsupport@theoffro.com"}), apply=True)
    assert values()["supportLine"]["value"] == "سطر\nsupport@theoffro.com"


def test_baseline_changed_since_download_is_flagged_and_compared_with_current():
    sa = base_setup()
    data = download(sa)
    put(sa, "loginTitle", "Welcome 2", "ar"); publish(sa, "ar")
    r = do_import(sa, edit_file(data, {"loginTitle": "Welcome"}), apply=True).json()
    assert r["baseline_changed"] is True and status_of(sa, "ar", "loginTitle") == "modified"   # "Welcome" differs from the NEW baseline


@pytest.mark.parametrize("code", ["hi", "fr"])
def test_import_works_for_any_language(code):
    sa = base_setup(code)
    r = do_import(sa, edit_file(download(sa, code), {"loginTitle": "Salut"}), lang=code, apply=True).json()
    assert r["updated"] == 1 and status_of(sa, code, "loginTitle") == "modified"
    assert DB[T.VALUES].count_documents({"lang": "ar"}) == 0                                   # other languages untouched


def test_legal_track_import():
    sa = super_admin(); mk_lang(sa, "hi")
    r = do_import(sa, edit_file(download(sa, "hi", "legal"), {"privacyIntro": "नीति"}), "hi", "legal", apply=True).json()
    assert r["updated"] == 1 and values("hi")["privacyIntro"]["track"] == "legal"


def test_wrong_language_or_track_file_is_rejected_before_any_write():
    sa = base_setup(); mk_lang(sa, "hi")
    data = edit_file(download(sa, "ar"), {"loginTitle": "X"})
    r = do_import(sa, data, lang="hi", apply=True)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "language_mismatch" and DB[T.VALUES].count_documents({"lang": "hi"}) == 0
    r = do_import(sa, data, lang="ar", track="legal", apply=True)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "track_mismatch"
    assert values()["loginTitle"]["value"] == "Welcome"


def _zip_with(extra):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<x/>"); z.writestr("xl/workbook.xml", "<x/>")
        for n, c in extra.items():
            z.writestr(n, c)
    return buf.getvalue()


def _no_info_sheet():
    wb = Workbook(); wb.active.title = "Translations"
    out = io.BytesIO(); wb.save(out); return out.getvalue()


@pytest.mark.parametrize("label,blob,name,code", [
    ("not a zip", b"just some text, not excel", "draft.xlsx", "not_xlsx"),
    ("csv renamed", b"key,en,ar\nloginTitle,a,b\n", "draft.xlsx", "not_xlsx"),
    ("wrong extension", None, "draft.csv", "not_xlsx"),
    ("empty", b"", "draft.xlsx", "empty_file"),
    ("zip without workbook", _zip_with({}) [:0] or b"PK\x03\x04junk", "draft.xlsx", "not_xlsx"),
    ("macros", _zip_with({"xl/vbaProject.bin": "x"}), "draft.xlsx", "macros_not_allowed"),
    # explicit ids: without them pytest puts the raw bytes of these two payloads into the test name (a 2 MB name for "too large"),
    # which Windows cannot hold in PYTEST_CURRENT_TEST (limit 32767 characters). Same data, same assertions.
    pytest.param("no Info sheet", _no_info_sheet(), "draft.xlsx", "missing_sheet", id="no-info-sheet"),
    pytest.param("too large", b"PK\x03\x04" + b"0" * (D.MAX_BYTES + 10), "draft.xlsx", "file_too_large", id="too-large"),
])
def test_bad_files_are_refused_cleanly(label, blob, name, code):
    sa = base_setup()
    blob = download(sa) if blob is None else blob
    r = do_import(sa, blob, name=name, apply=True)
    assert r.status_code in (413, 422) and r.json()["detail"]["code"] == code, (label, r.text)
    assert values()["loginTitle"]["value"] == "Welcome"


def test_a_sheet_that_is_not_a_draft_is_refused():
    sa = base_setup()
    wb = Workbook(); wb.active.title = "Translations"; wb.create_sheet("Info"); wb["Info"]["A1"] = "File"; wb["Info"]["B1"] = "something else"
    out = io.BytesIO(); wb.save(out)
    assert do_import(sa, out.getvalue(), apply=True).json()["detail"]["code"] == "not_a_draft_file"


def test_formula_and_number_cells_in_the_translation_column_are_invalid():
    sa = base_setup()
    data = edit_file(download(sa), set_type={"D2": "=1+1", "D3": 12345})
    r = do_import(sa, data, apply=True).json()
    codes = sorted(i["code"] for i in r["invalid_rows"])
    assert codes == ["formula_not_allowed", "translation_not_text"] and r["updated"] == 0


def test_too_many_rows_are_refused():
    sa = base_setup()
    data = edit_file(download(sa), add=[[f"k{i}", "", "", "t"] for i in range(D.MAX_ROWS + 5)])
    r = do_import(sa, data, apply=True)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "too_many_rows"


def test_import_permissions():
    sa = base_setup(); data = edit_file(download(sa), {"loginTitle": "X"})
    assert do_import(viewer(), data, apply=True).status_code == 403
    assert do_import(fake_super(), data, apply=True).status_code == 403
    assert api.post("/admin/translations/import", files={"file": ("d.xlsx", data, D.XLSX_MIME)}, data={"lang": "ar"}).status_code in (401, 403)
    assert values()["loginTitle"]["value"] == "Welcome"
    assert do_import(editor(), data, apply=True).status_code == 200                        # edit permission is enough; no publish right needed
    assert publish(editor(), "ar").status_code == 403                                      # ... and it still cannot publish


def test_import_refused_when_environment_unknown(monkeypatch):
    sa = base_setup(); data = edit_file(download(sa), {"loginTitle": "X"})
    setup_env(monkeypatch, None)
    r = do_import(sa, data, apply=True)
    assert r.status_code == 503 and values()["loginTitle"]["value"] == "Welcome"


def test_disabled_language_is_refused():
    sa = base_setup(); data = download(sa)
    assert api.patch("/admin/translations/languages/ar", headers=sa, json={"status": "disabled"}).status_code == 200
    assert do_import(sa, data, apply=True).status_code == 409


# ═══ dashboard (real JS, headless browser, mocked API) ═══════════════════════════════════════════════════════════════════════
pw = pytest.importorskip("playwright.sync_api")
ROOT = os.path.dirname(os.path.abspath(T.__file__)) + "/.."
rd = lambda *p: open(os.path.normpath(os.path.join(ROOT, *p)), encoding="utf-8").read()
JS, CSS, PARTIAL = rd("static", "js", "translations.js"), rd("static", "css", "translations.css"), rd("templates", "partials", "translations.html")


def _fx(sa=True, can_edit=True, restored=False):
    ind = {"missing": 0, "draft_changes": 0, "waiting_review": 0, "complete": True, "published_version": 1}
    return {"env": {"environment": {"classification": "staging"}, "caller_is_system_super_admin": sa, "kill_switch": {"enabled": False}},
            "langs": {"base_language": {"code": "en", "name_en": "English", "name_native": "English", "direction": "ltr", "bundled": True, "countries": []},
                      "supported_countries": [],
                      "languages": [{"code": "ar", "name_en": "Arabic", "name_native": "العربية", "direction": "rtl", "status": "active", "countries": [],
                                     "status_indicators": {"app": ind, "legal": ind}, "last_updated": {"app": None, "legal": None}}]},
            "strings": {"lang": "ar", "track": "app", "total": 1, "page": 1, "page_size": 50, "rows": [
                {"key": "loginTitle", "screen_group": "login", "english": "Welcome", "placeholders": [], "max_length": 60, "value": "Welcome",
                 "status": "published", "locked": False, "lock_reason": ""}]}}


def _open(p, fx, handler=None, perms=True):
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1280, "height": 800}); calls = []
    html = ("<!doctype html><html><head><meta charset='utf-8'><link rel='stylesheet' href='/static/css/translations.css'></head><body><main>" + PARTIAL +
            f"<script>window._rbacCanDo=function(){{return {str(bool(perms)).lower()}}};</script><script src='/static/js/translations.js'></script></main></body></html>")

    def handle(route):
        url, req = route.request.url, route.request
        if url.endswith("/page"): return route.fulfill(body=html, content_type="text/html")
        if "/static/js/" in url: return route.fulfill(body=JS, content_type="application/javascript")
        if "/static/css/" in url: return route.fulfill(body=CSS, content_type="text/css")
        path = url.split("/admin/translations", 1)[1]
        try: body = req.post_data_buffer
        except Exception: body = None
        calls.append((req.method, path, body))
        if handler and handler(route, path, body): return
        for k, v in (("/environment", fx["env"]), ("/languages", fx["langs"]), ("/strings", fx["strings"])):
            if path.startswith(k) and req.method == "GET": return route.fulfill(body=json.dumps(v), content_type="application/json")
        route.fulfill(status=200, body="{}", content_type="application/json")
    pg.route("http://t.test/**", handle)
    pg.goto("http://t.test/page"); pg.evaluate("Translations.open()"); pg.wait_for_selector("#trn_pane_overview table")
    pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
    return b, pg, calls


def test_toolbar_has_download_draft_and_import_once_each():
    with pw.sync_playwright() as p:
        b, pg, _ = _open(p, _fx())
        tb = pg.locator("#trn_pane_strings .trn-toolbar").first
        assert tb.locator("[data-act=downloaddraft]").count() == 1 and tb.locator("[data-act=importdraft]").count() == 1
        assert tb.locator("[data-act=publish]").count() == 1
        assert pg.locator("[data-act=downloaddraft]").inner_text() == "Download Draft" and pg.locator("[data-act=importdraft]").inner_text() == "Import"
        b.close()


def test_import_button_needs_edit_permission_but_download_does_not():
    with pw.sync_playwright() as p:
        b, pg, _ = _open(p, _fx(sa=False), perms=False)
        assert pg.locator("[data-act=downloaddraft]").count() == 1 and pg.locator("[data-act=importdraft]").count() == 0
        b.close()


def test_download_draft_requests_the_selected_language_and_track_and_saves_the_file():
    def handler(route, path, body):
        if path.startswith("/draft"):
            route.fulfill(status=200, body=b"PK-fake", headers={"Content-Type": D.XLSX_MIME, "Content-Disposition": 'attachment; filename="offro-translations-ar-app-20261122.xlsx"'}); return True
    with pw.sync_playwright() as p:
        b, pg, calls = _open(p, _fx(), handler)
        with pg.expect_download() as dl:
            pg.click("[data-act=downloaddraft]")
        assert dl.value.suggested_filename == "offro-translations-ar-app-20261122.xlsx"
        assert [c for c in calls if c[1].startswith("/draft")][0][1] == "/draft?lang=ar&track=app"
        b.close()


SUMMARY = {"lang": "ar", "track": "app", "applied": False, "published": False, "total": 10, "updated": 3, "unchanged": 5, "invalid": 1, "missing": 1,
           "empty_cells": 0, "english_edits_ignored": 0, "baseline_changed": False,
           "invalid_rows": [{"row": 4, "key": "bad<img>", "code": "unknown_key"}], "updated_keys": ["a", "b", "c"], "missing_keys": ["z"]}


def test_import_dialog_checks_first_shows_the_five_counts_then_applies():
    posts = []
    def handler(route, path, body):
        if path == "/import" and route.request.method == "POST":
            apply = b'name="apply"\r\n\r\n1\r\n' in (body or b"")
            posts.append({"apply": apply, "has_file": b'filename="edited.xlsx"' in (body or b""), "lang": b'name="lang"\r\n\r\nar' in (body or b"")})
            route.fulfill(body=json.dumps({**SUMMARY, "applied": apply}), content_type="application/json"); return True
    with pw.sync_playwright() as p:
        b, pg, _ = _open(p, _fx(), handler)
        pg.click("[data-act=importdraft]"); assert pg.locator("#trn_modal").is_visible()
        pg.click("#trn_modal_ok"); assert "Choose the edited" in pg.inner_text("#trn_modal_err")        # nothing uploaded yet
        pg.set_input_files("#trn_imp_file", {"name": "edited.xlsx", "mimeType": D.XLSX_MIME, "buffer": b"PK-fake"})
        pg.click("#trn_modal_ok"); pg.wait_for_selector("#trn_imp_out table")
        got = {k: pg.inner_text(f"[data-sum='{k}']") for k in ("Total records", "Updated records", "Unchanged records", "Invalid records", "Missing records")}
        assert got == {"Total records": "10", "Updated records": "3", "Unchanged records": "5", "Invalid records": "1", "Missing records": "1"}
        assert posts == [{"apply": False, "has_file": True, "lang": True}] and pg.inner_text("#trn_modal_ok").startswith("Apply import")
        assert pg.locator("#trn_imp_out img").count() == 0 and "unknown_key" in pg.inner_text("#trn_imp_out")     # server text is escaped
        pg.click("#trn_modal_ok"); pg.wait_for_selector("#trn_modal", state="hidden")
        assert posts[1]["apply"] is True
        assert "Publish Changes" in pg.inner_text("#trn_alert") and "3 updated" in pg.inner_text("#trn_alert")
        b.close()


def test_choosing_another_file_after_the_check_requires_checking_again():
    posts = []
    def handler(route, path, body):
        if path == "/import": posts.append(1); route.fulfill(body=json.dumps(SUMMARY), content_type="application/json"); return True
    with pw.sync_playwright() as p:
        b, pg, _ = _open(p, _fx(), handler)
        pg.click("[data-act=importdraft]")
        pg.set_input_files("#trn_imp_file", {"name": "a.xlsx", "mimeType": D.XLSX_MIME, "buffer": b"PK1"}); pg.click("#trn_modal_ok"); pg.wait_for_selector("#trn_imp_out table")
        pg.set_input_files("#trn_imp_file", {"name": "b.xlsx", "mimeType": D.XLSX_MIME, "buffer": b"PK2"})
        assert pg.inner_text("#trn_modal_ok") == "Check file" and pg.locator("#trn_imp_out table").count() == 0
        b.close()


def test_server_error_is_shown_in_the_dialog_and_nothing_applies():
    def handler(route, path, body):
        if path == "/import": route.fulfill(status=422, body=json.dumps({"detail": {"code": "language_mismatch"}}), content_type="application/json"); return True
    with pw.sync_playwright() as p:
        b, pg, _ = _open(p, _fx(), handler)
        pg.click("[data-act=importdraft]")
        pg.set_input_files("#trn_imp_file", {"name": "a.xlsx", "mimeType": D.XLSX_MIME, "buffer": b"PK1"}); pg.click("#trn_modal_ok")
        pg.wait_for_function("document.querySelector('#trn_modal_err') && !document.querySelector('#trn_modal_err').hidden")
        assert "different language" in pg.inner_text("#trn_modal_err") and pg.locator("#trn_modal").is_visible()
        b.close()


def test_wrong_file_extension_is_refused_in_the_browser_before_upload():
    posts = []
    def handler(route, path, body):
        if path == "/import": posts.append(1); return False
    with pw.sync_playwright() as p:
        b, pg, _ = _open(p, _fx(), handler)
        pg.click("[data-act=importdraft]")
        pg.set_input_files("#trn_imp_file", {"name": "a.csv", "mimeType": "text/csv", "buffer": b"a,b"}); pg.click("#trn_modal_ok")
        assert ".xlsx" in pg.inner_text("#trn_modal_err") and posts == []
        b.close()


def test_saving_the_published_wording_shows_published_in_the_dashboard():
    def handler(route, path, body):
        if path.startswith("/strings/ar/loginTitle") and route.request.method == "PUT":
            route.fulfill(body=json.dumps({"status": "published", "key": "loginTitle"}), content_type="application/json"); return True
    with pw.sync_playwright() as p:
        b, pg, _ = _open(p, _fx(), handler)
        pg.fill("tr[data-row=loginTitle] textarea", "Welcome back"); pg.click("tr[data-row=loginTitle] [data-act=save]")
        pg.wait_for_function("document.getElementById('trn_alert').textContent.length > 0")      # the save finished (the pill already read 'published' before)
        assert pg.inner_text("[data-status]") == "published"
        assert "Published" in pg.inner_text("#trn_alert") and "Modified" not in pg.inner_text("#trn_alert")
        b.close()


def test_missing_openpyxl_only_disables_these_two_features(monkeypatch):
    sa = base_setup(); data = download(sa)
    def gone():
        raise T.TranslationError("xlsx_support_missing", 503, "The openpyxl package is not installed on this server")
    monkeypatch.setattr(D, "_xl", gone)
    assert api.get("/admin/translations/draft", headers=sa, params={"lang": "ar"}).status_code == 503
    assert do_import(sa, data, apply=True).status_code == 503
    assert put(sa, "loginTitle", "still works", "ar").status_code == 200              # ordinary editing is unaffected
    import subprocess, sys
    src = open(D.__file__, encoding="utf-8").read()
    assert not re.search(r"^(from|import) openpyxl", src, re.M)                       # never imported at server start-up


def test_openpyxl_is_pinned_in_requirements():
    req = open(os.path.join(os.path.dirname(D.__file__), "..", "requirements.txt"), encoding="utf-8").read()
    assert re.search(r"^openpyxl==\d+\.\d+\.\d+\s*$", req, re.M)


# ═══ environment safety: a file belongs to exactly one environment ═══════════════════════════════════════════════════════════
def _writes_snapshot():
    return (list(DB[T.VALUES].find({}, {"_id": 0})), DB[T.VERSIONS].count_documents({}), DB[T.LANGS].count_documents({}))


@pytest.mark.parametrize("file_env,server_env", [("staging", "production"), ("production", "staging")])
@pytest.mark.parametrize("apply", [False, True])
def test_a_file_from_the_other_environment_is_refused_for_check_and_apply(monkeypatch, file_env, server_env, apply):
    setup_env(monkeypatch, file_env)
    sa = base_setup()
    data = edit_file(download(sa), {"loginTitle": "Changed in the other environment"})
    assert load_workbook(io.BytesIO(data))["Info"]["B5"].value == file_env
    setup_env(monkeypatch, server_env)                                      # the same database content, now served as the other environment
    before = _writes_snapshot(); audits = DB.translation_audit.count_documents({"action": "import_draft", "outcome": "ok"})
    r = do_import(sa, data, apply=apply)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "environment_mismatch"
    assert r.json()["detail"]["detail"] == {"file": file_env, "server": server_env}
    assert _writes_snapshot() == before and values()["loginTitle"]["value"] == "Welcome"            # nothing written
    assert DB.translation_audit.count_documents({"action": "import_draft", "outcome": "ok"}) == audits
    # the matching environment still accepts the very same file
    setup_env(monkeypatch, file_env)
    assert do_import(sa, data, apply=apply).status_code == 200


@pytest.mark.parametrize("label,value", [("missing", None), ("blank", "   "), ("unknown", "qa"), ("not an environment", "production-eu"),
                                         ("different case", "Staging"), ("number", 7)])
def test_missing_or_unknown_environment_in_the_file_is_refused(label, value):
    sa = base_setup()
    data = edit_info(edit_file(download(sa), {"loginTitle": "X"}), "Environment", value)
    before = _writes_snapshot()
    for apply in (False, True):
        r = do_import(sa, data, apply=apply)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "environment_mismatch", (label, apply, r.text)
    assert _writes_snapshot() == before


def test_environment_row_deleted_from_the_info_sheet_is_refused():
    sa = base_setup()
    wb = load_workbook(io.BytesIO(download(sa))); ws = wb["Info"]
    for row in list(ws.iter_rows(min_row=1, max_col=2)):
        if row[0].value == "Environment":
            row[0].value = row[1].value = None
    out = io.BytesIO(); wb.save(out)
    r = do_import(sa, out.getvalue(), apply=True)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "environment_mismatch"
    assert r.json()["detail"]["detail"]["file"] == "missing"


def test_environment_is_checked_before_language_and_track_and_before_any_row_is_read():
    sa = base_setup(); mk_lang(sa, "hi")
    data = edit_info(download(sa, "ar"), "Environment", "production")          # wrong environment AND wrong language: environment wins
    r = do_import(sa, data, lang="hi", apply=True)
    assert r.json()["detail"]["code"] == "environment_mismatch"


def test_unknown_server_environment_still_refuses_even_a_matching_file(monkeypatch):
    sa = base_setup(); data = download(sa)
    setup_env(monkeypatch, None)
    assert do_import(sa, data, apply=False).status_code == 503 and do_import(sa, data, apply=True).status_code == 503


def test_dashboard_explains_the_environment_error():
    assert "environment_mismatch" in JS and "different environment" in JS


# ═══ partial import: valid rows applied, invalid rows skipped, nothing invalid ever written ══════════════════════════════════
def _mixed_file(sa):
    return edit_file(download(sa), {"loginTitle": "Welcome back", "otpSent": "wrong, no placeholder", "resendOtp": "Resend"},
                     drop=("supportLine",),
                     add=[["ghostKey", "x", "x", "t"], ["smsInstead", "otp", "x", "dup"], ["resendOtp", "otp", "x", "dup of an existing row"]])


def test_check_only_reports_valid_invalid_unchanged_and_missing_without_writing():
    sa = base_setup()
    before = _writes_snapshot()
    r = do_import(sa, _mixed_file(sa), apply=False).json()
    # rows: loginTitle(valid, changed) otpSent(invalid) resendOtp x2 (duplicate -> invalid) smsInstead x2 (duplicate -> invalid) ghostKey(invalid)
    assert r["valid"] == 1 and r["updated"] == 1 and r["unchanged"] == 0 and r["invalid"] == 6 and r["total"] == 7 and r["missing"] == 1
    assert r["total"] == r["valid"] + r["invalid"] and r["valid"] == r["updated"] + r["unchanged"]
    assert r["applied"] is False and _writes_snapshot() == before


def test_apply_writes_the_valid_rows_and_never_an_invalid_or_duplicate_one():
    sa = base_setup()
    data = _mixed_file(sa)
    check = do_import(sa, data, apply=False).json()
    r = do_import(sa, data, apply=True).json()
    for k in ("total", "valid", "updated", "unchanged", "invalid", "missing"):
        assert r[k] == check[k], k                                                   # Check file told the truth about what Apply does
    v = values()
    assert v["loginTitle"]["value"] == "Welcome back"                                # valid: applied
    assert v["resendOtp"]["value"] == "Resend"                                       # duplicate key: both occurrences skipped, old value kept
    assert "otpSent" not in v and "smsInstead" not in v and "ghostKey" not in v      # invalid / duplicate / unknown: never written
    assert status_of(sa, "ar", "loginTitle") == "modified" and DB[T.VERSIONS].count_documents({"lang": "ar"}) == 1


def test_dialog_tells_the_admin_that_apply_skips_invalid_rows():
    sa_js = JS
    assert "will store the" in sa_js and "skip the" in sa_js and "never written" in sa_js and "'Valid records'" in sa_js


def test_import_dialog_shows_valid_and_the_apply_skips_invalid_warning():
    def handler(route, path, body):
        if path == "/import": route.fulfill(body=json.dumps({**SUMMARY, "valid": 9}), content_type="application/json"); return True
    with pw.sync_playwright() as p:
        b, pg, _ = _open(p, _fx(), handler)
        pg.click("[data-act=importdraft]")
        pg.set_input_files("#trn_imp_file", {"name": "e.xlsx", "mimeType": D.XLSX_MIME, "buffer": b"PK"}); pg.click("#trn_modal_ok"); pg.wait_for_selector("#trn_imp_out table")
        assert pg.inner_text("[data-sum='Valid records']") == "9"
        note = pg.inner_text("#trn_imp_out .trn-partial")
        assert "check only" in note and "3 updated records" in note and "skip the 1 invalid record" in note and "never written" in note
        b.close()


# ═══ order of checks: environment first, translation lookups only afterwards ═════════════════════════════════════════════════
class _NoDb:
    """Stands in for services.translations.db: ANY access is a test failure."""
    def __getitem__(self, name):
        raise AssertionError(f"translation database accessed ({name}) before the environment was validated")

    def __getattr__(self, name):
        raise AssertionError(f"translation database accessed ({name}) before the environment was validated")


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("bad_env", ["production", "qa", "", None])
def test_mismatched_environment_is_rejected_before_get_language_or_any_database_access(monkeypatch, apply, bad_env):
    sa = base_setup()
    data = edit_info(edit_file(download(sa), {"loginTitle": "X"}), "Environment", bad_env)
    calls = []
    monkeypatch.setattr(T, "get_language", lambda *a, **k: calls.append("get_language") or {"status": "active"})
    monkeypatch.setattr(T, "_check_track", lambda *a, **k: calls.append("_check_track"))
    monkeypatch.setattr(T, "db", _NoDb())                                          # any translation DB access now fails the test
    with pytest.raises(T.TranslationError) as e:
        D.import_draft("ar", "app", data, "tester", apply=apply)
    assert e.value.code == "environment_mismatch" and e.value.status == 422
    assert calls == []                                                             # neither the language nor the track was looked up


def test_a_file_for_an_unknown_language_from_the_wrong_environment_reports_the_environment_first(monkeypatch):
    sa = base_setup()
    data = edit_info(download(sa), "Environment", "production")
    r = do_import(sa, data, lang="zz", apply=True)                                 # 'zz' does not exist: still the environment error, not 404
    assert r.status_code == 422 and r.json()["detail"]["code"] == "environment_mismatch"


def test_matching_environment_reads_the_file_first_and_only_then_looks_up_the_language(monkeypatch):
    sa = base_setup(); data = download(sa)
    order = []
    real_parse, real_lang, real_track = D.parse_draft, T.get_language, T._check_track
    monkeypatch.setattr(D, "parse_draft", lambda d: (order.append("parse"), real_parse(d))[1])
    monkeypatch.setattr(T, "get_language", lambda c: (order.append("get_language"), real_lang(c))[1])
    monkeypatch.setattr(T, "_check_track", lambda t: (order.append("check_track"), real_track(t))[1])
    D.import_draft("ar", "app", data, "tester", apply=False)
    assert order == ["parse", "get_language", "check_track"]


def test_unidentified_server_environment_is_refused_before_the_file_is_even_read(monkeypatch):
    sa = base_setup(); data = download(sa)
    setup_env(monkeypatch, None)
    monkeypatch.setattr(D, "parse_draft", lambda d: (_ for _ in ()).throw(AssertionError("file read before require_env")))
    monkeypatch.setattr(T, "db", _NoDb())
    with pytest.raises(T.TranslationError) as e:
        D.import_draft("ar", "app", data, "tester", apply=True)
    assert e.value.code == "environment_unknown" and e.value.status == 503
