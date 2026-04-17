from pymongo import MongoClient
from passlib.context import CryptContext

client = MongoClient("mongodb://localhost:27017")
db = client["localsaver"]

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# -----------------------
# Seed Cities
# -----------------------

cities = [
    {"name": "Dubai"},
    {"name": "Abu Dhabi"},
    {"name": "Sharjah"}
]

if db.cities.count_documents({}) == 0:
    db.cities.insert_many(cities)
    print("Cities inserted")


# -----------------------
# Seed Categories
# -----------------------

categories = [
    {"name": "Restaurants"},
    {"name": "Cafes"},
    {"name": "Salons"},
    {"name": "Gyms"}
]

if db.categories.count_documents({}) == 0:
    db.categories.insert_many(categories)
    print("Categories inserted")


# -----------------------
# Seed Super Admin
# -----------------------

admin_email = "admin@localsaver.com"
admin_password = "admin123"

hashed_password = pwd_context.hash(admin_password)

admin = {
    "email": admin_email,
    "password": hashed_password,
    "role": "admin",
    "status": "active"
}

if db.admins.count_documents({"email": admin_email}) == 0:
    db.admins.insert_one(admin)
    print("Admin created")


print("Database seeded successfully")