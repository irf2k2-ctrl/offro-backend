"""
services/wa_campaign_settings.py
--------------------------------
Safety switches for WhatsApp campaigns (Phase 2A). NOTHING here sends a message.

Sending is possible only when BOTH keys are on (two independent keys, default OFF):
    1. env  WA_CAMPAIGN_SENDING_ENABLED  truthy (1/true/yes/on)  - set per Railway environment; never set on
       Production until bulk sending is explicitly approved.
    2. DB   wa_campaign_settings._id == "global", sending_enabled == True - the runtime KILL SWITCH. An admin can turn
       it OFF instantly (no redeploy). Turning it ON needs the env key AND the 'approve' permission, and is audited.
Any error reading either key means "not allowed" (fail closed). A missing settings document means OFF.

Other (read-only here):
    WA_CAMPAIGN_WORKER_ENABLED   reserved for the Phase 2E worker; default OFF, never on in Production by default
    WA_CAMPAIGN_TEST_NUMBERS     comma/space separated allow-list of numbers that may receive TEST messages; if unset
                                 or empty NO number may receive a test message (fail closed)
"""
import logging
import os
from datetime import datetime

from database import db
from services import wa_consent
from services.wa_webhook_security import exc_summary, mask_phone, signature_mode

logger = logging.getLogger("offro.wa_campaigns.settings")

SETTINGS = "wa_campaign_settings"
GLOBAL_ID = "global"
_TRUTHY = ("1", "true", "yes", "on")


class SendingGateError(Exception):
    """The requested change is refused (e.g. enabling while the environment key is off)."""


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def env_sending_enabled() -> bool:
    return _env_flag("WA_CAMPAIGN_SENDING_ENABLED")


def worker_enabled() -> bool:
    return _env_flag("WA_CAMPAIGN_WORKER_ENABLED")


# ── Environment identification (explicit, fail closed) ───────────────────────────────────────────
# History: Production was detected only by matching "production"/"prod" in five variables, and anything unrecognised was treated as NOT
# production. That (a) cannot tell Staging from Production when a copied variable says "production" and (b) fails OPEN on an unknown name.
#
# Rule now:
#   * WA_CAMPAIGN_ENVIRONMENT names the environment explicitly: production | staging | development | test.
#   * Any other / missing value => "unknown" => every sending path is refused.
#   * Platform-set names (RAILWAY_ENVIRONMENT_NAME / RAILWAY_ENVIRONMENT) saying production can NOT be overridden by the explicit value:
#     they cannot be copied between services by accident, so a disagreement is treated as Production (fail closed).
#   * User-set generic names (APP_ENV / ENVIRONMENT / ENV) saying production lose to a non-production explicit value (they are the
#     variables most often copied from Production to Staging by mistake); the override is reported in the status.
ENVIRONMENT_VAR = "WA_CAMPAIGN_ENVIRONMENT"
ENVIRONMENTS = ("production", "staging", "development", "test")
_PLATFORM_ENV_VARS = ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT")
_GENERIC_ENV_VARS = ("APP_ENV", "ENVIRONMENT", "ENV")
_PRODUCTION_WORDS = ("production", "prod")


def _env_value(name: str) -> str:
    return (os.getenv(name, "") or "").strip().lower()


def environment_status() -> dict:
    """What this process believes it is. Names and the environment words only - no secrets. classification is one of
    production | staging | development | test | unknown. Never raises."""
    explicit = _env_value(ENVIRONMENT_VAR)
    platform_prod = [n for n in _PLATFORM_ENV_VARS if _env_value(n) in _PRODUCTION_WORDS]
    generic_prod = [n for n in _GENERIC_ENV_VARS if _env_value(n) in _PRODUCTION_WORDS]
    out = {"classification": "unknown", "source": "none", "explicit": explicit or None, "conflict": False,
           "production_signals": platform_prod + generic_prod, "overridden_signals": []}
    if explicit and explicit not in ENVIRONMENTS:
        out["source"] = "explicit_invalid"
    elif explicit == "production":
        out.update(classification="production", source="explicit")
    elif explicit:
        if platform_prod:
            out.update(classification="production", source="platform_conflict", conflict=True)
        else:
            out.update(classification=explicit, source="explicit", overridden_signals=generic_prod)
    elif platform_prod or generic_prod:
        out.update(classification="production", source="signal")
    return out


