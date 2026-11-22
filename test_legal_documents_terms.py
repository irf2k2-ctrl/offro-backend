"""Enhancement batch, item 2 (+ 3): User Terms & Conditions and Merchant Terms & Conditions in Translation > Legal documents, and the
Overview 'Legal Documents' row.

ROOT CAUSE (kept here as documentation): the document list (services/legal_documents.py) was correct - both Terms are registered and read from the
EXISTING `terms` collection (type user / merchant). But the editor's dropdown listed only documents that HAVE English text, and the Terms are the
only existing documents with no built-in default text (Privacy / Refund / KYC fall back to a default in routers/public.py; the Terms endpoints
return '' until an administrator saves text in Dashboard > Terms & Conditions). In a database where the Terms were never saved they were therefore
filtered out of the dropdown and only named in a small "No English text yet" line. Fix: documents stored in the existing collections
(policies / terms / settings) are always listed, labelled "No English text yet" until their English is saved; nothing is created, copied or migrated.
(file name sorts after test_banner_media.py on purpose.)  Run:  python -m pytest test_legal_documents_terms.py -q -W ignore
"""
import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import merchant_app as MA
from routers import public as PUB
from routers import translations_admin as TA
from routers import translations_public as TP
from services import legal_documents as LD
from services import translations as T
from translation_test_support import DB, add_ar, client as api, put, publish, setup_env, super_admin, sync_fixture_keys, viewer

app = FastAPI()
app.include_router(TA.router, prefix="/admin")
app.include_router(TP.router)
app.include_router(PUB.router)
app.include_router(MA.router, prefix="/merchant")
web = TestClient(app)

USER_TERMS = "# Terms & Conditions\n\n## 1. Acceptance\nBy using Offro you agree.\n"
MERCHANT_TERMS = "# Merchant Terms\n\nMerchants must follow the rules.\n"


@pytest.fixture(autouse=True)
def staging(monkeypatch):
    setup_env(monkeypatch, "staging")
    sync_fixture_keys()


def listing(sa, lang="ar"):
    r = api.get("/admin/translations/documents", headers=sa, params={"lang": lang})
    assert r.status_code == 200, r.text
    return {d["slug"]: d for d in r.json()["documents"]}


def snap():
    return copy.deepcopy([list(DB[c].find({}, {"_id": 0})) for c in ("policies", "terms", "settings", LD.OWN)])


class TestRootCause:
    def test_terms_are_registered_from_the_existing_terms_collection_even_when_empty(self):
        sa = super_admin(); assert add_ar(sa).status_code == 200
        rows = listing(sa)
        for slug, aud in (("terms_conditions", "customer"), ("merchant_terms", "merchant")):
            assert rows[slug]["source"] == "terms" and rows[slug]["audience"] == aud
            assert rows[slug]["has_english"] is False                 # nothing saved in Dashboard > Terms & Conditions -> nothing to translate yet
        assert rows["privacy_policy"]["has_english"] is True          # has a built-in default, which is why Privacy / Refund / KYC were never missing
        assert DB.terms.count_documents({}) == 0 and DB[LD.OWN].count_documents({}) == 0     # listing created nothing

    def test_saved_terms_are_listed_with_their_english_and_reuse_the_existing_records(self):
        sa = super_admin(); assert add_ar(sa).status_code == 200
        DB.terms.insert_one({"type": "user", "content": USER_TERMS}); DB.terms.insert_one({"type": "merchant", "content": MERCHANT_TERMS})
        before = snap()
        rows = listing(sa)
        assert rows["terms_conditions"]["has_english"] and rows["merchant_terms"]["has_english"]
        d = api.get("/admin/translations/documents/merchant_terms", headers=sa, params={"lang": "ar"}).json()
        assert d["english"] == MERCHANT_TERMS.strip() or d["english"] == MERCHANT_TERMS
        assert put(sa, "doc_terms_conditions", "# الشروط", "ar").status_code == 200
        assert put(sa, "doc_merchant_terms", "# شروط التاجر", "ar").status_code == 200
        assert snap() == before                                       # no duplicate, no migration, no write to the Terms / Policies / About records
        assert DB.terms.count_documents({}) == 2 and DB[LD.OWN].count_documents({}) == 0


