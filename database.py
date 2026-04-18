import os
from pymongo import MongoClient

client = MongoClient(os.getenv("MONGO_URL"))
db = client.get_database()

# EXISTING COLLECTIONS
users_collection = db["users"]
merchants_collection = db["merchants"]
stores_collection = db["stores"]
deals_collection = db["deals"]
redemptions_collection = db["redemptions"]
wallet_collection = db["wallet"]
withdraw_requests_collection = db["withdraw_requests"]

# ✅ ADD THIS (FIX)
admin_collection = db["admins"]