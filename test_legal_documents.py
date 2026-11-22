"""Stage 2: Legal & Information Documents - the document list, the English-read-only / translation editor API, the public endpoints and
the dashboard editor. Run:  python -m pytest test_legal_documents.py -q -W ignore
"""
import copy
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import merchant_app as MA
from routers import public as PUB
from routers import translations_admin as TA
from routers import translations_public as TP
from services import legal_documents as LD
from services import translations as T
from translation_test_support import (DB, add_ar, audit_actions, client as api, editor, make_user, publish, put, setup_env, super_admin,
                                      sync_fixture_keys, viewer, fake_super, wipe_translation_data)

app = FastAPI()
app.include_router(TA.router, prefix="/admin")
app.include_router(TP.router)
app.include_router(PUB.router)
app.include_router(MA.router, prefix="/merchant")
web = TestClient(app)

PRIVACY = "# Privacy Policy\n\nWe respect your privacy.\n\n## Data we collect\n- Name\n- Phone number\n\nContact privacy@offro.example or see https://offro.example/privacy for details.\n"
TERMS = "# Terms & Conditions\n\n## 1. Acceptance\nBy using Offro you agree to these terms. Settings like {braces} are literal.\n"
ARABIC_PRIVACY = "# سياسة الخصوصية\n\nنحن نحترم خصوصيتك.\n\n## البيانات التي نجمعها\n- الاسم\n- رقم الهاتف\n\nتواصل مع privacy@offro.example أو راجع https://offro.example/privacy لمزيد من التفاصيل."

CATALOG_SLUGS = ["about_us", "contact_us", "privacy_policy", "terms_conditions", "refund_policy", "cancellation_policy", "disclaimer",
                 "faq", "how_offro_works", "merchant_terms", "merchant_policy", "kyc_policy"]


@pytest.fixture(autouse=True)
def staging(monkeypatch):
    setup_env(monkeypatch, "staging")
    sync_fixture_keys()


@pytest.fixture
def seeded():
    """The documents that already exist in the app's existing collections (never copied anywhere by Stage 2)."""
    DB.policies.insert_one({"type": "privacy", "content": PRIVACY})
    DB.policies.insert_one({"type": "refund", "content": "# Refund Policy\n\nRefunds are processed in 7 days."})
    DB.policies.insert_one({"type": "kyc", "content": "# KYC Policy\n\nMerchants must verify identity."})
    DB.terms.insert_one({"type": "user", "content": TERMS})
    DB.terms.insert_one({"type": "merchant", "content": "# Merchant Terms\n\nMerchants must follow the rules."})
    DB.settings.insert_one({"key": "about_us", "content": "# About Offro\n\nOffro helps you save."})


def snapshot_sources():
    return [copy.deepcopy(list(DB[c].find({}, {"_id": 0}))) for c in ("policies", "terms", "settings", LD.OWN)]


def ar(sa):
    assert add_ar(sa).status_code == 200


def doc(headers, slug, lang="ar"):
    r = api.get(f"/admin/translations/documents/{slug}", headers=headers, params={"lang": lang})
    assert r.status_code == 200, r.text
    return r.json()


def save(headers, slug, value, lang="ar"):
    return put(headers, "doc_" + slug, value, lang)


def listing(headers, lang="ar"):
    r = api.get("/admin/translations/documents", headers=headers, params={"lang": lang})
    assert r.status_code == 200, r.text
    return {d["slug"]: d for d in r.json()["documents"]}


