"""Enhancement batch, items 1 + 3 in the real dashboard JS (headless browser, mocked API): the Overview tab keeps its two tabs and its
Languages table; each language row gets Delete beside Open (confirmation names the language), Edit is a yellow pen icon, and the Track
column says "Legal Documents" (with the real Legal Documents status) instead of "Legal text"; the Translations tab's text-type
dropdown no longer offers "Legal text".  Run:  python -m pytest test_translations_overview_delete_ui.py -q -W ignore
(file name sorts after test_banner_media.py on purpose.)"""
import copy
import json

import pytest

import test_translations_dashboard_ui as UI
from test_translations_dashboard_ui import _fixture, pw, staging  # noqa: F401  (autouse fixture re-used)


def fx(**kw):
    f = _fixture(**kw)
    ar = f["languages"]["languages"][0]
    ar["status"] = "active"; ar["countries"] = ["SA"]
    ar["legal_documents"] = {"total": 5, "missing": 3, "draft_changes": 1, "complete": False, "published_version": 1, "last_updated": "2026-11-05T10:00:00Z"}
    hi = copy.deepcopy(ar)
    hi.update({"code": "hi", "name_en": "Hindi", "name_native": "हिन्दी", "direction": "ltr", "countries": ["IN"]})
    hi["legal_documents"] = {"total": 5, "missing": 5, "draft_changes": 0, "complete": False, "published_version": None, "last_updated": None}
    f["languages"]["languages"] = [ar, hi]
    return f


def routes(state, delete_status=200, delete_body=None):
    def extra(route, path):
        m = route.request.method
        if m == "GET" and path.startswith("/languages/") and path.endswith("/delete-impact"):
            route.fulfill(body=json.dumps({"code": "x", "name_en": "x", "status": "active", "values": 7, "versions": 2, "country_mapping": 1}), content_type="application/json"); return True
        if m == "DELETE" and path.startswith("/languages/"):
            state["deleted"].append(path)
            if delete_status == 200:
                code = path.split("/languages/")[1].split("?")[0]
                state["fx"]["languages"]["languages"] = [l for l in state["fx"]["languages"]["languages"] if l["code"] != code]
            route.fulfill(status=delete_status, body=json.dumps(delete_body or {"code": "ok"}), content_type="application/json"); return True
    return extra


def open_overview(p, **kw):
    state = {"deleted": [], "fx": fx(**{k: v for k, v in kw.items() if k in ("env", "sa")})}
    b, pg, calls = UI._page(p, state["fx"], routes(state, kw.get("delete_status", 200), kw.get("delete_body")), can_edit=kw.get("can_edit", True))
    return b, pg, calls, state


def rows(pg):
    return pg.locator("#trn_pane_overview tbody tr")


def test_tabs_and_the_languages_table_are_unchanged():
    with pw.sync_playwright() as p:
        b, pg, calls, st = open_overview(p)
        assert [t.strip() for t in pg.locator("#trn_tabs button").all_inner_texts()] == ["Overview", "Translations"]
        heads = [h.strip().lower() for h in pg.locator("#trn_pane_overview thead th").all_inner_texts()]
        assert heads == ["language", "countries", "track", "modified", "completion", "status", "last updated", ""]
        assert "Arabic" in pg.inner_text("#trn_pane_overview") and "Hindi" in pg.inner_text("#trn_pane_overview")
        assert pg.locator("#trn_nl_code").count() == 1 and pg.locator("[data-act=killswitch]").count() == 1      # add-language form and kill switch still there
        b.close()


def test_track_column_says_legal_documents_with_the_real_status_and_never_legal_text():
    with pw.sync_playwright() as p:
        b, pg, calls, st = open_overview(p)
        txt = pg.inner_text("#trn_pane_overview")
        assert "Legal text" not in txt and "Legal Documents" in txt
        ar_docs = [r for r in rows(pg).all() if "Arabic" in r.inner_text() and "Legal Documents" in r.inner_text()][0].inner_text()
        assert "1 modified" in ar_docs and "3 missing" in ar_docs and "05 Nov 2026" in ar_docs and "Needs attention" in ar_docs
        hi_docs = [r for r in rows(pg).all() if "Hindi" in r.inner_text() and "Legal Documents" in r.inner_text()][0].inner_text()
        assert "5 missing" in hi_docs and "Not published" in hi_docs
        b.close()


