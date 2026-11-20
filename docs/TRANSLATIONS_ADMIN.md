# Translation Module – Backend (Phase G)

Scope: data model + admin API (T1), public API + kill switches (T3), HMAC-signed promotion package. No dashboard tab, Excel, Flutter loader (Phases H/I).

## Rules
- Only values are editable; keys come from the app (`tools/sync_translation_keys.py`, dry-run by default). Real registry stays empty until localization creates `app_en.arb`.
- Plural keys: bundled-only, read-only. Legal text: separate track, separate versions, review of the exact value hash required before publish.
- Super Admin (all privileged ops) = `role_name == "Super Admin"` AND `is_system_role == true`. A renamed role never qualifies.
- Other admins need explicit "Translations" module grants (none by default); they edit drafts and submit; they cannot publish/approve/rollback/export/import/toggle kill switch.
- Production: no direct edits, no new languages, no direct rollback (403). To roll back production: roll back in staging, approve the new version, export, import, then explicit publish. Emergency lever: production kill switch. Content arrives only as a signed package from staging.
- Audit: append-only `translation_audit`, no TTL. Strict "started" entry before each sensitive op (failure -> 503 `audit_unavailable`, nothing changes). Denials logged.

## Promotion flow
1. Staging: edit -> publish -> `POST /admin/translations/approve-for-production` -> `GET /admin/translations/package?lang=&version=` (signed with staging `TRANSLATION_PACKAGE_SECRET`).
2. Production: `POST /admin/translations/package/import` -> verifies kind/schema, source=staging, hash, approval, signature (with `TRANSLATION_PACKAGE_VERIFY_SECRET`), key/value rules. Creates a **DRAFT only**; nothing goes live.
3. Production: separate explicit `POST /admin/translations/publish`.

Design note: HMAC is symmetric, so production must hold staging's secret to verify. Resolution: production has its OWN `TRANSLATION_PACKAGE_SECRET` (seals pending imports, different from staging's) plus `TRANSLATION_PACKAGE_VERIFY_SECRET` (= staging's value, verify-only; cannot be used to publish since production publish also needs a pending sealed record). Identical own/verify secrets are refused.

## Kill switch
`POST /admin/translations/kill-switch` per environment (stored in each environment's own DB). ON -> `/translations/manifest` reports `overrides_enabled:false` and the app uses bundled strings. Unreadable state fails closed (ON).

## Public API
`GET /translations/manifest`, `GET /translations/{lang}?track=app|legal`. ETag/304; per-IP in-process rate limit (default 60/min, `TRANSLATION_PUBLIC_RATE_PER_MIN` 10-600). **Per-process only, not a distributed limiter.**

## Staging setup checklist (two HMAC secrets)
1. Generate two independent secrets: `python -c "import secrets;print(secrets.token_urlsafe(48))"` (run twice). Never reuse, never commit.
2. Staging service: `OFFRO_ENVIRONMENT=staging`, `TRANSLATION_PACKAGE_SECRET=<S1>`.
3. Production service: `OFFRO_ENVIRONMENT=production`, `TRANSLATION_PACKAGE_SECRET=<S2>`, `TRANSLATION_PACKAGE_VERIFY_SECRET=<S1>`.
4. Each as a Super Admin (system role): `GET /admin/translations/environment` – confirm environment, `ready` flags, and fingerprints: staging own == production verify; production own != production verify.
5. Secrets must be >=32 chars, >=12 distinct characters, no placeholder words. Missing/weak -> export/import disabled (503).
6. New empty collections/indexes are created at first startup after deploy.
7. Grant "Translations" permissions to non-Super-Admin roles via the roles API (dashboard role editor UI arrives in Phase H).
8. Legal review is not re-required on production import (staging version was already reviewed and approved).

## Dashboard tab (Phase H)
Admin Dashboard -> Translations (`templates/partials/translations.html`, `static/js/translations.js`, `static/css/translations.css`). It only calls the APIs above.
- Sidebar entry and tab are visible to roles with the **Translations -> view** permission (Super Admin always). The role editor now lists the Translations module, so grants no longer need the API.
- Overview: per language and track, published version, draft (next version, number of changes), missing keys, awaiting-review / pending imported package, last updated (from `GET /languages`).
- Translations: search/filter keys, English source, edit and save drafts (edit permission, staging only), plural keys locked, mark drafts ready for review, publish (Super Admin).
- Versions: history, approve for production, roll back (new version), export signed package (staging only).
- Promotion & safety (Super Admin): secret readiness/fingerprints, import package as draft (production only), pending packages, kill switch.
- Audit: recent translation audit entries.
- Production: no edit, add-language, approve, rollback or export controls are shown; "Publish imported package" appears only while an imported package is pending. The server enforces all of this regardless of the UI.
- Not in this phase: Excel import/export, Flutter localization and loader.

## Environment identification and first-time setup (fix after Phase H)
- Set `OFFRO_ENVIRONMENT=staging` on the Staging service and `OFFRO_ENVIRONMENT=production` on Production. This is the primary, explicit setting.
- Safety net: if the variable is missing AND `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID` and `RAILWAY_SERVICE_ID` all equal the known Staging ids, the service is treated as **staging**. Production is never inferred; a missing/invalid value anywhere else stays "unknown" and every change is refused. The dashboard shows the reason (never ids or secrets).
- While the environment is "unknown" the dashboard disables every change; the server refuses them too (503 `environment_unknown`).
- English is the app's built-in source language: it is shown as a read-only base row and is never stored. Other languages are explicit Super Admin actions (Overview -> Add a language; "Fill in Arabic" pre-fills the form). Nothing is seeded automatically; adding an existing code returns 409 and never duplicates or overwrites.
- Keys come from the app: with the Flutter `lib/l10n/app_en.arb` run, in a Staging shell (needs the staging database), `python tools/sync_translation_keys.py <app_en.arb>` (dry run), then `--apply`. It only writes `translation_keys`; re-running is idempotent and never touches languages or translations. The Phase I ARB (72 keys) and its draft Arabic are covered by tests.
