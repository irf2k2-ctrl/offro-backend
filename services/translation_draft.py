"""services/translation_draft.py - "Download Draft" (XLSX) and "Import" for the translation module.

Download: one workbook per language + track. Sheet "Translations": Key | Feature / Screen | English (reference) | <language> translation | Status.
          Sheet "Info": what the file is for (schema marker, language, track, environment, the published baseline version/hash).
Import:   the edited workbook is read back and matched by the KEY column (a stable identifier), never by displayed text.
          - English and every other column except the translation column are ignored: the English source can never be changed here.
          - Each translation goes through the same validation as a manual edit (placeholders, length, hidden characters, ...).
          - It is compared with the last PUBLISHED text (T.effective_status): a value that differs is Modified, a value equal to the
            published text is Published. A value equal to the current working value is left alone (its status is kept).
          - Nothing is published and no version is created. Publish Changes is still the only way to go live.
Environment: a file belongs to ONE environment (staging or production, recorded in its Info sheet). The import compares it with the
          environment THIS server is running (T.environment()) and rejects the whole file (environment_mismatch) when it is missing,
          unknown or different, for the check and for the apply alike, before anything is read from or written to the database. A
          staging file can never be imported into production or the other way round.
Partial import (intentional): the file is validated row by row. "Check file" writes nothing and reports valid (= updated + unchanged),
          invalid and missing rows. "Apply import" stores every VALID changed row and SKIPS every invalid row (unknown / wrong-type /
          plural / deprecated / missing key, every occurrence of a duplicate key, failed validation, formulas, non-text cells): an
          invalid row is never written, in whole or in part. Unchanged and missing rows are left exactly as they are.
Safety:   size and structure limits, no macros, no formulas, text cells only, row cap; structural problems (wrong environment,
          language, text type, sheet) reject the whole file before any write.
"""
import io
import zipfile
from datetime import datetime

from services import translations as T
from services.translations import TranslationError

MARKER = "OFFRO_TRANSLATION_DRAFT"
SCHEMA_VERSION = "1"
MAX_BYTES = 2 * 1024 * 1024
MAX_UNCOMPRESSED = 30 * 1024 * 1024
MAX_ZIP_ENTRIES = 200
MAX_ROWS = 5000
REPORT_LIMIT = 100                       # invalid / missing / updated keys listed in a summary (counts are always exact)
SHEET, INFO = "Translations", "Info"
KNOWN_ENVIRONMENTS = ("staging", "production", "development", "test")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xl():
    """openpyxl, imported only when a draft is built or read: if the package is not installed the rest of the server (and every other
    translation feature) keeps working and only Download Draft / Import answer 503 xlsx_support_missing."""
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill, Protection
    except ImportError:
        raise TranslationError("xlsx_support_missing", 503, "The openpyxl package is not installed on this server")
    return openpyxl, Alignment, Font, PatternFill, Protection


def _eligible_keys(track: str) -> dict:
    """Registry keys that can be edited for this track: not deprecated, not plural (plural strings are bundled-only)."""
    return {k["key"]: k for k in T.db[T.KEYS].find({"deprecated": {"$ne": True}, "is_plural": {"$ne": True}}) if T.track_of(k) == track}


def _text(cell, value) -> None:
    """Always a TEXT cell, even for values starting with = + - @ (so nothing in a translation can run as a formula)."""
    cell.value = value
    cell.data_type = "s"


