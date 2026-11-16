# Files included and excluded

**Baseline `335c971`.** Tested result with the patch: **1279 passed**. See `APPLY-PRODUCTION.md` for Windows CMD steps and `PRODUCTION-REVIEW.md` for the review.

Baseline `origin/main` 335c971. Source: `origin/staging` b79d7f9 (16 commits ahead: Phase 1 delta, 2A-2G, admin OTP, legacy retirement, Patch A/A2/B/C).
The patch is the tree difference between those two, minus the exclusions below, plus one new test file. Total: **70 files (17 modified, 53 new)**.

## Not changed at all (identical on main and staging, so not in the patch)
`routers/webhook.py`, `docs/WHATSAPP_WEBHOOK_STAGING_SETUP.md`, `docs/WHATSAPP_PHASE2_REQUIREMENTS.md`, `services/security.py` (CORS allow-list, SecurityMiddleware, Secure cookies), `routers/maps.py`, all product / collaboration routers and services, `utils/*`, `requirements.txt`, `database.py`.

## Modified (17)
`README.md`, `conftest.py`, `routers/admin.py`, `routers/dashboard_auth.py`, `routers/otp_service.py`, `routers/popup_campaigns.py`, `routers/wa_chat.py`, `routers/wa_template_test.py`, `server.py`, `services/wa_consent.py`, `services/whatsapp.py`, `templates/admin_dashboard.html`, `templates/admin_login.html`, `test_products_api.py`, `test_security_hardening.py`, `test_wa_template_test.py`, `test_wa_webhook_consent.py`

## New (53)
- Admin security: `services/admin_login_otp.py`, `services/admin_login_limits.py`, `services/admin_trusted_devices.py`, `services/admin_otp_test_mode.py` (Staging-only, inert in Production, see PRODUCTION-REVIEW.md), `tools/create_super_admin.py`, `docs/ADMIN_LOGIN_2FA.md`.
- Admin security tests: `test_security_admin_login_2fa.py`, `test_security_admin_legacy_auth_retired.py`, `test_security_admin_login_limits.py`, `test_security_admin_trusted_device.py`, `test_security_admin_otp_test_mode.py`, and **`test_security_admin_otp_test_mode_production.py` (new, written for this release)**.
- WhatsApp 2A-2G services: `services/wa_audience.py`, `wa_campaign_audit.py`, `wa_campaign_manager.py`, `wa_campaign_settings.py`, `wa_campaign_worker.py`, `wa_delivery.py`, `wa_eligibility.py`, `wa_templates.py`.
- WhatsApp routers: `routers/wa_campaigns.py`, `wa_campaign_audience.py`, `wa_campaign_lifecycle.py`, `wa_campaign_media.py`, `wa_campaign_recipient_details.py`, `wa_campaign_run.py`, `wa_campaign_templates.py`, `wa_consent_admin.py`.
- Dashboard UI: `static/css/wa_campaigns.css`, `static/js/wa_campaigns.js`, `wa_campaigns_admin.js`, `wa_campaigns_audience.js`, `wa_campaigns_editor.js`, `wa_campaigns_templates.js`, `templates/partials/wa_campaigns.html`.
- WhatsApp tests: `test_wa_campaign_audience.py`, `test_wa_campaign_lifecycle.py`, `test_wa_campaign_templates.py`, `test_wa_campaign_test_send_safety.py`, `test_wa_campaign_worker.py`, `test_wa_campaigns_foundation.py`, `test_wa_dashboard_ui.py`, `test_wa_delivery_tracking.py`, `test_wa_environment_guard.py`, `test_wa_phase2g_endpoints.py`, `test_wa_worker_safety_followup.py` (trimmed, see below).
- Docs: `docs/WHATSAPP_PHASE2A_FOUNDATIONS.md`, `..2B_AUDIENCE.md`, `..2C_TEMPLATES.md`, `..2D_CAMPAIGNS.md`, `..2E2F_WORKER_DELIVERY.md` (edited, see below), `..2G_BACKEND_SAFETY_TASKS.md`, `..2G_DASHBOARD.md`.

## Excluded (Phase 2H and Staging-only procedures)
Phase 2H = the Staging concurrency verification work: the tool (first added with the 2E/F commit, extended by commit f296871), its tests and its procedure. These are verification aids for Staging only and must not reach Production. They are independent of the worker code, which is kept whole.

| File | Why |
|---|---|
| `tools/wa_staging_concurrency_check.py` | 2H Staging concurrency verification tool |
| `test_wa_staging_concurrency_script.py` | tests of that tool |
| `docs/WHATSAPP_STAGING_CONCURRENCY_PROCEDURE.md` | Staging-only procedure |
| 12 tests at the end of `test_wa_worker_safety_followup.py` (section "4. staging concurrency script helpers") | they import the excluded tool; removed from the shipped copy |
| Section 9 and one file-list line of `docs/WHATSAPP_PHASE2E2F_WORKER_DELIVERY.md` | described the excluded procedure; replaced by a one-paragraph note |

## Retained Phase 2E/F worker safety corrections
`services/wa_campaign_worker.py` is **unchanged by 2H** and ships whole: per-message template re-verification, worker key re-read in the final gate, atomic lease renewal, `send_started` / final lease check, Production guard. The 26 tests in `test_wa_worker_safety_followup.py` that cover them ship (the 12 script-runner tests do not).

## Not touched
Your uncommitted product edits (`routers/admin_products.py`, `routers/maps.py`, `routers/merchant_products.py`, `services/products.py`, `templates/admin_dashboard.html` local edits, and the untracked test files in your working folder) are not in the patch and are not read by it. The patch was built from committed `origin/main` and `origin/staging` only. NOTE: `templates/admin_dashboard.html` is in the patch (menu entry + script tags); if your local working copy also edits that file, apply the patch in the separate worktree from APPLY-PRODUCTION.md and reconcile later.
