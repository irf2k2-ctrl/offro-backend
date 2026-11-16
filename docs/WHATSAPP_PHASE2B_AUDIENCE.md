# WhatsApp Campaign Manager - Phase 2B: Audience management

Audience only. **Nothing in this phase sends a message, calls Meta, starts a worker or schedules anything.**
Code: `services/wa_audience.py`, `routers/wa_campaign_audience.py` (mounted from `routers/wa_campaigns.py`).
Tests: `test_wa_campaign_audience.py`.

All routes are under `/admin/whatsapp-campaigns/audience` and need the dedicated **WhatsApp Campaigns** module
(validated server-side on every route). Phone numbers are always returned masked (`***3210`).

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/filters` | view | City / Area / State options from active stores (admin's city scope applied) |
| GET | `/merchants?q=&city=&area=&state=&page=&page_size=` | view | Qualifying merchants + their eligibility |
| POST | `/imports` | add | Manual custom numbers (JSON) |
| POST | `/imports/csv` | add | CSV upload (multipart field `file`) |
| GET | `/imports` | view | Stored imports (summaries) |
| GET | `/imports/{id}` | view | One import with rejected / duplicate rows (row number + reason) |
| DELETE | `/imports/{id}` | delete | Delete a stored import (audited) |
| POST | `/preview` | view | Combined, de-duplicated audience with per-recipient eligibility |

## Merchants
* Source: `accounts` with `roles` containing `merchant`, joined to `stores` by `stores.merchant_id == str(accounts._id)`.
* A merchant **qualifies** when the account `status` is `active` (missing = active, as elsewhere in the app), it has
  **at least one store with `status == "active"`**, its phone normalizes to a valid number, and (for city-scoped
  admins) at least one store is in their assigned cities. A merchant with several stores is **one** recipient.
* City / Area / State filters are **exact, case-insensitive** matches on the merchant's active stores
  (a merchant matches if any active store matches). `q` searches merchant name, store name and phone digits (>= 3 digits).
* `stores.state` is optional in the existing data; merchants whose stores have no state simply never match a State filter.
* City scope follows the dashboard rule: Super Admin or `assigned_cities` containing `*` = everything; a custom role
  with **no** assigned cities sees **no** merchants (fail closed).
* Matching custom numbers to merchants scans merchant accounts' `phone` (any stored format, normalized). The legacy
  `merchants` collection is not read.

## Custom numbers
* Bare 10-digit numbers starting 6-9 (and `0`/`91`/`+91`/`0091` forms) are Indian, as in Phase 1/2A.
* Any other number must be written with a leading `+` (or `00`) and country code; otherwise it is rejected with
  `international_needs_plus` (a bare non-Indian number is ambiguous, so it is never guessed).
* One canonical key per number (`91XXXXXXXXXX`, or E.164 digits). The same number written differently is a duplicate.
* Rejection reasons: `empty_phone`, `scientific_notation`, `invalid_characters`, `too_short`, `too_long`,
  `invalid_indian_mobile`, `international_needs_plus`, `invalid_format`.
* CSV only (UTF-8, comma / semicolon / tab). Limits: 2 MB, 20,000 rows; manual: 1,000 numbers. Header row with a phone
  column (`phone`, `mobile`, `mobile_number`, `whatsapp`, `number`, ...) and optional `name`; a header-less file is
  accepted only if its first cell is a number. Row numbers match a spreadsheet. A trailing `.0` is tolerated; Excel
  scientific notation is rejected with an explanation.
* **Consent columns are never used.** Columns such as `consent` / `opt_in` are reported in `ignored_columns` with the
  warning `consent_columns_ignored`; their values are not read or stored.
* Names are stripped of control characters and leading `= + - @` (spreadsheet-formula injection) and cut at 100 chars.

## Eligibility (preview)
Every unique recipient goes through `services/wa_eligibility.py` (Phase 2A) in batches - the Phase 1 consent store is
the only source of truth. Eligible = customer START, or complete admin-recorded evidence. An opt-out always wins.
Reasons: `unknown_consent`, `opted_out`, `consent_evidence_missing`, `invalid_number`, `consent_lookup_failed`
(database error -> nobody eligible, `consent_check_ok: false`), plus audience reasons `duplicate`,
`merchant_inactive`, `merchant_no_active_store`, `merchant_not_found`, `merchant_out_of_scope`.
Duplicate precedence: a merchant row wins over a custom row with the same number (`duplicate_of_merchant`); otherwise
the first occurrence wins (`duplicate_in_upload` / `duplicate_recipient`). A custom number that is a registered merchant
but whose merchant is **not** selected is kept and flagged `registered_merchant`.
Rows rejected at import are counted in `summary.rejected_at_import`, not listed as recipients.
**The preview is a snapshot**: eligibility must be re-checked immediately before each send (later phases).

## Storage lifecycle of imported audience data (`wa_audience_imports`)
| Data | Stored? | Lifetime |
|---|---|---|
| Uploaded file / raw cells | **No** - parsed in memory and discarded | - |
| Valid rows: `{row, canonical phone, sanitized name}` | Yes (needed by later campaigns) | Until an admin deletes the import |
| Rejected / duplicate rows | Row number + reason code only (raw value dropped) | Same document |
| Consent / eligibility | **No** - recomputed on every preview | - |
| Uploader identity, file name (sanitized), counts | Yes | Same document |
| Audit entries (`audience.import`, `audience.import_delete`) | Yes, **no phone numbers** | Audit retention (pending) |

There is **no automatic expiry yet** (no TTL index). Proposal for approval: delete an import automatically 30 days after
creation unless a campaign still references it (7 days is preferable for unused imports) - data minimisation (DPDP Act
2023 storage-limitation) versus the need to re-run a campaign; to be confirmed with counsel. Until then admins can delete
imports through `DELETE /imports/{id}`. The collection and its (default `_id`) index are created implicitly on first write;
nothing is created at startup, so a Production deploy changes no data.

## Examples
`GET /admin/whatsapp-campaigns/audience/merchants?city=Pune`
```json
{"items":[{"merchant_id":"6af8...","name":"Asha Bakery","phone_masked":"***3210","store_count":2,
  "stores":[{"store_id":"6af8...","store_name":"Asha Cafe","city":"Pune","area":"Kothrud","state":"Maharashtra","category":"Cafe"}],
  "eligibility":{"eligible":true,"reason":"eligible","consent_source":"customer_start","opted_out":false}}],
 "total":1,"page":1,"page_size":50,"pages":1,"universe":{"merchant_accounts":1}}
