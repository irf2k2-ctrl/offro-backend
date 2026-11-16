# APPLY-PRODUCTION: WhatsApp Phase 2A-2G + admin security, Production integration

**Baseline commit (exact): `335c971`** = `origin/main` ("Merge pull request #7 from irf2k2-ctrl/release/whatsapp-phase1").
**Content source:** `origin/staging` b79d7f9 minus Phase 2H, plus one new Production test file.
**Result tested:** **1279 passed** (the same tree without this patch: 360 passed).
This guide never commits, pushes, merges or deploys. All commands are Windows CMD (not PowerShell).

## Patch files and integrity
| File | Use when | SHA256 |
|---|---|---|
| `phase-prod-integration-whatsapp-2A-2G-and-admin-security-CRLF.patch` | your checkout has CRLF line endings (normal on Windows) | `3ebc545dbac383fda9a081697db4341ad31a63a0c94e6ffbd694c95cb13ffe75` |
| `phase-prod-integration-whatsapp-2A-2G-and-admin-security.patch` | your checkout has LF line endings | `104a11f5ae84146774fc7b3643f7eb5a39cb75962cdad7ee4533a36a416cc335` |

Both contain the same change: 70 files (17 modified, 53 new). Check your copy first:

    certutil -hashfile C:\path\to\phase-prod-integration-whatsapp-2A-2G-and-admin-security-CRLF.patch SHA256

The hash must match the table (ignore spaces). If not, the download is damaged: stop and re-download.

## 0. Rules
Never use `--reject`, `--3way`, `--ignore-whitespace` or `-f`/`--force` with `git apply`. If any step fails, stop and send me the exact message.

## 1. Clean worktree on the exact baseline
Use a separate worktree so your main folder and its uncommitted product edits stay untouched.

    cd /d "D:\irfan work\Offro\backend-staging"
    git fetch origin +refs/heads/main:refs/remotes/origin/main
    git rev-parse --short origin/main
    git worktree add ..\offro-production-release-2 -b production-release-2 origin/main
    cd /d "D:\irfan work\Offro\offro-production-release-2"
    git status
    git rev-parse --short HEAD

