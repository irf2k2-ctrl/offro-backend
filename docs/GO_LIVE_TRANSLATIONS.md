# Go-live checklist: dashboard-managed translations in production

Nothing in this checklist has been run. Every step that touches Railway, the production database, a production build or a store
release needs the owner's separate approval. Tick each box in order; stop at the first failure.

Facts this plan rests on
* Backend: FastAPI + MongoDB (`database.py` uses the database name `offro_db` on whatever server `MONGO_URL` points to).
* Staging and production share **no** data, sync or API. Production gets staging's translations **once**, by the manual transfer.
* After the Flutter production build is released, a Publish in the production dashboard reaches the app without a new APK.
* Legal-track text is stored but not delivered to the app.
* The app uses conditional requests (If-None-Match / 304) per environment, in addition to manifest version + SHA-256 hash validation.

## A. Before anything: owner decisions
- [ ] **Production API host confirmed (currently UNVERIFIED).** The only place it is configured in the app is
      `kRemoteProductionHost` in `lib/core/l10n/remote/remote_translation_config.dart` (value
      `offro-backend-production.up.railway.app`, copied from a comment on line 10 of `lib/core/constants/app_constants.dart`). The
      repo does not record the real production `API_BASE_URL` (it is a build-time `--dart-define`; `codemagic.yaml` builds iOS against
      staging and the Windows release script passes no define). Verify:
      1. Railway -> production service -> Settings -> Networking: the public domain the live app uses.
      2. The exact `API_BASE_URL` of the store build (your build notes/CI), optionally from the released APK:
         `unzip -p app-release.apk lib/arm64-v8a/libapp.so | strings | findstr /i railway.app`.
      3. `https://<host>/translations/manifest` returns JSON (`schema: 1`) and that backend's dashboard Overview says **production**.
      (The backend code also lists this same host as a default in `services/security.py`, `routers/admin.py`, `routers/wa_chat.py`
      and `services/admin_otp_test_mode.py`; that is supporting evidence, not proof.)
      If it differs, change the constant and the pinned value in `test/remote_translation_environments_test.dart`.
- [ ] Stages 4-6 reviewed and approved; the backend and Flutter changes committed and merged by you.

## B. Railway configuration (staging and production, each checked separately)
- [ ] **Two independent databases.** Staging and production `MONGO_URL` point to DIFFERENT MongoDB servers/clusters. Because the code
      always uses database name `offro_db`, two URLs on the same cluster would share one database. Compare the host part of the
      two values by eye in Railway's variables panel (do not paste them anywhere).
- [ ] Staging service: `OFFRO_ENVIRONMENT=staging`, its own `MONGO_URL`.
- [ ] Production service: `OFFRO_ENVIRONMENT=production`, its own `MONGO_URL`.
- [ ] No variable in production references the staging database or host (and vice versa).
- [ ] Both services deployed from the intended commit (the one containing Stages 1-6) and healthy.
- [ ] Open each dashboard -> Translations -> Overview: staging shows **staging**, production shows **production** (also
      `GET /admin/translations/environment`). If either shows unknown, STOP: writes are refused until it is fixed.
- [ ] Production kill switch is OFF but understood (Overview -> kill switch). Super Admin accounts exist in both environments.

## C. Staging verification (backend)
- [ ] `python -m pytest test_translations.py test_translations_environment_fix.py test_translations_dashboard_ui.py test_translations_dashboard_layout.py test_translations_transfer.py -q -W ignore` -> 194 passed.
- [ ] In the staging dashboard, Arabic is complete: edit -> status Modified -> Publish Changes -> new version; `GET /translations/manifest`
      on staging shows `languages[ar].tracks.app` with the expected version and hash.
- [ ] Super Admin only: a non-Super-Admin cannot publish or toggle the kill switch (403); the audit endpoint shows the publish.

## D. Flutter staging build verification
- [ ] Build: `tools\build_staging_apk.cmd` (flag ON, staging host). Install on a test phone.
- [ ] Switch the app to Arabic. Publish a visibly changed string in the staging dashboard; reopen/resume the app: the new text shows
      **without a new APK**.
- [ ] Airplane mode + cold start: app opens normally, shows the last cached text, or the bundled Arabic if never downloaded.
- [ ] Kill switch ON in staging dashboard -> app returns to bundled Arabic after the next check; OFF -> published text returns.
- [ ] English screens unchanged. Arabic is right-to-left.
- [ ] OTP (staging master code 1234 still works on this build), login, registration and role selection behave as before.
- [ ] ETag: with a proxy or logs, a second check sends `If-None-Match` and gets 304 (no language download); after a publish it gets 200.
- [ ] Network logging (optional): only `offro-backend-staging-d375.up.railway.app` is contacted for `/translations/*`.
- [ ] `dart format --set-exit-if-changed lib test`, `flutter analyze` and `flutter test` all pass on your machine (they were not run in the delivery environment).

## E. Production transfer (separate approval required)
- [ ] Approval given in writing for the transfer.
- [ ] Staging content final: nothing left Modified that should be published (staging Overview).
- [ ] Export from staging (read-only): see `docs/TRANSLATIONS_TRANSFER.md` step 1. Keep `transfer.json` private.
- [ ] **Dry run** against production: step 2. Read the before/after table: every language/track shows `nothing published -> vN`,
      "No conflicts." If it reports a conflict or a refusal, stop and investigate; never force it.
- [ ] Review and approve the dry-run output.
- [ ] Apply: step 3 with `--apply --confirm-target-db offro_db`. Expect `APPLIED` and the counts from the dry run.
- [ ] Re-run the dry run: everything is "already identical", 0 to insert.
- [ ] Delete `transfer.json` and any shell variables holding connection strings.
- [ ] Production dashboard: Translations shows Arabic with all strings Published; `GET <production>/translations/manifest` returns the
      **same version and hash** as staging's for the `app` track. The production kill switch is still OFF.
- [ ] Production's own key registry stays in sync with the app's ARB (`tools/sync_translation_keys.py`, dry run first) whenever a release adds keys.

## F. Production Flutter release (separate approval required)
- [ ] Production host verified (A).
- [ ] Build: `tools\build_production_apk.cmd https://<verified-production-host>` (or the equivalent `flutter build` with
      `--dart-define=ENABLE_REMOTE_TRANSLATIONS=true`), keeping your normal signing and razorpay patch steps.
- [ ] Smoke-test that exact build before upload: Arabic shows the production text; flight-mode start works; login/OTP (real SMS,
      master code **not** accepted), registration, role selection work.
- [ ] Release through your normal store process. Remote translations are live for users only once they have this build.

## G. Final production verification
- [ ] In the production dashboard make one harmless edit to a non-critical Arabic string -> Publish Changes -> v(N+1).
- [ ] On the released production build: the change appears without any update/APK. Revert the text the same way (new version).
- [ ] Toggle the production kill switch ON then OFF once and watch the app follow (bundled -> published).
- [ ] Audit trail (Overview/`GET /admin/translations/audit`) shows the transfer, the publishes and the kill switch events.
- [ ] Staging is unaffected by anything done in production (compare staging manifest version before/after).

## H. Rollback
* Instant: production kill switch ON (apps fall back to bundled text at their next check; nothing is deleted).
* Bad text: correct it and Publish (versions are append-only; there is no overwrite).
* Bad app build: stop the rollout in the store; the previous build keeps working (the feature is off in builds without the flag).
* The transfer itself is insert-only; if it must be undone, that is a manual database operation to be planned separately and needs approval.
