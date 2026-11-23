# Phase 3 - Country-based pricing and international payment (backend + Flutter)

Baseline: backend `origin/staging` 148fbb4, Flutter `origin/staging` 0446265. Nothing committed, pushed, merged or deployed. Production untouched.

## How it works
* The server resolves the country from the signed-in account (`phone_country`, else the stored phone). A client-supplied country is ignored.
* India (and a legacy account whose country cannot be resolved) uses the existing India pricing path, unchanged.
* Any other country reads its own Pricing Countries record. Missing country/price/tax gives HTTP 409 with `{code, message}`
  (`pricing_not_configured`, `country_inactive`, `tax_not_configured`, `price_not_configured`). There is no fallback to INR or India prices.
* Orders, subscriptions and invoices carry `currency_code`, `currency_symbol`, `tax_label`.
* Payment route per order: Razorpay only if keys exist AND the order currency is listed in env `RAZORPAY_INTERNATIONAL_CURRENCIES`
  (comma separated, empty by default = no international currency assumed supported). Otherwise `pay_mode = "manual"` with
  `manual_reason` (`currency_not_enabled` / `payment_gateway_not_configured`) and the existing admin-confirmed activation path.
* Product purchases remain offline; no Offro Pay, payouts or wallet.

## Backend files
services/country_context.py (new), routers/merchant_app.py, routers/merchant_products.py, routers/public_pricing.py, routers/users.py,
services/products.py, test_pricing_country_payments.py (new, 25 tests), test_pricing_public_country.py (updated for the new `payment` shape).

## Flutter files
lib/core/pricing/country_money.dart (new, pure Dart), lib/core/services/api_service.dart, lib/screens/merchant/merchant_screens.dart,
lib/screens/merchant/products_phase2.dart, lib/screens/influencer/my_influencer_profile.dart, test/country_money_test.dart (new).

## API connections
GET /merchant/plans, POST /merchant/subscribe, GET /merchant/banners/pricing, POST /merchant/banners/order, product slots/order/renew/verify,
GET /user/influencer-profile/subscription-pricing, POST /user/influencer-profile/publish, /merchant/invoices(/full), GET /country-pricing/{ISO}.

## Tests
* Backend: targeted files 37 passed. Full suite earlier: 2,438 passed, 16 skipped, 76 failed - all 76 are the browser (Playwright) files that cannot run in this sandbox; same set as before the change.
* Flutter helper tests: 11 passed (run with a minimal flutter_test shim and plain Dart).
* NOT verified: `flutter analyze`, `flutter build`, widget tests, a real Razorpay checkout, a device run (no Flutter SDK in the sandbox).

## Known gaps / external dependencies
1. Razorpay international: activation on the account and per-currency support must be confirmed with Razorpay; set `RAZORPAY_INTERNATIONAL_CURRENCIES` only for confirmed currencies. The minor-unit table in `to_minor` (zero-decimal and three-decimal currencies) is my assumption and must be checked against the Razorpay docs.
2. Non-India labels that localized strings build with a hard-coded rupee sign use English helpers (`CountryMoney`); proper ARB keys need `flutter gen-l10n`.
3. Admin-created products and admin store renewals for non-India stores still use India settings (`routers/admin.py`).
4. `/merchant/plans` without a token still returns India plans (as before).
5. Customer-facing merchant product prices in lib/main.dart still show the rupee sign (merchant-set prices, out of scope).
6. Invoices: layout unchanged; the tax label and currency come from the record. Local VAT-invoice legal requirements are not implemented (no tax engine, per brief).

## Security findings (reported only; nothing rotated or modified)
* `android/key.properties` and `upload-keystore.jks` are committed in the public Flutter repo (commit 4c9b3b0) with plaintext passwords. Treat the upload key as exposed: rotate via Play Console upload-key reset and remove the files from history after approval.
* `lib/main.dart` contains `kRazorpayKey` (a key id, not the secret); the server-returned key is preferred. `RAZORPAY_KEY_SECRET` is never in Flutter.
* `google-services.json` / `GoogleService-Info.plist` are tracked (client config, restrict keys in the Google console).
