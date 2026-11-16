"""Tests of tools/wa_staging_concurrency_check.py itself (safety interlocks, output hygiene, cleanup scope, check logic) using
mongomock and an in-process runner. The real cross-process guarantee is what the script verifies on Staging MongoDB.
Run:  python -m pytest test_wa_staging_concurrency_script.py -q -W ignore"""
import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import time
import types
from datetime import datetime, timedelta, timezone

import mongomock
import pytest

import services.wa_campaign_manager as M
import tools.wa_staging_concurrency_check as S

SECRET = "SuperSecretPw9"
URL = "mongodb://appuser:%s@mongo-staging.proxy.example.net:27017/?authSource=admin" % SECRET


REAL_SCAN = S._assert_no_real_app_db


@pytest.fixture(autouse=True)
def isolate_ownership_from_the_host_os(monkeypatch, tmp_path):
    """Every test here runs with the ownership records in a private temp folder and WITHOUT the host's real Windows policy or real
    icacls/whoami. The Windows policy itself is exercised only by the dedicated tests in the "Windows permission policy" section, which
    re-enable it on top of FakeWindows. Without this, running the suite on a Windows PC would apply the real Windows rules (default
    %USERPROFILE% folder only, real ACL changes) to tests that are about HMAC proofs and cleanup logic."""
    monkeypatch.setattr(S, "_is_windows", lambda: False)
    monkeypatch.setenv("CONC_OWNER_DIR", str(tmp_path / "own"))

    def never(*a, **k):
        raise AssertionError("a test reached the REAL icacls/whoami; use FakeWindows")
    monkeypatch.setattr(S, "_icacls", never)
    monkeypatch.setattr(S, "_whoami", never)


def _tripwire_module():
    mod = types.ModuleType("database")
    mod.__dict__["__wa_tripwire__"] = True
    return mod


def _make_server(monkeypatch, with_app_db, owner_dir):
    client = mongomock.MongoClient()
    if with_app_db:
        client["offro_db"]["users"].insert_one({"name": "real customer", "phone": "919800000000"})
        client["offro_db"]["wa_campaigns"].insert_one({"name": "real campaign"})
    monkeypatch.setenv("CONC_MONGO_URL", URL)
    monkeypatch.setenv("CONC_OWNER_DIR", str(owner_dir))                       # local ownership records live in a temp dir
    monkeypatch.setattr(S, "_client", lambda: client)
    monkeypatch.setattr(S, "RATE_START_DELAY", 0)
    monkeypatch.setattr(S, "RATE_WINDOW_SECONDS", 1)
    monkeypatch.setattr(M, "db", None)
    monkeypatch.setitem(sys.modules, "database", _tripwire_module())           # the state a real fresh run is in
    monkeypatch.setattr(S, "_assert_no_real_app_db", lambda: None)             # (the real scan is tested separately)

    def seq_run(procs, target, args, timeout=0):          # same code paths, one process after another
        res = []
        for _ in range(procs):
            q = queue.Queue()

            class Snd:
                def put(self, v):
                    q.put(v)
            target(*(args + (Snd(),)))
            res.append(q.get())
        return res
    monkeypatch.setattr(S, "_run", seq_run)
    return client


@pytest.fixture
def server(monkeypatch, tmp_path):                                  # the Staging shape: holds the application database
    yield _make_server(monkeypatch, True, tmp_path / 'own')
    M.db = None


@pytest.fixture
def isolated(monkeypatch, tmp_path):                                # a fresh dedicated MongoDB: no application database at all
    yield _make_server(monkeypatch, False, tmp_path / 'own')
    M.db = None


def fp():
    return S.fingerprint(*S.parse_target(URL))


def app_snapshot(client):
    return {c: list(client["offro_db"][c].find({}, {"_id": 0})) for c in client["offro_db"].list_collection_names()}


def run_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["x", *argv])
    with pytest.raises(SystemExit) as e:
        S.main()
    return e.value.code


def staging(*extra):
    return ["--target", "staging", "--yes-this-is-staging", "--expect-target", fp(), *extra]


def isol(*extra):
    return ["--target", "isolated", "--yes-this-is-isolated", "--expect-target", fp(), *extra]


def leftovers(client):
    return [d for d in client.list_database_names() if d.startswith(S.PREFIX)]


def make_owned(client, run_id):
    """A genuinely tool-owned database: local record + stamped marker + stamped documents in every allowed collection."""
    token = S.create_record(run_id)
    db = client[S.PREFIX + run_id]
    S.write_marker(db, run_id, token)
    db["wa_campaigns"].insert_many(S.stamp("wa_campaigns", [{"x": 1}], run_id, token))
    db["wa_campaign_recipients"].insert_many(S.stamp("wa_campaign_recipients", [{"x": 1}, {"x": 2}], run_id, token))
    db["wa_campaign_rate"].insert_many(S.stamp("wa_campaign_rate", [{"_id": "s1000", "count": 0}], run_id, token))
    return db.name


# ---------------------------------------------------------------- target / fingerprint (issue 4)

def test_fingerprint_and_masking_never_expose_credentials(server, capsys):
    host, port = S.parse_target(URL)
    assert host == "mongo-staging.proxy.example.net" and port == 27017 and len(fp()) == 10
    assert SECRET not in S.mask_host(host) and "appuser" not in S.mask_host(host)
    assert S.show_target() == 0
    out = capsys.readouterr().out
    assert SECRET not in out and "appuser" not in out and fp() in out and "No writes were made" in out
    assert "mongo-staging.proxy" not in out                                   # host is masked unless --reveal-host


def test_show_target_says_the_fingerprint_is_not_proof_and_demands_a_manual_host_and_port_comparison(server, capsys):
    S.show_target()
    out = capsys.readouterr().out
    assert "does NOT prove" in out and "compare host AND port" in out.replace("Railway Staging", "").replace("MANDATORY manual check: ", "")
    assert "Production" in out
    assert "--target staging" in out                                          # role is reported


def test_reveal_host_prints_the_full_host_and_port_but_never_credentials(server, capsys):
    S.show_target(reveal=True)
    out = capsys.readouterr().out
    assert "mongo-staging.proxy.example.net:27017" in out and SECRET not in out and "appuser" not in out


def test_show_target_is_read_only(server):
    before = {n: server[n].list_collection_names() for n in server.list_database_names()}
    S.show_target()
    assert {n: server[n].list_collection_names() for n in server.list_database_names()} == before


def test_run_aborts_before_any_write_when_the_target_fingerprint_is_wrong_or_missing(server, monkeypatch, capsys):
    for extra in (["--expect-target", "0000000000"], []):
        assert run_main(monkeypatch, "--target", "staging", "--yes-this-is-staging", *extra) == 2
    assert leftovers(server) == []
    assert "ABORT" in capsys.readouterr().out


def test_run_requires_the_matching_confirmation_flag_and_sane_limits(server, monkeypatch):
    bad = [["--target", "staging", "--expect-target", fp()],
           ["--target", "isolated", "--yes-this-is-staging", "--expect-target", fp()],      # wrong flag for the path
           ["--target", "staging", "--yes-this-is-isolated", "--expect-target", fp()],
           staging("--processes", "999"), staging("--recipients", "99999999"), staging("--processes", "0")]
    for argv in bad:
        assert isinstance(run_main(monkeypatch, *argv), str)                  # exits with a message, before connecting or writing
    assert leftovers(server) == []


def test_missing_url_is_a_clear_error_without_secrets(monkeypatch):
    monkeypatch.delenv("CONC_MONGO_URL", raising=False)
    with pytest.raises(SystemExit) as e:
        S._url()
    assert "CONC_MONGO_URL" in str(e.value)


# ---------------------------------------------------------------- the two paths (issue 3)

def test_isolated_path_runs_on_a_fresh_server_without_the_application_database(isolated, monkeypatch, capsys):
    assert "offro_db" not in isolated.list_database_names()
    code = run_main(monkeypatch, *isol("--processes", "3", "--recipients", "60"))
    out = capsys.readouterr().out
    assert code == 0 and "RESULT: ALL PASS (8/8 checks)" in out, out
    assert "offro_db" not in isolated.list_database_names()                   # never created
    assert leftovers(isolated) == []


def test_show_target_on_a_fresh_server_reports_isolated_and_does_not_require_the_application_database(isolated, capsys):
    assert S.show_target() == 0
    out = capsys.readouterr().out
    assert "--target isolated" in out and "NOT present" in out


