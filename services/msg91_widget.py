"""services/msg91_widget.py - server side of the MSG91 OTP Widget (OFFROOTP) used for ADMIN login.

The browser runs the widget (send / retry / verify). It then hands the widget's access token to OUR server, and this module asks MSG91
(POST https://control.msg91.com/api/v5/widget/verifyAccessToken, body {"authkey", "access-token"}, exactly as shown in the widget's "Server Side
Integration" page) whether that token is genuine. Nothing the browser says is trusted by itself.

Credentials (names only; values live in Railway, never in code, HTML, JavaScript or logs):
    MSG91_AUTH_KEY               server secret (account level), used ONLY here, ONLY for verifyAccessToken
    MSG91_WEB_WIDGET_ID          public identifier of the WEB widget (OffroWeb) used by the admin website
    MSG91_WEB_WIDGET_TOKEN_AUTH  that widget's public client token (public by design; it is NOT the auth key)
  The MOBILE widget (OffroOTP: MSG91_WIDGET_ID / MSG91_WIDGET_TOKEN_AUTH) is used by the Flutter app and is NEVER used for the admin website: MSG91
  answers "Web requests are not allowed for this widget" when a web page uses a mobile widget. There is deliberately NO fallback to the mobile widget.

DOCUMENTED UNCERTAINTY (isolated in parse_verified_identifier): MSG91's public documentation shows the verifyAccessToken REQUEST but not the shape of
its RESPONSE (the dashboard only says it returns "a success or failure"). The admin sign-in MUST know WHICH phone number was verified, otherwise an
attacker who knows a PIN could verify their OWN phone and be let in. So the parser looks for the verified number in (a) the response body and (b) the
claims of the access token itself (a signed JWT that MSG91 has just confirmed is genuine), and if neither contains one it returns None and the login
FAILS CLOSED. It never guesses and never trusts an unverified value. The response SHAPE (key names and types only, never values) is logged so the
parser can be tightened after the first real Staging run.
"""
import base64
import json
import os
import re

import httpx

VERIFY_URL = "https://control.msg91.com/api/v5/widget/verifyAccessToken"
TIMEOUT_SECONDS = 10.0
TOKEN_MIN_LEN, TOKEN_MAX_LEN = 20, 4096
_JWT = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9._~+/=-]+$")          # sanity only: the token is NOT required to be a JWT; MSG91 is the judge

# Candidate field names for "the verified phone number" (matched case-insensitively, searched up to 3 levels deep).
IDENTIFIER_KEYS = ("identifier", "mobile", "mobilenumber", "mobile_number", "phone", "phonenumber", "phone_number", "contact", "contactnumber",
                   "contact_number", "number", "msisdn")


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


WEB_ID_VAR, WEB_TOKEN_VAR = "MSG91_WEB_WIDGET_ID", "MSG91_WEB_WIDGET_TOKEN_AUTH"
MOBILE_ID_VAR = "MSG91_WIDGET_ID"            # read ONLY to detect a copy/paste mistake; never used to configure the website


def public_config() -> dict:
    """The only widget settings the browser receives: the WEB widget's, both public by design. The auth key is never included."""
    return {"widget_id": _env(WEB_ID_VAR), "token_auth": _env(WEB_TOKEN_VAR)}


def config_problems() -> list:
    """Names (never values) of what is wrong with the admin web-widget configuration; empty = usable."""
    out = [n for n in (WEB_ID_VAR, WEB_TOKEN_VAR, "MSG91_AUTH_KEY") if not _env(n)]
    wid, mid = _env(WEB_ID_VAR), _env(MOBILE_ID_VAR)
    if wid and mid and wid == mid:
        out.append(WEB_ID_VAR + " is the same as the mobile widget id (MSG91 refuses web requests for a mobile widget)")
    return out


def configured() -> bool:
    return not config_problems()


