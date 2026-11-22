# One-time translation transfer (staging -> production)

`tools/transfer_translations.py` copies the finished translations from the staging database to the (empty) production database
**once**. It is a manual tool: the server never imports or runs it, nothing schedules it, and there is no ongoing connection
between the environments. It was written and tested against mock databases only; it has **not** been run against any real
database.

## Two steps, so no run is connected to both databases
1. `export` reads the STAGING database (read-only) and writes a package file (`transfer.json`).
2. `import` reads the package file and compares it with the PRODUCTION database. **Dry run unless `--apply` is given.**

## Credentials
None are in the script, the package, the logs or the ZIP. You pass the NAME of an environment variable (defaults
`TRANSLATION_SOURCE_MONGO_URL` / `TRANSLATION_TARGET_MONGO_URL`); the connection string is read from it and never printed. Connection
errors report only the error type. Set the variable in your own shell for the duration of the command only. The application uses
the database name `offro_db`; pass the real name with `--source-db` / `--target-db`.

## What is copied
Languages (registry), keys (registry), the **latest published version** of every language + track (`app`, `legal`; narrow with
`--tracks app`), and the matching working values (marked `published`, so a later edit in production is a normal Modified edit and
the next Publish contains every string).
**Not copied:** the kill switch (production keeps its own, default OFF), the audit trail, drafts / Modified values, older versions.
The `legal` track is copied but the app does not request it.

## Checks (all before the first write)
* package format and its own SHA-256 (detects a damaged or edited file);
* language registry (code, direction, status, names), key registry (names, groups);
* every version number >= 1 and **its SHA-256 recomputed from its strings** (must equal the stored hash);
* every text validated against the key registry (placeholders, length, control/hidden characters, plural/deprecated keys, track);
* export refuses data labelled for another environment; import refuses when source = target environment, when the target is the
  same server + database as the source (one-way fingerprint, no credentials), or when the target already holds data labelled for a
  different environment;
* **no overwrites, ever.** Only missing documents are inserted. Anything already in the target is identical (skipped) or a
  CONFLICT; one conflict stops everything before any write. Re-running after a success reports everything as identical;
* `--apply` also requires `--confirm-target-db <exact database name>`;
* versions are inserted LAST, so the app is never served a half-copied language; a started/ok/failed audit entry
  (`transfer_import`, counts + package hash only, no text) is written to the target.

## Commands (PowerShell; do NOT run the import until the go-live checklist says so)
```
# 1. Export from STAGING (read-only)
$env:TRANSLATION_SOURCE_MONGO_URL = "<staging connection string>"
python tools/transfer_translations.py export --source-db offro_db --source-environment staging --out transfer.json
Remove-Item Env:TRANSLATION_SOURCE_MONGO_URL

# 2. DRY RUN against PRODUCTION (writes nothing)
$env:TRANSLATION_TARGET_MONGO_URL = "<production connection string>"
python tools/transfer_translations.py import transfer.json --target-db offro_db --target-environment production

# 3. Only after review and approval: write
python tools/transfer_translations.py import transfer.json --target-db offro_db --target-environment production --apply --confirm-target-db offro_db
Remove-Item Env:TRANSLATION_TARGET_MONGO_URL
```
Exit codes: `0` ok / dry run, `2` refused or failed (message printed), `3` conflicts. Use `--log-file <path>` to keep a log (no text, no
credentials). The package contains translation text: delete it after the transfer.

## Tests
`python -m pytest test_translations_transfer.py -q -W ignore` (mock databases; includes export read-only, tampered packages,
hash mismatch, conflicts, idempotent re-run, failure mid-write, CLI dry-run/confirmation, no credential leakage, static checks
that the script has no delete/overwrite operations, scheduler, or import of the application's database, and an end-to-end test
that a production-identified server serves the same version and hash as staging after the import).
