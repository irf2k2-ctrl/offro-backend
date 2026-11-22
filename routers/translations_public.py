"""routers/translations_public.py - PUBLIC, read-only translation API for the app (Phase G).

GET /translations/manifest      kill switch + active languages with their published version and hash per track
GET /translations/{lang}        the published override file for one language (?track=app|legal, default app)
GET /legal/documents            the customer-facing documents that have English text
GET /legal/{slug}?lang=xx       one document: the published translation for xx, else English

  * Published data only: drafts are never returned. No login, no personal data. It exposes nothing the app does not already
    ship in its own bundled files.
  * Kill switch ON (or unreadable): the manifest says kill_switch=true / overrides_enabled=false with no languages, and the
    language endpoint answers 404. The app then uses its bundled text. Each environment answers from its OWN database/switch.
  * ETag + 304: the manifest revalidates on every request (Cache-Control: no-cache) so a kill switch takes effect quickly; a
    language file is cacheable for 5 minutes.
  * Rate limit: a modest PER-IP limit (default 60 requests per minute, TRANSLATION_PUBLIC_RATE_PER_MIN, 10-600) held IN MEMORY IN
    EACH PROCESS. It is not a distributed rate limiter: with several instances each keeps its own counters, and a restart resets
    them. It only blunts casual abuse; real protection belongs at the edge (Railway / CDN).
"""
import os
import time
from collections import deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from services import legal_documents as LD
from services import translations as T

router = APIRouter(tags=["Translations (public)"])

_WINDOW_SECONDS = 60.0
_MAX_TRACKED_IPS = 5000
_hits: dict = {}


def _limit() -> int:
    try:
        return max(10, min(600, int(os.getenv("TRANSLATION_PUBLIC_RATE_PER_MIN", "60"))))
    except ValueError:
        return 60


def reset_rate_limit() -> None:
    _hits.clear()


def _client_ip(request: Request) -> str:
    try:
        from routers.dashboard_auth import _client_ip as _ip
        return _ip(request)
    except Exception:
        return "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    if len(_hits) > _MAX_TRACKED_IPS:                       # bound memory: drop idle entries
        for k in [k for k, dq in _hits.items() if not dq or now - dq[-1] > _WINDOW_SECONDS]:
            _hits.pop(k, None)
        if len(_hits) > _MAX_TRACKED_IPS:
            _hits.clear()
    dq = _hits.setdefault(ip, deque())
    while dq and now - dq[0] > _WINDOW_SECONDS:
        dq.popleft()
    if len(dq) >= _limit():
        return True
    dq.append(now)
    return False


def _etag_matches(header: str, etag: str) -> bool:
    if not header:
        return False
    for part in header.split(","):
        p = part.strip()
        if p == "*" or p == etag or (p.startswith("W/") and p[2:] == etag):
            return True
    return False


def _limited_response() -> JSONResponse:
    return JSONResponse({"detail": "rate_limited"}, status_code=429, headers={"Retry-After": "60", "Cache-Control": "no-store"})


@router.get("/translations/manifest")
async def manifest(request: Request):
    if _rate_limited(_client_ip(request)):
        return _limited_response()
    body = T.public_manifest()
    etag = '"' + T.hash_strings(body)[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _etag_matches(request.headers.get("if-none-match", ""), etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)


@router.get("/translations/{lang}")
async def language_file(lang: str, request: Request, track: str = "app"):
    if _rate_limited(_client_ip(request)):
        return _limited_response()
    data = T.public_language(lang, track) if T._LANG_RE.match(lang) else None
    if data is None:
        return JSONResponse({"detail": "not_available"}, status_code=404, headers={"Cache-Control": "no-store"})
    etag = '"' + data["hash"] + '"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=300"}
    if _etag_matches(request.headers.get("if-none-match", ""), etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(data, headers=headers)


@router.get("/legal/documents")
async def legal_documents(request: Request):
    if _rate_limited(_client_ip(request)):
        return _limited_response()
    return JSONResponse({"documents": LD.public_list()}, headers={"Cache-Control": "no-cache"})


@router.get("/legal/{slug}")
async def legal_document(slug: str, request: Request, lang: str = ""):
    """One document. Published translation for `lang` when there is one, otherwise English (never a draft). 404 when unknown or empty."""
    if _rate_limited(_client_ip(request)):
        return _limited_response()
    data = LD.public_document(slug, lang) if LD.SLUG_RE.match(slug) else None
    if data is None:
        return JSONResponse({"detail": "not_available"}, status_code=404, headers={"Cache-Control": "no-store"})
    return JSONResponse(data, headers={"Cache-Control": "no-cache"})
