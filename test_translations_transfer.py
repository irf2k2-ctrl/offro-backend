"""Stage 4: the one-time transfer script (tools/transfer_translations.py). Mock databases only (mongomock): no network, no real database,
no credentials. Run:  python -m pytest test_translations_transfer.py -q -W ignore"""
import copy
import json
import os
import re

import mongomock
import pytest

from services import translations as T
from tools import transfer_translations as X
from translation_test_support import DB, add_ar, client, wipe_translation_data, publish, put, setup_env, super_admin, sync_fixture_keys

SRC = "https://x"


@pytest.fixture(autouse=True)
def staging(monkeypatch):
    setup_env(monkeypatch, "staging")
    sync_fixture_keys()


def target_db():
    return mongomock.MongoClient()["prod_like"]


def staged():
    """Real staging data produced through the real service: Arabic app + legal published."""
    h = super_admin()
    add_ar(h)
    assert put(h, "loginTitle", "مرحبا بك في عرض").status_code == 200
    assert put(h, "otpSent", "أرسلنا رمزا إلى {phone}").status_code == 200
    assert put(h, "privacyIntro", "بالمتابعة فإنك توافق على سياسة الخصوصية.").status_code == 200
    assert publish(h).status_code == 200
    assert publish(h, track="legal").status_code == 200
    return h


def package(**kw):
    return X.export_package(DB, "staging", source_fp=kw.get("fp", "srcfp"))


def reseal(pkg):
    pkg["content_sha256"] = X.sha256_hex(X.canonical_json(pkg["data"]))
    return pkg


class TestHashing:
    def test_script_hash_equals_the_service_hash(self):
        s = {"b": "ب", "a": "x {p}"}
        assert X.hash_strings(s) == T.hash_strings(s)
        assert X.canonical_json(s) == T.canonical_json(s)
        assert X.value_hash("ا") == T.value_hash("ا")


class TestExport:
    def test_exports_latest_version_per_track_and_is_valid(self):
        staged()
        pkg = package()
        assert X.validate_package(pkg) == []
        assert {(v["track"], v["lang"]) for v in pkg["data"]["versions"]} == {("app", "ar"), ("legal", "ar")}
        assert pkg["counts"]["versions"] == 2 and pkg["content_sha256"]

    def test_export_is_read_only(self):
        staged()
        before = {c: list(DB[c].find({})) for c in DB.list_collection_names()}
        package()
        after = {c: list(DB[c].find({})) for c in DB.list_collection_names()}
        assert before == after

    def test_only_latest_version_is_exported(self):
        h = staged()
        assert put(h, "loginTitle", "عنوان جديد").status_code == 200
        assert publish(h).json()["version"] == 2
        v = [x for x in package()["data"]["versions"] if x["track"] == "app"][0]
        assert v["version"] == 2 and v["strings"]["loginTitle"] == "عنوان جديد"

    def test_drafts_are_not_exported(self):
        h = staged()
        put(h, "loginTitle", "مسودة غير منشورة")
        text = json.dumps(package(), ensure_ascii=False)
        assert "مسودة غير منشورة" not in text

    def test_nothing_published_refuses(self):
        with pytest.raises(X.TransferError, match="nothing to export"):
            package()

    def test_source_labelled_for_another_environment_refuses(self):
        staged()
        DB.translation_versions.update_many({}, {"$set": {"environment": "production"}})
        with pytest.raises(X.TransferError, match="refusing to export"):
            package()

    def test_track_selection(self):
        staged()
        pkg = X.export_package(DB, "staging", tracks=("app",))
        assert [v["track"] for v in pkg["data"]["versions"]] == ["app"]

    def test_no_secrets_in_the_package(self):
        staged()
        text = json.dumps(package())
        assert "mongodb" not in text.lower() and "password" not in text.lower() and "@" not in text.replace("support@", "")


