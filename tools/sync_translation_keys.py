"""Sync the translation KEY REGISTRY from the app's English ARB file (Phase G).

Keys come from the app, never from the dashboard. This tool reads app_en.arb and upserts `translation_keys`.
  * DRY RUN is the default: it parses and validates the file and prints what it found. It never connects to a database.
  * --apply writes ONLY the translation_keys collection (upsert). Keys no longer in the file are marked deprecated, never deleted.
    Nothing here touches values, versions, languages, the kill switch or any other collection.
  * It is never run automatically (not at startup, not on deploy). The real registry stays empty until the localization phase
    creates app_en.arb. A fixture file (test_data/app_en_fixture.arb) exists only for the tests.

ARB conventions understood (standard Flutter ARB plus three optional metadata fields on "@key"):
    "x-group": "legal"       -> the key belongs to the separate, review-gated legal track (default "app")
    "x-screen": "login"      -> screen group shown to translators
    "x-max-length": 40       -> longest allowed translation (default 500)
    "placeholders": {...}    -> placeholder names; otherwise they are read from {name} in the text
A value containing an ICU plural/select expression is stored as is_plural=true: bundled-only and read-only in the first release.

Usage:  python tools/sync_translation_keys.py path/to/app_en.arb            # dry run
        python tools/sync_translation_keys.py path/to/app_en.arb --apply    # write translation_keys
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ICU_RE = re.compile(r"\{\s*\w+\s*,\s*(plural|select|selectordinal)\s*,")
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def parse_arb(path: str) -> list:
    """Return registry entries (a list of dicts). Raises ValueError with a clear message on a malformed file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("ARB root must be a JSON object")
    entries, seen = [], set()
    for k, v in data.items():
        if k.startswith("@"):
            continue
        if not _KEY_RE.match(k):
            raise ValueError(f"invalid key name: {k[:60]!r}")
        if not isinstance(v, str):
            raise ValueError(f"value of {k!r} must be text")
        if k in seen:
            raise ValueError(f"duplicate key: {k}")
        seen.add(k)
        meta = data.get("@" + k) or {}
        if not isinstance(meta, dict):
            raise ValueError(f"metadata of {k!r} must be an object")
        group = str(meta.get("x-group", "app"))
        if group not in ("app", "legal"):
            raise ValueError(f"{k}: x-group must be app or legal")
        is_plural = bool(_ICU_RE.search(v))
        if isinstance(meta.get("placeholders"), dict):
            placeholders = sorted(meta["placeholders"])
        else:
            placeholders = sorted(set(_PLACEHOLDER_RE.findall(v)))
        entries.append({"key": k, "english": v, "placeholders": [] if is_plural else placeholders, "is_plural": is_plural,
                        "group": group, "screen_group": str(meta.get("x-screen", "")),
                        "description": str(meta.get("description", "")),
                        "max_length": int(meta.get("x-max-length") or 500)})
    return entries


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sync translation_keys from an English ARB file (dry run by default).")
    ap.add_argument("arb")
    ap.add_argument("--apply", action="store_true", help="write translation_keys (otherwise dry run, no database connection)")
    a = ap.parse_args(argv)
    try:
        entries = parse_arb(a.arb)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print("ARB file rejected:", e)
        return 2
    legal = sum(1 for e in entries if e["group"] == "legal")
    plural = sum(1 for e in entries if e["is_plural"])
    print(f"{len(entries)} keys parsed: {len(entries) - legal} app, {legal} legal, {plural} plural (read-only).")
    if not a.apply:
        print("Dry run: nothing was written. Re-run with --apply to update translation_keys.")
        return 0
    from services import translations as T
    res = T.sync_keys(entries)
    print("translation_keys updated:", res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
