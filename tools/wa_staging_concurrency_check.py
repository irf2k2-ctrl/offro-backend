"""
Real-MongoDB concurrency verification for the Phase 2E/2F worker (no WhatsApp, no Meta, no application data touched).

mongomock cannot prove cross-process atomicity, so this script runs the SAME worker functions against a real MongoDB server from
several OS processes at once, inside a throw-away database  wa_conc_check_<run id>.  Step-by-step Windows procedure:
docs/WHATSAPP_STAGING_CONCURRENCY_PROCEDURE.md

Safety design
  * Two verification paths, chosen explicitly with --target:
      staging   the Railway STAGING MongoDB that also holds the application database `offro_db` (it MUST be present)
      isolated  a fresh, EMPTY dedicated MongoDB (local Docker, or a separate throw-away server): `offro_db` must be ABSENT and the server
                must hold nothing but system databases. A server that fits neither path is refused.
  * The connection string comes from the environment variable CONC_MONGO_URL only; it is never printed or logged. Errors are reported by class name.
  * FAIL-CLOSED TRIPWIRE: the application's `database` module is replaced by a tripwire that raises on any use. If the real `database`
    module (or any service already holding a real application database handle) is present in this process, the tool REFUSES to run.
  * The fingerprint is only a typo / mix-up guard (a hash of the host:port in the connection string). It does NOT prove which environment a
    server belongs to: compare the host AND port with Railway yourself (use --show-target --reveal-host).
  * OWNERSHIP (independent proofs): each run first creates a LOCAL record (random 256-bit token from `secrets`, private to the current user:
    POSIX 0600/0700; Windows an explicit current-user-only ACL, verified, custom folders refused; no MongoDB credentials) and only then writes
    to MongoDB. EVERY document the tool writes (marker, campaigns, recipients, rate counters) carries the run id, a role and an HMAC-SHA256 proof
    keyed with that token over its IMMUTABLE ownership fields (see IMMUTABLE_FIELDS; worker-mutable fields are not sealed). A database is dropped (end of run, or --cleanup-leftovers) only if the local record is valid, the marker matches, only
    this tool's collections exist AND every document in every collection verifies. One foreign / unproven / malformed document refuses the whole
    drop and the local record is kept. Names, shapes, predictable ids and the marker alone are never trusted.

Modes
    python tools/wa_staging_concurrency_check.py --show-target [--reveal-host]                       (read-only; writes nothing)
    python tools/wa_staging_concurrency_check.py --target staging  --yes-this-is-staging  --expect-target <fp> [--processes 8] [--recipients 400]
    python tools/wa_staging_concurrency_check.py --target isolated --yes-this-is-isolated --expect-target <fp> [...]
    python tools/wa_staging_concurrency_check.py --cleanup-leftovers --target <staging|isolated> --yes-this-is-<staging|isolated> --expect-target <fp>
"""
import argparse
import hashlib
import hmac
import json
import multiprocessing as mp
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta

from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PREFIX = "wa_conc_check_"
APP_DB = "offro_db"                  # the application's real database: never written, only checked for presence
SYSTEM_DBS = frozenset({"admin", "config", "local"})
OWNER_COLL = "_wa_conc_owner"        # holds the single ownership-marker document of a throw-away database
TOOL_ID = "wa_staging_concurrency_check/5"
OWNER_DIR_ENV = "CONC_OWNER_DIR"      # optional override of the local ownership-record directory
NAME_RE = re.compile(r"^wa_conc_check_[0-9a-f]{12}$")
KNOWN_COLLECTIONS = frozenset({OWNER_COLL, "wa_campaigns", "wa_campaign_recipients", "wa_campaign_rate"})   # everything a run may create
RATE_PER_SECOND = 5
RATE_WINDOW_SECONDS = 3        # the measured window: exactly this long, starting at the common start instant
RATE_START_DELAY = 2           # seconds from now until every process starts measuring together
RUN_TIMEOUT = 120              # a child that neither answers nor exits within this is terminated and reported
MAX_PROCESSES, MAX_RECIPIENTS = 16, 20000


def rate_upper_bound(rate, window):
    """A window of `window` seconds can overlap window+1 one-second buckets, each allowing `rate` tokens."""
    return rate * (int(window) + 1)


def rate_lower_bound(rate, window):
    """It always contains at least window-1 COMPLETE buckets; fewer tokens than that means tokens are not flowing at all."""
    return rate * (int(window) - 1)


class SafetyAbort(Exception):
    """A safety interlock refused to continue. Nothing was written."""


class _Tripwire:
    """Stands in for the application's database handle. Any use raises, so a code path that reaches for application data fails
    loudly (and never connects to it) instead of silently touching offro_db."""
    def _boom(self, *a, **k):
        raise RuntimeError("blocked: application database access during the concurrency check")
    __getattr__ = __getitem__ = __call__ = __iter__ = _boom


def _is_our_tripwire(mod):
    return getattr(mod, "__dict__", {}).get("__wa_tripwire__") is True


