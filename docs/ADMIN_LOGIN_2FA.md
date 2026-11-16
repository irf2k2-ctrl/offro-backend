# Admin login: server-side 4-digit SMS OTP (security fix)

Base: staging `636fb1b`. Adds no Railway variables and no credentials: SMS uses the existing `MSG91_AUTH_KEY`,
`MSG91_ADMIN_TEMPLATE_ID` (else `MSG91_TEMPLATE_ID`) and `MSG91_SENDER_ID` through `routers/otp_service.send_sms_otp`.

## The vulnerability this closes
`POST /admin/auth/verify-otp` used to accept `{"mobile": "<admin mobile>", "otp": "<any 4 digits>", "msg91_verified": true}` and returned a
full admin session cookie: no PIN and no OTP were needed. The same code was on `main` (Production). The browser also held the MSG91 widget
credentials in the public page, the OTP was written to the server log in clear text, stored unhashed, and a "trusted device" cookie
skipped the OTP.

## New flow
1. `POST /admin/auth/login {mobile, pin}`: PIN + lockout checked exactly as before. Success returns `{otp_required, challenge_id, mobile_masked,
   otp_expires_in: 300, resend_available_in: 30}` only. No cookie, no session token is written.
2. `POST /admin/auth/verify-otp {challenge_id, otp}`: server-side check. Only here is `admin_token` created.
3. `POST /admin/auth/resend-otp {challenge_id}`: a new OTP for the same challenge.

## Rules
| Rule | Value / behaviour |
|---|---|
| OTP | exactly 4 digits, `secrets` CSPRNG |
| Storage | salted PBKDF2-SHA256 hash bound to challenge id + account id; the plain OTP and the challenge token are never stored or logged |
| Expiry | 5 minutes per issued OTP; a challenge lives at most 15 minutes including resends |
| Single use | the challenge is consumed with one atomic update; exactly one concurrent verifier wins; replay fails |
| Wrong attempts | 5 per challenge, shared across resends (resend never resets them); then the challenge is locked and a new PIN login is needed |
| Resend | 30 s cooldown, max 3 resends per challenge, claimed atomically (concurrent resends send one SMS) |
| SMS rate limits | 5 per mobile number per hour, 10 per client IP per hour (first send and resends) |
| Other IP limits | 30 PIN-step requests and 30 verify/resend requests per IP per 15 minutes |
| Account lockout | unchanged (5 wrong PINs, 15 minutes); unknown numbers get the same lockout response |
| Trusted device | superseded in Patch C by hash-only, opt-in "Remember this device" (see the Patch C section). The old plain-token `trusted_devices` records are ignored |
| Everyone | Super Admin included: OTP is mandatory |
| Errors | one generic message for unknown number / wrong PIN / disabled account; one generic message for wrong, expired, used or unknown OTP; unexpected errors return a fixed friendly text, never exception text |
| SMS failure | 503 "We could not send the verification code", challenge marked `send_failed`, no session, SMS provider text never shown |
| SMS not configured | fails closed (no dev-mode "OTP in the logs" fallback any more) |

