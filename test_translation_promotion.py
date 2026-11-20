"""Phase G targeted tests: HMAC-signed promotion package (staging -> production), import as DRAFT only, explicit production
publish, secrets handling. Run:  python -m pytest test_translation_promotion.py -q -W ignore
Staging and production are simulated with the one in-memory database by wiping the translation collections between the two steps
(each real environment has its own database)."""
import copy
import hashlib
import hmac as _hmac

import pytest

from services import translation_audit as AU
from services import translation_promotion as P
from services import translations as T
from translation_test_support import (DB, PROD_OWN_SECRET, STAGING_SECRET, add_ar, audit_actions, client, editor, publish, put,
                                      setup_env, super_admin, sync_fixture_keys, wipe_translation_data)

A = "/admin/translations"


@pytest.fixture(autouse=True)
def staging(monkeypatch):
    setup_env(monkeypatch, "staging")
    sync_fixture_keys()


def approved_package(sa, *, approve=True):
    """In STAGING: author, publish, (approve), export. Returns the envelope."""
    add_ar(sa)
    put(sa, "loginTitle", "مرحبا بك")
    put(sa, "otpSent", "أرسلنا رمزاً إلى {phone}")
    assert publish(sa).status_code == 200
    if approve:
        assert client.post(A + "/approve-for-production", headers=sa, json={"lang": "ar", "version": 1, "note": "tested on device"}).status_code == 200
    r = client.get(A + "/package?lang=ar&version=1", headers=sa)
    return r


def resign(envelope, secret=STAGING_SECRET):
    envelope = copy.deepcopy(envelope)
    envelope["signature"] = _hmac.new(secret.encode(), T.canonical_json(envelope["payload"]).encode(), hashlib.sha256).hexdigest()
    return envelope


def to_production(monkeypatch, own=PROD_OWN_SECRET, verify=STAGING_SECRET):
    wipe_translation_data()
    setup_env(monkeypatch, "production", own=own, verify=verify)
    sync_fixture_keys()


# ═════ secrets ═══════════════════════════════════════════════════════════════════════════════════════════════════════
class TestSecrets:
    @pytest.mark.parametrize("secret,problem", [("", "missing"), ("short", "too_short"), ("a" * 40, "low_variety"),
                                                ("changeme-" + "Zq7Lm2Xv9Rt4" * 3, "looks_like_a_placeholder")])
    def test_weak_secrets_are_refused(self, secret, problem):
        assert P.strength_problem(secret) == problem

    def test_strong_secret_ok_and_fingerprint_hides_it(self):
        assert P.strength_problem(STAGING_SECRET) is None
        fp = P.secret_fingerprint(STAGING_SECRET)
        assert len(fp) == 8 and fp not in STAGING_SECRET and P.secret_fingerprint(PROD_OWN_SECRET) != fp

    def test_config_status_never_contains_a_secret(self, monkeypatch):
        sa = super_admin()
        body = client.get(A + "/environment", headers=sa).text
        assert STAGING_SECRET not in body and "package_config" in body
        to_production(monkeypatch)
        body = client.get(A + "/environment", headers=super_admin("sa2")).text
        assert STAGING_SECRET not in body and PROD_OWN_SECRET not in body
        cfg = client.get(A + "/environment", headers=super_admin("sa3")).json()["package_config"]
        assert cfg["ready_to_import"] is True and cfg["secrets_distinct"] is True
        assert cfg["verify_fingerprint"] == P.secret_fingerprint(STAGING_SECRET) != cfg["own_fingerprint"]

    def test_secrets_are_never_hardcoded(self):
        import inspect
        src = inspect.getsource(P)
        assert STAGING_SECRET not in src and PROD_OWN_SECRET not in src
        assert "os.getenv" in src

    def test_export_disabled_without_a_strong_secret(self, monkeypatch):
        sa = super_admin()
        approved_package(sa)
        monkeypatch.delenv("TRANSLATION_PACKAGE_SECRET")
        r = client.get(A + "/package?lang=ar&version=1", headers=sa)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "package_secret_not_configured"
        monkeypatch.setenv("TRANSLATION_PACKAGE_SECRET", "tooshort")
        assert client.get(A + "/package?lang=ar&version=1", headers=sa).status_code == 503


