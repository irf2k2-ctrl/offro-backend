from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response
from database import db
from bson import ObjectId
import datetime, base64, re

router = APIRouter()

# ── Auth helper ───────────────────────────────────────────
def _require_admin(request: Request, action: str = None):
    """The signed-in dashboard user, with the role resolved (role_name / permissions / assigned_cities) exactly as
    routers.admin.get_current_admin does. When `action` ("view" | "add" | "edit" | "delete") is given, the user must hold that
    permission on the "Popup Campaigns" module (Super Admin always does). A missing role grants nothing (fail closed)."""
    token = (request.cookies.get("admin_token") or
             request.headers.get("x-admin-token", "")).strip()
    if not token:
        raise HTTPException(401, "Unauthorized")
    # Check new RBAC dashboard_users first (mobile+PIN login)
    du = db.dashboard_users.find_one({"token": token})
    if du:
        if du.get("status") in ("disabled", "suspended"):
            raise HTTPException(403, "Account is " + du.get("status", "disabled"))
        role = None
        try:
            role = db.dashboard_roles.find_one({"_id": du["role_id"]}) if du.get("role_id") else None
        except Exception:
            role = None
        role = role or {"role_name": "Unknown", "permissions": {}}
        du["role"] = role
        du["role_name"] = role.get("role_name", "Unknown")
        du["permissions"] = role.get("permissions", {})
        du["assigned_cities"] = du.get("assigned_cities", [])
        if action and du["role_name"] != "Super Admin":
            if not ((du["permissions"] or {}).get(MODULE) or {}).get(action, False):
                raise HTTPException(403, "Access denied: Missing " + action + " permission for " + MODULE)
        return du
    raise HTTPException(401, "Unauthorized")           # legacy admins.token sessions were retired (Patch B)


MODULE = "Popup Campaigns"


# ── Who may touch which campaign (country + assigned cities) ───────────────────────────────────────────────────────────
def _scope_of(admin: dict):
    """(country, cities): the restriction of this dashboard user.
      country  '' = none (Super Admin / a user with no country); an ISO code; or dashboard_country.INVALID_SCOPE (fail closed)
      cities   None = no city restriction (Super Admin or '*'); otherwise the set of lower-cased assigned city names (an empty
               set = no city at all, the same rule routers.admin._city_filter applies to every other module)"""
    from services import dashboard_country as _dc
    iso = _dc.scope(admin)
    if admin.get("role_name") == "Super Admin":
        return "", None
    assigned = admin.get("assigned_cities") or []
    if "*" in assigned:
        return iso, None
    return iso, {" ".join(str(c or "").split()).lower() for c in assigned if str(c or "").strip()}


def _is_restricted(admin: dict) -> bool:
    iso, cities = _scope_of(admin)
    return bool(iso) or cities is not None


def campaign_in_scope(d: dict, admin: dict) -> bool:
    """May this dashboard user see / change a campaign with these audience fields?
    Unrestricted users: always. A restricted user may only handle a campaign whose WHOLE audience is inside their scope:
      * "All Users" campaigns and legacy (pre-country) campaigns are global: country-restricted users never get them;
        a user restricted only by assigned cities gets a legacy campaign only when every one of its city names is assigned;
      * a country-restricted user: every targeted country / city_target must be their own country;
      * a city-restricted user: the audience must be individual cities, all assigned (a whole-country target would reach
        cities they do not have)."""
    iso, cities = _scope_of(admin)
    if not iso and cities is None:
        return True
    if d.get("target", "all") != "selected_city":
        return False
    if d.get("audience_version") != 2:
        if iso:
            return False
        names = [_key(c) for c in (d.get("cities") or [])]
        return bool(names) and all(n in cities for n in names)
    countries = [str(c).upper() for c in (d.get("countries") or [])]
    singles = [(str(x.get("country", "")).upper(), _key(x.get("city"))) for x in (d.get("city_targets") or [])]
    if not countries and not singles:
        return False
    if iso and (any(c != iso for c in countries) or any(c != iso for c, _ in singles)):
        return False
    if cities is not None:
        if countries:
            return False
        if any(n not in cities for _, n in singles):
            return False
    return True


def _require_in_scope(d: dict, admin: dict):
    if not campaign_in_scope(d, admin):
        raise HTTPException(403, "This campaign is outside your country / city access.")