def selection_label() -> str:
    """Safe description of which widget configuration is selected, for logs: the kind and a short fingerprint of the (public) widget id.
    Never the token, never the auth key, never the full id."""
    import hashlib
    wid = _env(WEB_ID_VAR)
    fp = hashlib.sha256(wid.encode()).hexdigest()[:8] if wid else "none"
    return "widget=web(OffroWeb) id_fp=%s token=%s auth_key=%s" % (fp, "set" if _env(WEB_TOKEN_VAR) else "MISSING",
                                                                  "set" if _env("MSG91_AUTH_KEY") else "MISSING")


def identifier_for(mobile: str) -> str:
    """Country code + 10-digit number, no '+', as the widget requires (e.g. 919999999999)."""
    d = "".join(ch for ch in str(mobile) if ch.isdigit())
    return ("91" + d[-10:]) if len(d) >= 10 else ""


def normalize_identifier(value) -> str:
    """Digits only; a bare 10-digit number is treated as Indian (91 prefixed) so both spellings compare equal."""
    d = "".join(ch for ch in str(value) if ch.isdigit())
    if len(d) == 10:
        d = "91" + d
    if len(d) == 11 and d.startswith("0"):
        d = "91" + d[1:]
    return d


def _b64json(part: str):
    try:
        pad = "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part + pad).decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def token_claims(access_token: str) -> dict:
    """Claims of the access token WITHOUT verifying a signature. Only ever used AFTER MSG91 has confirmed the token is genuine (which covers its
    signature), and only for the sanity checks in admin_login_otp (verified number, issued-at, expiry). Never used on its own."""
    if not isinstance(access_token, str) or not _JWT.match(access_token):
        return {}
    return _b64json(access_token.split(".")[1]) or {}


def _walk(obj, depth=0):
    """Yield (lower-cased key, value) pairs from nested dicts / JSON strings, up to 3 levels deep."""
    if depth > 3:
        return
    if isinstance(obj, str) and obj[:1] in "{[" and depth < 3:
        try:
            obj = json.loads(obj)
        except Exception:
            return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k).lower(), v
            if isinstance(v, (dict, str)):
                for pair in _walk(v, depth + 1):
                    yield pair
    elif isinstance(obj, list):
        for v in obj[:5]:
            for pair in _walk(v, depth + 1):
                yield pair


def _find_number(obj):
    for key, value in _walk(obj):
        if key in IDENTIFIER_KEYS and isinstance(value, (str, int)) and not isinstance(value, bool):
            d = normalize_identifier(value)
            if 10 <= len(d) <= 15:
                return d
    return None


def parse_verified_identifier(response_body, access_token: str):
    """The phone number MSG91 says was verified, or None if it cannot be determined (the caller must then REJECT the sign-in)."""
    return _find_number(response_body) or _find_number(token_claims(access_token))


def _shape(obj, depth=0):
    """Key names and value TYPES only, never values (safe to log)."""
    if depth > 2:
        return "..."
    if isinstance(obj, dict):
        return {str(k)[:24]: _shape(v, depth + 1) for k, v in list(obj.items())[:12]}
    return type(obj).__name__


def _is_success(body) -> bool:
    if not isinstance(body, dict):
        return False
    t = str(body.get("type", "")).lower()
    if t == "error" or str(body.get("code", "")) == "201":
        return False                                                   # MSG91 widget routes can answer HTTP 200 with an error payload
    return t == "success"