def test_staging_path_runs_and_leaves_application_data_untouched(server, monkeypatch, capsys):
    snap = app_snapshot(server)
    code = run_main(monkeypatch, *staging("--processes", "3", "--recipients", "60"))
    out = capsys.readouterr().out
    assert code == 0 and "RESULT: ALL PASS (8/8 checks)" in out, out
    for label in ("1. recipients", "2. campaign", "3. lease renewal", "4. expired lease", "5. delivery", "6. rate limit",
                  "7a. recovery", "7b. uncertain"):
        assert label in out
    assert out.count("PASS") >= 9 and "FAIL" not in out and "dropped and verified: yes" in out
    assert app_snapshot(server) == snap                                       # offro_db: not one document changed
    assert leftovers(server) == []
    assert SECRET not in out and "appuser" not in out


def test_each_path_refuses_the_wrong_kind_of_server_before_writing(server, monkeypatch, capsys):
    # (a) --target isolated against a server that holds offro_db
    assert run_main(monkeypatch, *isol()) == 2
    assert "holds 'offro_db'" in capsys.readouterr().out and leftovers(server) == []
    # (b) --target staging against a fresh server without offro_db
    fresh = mongomock.MongoClient()
    monkeypatch.setattr(S, "_client", lambda: fresh)
    assert run_main(monkeypatch, *staging()) == 2
    assert "needs 'offro_db' present" in capsys.readouterr().out and fresh.list_database_names() == []


def test_a_server_that_is_neither_staging_nor_fresh_is_refused_for_both_paths(isolated, monkeypatch, capsys):
    isolated["some_other_app"]["t"].insert_one({"keep": 1})                   # someone else's data, no offro_db
    assert S.server_role(isolated)[0] == "unknown"
    for argv in (isol(), staging()):
        assert run_main(monkeypatch, *argv) == 2
    assert leftovers(isolated) == [] and isolated["some_other_app"]["t"].count_documents({}) == 1
    assert "ABORT" in capsys.readouterr().out


def test_an_unreadable_database_list_fails_closed(isolated, monkeypatch):
    monkeypatch.setattr(S, "_list_dbs", lambda c: None)
    assert S.server_role(isolated)[0] == "unknown"
    assert run_main(monkeypatch, *isol()) == 2


# ---------------------------------------------------------------- run behaviour (preserved)

def test_a_broken_claim_is_reported_as_a_failed_check_and_nonzero_exit(server, monkeypatch, capsys):
    import services.wa_campaign_worker as W
    n = {"i": 0}

    def racing_claim(cid, wid, cfg, now):                                     # an implementation where two processes both "win" the same recipients
        n["i"] += 1
        db = server[next(d for d in server.list_database_names() if d.startswith(S.PREFIX))]
        return db[M.RECIPIENTS].find_one({"campaign_id": cid}) if n["i"] <= 4 else None
    monkeypatch.setattr(W, "claim_recipient", racing_claim)
    code = run_main(monkeypatch, *staging("--processes", "2", "--recipients", "10"))
    out = capsys.readouterr().out
    assert code == 1 and "1. recipients" in out and "FAIL" in out and "FAILURES" in out
    assert leftovers(server) == []                                            # still cleaned up (it was owned)


def test_runner_failure_becomes_a_failed_check_not_a_traceback(server, monkeypatch, capsys):
    def broken(procs, target, args, timeout=0):
        raise RuntimeError("child 1 exited (code 3) without a result")
    monkeypatch.setattr(S, "_run", broken)
    code = run_main(monkeypatch, *staging("--processes", "2", "--recipients", "10"))
    out = capsys.readouterr().out
    assert code == 1 and "without a result" in out and "Traceback" not in out


def test_connection_errors_are_reported_by_class_name_only(server, monkeypatch, capsys):
    def boom():
        raise ConnectionError("auth failed for appuser:%s@mongo-staging.proxy.example.net" % SECRET)
    monkeypatch.setattr(S, "_client", boom)
    code = run_main(monkeypatch, *staging())
    out = capsys.readouterr().out
    assert code == 1 and "ConnectionError" in out and SECRET not in out and "appuser" not in out and "mongo-staging" not in out


# ---------------------------------------------------------------- cleanup ownership (issue 1)

def test_the_marker_is_the_first_write_of_a_run(server, monkeypatch):
    seen = {}
    real = S.write_marker

    def spy(db, run_id, token):
        seen["before"] = db.list_collection_names()
        real(db, run_id, token)
    monkeypatch.setattr(S, "write_marker", spy)
    assert run_main(monkeypatch, *staging("--processes", "2", "--recipients", "10")) == 0
    assert seen["before"] == []                                               # nothing existed in the database before the marker


def test_cleanup_refuses_a_prefixed_database_without_a_valid_marker(server, monkeypatch, capsys):
    server["wa_conc_check_aaaaaaaaaaaa"]["precious"].insert_one({"keep": 1})  # right name, no marker
    snap = server["wa_conc_check_aaaaaaaaaaaa"]["precious"].find_one({}, {"_id": 0})
    assert S.validate_owned(server, "wa_conc_check_aaaaaaaaaaaa") == (False, "no_local_record")
    assert run_main(monkeypatch, "--cleanup-leftovers", *staging()) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "wa_conc_check_aaaaaaaaaaaa" in out and "Dropped 0" in out
    assert server["wa_conc_check_aaaaaaaaaaaa"]["precious"].find_one({}, {"_id": 0}) == snap


def test_cleanup_with_several_prefixed_databases_drops_only_the_owned_ones(server, monkeypatch, capsys):
    owned1, owned2 = make_owned(server, "111111111111"), make_owned(server, "222222222222")
    server["wa_conc_check_333333333333"]["stuff"].insert_one({"mine": "not the tool's"})        # prefixed, no marker
    forged = server["wa_conc_check_444444444444"]
    forged[S.OWNER_COLL].insert_one({"_id": "owner", "tool": S.TOOL_ID, "run_id": "444444444444",
                                     "db_name": forged.name, "token_hash": "forged"})             # marker, no local record
    foreign = make_owned(server, "555555555555")
    server[foreign]["customers"].insert_one({"real": "data"})                                    # valid marker but foreign data inside
    mismatched = server["wa_conc_check_666666666666"]
    mismatched[S.OWNER_COLL].insert_one({"_id": "owner", "tool": S.TOOL_ID, "run_id": "111111111111",
                                         "db_name": "wa_conc_check_111111111111",
                                         "token_hash": hashlib.sha256(b"x").hexdigest()})                  # marker copied from another run, no local record
    server["wa_conc_check_not_hex_name"]["x"].insert_one({"k": 1})                               # prefixed, wrong name shape
    server["offro_staging_other"]["x"].insert_one({"keep": 1})                                   # unrelated
    snap_app = app_snapshot(server)
    untouched = {n: {c: list(server[n][c].find({}, {"_id": 0})) for c in server[n].list_collection_names()}
                 for n in ("wa_conc_check_333333333333", "wa_conc_check_444444444444", foreign, "wa_conc_check_666666666666",
                           "wa_conc_check_not_hex_name", "offro_staging_other")}
    code = run_main(monkeypatch, "--cleanup-leftovers", *staging())
    out = capsys.readouterr().out
    names = server.list_database_names()
    assert owned1 not in names and owned2 not in names and "Dropped 2" in out
    assert code == 1 and out.count("REFUSED") == 5
    for n, content in untouched.items():
        assert n in names
        assert {c: list(server[n][c].find({}, {"_id": 0})) for c in server[n].list_collection_names()} == content
    assert app_snapshot(server) == snap_app and "offro_db" in names


def test_validate_owned_checks_run_id_name_and_contents(server):
    name = make_owned(server, "abcabcabcabc")
    assert S.validate_owned(server, name) == (True, "owned")
    assert S.validate_owned(server, name, expected_run_id="abcabcabcabc") == (True, "owned")
    assert S.validate_owned(server, name, expected_run_id="000000000000") == (False, "run_id_mismatch")
    assert S.validate_owned(server, "offro_db") == (False, "name_pattern")
    assert S.validate_owned(server, "wa_conc_check_ABC") == (False, "name_pattern")
    server[name]["anything_else"].insert_one({"x": 1})
    assert S.validate_owned(server, name) == (False, "foreign_collections")


def test_drop_owned_never_drops_an_unproven_database(server):
    server["wa_conc_check_bbbbbbbbbbbb"]["x"].insert_one({"a": 1})
    assert S.drop_owned(server, "wa_conc_check_bbbbbbbbbbbb")[0] is False
    assert S.drop_owned(server, "offro_db")[0] is False
    assert "wa_conc_check_bbbbbbbbbbbb" in server.list_database_names() and "offro_db" in server.list_database_names()


