from fastapi import FastAPI, Request, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from routers import admin, users, public, merchant_app, popup_campaigns, webhook, wa_chat, dashboard_auth
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
app.include_router(merchant_app.router,    prefix="/merchant")
app.include_router(admin.router,          prefix="/admin")
app.include_router(users.router,          prefix="/user")
app.include_router(public.router)
app.include_router(popup_campaigns.router)
app.include_router(webhook.router)          # WhatsApp Cloud API webhook — no prefix
app.include_router(wa_chat.router, prefix="/admin")  # WhatsApp Live Chat admin API
app.include_router(dashboard_auth.router, prefix="/admin")  # RBAC dashboard auth — /admin/auth/*

# ── Root redirect → admin login (dashboard route itself re-checks the session) ──
@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/admin", status_code=307)



# ── File download endpoints ──
@app.get("/download/admin_dashboard")
def download_admin_dashboard():
    return FileResponse(
        path="templates/admin_dashboard.html",
        media_type="text/html",
        filename="admin_dashboard.html",
        headers={"Content-Disposition": "attachment; filename=admin_dashboard.html"}
    )

# ── Public gift-voucher endpoint ──
@app.get("/gift-vouchers")
def public_gift_vouchers():
    # Phase 1: Only Premium products shown in Discover Products (exclude standard/subscription-linked)
    docs = list(db.gift_vouchers.find(
        {"is_active": True, "product_type": {"$ne": "standard"}}
    ).sort("_id", -1))
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


# ── Admin image upload is handled by routers/admin.py (/admin/upload-image)
# which returns a proper public https:// URL compatible with FCM push notifications.
# The old base64 data URL approach is removed — FCM does not support data: URLs.


