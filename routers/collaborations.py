"""
Creator Collaborations  —  merchant campaign briefs + influencer opportunities.

Collections
  collab_campaigns   one document per published campaign brief (owned by a merchant's store)
  collab_responses   one document per (campaign, influencer): accepted | declined

Rules (existing OffrO business rules)
  * This is for the influencer's Instagram promotion. There is NO chat.
  * After the influencer ACCEPTS, the MERCHANT's contact number is revealed to the influencer.
  * The influencer's phone number is never returned to the merchant.
  * A merchant can only create campaigns for their OWN, active stores.
  * An influencer only sees campaigns for their own city, and only while their public
    profile is live (same publish / active gate as the public influencer directory).
"""
from datetime import datetime

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from database import db
from routers.merchant_app import get_merchant, _mid
from routers.users import get_current_user, _resolve_own_account
from services import products as P          # IST date helpers only (ist_today / parse_date)

CAMPAIGNS, RESPONSES = "collab_campaigns", "collab_responses"

OBJECTIVES = ["Store awareness", "New product launch", "Store visit", "Offer promotion", "Event"]
CONTENT_TYPES = ["1 reel", "3 stories", "Store visit", "Photo post"]
ANY_SPECIALITY = "Any"

merchant_router = APIRouter(prefix="/merchant/collab", tags=["Creator Collaborations (merchant)"])
influencer_router = APIRouter(prefix="/influencer", tags=["Creator Collaborations (influencer)"])


# ── helpers ────────────────────────────────────────────────────────────────────
def _oid(v, what="ID"):
    try:
        return ObjectId(str(v))
    except Exception:
        raise HTTPException(400, f"Invalid {what}")


def _ymd(d) -> str:
    return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else (str(d)[:10] if d else "")


def _summary(content_types) -> str:
    return " · ".join(content_types or [])


def _is_expired(c: dict) -> bool:
    pb = P.parse_date(c.get("publish_by"))
    return bool(pb and pb < P.ist_today())


def _campaign_state(c: dict) -> str:
    """active | expired | closed (what the merchant sees on the card)."""
    if c.get("status") == "closed":
        return "closed"
    return "expired" if _is_expired(c) else "active"


def _clean_text(v, label, min_len, max_len) -> str:
    s = " ".join(str(v or "").split()) if max_len <= 120 else str(v or "").strip()
    if len(s) < min_len:
        raise HTTPException(400, f"{label} is required" if min_len <= 1 else f"{label} must be at least {min_len} characters")
    if len(s) > max_len:
        raise HTTPException(400, f"{label} must be at most {max_len} characters")
    return s


def ensure_indexes():
    db[CAMPAIGNS].create_index([("merchant_id", 1), ("created_at", -1)])
    db[CAMPAIGNS].create_index([("status", 1), ("city_key", 1), ("publish_by", 1)])
    db[RESPONSES].create_index([("campaign_id", 1), ("influencer_id", 1)], unique=True)
    db[RESPONSES].create_index([("influencer_id", 1), ("responded_at", -1)])


# ══════════════════════════════════════════════════════════════════════════════
# MERCHANT
# ══════════════════════════════════════════════════════════════════════════════
def _merchant_row(c: dict, accepted: int = 0) -> dict:
    return {
        "id": str(c["_id"]), "title": c.get("title", ""), "store_id": c.get("store_id", ""),
        "store_name": c.get("store_name", ""), "city": c.get("city", ""),
        "objective": c.get("objective", ""), "message": c.get("message", ""),
        "content_types": c.get("content_types", []), "content_summary": _summary(c.get("content_types")),
        "visit_date": _ymd(c.get("visit_date")), "publish_by": _ymd(c.get("publish_by")),
        "speciality": c.get("speciality", ANY_SPECIALITY),
        "status": _campaign_state(c), "proposals": accepted,
        "created_at": str(c.get("created_at", ""))[:19],
    }


@merchant_router.get("/options")
def collab_options(m=Depends(get_merchant)):
    """Dropdown / chip values for the brief form (single source of truth: this module)."""
    return {"objectives": OBJECTIVES, "content_types": CONTENT_TYPES, "any_speciality": ANY_SPECIALITY}