class TestDraftPublishedAndFallback:
    @pytest.fixture
    def terms(self):
        DB.terms.insert_one({"type": "user", "content": USER_TERMS}); DB.terms.insert_one({"type": "merchant", "content": MERCHANT_TERMS})
        sa = super_admin(); assert add_ar(sa).status_code == 200
        listing(sa)
        return sa

    def test_a_draft_is_never_served_to_app_users(self, terms):
        sa = terms
        assert put(sa, "doc_terms_conditions", "# مسودة", "ar").status_code == 200
        assert put(sa, "doc_merchant_terms", "# مسودة التاجر", "ar").status_code == 200
        for url, params in (("/terms/user", {"lang": "ar"}), ("/terms/merchant", {"lang": "ar"}), ("/merchant/terms", {"lang": "ar"}),
                            ("/legal/terms_conditions", {"lang": "ar"}), ("/legal/merchant_terms", {"lang": "ar"})):
            r = web.get(url, params=params).json()
            assert r["translated"] is False and r["content"].startswith("# ") and "مسودة" not in r["content"], url
            assert r["lang"] == "en"                                                                   # English fallback preserved

    def test_published_translations_are_served_and_english_stays_the_default(self, terms):
        sa = terms
        put(sa, "doc_terms_conditions", "# الشروط", "ar"); put(sa, "doc_merchant_terms", "# شروط التاجر", "ar")
        assert publish(sa, "ar", "documents").status_code == 200
        assert web.get("/terms/user", params={"lang": "ar"}).json() == {"type": "user", "content": "# الشروط", "lang": "ar", "translated": True, "direction": "rtl"}
        assert web.get("/terms/merchant", params={"lang": "ar"}).json()["content"] == "# شروط التاجر"
        assert web.get("/merchant/terms", params={"lang": "ar"}).json() == {"content": "# شروط التاجر", "lang": "ar", "translated": True, "direction": "rtl"}
        assert web.get("/terms/user").json() == {"type": "user", "content": USER_TERMS}              # no ?lang= : byte-for-byte the old response
        assert web.get("/terms/user", params={"lang": "hi"}).json()["translated"] is False           # a language without a published text -> English
        assert listing(sa)["terms_conditions"]["status"] == "published" and listing(sa)["merchant_terms"]["status"] == "published"

    def test_editing_after_publish_becomes_modified_and_the_published_text_stays_live(self, terms):
        sa = terms
        put(sa, "doc_terms_conditions", "# الشروط", "ar"); publish(sa, "ar", "documents")
        put(sa, "doc_terms_conditions", "# الشروط الجديدة", "ar")
        assert listing(sa)["terms_conditions"]["status"] == "modified"
        assert web.get("/terms/user", params={"lang": "ar"}).json()["content"] == "# الشروط"

    def test_a_viewer_can_open_but_not_save_terms(self, terms):
        vw = viewer()
        assert api.get("/admin/translations/documents/terms_conditions", headers=vw, params={"lang": "ar"}).status_code == 200
        assert put(vw, "doc_terms_conditions", "x", "ar").status_code == 403


class TestOverviewLegalDocumentsRow:
    def test_summary_counts_only_documents_with_english_and_uses_the_one_status_rule(self):
        DB.terms.insert_one({"type": "user", "content": USER_TERMS})
        sa = super_admin(); assert add_ar(sa).status_code == 200
        listing(sa)
        s = LD.language_summaries(["ar"])["ar"]
        with_english = [r for r in LD.list_documents() if r["has_english"]]
        assert s["total"] == len(with_english) and s["missing"] == s["total"] and s["draft_changes"] == 0 and not s["complete"] and s["published_version"] is None
        put(sa, "doc_terms_conditions", "# الشروط", "ar")
        assert LD.language_summaries(["ar"])["ar"]["draft_changes"] == 1
        publish(sa, "ar", "documents")
        s = LD.language_summaries(["ar"])["ar"]
        assert s["draft_changes"] == 0 and s["missing"] == s["total"] - 1 and s["published_version"] == 1 and s["last_updated"]


# ── the dashboard editor (real JS, headless browser, mocked API) ──────────────────────────────────────────────────────────
pw_mod = pytest.importorskip("playwright.sync_api")
import test_translations_dashboard_ui as UI                                      # noqa: E402
from test_translations_dashboard_ui import _fixture                              # noqa: E402,F401

