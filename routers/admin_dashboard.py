from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from bson import ObjectId
from database import db
import uuid

router = APIRouter(prefix="/admin-dashboard", tags=["Admin Dashboard"])

# Set up template rendering
templates = Jinja2Templates(directory="templates")

# ---------------- SAFE OBJECTID ---------------- #
def safe_object_id(val):
    try:
        return ObjectId(val)
    except:
        return val

# ---------------- ADMIN LOGIN ---------------- #
@router.post("/admin/login")
async def admin_login(data: dict):
    username = data.get("username")
    password = data.get("password")

    # Check if the admin exists in the database
    admin = db.super_admins.find_one({
        "username": username,
        "password": password
    })

    if not admin:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Generate token and send response
    token = str(uuid.uuid4())
    db.super_admins.update_one(
        {"_id": admin["_id"]},
        {"$set": {"token": token}}
    )

    response = JSONResponse(content={
        "message": "Login successful",
        "token": token
    })
    response.set_cookie(key="admin_token", value=token, httponly=True)

    return response

# ---------------- ADMIN DASHBOARD ---------------- #
@router.get("/", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    # Check if the admin_token cookie exists
    token = request.cookies.get("admin_token")
    
    if not token:
        return RedirectResponse("/admin-login")  # If no token, redirect to login page
    
    admin = db.super_admins.find_one({"token": token})

    if not admin:
        return RedirectResponse("/admin-login")  # If no valid admin found, redirect to login

    return templates.TemplateResponse("admin_dashboard.html", {"request": request, "admin": admin})

# ---------------- ADMIN DASHBOARD OVERVIEW ---------------- #
@router.get("/overview")
def admin_overview():
    return {
        "stores": db.stores.count_documents({}),
        "deals": db.deals.count_documents({}),
        "users": db.users.count_documents({}),
        "redemptions": db.redemptions.count_documents({})
    }

# ---------------- GET STORES ---------------- #
@router.get("/stores")
def get_stores(city: str = Query(None)):
    query = {}
    if city:
        query["city"] = city

    stores = list(db.stores.find(query))
    result = []

    for s in stores:
        merchant = db.merchants.find_one({
            "_id": safe_object_id(s.get("merchant_id"))
        })

        store_id_str = str(s["_id"]).strip()

        deal = db.deals.find_one(
            {"store_id": store_id_str},
            sort=[("created_at", -1)]
        )

        start_date = "-"
        end_date = "-"

        if deal:
            start_date = deal.get("start_date") or "-"
            end_date = deal.get("end_date") or "-"

        result.append({
            "_id": store_id_str,
            "store_name": s.get("store_name"),
            "merchant_name": merchant.get("name") if merchant else "N/A",
            "city": s.get("city"),
            "status": s.get("status", "active"),
            "start_date": start_date,
            "end_date": end_date
        })

    return result

# ---------------- GET MERCHANTS ---------------- #
@router.get("/merchants")
def get_merchants(request: Request):
    city = request.query_params.get("city")
    query = {}
    if city:
        query["city"] = city

    merchants = list(db.merchants.find(query))

    return [{
        "_id": str(m["_id"]),
        "name": m.get("name"),
        "phone": m.get("phone"),
        "city": m.get("city"),
        "status": m.get("status", "active")
    } for m in merchants]

# ---------------- GET DEALS ---------------- #
@router.get("/deals")
def get_all_deals():
    deals = list(db.deals.find())
    result = []

    for d in deals:
        store = db.stores.find_one({
            "_id": safe_object_id(d.get("store_id"))
        })

        merchant = db.merchants.find_one({
            "_id": safe_object_id(d.get("merchant_id"))
        })

        result.append({
            "_id": str(d["_id"]),
            "store_name": store.get("store_name") if store else "-",
            "merchant_name": merchant.get("name") if merchant else "-",
            "discount": d.get("discount"),
            "start_date": d.get("start_date"),
            "end_date": d.get("end_date"),
            "status": d.get("status", "active")
        })

    return result

# ---------------- UPDATE STORE STATUS ---------------- #
@router.put("/store-status/{store_id}")
def update_store_status(store_id: str, status: str):
    db.stores.update_one(
        {"_id": ObjectId(store_id)},
        {"$set": {"status": status}}
    )
    return {"message": "Status updated"}

# ---------------- DELETE STORE ---------------- #
@router.delete("/store/{id}")
def delete_store(id: str):
    db.stores.delete_one({"_id": ObjectId(id)})
    db.deals.delete_many({"store_id": id})
    return {"message": "Store deleted"}