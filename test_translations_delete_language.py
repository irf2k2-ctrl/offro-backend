"""Enhancement batch, item 1: deleting a language from Dashboard > Translations > Overview.

What a delete removes (and nothing else): the language record, ITS translation values (every track, incl. legal documents), ITS published
versions and ITS country mapping. Other languages are never touched, English can never be deleted, the audit log is kept.
Backend rules tested here: Super Admin only (a role merely renamed 'Super Admin' is refused), typed confirmation required by the API,
identified environment required, unknown language -> 404, an interrupted delete leaves a DISABLED language that can be deleted again
(never orphaned data), re-adding the code afterwards starts clean.
(file name sorts after test_banner_media.py on purpose.)  Run:  python -m pytest test_translations_delete_language.py -q -W ignore
"""
import pytest

from services import legal_documents as LD
from services import translations as T
from translation_test_support import (DB, add_ar, audit_actions, client as api, editor, fake_super, publish, put, setup_env, super_admin,
                                      sync_fixture_keys, viewer)
from routers import public as PUB   # noqa: F401  (imported so the public document endpoints exist for the app-facing check below)

PRIVACY = "# Privacy Policy\n\nWe respect your privacy.\n"


@pytest.fixture(autouse=True)
def staging(monkeypatch):
    setup_env(monkeypatch, "staging")
    sync_fixture_keys()
    DB.policies.insert_one({"type": "privacy", "content": PRIVACY})


def add_lang(sa, code, en, native, direction="ltr", countries=None):
    r = api.post("/admin/translations/languages", headers=sa,
                 json={"code": code, "name_en": en, "name_native": native, "direction": direction, "countries": countries or []})
    assert r.status_code == 200, r.text


def populate(sa, code, text="x"):
    """A language with app text + a legal document translation, both published, plus a country mapping."""
    assert api.get("/admin/translations/documents", headers=sa, params={"lang": code}).status_code == 200   # registers the document keys
    assert put(sa, "loginTitle", text + "1", code).status_code == 200
    assert put(sa, "doc_privacy_policy", "# " + text, code).status_code == 200
    assert publish(sa, code, "app").status_code == 200
    assert publish(sa, code, "documents").status_code == 200
    assert put(sa, "loginTitle", text + "2", code).status_code == 200                                         # + one draft (Modified) value


def counts(code):
    return {"lang": T.db[T.LANGS].count_documents({"code": code}), "values": T.db[T.VALUES].count_documents({"lang": code}),
            "versions": T.db[T.VERSIONS].count_documents({"lang": code}), "countries": T.db[T.LANG_COUNTRIES].count_documents({"lang": code})}


def delete(headers, code, confirm=None):
    return api.delete(f"/admin/translations/languages/{code}", headers=headers, params={"confirm": code if confirm is None else confirm})


@pytest.fixture
def two_languages():
    sa = super_admin()
    add_lang(sa, "ar", "Arabic", "العربية", "rtl", ["SA"])
    add_lang(sa, "hi", "Hindi", "हिन्दी", "ltr", ["IN"])
    populate(sa, "ar", "a")
    populate(sa, "hi", "h")
    return sa