class TestPackageValidation:
    def test_wrong_format_and_version(self):
        assert X.validate_package({"format": "other"})
        staged()
        p = package()
        p["format_version"] = 99
        assert "unsupported" in X.validate_package(p)[0]

    def test_tampered_content_is_detected_by_the_package_hash(self):
        staged()
        p = package()
        p["data"]["versions"][0]["strings"]["loginTitle"] = "تلاعب"
        assert "content hash mismatch" in X.validate_package(p)[0]

    def test_version_hash_is_recomputed_even_if_package_is_resealed(self):
        staged()
        p = package()
        p["data"]["versions"][0]["strings"]["loginTitle"] = "تلاعب"
        reseal(p)
        assert any("SHA-256 hash does not match" in x for x in X.validate_package(p))

    def test_unknown_key_missing_language_bad_version_number(self):
        staged()
        p = package()
        v = p["data"]["versions"][0]
        v["strings"]["noSuchKey"] = "x"
        v["hash"] = X.hash_strings(v["strings"])
        v["lang"] = "xx"
        v["version"] = 0
        reseal(p)
        msgs = " | ".join(X.validate_package(p))
        assert "not in the key registry" in msgs and "language registry" in msgs and "version number" in msgs

    def test_placeholder_mismatch_and_wrong_track_and_plural(self):
        staged()
        p = package()
        app = [v for v in p["data"]["versions"] if v["track"] == "app"][0]
        app["strings"]["otpSent"] = "أرسلنا رمزا"
        app["strings"]["privacyIntro"] = "x"          # legal key inside the app track
        app["strings"]["visitsCount"] = "x"           # plural key
        app["hash"] = X.hash_strings(app["strings"])
        reseal(p)
        msgs = " | ".join(X.validate_package(p))
        assert "placeholder_mismatch" in msgs and "belongs to the legal track" in msgs and "plural or deprecated" in msgs

    def test_duplicate_version_for_one_track_language(self):
        staged()
        p = package()
        p["data"]["versions"].append(copy.deepcopy(p["data"]["versions"][0]))
        reseal(p)
        assert any("more than one version" in x for x in X.validate_package(p))

    def test_invalid_language_details(self):
        staged()
        p = package()
        p["data"]["languages"][0]["direction"] = "up"
        p["data"]["languages"][0]["status"] = "weird"
        reseal(p)
        msgs = " | ".join(X.validate_package(p))
        assert "invalid direction" in msgs and "invalid status" in msgs

    def test_problem_messages_never_contain_translation_text(self):
        staged()
        p = package()
        p["data"]["versions"][0]["strings"]["loginTitle"] = "نص سري"
        reseal(p)
        assert "نص سري" not in " ".join(X.validate_package(p))


class TestPlanAndDryRun:
    def test_empty_target_plans_everything_and_writes_nothing(self):
        staged()
        t = target_db()
        plan = X.plan_import(package(), t, "production")
        assert not plan["conflicts"]
        assert len(plan["insert"][X.LANGS]) == 1 and len(plan["insert"][X.VERSIONS]) == 2 and len(plan["insert"][X.VALUES]) == 3
        assert t.list_collection_names() == []

    def test_report_shows_before_and_after_without_text(self):
        staged()
        rep = X.format_report(X.plan_import(package(), target_db(), "production"))
        assert "nothing published" in rep and "-> v1" in rep and "No conflicts." in rep
        assert "مرحبا" not in rep

    def test_same_environment_refused(self):
        staged()
        with pytest.raises(X.TransferError, match="same"):
            X.plan_import(package(), target_db(), "staging")

    def test_same_server_and_database_refused(self):
        staged()
        with pytest.raises(X.TransferError, match="same server and database"):
            X.plan_import(package(fp="abc"), target_db(), "production", target_fp="abc")

    def test_target_with_other_environment_data_refused(self):
        staged()
        t = target_db()
        t.translation_versions.insert_one({"track": "app", "lang": "ar", "version": 1, "environment": "staging", "hash": "x", "strings": {}})
        with pytest.raises(X.TransferError, match="not a production database"):
            X.plan_import(package(), t, "production")

    def test_invalid_package_refused_before_the_target_is_read(self):
        staged()
        p = package()
        p["data"]["versions"][0]["hash"] = "0" * 64
        reseal(p)
        with pytest.raises(X.TransferError, match="validation failed"):
            X.plan_import(p, target_db(), "production")


