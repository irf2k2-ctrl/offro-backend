"""One shared in-memory MongoDB for ALL product tests.

services/products.py (and the routers) bind to `database.db` at first import, so every
test file in a process must use the SAME fake `database` module. Installed here, before
any test module is imported."""
import os
import sys
import types

import mongomock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if not getattr(sys.modules.get("database"), "_offro_test_fake", False):
    _fake = mongomock.MongoClient()["offro_db"]
    _m = types.ModuleType("database")
    _m.db = _fake
    for _n in ["users", "merchants", "stores", "deals", "redemptions", "wallet", "withdraw_requests", "admins"]:
        setattr(_m, _n + "_collection", _fake[_n])
    _m.admin_collection = _fake["admins"]
    _m.client = None
    _m._offro_test_fake = True
    sys.modules["database"] = _m


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _explicit_test_environment(monkeypatch):
    """Tests run as an explicitly identified non-production environment (unknown environments now fail closed). Tests of the
    environment guard itself override or remove this."""
    monkeypatch.setenv("WA_CAMPAIGN_ENVIRONMENT", "staging")
    for n in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT", "APP_ENV", "ENVIRONMENT", "ENV"):
        monkeypatch.delenv(n, raising=False)


@pytest.fixture(autouse=True)
def _clean_db():
    """Every test starts from an empty database."""
    db = sys.modules["database"].db
    for name in db.list_collection_names():
        db.drop_collection(name)
    yield


@pytest.fixture(autouse=True)
def _legacy_sms_engine_for_existing_tests(monkeypatch):
    """Real admin sign-ins use the MSG91 widget in the app. The long-standing challenge-engine tests exercise the shared machinery through the
    old server-sent engine, so they select it here; the widget tests (test_security_admin_otp_widget.py) select the widget explicitly."""
    from services import admin_login_otp as _otp
    monkeypatch.setattr(_otp, "REAL_PROVIDER", "sms")
