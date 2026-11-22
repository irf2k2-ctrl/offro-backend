"""One-time translation transfer: staging -> production (Stage 4).  STANDALONE, MANUAL, NEVER AUTOMATIC.

Two separate commands, so no single run is ever connected to both databases:

  export   reads the SOURCE (staging) database, READ-ONLY, and writes a package file (JSON, integrity-hashed).
  import   reads that package file and compares it with the TARGET (production) database.
           DRY RUN is the default and writes nothing. Writes happen only with --apply (plus an explicit database confirmation).

Safety rules (all enforced in code and covered by tests):
  * No credentials anywhere: connection strings are read from environment variables whose NAMES you pass on the command line.
    Nothing is embedded, printed, logged or written into the package (only a one-way fingerprint of host + database name).
  * Nothing is ever overwritten or deleted. Only missing documents are inserted. Anything already present in the target is either
    identical (skipped) or a CONFLICT, and any conflict stops the whole import before the first write.
  * The package must pass: format check, SHA-256 of its content, language registry, key registry, every version's SHA-256 recomputed
    from its strings, every text validated against the key registry (placeholders, length, characters).
  * The target must not be the source (same host + database is refused), must carry no data labelled for a different environment,
    and --apply requires --confirm-target-db to repeat the target database name.
  * Copied: languages, key registry, the LATEST published version of each language+track, and the matching published working values.
    NOT copied: kill switch (production keeps its own), audit trail, drafts / modified values, older versions.
  * Versions are inserted last, so the app is never served a half-copied language.
  * No scheduler, no sync, no background connection. Delete the package file after the transfer (it contains translation text).

Examples (PowerShell; names are only examples):
  $env:TRANSLATION_SOURCE_MONGO_URL = "<staging connection string>"
  python tools/transfer_translations.py export --source-db offro_db --source-environment staging --out transfer.json
  $env:TRANSLATION_TARGET_MONGO_URL = "<production connection string>"
  python tools/transfer_translations.py import transfer.json --target-db offro_db --target-environment production          # dry run
  python tools/transfer_translations.py import transfer.json --target-db offro_db --target-environment production \\
         --apply --confirm-target-db offro_db                                                                              # writes
"""
import argparse
import hashlib
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime

logger = logging.getLogger("offro.transfer")

FORMAT = "offro-translation-transfer"
FORMAT_VERSION = 1
TRACKS = ("app", "legal")
DIRECTIONS = ("ltr", "rtl")
LANG_STATUSES = ("draft", "active", "disabled")
ENVIRONMENTS = ("staging", "production")
LANGS, KEYS, VALUES, VERSIONS, AUDIT = (
    "translation_languages", "translation_keys", "translation_values", "translation_versions", "translation_audit")
DEFAULT_SOURCE_VAR = "TRANSLATION_SOURCE_MONGO_URL"
DEFAULT_TARGET_VAR = "TRANSLATION_TARGET_MONGO_URL"
SCHEMA = 1

_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ALLOWED_FORMAT_CHARS = {"‌", "‍", "‎", "‏", "؜"}
_ABSOLUTE_MAX_LENGTH = 2000


class TransferError(Exception):
    """A refused or failed transfer step. The message never contains connection strings or translation text."""


# ── hashing (must equal services.translations.hash_strings; a test pins that) ──────────────────────────────────────────
def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_strings(strings: dict) -> str:
    return sha256_hex(canonical_json(strings))


def value_hash(value: str) -> str:
    return sha256_hex(value or "")


def short(h) -> str:
    return (str(h)[:12] + "…") if h else "-"


def fingerprint(uri: str, db_name: str) -> str:
    """One-way identity of 'which server + which database' (credentials are never part of it)."""
    from pymongo.uri_parser import parse_uri
    parsed = parse_uri(uri, warn=False)
    nodes = sorted(f"{h}:{p}" for h, p in (parsed.get("nodelist") or parsed.get("nodes") or []))
    return sha256_hex(canonical_json({"nodes": [n.lower() for n in nodes], "db": db_name}))


