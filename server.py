from fastapi import FastAPI, Request, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from routers import admin, users, public, merchant_app
from database import db
import base64, io

app = FastAPI(title="OffrO API", version="4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Routers
app.include_router(merchant_app.router, prefix="/merchant")
app.include_router(admin.router, prefix="/admin")
app.include_router(users.router, prefix="/user")
app.include_router(public.router)  # /stores /categories — public

# ── Public gift-voucher endpoint (no auth — Flutter app reads this) ──
@app.get("/gift-vouchers")
def public_gift_vouchers():
    """Returns active gift voucher cards for the app home screen."""
    docs = list(db.gift_vouchers.find({"is_active": True}).sort("_id", -1))
    result = []
    for v in docs:
        result.append({
            "id":       str(v["_id"]),
            "title":    v.get("title", ""),
            "text":     v.get("text", ""),
            "validity": v.get("validity", ""),
            "logo":     v.get("logo", ""),
        })
    return result

# ── Admin image upload endpoint (used by Gift Cards form) ──
@app.post("/admin/upload-image")
async def upload_image(file: UploadFile = File(...)):
    """Convert uploaded image to base64 data URL."""
    try:
        contents = await file.read()
        mime = file.content_type or "image/jpeg"
        b64 = base64.b64encode(contents).decode()
        data_url = f"data:{mime};base64,{b64}"
        return JSONResponse({"url": data_url})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.on_event("startup")
def startup():
    admin.seed_admin()

# Admin pages
@app.get("/admin", response_class=HTMLResponse)
def serve_admin_login(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})

@app.get("/admin/dashboard", response_class=HTMLResponse)
def serve_admin_dashboard(request: Request):
    try:
        response = templates.TemplateResponse("admin_dashboard.html", {"request": request})
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception as e:
        import traceback
        return HTMLResponse(f"<pre>Template Error:\n{traceback.format_exc()}</pre>", status_code=500)
    return response
