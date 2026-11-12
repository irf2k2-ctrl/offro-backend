"""
services/wa_webhook_security.py
-------------------------------
Helpers for the WhatsApp webhook: Meta signature verification, safe logging, constant-time compare.

Signature rollout (env WHATSAPP_WEBHOOK_SIGNATURE_MODE, case-insensitive, trimmed):
    off      (default ONLY when unset/empty) -> no verification, behaves exactly as before
    log      -> verify every POST, log a mismatch (reason only, never the payload), ALWAYS accept.
                Any other non-empty value (a typo such as "enforced") also resolves to log, never to off, so a
                typo can never silently disable verification; it is reported at ERROR level.
    enforce  -> reject (403) a POST whose X-Hub-Signature-256 is missing/invalid, or when
                WHATSAPP_APP_SECRET is not configured (fail closed)

Nothing in this module reads or logs a secret value. It never imports services.whatsapp
(that module imports from here), so there is no circular import.
"""
import hashlib
import hmac
import logging
import os
import re

logger = logging.getLogger("offro.webhook.security")

MODES = ("off", "log", "enforce")
MAX_BODY_BYTES = 1024 * 1024        # webhook request bodies above ~1 MB are rejected (Meta payloads are a few KB)
_reported_bad_modes = set()


# ── Mode / secret ──────────────────────────────────────────────────────────────

def signature_mode() -> str:
    raw = os.getenv("WHATSAPP_WEBHOOK_SIGNATURE_MODE", "").strip().lower()
    if not raw:
        return "off"                      # unset/empty: the existing default
    if raw in MODES:
        return raw
    # Unknown non-empty value (typo): NEVER fall back to off. Verify and log, but do not block (log mode).
    if raw not in _reported_bad_modes:
        _reported_bad_modes.add(raw)
        logger.error("[WA-Webhook] Invalid WHATSAPP_WEBHOOK_SIGNATURE_MODE (expected off|log|enforce) - "
                     "using 'log' so signature verification is NOT disabled")
    return "log"


def _app_secret() -> str:
    return os.getenv("WHATSAPP_APP_SECRET", "").strip()   # a pasted secret with a stray newline/space must still verify


# ── Signature verification ─────────────────────────────────────────────────────

def verify_signature(raw_body: bytes, header_value, secret: str) -> bool:
    """True only if header is 'sha256=<hex>' and equals HMAC-SHA256(secret, raw_body). Constant-time."""
    if not secret or not header_value or not isinstance(raw_body, (bytes, bytearray)):
        return False
    header_value = str(header_value).strip()
    if not header_value.lower().startswith("sha256="):
        return False
    supplied = header_value[7:].strip().lower()
    expected = hmac.new(secret.encode("utf-8"), bytes(raw_body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def check_signature(raw_body: bytes, header_value) -> tuple:
    """Apply the configured mode. Returns (allowed: bool, reason: str). reason is a short fixed code
    ('off', 'ok', 'secret_missing', 'header_missing', 'mismatch') - safe to log."""
    mode = signature_mode()
    if mode == "off":
        return True, "off"
    secret = _app_secret()
    if not secret:
        reason = "secret_missing"
    elif not header_value:
        reason = "header_missing"
    elif verify_signature(raw_body, header_value, secret):
        if mode == "log":
            logger.info("[WA-Webhook] Signature check (log mode): ok")   # positive evidence for the staging rollout
        return True, "ok"
    else:
        reason = "mismatch"
    if mode == "log":
        logger.warning("[WA-Webhook] Signature check (log mode): %s - request accepted", reason)
        return True, reason
    logger.warning("[WA-Webhook] Signature check (enforce mode): %s - request REJECTED", reason)
    return False, reason


def tokens_equal(a, b) -> bool:
    """Constant-time string comparison; None / non-str never match."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ── Safe logging helpers ───────────────────────────────────────────────────────

def mask_phone(phone) -> str:
    """'919876543210' -> '***3210'. Never returns more than the last 4 digits."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return "***"
    return "***" + digits[-4:] if len(digits) > 4 else "***"


_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_LONG_TOKEN_RE = re.compile(r"\bEAA[A-Za-z0-9]{8,}\b")
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://[^\s'\"<>)\]]+")          # signed Meta media URLs carry credentials
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{6,}\d")                     # 8+ digits, optionally separated


def redact(text, limit: int = 200) -> str:
    """Remove credentials, URLs (signed Meta media URLs) and phone numbers from free text before logging."""
    out = str(text if text is not None else "")
    tok = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    if tok:
        out = out.replace(tok, "***")
    secret = _app_secret()
    if secret:
        out = out.replace(secret, "***")
    out = _URL_RE.sub("<url>", out)
    out = _BEARER_RE.sub("Bearer ***", out)
    out = _LONG_TOKEN_RE.sub("***", out)
    out = _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), out)
    return out[:limit]


