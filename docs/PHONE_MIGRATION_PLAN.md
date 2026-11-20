# International phone numbers: audit and migration plan (Phase C)

Status: PROPOSAL. Nothing here has been run. No database record has been read or changed by this work. Each step needs separate approval.

## 1. Canonical format and coexistence

- Canonical format is **E.164** (`+<country code><national number>`), produced by `utils/phone.py` (libphonenumber via the `phonenumbers` package).
- Existing field `phone` is **never rewritten** in the first steps. Every existing app version and every existing query keeps working on it.
- New optional fields are added **beside** it: `phone_e164`, `country` (ISO code), `language`. A record without them is a legacy record and is still found through the legacy lookup keys.
- New accounts write both: `phone` (kept in the form old code expects for Indian numbers, `+91XXXXXXXXXX`; E.164 for other countries) and `phone_e164`.

## 2. How existing Indian accounts keep logging in

- Old apps send `+91` plus 10 digits and no country. The server treats a request without a country as Indian (`legacy_india=True`).
- `utils.phone.lookup_keys_for(raw, country, legacy_india)` returns, for India, exactly the formats the old code searched: `+91N`, `91N`, `N`, `0N`. Tests pin this equal to the existing `_phone_variants`.
- Indian-shaped numbers that fail modern validation (old test accounts such as `5551234567`) are still found through `legacy_india_keys`, so no existing account becomes unreachable.
- Other countries match their exact E.164 (`+966...` and `966...`) only. A bare national number is never a key for a non-Indian country, so a Nepal or Philippines number can never match an Indian account, and the reverse.

## 3. Step 0: read-only audit (approval needed to run)

`python tools/audit_phone_formats.py` (staging first). It only reads, prints counts and masked samples (never full numbers), and reports per collection (`accounts`, `users`, `merchants`, `influencers`, `stores.merchant_phone`, `wa_contacts`, `customers_whatsapp`):

1. how numbers are stored (bare 10 / 91+10 / +91+10 / 0+10 / other countries / invalid);
2. Indian-shaped numbers that fail validation;
3. duplicate numbers inside one collection (the unique index only stops identical strings, so `9876543210` and `+919876543210` can coexist);
4. overlap between collections (expected: accounts, users and merchants mirror each other);
5. look-alike pairs (same national digits in two different countries).

Take a backup export of the collections before anything else.

## 4. Handling duplicates and conflicts (never automatic)

- Duplicate Indian records are listed and left alone. An administrator decides per case. No merge is performed by any script.
- A login that matches more than one account signs nobody in, shows a neutral "contact support" message and logs the case for an admin.
- A new international number whose digits equal an Indian legacy number is a different account (matching requires the same country).
- Registering an already-registered E.164 returns the existing account's normal response, with no second account.

## 5. Backfill (separate approval, staging first)

1. Dry run: for each record whose `phone` classifies cleanly, compute `phone_e164` and `country`. Write nothing. Print counts of would-update, ambiguous and skipped.
2. Skipped and ambiguous records (invalid numbers, duplicate groups) are listed for an administrator and not touched.
3. Apply on staging in small batches, only to records without `phone_e164`, with `$set` of the new fields only. `phone` is not modified.
4. Verify: old-format logins, new-format logins, counts match the dry run.
5. Repeat on production only after your explicit approval and a fresh backup.
6. Rollback: `$unset` the new fields. Because `phone` was never changed, nothing else is needed.

## 6. Uniqueness (last)

Only after the audit shows no duplicates (or they are resolved by an administrator) and the backfill is complete, add a unique partial index on `phone_e164`. Not before.

## 7. Where the helper gets wired in (Phase D, not Phase C)

`routers/users.py` (check-phone, register, account-login, influencer paths), `routers/merchants.py`, `routers/merchant_app.py`, `routers/admin.py`, `server.py`, `services/wa_consent.py`, `services/wa_audience.py`, `services/msg91_widget.py`, `services/admin_otp_test_mode.py`, `routers/otp_service.py`, `routers/wa_template_test.py`. In Phase C none of these files is changed.

Note for WhatsApp: `services/wa_consent.normalize_phone` still treats a bare 10-digit number starting 6 to 9 as Indian. That is correct for Indian data but wrong for a bare Nepal or Philippines number. Phase D must pass the known country to it. A test (`test_wa_consent_still_guesses_for_bare_numbers_documented_hazard`) records today's behaviour.
