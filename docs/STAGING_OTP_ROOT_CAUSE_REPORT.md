# Staging OTP not received on Indian mobile numbers - investigation report

Status: **code-level investigation complete; the failing step is NOT yet identified.** No MSG91 screenshot or delivery report reached the investigation
(the 22 Nov 2026 dashboard screenshot was not attached), there are no MSG91 credentials in this environment, and outbound access is limited to package
registries and GitHub, so no OTP request was sent and no provider response was observed. Nothing below claims a delivery result. No OTP code or configuration was changed.
Reviewed: backend `b10cb5f`, Flutter `e13b01b` (staging).

## 1. How the app's OTP actually travels
```
Flutter (login_screen.dart)                    MSG91 OTP widget (provider)                 Staging backend
  checkPhoneInput -> '+<dial><national>'  ──────────────────────────────────────────────>  POST /user/check-phone (and /register)   (no OTP involved)
  OTPWidget.sendOTP({'identifier':'91XXXXXXXXXX'}) ──> MSG91 creates the request, picks channel/template/sender from the WIDGET's own settings
  <── {type:'success', message:<reqId>}  = request ACCEPTED
  OTPWidget.verifyOTP({reqId, otp}) ──────────────> MSG91 verifies
  after success ────────────────────────────────────────────────────────────────────────>  POST /user/account-login (phone only)
```
* **The staging backend is not in the OTP send path for app users.** Flutter talks to MSG91 directly through the widget SDK. The backend's `MSG91_AUTH_KEY` /
  `MSG91_TEMPLATE_ID` / `MSG91_SENDER_ID` (routers/otp_service.py) and `MSG91_WEB_WIDGET_*` (services/msg91_widget.py) are used only for the **admin dashboard**
  login (SMS 2FA / web widget). They cannot affect whether an app user receives an SMS.
* The widget id and token are compiled into the app (`_kWidgetId`, `_kTokenAuth` in login_screen.dart). They are public client values, identical in staging and
  production builds: staging does **not** use a different widget, template or sender from production. (No value is reproduced here.)
* Rate limiting / repeat-request throttling for app OTPs is done by MSG91 (the backend has none for this flow), which fits repeated requests for the same number in the log.

## 2. Checklist from the task, with what the code proves
| Question | Finding |
|---|---|
| Correct auth configuration / route | App uses the MSG91 **mobile widget** (OffroOTP) via the SDK, not the backend. Backend creds are irrelevant to app OTP. |
| Country code and number format | Correct: digits-only E.164 without '+', e.g. `91` + 10 digits (`phone.substring(1)` of the validated number). India keeps the unchanged 10-digit rule. |
| Template / sender ID | Not in code. They live in the **MSG91 widget's** SMS template/channel settings. Cannot be read from the repo. |
| DLT / operator restrictions | Cannot be determined from code. India SMS needs a DLT-registered sender (header) and an approved, linked template; otherwise MSG91 accepts the request but the operator drops/rejects it. The in-repo test tool marks India's expected first channel as SMS and the other four countries as WhatsApp. |
| Provider status (submitted/delivered/failed/rejected/pending) | Not observable by the app or backend; only in MSG91's dashboard / delivery report. |
| Staging vs production configuration | Same widget (compiled constant). Staging-only difference: a temporary **master OTP `1234`** accepted only on the exact staging host (staging_gate.dart). It skips verification, not sending. |
| Rate limited / blocked | Possible on MSG91's side (repeated requests to one number). Not visible from code. |
| Backend handling of MSG91 errors | N/A for app OTP. For the admin flow (otp_service.py) errors are handled, but see the logging note below. |
| Does Flutter show success when the provider rejected? | **Partly.** `sendOTP` is treated as success only when `type == 'success'`; any other reply shows a friendly error. But `success` only means *MSG91 accepted the request*. The app then opens the code screen even if the SMS is later rejected by DLT/the operator or never delivered - that is the symptom you see. |

## 3. Most likely location of the fault (NOT confirmed)
The request reaches MSG91 (it appears in the dashboard) and the app got `type: success`; so the failure is **after acceptance**: SMS template / DLT / sender-ID /
operator delivery, or account balance / DND / throttling, configured in the MSG91 widget and account - **not in Flutter or the staging backend code**.
This is an inference from the code path, not a finding from provider data.

## 4. What is needed to confirm (MSG91 dashboard, no code change, no production change)
1. Open the failing request (or the OTP/SMS delivery report for that number & time). Read the **delivery status and the error/description** for each of the repeated requests: submitted, delivered, failed, rejected or pending, and the failure reason (e.g. DLT template mismatch, sender/header not approved, DND, insufficient balance, operator rejection).
2. In the OTP **widget settings** for the widget used by the app: confirm the SMS channel is enabled for India, the **DLT template id** and **sender/header** are the approved ones, and the template text matches what was approved (a changed text is rejected by operators).
3. Confirm the account has SMS balance and that India SMS is permitted on this widget/account.
4. Confirm whether the repeated requests were throttled (same number requested several times in a short time).
5. Test with one approved test number only, with a single request, and compare the dashboard status with what arrives on the handset.
Send me the delivery status/error text (redacted) from steps 1-2 and I will turn this into a definitive root cause and, if a code change is needed, the exact fix.

## 5. Recommended fix (provisional)
* If step 1 shows a DLT/template/sender/balance/operator reason: **dashboard/configuration change only** (no code).
* Optional hardening, only if you approve it afterwards (not applied): word the code-screen message as "We have requested your code" instead of implying delivery, and show the raw provider text through `friendlyError` on verify failures (`login_screen.dart` shows `resp['message']` verbatim today).

## 6. Other observations (not changed)
* `routers/otp_service.py` prints the destination number and, when MSG91 keys are missing, the generated OTP, and logs the full MSG91 response and headers. Production should have the keys set (so no OTP is printed) but these logs contain phone numbers; consider removing them.
* `/user/account-login` identifies the user by phone alone: the OTP check is done in the app. A server-side verification of the MSG91 access token would be the secure fix; this is a separate, larger change.
* `services/msg91_widget.identifier_for()` assumes India (`91`) - admin login only.

## 7. Safety
No OTP value, API key or token is printed or stored in this report or in the generated files. No OTP was sent. No production variable, template or sender was touched.