def _assert_no_real_app_db():
    """Fail closed if any already-imported application module holds a REAL pymongo database handle (e.g. `database` was imported and
    then removed from sys.modules, or a service bound `db` earlier). Such a handle would reach offro_db."""
    from pymongo.database import Database
    for name, mod in list(sys.modules.items()):
        if name == "database" or name.startswith(("services", "routers")) or name == "server":
            handle = getattr(mod, "__dict__", {}).get("db")
            if isinstance(handle, Database):
                raise SafetyAbort("ABORT: module '%s' already holds a real MongoDB handle (the application's database). Start a fresh "
                                  "Python process and run only this tool. Nothing was written." % name)


def _install_tripwire():
    """Install the tripwire, or REFUSE. Never silently continues without it."""
    existing = sys.modules.get("database")
    if existing is not None and not _is_our_tripwire(existing):
        raise SafetyAbort("ABORT: the real application 'database' module is already imported in this process, so the tripwire cannot be "
                          "installed. Run this tool in a fresh process (python tools\\wa_staging_concurrency_check.py ...). Nothing was written.")
    if existing is None:
        _assert_no_real_app_db()
        import types
        mod = types.ModuleType("database")
        mod.__dict__["__wa_tripwire__"] = True
        mod.__getattr__ = lambda name: _Tripwire() if not name.startswith("__") else (_ for _ in ()).throw(AttributeError(name))
        sys.modules["database"] = mod
    else:
        _assert_no_real_app_db()
    if not _is_our_tripwire(sys.modules.get("database")):                       # belt and braces
        raise SafetyAbort("ABORT: tripwire is not active. Nothing was written.")


def parse_target(url):
    """(host, port) of the first node, from the connection string, without exposing credentials."""
    from pymongo.uri_parser import parse_uri
    host, port = parse_uri(url, warn=False)["nodelist"][0]
    return host, port


def fingerprint(host, port):
    return hashlib.sha256(("%s:%s" % (host.lower(), port)).encode()).hexdigest()[:10]


def mask_host(host):
    return (host[:3] + "***" + host[-14:]) if len(host) > 20 else (host[:2] + "***")


def _url():
    url = os.environ.get("CONC_MONGO_URL", "")
    if not url:
        raise SystemExit("CONC_MONGO_URL is not set (see docs/WHATSAPP_STAGING_CONCURRENCY_PROCEDURE.md)")
    return url


def _client():
    from pymongo import MongoClient
    return MongoClient(_url(), serverSelectionTimeoutMS=8000)


def _setup(db_name):
    _install_tripwire()
    assert NAME_RE.match(db_name) and db_name != APP_DB
    db = _client()[db_name]
    import services.wa_campaign_manager as M
    M.db = db                                         # every worker/delivery function reaches Mongo through M._db()
    import services.wa_campaign_worker as W
    assert M._db().name == db_name
    return db, M, W


def _claim_proc(db_name, cid, out_q):
    db, M, W = _setup(db_name)
    cfg, mine = W.config(), []
    while True:
        r = W.claim_recipient(cid, "p%d" % os.getpid(), cfg, datetime.utcnow())
        if not r:
            break
        mine.append(str(r["_id"]))
    out_q.put(mine)


def _lease_proc(db_name, rounds, out_q):
    """Tries to take the (single) campaign lease `rounds` times; returns the owner ids it won."""
    db, M, W = _setup(db_name)
    cfg, won, me = W.config(), [], "p%d" % os.getpid()
    for _ in range(rounds):
        if W._claim_campaign(me, cfg, datetime.utcnow()):
            won.append(me)
    out_q.put(won)


def _renew_proc(db_name, cid, out_q):
    """Tries to renew a campaign lease that belongs to somebody else; returns (ok, reason)."""
    db, M, W = _setup(db_name)
    b = W._Batch({"_id": M.ObjectId(cid)}, None, "test", W.config(), "p%d" % os.getpid())
    out_q.put(W._renew_campaign_lease(b))


def _rate_proc(db_name, start_at, out_q):
    db, M, W = _setup(db_name)
    cfg = {"rate_per_second": RATE_PER_SECOND}
    while time.time() < start_at:                       # wait for the common start: tokens taken earlier are NOT counted
        time.sleep(0.001)
    t_end = start_at + RATE_WINDOW_SECONDS
    n = 0
    while time.time() < t_end:
        if W.take_rate_token(cfg):
            n += 1
    out_q.put(n)


def _status_proc(db_name, events, out_q):
    db, M, W = _setup(db_name)
    import services.wa_delivery as D
    for status, ts in events:
        D.handle_status(msg_id="wamid.CONC", recipient="919000000001", status=status, timestamp=str(ts), raw={})
    out_q.put(1)


class _Sender:
    """What a child receives instead of a Queue: `put(x)` sends exactly one result down ITS OWN one-way pipe."""
    def __init__(self, conn):
        self.conn = conn

    def put(self, value):
        self.conn.send(value)
        self.conn.close()


