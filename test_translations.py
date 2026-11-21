"""Phase G targeted tests: environment helper, validation, registry, permissions, drafts, versions, rollback, legal track, public API,
kill switch, rate limit and audit. Run:  python -m pytest test_translations.py -q -W ignore
Uses the shared in-memory fake database; no network and no real database."""
import inspect
import sys

import pytest

from fastapi.testclient import TestClient

import server
from routers import translations_admin as TA
from routers import translations_public as TP
from services import admin_otp_test_mode as OTM
from services import app_environment as AE
from services import translation_audit as AU
from services import translations as T
from translation_test_support import (DB, add_ar, audit_actions, client, editor, fake_super, make_user, publish, put, setup_env,
                                      super_admin, sync_fixture_keys, viewer, grant)


@pytest.fixture(autouse=True)
def staging(monkeypatch):
    setup_env(monkeypatch, "staging")
    sync_fixture_keys()


# ═════ environment helper ════════════════════════════════════════════════════════════════════════════════════════════
class TestEnvironment:
    def test_not_set_or_invalid_is_unknown(self, monkeypatch):
        setup_env(monkeypatch, None)
        assert AE.classification() == "unknown"
        monkeypatch.setenv("OFFRO_ENVIRONMENT", "stage")
        assert AE.classification() == "unknown" and AE.status()["reason"] == "environment_value_invalid"

    @pytest.mark.parametrize("v", ["production", "staging", "development", "test"])
    def test_explicit_values(self, monkeypatch, v):
        setup_env(monkeypatch, v)
        assert AE.classification() == v

    def test_independent_of_the_whatsapp_setting(self, monkeypatch):
        setup_env(monkeypatch, "staging")
        monkeypatch.setenv("WA_CAMPAIGN_ENVIRONMENT", "production")      # a WhatsApp-specific value must not matter here
        assert AE.classification() == "staging"
        monkeypatch.delenv("WA_CAMPAIGN_ENVIRONMENT")
        assert AE.classification() == "staging"

    def test_platform_environment_named_production_cannot_be_talked_away(self, monkeypatch):
        setup_env(monkeypatch, "staging")
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
        s = AE.status()
        assert s["classification"] == "production" and s["conflict"] is True

    def test_known_staging_service_is_staging_even_if_the_railway_environment_is_named_production(self, monkeypatch):
        setup_env(monkeypatch, "staging")
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
        monkeypatch.setenv("RAILWAY_PROJECT_ID", OTM.STAGING_PROJECT_ID)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", OTM.STAGING_ENVIRONMENT_ID)
        monkeypatch.setenv("RAILWAY_SERVICE_ID", OTM.STAGING_SERVICE_ID)
        assert AE.classification() == "staging"

    def test_production_declared_on_the_staging_service_is_refused(self, monkeypatch):
        setup_env(monkeypatch, "production")
        monkeypatch.setenv("RAILWAY_PROJECT_ID", OTM.STAGING_PROJECT_ID)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", OTM.STAGING_ENVIRONMENT_ID)
        monkeypatch.setenv("RAILWAY_SERVICE_ID", OTM.STAGING_SERVICE_ID)
        assert AE.classification() == "unknown"

    def test_unknown_environment_refuses_every_write(self, monkeypatch):
        sa = super_admin()
        add_ar(sa)
        setup_env(monkeypatch, None)
        r = put(sa, "loginTitle", "مرحبا")
        assert r.status_code == 503 and r.json()["detail"]["code"] == "environment_unknown"
        assert publish(sa).status_code == 503


# ═════ value validation ══════════════════════════════════════════════════════════════════════════════════════════════
class TestValidation:
    def kd(self, key):
        return T.get_key(key)

    def test_good_values(self):
        assert T.validate_value(self.kd("loginTitle"), "  مرحبا بك في عرض ") == "مرحبا بك في عرض"
        assert T.validate_value(self.kd("otpSent"), "أرسلنا رمزاً من 4 أرقام إلى {phone}")
        assert T.validate_value(self.kd("supportLine"), "تحتاج مساعدة؟ راسل support@theoffro.com")   # e-mail already in English
        assert T.validate_value(self.kd("loginTitle"), "می‌خواهم")                              # ZWNJ is legitimate

    @pytest.mark.parametrize("key,value,code", [
        ("loginTitle", "<b>مرحبا</b>", "html_not_allowed"),
        ("loginTitle", "a < script", "html_not_allowed"),
        ("loginTitle", "x &lt;b&gt;", "html_not_allowed"),
        ("loginTitle", "ab‮cd", "hidden_or_bidi_control_character"),
        ("loginTitle", "ab⁧cd", "hidden_or_bidi_control_character"),
        ("loginTitle", "ab​cd", "hidden_or_bidi_control_character"),
        ("loginTitle", "ab\x07cd", "control_character"),
        ("loginTitle", "x" * 41, "too_long"),
        ("loginTitle", "", "empty_value"),
        ("loginTitle", "   ", "empty_value"),
        ("loginTitle", 12, "value_not_text"),
        ("otpSent", "رمز", "placeholder_mismatch"),
        ("otpSent", "رمز {phone} {name}", "placeholder_mismatch"),
        ("otpSent", "رمز {phoen}", "placeholder_mismatch"),
        ("otpSent", "رمز {phone", "malformed_placeholder"),
        ("loginTitle", "زر https://evil.example.com الآن", "url_email_or_phone_not_in_english"),
        ("loginTitle", "راسل hacker@evil.com", "url_email_or_phone_not_in_english"),
        ("loginTitle", "اتصل +966 50 123 4567", "url_email_or_phone_not_in_english"),
        ("loginTitle", "اتصل ٠٥٠١٢٣٤٥٦٧٨", "url_email_or_phone_not_in_english"),
        ("supportLine", "راسل other@evil.com", "url_email_or_phone_not_in_english"),
    ])
    def test_rejected(self, key, value, code):
        with pytest.raises(T.TranslationError) as e:
            T.validate_value(self.kd(key), value)
        assert e.value.code == code


# ═════ registry ══════════════════════════════════════════════════════════════════════════════════════════════════════
class TestRegistry:
    def test_fixture_sync(self):
        assert T.get_key("visitsCount")["is_plural"] is True
        assert T.get_key("privacyIntro")["group"] == "legal"
        assert T.get_key("otpSent")["placeholders"] == ["phone"]
        assert DB.translation_keys.count_documents({}) == 8

    def test_resync_deprecates_never_deletes(self):
        T.sync_keys([{"key": "loginTitle", "english": "Welcome to OffrO"}])
        assert DB.translation_keys.count_documents({}) == 8
        assert T.get_key("otpSent")["deprecated"] is True and T.get_key("loginTitle")["deprecated"] is False

    def test_bad_keys_rejected(self):
        with pytest.raises(T.TranslationError):
            T.sync_keys([{"key": "bad key!", "english": "x"}])

    def test_real_registry_is_empty_until_the_tool_is_run(self):
        T.db.drop_collection(T.KEYS)
        assert DB.translation_keys.count_documents({}) == 0                # nothing syncs automatically

    def test_tool_dry_run_never_connects_to_a_database(self, capsys):
        from tools import sync_translation_keys as S
        assert S.main([__import__("translation_test_support").FIXTURE_ARB]) == 0
        assert "Dry run" in capsys.readouterr().out