# ── value validation against the key registry (same rules the dashboard enforces, minus the English-only URL rule) ────────
def check_value(key_doc: dict, value) -> str:
    """Return '' when acceptable, else a short problem code."""
    if not isinstance(value, str):
        return "value_not_text"
    if not value.strip():
        return "empty_value"
    if value != unicodedata.normalize("NFC", value).strip():
        return "not_normalised"
    limit = min(int(key_doc.get("max_length") or 500), _ABSOLUTE_MAX_LENGTH)
    if len(value) > limit:
        return "too_long"
    for ch in value:
        cat = unicodedata.category(ch)
        if (cat == "Cc" and ch != "\n") or (cat == "Cf" and ch not in _ALLOWED_FORMAT_CHARS):
            return "control_character"
    rest = _PLACEHOLDER_RE.sub("", value)
    if "{" in rest or "}" in rest:
        return "malformed_placeholder"
    if sorted(set(_PLACEHOLDER_RE.findall(value))) != sorted(set(key_doc.get("placeholders") or [])):
        return "placeholder_mismatch"
    return ""


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, datetime) else str(v or "")


# ── export (READ-ONLY on the source) ───────────────────────────────────────────────────────────────────────────────────────
def export_package(source_db, source_environment: str, tracks=TRACKS, source_fp: str = "") -> dict:
    if source_environment not in ENVIRONMENTS:
        raise TransferError(f"--source-environment must be one of {ENVIRONMENTS}")
    langs = [{"code": d["code"], "name_en": d.get("name_en", ""), "name_native": d.get("name_native", ""),
              "direction": d.get("direction", ""), "status": d.get("status", ""), "bundled": bool(d.get("bundled", False))}
             for d in source_db[LANGS].find({}).sort("code", 1)]
    keys = [{"key": d["key"], "english": d.get("english", ""), "placeholders": sorted(d.get("placeholders") or []),
             "is_plural": bool(d.get("is_plural", False)), "group": d.get("group", "app"),
             "screen_group": d.get("screen_group", ""), "description": d.get("description", ""),
             "max_length": int(d.get("max_length") or 500), "deprecated": bool(d.get("deprecated", False))}
            for d in source_db[KEYS].find({}).sort("key", 1)
            if d.get("group", "app") in TRACKS]       # whole legal / information documents (group "documents") are per environment: never exported
    versions = []
    for d in source_db[VERSIONS].find({}):
        if d.get("environment") not in (None, "", source_environment):
            raise TransferError(
                f"source has a version labelled environment={d.get('environment')!r}, not {source_environment!r}; refusing to export")
    for track in tracks:
        for lang in sorted({d["lang"] for d in source_db[VERSIONS].find({"track": track})}):
            latest = None
            for row in source_db[VERSIONS].find({"track": track, "lang": lang}).sort("version", -1).limit(1):
                latest = row
            versions.append({"track": track, "lang": lang, "version": int(latest["version"]), "hash": latest["hash"],
                             "strings": dict(latest["strings"]), "note": str(latest.get("note", ""))[:300],
                             "published_at": _iso(latest.get("published_at"))})
    if not versions:
        raise TransferError("the source has no published version for the selected tracks; nothing to export")
    data = {"languages": langs, "keys": keys, "versions": versions}
    pkg = {"format": FORMAT, "format_version": FORMAT_VERSION, "created_at": datetime.utcnow().isoformat() + "Z",
           "source_environment": source_environment, "source_fingerprint": source_fp, "tracks": list(tracks),
           "counts": {"languages": len(langs), "keys": len(keys), "versions": len(versions)},
           "data": data, "content_sha256": sha256_hex(canonical_json(data))}
    problems = validate_package(pkg)
    if problems:
        raise TransferError("the source data does not pass validation: " + "; ".join(problems[:10]))
    return pkg