def test_delete_sits_beside_open_on_the_first_row_of_each_language_only_with_a_yellow_pen_for_edit():
    with pw.sync_playwright() as p:
        b, pg, calls, st = open_overview(p)
        lang_rows = [r for r in rows(pg).all() if r.locator("[data-act=open]").count()]
        assert len(lang_rows) == 4                                                   # 2 languages x (App text, Legal Documents)
        for i, r in enumerate(lang_rows):
            assert r.locator("[data-act=open]").count() == 1                         # Open stays on BOTH rows of a language
            if i % 2 == 0:                                                           # first row (App text): Delete directly beside Open
                assert r.locator("[data-act=open] + [data-act=deletelang]").count() == 1
                assert r.locator("[data-act=deletelang]").inner_text().strip() == "Delete"
                assert "App text" in r.inner_text()
            else:                                                                    # second row (Legal Documents): no Delete
                assert r.locator("[data-act=deletelang]").count() == 0 and "Legal Documents" in r.inner_text()
        assert pg.locator("[data-act=deletelang]").count() == 2                      # exactly once per language
        assert "both" in pg.locator("[data-act=deletelang]").first.get_attribute("title")
        pens = pg.locator("[data-act=editcountries]")
        assert pens.count() == 5                                                     # 4 language rows + English base row
        for i in range(pens.count()):
            assert pens.nth(i).locator("i.fa-pen").count() == 1 and pens.nth(i).inner_text().strip() == ""
            assert pens.nth(i).get_attribute("aria-label").startswith("Edit countries for")
        color = pens.first.evaluate("e => getComputedStyle(e.querySelector('i')).color")
        r_, g_, b_ = [int(x) for x in color[color.index("(") + 1:color.index(")")].split(",")[:3]]
        assert r_ > 200 and g_ > 140 and b_ < 60                                     # yellow / amber
        b.close()


def test_delete_asks_with_the_real_language_name_and_cancel_changes_nothing():
    with pw.sync_playwright() as p:
        b, pg, calls, st = open_overview(p)
        pg.locator("tr:has-text('Arabic') [data-act=deletelang]").first.click()
        pg.wait_for_selector("#trn_modal:not([hidden])")
        assert "removes the whole language: both tracks, App text and Legal Documents" in pg.inner_text("#trn_modal_fields")
        assert pg.inner_text("#trn_modal_text") == "Are you sure you want to delete Arabic? This action cannot be undone."
        assert "Delete Arabic" in pg.inner_text("#trn_modal_title")
        assert "7 saved translations" in pg.inner_text("#trn_modal_fields") and "2 published versions" in pg.inner_text("#trn_modal_fields")
        assert "Other languages are not affected" in pg.inner_text("#trn_modal_fields")
        pg.click("#trn_modal_cancel"); pg.wait_for_selector("#trn_modal", state="hidden")
        assert st["deleted"] == [] and "Arabic" in pg.inner_text("#trn_pane_overview")
        b.close()


def test_confirming_deletes_only_that_language_and_removes_it_from_the_selectors():
    with pw.sync_playwright() as p:
        b, pg, calls, st = open_overview(p)
        pg.locator("tr:has-text('Arabic') [data-act=deletelang]").first.click(); pg.wait_for_selector("#trn_modal:not([hidden])")
        pg.fill("#trn_f_typed", "Arabic"); pg.click("#trn_modal_ok"); pg.wait_for_selector("#trn_modal", state="hidden")
        pg.wait_for_function("!document.querySelector('#trn_pane_overview tbody').innerText.includes('Arabic')")      # (the add-language hint may mention Arabic)
        assert st["deleted"] == ["/languages/ar?confirm=ar"]                         # the server also requires the typed code
        assert "Hindi" in pg.inner_text("#trn_pane_overview tbody") and "was deleted" in pg.inner_text("#trn_alert")
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("select[data-change=lang]")
        opts = pg.locator("select[data-change=lang] option").all_inner_texts()
        assert opts == ["Hindi (hi)"]                                                # the deleted language is gone from the language dropdown
        b.close()


