# Pricing & Plans -> Flutter app integration: audit and plan (phase 1)

Status: audit + one additive read-only API. No checkout, Razorpay, merchant, influencer or wallet code was changed.
Base: staging 06a6f60. Production untouched.

## Who pays what (current behaviour, all India-only)
There is one Flutter app with customer, merchant and influencer roles. End customers pay OffrO nothing. The OffrO platform charges are paid by:

| Charge | Who | Backend (reads India `pricing` doc, INR hard-coded) | Flutter |
|---|---|---|---|
| Store subscription plans | merchant | `GET /merchant/plans` (public, no country), `POST /merchant/subscribe` (merchant_app.py ~724-950; order currency "INR" ~885) | `ApiService.getPlans` -> merchant_screens.dart |
| Banner charges | merchant | `GET /merchant/banners/pricing` (~1541), banner order (INR ~1764) | `ApiService.getBannerPricing`, merchant_screens.dart ~1148-1435 |
| Product day price / free window / max validity | merchant | `services/products.py` + `routers/merchant_products.py` (INR at ~139 and ~483) | merchant_screens.dart, products_phase2.dart |
| Influencer subscription | influencer (user app) | `users.py` ~743-1030 (INR at ~764, 977, 997, 1030) | my_influencer_profile.dart, `getInfluencerSubscriptionPricing` |
| Reward conversion | customer wallet | `users.py` ~315, ~1185 (`conversion_rate`, `min_withdraw_points`) | wallet_page.dart (out of scope) |

Merchant product purchases are offline; nothing in the app collects them. No Offro Pay / payout / wallet payment code was added.

## Country detection today
There is no stored country on merchant/user documents. The country comes from the phone number (`utils/phone.py`, `ApiService.customerCountry` / `countryIsoForPhone`). The server must derive the pricing country from the authenticated account's phone, not from a client-sent value.

## Gaps found
1. Every pricing endpoint above reads only the India document; none accepts or derives a country. A non-India account would be shown and charged India's INR price and GST.
2. All Razorpay orders are created with `"currency": "INR"`.
3. Flutter hard-codes the rupee sign (about 60 occurrences: merchant_screens.dart 36, main.dart 6, my_influencer_profile.dart 5, products_phase2.dart 5, store_products_section.dart 6, product_share_card.dart 2, wallet_page.dart 2) and the label "GST".
4. `lib/main.dart` holds a `rzp_live_...` key id constant (`kRazorpayKey`). Screens already prefer the key returned by the server; the constant is a fallback. A key id is not a secret, but the fallback should be reviewed.
5. Repo root contains `key.properties` and `upload-keystore.jks`. Please confirm they are not real signing secrets in version control (not touched here).
6. No API existed that returned one country's prices, tax and "can this country pay?" in one call.

## Added in this phase (additive, read-only)
`GET /country-pricing/{ISO}` (routers/public_pricing.py), no login, no writes. Returns that country's currency, tax (label, percent, tax-free, configured), store plans with tax and total, influencer fee, banner price, product price + free window + max validity, and `payment.supported`.
- Only the requested country is used. Unknown or inactive country -> 404; invalid code -> 422; missing tax -> `tax.configured:false` with no totals; unset prices -> `configured:false`. India is never substituted.
- `payment.supported` is true only for India (INR). Other countries return `reason: international_payments_not_enabled`.

## Decisions / Razorpay items needed from you (not assumed)
- Is International Payments enabled on the single Indian Razorpay account, and in which currencies? Razorpay requires activation for non-INR; settlement and compliance rules apply. Until confirmed, non-India accounts can see configured prices but must not reach checkout.
- Business rule for a non-India merchant meanwhile: block checkout with a clear message, or offer a manual/offline activation? 
- GST/VAT invoice format for non-India countries (numbering, tax label wording, legal entity) is a business/accounting decision.
- Influencer in a non-India country: charge in local currency (needs Razorpay approval) or keep India-only?

## Proposed next steps (each needs your approval)
1. Server: resolve the account country from its phone; make `/merchant/plans`, `/merchant/banners/pricing`, product and influencer pricing return that country's values; return `pricing_not_configured` / `payment_not_supported` errors instead of India's values. India path unchanged and covered by tests.
2. Server: make the order currency come from the country config only when payments are supported; otherwise refuse before creating an order.
3. Flutter: replace the rupee sign and "GST" with the server's `currency_symbol` and tax label; show "pricing not available" / "payments not available in your country" states; keep India identical.
4. End-to-end test of each flow on staging with a test Razorpay key.