class TestValidDeletion:
    def test_language_and_its_translations_are_removed(self, two_languages):
        sa = two_languages
        before = counts("ar")
        assert all(v >= 1 for v in before.values()) and before["versions"] == 2           # app + documents versions
        r = delete(sa, "ar")
        assert r.status_code == 200, r.text
        assert r.json()["code"] == "ar" and r.json()["deleted"] == {"values": before["values"], "versions": 2, "country_mapping": 1}
        assert counts("ar") == {"lang": 0, "values": 0, "versions": 0, "countries": 0}     # no orphans anywhere

    def test_other_languages_are_not_affected(self, two_languages):
        sa = two_languages
        hi_before = counts("hi")
        hi_values = sorted((v["key"], v["track"], v["value"], v["status"]) for v in T.db[T.VALUES].find({"lang": "hi"}))
        hi_versions = sorted((v["track"], v["version"], v["hash"]) for v in T.db[T.VERSIONS].find({"lang": "hi"}))
        assert delete(sa, "ar").status_code == 200
        assert counts("hi") == hi_before
        assert sorted((v["key"], v["track"], v["value"], v["status"]) for v in T.db[T.VALUES].find({"lang": "hi"})) == hi_values
        assert sorted((v["track"], v["version"], v["hash"]) for v in T.db[T.VERSIONS].find({"lang": "hi"})) == hi_versions
        assert T.language_countries("hi") == ["IN"] and T.db[T.LANGS].find_one({"code": "hi"})["status"] == "active"

    def test_statuses_of_the_remaining_language_stay_consistent(self, two_languages):
        sa = two_languages
        before = api.get("/admin/translations/documents", headers=sa, params={"lang": "hi"}).json()["documents"]
        assert delete(sa, "ar").status_code == 200
        assert api.get("/admin/translations/documents", headers=sa, params={"lang": "hi"}).json()["documents"] == before
        rows = {r["key"]: r for r in api.get("/admin/translations/strings", headers=sa, params={"lang": "hi"}).json()["rows"]}
        assert rows["loginTitle"]["value"] == "h2" and rows["loginTitle"]["status"] == "modified"

    def test_deleted_language_disappears_from_every_selector_and_public_list(self, two_languages):
        sa = two_languages
        assert delete(sa, "ar").status_code == 200
        langs = api.get("/admin/translations/languages", headers=sa).json()
        assert [l["code"] for l in langs["languages"]] == ["hi"]
        man = api.get("/translations/manifest").json()
        assert [l["code"] for l in man["languages"]] == ["hi"] and "ar" not in man["language_countries"]
        assert api.get("/translations/ar").status_code == 404
        assert api.get("/admin/translations/documents", headers=sa, params={"lang": "ar"}).status_code == 404
        assert api.get("/admin/translations/strings", headers=sa, params={"lang": "ar"}).status_code in (404, 422)

    def test_customers_asking_for_the_deleted_language_get_english(self, two_languages):
        sa = two_languages
        assert api.get("/legal/privacy_policy", params={"lang": "ar"}).json()["translated"] is True
        assert delete(sa, "ar").status_code == 200
        r = api.get("/legal/privacy_policy", params={"lang": "ar"}).json()
        assert r["translated"] is False and r["content"] == PRIVACY and r["lang"] == "en"

    def test_the_audit_history_is_kept_and_the_delete_is_audited(self, two_languages):
        sa = two_languages
        old = T.db["translation_audit"].count_documents({"lang": "ar"})
        assert old > 0
        assert delete(sa, "ar").status_code == 200
        assert T.db["translation_audit"].count_documents({"lang": "ar"}) >= old + 2           # history kept + started/ok entries
        assert audit_actions("started").count("delete_language") == 1 and audit_actions("ok").count("delete_language") == 1

    def test_the_code_can_be_added_again_and_starts_clean(self, two_languages):
        sa = two_languages
        assert delete(sa, "ar").status_code == 200
        add_lang(sa, "ar", "Arabic", "العربية", "rtl")
        assert counts("ar") == {"lang": 1, "values": 0, "versions": 0, "countries": 0}
        assert T.db[T.LANGS].find_one({"code": "ar"})["status"] == "draft"

    def test_a_deleted_draft_language_without_anything_published(self):
        sa = super_admin()
        add_lang(sa, "kn", "Kannada", "ಕನ್ನಡ")
        assert delete(sa, "kn").status_code == 200 and counts("kn") == {"lang": 0, "values": 0, "versions": 0, "countries": 0}


class TestEnglishAndInvalidTargets:
    @pytest.mark.parametrize("code", ["en", "EN", " en "])
    def test_english_can_never_be_deleted(self, code):
        sa = super_admin()
        r = api.delete("/admin/translations/languages/" + code.strip(), headers=sa, params={"confirm": code})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "english_not_deletable"
        assert api.get("/admin/translations/languages/en/delete-impact", headers=sa).status_code == 409
        assert DB[T.LANGS].count_documents({"code": "en"}) == 0                                   # English is a built-in, never a stored record

    def test_the_english_country_mapping_survives_an_attempted_delete(self):
        sa = super_admin()
        assert api.put("/admin/translations/languages/en/countries", headers=sa, json={"countries": ["SA", "IN"]}).status_code == 200
        assert api.delete("/admin/translations/languages/en", headers=sa, params={"confirm": "en"}).status_code == 409
        assert T.language_countries("en") == ["IN", "SA"]

    @pytest.mark.parametrize("code", ["zz", "xx-YY", "..%2Fx", "a b"])
    def test_unknown_or_invalid_language_is_404_and_changes_nothing(self, code):
        sa = super_admin(); add_lang(sa, "ar", "Arabic", "العربية", "rtl")
        before = DB[T.LANGS].count_documents({})
        r = api.delete("/admin/translations/languages/" + code, headers=sa, params={"confirm": code})
        assert r.status_code in (404, 405) and DB[T.LANGS].count_documents({}) == before
        if isinstance(r.json().get("detail"), dict):                                              # a real lookup, not a routing 404
            assert r.json()["detail"]["code"] == "unknown_language"

    def test_deleting_twice_the_second_is_a_clear_404(self):
        sa = super_admin(); add_lang(sa, "ar", "Arabic", "العربية", "rtl")
        assert delete(sa, "ar").status_code == 200
        r = delete(sa, "ar")
        assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_language"


