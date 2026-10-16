"""
Product Smart Links — https://offro.app/p/<product_id>

What this module serves (all public, read-only, no auth):

  GET /p/{product_id}
      Landing page for a shared product link. Browsers that reach it are
      routed like this:
        * Android  → intent:// link (opens the OffrO app if installed, else the
                     Google Play listing)
        * iOS      → App Store listing (an installed app is opened by Universal
                     Links before the browser ever loads this page)
        * desktop  → this page, with the product summary + store buttons
      An unknown / expired product still gets the generic page with the store
      buttons — a shared link never dead-ends.

  GET /products/{product_id}/public
      JSON product (same field names the Store Detail product list uses) so the
      app can open Product Details straight from a link.

  GET /.well-known/assetlinks.json
  GET /.well-known/apple-app-site-association      (also /apple-app-site-association)
      Domain-verification files for Android App Links and iOS Universal Links.
      They need values only the app owner has, supplied as environment
      variables:
          ANDROID_SHA256_FINGERPRINTS  comma-separated SHA-256 signing-cert
                                       fingerprints (Play App Signing key, plus
                                       the upload key for sideloaded builds)
          APPLE_TEAM_ID                10-character Apple Team ID
      When a value is not configured the matching file answers 404 — it is never
      served with placeholder values, because a wrong file silently breaks
      verification.

Nothing here touches analytics, pricing or any existing route.
"""
import html as _html
import json as _json
import os
import re
from urllib.parse import quote

from bson import ObjectId
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, Response

from database import db
from routers.public import _end_date_passed, _safe_end_date

router = APIRouter(tags=["Product Links"])

ANDROID_PACKAGE = "com.offro.app"
IOS_BUNDLE_ID = "com.mibtech.offro"
PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=com.offro.app"
APP_STORE_URL = "https://apps.apple.com/il/app/offro/id6798829235"
LINK_HOST = "offro.app"

_HEX64 = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){31}$")
_TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")


# ── data lookup ───────────────────────────────────────────────────────────
def _oid_or_none(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _image(p):
    for k in ("logo_url", "logo_thumb", "image_url", "logo", "image"):
        v = str(p.get(k, "") or "")
        if v.startswith("http") or v.startswith("data:"):
            return v
    return ""


def _expired(p):
    for k in ("end_date", "validity_end"):
        raw = p.get(k)
        if raw is None or raw == "":
            continue
        passed = _end_date_passed(raw)
        if passed is not None:
            return passed
    return False


def _find_product(pid):
    """Return (doc, kind) for a visible product, else (None, None).
    Same visibility rules as the Store Detail product list: approved, not
    expired, standard-product mirror copies skipped."""
    oid = _oid_or_none(pid)
    if oid is None:
        return None, None
    p = db.merchant_vouchers.find_one(
        {"_id": oid, "status": "approved", "approval_status": "approved"})
    if p and not _expired(p):
        return p, "premium"
    p = db.gift_vouchers.find_one({"_id": oid, "approval_status": "approved"})
    if (p and not p.get("source_voucher_id")
            and p.get("is_active", True) not in (False, "false", "0", 0)
            and not _expired(p)):
        return p, "standard"
    return None, None


def _find_store(store_id):
    if not store_id:
        return None
    clauses = [{"_id": _oid_or_none(store_id)}] if _oid_or_none(store_id) else []
    if not clauses:
        return None
    return db.stores.find_one(clauses[0])


def _product_payload(p, kind):
    store = _find_store(p.get("store_id")) or {}
    store_id = str(p.get("store_id") or "")
    rating = float(store.get("rating") or store.get("admin_rating") or 0)
    offer = p.get("offer_text", "") if kind == "premium" else (p.get("text", "") or p.get("offer_text", ""))
    out = {
        "_id": str(p["_id"]),
        "title": p.get("title", ""),
        "offer_text": offer,
        "logo_url": _image(p),
        "price": str(p.get("price", "") or ""),
        "original_price": str(p.get("original_price", "") or ""),
        "discount": str(p.get("discount_label", "") or "") if kind == "premium" else "",
        "validity": _safe_end_date(p) or (p.get("validity", "") or ""),
        "end_date": str(p.get("end_date", "") or ""),
        "rating": rating,
        "store_id": store_id,
        "store_name": store.get("store_name", "") or p.get("store_name", ""),
        "city": store.get("city", "") or p.get("city", ""),
        "area": store.get("area", "") or p.get("area", ""),
        "category": p.get("category", "") or store.get("category", ""),
        "product_type": kind,
    }
    out["store"] = {
        "_id": store_id,
        "store_name": out["store_name"],
        "city": out["city"],
        "area": out["area"],
        "rating": rating,
        "image_url": store.get("image_url", "") or "",
    }
    return out


@router.get("/products/{product_id}/public")
def public_product(product_id: str):
    """One visible product, for opening Product Details from a shared link."""
    p, kind = _find_product(product_id)
    if not p:
        return JSONResponse(status_code=404, content={"detail": "Product not found"})
    return _product_payload(p, kind)


# ── domain verification files ─────────────────────────────────────────────
def _android_fingerprints():
    raw = os.environ.get("ANDROID_SHA256_FINGERPRINTS", "")
    prints = [x.strip().upper() for x in raw.split(",") if x.strip()]
    return [x for x in prints if _HEX64.match(x)]


def build_assetlinks(prints):
    return [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": ANDROID_PACKAGE,
            "sha256_cert_fingerprints": prints,
        },
    }]


