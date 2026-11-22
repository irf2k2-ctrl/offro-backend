# Default Images – country mapping (Stage 3)

Dashboard → Default Images. Every category (store, product, offer, city, merchant_banner) and Service Unavailable can be limited to
one or more countries from the single Country Master (`utils.phone.SUPPORTED_COUNTRIES`: IN, JO, NP, PH, SA).

## Data
* `settings {_type:"default_images"}` is unchanged (arrays of URLs, single Service Unavailable config).
* NEW `default_image_countries` – one record per MAPPED image: `category, url_key (sha256 of url), countries[], updated_by, updated_at`
  (unique index category+url_key). No record = "All countries" (every pre-existing image).
* NEW `default_no_service_variants` – country-specific Service Unavailable screens: `variant_id, url, title, message, countries[>=1]`.
  The existing single config stays the "All countries" screen.
* Both collections are created on first use. No migration, no backfill, nothing is rewritten or deleted. Rollback = ignore/drop them.

## Retrieval ("most specific wins", same for every category)
* `GET /default-images?country=SA`: images mapped to SA if any, otherwise the unmapped (All countries) images.
* No/unknown country (old apps, guests): only unmapped images – identical to the old response. Country-specific images never reach other countries.
* Service Unavailable: first variant (oldest first) containing the country, blank title/message fall back to the default text; otherwise the default.
* Response keeps every legacy key and adds `country`, `country_specific`.

## Admin API (writes need Settings → edit; Super Admin always)
* `GET /admin/default-images?country=all|global|SA` – legacy keys + `items{cat:[{url,countries}]}`, `filter`, `no_service_variants`, `countries`.
* `PUT /admin/default-images` (multipart) and `PUT /admin/default-images/urls` accept `countries`.
* `PUT /admin/default-images/countries` `{type,url,countries}` – edit mapping (`[]` = all countries).
* `POST /admin/default-images/no-service-variants`, `PUT|DELETE /admin/default-images/no-service-variants/{id}`.
* Validation (before anything is stored): unknown code → 422 `invalid_country`; repeated code → 422 `duplicate_country`; not a list → 422 `invalid_countries`;
  variant without country → 422 `countries_required`. Codes are normalised to upper case.

## Indexes
`server.startup()` calls `services.default_images.ensure_indexes_safe()`, which creates the unique indexes
`default_image_countries_cat_url` (category+url_key) and `default_no_service_variants_id` (variant_id). `create_index` with an identical spec is a
no-op, so restarts are safe; nothing is dropped or rewritten. A failure is logged at ERROR level ("default image country index creation failed")
and startup continues; `ensure_indexes_once()` retries before the first mapping/variant write (once per process after success).
