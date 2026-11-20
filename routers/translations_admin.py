"""routers/translations_admin.py - admin API for the translation module (Phase G; dashboard UI added in Phase H).

Prefix /admin/translations (mounted in server.py). Uses the existing dashboard session (get_current_dashboard_user).

Who may do what
  Privileged ("system Super Admin") = role_name == "Super Admin" AND is_system_role is True. A role that was merely RENAMED
  "Super Admin" never qualifies. Privileged only: add/change a language, publish, roll back, approve for production, export or
  import a package, kill switch, legal review. Everyone else needs the explicit "Translations" module permission:
  view -> read endpoints, edit -> save drafts and submit. The role is checked here on every call (never trusted from the client).
Audit
  Every sensitive operation writes a strict "started" entry BEFORE it changes anything (if that cannot be stored the operation is
  refused with 503 audit_unavailable) and an ok/failed entry afterwards. Denied attempts are recorded too.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from routers.dashboard_auth import get_current_dashboard_user
from services import app_environment as _env
from services import translation_audit as audit
from services import translation_promotion as promo
from services import translations as T

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
                     details={"code": e.code}, ip=ip)
        raise _http(e)
    ok = audit.record(action, admin, "ok", environment=env, lang=lang, track=track,
                      version=result.get("version") if isinstance(result, dict) else None,
                      new_hash=(result.get("hash") if isinstance(result, dict) else "") or "", ip=ip)
    if not ok and isinstance(result, dict):
        result["audit_warning"] = "The operation succeeded but its completion entry could not be written to the audit log"
    return result


# ── read endpoints ────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/environment")
async def environment(user: dict = Depends(require_perm("view"))):
    out = {"environment": _env.status(), "caller_is_system_super_admin": is_system_super_admin(user),
           "kill_switch": T.kill_switch_state()}
    if is_system_super_admin(user):
        out["package_config"] = promo.config_status()
        out["pending_packages"] = promo.pending_packages()
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
    for l in T.db[T.LANGS].find().sort("code", 1):
        l.pop("_id", None)
        l["created_at"] = l["created_at"].isoformat() + "Z" if isinstance(l.get("created_at"), datetime) else l.get("created_at")
        l["status_indicators"] = T.language_status(l["code"])
        l["last_updated"] = _last_updated(l["code"])
        rows.append(l)
    return {"languages": rows}


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


@router.get("/versions")
async def versions(lang: str, track: str = "app", user: dict = Depends(require_perm("view"))):
    try:
        T.get_language(lang)
        T._check_track(track)
    except T.TranslationError as e:
        raise _http(e)
    out = []
    for v in T.db[T.VERSIONS].find({"lang": lang, "track": track}).sort("version", -1):
        out.append({"version": v["version"], "hash": v["hash"], "count": len(v["strings"]), "note": v.get("note", ""),
                    "published_by": v.get("published_by"), "published_at": v["published_at"].isoformat() + "Z",
                    "restored_from": v.get("restored_from"), "promoted_from": v.get("promoted_from"),
                    "approved": bool(v.get("approval")), "environment": v.get("environment", "")})
    return {"lang": lang, "track": track, "versions": out}


@router.get("/audit")
async def audit_log(action: str = "", lang: str = "", before: str = "", limit: int = 50,
                    user: dict = Depends(require_perm("view"))):
    return {"entries": audit.list_entries(limit, action, lang, before or None)}


@router.get("/kill-switch")
async def get_kill_switch(user: dict = Depends(require_perm("view"))):
    return T.kill_switch_state()


# ── draft editing (edit permission; never available in production) ────────────────────────────────────────────────────
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


@router.post("/submit")
async def submit_drafts(data: dict, request: Request, user: dict = Depends(require_perm("edit"))):
    admin, ip, env = audit.admin_ref(user), _ip(request), T.environment()
    lang, track = str(data.get("lang", "")), str(data.get("track", "app"))
    try:
        n = T.submit(lang, track)
    except T.TranslationError as e:
        audit.record("submit", admin, "failed", environment=env, lang=lang, track=track, details={"code": e.code}, ip=ip)
        raise _http(e)
    audit.record("submit", admin, "ok", environment=env, lang=lang, track=track, details={"submitted": n}, ip=ip)
    return {"lang": lang, "track": track, "submitted": n}


# ── Super Admin operations ────────────────────────────────────────────────────────────────────────────────────────────
@router.post("/languages")
async def add_language(data: dict, request: Request, user: dict = Depends(require_super_admin("add_language"))):
    def fn():
        d = T.add_language(data.get("code"), data.get("name_en"), data.get("name_native"), data.get("direction"),
                           audit.admin_ref(user)["id"])
        return {"code": d["code"], "status": d["status"], "direction": d["direction"]}
    return _run_sensitive(request, user, "add_language", fn, lang=str(data.get("code", ""))[:20])


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


@router.post("/rollback")
async def rollback(data: dict, request: Request, user: dict = Depends(require_super_admin("rollback"))):
    lang, track = str(data.get("lang", "")), str(data.get("track", "app"))
    return _run_sensitive(request, user, "rollback",
                          lambda: T.rollback(lang, track, data.get("version"), data.get("note"), _actor(user)),
                          lang=lang, track=track, details={"to_version": data.get("version")})


@router.post("/approve-for-production")
async def approve(data: dict, request: Request, user: dict = Depends(require_super_admin("approve_for_production"))):
    lang, track = str(data.get("lang", "")), str(data.get("track", "app"))
    def fn():
        r = T.approve_for_production(lang, track, data.get("version"), data.get("note"), _actor(user))
        return {"lang": lang, "track": track, "version": r["version"], "approved": True}
    return _run_sensitive(request, user, "approve_for_production", fn, lang=lang, track=track)


@router.get("/package")
async def export_package(lang: str, version: int, request: Request, track: str = "app",
                         user: dict = Depends(require_super_admin("package_export"))):
    """Staging only. Returns {payload, signature, alg}; the file is carried to production and uploaded there."""
    out = {}
    def fn():
        out.update(promo.build_package(lang, track, version, _actor(user)))
        return {"version": out["payload"]["version"], "hash": out["payload"]["hash"]}
    _run_sensitive(request, user, "package_export", fn, lang=lang, track=track, version=version)
    return out


@router.post("/package/import")
async def import_package(data: dict, request: Request, user: dict = Depends(require_super_admin("package_import"))):
    """Production only. Verifies the package and stores it as a DRAFT. It does NOT publish."""
    def fn():
        return promo.import_package(data, _actor(user))
    return _run_sensitive(request, user, "package_import", fn,
                          lang=str((data.get("payload") or {}).get("lang", ""))[:20] if isinstance(data, dict) else "")


@router.post("/kill-switch")
async def kill_switch(data: dict, request: Request, user: dict = Depends(require_super_admin("kill_switch"))):
    """enabled=true: the app ignores every remote override and uses its bundled text. Affects this environment only."""
    return _run_sensitive(request, user, "kill_switch",
                          lambda: T.set_kill_switch(data.get("enabled"), data.get("reason"), _actor(user)),
                          details={"enabled": data.get("enabled")})


@router.post("/legal/review")
async def legal_review(data: dict, request: Request, user: dict = Depends(require_super_admin("legal_review"))):
    lang, key = str(data.get("lang", "")), str(data.get("key", ""))
    return _run_sensitive(request, user, "legal_review",
                          lambda: T.record_legal_review(lang, key, _actor(user), data.get("note")), lang=lang)