class TestApply:
    def test_apply_copies_data_and_the_public_data_matches(self, monkeypatch):
        staged()
        pkg = package()
        t = target_db()
        plan = X.plan_import(pkg, t, "production")
        written = X.apply_plan(plan, t, "staging")
        assert written == {X.LANGS: 1, X.KEYS: len(pkg["data"]["keys"]), X.VALUES: 3, X.VERSIONS: 2}
        v = t.translation_versions.find_one({"track": "app", "lang": "ar"})
        src = DB.translation_versions.find_one({"track": "app", "lang": "ar"})
        assert (v["version"], v["hash"], v["strings"]) == (src["version"], src["hash"], src["strings"])
        assert X.hash_strings(v["strings"]) == v["hash"]
        assert v["environment"] == "production" and v["transferred_from"] == "staging"
        vals = {d["key"]: d for d in t.translation_values.find({"lang": "ar"})}
        assert all(d["status"] == "published" and d["value_hash"] == X.value_hash(d["value"]) for d in vals.values())
        assert X.validate_package(pkg) == []

    def test_second_run_is_idempotent_all_identical(self):
        staged()
        pkg, t = package(), target_db()
        X.apply_plan(X.plan_import(pkg, t, "production"), t, "staging")
        plan2 = X.plan_import(pkg, t, "production")
        assert not plan2["conflicts"] and all(len(x) == 0 for x in plan2["insert"].values())
        assert plan2["unchanged"][X.VERSIONS] == 2
        assert X.apply_plan(plan2, t, "staging") == {c: 0 for c in plan2["insert"]}
        assert t.translation_versions.count_documents({}) == 2

    def test_existing_different_published_version_is_a_conflict_and_nothing_is_overwritten(self):
        h = staged()
        t = target_db()
        X.apply_plan(X.plan_import(package(), t, "production"), t, "staging")
        put(h, "loginTitle", "عنوان آخر")
        publish(h)
        plan = X.plan_import(package(), t, "production")
        assert any("never overwrites" in c for c in plan["conflicts"])
        before = list(t.translation_versions.find({}))
        with pytest.raises(X.TransferError, match="conflicts"):
            X.apply_plan(plan, t, "staging")
        assert list(t.translation_versions.find({})) == before

    def test_conflicting_value_key_and_language_block_every_write(self):
        staged()
        t = target_db()
        t.translation_languages.insert_one({"code": "ar", "name_en": "Arabic", "name_native": "x", "direction": "rtl", "status": "active"})
        t.translation_keys.insert_one({"key": "loginTitle", "english": "Different", "placeholders": [], "is_plural": False,
                                       "group": "app", "max_length": 40, "deprecated": False})
        t.translation_values.insert_one({"lang": "ar", "key": "otpSent", "value": "قيمة أخرى"})
        plan = X.plan_import(package(), t, "production")
        assert len(plan["conflicts"]) >= 3
        with pytest.raises(X.TransferError):
            X.apply_plan(plan, t, "staging")
        assert t.translation_versions.count_documents({}) == 0 and t.translation_audit.count_documents({}) == 0

    def test_pre_synced_identical_registry_is_accepted(self):
        staged()
        t = target_db()
        for k in DB.translation_keys.find({}, {"_id": 0}):
            t.translation_keys.insert_one(k)
        plan = X.plan_import(package(), t, "production")
        assert not plan["conflicts"] and plan["unchanged"][X.KEYS] == len(list(DB.translation_keys.find({})))

    def test_kill_switch_audit_and_drafts_are_not_copied(self):
        h = staged()
        T.set_kill_switch(True, "test", {"name": "x"})
        put(h, "loginTitle", "مسودة")
        t = target_db()
        X.apply_plan(X.plan_import(package(), t, "production"), t, "staging")
        assert t.translation_state.count_documents({}) == 0
        assert [a["action"] for a in t.translation_audit.find({})] == ["transfer_import"]
        assert t.translation_values.find_one({"key": "loginTitle"})["value"] == "مرحبا بك في عرض"

    def test_audit_entry_has_counts_and_hash_but_no_text(self):
        staged()
        t = target_db()
        X.apply_plan(X.plan_import(package(), t, "production"), t, "staging")
        a = t.translation_audit.find_one({"action": "transfer_import"})
        assert a["outcome"] == "ok" and a["environment"] == "production" and a["details"]["written"][X.VERSIONS] == 2
        assert "مرحبا" not in json.dumps(a, default=str, ensure_ascii=False)

    def test_versions_are_written_last(self):
        staged()
        t = target_db()
        order = []
        real = mongomock.collection.Collection.insert_one

        def spy(self, doc, *a, **k):
            order.append(self.name)
            return real(self, doc, *a, **k)
        mongomock.collection.Collection.insert_one = spy
        try:
            X.apply_plan(X.plan_import(package(), t, "production"), t, "staging")
        finally:
            mongomock.collection.Collection.insert_one = real
        real_order = [c for c in order if c != X.AUDIT]
        first_version = real_order.index(X.VERSIONS)
        assert all(c == X.VERSIONS for c in real_order[first_version:])

    def test_failure_mid_write_is_reported_and_audited_failed(self, monkeypatch):
        staged()
        t = target_db()
        plan = X.plan_import(package(), t, "production")
        real = mongomock.collection.Collection.insert_one

        def boom(self, doc, *a, **k):
            if self.name == X.VALUES:
                raise RuntimeError("disk full")
            return real(self, doc, *a, **k)
        monkeypatch.setattr(mongomock.collection.Collection, "insert_one", boom)
        with pytest.raises(X.TransferError, match="write failed"):
            X.apply_plan(plan, t, "staging")
        monkeypatch.undo()
        assert t.translation_versions.count_documents({}) == 0          # nothing is served from a half copy
        assert t.translation_audit.find_one({"action": "transfer_import"})["outcome"] == "failed"

    def test_duplicate_appearing_during_write_stops(self, monkeypatch):
        staged()
        t = target_db()
        plan = X.plan_import(package(), t, "production")
        t.translation_languages.create_index("code", unique=True)
        t.translation_languages.insert_one({"code": "ar", "name_en": "x", "name_native": "x", "direction": "rtl", "status": "active"})
        with pytest.raises(X.TransferError, match="duplicate"):
            X.apply_plan(plan, t, "staging")