Client IP for limits: the right-most `X-Forwarded-For` entry (added by Railway's edge), else the socket address.

New collections (created with indexes at startup, TTL-purged): `admin_login_challenges`, `admin_auth_throttle`. `admin_otp_sessions`
(old plain-text OTPs) is no longer written; existing rows expire by their TTL.

## Operations
* Nothing to change in Railway. `MSG91_*` must already be set on the service (it was needed before). If they are missing, login now stops at the
  PIN step with the friendly SMS error instead of printing the OTP to the log.
* The MSG91 widget ID/token that was in `templates/admin_login.html` is no longer served, but it remains in git history and was public:
  rotate/disable that widget in MSG91 when convenient (the customer app uses the same widget, so coordinate before rotating).
* Rollback: revert the patch; nothing else depends on the new collections.

## Legacy admin authentication retired (Patch B)
* `POST /admin/login` (username/password, no PIN, no OTP) now always answers **410** with a pointer to `/admin`; it reads no data, creates no
  session and sets no cookie. The route still exists so old clients get a clear answer.
* The `admins.token` fallback was removed from every active consumer: `get_current_admin` (`routers/admin.py`), `get_current_dashboard_user`
  (`routers/dashboard_auth.py`), `routers/wa_chat.py`, `routers/popup_campaigns.py`, `/download/admin_dashboard` and `/admin/dashboard`
  (`server.py`). Only `dashboard_users` sessions (created by the PIN + OTP sign-in) authenticate; roles, permissions and the inactivity timeout
  are unchanged.
* `seed_admin()` no longer creates `admin` / `admin123`; `seed_rbac()` no longer creates a default Super Admin (`9999999999` / `123456`,
  `RBAC_SUPER_ADMIN_*`) and prints no credentials. Existing accounts and roles are never touched. On an EMPTY database create the first Super
  Admin deliberately: `python tools/create_super_admin.py` (PIN prompted, not echoed, never printed or logged).
* **No data was deleted or modified and no token was revoked.** Legacy `admins` rows and tokens stay in the database but no longer authenticate.
  Anyone still signed in with a legacy session is signed out of the admin tools and must sign in again with PIN + OTP.
* Not changed (separate decisions): the unmounted legacy files (`routers/admin_dashboard.py`, `routers/categories.py`, `auth.py`,
  `routers/temp admin.py`, `seed.py`) still contain old password-login code, but none is mounted in `server.py`; the CSP still allows
  `connect-src https://control.msg91.com`; the customer app verifies OTPs on the device and the backend trusts that (`/user/account-login`).

## Separate: the original "Server error. Please try again." (still UNRESOLVED)
This patch does not fix it and must not be reported as fixing it. The old message came from the page's catch-all `catch`, reached by (a) a
network failure or non-JSON reply from `/admin/auth/login`, or (b) the browser's own call to MSG91. This patch removes (b) from the page
and shows HTTP status in the message for non-JSON replies, so the next occurrence is distinguishable, but the cause is unproven.
Needed to establish it:
* Browser DevTools > Network, on a failed login: for `POST /admin/auth/login` the status, `content-type` and response body (first lines);
  for the old build also any request to `control.msg91.com` and its status; Console error text.
* Railway logs for the same minute: any traceback, `[2FA]` / `[OTP_SERVICE]` lines, and Railway HTTP log status for the request
  (502/504 vs 200/401/500). Redact mobile numbers.

## Staging-only fixed test OTP (no SMS on Staging)
Staging has no MSG91 configuration. For allow-listed admin numbers on the *verified* Staging service the challenge is created normally (after the PIN
is verified) but its OTP is the fixed code `1234` instead of a random one, and no SMS is sent. The code is stored as the usual salted hash bound to
the challenge and the account, so `/admin/auth/verify-otp` is unchanged and every protection still applies: 5 min expiry, 5 attempts, single use,
30 s resend cooldown and 3 resends (a resend re-hashes `1234` with a fresh salt), rate limits, no cookie before success. A test-mode challenge is
refused (never accepted) if test mode stops verifying. There is no retrieval endpoint, key or HMAC. Code: `services/admin_otp_test_mode.py`.

**Test mode is active only if ALL pass, checked on every call (any miss, mismatch or error = inactive = real MSG91 path):**
1. `ADMIN_OTP_STAGING_TEST_MODE` is `1/true/yes/on`.
2. Railway's `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID`, `RAILWAY_SERVICE_ID` equal the Staging constants in `services/admin_otp_test_mode.py`.
3. `RAILWAY_PUBLIC_DOMAIN` is exactly `offro-backend-staging-d375.up.railway.app`.
4. Every host in `MONGO_URL` is `mongodb.railway.internal` (the separate Staging MongoDB).
5. The admin number is in `ADMIN_OTP_TEST_MOBILES`.
The environment *name* is not used (the Staging environment was once named `production`). Do not define variables named `RAILWAY_*`.

**Production:** has different IDs, domain and database host, so a copied flag does nothing; Production always uses real MSG91 and never accepts `1234`.
**A fixed code is public:** on Staging the PIN is the only real factor. Keep the allow-list short and never copy real Production PINs to Staging.

**Staging Railway variables (only these two; Staging only, never Production):**
`ADMIN_OTP_STAGING_TEST_MODE=1` and `ADMIN_OTP_TEST_MOBILES=<comma-separated admin numbers>`.
Startup logs `[2FA] admin OTP test mode: ACTIVE|INACTIVE (<code>)`. Codes: flag_off, not_on_railway, project_mismatch, environment_mismatch,
service_mismatch, domain_mismatch, database_unverified, database_mismatch, allow_list_empty, check_error.
If the Staging service is ever recreated, update the constants (test mode stays off until then).

## Patch C: Remember this device and login limits

**Remember this device.** Unticked: PIN + SMS code every time. Ticked: the first sign-in uses PIN + code, then the browser
receives an `offro_td` cookie (HttpOnly, SameSite=Lax, Secure over HTTPS, path `/admin/auth`, 30 days). Later sign-ins
from that browser need the PIN only. The server stores only a SHA-256 hash of a 256-bit random token, bound to the admin.
The tick box is a request, never proof: a device is registered only after a successful OTP verification.
Revoked automatically on PIN change/reset, mobile or role change, deactivation, deletion; also via logout (`keep_trusted=false`)
and the device list endpoints. Super Admin is excluded unless `ADMIN_TRUSTED_DEVICE_SUPER_ADMIN=1`.
Optional variables: `ADMIN_TRUSTED_DEVICE_DAYS` (default 30, 0 = off, max 90).

**Login limits.** Only failures count. SMS sends are tied to their challenge and released when it completes, and are not
counted at all in Staging test mode (no SMS is sent). Wrong PIN: per mobile+IP 5 free then 1/2/5/15/30 min; per account
ceiling at 15; per IP ceiling at 30. Wrong OTP: per account 10, and per IP 20 failures. Abandoned sends: 10/account and
20/IP per hour. A correct PIN clears the pair counter; a completed sign-in or PIN reset clears the account counters.
Root cause of the lockouts: every PIN-stage login consumed the shared SMS budget (5/hour per mobile, 10/hour per IP) and
nothing released it, even in test mode.