# ── package validation (no database needed) ───────────────────────────────────────────────────────────────────────────────
def validate_package(pkg) -> list:
    """Return a list of problems (empty = valid). Never includes translation text."""
    p = []
    if not isinstance(pkg, dict) or pkg.get("format") != FORMAT:
        return ["not an OffrO translation transfer package"]
    if pkg.get("format_version") != FORMAT_VERSION:
        return [f"unsupported format_version {pkg.get('format_version')!r}"]
    data = pkg.get("data")
    if not isinstance(data, dict) or not all(isinstance(data.get(k), list) for k in ("languages", "keys", "versions")):
        return ["package data is malformed"]
    if pkg.get("content_sha256") != sha256_hex(canonical_json(data)):
        return ["package content hash mismatch (the file was changed or damaged)"]
    if pkg.get("source_environment") not in ENVIRONMENTS:
        p.append("source_environment missing or invalid")
    langs, seen = {}, set()
    for l in data["languages"]:
        c = l.get("code")
        if not isinstance(c, str) or not _LANG_RE.match(c):
            p.append(f"language code invalid: {str(c)[:20]!r}")
            continue
        if c in langs:
            p.append(f"duplicate language {c}")
        if l.get("direction") not in DIRECTIONS:
            p.append(f"language {c}: invalid direction")
        if l.get("status") not in LANG_STATUSES:
            p.append(f"language {c}: invalid status")
        if not str(l.get("name_en", "")).strip() or not str(l.get("name_native", "")).strip():
            p.append(f"language {c}: names required")
        langs[c] = l
    keys = {}
    for k in data["keys"]:
        n = k.get("key")
        if not isinstance(n, str) or not _KEY_RE.match(n):
            p.append(f"key name invalid: {str(n)[:30]!r}")
            continue
        if n in keys:
            p.append(f"duplicate key {n}")
        if k.get("group") not in TRACKS:
            p.append(f"key {n}: invalid group")
        keys[n] = k
    for v in data["versions"]:
        tag = f"{v.get('track')}/{v.get('lang')}"
        if v.get("track") not in TRACKS:
            p.append(f"version {tag}: invalid track")
            continue
        if v.get("lang") not in langs:
            p.append(f"version {tag}: language is not in the language registry")
        if (v.get("track"), v.get("lang")) in seen:
            p.append(f"version {tag}: more than one version in the package")
        seen.add((v.get("track"), v.get("lang")))
        if not isinstance(v.get("version"), int) or isinstance(v.get("version"), bool) or v["version"] < 1:
            p.append(f"version {tag}: version number must be an integer >= 1")
        strings = v.get("strings")
        if not isinstance(strings, dict) or not strings:
            p.append(f"version {tag}: strings missing or empty")
            continue
        if hash_strings(strings) != v.get("hash"):
            p.append(f"version {tag}: SHA-256 hash does not match the strings")
        for key, value in strings.items():
            kd = keys.get(key)
            if kd is None:
                p.append(f"version {tag}: key {key} is not in the key registry")
            elif kd.get("is_plural") or kd.get("deprecated"):
                p.append(f"version {tag}: key {key} is plural or deprecated")
            elif kd.get("group") != v["track"]:
                p.append(f"version {tag}: key {key} belongs to the {kd.get('group')} track")
            else:
                bad = check_value(kd, value)
                if bad:
                    p.append(f"version {tag}: key {key}: {bad}")
    return p


# ── import planning (READ-ONLY on the target) ─────────────────────────────────────────────────────────────────────────────
def _same(a: dict, b: dict, fields) -> bool:
    return all(a.get(f) == b.get(f) for f in fields)


_LANG_FIELDS = ("name_en", "name_native", "direction", "status")
_KEY_FIELDS = ("english", "placeholders", "is_plural", "group", "max_length", "deprecated")


