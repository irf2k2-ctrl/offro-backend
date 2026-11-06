"""
services/security.py — web hardening shared by server.py
=========================================================
  * cors_origins()            explicit trusted-origin list (replaces allow_origins=["*"] + credentials)
  * SecurityMiddleware (ASGI) security headers on every response, CSP on HTML, the Secure flag on every
                              Set-Cookie sent over HTTPS, and noindex / no-docs / no-smart-link on STAGING hosts
Nothing here reads or writes the database or any credential.
"""
import os

# Origins that may make credentialed browser requests. Native mobile apps do not use CORS.
DEFAULT_CORS_ORIGINS = (
    "https://theoffro.com",
    "https://www.theoffro.com",
    "https://admin.theoffro.com",
    "https://offro-backend-staging-d375.up.railway.app",
    "https://offro-backend-production.up.railway.app",
)


def cors_origins() -> list:
    """DEFAULT_CORS_ORIGINS plus anything in the optional env var CORS_ALLOW_ORIGINS (comma separated).
    A literal '*' is ignored: wildcard origins must never be combined with credentials."""
    extra = [o.strip().rstrip("/") for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",")]
    out = list(DEFAULT_CORS_ORIGINS)
    for o in extra:
        if o and o != "*" and o not in out:
            out.append(o)
    return out


# Pages need inline <script>/<style> today (inline onclick handlers) - 'unsafe-inline' stays until they are
# refactored. Everything else is pinned to the hosts the templates actually load from.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com",
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com",
    "font-src 'self' data: https://cdnjs.cloudflare.com https://cdn.jsdelivr.net",
    "img-src 'self' data: blob: https:",
    "media-src 'self' blob: https:",
    "connect-src 'self' https://control.msg91.com",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])

_STATIC_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"strict-origin-when-cross-origin",
    b"permissions-policy": b"camera=(self), microphone=(), geolocation=(self), payment=()",
}
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")


def is_staging_host(host: str) -> bool:
    """True for a Staging deployment host (first label contains 'staging'), e.g. offro-backend-staging-d375.up.railway.app."""
    first = (host or "").split(":")[0].lower().split(".")[0]
    return "staging" in first


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


async def _respond(send, status: int, body: bytes, ctype: bytes = b"application/json"):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode()),
                            (b"x-robots-tag", b"noindex, nofollow")]})
    await send({"type": "http.response.body", "body": body})


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        req = {k.lower(): v for k, v in scope.get("headers", [])}
        host = req.get(b"host", b"").decode("latin-1")
        proto = req.get(b"x-forwarded-proto", b"").decode("latin-1").split(",")[0].strip().lower() or scope.get("scheme", "http")
        https = proto == "https"
        staging = is_staging_host(host)
        crawl_ok = _flag("STAGING_ALLOW_CRAWL")   # temporarily set while a Safe Browsing review is pending (Googlebot must reach the pages)
        path = scope.get("path", "")

        if staging:
            if path == "/robots.txt" and not crawl_ok:
                return await _respond(send, 200, b"User-agent: *\nDisallow: /\n", b"text/plain")
            if path in _DOC_PATHS and not _flag("ENABLE_API_DOCS"):
                return await _respond(send, 404, b'{"detail":"Not Found"}')
            if path == "/open":      # smart-link store-redirect page belongs to theoffro.com, not a staging host
                return await _respond(send, 404, b'{"detail":"Not Found"}')

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"").lower()
                for k, v in _STATIC_HEADERS.items():
                    if k not in present:
                        headers.append((k, v))
                if ctype.startswith(b"text/html") and b"content-security-policy" not in present:
                    headers.append((b"content-security-policy", CSP.encode()))
                if https and b"strict-transport-security" not in present:
                    headers.append((b"strict-transport-security", b"max-age=15552000"))
                if staging and not crawl_ok and b"x-robots-tag" not in present:
                    headers.append((b"x-robots-tag", b"noindex, nofollow"))
                if https:    # every cookie set over HTTPS gets the Secure flag (works for all current and future cookies)
                    fixed = []
                    for k, v in headers:
                        if k.lower() == b"set-cookie":
                            attrs = [a.strip().lower() for a in v.split(b";")[1:]]
                            if b"secure" not in attrs:
                                v = v + b"; Secure"
                        fixed.append((k, v))
                    headers = fixed
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)
