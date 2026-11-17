> **Superseded in part (Widget patch):** real admin sign-ins no longer send the SMS from this server. They use the existing MSG91 OTP Widget
> (OFFROOTP) driven by the login page and verified by the server through `verifyAccessToken`. See "MSG91 Widget login" at the end of this file.
> The challenge, attempt, resend, rate-limit, trusted-device and Staging test-mode rules below still apply.

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

## MSG91 Widget login (replaces the SendOTP-API route)

Why: the customer app already sends OTPs through the MSG91 OTP Widget (OFFROOTP), which needs no separate DLT template/sender for the OTP SMS.
Admin login now uses the same widget, independently of the customer app (nothing in the customer app, its endpoints or the MSG91 widget settings changes).

Flow:
1. `POST /admin/auth/login` (mobile + PIN). Wrong PIN: nothing is started. Correct PIN: a challenge (`provider: "widget"`) is created and the response carries
   `challenge_id` and `provider`. No OTP exists on the server and no SMS is sent by it.
2. The page calls `POST /admin/auth/otp-request` with the challenge id. The server answers with the identifier (`91` + the ACCOUNT's own mobile, derived
   from the challenge, never from the browser) and the PUBLIC widget config. The first call is the send; later calls are resends. The 30 s cooldown,
   3-resend cap and SMS budget are enforced here.
3. The page loads `https://verify.msg91.com/otp-provider.js` (`exposeMethods: true`) and calls `window.sendOtp(identifier, ok, fail)` /
   `window.retryOtp(null, ok, fail)` / `window.verifyOtp(code, ok, fail)`.
4. On a correct code the widget returns an access token. The page posts `{challenge_id, access_token}` to `POST /admin/auth/verify-otp`. A wrong code is
   reported to `POST /admin/auth/otp-failed`, which can only spend attempts (5 locks the challenge).
5. The server calls `POST https://control.msg91.com/api/v5/widget/verifyAccessToken` with `{authkey, access-token}` and requires, in this order: the challenge is a pending,
   unexpired widget challenge of an active account; an attempt is reserved atomically (cap 5); MSG91 confirms the token (HTTP 200 with `type:"error"`, `code 201`,
   non-JSON, anything unclear = refused); the verified number equals this account's number exactly; the token is not older than the challenge nor expired;
   the token has never been accepted before (`admin_widget_tokens`, sha256, unique index plus an explicit check); the challenge is consumed atomically.
   Only then `_issue_session` creates the session and cookie. A browser-side "verified" flag, a typed code or a token for another number never works.
6. MSG91 unreachable, or a confirmed token that does not say which number it was for: HTTP 503 with a reference id, NO attempt is used, NO session.

Configuration (Railway variable NAMES; set per environment, values stay in Railway):
- `MSG91_WEB_WIDGET_ID` (the OffroWeb widget id), `MSG91_WEB_WIDGET_TOKEN_AUTH` (the web widget's PUBLIC tokenAuth, sent to the browser by design). The mobile widget
  (`MSG91_WIDGET_ID`/`MSG91_WIDGET_TOKEN_AUTH`, OffroOTP) belongs to the Flutter app and is never used for the website: MSG91 answers "Web requests are not allowed for this widget" otherwise. No fallback.
- `MSG91_AUTH_KEY` (server key, already present; used ONLY for verifyAccessToken, never sent to a browser, never logged). If MSG91 IP security is on for this key,
  Railway's outbound IP must be allowed or verification answers "auth" (logged as a clear 503).
Any of the three missing: login answers 503 with a reference; there is NO fallback code.

Staging and Production:
- Staging test mode (fixed code 1234) is unchanged: only for allow-listed numbers on the verified Staging service, `provider: "local"`, no widget involved.
- Production can never use it (all five identity checks still fail closed) and `REAL_PROVIDER` is a code constant, not configuration. To test the real widget on
  Staging use an admin that is NOT in `ADMIN_OTP_TEST_MOBILES`.

Known limits (honest list):
- The browser talks to MSG91 directly, so the server cannot count wrong codes itself. The 5-attempt cap is enforced for honest clients (the page reports each failure);
  a hostile client's real brake is MSG91's own attempt limit on the widget. Check that limit in the MSG91 dashboard.
- `verifyAccessToken`'s reply format is not documented in the material available. `services/msg91_widget.parse_verified_identifier` looks in the reply, then in the token's
  claims, and refuses to sign in if neither names the number. The first Staging run logs the reply SHAPE (key names and types only) to settle this.
- The login page's CSP (only that page) allows `*.msg91.com` and common captcha hosts. Confirm in the browser console on Staging.
- OTP length/expiry are widget settings. The page has four boxes; if the widget issues 6 digits, set the widget to 4 or the page needs six boxes.

Page rule (regression, "OTP not provided in verifyOtp() method"): the login page must never declare a function named `sendOtp`, `retryOtp`, `verifyOtp`,
`getWidgetData` or `isCaptchaVerified`. The SDK installs those names on `window`; a page function with the same name made the Verify button call the SDK with no
argument. The page's own handler is `submitOtp()`; it reads the code from the four on-page boxes (exposeMethods hides the SDK's own UI, so there is no Shadow DOM
input to read) and passes it as a string to the SDK method captured after `initSendOTP`. `test_security_admin_login_browser.py` runs the real page in headless
Chromium against a stand-in SDK (needs `pip install playwright` + Chromium; skipped when absent); static equivalents run everywhere.
