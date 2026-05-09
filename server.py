from fastapi import FastAPI, Request, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from routers import admin, users, public, merchant_app
from database import db
import base64, io, re

app = FastAPI(title="OffrO API", version="5.0")

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
app.include_router(admin.router,        prefix="/admin")
app.include_router(users.router,        prefix="/user")
app.include_router(public.router)       # /stores /categories /store-image — public


# ── Public gift-voucher endpoint ──
@app.get("/gift-vouchers")
def public_gift_vouchers():
    docs = list(db.gift_vouchers.find({"is_active": True}).sort("_id", -1))
    result = []
    for v in docs:
        # Voucher image: proxy if base64
        logo = v.get("logo","") or ""
        vid  = str(v["_id"])
        if logo.startswith("data:image"):
            logo_out = f"/voucher-image/{vid}"
        elif logo.startswith("http"):
            logo_out = logo
        else:
            logo_out = ""
        result.append({
            "id":       vid,
            "title":    v.get("title", ""),
            "price":    str(v.get("price", v.get("value", ""))),
            "text":     v.get("text", ""),
            "validity": v.get("validity", ""),
            "logo":     logo_out,
        })
    return result


# ── Voucher image proxy ──
@app.get("/voucher-image/{vid}")
def get_voucher_image(vid: str):
    from fastapi.responses import Response
    from fastapi import HTTPException
    from bson import ObjectId
    try:
        doc = db.gift_vouchers.find_one({"_id": ObjectId(vid)}, {"logo": 1})
    except Exception:
        raise HTTPException(400, "Bad id")
    if not doc:
        raise HTTPException(404, "Not found")
    raw = doc.get("logo","") or ""
    if not raw.startswith("data:image"):
        raise HTTPException(404, "No image")
    match = re.match(r"data:([^;]+);base64,(.+)", raw, re.DOTALL)
    if not match:
        raise HTTPException(400, "Bad image data")
    mime    = match.group(1)
    b64data = match.group(2)
    try:
        img_bytes = base64.b64decode(b64data)
    except Exception:
        raise HTTPException(500, "Decode error")
    return Response(content=img_bytes, media_type=mime,
        headers={"Cache-Control": "public, max-age=86400", "Content-Length": str(len(img_bytes))})


# ── Combined home-data endpoint — single call replaces 5 parallel calls ──
@app.get("/home-data")
def get_home_data(
    city:     str = None,
    category: str = None,
    limit:    int = 50,
    skip:     int = 0,
):
    """Returns stores + categories + gift-vouchers + promo-sliders in one call."""
    from routers.public import get_stores, get_categories, get_promo_sliders_public

    stores_resp = get_stores(city=city, category=category, limit=limit, skip=skip)
    cats = get_categories()

    # Gift vouchers — lightweight (no base64, just proxy URLs)
    gv_docs = list(db.gift_vouchers.find({"is_active": True}).sort("_id", -1))
    gift_vouchers = []
    for v in gv_docs:
        logo = v.get("logo","") or ""
        vid  = str(v["_id"])
        if logo.startswith("data:image"):
            logo_out = f"/voucher-image/{vid}"
        elif logo.startswith("http"):
            logo_out = logo
        else:
            logo_out = ""
        gift_vouchers.append({
            "id":       vid,
            "title":    v.get("title",""),
            "text":     v.get("text",""),
            "validity": v.get("validity",""),
            "logo":     logo_out,
        })

    # Promo sliders
    ps_docs = list(db.promo_sliders.find({"is_active": True}).sort("sort_order", 1))
    promo_sliders = [{"id":str(p["_id"]),"title":p.get("title",""),"image_url":p.get("image_url",""),
                      "link":p.get("link",""),"order":p.get("sort_order",1)} for p in ps_docs]

    return {
        "stores":       stores_resp.get("stores", []),
        "total":        stores_resp.get("total", 0),
        "has_more":     stores_resp.get("has_more", False),
        "categories":   cats,
        "gift_vouchers": gift_vouchers,
        "promo_sliders": promo_sliders,
    }


