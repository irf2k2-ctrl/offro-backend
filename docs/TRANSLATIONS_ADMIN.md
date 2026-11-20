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
