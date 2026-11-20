"""services/app_environment.py - which OffRo environment is this process? (generic; used by the translation module.)

Why this exists: services/wa_campaign_settings.py answers the same question for WhatsApp sending through the variable
WA_CAMPAIGN_ENVIRONMENT. Translation security must not depend on a WhatsApp-specific setting, and that detector has two
properties that are wrong for this purpose:
  * with no WA_CAMPAIGN_ENVIRONMENT it can only say "unknown" for Staging, and
  * a platform name of "production" always wins - but Railway's DEFAULT environment is literally called "production", so a
    Staging service living in such an environment would be classified as Production even when set up correctly.
This module leaves that code untouched and adds a separate, explicit, fail-closed rule set:

  OFFRO_ENVIRONMENT   production | staging | development | test      (set explicitly on every service; missing/invalid = unknown)

  0. Value missing, but ALL THREE Railway project / environment / service IDs equal the known STAGING ones
                                                                -> staging   (inferred from exact IDs; never inferred for any other case)
  1. Missing (no staging-ID proof) or invalid value             -> unknown   (every translation write is refused)
  2. Value says production but the Railway project / environment / service IDs are the known STAGING ones
                                                                -> unknown   (misconfiguration; refuse)
  3. Value says production                                      -> production
  4. Value says a non-production environment:
       a. the Railway IDs prove this is the known Staging service -> that value is accepted (even if the platform environment is
          called "production")
       b. otherwise a platform name saying production (RAILWAY_ENVIRONMENT_NAME / RAILWAY_ENVIRONMENT) cannot be talked away
          -> production (conflict = fail closed)
       c. otherwise -> the stated value

classification() never raises. Nothing here reads a secret or writes anything.
"""
import hmac
import os

ENVIRONMENT_VAR = "OFFRO_ENVIRONMENT"
ENVIRONMENTS = ("production", "staging", "development", "test")
NON_PRODUCTION = ("staging", "development", "test")
_PLATFORM_ENV_VARS = ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT")
_PRODUCTION_WORDS = ("production", "prod")


def _env(name: str) -> str:
    return (os.getenv(name, "") or "").strip()


def _known_staging_ids() -> bool:
    """True when RAILWAY_PROJECT_ID / RAILWAY_ENVIRONMENT_ID / RAILWAY_SERVICE_ID are ALL present and equal the known Staging
    constants (the same constants the admin OTP staging test mode already trusts)."""
    try:
        from services import admin_otp_test_mode as _m
        pairs = (("RAILWAY_PROJECT_ID", _m.STAGING_PROJECT_ID),
                 ("RAILWAY_ENVIRONMENT_ID", _m.STAGING_ENVIRONMENT_ID),
                 ("RAILWAY_SERVICE_ID", _m.STAGING_SERVICE_ID))
        for var, expected in pairs:
            actual = _env(var)
            if not actual or not hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8")):
                return False
        return True
    except Exception:
        return False


def status() -> dict:
    """{classification, source, reason, conflict, platform_says_production, staging_ids_match}. Names only; no secrets."""
    explicit = _env(ENVIRONMENT_VAR).lower()
    platform_prod = [n for n in _PLATFORM_ENV_VARS if _env(n).lower() in _PRODUCTION_WORDS]
    out = {"classification": "unknown", "source": "none", "reason": "environment_not_set", "conflict": False,
           "platform_says_production": bool(platform_prod), "staging_ids_match": False}
    try:
        staging_ids = _known_staging_ids()
        out["staging_ids_match"] = staging_ids
        if not explicit:
            if staging_ids:
                # Safety net for a Staging service whose OFFRO_ENVIRONMENT was not set yet. Exact-ID proof only, and only ever
                # toward the NON-production side. Production is never inferred: it must be declared explicitly.
                out.update(classification="staging", source="staging_ids", reason="inferred_from_staging_ids")
            return out
        if explicit not in ENVIRONMENTS:
            out.update(source="explicit_invalid", reason="environment_value_invalid")
            return out
        if explicit == "production":
            if staging_ids:
                out.update(source="explicit", reason="production_declared_on_staging_service", conflict=True)
                return out
            out.update(classification="production", source="explicit", reason="ok")
            return out
        # a non-production value
        if staging_ids:
            out.update(classification=explicit, source="explicit+staging_ids", reason="ok")
        elif platform_prod:
            out.update(classification="production", source="platform_conflict", reason="platform_says_production", conflict=True)
        else:
            out.update(classification=explicit, source="explicit", reason="ok")
        return out
    except Exception:
        return {"classification": "unknown", "source": "error", "reason": "check_error", "conflict": False,
                "platform_says_production": False, "staging_ids_match": False}


def classification() -> str:
    return status()["classification"]


def is_production() -> bool:
    return classification() == "production"


def is_staging() -> bool:
    return classification() == "staging"


def is_non_production() -> bool:
    return classification() in NON_PRODUCTION
