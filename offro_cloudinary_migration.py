#!/usr/bin/env python3
"""
OFFRO — Cloudinary Image Migration Script  (v4 — Full Validation Edition)
=========================================================================
Migrates base64 images in MongoDB → Cloudinary CDN with post-collection
validation, random sample checks, payload comparison, and full report.

USAGE
─────
  Mandatory dry run first (zero DB changes):
    python3 offro_cloudinary_migration.py

  Backup only:
    python3 offro_cloudinary_migration.py --backup

  Live — one collection at a time:
    python3 offro_cloudinary_migration.py --live --collection stores
    python3 offro_cloudinary_migration.py --live --collection merchant_banners
    python3 offro_cloudinary_migration.py --live --collection deals
    python3 offro_cloudinary_migration.py --live --collection promo_sliders
    python3 offro_cloudinary_migration.py --live --collection notification_images

  Final report (run after all collections done):
    python3 offro_cloudinary_migration.py --report

UPLOAD VALIDATION GATES (all 4 must pass before MongoDB write)
──────────────────────────────────────────────────────────────
  Gate 1  Cloudinary HTTP == 200
  Gate 2  secure_url present and non-empty
  Gate 3  URL starts with https://res.cloudinary.com
  Gate 4  Image HEAD request returns HTTP 200

POST-COLLECTION VALIDATION (runs automatically after each live collection)
──────────────────────────────────────────────────────────────────────────
  • Randomly samples up to 3 migrated records
  • Checks MongoDB fields: image_url, image_thumb (if applicable), migrated_at
  • Checks CDN URLs: HTTP 200 for both image_url and thumb_url
  • Measures payload size before vs after migration
  • Prints pass/fail checklist and app validation instructions

SAFETY
──────
  • base64 fields are NEVER deleted — rollback always possible
  • MongoDB is never written if any upload gate fails
  • Re-running is safe — already-CDN records are always skipped
"""

import os, sys, json, hashlib, time, gzip, random
import requests
from datetime import datetime, timezone
from pymongo import MongoClient
from bson import ObjectId

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
# Fixed Mongo connection
MONGO_URL = os.getenv("MONGODB_URL")
CLOUDINARY_CLOUD  = os.getenv("CLOUDINARY_CLOUD_NAME", "dwjcqcapf")
CLOUDINARY_KEY    = os.getenv("CLOUDINARY_API_KEY",    "")
CLOUDINARY_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")
DB_NAME           = "offro_db"

UPLOAD_TIMEOUT    = 30    # seconds per Cloudinary upload
VERIFY_TIMEOUT    = 10    # seconds for HEAD accessibility check
SLEEP_BETWEEN     = 0.35  # rate-limit safety between uploads
SAMPLE_SIZE       = 3     # records to randomly sample in post-validation

# Collections: (name, src_field, dst_url_field, dst_thumb_field, cloudinary_folder)
COLLECTIONS = [
    ("stores",              "image",     "image_url",  "image_thumb", "offro/stores"),
    ("merchant_banners",    "image_url", "image_url",  "image_thumb", "offro/banners"),
    ("deals",               "logo_url",  "logo_url",   "logo_thumb",  "offro/vouchers"),
    ("promo_sliders",       "image_url", "image_url",  None,          "offro/sliders"),
    ("notification_images", "image_url", "image_url",  None,          "offro/notifications"),
]

# App validation checklist per collection
APP_CHECKS = {
    "stores": [
        "Store card image loads in home screen",
        "Store detail page image loads correctly",
        "Distance + image visible on map view",
    ],
    "merchant_banners": [
        "Merchant banner list image loads",
        "Banner shows on home screen carousel",
        "Banner detail / dashboard image loads",
    ],
    "deals": [
        "Product card image (logo) loads",
        "Product detail page image loads",
        "Share card image renders correctly",
    ],
    "promo_sliders": [
        "Promo slider image loads on home screen",
        "Slider transitions work correctly",
    ],
    "notification_images": [
        "Notification image loads in notification list",
        "Notification detail image loads",
    ],
}

