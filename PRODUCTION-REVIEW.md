# PRODUCTION-REVIEW: Production integration of WhatsApp 2A-2G + admin security

**Baseline: `335c971` (`origin/main`).** Tested tree = that baseline + the delivered patch. Exact test command: `python -m pytest -q -W ignore -p no:cacheprovider`; expected result: **1279 passed** (baseline alone: 360 passed). Companion files: `APPLY-PRODUCTION.md` (Windows CMD steps, checks, rollback), `FILES-INCLUDED-EXCLUDED.md`.

Baseline `origin/main` = 335c971. Result tested: 1279 tests pass (baseline 360). Nothing was committed, pushed, merged or deployed; no Railway or database access.

## 1. What I found (and corrected)
- The clone I first inspected tracked only `staging`, so its `origin/main` was stale (e34c12f). The real `origin/main` is **335c971** (PR #7, WhatsApp Phase 1). Everything below is against 335c971.
- main is a direct ancestor of staging (16 commits ahead, 0 behind). The whole staging-minus-main difference applies cleanly to main.
- The five earlier "conflicts" were an artifact of cherry-picking 2A-2G one commit at a time onto a main whose Phase 1 text differs from the Phase 1 text those commits were built on. In the final trees there is nothing left to conflict. I did **not** use cherry-pick; the patch is a tree difference.

## 2. The five conflicts, one by one
| File | State main vs staging | Resolution |
|---|---|---|
| `routers/webhook.py` | byte-identical | **Not in the patch.** Production's webhook (signature check, consent, de-dupe, retry) is exactly as it is. Delivery tracking (2F) plugs into the existing `routers.webhook._STATUS_HANDLERS` list at startup via `wa_delivery.register()`. |
| `docs/WHATSAPP_WEBHOOK_STAGING_SETUP.md` | byte-identical | **Not in the patch.** |
| `server.py` | staging only adds, plus Patch B removals | Taken from staging; reviewed hunk by hunk (section 3). Production's CORS allow-list, `SecurityMiddleware`, product/collaboration routers, gift vouchers, health, startup blocks are untouched context. |
| `services/wa_consent.py` | additive (+100 lines) | Taken from staging: only `import hashlib` and the new `record_admin_consent` + `AdminConsentError` (2A, evidence-based admin consent that can never override an opt-out). Production's Phase 1 consent logic is not modified. |
| `test_wa_webhook_consent.py` | one test assertion widened | Taken from staging: the old test said "no campaign routes exist"; it now says campaign routes may exist only under `/admin/whatsapp-campaigns/` and none may be bulk/launch/send. The other 306 Phase 1 tests are unchanged and pass. |

## 3. `server.py`: exact changes against main (everything else is unchanged)
1. Import + `app.include_router(wa_campaigns.router, prefix="/admin")` (permission-gated, nothing sends by itself).
2. Three guarded startup blocks (audit indexes, campaign indexes, worker + delivery). The worker thread starts only when `WA_CAMPAIGN_WORKER_ENABLED` is on **and** the environment is not Production (or an explicit override exists). Each block is wrapped in try/except so a failure cannot stop the app.
3. `/download/admin_dashboard` (Production-specific download authorization): **the authorization rules are unchanged** (session required, disabled/suspended refused). Two edits: (a) the legacy `db.admins.token` fallback is removed (Patch B, approved: those sessions bypassed PIN+OTP); (b) it now serves the *rendered* template instead of the raw file, because `admin_dashboard.html` now has a `{% include %}` of a partial, and the raw file would download with an unresolved tag. Same attachment name and headers. This is the only touch of that logic; test `test_download_requires_admin_session` passes and also proves a legacy token now gets 401.
4. `/admin/dashboard` page route: legacy `admins.token` fallback removed; template-error output is HTML-escaped.
No change to CORS, `SecurityMiddleware`, product, collaboration or maps wiring.

## 4. Production-specific code NOT weakened
CORS allow-list and credentials rule, security headers/CSP, Secure cookies over HTTPS, staging-host lockdown, download authorization, product and collaboration wiring: none modified (their tests, including `test_security_hardening.py`, pass; the one change there swaps the old password login for the real PIN+OTP login in its fixture). Webhook signature verification: untouched file.

## 5. Staging-only code and why the fixed OTP 1234 cannot activate in Production (Option A: kept, inert)
`services/admin_otp_test_mode.py` ships, but test mode is on only if **all** hold, evaluated on every call; any miss, mismatch or error means INACTIVE (real MSG91 SMS, random code):
1. `ADMIN_OTP_STAGING_TEST_MODE` truthy; 2. Railway project, environment and service IDs equal the three Staging IDs (Railway injects them; Production has different ones); 3. public domain is exactly the Staging host (never `theoffro.com`, `www/admin.theoffro.com` or the Production Railway host); 4. every MongoDB host in `MONGO_URL` equals the Staging internal host; 5. the admin's number is in `ADMIN_OTP_TEST_MOBILES`.
Extra protection in the verify path: a challenge created in test mode is refused if test mode is not active when it is used (a Staging backup restored into Production cannot be used with 1234).
New release gate `test_security_admin_otp_test_mode_production.py` (69 tests) proves, among others: every combination of copying 0 to 4 of the five Staging identity values into Production (31 combinations) stays inactive even with the switch and allow-list set; each single value removed/blank/changed turns it off; Production and `theoffro.com` domains (including look-alikes) refused; Production, Atlas and mixed database hosts refused; non-truthy switch values refused; running off-Railway refused; any exception while checking fails closed; Production environment labels (`production` or `staging`) neither enable nor disable it; a Production login sends one real SMS carrying the generated code, rejects 1234 and issues the session only for the real code; with SMS down it returns 503 and never falls back to 1234; a test-mode challenge copied into Production is unusable; the startup line says INACTIVE and leaks no IDs, hosts, numbers or code; the fixed code appears only in `admin_login_otp.py` and `admin_otp_test_mode.py`; the login page and API never mention it.
Residual risk (accepted with Option A): someone setting *all five* Staging identity values on a Production service would be needed; Railway-injected IDs cannot be forged from variables the way a flag can, but an operator who deploys this very code to the Staging service is, by definition, Staging.

## 6. Campaigns in Production: dormant until you approve
Sending needs ALL of: `WA_CAMPAIGN_SENDING_ENABLED` (env), the database kill switch ON, an environment that is not Production/unknown, worker enabled, and (in Production) the explicit `WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION` override; test sends additionally need `WA_CAMPAIGN_TEST_NUMBERS`. None is set by the patch. The only outbound Meta call without those gates is a read-only template list. Consent (STOP) is checked before each message and fails closed.

## 6b. Environment variables and restrictions (summary; full lists and CMD steps are in APPLY-PRODUCTION.md section 7)
- **Production required:** `MSG91_AUTH_KEY`, `MSG91_SENDER_ID`, `MSG91_ADMIN_TEMPLATE_ID` (or `MSG91_TEMPLATE_ID`), `MONGO_URL`, and the existing Phase 1 `WHATSAPP_*` variables. **Recommended:** `WA_CAMPAIGN_ENVIRONMENT=production`. **Optional:** `ADMIN_TRUSTED_DEVICE_DAYS`, `ADMIN_TRUSTED_DEVICE_SUPER_ADMIN`.
- **Production must NOT have:** `ADMIN_OTP_STAGING_TEST_MODE`, `ADMIN_OTP_TEST_MOBILES`, `WA_CAMPAIGN_SENDING_ENABLED`, `WA_CAMPAIGN_WORKER_ENABLED`, `WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION`, `WA_CAMPAIGN_TEST_NUMBERS`.
- **Staging (unchanged):** fixed OTP needs the test-mode switch, the three Staging Railway IDs, the Staging domain, the Staging Mongo host and the allow-list, all together; campaigns need `WA_CAMPAIGN_ENVIRONMENT=staging` plus the sending/worker/test-number keys only when you test them.

## 7. Behaviour changes admins will notice
- Sign-in is PIN then SMS code, always (the same shape as today, now server-verified with hashed, single-use, expiring codes). PIN hashing is unchanged, so existing PINs keep working.
- Existing sessions stay valid (same token, same 30-minute inactivity limit). Old "trusted device" cookies (`offro_trusted`) stop counting, so each admin gets one SMS prompt. New "Remember this device" is opt-in; Super Admin excluded unless `ADMIN_TRUSTED_DEVICE_SUPER_ADMIN=1`.
- Legacy username/password `/admin/login` returns 410; default `admin/admin123` account is no longer created; `admins.token` sessions are not accepted anywhere.
- Lockouts: only failures count; progressive delays with a clear message; completed sign-ins never lock anyone out.
- Startup creates indexes on new collections only.

## 7b. Phase 2H exclusion and what was kept
Excluded as 2H: the Staging concurrency verification tool (`tools/wa_staging_concurrency_check.py`, first added in the 2E/F commit and extended by commit f296871), its test file, and its Windows procedure document. The worker itself (`services/wa_campaign_worker.py`) is not changed by 2H, so every 2E/F safety correction ships: template re-verification before each message, worker key re-read in the final gate, atomic lease renewal, `send_started` plus final lease check, Production guard.

## 8. Risks and things I could not verify
1. **No active Super Admin in Production = lockout.** Highest-impact risk; pre-deployment checklist A in APPLY-PRODUCTION.md.
2. **MSG91 delivery in Production** is outside my reach; checklist B. If SMS fails, nobody can sign in (by design: no fallback code).
3. I have no access to Railway or Production data, so variable *values*, existing data shapes (e.g. whether `role_id` is stored as ObjectId) and live Meta behaviour are unverified. Tests use an in-memory database; mongomock does not prove MongoDB atomicity (that was Phase 2H's separate Staging verification, which you excluded from this release; the atomic operations themselves are in the shipped worker).
4. `routers/temp admin.py` and `seed.py` still contain old default-admin code on both branches. Neither is imported or run at startup (checked), so they are inert; I left them as they are.
5. Original "Server error. Please try again." from earlier Staging work: not claimed fixed without live evidence.
6. The CRLF patch assumes a CRLF checkout of main; if your files are LF use the LF patch. `git apply --check` tells you immediately.