Required: both `rev-parse` lines print **335c971**, and `git status` says the working tree is clean. If `origin/main` prints anything else, STOP (a clone that tracks only `staging` shows an old `origin/main`; the fetch line above fixes that). Adjust the folder names to yours; keep the `..\` layout.

## 2. Check file hashes before applying (CRLF checkout, read-only)
For any file below run, for example: `certutil -hashfile routers\dashboard_auth.py SHA256` (first 16 hex digits). Expected before and after applying:

| File | Before | After |
|---|---|---|
| `README.md` | `60026527d340d7f7` | `00a1b761bb3b93f1` |
| `conftest.py` | `3c693ed7a1e7d7f5` | `024a4d2d3e7f8bd0` |
| `routers\admin.py` | `c32c8dfb9a7fc4fa` | `ff1054acbcf96a2a` |
| `routers\dashboard_auth.py` | `f657a9948a898222` | `3907f667c7395a83` |
| `routers\otp_service.py` | `c40a6409032ba944` | `4e6b870325b72339` |
| `routers\popup_campaigns.py` | `3c152d6e2f4324b2` | `8ed5e8c3ff9da76d` |
| `routers\wa_chat.py` | `d0c59abf30f4413d` | `b977c45f37462aff` |
| `routers\wa_template_test.py` | `d57f3536867f9e33` | `807fa64b709ab7d3` |
| `server.py` | `d2629f7c1ae7c5c7` | `a22c1c7aab5e4fbb` |
| `services\wa_consent.py` | `9a25e732bc0f6ab5` | `4b0cbc03f43f0a7c` |
| `services\whatsapp.py` | `38eb33785bb592c8` | `7bb2cdba3e0dafe2` |
| `templates\admin_dashboard.html` | `1d6efa9fd206a705` | `b588096f23cd8fa0` |
| `templates\admin_login.html` | `2e957f74c50462cd` | `76cfd865a3d03008` |
| `test_products_api.py` | `a5fca0a2609b4f70` | `1be67c3559dbe20f` |
| `test_security_hardening.py` | `6fc398cbdf8d09b9` | `d127abe3756a1408` |
| `test_wa_template_test.py` | `eeb8fc18f39d96d7` | `071f0b12b87649e2` |
| `test_wa_webhook_consent.py` | `3f53e0ebe873e206` | `d5da28573c14c811` |

If a "Before" hash differs, your checkout is not a pristine CRLF 335c971: stop. For an LF checkout these hashes differ; `git apply --check` is the test there.

## 3. Apply (no flags)
    git apply --check "C:\path\to\phase-prod-integration-whatsapp-2A-2G-and-admin-security-CRLF.patch"
    git apply "C:\path\to\phase-prod-integration-whatsapp-2A-2G-and-admin-security-CRLF.patch"
    git status --short

Expected: `--check` prints nothing; `git status --short` shows 17 files with `M` and 53 with `??`. Nothing else. Do not commit until section 5 passes.

## 4. Environment for the tests (once)
    python -m pip install -r requirements.txt pytest mongomock

Tests use an in-memory fake database. They never contact MongoDB, Meta, MSG91 or Railway. Do not set the Production variables in your shell while testing (if you exported `MONGO_URL` or `WA_CAMPAIGN_*` variables in this CMD window, open a new window).

## 5. Run the tests: exact command and expected result
    python -m pytest -q -W ignore -p no:cacheprovider

**Expected last line: `1279 passed`** (about 70 s on Linux; allow longer on Windows). Any failure or any other number: stop and send me the output.

If you want to run it in parts (same total):
| Group | Files | Expected |
|---|---|---|
| Fixed OTP impossible in Production (new) | `test_security_admin_otp_test_mode_production.py` | 69 passed |
| Staging test mode behaviour | `test_security_admin_otp_test_mode.py` | 50 passed |
| Mandatory PIN+OTP | `test_security_admin_login_2fa.py` | 42 passed |
| Legacy login retired | `test_security_admin_legacy_auth_retired.py` | 46 passed |
| Trusted device | `test_security_admin_trusted_device.py` | 28 passed |
| Login limits | `test_security_admin_login_limits.py` | 22 passed |
| Hardening (CORS, headers, download) | `test_security_hardening.py` | 17 passed |
| Phase 1 webhook and consent | `test_wa_webhook_consent.py` | 307 passed |
| Template test | `test_wa_template_test.py` | 25 passed |
| Campaigns 2A-2G | `test_wa_campaign_audience.py` 64, `test_wa_campaign_lifecycle.py` 111, `test_wa_campaign_templates.py` 99, `test_wa_campaign_test_send_safety.py` 36, `test_wa_campaign_worker.py` 52, `test_wa_campaigns_foundation.py` 122, `test_wa_dashboard_ui.py` 26, `test_wa_delivery_tracking.py` 34, `test_wa_environment_guard.py` 42, `test_wa_phase2g_endpoints.py` 50 | 636 passed |
| Worker safety (2E/F) | `test_wa_worker_safety_followup.py` | 26 passed |
| Products | `test_product_service.py` 6, `test_products_api.py` 5 | 11 passed |

Sum: 1279.

## 6. Pre-deployment checks (YOU run these; I cannot and did not access Production or Railway)
Do not merge or deploy until A to E are done.

**A. An active Super Admin must exist in Production `dashboard_users`.** This patch removes the old username/password login and the default account; only mobile + PIN + SMS code remains. In mongosh against the Production database (read-only; these return counts and names, no PIN data):

    const r = db.dashboard_roles.findOne({role_name: "Super Admin"}, {_id: 1});
    r
    db.dashboard_users.countDocuments({role_id: r._id, status: {$nin: ["disabled","suspended"]}, pin: {$exists: true}, mobile: {$exists: true}})
    // if that prints 0, role_id may be stored as text:
    db.dashboard_users.countDocuments({role_id: String(r._id), status: {$nin: ["disabled","suspended"]}, pin: {$exists: true}, mobile: {$exists: true}})
    db.dashboard_users.find({role_id: r._id}, {_id: 0, full_name: 1, status: 1}).toArray()

Required: **at least 2** active Super Admins whose mobile numbers can receive SMS (one is a spare). If 0 or 1: do NOT deploy; tell me. `tools\create_super_admin.py` exists but writes to the database, so it needs your explicit approval and your hands.

**B. Production MSG91 SMS must work.** (1) On the CURRENT Production admin login, sign in and confirm the SMS code arrives on the phone (today's Production uses the same MSG91 variables). (2) In Railway, Production service, Variables (view only), confirm the names in section 7 exist and are not empty. (3) In the MSG91 dashboard confirm credit remains and the admin template is approved. If SMS fails after deployment nobody can sign in (there is deliberately no fallback code).

**C. Safety net.** Existing admin sessions stay valid after the patch (same `dashboard_users` token, same 30-minute inactivity limit). Keep one Super Admin tab active while you deploy, and read section 9 (rollback) first.

**D. Nobody uses the old login.** No staff member or script may use the old `/admin/login` username/password page or API: it now answers 410.

**E. Staging switches absent.** In Railway Production Variables, confirm none of the "must be absent" names in section 7 exist.

## 7. Environment variables (names only; I never read values)

### Production: required (already used by today's Production, verify they exist)
- `MSG91_AUTH_KEY`, `MSG91_SENDER_ID`, and `MSG91_ADMIN_TEMPLATE_ID` (if absent, `MSG91_TEMPLATE_ID` is used).
- `MONGO_URL` (or whatever `database.py` already reads), unchanged.
- Phase 1 WhatsApp variables, unchanged: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_BUSINESS_ACCOUNT_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_WEBHOOK_SIGNATURE_MODE`.