@merchant_router.post("/campaigns")
def create_campaign(data: dict, m=Depends(get_merchant)):
    """Validate the brief, save it and publish it (status published). It then shows up
    for matching influencers in their Opportunities."""
    mid = _mid(m)
    store_id = str(data.get("store_id") or "").strip()
    if not store_id:
        raise HTTPException(400, "Please select a store")
    store = db.stores.find_one({"_id": _oid(store_id, "store ID")})
    if not store:
        raise HTTPException(404, "Store not found")
    if str(store.get("merchant_id") or "") != str(mid):
        raise HTTPException(403, "This store does not belong to your account.")
    if store.get("status") != "active":
        raise HTTPException(400, "Only an active store can run a campaign. Subscribe and activate the store first.")
    city = str(store.get("city") or "").strip()
    if not city:
        raise HTTPException(400, "This store has no city set, so creators cannot be matched. Edit the store first.")

    title = _clean_text(data.get("title"), "Campaign title", 3, 100)
    message = _clean_text(data.get("message"), "Campaign message", 10, 1000)
    objective = str(data.get("objective") or "").strip()
    if objective not in OBJECTIVES:
        raise HTTPException(400, "Please choose a campaign objective")
    raw_types = data.get("content_types")
    if not isinstance(raw_types, list):
        raw_types = []
    types = [t for t in CONTENT_TYPES if t in {str(x) for x in raw_types}]        # canonical order, no unknowns
    if not types:
        raise HTTPException(400, "Select at least one type of requested content")
    today = P.ist_today()
    visit = P.parse_date(data.get("visit_date")) if data.get("visit_date") else None
    if data.get("visit_date") and visit is None:
        raise HTTPException(400, "Please choose a valid visit date")
    if "Store visit" in types and visit is None:
        raise HTTPException(400, "Please choose a visit date for the store visit")
    publish_by = P.parse_date(data.get("publish_by")) if data.get("publish_by") else None
    if publish_by is None:
        raise HTTPException(400, "Please choose the publish-by date")
    if visit is not None and visit < today:
        raise HTTPException(400, "Visit date cannot be in the past")
    if publish_by < today:
        raise HTTPException(400, "Publish-by date cannot be in the past")
    if visit is not None and publish_by < visit:
        raise HTTPException(400, "Publish-by date cannot be before the visit date")
    speciality = str(data.get("speciality") or ANY_SPECIALITY).strip()[:60] or ANY_SPECIALITY

    now = datetime.utcnow()
    doc = {
        "merchant_id": str(mid), "merchant_name": str(m.get("name") or ""), "merchant_phone": str(m.get("phone") or ""),
        "store_id": store_id, "store_name": str(store.get("store_name") or ""),
        "city": city, "city_key": city.lower(),
        "area": str(store.get("area") or ""), "address": str(store.get("address") or ""),
        "title": title, "objective": objective, "message": message, "content_types": types,
        "visit_date": visit, "publish_by": publish_by, "speciality": speciality,
        "status": "published", "published_at": now, "created_at": now, "updated_at": now,
    }
    doc["_id"] = db[CAMPAIGNS].insert_one(doc).inserted_id
    return {"ok": True, "message": "Campaign published", "campaign": _merchant_row(doc, 0)}


@merchant_router.get("/campaigns")
def my_campaigns(m=Depends(get_merchant)):
    mid = str(_mid(m))
    docs = list(db[CAMPAIGNS].find({"merchant_id": mid}).sort("created_at", -1))
    counts = {}
    if docs:
        for r in db[RESPONSES].aggregate([
                {"$match": {"campaign_id": {"$in": [str(d["_id"]) for d in docs]}, "status": "accepted"}},
                {"$group": {"_id": "$campaign_id", "n": {"$sum": 1}}}]):
            counts[r["_id"]] = r["n"]
    rows = [_merchant_row(d, counts.get(str(d["_id"]), 0)) for d in docs]
    return {"campaigns": rows, "active_count": sum(1 for r in rows if r["status"] == "active")}


def _own_campaign(cid: str, m) -> dict:
    c = db[CAMPAIGNS].find_one({"_id": _oid(cid, "campaign ID"), "merchant_id": str(_mid(m))})
    if not c:
        raise HTTPException(404, "Campaign not found")
    return c


