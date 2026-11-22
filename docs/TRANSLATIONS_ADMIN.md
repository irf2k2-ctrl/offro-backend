# Translation Management (simplified)

Workflow: **edit a translation -> its status becomes `modified` -> Publish Changes -> the translation is published on the server** (this environment's database, served by the public API below).

**Remote delivery to the Flutter app is NOT active yet in production.** Today only Flutter builds made with `ENABLE_REMOTE_TRANSLATIONS=true` **and** pointed at the exact staging host fetch published translations (Phase K, staging testing only); production builds do not fetch anything, so publishing in a production dashboard changes nothing in the production app. Updating app text **without a new APK or app build** becomes available for production only after the **Flutter build that enables remote translations for production (Stage 5, see the Flutter repo's `docs/REMOTE_TRANSLATIONS.md`) is released** to users. Until then, everything below describes what the server stores and serves, not what the app already shows.
There is no approval, review, promotion, package-transfer or rollback workflow.

## What exists
- **Admin API** (`/admin/translations`, `routers/translations_admin.py`):
  `GET /environment`, `GET /languages`, `POST /languages`, `PATCH /languages/{code}`, `GET /strings`, `GET /preview`,
  `PUT /strings/{lang}/{key}`, `POST /publish`, `GET /audit` (read-only, no dashboard tab), `GET /kill-switch`, `POST /kill-switch`.
- **Public API** (`routers/translations_public.py`): `GET /translations/manifest`, `GET /translations/{lang}?track=app|legal`
  (ETag/304; per-IP in-process rate limit, default 60/min, `TRANSLATION_PUBLIC_RATE_PER_MIN` 10-600; per-process only, not a distributed limiter).
- **Service** (`services/translations.py`): registry, validation, editing, publishing, kill switch, public data. Audit: `services/translation_audit.py`.
- **Dashboard** (`templates/partials/translations.html`, `static/js/translations.js`, `static/css/translations.css`): two sections only.
  **Overview**: languages, live version, number of Modified strings, completion, emergency kill switch, add language.
  **Translations**: search/filter, English read-only reference, edit Arabic, status Modified / Published, **Publish Changes**.

## Rules
- Only values are editable; keys come from the app (`tools/sync_translation_keys.py`, dry-run by default). English is the built-in read-only reference and is never stored or edited here.
- Plural keys are bundled-only and read-only. Deprecated keys cannot be edited.
- Validation (editing and publishing): plain text only (no HTML), no hidden/bidi-override characters, length limit, placeholders must equal the English set, no URL/e-mail/phone number the English text does not already contain.
- Super Admin = `role_name == "Super Admin"` AND `is_system_role == true` (a renamed role never qualifies). Only a Super Admin can add/change a language, publish, or use the kill switch. Other admins need the "Translations" module grant: `view` reads, `edit` saves values.
- **Publish Changes** writes a new immutable version (`translation_versions`: number + SHA-256 hash of the canonical JSON of the strings; `note` optional) to THIS environment's database and marks the published values `published`. A Flutter build with remote translations enabled detects changes by comparing the manifest's `version` and `hash`; versions are never overwritten or deleted. There is no version-history screen or endpoint.
- Tracks: `app` and `legal` are versioned and published separately. Legal text is edited and published like any other text (validation still applies).
  **Legal limitation:** the Flutter app currently reads ONLY the `app` track, so publishing legal text does **not** change what customers see, even in a build with remote translations enabled, until the app is changed to read the legal track.
- Kill switch (per environment, stored in that environment's own database): ON -> `/translations/manifest` reports `overrides_enabled:false`, so a build with remote translations enabled uses its bundled strings. Unreadable state fails closed (ON).
- Audit: append-only `translation_audit`, no TTL. A strict "started" entry is written before each sensitive operation (if it cannot be stored: 503 `audit_unavailable`, nothing changes), then `ok`/`failed`; denials are logged. Failure entries hold codes and key names, never translation text.

## Staging and production are independent
Staging and production run this same code and the same dashboard, and each reads and writes ONLY its own database. There is no promotion, synchronisation, package transfer or connection between them, and no translation code makes a network call to another environment.
Separation is guaranteed by **configuration**, not by anything in this code or its mock-database tests: each environment MUST run with its **own, independent `MONGO_URL`** (a different database or cluster) and its own `OFFRO_ENVIRONMENT` (`staging` or `production`). If both pointed at the same database they would share translations and no test would notice. Verify both values in each Railway service during production setup, before any production publish.
Translation data is copied to production only by a separate, one-time, manually run transfer: `tools/transfer_translations.py` (see `docs/TRANSLATIONS_TRANSFER.md`). It is never run by the server, never scheduled, and is a dry run unless `--apply` is given. The full release procedure is `docs/GO_LIVE_TRANSLATIONS.md`.

## Environment identification
- Set `OFFRO_ENVIRONMENT=staging` on the Staging service and `OFFRO_ENVIRONMENT=production` on Production (explicit, primary setting).
- Safety net: if the variable is missing AND `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID` and `RAILWAY_SERVICE_ID` all equal the known Staging ids, the service is treated as **staging**. Production is never inferred; a missing/invalid value anywhere else stays "unknown".
- While the environment is "unknown" every change is refused (503 `environment_unknown`) and the dashboard disables them; reads still work.
- Other languages are explicit Super Admin actions (Overview -> Add a language; "Fill in Arabic" pre-fills the form). Adding an existing code returns 409 and never duplicates or overwrites.
- Keys come from the app: with the Flutter `lib/l10n/app_en.arb`, run `python tools/sync_translation_keys.py <app_en.arb>` (dry run), then `--apply`. It only writes `translation_keys`; re-running is idempotent and never touches languages or translations.

## Old data that may still exist (not removed)
The retired workflows are removed from the code only; no database was modified. Existing databases may still contain `translation_rollbacks`, `translation_legal_reviews`, a `pending` entry in `translation_state`, and older fields (`submitted`, `approval`, `promoted_from`, `restored_from`) on values or versions. They are ignored by the code and harmless; remove them manually later only if you want to, after backing up. An old unfinished rollback claim, if one ever existed, no longer blocks anything.
