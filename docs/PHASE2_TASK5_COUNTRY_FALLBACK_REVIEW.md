# Phase 2 / Task 5 - Country resolution and India-fallback review

Status: **review and proposal only. No resolution behaviour was changed, no record was written.** Country Diagnostics is untouched and
still deployed. The behaviour below is pinned by `test_phase2_task5_country_resolution.py` so any approved change must update those tests on purpose.

## 1. Current resolution rule (`utils/countries.resolve_account_country`, the single implementation)
| Priority | Input | Result | `source` |
|---|---|---|---|
| 1 | stored `phone_country` is a supported ISO code (case-insensitive) | that country, even if the phone prefix disagrees | `explicit` |
| 2 | stored phone carries a calling code (`+`/`00`) of a supported country | that country | `parsed` |
| 3 | stored phone is a bare legacy Indian shape (`9876543210`, `919876543210`, `09876543210`) | India | `legacy_india` |
| 4 | anything else (bare 9-digit number, unsupported prefix, no phone) | none | `unresolved` |

An invalid stored `phone_country` (e.g. `XX`) is ignored and the phone is parsed instead. Diagnostics reports `invalid_phone_country`, `country_mismatch` (explicit country vs phone prefix) and `legacy_india`.

## 2. Silent / questionable India fallbacks found
| # | Where | Behaviour today | Risk | Priority |
|---|---|---|---|---|
| F1 | `utils/countries.py` rule 3 (also `dashboard_country.account_query` regex) | A BARE 10-digit number with no `phone_country` is India. A Philippine/Nepal mobile saved without `+` and without `phone_country` is therefore read as India. | Wrong country on the account; wrong dashboard country scope, wrong currency in public lists. Labelled `legacy_india`, so it is visible in Diagnostics, but not blocked. | **High** (only real mis-classification path; size unknown until Diagnostics counts are read) |
| F2 | `services/owner_country.py` (`iso`, `influencer_iso`, `owner_iso_of_banner`) | Owner not found, no owner key, or owner country unresolved -> **India** (INR display currency) | Stores/products/deals/banners of an unresolved owner appear in the India feed with INR. | **High** |
| F3 | `routers/public.py::_store_in_country` | Unresolved owner or any exception -> India | Same as F2 for the store-by-country filter. | High |
| F4 | `routers/admin.py` withdraw requests list | `country` missing -> `IN`, currency missing -> `INR`/`₹` | Correct for requests made before per-country rewards (all rupees); wrong only if a non-India request lacks its stamp. | Low |
| F5 | `services/country_context.stamp` | order/subscription/invoice without `currency_code` -> `INR`, `GST` | Same reasoning as F4: legitimate for pre-international records. | Low (Task 2 already avoids it for non-India payments in the dashboard) |
| F6 | `services/pricing_config.wallet_reward` | no country on the account -> India rewards | An unresolved account sees Indian reward amounts. | Medium |
| F7 | `services/areas.py` | same city text in several countries and no country sent (old apps) -> India areas only | Intentional: older apps are India-only. | Info |
| F8 | `routers/maps.py` Places text search | `regionCode: "IN"` hard-coded | Bias toward India for non-India searches. Not an account-country fallback. | Low |

Already safe: Stage 1 areas (unresolved owner not counted as India), Task 2 payments (unresolved stays blank, no INR for non-India), Task 4 country notifications (country required, never defaulted), country-restricted dashboard users (fail closed).

## 3. Recommended resolution priority (unchanged order, plus two guards)
1. Explicit valid `phone_country` (keep).
2. Calling-code prefix of the stored phone (keep).
3. Legacy Indian shape -> India **only for an account that has no `phone_country` AND whose number was stored before international rollout** (see proposal P1).
4. Otherwise **unresolved**. Unresolved is shown as "not known" and never receives India's currency, rewards or feed (proposals P2-P3).

## 4. Proposed changes - NOT applied, need your approval
**P1 - bare 10-digit numbers (F1).** Keep treating them as India for accounts that registered through the Indian flow, but stop assuming it for ones that can be shown otherwise. Exact proposal: no change to resolution; instead *register* every future account with `phone_country` (already done by `register_phone`), and let support correct the existing ones through the new Edit screen after reviewing the Diagnostics `legacy_india` list. Affected categories: accounts with `source=legacy_india` (count from Diagnostics). Zero behaviour change for them until corrected.

**P2 - unresolved owner in public lists (F2/F3).** Today: India. Proposed: unresolved-owner records are returned **only when the app did not send a country (older builds)**, and are excluded from any request that sends an explicit country, including India. Affected: stores, products, deals, banners whose owner account has `source=unresolved` or was deleted. Effect: those records disappear from the India feed of current apps and stay visible to older apps.

**P3 - unresolved account rewards (F6).** Today: India rewards. Proposed: `redeemable: False`, no amount (same as an unconfigured country). Affected: accounts with `source=unresolved` only.

**P4 - maps region bias (F8).** Pass the account/store country instead of the constant. Affected: none of the stored data.

Nothing here edits or migrates a stored record. P1 relies on manual correction through Task 3; no bulk update is proposed.

## 5. What to read in Country Diagnostics before deciding
`unresolved` count, `legacy_india` count (F1 exposure), `country_mismatch` list, `invalid_phone_country` list. If `legacy_india` contains non-Indian numbers, P1 is urgent; if `unresolved` is non-zero, P2/P3 matter.
