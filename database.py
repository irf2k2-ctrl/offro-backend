from pymongo import MongoClient

client = MongoClient("mongodb://localhost:27017")

db = client["localsaver"]

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