def plan_import(pkg: dict, target_db, target_environment: str, source_fp: str = "", target_fp: str = "") -> dict:
    """Compare the package with the target. Returns a plan; raises TransferError for a refusal. Reads only."""
    if target_environment not in ENVIRONMENTS:
        raise TransferError(f"--target-environment must be one of {ENVIRONMENTS}")
    problems = validate_package(pkg)
    if problems:
        raise TransferError("package validation failed: " + "; ".join(problems[:15]) + (" …" if len(problems) > 15 else ""))
    if pkg["source_environment"] == target_environment:
        raise TransferError("source and target environment are the same; this tool only copies between different environments")
    if pkg.get("source_fingerprint") and target_fp and pkg["source_fingerprint"] == target_fp:
        raise TransferError("the target is the same server and database as the source; refusing")
    for row in target_db[VERSIONS].find({}):
        if row.get("environment") not in (None, "", target_environment):
            raise TransferError(
                f"the target already holds versions labelled environment={row.get('environment')!r}; "
                f"it is not a {target_environment} database; refusing")
    data = pkg["data"]
    plan = {"insert": {LANGS: [], KEYS: [], VALUES: [], VERSIONS: []}, "unchanged": {LANGS: 0, KEYS: 0, VALUES: 0, VERSIONS: 0},
            "conflicts": [], "versions_view": [], "package_sha256": pkg["content_sha256"], "target_environment": target_environment}
    for l in data["languages"]:
        cur = target_db[LANGS].find_one({"code": l["code"]})
        if cur is None:
            plan["insert"][LANGS].append(l)
        elif _same(cur, l, _LANG_FIELDS):
            plan["unchanged"][LANGS] += 1
        else:
            plan["conflicts"].append(f"language {l['code']} already exists in the target with different details")
    for k in data["keys"]:
        cur = target_db[KEYS].find_one({"key": k["key"]})
        if cur is None:
            plan["insert"][KEYS].append(k)
        elif _same({**cur, "placeholders": sorted(cur.get("placeholders") or [])}, k, _KEY_FIELDS):
            plan["unchanged"][KEYS] += 1
        else:
            plan["conflicts"].append(f"key {k['key']} already exists in the target with a different definition")
    keydefs = {k["key"]: k for k in data["keys"]}
    for v in data["versions"]:
        track, lang = v["track"], v["lang"]
        latest = None
        for row in target_db[VERSIONS].find({"track": track, "lang": lang}).sort("version", -1).limit(1):
            latest = row
        before = f"v{latest['version']} {short(latest['hash'])}" if latest else "nothing published"
        after = f"v{v['version']} {short(v['hash'])}"
        if latest is None:
            plan["insert"][VERSIONS].append(v)
            status = "will be created"
        elif latest["version"] == v["version"] and latest["hash"] == v["hash"]:
            plan["unchanged"][VERSIONS] += 1
            status = "already identical (skipped)"
        else:
            plan["conflicts"].append(
                f"{track}/{lang}: the target already has {before}; this tool never overwrites published translations")
            status = "CONFLICT"
        plan["versions_view"].append({"track": track, "lang": lang, "before": before, "after": after, "status": status,
                                      "strings": len(v["strings"])})
        for key, value in v["strings"].items():
            cur = target_db[VALUES].find_one({"lang": lang, "key": key})
            doc = {"lang": lang, "key": key, "track": track, "value": value}
            if cur is None:
                plan["insert"][VALUES].append(doc)
            elif cur.get("value") == value:
                plan["unchanged"][VALUES] += 1
            else:
                plan["conflicts"].append(f"{lang}/{key}: the target already has a different value")
    return plan


def format_report(plan: dict, applied: bool = False) -> str:
    out = ["", f"Target environment: {plan['target_environment']}   Package SHA-256: {short(plan['package_sha256'])}",
           "", "Before -> after (per language and track):"]
    for v in plan["versions_view"]:
        out.append(f"  {v['track']:<5} {v['lang']:<6} {v['before']:<28} -> {v['after']:<22} {v['strings']:>4} strings   {v['status']}")
    out += ["", "Collection                 to insert   already identical"]
    for c in (LANGS, KEYS, VALUES, VERSIONS):
        out.append(f"  {c:<24} {len(plan['insert'][c]):>9}   {plan['unchanged'][c]:>9}")
    out.append("")
    if plan["conflicts"]:
        out.append(f"CONFLICTS ({len(plan['conflicts'])}) - nothing can be written until these are resolved by hand:")
        out += [f"  - {c}" for c in plan["conflicts"][:50]]
    else:
        out.append("No conflicts.")
    out.append("Nothing overwritten or deleted. Kill switch, audit trail and drafts are not transferred.")
    return "\n".join(out)