def test_end_of_run_cleanup_refuses_when_ownership_cannot_be_proven(server, monkeypatch, capsys):
    monkeypatch.setattr(S, "write_marker", lambda db, run_id, token: db["wa_campaigns"].insert_one({"seed": 1}))   # no marker is written
    code = run_main(monkeypatch, *staging("--processes", "2", "--recipients", "10"))
    out = capsys.readouterr().out
    assert code == 1 and "NOT dropped" in out and "no_marker" in out
    assert len(leftovers(server)) == 1                                        # left for the person to inspect, never force-dropped


def test_cleanup_also_requires_the_matching_fingerprint_and_the_right_target(server, monkeypatch):
    name = make_owned(server, "cccccccccccc")
    assert run_main(monkeypatch, "--cleanup-leftovers", "--target", "staging", "--yes-this-is-staging", "--expect-target", "nope") == 2
    assert run_main(monkeypatch, "--cleanup-leftovers", *isol()) == 2          # wrong path for this server
    assert name in server.list_database_names()


def test_cleanup_works_on_an_isolated_server_too(isolated, monkeypatch):
    name = make_owned(isolated, "dddddddddddd")
    isolated["wa_conc_check_eeeeeeeeeeee"]["x"].insert_one({"a": 1})
    assert run_main(monkeypatch, "--cleanup-leftovers", *isol()) == 1          # 1 because of the refused, unowned one
    assert name not in isolated.list_database_names() and "wa_conc_check_eeeeeeeeeeee" in isolated.list_database_names()


def test_setup_refuses_a_database_name_that_is_not_a_run_database(server):
    for bad in ("offro_db", "admin", "random", "wa_conc_check_", "wa_conc_check_zzzz", "wa_conc_check_aaaa1111"):
        with pytest.raises(AssertionError):
            S._setup(bad)


# ---------------------------------------------------------------- independent local ownership record

def record_file(tmp_path, run_id):
    return tmp_path / "own" / ("%s.json" % run_id)


def test_the_token_is_random_and_the_marker_stores_only_its_hash(server, tmp_path):
    t1, t2 = S.create_record("a1a1a1a1a1a1"), S.create_record("b2b2b2b2b2b2")
    assert t1 != t2 and len(t1) == 64 and int(t1, 16) >= 0
    db = server["wa_conc_check_a1a1a1a1a1a1"]
    S.write_marker(db, "a1a1a1a1a1a1", t1)
    marker = db[S.OWNER_COLL].find_one({"_id": "owner"})
    assert t1 not in json.dumps(marker, default=str) and marker["token_hash"] == hashlib.sha256(t1.encode()).hexdigest()
    assert "token" not in marker


def test_the_local_record_is_private_and_holds_no_credentials(server, tmp_path):
    S.create_record("c3c3c3c3c3c3")
    f = record_file(tmp_path, "c3c3c3c3c3c3")
    text = f.read_text()
    assert SECRET not in text and "appuser" not in text and "mongodb" not in text and "proxy" not in text
    rec = json.loads(text)
    assert set(rec) == {"tool", "run_id", "db_name", "token", "created_at"} and rec["db_name"] == "wa_conc_check_c3c3c3c3c3c3"
    if os.name == "posix":
        assert (f.stat().st_mode & 0o777) == 0o600 and ((tmp_path / "own").stat().st_mode & 0o777) == 0o700


def test_the_local_record_is_never_overwritten(server):
    S.create_record("d4d4d4d4d4d4")
    with pytest.raises(S.SafetyAbort):
        S.create_record("d4d4d4d4d4d4")


def test_a_predictable_correctly_calculated_marker_is_not_accepted(server):
    """The previous design's token: sha256(tool|run|db)[:32]. Anyone can compute it; without a local record it must prove nothing."""
    rid, name = "e5e5e5e5e5e5", "wa_conc_check_e5e5e5e5e5e5"
    for guess in (hashlib.sha256(("%s|%s|%s" % (S.TOOL_ID, rid, name)).encode()).hexdigest()[:32],
                  hashlib.sha256(("wa_staging_concurrency_check/2|%s|%s" % (rid, name)).encode()).hexdigest()[:32],
                  hashlib.sha256((rid + name).encode()).hexdigest()):
        server[name][S.OWNER_COLL].delete_many({})
        server[name][S.OWNER_COLL].insert_one({"_id": "owner", "tool": S.TOOL_ID, "run_id": rid, "db_name": name,
                                               "token": guess, "token_hash": guess, "created_at": 0})
        assert S.validate_owned(server, name) == (False, "no_local_record")
        assert S.drop_owned(server, name)[0] is False
    assert name in server.list_database_names()


def test_a_forged_marker_is_refused_even_when_a_local_record_exists_for_a_different_token(server):
    rid, name = "f6f6f6f6f6f6", "wa_conc_check_f6f6f6f6f6f6"
    S.create_record(rid)                                                         # genuine local record
    server[name][S.OWNER_COLL].insert_one({"_id": "owner", "tool": S.TOOL_ID, "run_id": rid, "db_name": name,
                                           "token_hash": hashlib.sha256(b"attacker-guess").hexdigest()})
    assert S.validate_owned(server, name) == (False, "marker_invalid")
    assert name in server.list_database_names()


def test_a_random_token_with_no_matching_local_record_is_refused(server, tmp_path):
    rid, name = "a7a7a7a7a7a7", "wa_conc_check_a7a7a7a7a7a7"
    token = S.create_record(rid)
    S.write_marker(server[name], rid, token)                                     # a perfectly random, correct marker...
    assert S.validate_owned(server, name) == (True, "owned")
    record_file(tmp_path, rid).unlink()                                          # ...but the local record is gone
    assert S.validate_owned(server, name) == (False, "no_local_record")
    assert S.drop_owned(server, name) == (False, "no_local_record")
    assert name in server.list_database_names()


@pytest.mark.parametrize("mutate,reason", [
    (lambda r: r.update(run_id="000000000000"), "local_record_mismatch"),
    (lambda r: r.update(db_name="wa_conc_check_000000000000"), "local_record_mismatch"),
    (lambda r: r.update(tool="other/1"), "local_record_mismatch"),
    (lambda r: r.update(token="not-hex"), "local_record_invalid"),
    (lambda r: r.pop("token"), "local_record_invalid"),
    (lambda r: r.update(token="0" * 64), "marker_invalid"),                       # well-formed but not the genuine token
])
def test_a_mismatched_or_invalid_local_record_refuses_deletion(server, tmp_path, mutate, reason):
    name = make_owned(server, "b8b8b8b8b8b8")
    f = record_file(tmp_path, "b8b8b8b8b8b8")
    rec = json.loads(f.read_text())
    mutate(rec)
    f.write_text(json.dumps(rec))
    if os.name == "posix":
        f.chmod(0o600)
    assert S.validate_owned(server, name) == (False, reason)
    assert S.drop_owned(server, name)[0] is False and name in server.list_database_names()


def test_an_unreadable_corrupt_or_loose_or_linked_local_record_refuses_deletion(server, tmp_path):
    name = make_owned(server, "c9c9c9c9c9c9")
    f = record_file(tmp_path, "c9c9c9c9c9c9")
    good = f.read_text()
    f.write_text("{not json")
    assert S.validate_owned(server, name) == (False, "local_record_invalid")
    f.write_text("[]")
    assert S.validate_owned(server, name) == (False, "local_record_invalid")
    f.write_text(good)
    if os.name == "posix":
        f.chmod(0o644)                                                           # group/other readable: not private any more
        assert S.validate_owned(server, name) == (False, "local_record_invalid")
        f.chmod(0o600)
        real = tmp_path / "real.json"
        real.write_text(good)
        real.chmod(0o600)
        f.unlink()
        os.symlink(real, f)                                                      # a link is not a regular file
        assert S.validate_owned(server, name) == (False, "local_record_invalid")
    assert name in server.list_database_names()


def test_genuine_owned_database_is_dropped_by_cleanup_and_its_local_record_is_then_removed(server, tmp_path, monkeypatch, capsys):
    name = make_owned(server, "d0d0d0d0d0d0")
    assert record_file(tmp_path, "d0d0d0d0d0d0").exists()
    assert run_main(monkeypatch, "--cleanup-leftovers", *staging()) == 0
    assert name not in server.list_database_names() and "Dropped 1" in capsys.readouterr().out
    assert not record_file(tmp_path, "d0d0d0d0d0d0").exists()


