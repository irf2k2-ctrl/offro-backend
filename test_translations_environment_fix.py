"""Staging 'Environment not identified' / 'No languages yet' fix. Run: python -m pytest test_translations_environment_fix.py -q -W ignore
Covers: environment inference rules (staging only, exact IDs), languages API (base language, no duplicates, nothing overwritten),
syncing the real Phase I Flutter ARB through the existing sync mechanism, and its draft Arabic passing the module's validation."""
import os

import pytest

from services import admin_otp_test_mode as OTM
from services import app_environment as AE
from services import translations as T
from tools.sync_translation_keys import parse_arb
from translation_test_support import DB, add_ar, client, editor, setup_env, super_admin, wipe_translation_data

A = "/admin/translations"
HERE = os.path.dirname(os.path.abspath(__file__))
PHASEI_EN = os.path.join(HERE, "test_data", "app_en_phasei.arb")
PHASEI_AR = os.path.join(HERE, "test_data", "app_ar_phasei_draft.arb")


def staging_ids(mp, project=OTM.STAGING_PROJECT_ID, env=OTM.STAGING_ENVIRONMENT_ID, service=OTM.STAGING_SERVICE_ID):
    for k, v in (("RAILWAY_PROJECT_ID", project), ("RAILWAY_ENVIRONMENT_ID", env), ("RAILWAY_SERVICE_ID", service)):
        if v is None:
            mp.delenv(k, raising=False)
        else:
            mp.setenv(k, v)


# ═════ environment identification ═════════════════════════════════════════════════════════════════════════════════
class TestEnvironmentIdentification:
    def test_staging_service_without_the_variable_is_identified_from_its_exact_ids(self, monkeypatch):
        setup_env(monkeypatch, None)
        staging_ids(monkeypatch)
        s = AE.status()
        assert s["classification"] == "staging" and s["source"] == "staging_ids" and s["reason"] == "inferred_from_staging_ids"

    def test_staging_named_production_by_railway_is_still_staging(self, monkeypatch):
        setup_env(monkeypatch, None)
        staging_ids(monkeypatch)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")          # Railway's default environment name
        assert AE.classification() == "staging"

    def test_nothing_is_ever_inferred_as_production(self, monkeypatch):
        setup_env(monkeypatch, None)
        assert AE.classification() == "unknown"                                       # no variable, no ids
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
        assert AE.classification() == "unknown"                                       # platform name alone proves nothing
        staging_ids(monkeypatch, project="11111111-1111-1111-1111-111111111111", env="22222222-2222-2222-2222-222222222222",
                    service="33333333-3333-3333-3333-333333333333")
        assert AE.classification() == "unknown"                                       # other (e.g. production) ids

    @pytest.mark.parametrize("missing", ["project", "env", "service"])
    def test_partial_or_wrong_ids_are_not_enough(self, monkeypatch, missing):
        setup_env(monkeypatch, None)
        staging_ids(monkeypatch, **{missing: None})
        assert AE.classification() == "unknown"
        staging_ids(monkeypatch, **{missing: "not-the-staging-id"})
        assert AE.classification() == "unknown"

    def test_an_explicit_invalid_value_is_not_overridden_by_the_ids(self, monkeypatch):
        setup_env(monkeypatch, "stage")
        staging_ids(monkeypatch)
        assert AE.classification() == "unknown" and AE.status()["reason"] == "environment_value_invalid"

    def test_production_declared_on_the_staging_service_is_still_refused(self, monkeypatch):
        setup_env(monkeypatch, "production")
        staging_ids(monkeypatch)
        assert AE.classification() == "unknown"

    def test_the_explicit_variable_still_wins(self, monkeypatch):
        setup_env(monkeypatch, "staging")
        assert AE.classification() == "staging" and AE.status()["source"] == "explicit"
        setup_env(monkeypatch, "production")
        assert AE.classification() == "production"

    def test_whatsapp_setting_is_irrelevant(self, monkeypatch):
        setup_env(monkeypatch, None)
        staging_ids(monkeypatch)
        monkeypatch.setenv("WA_CAMPAIGN_ENVIRONMENT", "production")
        assert AE.classification() == "staging"

    def test_environment_endpoint_reports_the_reason(self, monkeypatch):
        sa = super_admin()
        setup_env(monkeypatch, None)
        env = client.get(A + "/environment", headers=sa).json()["environment"]
        assert env["classification"] == "unknown" and env["reason"] == "environment_not_set"
        staging_ids(monkeypatch)
        env = client.get(A + "/environment", headers=sa).json()["environment"]
        assert env["classification"] == "staging" and env["reason"] == "inferred_from_staging_ids"
        assert "RAILWAY" not in str(env) and OTM.STAGING_SERVICE_ID not in str(env)    # no ids or secrets leak

    def test_unknown_environment_still_refuses_every_change_but_reads_work(self, monkeypatch):
        sa = super_admin()
        setup_env(monkeypatch, None)
        for r in (client.post(A + "/languages", headers=sa, json={"code": "ar", "name_en": "Arabic", "name_native": "العربية", "direction": "rtl"}),
                  client.post(A + "/kill-switch", headers=sa, json={"enabled": True, "reason": "x"}),
                  client.post(A + "/publish", headers=sa, json={"lang": "ar", "note": "x"})):
            assert r.status_code in (503, 404), r.text
        assert DB.translation_languages.count_documents({}) == 0
        assert client.get(A + "/languages", headers=sa).status_code == 200
        assert client.get(A + "/versions?lang=ar", headers=sa).status_code == 404         # unknown language, not a crash
        assert client.get(A + "/audit", headers=sa).status_code == 200


