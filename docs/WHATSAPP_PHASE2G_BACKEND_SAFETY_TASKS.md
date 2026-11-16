# WhatsApp Campaigns: backend safety tasks to finish before any Production sending is enabled

Status: **open, not part of Phase 2G Step 1** (Step 1 is strictly read-only and changes no backend behaviour).

## 1. Single-number test send has no independent Production guard
* The bulk worker refuses to run on a server that identifies as Production (`production_blocked()` in `services/wa_campaign_worker.py`,
  unless `WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION` is set).
* `send_test_message` (`POST /admin/whatsapp-campaigns/campaigns/{id}/test-send`) has **no such guard**. In Production it would be
  limited only by `WA_CAMPAIGN_SENDING_ENABLED`, the database kill switch, the `approve` permission and the test-number allow-list.
* The dashboard therefore says that the Production guard covers the worker only, and never claims that all sending is blocked in Production.
* Task: decide and implement a Production policy for test sends (for example the same `production_blocked()` check with an explicit,
  separate override), with tests. Needs approval before it changes existing behaviour.

## 2. Production detection depends on environment naming
* `production_blocked()` looks for `production`/`prod` in `RAILWAY_ENVIRONMENT_NAME`, `RAILWAY_ENVIRONMENT`, `APP_ENV`, `ENVIRONMENT`, `ENV`.
  A Production environment with another name would not be detected (the two sending keys would still hold).
* Task: confirm the Production environment name, or invert the guard to an explicit allow-list of non-Production environments.

Neither item may be left open when `WA_CAMPAIGN_SENDING_ENABLED` is set on Production.