def environment_gate(allow_production: bool = False) -> tuple:
    """(ok, reason) for ANY actual send. Production is refused (the bulk worker alone may be explicitly allowed through
    WA_CAMPAIGN_WORKER_ALLOW_PRODUCTION); an unknown environment is ALWAYS refused. Fails closed on any error."""
    try:
        c = environment_status()["classification"]
    except Exception as e:
        logger.error("[WA-Campaign] environment check failed (%s) - treating as unknown", exc_summary(e))
        return False, "environment_unknown"
    if c == "production":
        return (True, "ok") if allow_production else (False, "production_blocked")
    if c == "unknown":
        return False, "environment_unknown"
    return True, "ok"


def switch_on() -> bool:
    """The runtime kill-switch value. Raises on a database error; an absent document is OFF."""
    doc = db[SETTINGS].find_one({"_id": GLOBAL_ID}, {"sending_enabled": 1})
    return bool(doc and doc.get("sending_enabled") is True)


def sending_allowed() -> tuple:
    """(allowed, reason). reason: ok | env_disabled | switch_off | settings_unavailable. Fails closed."""
    if not env_sending_enabled():
        return False, "env_disabled"
    try:
        if not switch_on():
            return False, "switch_off"
    except Exception as e:
        logger.error("[WA-Campaign] sending switch unreadable (%s) - treating sending as OFF", exc_summary(e))
        return False, "settings_unavailable"
    return True, "ok"


def set_sending_enabled(enabled: bool, admin: dict) -> bool:
    """Flip the runtime kill switch. Turning it OFF always works. Turning it ON requires the environment key.
    Raises SendingGateError if refused; database errors propagate (caller returns 503)."""
    if enabled and not env_sending_enabled():
        raise SendingGateError("env_disabled")
    now = datetime.utcnow()
    res = db[SETTINGS].update_one(
        {"_id": GLOBAL_ID},
        {"$set": {"sending_enabled": bool(enabled), "updated_at": now,
                  "updated_by": {"id": str((admin or {}).get("id", "")), "name": str((admin or {}).get("name", ""))[:80]}}},
        upsert=True)
    if not res.acknowledged:
        raise RuntimeError("settings write not acknowledged")
    return bool(enabled)


# ── Test-number allow-list ─────────────────────────────────────────────────────

def test_numbers() -> frozenset:
    """Canonical numbers from WA_CAMPAIGN_TEST_NUMBERS (invalid entries ignored)."""
    raw = os.getenv("WA_CAMPAIGN_TEST_NUMBERS", "")
    out = set()
    for part in raw.replace(";", ",").replace("\n", ",").split(","):
        p = wa_consent.normalize_phone(part.strip())
        if p:
            out.add(p)
    return frozenset(out)


def is_test_number(phone) -> bool:
    """True only if the number is on the allow-list. Empty allow-list -> False for everyone."""
    p = wa_consent.normalize_phone(phone)
    return bool(p) and p in test_numbers()


# ── Safe configuration snapshot (booleans / counts / last-4 of non-secret ids ONLY) ──

def _last4(value):
    v = str(value or "").strip()
    return ("***" + v[-4:]) if len(v) >= 4 else None


def config_snapshot() -> dict:
    from services import whatsapp as wa          # late import: keeps this module import-light
    allowed, reason = sending_allowed()
    try:
        sw = switch_on()
    except Exception:
        sw = None
    return {
        "sending": {"allowed": allowed, "reason": reason, "env_key_on": env_sending_enabled(), "switch_on": sw},
        "worker_env_enabled": worker_enabled(),
        "environment": environment_status(),
        "test_numbers_configured": len(test_numbers()),
        "test_numbers_masked": sorted("***" + n[-4:] for n in test_numbers()),
        "whatsapp": {
            "access_token_configured": bool(wa.WHATSAPP_ACCESS_TOKEN),
            "phone_number_id_configured": bool(wa.WHATSAPP_PHONE_NUMBER_ID),
            "phone_number_id_last4": _last4(wa.WHATSAPP_PHONE_NUMBER_ID),
            "business_account_id_configured": bool(wa.WHATSAPP_BUSINESS_ACCOUNT_ID),
            "business_account_id_last4": _last4(wa.WHATSAPP_BUSINESS_ACCOUNT_ID),
            "verify_token_is_default": wa.WHATSAPP_VERIFY_TOKEN == "offro_webhook_secret",
        },
        "webhook": {"signature_mode": signature_mode(),
                    "app_secret_configured": bool(os.getenv("WHATSAPP_APP_SECRET", "").strip())},
        "template_test_enabled": _env_flag("WHATSAPP_TEMPLATE_TEST_ENABLED"),
    }