def _run(procs, target, args, timeout=RUN_TIMEOUT):
    """Run `procs` children, one result from each. One one-way Pipe per child ties every result to its child, so a child that exits
    WITHOUT a result is detected reliably as EOF on its own pipe (no Queue.empty(), no feeder-thread races: data sent before the child
    exits stays readable). Never hangs: any child still silent after `timeout` seconds ends the run, and every child is terminated.
    Raises RuntimeError on a crashed child, a child that exits without a result, a non-zero exit, or a timeout. -> results in child order."""
    from multiprocessing.connection import wait
    ps, pending = [], {}
    results, problem = [None] * procs, ""
    try:
        for i in range(procs):
            recv, send = mp.Pipe(duplex=False)
            p = mp.Process(target=target, args=args + (_Sender(send),))
            p.start()
            send.close()               # parent drops ITS copy of the send end (and children started later never inherit it): EOF is reliable
            ps.append(p)
            pending[recv] = i
        deadline = time.time() + timeout
        while pending and not problem:
            left = deadline - time.time()
            if left <= 0:
                problem = "timed out after %ss waiting for children" % timeout
                break
            for conn in wait(list(pending), timeout=min(0.5, left)):
                i = pending.pop(conn)
                try:
                    results[i] = conn.recv()
                except EOFError:       # the child closed its end without ever sending a result
                    ps[i].join(timeout=5)
                    problem = "child %d exited (code %s) without a result" % (i, ps[i].exitcode)
                finally:
                    conn.close()
    finally:
        for conn in pending:
            conn.close()
        for p in ps:
            p.join(timeout=0.5 if problem else 5)
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
    if not problem:
        bad = [p.exitcode for p in ps if p.exitcode != 0]
        if bad:
            problem = "child exited with code %s" % bad[0]
    if problem:
        raise RuntimeError(problem)
    return results


def _list_dbs(client):
    try:
        return sorted(client.list_database_names())
    except Exception:
        return None                                    # no listDatabases privilege: the caller fails closed where it matters


# ---- ownership --------------------------------------------------------------------------------

def _is_windows():
    return os.name == "nt"


def owner_dir():
    """Windows: ONLY the per-user profile location (a custom folder could be shared, and an ACL cannot be trusted there), so the override is
    refused. POSIX: ~/.offro_conc_check, or CONC_OWNER_DIR."""
    if _is_windows():
        if os.environ.get(OWNER_DIR_ENV):
            raise SafetyAbort("ABORT: %s is not allowed on Windows; the ownership records must live in your own profile "
                              "(%%USERPROFILE%%\\.offro_conc_check). Unset it and retry. Nothing was written." % OWNER_DIR_ENV)
        prof = os.environ.get("USERPROFILE")
        if not prof:
            raise SafetyAbort("ABORT: USERPROFILE is not set, so a private ownership folder cannot be established. Nothing was written.")
        return os.path.join(prof, ".offro_conc_check")
    return os.environ.get(OWNER_DIR_ENV) or os.path.join(os.path.expanduser("~"), ".offro_conc_check")


# ---- Windows: restrictive per-user ACL (fail closed) -------------------------------------------

def _icacls(*args):
    r = subprocess.run(["icacls", *args], capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout


def _whoami():
    """(DOMAIN\\user, SID) of the current user, or None."""
    r = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    parts = [p.strip().strip('"') for p in r.stdout.strip().splitlines()[0].split('","')]
    return (parts[0], parts[1]) if len(parts) == 2 and parts[1].startswith("S-1-") else None


_ACE_RE = re.compile(r"^(?P<name>.+?):(?P<perms>(?:\([^)]*\))+)\s*$")


def _win_acl_is_private(path):
    """True only if the ACL has NO inherited entries and every entry belongs to the current user (no Everyone / Users / Authenticated Users,
    no other account)."""
    me = _whoami()
    if me is None:
        return False
    rc, out = _icacls(path)
    if rc != 0:
        return False
    aces = 0
    for line in out.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("successfully processed", "failed processing")):
            continue
        if line.lower().startswith(path.lower()):
            line = line[len(path):].strip()
        m = _ACE_RE.match(line)
        if not m:
            return False                                           # anything we cannot parse is not trusted
        if m.group("name").strip().lower() != me[0].lower() or "(I)" in m.group("perms"):
            return False
        aces += 1
    return aces >= 1


def _win_make_private(path, is_dir):
    me = _whoami()
    if me is None:
        return False
    grant = "*%s:(OI)(CI)F" % me[1] if is_dir else "*%s:F" % me[1]
    rc, _ = _icacls(path, "/inheritance:r", "/grant:r", grant)
    return rc == 0 and _win_acl_is_private(path)


def _record_path(run_id):
    assert re.fullmatch(r"[0-9a-f]{12}", run_id or "")
    return os.path.join(owner_dir(), "%s.json" % run_id)


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_run_id():
    return uuid.uuid4().hex[:12]


