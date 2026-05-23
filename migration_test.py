from pymongo import MongoClient

MONGO_URI = "mongodb://mongo:FGyxBURlEDfqBMHAfNxqDOnJKvNwcwQR@roundhouse.proxy.rlwy.net:32523"

try:
    client = MongoClient(MONGO_URI)
    dbs = client.list_database_names()

    print("✅ Connected successfully")
    print("Databases:")
    for db in dbs:
        print("-", db)

except Exception as e:
    print("❌ Error:")
    print(e)