# ── download ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def build_draft(lang: str, track: str) -> tuple:
    """(xlsx bytes, filename). Read only."""
    openpyxl, Alignment, Font, PatternFill, Protection = _xl()
    Workbook = openpyxl.Workbook
    language = T.get_language(lang)
    T._check_track(track)
    working, baseline = T.working_strings(track, lang), T.published_strings(track, lang)
    latest = T.latest_version(track, lang)
    keys = _eligible_keys(track)
    rtl = language.get("direction") == "rtl"

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    head = ["Key (do not edit)", "Feature / Screen (do not edit)", "English (Reference) - read only",
            f"{language.get('name_en', lang)} translation (edit this column)", "Status (information only)"]
    for i, t in enumerate(head, 1):
        c = ws.cell(row=1, column=i, value=t)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="3E5F55" if i != 4 else "B45309")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for r, key in enumerate(sorted(keys), 2):
        kd = keys[key]
        value = working.get(key)
        status = T.effective_status(baseline, key, value) if value is not None else "missing"
        _text(ws.cell(row=r, column=1), key)
        _text(ws.cell(row=r, column=2), str(kd.get("screen_group") or ""))
        _text(ws.cell(row=r, column=3), str(kd.get("english") or ""))
        tc = ws.cell(row=r, column=4)
        _text(tc, value if value is not None else "")
        tc.protection = Protection(locked=False)                    # the only editable column when sheet protection is on
        _text(ws.cell(row=r, column=5), status)
        for col in (1, 2, 3, 5):
            ws.cell(row=r, column=col).alignment = Alignment(wrap_text=True, vertical="top")
        tc.alignment = Alignment(wrap_text=True, vertical="top", horizontal="right" if rtl else "left", readingOrder=2 if rtl else 1)
    for col, w in zip("ABCDE", (34, 24, 52, 52, 16)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:E{max(2, len(keys) + 1)}"
    ws.protection.sheet = True                                      # no password: a guard against accidents, not a security boundary
    ws.protection.autoFilter = False
    ws.protection.sort = False

    info = wb.create_sheet(INFO)
    rows = [("File", MARKER), ("Schema", SCHEMA_VERSION), ("Language", lang), ("Track", track), ("Environment", T.environment()),
            ("Published baseline version", (latest or {}).get("version") or 0),
            ("Published baseline hash", (latest or {}).get("hash") or ""),
            ("Generated (UTC)", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
            ("How to use", "Edit only the translation column of the Translations sheet, then use Import in the dashboard. "
                           "Rows are matched by the Key column; do not change, add or delete keys. English is the reference and cannot be "
                           "changed here. Importing marks changed texts as Modified; nothing goes live until Publish Changes.")]
    for i, (a, b) in enumerate(rows, 1):
        _text(info.cell(row=i, column=1), a)
        info.cell(row=i, column=1).font = Font(bold=True)
        _text(info.cell(row=i, column=2), str(b))
    info.column_dimensions["A"].width = 30
    info.column_dimensions["B"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), f"offro-translations-{lang}-{track}-{datetime.utcnow().strftime('%Y%m%d')}.xlsx"


# ── import: reading the file ──────────────────────────────────────────────────────────────────────────────────────────
def _check_container(data: bytes) -> None:
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise TranslationError("empty_file", 422)
    if len(data) > MAX_BYTES:
        raise TranslationError("file_too_large", 413)
    if data[:4] != b"PK\x03\x04":
        raise TranslationError("not_xlsx", 422)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            names = {i.filename for i in infos}
            if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
                raise TranslationError("file_too_large", 413)
            if "xl/workbook.xml" not in names or "[Content_Types].xml" not in names:
                raise TranslationError("not_xlsx", 422)
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                raise TranslationError("macros_not_allowed", 422)
    except zipfile.BadZipFile:
        raise TranslationError("not_xlsx", 422)


def parse_draft(data: bytes) -> tuple:
    """(meta dict, rows). rows = [{row, key, english, value, problem}] for every non-blank row; problem is a code or ''."""
    _check_container(data)
    load_workbook = _xl()[0].load_workbook
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except Exception:
        raise TranslationError("invalid_file", 422)
    try:
        if SHEET not in wb.sheetnames or INFO not in wb.sheetnames:
            raise TranslationError("missing_sheet", 422)
        meta = {}
        for r in wb[INFO].iter_rows(min_row=1, max_row=40, max_col=2, values_only=True):
            if r and isinstance(r[0], str) and isinstance(r[1], (str, int, float)):
                meta[r[0].strip()] = str(r[1]).strip()
        if meta.get("File") != MARKER or meta.get("Schema") != SCHEMA_VERSION:
            raise TranslationError("not_a_draft_file", 422)
        rows = []
        for n, r in enumerate(wb[SHEET].iter_rows(min_row=2, max_col=4), 2):
            if len(rows) >= MAX_ROWS:
                raise TranslationError("too_many_rows", 422, {"max_rows": MAX_ROWS})
            cells = list(r) + [None] * (4 - len(r))
            vals = [getattr(c, "value", None) for c in cells[:4]]
            if all(v is None or (isinstance(v, str) and not v.strip()) for v in vals):
                continue                                                      # blank row
            problem, key = "", vals[0]
            if not isinstance(key, str) or not key.strip():
                problem = "missing_key"
            tcell, tv = cells[3], vals[3]
            if getattr(tcell, "data_type", "s") == "f":
                problem = problem or "formula_not_allowed"
            elif tv is not None and not isinstance(tv, str):
                problem = problem or "translation_not_text"
            rows.append({"row": n, "key": key.strip() if isinstance(key, str) else "", "english": vals[2] if isinstance(vals[2], str) else "",
                         "value": tv if isinstance(tv, str) else None, "problem": problem})
        return meta, rows
    finally:
        try:
            wb.close()
        except Exception:
            pass


# ── import: matching, validating, applying ────────────────────────────────────────────────────────────────────────────
def _norm_newlines(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def import_draft(lang: str, track: str, data: bytes, actor_id: str = "", apply: bool = False) -> dict:
    """Validates the workbook against [lang]+[track] and (when apply=True) stores the changed translations as drafts. Never publishes."""
    # ORDER MATTERS. 1) the server must know its own environment; 2) the file is read (no database) and its recorded environment must equal
    # it; only THEN is the language / text type looked up and any translation record touched. A file from another environment is refused
    # before a single translation lookup.
    T.require_env("edit" if apply else "any_known")
    meta, rows = parse_draft(data)
    server_env, file_env = T.environment(), str(meta.get("Environment", "")).strip()
    if file_env not in KNOWN_ENVIRONMENTS or file_env != server_env:      # missing, unknown or different: the whole file is refused
        raise TranslationError("environment_mismatch", 422, {"file": file_env[:20] or "missing", "server": server_env})
    language = T.get_language(lang)
    if language["status"] == "disabled":
        raise TranslationError("language_disabled", 409)
    T._check_track(track)
    if meta.get("Language") != lang:
        raise TranslationError("language_mismatch", 422, {"file": meta.get("Language", "")[:20], "selected": lang})
    if meta.get("Track") != track:
        raise TranslationError("track_mismatch", 422, {"file": meta.get("Track", "")[:10], "selected": track})

    eligible = _eligible_keys(track)
    registry_all = {k["key"]: k for k in T.db[T.KEYS].find({})}
    baseline = T.published_strings(track, lang)
    current = {v["key"]: v for v in T.db[T.VALUES].find({"lang": lang, "track": track})}
    seen = {}
    for r in rows:
        if r["key"]:
            seen[r["key"]] = seen.get(r["key"], 0) + 1

    updated, invalid, unchanged, empty, english_ignored = [], [], 0, 0, 0
    to_write = []
    for r in rows:
        key, problem = r["key"], r["problem"]
        if not problem and seen.get(key, 0) > 1:
            problem = "duplicate_key"
        if not problem and key not in eligible:
            kd = registry_all.get(key)
            problem = ("unknown_key" if not kd else "wrong_track" if T.track_of(kd) != track
                       else "plural_read_only" if kd.get("is_plural") else "key_deprecated")
        if problem:
            invalid.append({"row": r["row"], "key": key[:120], "code": problem})
            continue
        kd = eligible[key]
        if r["english"] and " ".join(r["english"].split()) != " ".join(str(kd.get("english") or "").split()):
            english_ignored += 1                                              # information only: English is never imported
        raw = r["value"]
        if raw is None or not raw.strip():
            empty += 1
            unchanged += 1                                                    # a blank cell never deletes or changes an existing value
            continue
        try:
            clean = T.validate_value(kd, _norm_newlines(raw))
        except TranslationError as e:
            invalid.append({"row": r["row"], "key": key[:120], "code": e.code})
            continue
        cur = current.get(key)
        if cur is not None and cur.get("value") == clean:
            unchanged += 1                                                    # nothing changed: the status stays exactly as it is
            continue
        updated.append(key)
        to_write.append((key, clean))

    if apply:
        for key, clean in to_write:
            T._store_value(lang, key, track, clean, T.effective_status(baseline, key, clean), actor_id, "import")

    in_file = set(seen)
    missing = sorted(k for k in eligible if k not in in_file)
    total = len(rows)
    latest = T.latest_version(track, lang)
    return {
        "lang": lang, "track": track, "applied": bool(apply), "published": False,
        "total": total, "valid": total - len(invalid), "updated": len(updated), "unchanged": unchanged, "invalid": len(invalid),
        "missing": len(missing),
        "empty_cells": empty, "english_edits_ignored": english_ignored,
        "baseline_changed": str(meta.get("Published baseline hash", "")) != str((latest or {}).get("hash") or ""),
        "invalid_rows": invalid[:REPORT_LIMIT], "updated_keys": sorted(updated)[:REPORT_LIMIT], "missing_keys": missing[:REPORT_LIMIT],
    }
