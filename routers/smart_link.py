"""
OffrO Smart Link — https://theoffro.com/open

One generic link that only opens the OffrO app (no product / store routing):

  GET /open
      Landing page. Who sees what:
        * app installed        → the OS opens the app before this page loads
                                 (Android App Links / iOS Universal Links)
        * Android, no app      → intent:// link; Chrome falls back to Google Play
        * iPhone/iPad, no app  → redirected to the App Store listing
        * desktop / other      → this page, with Google Play + App Store buttons

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

No database access; nothing here touches any existing route.
"""
import html as _html
import json as _json
import os
import re
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, Response

router = APIRouter(tags=["Smart Link"])

ANDROID_PACKAGE = "com.offro.app"
IOS_BUNDLE_ID = "com.mibtech.offro"
PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=com.offro.app"
APP_STORE_URL = "https://apps.apple.com/il/app/offro/id6798829235"
LINK_HOST = "theoffro.com"
OPEN_PATH = "/open"

_HEX64 = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){31}$")
_TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")


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
                "components": [{"/": OPEN_PATH, "comment": "OffrO Smart Link"}],
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
def _android_intent_url():
    fallback = quote(PLAY_STORE_URL, safe="")
    return (f"intent://{LINK_HOST}{OPEN_PATH}"
            f"#Intent;scheme=https;package={ANDROID_PACKAGE};"
            f"S.browser_fallback_url={fallback};end")


def render_landing():
    e = _html.escape
    intent = e(_android_intent_url(), quote=True)
    page_url = f"https://{LINK_HOST}{OPEN_PATH}"
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OffrO</title>
<meta property="og:title" content="OffrO">
<meta property="og:description" content="Discover offers near you on OffrO">
<meta property="og:url" content="{e(page_url, quote=True)}">
<meta property="og:type" content="website">
<style>
body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#fdfbf6;color:#1a2e27}}
.card{{max-width:420px;margin:32px auto;background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 5px 16px rgba(0,0,0,.09)}}
.body{{padding:28px 20px 24px;text-align:center}}
h1{{font-size:26px;margin:0 0 6px}}
p{{margin:0 0 16px;color:#5f6f68}}
a.btn{{display:block;text-align:center;padding:13px;border-radius:12px;margin-top:10px;font-weight:700;text-decoration:none}}
.primary{{background:#3e5f55;color:#fff}}
.secondary{{border:1px solid #3e5f55;color:#3e5f55}}
</style></head>
<body><div class="card"><div class="body">
<h1>OffrO</h1>
<p>Discover offers near you</p>
<a class="btn primary" id="open" href="{intent}">Open in OffrO</a>
<a class="btn secondary" href="{e(PLAY_STORE_URL, quote=True)}">Get it on Google Play</a>
<a class="btn secondary" href="{e(APP_STORE_URL, quote=True)}">Download on the App Store</a>
</div></div>
<script>
(function(){{
  var ua = navigator.userAgent || "";
  if (/Android/i.test(ua)) {{ window.location.replace({_json.dumps(_android_intent_url())}); }}
  else if (/iPhone|iPad|iPod/i.test(ua)) {{ window.location.replace({_json.dumps(APP_STORE_URL)}); }}
  else {{ var o = document.getElementById("open"); if (o) o.style.display = "none"; }}
}})();
</script>
</body></html>"""


@router.get(OPEN_PATH, response_class=HTMLResponse, include_in_schema=False)
def open_offro():
    return HTMLResponse(content=render_landing(),
                        headers={"Cache-Control": "public, max-age=300"})
