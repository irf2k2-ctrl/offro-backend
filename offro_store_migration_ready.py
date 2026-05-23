import time
from datetime import datetime, timezone
from pymongo import MongoClient
import cloudinary
import cloudinary.uploader

# MongoDB
MONGO_URL = "mongodb://mongo:FGyxBURlEDfqBMHAfNxqDOnJKvNwcwQR@roundhouse.proxy.rlwy.net:32523/offro_db?authSource=admin"

client = MongoClient(MONGO_URL)
db = client["offro_db"]

# Cloudinary
cloudinary.config(
    cloud_name="dwjcqcapf",
    api_key="888983174117729",
    api_secret="A2QVryyQNU0XegL6G6eBbOGrF_Y"
)

print("Connected to offro_db")

stores = db["stores"]
count = 0

docs = list(stores.find({
    "image": {
        "$exists": True,
        "$ne": None
    }
}))

print(f"Found {len(docs)} store images")

for doc in docs:

    if doc.get("image_url"):
        print(f"Skipping {doc['_id']} (already migrated)")
        continue

    img = doc.get("image", "")

    if not isinstance(img, str):
        continue

    if not img.startswith("data:image"):
        continue

    try:
        print(f"Migrating {doc['_id']}")

        result = cloudinary.uploader.upload(
            img,
            folder="offro/stores"
        )

        url = result.get("secure_url")

        if not url:
            print("Upload failed")
            continue

        thumb = url.replace(
            "/upload/",
            "/upload/w_300,c_fill/"
        )

        stores.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "image_url": url,
                "image_thumb": thumb,
                "migrated_at": datetime.now(timezone.utc).isoformat()
            }}
        )

        count += 1
        print("OK")
        time.sleep(0.5)

    except Exception as e:
        print(f"Failed: {e}")

print("Migration completed")
print(f"Total migrated: {count}")