```
`POST /imports/csv` (file `phone,name,consent` / `9876543210,Asha,yes` / `abc,Bad,yes` / `9812345678,Ravi,yes`)
```json
{"import_id":"6af8...","kind":"csv","filename":"nums.csv","created_by":"Asha Admin",
 "counts":{"rows":3,"valid":2,"invalid":1,"duplicates":0,"registered_merchants":1},
 "ignored_columns":["consent"],"warnings":["consent_columns_ignored"],
 "problems":[{"row":3,"problem":"invalid","reason":"invalid_characters","message":"Contains characters other than digits, ..."}],
 "problems_total":1}
```
`POST /imports` body `{"entries":[{"phone":"9876543210","name":"Asha"},{"phone":"12"}]}` (or `{"phones":["..."]}`) returns the same shape.

`POST /preview`
```json
{"merchants":{"ids":["<account id>"],"filter":{"city":"Pune","area":"","state":"","q":""}},
 "imports":["<import id>"],"page":1,"page_size":50,"status":"all"}
```
`merchants.ids` and `merchants.filter` can be combined (union, no double counting; `filter: {}` = every qualifying
merchant); `status` is `all|eligible|excluded`; `page_size` max 200; audience max 20,000.
```json
{"summary":{"total":3,"eligible":1,"excluded":2,"by_reason":{"duplicate":1,"unknown_consent":1},
  "by_source":{"merchant":{"total":1,"eligible":1},"custom":{"total":2,"eligible":0}},
  "rejected_at_import":1,"duplicates_at_import":0,"consent_check_ok":true,"note":"..."},
 "recipients":[{"source":"custom","ref":"<import id>#2","name":"Asha","phone_masked":"***3210","eligible":false,
   "exclusion_reason":"duplicate","consent_source":"none","opted_out":false,"duplicate_status":"duplicate_of_merchant",
   "invalid_phone":false,"registered_merchant":{"merchant_id":"6af8...","name":"Asha Bakery"},"store_count":0}],
 "page":1,"page_size":50,"pages":1,"filtered_total":3}
```
Errors: `400 {"detail":{"code":"no_phone_column|not_csv|empty_file|file_too_large|too_many_rows|bad_encoding|empty_selection|audience_too_large|...","message":"..."}}`,
`404 import_not_found`, `403` missing permission, `503` database / audit unavailable (nothing stored).