# ═════ permissions ═══════════════════════════════════════════════════════════════════════════════════════════════════
class TestPermissions:
    def test_unauthenticated(self):
        assert client.get("/admin/translations/languages").status_code == 401

    def test_no_permission_denied_and_audited(self):
        h = make_user("none-token")
        assert client.get("/admin/translations/languages", headers=h).status_code == 403
        assert any(a.startswith("denied:view") for a in audit_actions("denied"))

    def test_viewer_reads_but_cannot_edit(self):
        sa = super_admin(); add_ar(sa)
        v = viewer()
        assert client.get("/admin/translations/languages", headers=v).status_code == 200
        assert put(v, "loginTitle", "مرحبا").status_code == 403

    def test_editor_edits_drafts_but_cannot_publish_or_anything_privileged(self):
        sa = super_admin(); add_ar(sa)
        e = editor()
        assert put(e, "loginTitle", "مرحبا").status_code == 200
        assert client.post("/admin/translations/submit", headers=e, json={"lang": "ar"}).json()["submitted"] == 1
        for path, body in [("/publish", {"lang": "ar", "note": "x"}), ("/rollback", {"lang": "ar", "version": 1, "note": "x"}),
                           ("/languages", {"code": "hi", "name_en": "Hindi", "name_native": "हिन्दी", "direction": "ltr"}),
                           ("/approve-for-production", {"lang": "ar", "version": 1, "note": "x"}),
                           ("/package/import", {}), ("/kill-switch", {"enabled": True}),
                           ("/legal/review", {"lang": "ar", "key": "privacyIntro"})]:
            assert client.post("/admin/translations" + path, headers=e, json=body).status_code == 403, path
        assert client.patch("/admin/translations/languages/ar", headers=e, json={"status": "disabled"}).status_code == 403
        assert client.get("/admin/translations/package?lang=ar&version=1", headers=e).status_code == 403
        assert DB.translation_versions.count_documents({}) == 0
        assert T.kill_switch_state()["enabled"] is False

    def test_renamed_role_never_gains_super_admin_power(self):
        sa = super_admin(); add_ar(sa)
        f = fake_super()
        assert put(sa, "loginTitle", "مرحبا").status_code == 200
        for path, body in [("/publish", {"lang": "ar", "note": "x"}), ("/rollback", {"lang": "ar", "version": 1, "note": "x"}),
                           ("/languages", {"code": "hi", "name_en": "Hindi", "name_native": "हिन्दी", "direction": "ltr"}),
                           ("/approve-for-production", {"lang": "ar", "version": 1, "note": "x"}),
                           ("/package/import", {}), ("/kill-switch", {"enabled": True}),
                           ("/legal/review", {"lang": "ar", "key": "privacyIntro"})]:
            assert client.post("/admin/translations" + path, headers=f, json=body).status_code == 403, path
        assert client.get("/admin/translations/languages", headers=f).status_code == 403          # no matrix grant either
        assert put(f, "loginTitle", "x").status_code == 403
        assert DB.translation_versions.count_documents({}) == 0

    def test_system_super_admin_allowed(self):
        sa = super_admin()
        assert add_ar(sa).status_code == 200
        assert client.get("/admin/translations/environment", headers=sa).json()["caller_is_system_super_admin"] is True

    def test_super_admin_rule_is_the_exact_name_and_flag(self):
        assert TA.is_system_super_admin({"role": {"role_name": "Super Admin", "is_system_role": True}})
        assert not TA.is_system_super_admin({"role": {"role_name": "Super Admin", "is_system_role": False}})
        assert not TA.is_system_super_admin({"role": {"role_name": "Super Admin"}})
        assert not TA.is_system_super_admin({"role": {"role_name": "Administrator", "is_system_role": True}})
        assert not TA.is_system_super_admin({"role": {"role_name": "super admin", "is_system_role": True}})
        assert not TA.is_system_super_admin({})

    def test_module_is_registered_and_off_by_default_in_seeded_roles(self):
        from routers import dashboard_auth as DA
        assert "Translations" in DA.ALL_MODULES
        DB.dashboard_roles.delete_many({})
        DA.seed_rbac()
        for name in ("Administrator", "Read Only", "Sales Executive", "Finance"):
            perms = DB.dashboard_roles.find_one({"role_name": name})["permissions"]["Translations"]
            assert not any(perms.values()), name
        assert all(DB.dashboard_roles.find_one({"role_name": "Super Admin"})["permissions"]["Translations"].values())


# ═════ drafts ════════════════════════════════════════════════════════════════════════════════════════════════════════
class TestDrafts:
    def test_plural_is_read_only(self):
        sa = super_admin(); add_ar(sa)
        r = put(sa, "visitsCount", "{count} زيارات")
        assert r.status_code == 422 and r.json()["detail"]["code"] == "plural_read_only"

    def test_unknown_and_deprecated_keys(self):
        sa = super_admin(); add_ar(sa)
        assert put(sa, "nope", "x").status_code == 404
        T.sync_keys([{"key": "loginTitle", "english": "Welcome to OffrO"}])
        assert put(sa, "otpSent", "x {phone}").json()["detail"]["code"] == "key_deprecated"

    def test_edit_status_and_listing(self):
        sa = super_admin(); add_ar(sa)
        assert put(sa, "loginTitle", "مرحبا").json()["status"] == "modified"
        rows = client.get("/admin/translations/strings?lang=ar", headers=sa).json()
        by = {r["key"]: r for r in rows["rows"]}
        assert by["loginTitle"]["status"] == "modified" and by["resendOtp"]["status"] == "missing"
        assert client.get("/admin/translations/strings?lang=ar&filter=missing", headers=sa).json()["total"] >= 3
        assert client.get("/admin/translations/strings?lang=ar&q=otp", headers=sa).json()["total"] >= 1

    def test_legal_keys_listed_only_on_the_legal_track(self):
        sa = super_admin(); add_ar(sa)
        app_keys = {r["key"] for r in client.get("/admin/translations/strings?lang=ar", headers=sa).json()["rows"]}
        legal_keys = {r["key"] for r in client.get("/admin/translations/strings?lang=ar&track=legal", headers=sa).json()["rows"]}
        assert "privacyIntro" not in app_keys and "privacyIntro" in legal_keys

    def test_invalid_language_code_and_direction(self):
        sa = super_admin()
        assert client.post("/admin/translations/languages", headers=sa, json={"code": "A!", "name_en": "x", "name_native": "x", "direction": "rtl"}).status_code == 422
        assert client.post("/admin/translations/languages", headers=sa, json={"code": "ar", "name_en": "x", "name_native": "x", "direction": "up"}).status_code == 422
        assert add_ar(sa).status_code == 200 and add_ar(sa).status_code == 409


# ═════ versions, rollback, legal ═════════════════════════════════════════════════════════════════════════════════════
class TestVersions:
    def seed(self):
        sa = super_admin(); add_ar(sa)
        put(sa, "loginTitle", "مرحبا"); put(sa, "resendOtp", "إعادة الإرسال")
        return sa

    def test_publish_creates_immutable_versions(self):
        sa = self.seed()
        r = publish(sa)
        assert r.status_code == 200 and r.json()["version"] == 1
        v1 = DB.translation_versions.find_one({"version": 1})["strings"]
        put(sa, "loginTitle", "أهلا بك")
        assert publish(sa, note="second").json()["version"] == 2
        assert DB.translation_versions.find_one({"version": 1})["strings"] == v1         # old version unchanged
        assert DB.translation_versions.find_one({"version": 2})["strings"]["loginTitle"] == "أهلا بك"
        assert DB.translation_languages.find_one({"code": "ar"})["status"] == "active"

    def test_note_required_and_nothing_to_publish(self):
        sa = self.seed()
        assert publish(sa, note="").json()["detail"]["code"] == "note_required"
        assert publish(sa).status_code == 200
        assert publish(sa).json()["detail"]["code"] == "nothing_to_publish"

    def test_rollback_publishes_a_new_version_with_restored_from(self):
        sa = self.seed(); publish(sa)
        put(sa, "loginTitle", "أهلا بك"); publish(sa, note="v2")
        r = client.post("/admin/translations/rollback", headers=sa, json={"lang": "ar", "version": 1, "note": "bad wording"})
        assert r.status_code == 200 and r.json()["version"] == 3 and r.json()["restored_from"] == 1
        assert DB.translation_versions.count_documents({}) == 3                           # nothing deleted
        assert DB.translation_versions.find_one({"version": 3})["strings"] == DB.translation_versions.find_one({"version": 1})["strings"]
        assert DB.translation_values.find_one({"lang": "ar", "key": "loginTitle"})["value"] == "مرحبا"
        assert client.post("/admin/translations/rollback", headers=sa, json={"lang": "ar", "version": 3, "note": "x"}).json()["detail"]["code"] == "already_current"
        assert client.post("/admin/translations/rollback", headers=sa, json={"lang": "ar", "version": 99, "note": "x"}).status_code == 404

    def test_app_publish_never_carries_legal_text(self):
        sa = self.seed()
        put(sa, "privacyIntro", "باستمرارك فإنك تقبل سياسة الخصوصية.")
        publish(sa)
        assert "privacyIntro" not in DB.translation_versions.find_one({"track": "app"})["strings"]
        assert DB.translation_versions.count_documents({"track": "legal"}) == 0

    def test_legal_needs_review_of_the_exact_text(self):
        sa = self.seed()
        put(sa, "privacyIntro", "باستمرارك فإنك تقبل سياسة الخصوصية.")
        r = publish(sa, track="legal")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "legal_review_required"
        assert client.post("/admin/translations/legal/review", headers=sa, json={"lang": "ar", "key": "loginTitle"}).json()["detail"]["code"] == "not_a_legal_key"
        assert client.post("/admin/translations/legal/review", headers=sa, json={"lang": "ar", "key": "privacyIntro", "note": "checked by counsel"}).status_code == 200
        assert publish(sa, track="legal").status_code == 200
        put(sa, "privacyIntro", "نص معدل بعد المراجعة.")                                   # editing invalidates the review
        assert publish(sa, track="legal").json()["detail"]["code"] == "legal_review_required"
        assert DB.translation_versions.count_documents({"track": "legal"}) == 1           # own version counter

    def test_direction_locked_after_publish(self):
        sa = self.seed(); publish(sa)
        r = client.patch("/admin/translations/languages/ar", headers=sa, json={"direction": "ltr"})
        assert r.status_code == 409
        assert client.patch("/admin/translations/languages/ar", headers=sa, json={"status": "disabled"}).status_code == 200
        assert put(sa, "loginTitle", "x").json()["detail"]["code"] == "language_disabled"

    def test_invalid_values_cannot_be_published_even_if_they_reached_the_draft(self):
        sa = self.seed()
        DB.translation_values.update_one({"key": "loginTitle"}, {"$set": {"value": "<b>x</b>"}})   # simulate tampering
        r = publish(sa)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_values"
        assert DB.translation_versions.count_documents({}) == 0

    def test_production_has_no_direct_edits_and_cannot_add_languages(self, monkeypatch):
        sa = super_admin(); add_ar(sa)
        setup_env(monkeypatch, "production", verify="X" * 5)
        assert put(sa, "loginTitle", "مرحبا").json()["detail"]["code"] == "production_edit_blocked"
        assert add_ar(sa, "hi").json()["detail"]["code"] == "production_edit_blocked"
        assert client.post("/admin/translations/submit", headers=sa, json={"lang": "ar"}).status_code == 403

    def test_status_indicators(self):
        sa = self.seed()
        s = client.get("/admin/translations/languages", headers=sa).json()["languages"][0]["status_indicators"]["app"]
        assert s["draft_changes"] == 2 and s["complete"] is False and s["published_version"] is None
        publish(sa)
        s = client.get("/admin/translations/languages", headers=sa).json()["languages"][0]["status_indicators"]["app"]
        assert s["draft_changes"] == 0 and s["published_version"] == 1

    def test_preview_diff_and_versions_list(self):
        sa = self.seed()
        p = client.get("/admin/translations/preview?lang=ar", headers=sa).json()
        assert p["counts"]["changed"] == 2 and p["live_version"] is None
        publish(sa)
        v = client.get("/admin/translations/versions?lang=ar", headers=sa).json()["versions"]
        assert v[0]["version"] == 1 and v[0]["count"] == 2