# ── DB indexes — created once on startup ──
@app.post("/register-fcm-token")
async def register_fcm_token(request: Request):
    """
    Public endpoint — saves FCM device token for push notifications.
    Called by Flutter after FCM init. No auth required (token lookup by phone/user_id).
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    fcm_token = (data.get("token") or data.get("fcm_token") or "").strip()
    phone     = (data.get("phone") or "").strip()
    user_id   = (data.get("user_id") or "").strip()

    if not fcm_token:
        return JSONResponse({"ok": False, "error": "no token"}, status_code=400)

    from routers.users import _phone_variants
    from bson import ObjectId
    import datetime

    user = None
    # Try user_id first
    if user_id:
        try:
            user = db.accounts.find_one({"_id": ObjectId(user_id)})
        except Exception:
            pass
    if not user and phone:
        variants = _phone_variants(phone)
        user = (db.accounts.find_one({"phone": {"$in": variants}}) or
                db.users.find_one({"phone": {"$in": variants}}))

    if user:
        db.accounts.update_one(
            {"_id": user["_id"]},
            {"$set": {"fcm_token": fcm_token, "fcm_updated_at": datetime.datetime.utcnow()}},
        )
        db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"fcm_token": fcm_token}},
        )
        print(f"[FCM] ✅ Token saved for {user.get('phone','?')}")
        return JSONResponse({"ok": True})
    else:
        # Save with phone key only — user may not be registered yet
        if phone:
            db.fcm_pending.update_one(
                {"phone": phone},
                {"$set": {"fcm_token": fcm_token, "updated_at": datetime.datetime.utcnow()}},
                upsert=True,
            )
        print(f"[FCM] ⚠️ User not found for phone={phone} user_id={user_id} — token pending")
        return JSONResponse({"ok": True, "note": "user not found, token queued"})


@app.on_event("startup")
def startup():
    try:
        admin.seed_admin()
    except Exception as e:
        print(f"⚠️  seed_admin skipped (DB unreachable at startup): {e}")
    # Seed RBAC collections (roles, super admin user)
    try:
        from routers.dashboard_auth import seed_rbac
        seed_rbac()
    except Exception as e:
        print(f"⚠️  seed_rbac skipped (DB unreachable at startup): {e}")
    try:
        _ensure_indexes()
    except Exception as e:
        print(f"⚠️  index creation skipped (DB unreachable at startup): {e}")
    # Ensure OTP TTL index for auto-expiry of otp_sessions collection
    try:
        from routers.otp_service import _ensure_otp_indexes
        _ensure_otp_indexes()
    except Exception as e:
        print(f"⚠️  OTP index warning: {e}")


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
        # Accounts — phone lookup (unified login)
        db.accounts.create_index([("phone", 1)], name="accounts_phone", background=True)
        db.accounts.create_index([("token", 1)], name="accounts_token", background=True)
        db.accounts.create_index([("roles", 1)], name="accounts_roles", background=True)
        # Users — phone lookup (legacy fallback)
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
        # Accounts — unified collection indexes
        db.accounts.create_index("phone",       unique=True, background=True, sparse=True)
        db.accounts.create_index("token",       background=True, sparse=True)
        db.accounts.create_index("roles",       background=True)
        db.accounts.create_index("merchant_id", background=True, sparse=True)
        db.accounts.create_index("user_id",     background=True, sparse=True)
        # PERMANENT DUPLICATE-BANNER FIX: hard DB-level guarantee that a single
        # banner_orders submission (order_id) can never produce more than one
        # merchant_banners document — even under concurrent/double-tap requests
        # that race past the application-level idempotency check. This is a
        # sparse unique index (old records with no source_order_id are unaffected).
        db.merchant_banners.create_index(
            "source_order_id",
            name="merchant_banners_source_order_id_unique",
            unique=True,
            sparse=True,
            background=True,
        )
        # PERMANENT DUPLICATE-BANNER FIX (approval side): the admin "Approve"
        # button upserts a promo_sliders doc keyed on source_banner_id. Without
        # a unique index backing that key, MongoDB's upsert is NOT safe against
        # concurrent requests (double-click, dashboard retry) — both can find
        # "no existing match" before either has inserted, producing two
        # promo_sliders documents for the same banner. This unique sparse index
        # makes that scenario impossible at the DB layer (old admin-created
        # banners with no source_banner_id are unaffected — sparse).
        # SELF-HEALING: pre-existing duplicate source_banner_id values (created
        # by the race before this fix existed) would make index creation fail,
        # so clean those up first — keep the oldest doc per source_banner_id.
        try:
            _sbid_groups = {}
            for _s in db.promo_sliders.find({"source_banner_id": {"$exists": True, "$ne": ""}}, {"_id": 1, "source_banner_id": 1, "created_at": 1}):
                _sbid_groups.setdefault(_s["source_banner_id"], []).append(_s)
            for _sbid, _docs in _sbid_groups.items():
                if len(_docs) < 2:
                    continue
                _docs_sorted = sorted(_docs, key=lambda d: str(d.get("created_at", "")))
                for _dup in _docs_sorted[1:]:
                    db.promo_sliders.delete_one({"_id": _dup["_id"]})
            if _sbid_groups:
                print("✅ Pre-index dedup: cleaned any pre-existing promo_sliders duplicates")
        except Exception as _dedup_err:
            print(f"⚠️  Pre-index promo_sliders dedup warning: {_dedup_err}")
        db.promo_sliders.create_index(
            "source_banner_id",
            name="promo_sliders_source_banner_id_unique",
            unique=True,
            sparse=True,
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
    # ── Server-side auth gate ──
    # Visiting this URL directly (bookmark, typed domain, stale tab) with no valid
    # session must NOT render the dashboard shell — redirect to the login page.
    token = request.cookies.get("admin_token") or \
            request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        return RedirectResponse(url="/admin", status_code=307)
    session_user = db.dashboard_users.find_one({"token": token}) or db.admins.find_one({"token": token})
    if not session_user:
        return RedirectResponse(url="/admin", status_code=307)
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
