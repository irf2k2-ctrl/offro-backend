# WhatsApp Campaigns – Phase 2E (worker) + 2F (delivery tracking)

**Status: implemented and testable; every sending control is OFF by default.** Nothing in this change enables the worker, sends a
message, or touches Production. Phase 2G (dashboard) is not part of this change.

## 1. Architecture

```
scheduled ──(send_at reached + all gates)──▶ running ◀──▶ paused        running ──▶ completed | cancelled
                                               │
   worker tick (any replica):  recover abandoned → sweep cancelled → launch due → lease one campaign → claim a batch
                                               │
   per recipient:  claim ONE recipient → campaign running? + lease heartbeat → test-mode allow-list → rate token → consent/STOP re-read →
                   template payload → LIVE template check at Meta → FINAL gate (worker key, sending env key, DB kill switch,
                   live key, Production guard) → (a) send_started=true (conditional) → (b) atomic campaign lease verify+renew
                   (running + still ours + unexpired) → Meta call → record outcome
                                               │
   Meta webhook (Phase 1: signature, duplicate claim, retry) ──▶ wa_delivery.handle_status ──▶ recipient by message_id
```

* `services/wa_campaign_worker.py` (2E) – worker, run controls, recovery, rate limiter, config. Reuses `wa_campaign_manager`
  (campaigns, snapshot recipients, template payload), `wa_templates.verify_live`, `wa_eligibility.check`, `wa_campaign_settings`
  (kill switch), `wa_campaign_audit`, `whatsapp.send_whatsapp_template_checked`. No logic duplicated.
* `services/wa_delivery.py` (2F) – delivery handler + report numbers. Independent of the worker (recipients can be written directly in tests).
* `routers/wa_campaign_run.py` – run controls and read-only reporting. Sends nothing itself.
* The Phase 1 webhook is **unchanged**; `register()` appends the handler to its existing `_STATUS_HANDLERS`.

### Recipient states (one document per number in `wa_campaign_recipients`)
`snapshot → queued → sending → sent → delivered → read`; `failed` (rejected, retries exhausted, or Meta delivery failure);
`uncertain` (outcome unknown, never retried); `cancelled` (STOP / not a test number / campaign cancelled; `cancel_reason` kept).

### Exactly-once layers
1. atomic recipient claim `queued → sending` (`find_one_and_update`); 2. recipient lease; 3. `send_started` flag set conditionally
on the owner's claim, an **unexpired** recipient lease and `send_started=false`, followed by one atomic campaign-lease
verify-and-renew (campaign `running`, lease owner is this worker, lease not expired); if that check fails the flag is taken back
(conditional) and nothing is sent, and if it cannot be taken back, recovery marks the recipient `uncertain` (never resent); recovery of an expired lease re-queues only when `send_started` is false,
otherwise marks `uncertain`; 4. unique `(campaign_id, phone)`; 5. only the lease owner writes the outcome (a late owner may upgrade
`uncertain → sent` because it knows the message id); 6. campaign lease (one worker per campaign at a time).

## 2. Files
Added: `services/wa_campaign_worker.py`, `services/wa_delivery.py`, `routers/wa_campaign_run.py`,
`test_wa_campaign_worker.py`, `test_wa_delivery_tracking.py`, `tools/wa_staging_concurrency_check.py`, this document.
Modified (small): `routers/wa_campaigns.py` (mount 2 routers), `routers/wa_campaign_lifecycle.py` (list filter accepts
`running`/`paused`), `server.py` (startup: worker indexes, delivery handler registration, worker start **only if enabled**).
Phase 1–2D logic is untouched (`wa_campaign_manager`, consent, eligibility, settings, webhook are not edited).

## 3. MongoDB collections and indexes
Existing collections reused: `wa_campaigns` (new fields `run`, `run_config`), `wa_campaign_recipients` (new fields: `attempts`,
`next_attempt_at`, `lease_owner`, `lease_until`, `send_started`, `message_id`, `delivery_rank`, `status_ts`, `error_code`,
`cancel_reason`). One new collection: `wa_campaign_rate` (per-second counters, TTL 300 s).
Indexes: recipients `(campaign_id,state,next_attempt_at)`, recipients `(state,lease_until)`, recipients `message_id` unique+sparse,
campaigns `(status,run.lease_until)`, rate `at` TTL. Created at startup (new/sparse only, safe on Production).

