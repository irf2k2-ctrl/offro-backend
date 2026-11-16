# WhatsApp Campaign Manager - Phase 2C: Template management

Configuration only. **Nothing in this phase sends a WhatsApp message, starts a worker or schedules anything.** The only
Meta call is the read-only template listing behind `POST /templates/sync`.
Code: `services/wa_templates.py`, `routers/wa_campaign_templates.py` (mounted from `routers/wa_campaigns.py`).
Tests: `test_wa_campaign_templates.py` (Meta is faked; no network).

## Reused, not duplicated
| Reused | From |
|---|---|
| Graph base URL, auth header, token / phone-number-id / WABA-id config | `services/whatsapp.py` |
| RBAC module "WhatsApp Campaigns", `require_permission` | Phase 2A / `routers/dashboard_auth.py` |
| Audit trail (`record`, `admin_ref`, strict audit-before-grant) | `services/wa_campaign_audit.py` |
| Safe error summaries (`exc_summary`) | `services/wa_webhook_security.py` |
| Same template-shape rules in spirit (image header, variables, dynamic URL buttons) | `routers/wa_template_test.py` (left untouched) |

`routers/wa_template_test.py::fetch_template_status` fetches ONE hard-coded template; the campaign code needs the whole
listing, so it has its own paged read using the same credentials and headers.

## Endpoints (`/admin/whatsapp-campaigns/templates`, all need the WhatsApp Campaigns module)
| Method | Path | Permission | Purpose |
|---|---|---|---|
| POST | `/sync` | edit | Refresh the local cache from Meta (30 s cooldown across replicas, audited) |
| GET | `` | view | List; filters `status`, `category`, `language`, `q` (name), `campaign=usable\|enabled\|not_ready`, paging, facets |
| GET | `/{name}/{language}` | view | Detail: components, variables (+Meta examples), readiness, campaign use |
| POST | `/{name}/{language}/preview` | view | Render with values / Meta example values. Sends nothing |
| PUT | `/{name}/{language}/campaign-use` | edit (enable: **approve**) | Save variable mapping; enable / disable the template for campaigns |

### Sync
`POST /sync` -> `200 {"ok":true,"counts":{"fetched":9,"added":9,"updated":0,"unchanged":0,"status_changed":0,"marked_missing":0,"restored":0,"skipped_invalid":0},"status_changes":[],"synced_at":"..."}`
* The whole listing is fetched first (cursor paging, max 20 pages x 100); **nothing is written unless it all succeeded**.
* Templates Meta no longer returns are kept but marked `missing_from_meta` (and become unusable); they are restored if they reappear.
* The admin's `campaign_use` configuration is never overwritten by a sync.
* Errors (`{"detail":{"code","message","meta_code"}}`, no Meta text, no credentials): `not_configured` 503, `meta_auth_failed` 502,
  `meta_permission_denied` 502, `meta_rate_limited` 429, `meta_unreachable` 504, `meta_error` 502, `meta_bad_response` 502,
  `too_many_templates` 502, `sync_busy` 429, `storage_failed` 503. Failures are audited with the code only.

### Detail (abridged)
```json
{"name":"offro_offer","language":"en","status":"APPROVED","category":"MARKETING","variable_count":2,
 "campaign_supported":true,"unsupported":[],"requires_header_media":false,"missing_from_meta":false,
 "campaign_enabled":true,"usable_for_campaign":true,"readiness":[],
 "parsed":{"parameter_format":"POSITIONAL","body":{"text":"Hello {{1}}, offer {{2}}","variables":["1","2"]},
   "variables":[{"key":"body.1","component":"body","name":"1","example":"Asha"},{"key":"body.2","component":"body","name":"2","example":"20%"}],
   "buttons":[],"footer":null,"header":null},
 "campaign_use":{"enabled":true,"variables":{"body.1":{"source":"merchant_name","fallback":"Valued merchant"},"body.2":{"source":"static","value":"20% off"}},
   "sample_values":{},"stale":false,"updated_by":"Asha Admin","updated_at":"..."},
 "variable_sources":{"static":"...","merchant_name":"...","store_name":"...","store_city":"...","store_area":"...","recipient_name":"..."}}
```
Variable keys: `header.<n>`, `body.<n>` (positional `1,2..` or named), `button.<position>` (URL-button variable).