# ═════ staging: approve + export ═════════════════════════════════════════════════════════════════════════════════════
class TestExport:
    def test_unapproved_version_cannot_be_exported(self):
        sa = super_admin()
        r = approved_package(sa, approve=False)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "version_not_approved"

    def test_approved_version_exports_a_signed_package(self):
        sa = super_admin()
        r = approved_package(sa).json()
        assert r["alg"] == "HMAC-SHA256" and r["payload"]["source_environment"] == "staging"
        assert r["payload"]["hash"] == T.hash_strings(r["payload"]["strings"])
        assert r["payload"]["approval"]["approved_by"]["name"] == "Super Admin User"
        assert r["signature"] == resign(r)["signature"]

    def test_approval_rules(self):
        sa = super_admin()
        approved_package(sa)
        assert client.post(A + "/approve-for-production", headers=sa, json={"lang": "ar", "version": 1, "note": "x"}).json()["detail"]["code"] == "already_approved"
        assert client.post(A + "/approve-for-production", headers=sa, json={"lang": "ar", "version": 9, "note": "x"}).status_code == 404
        assert client.post(A + "/approve-for-production", headers=sa, json={"lang": "ar", "version": 1, "note": ""}).status_code == 422
        assert client.post(A + "/approve-for-production", headers=editor(), json={"lang": "ar", "version": 1, "note": "x"}).status_code == 403

    def test_export_and_approval_are_audited(self):
        sa = super_admin()
        approved_package(sa)
        assert {"approve_for_production", "package_export"} <= set(audit_actions("ok"))

    def test_staging_cannot_import_and_production_cannot_export(self, monkeypatch):
        sa = super_admin()
        env = approved_package(sa).json()
        r = client.post(A + "/package/import", headers=sa, json=env)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "package_import_production_only"
        to_production(monkeypatch)
        add = client.get(A + "/package?lang=ar&version=1", headers=super_admin("sa-prod"))
        assert add.status_code == 403 and add.json()["detail"]["code"] == "package_export_staging_only"