class TestCommandLine:
    """The CLI with an injected connection function (no real connection is ever made)."""

    def run(self, argv, dbs, capsys):
        calls = []

        def connect(var, name):
            calls.append((var, name))
            return dbs[var], {"SRC_VAR": "srcfp", "TGT_VAR": "tgtfp"}[var]
        rc = X.main(argv, connect=connect)
        return rc, capsys.readouterr().out, calls

    def exported(self, tmp_path, capsys):
        out = tmp_path / "p.json"
        rc, text, calls = self.run(["export", "--source-env-var", "SRC_VAR", "--source-db", "d", "--source-environment", "staging",
                                    "--out", str(out)], {"SRC_VAR": DB}, capsys)
        assert rc == 0 and out.exists()
        return out

    def test_dry_run_is_the_default(self, tmp_path, capsys):
        staged()
        p, t = self.exported(tmp_path, capsys), target_db()
        rc, out, calls = self.run(["import", str(p), "--target-env-var", "TGT_VAR", "--target-db", "d", "--target-environment", "production"],
                                  {"TGT_VAR": t}, capsys)
        assert rc == 0 and "DRY RUN" in out and t.list_collection_names() == []

    def test_apply_needs_the_database_confirmation(self, tmp_path, capsys):
        staged()
        p, t = self.exported(tmp_path, capsys), target_db()
        base = ["import", str(p), "--target-env-var", "TGT_VAR", "--target-db", "d", "--target-environment", "production", "--apply"]
        rc, out, calls = self.run(base, {"TGT_VAR": t}, capsys)
        assert rc == 2 and "--confirm-target-db" in out and calls == [] and t.list_collection_names() == []
        rc, out, _ = self.run(base + ["--confirm-target-db", "other"], {"TGT_VAR": t}, capsys)
        assert rc == 2 and t.list_collection_names() == []

    def test_apply_with_confirmation_writes(self, tmp_path, capsys):
        staged()
        p, t = self.exported(tmp_path, capsys), target_db()
        rc, out, _ = self.run(["import", str(p), "--target-env-var", "TGT_VAR", "--target-db", "d", "--target-environment", "production",
                               "--apply", "--confirm-target-db", "d"], {"TGT_VAR": t}, capsys)
        assert rc == 0 and "APPLIED" in out and t.translation_versions.count_documents({}) == 2

    def test_conflicts_exit_code_and_no_write(self, tmp_path, capsys):
        staged()
        p, t = self.exported(tmp_path, capsys), target_db()
        t.translation_versions.insert_one({"track": "app", "lang": "ar", "version": 9, "hash": "f" * 64, "strings": {"a": "b"}})
        rc, out, _ = self.run(["import", str(p), "--target-env-var", "TGT_VAR", "--target-db", "d", "--target-environment", "production",
                               "--apply", "--confirm-target-db", "d"], {"TGT_VAR": t}, capsys)
        assert rc == 3 and "CONFLICTS" in out and t.translation_versions.count_documents({}) == 1

    def test_export_never_overwrites_a_file(self, tmp_path, capsys):
        staged()
        p = self.exported(tmp_path, capsys)
        rc, out, _ = self.run(["export", "--source-env-var", "SRC_VAR", "--source-db", "d", "--source-environment", "staging",
                               "--out", str(p)], {"SRC_VAR": DB}, capsys)
        assert rc == 2 and "already exists" in out

    def test_damaged_package_file_is_refused(self, tmp_path, capsys):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        rc, out, calls = self.run(["import", str(p), "--target-db", "d", "--target-environment", "production"], {}, capsys)
        assert rc == 2 and "cannot read" in out and calls == []

    def test_missing_environment_variable_is_a_clear_error_without_secrets(self, monkeypatch, capsys):
        monkeypatch.delenv("NOT_SET_VAR", raising=False)
        with pytest.raises(X.TransferError, match="NOT_SET_VAR is not set"):
            X._connect("NOT_SET_VAR", "d")

    def test_connection_errors_do_not_leak_the_connection_string(self, monkeypatch):
        monkeypatch.setenv("LEAKY", "mongodb://testuser:dummy-not-real@127.0.0.1:1/?serverSelectionTimeoutMS=200")
        with pytest.raises(X.TransferError) as e:
            X._connect("LEAKY", "d")
        assert "dummy-not-real" not in str(e.value) and "testuser" not in str(e.value)

    def test_fingerprint_ignores_credentials(self):
        a = X.fingerprint("mongodb://u1:p1@host-a:27017/x", "db")
        assert a == X.fingerprint("mongodb://u2:p2@HOST-A:27017/y", "db")
        assert a != X.fingerprint("mongodb://u1:p1@host-b:27017/x", "db")
        assert a != X.fingerprint("mongodb://u1:p1@host-a:27017/x", "other")


