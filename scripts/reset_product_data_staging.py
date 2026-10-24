"""
STAGING ONLY — remove the old product collections and prepare the new ones.

    python scripts/reset_product_data_staging.py                       # DRY RUN (default): only prints
    python scripts/reset_product_data_staging.py --apply --i-am-on-staging

Reads MONGO_URL from the environment (the same variable the backend uses).
Prints the target host + database and the number of documents it would remove;
with --apply it needs --i-am-on-staging as well, so it can never run by accident.

What it does
  1. drops the OLD product collections (staging data is disposable):
       gift_vouchers, merchant_vouchers, products,
       voucher_orders, product_renewal_orders, product_upgrade_orders
  2. removes data that only pointed at those old products:
       product_events, product_reviews, product_history, merchant_locks,
       and users.favorite_product_ids
  3. creates MISSING product settings (free_product_limit, additional_product_price,
     product_max_validity_days) — never overwrites a value the Admin already set
  4. creates the indexes for merchant_products / product_orders

It does NOT touch: withdraw_requests, gift_product_requests, reward_transactions,
discounts, invoices, stores, deals, banners, users (other than favorite_product_ids).
"""
import os
import sys

OLD_COLLECTIONS = ["gift_vouchers", "merchant_vouchers", "products",
                   "voucher_orders", "product_renewal_orders", "product_upgrade_orders"]
ORPHANS = ["product_events", "product_reviews", "product_history", "merchant_locks"]


def main():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    apply_ = "--apply" in sys.argv
    if apply_ and "--i-am-on-staging" not in sys.argv:
        sys.exit("Refusing to apply: add --i-am-on-staging to confirm this is the STAGING database.")
    url = os.getenv("MONGO_URL")
    if not url:
        sys.exit("MONGO_URL is not set.")
    from database import db, client
    host = ",".join(f"{h}:{p}" for h, p in (client.nodes or []))
    print(f"Target: host={host or '?'}  database={db.name}")
    print("Mode  : " + ("APPLY (will delete)" if apply_ else "DRY RUN (nothing is changed)"))
    print()
    for name in OLD_COLLECTIONS + ORPHANS:
        n = db[name].estimated_document_count() if name in db.list_collection_names() else 0
        print(f"  {'drop' if name in OLD_COLLECTIONS else 'clear':5} {name:28} {n} docs")
        if apply_ and name in db.list_collection_names():
            db.drop_collection(name) if name in OLD_COLLECTIONS else db[name].delete_many({})
    fav = db.users.count_documents({"favorite_product_ids": {"$exists": True, "$ne": []}})
    print(f"  unset users.favorite_product_ids on {fav} users")
    if apply_:
        db.users.update_many({"favorite_product_ids": {"$exists": True}}, {"$unset": {"favorite_product_ids": ""}})
    from services import products as P
    if apply_:
        created = P.ensure_settings()
        P.ensure_indexes()
        print(f"\nSettings created: {created or 'none (all already set)'}")
        print("Indexes ensured on merchant_products / product_orders.")
    else:
        doc = db.pricing.find_one({}) or {}
        missing = [k for k in P.SEED_DEFAULTS if doc.get(k) is None]
        print(f"\nWould create settings: {missing or 'none (all already set)'}"
              + ("  (free_product_limit would be taken from legacy standard_product_limit)"
                 if "free_product_limit" in missing and doc.get("standard_product_limit") is not None else ""))
        print("\nDry run only. Re-run with:  --apply --i-am-on-staging")


if __name__ == "__main__":
    main()
