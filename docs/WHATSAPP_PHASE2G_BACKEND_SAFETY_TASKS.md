# WhatsApp Campaigns: backend safety tasks (status: CLOSED in Phase 2G complete)

Both tasks recorded after Phase 2G Step 1 are resolved. Details: `docs/WHATSAPP_PHASE2G_DASHBOARD.md`.

## 1. Single-number test send had no independent Production guard: closed
`send_test_message` (`POST /admin/whatsapp-campaigns/campaigns/{id}/test-send`) now calls `services.wa_campaign_settings.environment_gate()`
before the allow-list check. Production and unidentified environments are refused (HTTP 409, reason `production_blocked` / `environment_unknown`).
There is deliberately **no override** for test sends. The kill switch, environment key, consent, allow-list and `approve` permission are unchanged.

## 2. Production detection depended on environment naming: closed
The guard no longer guesses from names. It requires an explicit `WA_CAMPAIGN_ENVIRONMENT` (production / staging / development / test).
A missing or invalid value is "unknown" and sending is refused (fail closed). A platform signal that says production
(`RAILWAY_ENVIRONMENT_NAME` / `RAILWAY_ENVIRONMENT`) always wins over an explicit non-production value (a conflict means Production).
Tests: `test_wa_environment_guard.py`.
