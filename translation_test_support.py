"""Shared helpers for the Phase G translation tests (not a test module)."""
import os
import sys
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import translations_admin as TA
from routers import translations_public as TP
from services import translations as T
from tools.sync_translation_keys import parse_arb

DB = sys.modules["database"].db
FIXTURE_ARB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_data", "app_en_fixture.arb")

app = FastAPI()
app.include_router(TA.router, prefix="/admin")
app.include_router(TP.router)
client = TestClient(app)

ALL = ["view", "add", "edit", "delete"]


def make_user(token, *, role_name="Custom Role", perms=None, system=False, status="active", name="Asha Admin"):
    rid = DB.dashboard_roles.insert_one({"role_name": role_name, "permissions": perms or {}, "is_system_role": system}).inserted_id
    DB.dashboard_users.insert_one({"token": token, "role_id": rid, "full_name": name, "status": status,
                                   "last_active_at": datetime.utcnow(), "mobile": "9000000001"})
    return {"Authorization": "Bearer " + token}


def grant(*actions):
    return {TA.MODULE: {a: True for a in ALL if a in actions}}


def super_admin(token="sa-token"):
    return make_user(token, role_name="Super Admin", system=True, name="Super Admin User")


def editor(token="ed-token"):
    return make_user(token, perms=grant("view", "edit"), name="Editor User")


def viewer(token="vw-token"):
    return make_user(token, perms=grant("view"), name="Viewer User")


def fake_super(token="fake-token"):
    """A role merely RENAMED 'Super Admin' (is_system_role not true)."""
    return make_user(token, role_name="Super Admin", system=False, perms={}, name="Renamed Role")


def setup_env(monkeypatch, env="staging"):
    """Declare which environment this server is (None = not set). Staging and production behave identically."""
    for n in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID",
              "TRANSLATION_PUBLIC_RATE_PER_MIN"):
        monkeypatch.delenv(n, raising=False)
    if env:
        monkeypatch.setenv("OFFRO_ENVIRONMENT", env)
    else:
        monkeypatch.delenv("OFFRO_ENVIRONMENT", raising=False)
    TP.reset_rate_limit()
    T.ensure_indexes()


def sync_fixture_keys():
    return T.sync_keys(parse_arb(FIXTURE_ARB))


def wipe_translation_data():
    """Simulate a different environment's (empty) database for the translation collections."""
    for c in (T.LANGS, T.KEYS, T.VALUES, T.VERSIONS, T.STATE, "translation_audit"):
        DB.drop_collection(c)
    T.ensure_indexes()


def add_ar(headers, code="ar"):
    return client.post("/admin/translations/languages", headers=headers,
                       json={"code": code, "name_en": "Arabic", "name_native": "العربية", "direction": "rtl"})


def put(headers, key, value, lang="ar"):
    return client.put(f"/admin/translations/strings/{lang}/{key}", headers=headers, json={"value": value})


def publish(headers, lang="ar", track="app", note="release"):
    return client.post("/admin/translations/publish", headers=headers, json={"lang": lang, "track": track, "note": note})


def audit_actions(outcome=None):
    q = {} if outcome is None else {"outcome": outcome}
    return [d["action"] for d in DB.translation_audit.find(q)]