DOCS = [
    {"slug": "privacy_policy", "key": "doc_privacy_policy", "title": "Privacy Policy", "audience": "customer", "has_english": True, "length": 10, "too_long": False, "source": "policies", "editable_english_here": False, "status": "missing"},
    {"slug": "terms_conditions", "key": "doc_terms_conditions", "title": "Terms & Conditions", "audience": "customer", "has_english": False, "length": 0, "too_long": False, "source": "terms", "editable_english_here": False, "status": "missing"},
    {"slug": "merchant_terms", "key": "doc_merchant_terms", "title": "Merchant Terms & Conditions", "audience": "merchant", "has_english": False, "length": 0, "too_long": False, "source": "terms", "editable_english_here": False, "status": "missing"},
    {"slug": "contact_us", "key": "doc_contact_us", "title": "Contact Us", "audience": "customer", "has_english": False, "length": 0, "too_long": False, "source": "own", "editable_english_here": True, "status": "missing"},
]


def _routes(sent, with_english=False):
    def extra(route, path):
        m = route.request.method
        docs = [dict(d, has_english=True, length=20) if with_english and d["source"] == "terms" else d for d in DOCS]
        if m == "GET" and path.startswith("/documents/"):
            slug = path.split("/documents/")[1].split("?")[0]
            d = [x for x in docs if x["slug"] == slug][0]
            route.fulfill(body=json.dumps({"slug": slug, "key": d["key"], "title": d["title"], "audience": d["audience"], "lang": "ar",
                                           "english": "English of " + slug if d["has_english"] else "", "has_english": d["has_english"], "too_long": False,
                                           "value": None, "status": "missing", "english_changed": False, "max_length": 30000}), content_type="application/json"); return True
        if m == "GET" and path.startswith("/documents"):
            route.fulfill(body=json.dumps({"lang": "ar", "documents": docs, "max_length": 30000}), content_type="application/json"); return True
        if m == "PUT" and path.startswith("/strings/ar/doc_"):
            sent.append(path); route.fulfill(body=json.dumps({"lang": "ar", "key": "doc_x", "track": "documents", "status": "modified"}), content_type="application/json"); return True
    return extra


def _open(p, sent, with_english=False):
    b, pg, calls = UI._page(p, _fixture(), _routes(sent, with_english))
    pg.click("[data-act=tab][data-tab=strings]"); pg.wait_for_selector("tr[data-row=loginTitle]")
    pg.select_option("select[data-change=track]", "documents"); pg.wait_for_selector(".trn-doc-grid, [data-no-english]")
    return b, pg, calls


def test_browser_dropdown_lists_both_terms_even_before_their_english_is_saved():
    with pw_mod.sync_playwright() as p:
        b, pg, calls = _open(p, [])
        opts = pg.locator("select[data-change=doc] option").all_inner_texts()
        assert [o.split(" · ")[0] for o in opts] == ["Privacy Policy", "Terms & Conditions", "Merchant Terms & Conditions"]
        assert opts[1].endswith("No English text yet") and opts[2].endswith("No English text yet") and not opts[0].endswith("No English text yet")
        assert not any("Contact Us" in o for o in opts)   # a type with no storage yet is still only named below
        assert "Contact Us" in pg.inner_text("#trn_pane_strings")
        pg.select_option("select[data-change=doc]", "terms_conditions"); pg.wait_for_selector("[data-no-english]")
        assert "no English text saved yet" in pg.inner_text("[data-no-english]")
        assert pg.locator("textarea.trn-doc-val").count() == 0 and pg.locator("[data-act=savedoc]").count() == 0   # nothing to translate, nothing to save
        b.close()


def test_browser_both_terms_open_and_save_through_the_existing_workflow_once_english_exists():
    with pw_mod.sync_playwright() as p:
        sent = []
        b, pg, calls = _open(p, sent, with_english=True)
        opts = pg.locator("select[data-change=doc] option").all_inner_texts()
        assert [o.split(" · ")[0] for o in opts] == ["Privacy Policy", "Terms & Conditions", "Merchant Terms & Conditions"]
        for slug in ("terms_conditions", "merchant_terms"):
            pg.select_option("select[data-change=doc]", slug); pg.wait_for_function("document.querySelector('textarea.trn-doc-en').value === 'English of %s'" % slug)
            assert pg.locator("textarea.trn-doc-en").get_attribute("readonly") is not None
            pg.fill("textarea.trn-doc-val", "ترجمة"); pg.click("[data-act=savedoc]"); pg.wait_for_selector("[data-doc-status]:has-text('Modified')")
        assert sent == ["/strings/ar/doc_terms_conditions", "/strings/ar/doc_merchant_terms"]
        b.close()