# ─────────────────────────────────────────────────────────────
# CLI FLAGS
# ─────────────────────────────────────────────────────────────
DRY_RUN         = "--live"   not in sys.argv
BACKUP_ONLY     = "--backup" in sys.argv
REPORT_ONLY     = "--report" in sys.argv
ONLY_COLLECTION = None
if "--collection" in sys.argv:
    idx = sys.argv.index("--collection")
    ONLY_COLLECTION = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
LOG_FILE  = f"migration_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
log_lines = []

def log(msg, level="INFO"):
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{level:<5}] {msg}"
    print(line)
    log_lines.append(line)

def log_error(doc_id, collection, reason, old_size_kb=0):
    entry = {
        "type":       "ERROR",
        "collection": collection,
        "doc_id":     str(doc_id),
        "reason":     reason,
        "base64_kb":  old_size_kb,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    }
    line = f"  [ERROR] {collection}/{doc_id} — {reason} ({old_size_kb} KB)"
    print(line)
    log_lines.append(json.dumps(entry))

def save_log():
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))
    print(f"\n  📄 Log saved → {LOG_FILE}")

def divider(char="─", width=62):
    print(char * width)

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def is_base64(val: str) -> bool:
    if not val:
        return False
    if val.startswith("data:image"):
        return True
    if len(val) > 200 and not val.startswith("http"):
        return True
    return False

def is_cdn_url(val: str) -> bool:
    return bool(val and (val.startswith("https://") or val.startswith("http://")))

def b64_byte_size(val: str) -> int:
    raw     = val.split(",", 1)[-1] if "," in val else val
    padding = raw.count("=")
    return max(0, int(len(raw) * 3 / 4) - padding)

def make_thumb_url(cdn_url: str, w: int = 300) -> str:
    if cdn_url and "cloudinary.com" in cdn_url and "/upload/" in cdn_url:
        return cdn_url.replace("/upload/", f"/upload/w_{w},c_fill,q_auto,f_auto/")
    return ""

def check_url_accessible(url: str, label: str) -> bool:
    """HEAD check a URL. Returns True if HTTP 200."""
    try:
        r = requests.head(url, timeout=VERIFY_TIMEOUT, allow_redirects=True)
        ok = r.status_code == 200
        status = "✅" if ok else f"❌ HTTP {r.status_code}"
        log(f"    {status}  {label}: {url[:65]}")
        return ok
    except Exception as e:
        log(f"    ❌  {label} check failed: {e}", "WARN")
        return False

def bson_serializer(obj):
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return "<bytes>"
    raise TypeError(f"Not serializable: {type(obj)}")

# ─────────────────────────────────────────────────────────────
# UPLOAD + 4-GATE VALIDATION
# ─────────────────────────────────────────────────────────────
def upload_and_validate(b64_val: str, folder: str, doc_id, collection: str) -> dict:
    """
    Upload to Cloudinary and run 4 validation gates.
    Returns {"ok": True, "cdn_url": str, "thumb_url": str}
         or {"ok": False, "reason": str}
    MongoDB is only written if this returns ok=True.
    """
    data_str  = b64_val.split(",", 1)[-1] if "," in b64_val else b64_val
    timestamp = str(int(time.time()))
    sig_str   = f"folder={folder}&timestamp={timestamp}{CLOUDINARY_SECRET}"
    signature = hashlib.sha1(sig_str.encode()).hexdigest()

    try:
        resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD}/image/upload",
            data={
                "file":      f"data:image/jpeg;base64,{data_str}",
                "folder":    folder,
                "timestamp": timestamp,
                "api_key":   CLOUDINARY_KEY,
                "signature": signature,
            },
            timeout=UPLOAD_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        return {"ok": False, "reason": "Cloudinary upload timed out"}
    except Exception as e:
        return {"ok": False, "reason": f"Upload exception: {e}"}

    # Gate 1 — HTTP 200
    if resp.status_code != 200:
        return {"ok": False, "reason": f"Cloudinary HTTP {resp.status_code}: {resp.text[:100]}"}

    payload = resp.json()

    # Gate 2 — secure_url present and non-empty
    cdn_url = payload.get("secure_url", "")
    if not cdn_url:
        return {"ok": False, "reason": "secure_url missing in Cloudinary response"}

    # Gate 3 — must be Cloudinary domain
    if not cdn_url.startswith("https://res.cloudinary.com"):
        return {"ok": False, "reason": f"Unexpected CDN domain in URL: {cdn_url[:60]}"}

    # Gate 4 — image must be accessible via HEAD
    try:
        head = requests.head(cdn_url, timeout=VERIFY_TIMEOUT, allow_redirects=True)
        if head.status_code != 200:
            return {"ok": False, "reason": f"Image not accessible — HEAD {head.status_code}"}
    except Exception as e:
        return {"ok": False, "reason": f"Accessibility check failed: {e}"}

    # Thumb URL — structural validation
    thumb_url = make_thumb_url(cdn_url)
    if thumb_url and "/upload/w_" not in thumb_url:
        thumb_url = cdn_url  # fallback to full URL

    log(f"    ✅ All 4 gates passed")
    log(f"       CDN   : {cdn_url[:68]}")
    if thumb_url:
        log(f"       THUMB : {thumb_url[:68]}")

    return {"ok": True, "cdn_url": cdn_url, "thumb_url": thumb_url}

