"""Stage 2: the real dashboard JS of the legal document editor in a headless browser, against mocked API responses.
Run:  python -m pytest test_legal_documents_browser.py -q -W ignore"""
import json

import pytest

import test_translations_dashboard_ui as UI
from test_translations_dashboard_ui import _fixture, pw, staging  # noqa: F401  (autouse fixture re-used)

EN = "# Privacy Policy\n\nWe respect your privacy.\n\n- Name\n- Phone"
DOCS = [
    {"slug": "privacy_policy", "key": "doc_privacy_policy", "title": "Privacy Policy", "audience": "customer", "has_english": True, "length": len(EN), "too_long": False, "source": "policies", "editable_english_here": False, "status": "missing"},
    {"slug": "terms_conditions", "key": "doc_terms_conditions", "title": "Terms & Conditions", "audience": "customer", "has_english": True, "length": 20, "too_long": False, "source": "terms", "editable_english_here": False, "status": "published"},
    {"slug": "faq", "key": "doc_faq", "title": "FAQ", "audience": "customer", "has_english": True, "length": 5, "too_long": False, "source": "own", "editable_english_here": True, "status": "missing"},
    {"slug": "contact_us", "key": "doc_contact_us", "title": "Contact Us", "audience": "customer", "has_english": False, "length": 0, "too_long": False, "source": "own", "editable_english_here": True, "status": "missing"},
]


def view(slug, value=None, status="missing", changed=False):
    d = [x for x in DOCS if x["slug"] == slug][0]
    return {"slug": slug, "key": d["key"], "title": d["title"], "audience": "customer", "lang": "ar", "english": EN if slug == "privacy_policy" else "English of " + slug,
            "has_english": True, "too_long": False, "value": value, "status": status, "english_changed": changed, "max_length": 30000}


def route_factory(sent, put_status=None, fail=None):
    def extra(route, path):
        m = route.request.method
        if m == "GET" and path.startswith("/documents/"):
            slug = path.split("/documents/")[1].split("?")[0]
            route.fulfill(body=json.dumps(view(slug, "نص عربي" if slug == "terms_conditions" else None, "published" if slug == "terms_conditions" else "missing")), content_type="application/json"); return True
        if m == "GET" and path.startswith("/documents"):
            route.fulfill(body=json.dumps({"lang": "ar", "documents": DOCS, "max_length": 30000}), content_type="application/json"); return True
        if m == "PUT" and path.startswith("/strings/ar/doc_"):
            sent.append((path, route.request.post_data_json))
            if fail:
                route.fulfill(status=422, body=json.dumps({"detail": {"code": fail}}), content_type="application/json"); return True
            route.fulfill(body=json.dumps({"lang": "ar", "key": "doc_x", "track": "legal", "status": put_status or "modified"}), content_type="application/json"); return True
    return extra


def open_docs(p, sent=None, **kw):
    b, pg, calls = UI._page(p, _fixture(), route_factory(sent if sent is not None else [], **kw))
    pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
    pg.select_option("select[data-change=track]", "documents"); pg.wait_for_selector(".trn-doc-grid")
    return b, pg, calls


def test_dropdown_lists_only_documents_that_have_english_text():
    with pw.sync_playwright() as p:
        b, pg, calls = open_docs(p)
        opts = pg.locator("select[data-change=doc] option").all_inner_texts()
        assert [o.split(" · ")[0] for o in opts] == ["Privacy Policy", "Terms & Conditions", "FAQ"]
        assert "Contact Us" in pg.inner_text("#trn_pane_strings")                       # named as waiting for English, not selectable
        assert any(c[1].startswith("/documents/privacy_policy?lang=ar") for c in calls)
        b.close()


