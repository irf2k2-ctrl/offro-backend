# Pending transactions archive (Payments > Archived Pending Transactions)

Only `subscriptions` documents with `status == "pending"` are considered.

| Rule | Behaviour |
|---|---|
| Archive | older than `PENDING_ARCHIVE_AFTER_DAYS` (default 90), age = `updated_at`, else `created_at`; moved to `subscriptions_archive` with every original field kept (status stays `pending`) plus `archived_at` |
| Never archived | any other status; any pending record with `razorpay_order_id` or `razorpay_payment_id` (payment may have been captured without app-side verification); a record with `paid_at`; a record whose age cannot be read |
| Purge | archived records are deleted `PENDING_ARCHIVE_RETENTION_DAYS` (default 90) after `archived_at` |
| Job | background thread, every `PENDING_ARCHIVE_INTERVAL_HOURS` (default 24), first run 5 min after start. `PENDING_ARCHIVE_ENABLED=0` stops it |
| Manual delete | Payments tab > Archived: checkboxes, Select All, Delete Selected (confirmation). `POST /admin/payments/archive/delete`; needs Payments **delete**; archive collection only; audited in `security_audit` (`payments.archive.delete`: started / ok / denied / failed) |
| Not touched | `banner_orders`, `product_orders`, invoices, Razorpay creation/verification, Flutter flows |

Invalid or non-positive values fall back to the defaults. The job is safe to repeat and runs on several instances at once: the copy is
written once (`$setOnInsert`) and the original is removed only while it still matches the rule.
Only new item: collection `subscriptions_archive` (index on `archived_at`, created at startup). No existing document is migrated.

## Several instances at once
No MongoDB transaction is needed. Per subscription: (1) copy to the archive with `$setOnInsert` (the creator is known from `upserted_id`);
(2) remove the original with one atomic `find_one_and_delete` guarded by *still pending, no Razorpay ids, not paid, same timestamps*;
(3) only the worker that removed the original rewrites the archive record with the document it really removed (+ `archived_at`).
A worker that loses step 2 never deletes the archive record because the original is gone (another worker moved it). It deletes a record only
if it created it AND the original is still live (it changed). A leftover copy of a payment that completed is removed by the next run
(`reconcile_orphans`), since the live original is the authority.
