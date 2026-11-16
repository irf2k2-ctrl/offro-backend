# WhatsApp Campaigns: Phase 2G (complete dashboard)

Baseline: staging `c72fa94`. Nothing here sends a message by itself. There is no "Send now"; the only things that can reach a phone are a
scheduled campaign run by the worker and an explicitly confirmed single test send.

## 1. Environment guard (root cause and fix)
**Root cause of "Worker Production guard: Blocked" on Staging.** The old guard matched `production`/`prod` inside `RAILWAY_ENVIRONMENT_NAME`,
`RAILWAY_ENVIRONMENT`, `APP_ENV`, `ENVIRONMENT` or `ENV`. Any production-looking value in any of them (for example a shared `APP_ENV=production`
copied to Staging, or a Railway environment name containing "prod") blocked Staging, while an unusually named Production environment passed
(fail open). Test send had no guard at all. The dashboard's new **Environment** card shows the classification, its source and the signals seen.

**Rules (`services/wa_campaign_settings.environment_status()` / `environment_gate()`):**
| Situation | Result |
|---|---|
| `WA_CAMPAIGN_ENVIRONMENT=staging` (or development / test), no platform production signal | that environment: allowed to pass the environment gate |
| `WA_CAMPAIGN_ENVIRONMENT=production` | production: refused for test sends; refused for the worker unless `WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION` is set |
| platform name (`RAILWAY_ENVIRONMENT_NAME`/`RAILWAY_ENVIRONMENT`) says production but explicit value is non-production | production (conflict, fail closed) |
| generic `APP_ENV`/`ENVIRONMENT`/`ENV` says production but explicit value is non-production | explicit value wins; the signal is reported as `overridden_signals` |
| no explicit value, a production signal present | production |
| no explicit value, no signal / invalid value / any error | unknown: all sending refused |

The environment gate is only one of several gates. Unchanged and still required: `WA_CAMPAIGN_SENDING_ENABLED`, the database kill switch,
`WA_CAMPAIGN_WORKER_ENABLED` (worker), `WA_CAMPAIGN_BULK_LIVE_ENABLED` (live mode), consent checked immediately before each send, the test-number allow-list and permissions.

## 2. Required Railway configuration (do this yourself; the patch changes no variables)
* **Staging service:** add `WA_CAMPAIGN_ENVIRONMENT=staging`. Without it the Environment card shows "unknown" and sending stays blocked.
* **Production service:** add `WA_CAMPAIGN_ENVIRONMENT=production`. Do not set `WA_CAMPAIGN_SENDING_ENABLED`, `WA_CAMPAIGN_WORKER_ENABLED`,
  `WA_CAMPAIGN_BULK_LIVE_ENABLED` or `WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION` there.
* If Staging still reads Blocked after this, open Overview > Environment: it lists the signal. A Railway environment whose own name is production
  on the Staging service is blocked by design (conflict means Production).
* Sending on Staging additionally needs the keys above plus the kill switch turned ON from the dashboard (Super Admin / `approve`).

## 3. New endpoints (all under `/admin/whatsapp-campaigns`, reuse existing services/collections, read-only unless stated)
| Method and path | Permission (WhatsApp Campaigns) | Notes |
|---|---|---|
| `GET /campaigns/{id}/recipient-details` | view | masked phone, eligibility snapshot + consent now, state, timestamps, failure class/code, retry info, hash message reference (never the wamid). Filters: `state`, `last4`, paging (page_size <= 100) |
| `GET /consents` | view | masked phone, status, source, timestamps, evidence reference; filters `status`, `last4`, paging. Also returns admin consent source names |
| `GET /consent-events` | view | STOP/START/admin-consent ledger, masked; filters `event`, `last4`, paging |
| `POST /media/header` | edit | multipart upload of a template header media file to WhatsApp. jpeg/png <= 5 MB, mp4/3gpp <= 16 MB, pdf <= 10 MB; magic bytes verified; filename sanitised; audited as `campaign.media_upload`; errors never contain provider text |

The older unprotected upload route is not used. Existing endpoints used by the dashboard are unchanged.

## 4. Dashboard (tabs)
Overview (config, environment, kill switch, worker, allow-list, campaign counts, kill-switch ON/OFF with confirmation) · Campaigns (list, create, edit with
template + variable mapping + audience + header media, preview, validate, review checklist and snapshot, schedule with typed eligible count, unschedule,
cancel, revert to draft, delete draft, test send with dry run default, run mode test/live with typed confirmation, pause/resume/cancel run,
report tiles and progress, masked recipient details) · Audience (merchants and filters, CSV and manual import with counts, preview with reasons) ·
Templates (sync, status, mapping, header requirements, preview, enable/disable) · Consent (masked list, history, admin-recorded consent with evidence) · Audit.
Permissions are enforced by the backend; the UI only hides controls. Phone numbers typed by an admin are sent once and cleared; nothing is stored in the browser.

## 5. Tests
`test_wa_environment_guard.py`, `test_wa_phase2g_endpoints.py`, `test_wa_dashboard_ui.py` plus the existing lifecycle / worker / test-send / webhook suites.
Browser verification (Playwright, mocked Meta) covered every flow, the viewer and no-access roles, and layout at 1024/1366/1920 px with no horizontal overflow.