def test_english_left_is_read_only_and_translation_right_is_editable():
    with pw.sync_playwright() as p:
        b, pg, calls = open_docs(p)
        en, val = pg.locator("textarea.trn-doc-en"), pg.locator("textarea.trn-doc-val")
        assert en.input_value() == EN and en.get_attribute("readonly") is not None and en.get_attribute("dir") == "ltr"
        assert val.get_attribute("dir") == "rtl" and val.is_editable() and not en.is_editable()
        assert "Read-only source" in pg.inner_text(".trn-doc-panel-en") and "Arabic (ar)" in pg.inner_text("[data-doc-lang]")
        en.evaluate("e => e.value = 'hacked'")                                           # even a forced change is never sent anywhere
        pg.fill("textarea.trn-doc-val", "ترجمة"); pg.click("[data-act=savedoc]"); pg.wait_for_selector("[data-doc-status]:has-text('Modified')")
        assert not any(c[0] == "PUT" and "english" in c[1] for c in calls)
        assert pg.locator("[data-act=editenglish]").count() == 0                         # policies keep their own editor
        b.close()


def test_side_by_side_on_desktop_and_stacked_on_mobile_and_scrollable():
    with pw.sync_playwright() as p:
        b, pg, calls = open_docs(p)
        pg.set_viewport_size({"width": 1280, "height": 900})
        a, c = pg.locator(".trn-doc-panel-en").bounding_box(), pg.locator(".trn-doc-panel").nth(1).bounding_box()
        assert abs(a["y"] - c["y"]) < 4 and c["x"] > a["x"] + a["width"] - 2
        pg.set_viewport_size({"width": 420, "height": 800})
        a, c = pg.locator(".trn-doc-panel-en").bounding_box(), pg.locator(".trn-doc-panel").nth(1).bounding_box()
        assert c["y"] > a["y"] + a["height"] - 2 and a["width"] <= 420
        pg.fill("textarea.trn-doc-val", "سطر\n" * 400)
        assert pg.eval_on_selector("textarea.trn-doc-val", "e => e.scrollHeight > e.clientHeight && getComputedStyle(e).overflowY !== 'hidden'")
        assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2")
        b.close()


def test_save_marks_modified_and_restoring_published_text_shows_published():
    sent = []
    with pw.sync_playwright() as p:
        b, pg, calls = open_docs(p, sent)
        assert pg.locator("[data-act=savedoc]").is_hidden()
        pg.fill("textarea.trn-doc-val", "ترجمة جديدة"); assert pg.locator("[data-act=savedoc]").is_visible()
        pg.click("[data-act=savedoc]"); pg.wait_for_selector("[data-doc-status]:has-text('Modified')")
        assert sent == [("/strings/ar/doc_privacy_policy", {"value": "ترجمة جديدة"})]
        assert "Status is now Modified" in pg.inner_text("#trn_alert")
        assert "Privacy Policy · Modified" in pg.locator("select[data-change=doc]").inner_text()
        b.close()
    sent = []
    with pw.sync_playwright() as p:
        b, pg, calls = open_docs(p, sent, put_status="published")
        pg.fill("textarea.trn-doc-val", "الأصل"); pg.click("[data-act=savedoc]"); pg.wait_for_selector("[data-doc-status]:has-text('Published')")
        assert "status is Published" in pg.inner_text("#trn_alert")
        b.close()


def test_switching_document_loads_its_english_and_translation_and_language_is_not_hardcoded():
    with pw.sync_playwright() as p:
        b, pg, calls = open_docs(p)
        pg.select_option("select[data-change=doc]", "terms_conditions"); pg.wait_for_function("document.querySelector('textarea.trn-doc-val').value === 'نص عربي'")
        assert pg.locator("textarea.trn-doc-en").input_value() == "English of terms_conditions"
        assert "Published" in pg.inner_text("[data-doc-status]")
        assert any(c[1].startswith("/documents/terms_conditions?lang=ar") for c in calls)
        b.close()


def test_unsaved_text_asks_before_it_is_discarded():
    with pw.sync_playwright() as p:
        b, pg, calls = open_docs(p)
        pg.fill("textarea.trn-doc-val", "غير محفوظ")
        msgs = []
        pg.once("dialog", lambda d: (msgs.append(d.message), d.dismiss()))
        pg.select_option("select[data-change=doc]", "faq")
        assert msgs and "unsaved" in msgs[0].lower()
        assert pg.locator("textarea.trn-doc-val").input_value() == "غير محفوظ" or pg.locator("select[data-change=doc]").input_value() == "privacy_policy"
        b.close()