# ═════ public API ════════════════════════════════════════════════════════════════════════════════════════════════════
class TestPublic:
    def published(self):
        sa = super_admin(); add_ar(sa)
        put(sa, "loginTitle", "مرحبا"); publish(sa)
        put(sa, "resendOtp", "مسودة فقط")                                                  # a DRAFT that must never be public
        return sa

    def test_manifest_and_file_return_published_data_only(self):
        self.published()
        m = client.get("/translations/manifest").json()
        assert m["kill_switch"] is False and m["overrides_enabled"] is True
        assert m["languages"][0]["code"] == "ar" and m["languages"][0]["direction"] == "rtl"
        assert m["languages"][0]["tracks"]["app"]["version"] == 1
        f = client.get("/translations/ar").json()
        assert f["strings"] == {"loginTitle": "مرحبا"} and "resendOtp" not in f["strings"]
        assert f["hash"] == m["languages"][0]["tracks"]["app"]["hash"]

    def test_draft_language_is_not_listed(self):
        sa = super_admin(); add_ar(sa); put(sa, "loginTitle", "مرحبا")
        assert client.get("/translations/manifest").json()["languages"] == []
        assert client.get("/translations/ar").status_code == 404

    def test_etag_and_304(self):
        self.published()
        r = client.get("/translations/ar")
        etag = r.headers["etag"]
        assert r.headers["cache-control"].startswith("public")
        assert client.get("/translations/ar", headers={"If-None-Match": etag}).status_code == 304
        assert client.get("/translations/ar", headers={"If-None-Match": "W/" + etag}).status_code == 304
        assert client.get("/translations/ar", headers={"If-None-Match": '"other"'}).status_code == 200
        m = client.get("/translations/manifest")
        assert m.headers["cache-control"] == "no-cache"
        assert client.get("/translations/manifest", headers={"If-None-Match": m.headers["etag"]}).status_code == 304

    def test_etag_changes_after_a_new_version(self):
        sa = self.published()
        old = client.get("/translations/ar").headers["etag"]
        publish(sa, note="v2")
        assert client.get("/translations/ar").headers["etag"] != old

    def test_unknown_language_and_bad_track(self):
        self.published()
        assert client.get("/translations/xx").status_code == 404
        assert client.get("/translations/ar?track=bogus").status_code == 404
        assert client.get("/translations/AR!").status_code == 404

    def test_legal_track_is_separate(self):
        sa = self.published()
        put(sa, "privacyIntro", "باستمرارك فإنك تقبل سياسة الخصوصية.")
        client.post("/admin/translations/legal/review", headers=sa, json={"lang": "ar", "key": "privacyIntro"})
        publish(sa, track="legal")
        assert "privacyIntro" not in client.get("/translations/ar").json()["strings"]
        assert "privacyIntro" in client.get("/translations/ar?track=legal").json()["strings"]
        assert set(client.get("/translations/manifest").json()["languages"][0]["tracks"]) == {"app", "legal"}

    def test_kill_switch_means_the_app_uses_bundled_text(self):
        sa = self.published()
        r = client.post("/admin/translations/kill-switch", headers=sa, json={"enabled": True, "reason": "bad copy"})
        assert r.status_code == 200 and r.json()["enabled"] is True
        m = client.get("/translations/manifest").json()
        assert m == {"schema": 1, "kill_switch": True, "overrides_enabled": False, "languages": []}
        assert client.get("/translations/ar").status_code == 404
        client.post("/admin/translations/kill-switch", headers=sa, json={"enabled": False})
        assert client.get("/translations/ar").status_code == 200

    def test_kill_switch_must_be_a_boolean(self):
        sa = super_admin()
        assert client.post("/admin/translations/kill-switch", headers=sa, json={"enabled": "yes"}).status_code == 422

    def test_unreadable_kill_switch_fails_closed(self, monkeypatch):
        self.published()
        monkeypatch.setattr(T, "kill_switch_on", T.kill_switch_on)
        class Boom:
            def find_one(self, *a, **k): raise RuntimeError("db down")
        real = T.db
        monkeypatch.setattr(T, "db", {T.STATE: Boom(), T.LANGS: real[T.LANGS], T.VERSIONS: real[T.VERSIONS]})
        assert T.kill_switch_on() is True
        m = T.public_manifest()
        assert m["overrides_enabled"] is False and m["languages"] == []

    def test_kill_switch_lives_in_each_environments_own_database(self, monkeypatch):
        sa = self.published()
        client.post("/admin/translations/kill-switch", headers=sa, json={"enabled": True})
        from translation_test_support import wipe_translation_data
        wipe_translation_data()                                                            # "production" has its own, empty database
        setup_env(monkeypatch, "production", verify="X" * 5)
        assert T.kill_switch_on() is False

    def test_rate_limit_is_per_ip_and_modest(self, monkeypatch):
        monkeypatch.setenv("TRANSLATION_PUBLIC_RATE_PER_MIN", "10")
        TP.reset_rate_limit()
        codes = [client.get("/translations/manifest", headers={"X-Forwarded-For": "1.1.1.1"}).status_code for _ in range(12)]
        assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]
        r = client.get("/translations/manifest", headers={"X-Forwarded-For": "1.1.1.1"})
        assert r.headers["retry-after"] == "60"
        assert client.get("/translations/manifest", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
        monkeypatch.setenv("TRANSLATION_PUBLIC_RATE_PER_MIN", "999999")
        assert TP._limit() == 600
        monkeypatch.setenv("TRANSLATION_PUBLIC_RATE_PER_MIN", "x")
        assert TP._limit() == 60

    def test_the_rate_limit_documents_that_it_is_per_process(self):
        assert "not a distributed rate limiter" in TP.__doc__


# ═════ audit ═════════════════════════════════════════════════════════════════════════════════════════════════════════
class TestAudit:
    def test_sensitive_operations_write_started_and_ok(self):
        sa = super_admin(); add_ar(sa); put(sa, "loginTitle", "مرحبا"); publish(sa)
        client.post("/admin/translations/kill-switch", headers=sa, json={"enabled": True})
        rows = list(DB.translation_audit.find())
        for action in ("add_language", "publish", "kill_switch"):
            outs = [r["outcome"] for r in rows if r["action"] == action]
            assert outs == ["started", "ok"], action
        assert any(r["action"] == "edit" and r["outcome"] == "ok" and r["new_hash"] for r in rows)

    def test_failed_operations_are_recorded(self):
        sa = super_admin()
        publish(sa, lang="zz")
        assert [r["outcome"] for r in DB.translation_audit.find({"action": "publish"})] == ["started", "failed"]
        put(sa, "loginTitle", "x")
        assert any(r["action"] == "edit" and r["outcome"] == "failed" for r in DB.translation_audit.find())

    def test_denied_attempts_are_recorded(self):
        sa = super_admin(); add_ar(sa)
        publish(editor())
        publish(fake_super("f2"))
        denied = DB.translation_audit.find({"outcome": "denied"})
        assert {d["action"] for d in denied} == {"denied:publish"}
        assert DB.translation_audit.count_documents({"outcome": "denied"}) == 2

    def test_audit_never_stores_translation_text_or_secrets(self):
        sa = super_admin(); add_ar(sa); put(sa, "loginTitle", "نص سري جداً")
        dump = str(list(DB.translation_audit.find()))
        assert "نص سري جداً" not in dump

    def test_sensitive_operation_is_refused_if_the_audit_cannot_be_written(self, monkeypatch):
        sa = super_admin(); add_ar(sa); put(sa, "loginTitle", "مرحبا")
        real = AU.record
        def failing(*a, strict=False, **k):
            if strict:
                raise AU.AuditWriteError("down")
            return False
        monkeypatch.setattr(AU, "record", failing)
        r = publish(sa)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "audit_unavailable"
        assert DB.translation_versions.count_documents({}) == 0                           # nothing was changed
        assert client.post("/admin/translations/kill-switch", headers=sa, json={"enabled": True}).status_code == 503
        assert T.kill_switch_state()["enabled"] is False

    def test_a_failed_completion_entry_is_reported_not_hidden(self, monkeypatch):
        sa = super_admin(); add_ar(sa); put(sa, "loginTitle", "مرحبا")
        real = AU.record
        def flaky(action, admin, outcome="ok", **k):
            if outcome == "ok":
                return False
            return real(action, admin, outcome, **k)
        monkeypatch.setattr(AU, "record", flaky)
        r = publish(sa)
        assert r.status_code == 200 and "audit_warning" in r.json()

    def test_audit_module_is_append_only(self):
        names = [n for n, f in inspect.getmembers(AU, inspect.isfunction)]
        assert not any(x in n for n in names for x in ("update", "delete", "remove", "purge", "drop"))
        assert "expireAfterSeconds" not in inspect.getsource(AU)

    def test_audit_endpoint_and_no_ttl_unlike_activity_logs(self):
        sa = super_admin(); add_ar(sa)
        e = client.get("/admin/translations/audit", headers=sa).json()["entries"]
        assert e and e[0]["action"] == "add_language"


# ═════ Phase L1: rollback safety (impact preview, draft protection, validation, journaled resumable rollback) ═══════════
import json as _json
from datetime import datetime as _dt, timedelta as _td

from pymongo.errors import DuplicateKeyError as _Dup, AutoReconnect as _Reconnect

@pytest.fixture
def fault():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


MARKER = "علامة_سرية_فريدة"            # a distinctive draft text: it must never appear in any audit entry or claim


def rb(headers, version, note="restore", confirm=None, lang="ar", track="app"):
    body = {"lang": lang, "track": track, "version": version, "note": note}
    if confirm:
        body["confirm_impact_hash"] = confirm
    return client.post("/admin/translations/rollback", headers=headers, json=body)


def prev(headers, version, **params):
    return client.get("/admin/translations/rollback-preview", headers=headers, params={"lang": "ar", "track": "app", "version": version, **params})


def rb_status(headers, lang="ar", track="app"):
    return client.get("/admin/translations/rollback-status", headers=headers, params={"lang": lang, "track": track})


def rb_resume(headers, action="resume", lang="ar", track="app"):
    return client.post("/admin/translations/rollback/resume", headers=headers, json={"lang": lang, "track": track, "action": action})


def code(r):
    d = r.json().get("detail")
    return d.get("code") if isinstance(d, dict) else d


def working():
    return {d["key"]: d["value"] for d in DB.translation_values.find({"lang": "ar", "track": "app"})}


def working_full():
    return sorted(({k: v for k, v in d.items() if k != "_id"} for d in DB.translation_values.find({})), key=lambda d: (d["lang"], d["key"]))


def claim():
    return DB.translation_rollbacks.find_one({"_id": "app:ar"})


def versions_n():
    return DB.translation_versions.count_documents({"track": "app", "lang": "ar"})


def scenario(sa, drafts=True):
    """v1 = {loginTitle:a1, resendOtp:b1}; v2 = {loginTitle:a2, resendOtp:b1, smsInstead:c1} (live).
    With drafts: loginTitle (changed), otpSent (a new key) are unpublished drafts."""
    add_ar(sa)
    put(sa, "loginTitle", "مرحبا"); put(sa, "resendOtp", "إعادة الإرسال"); assert publish(sa).json()["version"] == 1
    put(sa, "loginTitle", "أهلا"); put(sa, "smsInstead", "أرسل رسالة"); assert publish(sa, note="v2").json()["version"] == 2
    if drafts:
        put(sa, "loginTitle", MARKER); put(sa, "otpSent", "رمز {phone}")


V1 = {"loginTitle": "مرحبا", "resendOtp": "إعادة الإرسال"}


def audit_dump():
    return _json.dumps(list(DB.translation_audit.find()), default=str, ensure_ascii=False)


class TestRollbackImpact:
    def test_clean_working_copy_rolls_back_without_confirmation_exactly_as_before(self):
        sa = super_admin(); scenario(sa, drafts=False)
        r = rb(sa, 1)
        assert r.status_code == 200 and r.json()["version"] == 3 and r.json()["restored_from"] == 1
        assert r.json()["impact"]["overwritten"] == 0 and r.json()["impact"]["deleted"] == 0
        assert working() == V1 and claim() is None

    def test_impact_matches_the_worked_example(self):
        sa = super_admin(); scenario(sa)
        p = prev(sa, 1).json()
        assert p["counts"] == {"overwritten": 1, "deleted": 1, "working_removed": 1, "status_only": 0, "live_changed": 2,
                               "live_restored": 1, "live_removed": 1, "ready_for_review": 0, "blockers": 0}
        assert p["keys"]["overwritten"] == [{"key": "loginTitle", "draft": MARKER, "live": "أهلا", "restored": "مرحبا", "ready_for_review": False}]
        assert [r["key"] for r in p["keys"]["deleted"]] == ["otpSent"]
        assert [r["key"] for r in p["keys"]["working_removed"]] == ["smsInstead"]
        assert {r["key"]: r["change"] for r in p["keys"]["live_changed"]} == {"loginTitle": "changed", "smsInstead": "removed"}
        assert p["requires_confirmation"] is True and p["from_version"] == 2 and p["to_version"] == 1 and p["would_create_version"] is True

    def test_ready_for_review_drafts_are_counted(self):
        sa = super_admin(); scenario(sa)
        assert client.post("/admin/translations/submit", headers=sa, json={"lang": "ar", "track": "app"}).status_code == 200
        assert prev(sa, 1).json()["counts"]["ready_for_review"] == 2

    def test_a_draft_that_equals_the_restored_text_loses_nothing(self):
        sa = super_admin(); scenario(sa, drafts=False)
        put(sa, "loginTitle", "مرحبا")                                   # differs from live (v2) but equals v1
        p = prev(sa, 1).json()
        assert p["counts"]["overwritten"] == 0 and p["counts"]["status_only"] == 1 and p["requires_confirmation"] is False
        assert rb(sa, 1).status_code == 200

    def test_status_flag_is_considered_not_only_the_text(self):
        sa = super_admin(); scenario(sa, drafts=False)
        DB.translation_values.update_one({"lang": "ar", "key": "resendOtp"}, {"$set": {"status": "modified"}})   # same text as live
        p = prev(sa, 1).json()
        assert p["counts"]["status_only"] == 1 and p["counts"]["overwritten"] == 0 and p["requires_confirmation"] is False
        DB.translation_values.update_one({"lang": "ar", "key": "smsInstead"}, {"$set": {"value": "نص مختلف", "status": "published"}})
        p = prev(sa, 1).json()                                            # marked published but the text differs: still an unpublished draft
        assert [r["key"] for r in p["keys"]["deleted"]] == ["smsInstead"] and p["requires_confirmation"] is True

    def test_preview_writes_nothing_and_adds_no_audit_entry(self):
        sa = super_admin(); scenario(sa)
        before = (working_full(), DB.translation_versions.count_documents({}), DB.translation_audit.count_documents({}),
                  DB.translation_rollbacks.count_documents({}), DB.translation_state.count_documents({}))
        assert prev(sa, 1).status_code == 200 and prev(sa, 1, section="deleted").status_code == 200 and prev(sa, 1, format="csv").status_code == 200
        after = (working_full(), DB.translation_versions.count_documents({}), DB.translation_audit.count_documents({}),
                 DB.translation_rollbacks.count_documents({}), DB.translation_state.count_documents({}))
        assert before == after

    def test_unconfirmed_rollback_writes_nothing(self):
        sa = super_admin(); scenario(sa)
        before = working_full()
        r = rb(sa, 1)
        assert r.status_code == 409 and code(r) == "draft_loss_confirmation_required"
        assert r.json()["detail"]["detail"]["counts"]["overwritten"] == 1 and MARKER not in r.text
        assert working_full() == before and versions_n() == 2 and claim() is None

    def test_wrong_or_stale_confirmation_is_refused_and_nothing_changes(self):
        sa = super_admin(); scenario(sa)
        h = prev(sa, 1).json()["impact_hash"]
        before = working_full()
        assert code(rb(sa, 1, confirm="0" * 64)) == "impact_changed"
        put(sa, "resendOtp", "نص جديد")                                   # the working copy changes after the preview
        r = rb(sa, 1, confirm=h)
        assert r.status_code == 409 and code(r) == "impact_changed"
        assert versions_n() == 2 and claim() is None and working()["resendOtp"] == "نص جديد" and before != working_full()

    def test_confirmed_rollback_restores_working_copy_and_drops_the_drafts(self):
        sa = super_admin(); scenario(sa)
        h = prev(sa, 1).json()["impact_hash"]
        r = rb(sa, 1, confirm=h)
        assert r.status_code == 200 and r.json()["version"] == 3 and r.json()["restored_from"] == 1
        assert working() == V1 and claim() is None and versions_n() == 3
        assert all(d["status"] == "published" and d["submitted"] is False for d in DB.translation_values.find({"lang": "ar"}))
        v3, v1 = DB.translation_versions.find_one({"version": 3}), DB.translation_versions.find_one({"version": 1})
        assert v3["strings"] == v1["strings"] and v3["hash"] == v1["hash"] and v3["restored_from"] == 1 and v3["intent_id"]

    def test_public_api_after_rollback_serves_the_restored_version(self):
        sa = super_admin(); scenario(sa)
        rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        m = client.get("/translations/manifest").json()["languages"][0]["tracks"]["app"]
        assert m["version"] == 3 and m["hash"] == DB.translation_versions.find_one({"version": 1})["hash"]
        assert client.get("/translations/ar").json()["strings"] == V1

    def test_more_than_fifty_keys_totals_paging_and_full_csv(self):
        sa = super_admin()
        T.sync_keys(__import__("tools.sync_translation_keys", fromlist=["parse_arb"]).parse_arb(__import__("translation_test_support").FIXTURE_ARB)
                    + [{"key": f"bulk{i:03d}", "english": "Bulk"} for i in range(120)])
        add_ar(sa)
        for round_, label in enumerate(("اول", "ثاني"), 1):
            for i in range(120):
                T.set_value("ar", f"bulk{i:03d}", f"{label} {i}")
            assert publish(sa, note=f"r{round_}").status_code == 200
        for i in range(120):
            T.set_value("ar", f"bulk{i:03d}", f"مسودة {i}")
        p = prev(sa, 1).json()
        assert p["counts"]["overwritten"] == 120 and p["totals"]["overwritten"] == 120 and len(p["keys"]["overwritten"]) == 50
        pg3 = prev(sa, 1, section="overwritten", page=3).json()
        assert pg3["total"] == 120 and len(pg3["rows"]) == 20 and pg3["page"] == 3
        assert len(prev(sa, 1, section="overwritten", page_size=5000).json()["rows"]) == 120 and prev(sa, 1, section="overwritten", page_size=5000).json()["page_size"] == 200
        assert pg3["impact_hash"] == p["impact_hash"]
        csv_ = prev(sa, 1, format="csv")
        assert csv_.status_code == 200 and "text/csv" in csv_.headers["content-type"] and csv_.headers["cache-control"] == "no-store"
        lines = csv_.content.decode("utf-8-sig").strip().splitlines()
        assert lines[0].startswith("section,key,change") and sum(1 for ln in lines if ln.startswith("overwritten,")) == 120
        only = prev(sa, 1, format="csv", section="overwritten").content.decode("utf-8-sig").strip().splitlines()
        assert len(only) == 121 and all(ln.startswith("overwritten,") for ln in only[1:])
        assert code(rb(sa, 1)) == "draft_loss_confirmation_required"
        assert rb(sa, 1, confirm=p["impact_hash"]).status_code == 200 and working()["bulk000"] == "اول 0"

    def test_csv_neutralises_spreadsheet_formulas(self):
        sa = super_admin(); scenario(sa, drafts=False)
        put(sa, "loginTitle", "=1+1")
        text = prev(sa, 1, format="csv").content.decode("utf-8-sig")
        assert "'=1+1" in text and ",=1+1" not in text

    def test_paging_and_section_arguments_are_validated(self):
        sa = super_admin(); scenario(sa)
        assert code(prev(sa, 1, section="nope")) == "invalid_section" and prev(sa, 1, section="nope").status_code == 422
        assert code(prev(sa, 1, format="xml")) == "invalid_format"
        assert prev(sa, 1, page_size="x").status_code == 422
        assert prev(sa, 99).status_code == 404 and code(prev(sa, 99)) == "unknown_version"


class TestRollbackSameHash:
    def test_target_equal_to_latest_is_refused_and_drafts_are_untouched(self):
        sa = super_admin(); scenario(sa)
        before = working_full()
        p = prev(sa, 2).json()
        assert p["already_current"] is True and p["would_create_version"] is False and p["requires_confirmation"] is False
        assert p["counts"]["overwritten"] == 1 and p["counts"]["deleted"] == 1            # the drafts are still detected and reported
        r = rb(sa, 2)
        assert r.status_code == 409 and code(r) == "already_current"
        assert working_full() == before and versions_n() == 2 and claim() is None

    def test_an_older_version_with_the_same_content_as_the_latest_is_refused_too(self):
        sa = super_admin(); scenario(sa, drafts=False)
        assert rb(sa, 1).status_code == 200                                              # v3 now has v1's content
        put(sa, "loginTitle", MARKER)
        before = working_full()
        r = rb(sa, 1)
        assert r.status_code == 409 and code(r) == "already_current"
        assert prev(sa, 1).json()["already_current"] is True and prev(sa, 1).json()["counts"]["overwritten"] == 1
        assert working_full() == before and versions_n() == 3 and claim() is None


class TestRollbackValidation:
    def test_unknown_and_invalid_versions(self):
        sa = super_admin(); scenario(sa, drafts=False)
        assert rb(sa, 99).status_code == 404
        for bad in ("x", True, 1.5, None, [1]):
            r = rb(sa, bad)
            assert r.status_code in (422, 409) and code(r) in ("invalid_version", "already_current"), bad
        assert rb(sa, "x").status_code == 422 and code(rb(sa, "x")) == "invalid_version"

    def test_note_is_required(self):
        sa = super_admin(); scenario(sa, drafts=False)
        assert code(rb(sa, 1, note="")) == "note_required"

    def test_a_key_that_is_deprecated_now_blocks_the_rollback(self):
        sa = super_admin(); scenario(sa, drafts=False)
        entries = [e for e in __import__("tools.sync_translation_keys", fromlist=["parse_arb"]).parse_arb(__import__("translation_test_support").FIXTURE_ARB) if e["key"] != "resendOtp"]
        T.sync_keys(entries)
        before = working_full()
        r = rb(sa, 1)
        assert r.status_code == 422 and code(r) == "invalid_values" and r.json()["detail"]["detail"] == {"resendOtp": "key_not_publishable"}
        assert working_full() == before and versions_n() == 2 and claim() is None
        assert prev(sa, 1).json()["blockers"]["keys"] == {"resendOtp": "key_not_publishable"}

    @pytest.mark.parametrize("mutate,expected", [
        (lambda: DB.translation_keys.update_one({"key": "loginTitle"}, {"$set": {"max_length": 3}}), "too_long"),
        (lambda: DB.translation_keys.update_one({"key": "loginTitle"}, {"$set": {"placeholders": ["name"]}}), "placeholder_mismatch"),
    ])
    def test_a_text_that_no_longer_fits_the_current_registry_blocks_the_rollback(self, mutate, expected):
        sa = super_admin(); scenario(sa, drafts=False)
        mutate()
        r = rb(sa, 1)
        assert r.status_code == 422 and r.json()["detail"]["detail"]["loginTitle"] == expected
        assert versions_n() == 2 and claim() is None

    def test_disabled_language_and_failed_integrity_are_refused(self):
        sa = super_admin(); scenario(sa, drafts=False)
        DB.translation_versions.update_one({"version": 1}, {"$set": {"strings.loginTitle": "تم العبث"}})
        r = rb(sa, 1)
        assert r.status_code == 409 and code(r) == "version_integrity_failed" and prev(sa, 1).json()["blockers"]["global"] == ["version_integrity_failed"]
        DB.translation_versions.update_one({"version": 1}, {"$set": {"strings.loginTitle": "مرحبا"}})
        DB.translation_languages.update_one({"code": "ar"}, {"$set": {"status": "disabled"}})
        assert code(rb(sa, 1)) == "language_disabled" and versions_n() == 2

    def test_legal_track_needs_the_review_of_the_restored_text(self):
        sa = super_admin(); add_ar(sa)
        client.post("/admin/translations/legal/review", headers=sa, json={"lang": "ar", "key": "privacyIntro"})   # (no value yet: refused)
        put(sa, "privacyIntro", "باستمرارك فإنك تقبل سياسة الخصوصية.")
        assert client.post("/admin/translations/legal/review", headers=sa, json={"lang": "ar", "key": "privacyIntro", "note": "ok"}).status_code == 200
        assert publish(sa, track="legal").status_code == 200
        put(sa, "privacyIntro", "نص معدل بعد المراجعة.")
        client.post("/admin/translations/legal/review", headers=sa, json={"lang": "ar", "key": "privacyIntro", "note": "ok2"})
        assert publish(sa, track="legal").status_code == 200
        r = rb(sa, 1, track="legal")                                                       # v1's exact text has a recorded review
        assert r.status_code == 200 and r.json()["version"] == 3
        put(sa, "privacyIntro", "نص ثالث.")
        client.post("/admin/translations/legal/review", headers=sa, json={"lang": "ar", "key": "privacyIntro", "note": "ok3"})
        assert publish(sa, track="legal").status_code == 200                               # v4
        DB.translation_legal_reviews.delete_many({})                                       # the review of v1's text is gone
        r = rb(sa, 1, track="legal")
        assert r.status_code == 409 and code(r) == "legal_review_required" or code(r) == "already_current"


class TestRollbackAccess:
    def test_new_routes_are_super_admin_only_and_denials_are_audited(self):
        sa = super_admin(); scenario(sa, drafts=False)
        for h in (editor(), viewer(), fake_super()):
            assert prev(h, 1).status_code == 403 and rb_status(h).status_code == 403 and rb_resume(h).status_code == 403 and rb(h, 1).status_code == 403
        assert any("rollback_preview" in a for a in audit_actions("denied")) and any("rollback_status" in a for a in audit_actions("denied"))
        assert client.get("/admin/translations/rollback-preview", params={"lang": "ar", "version": 1}).status_code == 401

    def test_production_has_no_preview_status_resume_or_rollback(self, monkeypatch):
        sa = super_admin(); scenario(sa, drafts=False)
        setup_env(monkeypatch, "production", verify="Qm4xT9vZb2KdW7nLc5RhY1eJ8uSaF3oGpX6A")
        for r in (prev(sa, 1), rb_status(sa), rb_resume(sa), rb(sa, 1)):
            assert r.status_code == 403 and code(r) == "production_edit_blocked"
        assert versions_n() == 2 and DB.translation_rollbacks.count_documents({}) == 0

    def test_languages_report_a_pending_rollback(self, fault):
        sa = super_admin(); scenario(sa, drafts=False)
        assert client.get("/admin/translations/languages", headers=sa).json()["languages"][0]["status_indicators"]["app"]["rollback_pending"] is False
        fault.setattr(T, "_restore_working_copy", lambda *a, **k: (_ for _ in ()).throw(_Reconnect("down")))
        assert rb(sa, 1).status_code == 503
        assert client.get("/admin/translations/languages", headers=sa).json()["languages"][0]["status_indicators"]["app"]["rollback_pending"] is True


def boom(*a, **k):
    raise _Reconnect("connection lost")


class TestRollbackJournal:
    """Failures at every write stage. mongomock has no transactions either, so these prove the journal, not a database feature."""

    def test_stage_1_claim_cannot_be_written(self, fault):
        sa = super_admin(); scenario(sa); before = working_full()
        fault.setattr(T, "_claim_insert", boom)
        r = rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        assert r.status_code == 503 and code(r) == "rollback_not_started"
        assert working_full() == before and versions_n() == 2 and claim() is None
        fault.undo()
        assert rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"]).status_code == 200 and versions_n() == 3

    def test_stage_2_failure_before_the_version_exists_can_be_aborted_or_resumed(self, fault):
        sa = super_admin(); scenario(sa); before = working_full()
        h = prev(sa, 1).json()["impact_hash"]
        fault.setattr(T, "_insert_rollback_version", boom)
        r = rb(sa, 1, confirm=h)
        assert r.status_code == 503 and code(r) == "rollback_interrupted" and r.json()["detail"]["detail"]["phase"] == "started"
        assert versions_n() == 2 and claim()["phase"] == "writing" and working_full() == before
        st = rb_status(sa).json()
        assert st["active"] and st["version_written"] is False and st["can_abort"] is True and st["can_resume"] is True and st["is_owner"] is True
        fault.undo()
        a = rb_resume(sa, "abort")
        assert a.status_code == 200 and a.json()["aborted"] is True and claim() is None
        assert versions_n() == 2 and working_full() == before                                   # drafts untouched by an abort
        assert rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"]).status_code == 200 and versions_n() == 3

    def test_stage_2_resume_instead_of_abort_completes_it(self, fault):
        sa = super_admin(); scenario(sa)
        fault.setattr(T, "_insert_rollback_version", boom)
        rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        fault.undo()
        r = rb_resume(sa)
        assert r.status_code == 200 and r.json()["version"] == 3 and r.json()["resumed"] is True
        assert versions_n() == 3 and working() == V1 and claim() is None

    def test_stage_2_acknowledgement_lost_never_creates_a_second_version(self, fault):
        sa = super_admin(); scenario(sa)
        real = T._insert_rollback_version
        def insert_then_lose_ack(claim_, target, actor):
            real(claim_, target, actor)
            raise _Reconnect("ack lost")
        fault.setattr(T, "_insert_rollback_version", insert_then_lose_ack)
        r = rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        assert r.status_code == 503 and code(r) == "rollback_incomplete" and r.json()["detail"]["detail"]["phase"] == "version_written"
        assert versions_n() == 3 and client.get("/translations/manifest").json()["languages"][0]["tracks"]["app"]["version"] == 3
        fault.undo()
        assert rb_resume(sa).status_code == 200
        assert versions_n() == 3 and working() == V1 and claim() is None

    def test_stage_3_partial_restore_then_resume(self, fault):
        sa = super_admin(); scenario(sa)
        def half(claim_, target, actor):
            first = sorted(target["strings"])[0]
            DB.translation_values.update_one({"lang": "ar", "key": first}, {"$set": {"value": target["strings"][first], "status": "published"}})
            raise _Reconnect("died halfway")
        fault.setattr(T, "_restore_working_copy", half)
        r = rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        assert r.status_code == 503 and code(r) == "rollback_incomplete"
        # the app already sees the new version, never the working copy
        pub = client.get("/translations/ar")
        assert pub.json()["version"] == 3 and pub.json()["strings"] == V1 and MARKER not in pub.text and "رمز" not in pub.text
        assert versions_n() == 3 and set(working()) != set(V1)                                  # working copy is only partly restored
        st = rb_status(sa).json()
        assert st["phase"] == "version_written" and st["version_written"] is True and st["can_abort"] is False and st["can_resume"] is True
        # everything that could disturb the working copy is paused
        assert code(publish(sa)) == "rollback_in_progress" and code(put(sa, "resendOtp", "x")) == "rollback_in_progress"
        assert code(client.post("/admin/translations/submit", headers=sa, json={"lang": "ar", "track": "app"})) == "rollback_in_progress"
        assert code(rb(sa, 1)) == "rollback_in_progress"
        a = rb_resume(sa, "abort")
        assert a.status_code == 409 and code(a) == "abort_not_allowed_version_written" and claim() is not None
        fault.undo()
        r = rb_resume(sa)
        assert r.status_code == 200 and r.json()["version"] == 3
        assert working() == V1 and versions_n() == 3 and claim() is None
        assert put(sa, "resendOtp", "إعادة").status_code == 200                                  # editing works again

    def test_stage_4_pending_package_cannot_be_cleared(self, fault):
        sa = super_admin(); scenario(sa)
        fault.setattr(T, "_clear_pending", boom)
        r = rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        assert r.status_code == 503 and code(r) == "rollback_incomplete" and working() == V1
        fault.undo()
        assert rb_resume(sa).status_code == 200 and claim() is None and versions_n() == 3

    def test_stage_5_phase_update_after_the_insert_fails_abort_still_refused(self, fault):
        sa = super_admin(); scenario(sa)
        real = T._claim_update
        def fail_on_written(claim_, fields, phases=None):
            if fields.get("phase") == "version_written":
                raise _Reconnect("lost")
            return real(claim_, fields, phases)
        fault.setattr(T, "_claim_update", fail_on_written)
        r = rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        assert r.status_code == 503 and code(r) == "rollback_incomplete" and claim()["phase"] == "writing"
        st = rb_status(sa).json()
        assert st["version_written"] is True and st["can_abort"] is False             # decided from the version itself, not the phase flag
        a = rb_resume(sa, "abort")
        assert a.status_code == 409 and code(a) == "abort_not_allowed_version_written" and claim() is not None and versions_n() == 3
        fault.undo()
        assert rb_resume(sa).status_code == 200 and working() == V1 and claim() is None

    def test_stage_6_claim_cannot_be_deleted_at_the_end(self, fault):
        sa = super_admin(); scenario(sa)
        fault.setattr(T, "_claim_delete", boom)
        assert code(rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])) == "rollback_incomplete"
        assert working() == V1 and claim() is not None
        fault.undo()
        assert rb_resume(sa).status_code == 200 and claim() is None and versions_n() == 3
        assert code(rb_resume(sa)) == "no_rollback_in_progress" and rb_resume(sa).status_code == 404       # a second Resume is harmless

    def test_resume_can_be_repeated_after_a_second_failure(self, fault):
        sa = super_admin(); scenario(sa)
        fault.setattr(T, "_restore_working_copy", boom)
        rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        assert code(rb_resume(sa)) == "rollback_incomplete" and versions_n() == 3
        fault.undo()
        assert rb_resume(sa).status_code == 200 and versions_n() == 3 and working() == V1

    def test_a_second_rollback_while_one_is_unfinished_is_refused(self, fault):
        sa = super_admin(); scenario(sa)
        fault.setattr(T, "_restore_working_copy", boom)
        rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        fault.undo()
        r = rb(sa, 1)
        assert r.status_code == 409 and code(r) == "rollback_in_progress" and versions_n() == 3

    def test_an_edit_that_slips_in_before_the_claim_is_caught_by_the_recheck(self, fault):
        sa = super_admin(); scenario(sa)
        h = prev(sa, 1).json()["impact_hash"]
        real = T._claim_insert
        def insert_then_edit(c):
            real(c)
            DB.translation_values.update_one({"lang": "ar", "key": "otpSent"}, {"$set": {"value": "رمز آخر {phone}"}})
        fault.setattr(T, "_claim_insert", insert_then_edit)
        r = rb(sa, 1, confirm=h)
        assert r.status_code == 409 and code(r) == "impact_changed"
        assert versions_n() == 2 and claim() is None and working()["otpSent"] == "رمز آخر {phone}"

    def test_a_publish_that_took_the_version_number_is_detected_and_leaves_no_claim(self, fault):
        sa = super_admin(); scenario(sa)
        def foreign(claim_, target, actor):
            DB.translation_versions.insert_one({"track": "app", "lang": "ar", "version": 3, "strings": {}, "hash": T.hash_strings({}), "note": "other",
                                                "published_at": _dt.utcnow(), "intent_id": None})
            raise _Dup("duplicate")
        fault.setattr(T, "_insert_rollback_version", foreign)
        r = rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        assert r.status_code == 409 and code(r) == "version_conflict" and claim() is None
        assert versions_n() == 3 and DB.translation_versions.find_one({"version": 3})["note"] == "other"      # nobody's version was overwritten

    def test_resume_revalidates_before_it_writes_the_version(self, fault):
        sa = super_admin(); scenario(sa)
        fault.setattr(T, "_insert_rollback_version", boom)
        rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        fault.undo()
        DB.translation_keys.update_one({"key": "loginTitle"}, {"$set": {"max_length": 3}})
        r = rb_resume(sa)
        assert r.status_code == 422 and code(r) == "invalid_values" and versions_n() == 2 and claim() is not None
        assert rb_resume(sa, "abort").status_code == 200 and claim() is None


