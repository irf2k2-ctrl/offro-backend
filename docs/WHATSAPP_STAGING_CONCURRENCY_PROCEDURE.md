# Real-MongoDB concurrency verification – step by step (Windows CMD)

Purpose: prove on a **real MongoDB server** that the Phase 2E/2F claims, leases, recovery and rate limiter are atomic across several
processes. `tools/wa_staging_concurrency_check.py` works only inside a throw-away database `wa_conc_check_<12 hex>`. It never sends
WhatsApp, never calls Meta, never enables the worker or any `WA_CAMPAIGN_*` variable and never reads or writes `offro_db`.

## 0. Safety design (read once)
* **Two paths, chosen explicitly with `--target`:**
  | Path | Server | `offro_db` | Flag pair |
  |---|---|---|---|
  | **A. Staging** | the Railway **Staging** MongoDB | **must be present** | `--target staging --yes-this-is-staging` |
  | **B. Isolated** | a fresh, empty, dedicated MongoDB (local Docker or a separate throw-away server) | **must be absent**; the server holds nothing but `admin`/`config`/`local` | `--target isolated --yes-this-is-isolated` |
  A server that fits neither (unreadable database list, or other databases but no `offro_db`) is refused. Path A refuses a server without
  `offro_db`; path B refuses a server that has it. Nothing is written when a path is refused.
* **The fingerprint is a typo guard only.** It is a hash of the `host:port` in your connection string. It does **not** prove that a server is
  Staging. *You* prove that by comparing the **host AND port** with Railway (section 3).
* **Ownership-based cleanup (local record + document-level proof).** MongoDB alone cannot prove who created a database or its contents, so each run
  first creates a **local ownership record**: a random 256-bit token (`secrets.token_hex`) in `%USERPROFILE%\.offro_conc_check\<run id>.json` (no
  MongoDB URL, host or credentials in it). Only then does it write to MongoDB. **Every document the tool writes** carries `_conc_run` (run id),
  `_conc_role` and `_conc_proof`. A database is dropped (end of run, or `--cleanup-leftovers`) only if ALL hold: name `wa_conc_check_` + 12 hex; a
  valid, private local record; a matching MongoDB marker; only this tool's four collections; and **every document in every collection verifies**.
  One foreign, unproven, malformed, tampered or look-alike document makes the tool **refuse the whole database drop** (`REFUSED … [foreign_documents]`),
  keep the database and keep the local record. The tool never force-drops and never deletes the local record unless the drop is verified.
* **Exactly what the proof covers.** `_conc_proof` = HMAC-SHA256, keyed with the secret local token, over a canonical encoding of: proof version,
  run id, collection, the `_id` (with its type), and the document's **immutable fields** (a field that was absent at stamping must stay absent):
  | Collection | Immutable (sealed) fields | Mutable (worker may change; NOT sealed) |
  |---|---|---|
  | `wa_campaigns` | `_id`, `_conc_run`, `_conc_role` | `status`, `run.*` (lease / backoff), version, counters, timestamps … |
  | `wa_campaign_recipients` | `_id`, `_conc_run`, `_conc_role`, `campaign_id`, `phone`, `idx` | `state`, `attempts`, `lease_owner/lease_until`, `send_started`, `next_attempt_at`, `message_id`, `delivery_*`, `error*`, `updated_at` … |
  | `wa_campaign_rate` | `_id`, `_conc_run`, `_conc_role`, `at` | `count` |
  | `_wa_conc_owner` (marker) | `_id`, `_conc_run`, `_conc_role`, `tool`, `run_id`, `db_name`, `token_hash`, `created_at` | none |
  Changing, adding or removing a sealed field, moving a proof to another `_id`, another collection or another run, a wrong or malformed proof, and a
  missing `_conc_run` / `_conc_role` / `_conc_proof` all fail verification. Legitimate worker updates touch only the mutable fields, so proofs stay valid.
