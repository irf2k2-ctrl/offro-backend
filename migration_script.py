"""
OFFRO — MongoDB accounts migration script.
Run this ONCE on your Railway server (or locally connected to Atlas).
Merges users + merchants → accounts collection.
Safe: uses upsert, never deletes originals until you confirm.
"""
from pymongo import MongoClient, UpdateOne
import os, re, datetime

MONGO_URL = "mongodb://mongo:FGyxBURlEDfqBMHAfNxqDOnJKvNwcwQR@roundhouse.proxy.rlwy.net:32523"
client = MongoClient(MONGO_URL)
db = client["offro_db"]

def phone_variants(phone):
    p = re.sub(r'\D', '', str(phone))
    variants = {p}
    if len(p) == 10:
        variants.update([f'+91{p}', f'91{p}'])
    elif len(p) == 12 and p.startswith('91'):
        variants.update([p[2:], f'+{p}'])
    elif len(p) == 13 and p.startswith('+91'):
        variants.update([p[3:], p[1:]])
    return list(variants)

def migrate():
    print("=" * 60)
    print("OFFRO — Unified Accounts Migration")
    print("=" * 60)

    users_list = list(db.users.find({}))
    merchants_list = list(db.merchants.find({}))

    print(f"\nFound {len(users_list)} users, {len(merchants_list)} merchants")

    phone_map = {}

    for u in users_list:
        raw = str(u.get("phone", ""))
        variants = phone_variants(raw)
        phone_10 = next((v for v in variants if len(v) == 10), raw)

        doc = {
            "phone": phone_10,
            "phone_variants": phone_variants(phone_10),
            "name": u.get("name", u.get("full_name", "")),
            "city": u.get("city", ""),
            "area": u.get("area", ""),
            "status": u.get("status", "active"),
            "roles": ["user"],
            "visit_points": u.get("visit_points", u.get("points", 0) or 0),
            "visit_pts": u.get("visit_pts", u.get("visit_points", u.get("points", 0) or 0)),
            "pool_pts": u.get("pool_pts", 0),
            "token": u.get("token", ""),
            "fcm_token": u.get("fcm_token", ""),
            "user_id": str(u["_id"]),
            "profile_image": u.get("profile_image", ""),
            "scans": u.get("scans", u.get("scan_count", 0) or 0),
            "last_login": u.get("last_login", ""),
            "created_at": u.get("created_at", datetime.datetime.utcnow().isoformat()),
            "migrated_at": datetime.datetime.utcnow().isoformat(),
            "migrated_from": "users",
        }
        phone_map[phone_10] = doc

    for m in merchants_list:
        raw = str(m.get("phone", ""))
        variants = phone_variants(raw)
        phone_10 = next((v for v in variants if len(v) == 10), raw)

        merchant_extra = {
            "merchant_id": str(m["_id"]),
            "merchant_name": m.get("name", ""),
            "business_name": m.get("business_name", m.get("name", "")),
            "merchant_phone": phone_10,
            "merchant_token": m.get("token", ""),
            "merchant_fcm": m.get("fcm_token", ""),
            "merchant_city": m.get("city", ""),
            "merchant_status": m.get("status", "active"),
            "migrated_from": "both",
        }

        if phone_10 in phone_map:
            phone_map[phone_10].update(merchant_extra)
            phone_map[phone_10]["roles"] = list(set(
                phone_map[phone_10].get("roles", ["user"]) + ["merchant"]
            ))
            if not phone_map[phone_10].get("token"):
                phone_map[phone_10]["token"] = m.get("token", "")
        else:
            doc = {
                "phone": phone_10,
                "phone_variants": phone_variants(phone_10),
                "name": m.get("name", ""),
                "city": m.get("city", ""),
                "status": m.get("status", "active"),
                "roles": ["merchant"],
                "visit_points": 0,
                "visit_pts": 0,
                "pool_pts": 0,
                "token": m.get("token", ""),
                "fcm_token": m.get("fcm_token", ""),
                "scans": 0,
                "created_at": m.get("created_at", datetime.datetime.utcnow().isoformat()),
                "migrated_at": datetime.datetime.utcnow().isoformat(),
                "migrated_from": "merchants",
            }
            doc.update(merchant_extra)
            phone_map[phone_10] = doc

    print(f"\nMigrating {len(phone_map)} unique accounts...")
    created = updated = 0

    for phone_10, doc in phone_map.items():
        result = db.accounts.update_one(
            {"phone": phone_10},
            {"$set": doc},
            upsert=True
        )
        if result.upserted_id:
            created += 1
        else:
            updated += 1

    print(f"✅ Created: {created}")
    print(f"✅ Updated: {updated}")
    print(f"✅ Total accounts: {db.accounts.count_documents({})}")

    print("\n⚠ Skipping index creation temporarily")
    print("Accounts data migrated successfully")

    print("\n✅ Migration complete!")

if __name__ == "__main__":
    migrate()