class TestRollbackLeaseAndTakeover:
    def other_admin(self):
        return make_user("sa2-token", role_name="Super Admin", system=True, name="Second Admin")

    def unfinished(self, fault, sa):
        fault.setattr(T, "_restore_working_copy", boom)
        rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        fault.undo()

    def test_status_shows_owner_and_lease_and_what_the_caller_may_do(self, fault):
        sa = super_admin(); sa2 = self.other_admin(); scenario(sa)
        self.unfinished(fault, sa)
        mine = rb_status(sa).json()
        assert mine["active"] and mine["owner"]["name"] == "Super Admin User" and mine["started_by"]["name"] == "Super Admin User"
        assert mine["lease_until"] and mine["lease_expired"] is False and mine["is_owner"] is True and mine["can_resume"] is True
        assert mine["target_version"] == 1 and mine["new_version"] == 3 and mine["version_written"] is True and mine["can_abort"] is False
        theirs = rb_status(sa2).json()
        assert theirs["is_owner"] is False and theirs["can_resume"] is False and theirs["can_abort"] is False and theirs["reason"] == "owned_by_other"

    def test_another_super_admin_cannot_act_before_the_lease_expires(self, fault):
        sa = super_admin(); sa2 = self.other_admin(); scenario(sa)
        self.unfinished(fault, sa)
        for action in ("resume", "abort"):
            r = rb_resume(sa2, action)
            assert r.status_code == 409 and code(r) == "rollback_owned_by_other"
            assert r.json()["detail"]["detail"]["owner"] == "Super Admin User" and r.json()["detail"]["detail"]["lease_until"]
        assert claim()["owner"]["name"] == "Super Admin User" and versions_n() == 3

    def test_after_expiry_another_super_admin_takes_over_and_it_is_audited(self, fault):
        sa = super_admin(); sa2 = self.other_admin(); scenario(sa)
        self.unfinished(fault, sa)
        sa_id = DB.dashboard_users.find_one({"token": "sa-token"})["_id"]
        later = _dt.utcnow() + _td(minutes=11)
        fault.setattr(T, "_now", lambda: later)
        st = rb_status(sa2).json()
        assert st["lease_expired"] is True and st["can_resume"] is True and st["is_owner"] is False
        r = rb_resume(sa2)
        assert r.status_code == 200 and r.json()["version"] == 3 and working() == V1 and claim() is None and versions_n() == 3
        e = [d for d in DB.translation_audit.find({"action": "rollback_resume", "outcome": "ok"})]
        assert len(e) == 1 and e[0]["admin"]["name"] == "Second Admin"
        assert e[0]["details"]["takeover"] is True and e[0]["details"]["previous_owner_id"] == str(sa_id) and e[0]["details"]["resumed"] is True

    def test_takeover_moves_ownership_and_renews_the_lease(self, fault):
        sa = super_admin(); sa2 = self.other_admin(); sa3 = make_user("sa3-token", role_name="Super Admin", system=True, name="Third Admin")
        scenario(sa)
        self.unfinished(fault, sa)
        later = _dt.utcnow() + _td(minutes=11)
        fault.setattr(T, "_now", lambda: later)
        fault.setattr(T, "_restore_working_copy", boom)                 # the new owner's attempt fails too: the claim stays
        assert code(rb_resume(sa2)) == "rollback_incomplete"
        c = claim()
        assert c["owner"]["name"] == "Second Admin" and c["takeover_count"] == 1
        assert abs((c["lease_until"] - (later + _td(seconds=T.LEASE_SECONDS))).total_seconds()) < 1          # every phase renews the lease
        r = rb_resume(sa3)                                                     # exactly one winner: the lease is valid again
        assert r.status_code == 409 and code(r) == "rollback_owned_by_other" and r.json()["detail"]["detail"]["owner"] == "Second Admin"
        assert rb_status(sa).json()["can_resume"] is False

    def test_losing_the_atomic_takeover_race_is_refused(self, fault):
        sa = super_admin(); sa2 = self.other_admin(); scenario(sa)
        self.unfinished(fault, sa)
        fault.setattr(T, "_now", lambda: _dt.utcnow() + _td(minutes=11))
        coll = T.db[T.ROLLBACKS]
        fault.setattr(coll, "update_one", lambda *a, **k: type("R", (), {"modified_count": 0, "matched_count": 0})())
        r = rb_resume(sa2)
        assert r.status_code == 409 and code(r) == "rollback_owned_by_other" and claim()["owner"]["name"] == "Super Admin User"

    def test_abort_after_takeover_only_when_the_version_was_not_written(self, fault):
        sa = super_admin(); sa2 = self.other_admin(); scenario(sa)
        fault.setattr(T, "_insert_rollback_version", boom)
        rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        fault.undo()
        fault.setattr(T, "_now", lambda: _dt.utcnow() + _td(minutes=11))
        a = rb_resume(sa2, "abort")
        assert a.status_code == 200 and claim() is None and versions_n() == 2
        ab = [d for d in DB.translation_audit.find({"action": "rollback_abort", "outcome": "ok"})]
        assert len(ab) == 1 and ab[0]["details"]["takeover"] is True

    def test_abort_is_refused_after_takeover_once_the_version_is_written(self, fault):
        sa = super_admin(); sa2 = self.other_admin(); scenario(sa)
        self.unfinished(fault, sa)
        fault.setattr(T, "_now", lambda: _dt.utcnow() + _td(minutes=11))
        a = rb_resume(sa2, "abort")
        assert a.status_code == 409 and code(a) == "abort_not_allowed_version_written" and claim() is not None
        assert claim()["owner"]["name"] == "Second Admin"                                        # the takeover itself stands

    def test_invalid_action_and_missing_claim(self):
        sa = super_admin(); scenario(sa, drafts=False)
        assert rb_resume(sa).status_code == 404 and code(rb_resume(sa)) == "no_rollback_in_progress"
        assert rb_resume(sa, "explode").status_code == 422 and code(rb_resume(sa, "explode")) == "invalid_action"
        assert rb_status(sa).json() == {"lang": "ar", "track": "app", "active": False}


