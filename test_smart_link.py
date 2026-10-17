"""Smart Link tests. Run:  BACKEND=<backend dir> python test_smart_link.py
No database needed — /open and the .well-known files never touch it."""
import sys, os, json
sys.path.insert(0, os.environ.get("BACKEND", os.path.dirname(os.path.abspath(__file__))))
from fastapi import FastAPI
from fastapi.testclient import TestClient
import routers.smart_link as sl

app = FastAPI(); app.include_router(sl.router)
c = TestClient(app)
n = 0
def check(name, cond):
    global n
    print(("PASS " if cond else "FAIL ") + name); assert cond, name; n += 1

ANDROID_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) Chrome/120 Mobile Safari/537.36"
IOS_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"
DESK_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"

# /open ----------------------------------------------------------------
r = c.get("/open", headers={"user-agent": DESK_UA})
check("/open 200 html", r.status_code == 200 and "text/html" in r.headers["content-type"])
t = r.text
check("play store button", sl.PLAY_STORE_URL in t)
check("app store button", sl.APP_STORE_URL in t)
check("android intent has package + play fallback",
      "package=com.offro.app" in t and "S.browser_fallback_url=https%3A%2F%2Fplay.google.com" in t
      and "intent://offro.app/open#Intent;scheme=https" in t)
check("android script redirects to intent", "/Android/i.test(ua)" in t)
check("ios script redirects to app store", "/iPhone|iPad|iPod/i.test(ua)" in t)
check("no product/store specifics", "offro.app/p/" not in t and "offro.app/store/" not in t)
check("/open ignores query", c.get("/open?x=1").status_code == 200)
check("/p/<id> gone", c.get("/p/6ac74822399939d9d31a98f8").status_code == 404)
check("/store/<id> gone", c.get("/store/6ac74822399939d9d31a98f8").status_code == 404)
check("/products/<id>/public gone", c.get("/products/6ac74822399939d9d31a98f8/public").status_code == 404)

# assetlinks ------------------------------------------------------------
os.environ.pop("ANDROID_SHA256_FINGERPRINTS", None)
check("assetlinks 404 when unset", c.get("/.well-known/assetlinks.json").status_code == 404)
fp = ":".join(["AB"] * 32)
os.environ["ANDROID_SHA256_FINGERPRINTS"] = fp + ",not-a-fingerprint"
j = c.get("/.well-known/assetlinks.json").json()
check("assetlinks package + valid fingerprint only",
      j[0]["target"]["package_name"] == "com.offro.app"
      and j[0]["target"]["sha256_cert_fingerprints"] == [fp]
      and j[0]["relation"] == ["delegate_permission/common.handle_all_urls"])

# AASA ------------------------------------------------------------------
os.environ.pop("APPLE_TEAM_ID", None)
check("aasa 404 when unset", c.get("/.well-known/apple-app-site-association").status_code == 404)
os.environ["APPLE_TEAM_ID"] = "bad"
check("aasa 404 for malformed team id", c.get("/.well-known/apple-app-site-association").status_code == 404)
os.environ["APPLE_TEAM_ID"] = "ABCDE12345"
for p in ("/.well-known/apple-app-site-association", "/apple-app-site-association"):
    r = c.get(p)
    j = json.loads(r.text)
    check(f"aasa {p}", r.status_code == 200 and r.headers["content-type"].startswith("application/json")
          and j["applinks"]["details"][0]["appIDs"] == ["ABCDE12345.com.mibtech.offro"]
          and j["applinks"]["details"][0]["components"][0]["/"] == "/open")
print(f"\n{n} checks passed")