* **Limitations (be aware).** (1) Mutable fields are deliberately not protected: someone with write access to the throw-away database could change
  `state`, `count`, `status` etc. without detection; that cannot enable a wrong drop (they would still need a valid proof for every document), but
  it means a *passing* ownership check says "every document is one this run created", not "no value was altered". (2) The proof stops forgery and
  tampering by anyone without the local token; it does **not** stop someone who already holds your token (i.e. read access to your Windows profile
  folder) or an administrator of your PC. (3) Documents deleted by someone else are not detected (nothing foreign remains to protect). (4) The tool
  must stay in sync with the worker: if a future worker version starts writing a sealed field, runs will be refused (fail closed), never wrongly dropped.
  (5) **Rate counters and the host time zone.** The worker names its per-second counters from a naive UTC time via `.timestamp()`, which Python reads as
  local time, so on a PC whose time zone is not UTC (e.g. India, UTC+05:30) the ids are shifted. The tool pre-creates and stamps the counters for both
  forms (true epoch seconds and the host-local form). A counter with any other id would be unproven and the drop refused, never forced.
* **Local record permissions.** POSIX: file 0600 in a 0700 folder (checked on every read). **Windows: only the default
  `%USERPROFILE%\.offro_conc_check` is allowed; setting `CONC_OWNER_DIR` on Windows aborts the run.** The tool removes inherited permissions
  (`icacls /inheritance:r /grant:r *<your SID>:…`) on the folder and on each record and then re-reads the ACL: it must list **only your own account**
  and no inherited entry (no Users / Everyone / Administrators / SYSTEM). If this cannot be established, the run aborts before any MongoDB write
  and cleanup refuses records whose ACL has become broader. (Run as your normal Windows user; an administrator-only ACL is not accepted.)
* **Fail-closed tripwire.** The application's `database` module is replaced by a tripwire. If the real module (or a service holding a real
  application database handle) is already loaded, the tool prints `ABORT` and exits with code 2 before connecting. Always start it as a fresh
  `python tools\...` process, never from a shell that imported the application.
* The connection string lives only in `CONC_MONGO_URL`, typed into a hidden prompt. Errors print the class name only.
* Nothing here changes Railway settings or touches Production. You only *read* values in the Railway dashboard.

