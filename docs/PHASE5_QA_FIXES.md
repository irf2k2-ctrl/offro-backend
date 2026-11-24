# Phase 5 - staging QA fixes (backend)

* **Store status (Issue 1)** - `services/store_status.py` is now the single rule used by the admin Stores list (`routers/admin.py::_fmt_store_fast`),
  the customer `/stores` (`routers/public.py::get_stores`) and the merchant `/merchant/stores` + `/merchant/stores/{id}`:
  only PAID/active subscription rows count (an abandoned/failed renewal leaves a `pending` row with a future `end_date` that used to make
  the admin/customer views Active), the effective end is the latest of the paid `subscriptions.end_date` and `stores.subscription_end`,
  and a subscription is valid through its end date (23:59:59 IST). The merchant detail endpoint also now handles string dates.
  The stored `status` field, renewal flows and prices are untouched.
* **Default Store Image (Issue 2)** - `/city-status` also treats a city that has (or had) a non-deleted store as inside the service area
  (country taken from the store owner's account), so a city whose stores are all expired / missing from the City Master is no longer
  "outside the service area". Cities with no match anywhere still return `supported: false`.
* **Category names (Issues 3, 5)** - `services/category_names.py`; `/categories` adds `names` and `display_name` (`?lang=`); admin
  `POST/PUT /admin/categories` accept `names` ({"ar": "..."}); the admin Categories page has an Arabic-name box and click-to-edit.
  The English `name` is unchanged everywhere. Built-in glossary covers the standard categories until an admin sets a translation.
* **Default content for every empty city (addendum)** - no backend logic change was needed: `/stores` already answers 200 `[]` for a city
  with no record, outside the service area, or with only expired/inactive/deleted stores, and `/default-images` (country mapping + rotation)
  is independent of cities. `/city-status` is kept for compatibility but the app no longer uses it for the empty state.
* **Temporary store-status diagnostic** - `GET /admin/diagnostics/store-status?name=test store stage` (or `?store_id=`): Super Admin dashboard
  session only, read-only, no payment identifiers or customer data. Reports the store's own status/`subscription_end`, every subscription row
  (whitelisted fields), and the final status under the admin-dashboard, customer-app, current merchant-app and pre-Phase-5 merchant rules.
  Remove `routers/admin_store_status_diag.py` and its `include_router` line in `server.py` when the investigation is finished.