# ── apply (the ONLY function that writes) ─────────────────────────────────────────────────────────────────────────────────
def apply_plan(plan: dict, target_db, source_environment: str, actor: str = "transfer-script") -> dict:
    from pymongo.errors import DuplicateKeyError
    if plan["conflicts"]:
        raise TransferError("refusing to write: the plan has conflicts")
    now = datetime.utcnow()
    target_db[LANGS].create_index("code", unique=True, name="translation_languages_code")
    target_db[KEYS].create_index("key", unique=True, name="translation_keys_key")
    target_db[VALUES].create_index([("lang", 1), ("key", 1)], unique=True, name="translation_values_lang_key")
    target_db[VERSIONS].create_index([("track", 1), ("lang", 1), ("version", 1)], unique=True,
                                     name="translation_versions_track_lang_version")
    counts = {c: len(plan["insert"][c]) for c in plan["insert"]}
    audit = {"at": now, "action": "transfer_import", "environment": plan["target_environment"], "admin": {"name": actor},
             "details": {"counts": counts, "package_sha256": plan["package_sha256"], "source_environment": source_environment},
             "schema": SCHEMA}
    # A started entry BEFORE any change: if it cannot be stored, nothing is written.
    aid = target_db[AUDIT].insert_one({**audit, "outcome": "started"}).inserted_id
    written = {c: 0 for c in counts}
    try:
        for l in plan["insert"][LANGS]:
            target_db[LANGS].insert_one({"code": l["code"], "name_en": l["name_en"], "name_native": l["name_native"],
                                         "direction": l["direction"], "status": l["status"], "bundled": l.get("bundled", False),
                                         "created_by": actor, "created_at": now, "schema": SCHEMA})
            written[LANGS] += 1
        for k in plan["insert"][KEYS]:
            target_db[KEYS].insert_one({**k, "schema": SCHEMA, "synced_at": now})
            written[KEYS] += 1
        for v in plan["insert"][VALUES]:
            target_db[VALUES].insert_one({"lang": v["lang"], "key": v["key"], "track": v["track"], "value": v["value"],
                                          "value_hash": value_hash(v["value"]), "status": "published", "updated_by": actor,
                                          "updated_at": now, "source": "transfer", "schema": SCHEMA})
            written[VALUES] += 1
        for v in plan["insert"][VERSIONS]:      # last: the app is served from versions only
            target_db[VERSIONS].insert_one({"track": v["track"], "lang": v["lang"], "version": v["version"], "strings": v["strings"],
                                            "hash": v["hash"], "note": v.get("note", ""), "published_by": {"name": actor},
                                            "published_at": now, "environment": plan["target_environment"],
                                            "transferred_from": source_environment, "schema": SCHEMA})
            written[VERSIONS] += 1
    except DuplicateKeyError as e:
        target_db[AUDIT].update_one({"_id": aid}, {"$set": {"outcome": "failed", "details.written": written}})
        raise TransferError(f"a document appeared in the target while writing (duplicate); stopped. Written so far: {written}. "
                            "Re-run the dry run to see the new state.") from e
    except Exception as e:
        target_db[AUDIT].update_one({"_id": aid}, {"$set": {"outcome": "failed", "details.written": written}})
        raise TransferError(f"write failed ({type(e).__name__}); stopped. Written so far: {written}. "
                            "Re-run the dry run to see the new state.") from e
    target_db[AUDIT].update_one({"_id": aid}, {"$set": {"outcome": "ok", "details.written": written}})
    return written


