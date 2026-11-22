"""routers/translations_admin.py - admin API for the translation module (simplified: edit -> Publish Changes).

Prefix /admin/translations (mounted in server.py). Uses the existing dashboard session (get_current_dashboard_user).

Who may do what
  Privileged ("system Super Admin") = role_name == "Super Admin" AND is_system_role is True. A role that was merely RENAMED
  "Super Admin" never qualifies. Privileged only: add/change a language, publish, kill switch. Everyone else needs the explicit
  "Translations" module permission: view -> read endpoints, edit -> save values. The role is checked here on every call (never
  trusted from the client). Staging and production run this same code, each against its own database.
Audit
  Every sensitive operation writes a strict "started" entry BEFORE it changes anything (if that cannot be stored the operation is
  refused with 503 audit_unavailable) and an ok/failed entry afterwards. Denied attempts are recorded too.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from routers.dashboard_auth import get_current_dashboard_user
from services import app_environment as _env
from services import translation_audit as audit
from services import translations as T
from utils import countries as _countries

router = APIRouter(prefix="/translations", tags=["Translations"])
MODULE = "Translations"


# ── permissions ───────────────────────────────────────────────────────────────────────────────────────────────────────
def is_system_super_admin(user: dict) -> bool:
    role = user.get("role") if isinstance(user.get("role"), dict) else {}
    return role.get("role_name") == "Super Admin" and role.get("is_system_role") is True


def _has_perm(user: dict, action: str) -> bool:
    if is_system_super_admin(user):
        return True
    role = user.get("role") if isinstance(user.get("role"), dict) else {}
    return bool((role.get("permissions", {}).get(MODULE, {}) or {}).get(action, False))


def _ip(request: Request) -> str:
    try:
        from routers.dashboard_auth import _client_ip
        return _client_ip(request)
    except Exception:
        return "unknown"


def _deny(request: Request, user: dict, what: str, status: int = 403):
    audit.record(what, audit.admin_ref(user), "denied", environment=T.environment(), ip=_ip(request))   # best effort, logged on failure
    raise HTTPException(status_code=status, detail={"code": "forbidden", "detail": "Not allowed for your role"})


def require_perm(action: str):
    async def dep(request: Request, user: dict = Depends(get_current_dashboard_user)) -> dict:
        if not _has_perm(user, action):
            _deny(request, user, f"denied:{action}:{request.url.path[-60:]}")
        return user
    return dep


def require_super_admin(operation: str):
    async def dep(request: Request, user: dict = Depends(get_current_dashboard_user)) -> dict:
        if not is_system_super_admin(user):
            _deny(request, user, f"denied:{operation}")
        return user
    return dep


def _http(e: T.TranslationError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"code": e.code, "detail": e.detail})


def _actor(user: dict) -> dict:
    a = audit.admin_ref(user)
    return {"id": a["id"], "name": a["name"]}


def _safe_failure_details(e: T.TranslationError) -> dict:
    """Failure details that are safe for the audit log: for invalid_values, key NAMES with error codes (never translation text)."""
    d, out = e.detail, {}
    if isinstance(d, dict) and e.code == "invalid_values":
        out["blockers"] = {str(k)[:100]: str(v)[:40] for k, v in list(d.items())[:30]}
    return out


def _run_sensitive(request: Request, user: dict, action: str, fn, *, lang="", track="", version=None, details=None):
    """Strict 'started' audit entry first, then the operation, then the outcome entry."""
    admin, ip, env = audit.admin_ref(user), _ip(request), T.environment()
    try:
        audit.record(action, admin, "started", environment=env, lang=lang, track=track, version=version, details=details, ip=ip, strict=True)
    except audit.AuditWriteError:
        raise HTTPException(status_code=503, detail={"code": "audit_unavailable", "detail": "Nothing was changed because the audit log could not be written"})
    try:
        result = fn()
    except T.TranslationError as e:
        audit.record(action, admin, "failed", environment=env, lang=lang, track=track, version=version,
                     details={"code": e.code, **_safe_failure_details(e)}, ip=ip)
        raise _http(e)
    except Exception:
        audit.record(action, admin, "failed", environment=env, lang=lang, track=track, version=version,
                     details={"code": "internal_error"}, ip=ip)
        raise
    ok_details = result.pop("audit_details", None) if isinstance(result, dict) else None
    ok = audit.record(action, admin, "ok", environment=env, lang=lang, track=track,
                      version=result.get("version") if isinstance(result, dict) else None,
                      new_hash=(result.get("hash") if isinstance(result, dict) else "") or "", details=ok_details, ip=ip)
    if not ok and isinstance(result, dict):
        result["audit_warning"] = "The operation succeeded but its completion entry could not be written to the audit log"
    return result


# ── read endpoints ────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/environment")
async def environment(user: dict = Depends(require_perm("view"))):
    out = {"environment": _env.status(), "caller_is_system_super_admin": is_system_super_admin(user),
           "kill_switch": T.kill_switch_state()}
    return out


def _last_updated(lang: str) -> dict:
    """Per track: the newest of the last published version and the last draft edit (UTC ISO), or None. Read only."""
    out = {}
    for track in T.TRACKS:
        stamps = []
        v = T.latest_version(track, lang)
        if v and isinstance(v.get("published_at"), datetime):
            stamps.append(v["published_at"])
        e = T.db[T.VALUES].find_one({"lang": lang, "track": track, "updated_at": {"$type": "date"}}, sort=[("updated_at", -1)])
        if e:
            stamps.append(e["updated_at"])
        out[track] = max(stamps).isoformat() + "Z" if stamps else None
    return out


@router.get("/languages")
async def list_languages(user: dict = Depends(require_perm("view"))):
    rows = []
    mapping = T.all_language_countries()
    for l in T.db[T.LANGS].find().sort("code", 1):
        l.pop("_id", None)
        l["countries"] = mapping.get(l["code"], [])
        l["created_at"] = l["created_at"].isoformat() + "Z" if isinstance(l.get("created_at"), datetime) else l.get("created_at")
        l["status_indicators"] = T.language_status(l["code"])
        l["last_updated"] = _last_updated(l["code"])
        rows.append(l)
    # English is the app's built-in source language, not a stored record (nothing to seed, nothing to duplicate).
    base = {"code": "en", "name_en": "English", "name_native": "English", "direction": "ltr", "bundled": True,
            "countries": mapping.get(T.BASE_LANGUAGE, []),
            "note": "Built-in source language. Its text comes from the app and is not edited here."}
    # The countries an administrator can pick come from the one country master (utils.phone.SUPPORTED_COUNTRIES); this API keeps no list.
    return {"languages": rows, "base_language": base, "supported_countries": _countries.supported_countries()}


@router.get("/strings")
async def list_strings(lang: str, track: str = "app", q: str = "", filter: str = "all", page: int = 1,
                       user: dict = Depends(require_perm("view"))):
    try:
        T.get_language(lang)
        T._check_track(track)
    except T.TranslationError as e:
        raise _http(e)
    if filter not in ("all", "missing", "modified", "published"):
        raise HTTPException(400, {"code": "invalid_filter"})
    work = {v["key"]: v for v in T.db[T.VALUES].find({"lang": lang, "track": track})}
    rows, ql = [], q.strip().lower()
    for k in T.db[T.KEYS].find({"deprecated": {"$ne": True}}).sort("key", 1):
        if T.track_of(k) != track:
            continue
        v = work.get(k["key"])
        locked = bool(k.get("is_plural"))
        status = "locked" if locked else (v["status"] if v else "missing")
        if filter != "all" and status != filter:
            continue
        if ql and ql not in k["key"].lower() and ql not in k.get("english", "").lower():
            continue
        rows.append({"key": k["key"], "screen_group": k.get("screen_group", ""), "english": k.get("english", ""),
                     "placeholders": k.get("placeholders", []), "max_length": k.get("max_length"),
                     "value": v["value"] if v else None, "status": status, "locked": locked,
                     "lock_reason": "plural strings are bundled-only and read-only" if locked else ""})
    size, page = 50, max(1, int(page))
    return {"lang": lang, "track": track, "total": len(rows), "page": page, "page_size": size,
            "rows": rows[(page - 1) * size: page * size]}


@router.get("/preview")
async def preview(lang: str, track: str = "app", user: dict = Depends(require_perm("view"))):
    try:
        return T.preview(lang, track)
    except T.TranslationError as e:
        raise _http(e)


@router.get("/audit")
async def audit_log(action: str = "", lang: str = "", before: str = "", limit: int = 50,
                    user: dict = Depends(require_perm("view"))):
    return {"entries": audit.list_entries(limit, action, lang, before or None)}


@router.get("/kill-switch")
async def get_kill_switch(user: dict = Depends(require_perm("view"))):
    return T.kill_switch_state()


# ── draft editing (edit permission; same in every known environment) ────────────────────────────────────────────────────
@router.put("/strings/{lang}/{key}")
async def put_value(lang: str, key: str, data: dict, request: Request, user: dict = Depends(require_perm("edit"))):
    admin, ip, env = audit.admin_ref(user), _ip(request), T.environment()
    try:
        res = T.set_value(lang, key, data.get("value"), admin["id"])
    except T.TranslationError as e:
        audit.record("edit", admin, "failed", environment=env, lang=lang, key=key, details={"code": e.code}, ip=ip)
        raise _http(e)
    ok = audit.record("edit", admin, "ok", environment=env, lang=lang, track=res["track"], key=key,
                      old_hash=res["old_hash"], new_hash=res["new_hash"], ip=ip)
    if not ok:
        res["audit_warning"] = "The edit was saved but its audit entry could not be written"
    return res


# ── Super Admin operations ────────────────────────────────────────────────────────────────────────────────────────────
@router.post("/languages")
async def add_language(data: dict, request: Request, user: dict = Depends(require_super_admin("add_language"))):
    def fn():
        d = T.add_language(data.get("code"), data.get("name_en"), data.get("name_native"), data.get("direction"),
                           audit.admin_ref(user)["id"], countries=data.get("countries"))
        return {"code": d["code"], "status": d["status"], "direction": d["direction"], "countries": d.get("countries", []),
                "audit_details": {"countries": d.get("countries", [])}}
    return _run_sensitive(request, user, "add_language", fn, lang=str(data.get("code", ""))[:20])


@router.put("/languages/{code}/countries")
async def put_language_countries(code: str, data: dict, request: Request,
                                 user: dict = Depends(require_super_admin("set_language_countries"))):
    """Replaces the countries a language (or English, 'en') is offered in. Super Admin only; audited; this environment only."""
    def fn():
        saved = T.set_language_countries(code, data.get("countries"), audit.admin_ref(user)["id"])
        return {"code": code, "countries": saved, "audit_details": {"countries": saved}}
    return _run_sensitive(request, user, "set_language_countries", fn, lang=code[:20])


@router.patch("/languages/{code}")
async def patch_language(code: str, data: dict, request: Request, user: dict = Depends(require_super_admin("update_language"))):
    def fn():
        d = T.update_language(code, data)
        return {"code": d["code"], "status": d["status"], "direction": d["direction"],
                "name_en": d["name_en"], "name_native": d["name_native"]}
    return _run_sensitive(request, user, "update_language", fn, lang=code[:20], details={"fields": sorted(data)[:6]})


@router.post("/publish")
async def publish(data: dict, request: Request, user: dict = Depends(require_super_admin("publish"))):
    lang, track = str(data.get("lang", "")), str(data.get("track", "app"))
    return _run_sensitive(request, user, "publish", lambda: T.publish(lang, track, data.get("note"), _actor(user)),
                          lang=lang, track=track)


@router.post("/kill-switch")
async def kill_switch(data: dict, request: Request, user: dict = Depends(require_super_admin("kill_switch"))):
    """enabled=true: the app ignores every remote override and uses its bundled text. Affects this environment only."""
    return _run_sensitive(request, user, "kill_switch",
                          lambda: T.set_kill_switch(data.get("enabled"), data.get("reason"), _actor(user)),
                          details={"enabled": data.get("enabled")})