def test_cleanup_leaves_unrelated_unowned_and_unrecorded_databases_alone(server, tmp_path, monkeypatch, capsys):
    owned = make_owned(server, "1a1a1a1a1a1a")
    no_record = make_owned(server, "2b2b2b2b2b2b")
    record_file(tmp_path, "2b2b2b2b2b2b").unlink()                               # an interrupted run whose record was lost
    server["wa_conc_check_3c3c3c3c3c3c"]["x"].insert_one({"k": 1})               # prefixed, nothing
    server["offro_staging_other"]["x"].insert_one({"keep": 1})
    snap = app_snapshot(server)
    assert run_main(monkeypatch, "--cleanup-leftovers", *staging()) == 1
    names = server.list_database_names()
    assert owned not in names and no_record in names and "wa_conc_check_3c3c3c3c3c3c" in names and "offro_staging_other" in names
    assert "no_local_record" in capsys.readouterr().out and app_snapshot(server) == snap


def test_a_normal_run_creates_the_record_first_and_removes_it_after_the_verified_drop(server, tmp_path, monkeypatch, capsys):
    seen = {}
    real = S.write_marker

    def spy(db, run_id, token):
        seen["record_existed"] = record_file(tmp_path, run_id).exists()
        real(db, run_id, token)
    monkeypatch.setattr(S, "write_marker", spy)
    assert run_main(monkeypatch, *staging("--processes", "2", "--recipients", "10")) == 0
    assert seen["record_existed"] is True
    assert list((tmp_path / "own").glob("*.json")) == []                         # no stale record after a verified cleanup


def test_if_the_local_record_is_lost_during_a_run_the_end_of_run_cleanup_refuses(server, tmp_path, monkeypatch, capsys):
    real = S.write_marker

    def spy(db, run_id, token):
        real(db, run_id, token)
        record_file(tmp_path, run_id).unlink()
    monkeypatch.setattr(S, "write_marker", spy)
    code = run_main(monkeypatch, *staging("--processes", "2", "--recipients", "10"))
    out = capsys.readouterr().out
    assert code == 1 and "NOT dropped" in out and "no_local_record" in out and len(leftovers(server)) == 1


