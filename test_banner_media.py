"""Banner image/video backend checks (no real DB / Cloudinary).
Run:  BACKEND=<backend dir> python test_banner_media.py"""
import sys, os, types, base64
sys.path.insert(0, os.environ.get("BACKEND", os.path.dirname(os.path.abspath(__file__))))
import mongomock
db = mongomock.MongoClient()["offro_db"]
stub = types.ModuleType("database"); stub.db = db; stub.client = None
sys.modules["database"] = stub
for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
    os.environ.pop(k, None)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import routers.merchant_app as ma

n = 0
def check(name, cond):
    global n
    print(("PASS " if cond else "FAIL ") + name); assert cond, name; n += 1

def uri(nbytes, kind="video/mp4"):
    return f"data:{kind};base64," + base64.b64encode(b"\0" * nbytes).decode()

# size helper
check("decoded size exact", ma._b64_decoded_size(uri(1000)) == 1000)
check("decoded size w/ padding", ma._b64_decoded_size(uri(1001)) == 1001 and ma._b64_decoded_size(uri(1002)) == 1002)
check("10 MB limit constant", ma.BANNER_VIDEO_MAX_BYTES == 10 * 1024 * 1024)

# poster url
v = "https://res.cloudinary.com/demo/video/upload/v1/offro/banners/abc.mp4"
check("poster url is a jpg still", ma._video_poster_url(v) == "https://res.cloudinary.com/demo/video/upload/so_0,w_300,c_fill,q_auto/v1/offro/banners/abc.jpg")
check("poster '' for non-cloudinary", ma._video_poster_url("https://x.com/a.mp4") == "" and ma._video_poster_url("data:video/mp4;base64,AAA") == "")

# _banner_media: image path unchanged, no media_type => image
img = "https://res.cloudinary.com/demo/image/upload/v1/offro/banners/a.jpg"
check("image (no media_type) -> image", ma._banner_media({"image_url": img}) == (img, ma._make_thumb_url(img), "image"))
check("image explicit", ma._banner_media({"image_url": img, "media_type": "image"})[2] == "image")
check("junk media_type -> image", ma._banner_media({"image_url": img, "media_type": "gif"})[2] == "image")
# video with CDN URL passes through
u, t, mt = ma._banner_media({"image_url": v, "media_type": "video"})
check("video url passthrough", (u, mt) == (v, "video") and t.endswith("/abc.jpg"))
# oversize inline video rejected
try:
    ma._banner_media({"image_url": uri(10 * 1024 * 1024 + 1), "media_type": "video"}); ok = False
except HTTPException as e:
    ok = e.status_code == 400 and e.detail == "Video is too large. Please select a video up to 10 MB."
check("oversize inline video -> 400 + message", ok)
# exactly 10 MB inline video is accepted (no Cloudinary configured -> passthrough)
u, t, mt = ma._banner_media({"image_url": uri(10 * 1024 * 1024), "media_type": "video"})
check("10 MB inline video accepted", mt == "video" and u.startswith("data:video"))

# upload endpoint
app = FastAPI(); app.include_router(ma.router, prefix="/merchant")
app.dependency_overrides[ma.get_merchant] = lambda: {"_id": "m1", "name": "M"}
c = TestClient(app)
r = c.post("/merchant/banners/upload-video", json={"video": uri(2000)})
check("upload ok (no CDN configured => passthrough)", r.status_code == 200 and r.json()["media_type"] == "video")
r = c.post("/merchant/banners/upload-video", json={"video": uri(10 * 1024 * 1024 + 1)})
check("upload >10MB -> 400 message", r.status_code == 400 and r.json()["detail"] == "Video is too large. Please select a video up to 10 MB.")
check("upload non-video -> 400", c.post("/merchant/banners/upload-video", json={"video": uri(10, "image/png")}).status_code == 400)
check("upload empty -> 400", c.post("/merchant/banners/upload-video", json={}).status_code == 400)
print(f"\n{n} checks passed")


# ---- Cloudinary signature regression (root cause of "Video upload failed") ----
def _test_video_signature():
    import os, hashlib, re
    from unittest import mock
    import routers.admin as adm
    os.environ.update(CLOUDINARY_CLOUD_NAME="c", CLOUDINARY_API_KEY="k", CLOUDINARY_API_SECRET="s3cr3t")
    seen = {}
    class R:  status_code = 200; text = ""; 
    R.json = lambda self: {"secure_url": "https://res.cloudinary.com/c/video/upload/v1/x.mp4"}
    def fake_post(url, data=None, timeout=None):
        seen.update(url=url, data=data); return R()
    with mock.patch("requests.post", fake_post):
        out = adm._cloudinary_upload_video("data:video/mp4;base64,AAAA", folder="offro/banners")
    d = seen["data"]
    expect = hashlib.sha1(f"folder=offro/banners&timestamp={d['timestamp']}s3cr3t".encode()).hexdigest()
    assert d["signature"] == expect, "signature must not include resource_type"
    assert seen["url"].endswith("/c/video/upload") and out.startswith("https://")
    print("ok video signature signs only folder+timestamp")
_test_video_signature()
