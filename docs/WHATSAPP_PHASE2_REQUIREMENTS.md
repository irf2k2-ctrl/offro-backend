# WhatsApp Campaign Manager — Phase 2 MANDATORY requirements (not implemented in Phase 1)

Phase 1 delivers the consent store (`wa_contacts`, `wa_consent_events`), webhook signature verification, STOP/START handling
and webhook de-duplication. **No campaign collections, no bulk-send code and no campaign endpoints exist yet.**
Phase 2 must not be approved unless every item below is implemented and tested.

1. **Check consent immediately before every send.** Every campaign recipient is checked against `wa_contacts`
   (`services.wa_consent.may_send_marketing` / `consent_status`) at the moment of sending — not only when the audience is
   built. A STOP that arrives while a campaign is running must stop further messages to that person.
2. **Only explicit opt-in may receive marketing.** Contacts that are `opted_out`, contacts with **unknown consent** (no
   `wa_contacts` document — which includes every existing merchant, customer, influencer and Live Chat contact) and
   **invalid phone numbers** are excluded. Only `status == "opted_in"` is eligible. Consent is never inferred from
   `users`, `merchants`, `accounts`, `customers_whatsapp` or from the ledger.
3. **Fail closed.** If the consent database cannot be read (`consent_status` raises), nothing is sent. No retry-as-send,
   no "send anyway", no cached allow-list.
4. **`POST /admin/whatsapp/template-test` must enforce the same check** before sending (it currently relies only on the
   admin's `consent_confirmed` flag and note). An opted-out or unknown-consent number must be refused, including in
   `dry_run`, with the same fail-closed behaviour.
5. **One canonical phone key.** Audience building, sending and consent checks all use
   `services.wa_consent.normalize_phone` (`+91…`, `91…`, `0…` and 10-digit Indian mobiles → `91XXXXXXXXXX`); invalid
   numbers are excluded rather than stored under a second key. Never compare raw phone strings.
6. **Read consent only from `wa_contacts`.** `wa_consent_events` is an append-only audit ledger and may lag or disagree
   after a partial write; it is never used to decide eligibility.
7. **Monitoring.** Alert on log lines containing `[WA-CONSENT-FAILURE]` (a STOP/START could not be saved) before enabling
   any campaign send.