_ERR_CODE_RE = re.compile(r"#\s*(\d{3,7})\b|\bcode[=:\s]+(\d{3,7})\b", re.I)


def safe_error(text) -> str:
    """Log-safe description of a free-text error returned by Meta / the network: ONLY a numeric error code, if one
    is present. Free text can embed names, message text or numbers that no pattern can reliably catch, so it is
    withheld entirely (the code is enough to look the error up in Meta's documentation)."""
    m = _ERR_CODE_RE.search(str(text if text is not None else ""))
    code = (m.group(1) or m.group(2)) if m else None
    return ("error code %s (details withheld)" % code) if code else "details withheld"


def exc_summary(exc) -> str:
    """Exception TYPE and innermost code location only - never the exception message (which can carry phone
    numbers, names, message text, signed URLs or database documents). Use instead of logger.exception / %s of e."""
    name = type(exc).__name__
    tb = getattr(exc, "__traceback__", None)
    last = None
    while tb is not None:
        last, tb = tb, tb.tb_next
    if last is None:
        return name
    return "%s at %s:%d in %s" % (name, os.path.basename(last.tb_frame.f_code.co_filename),
                                  last.tb_lineno, last.tb_frame.f_code.co_name)


def payload_summary(body) -> str:
    """One safe line describing a webhook payload: counts only - no names, phones or message text."""
    msgs = stats = errs = 0
    try:
        for entry in body.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                v = change.get("value", {}) or {}
                msgs += len(v.get("messages", []) or [])
                stats += len(v.get("statuses", []) or [])
                errs += len(v.get("errors", []) or [])
    except Exception:
        return "unparseable"
    return "messages=%d statuses=%d errors=%d" % (msgs, stats, errs)


# ── Uvicorn access-log redaction of the webhook verify token ───────────────────
# Meta can only send the verification token in the QUERY STRING of GET /webhook, and Uvicorn's access log writes
# the full request path. This filter rewrites the record so the token value never reaches any log handler.

_VERIFY_TOKEN_RE = re.compile(r"(hub(?:\.|%2e|_)verify_token(?:=|%3d))[^&#\s\"']*", re.IGNORECASE)


def redact_verify_token(text):
    """Replace the value of hub.verify_token (also URL-encoded spellings) with ***."""
    return _VERIFY_TOKEN_RE.sub(r"\1***", text) if isinstance(text, str) else text


class VerifyTokenRedactingFilter(logging.Filter):
    """Never raises and never drops a record: it only rewrites it."""

    def filter(self, record) -> bool:
        try:
            if isinstance(record.args, tuple) and record.args:
                record.args = tuple(redact_verify_token(a) for a in record.args)
            if isinstance(record.msg, str):
                record.msg = redact_verify_token(record.msg)
        except Exception:
            pass
        return True


def install_access_log_redaction() -> bool:
    """Attach the filter to the `uvicorn.access` logger once (idempotent). A logger-level filter survives
    Uvicorn re-applying its logging config. Returns True if it was newly installed."""
    lg = logging.getLogger("uvicorn.access")
    if any(isinstance(f, VerifyTokenRedactingFilter) for f in lg.filters):
        return False
    lg.addFilter(VerifyTokenRedactingFilter())
    return True