# ── every document type is known; existing documents are used where they already are ───────────────────────────────────────
class TestDocumentList:
    def test_every_requested_type_is_in_the_list(self):
        sa = super_admin(); ar(sa)
        assert set(CATALOG_SLUGS) <= set(listing(sa))

    def test_types_without_english_are_listed_but_not_translatable(self):
        sa = super_admin(); ar(sa)
        rows = listing(sa)
        for slug in ("contact_us", "cancellation_policy", "disclaimer", "faq", "how_offro_works", "merchant_policy"):
            assert rows[slug]["has_english"] is False
            assert api.get(f"/admin/translations/documents/{slug}", headers=sa, params={"lang": "ar"}).json()["has_english"] is False
            assert DB.translation_keys.find_one({"key": "doc_" + slug, "deprecated": {"$ne": True}}) is None

    @pytest.mark.parametrize("slug,coll,sel,expected", [
        ("privacy_policy", "policies", {"type": "privacy"}, PRIVACY),
        ("refund_policy", "policies", {"type": "refund"}, "# Refund Policy\n\nRefunds are processed in 7 days."),
        ("kyc_policy", "policies", {"type": "kyc"}, "# KYC Policy\n\nMerchants must verify identity."),
        ("terms_conditions", "terms", {"type": "user"}, TERMS),
        ("merchant_terms", "terms", {"type": "merchant"}, "# Merchant Terms\n\nMerchants must follow the rules."),
        ("about_us", "settings", {"key": "about_us"}, "# About Offro\n\nOffro helps you save."),
    ])
    def test_existing_documents_are_read_from_where_they_already_live(self, seeded, slug, coll, sel, expected):
        sa = super_admin(); ar(sa)
        d = doc(sa, slug)
        assert d["english"] == expected and d["has_english"] and d["value"] is None and d["status"] == "missing"
        assert DB[coll].count_documents(sel) == 1                      # nothing was duplicated or moved
        assert DB[LD.OWN].count_documents({}) == 0

    def test_policy_defaults_shown_to_customers_are_the_english_source(self):
        sa = super_admin(); ar(sa)                                     # nothing stored: the built-in default is what customers see
        assert doc(sa, "privacy_policy")["english"] == PUB._default_privacy()
        assert web.get("/policy/privacy").json()["content"] == PUB._default_privacy()

    def test_other_policy_types_in_the_database_are_not_hidden(self, seeded):
        DB.policies.insert_one({"type": "safety", "content": "# Safety\n\nStay safe."})
        sa = super_admin(); ar(sa)
        rows = listing(sa)
        assert rows["policy_safety"]["has_english"] and rows["policy_safety"]["source"] == "policies"
        assert doc(sa, "policy_safety")["english"] == "# Safety\n\nStay safe."

    def test_listing_does_not_modify_any_source(self, seeded):
        sa = super_admin(); ar(sa)
        before = snapshot_sources()
        listing(sa); doc(sa, "privacy_policy")
        assert snapshot_sources() == before

    def test_list_status_per_language(self, seeded):
        sa = super_admin(); ar(sa)
        assert listing(sa)["privacy_policy"]["status"] == "missing"
        save(sa, "privacy_policy", ARABIC_PRIVACY)
        assert listing(sa)["privacy_policy"]["status"] == "modified"

    def test_oversized_english_is_flagged_not_silently_cut(self):
        DB.policies.insert_one({"type": "privacy", "content": "x" * (T.DOCUMENT_MAX_LENGTH + 1)})
        sa = super_admin(); ar(sa)
        row = listing(sa)["privacy_policy"]
        assert row["too_long"] is True
        assert doc(sa, "privacy_policy")["too_long"] is True
        assert save(sa, "privacy_policy", "x").status_code == 404      # no registry key, so nothing to edit


# ── loading English and the selected language ──────────────────────────────────────────────────────────────────────────
class TestLoading:
    def test_english_and_translation_load_for_the_selected_language(self, seeded):
        sa = super_admin(); ar(sa)
        save(sa, "privacy_policy", ARABIC_PRIVACY)
        d = doc(sa, "privacy_policy", "ar")
        assert d["english"] == PRIVACY and d["value"] == ARABIC_PRIVACY and d["lang"] == "ar" and d["max_length"] == T.DOCUMENT_MAX_LENGTH

    def test_unknown_document_and_language(self, seeded):
        sa = super_admin(); ar(sa)
        assert api.get("/admin/translations/documents/nope", headers=sa, params={"lang": "ar"}).status_code == 404
        assert api.get("/admin/translations/documents/privacy_policy", headers=sa, params={"lang": "xx"}).status_code == 404

    def test_formatting_survives_a_round_trip_exactly(self, seeded):
        sa = super_admin(); ar(sa)
        text = "# عنوان\n\nفقرة أولى.\n\nفقرة ثانية مع [رابط](https://offro.example/privacy).\n\n1. أول\n2. ثاني\n\n- نقطة\n- نقطة أخرى\n\n## قسم فرعي\nنص {literal}"
        assert save(sa, "privacy_policy", text).status_code == 200
        assert doc(sa, "privacy_policy")["value"] == text

    def test_a_long_document_is_accepted_up_to_the_limit(self, seeded):
        sa = super_admin(); ar(sa)
        para = "فقرة طويلة من النص القانوني.\n\n"
        text = (para * 3000)[:T.DOCUMENT_MAX_LENGTH].strip()
        assert 20000 < len(text) <= T.DOCUMENT_MAX_LENGTH
        assert save(sa, "privacy_policy", text).status_code == 200
        r = save(sa, "privacy_policy", text + "x" * (T.DOCUMENT_MAX_LENGTH))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "too_long"

    def test_ordinary_strings_keep_their_original_limit(self):
        sa = super_admin(); ar(sa)
        r = put(sa, "loginTitle", "x" * 2001)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "too_long"
        r = put(sa, "otpSent", "رمز {phone}")
        assert r.status_code == 200
        assert put(sa, "otpSent", "رمز {other}").json()["detail"]["code"] == "placeholder_mismatch"   # placeholders still enforced for strings