class TestRollbackAudit:
    def started_failed(self, action="rollback"):
        es = list(DB.translation_audit.find({"action": action}))
        return [e["outcome"] for e in es], es

    def test_every_failure_is_audited_with_codes_and_never_with_text(self, fault):
        sa = super_admin(); scenario(sa)
        ed = editor()
        cases = []
        cases.append(rb(sa, 99))                                                    # 404
        cases.append(rb(sa, "x"))                                                   # 422 invalid_version
        cases.append(rb(sa, 1))                                                     # 409 confirmation required
        cases.append(rb(sa, 1, confirm="f" * 64))                                   # 409 impact_changed
        cases.append(rb(sa, 2))                                                     # 409 already_current
        cases.append(rb(ed, 1))                                                     # 403
        DB.translation_keys.update_one({"key": "resendOtp"}, {"$set": {"max_length": 2}})
        cases.append(rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"]))          # 422 invalid_values
        DB.translation_keys.update_one({"key": "resendOtp"}, {"$set": {"max_length": 100}})
        fault.setattr(T, "_restore_working_copy", boom)
        cases.append(rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"]))          # 503 incomplete
        cases.append(rb_resume(sa, "abort"))                                        # 409 abort refused
        assert [c.status_code for c in cases] == [404, 422, 409, 409, 409, 403, 422, 503, 409]
        outs, es = self.started_failed("rollback")
        assert outs.count("started") == outs.count("failed") + outs.count("ok") and outs.count("started") == 7         # 403 is a denial, not a start
        codes = [e["details"].get("code") for e in es if e["outcome"] == "failed"]
        assert codes == ["unknown_version", "invalid_version", "draft_loss_confirmation_required", "impact_changed", "already_current",
                         "invalid_values", "rollback_incomplete"]
        by = {e["details"]["code"]: e["details"] for e in es if e["outcome"] == "failed"}
        assert by["draft_loss_confirmation_required"]["counts"]["overwritten"] == 1 and by["draft_loss_confirmation_required"]["impact_hash"]
        assert by["invalid_values"]["blockers"] == {"resendOtp": "too_long"}
        assert by["rollback_incomplete"]["phase"] == "version_written" and by["rollback_incomplete"]["new_version"] == 3
        assert any(a.startswith("denied:") for a in audit_actions("denied"))
        assert MARKER not in audit_dump() and "رمز" not in audit_dump()
        assert MARKER not in _json.dumps(claim(), default=str, ensure_ascii=False)
        ab = list(DB.translation_audit.find({"action": "rollback_abort"}))
        assert [e["outcome"] for e in ab] == ["started", "failed"] and ab[1]["details"]["code"] == "abort_not_allowed_version_written"

    def test_a_successful_rollback_audit_has_counts_and_no_text(self):
        sa = super_admin(); scenario(sa)
        h = prev(sa, 1).json()["impact_hash"]
        rb(sa, 1, confirm=h)
        es = list(DB.translation_audit.find({"action": "rollback"}))
        assert [e["outcome"] for e in es] == ["started", "ok"]
        assert es[0]["details"] == {"to_version": 1, "confirm_provided": True}
        assert es[1]["version"] == 3 and es[1]["new_hash"] and es[1]["details"]["counts"]["overwritten"] == 1 and es[1]["details"]["resumed"] is False
        assert MARKER not in audit_dump()

    def test_resume_and_abort_are_audited_as_their_own_actions(self, fault):
        sa = super_admin(); scenario(sa)
        fault.setattr(T, "_restore_working_copy", boom)
        rb(sa, 1, confirm=prev(sa, 1).json()["impact_hash"])
        fault.undo()
        rb_resume(sa)
        outs, _ = self.started_failed("rollback_resume")
        assert outs == ["started", "ok"] and MARKER not in audit_dump()

    def test_an_unexpected_error_is_audited_as_internal_error_and_still_raised(self, fault):
        sa = super_admin(); scenario(sa, drafts=False)
        fault.setattr(T, "rollback", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
        with pytest.raises(RuntimeError):
            rb(sa, 1)
        es = list(DB.translation_audit.find({"action": "rollback"}))
        assert [e["outcome"] for e in es] == ["started", "failed"] and es[1]["details"] == {"code": "internal_error"}

    def test_a_malformed_body_is_audited_as_bad_request(self):
        sa = super_admin(); scenario(sa, drafts=False)
        for path in ("/admin/translations/rollback", "/admin/translations/rollback/resume"):
            for body in ("[1,2]", "not json", '"text"'):
                r = client.post(path, headers={**sa, "Content-Type": "application/json"}, content=body)
                assert r.status_code == 422 and code(r) == "bad_request"
        fails = [e for e in DB.translation_audit.find({"outcome": "failed"})]
        assert len(fails) == 6 and all(e["details"] == {"code": "bad_request"} for e in fails)
        assert versions_n() == 2 and DB.translation_rollbacks.count_documents({}) == 0

    def test_when_the_audit_log_is_down_nothing_is_changed(self, fault):
        sa = super_admin(); scenario(sa, drafts=False)
        def down(*a, **k):
            if k.get("strict"):
                raise AU.AuditWriteError("down")
            return False
        fault.setattr(AU, "record", down)
        before = working_full()
        r = rb(sa, 1)
        assert r.status_code == 503 and code(r) == "audit_unavailable"
        assert working_full() == before and versions_n() == 2 and DB.translation_rollbacks.count_documents({}) == 0