def test_delete_button_stays_disabled_until_the_exact_language_name_is_typed():
    with pw.sync_playwright() as p:
        b, pg, calls, st = open_overview(p)
        pg.locator("tr:has-text('Arabic') [data-act=deletelang]").first.click(); pg.wait_for_selector("#trn_modal:not([hidden])")
        assert pg.locator("#trn_f_typed").count() == 1 and "Arabic" in pg.inner_text("#trn_modal_fields")
        assert pg.locator("#trn_modal_ok").is_disabled()                              # nothing typed yet
        for wrong in ["ar", "arabic", "Arab", "Arabic x", "Hindi"]:                  # code, wrong case, partial, extra text, another language
            pg.fill("#trn_f_typed", wrong)
            assert pg.locator("#trn_modal_ok").is_disabled(), wrong
        pg.locator("#trn_modal_ok").evaluate("e => { e.disabled = false; e.click(); }")   # even a forced click is validated in the script
        pg.wait_for_selector("#trn_modal_err:not([hidden])")
        assert "exactly" in pg.inner_text("#trn_modal_err") and st["deleted"] == []         # no DELETE request was sent
        pg.fill("#trn_f_typed", "Arabic")
        assert pg.locator("#trn_modal_ok").is_enabled()
        pg.fill("#trn_f_typed", "Arabic "); assert pg.locator("#trn_modal_ok").is_enabled()  # surrounding whitespace is ignored
        pg.fill("#trn_f_typed", "Arabi"); assert pg.locator("#trn_modal_ok").is_disabled()   # editing it again re-disables
        pg.click("#trn_modal_cancel"); pg.wait_for_selector("#trn_modal", state="hidden")
        assert st["deleted"] == []
        b.close()


def test_other_dialogs_are_not_affected_by_the_typed_gate():
    with pw.sync_playwright() as p:
        b, pg, calls, st = open_overview(p)
        pg.locator("tr:has-text('Arabic') [data-act=deletelang]").first.click(); pg.wait_for_selector("#trn_modal:not([hidden])")
        pg.click("#trn_modal_cancel"); pg.wait_for_selector("#trn_modal", state="hidden")
        pg.locator("tr:has-text('Arabic') [data-act=editcountries]").first.click(); pg.wait_for_selector("#trn_modal:not([hidden])")
        assert pg.locator("#trn_f_typed").count() == 0 and pg.locator("#trn_modal_ok").is_enabled()
        b.close()


def test_a_refused_delete_shows_the_reason_and_keeps_the_language():
    with pw.sync_playwright() as p:
        b, pg, calls, st = open_overview(p, delete_status=409, delete_body={"detail": {"code": "english_not_deletable", "detail": None}})
        pg.locator("tr:has-text('Hindi') [data-act=deletelang]").first.click(); pg.wait_for_selector("#trn_modal:not([hidden])")
        pg.fill("#trn_f_typed", "Hindi"); pg.click("#trn_modal_ok"); pg.wait_for_selector("#trn_modal_err:not([hidden])")
        assert "English is the built-in base language" in pg.inner_text("#trn_modal_err")
        assert "Hindi" in pg.inner_text("#trn_pane_overview") and pg.locator("#trn_modal:not([hidden])").count() == 1
        b.close()


@pytest.mark.parametrize("kw", [{"sa": False}, {"env": "unknown"}])
def test_no_delete_button_without_super_admin_or_an_identified_environment(kw):
    with pw.sync_playwright() as p:
        b, pg, calls, st = open_overview(p, **kw)
        assert pg.locator("[data-act=deletelang]").count() == 0 and pg.locator("[data-act=open]").count() >= 1
        b.close()


def test_open_on_the_legal_documents_row_goes_to_the_legal_documents_view():
    with pw.sync_playwright() as p:
        b, pg, calls, st = open_overview(p)
        pg.locator("tr:has-text('Hindi'):has-text('Legal Documents') [data-act=open]").click()
        pg.wait_for_selector("select[data-change=track]")
        assert pg.locator("select[data-change=track]").input_value() == "documents"
        assert pg.locator("select[data-change=lang]").input_value() == "hi"
        assert pg.locator("select[data-change=track] option").all_inner_texts() == ["App text", "Legal documents"]
        b.close()