# ═════ production: import as DRAFT, then explicit publish ═════════════════════════════════════════════════════════════
class TestProductionImport:
    def imported(self, monkeypatch):
        sa = super_admin()
        env = approved_package(sa).json()
        to_production(monkeypatch)
        psa = super_admin("prod-sa")
        r = client.post(A + "/package/import", headers=psa, json=env)
        return psa, env, r

    def test_import_creates_a_draft_and_does_not_publish(self, monkeypatch):
        psa, env, r = self.imported(monkeypatch)
        assert r.status_code == 200 and r.json()["published"] is False
        assert DB.translation_versions.count_documents({}) == 0                           # NOT published
        assert DB.translation_values.count_documents({"lang": "ar", "source": "package"}) == 2
        assert DB.translation_languages.find_one({"code": "ar"})["status"] == "draft"
        assert client.get("/translations/manifest").json()["languages"] == []             # the app sees nothing yet
        assert client.get("/translations/ar").status_code == 404
        assert "package_import" in audit_actions("ok")

    def test_explicit_production_publish_then_goes_live(self, monkeypatch):
        psa, env, _ = self.imported(monkeypatch)
        r = publish(psa, note="promote v1")
        assert r.status_code == 200 and r.json()["version"] == 1
        v = DB.translation_versions.find_one({"lang": "ar"})
        assert v["promoted_from"]["environment"] == "staging" and v["promoted_from"]["version"] == 1
        assert v["promoted_from"]["hash"] == env["payload"]["hash"] and v["approval"]["note"] == "tested on device"
        assert v["environment"] == "production"
        assert client.get("/translations/ar").json()["strings"]["loginTitle"] == "مرحبا بك"
        assert DB.translation_state.find_one({"_id": "state"}).get("pending", {}) == {}
        assert publish(psa).status_code == 409                                             # nothing pending any more

    def test_publish_needs_a_pending_package(self, monkeypatch):
        to_production(monkeypatch)
        psa = super_admin("prod-sa")
        r = publish(psa)
        assert r.status_code in (404, 409)

    def test_only_super_admin_may_import_or_publish(self, monkeypatch):
        psa, env, _ = self.imported(monkeypatch)
        ed = editor("prod-ed")
        assert client.post(A + "/package/import", headers=ed, json=env).status_code == 403
        assert publish(ed).status_code == 403

    def test_tampered_strings_fail_the_signature(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        to_production(monkeypatch)
        bad = copy.deepcopy(env); bad["payload"]["strings"]["loginTitle"] = "نص مزور"
        r = client.post(A + "/package/import", headers=super_admin("p"), json=bad)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "package_signature_invalid"
        assert DB.translation_values.count_documents({}) == 0

    def test_tampered_strings_with_recomputed_hash_still_fail_the_signature(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        to_production(monkeypatch)
        bad = copy.deepcopy(env); bad["payload"]["strings"]["loginTitle"] = "نص مزور"
        bad["payload"]["hash"] = T.hash_strings(bad["payload"]["strings"])
        assert client.post(A + "/package/import", headers=super_admin("p"), json=bad).json()["detail"]["code"] == "package_signature_invalid"

    def test_signed_with_the_wrong_secret(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        to_production(monkeypatch)
        forged = resign(env, "Zy9wK3pQm7Lx2VbN6cTd8RjH4sFa1UgE5oXi")
        assert client.post(A + "/package/import", headers=super_admin("p"), json=forged).json()["detail"]["code"] == "package_signature_invalid"

    def test_missing_or_malformed_signature(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        to_production(monkeypatch)
        for bad in ({"payload": env["payload"], "alg": "HMAC-SHA256"}, {"signature": "x", "alg": "HMAC-SHA256"}, {},
                    {**env, "alg": "none"}, {**env, "signature": ""}):
            r = client.post(A + "/package/import", headers=super_admin("p"), json=bad)
            assert r.status_code in (403, 422), bad

    def test_correctly_signed_but_unapproved_package(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        to_production(monkeypatch)
        no = copy.deepcopy(env); no["payload"]["approval"] = None
        assert client.post(A + "/package/import", headers=super_admin("p"), json=resign(no)).json()["detail"]["code"] == "package_not_approved"
        mism = copy.deepcopy(env); mism["payload"]["approval"]["hash"] = "0" * 64
        assert client.post(A + "/package/import", headers=super_admin("p2"), json=resign(mism)).json()["detail"]["code"] == "package_not_approved"

    def test_package_not_from_staging_or_wrong_kind(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        to_production(monkeypatch)
        a = copy.deepcopy(env); a["payload"]["source_environment"] = "production"
        assert client.post(A + "/package/import", headers=super_admin("p"), json=resign(a)).json()["detail"]["code"] == "package_not_from_staging"
        b = copy.deepcopy(env); b["payload"]["kind"] = "other"
        assert client.post(A + "/package/import", headers=super_admin("p2"), json=resign(b)).json()["detail"]["code"] == "package_kind_or_schema_invalid"

    def test_correctly_signed_but_bad_values_are_rejected(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        to_production(monkeypatch)
        for strings, label in (({"loginTitle": "<b>x</b>"}, "html"), ({"nope": "x"}, "unknown key"),
                               ({"otpSent": "no placeholder"}, "placeholder"), ({"visitsCount": "x"}, "plural"),
                               ({"privacyIntro": "x"}, "legal key in app package")):
            bad = copy.deepcopy(env); bad["payload"]["strings"] = strings; bad["payload"]["hash"] = T.hash_strings(strings)
            bad["payload"]["approval"]["hash"] = bad["payload"]["hash"]
            r = client.post(A + "/package/import", headers=super_admin("p-" + label), json=resign(bad))
            assert r.status_code == 422 and r.json()["detail"]["code"] == "package_values_rejected", label
        assert DB.translation_values.count_documents({}) == 0

    def test_identical_secrets_are_refused(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        to_production(monkeypatch, own=STAGING_SECRET, verify=STAGING_SECRET)
        r = client.post(A + "/package/import", headers=super_admin("p"), json=env)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "package_secrets_identical"

    def test_missing_or_weak_secrets_disable_import(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        for own, ver in ((PROD_OWN_SECRET, None), (None, STAGING_SECRET), (PROD_OWN_SECRET, "weak"), ("weak", STAGING_SECRET)):
            to_production(monkeypatch, own=own, verify=ver)
            r = client.post(A + "/package/import", headers=super_admin("p"), json=env)
            assert r.status_code == 503 and r.json()["detail"]["code"] == "package_secret_not_configured"
            DB.dashboard_users.delete_many({}); DB.dashboard_roles.delete_many({})

    def test_publish_refused_if_the_draft_is_changed_after_import(self, monkeypatch):
        psa, env, _ = self.imported(monkeypatch)
        DB.translation_values.update_one({"lang": "ar", "key": "loginTitle"}, {"$set": {"value": "تعديل مباشر"}})
        r = publish(psa)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "pending_package_mismatch"
        assert DB.translation_versions.count_documents({}) == 0

    def test_publish_refused_if_the_pending_record_is_tampered(self, monkeypatch):
        psa, env, _ = self.imported(monkeypatch)
        DB.translation_state.update_one({"_id": "state"}, {"$set": {"pending.app:ar.source_version": 99}})
        r = publish(psa)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "pending_package_tampered"

    def test_publish_refused_if_the_seal_secret_changed(self, monkeypatch):
        psa, env, _ = self.imported(monkeypatch)
        monkeypatch.setenv("TRANSLATION_PACKAGE_SECRET", "Nq5hT8wLc2ZpD7tYb4RjK9mXe3UaS6oFgV1i")
        assert publish(psa).json()["detail"]["code"] == "pending_package_tampered"

    def test_import_replaces_the_whole_draft_for_that_language_and_track(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        to_production(monkeypatch)
        T.add_language("ar", "Arabic", "العربية", "rtl") if False else None
        DB.translation_languages.insert_one({"code": "ar", "name_en": "Arabic", "name_native": "العربية", "direction": "rtl", "status": "draft", "bundled": False})
        DB.translation_values.insert_one({"lang": "ar", "key": "resendOtp", "track": "app", "value": "old leftover", "status": "modified"})
        assert client.post(A + "/package/import", headers=super_admin("p"), json=env).status_code == 200
        assert DB.translation_values.find_one({"lang": "ar", "key": "resendOtp"}) is None

    def test_direction_mismatch_with_an_existing_language(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        to_production(monkeypatch)
        DB.translation_languages.insert_one({"code": "ar", "name_en": "Arabic", "name_native": "العربية", "direction": "ltr", "status": "draft", "bundled": False})
        assert client.post(A + "/package/import", headers=super_admin("p"), json=env).json()["detail"]["code"] == "package_direction_mismatch"

    def test_production_cannot_roll_back_directly(self, monkeypatch):
        """Rollback would create a version without a signed staging package -> blocked; versions are untouched."""
        sa = super_admin(); env = approved_package(sa).json()
        # staging makes a second version so there is something to roll back to
        put(sa, "loginTitle", "عنوان جديد"); assert publish(sa, note="v2").status_code == 200
        to_production(monkeypatch)
        psa = super_admin("prod-sa")
        assert client.post(A + "/package/import", headers=psa, json=env).status_code == 200
        assert publish(psa).status_code == 200                                              # v1 via the package
        before = list(DB.translation_versions.find({}, {"_id": 0, "version": 1, "hash": 1}))
        for body in ({"lang": "ar", "version": 1, "note": "x"}, {"lang": "ar", "version": 99, "note": "x"},
                     {"lang": "ar", "version": 1, "track": "legal", "note": "x"}):
            r = client.post(A + "/rollback", headers=psa, json=body)
            assert r.status_code == 403 and r.json()["detail"]["code"] == "production_edit_blocked", body
        assert list(DB.translation_versions.find({}, {"_id": 0, "version": 1, "hash": 1})) == before
        assert DB.translation_versions.count_documents({"restored_from": {"$ne": None}}) == 0
        assert client.get("/translations/ar").json()["strings"]["loginTitle"] == "مرحبا بك"
        assert not [r for r in DB.translation_audit.find({"action": "rollback", "outcome": "ok"})]

    def test_production_rollback_blocked_even_with_a_pending_package(self, monkeypatch):
        psa, env, _ = self.imported(monkeypatch)
        assert client.post(A + "/rollback", headers=psa, json={"lang": "ar", "version": 1, "note": "x"}).status_code == 403
        assert DB.translation_versions.count_documents({}) == 0

    def test_rollback_reaches_production_only_via_the_package_flow(self, monkeypatch):
        """staging: v1, v2, rollback->v3 (restored_from 1), approve, export; production: import -> draft -> explicit publish."""
        sa = super_admin()
        approved_package(sa)
        put(sa, "loginTitle", "عنوان جديد"); publish(sa, note="v2")
        r = client.post(A + "/rollback", headers=sa, json={"lang": "ar", "version": 1, "note": "bad wording"})
        assert r.status_code == 200 and r.json()["version"] == 3 and r.json()["restored_from"] == 1     # staging rollback still works
        assert client.post(A + "/approve-for-production", headers=sa, json={"lang": "ar", "version": 3, "note": "re-tested"}).status_code == 200
        env3 = client.get(A + "/package?lang=ar&version=3", headers=sa).json()
        to_production(monkeypatch)
        psa = super_admin("prod-sa")
        assert client.post(A + "/package/import", headers=psa, json=env3).status_code == 200
        assert DB.translation_versions.count_documents({}) == 0                              # still not live
        assert publish(psa).status_code == 200
        assert client.get("/translations/ar").json()["strings"]["loginTitle"] == "مرحبا بك"

    def test_independent_kill_switch_is_the_production_emergency_lever(self, monkeypatch):
        psa, env, _ = self.imported(monkeypatch)
        publish(psa)
        assert client.post(A + "/kill-switch", headers=psa, json={"enabled": True}).status_code == 200
        assert client.get("/translations/manifest").json()["overrides_enabled"] is False
        wipe_translation_data()                                                            # staging's own database has its own switch
        setup_env(monkeypatch, "staging")
        assert T.kill_switch_on() is False

    def test_every_import_and_publish_step_is_audited(self, monkeypatch):
        psa, env, _ = self.imported(monkeypatch)
        publish(psa)
        rows = list(DB.translation_audit.find({"action": {"$in": ["package_import", "publish"]}}))
        assert [(r["action"], r["outcome"]) for r in rows] == [("package_import", "started"), ("package_import", "ok"),
                                                               ("publish", "started"), ("publish", "ok")]

    def test_failed_import_is_audited_as_failed(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        to_production(monkeypatch)
        bad = copy.deepcopy(env); bad["payload"]["strings"]["loginTitle"] = "x"
        client.post(A + "/package/import", headers=super_admin("p"), json=bad)
        assert ("package_import", "failed") in [(r["action"], r["outcome"]) for r in DB.translation_audit.find()]

    def test_import_is_refused_if_the_audit_cannot_be_written(self, monkeypatch):
        sa = super_admin(); env = approved_package(sa).json()
        to_production(monkeypatch)
        psa = super_admin("prod-sa")
        def failing(*a, strict=False, **k):
            if strict:
                raise AU.AuditWriteError("down")
            return False
        monkeypatch.setattr(AU, "record", failing)
        assert client.post(A + "/package/import", headers=psa, json=env).status_code == 503
        assert DB.translation_values.count_documents({}) == 0