def create_record(run_id):
    """Independent ownership record, kept OUTSIDE MongoDB: a random secret token generated by this process, stored in a local file private to the
    current user (POSIX 0600 in a 0700 folder; Windows: an explicit per-user ACL with inheritance removed, verified, or the run aborts). Holds no
    MongoDB credentials, host or URL. Created BEFORE anything is written to MongoDB. Returns the token."""
    token = secrets.token_hex(32)
    db_name = PREFIX + run_id
    path = created = None
    try:
        d = owner_dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        if _is_windows() and not _win_make_private(d, True):
            raise SafetyAbort("ABORT: could not establish a private (current-user-only) Windows ACL on the ownership folder. Nothing was "
                              "written to MongoDB.")
        path = _record_path(run_id)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))               # exclusive: never overwrites; empty so far
        created = path
        if _is_windows() and not _win_make_private(path, False):
            raise SafetyAbort("ABORT: could not establish a private Windows ACL on the ownership record. Nothing was written to MongoDB.")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"tool": TOOL_ID, "run_id": run_id, "db_name": db_name, "token": token,
                       "created_at": datetime.utcnow().isoformat()}, f)
    except SafetyAbort:
        _discard(created)
        raise
    except (OSError, subprocess.SubprocessError) as e:                   # includes icacls / whoami missing, failing or timing out
        _discard(created)
        raise SafetyAbort("ABORT: cannot create the local ownership record (%s). Nothing was written to MongoDB." % type(e).__name__)
    return token


