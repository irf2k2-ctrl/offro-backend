from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from routers import admin, users, public, merchant_app
from database import db

app = FastAPI(title="LocalSaver API", version="3.0")

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

@app.on_event("startup")
def startup():
    admin.seed_admin()

# Admin pages
@app.get("/admin", response_class=HTMLResponse)
def serve_admin_login(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})

@app.get("/admin/dashboard", response_class=HTMLResponse)
def serve_admin_dashboard(request: Request):
    response = templates.TemplateResponse("admin_dashboard.html", {"request": request})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