def verify_access_token(access_token: str) -> dict:
    """Ask MSG91 whether `access_token` is genuine. Returns:
        {"ok": True,  "identifier": "91XXXXXXXXXX" | None, "claims": {...}}      MSG91 confirmed the token (identifier None = could not be determined)
        {"ok": False, "error": "invalid"}                                       MSG91 rejected the token (wrong / expired / used)
        {"ok": False, "error": "unavailable", "detail": <short code>}           MSG91 could not be asked or answered unusably (NOT the user's fault)
    Never logs or returns the token or the auth key."""
    if not isinstance(access_token, str) or not (TOKEN_MIN_LEN <= len(access_token) <= TOKEN_MAX_LEN) or not _TOKEN_CHARS.match(access_token):
        return {"ok": False, "error": "invalid"}
    key = _env("MSG91_AUTH_KEY")
    if not key:
        return {"ok": False, "error": "unavailable", "detail": "not_configured"}
    try:
        resp = httpx.post(VERIFY_URL, json={"authkey": key, "access-token": access_token}, headers={"Content-Type": "application/json"},
                          timeout=TIMEOUT_SECONDS)
    except httpx.TimeoutException:
        print("[2FA][widget] verifyAccessToken timed out")
        return {"ok": False, "error": "unavailable", "detail": "timeout"}
    except Exception as e:
        print("[2FA][widget] verifyAccessToken network error type=" + type(e).__name__)
        return {"ok": False, "error": "unavailable", "detail": "network"}
    try:
        body = resp.json()
    except Exception:
        print("[2FA][widget] verifyAccessToken non-JSON reply, HTTP %s" % resp.status_code)
        return {"ok": False, "error": "unavailable", "detail": "non_json"}
    if resp.status_code >= 500:
        return {"ok": False, "error": "unavailable", "detail": "http_%s" % resp.status_code}
    if resp.status_code in (401, 403) or (isinstance(body, dict) and str(body.get("code", "")) == "201"):
        print("[2FA][widget] verifyAccessToken: MSG91 rejected OUR credentials (HTTP %s); check MSG91_AUTH_KEY and its IP allow-list" % resp.status_code)
        return {"ok": False, "error": "unavailable", "detail": "auth"}
    print("[2FA][widget] verifyAccessToken HTTP %s response shape=%s" % (resp.status_code, json.dumps(_shape(body), sort_keys=True)))
    if not _is_success(body):
        msg = body.get("message") if isinstance(body, dict) else None
        print("[2FA][widget] verifyAccessToken: token not accepted by MSG91 (%s) reason=%s" % (selection_label(), str(msg)[:80] if isinstance(msg, str) else "n/a"))
        return {"ok": False, "error": "invalid"}
    claims = token_claims(access_token)
    print("[2FA][widget] access token claim names=%s" % sorted(str(k)[:24] for k in claims)[:12])
    return {"ok": True, "identifier": parse_verified_identifier(body, access_token), "claims": claims}


# ── Content-Security-Policy for the admin LOGIN PAGE only ──────────────────────────────────────────────────────────────────────────
# The widget SDK is loaded from verify.msg91.com and talks to MSG91 (and may render a captcha). The exact hosts it uses are not documented in
# the material available when this was written, so the login page (and ONLY the login page) additionally allows *.msg91.com and the two common
# captcha providers. Every other page keeps the strict site-wide policy in services/security.py. Verify on Staging with the browser console
# open: any "Refused to ..." line names a host that still needs adding here.
_MSG91_HOSTS = "https://*.msg91.com"
_CAPTCHA_HOSTS = "https://www.google.com https://www.gstatic.com https://www.recaptcha.net https://hcaptcha.com https://*.hcaptcha.com"


def login_page_csp(base: str) -> str:
    out = []
    for part in base.split("; "):
        name = part.split(" ", 1)[0]
        if name == "script-src":
            part += " " + _MSG91_HOSTS + " " + _CAPTCHA_HOSTS
        elif name == "connect-src":
            part += " " + _MSG91_HOSTS + " " + _CAPTCHA_HOSTS
        elif name == "style-src":
            part += " " + _CAPTCHA_HOSTS
        out.append(part)
    if not any(p.startswith("frame-src") for p in out):
        out.append("frame-src " + _CAPTCHA_HOSTS + " " + _MSG91_HOSTS)
    return "; ".join(out)