def test_server_errors_are_shown_and_nothing_is_marked_saved():
    with pw.sync_playwright() as p:
        b, pg, calls = open_docs(p, fail="html_not_allowed")
        pg.fill("textarea.trn-doc-val", "<b>x</b>"); pg.click("[data-act=savedoc]")
        pg.wait_for_selector("[data-doc-err]:not([hidden])")
        assert "HTML tags" in pg.inner_text("[data-doc-err]") and pg.locator("[data-act=savedoc]").is_visible()
        assert "Not translated" in pg.inner_text("[data-doc-status]")
        b.close()


def test_stale_english_notice_and_script_in_text_is_not_executed():
    evil = "<img src=x onerror=window.__pwned=1><script>window.__pwned=1</script>"
    def extra(route, path):
        if route.request.method == "GET" and path.startswith("/documents/"):
            v = view("privacy_policy", evil, "modified", True); v["english"] = evil; v["title"] = evil
            route.fulfill(body=json.dumps(v), content_type="application/json"); return True
        return route_factory([])(route, path)
    with pw.sync_playwright() as p:
        b, pg, calls = UI._page(p, _fixture(), extra)
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        pg.select_option("select[data-change=track]", "documents"); pg.wait_for_selector(".trn-doc-grid")
        assert "English text was changed" in pg.inner_text("#trn_pane_strings")
        assert pg.evaluate("window.__pwned") is None
        assert pg.locator("textarea.trn-doc-en").input_value() == evil
        b.close()


def test_viewer_sees_both_panels_but_cannot_edit_or_publish():
    with pw.sync_playwright() as p:
        b, pg, calls = UI._page(p, _fixture(sa=False), route_factory([]), can_edit=False)
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        pg.select_option("select[data-change=track]", "documents"); pg.wait_for_selector(".trn-doc-grid")
        assert not pg.locator("textarea.trn-doc-val").is_editable()
        assert pg.locator("[data-act=savedoc]").count() == 0 and pg.locator("[data-act=publish]").count() == 0 and pg.locator("[data-act=adddoc]").count() == 0
        assert pg.locator("textarea.trn-doc-en").input_value() == EN
        b.close()


def test_super_admin_can_add_a_document_and_publish_the_legal_text_type():
    sent = []
    def extra(route, path):
        if route.request.method == "POST" and path == "/documents":
            sent.append(("doc", route.request.post_data_json)); route.fulfill(body='{"slug":"faq","key":"doc_faq","title":"FAQ"}', content_type="application/json"); return True
        if route.request.method == "POST" and path == "/publish":
            sent.append(("publish", route.request.post_data_json)); route.fulfill(body='{"version":1}', content_type="application/json"); return True
        return route_factory([])(route, path)
    with pw.sync_playwright() as p:
        b, pg, calls = UI._page(p, _fixture(), extra)
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        pg.select_option("select[data-change=track]", "documents"); pg.wait_for_selector(".trn-doc-grid")
        pg.click("[data-act=adddoc]")
        pg.fill("#trn_f_slug", "Faq"); pg.fill("#trn_f_title", "FAQ"); pg.fill("#trn_f_content", "Q? A.")
        pg.click("#trn_modal_ok"); pg.wait_for_selector("#trn_modal", state="hidden")
        assert sent[0] == ("doc", {"slug": "faq", "title": "FAQ", "audience": "customer", "content": "Q? A."})
        pg.click("[data-act=publish]")
        assert "legal document" in pg.inner_text("#trn_modal_text") and "staging only" in pg.inner_text("#trn_modal_text")
        pg.click("#trn_modal_ok"); pg.wait_for_selector("#trn_modal", state="hidden")
        assert sent[-1] == ("publish", {"lang": "ar", "track": "documents", "note": ""})
        b.close()


def test_the_other_views_are_unchanged():
    with pw.sync_playwright() as p:
        b, pg, calls = UI._page(p, _fixture(), route_factory([]))
        pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
        assert pg.locator(".trn-doc-grid").count() == 0
        assert pg.locator("select[data-change=track] option").all_inner_texts() == ["App text", "Legal documents"]
        assert any("track=app" in c[1] for c in calls)                                    # the App text view still loads as before
        b.close()