def _discard(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def read_record(run_id):
    """(record, reason). Never raises. A record is trusted only if it is a regular file (not a link), private to this user (POSIX mode; on
    Windows a verified current-user-only ACL on the folder AND the file), well-formed, and its tool / run id / database name / token agree."""
    try:
        path = _record_path(run_id)
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return None, "local_record_invalid"
        if os.name == "posix" and st.st_mode & 0o077:
            return None, "local_record_invalid"
        if _is_windows() and not (_win_acl_is_private(owner_dir()) and _win_acl_is_private(path)):
            return None, "local_record_invalid"
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except FileNotFoundError:
        return None, "no_local_record"
    except Exception:
        return None, "local_record_invalid"
    tok = rec.get("token") if isinstance(rec, dict) else None
    if not (isinstance(tok, str) and re.fullmatch(r"[0-9a-f]{64}", tok)):
        return None, "local_record_invalid"
    if rec.get("tool") != TOOL_ID or rec.get("run_id") != run_id or rec.get("db_name") != PREFIX + run_id:
        return None, "local_record_mismatch"
    return rec, "ok"


def remove_record(run_id):
    try:
        os.remove(_record_path(run_id))
        return True
    except OSError:
        return False


# Document-level ownership proof ------------------------------------------------------------------
# The proof is an HMAC-SHA256, keyed with the secret local token, over a canonical encoding of the document's IMMUTABLE OWNERSHIP FIELDS:
#     run id, collection, _id (with its type), the tool-set role field, and the per-collection content fields below.
# IMMUTABLE = set once by this tool and never changed by the worker functions the checks exercise. Changing, adding or removing any of them
# after stamping invalidates the proof.  MUTABLE = every other field (the worker's state: status, run.*, state, attempts, lease_*,
# send_started, next_attempt_at, message_id, delivery_*, error*, updated_at, count ...). Mutable fields are deliberately NOT sealed, because
# the worker legitimately rewrites them; their values are therefore NOT protected by the proof (see the procedure's "limitations").
ROLE = {OWNER_COLL: "marker", "wa_campaigns": "campaign", "wa_campaign_recipients": "recipient", "wa_campaign_rate": "rate_counter"}
IMMUTABLE_FIELDS = {
    OWNER_COLL: ("_conc_role", "tool", "run_id", "db_name", "token_hash", "created_at"),
    "wa_campaigns": ("_conc_role",),
    "wa_campaign_recipients": ("_conc_role", "campaign_id", "phone", "idx"),
    "wa_campaign_rate": ("_conc_role", "at"),
}
PROOF_VERSION = "conc-proof-v2"
_ABSENT = ("absent",)


def rate_counter_seconds(W, offset, span):
    """Ids ('s<second>') of the limiter counters the workers can use in the window, computed the way the host computes them. The worker
    derives the second from a NAIVE UTC datetime via `.timestamp()`, which Python interprets as LOCAL time: on any host whose time zone is not
    UTC the ids are shifted by the UTC offset (e.g. +05:30 -> 19800 s). The tool pre-creates (and stamps) the counters, so it must cover that
    form as well as true epoch seconds. Unused counters are stamped too, so they do no harm; any other id would stay unproven and refuse."""
    now = getattr(W, "_now", datetime.utcnow)
    bases = (int(time.time() + offset), int((now() + timedelta(seconds=offset)).timestamp()))
    return sorted({sec for b in bases for sec in range(b - 1, b + span)})


def _canon(v):
    """Deterministic, type-tagged encoding; datetimes at MongoDB's millisecond precision. Unsupported types raise (callers fail closed)."""
    if v is None:
        return ["null"]
    if isinstance(v, bool):
        return ["bool", v]
    if isinstance(v, int):
        return ["int", v]
    if isinstance(v, float):
        return ["float", repr(v)]
    if isinstance(v, str):
        return ["str", v]
    if isinstance(v, ObjectId):
        return ["oid", str(v)]
    if isinstance(v, datetime):
        d = v - datetime(1970, 1, 1)
        return ["dt", d.days * 86400000 + d.seconds * 1000 + d.microseconds // 1000]
    raise TypeError("unsupported type in an ownership field")


def doc_proof(token, run_id, coll, doc):
    fields = IMMUTABLE_FIELDS[coll]                                   # KeyError for an unknown collection: fail closed
    body = {"v": PROOF_VERSION, "run": run_id, "coll": coll, "id": _canon(doc["_id"]),
            "f": {k: (_canon(doc[k]) if k in doc else list(_ABSENT)) for k in fields}}
    return hmac.new(token.encode(), json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def stamp(coll, docs, run_id, token):
    """EVERY document this tool writes goes through here: explicit _id + run id + role + keyed proof over the immutable fields."""
    out = []
    for d in docs:
        d = dict(d)
        d.setdefault("_id", ObjectId())
        d["_conc_run"] = run_id
        d["_conc_role"] = ROLE[coll]
        d["_conc_proof"] = doc_proof(token, run_id, coll, d)
        out.append(d)
    return out


def write_marker(db, run_id, token):
    """THE FIRST WRITE to MongoDB of a run (a stamped document like every other)."""
    assert db.name == PREFIX + run_id
    db[OWNER_COLL].insert_one(stamp(OWNER_COLL, [{"_id": "owner", "tool": TOOL_ID, "run_id": run_id, "db_name": db.name,
                                                  "token_hash": _token_hash(token), "created_at": datetime.utcnow()}], run_id, token)[0])


def _classify(d, coll, run_id, token):
    """None if the document is proven to be this run's, else a short reason category (never document content)."""
    try:
        if not isinstance(d, dict):
            return "not_a_document"
        if "_id" not in d:
            return "no_id"
        if "_conc_run" not in d and "_conc_role" not in d and "_conc_proof" not in d:
            return "missing_ownership_metadata"
        if d.get("_conc_run") != run_id:
            return "missing_or_wrong_run_tag"
        if d.get("_conc_role") != ROLE[coll]:
            return "missing_or_wrong_role"
        proof = d.get("_conc_proof")
        if not isinstance(proof, str) or not re.fullmatch(r"[0-9a-f]{64}", proof):
            return "missing_or_malformed_proof"
        if not hmac.compare_digest(proof, doc_proof(token, run_id, coll, d)):
            return "proof_mismatch"                       # sealed field changed, or proof replayed / forged
        return None
    except Exception:
        return "unverifiable"                            # e.g. an unsupported value in a sealed field


def _is_own(d, coll, run_id, token):
    return _classify(d, coll, run_id, token) is None


def _foreign_documents(database, run_id, token):
    """Number of documents, across EVERY collection, that cannot be positively attributed to this run (missing tag or proof, wrong proof,
    malformed). Any of them forbids the drop."""
    return sum(1 for coll in database.list_collection_names() for d in database[coll].find({}) if not _is_own(d, coll, run_id, token))


def describe_foreign(client, name):
    """Diagnostics for a refusal: lines like '  wa_campaign_rate: 2 document(s): missing_ownership_metadata'. Collection names, counts and reason
    categories ONLY - never ids, phone numbers or any field value."""
    rec, _ = read_record(name[len(PREFIX):])
    if rec is None:
        return []
    counts = {}
    try:
        for coll in client[name].list_collection_names():
            for d in client[name][coll].find({}):
                why = _classify(d, coll, name[len(PREFIX):], rec["token"]) if coll in ROLE else "unexpected_collection"
                if why:
                    counts[(coll, why)] = counts.get((coll, why), 0) + 1
    except Exception:
        return ["  (diagnostics unavailable)"]
    return ["  %s: %d document(s): %s" % (c, n, w) for (c, w), n in sorted(counts.items())]

def validate_owned(client, name, expected_run_id=None):
    """(ok, reason). Owned = exact name pattern + a VALID LOCAL RECORD (never skipped) + a MongoDB marker agreeing with it + only this tool's
    collections + EVERY document in every collection proven to belong to this run (keyed proof). The marker alone is never trusted."""
    if not NAME_RE.match(name or "") or name == APP_DB:
        return False, "name_pattern"
    run_id = name[len(PREFIX):]
    if expected_run_id is not None and run_id != expected_run_id:
        return False, "run_id_mismatch"
    rec, why = read_record(run_id)
    if rec is None:
        return False, why
    try:
        database = client[name]
        marker = database[OWNER_COLL].find_one({"_id": "owner"})
        if not isinstance(marker, dict):
            return False, "no_marker"
        th = marker.get("token_hash")
        if marker.get("tool") != TOOL_ID or marker.get("run_id") != run_id or marker.get("db_name") != name \
                or not isinstance(th, str) or not hmac.compare_digest(th, _token_hash(rec["token"])):
            return False, "marker_invalid"
        if set(database.list_collection_names()) - KNOWN_COLLECTIONS:
            return False, "foreign_collections"
        if database[OWNER_COLL].count_documents({}) != 1:
            return False, "foreign_documents"
        if _foreign_documents(database, run_id, rec["token"]):
            return False, "foreign_documents"
    except Exception:
        return False, "check_failed"
    return True, "owned"


def drop_owned(client, name, expected_run_id=None):
    """Drops `name` ONLY after validate_owned() succeeds (local record AND MongoDB marker). The single place in the tool that drops a
    database. The local record is removed only after the database is verified gone."""
    ok, why = validate_owned(client, name, expected_run_id)
    if not ok:
        return False, why
    client.drop_database(name)
    if name in (_list_dbs(client) or []):
        return False, "drop_not_verified"
    remove_record(name[len(PREFIX):])
    return True, "dropped"


def _prefixed(dbs):
    return [d for d in (dbs or []) if d.startswith(PREFIX) and d != APP_DB]


# ---- server role / target ----------------------------------------------------------------------

def server_role(client):
    """'staging' (holds the application DB), 'isolated' (nothing but system DBs and our own prefixed DBs) or 'unknown'
    (anything else, or the database list is not readable). Fails closed."""
    dbs = _list_dbs(client)
    if dbs is None:
        return "unknown", None
    if APP_DB in dbs:
        return "staging", dbs
    others = set(dbs) - SYSTEM_DBS - set(_prefixed(dbs))
    return ("isolated" if not others else "unknown"), dbs


def show_target(reveal=False):
    client = _client()
    host, port = parse_target(_url())
    try:
        info = client.server_info()
    except Exception as e:
        print("Could not reach the server (%s). Check the URL / network. Nothing was written." % type(e).__name__)
        return 1
    role, dbs = server_role(client)
    print("Target (credentials are never printed)")
    if reveal:
        print("  host:port (FULL)     : %s:%s     <- compare with Railway; do not screenshot or share" % (host, port))
    else:
        print("  host (masked)        : %s:%s     (add --reveal-host to see it in full)" % (mask_host(host), port))
    print("  TARGET FINGERPRINT   : %s     (a typo guard only - it does NOT prove this is Staging)" % fingerprint(host, port))
    print("  MongoDB version      : %s" % info.get("version", "?"))
    if dbs is None:
        print("  databases            : (listing not permitted for this user)")
    else:
        print("  databases            : %s" % ", ".join(dbs))
        print("  application DB '%s' : %s" % (APP_DB, "present" if APP_DB in dbs else "NOT present on this server"))
        owned = [d for d in _prefixed(dbs) if validate_owned(client, d)[0]]
        print("  throw-away DBs       : %d owned by this tool, %d prefixed but NOT owned (never touched)" %
              (len(owned), len(_prefixed(dbs)) - len(owned)))
    print("  server role          : %s" % {"staging": "holds the application DB  -> use  --target staging",
                                          "isolated": "empty / dedicated      -> use  --target isolated",
                                          "unknown": "NEITHER (unreadable list or unexpected databases) -> a run would be refused"}[role])
    print("MANDATORY manual check: compare host AND port with the Railway Staging MongoDB service (and confirm they differ from Production).")
    print("No writes were made.")
    return 0


def _require_target(a):
    """All pre-write interlocks. Returns the connected client or raises SafetyAbort."""
    host, port = parse_target(_url())
    if not a.expect_target or fingerprint(host, port) != a.expect_target:
        raise SafetyAbort("ABORT: --expect-target does not match this connection string (run --show-target). Nothing was written.")
    client = _client()
    role, dbs = server_role(client)
    if role != a.target:
        why = {"staging": "this server holds '%s' (the application database), so it can only be used with --target staging" % APP_DB,
               "isolated": "this server is empty/dedicated, so it can only be used with --target isolated (a Staging run needs '%s' present)" % APP_DB,
               "unknown": "this server is neither the application's Mongo nor a fresh isolated one (or its database list is unreadable)"}[role]
        raise SafetyAbort("ABORT: --target %s refused: %s. Nothing was written." % (a.target, why))
    return client


def cleanup_leftovers(client):
    dbs = _list_dbs(client)
    if dbs is None:
        print("Cannot list databases with this user; nothing dropped.")
        return 1
    dropped, refused = [], []
    for d in _prefixed(dbs):
        ok, why = drop_owned(client, d)
        (dropped if ok else refused).append((d, why))
    print("Dropped %d throw-away database(s) owned by this tool." % len(dropped))
    for d, why in refused:
        print("REFUSED (left untouched, not proven to be owned by this tool): %s [%s]" % (d, why))
        if why == "foreign_documents":
            print("\n".join(describe_foreign(client, d)))
    if refused:
        print("Inspect the refused database(s) yourself; this tool will never delete them.")
    return 0 if not refused else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=("staging", "isolated"), default="staging")
    ap.add_argument("--yes-this-is-staging", action="store_true")
    ap.add_argument("--yes-this-is-isolated", action="store_true")
    ap.add_argument("--show-target", action="store_true", help="read-only: print the target + fingerprint, then exit")
    ap.add_argument("--reveal-host", action="store_true", help="with --show-target: print the FULL host:port for manual comparison")
    ap.add_argument("--cleanup-leftovers", action="store_true", help="drop leftover throw-away databases PROVEN to be owned by this tool")
    ap.add_argument("--expect-target", default="")
    ap.add_argument("--processes", type=int, default=8)
    ap.add_argument("--recipients", type=int, default=400)
    a = ap.parse_args()
    try:
        _install_tripwire()                              # first thing, for every mode: fail closed if it cannot be installed
        if a.show_target:
            sys.exit(show_target(a.reveal_host))
        flag = a.yes_this_is_staging if a.target == "staging" else a.yes_this_is_isolated
        if not flag:
            raise SystemExit("--yes-this-is-%s is required for --target %s (or use --show-target)" % (a.target, a.target))
        if not (1 <= a.processes <= MAX_PROCESSES and 1 <= a.recipients <= MAX_RECIPIENTS):
            raise SystemExit("--processes must be 1..%d and --recipients 1..%d" % (MAX_PROCESSES, MAX_RECIPIENTS))
        client = _require_target(a)
        if a.cleanup_leftovers:
            sys.exit(cleanup_leftovers(client))
        sys.exit(run_checks(a))
    except SafetyAbort as e:
        print(e)
        sys.exit(2)
    except SystemExit:
        raise
    except Exception as e:                              # driver messages can contain host names: class name only
        print("FAILED before/while connecting: %s. Run --cleanup-leftovers if a run was interrupted." % type(e).__name__)
        sys.exit(1)


def run_checks(a):
    run_id = new_run_id()
    name = PREFIX + run_id
    token = create_record(run_id)                     # local record first (outside MongoDB)
    db, M, W = _setup(name)
    write_marker(db, run_id, token)                   # FIRST write: ownership marker; everything after lives in this database only
    ok, total = True, 8
    results = []

    def put(coll, docs):                                # THE only way the checks insert: every document is stamped
        docs = stamp(coll, docs, run_id, token)
        db[coll].insert_many(docs)
        return [d["_id"] for d in docs]

    def check(label, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        results.append(bool(cond))
        print("%-64s %s %s" % (label, "PASS" if cond else "FAIL", detail))

    def step(label, fn):
        """A runner failure (crashed / hung child) is a FAILED check with a reason, never a hang or a traceback."""
        try:
            for coll in (M.RECIPIENTS, M.CAMPAIGNS):    # each check starts clean: remove only documents PROVEN to be this run's;
                db[coll].delete_many({"_id": {"$in": [d["_id"] for d in db[coll].find({}) if _is_own(d, coll, run_id, token)]}})
            fn()
        except RuntimeError as e:
            check(label, False, "(%s)" % e)

    now0 = datetime.utcnow()

    def campaign(owner, lease_delta):
        return str(put(M.CAMPAIGNS, [{"status": "running", "run": {
            "lease_owner": owner, "lease_until": datetime.utcnow() + timedelta(seconds=lease_delta), "backoff_until": W.EPOCH}}])[0])

    def c1():
        W.ensure_indexes()
        cid, now = "conc-campaign", datetime.utcnow()
        put(M.RECIPIENTS, [{"campaign_id": cid, "phone": "9190%07d" % i, "state": "queued", "attempts": 0,
                                       "next_attempt_at": now, "send_started": False} for i in range(a.recipients)])
        claimed = [x for part in _run(a.processes, _claim_proc, (name, cid), timeout=300) for x in part]
        in_db = db[M.RECIPIENTS].count_documents({"campaign_id": cid, "state": "sending", "attempts": 1})
        check("1. recipients: each claimed by exactly one process", len(claimed) == a.recipients and len(set(claimed)) == a.recipients
              and in_db == a.recipients, "(%d claims / %d recipients / %d in DB with attempts=1 / %d processes)"
              % (len(claimed), a.recipients, in_db, a.processes))

    def c2():
        cid = str(put(M.CAMPAIGNS, [{"status": "running", "run": {"lease_until": W.EPOCH, "backoff_until": W.EPOCH}}])[0])
        won = [x for part in _run(a.processes, _lease_proc, (name, 20)) for x in part]
        owner = db[M.CAMPAIGNS].find_one({"_id": M.ObjectId(cid)})["run"]["lease_owner"]
        check("2. campaign: one process wins the claim", len(won) == 1 and owner == won[0], "(winners=%d)" % len(won))

    def c3():
        cid = campaign("owner-A", 600)                # generous, so slow process start-up can never expire it mid-check
        oid = M.ObjectId(cid)
        before = db[M.CAMPAIGNS].find_one({"_id": oid})["run"]
        res = _run(a.processes, _renew_proc, (name, cid))
        after = db[M.CAMPAIGNS].find_one({"_id": oid})["run"]
        cfg = W.config()
        own = W._renew_campaign_lease(W._Batch({"_id": oid}, None, "test", cfg, "owner-A"))
        mine = db[M.CAMPAIGNS].find_one({"_id": oid})["run"]
        want = datetime.utcnow() + timedelta(seconds=cfg["lease_seconds"])        # a renewal sets the lease to now + lease_seconds
        renewed = abs((mine["lease_until"] - want).total_seconds()) < 15 and mine["lease_owner"] == "owner-A"
        check("3. lease renewal cannot overwrite another worker's lease",
              all(tuple(r) == (False, "lease_lost") for r in res) and after == before and tuple(own) == (True, "ok") and renewed,
              "(%d foreign renewals refused and the lease untouched; the owner's own renewal worked)" % len(res))

    def c4():
        cid = campaign("owner-A", -2)                 # owner A's lease has EXPIRED
        stale = W._renew_campaign_lease(W._Batch({"_id": M.ObjectId(cid)}, None, "test", W.config(), "owner-A"))
        won = [x for part in _run(a.processes, _lease_proc, (name, 5)) for x in part]
        owner = db[M.CAMPAIGNS].find_one({"_id": M.ObjectId(cid)})["run"]["lease_owner"]
        check("4. expired lease: not revived by its old owner, taken by exactly one", stale == (False, "lease_lost") and len(won) == 1
              and owner == won[0], "(winners=%d)" % len(won))

    def c5():
        put(M.RECIPIENTS, [{"campaign_id": "d", "phone": "919000000001", "state": "sent", "message_id": "wamid.CONC", "delivery_rank": 2}])
        ev = [("sent", 100), ("delivered", 200), ("read", 300), ("delivered", 200), ("sent", 100), ("read", 300)]
        _run(a.processes, _status_proc, (name, ev))
        final = db[M.RECIPIENTS].find_one({"message_id": "wamid.CONC"})
        check("5. delivery status: concurrent / out-of-order events end at read", final["state"] == "read" and final["delivery_rank"] == 4)

    def c6():
        start_at = time.time() + RATE_START_DELAY
        # the limiter's per-second counters are created HERE, stamped, so that every document in the database is attributable;
        # the worker only $inc's them (see rate_counter_seconds for why two id families are covered)
        put(W.RATE, [{"_id": "s%d" % sec, "count": 0, "at": datetime.utcnow()}
                     for sec in rate_counter_seconds(W, RATE_START_DELAY, RATE_WINDOW_SECONDS + 3)])
        sent = sum(_run(a.processes, _rate_proc, (name, start_at), timeout=RUN_TIMEOUT))
        lo, hi = rate_lower_bound(RATE_PER_SECOND, RATE_WINDOW_SECONDS), rate_upper_bound(RATE_PER_SECOND, RATE_WINDOW_SECONDS)
        check("6. rate limit: %d/s cluster-wide over a %d s window" % (RATE_PER_SECOND, RATE_WINDOW_SECONDS), lo <= sent <= hi,
              "(sent=%d, expected %d..%d)" % (sent, lo, hi))

    def c7():
        past = datetime.utcnow() - timedelta(seconds=5)
        put(M.RECIPIENTS, [{"campaign_id": "r", "idx": i, "phone": "9190%07d" % i, "state": "sending", "lease_owner": "dead",
                                       "lease_until": past, "send_started": i % 2 == 0, "attempts": 1} for i in range(10)])
        res = W.recover_abandoned()
        unc = {d["idx"] for d in db[M.RECIPIENTS].find({"state": "uncertain"})}
        que = {d["idx"] for d in db[M.RECIPIENTS].find({"state": "queued"})}
        check("7a. recovery: send_started=true -> uncertain, false -> queued", res == {"requeued": 5, "uncertain": 5}
              and unc == {0, 2, 4, 6, 8} and que == {1, 3, 5, 7, 9}, str(res))
        claimed = [x for part in _run(a.processes, _claim_proc, (name, "r")) for x in part]
        got = {d["idx"] for d in db[M.RECIPIENTS].find({"_id": {"$in": [M.ObjectId(x) for x in claimed]}})}
        still = db[M.RECIPIENTS].count_documents({"state": "uncertain", "attempts": 1, "send_started": True})
        check("7b. uncertain recipients are never claimed (never resent)", len(claimed) == 5 and got == {1, 3, 5, 7, 9} and still == 5,
              "(re-claimed %d of the 5 queued; 5 uncertain untouched)" % len(claimed))

    try:
        for label, fn in (("1", c1), ("2", c2), ("3", c3), ("4", c4), ("5", c5), ("6", c6), ("7", c7)):
            step("check " + label, fn)
    finally:
        try:
            dropped, why = drop_owned(db.client, name, run_id)     # validated: marker + run id + only this tool's collections
            gone = dropped and name not in (_list_dbs(db.client) or [])
            if gone:
                print("cleanup: throw-away database dropped and verified: yes")
            else:
                print("cleanup: NOT dropped (%s) - inspect it yourself; --cleanup-leftovers will also refuse it unless proven owned" % why)
                if why == "foreign_documents":
                    print("\n".join(describe_foreign(db.client, name)))
                ok = False
        except Exception as e:
            print("cleanup: FAILED (%s) - run --cleanup-leftovers" % type(e).__name__)
            ok = False
    good = ok and len(results) == 8 and all(results)
    print("RESULT:", "ALL PASS (%d/%d checks)" % (len(results), 8) if good else "FAILURES (%d/%d checks passed)" % (sum(results), 8))
    return 0 if good else 1


if __name__ == "__main__":
    main()
