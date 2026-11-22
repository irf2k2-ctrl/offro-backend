# Dashboard user country access (Stage 4, Part A) and QR scan count (Part B)

## Country access
* `dashboard_users.country` – optional ISO code from the one Country Master (`utils.phone.SUPPORTED_COUNTRIES`). Missing/empty = **no
  country restriction** (every existing user keeps exactly today's access; their city restriction still applies). Super Admin is never restricted.
* A record's country = its owner account's country (`accounts.phone_country`, else parsed from the stored phone – the same rule as
  `utils.countries.account_country`). Stores, products, deals, subscriptions, invoices, banners, reviews, withdrawals, influencers belong to a
  merchant/account. A record whose owner country cannot be resolved is hidden from a country-restricted user. No migration, no new field on records.
* Enforced by the backend in `services/dashboard_country.py` and applied to: accounts (list/detail/status/deletion), merchants, stores
  (list/slim/detail/QR/edit/status/approve/rating/renewal/delete/create), users + points, stats, subscriptions, merchant transactions,
  invoices, withdraw requests, influencers, merchant banners, deals, products (+ reports), reviews, FCM diagnostic, notifications,
  WhatsApp merchant audience and live chat, dashboard users, activity logs. Unknown record / other country answers 404 (existence not revealed).
* Dashboard users: Country on create/edit; nobody except Super Admin changes their own country; a country-restricted user manages only users of its
  own country, cannot create/assign Super Admin and cannot move a user to another country or lift its restriction.
* New endpoint `GET /admin/auth/countries` (the Country Master for the selector). API: `POST/PUT /admin/auth/users` accept `country`;
  `GET /admin/auth/users` and `/admin/auth/me` return `country` (and `country_name`). Invalid code -> 422 `{"code":"invalid_country"}`.

## QR scan count
Root cause: `POST /user/redeem` stores a `redemptions` record and credits points, but the number shown in Dashboard > Accounts is `accounts.scans`,
set only by the old one-time migration – nothing incremented it. Fix: `/user/redeem` increments `scans` in the same update that credits points (only
after every check passed); the dashboard shows `max(recorded redemptions of that account, stored counter)`.

## Database
No new collections, no indexes, no migration. Rollback = restore the previous files (`dashboard_users.country` / `accounts.scans` are plain fields
that older code ignores).