def test_a_run_aborts_before_touching_mongodb_if_the_local_record_cannot_be_created(server, tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("CONC_OWNER_DIR", str(blocker / "sub"))
    assert run_main(monkeypatch, *staging()) == 2
    assert "ownership record" in capsys.readouterr().out and leftovers(server) == []


# ---------------------------------------------------------------- document-level ownership

COLLS = ("wa_campaigns", "wa_campaign_recipients", "wa_campaign_rate", S.OWNER_COLL)
OTHER = {"wa_campaigns": "wa_campaign_rate", "wa_campaign_recipients": "wa_campaigns", "wa_campaign_rate": "wa_campaigns", S.OWNER_COLL: "wa_campaigns"}


def token_of(tmp_path, run_id):
    return json.loads(record_file(tmp_path, run_id).read_text())["token"]


def foreign_variants(client, name, coll, run_id, token, tmp_path):
    """Documents that are NOT this run's, some of which look exactly like genuine ones."""
    genuine = client[name][coll].find_one({"_conc_proof": {"$exists": True}})
    other_id = "zz-foreign"
    return {
        "no_metadata": {"_id": other_id, "x": 1},
        "run_tag_only": {"_id": other_id, "_conc_run": run_id},
        "lookalike_copy_of_genuine": {**{k: v for k, v in genuine.items() if k != "_id"}, "_id": other_id},   # genuine proof replayed on a new _id
        "predictable_proof": {"_id": other_id, "_conc_run": run_id,
                              "_conc_proof": hashlib.sha256(("%s|%s|%s" % (run_id, coll, other_id)).encode()).hexdigest()},
        "proof_from_other_token": {"_id": other_id, "_conc_run": run_id, "_conc_role": S.ROLE[coll], "_conc_proof": S.doc_proof("f" * 64, run_id, coll, {"_id": other_id, "_conc_role": S.ROLE[coll]})},
        "proof_for_other_collection": {"_id": other_id, "_conc_run": run_id, "_conc_role": S.ROLE[coll], "_conc_proof": S.doc_proof(token, run_id, OTHER[coll], {"_id": other_id, "_conc_role": S.ROLE[OTHER[coll]]})},
        "wrong_run_valid_looking": {"_id": other_id, "_conc_run": "000000000000", "_conc_role": S.ROLE[coll], "_conc_proof": S.doc_proof(token, run_id, coll, {"_id": other_id, "_conc_role": S.ROLE[coll]})},
        "malformed_proof": {"_id": other_id, "_conc_run": run_id, "_conc_proof": 12345},
        "empty_proof": {"_id": other_id, "_conc_run": run_id, "_conc_proof": ""},
    }


@pytest.mark.parametrize("coll", COLLS)
def test_one_foreign_document_in_any_allowed_collection_refuses_the_whole_drop(server, tmp_path, monkeypatch, capsys, coll):
    run_id = "ab12ab12ab12"
    name = make_owned(server, run_id)
    token = token_of(tmp_path, run_id)
    variants = foreign_variants(server, name, coll, run_id, token, tmp_path)
    assert len(variants) == 9
    for label, doc in variants.items():
        server[name][coll].delete_many({"_id": doc["_id"]})
        server[name][coll].insert_one(dict(doc))
        assert S.validate_owned(server, name) == (False, "foreign_documents"), label
        assert S.drop_owned(server, name) == (False, "foreign_documents"), label
        assert run_main(monkeypatch, "--cleanup-leftovers", *staging()) == 1, label
        out = capsys.readouterr().out
        assert "REFUSED" in out and "foreign_documents" in out and "Dropped 0" in out, label
        assert name in server.list_database_names(), label                                # database preserved
        assert record_file(tmp_path, run_id).exists(), label                              # local ownership record preserved
        assert server[name][coll].count_documents({"_id": doc["_id"]}) == 1                # and the foreign document itself untouched
        server[name][coll].delete_many({"_id": doc["_id"]})
    assert S.validate_owned(server, name) == (True, "owned")                              # removing the foreign document restores ownership
    assert run_main(monkeypatch, "--cleanup-leftovers", *staging()) == 0                  # a genuine database still cleans up
    assert name not in server.list_database_names() and not record_file(tmp_path, run_id).exists()


@pytest.mark.parametrize("coll", COLLS)
def test_a_foreign_document_appearing_during_a_run_makes_the_end_of_run_cleanup_refuse(server, tmp_path, monkeypatch, capsys, coll):
    real = S.write_marker

    def spy(db, run_id, token):
        real(db, run_id, token)
        db[coll].insert_one({"_id": "zz-foreign", "_conc_run": run_id, "phone": "919000000000"})      # resembles a tool record, no proof
    monkeypatch.setattr(S, "write_marker", spy)
    code = run_main(monkeypatch, *staging("--processes", "2", "--recipients", "10"))
    out = capsys.readouterr().out
    assert code == 1 and "NOT dropped" in out and "foreign_documents" in out
    assert len(leftovers(server)) == 1                                                    # database preserved
    assert len(list((tmp_path / "own").glob("*.json"))) == 1                              # local record preserved


def test_a_document_written_by_the_worker_without_a_proof_is_refused(server, tmp_path):
    run_id = "cd34cd34cd34"
    name = make_owned(server, run_id)
    server[name]["wa_campaign_rate"].insert_one({"_id": "s9999", "count": 1, "at": 0})    # what an unplanned limiter upsert would leave
    assert S.validate_owned(server, name) == (False, "foreign_documents")


def test_worker_updates_do_not_disturb_ownership_proofs(server, tmp_path):
    run_id = "ef56ef56ef56"
    name = make_owned(server, run_id)
    server[name]["wa_campaign_recipients"].update_many({}, {"$set": {"state": "sent"}, "$inc": {"attempts": 1}, "$unset": {"x": ""}})
    server[name]["wa_campaign_rate"].update_one({"_id": "s1000"}, {"$inc": {"count": 1}})
    assert S.validate_owned(server, name) == (True, "owned")


def test_a_missing_marker_or_a_second_marker_document_is_refused(server, tmp_path):
    run_id = "1212121212ab"
    name = make_owned(server, run_id)
    token = token_of(tmp_path, run_id)
    server[name][S.OWNER_COLL].insert_one(S.stamp(S.OWNER_COLL, [{"_id": "second", "tool": S.TOOL_ID}], run_id, token)[0])
    assert S.validate_owned(server, name) == (False, "foreign_documents")                 # even a correctly stamped extra marker
    server[name][S.OWNER_COLL].delete_many({})
    assert S.validate_owned(server, name) == (False, "no_marker")


def test_every_tool_write_goes_through_stamp():
    src = open(S.__file__, encoding="utf-8").read()
    assert src.count("insert_one(") == 1 and src.count("insert_many(") == 1                # write_marker (stamped) and put() (stamped)
    assert "stamp(OWNER_COLL" in src and "docs = stamp(coll, docs, run_id, token)" in src
    for forbidden in (".update_one(", ".update_many(", ".replace_one(", ".bulk_write(", "insert_one({"):
        assert forbidden not in src.replace("def update_one", "")


def test_a_genuine_run_leaves_only_proven_documents_and_cleans_up(server, tmp_path, monkeypatch, capsys):
    seen = {}
    real = S.drop_owned

    def spy(client, name, expected_run_id=None):
        seen["verdict"] = S.validate_owned(client, name, expected_run_id)
        seen["docs"] = {c: client[name][c].count_documents({}) for c in client[name].list_collection_names()}
        return real(client, name, expected_run_id)
    monkeypatch.setattr(S, "drop_owned", spy)
    assert run_main(monkeypatch, *staging("--processes", "3", "--recipients", "60")) == 0
    assert seen["verdict"] == (True, "owned") and seen["docs"]["wa_campaign_recipients"] > 0 and seen["docs"]["wa_campaign_rate"] > 0
    assert leftovers(server) == [] and list((tmp_path / "own").glob("*.json")) == []


# ---------------------------------------------------------------- the proof covers the immutable fields (not only _id)

def first_doc(client, name, coll):
    return client[name][coll].find_one({"_conc_proof": {"$exists": True}} if coll != S.OWNER_COLL else {})


def assert_refused_and_preserved(server, tmp_path, monkeypatch, capsys, name, run_id, label="", reasons=("foreign_documents",)):
    ok, why = S.validate_owned(server, name)
    assert ok is False and why in reasons, (label, why)
    assert S.drop_owned(server, name) == (False, why), label
    assert run_main(monkeypatch, "--cleanup-leftovers", *staging()) == 1, label
    assert "REFUSED" in capsys.readouterr().out, label
    assert name in server.list_database_names() and record_file(tmp_path, run_id).exists(), label


# (collection, field, new value): every immutable field, changed while the ORIGINAL proof is retained
IMMUTABLE_MUTATIONS = [
    ("wa_campaigns", "_conc_role", "recipient"),
    ("wa_campaign_recipients", "phone", "919999999999"),
    ("wa_campaign_recipients", "campaign_id", "someone-else"),
    ("wa_campaign_recipients", "idx", 999),
    ("wa_campaign_recipients", "_conc_role", "campaign"),
    ("wa_campaign_rate", "at", 12345),
    ("wa_campaign_rate", "_conc_role", "campaign"),
    (S.OWNER_COLL, "created_at", 1),
    (S.OWNER_COLL, "db_name", "wa_conc_check_000000000000"),
    (S.OWNER_COLL, "token_hash", "0" * 64),
]


@pytest.mark.parametrize("coll,field,value", IMMUTABLE_MUTATIONS)
def test_changing_an_immutable_field_while_keeping_the_original_proof_is_refused(server, tmp_path, monkeypatch, capsys, coll, field, value):
    run_id = "a0a0a0a0a0a0"
    name = make_owned(server, run_id)
    assert S.validate_owned(server, name) == (True, "owned")
    doc = first_doc(server, name, coll)
    server[name][coll].update_one({"_id": doc["_id"]}, {"$set": {field: value}})            # _conc_proof is left exactly as it was
    assert server[name][coll].find_one({"_id": doc["_id"]})["_conc_proof"] == doc["_conc_proof"]
    assert_refused_and_preserved(server, tmp_path, monkeypatch, capsys, name, run_id, "%s.%s" % (coll, field),
                                 reasons=("foreign_documents", "marker_invalid") if coll == S.OWNER_COLL else ("foreign_documents",))


@pytest.mark.parametrize("coll,field", [("wa_campaign_recipients", "idx"), ("wa_campaign_recipients", "phone"), ("wa_campaign_rate", "at"),
                                        ("wa_campaign_recipients", "campaign_id"), (S.OWNER_COLL, "created_at")])
def test_removing_or_adding_an_immutable_field_is_refused(server, tmp_path, monkeypatch, capsys, coll, field):
    run_id = "b1b1b1b1b1b1"
    name = make_owned(server, run_id)
    doc = first_doc(server, name, coll)
    if field in doc:
        server[name][coll].update_one({"_id": doc["_id"]}, {"$unset": {field: ""}})              # removed
    else:
        server[name][coll].update_one({"_id": doc["_id"]}, {"$set": {field: "added-later"}})     # added after stamping
    assert_refused_and_preserved(server, tmp_path, monkeypatch, capsys, name, run_id,
                                 reasons=("foreign_documents", "marker_invalid") if coll == S.OWNER_COLL else ("foreign_documents",))


def test_adding_an_immutable_field_that_was_absent_at_stamp_time_is_refused(server, tmp_path, monkeypatch, capsys):
    run_id = "c2c2c2c2c2c2"
    name = make_owned(server, run_id)
    token = token_of(tmp_path, run_id)
    d = S.stamp("wa_campaign_recipients", [{"campaign_id": "k", "phone": "9190000001"}], run_id, token)[0]       # no idx at stamp time
    server[name]["wa_campaign_recipients"].insert_one(dict(d))
    assert S.validate_owned(server, name) == (True, "owned")
    server[name]["wa_campaign_recipients"].update_one({"_id": d["_id"]}, {"$set": {"idx": 7}})
    assert_refused_and_preserved(server, tmp_path, monkeypatch, capsys, name, run_id)


@pytest.mark.parametrize("coll", ("wa_campaigns", "wa_campaign_recipients", "wa_campaign_rate"))
def test_replaying_a_genuine_proof_against_another_id_or_collection_is_refused(server, tmp_path, monkeypatch, capsys, coll):
    run_id = "d3d3d3d3d3d3"
    name = make_owned(server, run_id)
    genuine = first_doc(server, name, coll)
    clone = {k: v for k, v in genuine.items() if k != "_id"}
    server[name][coll].insert_one({**clone, "_id": "another-id"})                           # same fields, same proof, different _id
    assert_refused_and_preserved(server, tmp_path, monkeypatch, capsys, name, run_id, "other id")
    server[name][coll].delete_many({"_id": "another-id"})
    other = OTHER[coll]
    server[name][other].insert_one({**genuine, "_id": genuine["_id"]})
    assert S.validate_owned(server, name) == (False, "foreign_documents")                   # same _id, proof moved to another collection
    assert name in server.list_database_names() and record_file(tmp_path, run_id).exists()


@pytest.mark.parametrize("coll", ("wa_campaigns", "wa_campaign_recipients", "wa_campaign_rate", S.OWNER_COLL))
@pytest.mark.parametrize("label,edit", [
    ("missing_proof", {"$unset": {"_conc_proof": ""}}),
    ("missing_run", {"$unset": {"_conc_run": ""}}),
    ("missing_role", {"$unset": {"_conc_role": ""}}),
    ("empty_proof", {"$set": {"_conc_proof": ""}}),
    ("null_proof", {"$set": {"_conc_proof": None}}),
    ("numeric_proof", {"$set": {"_conc_proof": 12345}}),
    ("list_proof", {"$set": {"_conc_proof": ["a"]}}),
    ("short_hex_proof", {"$set": {"_conc_proof": "ab12"}}),
    ("non_hex_proof", {"$set": {"_conc_proof": "zz" * 32}}),
    ("flipped_proof_char", "flip"),
    ("wrong_run", {"$set": {"_conc_run": "000000000000"}}),
    ("wrong_role", {"$set": {"_conc_role": "bogus"}}),
    ("run_not_string", {"$set": {"_conc_run": 5}}),
])
def test_missing_malformed_or_invalid_ownership_metadata_is_refused(server, tmp_path, monkeypatch, capsys, coll, label, edit):
    run_id = "e4e4e4e4e4e4"
    name = make_owned(server, run_id)
    doc = first_doc(server, name, coll)
    if edit == "flip":
        p = doc["_conc_proof"]
        edit = {"$set": {"_conc_proof": p[:-1] + ("0" if p[-1] != "0" else "1")}}
    server[name][coll].update_one({"_id": doc["_id"]}, edit)
    assert_refused_and_preserved(server, tmp_path, monkeypatch, capsys, name, run_id, "%s/%s" % (coll, label),
                                 reasons=("foreign_documents", "no_marker", "marker_invalid") if coll == S.OWNER_COLL else ("foreign_documents",))


def test_an_unsupported_value_in_an_immutable_field_is_refused_not_crashed(server, tmp_path, monkeypatch, capsys):
    run_id = "f5f5f5f5f5f5"
    name = make_owned(server, run_id)
    doc = first_doc(server, name, "wa_campaign_recipients")
    server[name]["wa_campaign_recipients"].update_one({"_id": doc["_id"]}, {"$set": {"phone": {"nested": ["x"]}}})
    assert_refused_and_preserved(server, tmp_path, monkeypatch, capsys, name, run_id)
    with pytest.raises(TypeError):
        S.stamp("wa_campaign_recipients", [{"campaign_id": "c", "phone": {"x": 1}}], run_id, token_of(tmp_path, run_id))


def test_legitimate_worker_updates_of_mutable_fields_keep_the_proofs_valid_and_cleanup_succeeds(server, tmp_path, monkeypatch, capsys):
    from datetime import datetime
    run_id = "a6a6a6a6a6a6"
    name = make_owned(server, run_id)
    db = server[name]
    now = datetime.utcnow()
    db["wa_campaign_recipients"].update_many({}, {"$set": {"state": "sending", "lease_owner": "w1", "lease_until": now, "send_started": True,
                                                           "next_attempt_at": now, "message_id": "wamid.X", "delivery_rank": 3,
                                                           "updated_at": now, "error_code": 131056, "new_worker_field": [1, 2]},
                                                 "$inc": {"attempts": 1}, "$unset": {"x": ""}})
    db["wa_campaigns"].update_many({}, {"$set": {"status": "paused", "run": {"lease_owner": "w1", "lease_until": now}, "version": 9,
                                                 "counters.sent": 4}, "$unset": {"run.backoff_until": ""}})
    db["wa_campaign_rate"].update_many({}, {"$inc": {"count": 3}})
    assert S.validate_owned(server, name) == (True, "owned")
    assert run_main(monkeypatch, "--cleanup-leftovers", *staging()) == 0
    assert name not in server.list_database_names() and not record_file(tmp_path, run_id).exists()
    capsys.readouterr()


def test_the_proof_is_verified_immediately_before_the_end_of_run_drop(server, tmp_path, monkeypatch, capsys):
    real = S.drop_owned

    def tamper_then_drop(client, name, expected_run_id=None):
        r = client[name]["wa_campaign_recipients"].find_one({})
        client[name]["wa_campaign_recipients"].update_one({"_id": r["_id"]}, {"$set": {"phone": "919000000000"}})   # after all checks passed
        return real(client, name, expected_run_id)
    monkeypatch.setattr(S, "drop_owned", tamper_then_drop)
    code = run_main(monkeypatch, *staging("--processes", "2", "--recipients", "10"))
    out = capsys.readouterr().out
    assert code == 1 and "NOT dropped" in out and "foreign_documents" in out
    assert len(leftovers(server)) == 1 and len(list((tmp_path / "own").glob("*.json"))) == 1


def test_immutable_and_mutable_field_sets_are_defined_and_disjoint_from_the_worker_state():
    assert set(S.IMMUTABLE_FIELDS) == set(S.ROLE) == {"_wa_conc_owner", "wa_campaigns", "wa_campaign_recipients", "wa_campaign_rate"}
    for coll, fields in S.IMMUTABLE_FIELDS.items():
        assert "_conc_role" in fields
    # fields the worker is allowed to change must not be sealed
    for mutable in ("state", "attempts", "lease_owner", "lease_until", "send_started", "next_attempt_at", "status", "run", "count",
                    "message_id", "delivery_rank", "updated_at"):
        assert all(mutable not in f for f in S.IMMUTABLE_FIELDS.values())
    # that the real worker never changes a sealed field is proven dynamically: test_a_genuine_run_leaves_only_proven_documents_and_cleans_up
    # runs every worker path and every document must still verify at drop time



# ---------------------------------------------------------------- regression: rate counters under a non-UTC host time zone
# Root cause of the 7 Windows failures: the worker names its per-second counters "s<int(naive_utc_datetime.timestamp())>", and Python reads a
# naive datetime as LOCAL time, so on a host with e.g. UTC+05:30 the ids are shifted by 19800 s. The tool pre-created counters for true epoch
# seconds only, so the worker upserted new, unstamped counters ("foreign_documents"). Every test here is host-OS independent except the tzset one.

@pytest.fixture
def host_tz(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX-only (on Windows the host's real time zone is the test)")
    old = os.environ.get("TZ")

    def set_tz(name):
        os.environ["TZ"] = name
        time.tzset()
    yield set_tz
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.mark.parametrize("tz", ["Asia/Kolkata", "America/Los_Angeles", "Pacific/Auckland", "Asia/Kathmandu", "UTC"])
def test_a_full_run_passes_and_cleans_up_on_a_host_with_any_time_zone(server, tmp_path, monkeypatch, capsys, host_tz, tz):
    host_tz(tz)
    code = run_main(monkeypatch, *staging("--processes", "2", "--recipients", "10"))
    out = capsys.readouterr().out
    assert code == 0 and "RESULT: ALL PASS (8/8 checks)" in out and "foreign_documents" not in out, out
    assert leftovers(server) == [] and list((tmp_path / "own").glob("*.json")) == []


def test_the_counter_ids_cover_the_worker_formula_for_any_utc_offset(monkeypatch):
    import services.wa_campaign_worker as W
    for shift in (0, 19800, -28800, 45900, 13 * 3600):
        monkeypatch.setattr(W, "_now", lambda shift=shift: datetime.utcnow() + timedelta(seconds=shift))
        ids = {"s%d" % x for x in S.rate_counter_seconds(W, 0, 4)}
        for t in range(4):                                                          # every second of the window, by BOTH formulas
            at = datetime.utcnow() + timedelta(seconds=t)
            assert "s%d" % int((W._now() + timedelta(seconds=t)).timestamp()) in ids
            assert "s%d" % int(at.replace(tzinfo=timezone.utc).timestamp()) in ids


def test_rate_counters_are_all_stamped_and_proven_before_any_worker_touches_them(server, monkeypatch):
    seen = {}
    inner = S._run

    def spy(procs, target, args, timeout=0):
        if target is S._rate_proc:
            db = server[args[0]]
            docs = list(db["wa_campaign_rate"].find({}))
            seen["n"] = len(docs)
            seen["all_own"] = all(S._is_own(d, "wa_campaign_rate", args[0][len(S.PREFIX):], token_from(args[0])) for d in docs)
            seen["counts"] = {d["count"] for d in docs}
        return inner(procs, target, args, timeout)

    def token_from(name):
        return S.read_record(name[len(S.PREFIX):])[0]["token"]
    monkeypatch.setattr(S, "_run", spy)
    assert run_main(monkeypatch, *staging("--processes", "2", "--recipients", "10")) == 0
    assert seen["n"] >= S.RATE_WINDOW_SECONDS + 3 and seen["all_own"] and seen["counts"] == {0}


def test_the_exact_failure_an_unstamped_worker_created_counter_is_refused_with_safe_diagnostics(server, tmp_path, monkeypatch, capsys):
    run_id = "3e3e3e3e3e3e"
    name = make_owned(server, run_id)
    # exactly what the worker's upsert leaves when its second id is not one of the pre-created counters
    server[name]["wa_campaign_rate"].insert_one({"_id": "s1794811427", "count": 1, "at": datetime.utcnow()})
    server[name]["wa_campaign_recipients"].insert_one({"_id": "zz", "phone": "919812345678", "campaign_id": "secret-campaign"})
    assert S.validate_owned(server, name) == (False, "foreign_documents")
    lines = S.describe_foreign(server, name)
    assert lines == ["  wa_campaign_rate: 1 document(s): missing_ownership_metadata",
                     "  wa_campaign_recipients: 1 document(s): missing_ownership_metadata"]
    assert run_main(monkeypatch, "--cleanup-leftovers", *staging()) == 1
    out = capsys.readouterr().out
    assert "wa_campaign_rate: 1 document(s)" in out
    for secret in ("919812345678", "secret-campaign", "s1794811427", "zz"):
        assert secret not in out
    assert name in server.list_database_names() and record_file(tmp_path, run_id).exists()


def test_end_of_run_refusal_prints_which_collection_and_why_without_content(server, tmp_path, monkeypatch, capsys):
    real = S.write_marker

    def spy(db, run_id, token):
        real(db, run_id, token)
        db["wa_campaign_rate"].insert_one({"_id": "s42", "count": 9})
    monkeypatch.setattr(S, "write_marker", spy)
    assert run_main(monkeypatch, *staging("--processes", "2", "--recipients", "10")) == 1
    out = capsys.readouterr().out
    assert "NOT dropped (foreign_documents)" in out and "wa_campaign_rate: 1 document(s): missing_ownership_metadata" in out and "s42" not in out


def test_every_reason_category_is_reported_distinctly(server, tmp_path):
    run_id = "4f4f4f4f4f4f"
    make_owned(server, run_id)
    token = token_of(tmp_path, run_id)
    good = S.stamp("wa_campaign_recipients", [{"campaign_id": "c", "phone": "p", "idx": 1}], run_id, token)[0]
    cases = {
        "missing_ownership_metadata": {"_id": 1},
        "missing_or_wrong_run_tag": {**good, "_id": 2, "_conc_run": "x"},
        "missing_or_wrong_role": {**good, "_conc_role": "campaign"},
        "missing_or_malformed_proof": {**good, "_conc_proof": "zz"},
        "proof_mismatch": {**good, "phone": "changed"},
        "unverifiable": {**good, "phone": {"nested": 1}},
    }
    for want, doc in cases.items():
        assert S._classify(doc, "wa_campaign_recipients", run_id, token) == want
    assert S._classify(good, "wa_campaign_recipients", run_id, token) is None


def test_proofs_survive_the_bson_round_trip_exactly_as_mongodb_stores_documents(tmp_path):
    """Canonicalization vs MongoDB read-back: datetimes are stored at MILLISECOND precision, ints as int32/int64, strings as UTF-8."""
    import bson
    token = S.create_record("5a5a5a5a5a5a")
    run_id = "5a5a5a5a5a5a"
    now = datetime(2026, 11, 16, 6, 43, 47, 987654)                                  # sub-millisecond digits that BSON truncates
    docs = (S.stamp("wa_campaign_rate", [{"_id": "s1794811427", "count": 0, "at": now}], run_id, token)
            + S.stamp("wa_campaign_recipients", [{"campaign_id": "c\u00e9", "phone": "9190000001", "idx": 2 ** 40}, {"campaign_id": "c", "phone": "p", "idx": 7}], run_id, token)
            + S.stamp("wa_campaigns", [{"status": "running"}], run_id, token)
            + S.stamp(S.OWNER_COLL, [{"_id": "owner", "tool": S.TOOL_ID, "run_id": run_id, "db_name": "wa_conc_check_" + run_id,
                                      "token_hash": "0" * 64, "created_at": now}], run_id, token))
    for d in docs:
        coll = {"campaign": "wa_campaigns", "recipient": "wa_campaign_recipients", "rate_counter": "wa_campaign_rate", "marker": S.OWNER_COLL}[d["_conc_role"]]
        back = bson.decode(bson.encode(d))                                           # what a real server returns
        assert S._classify(back, coll, run_id, token) is None, coll
    assert bson.decode(bson.encode({"t": now}))["t"].microsecond == 987000          # the truncation the canonical form accounts for


def test_the_local_record_survives_a_drop_that_cannot_be_verified(server, tmp_path, monkeypatch):
    name = make_owned(server, "9a9a9a9a9a9a")
    monkeypatch.setattr(type(server), "drop_database", lambda self, n: None, raising=False)     # the server silently ignores the drop
    assert S.drop_owned(server, name) == (False, "drop_not_verified")
    assert name in server.list_database_names() and record_file(tmp_path, "9a9a9a9a9a9a").exists()


# ---------------------------------------------------------------- Windows permission policy

ME = ("HOST\\alice", "S-1-5-21-111-222-333-1001")


class FakeWindows:
    """Stands in for icacls/whoami. New paths start with broad inherited entries (like a real folder under a shared parent)."""
    def __init__(self, monkeypatch, tmp_path, grant_rc=0, leak=None, whoami=ME, garbage=False):
        self.acl, self.calls, self.grant_rc, self.leak, self.garbage = {}, [], grant_rc, leak, garbage
        monkeypatch.setattr(S, "_is_windows", lambda: True)
        monkeypatch.setattr(S, "_whoami", lambda: whoami)
        monkeypatch.setattr(S, "_icacls", self.icacls)
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
        monkeypatch.delenv("CONC_OWNER_DIR", raising=False)

    def icacls(self, *args):
        path = args[0]
        self.calls.append(args)
        if len(args) == 1:
            if self.garbage:
                return 0, "this is not an acl listing"
            aces = self.acl.get(path, [("BUILTIN\\Users", "(I)(RX)"), (ME[0], "(I)(F)")])
            lines = (["%s %s:%s" % (path, aces[0][0], aces[0][1])] + ["          %s:%s" % a for a in aces[1:]]) if aces else []
            return 0, "\n".join(lines) + "\n\nSuccessfully processed 1 files; Failed processing 0 files\n"
        if self.grant_rc:
            return self.grant_rc, "Access is denied."
        self.acl[path] = [(ME[0], "(OI)(CI)(F)" if args[-1].endswith("(CI)F") else "(F)")] + ([self.leak] if self.leak else [])
        return 0, "Successfully processed 1 files"


def win_path(tmp_path):
    return tmp_path / "profile" / ".offro_conc_check"


def test_windows_establishes_and_verifies_a_current_user_only_acl_on_folder_and_record(monkeypatch, tmp_path):
    fw = FakeWindows(monkeypatch, tmp_path)
    (tmp_path / "profile").mkdir()
    token = S.create_record("aa11aa11aa11")
    locked = [c for c in fw.calls if "/inheritance:r" in c]
    assert len(locked) == 2 and any(c[-1] == "*%s:(OI)(CI)F" % ME[1] for c in locked) and any(c[-1] == "*%s:F" % ME[1] for c in locked)
    assert all(c[1:3] == ("/inheritance:r", "/grant:r") for c in locked)
    rec, why = S.read_record("aa11aa11aa11")
    assert why == "ok" and rec["token"] == token


def test_windows_refuses_a_custom_owner_directory(monkeypatch, tmp_path):
    FakeWindows(monkeypatch, tmp_path)
    monkeypatch.setenv("CONC_OWNER_DIR", str(tmp_path / "shared"))
    with pytest.raises(S.SafetyAbort, match="not allowed on Windows"):
        S.create_record("bb22bb22bb22")
    assert not (tmp_path / "shared").exists()
    assert S.read_record("bb22bb22bb22") == (None, "local_record_invalid")


def test_windows_without_a_profile_folder_fails_closed(monkeypatch, tmp_path):
    FakeWindows(monkeypatch, tmp_path)
    monkeypatch.delenv("USERPROFILE")
    with pytest.raises(S.SafetyAbort, match="USERPROFILE"):
        S.create_record("cc33cc33cc33")


@pytest.mark.parametrize("kwargs,label", [
    ({"grant_rc": 5}, "icacls cannot change the ACL"),
    ({"leak": ("BUILTIN\\Users", "(RX)")}, "another principal still has access after the grant"),
    ({"leak": ("Everyone", "(OI)(CI)(R)")}, "Everyone still has access"),
    ({"whoami": None}, "the current user cannot be determined"),
    ({"garbage": True}, "the ACL cannot be read back"),
])
def test_windows_refuses_when_a_private_acl_cannot_be_established(monkeypatch, tmp_path, server, capsys, kwargs, label):
    FakeWindows(monkeypatch, tmp_path, **kwargs)
    (tmp_path / "profile").mkdir()
    with pytest.raises(S.SafetyAbort):
        S.create_record("dd44dd44dd44")
    assert not (win_path(tmp_path) / "dd44dd44dd44.json").exists()                       # no half-secured record is left behind
    capsys.readouterr()


def test_windows_run_aborts_before_any_mongodb_write_when_the_acl_cannot_be_made_private(server, monkeypatch, tmp_path, capsys):
    FakeWindows(monkeypatch, tmp_path, grant_rc=5)
    (tmp_path / "profile").mkdir()
    assert run_main(monkeypatch, *staging()) == 2
    assert "ABORT" in capsys.readouterr().out and leftovers(server) == []


def test_windows_cleanup_refuses_and_preserves_everything_when_the_acl_has_become_broad(server, monkeypatch, tmp_path, capsys):
    fw = FakeWindows(monkeypatch, tmp_path)
    (tmp_path / "profile").mkdir()
    name = make_owned(server, "ee55ee55ee55")
    assert S.validate_owned(server, name) == (True, "owned")
    f = str(win_path(tmp_path) / "ee55ee55ee55.json")
    fw.acl[f].append(("BUILTIN\\Users", "(RX)"))                                         # someone widened the record's ACL
    assert S.validate_owned(server, name) == (False, "local_record_invalid")
    assert run_main(monkeypatch, "--cleanup-leftovers", *staging()) == 1
    assert name in server.list_database_names() and (win_path(tmp_path) / "ee55ee55ee55.json").exists()
    fw.acl[f].pop()
    fw.acl[str(win_path(tmp_path))].append(("Everyone", "(OI)(CI)(R)"))                    # or the folder's
    assert S.validate_owned(server, name) == (False, "local_record_invalid")
    fw.acl[str(win_path(tmp_path))].pop()
    fw.acl[f][0] = (ME[0], "(I)(F)")                                                       # or the record inherits permissions again
    assert S.validate_owned(server, name) == (False, "local_record_invalid")
    capsys.readouterr()


def test_windows_acl_parser_accepts_only_the_current_user(monkeypatch, tmp_path):
    fw = FakeWindows(monkeypatch, tmp_path)
    for acl, want in (([(ME[0], "(F)")], True), ([(ME[0].upper(), "(OI)(CI)(F)")], True), ([("NT AUTHORITY\\SYSTEM", "(F)")], False),
                      ([(ME[0], "(F)"), ("NT AUTHORITY\\SYSTEM", "(F)")], False), ([(ME[0], "(I)(F)")], False),
                      ([("HOST\\bob", "(F)")], False), ([], False)):
        fw.acl["P"] = acl
        assert S._win_acl_is_private("P") is want, acl


@pytest.mark.parametrize("failure", [FileNotFoundError("icacls"), subprocess.TimeoutExpired("icacls", 30), PermissionError("denied")])
@pytest.mark.parametrize("where", ["_icacls", "_whoami"])
def test_windows_fails_closed_when_the_acl_tools_cannot_run(monkeypatch, tmp_path, server, capsys, where, failure):
    FakeWindows(monkeypatch, tmp_path)
    (tmp_path / "profile").mkdir()

    def broken(*a, **k):
        raise failure
    monkeypatch.setattr(S, where, broken)
    with pytest.raises(S.SafetyAbort, match="Nothing was written to MongoDB"):
        S.create_record("1c1c1c1c1c1c")
    assert not (win_path(tmp_path) / "1c1c1c1c1c1c.json").exists()
    assert run_main(monkeypatch, *staging()) == 2 and leftovers(server) == []
    capsys.readouterr()


def test_windows_policy_is_active_in_the_dedicated_tests_and_off_in_the_logic_tests(monkeypatch, tmp_path):
    assert S._is_windows() is False                                           # the autouse isolation fixture
    FakeWindows(monkeypatch, tmp_path)
    assert S._is_windows() is True                                            # the Windows tests re-enable the REAL policy code
    monkeypatch.setenv("CONC_OWNER_DIR", str(tmp_path / "x"))
    with pytest.raises(S.SafetyAbort, match="not allowed on Windows"):
        S.owner_dir()


def test_a_full_run_and_a_cleanup_also_pass_under_the_windows_policy(server, monkeypatch, tmp_path, capsys):
    fw = FakeWindows(monkeypatch, tmp_path)
    (tmp_path / "profile").mkdir()
    assert run_main(monkeypatch, *staging("--processes", "2", "--recipients", "20")) == 0
    assert "RESULT: ALL PASS (8/8 checks)" in capsys.readouterr().out
    assert any("/inheritance:r" in c for c in fw.calls) and leftovers(server) == []
    name = make_owned(server, "2d2d2d2d2d2d")
    assert run_main(monkeypatch, "--cleanup-leftovers", *staging()) == 0 and name not in server.list_database_names()


def test_posix_permission_checks_are_unchanged(server, tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX only")
    S.create_record("ff66ff66ff66")
    f = record_file(tmp_path, "ff66ff66ff66")
    assert (f.stat().st_mode & 0o777) == 0o600 and ((tmp_path / "own").stat().st_mode & 0o777) == 0o700
    f.chmod(0o640)
    assert S.read_record("ff66ff66ff66") == (None, "local_record_invalid")


# ---------------------------------------------------------------- tripwire fails closed (issue 2)

def test_tripwire_blocks_application_database_access():
    with pytest.raises(RuntimeError, match="blocked"):
        S._Tripwire()["users"]
    with pytest.raises(RuntimeError, match="blocked"):
        S._Tripwire().find_one({})


def test_install_refuses_when_the_real_database_module_is_already_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "database", types.ModuleType("database"))     # a module without our marker = the real one
    with pytest.raises(S.SafetyAbort, match="already imported"):
        S._install_tripwire()
    assert getattr(sys.modules["database"], "__wa_tripwire__", None) is None        # nothing was swapped in silently


def test_every_mode_aborts_with_exit_2_and_no_writes_when_the_real_database_module_is_present(server, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "database", types.ModuleType("database"))
    for argv in (["--show-target"], ["--cleanup-leftovers", *staging()], staging("--processes", "2", "--recipients", "10")):
        assert run_main(monkeypatch, *argv) == 2
    out = capsys.readouterr().out
    assert out.count("ABORT") == 3 and leftovers(server) == []


def test_install_refuses_when_a_service_already_holds_a_real_database_handle(monkeypatch):
    import pymongo
    monkeypatch.setattr(S, "_assert_no_real_app_db", REAL_SCAN)
    monkeypatch.delitem(sys.modules, "database", raising=False)                    # `database` was imported, then removed
    fake = types.ModuleType("services.fake_holder")
    fake.db = pymongo.MongoClient("mongodb://localhost:1", connect=False)["offro_db"]   # lazy: never connects
    monkeypatch.setitem(sys.modules, "services.fake_holder", fake)
    with pytest.raises(S.SafetyAbort, match="real MongoDB handle"):
        S._install_tripwire()
    assert "database" not in sys.modules


def test_install_succeeds_and_is_idempotent_in_a_fresh_state(monkeypatch):
    monkeypatch.setattr(S, "_assert_no_real_app_db", lambda: None)
    monkeypatch.delitem(sys.modules, "database", raising=False)
    S._install_tripwire()
    first = sys.modules["database"]
    assert first.__wa_tripwire__ is True
    S._install_tripwire()                                                           # our own tripwire already there: fine
    assert sys.modules["database"] is first
    with pytest.raises(RuntimeError, match="blocked"):
        first.db["users"]


def _run_snippet(code):
    return subprocess.run([sys.executable, "-W", "ignore", "-c", code], capture_output=True, text=True, timeout=120,
                          env={k: v for k, v in os.environ.items() if k not in ("MONGO_URL", "CONC_MONGO_URL")})


def test_subprocess_real_database_imported_first_is_refused():
    r = _run_snippet("import sys; sys.path.insert(0, %r)\nimport database\nimport tools.wa_staging_concurrency_check as S\n"
                     "try:\n    S._install_tripwire(); print('CONTINUED')\nexcept S.SafetyAbort:\n    print('REFUSED')\n" % os.getcwd())
    assert "REFUSED" in r.stdout and "CONTINUED" not in r.stdout, r.stderr[-400:]


def test_subprocess_fresh_process_installs_the_tripwire_and_blocks_service_access():
    r = _run_snippet("import sys; sys.path.insert(0, %r)\nimport tools.wa_staging_concurrency_check as S\nS._install_tripwire()\n"
                     "import services.wa_consent as C, services.wa_campaign_settings as ST, services.wa_campaign_worker as W\n"
                     "try:\n    ST.db['wa_campaign_settings'].find_one({}); print('REACHED')\n"
                     "except RuntimeError:\n    print('BLOCKED')\n" % os.getcwd())
    assert "BLOCKED" in r.stdout and "REACHED" not in r.stdout, r.stderr[-400:]


def test_source_has_exactly_one_drop_and_it_is_the_validated_one():
    src = open(S.__file__, encoding="utf-8").read()
    assert src.count("drop_database(") == 1
    body = src[src.index("def drop_owned"):]
    assert body.index("validate_owned(") < body.index("drop_database(")
    for word in ("merchants", "wa_contacts", "wa_campaign_settings", "send_whatsapp"):
        assert word not in src