class TestStaticSafety:
    SRC_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "transfer_translations.py")

    def text(self):
        return open(self.SRC_FILE, encoding="utf-8").read()

    def test_no_credentials_or_connection_strings_in_the_script(self):
        t = self.text()
        assert not re.search(r"mongodb(\+srv)?://[^\s\"']*@", t)
        assert "railway.app" not in t and "mongodb.net" not in t

    def test_no_import_of_the_applications_own_database(self):
        t = self.text()
        assert "from database" not in t and "import database" not in t

    def test_no_delete_or_overwrite_operations(self):
        t = self.text()
        for bad in ("delete_one", "delete_many", "drop_collection", "drop_database", "replace_one", "update_many", "upsert", "bulk_write"):
            assert bad not in t, bad
        assert t.count("update_one") == 3          # only the audit entry's own outcome is updated

    def test_no_scheduler_or_background_loop(self):
        t = self.text()
        for bad in ("import sched", "import threading", "while True", "sleep(", "apscheduler", "import schedule"):
            assert bad not in t

    def test_dry_run_is_default_and_apply_is_explicit(self):
        t = self.text()
        assert 'action="store_true", help="actually write' in t and "if not a.apply" in t

    def test_nothing_in_the_backend_imports_the_script(self):
        root = os.path.dirname(os.path.abspath(__file__))
        for base in ("server.py", "routers", "services"):
            p = os.path.join(root, base)
            files = [p] if p.endswith(".py") else [os.path.join(p, f) for f in os.listdir(p) if f.endswith(".py")]
            for f in files:
                assert "transfer_translations" not in open(f, encoding="utf-8").read(), f


