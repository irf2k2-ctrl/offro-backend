# WhatsApp Campaign Manager - Phase 2A foundations

Phase 2A adds safety infrastructure only. **No campaign can be created, no template can be synced and no message is
sent by this code** (the only message paths that exist are the Phase 1 webhook replies and the existing single-recipient
template test, which now enforces opt-out).

## What exists

| Piece | Where |
|---|---|
| RBAC module **"WhatsApp Campaigns"** (view / add / edit / delete / approve / export) | `routers/dashboard_auth.py` `ALL_MODULES` |
| Admin API (5 routes under `/admin/whatsapp-campaigns/`) | `routers/wa_campaigns.py` |
| Audit trail `wa_campaign_audit` (append-only) | `services/wa_campaign_audit.py` |
| Kill switch, worker flag, test-number allow-list, safe config snapshot | `services/wa_campaign_settings.py` |
| Admin-recorded consent with evidence | `services/wa_consent.py` `record_admin_consent` |
| Centralized eligibility (`check`, `check_many`) | `services/wa_eligibility.py` |
| Opt-out enforcement in `POST /admin/whatsapp/template-test` | `routers/wa_template_test.py` |

## Permissions
* The module is **not granted to any existing or seeded role** except Super Admin (seeded Admin / Read Only roles are
  explicitly denied). Existing role documents have no key for it and a missing key means denied.
* Grant it deliberately in the Super Admin role editor after deploy. Suggested split: `view` = read config/audit and
  check eligibility; `edit` = turn the kill switch OFF; `approve` = record consent, turn sending ON.
* Legacy username/password admin sessions are treated as Super Admin by the existing dashboard auth; that is unchanged.

## Environment variables (all default OFF / empty; none is required)
| Variable | Meaning |
|---|---|
| `WA_CAMPAIGN_SENDING_ENABLED` | First key for sending. **Never set on Production** until bulk sending is approved. |
| `WA_CAMPAIGN_WORKER_ENABLED` | Reserved for the Phase 2E worker. Off by default. |
| `WA_CAMPAIGN_TEST_NUMBERS` | Allow-list (comma/space separated, `+` and country code for non-Indian numbers) of numbers that may receive TEST messages. Empty = nobody. |

Sending needs BOTH the environment key AND the runtime switch (`wa_campaign_settings`, `_id: "global"`). The switch is
OFF when the document is missing, and any read error means OFF. Turning it OFF always works; turning it ON needs the
env key, the `approve` permission, and an audit entry written first.

## Admin-recorded consent
Required: valid phone, `source` (signup_form, written_form, email, in_person, phone_call, whatsapp_optin_message, other),
`evidence_ref` (>= 6 chars), `consented_at` (ISO-8601, not in the future), `attested: true`. The administrator's identity
is taken from the session. It is audited before it is applied. It **never overrides an opt-out** (checked up front AND by
a conditional write, so a concurrent STOP also wins); only the customer's own START can restore consent. A later
customer STOP always applies (the record carries `consent_seq` 0). The same evidence submitted twice is one ledger row.

**International numbers must be entered with `+` and the country code.** A bare 10-digit number starting 6-9 is read
as an Indian mobile (Phase 1 rule); opt-outs are still checked under both readings.

## Eligibility (what every future campaign path must call, again immediately before each send)
Eligible only if: valid number, no opt-out under any storage variant, `opted_in` with complete evidence (a customer START
with its WhatsApp message id, or admin evidence with source, reference, time, attestation and admin id). Unknown consent,
opted-in-without-evidence, invalid numbers and any database error are NOT eligible.

## Checking which Meta account Staging uses (no secrets needed)
`GET /admin/whatsapp-campaigns/config` (needs `view`) returns booleans and the last 4 characters of the phone-number id and
business-account id. Compare them with the Staging WhatsApp Business Account in the Meta dashboard; they must differ from
Production's. Also shown: signature mode, whether the verify token is still the default, test-number count, kill-switch state.
Whether approved templates exist on that account is checked in Phase 2C (template sync).

## Retention - PROPOSAL ONLY, nothing is deleted and no TTL index is created for Phase 2 data
Periods are a business/legal decision; the reasoning below is operational, not legal advice - please confirm with
counsel (India's DPDP Act principles of purpose and storage limitation apply, as do Meta's opt-out rules).

| Data | Why it is kept | Risk of keeping | Risk of deleting | Proposal |
|---|---|---|---|---|
| `wa_contacts` (current consent state) and `wa_consent_events` (ledger) | It is the **suppression list** and the proof of consent. Deleting an opt-out can lead to messaging someone who said STOP. | Holds a phone number, last-4 and evidence text only. | Re-contacting opted-out people; no proof in a dispute. | **No automatic expiry** while OffrO may message the number; review annually. (Phase 1 behaviour, unchanged.) |
| `wa_campaign_audit` | Accountability: who approved/sent/paused what. Contains no phone numbers or message text. | Low (identities and counts). | Cannot investigate or answer Meta/regulator questions. | 24 months (covers an annual audit cycle plus a dispute window). |
| Per-recipient campaign records (Phase 2D+) | Match delivery callbacks, reconcile uncertain sends, answer a complaint. | Phone numbers per campaign. | Cannot reconcile or evidence a send. | 12 months in full, then keep only aggregated counts (or hashed numbers). |
| Uploaded file rows and rejected rows (Phase 2B) | Only needed until validation and confirmation finish. | Highest: names/numbers of people who never consented. | Cannot re-check a disputed rejection. | 30 days maximum (7 would be safer if no one needs rejected-row review). |
| `wa_webhook_events` | De-duplication (Meta retries for up to 7 days). | Low. | Duplicate processing after expiry. | 30 days (already in place). |

Operational notes: TTL deletion is irreversible and runs in the background (about once a minute); the TTL indexes should be added
only once the periods are approved, and Production gets them through a normal reviewed deploy. Reports must keep working
after recipient rows expire, so campaign-level aggregates are stored separately.

## Not done in 2A (by design)
Audience selection and CSV upload (2B), template sync and sender (2C), campaign lifecycle and test send (2D), worker (2E),
delivery tracking (2F), admin UI (2G).