### Preview
`POST /offro_offer/en/preview` body `{"values":{"body.1":"Ravi"},"use_samples":true}` ->
`{"preview":{"header":null,"body":"Hello Ravi, offer 20%","footer":null,"buttons":[]},"missing_variables":[],"invalid_values":{},"unknown_variables":[],"complete":true,"requires_header_media":false,"unsupported":[],"sent":false,"note":"..."}`
Unset variables use Meta's example values when `use_samples` is true. Values Meta would reject (empty, newline/tab, 4+ spaces,
too long) are reported in `invalid_values` and never rendered.

### Campaign use
`PUT /offro_offer/en/campaign-use` body
`{"enabled":true,"variables":{"body.1":{"source":"merchant_name","fallback":"Valued merchant"},"body.2":{"source":"static","value":"20% off"}},"sample_values":{}}`
* Sources: `static` (needs `value`), `merchant_name`, `store_name`, `store_city`, `store_area`, `recipient_name` (these need a non-empty `fallback`,
  used when a recipient has no value - Meta rejects blank parameters).
* **Enabling** needs `approve` plus: status `APPROVED`, category `MARKETING`, only supported components, every variable mapped and valid,
  not missing at Meta. Otherwise `409 {"code":"template_not_ready","reasons":[...],"mapping_problems":[...]}`. The audit entry is written first.
* Saving a draft (`enabled:false`) and disabling need only `edit` and always work.
* If Meta changes a template's content, its hash changes and the saved mapping becomes **stale** (`mapping_stale`, not usable) until saved again.
* `usable_for_campaign` is computed from the last sync. **A later send phase must re-verify the status with Meta immediately before sending.**

## Supported templates
Supported: text body; footer; header TEXT/IMAGE/VIDEO/DOCUMENT; buttons QUICK_REPLY, URL (one variable), PHONE_NUMBER; positional and named
variables. Media headers are flagged `requires_header_media` - the media itself is supplied per campaign in a later phase.
Not supported for campaigns (flagged, never fatal): OTP / COPY_CODE / FLOW / catalog / MPM buttons, LOCATION header, carousel and other
components, non-sequential positional variables, templates without a body. Only MARKETING templates can be enabled (UTILITY and
AUTHENTICATION are refused so they cannot be used for promotional sends).

## Pure helpers for later phases (no I/O, no sending)
`build_send_components(parsed, values, header_media)` builds the exact Meta `components` payload and raises rather than guess;
`resolve_values(campaign_use, recipient)` maps one recipient to values using the mapping and fallbacks.

## Storage
Collection `wa_campaign_templates` (`_id` = `"<name>|<language>"`): Meta id, status/category, components (template text only - no personal data),
parsed structure, content hash, sync timestamps and the admin's `campaign_use`. Sync cooldown state lives in `wa_campaign_settings` (`_id: "template_sync"`).
No indexes are created and nothing runs at startup, so a Production deploy changes no data and activates nothing. No retention rule is introduced
(templates are small, non-personal configuration).

## Security checklist
* Every route validates the WhatsApp Campaigns permission server-side; enabling needs the separate `approve` action.
* Admin identity from the session; sync, enable, refusals and mapping saves are audited (no phone numbers or secrets in entries).
* Tokens and Meta's free-text errors are never returned or logged (error codes only); `paging.next` URLs (which can carry tokens) are never followed.
* Sync is manual only (no scheduler), rate-limited across replicas, all-or-nothing, and bounded.