# ── Admin image upload — with size validation + compression ──
@app.post("/admin/upload-image")
async def upload_image(file: UploadFile = File(...)):
    """
    Upload image for admin use (gift cards, promo banners).
    Enforces 2MB limit. Compresses and resizes to max 800px width.
    Returns a hosted URL (Cloudinary) if configured, else compressed base64.
    """
    MAX_SIZE = 2 * 1024 * 1024  # 2MB

    try:
        contents = await file.read()

        # Size validation
        if len(contents) > MAX_SIZE:
            size_mb = len(contents) / 1024 / 1024
            return JSONResponse(
                {"error": f"Image too large ({size_mb:.1f}MB). Max 2MB allowed."},
                status_code=413
            )

        mime = file.content_type or "image/jpeg"

        # Compress + resize with Pillow
        try:
            from PIL import Image as PILImage
            img = PILImage.open(io.BytesIO(contents))

            # Convert RGBA → RGB for JPEG
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            # Resize if wider than 800px
            if img.width > 800:
                ratio  = 800 / img.width
                new_h  = int(img.height * ratio)
                img    = img.resize((800, new_h), PILImage.LANCZOS)

            # Save as JPEG with 82% quality (good balance)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=82, optimize=True)
            contents = buf.getvalue()
            mime     = "image/jpeg"

        except ImportError:
            pass  # Pillow not available — store as-is
        except Exception:
            pass  # Non-image file — store as-is

        b64      = base64.b64encode(contents).decode()
        data_url = f"data:{mime};base64,{b64}"
        return JSONResponse({"url": data_url, "size_kb": round(len(contents)/1024, 1)})

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── DB indexes — created once on startup ──
@app.on_event("startup")
def startup():
    admin.seed_admin()
    _ensure_indexes()


def _ensure_indexes():
    """Create MongoDB indexes for performance. Safe to call multiple times — idempotent."""
    try:
        # Stores — main compound index (covers status+city+category queries)
        db.stores.create_index(
            [("status", 1), ("city", 1), ("category", 1)],
            name="stores_status_city_category",
            background=True,
        )
        # Stores — geo/location index
        db.stores.create_index(
            [("lat", 1), ("lng", 1)],
            name="stores_lat_lng",
            background=True,
        )
        # Stores — merchant lookup
        db.stores.create_index(
            [("merchant_id", 1)],
            name="stores_merchant_id",
            background=True,
        )
        # Deals — store lookup (for N+1 elimination)
        db.deals.create_index(
            [("store_id", 1), ("status", 1)],
            name="deals_store_status",
            background=True,
        )
        # Users — phone lookup (login)
        db.users.create_index(
            [("phone", 1)],
            name="users_phone",
            background=True,
            unique=True,
            sparse=True,
        )
        # Subscriptions — store lookup
        db.subscriptions.create_index(
            [("store_id", 1), ("status", 1)],
            name="subs_store_status",
            background=True,
        )
        print("✅ MongoDB indexes ensured")
    except Exception as e:
        print(f"⚠️  Index creation warning: {e}")


# Admin pages
@app.get("/admin", response_class=HTMLResponse)
def serve_admin_login(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})


@app.get("/admin/dashboard", response_class=HTMLResponse)
def serve_admin_dashboard(request: Request):
    try:
        response = templates.TemplateResponse("admin_dashboard.html", {"request": request})
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"]  = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception:
        import traceback
        return HTMLResponse(f"<pre>Template Error:\n{traceback.format_exc()}</pre>", status_code=500)


@app.get("/merchant-dashboard", response_class=HTMLResponse)
def serve_merchant_dashboard(request: Request):
    try:
        return templates.TemplateResponse("merchant_dashboard.html", {"request": request})
    except Exception:
        import traceback
        return HTMLResponse(f"<pre>Template Error:\n{traceback.format_exc()}</pre>", status_code=500)


@app.get("/health")
def health():
    return {"status": "ok", "version": "5.0"}
