"""
services/wa_webhook_diag.py
---------------------------
Visible, PRIVACY-SAFE diagnostics for the WhatsApp webhook (POST /webhook).

Why: the app configures no logging, so every INFO line from the `offro.*` loggers is silently dropped (only WARNING and above reach Railway). That made it
impossible to see which events Meta sends. Turning on INFO globally would be noisy, so this module owns ONE dedicated logger, `offro.wa_webhook_diag`,
with its own stdout handler at INFO and propagate=False. Nothing else changes level; no other logger is affected.

What it logs (only these, only in these shapes):
    event   object=<known word|other> changes=[<field>(msgs=N,statuses=N,errors=N), ...]
    status  status=<sent|delivered|read|failed|other> ts=<digits> code=<Meta error code|-> ref=<message fingerprint> claim=<claimed|duplicate|...>

What it NEVER logs: phone numbers (not even masked), names, message text, tokens, headers, payloads, free-text Meta error titles, or a full message id.
A message id is shown only as a fingerprint `m_` + the first 12 hex characters of its SHA-256. (WhatsApp message ids are opaque and may embed
encoded identifiers, so even a suffix is not used.) The same fingerprint is stored on the template-test record and can be used with
GET /admin/whatsapp/template-test/status?ref=... to find the matching test.

Every function here swallows its own errors: a diagnostic can never change how the webhook answers Meta.
"""
import hashlib
import logging
import re
import sys

LOGGER_NAME = "offro.wa_webhook_diag"
_log = logging.getLogger(LOGGER_NAME)

_KNOWN_OBJECTS = ("whatsapp_business_account", "page", "instagram", "user")
_KNOWN_STATUSES = ("sent", "delivered", "read", "failed")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_DIGITS_RE = re.compile(r"^[0-9]{1,13}$")
_CLAIM_RE = re.compile(r"^[a-z_]{1,20}$")


class _StdoutHandler(logging.StreamHandler):
    """StreamHandler that always writes to the CURRENT sys.stdout (survives stdout being replaced/captured)."""
    def __init__(self):
        super().__init__(sys.stdout)

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value):
        pass


def install() -> bool:
    """Attach the dedicated stdout handler once (idempotent). Returns True if it was newly installed."""
    for h in _log.handlers:
        if getattr(h, "_wa_diag", False):
            return False
    h = _StdoutHandler()
    h._wa_diag = True
    h.setLevel(logging.INFO)
    h.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(h)
    _log.setLevel(logging.INFO)
    _log.propagate = False                      # never leaks into (or depends on) the root logger
    return True


install()


def msg_ref(msg_id) -> str:
    """Safe fingerprint of a Meta message id: 'm_' + 12 hex chars of SHA-256, or 'none'."""
    if not isinstance(msg_id, str) or not msg_id.strip():
        return "none"
    return "m_" + hashlib.sha256(msg_id.strip().encode("utf-8")).hexdigest()[:12]


def safe_word(value) -> str:
    """A status word safe to log: one of sent/delivered/read/failed, else 'other' (never free text)."""
    v = str(value or "").lower()
    return v if v in _KNOWN_STATUSES else "other"


def safe_digits(value) -> str:
    """A unix timestamp safe to log: 1-13 digits, else '-'."""
    v = str(value or "").strip()
    return v if _DIGITS_RE.match(v) else "-"


def _count(x) -> int:
    return len(x) if isinstance(x, list) else 0


def describe_event(body) -> str:
    """One line describing the shape of a webhook body: the object type and, per change, the field name and how many messages / statuses / errors."""
    if not isinstance(body, dict):
        return "event object=non-object"
    obj = body.get("object")
    obj = obj if obj in _KNOWN_OBJECTS else "other"
    parts = []
    entries = body.get("entry")
    for entry in (entries if isinstance(entries, list) else [])[:10]:
        changes = entry.get("changes") if isinstance(entry, dict) else None
        for ch in (changes if isinstance(changes, list) else [])[:10]:
            if not isinstance(ch, dict):
                continue
            field = ch.get("field")
            field = field if isinstance(field, str) and _FIELD_RE.match(field) else "other"
            v = ch.get("value") if isinstance(ch.get("value"), dict) else {}
            parts.append("%s(msgs=%d,statuses=%d,errors=%d)" % (field, _count(v.get("messages")), _count(v.get("statuses")), _count(v.get("errors"))))
    return "event object=%s changes=[%s]" % (obj, ", ".join(parts))


def error_code(status) :
    """Meta's numeric failure code from status['errors'][0]['code'], or None. Text fields (title/message/details) are never read."""
    try:
        c = (status.get("errors") or [{}])[0].get("code")
        return c if isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 999999999 else None
    except Exception:
        return None


def describe_status(status, claim="") -> str:
    status = status if isinstance(status, dict) else {}
    st = str(status.get("status", "")).lower()
    st = st if st in _KNOWN_STATUSES else "other"
    ts = str(status.get("timestamp", "")).strip()
    ts = ts if _DIGITS_RE.match(ts) else "-"
    code = error_code(status)
    claim = str(claim or "")
    claim = claim if _CLAIM_RE.match(claim) else "-"
    return "status=%s ts=%s code=%s ref=%s claim=%s" % (st, ts, "-" if code is None else code, msg_ref(status.get("id")), claim)


def _emit(line: str) -> None:
    try:
        _log.info("[WA-Webhook][diag] " + line)
    except Exception:
        pass


def event(body) -> None:
    try:
        _emit(describe_event(body))
    except Exception:
        pass


def status(status_entry, claim="") -> None:
    try:
        _emit(describe_status(status_entry, claim))
    except Exception:
        pass