### Production: recommended to add (you do this in Railway)
- `WA_CAMPAIGN_ENVIRONMENT=production`. Without it every campaign send is still refused (an unknown environment is refused) but the dashboard shows the environment as "unknown".

### Production: optional
- `ADMIN_TRUSTED_DEVICE_DAYS`: default 30; `0` turns "Remember this device" off; maximum 90.
- `ADMIN_TRUSTED_DEVICE_SUPER_ADMIN=1`: lets Super Admin use trusted devices. Default off (Super Admin always gets PIN + SMS).

### Production: MUST BE ABSENT (do not set; delete if present)
`ADMIN_OTP_STAGING_TEST_MODE`, `ADMIN_OTP_TEST_MOBILES`, `WA_CAMPAIGN_SENDING_ENABLED`, `WA_CAMPAIGN_WORKER_ENABLED`, `WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION`, `WA_CAMPAIGN_TEST_NUMBERS`. Leave `WHATSAPP_TEMPLATE_TEST_ENABLED` as it is today. Never define any `RAILWAY_*` variable (Railway injects them). Campaign sending stays OFF until you separately approve it.

### Staging: what it needs (unchanged by this patch; Staging is not modified)
- Admin fixed OTP (1234, no SMS) works only when ALL are true: `ADMIN_OTP_STAGING_TEST_MODE=1`; Railway-injected project, environment and service IDs equal the three Staging IDs pinned in `services\admin_otp_test_mode.py`; the public domain is exactly the Staging host; every host in `MONGO_URL` is `mongodb.railway.internal`; the admin's mobile is in `ADMIN_OTP_TEST_MOBILES` (comma separated, 10 digits).
- Campaigns on Staging: `WA_CAMPAIGN_ENVIRONMENT=staging`; sending needs `WA_CAMPAIGN_SENDING_ENABLED` plus the database kill switch; the worker needs `WA_CAMPAIGN_WORKER_ENABLED`; test sends need `WA_CAMPAIGN_TEST_NUMBERS`.
- Keep real Production PINs out of Staging (a fixed code is public, so the PIN is the only real factor there).

### Safety restrictions (enforced in code and tests)
- Production can never use the fixed OTP 1234: it needs all five Staging identity checks at once, evaluated on every call; any miss or error means real MSG91 SMS and a random code (69 dedicated tests).
- Campaign sending needs ALL of: environment key, database kill switch ON, a non-Production environment (or the explicit Production override), worker key on. None is set by this patch.
- Railway environment *names* are never trusted to enable anything.

## 8. Post-deployment verification (after YOU merge and deploy; outside this patch)
1. Open the Production admin login in a private window. Sign in with mobile + PIN: a real SMS arrives, you enter it, the dashboard opens. A wrong 4-digit code (for example 1234) is rejected.
2. Startup log contains `[2FA] admin OTP test mode: INACTIVE (...)` and no line saying test mode is ACTIVE.
3. Dashboard > WhatsApp Campaigns opens and sends nothing. Signed in, `GET /admin/whatsapp-campaigns/config` shows sending not allowed, worker not enabled, environment production.
4. Signed out: `/download/admin_dashboard` answers 401 and `POST /admin/login` answers 410. For example: `curl -i https://YOUR-PRODUCTION-HOST/download/admin_dashboard` must show `401`.
5. The WhatsApp webhook still works (send a test message to your business number; check the webhook log).
6. Each admin is asked for an SMS code once (old trusted-device cookies no longer count).
7. Within 15 minutes check Railway logs for `Traceback` or `WhatsApp ... skipped`; startup warnings of the form "indexes skipped" mean the database was unreachable at startup.

## 9. Rollback
- **Before merging:** just remove the worktree; nothing else changed:

      cd /d "D:\irfan work\Offro\backend-staging"
      git worktree remove --force ..\offro-production-release-2
      git branch -D production-release-2

  (Only do this if you are sure the branch holds nothing else you need.)
- **Patch applied but not committed:** `git apply -R "C:\path\to\phase-prod-integration-whatsapp-2A-2G-and-admin-security-CRLF.patch"` then `git status` (the 53 new files are removed by the reverse patch).
- **Merged and deployed:** in Railway redeploy the previous Production deployment (fastest), and/or on `main` run `git revert -m 1 <merge-commit>` and redeploy.
- Data: the patch has no data migration. New collections (`admin_login_challenges`, `admin_auth_throttle`, `admin_trusted_devices`, `wa_*`) and indexes are additive and harmless if left behind. The old `admins` and `trusted_devices` data was never touched, so an older deployment keeps working with it.