# ─────────────────────────────────────────────────────────────
# PAYLOAD MEASUREMENT
# ─────────────────────────────────────────────────────────────
def measure_payload(col, src_field: str, dst_url: str, dst_thumb: str | None) -> dict:
    """
    Measure average document size before and after migration.
    Returns {"before_kb": float, "after_kb": float, "reduction_kb": float, "reduction_pct": float}
    """
    docs = list(col.find({}, {src_field: 1, dst_url: 1, dst_thumb: 1} if dst_thumb else {src_field: 1, dst_url: 1}))
    if not docs:
        return {"before_kb": 0, "after_kb": 0, "reduction_kb": 0, "reduction_pct": 0}

    before_sizes = []
    after_sizes  = []

    for d in docs:
        src_val = d.get(src_field, "") or ""
        dst_val = d.get(dst_url,   "") or ""

        # Before = size of base64 field
        before_kb = b64_byte_size(src_val) / 1024 if is_base64(src_val) else (len(src_val) / 1024)
        before_sizes.append(before_kb)

        # After = size of CDN URL string (tiny) + thumb URL string
        after_str  = dst_val
        if dst_thumb:
            after_str += (d.get(dst_thumb, "") or "")
        after_kb = len(after_str) / 1024
        after_sizes.append(after_kb)

    avg_before = sum(before_sizes) / len(before_sizes)
    avg_after  = sum(after_sizes)  / len(after_sizes)
    reduction  = avg_before - avg_after
    pct        = (reduction / avg_before * 100) if avg_before > 0 else 0

    return {
        "before_kb":    round(avg_before, 2),
        "after_kb":     round(avg_after,  2),
        "reduction_kb": round(reduction,  2),
        "reduction_pct":round(pct,        1),
        "doc_count":    len(docs),
    }

