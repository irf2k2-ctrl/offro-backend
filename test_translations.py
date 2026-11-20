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