class TestEndToEndIntoAProductionLikeServer:
    """The package, applied to an empty database, makes a production-identified server serve exactly what staging served."""

    def test_production_serves_the_same_version_and_hash_then_publishes_v2_on_its_own(self, monkeypatch):
        h = staged()
        staging_manifest = client.get("/translations/manifest").json()
        staging_file = client.get("/translations/ar?track=app").json()
        pkg = package()
        wipe_translation_data()                              # this process now plays the (empty) production database
        setup_env(monkeypatch, "production")
        sync_fixture_keys()                                  # production registry, synced from the same ARB
        X.apply_plan(X.plan_import(pkg, DB, "production"), DB, "staging")
        man = client.get("/translations/manifest").json()
        assert man["languages"][0]["tracks"]["app"] == staging_manifest["languages"][0]["tracks"]["app"]
        prod_file = client.get("/translations/ar?track=app").json()
        assert prod_file["strings"] == staging_file["strings"] and prod_file["hash"] == staging_file["hash"]
        assert prod_file["version"] == staging_file["version"] == 1
        # the transferred working values are published, so a later edit + publish in production produces v2 containing everything
        sa = super_admin("prod-sa")
        assert put(sa, "loginTitle", "عنوان الإنتاج").status_code == 200
        r = publish(sa)
        assert r.status_code == 200 and r.json()["version"] == 2
        strings = client.get("/translations/ar?track=app").json()["strings"]
        assert set(strings) == {"loginTitle", "otpSent"} and strings["loginTitle"] == "عنوان الإنتاج"

    def test_production_kill_switch_is_independent_of_the_transfer(self, monkeypatch):
        h = staged()
        pkg = package()
        wipe_translation_data()
        setup_env(monkeypatch, "production")
        sync_fixture_keys()
        X.apply_plan(X.plan_import(pkg, DB, "production"), DB, "staging")
        assert client.get("/translations/manifest").json()["kill_switch"] is False


class TestLoggingIsolation:
    def test_the_tool_never_reconfigures_the_root_logger_or_stacks_handlers(self, tmp_path):
        import logging
        root_before = list(logging.getLogger().handlers)
        X._setup_logging(str(tmp_path / "a.log"))
        X._setup_logging(str(tmp_path / "b.log"))
        assert list(logging.getLogger().handlers) == root_before
        assert len(X.logger.handlers) == 2
        X._setup_logging(None)
        assert len(X.logger.handlers) == 1
        X.logger.propagate = True
        for h in list(X.logger.handlers):
            X.logger.removeHandler(h)

    def test_log_file_contains_no_translation_text(self, tmp_path):
        staged()
        log = tmp_path / "t.log"
        out = tmp_path / "p.json"
        X.main(["export", "--source-db", "d", "--source-environment", "staging", "--out", str(out), "--log-file", str(log)],
               connect=lambda v, n: (DB, "fp"))
        for h in list(X.logger.handlers):
            h.flush()
        text = log.read_text(encoding="utf-8")
        assert "exported" in text and "مرحبا" not in text
        for h in list(X.logger.handlers):
            X.logger.removeHandler(h)
        X.logger.propagate = True


class TestEtagContractUsedByTheFlutterClient:
    """The Flutter client stores and replays ETags; it accepts only quoted, printable tags of at most 200 characters."""
    SHAPE = re.compile(r'^(?:W/)?"[\x21\x23-\x7E]{1,200}"$')

    def test_both_public_endpoints_send_well_formed_etags_and_honour_if_none_match(self):
        staged()
        for path in ("/translations/manifest", "/translations/ar?track=app"):
            r = client.get(path)
            tag = r.headers["etag"]
            assert self.SHAPE.match(tag), tag
            assert client.get(path, headers={"If-None-Match": tag}).status_code == 304
            assert client.get(path, headers={"If-None-Match": '"other"'}).status_code == 200

    def test_manifest_etag_changes_with_the_kill_switch_and_with_a_new_version(self):
        h = staged()
        m1 = client.get("/translations/manifest").headers["etag"]
        T.set_kill_switch(True, "t", {"name": "x"})
        m2 = client.get("/translations/manifest").headers["etag"]
        T.set_kill_switch(False, "t", {"name": "x"})
        assert m2 != m1 and client.get("/translations/manifest").headers["etag"] == m1
        put(h, "loginTitle", "عنوان جديد")
        publish(h)
        assert client.get("/translations/manifest").headers["etag"] != m1

    def test_language_etag_follows_the_manifest_hash(self):
        staged()
        tag = client.get("/translations/ar?track=app").headers["etag"].strip('"')
        assert tag == client.get("/translations/manifest").json()["languages"][0]["tracks"]["app"]["hash"]