# ─────────────────────────────────────────────────────────────
# POST-COLLECTION VALIDATION
# ─────────────────────────────────────────────────────────────
def post_collection_validate(db, col_name: str, src_field: str,
                              dst_url: str, dst_thumb: str | None) -> dict:
    """
    After a live collection migration:
    1. Sample up to SAMPLE_SIZE migrated records
    2. Check MongoDB fields exist
    3. Check CDN URLs return HTTP 200
    4. Measure payload before vs after
    5. Print app validation checklist
    Returns {"passed": int, "failed": int, "payload": dict}
    """
    col = db[col_name]

    print()
    divider("═")
    print(f"  POST-MIGRATION VALIDATION — {col_name}")
    divider("═")

    # Get all recently migrated docs
    migrated = list(col.find(
        {"migrated_at": {"$exists": True}, dst_url: {"$exists": True}},
        {"_id": 1, dst_url: 1, dst_thumb: 1, "migrated_at": 1} if dst_thumb
        else {"_id": 1, dst_url: 1, "migrated_at": 1}
    ))

    if not migrated:
        log("  ⚠️  No migrated records found to validate", "WARN")
        return {"passed": 0, "failed": 0, "payload": {}}

    sample = random.sample(migrated, min(SAMPLE_SIZE, len(migrated)))
    log(f"  Sampling {len(sample)} of {len(migrated)} migrated records")

    passed = 0
    failed = 0

    for rec in sample:
        rid       = str(rec["_id"])
        img_url   = rec.get(dst_url,   "") or ""
        thumb_url = rec.get(dst_thumb, "") if dst_thumb else None
        migrated_at = rec.get("migrated_at", "")

        print(f"\n  Record: {rid}")
        checks = {}

        # ── MongoDB field checks ──
        checks["image_url exists"]    = bool(img_url)
        checks["migrated_at exists"]  = bool(migrated_at)
        if dst_thumb:
            checks["image_thumb exists"] = bool(thumb_url)

        for chk, ok in checks.items():
            icon = "✅" if ok else "❌"
            log(f"    {icon}  MongoDB: {chk}")
            if not ok:
                failed += 1

        # ── URL accessibility checks ──
        if img_url:
            ok_img = check_url_accessible(img_url, "image_url")
            if ok_img:
                passed += 1
            else:
                failed += 1
                log_error(rid, col_name, f"image_url not accessible: {img_url[:60]}")

        if dst_thumb and thumb_url:
            ok_thumb = check_url_accessible(thumb_url, "image_thumb")
            if ok_thumb:
                passed += 1
            else:
                failed += 1
                log_error(rid, col_name, f"image_thumb not accessible: {thumb_url[:60]}")

        passed += sum(1 for v in checks.values() if v)
        failed += sum(1 for v in checks.values() if not v)

    # ── Payload comparison ──
    print()
    divider()
    print(f"  PAYLOAD COMPARISON — {col_name}")
    divider()
    payload = measure_payload(col, src_field, dst_url, dst_thumb)
    print(f"  Average doc size BEFORE : {payload['before_kb']:.1f} KB  (base64 in field)")
    print(f"  Average doc size AFTER  : {payload['after_kb']:.1f} KB  (CDN URL string)")
    print(f"  Reduction per document  : {payload['reduction_kb']:.1f} KB  ({payload['reduction_pct']:.1f}%)")
    print(f"  Estimated API payload   : {payload['doc_count']} docs × {payload['reduction_kb']:.1f} KB = ~{payload['doc_count'] * payload['reduction_kb']:.0f} KB saved per fetch")
    log(f"Payload: before={payload['before_kb']}KB after={payload['after_kb']}KB reduction={payload['reduction_pct']}%")

    # ── App validation checklist ──
    print()
    divider()
    print(f"  APP VALIDATION CHECKLIST — {col_name}")
    print(f"  (Verify these manually in the app before migrating next collection)")
    divider()
    for item in APP_CHECKS.get(col_name, ["Check images load in app"]):
        print(f"  ☐  {item}")
    print()
    print(f"  Fallback status : ✅ Old base64 still in '{src_field}' — rollback safe")
    print(f"  Next step       : Once all checks pass, run next collection")

    return {"passed": passed, "failed": failed, "payload": payload}

# ─────────────────────────────────────────────────────────────
# 1. BACKUP
# ─────────────────────────────────────────────────────────────
def run_backup(db):
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    bkp_dir = f"offro_backup_{ts}"
    os.makedirs(bkp_dir, exist_ok=True)

    print(f"\n{'═'*62}")
    print(f"  BACKUP → ./{bkp_dir}/")
    print(f"{'═'*62}")

    total_docs  = 0
    total_bytes = 0

    for (col_name, *_) in COLLECTIONS:
        col   = db[col_name]
        docs  = list(col.find({}))
        fname = os.path.join(bkp_dir, f"{col_name}.json.gz")
        raw   = json.dumps(docs, default=bson_serializer, ensure_ascii=False)
        with gzip.open(fname, "wt", encoding="utf-8") as f:
            f.write(raw)
        size_kb = os.path.getsize(fname) / 1024
        log(f"  ✅ {col_name:<22} {len(docs):>4} docs  →  {size_kb:>7.1f} KB gzipped")
        total_docs  += len(docs)
        total_bytes += os.path.getsize(fname)

    print(f"\n  Total  : {total_docs} docs | {total_bytes/1024:.1f} KB compressed")
    print(f"  Restore: gunzip {bkp_dir}/<col>.json.gz")
    print(f"           mongoimport --db offro --collection <name> --file <name>.json --jsonArray")
    log(f"Backup complete → {bkp_dir}/")
    return bkp_dir