class TestPermissionsAreEnforcedByTheBackend:
    @pytest.mark.parametrize("who", ["editor", "viewer", "fake_super", "anonymous"])
    def test_non_super_admins_cannot_delete_or_preview(self, two_languages, who):
        headers = {"editor": editor, "viewer": viewer, "fake_super": fake_super}.get(who, lambda: {})()
        before = counts("ar")
        r = delete(headers, "ar")
        assert r.status_code in (401, 403)
        assert api.get("/admin/translations/languages/ar/delete-impact", headers=headers).status_code in (401, 403)
        assert counts("ar") == before
        if who != "anonymous":
            assert "denied:delete_language" in audit_actions("denied")

    def test_confirmation_is_required_and_must_match(self, two_languages):
        sa, before = two_languages, counts("ar")
        for bad in ("", "AR", "hi", "ar "):
            r = api.delete("/admin/translations/languages/ar", headers=sa, params={"confirm": bad} if bad != "ar " else {"confirm": "x"})
            assert r.status_code == 400 and r.json()["detail"]["code"] == "confirmation_mismatch"
        assert api.delete("/admin/translations/languages/ar", headers=sa).status_code == 400          # no confirm at all
        assert counts("ar") == before and T.db[T.LANGS].find_one({"code": "ar"})["status"] == "active"

    def test_an_unidentified_environment_refuses_the_delete(self, two_languages, monkeypatch):
        sa, before = two_languages, counts("ar")
        setup_env(monkeypatch, None)
        r = delete(sa, "ar")
        assert r.status_code == 503 and r.json()["detail"]["code"] == "environment_unknown"
        assert counts("ar") == before


class TestImpactPreview:
    def test_counts_match_what_is_deleted_and_nothing_changes(self, two_languages):
        sa = two_languages
        before = counts("ar")
        r = api.get("/admin/translations/languages/ar/delete-impact", headers=sa)
        assert r.status_code == 200
        assert (r.json()["values"], r.json()["versions"], r.json()["country_mapping"], r.json()["name_en"]) == (before["values"], before["versions"], 1, "Arabic")
        assert counts("ar") == before
        assert api.get("/admin/translations/languages/zz/delete-impact", headers=sa).status_code == 404


class TestInterruptedDeleteIsHarmless:
    def test_failure_midway_leaves_a_disabled_language_that_can_be_deleted_again(self, two_languages, monkeypatch):
        sa = two_languages
        hi_before = counts("hi")
        real = T.db[T.VERSIONS].delete_many
        monkeypatch.setattr(T.db[T.VERSIONS], "delete_many", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        r = delete(sa, "ar")
        assert r.status_code == 500 and r.json()["detail"]["code"] == "delete_incomplete"
        lang = T.db[T.LANGS].find_one({"code": "ar"})
        assert lang is not None and lang["status"] == "disabled"                                 # stops being served at once
        assert "ar" not in [l["code"] for l in api.get("/translations/manifest").json()["languages"]]
        assert api.get("/translations/ar").status_code == 404
        assert counts("hi") == hi_before                                                         # still nothing of another language touched
        monkeypatch.setattr(T.db[T.VERSIONS], "delete_many", real)
        assert delete(sa, "ar").status_code == 200
        assert counts("ar") == {"lang": 0, "values": 0, "versions": 0, "countries": 0}

    def test_nothing_is_deleted_when_the_audit_log_cannot_be_written(self, two_languages, monkeypatch):
        sa, before = two_languages, counts("ar")
        monkeypatch.setattr(T.db["translation_audit"], "insert_one", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down")))
        r = delete(sa, "ar")
        assert r.status_code == 503 and r.json()["detail"]["code"] == "audit_unavailable"
        assert counts("ar") == before and T.db[T.LANGS].find_one({"code": "ar"})["status"] == "active"


class TestOverviewRowGetsTheLegalDocumentsStatus:
    def test_languages_api_carries_the_legal_documents_summary(self, two_languages):
        sa = two_languages
        langs = {l["code"]: l for l in api.get("/admin/translations/languages", headers=sa).json()["languages"]}
        s = langs["ar"]["legal_documents"]
        assert s["total"] >= 1 and s["published_version"] == 1 and s["last_updated"]
        assert s["draft_changes"] == 0 and s["missing"] == s["total"] - 1                       # privacy policy published, the rest not translated
        assert langs["ar"]["status_indicators"]["legal"] is not None                            # the old fields are all still there
        put(sa, "doc_privacy_policy", "# changed", "ar")
        langs = {l["code"]: l for l in api.get("/admin/translations/languages", headers=sa).json()["languages"]}
        assert langs["ar"]["legal_documents"]["draft_changes"] == 1 and langs["hi"]["legal_documents"]["draft_changes"] == 0
