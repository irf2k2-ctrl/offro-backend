"""
image_utils.py — compress and thumbnail base64 images using Pillow
Called on every store create/update to keep MongoDB lean.
"""
import base64, io, re
from PIL import Image

# ── settings ──────────────────────────────────────────────────
MAIN_MAX_W   = 800    # px — max width for the main store image
MAIN_QUALITY = 72     # JPEG quality for main image
THUMB_W      = 240    # px — thumbnail width (used in app card list)
THUMB_QUALITY= 65     # JPEG quality for thumbnail
FORMAT       = "JPEG" # output format (JPEG is universal; WebP needs extra Pillow build)
# ──────────────────────────────────────────────────────────────


def _decode_b64(data_uri: str) -> bytes | None:
    """Strip data URI prefix and decode base64 bytes."""
    try:
        match = re.match(r"data:image/[^;]+;base64,(.+)", data_uri, re.DOTALL)
        raw = match.group(1) if match else data_uri
        return base64.b64decode(raw + "==")  # padding-safe
    except Exception:
        return None


def _encode_b64(img: Image.Image, quality: int) -> str:
    """Encode PIL image to base64 data URI (JPEG)."""
    buf = io.BytesIO()
    # Convert RGBA/P to RGB before saving as JPEG
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    img.save(buf, format=FORMAT, quality=quality, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}"


def process_store_image(raw_b64: str) -> dict:
    """
    Given a raw base64 image string, return:
      { "image": <compressed main>, "image_thumb": <thumbnail> }
    Falls back to original if Pillow fails.
    """
    if not raw_b64 or not raw_b64.startswith("data:image"):
        return {}

    data = _decode_b64(raw_b64)
    if not data:
        return {"image": raw_b64}

    try:
        img = Image.open(io.BytesIO(data))

        # ── main image — resize if wider than MAIN_MAX_W ──
        if img.width > MAIN_MAX_W:
            ratio = MAIN_MAX_W / img.width
            img = img.resize((MAIN_MAX_W, int(img.height * ratio)), Image.LANCZOS)
        main_b64 = _encode_b64(img, MAIN_QUALITY)

        # ── thumbnail — fixed width ──
        ratio_t = THUMB_W / img.width
        thumb = img.resize((THUMB_W, int(img.height * ratio_t)), Image.LANCZOS)
        thumb_b64 = _encode_b64(thumb, THUMB_QUALITY)

        return {"image": main_b64, "image_thumb": thumb_b64}

    except Exception as e:
        # Never break the save flow — just return original
        print(f"[image_utils] compress failed: {e}")
        return {"image": raw_b64}