@merchant_router.get("/campaigns/{cid}/proposals")
def campaign_proposals(cid: str, m=Depends(get_merchant)):
    """Creators who accepted. A creator's mobile number is included ONLY here, i.e. only for
    creators who accepted THIS merchant's campaign (never before acceptance, never in the
    influencer-facing / opportunities responses)."""
    c = _own_campaign(cid, m)
    out = []
    for r in db[RESPONSES].find({"campaign_id": str(c["_id"]), "status": "accepted"}).sort("responded_at", -1):
        inf = {}
        try:
            inf = db.influencers.find_one({"_id": ObjectId(r["influencer_id"])}) or {}
        except Exception:
            pass
        cats = inf.get("categories") or ([inf["category"]] if inf.get("category") else [])
        social = inf.get("social") or {}
        out.append({"influencer_id": r["influencer_id"], "name": inf.get("name", "Creator"),
                    "city": inf.get("city", ""), "categories": cats, "photo_url": inf.get("photo_url", ""),
                    "instagram": social.get("instagram", ""), "phone": str(inf.get("phone") or ""),
                    "accepted_at": str(r.get("responded_at", ""))[:19]})
    return {"campaign": _merchant_row(c, len(out)), "proposals": out}


@merchant_router.put("/campaigns/{cid}/close")
def close_campaign(cid: str, m=Depends(get_merchant)):
    c = _own_campaign(cid, m)
    db[CAMPAIGNS].update_one({"_id": c["_id"]}, {"$set": {"status": "closed", "closed_at": datetime.utcnow(),
                                                           "updated_at": datetime.utcnow()}})
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# INFLUENCER
# ══════════════════════════════════════════════════════════════════════════════
def _my_influencer(user: dict) -> dict:
    """The caller's own influencer profile. A profile that is not yet paid/published can still SEE
    campaigns (it just cannot accept - see _can_accept); only a disabled profile is shut out."""
    acct = _resolve_own_account(user)
    iid = (acct or {}).get("influencer_id")
    inf = None
    if iid:
        try:
            inf = db.influencers.find_one({"_id": ObjectId(str(iid))})
        except Exception:
            inf = None
    if not inf:
        raise HTTPException(403, "Create your creator profile to receive collaboration requests.")
    if inf.get("is_active", True) is False:
        raise HTTPException(403, "Your creator profile is disabled. Enable it to receive collaboration requests.")
    return inf


NEEDS_SUBSCRIPTION_MSG = "Complete your profile subscription to accept collaborations."


def _can_accept(inf: dict) -> bool:
    """Same 'live' rule the app uses everywhere: payment_status PAID and publish_status published
    (a missing field counts as PAID/published - older profiles predate those fields)."""
    return inf.get("payment_status", "PAID") == "PAID" and inf.get("publish_status", "published") == "published"


def _speciality_match(c: dict, inf: dict) -> bool:
    want = str(c.get("speciality") or ANY_SPECIALITY).strip().lower()
    if want in ("", ANY_SPECIALITY.lower()):
        return True
    cats = [str(x).strip().lower() for x in (inf.get("categories") or [])] + [str(inf.get("category") or "").strip().lower()]
    return want in cats


def _opp_row(c: dict, my_status: str, match: bool) -> dict:
    return {"id": str(c["_id"]), "title": c.get("title", ""), "store_name": c.get("store_name", ""),
            "city": c.get("city", ""), "content_types": c.get("content_types", []),
            "content_summary": _summary(c.get("content_types")), "publish_by": _ymd(c.get("publish_by")),
            "my_status": my_status, "speciality_match": match,
            "campaign_state": _campaign_state(c), "created_at": str(c.get("created_at", ""))[:19]}