## 1. Check the working copy (read-only)
```
cd /d "D:\irfan work\Offro\backend-staging"
git status -sb
git log -1 --oneline
python -m pytest test_wa_staging_concurrency_script.py test_wa_worker_safety_followup.py -q -W ignore
```
Expected: branch `staging`, all tests passing (fake Mongo: proves the script's logic and interlocks, not the server).

# PATH A – Railway Staging MongoDB

## 2. Staging and Production must be different servers
Railway dashboard → open **Production** and **Staging** (environment selector) → each MongoDB service → *Settings → Networking → Public Networking*
(a `….proxy.rlwy.net:<port>` address; just look). Write down both **hosts and ports**.
* Different host or different port → continue.
* **Identical** host and port → STOP: a throw-away database would be created on the Production server. Use Path B.

## 3. Get the Staging URL and enter it hidden
Dashboard → **Staging** environment (check the label) → **MongoDB** service → **Variables** → copy **`MONGO_PUBLIC_URL`** (not `MONGO_URL`, which
ends in `.railway.internal`). Then:
```
cd /d "D:\irfan work\Offro\backend-staging"
powershell -NoProfile
```
In the PowerShell window:
```
$s = Read-Host "Mongo URL (input is hidden)" -AsSecureString
$p = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
$env:CONC_MONGO_URL = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p); Remove-Variable s, p
```

## 4. Verify the target – independently (read-only, writes nothing)
```
python tools\wa_staging_concurrency_check.py --show-target --reveal-host
```
`--reveal-host` prints the full `host:port` so you can compare; do not screenshot or share this output. Expected shape:
```
  host:port (FULL)     : roundhouse.proxy.rlwy.net:12345     <- compare with Railway; do not screenshot or share
  TARGET FINGERPRINT   : 73a90f94c3     (a typo guard only - it does NOT prove this is Staging)
  MongoDB version      : 7.0.x
  databases            : admin, config, local, offro_db
  application DB 'offro_db' : present
  throw-away DBs       : 0 owned by this tool, 0 prefixed but NOT owned (never touched)
  server role          : holds the application DB  -> use  --target staging
No writes were made.
```
Tick ALL of these yourself. If any fails, stop and close the window:
1. **Host AND port** printed here equal the **Staging** MongoDB public address/port in Railway (section 2).
2. They are **different** from the Production MongoDB host/port.
3. `offro_db` is present and `server role` says `--target staging` (consistent with it being the app's Mongo, but not a proof by itself).
4. Note the fingerprint. It only protects you from pasting a different URL later.

## 5. Run (same PowerShell window)
```
python tools\wa_staging_concurrency_check.py --target staging --yes-this-is-staging --expect-target <FINGERPRINT> --processes 8 --recipients 400
python tools\wa_staging_concurrency_check.py --target staging --yes-this-is-staging --expect-target <FINGERPRINT> --processes 8 --recipients 2000
python tools\wa_staging_concurrency_check.py --target staging --yes-this-is-staging --expect-target <FINGERPRINT> --processes 16 --recipients 2000
```
Do the 8-process/2000 run three times in total. Each takes about 1–4 minutes.

# PATH B – fresh isolated MongoDB (no application database needed)
Use this when Staging shares a Mongo with Production, or you simply want no contact with application data. Start a throw-away server that holds
nothing else, e.g. `docker run --rm -p 27018:27017 mongo:7` (CMD), or a new separate MongoDB service in the Railway **Staging** environment.
Enter `mongodb://localhost:27018` (or that service's public URL) in the hidden prompt (section 3 commands), then:
```
python tools\wa_staging_concurrency_check.py --show-target --reveal-host
```
Verify: host and port are the throw-away server **you started**; `offro_db` is **NOT present**; databases are only `admin`/`config`/`local`;
`server role` says `--target isolated`. Run:
```
python tools\wa_staging_concurrency_check.py --target isolated --yes-this-is-isolated --expect-target <FINGERPRINT> --processes 8 --recipients 400
python tools\wa_staging_concurrency_check.py --target isolated --yes-this-is-isolated --expect-target <FINGERPRINT> --processes 8 --recipients 2000
```
This proves MongoDB's atomicity with the same code; it does not prove anything about Railway Staging itself.

## 6. Expected successful output of one run (both paths)
```
1. recipients: each claimed by exactly one process               PASS (2000 claims / 2000 recipients / 2000 in DB with attempts=1 / 8 processes)
2. campaign: one process wins the claim                          PASS (winners=1)
3. lease renewal cannot overwrite another worker's lease         PASS (8 foreign renewals refused and the lease untouched; the owner's own renewal worked)
4. expired lease: not revived by its old owner, taken by exactly one PASS (winners=1)
5. delivery status: concurrent / out-of-order events end at read PASS
6. rate limit: 5/s cluster-wide over a 3 s window                PASS (sent=15, expected 10..20)
7a. recovery: send_started=true -> uncertain, false -> queued    PASS {'requeued': 5, 'uncertain': 5}
7b. uncertain recipients are never claimed (never resent)        PASS (re-claimed 5 of the 5 queued; 5 uncertain untouched)
cleanup: throw-away database dropped and verified: yes
RESULT: ALL PASS (8/8 checks)
```
Exit code 0 (`$LASTEXITCODE`). Mapping: several processes claim one campaign → checks 2, 4; one recipient → 1; lease renewal cannot overwrite → 3;
expired leases → 4, 7a; `send_started=false` back to queue → 7a, 7b; `send_started=true` uncertain, never resent → 7a, 7b; cluster rate limit → 6; extra: webhook ordering → 5.

## 7. Failure interpretation and safe recovery
Whatever fails: the worker stays OFF, nothing is enabled, application data was never touched. Run section 8, then send me the output (no secrets in it).
| Output | Meaning | What to do |
|---|---|---|
| `ABORT: … tripwire` / `real application 'database' module` | the application was already loaded in this process | open a fresh window; run only the tool |
| `ABORT: --expect-target does not match` | a different URL than the one you verified | redo sections 3–4 |
| `ABORT: --target … refused: …` | server does not match the chosen path (see section 0) | choose the right path or the right server; nothing written |
| `FAILED … ServerSelectionTimeoutError` | cannot reach Mongo | check the public URL / proxy / network |
| `FAILED … OperationFailure` | user may not create or list databases | use the Mongo service admin user of that environment |
| check 1 FAIL | a recipient could be claimed twice → duplicate sends | STOP; do not enable the worker; send output |
| check 2/3/4 FAIL | two workers could hold one campaign / a lease could be overwritten | STOP; send output |
| check 6 FAIL below the range | slow network or clock | re-run once; if repeated send output |
| check 6 FAIL above the range | limiter lets too many through | STOP |
| check 7a/7b FAIL | recovery could resend or lose a possibly-sent message | STOP |
| `child N exited … without a result` / `timed out` | a process crashed or hung | re-run with `--processes 4`; run cleanup |
| `cleanup: NOT dropped (reason)` | ownership could not be proven (e.g. `no_local_record`) or the drop was not verified | keep any local record; inspect the database yourself; the tool will not force-drop it |
| `ABORT: cannot create the local ownership record` | the folder is not writable | fix the folder; nothing was written to MongoDB |
| `ABORT: … private (current-user-only) Windows ACL …` / `CONC_OWNER_DIR is not allowed on Windows` | Windows could not be made to keep the record private, or a custom folder was set | unset `CONC_OWNER_DIR`, run as your normal user from your own profile; nothing was written to MongoDB |
| `REFUSED … [foreign_documents]` | some document in the throw-away database is not proven to be this run's, or a sealed field was altered |
| lines under it such as `wa_campaign_rate: 2 document(s): missing_ownership_metadata` | **diagnostics**: collection, count and reason category only (`missing_ownership_metadata`, `missing_or_wrong_run_tag`, `missing_or_wrong_role`, `missing_or_malformed_proof`, `proof_mismatch`, `unverifiable`, `unexpected_collection`); never ids, phone numbers or values | send me these lines; keep the database and record | the database and local record are kept; inspect the database yourself; do not delete the record; never drop it on the tool's behalf |

## 8. Cleanup (only databases proven to be owned by this tool)
Each run drops its own database after validating BOTH the local record and the MongoDB marker; once the drop is verified, the tool deletes that run's
local record. After a clean run the folder `%USERPROFILE%\.offro_conc_check` should hold no `*.json` files.

**Interrupted run (Ctrl+C, crash, sleep): keep the local record.** It is the only proof that you own the leftover database. Do not delete the
`.offro_conc_check` folder or change its permissions, and run cleanup **on the same Windows user account and PC** that started the run:
```
dir "%USERPROFILE%\.offro_conc_check"
python tools\wa_staging_concurrency_check.py --cleanup-leftovers --target staging --yes-this-is-staging --expect-target <FINGERPRINT>
python tools\wa_staging_concurrency_check.py --show-target
```
(use `--target isolated --yes-this-is-isolated` on Path B.) Expected: `Dropped N throw-away database(s) owned by this tool.` and the matching `.json`
files gone. Any `REFUSED (left untouched, not proven to be owned by this tool): <name> [reason]` line is a database this tool will never delete (no
local record, lost record, forged marker, foreign data); the command exits 1. Look at it yourself in the Railway/Mongo tools and drop it manually only
if you are certain you created it.

**Removing records afterwards.** A record is removed automatically after its database is verified gone. A *stale* record (database already
dropped by hand, or a run that never created the database) is harmless; after `--show-target` shows 0 owned throw-away databases you may delete the
leftover files: `del "%USERPROFILE%\.offro_conc_check\<run id>.json"` (or the whole folder, only when nothing is left to clean up). Never copy these
files into chat, Git or screenshots.

Finish:
```
Remove-Item Env:CONC_MONGO_URL
exit
```
Clear the clipboard and do not save the URL anywhere.

## 9. Final pass/fail checklist
- [ ] Path chosen: A (Staging) or B (isolated), and the matching `--target`/`--yes-this-is-…` flags used.
- [ ] Path A: Staging and Production Mongo **host and port** compared in Railway and they differ (section 2).
- [ ] `--show-target --reveal-host`: host AND port match the intended server (Staging service for A; the throw-away server for B).
- [ ] Path A: `offro_db` present; Path B: `offro_db` absent and only system databases.
- [ ] 8 processes / 400 recipients: `ALL PASS (8/8 checks)`.
- [ ] 8 processes / 2000 recipients: `ALL PASS`, three times; 16 processes / 2000: `ALL PASS` (Path A).
- [ ] Every run printed `cleanup: … verified: yes`; `--show-target` afterwards: 0 owned, 0 refused; `%USERPROFILE%\.offro_conc_check` has no leftover `*.json` (after interrupted runs: records kept until cleanup succeeded).
- [ ] No secret in terminal output or screenshots; `CONC_MONGO_URL` removed; window closed.
- [ ] Worker, sending and live variables still OFF on Staging and Production (nothing here changes them).
Enabling the worker (Phase 2E/2F doc, section 9 step 4) still needs your separate explicit approval.