# ═════ languages ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestLanguages:
    @pytest.fixture(autouse=True)
    def staging(self, monkeypatch):
        setup_env(monkeypatch, None)
        staging_ids(monkeypatch)                         # exactly the situation of the staging service without OFFRO_ENVIRONMENT

    def test_empty_registry_returns_the_built_in_english_base_and_no_records(self):
        sa = super_admin()
        d = client.get(A + "/languages", headers=sa).json()
        assert d["languages"] == [] and d["base_language"]["code"] == "en" and d["base_language"]["bundled"] is True
        assert DB.translation_languages.count_documents({}) == 0                      # reading never creates anything

    def test_super_admin_adds_arabic_once_and_it_is_listed(self):
        sa = super_admin()
        assert add_ar(sa).status_code == 200
        langs = client.get(A + "/languages", headers=sa).json()["languages"]
        assert [(l["code"], l["direction"], l["status"]) for l in langs] == [("ar", "rtl", "draft")]
        assert langs[0]["status_indicators"]["app"]["published_version"] is None and "last_updated" in langs[0]

    def test_adding_it_twice_never_creates_a_duplicate(self):
        sa = super_admin()
        assert add_ar(sa).status_code == 200
        r = add_ar(sa)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "language_exists"
        assert DB.translation_languages.count_documents({"code": "ar"}) == 1

    def test_listing_and_environment_calls_do_not_modify_existing_data(self):
        sa = super_admin()
        add_ar(sa)
        DB.translation_values.insert_one({"lang": "ar", "key": "loginTitle", "track": "app", "value": "مرحبا", "status": "published"})
        before = (list(DB.translation_languages.find({}, {"_id": 0, "created_at": 0})), list(DB.translation_values.find({}, {"_id": 0})))
        for _ in range(2):
            client.get(A + "/languages", headers=sa); client.get(A + "/environment", headers=sa); client.get(A + "/audit", headers=sa)
        after = (list(DB.translation_languages.find({}, {"_id": 0, "created_at": 0})), list(DB.translation_values.find({}, {"_id": 0})))
        assert before == after

    def test_non_super_admin_cannot_add_languages_and_gets_the_same_list(self):
        sa = super_admin(); ed = editor()
        assert client.post(A + "/languages", headers=ed, json={"code": "ar", "name_en": "Arabic", "name_native": "العربية", "direction": "rtl"}).status_code == 403
        add_ar(sa)
        assert [l["code"] for l in client.get(A + "/languages", headers=ed).json()["languages"]] == ["ar"]

    def test_the_other_tabs_load_for_a_new_language(self):
        sa = super_admin()
        add_ar(sa)
        assert client.get(A + "/versions?lang=ar", headers=sa).json()["versions"] == []
        assert client.get(A + "/strings?lang=ar", headers=sa).json()["total"] == 0
        assert client.get(A + "/preview?lang=ar", headers=sa).status_code == 200
        assert client.get(A + "/audit", headers=sa).json()["entries"]                 # add_language is audited
        assert client.get(A + "/environment", headers=sa).json()["package_config"]["environment"] == "staging"
        assert client.get(A + "/kill-switch", headers=sa).status_code == 200


# ═════ Phase I Flutter keys through the existing sync mechanism ═════════════════════════════════════════════════════
class TestPhaseIKeys:
    @pytest.fixture(autouse=True)
    def staging(self, monkeypatch):
        setup_env(monkeypatch, "staging")

    def test_the_real_phase_i_arb_parses_and_syncs(self):
        entries = parse_arb(PHASEI_EN)
        assert len(entries) == 72 and not any(e["is_plural"] or e["group"] == "legal" for e in entries)
        r = T.sync_keys(entries)
        assert r == {"added": 72, "updated": 0, "deprecated": 0}
        assert DB.translation_keys.count_documents({"deprecated": {"$ne": True}}) == 72

    def test_syncing_again_is_idempotent_and_never_touches_translations(self):
        sa = super_admin()
        T.sync_keys(parse_arb(PHASEI_EN))
        add_ar(sa)
        assert client.put(A + "/strings/ar/loginGuest", headers=sa, json={"value": "المتابعة كضيف"}).status_code == 200
        before = DB.translation_values.find_one({"lang": "ar", "key": "loginGuest"}, {"_id": 0})
        assert T.sync_keys(parse_arb(PHASEI_EN)) == {"added": 0, "updated": 72, "deprecated": 0}
        assert DB.translation_values.find_one({"lang": "ar", "key": "loginGuest"}, {"_id": 0}) == before
        assert DB.translation_languages.count_documents({}) == 1

    def test_every_draft_arabic_value_passes_the_modules_own_validation(self):
        import json
        T.sync_keys(parse_arb(PHASEI_EN))
        ar = {k: v for k, v in json.load(open(PHASEI_AR, encoding="utf-8")).items() if not k.startswith("@")}
        assert len(ar) == 72
        bad = {}
        for k, v in ar.items():
            try:
                T.validate_value(T.get_key(k), v)
            except T.TranslationError as e:
                bad[k] = e.code
        assert bad == {}, bad

    def test_strings_tab_lists_the_synced_keys_with_english_source(self):
        sa = super_admin()
        T.sync_keys(parse_arb(PHASEI_EN))
        add_ar(sa)
        d = client.get(A + "/strings?lang=ar&page=1", headers=sa).json()
        assert d["total"] == 72 and d["rows"][0]["english"] and d["rows"][0]["status"] == "missing"
        row = [r for r in client.get(A + "/strings?lang=ar&q=otpSentTo", headers=sa).json()["rows"]][0]
        assert row["placeholders"] == ["phone"]