@influencer_router.get("/opportunities")
def opportunities(user=Depends(get_current_user)):
    inf = _my_influencer(user)
    iid = str(inf["_id"])
    city_key = str(inf.get("city") or "").strip().lower()
    mine = {r["campaign_id"]: r["status"] for r in db[RESPONSES].find({"influencer_id": iid})}
    q_open = {"status": "published", "city_key": city_key, "publish_by": {"$gte": P.ist_today()}} if city_key else None
    docs = {}
    if q_open:
        for c in db[CAMPAIGNS].find(q_open):
            docs[str(c["_id"])] = c
    # campaigns I already answered stay reachable (e.g. to read the merchant contact after accepting)
    extra = [ObjectId(k) for k in mine if k not in docs]
    if extra:
        for c in db[CAMPAIGNS].find({"_id": {"$in": extra}}):
            docs[str(c["_id"])] = c
    rows = [_opp_row(c, mine.get(cid, "new"), _speciality_match(c, inf)) for cid, c in docs.items()]
    order = {"new": 0, "accepted": 1, "declined": 2}
    rows.sort(key=lambda r: r["created_at"], reverse=True)                       # newest first ...
    rows.sort(key=lambda r: (order.get(r["my_status"], 3), not r["speciality_match"]))   # ... new, then accepted, then declined
    return {"opportunities": rows, "new_count": sum(1 for r in rows if r["my_status"] == "new"),
            "can_accept": _can_accept(inf)}


def _visible_campaign(cid: str, inf: dict) -> tuple:
    """(campaign, my_response_or_None). 404 unless it targets this influencer's city (or they already answered)."""
    c = db[CAMPAIGNS].find_one({"_id": _oid(cid, "campaign ID")})
    mine = db[RESPONSES].find_one({"campaign_id": cid, "influencer_id": str(inf["_id"])})
    if not c:
        raise HTTPException(404, "Campaign not found")
    same_city = str(c.get("city_key") or "") == str(inf.get("city") or "").strip().lower()
    if not mine and not (same_city and c.get("status") == "published"):
        raise HTTPException(404, "Campaign not found")
    return c, mine


@influencer_router.get("/opportunities/{cid}")
def opportunity_detail(cid: str, user=Depends(get_current_user)):
    inf = _my_influencer(user)
    c, mine = _visible_campaign(cid, inf)
    status = (mine or {}).get("status", "new")
    out = {**_opp_row(c, status, _speciality_match(c, inf)),
           "objective": c.get("objective", ""), "message": c.get("message", ""),
           "visit_date": _ymd(c.get("visit_date")), "speciality": c.get("speciality", ANY_SPECIALITY),
           "store": {"name": c.get("store_name", ""), "city": c.get("city", ""),
                     "area": c.get("area", ""), "address": c.get("address", "")},
           "can_respond": _campaign_state(c) == "active" and status != "accepted",
           "can_accept": _can_accept(inf), "accept_blocked_message": "" if _can_accept(inf) else NEEDS_SUBSCRIPTION_MSG,
           "merchant_phone": ""}
    if status == "accepted":                     # contact is revealed ONLY after the influencer accepts
        out["merchant_phone"] = str(c.get("merchant_phone") or "")
    return out


def _respond(cid: str, user: dict, status: str) -> dict:
    inf = _my_influencer(user)
    c, mine = _visible_campaign(cid, inf)
    if _campaign_state(c) != "active":
        raise HTTPException(409, "This collaboration is no longer open.")
    if status == "accepted" and not _can_accept(inf):       # enforced here, not only in the app
        raise HTTPException(403, NEEDS_SUBSCRIPTION_MSG)
    if mine and mine.get("status") == "accepted":
        raise HTTPException(409, "You have already accepted this collaboration.")
    now = datetime.utcnow()
    db[RESPONSES].update_one(
        {"campaign_id": cid, "influencer_id": str(inf["_id"])},
        {"$set": {"status": status, "responded_at": now, "updated_at": now,
                  "account_id": str(inf.get("account_id") or "")},
         "$setOnInsert": {"created_at": now}}, upsert=True)
    out = {"ok": True, "status": status, "merchant_phone": ""}
    if status == "accepted":
        out["merchant_phone"] = str(c.get("merchant_phone") or "")
        out["message"] = "Collaboration accepted. You can now contact the merchant."
    else:
        out["message"] = "Request declined."
    return out


@influencer_router.post("/opportunities/{cid}/accept")
def accept_opportunity(cid: str, user=Depends(get_current_user)):
    return _respond(cid, user, "accepted")


@influencer_router.post("/opportunities/{cid}/decline")
def decline_opportunity(cid: str, user=Depends(get_current_user)):
    return _respond(cid, user, "declined")