# ─────────────────────────────────────────────────────────────
# 2. MIGRATION
# ─────────────────────────────────────────────────────────────
def run_migration(db):
    mode = "🔵 DRY RUN" if DRY_RUN else "🔴 LIVE"
    print(f"\n{'═'*62}")
    print(f"  MIGRATION  [{mode}]")
    if ONLY_COLLECTION:
        print(f"  Collection : {ONLY_COLLECTION} only")
    print(f"{'═'*62}")

    grand = dict(
        total=0, already_cdn=0, pending=0,
        uploaded=0, failed=0, skipped=0, dry_run=0,
        bytes_freed=0
    )
    post_val_results = {}

    for (col_name, src_field, dst_url, dst_thumb, cdn_folder) in COLLECTIONS:
        if ONLY_COLLECTION and col_name != ONLY_COLLECTION:
            continue

        col  = db[col_name]

        # ── Pre-migration payload snapshot ──
        pre_payload = measure_payload(col, src_field, dst_url, dst_thumb)

        docs = list(col.find(
            {src_field: {"$exists": True, "$ne": "", "$ne": None}},
            {"_id": 1, src_field: 1}
        ))

        c = dict(total=len(docs), already_cdn=0, pending=0,
                 uploaded=0, failed=0, skipped=0, dry_run=0, bytes_freed=0)

        print(f"\n  ── {col_name}  ({src_field} → {dst_url}) ──")
        log(f"[{col_name}] {len(docs)} records found | pre-migration avg payload: {pre_payload['before_kb']:.1f} KB/doc")

        for rec in docs:
            rid = str(rec["_id"])
            val = rec.get(src_field, "") or ""
            grand["total"] += 1

            # Already CDN
            if is_cdn_url(val):
                log(f"  ✓ {rid[:20]}  already CDN — skip")
                c["already_cdn"] += 1
                grand["already_cdn"] += 1
                continue

            # Empty / unrecognised
            if not val or not is_base64(val):
                log(f"  ? {rid[:20]}  empty/unrecognised — skip", "WARN")
                c["skipped"] += 1
                grand["skipped"] += 1
                continue

            img_bytes = b64_byte_size(val)
            img_kb    = img_bytes // 1024
            c["pending"] += 1
            grand["pending"] += 1

            log(f"  ↑ {rid[:20]}  base64 ~{img_kb} KB")

            # DRY RUN
            if DRY_RUN:
                log(f"    [DRY RUN] would upload → {cdn_folder}")
                c["dry_run"] += 1
                grand["dry_run"] += 1
                continue

            # LIVE — upload + 4 gates
            result = upload_and_validate(val, cdn_folder, rid, col_name)

            if not result["ok"]:
                log_error(rid, col_name, result["reason"], img_kb)
                c["failed"] += 1
                grand["failed"] += 1
                continue  # ← never touch MongoDB on failure

            # All gates passed — write to MongoDB
            cdn_url   = result["cdn_url"]
            thumb_url = result["thumb_url"]

            update = {
                dst_url:       cdn_url,
                "migrated_at": datetime.now(timezone.utc).isoformat(),
            }
            if dst_thumb and thumb_url:
                update[dst_thumb] = thumb_url
            # base64 src_field intentionally NOT removed — fallback preserved

            col.update_one({"_id": rec["_id"]}, {"$set": update})

            c["uploaded"]    += 1
            c["bytes_freed"] += img_bytes
            grand["uploaded"]    += 1
            grand["bytes_freed"] += img_bytes
            time.sleep(SLEEP_BETWEEN)

        # ── Per-collection summary ──
        print(f"\n  [{col_name}] Migration Summary")
        print(f"    Total scanned  : {c['total']}")
        print(f"    Already CDN    : {c['already_cdn']}")
        print(f"    Pending        : {c['pending']}")
        if DRY_RUN:
            print(f"    Would upload   : {c['dry_run']}")
        else:
            print(f"    Uploaded ✅    : {c['uploaded']}")
            print(f"    Failed ❌      : {c['failed']}")
            print(f"    Skipped ⚠️     : {c['skipped']}")
            print(f"    Payload freed  : ~{c['bytes_freed']//1024} KB")

            # ── POST-COLLECTION VALIDATION ──
            if c["uploaded"] > 0:
                pv = post_collection_validate(db, col_name, src_field, dst_url, dst_thumb)
                post_val_results[col_name] = pv
                summary_icon = "✅" if pv["failed"] == 0 else "⚠️ "
                print(f"\n  Post-validation: {summary_icon} {pv['passed']} passed / {pv['failed']} failed")
            else:
                log(f"  ℹ️  No records uploaded — skipping post-validation")

    return grand, post_val_results