# ── English can never be changed through the translation interface ─────────────────────────────────────────────────────
class TestEnglishIsReadOnly:
    def test_english_language_cannot_be_written(self, seeded):
        sa = super_admin(); ar(sa)
        before = snapshot_sources()
        r = put(sa, "doc_privacy_policy", "changed", lang="en")
        assert r.status_code == 404
        assert api.get("/admin/translations/documents/privacy_policy", headers=sa, params={"lang": "en"}).status_code == 404
        assert snapshot_sources() == before and DB.translation_values.count_documents({}) == 0

    def test_saving_a_translation_never_changes_the_english(self, seeded):
        sa = super_admin(); ar(sa)
        listing(sa)
        before = snapshot_sources()
        key_before = DB.translation_keys.find_one({"key": "doc_privacy_policy"})["english"]
        save(sa, "privacy_policy", ARABIC_PRIVACY)
        assert snapshot_sources() == before
        assert DB.translation_keys.find_one({"key": "doc_privacy_policy"})["english"] == key_before == PRIVACY

    def test_translation_endpoints_have_no_way_to_set_english(self, seeded):
        sa = super_admin(); ar(sa)
        before = snapshot_sources()
        for body in ({"value": ARABIC_PRIVACY, "english": "hack"}, {"value": ARABIC_PRIVACY, "content": "hack"}):
            api.put("/admin/translations/strings/ar/doc_privacy_policy", headers=sa, json=body)
        assert snapshot_sources() == before
        assert doc(sa, "privacy_policy")["english"] == PRIVACY
        # the document-english route refuses documents whose English lives elsewhere
        r = api.put("/admin/translations/documents/privacy_policy/english", headers=sa, json={"content": "hack"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_editable_here"
        assert snapshot_sources() == before

    def test_creating_a_type_that_already_exists_does_not_replace_it(self, seeded):
        sa = super_admin(); ar(sa)
        before = snapshot_sources()
        for slug in ("privacy_policy", "terms_conditions", "about_us", "merchant_terms", "kyc_policy"):
            r = api.post("/admin/translations/documents", headers=sa, json={"slug": slug, "title": "X", "content": "replaced"})
            assert r.status_code == 409 and r.json()["detail"]["code"] == "document_exists"
        assert snapshot_sources() == before

    def test_english_edit_route_is_super_admin_only(self):
        sa, ed = super_admin(), editor()
        assert api.post("/admin/translations/documents", headers=sa, json={"slug": "faq", "title": "FAQ", "content": "Q?"}).status_code == 200
        for h in (ed, viewer("v1"), fake_super("f1")):
            assert api.put("/admin/translations/documents/faq/english", headers=h, json={"content": "x"}).status_code == 403
            assert api.post("/admin/translations/documents", headers=h, json={"slug": "disclaimer", "title": "D", "content": "x"}).status_code == 403
        assert DB[LD.OWN].find_one({"slug": "faq"})["content"] == "Q?"


# ── status: Stage 1 rule, for every language, no separate mechanism ────────────────────────────────────────────────────
class TestStatus:
    def test_edit_is_modified_publish_is_published_restore_returns_to_published(self, seeded):
        sa = super_admin(); ar(sa)
        assert save(sa, "privacy_policy", ARABIC_PRIVACY).json()["status"] == "modified"
        assert doc(sa, "privacy_policy")["status"] == "modified"
        assert publish(sa, track="documents").status_code == 200
        assert doc(sa, "privacy_policy")["status"] == "published"
        assert save(sa, "privacy_policy", ARABIC_PRIVACY + "\nإضافة").json()["status"] == "modified"
        assert doc(sa, "privacy_policy")["status"] == "modified"
        assert save(sa, "privacy_policy", ARABIC_PRIVACY).json()["status"] == "published"          # exact published text restored
        assert doc(sa, "privacy_policy")["status"] == "published"
        assert listing(sa)["privacy_policy"]["status"] == "published"

    def test_uses_the_shared_rule_and_adds_no_status_field_of_its_own(self, seeded):
        sa = super_admin(); ar(sa)
        save(sa, "privacy_policy", ARABIC_PRIVACY)
        v = DB.translation_values.find_one({"key": "doc_privacy_policy"})
        assert v["track"] == "documents" and v["status"] == "modified"
        assert not any(k for k in v if "legal_status" in k or k == "doc_status")
        assert T.effective_status({}, "doc_privacy_policy", v["value"]) == "modified"
        publish(sa, track="documents")
        assert T.effective_status(T.published_strings("documents", "ar"), "doc_privacy_policy", v["value"]) == "published"

    def test_publish_follows_the_existing_workflow(self, seeded):
        sa, ed = super_admin(), editor()
        ar(sa)
        save(ed, "privacy_policy", ARABIC_PRIVACY)
        assert publish(ed, track="documents").status_code == 403                   # an editor cannot publish
        assert publish(sa, track="documents").json()["version"] == 1
        assert DB.translation_versions.count_documents({"track": "documents", "lang": "ar"}) == 1
        assert publish(sa, track="documents").json()["detail"]["code"] == "nothing_to_publish"
        assert DB.translation_versions.count_documents({"track": "app"}) == 0      # the app track is untouched

    @pytest.mark.parametrize("code,name,native,direction", [("ar", "Arabic", "العربية", "rtl"), ("hi", "Hindi", "हिन्दी", "ltr"),
                                                              ("fr", "French", "Français", "ltr")])
    def test_every_language_works_the_same_way(self, seeded, code, name, native, direction):
        sa = super_admin()
        assert api.post("/admin/translations/languages", headers=sa, json={"code": code, "name_en": name, "name_native": native, "direction": direction}).status_code == 200
        text = f"# {native}\n\n{native} text.\nContact privacy@offro.example and https://offro.example/privacy."
        assert save(sa, "privacy_policy", text, code).json()["status"] == "modified"
        assert publish(sa, lang=code, track="documents").status_code == 200
        assert doc(sa, "privacy_policy", code)["status"] == "published"
        assert save(sa, "privacy_policy", text + "!", code).json()["status"] == "modified"
        assert save(sa, "privacy_policy", text, code).json()["status"] == "published"

    def test_languages_are_stored_and_published_separately(self, seeded):
        sa = super_admin(); ar(sa)
        api.post("/admin/translations/languages", headers=sa, json={"code": "fr", "name_en": "French", "name_native": "Français", "direction": "ltr"})
        save(sa, "privacy_policy", ARABIC_PRIVACY, "ar")
        assert doc(sa, "privacy_policy", "fr")["value"] is None
        publish(sa, lang="ar", track="documents")
        assert doc(sa, "privacy_policy", "ar")["status"] == "published" and doc(sa, "privacy_policy", "fr")["status"] == "missing"
        assert DB.translation_versions.count_documents({"track": "documents", "lang": "fr"}) == 0

    def test_english_changed_after_translating_is_flagged(self, seeded):
        sa = super_admin(); ar(sa)
        save(sa, "privacy_policy", ARABIC_PRIVACY)
        assert doc(sa, "privacy_policy")["english_changed"] is False
        DB.policies.update_one({"type": "privacy"}, {"$set": {"content": PRIVACY + "\nNew clause."}})
        d = doc(sa, "privacy_policy")
        assert d["english_changed"] is True and d["english"].endswith("New clause.") and d["value"] == ARABIC_PRIVACY


# ── validation is the existing validation ──────────────────────────────────────────────────────────────────────────────
class TestValidation:
    @pytest.mark.parametrize("bad,code", [
        ("<b>bold</b> text", "html_not_allowed"),
        ("نص &amp; نص", "html_not_allowed"),
        ("   ", "empty_value"),
        ("نص‮مخفي", "hidden_or_bidi_control_character"),
        ("نص\x07", "control_character"),
        ("اتصل https://evil.example/phish", "url_email_or_phone_not_in_english"),
        ("اتصل evil@evil.example", "url_email_or_phone_not_in_english"),
        ("اتصل +91 98765 43210", "url_email_or_phone_not_in_english"),
    ])
    def test_unsafe_translations_are_rejected(self, seeded, bad, code):
        sa = super_admin(); ar(sa)
        r = save(sa, "privacy_policy", bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == code
        assert DB.translation_values.count_documents({"key": "doc_privacy_policy"}) == 0

    def test_publish_revalidates_against_the_current_english(self, seeded):
        sa = super_admin(); ar(sa)
        save(sa, "privacy_policy", ARABIC_PRIVACY)                         # contains privacy@offro.example
        DB.policies.update_one({"type": "privacy"}, {"$set": {"content": "# Privacy\n\nNo contact details now."}})
        r = publish(sa, track="documents")
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_values"
        assert DB.translation_versions.count_documents({"track": "documents"}) == 0


# ── authentication and authorization ───────────────────────────────────────────────────────────────────────────────────
class TestAuth:
    def test_no_login_is_refused(self, seeded):
        for method, url in (("get", "/admin/translations/documents?lang=ar"), ("get", "/admin/translations/documents/privacy_policy?lang=ar"),
                            ("put", "/admin/translations/strings/ar/doc_privacy_policy"), ("post", "/admin/translations/documents"),
                            ("put", "/admin/translations/documents/faq/english")):
            r = getattr(api, method)(url, **({"json": {"value": "x"}} if method != "get" else {}))
            assert r.status_code in (401, 403), (method, url, r.status_code)

    def test_viewer_can_read_but_not_edit(self, seeded):
        sa, vw = super_admin(), viewer()
        ar(sa)
        assert doc(vw, "privacy_policy")["english"] == PRIVACY and "privacy_policy" in listing(vw)
        assert save(vw, "privacy_policy", ARABIC_PRIVACY).status_code == 403

    def test_a_role_without_the_module_cannot_read(self, seeded):
        sa = super_admin(); ar(sa)
        nobody = make_user("n1", perms={})
        assert api.get("/admin/translations/documents", headers=nobody, params={"lang": "ar"}).status_code == 403
        assert api.get("/admin/translations/documents/privacy_policy", headers=nobody, params={"lang": "ar"}).status_code == 403

    def test_editor_can_save_and_a_renamed_super_admin_cannot_publish(self, seeded):
        sa, ed, fk = super_admin(), editor(), fake_super()
        ar(sa)
        assert save(ed, "privacy_policy", ARABIC_PRIVACY).status_code == 200
        assert publish(fk, track="documents").status_code == 403
        assert publish(ed, track="documents").status_code == 403

    def test_saves_and_denials_are_audited_without_the_text(self, seeded):
        sa, vw = super_admin(), viewer()
        ar(sa)
        save(sa, "privacy_policy", ARABIC_PRIVACY)
        save(vw, "privacy_policy", ARABIC_PRIVACY)
        assert "edit" in audit_actions("ok") and any(a.startswith("denied:edit") for a in audit_actions("denied"))
        assert "سياسة" not in str(list(DB.translation_audit.find({})))


# ── environment isolation ──────────────────────────────────────────────────────────────────────────────────────────────
class TestEnvironment:
    def test_unidentified_environment_refuses_every_change(self, seeded, monkeypatch):
        sa = super_admin(); ar(sa)
        setup_env(monkeypatch, None)
        assert save(sa, "privacy_policy", ARABIC_PRIVACY).status_code == 503
        assert api.post("/admin/translations/documents", headers=sa, json={"slug": "faq", "title": "F", "content": "Q"}).status_code == 503
        assert publish(sa, track="documents").status_code == 503
        assert DB.translation_values.count_documents({}) == 0 and DB[LD.OWN].count_documents({}) == 0
        assert api.get("/admin/translations/documents", headers=sa, params={"lang": "ar"}).status_code == 200     # reading still works

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_both_environments_work_on_their_own_database_only(self, seeded, monkeypatch, env):
        setup_env(monkeypatch, env)
        sync_fixture_keys()
        sa = super_admin(); ar(sa)
        assert save(sa, "privacy_policy", ARABIC_PRIVACY).status_code == 200
        v = publish(sa, track="documents").json()
        assert DB.translation_versions.find_one({"track": "documents", "lang": "ar"})["environment"] == env
        # a different environment has a different (here: empty) database and sees nothing of it
        wipe_translation_data()
        assert DB.translation_values.count_documents({}) == 0 and DB.translation_versions.count_documents({}) == 0
        assert v["version"] == 1

    def test_translation_store_has_no_cross_environment_path(self):
        import inspect
        src = inspect.getsource(LD)
        for forbidden in ("requests", "httpx", "MONGO_URL", "urllib", "os.environ"):
            assert forbidden not in src


# ── document types added by an administrator ───────────────────────────────────────────────────────────────────────────
class TestAddingDocuments:
    def test_super_admin_adds_a_catalog_type_that_has_no_storage_yet(self):
        sa = super_admin(); ar(sa)
        r = api.post("/admin/translations/documents", headers=sa, json={"slug": "faq", "title": "FAQ", "content": "# FAQ\n\n**Q:** How?\n\nA: Like this.", "audience": "customer"})
        assert r.status_code == 200 and r.json()["key"] == "doc_faq"
        assert listing(sa)["faq"]["has_english"] is True
        assert doc(sa, "faq")["english"].startswith("# FAQ")
        assert save(sa, "faq", "# الأسئلة الشائعة\n\nسؤال؟").json()["status"] == "modified"
        assert "create_document" in audit_actions("started") and "create_document" in audit_actions("ok")

    def test_a_brand_new_type_needs_no_code_change(self):
        sa = super_admin(); ar(sa)
        r = api.post("/admin/translations/documents", headers=sa, json={"slug": "community_guidelines", "title": "Community Guidelines", "content": "Be kind."})
        assert r.status_code == 200
        row = listing(sa)["community_guidelines"]
        assert row["title"] == "Community Guidelines" and row["source"] == "own" and row["has_english"]
        assert web.get("/legal/community_guidelines").json()["content"] == "Be kind."
        assert {"slug": "community_guidelines", "title": "Community Guidelines", "audience": "customer"} in web.get("/legal/documents").json()["documents"]

    def test_duplicates_and_invalid_input_are_refused(self):
        sa = super_admin(); ar(sa)
        ok = {"slug": "faq", "title": "FAQ", "content": "Q"}
        assert api.post("/admin/translations/documents", headers=sa, json=ok).status_code == 200
        assert api.post("/admin/translations/documents", headers=sa, json=ok).status_code == 409
        for bad in ({"slug": "Bad Slug", "title": "T", "content": "x"}, {"slug": "../x", "title": "T", "content": "x"},
                    {"slug": "ok_slug", "title": "", "content": "x"}, {"slug": "ok_slug", "title": "T", "content": " "},
                    {"slug": "ok_slug", "title": "T", "content": "x" * (T.DOCUMENT_MAX_LENGTH + 1)}):
            assert api.post("/admin/translations/documents", headers=sa, json=bad).status_code == 422
        assert DB[LD.OWN].count_documents({}) == 1

    def test_english_of_an_added_document_can_be_updated_by_super_admin(self):
        sa = super_admin(); ar(sa)
        api.post("/admin/translations/documents", headers=sa, json={"slug": "faq", "title": "FAQ", "content": "Q1"})
        save(sa, "faq", "س1")
        assert api.put("/admin/translations/documents/faq/english", headers=sa, json={"content": "Q1\nQ2"}).status_code == 200
        d = doc(sa, "faq")
        assert d["english"] == "Q1\nQ2" and d["value"] == "س1" and d["english_changed"] is True

    def test_arb_key_sync_does_not_deprecate_documents(self, seeded):
        sa = super_admin(); ar(sa)
        listing(sa)
        sync_fixture_keys()                                                # tools/sync_translation_keys --apply equivalent
        assert DB.translation_keys.find_one({"key": "doc_privacy_policy"}).get("deprecated") is not True

    def test_a_document_with_no_english_left_is_deprecated_and_not_editable(self, seeded):
        sa = super_admin(); ar(sa)
        listing(sa)
        DB.settings.update_one({"key": "about_us"}, {"$set": {"content": ""}})
        listing(sa)
        assert DB.translation_keys.find_one({"key": "doc_about_us"})["deprecated"] is True
        assert save(sa, "about_us", "نص").status_code == 409


# ── the editor API integrates with the rest of the translation module ──────────────────────────────────────────────────
class TestIntegration:
    def test_short_legal_strings_still_work_and_documents_are_not_in_that_track(self, seeded):
        sa = super_admin(); ar(sa)
        listing(sa)                                                              # registers the document keys
        for track, present in (("legal", {"privacyIntro", "whatsappConsent"}), ("app", {"loginTitle"})):
            keys = {r["key"] for r in api.get("/admin/translations/strings", headers=sa, params={"lang": "ar", "track": track}).json()["rows"]}
            assert present <= keys and not any(k.startswith("doc_") for k in keys)
        assert put(sa, "privacyIntro", "مقدمة").status_code == 200
        legal = api.get("/admin/translations/draft", headers=sa, params={"lang": "ar", "track": "legal"})
        from openpyxl import load_workbook
        import io
        assert sorted(r[0].value for r in load_workbook(io.BytesIO(legal.content))["Translations"].iter_rows(min_row=2)) == ["privacyIntro", "whatsappConsent"]

    def test_documents_are_their_own_track_and_never_reach_the_app_files(self, seeded):
        sa = super_admin(); ar(sa)
        save(sa, "privacy_policy", ARABIC_PRIVACY); publish(sa, track="documents")
        publish_app = publish(sa, track="app")                                   # nothing to publish in the app track
        assert publish_app.status_code in (409, 422)
        assert web.get("/translations/ar", params={"track": "documents"}).status_code == 404
        assert "documents" not in str(web.get("/translations/manifest").json())
        assert web.get("/translations/manifest").json()["languages"] == []        # a language with only documents has no override files
        st = api.get("/admin/translations/languages", headers=sa).json()["languages"][0]
        assert set(st["status_indicators"]) == {"app", "legal"} and set(st["last_updated"]) == {"app", "legal"}

    def test_transfer_package_never_carries_documents(self, seeded):
        import json
        from tools import transfer_translations as X
        sa = super_admin(); ar(sa)
        put(sa, "loginTitle", "تسجيل الدخول"); publish(sa, track="app")
        save(sa, "privacy_policy", ARABIC_PRIVACY); publish(sa, track="documents")
        pkg = X.export_package(DB, "staging")
        text = json.dumps(pkg, ensure_ascii=False)
        assert "doc_" not in text and "privacy@" not in text and ARABIC_PRIVACY[:20] not in text
        assert [v["track"] for v in pkg["data"]["versions"]] == ["app"] and X.validate_package(pkg) == []
        assert all(k["group"] in ("app", "legal") for k in pkg["data"]["keys"])

    def test_long_documents_do_not_break_the_transfer_export(self):
        import json
        from tools import transfer_translations as X
        DB.policies.insert_one({"type": "privacy", "content": "نص " * 6000})
        sa = super_admin(); ar(sa)
        put(sa, "loginTitle", "تسجيل الدخول"); publish(sa, track="app")
        save(sa, "privacy_policy", "ن " * 6000); publish(sa, track="documents")
        assert X.validate_package(X.export_package(DB, "staging")) == []

    def test_download_draft_and_import_work_for_documents(self, seeded):
        import io
        from openpyxl import load_workbook
        sa = super_admin(); ar(sa)
        r = api.get("/admin/translations/draft", headers=sa, params={"lang": "ar", "track": "documents"})
        assert r.status_code == 200
        ws = load_workbook(io.BytesIO(r.content))["Translations"]
        keys = [row[0].value for row in ws.iter_rows(min_row=2)]
        assert "doc_privacy_policy" in keys
        for row in ws.iter_rows(min_row=2):
            if row[0].value == "doc_privacy_policy":
                row[3].value = ARABIC_PRIVACY
        buf = io.BytesIO(); ws.parent.save(buf)
        res = api.post("/admin/translations/import", headers=sa, data={"lang": "ar", "track": "documents", "apply": "1"},
                       files={"file": ("d.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert res.status_code == 200 and res.json()["updated"] == 1, res.text
        assert doc(sa, "privacy_policy")["value"] == ARABIC_PRIVACY

    def test_app_track_and_its_publish_are_unaffected(self, seeded):
        sa = super_admin(); ar(sa)
        put(sa, "loginTitle", "تسجيل الدخول")
        save(sa, "privacy_policy", ARABIC_PRIVACY)
        publish(sa, track="app")
        assert "doc_privacy_policy" not in T.published_strings("app", "ar")
        assert DB.translation_versions.count_documents({"track": "documents"}) == 0


# ── what the customer app receives ─────────────────────────────────────────────────────────────────────────────────────
class TestPublicEndpoints:
    def published(self, sa):
        save(sa, "privacy_policy", ARABIC_PRIVACY)
        assert publish(sa, track="documents").status_code == 200

    def test_without_lang_the_existing_responses_are_unchanged(self, seeded):
        sa = super_admin(); ar(sa); self.published(sa)
        assert web.get("/policy/privacy").json() == {"content": PRIVACY}
        assert web.get("/terms/user").json() == {"type": "user", "content": TERMS}
        assert web.get("/about").json() == {"content": "# About Offro\n\nOffro helps you save."}

    def test_the_selected_language_gets_the_published_translation(self, seeded):
        sa = super_admin(); ar(sa); self.published(sa)
        r = web.get("/policy/privacy", params={"lang": "ar"}).json()
        assert r == {"content": ARABIC_PRIVACY, "lang": "ar", "translated": True, "direction": "rtl"}

    def test_english_fallback_when_there_is_no_translation(self, seeded):
        sa = super_admin(); ar(sa); self.published(sa)
        r = web.get("/terms/user", params={"lang": "ar"}).json()                 # terms were not translated
        assert r["content"] == TERMS and r["translated"] is False and r["lang"] == "en"
        assert web.get("/about", params={"lang": "ar"}).json()["translated"] is False
        assert web.get("/policy/privacy", params={"lang": "fr"}).json()["translated"] is False      # language not added
        assert web.get("/policy/privacy", params={"lang": "en"}).json()["content"] == PRIVACY
        assert web.get("/policy/privacy", params={"lang": "zzzz-bad"}).json()["content"] == PRIVACY

    def test_drafts_are_never_served(self, seeded):
        sa = super_admin(); ar(sa); self.published(sa)
        save(sa, "privacy_policy", ARABIC_PRIVACY + "\nمسودة غير منشورة")
        assert web.get("/policy/privacy", params={"lang": "ar"}).json()["content"] == ARABIC_PRIVACY
        save(sa, "terms_conditions", "مسودة فقط")
        assert web.get("/terms/user", params={"lang": "ar"}).json()["translated"] is False

    def test_disabled_language_and_kill_switch_fall_back_to_english(self, seeded):
        sa = super_admin(); ar(sa); self.published(sa)
        assert api.post("/admin/translations/kill-switch", headers=sa, json={"enabled": True, "reason": "test"}).status_code == 200
        assert web.get("/policy/privacy", params={"lang": "ar"}).json()["content"] == PRIVACY
        api.post("/admin/translations/kill-switch", headers=sa, json={"enabled": False, "reason": "test"})
        api.patch("/admin/translations/languages/ar", headers=sa, json={"status": "disabled"})
        assert web.get("/policy/privacy", params={"lang": "ar"}).json()["translated"] is False

    def test_regional_language_codes_fall_back_to_the_base_language(self, seeded):
        sa = super_admin(); ar(sa); self.published(sa)
        assert web.get("/policy/privacy", params={"lang": "ar-SA"}).json()["translated"] is True
        assert web.get("/policy/privacy", params={"lang": "ar_SA"}).json()["translated"] is True

    def test_other_existing_urls_follow_the_same_rules(self, seeded):
        sa = super_admin(); ar(sa)
        save(sa, "merchant_terms", "# شروط التاجر"); save(sa, "kyc_policy", "# سياسة KYC"); save(sa, "about_us", "# من نحن")
        save(sa, "refund_policy", "# سياسة الاسترداد"); save(sa, "terms_conditions", "# الشروط")
        publish(sa, track="documents")
        assert web.get("/merchant/terms", params={"lang": "ar"}).json() == {"content": "# شروط التاجر", "lang": "ar", "translated": True, "direction": "rtl"}
        assert web.get("/merchant/terms").json() == {"content": "# Merchant Terms\n\nMerchants must follow the rules."}
        assert web.get("/policy/kyc", params={"lang": "ar"}).json()["content"] == "# سياسة KYC"
        assert web.get("/policy/refund", params={"lang": "ar"}).json()["content"] == "# سياسة الاسترداد"
        assert web.get("/about", params={"lang": "ar"}).json()["content"] == "# من نحن"
        assert web.get("/terms/user", params={"lang": "ar"}).json()["content"] == "# الشروط"
        assert web.get("/terms/merchant", params={"lang": "ar"}).json()["content"] == "# شروط التاجر"

    def test_merchant_default_text_is_unchanged_when_nothing_is_stored(self):
        assert web.get("/merchant/terms").json() == {"content": "Merchant terms and conditions will be posted here."}
        assert web.get("/merchant/terms", params={"lang": "ar"}).json()["content"] == "Merchant terms and conditions will be posted here."

    def test_generic_document_endpoint(self, seeded):
        sa = super_admin(); ar(sa); self.published(sa)
        r = web.get("/legal/privacy_policy", params={"lang": "ar"}).json()
        assert r["content"] == ARABIC_PRIVACY and r["translated"] and r["slug"] == "privacy_policy" and r["title"] == "Privacy Policy"
        assert web.get("/legal/privacy_policy").json()["content"] == PRIVACY
        assert web.get("/legal/faq").status_code == 404                           # no English text yet
        assert web.get("/legal/BAD..slug").status_code == 404
        slugs = {d["slug"] for d in web.get("/legal/documents").json()["documents"]}
        assert {"privacy_policy", "terms_conditions", "about_us"} <= slugs and "faq" not in slugs

    def test_public_endpoints_need_no_login_and_expose_no_translation_internals(self, seeded):
        sa = super_admin(); ar(sa); self.published(sa)
        r = web.get("/legal/privacy_policy", params={"lang": "ar"})
        assert r.status_code == 200 and set(r.json()) == {"content", "lang", "translated", "direction", "slug", "title"}

    def test_an_added_document_in_two_languages(self):
        sa = super_admin(); ar(sa)
        api.post("/admin/translations/languages", headers=sa, json={"code": "hi", "name_en": "Hindi", "name_native": "हिन्दी", "direction": "ltr"})
        api.post("/admin/translations/documents", headers=sa, json={"slug": "disclaimer", "title": "Disclaimer", "content": "Use at your own risk."})
        save(sa, "disclaimer", "استخدمه على مسؤوليتك", "ar"); save(sa, "disclaimer", "अपने जोखिम पर उपयोग करें", "hi")
        publish(sa, lang="ar", track="documents"); publish(sa, lang="hi", track="documents")
        assert web.get("/legal/disclaimer", params={"lang": "hi"}).json()["content"] == "अपने जोखिम पर उपयोग करें"
        assert web.get("/legal/disclaimer", params={"lang": "ar"}).json()["direction"] == "rtl"


# ── dashboard editor (static checks; the browser behaviour is in test_legal_documents_browser.py) ──────────────────────
class TestDashboardAssets:
    def js(self):
        import os
        return open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "js", "translations.js"), encoding="utf-8").read()

    def css(self):
        import os
        return open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "css", "translations.css"), encoding="utf-8").read()

    def test_editor_markup_and_read_only_english(self):
        js = self.js()
        for needle in ('data-change="doc"', 'trn-doc-en', 'trn-doc-val', 'readonly', "/documents"):
            assert needle in js
        # the English panel is a read-only element and has no save handler
        m = re.search(r"trn-doc-en[^`]*", js)
        assert m and "textarea" in js[js.index("trn-doc-en") - 200: js.index("trn-doc-en") + 300].lower()
        assert "data-act=\"savedoc\"" in js

    def test_responsive_two_column_layout(self):
        css = self.css()
        assert ".trn-doc-grid" in css and "grid-template-columns" in css and "@media" in css

    def test_no_unescaped_server_values(self):
        js = self.js()
        assert "innerHTML" in js and js.count("innerHTML =") <= 1          # only through setHtml


# ── read-only inspection tool ──────────────────────────────────────────────────────────────────────────────────────────
class TestInspectTool:
    def test_report_is_read_only_and_lists_existing_documents(self, seeded):
        import importlib.util, os
        spec = importlib.util.spec_from_file_location("inspect_legal_documents", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "inspect_legal_documents.py"))
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        sa = super_admin(); ar(sa)
        save(sa, "privacy_policy", ARABIC_PRIVACY); publish(sa, track="documents")
        DB.policies.insert_one({"type": "safety", "content": "x"})
        names = ("policies", "terms", "settings", LD.OWN, "translation_keys", "translation_values", "translation_versions")
        before = {c: copy.deepcopy(list(DB[c].find({}))) for c in names}
        rep = mod.inspect()
        assert {c: list(DB[c].find({})) for c in names} == before
        rows = {r["slug"]: r for r in rep["documents"]}
        assert rows["privacy_policy"]["stored_in"] == "policies" and rows["privacy_policy"]["translations"]["published_languages"] == ["ar"]
        assert rows["faq"]["has_english"] is False and "safety" in rep["other_stored_types"]["policies"] and rep["environment"] == "staging"