def build_aasa(team_id):
    return {
        "applinks": {
            "details": [{
                "appIDs": [f"{team_id}.{IOS_BUNDLE_ID}"],
                "components": [{"/": "/p/*", "comment": "Product links"}],
            }],
        },
    }


@router.get("/.well-known/assetlinks.json", include_in_schema=False)
def assetlinks():
    prints = _android_fingerprints()
    if not prints:
        return JSONResponse(status_code=404, content={
            "detail": "ANDROID_SHA256_FINGERPRINTS is not configured"})
    return JSONResponse(content=build_assetlinks(prints))


@router.get("/.well-known/apple-app-site-association", include_in_schema=False)
@router.get("/apple-app-site-association", include_in_schema=False)
def apple_app_site_association():
    team = os.environ.get("APPLE_TEAM_ID", "").strip().upper()
    if not _TEAM_ID.match(team):
        return JSONResponse(status_code=404, content={
            "detail": "APPLE_TEAM_ID is not configured"})
    # Apple requires application/json (also served without a redirect).
    return Response(content=_json.dumps(build_aasa(team)), media_type="application/json")


# ── landing page ──────────────────────────────────────────────────────────
def _android_intent_url(pid):
    fallback = quote(PLAY_STORE_URL, safe="")
    return (f"intent://{LINK_HOST}/p/{quote(pid, safe='')}"
            f"#Intent;scheme=https;package={ANDROID_PACKAGE};"
            f"S.browser_fallback_url={fallback};end")


def render_landing(pid, payload=None):
    e = _html.escape
    title = (payload or {}).get("title") or "OffrO"
    store = (payload or {}).get("store_name", "")
    price = (payload or {}).get("price", "")
    orig = (payload or {}).get("original_price", "")
    img = (payload or {}).get("logo_url", "")
    img = img if img.startswith("http") else ""
    desc = " · ".join(x for x in [store, f"₹{price}" if price else ""] if x) or "Discover offers near you on OffrO"
    lines = []
    if store:
        lines.append(f'<p class="store">{e(store)}</p>')
    if price:
        o = f' <s>₹{e(orig)}</s>' if orig and orig != price else ""
        lines.append(f'<p class="price">₹{e(price)}{o}</p>')
    intent = e(_android_intent_url(pid), quote=True)
    page_url = f"https://{LINK_HOST}/p/{quote(pid, safe='')}"
    og_img = f'<meta property="og:image" content="{e(img, quote=True)}">' if img else ""
    img_tag = f'<img class="hero" src="{e(img, quote=True)}" alt="">' if img else ""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title) if payload else "OffrO"}{" · OffrO" if payload else ""}</title>
<meta property="og:title" content="{e(title, quote=True)}">
<meta property="og:description" content="{e(desc, quote=True)}">
<meta property="og:url" content="{e(page_url, quote=True)}">
<meta property="og:type" content="website">
{og_img}
<style>
body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#fdfbf6;color:#1a2e27}}
.card{{max-width:420px;margin:32px auto;background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 5px 16px rgba(0,0,0,.09)}}
.hero{{width:100%;display:block;aspect-ratio:1/.57;object-fit:cover}}
.body{{padding:18px 20px 22px}}
h1{{font-size:20px;margin:0 0 6px}}
.store{{margin:0 0 8px;color:#5f6f68}}
.price{{font-size:22px;font-weight:800;color:#2f5a47;margin:0 0 14px}}
.price s{{font-size:14px;font-weight:500;color:#9e9e9e}}
a.btn{{display:block;text-align:center;padding:13px;border-radius:12px;margin-top:10px;font-weight:700;text-decoration:none}}
.primary{{background:#3e5f55;color:#fff}}
.secondary{{border:1px solid #3e5f55;color:#3e5f55}}
</style></head>
<body><div class="card">{img_tag}<div class="body">
<h1>{e(title)}</h1>
{''.join(lines)}
<a class="btn primary" id="open" href="{intent}">Open in OffrO</a>
<a class="btn secondary" href="{e(PLAY_STORE_URL, quote=True)}">Get it on Google Play</a>
<a class="btn secondary" href="{e(APP_STORE_URL, quote=True)}">Download on the App Store</a>
</div></div>
<script>
(function(){{
  var ua = navigator.userAgent || "";
  if (/Android/i.test(ua)) {{ window.location.replace({_json.dumps(_android_intent_url(pid))}); }}
  else if (/iPhone|iPad|iPod/i.test(ua)) {{ window.location.replace({_json.dumps(APP_STORE_URL)}); }}
  else {{ var o = document.getElementById("open"); if (o) o.style.display = "none"; }}
}})();
</script>
</body></html>"""


@router.get("/p/{product_id}", response_class=HTMLResponse, include_in_schema=False)
def product_landing(product_id: str):
    p, kind = _find_product(product_id)
    payload = _product_payload(p, kind) if p else None
    return HTMLResponse(content=render_landing(product_id, payload),
                        headers={"Cache-Control": "public, max-age=60"})