# ── command line ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _connect(var_name: str, db_name: str):
    """Return (database, fingerprint). The connection string is read from the named environment variable and never shown."""
    uri = os.environ.get(var_name, "")
    if not uri:
        raise TransferError(f"environment variable {var_name} is not set")
    try:
        from pymongo import MongoClient
        client = MongoClient(uri, serverSelectionTimeoutMS=8000)
        client.admin.command("ping")
        return client[db_name], fingerprint(uri, db_name)
    except TransferError:
        raise
    except Exception as e:
        raise TransferError(f"could not connect using {var_name} ({type(e).__name__})") from None


def _setup_logging(log_file):
    """Configure ONLY this tool's own logger (never the root logger), and never stack handlers on repeated calls."""
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def main(argv=None, connect=_connect) -> int:
    ap = argparse.ArgumentParser(description="One-time OffrO translation transfer (dry run by default).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="read the source database (read-only) and write a package file")
    ex.add_argument("--source-env-var", default=DEFAULT_SOURCE_VAR, help="NAME of the variable holding the source connection string")
    ex.add_argument("--source-db", required=True)
    ex.add_argument("--source-environment", required=True, choices=ENVIRONMENTS)
    ex.add_argument("--tracks", default=",".join(TRACKS))
    ex.add_argument("--out", required=True)
    ex.add_argument("--log-file")
    im = sub.add_parser("import", help="validate a package and compare it with the target (dry run unless --apply)")
    im.add_argument("package")
    im.add_argument("--target-env-var", default=DEFAULT_TARGET_VAR, help="NAME of the variable holding the target connection string")
    im.add_argument("--target-db", required=True)
    im.add_argument("--target-environment", required=True, choices=ENVIRONMENTS)
    im.add_argument("--apply", action="store_true", help="actually write (default: dry run, nothing is written)")
    im.add_argument("--confirm-target-db", help="with --apply: repeat the target database name")
    im.add_argument("--log-file")
    a = ap.parse_args(argv)
    _setup_logging(a.log_file)
    try:
        if a.cmd == "export":
            tracks = tuple(t for t in a.tracks.split(",") if t)
            if not tracks or any(t not in TRACKS for t in tracks):
                raise TransferError(f"--tracks must be a comma list of {TRACKS}")
            db, fp = connect(a.source_env_var, a.source_db)
            pkg = export_package(db, a.source_environment, tracks, fp)
            if os.path.exists(a.out):
                raise TransferError(f"{a.out} already exists; refusing to overwrite it")
            with open(a.out, "w", encoding="utf-8") as f:
                json.dump(pkg, f, ensure_ascii=False, indent=1, sort_keys=True)
            logger.info("exported %s to %s (read-only on the source)", pkg["counts"], a.out)
            print(f"Package written: {a.out}  content SHA-256 {pkg['content_sha256']}")
            print("It contains translation text but no credentials. Delete it after the transfer.")
            return 0
        try:
            with open(a.package, "r", encoding="utf-8") as f:
                pkg = json.load(f)
        except (OSError, ValueError) as e:
            raise TransferError(f"cannot read the package ({type(e).__name__})") from None
        if a.apply and a.confirm_target_db != a.target_db:
            raise TransferError("--apply requires --confirm-target-db with exactly the --target-db name")
        problems = validate_package(pkg)
        if problems:
            raise TransferError("package validation failed: " + "; ".join(problems[:15]))
        db, fp = connect(a.target_env_var, a.target_db)
        plan = plan_import(pkg, db, a.target_environment, pkg.get("source_fingerprint", ""), fp)
        print(format_report(plan))
        if plan["conflicts"]:
            logger.error("%d conflicts; nothing written", len(plan["conflicts"]))
            return 3
        if not a.apply:
            print("\nDRY RUN: nothing was written. Re-run with --apply --confirm-target-db <name> to write.")
            return 0
        written = apply_plan(plan, db, pkg["source_environment"])
        logger.info("applied: %s", written)
        print(f"\nAPPLIED. Documents written: {written}")
        return 0
    except TransferError as e:
        logger.error("%s", e)
        print("REFUSED:", e)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