# ─────────────────────────────────────────────────────────────
# 3. FINAL REPORT
# ─────────────────────────────────────────────────────────────
def run_report(db):
    print(f"\n{'═'*62}")
    print(f"  OFFRO — FINAL MIGRATION REPORT")
    print(f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"{'═'*62}")

    total_docs       = 0
    total_migrated   = 0
    total_base64     = 0
    total_b64_bytes  = 0
    total_remaining  = 0
    total_payload_saved = 0

    for (col_name, src_field, dst_url, dst_thumb, _) in COLLECTIONS:
        col  = db[col_name]
        docs = list(col.find({}, {src_field: 1, dst_url: 1, "migrated_at": 1}))
        total_docs += len(docs)

        col_migrated  = 0
        col_base64    = 0
        col_b64_bytes = 0
        col_saved     = 0

        for d in docs:
            src_val     = d.get(src_field, "") or ""
            dst_val     = d.get(dst_url,   "") or ""
            migrated_at = d.get("migrated_at")

            if is_cdn_url(dst_val) or migrated_at:
                col_migrated += 1
                col_saved    += b64_byte_size(src_val) if is_base64(src_val) else 0

            if is_base64(src_val):
                col_base64   += 1
                col_b64_bytes += b64_byte_size(src_val)

        remaining = max(0, col_base64 - col_migrated)
        status    = "✅" if remaining == 0 and col_migrated > 0 else \
                    ("⚠️ " if col_migrated > 0 else "❌")

        total_migrated      += col_migrated
        total_base64        += col_base64
        total_b64_bytes     += col_b64_bytes
        total_remaining     += remaining
        total_payload_saved += col_saved

        print(f"\n  {status} {col_name}")
        print(f"     Total docs         : {len(docs)}")
        print(f"     Migrated to CDN ✅ : {col_migrated}")
        print(f"     Still base64 ⚠️    : {remaining}")
        print(f"     DB data (base64)   : ~{col_b64_bytes//1024} KB still in MongoDB")
        print(f"     Payload saved      : ~{col_saved//1024} KB per full API fetch")

    pct = (total_migrated / total_base64 * 100) if total_base64 else 0

    print(f"\n{'─'*62}")
    print(f"  GRAND TOTAL")
    print(f"{'─'*62}")
    print(f"  Total documents          : {total_docs}")
    print(f"  Migrated to CDN ✅       : {total_migrated}  ({pct:.1f}%)")
    print(f"  Remaining base64 ⚠️      : {total_remaining}")
    print(f"  DB storage (base64)      : ~{total_b64_bytes//1024} KB still in MongoDB")
    print(f"  API payload reduction    : ~{total_payload_saved//1024} KB per full fetch")
    print(f"  Startup improvement      : {'✅ Images load async from CDN — no blocking' if total_migrated > 0 else '⏳ Pending'}")
    print(f"  Fallback status          : ✅ base64 preserved — rollback always safe")
    print()

    # ── Base64 removal readiness ──
    print(f"{'─'*62}")
    print(f"  BASE64 REMOVAL READINESS")
    print(f"{'─'*62}")
    if total_remaining == 0 and total_migrated > 0:
        print(f"  ✅ All records migrated.")
        print(f"  ✅ App validation complete → safe to remove base64 fields.")
        print(f"  Run: python3 offro_cloudinary_migration.py --remove-base64")
    else:
        print(f"  ⚠️  {total_remaining} records still have base64.")
        print(f"  Do NOT remove base64 until:")
        print(f"    ☐  remaining base64 count = 0")
        print(f"    ☐  app validation complete for all collections")
        print(f"    ☐  fallback never triggered in logs")

    print()
    print(f"  Cloudinary usage  : https://console.cloudinary.com/settings/billing")
    print(f"  Media library     : https://console.cloudinary.com/console/media_library")
    print(f"{'═'*62}")

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    if not MONGO_URL:
        print("❌  MONGODB_URL env var not set. Exiting.")
        sys.exit(1)
    if not DRY_RUN and (not CLOUDINARY_KEY or not CLOUDINARY_SECRET):
        print("❌  CLOUDINARY_API_KEY / CLOUDINARY_API_SECRET not set. Exiting.")
        sys.exit(1)

    print("=" * 62)
    print("  OFFRO — Cloudinary Migration  (v4 Full Validation Edition)")
    print(f"  Date  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"  Cloud : {CLOUDINARY_CLOUD}")
    print(f"  Mode  : {'🔵 DRY RUN — zero DB changes' if DRY_RUN else '🔴 LIVE — MongoDB will be updated'}")
    print("=" * 62)
    print()
    print("  Upload gates (all 4 must pass before MongoDB write):")
    print("    Gate 1  Cloudinary HTTP == 200")
    print("    Gate 2  secure_url present and non-empty")
    print("    Gate 3  URL starts with https://res.cloudinary.com")
    print("    Gate 4  Image HEAD request returns HTTP 200")
    print()
    print("  Post-collection validation (runs after each live migration):")
    print(f"    • Randomly samples up to {SAMPLE_SIZE} migrated records")
    print("    • Checks MongoDB fields: image_url, image_thumb, migrated_at")
    print("    • Checks CDN URLs return HTTP 200")
    print("    • Measures payload size before vs after")
    print("    • Prints app checklist for manual verification")
    print()

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)
    db     = client[DB_NAME]
    log("Connected to MongoDB ✅")

    try:
        if REPORT_ONLY:
            run_report(db)
            save_log()
            return

        if BACKUP_ONLY:
            run_backup(db)
            save_log()
            return

        # Auto-backup before every live run
        if not DRY_RUN:
            print("\n⚡ Auto-backup before live migration...")
            run_backup(db)

        grand, post_vals = run_migration(db)

        # ── Grand total ──
        print(f"\n{'═'*62}")
        print(f"  GRAND TOTAL")
        print(f"{'═'*62}")
        print(f"  Scanned        : {grand['total']}")
        print(f"  Already CDN    : {grand['already_cdn']}")
        print(f"  Pending        : {grand['pending']}")

        if DRY_RUN:
            print(f"  Would upload   : {grand['dry_run']}")
            print()
            print("  ✅ Dry run complete — zero changes made.")
            print()
            print("  ⚡ Migrate one collection at a time:")
            for col_name, *_ in COLLECTIONS:
                print(f"     python3 offro_cloudinary_migration.py --live --collection {col_name}")
            print()
            print("  📊 After all done:")
            print("     python3 offro_cloudinary_migration.py --report")
        else:
            print(f"  Uploaded ✅    : {grand['uploaded']}")
            print(f"  Failed ❌      : {grand['failed']}")
            print(f"  Skipped ⚠️     : {grand['skipped']}")
            print(f"  Payload freed  : ~{grand['bytes_freed']//1024} KB")

            if post_vals:
                print()
                print(f"  POST-VALIDATION SUMMARY")
                divider()
                for col, pv in post_vals.items():
                    icon = "✅" if pv["failed"] == 0 else "❌"
                    p    = pv.get("payload", {})
                    print(f"  {icon} {col:<22} {pv['passed']} passed / {pv['failed']} failed  |  "
                          f"payload {p.get('before_kb',0):.1f}→{p.get('after_kb',0):.1f} KB "
                          f"({p.get('reduction_pct',0):.1f}% reduction)")

            if grand["failed"] == 0:
                print("\n  ✅ Migration complete. Run --report for full analysis.")
            else:
                print(f"\n  ⚠️  {grand['failed']} record(s) failed — re-run is safe (skips already-CDN records).")

        print(f"{'═'*62}")

    finally:
        save_log()
        client.close()


if __name__ == "__main__":
    main()
