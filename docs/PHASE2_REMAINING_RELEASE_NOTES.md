# Phase 2 remaining work - release notes (D2, Tasks 2-5)

## D2 - login proof
* `/user/account-login` and `/merchant/login` now require proof: an MSG91 widget access token (`otp_proof`), or an existing session token (switch-mode / restore).
  A phone alone returns **426 "Please update the OffrO app..."**. Proof is verified server-side with MSG91 (`verifyAccessToken`), must name the same number, is fresh (10 min) and single-use (`login_proofs_used`, TTL 24 h).
* **Verify on Staging with a real widget token before production**: the server relies on MSG91 returning the verified identifier. If it does not, login fails closed (503).
* Staging test code `1234`: only when `LOGIN_OTP_STAGING_TEST_MODE=1`, Railway staging identity matches, and the number is in `LOGIN_OTP_TEST_MOBILES` (comma-separated digits). Otherwise staging testers use a real OTP.
* All currently shipped app builds get 426 as soon as this backend is deployed; release the Flutter build first or together.
* OTP delivery and the OTP screen are unchanged.

## Task 2 - Payments
`GET /admin/invoices/full | /admin/merchant-transactions | /admin/subscriptions` accept `?country=IN|JO|NP|PH|SA|NONE` and add `country`, `country_name`, `country_source`, `currency_code`, `currency_symbol`, `currency_source`. `GET /admin/payments/country-report` (read-only counts) lists historical rows without stored country/currency. Amounts are never converted. Revenue is shown per currency.

## Task 3 - Account edit (dashboard only)
`PUT /admin/accounts/{id}/edit-name`, `POST /admin/accounts/{id}/phone-change`, `GET /admin/accounts/{id}/phone-changes`, `POST /admin/phone-changes/{id}/approve|reject|resend|verify|cancel`. New collection `account_phone_changes`. Permission: Accounts > edit, country/city scope; approval: Super Admin other than the requester. The number is applied only by `verify` with the WhatsApp code; the account's session token is cleared so it must sign in again. Audit: `account.name_change`, `account.phone_change_*` in `security_audit` (masked numbers + digest, never the code).

## Task 4 - Notifications
New target `country` = every account (customers and merchants, i.e. the `all_users` population) of the selected country; country required. `all_users`, `city`, `specific`, `offers` unchanged. `dry_run` returns counts only; the dashboard shows the count and asks to confirm before sending.

## Task 5
Review and proposals only: `docs/PHASE2_TASK5_COUNTRY_FALLBACK_REVIEW.md`.

## Database changes
New collections: `login_proofs_used` (TTL index `at`), `account_phone_changes` (two indexes). New fields written only by the edit flow: `accounts.previous_phones`, `phone_changed_at`, `name_edited_at`. No migration, no backfill, no historical record modified.