## 4. Worker configuration (all OFF / conservative by default)
| Variable | Default | Meaning |
|---|---|---|
| `WA_CAMPAIGN_WORKER_ENABLED` | off | start the loop at all |
| `WA_CAMPAIGN_SENDING_ENABLED` + DB kill switch | off | Phase 2A pair, re-read before **every** message |
| `WA_CAMPAIGN_BULK_LIVE_ENABLED` | off | third key, needed only for `live` runs (default mode is `test`) |
| `WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION` | off | override of the Production refusal (`RAILWAY_ENVIRONMENT_NAME`/`APP_ENV`… = production) |
| `WA_CAMPAIGN_TEST_NUMBERS` | empty | test mode reaches only these numbers |
| `WA_CAMPAIGN_BATCH_SIZE` / `_CONCURRENCY` | 20 / 1 | clamped 1–200 / 1–8 |
| `WA_CAMPAIGN_RATE_PER_SECOND` | 5 | cluster-wide, clamped 1–80 |
| `WA_CAMPAIGN_MAX_ATTEMPTS` | 3 | confirmed-retryable failures only (1–5) |
| `WA_CAMPAIGN_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | 30 / 900 | exponential, ±20 % jitter |
| `WA_CAMPAIGN_LEASE_SECONDS` / `_POLL_SECONDS` | 120 / 15 | |
| `WA_CAMPAIGN_MAX_LATE_MINUTES` | 360 | longest a missed schedule may still start |

Operational controls: the final per-message gate re-reads the worker key, sending env key, DB kill switch, live key and Production guard; kill switch off ⇒ in-flight batch stops after the current message and the campaign is **auto-paused**
(resuming is a human `approve` action and is refused while sending is off); template no longer ready ⇒ auto-pause; Meta unreachable /
consent store down / Meta throttling ⇒ campaign back-off, nothing sent; `pause` / `cancel-run` need only `edit`.

## 5. Delivery webhook flow
Meta → Phase 1 `/webhook` (signature verified, event claimed once, retries bounded) → `_handle_statuses` → `handle_status`:
look up the recipient by `message_id`; apply the event with one conditional update (`delivery_rank < new` and `status_ts <= event`);
duplicates/older events are no-ops; `failed` stores only the error **code**; unknown ids are ignored (masked log); an id not yet saved
by the worker while the same number is `sending` raises `DeliveryPending` so Phase 1 returns 500 and Meta redelivers.

## 6. API (prefix `/admin/whatsapp-campaigns`, module "WhatsApp Campaigns")
| Route | Perm | Purpose |
|---|---|---|
| `GET /worker/status` | view | gate / config snapshot (no secrets) |
| `GET /campaigns/{id}/stats` | view | totals: total, queued, sending, sent, delivered, read, failed, cancelled, uncertain, accepted, pending_retry, cancelled_by_reason, consistent |
| `GET /campaigns/{id}/run` | view | run state + stats |
| `POST /campaigns/{id}/run-config` | approve | `{mode:"test"|"live", confirm:{acknowledge,mode:"live",eligible_count}}` on a scheduled campaign; audit-first |
| `POST /campaigns/{id}/pause` | edit | running → paused |
| `POST /campaigns/{id}/resume` | approve | paused → running (audit-first; refused while sending is off) |
| `POST /campaigns/{id}/cancel-run` | edit | running/paused → cancelled; queued recipients cancelled, records kept |
There is deliberately no "send now": a campaign starts only when its confirmed `send_at` arrives and every gate passes.

## 7. Security review (summary)
* Default OFF; four independent keys for a live bulk send; Production refused without an explicit override.
* STOP wins: consent re-read per message; unknown/opted-out/lookup-failure never sends; test mode cannot reach non-allow-listed numbers.
* Fail closed everywhere: gate, rate limiter, campaign state, template verification, audit (launch without audit ⇒ paused).
* Uncertain outcomes (timeouts, 5xx, sender exceptions, crashed worker after `send_started`) are never retried automatically.
* No full numbers in audit/logs (last-4 + hash); error text never stored; stats/status endpoints expose no numbers or secrets.
* Webhook security unchanged (Phase 1 signature + dedupe); handler is idempotent and ignores unknown ids.
* A campaign that missed its `send_at` by more than `WA_CAMPAIGN_MAX_LATE_MINUTES` (default 360) is never started late; it stays `scheduled` until an admin reschedules it.

## 8. Testing and the mongomock limitation
`test_wa_campaign_worker.py` and `test_wa_delivery_tracking.py` use a fake Meta/sender. mongomock does not make
`find_one_and_update`/`update_one` atomic across threads (a real MongoDB server does), so the thread tests run behind a lock shim that
models single-operation atomicity; they prove the application never splits a claim into check-then-write, **not** the server
guarantee itself. That is verified on a real server with the procedure below.

## 9. Staging concurrency verification procedure (real MongoDB, no WhatsApp)
Prerequisites: Staging only; never run against Production's database. 1) On a Staging shell with the backend checked out, export the
Staging connection string as `CONC_MONGO_URL` (do not paste it anywhere). 2) `python tools/wa_staging_concurrency_check.py
--yes-this-is-staging --processes 8 --recipients 2000`. It creates and drops its own database `wa_conc_check_<id>`; no application
collection is touched and no message is sent. 3) Expect five PASS lines (exact-once claims, single lease holder, rate ≤ limit, status
convergence to `read`, recovery split 5/5) and `RESULT: ALL PASS`; repeat 3×, and once with `--processes 16`. 4) Optional end-to-end
Staging drill (needs your explicit approval first): worker env on Staging only, kill switch on, `WA_CAMPAIGN_TEST_NUMBERS` = your own
number, schedule a 2-recipient campaign in test mode, watch `/worker/status`, `/campaigns/{id}/stats`, flip the kill switch mid-run and
confirm auto-pause, send STOP from the test phone and confirm the next message is cancelled with reason `opted_out`.

## 10. Follow-up safety corrections (applied on top of the original 2E/2F patch)
1. **Template readiness before every message.** `T.verify_live()` is called in `process_recipient()` for each message (after the
   consent check, before the final gate). Meta unreachable / any error ⇒ nothing is sent, the recipient is released with its attempt
   not counted, and the campaign backs off for 60 s (stays `running`). Template no longer ready ⇒ release + the campaign is auto-paused
   (`template_not_ready`). This costs one Meta GET per message (not a messaging call; it is separate from the send rate limit).
2. **Worker key is an immediate stop.** `WA_CAMPAIGN_WORKER_ENABLED` is re-read in the final gate with the sending env key, DB kill switch,
   live key and Production guard. Switching it off mid-batch sends no further message; the campaign is auto-paused
   (`kill_switch:worker_disabled`) and resuming is a human action.
3. **Campaign lease ownership and renewal.** One atomic `update_one` verifies **and** renews the lease: it matches only while the campaign is
   `running`, `run.lease_owner` is this worker and `run.lease_until` has not passed. It therefore cannot overwrite another worker's lease,
   cannot resurrect an expired one and cannot succeed after pause/cancel. It runs as a heartbeat at the start of every recipient and again
   immediately before every send. A worker that lost the lease releases its in-hand recipient (attempt not counted), stops, and does not
   touch the campaign (no auto-pause: the new owner continues). Recipients are now claimed **one at a time** just before they are handled, so a
   200-recipient batch never holds 199 claims that could expire; `send_started` also requires an unexpired recipient lease, so a recipient the
   recovery has already taken back can never be sent by the slow original owner.
4. **Concurrency script.** The rate check now waits for a common start instant and measures exactly `RATE_WINDOW_SECONDS` (3 s); the expected
   bounds come from one function (`rate_lower_bound`/`rate_upper_bound`: 10..20 for 5/s). The process runner checks child exit codes, treats a
   crashed child as a failed check, and terminates children after `RUN_TIMEOUT`; it can no longer hang.
5. **Cancellation / pause race.** Order is now: status check → … → conditional `send_started=true` (a) → atomic status+lease check (b) → send. A
   pause/cancel committed before (b) prevents the send (the flag is taken back); the only window left is after (b), i.e. the cancel is
   linearized after the send decision, and that single message is allowed. There is no read-then-write between (b) and the Meta call.
Tests: `test_wa_worker_safety_followup.py`.
