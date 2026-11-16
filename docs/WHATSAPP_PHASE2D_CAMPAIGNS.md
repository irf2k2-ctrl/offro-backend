# WhatsApp Campaign Manager - Phase 2D: Campaign lifecycle and test send

Drafts, validation, recipient snapshot, schedule **metadata** and a gated single-recipient **test send**.
**There is no bulk sender, no background worker and no "send now". Nothing reads a schedule and acts on it** (that is Phase 2E).
Code: `services/wa_campaign_manager.py`, `routers/wa_campaign_lifecycle.py` (mounted from `routers/wa_campaigns.py`).
Tests: `test_wa_campaign_lifecycle.py`.

## Reused (not duplicated)
Audience / eligibility preview (`services/wa_audience.build_preview`, `services/wa_eligibility`), templates and readiness
(`services/wa_templates`), kill switch and test-number allow-list (`services/wa_campaign_settings`), audit (`services/wa_campaign_audit`),
RBAC module, Phase 1 send-outcome classification (`classify_send_response`). Additive changes to earlier code are listed at the end.

## Lifecycle
```
draft --validate--> ready --schedule--> scheduled          draft / ready / scheduled --cancel--> cancelled (terminal)
  ^                  |  ^                   |               scheduled --unschedule--> ready
  +--revert-to-draft-+  +-------------------+               completed: reserved for the Phase 2E worker; no path leads to it here
```
Every change is a conditional update on **status and version**, so two admins or two replicas cannot both win (`409 conflict`).
Only a `draft` is editable; any change to a ready campaign goes back through revert-to-draft + validate.
`schedule` is the explicit approval and only stores `schedule.send_at`. There is deliberately no immediate launch: that will be a
separate approved action in Phase 2E.

## Endpoints (`/admin/whatsapp-campaigns/campaigns`; all need the WhatsApp Campaigns module)
| Method | Path | Permission | Purpose |
|---|---|---|---|
| POST | `` | add | Create a draft `{name, description}` |
| GET | `` | view | List (`status`, paging) |
| GET | `/{id}` | view | Detail |
| PATCH | `/{id}` | edit | Edit a draft: `name`, `description`, `template`, `header_media`, `audience`, optional `expected_version` |
| DELETE | `/{id}` | delete | Delete a draft / cancelled campaign (audit first) |
| POST | `/{id}/preview` | view | Render the template for a SAMPLE recipient (defaults are obviously fake) |
| POST | `/{id}/audience-preview` | view | Live recipients + eligibility counts (same engine as Phase 2B) |
| POST | `/{id}/validate` | edit | draft -> ready; freezes the recipient snapshot |
| POST | `/{id}/revert-to-draft` | edit | ready -> draft (drops the snapshot) |
| GET | `/{id}/recipients` | view | Snapshot (masked numbers) |
| GET | `/{id}/confirmation` | view | Checklist + warnings to read before scheduling |
| POST | `/{id}/test-send` | **approve** | One test message to an allow-listed number |
| POST | `/{id}/schedule` | **approve** | Explicit confirmation + schedule metadata |
| POST | `/{id}/unschedule` | edit | scheduled -> ready |
| POST | `/{id}/cancel` | edit | -> cancelled (never blocked by the audit log) |
Audit entries of a campaign: existing `GET /admin/whatsapp-campaigns/audit?campaign_id=<id>`.

### Examples
`POST /campaigns` `{"name":"Diwali offer","description":"..."}` -> `{"id":"6b..","status":"draft","version":1,"template":null,"snapshot":null,"schedule":null,"executes_automatically":false,...}`

`PATCH /campaigns/{id}`
```json
{"template":{"name":"offro_offer","language":"en"},
 "audience":{"merchants":{"ids":["<account id>"],"filter":{"city":"Pune"}},"imports":["<import id>"]},
 "header_media":{"type":"image","id":"1234567890"},"expected_version":2}
```
`header_media` is only for templates with an IMAGE / VIDEO / DOCUMENT header (`id` = WhatsApp media id, or an https `link`); it is never echoed back.

`POST /campaigns/{id}/validate` -> 200
```json
{"ok":true,"errors":[],"snapshot":{"id":"..","total":4,"eligible":2,"excluded":2,"by_reason":{"opted_out":1,"unknown_consent":1},
  "by_source":{"merchant":{"total":4,"eligible":2},"custom":{"total":0,"eligible":0}},"content_hash":"..","mapped_hash":"..","at":"..Z"},"campaign":{...}}
```
or 409 `{"detail":{"code":"validation_failed","errors":["template:not_enabled","audience:empty","consent_check_failed","no_eligible_recipients", "..."]}}`.
Validation checks: template selected, cached readiness (approved, MARKETING, supported, enabled, mapping complete and current), header media, non-empty
audience, consent readable (fail closed), every eligible recipient's template values valid (invalid ones are excluded as `invalid_variable_values`, never sent blank).

`POST /campaigns/{id}/schedule`
```json
{"send_at":"2026-12-01T10:00:00+05:30","confirm":{"acknowledge":true,"eligible_count":2,"template":"offro_offer/en"}}
```
Requires: status `ready`; snapshot younger than 24 h; `confirm.eligible_count` equal to the snapshot's; `confirm.template` exact; `send_at` with an explicit UTC
offset, 15 min to 90 days ahead (stored as UTC); template and mapping unchanged since validation; **live re-verification with Meta** (approved, marketing,
content unchanged; if Meta cannot be asked, scheduling is refused); audit entry written first. Response includes `warnings` (sending currently disabled,
no worker yet, consent re-checked at send time, excluded recipients, no test send yet).

