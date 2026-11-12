# WhatsApp webhook: signature verification, STOP/START, de-duplication (Phase 1) — STAGING setup

Applies to the **Staging** Railway service only. Do not set any of this on Production until Staging is verified
and you have approved the Production rollout separately.

## New environment variables

| Variable | Value | Notes |
|---|---|---|
| `WHATSAPP_APP_SECRET` | the Meta **App Secret** | Meta App Dashboard → App settings → Basic → App secret. This is **not** the access token. |
| `WHATSAPP_WEBHOOK_SIGNATURE_MODE` | `off` (default when unset/empty) \| `log` \| `enforce` | Case-insensitive, trimmed. **Any other non-empty value (a typo such as `enforced`) is treated as `log`, never `off`**, and an ERROR is logged once. |

Nothing else changes: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_BUSINESS_ACCOUNT_ID` and
`WHATSAPP_VERIFY_TOKEN` (including its existing fallback) are untouched. The Meta webhook URL/subscription is untouched.

> **Never paste the App Secret into chat, a ticket, or a commit.** Copy it from the Meta dashboard straight into the
> Railway variable field. Surrounding whitespace/newlines are ignored by the code.

## Modes

* `off` — today's behaviour: no verification.
* `log` — every POST is verified; the result is logged (`Signature check (log mode): ok`, or the reason
  `mismatch` / `header_missing` / `secret_missing`). **Requests are always accepted.** Payloads are never logged.
* A request body larger than 1 MB is rejected with HTTP 413 in every mode (before the signature is checked).
* `enforce` — a POST with a missing/invalid `X-Hub-Signature-256`, **or with no `WHATSAPP_APP_SECRET` set**, gets HTTP 403
  and is not processed (fail closed). Meta retries rejected deliveries, so a wrong secret in `enforce` stops Live Chat
  intake: that is why `enforce` is only turned on after `log` has been verified.

## Rollout (Staging)

1. Merge/deploy this change to **Staging** (your decision; nothing is deployed by the assistant).
2. In Railway → Staging service → Variables, add `WHATSAPP_APP_SECRET` (paste from Meta) and set
   `WHATSAPP_WEBHOOK_SIGNATURE_MODE=log`. Railway restarts the service.
3. **Do not change the Meta webhook callback URL or subscription.** In this Offro setup Meta points to Production,
   and there is no separate staging-connected test number. Staging will therefore receive no real Meta message/status
   callbacks; an empty Staging signature-log search is expected and is NOT a failure.
4. Verify Staging deployment health and run the automated signature/consent/webhook test suite. These tests cover
   valid and invalid HMAC signatures using the exact raw request bytes, missing headers/secrets, STOP/START handling,
   retry behaviour and Live Chat compatibility. They are code-level tests, not proof of live Meta traffic.
5. For the Production rollout, keep `WHATSAPP_WEBHOOK_SIGNATURE_MODE=log` initially and set the correct Meta App Secret
   in the Production Railway service. Do not enable `enforce` yet. The existing Production callback remains unchanged;
   live requests will then produce signature-check logs in Production while remaining accepted.
6. Review real Production signature logs for `ok` and investigate any `mismatch`, `header_missing` or `secret_missing`.
   Only after sufficient clean real traffic and your explicit approval should `enforce` be considered. Enabling it with
   an incorrect/missing secret can stop Live Chat intake.
7. Rollback at any time: set the mode back to `log` or `off` in Railway (no code change or redeploy needed).

## Responses and retries

* `200` processed, or deliberately ignored (non-JSON, duplicates, retries exhausted). `413` oversized. `403` enforce + bad signature.
* `500` = at least one message could not be processed (Live Chat/database failure, or a STOP/START failure notice that could not
  be delivered). Meta retries the delivery; messages already handled are skipped as duplicates and each message gets at most
  one automatic reply. A message is attempted at most 3 times, then answered with 200.
* `500` is also returned when another worker holds a live 120-second lease on the same event (a concurrent duplicate
  delivery) - that is expected; the retry finds the event finished. Status-callback handler failures are retried too
  (only the handlers that had not succeeded are re-run; handlers must be idempotent).
* Automatic replies: only a definitive, temporary Meta rejection (HTTP 429, error codes 4 / 80007 / 130429 / 131056)
  frees the reply slot for a retry. Timeouts, connection loss, 5xx and unparseable responses are kept as `uncertain` and
  are NEVER resent automatically; permanent rejections are recorded and not retried.
* Meta's documented behaviour (developers.facebook.com WhatsApp webhooks overview): a delivery not answered with 200 is
  retried "with decreasing frequency ... for up to 7 days"; the exact intervals are not published and duplicates are
  possible. Reliability here does NOT depend on that schedule: claims are idempotent and bounded (3 attempts).
* **Crash-recovery limitation.** If a worker dies mid-message, nothing re-drives the event on its own: it is recovered only
  when Meta redelivers it after the lease expires (a redelivery inside the lease gets 500 "busy"). If Meta's retries stop or
  attempts are exhausted the event is marked `abandoned` (`[WA-EVENT-ABANDONED]`) and not processed. A background sweeper
  arrives with Phase 2E. A crash after a reply slot is taken leaves it `sending`, so that one reply is never resent
  (a possibly-missing welcome/confirmation is preferred to a duplicate). A zombie worker's LIVE CHAT insert is not
  token-protected (`whatsapp_chats.whatsapp_msg_id` is not indexed and Live Chat is untouched in this phase).
* If the database is down for a long period Meta will keep retrying and may eventually disable the webhook subscription;
  monitor for it.

## Monitoring (add alerts before relying on STOP handling)

* `[WA-CONSENT-FAILURE]` (ERROR) — a STOP/START could not be saved. The user was told "We couldn't process your request.
  Please reply STOP again." (or the START equivalent); no confirmation was sent. `wa_webhook_events` holds a
  `consent_failed` marker where the database allowed it.
* `[WA-CONSENT-INVALID-TIMESTAMP]` (ERROR) — a STOP/START carried a timestamp more than 5 minutes ahead of server time. Consent
  was NOT changed. A START gets no reply; a STOP gets "We couldn't process your request. Please reply STOP again." Repeated
  hits from one number may be a forged request (check signature mode) or a skewed server clock.
* `[WA-EVENT-ABANDONED]` (ERROR) — an event stalled past its lease and exhausted its attempts; it was not processed.
* `[WA-REPLY-UNCERTAIN]` / `[WA-REPLY-REJECTED]` (ERROR) — an automatic reply's delivery could not be confirmed (not
  resent) / was refused by Meta (code only).
* `Invalid WHATSAPP_WEBHOOK_SIGNATURE_MODE` (ERROR) — the mode value is misspelt; verification is running in `log` mode.
* `Signature check ... REJECTED / mismatch / header_missing / secret_missing`.

## Testing STOP / START (use a number you own)

* Send `STOP` (also `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT`): you receive one unsubscribe confirmation (no welcome),
  the message still appears in Live Chat, and `wa_contacts` gets `status: opted_out` plus a `wa_consent_events` row.
* Send `START`: you receive one re-subscription confirmation; `wa_contacts` becomes `opted_in` with the message id and
  timestamp stored as evidence.
* Whole-message matching only: "please don't stop the offers" is an ordinary message and gets the normal welcome.
* If the consent record cannot be saved, **no confirmation is sent**; the user gets the neutral notice above, the message
  is still in Live Chat, and `[WA-CONSENT-FAILURE]` is logged. An older START/STOP can never overwrite a newer one
  (ordered by the WhatsApp timestamp; STOP wins a same-second tie).
* Phone numbers are stored in one canonical form: `+91…`, `91…`, `0…` and 10-digit Indian mobiles all become
  `91XXXXXXXXXX`; invalid numbers are rejected; other international numbers keep E.164 digits.

## Data notes

* Campaign sending is NOT part of Phase 1. See `docs/WHATSAPP_PHASE2_REQUIREMENTS.md` for the mandatory consent rules.
* New collections, created lazily; indexes are created at startup (failure to create them does not stop the app):
  `wa_contacts` (unique `phone`), `wa_consent_events`, `wa_webhook_events` (30-day TTL).
* Existing merchants, customers and Live Chat contacts are **never** imported or marked opted-in. A number appears in
  `wa_contacts` only when that person themselves sends a STOP-type keyword or START.
* De-duplication uses the Meta message id as the document `_id`, so a retried delivery is ignored even if the extra
  indexes were not created.

## Verify token in access logs

`GET /webhook?hub.verify_token=...` is logged by Uvicorn's access log. A filter on the `uvicorn.access` logger replaces the
token value with `***`. It does not apply if the service is started with an access logger other than `uvicorn.access`
(e.g. Gunicorn's own access log) - check the Railway start command. Rotating the token requires changing Meta's webhook
configuration and is not done here.