def _fmt(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


# ── Audience: All Users / Selected Countries + Cities ──────────────────────────────────────────────────────────────────
# Stored on the campaign (all additive; `target` and `cities` keep their old meaning):
#   target           "all" | "selected_city"   (selected_city = "Selected Countries / Cities" in the form)
#   audience_version 2 when the fields below are present (absent = an older campaign, matched by city NAME exactly as before)
#   countries        ["SA", "JO"]     ISO codes (country master): every active city of these countries is targeted
#   excluded_cities  [{"country": "SA", "city": "Madinah"}]   cities unchecked under a targeted country
#   city_targets     [{"country": "IN", "city": "Mumbai"}]    individually selected cities (country not targeted as a whole)
#   cities           flat English names - kept for older app builds that match by name (a snapshot of the cities above)
# Country targeting only ever compares COUNTRY CODES (never city names), so Saudi Arabia cannot pull in an Indian city.
def _cm():
    from services import city_master as cm
    return cm


def _key(name) -> str:
    return " ".join(str(name or "").split()).lower()


def _iso_or_empty(value) -> str:
    try:
        return _cm().require_country(value)
    except Exception:
        return ""


def clean_audience(data: dict):
    """The validated v2 audience for a create/update payload, or None when the payload is the older shape (no countries / city_targets).
    Raises HTTPException(400) for an unsupported country or a city that is not in its country."""
    if not any(k in data for k in ("countries", "city_targets", "excluded_cities")):
        return None
    cm = _cm()
    countries = []
    for c in data.get("countries") or []:
        try:
            iso = cm.require_country(c)
        except cm.CityError as e:
            raise HTTPException(400, e.message)
        if iso not in countries:
            countries.append(iso)

    def pairs(raw, label):
        out, seen = [], set()
        for it in raw or []:
            if not isinstance(it, dict):
                raise HTTPException(400, "%s must be {country, city} entries" % label)
            try:
                iso = cm.require_country(it.get("country"))
            except cm.CityError as e:
                raise HTTPException(400, e.message)
            city = " ".join(str(it.get("city") or "").split())
            if not city:
                continue
            err = cm.check_city_in_country(city, iso, strict=True)
            if err:
                raise HTTPException(400, err.message)
            k = (iso, _key(city))
            if k not in seen:
                seen.add(k)
                out.append({"country": iso, "city": city})
        return out

    excluded = [x for x in pairs(data.get("excluded_cities"), "excluded_cities") if x["country"] in countries]
    ex_keys = {(x["country"], _key(x["city"])) for x in excluded}
    # an individually selected city inside a targeted country is already covered by the country (unless it was unchecked)
    singles = [x for x in pairs(data.get("city_targets"), "city_targets") if x["country"] not in countries]
    names, seen = [], set()
    for iso in countries:
        for c in cm.country_cities(iso):
            if (iso, _key(c["name"])) not in ex_keys and _key(c["name"]) not in seen:
                seen.add(_key(c["name"]))
                names.append(c["name"])
    for x in singles:
        if _key(x["city"]) not in seen:
            seen.add(_key(x["city"]))
            names.append(x["city"])
    return {"audience_version": 2, "countries": countries, "excluded_cities": excluded, "city_targets": singles, "cities": names}


def _audience_fields(data: dict):
    """(fields to $set, fields to $unset) for the audience part of a create/update payload."""
    target = data.get("target", "all")
    aud = clean_audience(data) if target == "selected_city" else None
    if aud is not None:
        return {"target": target, **aud}, {}
    # older payload / All Users: exactly the previous shape; any v2 fields of an earlier save are removed so they cannot linger
    return ({"target": target, "cities": data.get("cities") or []},
            {"audience_version": "", "countries": "", "excluded_cities": "", "city_targets": ""})


def _candidate_countries(city: str) -> set:
    """Countries that have a city of this name in the City Master (for app builds that send only a city)."""
    out = set()
    k = _key(city)
    if not k:
        return out
    for d in db.cities.find({"$or": [{"name_key": k}, {"name": {"$regex": "^" + re.escape(" ".join(str(city).split())) + "$", "$options": "i"}}]}):
        if d.get("country"):
            out.add(str(d["country"]).upper())
    return out


def campaign_matches(d: dict, city: str = "", country: str = "") -> bool:
    """Does the stored campaign `d` apply to a user of `country` (ISO, may be '') in `city` (may be '')?"""
    if d.get("target", "all") != "selected_city":
        return True                                              # All Users
    if d.get("audience_version") != 2:                           # older campaign: city names, exactly as before
        if not city:
            return True
        return _key(city) in [_key(c) for c in (d.get("cities") or [])]
    if not city and not country:
        return True                                              # location unknown: same as the old behaviour
    countries = {str(c).upper() for c in d.get("countries") or []}
    excluded = {(str(x.get("country", "")).upper(), _key(x.get("city"))) for x in d.get("excluded_cities") or []}
    singles = {(str(x.get("country", "")).upper(), _key(x.get("city"))) for x in d.get("city_targets") or []}
    iso = _iso_or_empty(country) if country else ""
    cands = {iso} if iso else _candidate_countries(city)
    if not cands:                                                # a city the master does not know and no country: legacy name match
        return bool(city) and _key(city) in [_key(c) for c in (d.get("cities") or [])]
    for c in cands:
        if not city:
            if c in countries:
                return True
            continue
        if c in countries and (c, _key(city)) not in excluded:
            return True
        if (c, _key(city)) in singles:
            return True
    return False

# ── Admin: list ────────────────────────────────────────────────────────────────
@router.get("/admin/popup-campaigns")
def list_popup_campaigns(request: Request):
    adm = _require_admin(request, "view")
    docs = list(db.popup_campaigns.find().sort("_id", -1))
    if _is_restricted(adm):
        docs = [d for d in docs if campaign_in_scope(d, adm)]
    return [_fmt(d) for d in docs]

# ── Admin: create ──────────────────────────────────────────────────────────────
@router.post("/admin/popup-campaigns")
async def create_popup_campaign(request: Request):
    adm = _require_admin(request, "add")
    data = await request.json()
    now  = datetime.datetime.utcnow()
    doc  = {
        "name":         (data.get("name") or "").strip(),
        "image_url":    (data.get("image_url") or "").strip(),
        "start_dt":     (data.get("start_dt") or ""),
        "end_dt":       (data.get("end_dt") or ""),
        "frequency":    data.get("frequency", "once_per_day"),
        "click_action": data.get("click_action", "none"),
        "action_value": (data.get("action_value") or "").strip(),
        "is_active":    bool(data.get("is_active", True)),
        "created_at":   now,
        "updated_at":   now,
    }
    doc.update(_audience_fields(data)[0])
    _require_in_scope(doc, adm)
    result = db.popup_campaigns.insert_one(doc)
    if doc["image_url"].startswith("data:image"):
        proxy = f"/popup-image/{result.inserted_id}"
        db.popup_campaigns.update_one(
            {"_id": result.inserted_id},
            {"$set": {"_raw_image": doc["image_url"], "image_url": proxy}}
        )
    return {"ok": True, "id": str(result.inserted_id)}

# ── Admin: update ──────────────────────────────────────────────────────────────
@router.put("/admin/popup-campaigns/{cid}")
async def update_popup_campaign(cid: str, request: Request):
    adm = _require_admin(request, "edit")
    data = await request.json()
    now  = datetime.datetime.utcnow()
    upd  = {
        "name":         (data.get("name") or "").strip(),
        "image_url":    (data.get("image_url") or "").strip(),
        "start_dt":     (data.get("start_dt") or ""),
        "end_dt":       (data.get("end_dt") or ""),
        "frequency":    data.get("frequency", "once_per_day"),
        "click_action": data.get("click_action", "none"),
        "action_value": (data.get("action_value") or "").strip(),
        "is_active":    bool(data.get("is_active", True)),
        "updated_at":   now,
    }
    _set, _unset = _audience_fields(data)
    upd.update(_set)
    try:
        _oid = ObjectId(cid)
    except Exception:
        raise HTTPException(400, "Invalid id")
    if _is_restricted(adm):
        existing = db.popup_campaigns.find_one({"_id": _oid})
        if not existing:
            raise HTTPException(404, "Campaign not found")
        _require_in_scope(existing, adm)           # the campaign as stored must be theirs ...
        _require_in_scope(upd, adm)                # ... and so must the audience they are saving
    if upd["image_url"].startswith("data:image"):
        upd["_raw_image"] = upd["image_url"]
        upd["image_url"]  = f"/popup-image/{cid}"
    _op = {"$set": upd}
    if _unset:
        _op["$unset"] = _unset
    db.popup_campaigns.update_one({"_id": _oid}, _op)
    return {"ok": True}

# ── Admin: delete ──────────────────────────────────────────────────────────────
@router.delete("/admin/popup-campaigns/{cid}")
def delete_popup_campaign(cid: str, request: Request):
    adm = _require_admin(request, "delete")
    try:
        _oid = ObjectId(cid)
    except Exception:
        raise HTTPException(400, "Invalid id")
    if _is_restricted(adm):
        existing = db.popup_campaigns.find_one({"_id": _oid})
        if not existing:
            raise HTTPException(404, "Campaign not found")
        _require_in_scope(existing, adm)
    db.popup_campaigns.delete_one({"_id": _oid})
    return {"ok": True}

# ── Admin: toggle active ───────────────────────────────────────────────────────
@router.patch("/admin/popup-campaigns/{cid}/toggle")
async def toggle_popup_campaign(cid: str, request: Request):
    adm = _require_admin(request, "edit")
    data = await request.json()
    try:
        _oid = ObjectId(cid)
    except Exception:
        raise HTTPException(400, "Invalid id")
    if _is_restricted(adm):
        existing = db.popup_campaigns.find_one({"_id": _oid})
        if not existing:
            raise HTTPException(404, "Campaign not found")
        _require_in_scope(existing, adm)
    try:
        db.popup_campaigns.update_one(
            {"_id": _oid},
            {"$set": {"is_active": bool(data.get("is_active", False)),
                      "updated_at": datetime.datetime.utcnow()}}
        )
    except Exception:
        raise HTTPException(400, "Invalid id")
    return {"ok": True}

# ── Public: active campaigns for Flutter ──────────────────────────────────────
@router.get("/public/popup-campaigns")
def get_active_popup_campaigns(city: str = "", country: str = ""):
    docs = list(db.popup_campaigns.find({"is_active": True}).sort("_id", -1))
    result = []
    for d in docs:
        target = d.get("target", "all")
        if not campaign_matches(d, city, country):
            continue
        image_url = d.get("image_url") or ""
        doc_id    = str(d["_id"])

        # Cache-bust: append ?v=<updated_at_ms> so CachedNetworkImage treats
        # a newly uploaded image as a different URL and fetches it fresh.
        updated_at = d.get("updated_at")
        v = int(updated_at.timestamp() * 1000) if updated_at else 0

        if image_url.startswith("data:image"):
            image_url = f"/popup-image/{doc_id}?v={v}"
        elif image_url.startswith("/popup-image/"):
            base = image_url.split("?")[0]
            image_url = f"{base}?v={v}"

        result.append({
            "id":           doc_id,
            "name":         d.get("name", ""),
            "image_url":    image_url,
            "frequency":    d.get("frequency", "once_per_day"),
            "target":       target,
            "cities":       d.get("cities") or [],
            "countries":    d.get("countries") or [],
            "click_action": d.get("click_action", "none"),
            "action_value": d.get("action_value", ""),
            "start_dt":     d.get("start_dt", ""),
            "end_dt":       d.get("end_dt", ""),
        })
    return result

# ── Image proxy (serves stored base64 as binary) ──────────────────────────────
@router.get("/popup-image/{cid}")
def get_popup_image(cid: str):
    try:
        doc = db.popup_campaigns.find_one({"_id": ObjectId(cid)}, {"_raw_image": 1, "image_url": 1})
    except Exception:
        raise HTTPException(400, "Bad id")
    if not doc:
        raise HTTPException(404, "Not found")
    raw = doc.get("_raw_image") or doc.get("image_url") or ""
    if not raw.startswith("data:image"):
        raise HTTPException(404, "No image stored")
    m = re.match(r"data:([^;]+);base64,(.+)", raw, re.DOTALL)
    if not m:
        raise HTTPException(400, "Bad image data")
    try:
        img_bytes = base64.b64decode(m.group(2))
    except Exception:
        raise HTTPException(500, "Decode error")
    return Response(
        content=img_bytes,
        media_type=m.group(1),
        # no-cache forces the client to revalidate; the ?v= param in the URL
        # is the primary bust so CachedNetworkImage sees a new URL on update.
        headers={"Cache-Control": "no-cache",
                 "Content-Length": str(len(img_bytes))},
    )