`POST /campaigns/{id}/test-send` `{"phone":"+919876500001","confirm_test_send":true,"dry_run":false,"recipient":{"merchant_name":"Ravi"}}`
-> 200 `{"ok":true,"sent":true,"outcome":"sent","message_id":"wamid..","to_last4":"0001"}`; `dry_run:true` runs every gate and returns the exact Meta `components`
without sending. Failures: 400 `confirmation_required|invalid_phone`, 403 `not_a_test_number`, 409 `sending_disabled|recipient_not_eligible|template_not_ready|payload_invalid|campaign_closed`,
429 `rate_limited|cooldown`, 502 `template_unverified` (Meta unreachable / not verifiable) or a rejected send, **202 `uncertain`** (outcome unknown - never retried automatically), 503 `audit_unavailable`.

## Test-send gates (in this order; nothing reaches Meta until the local gates pass)
1. `confirm_test_send` is exactly `true`.
2. Template selected, enabled and cache-ready (+ header media when required).
3. **Phase 2A kill switch**: env `WA_CAMPAIGN_SENDING_ENABLED` AND the DB switch must be on (fail closed). Not bypassed, not even for dry runs.
4. **Allow-list**: the number must be in `WA_CAMPAIGN_TEST_NUMBERS` (empty list = nobody). Real recipients are not test numbers.
5. **Consent**: `wa_eligibility.check` - START or admin-recorded evidence, not opted out, lookup must succeed. A STOP received a moment earlier blocks the send.
6. Rate limits: same campaign+number 60 s; 10 per admin per hour; 30 per hour globally. (Dry runs do not count.)
7. **Live Meta re-check** of the template (APPROVED, MARKETING, content unchanged since mapping, supported). Meta error = no send.
8. Payload built strictly from the mapping (`build_send_components`); incomplete or invalid values = no send.
9. Strict audit entry + claim record written; if either fails nothing is sent.
10. One call to the classified sender. Outcome stored (`sent` / `failed` / `uncertain`) and audited. No retry, ever.

## Snapshot and data handling
`validate` writes one `wa_campaign_recipients` document per ELIGIBLE recipient: canonical phone, source/ref, consent basis, and the **resolved template values frozen at
validation**. Excluded people are only counted by reason (no numbers stored). Re-validating, reverting, cancelling or deleting removes the previous snapshot. Snapshot writes
are all-or-nothing (partial inserts are removed on failure or if the status change loses a race). Numbers are masked in every response and appear in audit as last-4 + hash.
`wa_campaign_test_sends` keeps last-4 + hash only. Retention: no TTL is introduced (still pending your approval); campaigns/snapshots are removed by delete/cancel/revert.

## Startup / Production safety
New collections `wa_campaigns`, `wa_campaign_recipients` (unique `campaign_id+phone`), `wa_campaign_test_sends`: indexes only, created at startup by `ensure_indexes_safe()` (never raises).
Nothing is processed or sent at startup. On Production the env key `WA_CAMPAIGN_SENDING_ENABLED` stays off, so test sends are impossible there, and scheduled campaigns cannot run (no worker).

## Security review (all covered by tests)
* Every route validates the WhatsApp Campaigns module server-side; `approve` is required to test-send or schedule; delete needs `delete`; cancel/unschedule need only `edit`.
* Identity is taken from the session; sensitive actions are audited first (strict) - schedule, test-send, delete; cancel is audited best-effort so it can never be blocked.
* The kill switch, allow-list, consent and live template checks are re-evaluated on every call; none can be skipped by request fields.
* Tokens, WABA ids, full phone numbers and Meta error text never appear in responses, logs, audit or storage (error codes only).
* No code in the new modules uses threads, timers, schedulers or background tasks; exactly one call site can reach WhatsApp (`send_test_message`).

## Changes to earlier code (additive; behaviour unchanged)
* `services/wa_audience.py`: optional `collect` argument on `build_preview` + `_merchant_ctx` (the snapshot needs canonical phones and merchant context). Output unchanged.
* `services/wa_templates.py`: `_meta_get` extracted from `fetch_from_meta` (same behaviour), plus `fetch_live_template`, `verify_live`, error code `template_not_found`.
* `services/whatsapp.py`: new `send_whatsapp_template_checked` (classified outcome). `send_whatsapp_template` and the Phase 1 senders are untouched.
* `routers/wa_campaigns.py` (+2 lines), `server.py` (startup index block), `test_wa_campaigns_foundation.py` (route-set assertion allows `/campaigns`).

## Follow-up: two test-send safety corrections
1. **Final consent re-read.** `E.check()` runs again as the last step before `send_whatsapp_template_checked`. STOP / unknown / opted-out, a consent DB failure, or any exception blocks the send; the outcome is audited as `blocked:recipient_not_eligible` (`stage: final_check`) and the attempt record is marked `blocked`.
2. **Atomic send claim.** The cooldown is now one upsert on `wa_campaign_test_locks` (`_id = campaign:phone_hash`, matches only when `next_allowed_at <= now`); a live claim yields DuplicateKeyError -> 429. Works across replicas. Sent/failed keep the 60 s cooldown, uncertain holds 15 min, pre-send failures release the claim (token-guarded). Claim DB errors -> 503, nothing sent. Hourly limits are re-counted after our own record is written.
3. **Final kill-switch re-read.** `ST.sending_allowed()` (env `WA_CAMPAIGN_SENDING_ENABLED` AND the database switch) is read again as the very last gate before the sender call. Either off, an unreadable switch or any error blocks the send (`blocked:sending_disabled`, `stage: final_check`, reason `env_disabled` / `switch_off` / `settings_unavailable`); the attempt is marked `blocked` (not sent/failed), and only this request's own claim is released.
