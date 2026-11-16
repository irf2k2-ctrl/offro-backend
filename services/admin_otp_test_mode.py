"""services/admin_otp_test_mode.py - Staging-ONLY fixed test OTP for the admin login (no SMS), fail closed.

Why: Staging has no MSG91 configuration, but the full PIN -> OTP -> session flow must still be testable. For an allow-listed admin number on the
verified Staging service the challenge is created normally (PIN already verified) but its OTP is the fixed code below instead of a random one, and
no SMS is sent. The code is hashed with the normal salted, challenge- and user-bound hash, so /admin/auth/verify-otp is UNCHANGED and all its
protections (5 min expiry, 5 attempts, single use, resend cooldown/cap, rate limits, no cookie before success) still apply. A fixed code is public
knowledge, so on Staging the PIN is the only real factor: keep real Production PINs out of Staging, and keep the allow-list short.

Test mode is active ONLY when every check passes, evaluated on every call. Any missing value, mismatch or error means INACTIVE and the real MSG91
path is used. Production can never pass: its Railway project/environment/service IDs, public domain and database host are different.

  1. ADMIN_OTP_STAGING_TEST_MODE is on                               (the switch; never trusted alone)
  2. RAILWAY_PROJECT_ID / RAILWAY_ENVIRONMENT_ID / RAILWAY_SERVICE_ID equal the Staging constants below (Railway injects them; never define them)
  3. RAILWAY_PUBLIC_DOMAIN is exactly the Staging host
  4. every host in MONGO_URL equals the Staging MongoDB host
  5. the admin number is in ADMIN_OTP_TEST_MOBILES
The Railway environment NAME is deliberately not used: the Staging environment was once named "production".
"""
import hmac
import os

# Infrastructure identity of the Offro Staging backend. Not secrets. If the service is ever recreated, update these (test mode stays OFF meanwhile).
STAGING_PROJECT_ID = "0f65310d-0638-436f-aee7-2caf1666d409"
STAGING_ENVIRONMENT_ID = "ea3a5ab3-2aa8-4ae7-8ff4-b3b806957cf8"
STAGING_SERVICE_ID = "756296c3-d40d-4d5b-8483-c9c9ff922e87"
STAGING_PUBLIC_DOMAIN = "offro-backend-staging-d375.up.railway.app"
STAGING_MONGO_HOST = "mongodb.railway.internal"
PRODUCTION_DOMAINS = ("theoffro.com", "www.theoffro.com", "admin.theoffro.com", "offro-backend-production.up.railway.app")
TEST_OTP = "1234"

_IDS = (("RAILWAY_PROJECT_ID", STAGING_PROJECT_ID, "project_mismatch"),
        ("RAILWAY_ENVIRONMENT_ID", STAGING_ENVIRONMENT_ID, "environment_mismatch"),
        ("RAILWAY_SERVICE_ID", STAGING_SERVICE_ID, "service_mismatch"))


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _mongo_hosts(url: str) -> list:
    """Hostnames in a mongodb:// or mongodb+srv:// URL (credentials and ports stripped). Never logged."""
    rest = url.split("://", 1)[1] if "://" in url else url
    authority = rest.split("/", 1)[0].split("?", 1)[0]
    authority = authority.rsplit("@", 1)[-1]
    hosts = []
    for h in authority.split(","):
        h = h.strip().lower()
        if h.startswith("["):                                           # IPv6 literal
            h = h[1:].split("]", 1)[0]
        else:
            h = h.split(":", 1)[0]
        if h:
            hosts.append(h)
    return hosts


def status() -> tuple:
    """(active: bool, reason: str). The reason is a short code; it never contains an ID, host or number."""
    try:
        if _env("ADMIN_OTP_STAGING_TEST_MODE").lower() not in ("1", "true", "yes", "on"):
            return False, "flag_off"
        for runtime_var, expected, reason in _IDS:
            actual = _env(runtime_var)
            if not actual:
                return False, "not_on_railway"
            if not _same(expected, actual):
                return False, reason
        domain = _env("RAILWAY_PUBLIC_DOMAIN").lower()
        if not domain or domain in PRODUCTION_DOMAINS or domain.endswith("theoffro.com") or not _same(domain, STAGING_PUBLIC_DOMAIN):
            return False, "domain_mismatch"
        hosts = _mongo_hosts(_env("MONGO_URL"))
        if not hosts:
            return False, "database_unverified"
        if any(h != STAGING_MONGO_HOST for h in hosts):
            return False, "database_mismatch"
        if not allowed_digits():
            return False, "allow_list_empty"
        return True, "ok"
    except Exception:
        return False, "check_error"


def is_active() -> bool:
    return status()[0]


def _digits(mobile) -> str:
    d = "".join(c for c in str(mobile) if c.isdigit())
    return d[-10:] if len(d) >= 10 else d


def allowed_digits() -> set:
    return {x for x in (_digits(m) for m in _env("ADMIN_OTP_TEST_MOBILES").split(",")) if len(x) == 10}


def mobile_allowed(mobile) -> bool:
    d = _digits(mobile)
    return len(d) == 10 and d in allowed_digits()


def applies_to(mobile) -> bool:
    """True when this login must use the fixed test OTP: test mode verified AND this admin number is allow-listed."""
    return is_active() and mobile_allowed(mobile)
