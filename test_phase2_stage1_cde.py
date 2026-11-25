"""NOTE: file name sorts AFTER test_banner_media.py on purpose (it swaps the shared fake `database` at import).
Phase 2 / Stage 1: Task C (permanent security audit), Task D (Areas API) and Task E (notification targeting safety).
Run:  python -m pytest test_phase2_stage1_cde.py -q -W ignore"""
import json
import sys
import threading
import urllib.error

import pytest
from fastapi.testclient import TestClient

import server
from services import notification_audience as AUD
from services import security_audit as SA

fake = sys.modules["database"].db
c = TestClient(server.app)
VIEW = {"view": True}
NOTIF = {"Notifications": {"view": True, "add": True, "edit": True, "delete": True}}


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _user(tok, role, perms=None, country=None, cities=("*",)):
    rid = fake.dashboard_roles.insert_one({"role_name": role, "permissions": perms if perms is not None else {}}).inserted_id
    d = {"token": tok, "status": "active", "full_name": tok, "mobile": "9" + str(abs(hash(tok)))[:9], "role_id": rid,
         "assigned_cities": list(cities)}
    if country is not None:
        d["country"] = country
    fake.dashboard_users.insert_one(d)
    return _h(tok)


@pytest.fixture(autouse=True)
def _world():
    for col in ("dashboard_users", "dashboard_roles", "accounts", "users", "stores", "cities", "notifications", "security_audit",
                "activity_logs"):
        fake[col].delete_many({})
    yield


# ═════════════════════════════ Task C: permanent audit ═════════════════════════════
def test_audit_record_is_permanent_and_redacts_secrets():
    ok = SA.record("account.phone_change", {"id": "a1", "name": "Admin", "role": "Super Admin"}, "ok",
                   target={"type": "account", "id": "acc1", "name": "Asha", "country": "sa"},
                   details={"otp": "123456", "otp_verified": True, "fcm_token": "tok", "Authorization": "Bearer x",
                            "nested": {"password": "p", "count": 3}, "reason": "x" * 500})
    assert ok is True
    d = fake.security_audit.find_one({})
    assert d["action"] == "account.phone_change" and d["outcome"] == "ok" and d["admin"]["role"] == "Super Admin"
    assert d["target"] == {"type": "account", "id": "acc1", "name": "Asha", "country": "SA"}
    assert d["details"]["otp"] == "[redacted]" and d["details"]["fcm_token"] == "[redacted]" and d["details"]["Authorization"] == "[redacted]"
    assert d["details"]["nested"] == {"password": "[redacted]", "count": 3} and len(d["details"]["reason"]) == 200
    assert "123456" not in json.dumps(d, default=str) and "Bearer x" not in json.dumps(d, default=str)
    assert d["at"] is not None


def test_audit_has_no_expiry_and_activity_log_behaviour_is_unchanged():
    SA.ensure_indexes()
    info = fake.security_audit.index_information()
    assert not any("expireAfterSeconds" in v for v in info.values())
    import inspect
    from routers import dashboard_auth
    assert "days=15" in inspect.getsource(dashboard_auth.log_activity)           # existing behaviour untouched
    from datetime import datetime, timedelta
    fake.security_audit.insert_one({"at": datetime.utcnow() - timedelta(days=400), "action": "old", "outcome": "ok"})
    SA.record("new", {"id": "x"})
    from routers.dashboard_auth import log_activity
    class R:                                                                       # minimal request
        client = None; headers = {}
    log_activity(R(), "u", "n", "m", "Mod", "act")
    assert fake.security_audit.count_documents({}) == 2                            # activity-log purge never touches the audit


def test_audit_strict_failure_raises():
    class Boom:
        def insert_one(self, d):
            raise RuntimeError("down")
    orig = SA.db
    SA.db = {SA.AUDIT: Boom()}
    try:
        assert SA.record("x", {"id": "1"}) is False
        with pytest.raises(SA.AuditWriteError):
            SA.record("x", {"id": "1"}, strict=True)
    finally:
        SA.db = orig


def test_audit_module_and_api_offer_no_update_or_delete():
    assert not any(n in dir(SA) for n in ("update", "delete", "remove", "purge", "clear", "edit"))
    routes = [r for r in server.app.routes if getattr(r, "path", "") == "/admin/security-audit"]
    assert routes and all(r.methods <= {"GET", "HEAD"} for r in routes)
    h = _user("sup", "Super Admin")
    SA.record("a", {"id": "1"})
    _id = str(fake.security_audit.find_one({})["_id"])
    for m in (c.put, c.delete, c.patch, c.post):
        assert m("/admin/security-audit", headers=h).status_code == 405
        assert m("/admin/security-audit/" + _id, headers=h).status_code in (404, 405)
    assert fake.security_audit.count_documents({}) == 1


def test_audit_read_permissions_and_country_scope():
    SA.record("a1", {"id": "1"}, target={"type": "account", "id": "x", "country": "SA"})
    SA.record("a2", {"id": "1"}, target={"type": "account", "id": "y", "country": "IN"})
    SA.record("a3", {"id": "1"}, target={"type": "account", "id": "z"})
    assert c.get("/admin/security-audit").status_code in (401, 403)
    assert c.get("/admin/security-audit", headers=_user("n", "Nobody", {})).status_code == 403
    assert c.get("/admin/security-audit", headers=_user("acc", "Acc", {"Accounts": VIEW})).status_code == 403
    sup = c.get("/admin/security-audit", headers=_user("sup", "Super Admin")).json()["entries"]
    assert sorted(e["action"] for e in sup) == ["a1", "a2", "a3"]
    um = c.get("/admin/security-audit", headers=_user("um", "UM", {"User Management": VIEW})).json()["entries"]
    assert len(um) == 3
    sa = c.get("/admin/security-audit", headers=_user("um_sa", "UM", {"User Management": VIEW}, country="SA")).json()["entries"]
    assert [e["action"] for e in sa] == ["a1"]                                       # other countries and country-less entries hidden


# ═════════════════════════════ Task D: Areas API ═════════════════════════════
def _store(city, area, merchant_id=None):
    fake.stores.insert_one({"city": city, "area": area, "merchant_id": merchant_id})


def _acct(phone, **kw):
    return str(fake.accounts.insert_one({"phone": phone, "roles": ["merchant"], **kw}).inserted_id)


PUB = "/areas"
ADM = "/admin/areas"


@pytest.mark.parametrize("url", [PUB, ADM])
def test_areas_blank_city_returns_empty_list(url):
    h = _user("sup", "Super Admin")
    for q in ("", "   ", None):
        r = c.get(url, params={} if q is None else {"city": q}, headers=h)
        assert r.status_code == 200 and r.json()["areas"] == []


@pytest.mark.parametrize("url", [PUB, ADM])
def test_areas_regex_characters_do_not_crash(url):
    h = _user("sup", "Super Admin")
    _store("Pune", "Kothrud")
    for bad in ("Zzz(x", "[", "*", "a.*", "\\", "(?i)", "Pun)e"):
        r = c.get(url, params={"city": bad}, headers=h)
        assert r.status_code == 200 and r.json()["areas"] == []
    assert c.get(url, params={"city": "pune"}, headers=h).json()["areas"] == ["Kothrud"]       # partial text still finds Pune
    assert c.get(url, params={"city": "p.*"}, headers=h).json()["areas"] == []                  # "." is no wildcard any more


def test_areas_indian_behaviour_and_response_shape_preserved():
    h = _user("sup", "Super Admin")
    r = c.get(PUB, params={"city": "Ballari"}).json()
    assert r["city"] == "Ballari" and "Cowl Bazaar" in r["areas"] and set(r) == {"city", "areas"}
    assert c.get(PUB, params={"city": "BENGALURU"}).json()["areas"][0] == "Indiranagar"
    assert "Banjara Hills" in c.get(ADM, params={"city": "hyderabad"}, headers=h).json()["areas"]
    assert set(c.get(ADM, params={"city": "hyderabad"}, headers=h).json()) == {"areas"}
    assert c.get(PUB, params={"city": "Hubballi-Dharwad"}).json()["areas"]                      # legacy partial match kept ("hubli"/"dharwad" in key)
    assert c.get(PUB).json() == {"city": "", "areas": []}


def test_areas_identical_city_names_do_not_mix_countries():
    m_in = _acct("9876543210")
    m_sa = _acct("+966501234567", phone_country="SA")
    m_ph = _acct("+639171234567")
    _store("Springfield", "In-Area", m_in)
    _store("Springfield", "Sa-Area", m_sa)
    _store("Springfield", "Ph-Area", m_ph)
    sup = _user("sup", "Super Admin")
    assert c.get(PUB, params={"city": "Springfield", "country": "SA"}).json()["areas"] == ["Sa-Area"]
    assert c.get(PUB, params={"city": "Springfield", "country": "ph"}).json()["areas"] == ["Ph-Area"]
    assert c.get(PUB, params={"city": "Springfield", "country": "IN"}).json()["areas"] == ["In-Area"]
    assert c.get(PUB, params={"city": "Springfield", "country": "ZZ"}).json()["areas"] == []     # invalid country: never another country's
    assert c.get(PUB, params={"city": "Springfield", "country": "JO"}).json()["areas"] == []
    # older apps (no country): several countries share the name -> only the Indian stores' areas
    assert c.get(PUB, params={"city": "Springfield"}).json()["areas"] == ["In-Area"]
    assert c.get(ADM, params={"city": "Springfield", "country": "SA"}, headers=sup).json()["areas"] == ["Sa-Area"]
    # a non-Indian country never receives the predefined Indian list
    assert c.get(PUB, params={"city": "Ballari", "country": "SA"}).json()["areas"] == []
    assert c.get(PUB, params={"city": "Ballari", "country": "IN"}).json()["areas"]


def test_areas_unresolved_owner_is_not_counted_as_india():
    ghost = _acct("501234567")                       # unresolved country
    _store("Zedcity", "Ghost-Area", ghost)
    _store("Zedcity", "No-Owner-Area", None)
    assert c.get(PUB, params={"city": "Zedcity", "country": "IN"}).json()["areas"] == []
    assert c.get(PUB, params={"city": "Zedcity"}).json()["areas"] == ["Ghost-Area", "No-Owner-Area"]   # older apps: single (unresolved) group, as before


def test_areas_does_not_change_store_data():
    m = _acct("9876543210")
    _store("Pune", "Kothrud", m)
    before = list(fake.stores.find({}))
    c.get(PUB, params={"city": "Pune"}); c.get(PUB, params={"city": "(("})
    assert list(fake.stores.find({})) == before


def test_admin_areas_requires_authentication_and_permission():
    assert c.get(ADM, params={"city": "Ballari"}).status_code in (401, 403)
    assert c.get(ADM, params={"city": "Ballari"}, headers={"Authorization": "Bearer nope"}).status_code in (401, 403)
    assert c.get(ADM, params={"city": "Ballari"}, headers=_user("n", "Nobody", {})).status_code == 403
    assert c.get(ADM, params={"city": "Ballari"}, headers=_user("pay", "Fin", {"Payments": VIEW})).status_code == 403
    for tok, perms in (("st", {"Stores": VIEW}), ("ac", {"Accounts": VIEW}), ("ci", {"Cities": VIEW})):
        assert c.get(ADM, params={"city": "Ballari"}, headers=_user(tok, "R" + tok, perms)).status_code == 200
    assert c.get(ADM, params={"city": "Ballari"}, headers=_user("sup", "Super Admin")).status_code == 200


def test_admin_areas_country_restricted_user_only_gets_own_country():
    m_sa = _acct("+966501234567", phone_country="SA")
    m_in = _acct("9876543210")
    _store("Springfield", "Sa-Area", m_sa); _store("Springfield", "In-Area", m_in)
    sa = _user("sa_u", "R", {"Stores": VIEW}, country="SA")
    assert c.get(ADM, params={"city": "Springfield"}, headers=sa).json()["areas"] == ["Sa-Area"]
    assert c.get(ADM, params={"city": "Springfield", "country": "IN"}, headers=sa).json()["areas"] == []
    assert c.get(ADM, params={"city": "Ballari"}, headers=sa).json()["areas"] == []             # Indian predefined list is not for SA
    ind = _user("in_u", "R", {"Stores": VIEW}, country="IN")
    assert c.get(ADM, params={"city": "Ballari"}, headers=ind).json()["areas"]


# ═════════════════════════════ Task E: notification targeting ═════════════════════════════
@pytest.fixture
def fcm(monkeypatch):
    """Fake FCM: a real RSA key so the existing JWT code runs, urlopen replaced, every sent token recorded."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT_JSON", json.dumps({"client_email": "x@y.z", "private_key": pem, "project_id": "p"}))
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "p")
    state = {"tokens": [], "fail": set(), "lock": threading.Lock()}

    class Resp:
        def __init__(self, body): self._b = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self._b).encode()

    def fake_urlopen(req, timeout=0):
        if "oauth2" in req.full_url:
            return Resp({"access_token": "at"})
        tok = json.loads(req.data)["message"]["token"]
        if tok in state["fail"]:
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)
        with state["lock"]:
            state["tokens"].append(tok)
        return Resp({"name": "projects/p/messages/1"})

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(AUD, "INLINE_MAX", 10 ** 9)          # synchronous unless a test overrides
    return state


def _city(name, country, **kw):
    fake.cities.insert_one({"name": name, "slug": name.lower(), "country": country, "status": "active", **kw})


def _cust(tok, phone, city, roles=("user",), **kw):
    d = {"phone": phone, "city": city, "roles": list(roles), **kw}
    if tok is not None:
        d["fcm_token"] = tok
    fake.accounts.insert_one(d)


def _send(h, **body):
    return c.post("/admin/notifications/send", json={"title": "T", "body": "B", **body}, headers=h)


NH = lambda tok="sup": _user(tok, "Super Admin")


def test_city_target_reaches_only_that_city_and_customers(fcm):
    _city("Ballari", "IN")
    _cust("t_in_ok", "9876543210", "Ballari")
    _cust("t_in_case", "9876543211", " ballari ")
    _cust("t_in_other_city", "9876543212", "Hubli")
    _cust("t_in_merchant", "9876543213", "Ballari", roles=("merchant",))
    _cust("t_in_both", "9876543214", "Ballari", roles=("user", "merchant"))
    _cust("t_in_influencer", "9876543215", "Ballari", roles=("influencer",))
    _cust("t_legacy_noroles", "9876543216", "Ballari", roles=())
    fake.accounts.update_one({"phone": "9876543216"}, {"$unset": {"roles": ""}})
    _cust(None, "9876543217", "Ballari")                                   # no token
    _cust("t_in_ok", "9876543218", "Ballari")                              # duplicate token
    r = _send(NH(), target="city", target_city="Ballari")
    assert r.status_code == 200, r.text
    j = r.json()
    assert sorted(fcm["tokens"]) == ["t_in_both", "t_in_case", "t_in_ok", "t_legacy_noroles"]
    assert (j["eligible_records"], j["sent_count"], j["failed_count"], j["skipped_no_token"], j["skipped_duplicate"]) == (6, 4, 0, 1, 1)
    assert j["skipped_count"] == 2 and j["unique_tokens"] == 4 and j["country"] == "IN" and j["status"] == "sent"


def test_identical_city_names_in_different_countries_do_not_mix(fcm):
    _city("Springfield", "IN"); _city("Springfield", "SA")
    _cust("t_in", "9876543210", "Springfield")
    _cust("t_sa", "+966501234567", "Springfield", phone_country="SA")
    _cust("t_ghost", "501234567", "Springfield")                            # country cannot be resolved -> never reached
    r = _send(NH(), target="city", target_city="Springfield")
    assert r.status_code == 400 and "Choose the country" in r.json()["detail"] and fcm["tokens"] == []
    assert _send(NH(), target="city", target_city="Springfield", target_country="SA").status_code == 200
    assert fcm["tokens"] == ["t_sa"]
    fcm["tokens"].clear()
    assert _send(NH(), target="city", target_city="springfield", target_country="in").status_code == 200
    assert fcm["tokens"] == ["t_in"]
    assert _send(NH(), target="city", target_city="Springfield", target_country="ZZ").status_code == 400


def test_city_without_known_country_is_refused_not_defaulted_to_india(fcm):
    _cust("t1", "9876543210", "Nowhere")
    r = _send(NH(), target="city", target_city="Nowhere")
    assert r.status_code == 400 and "cannot be determined" in r.json()["detail"] and fcm["tokens"] == []
    assert _send(NH(), target="city", target_city="Nowhere", target_country="IN").status_code == 200 and fcm["tokens"] == ["t1"]
    assert _send(NH(), target="city", target_city="").status_code == 400
    assert _send(NH(), target="city").status_code == 400


def test_city_aliases_from_city_master_match(fcm):
    _city("Bengaluru", "IN", names={"en": "Bengaluru", "kn": "ಬೆಂಗಳೂರು"}, slug="bangalore")
    _cust("t1", "9876543210", "Bangalore"); _cust("t2", "9876543211", "Bengaluru"); _cust("t3", "9876543212", "ಬೆಂಗಳೂರು")
    assert _send(NH(), target="city", target_city="Bengaluru").status_code == 200
    assert sorted(fcm["tokens"]) == ["t1", "t2", "t3"]


def test_offers_target_is_rejected_and_nothing_is_sent(fcm):
    _cust("t1", "9876543210", "Ballari")
    r = _send(NH(), target="offers")
    assert r.status_code == 400 and "disabled" in r.json()["detail"] and fcm["tokens"] == []
    assert fake.notifications.count_documents({}) == 0


def test_no_3000_recipient_cap_and_counts_are_exact(fcm):
    fake.accounts.insert_many([{"phone": "9%09d" % i, "city": "Big", "roles": ["user"], "fcm_token": "tok%05d" % i} for i in range(3500)])
    fake.accounts.insert_many([{"phone": "8%09d" % i, "city": "Big", "roles": ["user"]} for i in range(25)])          # no token
    fake.users.insert_many([{"phone": "7%09d" % i, "fcm_token": "utok%04d" % i} for i in range(700)])                 # legacy users
    fcm["fail"] = {"tok00007", "tok03499", "utok0003"}
    r = _send(NH(), target="all_users")
    assert r.status_code == 200
    j = r.json()
    assert j["eligible_records"] == 3500 + 25 + 700 and j["unique_tokens"] == 4200 and j["skipped_no_token"] == 25
    assert j["sent_count"] == 4197 and j["failed_count"] == 3 and j["status"] == "partial"
    assert len(fcm["tokens"]) == 4197 and len(set(fcm["tokens"])) == 4197
    doc = fake.notifications.find_one({})
    assert doc["sent_count"] == 4197 and doc["failed_count"] == 3 and doc["eligible_records"] == 4225


def test_large_audience_runs_in_background_with_final_counts(fcm, monkeypatch):
    monkeypatch.setattr(AUD, "INLINE_MAX", 100)
    fake.accounts.insert_many([{"phone": "9%09d" % i, "roles": ["user"], "fcm_token": "tk%04d" % i} for i in range(400)])

    class Inline(threading.Thread):
        def start(self):                                  # only the notification job runs inline; pool workers stay real threads
            self.run() if self.name.startswith("notif-send-") else super().start()
    monkeypatch.setattr(threading, "Thread", Inline)
    r = _send(NH(), target="all_users")
    j = r.json()
    assert r.status_code == 200 and j["status"] == "sending" and j["unique_tokens"] == 400 and j["eligible_records"] == 400
    doc = fake.notifications.find_one({})
    assert doc["status"] == "sent" and doc["sent_count"] == 400 and doc["failed_count"] == 0
    assert fake.security_audit.find_one({"action": "notification.send"})["details"]["sent"] == 400


def test_duplicate_tokens_are_sent_once(fcm):
    fake.accounts.insert_many([{"phone": "9%09d" % i, "roles": ["user"], "fcm_token": "same"} for i in range(5)])
    fake.users.insert_one({"phone": "9111111111", "fcm_token": "same"})
    j = _send(NH(), target="all_users").json()
    assert fcm["tokens"] == ["same"] and j["skipped_duplicate"] == 5 and j["sent_count"] == 1


def test_country_restricted_admin_city_and_broadcast(fcm):
    _city("Riyadh", "SA"); _city("Springfield", "SA"); _city("Springfield", "IN")
    _cust("t_sa", "+966501234567", "Riyadh", phone_country="SA")
    _cust("t_sa_spring", "+966501234568", "Springfield", phone_country="SA")
    _cust("t_in_spring", "9876543210", "Springfield")
    sa = _user("sa_admin", "Marketing", NOTIF, country="SA")
    assert _send(sa, target="city", target_city="Riyadh").status_code == 200 and fcm["tokens"] == ["t_sa"]
    fcm["tokens"].clear()
    assert _send(sa, target="city", target_city="Springfield").status_code == 400         # still two countries: must choose
    assert _send(sa, target="city", target_city="Springfield", target_country="IN").status_code == 403
    assert _send(sa, target="city", target_city="Springfield", target_country="SA").status_code == 200
    assert fcm["tokens"] == ["t_sa_spring"]
    fcm["tokens"].clear()
    assert _send(sa, target="all_users").status_code == 200
    assert sorted(fcm["tokens"]) == ["t_sa", "t_sa_spring"]                                # never the Indian account
    assert _send(sa, target="offers").status_code == 403
    sp = _send(sa, target="specific", target_phone="9876543210")
    assert sp.status_code == 404


def test_assigned_city_restriction_is_respected(fcm):
    _city("Ballari", "IN"); _city("Hubli", "IN")
    _cust("t_b", "9876543210", "Ballari"); _cust("t_h", "9876543211", "Hubli")
    fake.accounts.insert_one({"phone": "9876543212", "city": "Pune", "roles": ["user"], "fcm_token": "t_p"})
    adm = _user("city_adm", "CityMarketing", NOTIF, cities=("Ballari",))
    assert _send(adm, target="city", target_city="Hubli").status_code == 403
    assert _send(adm, target="city", target_city="ballari").status_code == 200 and fcm["tokens"] == ["t_b"]
    fcm["tokens"].clear()
    assert _send(adm, target="all_users").status_code == 200 and fcm["tokens"] == ["t_b"]   # all_users narrowed to the assigned city
    none = _user("no_city", "NoCity", NOTIF, cities=())
    j = _send(none, target="all_users").json()
    assert j["eligible_records"] == 0 and j["sent_count"] == 0
    assert _send(adm, target="specific", target_phone="9876543211").status_code == 404      # Hubli user: outside the assigned city


def test_permissions_unchanged_and_dry_run(fcm):
    _city("Ballari", "IN")
    _cust("t1", "9876543210", "Ballari")
    assert _send(_user("none", "N", {}), target="all_users").status_code == 403
    assert _send(_user("viewonly", "V", {"Notifications": VIEW}), target="all_users").status_code == 403
    assert c.post("/admin/notifications/send", json={"title": "T", "body": "B"}).status_code in (401, 403)
    j = _send(NH(), target="city", target_city="Ballari", dry_run=True).json()
    assert j["dry_run"] is True and j["eligible_records"] == 1 and j["unique_tokens"] == 1 and "tokens" not in j
    assert fcm["tokens"] == [] and fake.notifications.count_documents({}) == 0 and fake.security_audit.count_documents({}) == 0


def test_history_and_audit_do_not_expose_tokens_or_full_phones(fcm):
    _city("Ballari", "IN")
    _cust("SECRET_TOKEN_VALUE_1", "9876543210", "Ballari")
    _send(NH(), target="city", target_city="Ballari")
    _send(NH(), target="specific", target_phone="9876543210")
    sup = c.get("/admin/notifications", headers=NH("sup2")).json()
    assert any(n["target_phone"] == "9876543210" for n in sup)                               # Super Admin still sees it
    nh = _user("notif_viewer", "NV", NOTIF, cities=("Ballari",))
    rows = c.get("/admin/notifications", headers=nh).json()
    blob = json.dumps(rows, default=str, ensure_ascii=False)
    assert "9876543210" not in blob and "SECRET_TOKEN_VALUE" not in blob and "••" in blob
    audit = json.dumps(list(fake.security_audit.find({})), default=str)
    assert "SECRET_TOKEN_VALUE" not in audit and "9876543210" not in audit
    assert fake.security_audit.find_one({"action": "notification.send"})["details"]["eligible_records"] == 1
    assert "SECRET_TOKEN_VALUE" not in json.dumps(list(fake.notifications.find({})), default=str)


def test_history_is_country_scoped_for_restricted_admin(fcm):
    _city("Riyadh", "SA"); _city("Ballari", "IN")
    _cust("t_sa", "+966501234567", "Riyadh", phone_country="SA"); _cust("t_in", "9876543210", "Ballari")
    _send(NH(), target="city", target_city="Riyadh")
    _send(NH(), target="city", target_city="Ballari")
    sa = _user("sa_hist", "NV", NOTIF, country="SA")
    rows = c.get("/admin/notifications", headers=sa).json()
    assert [r["target_city"] for r in rows] == ["Riyadh"] and rows[0]["country"] == "SA"


def test_specific_target_still_works(fcm):
    _cust("t_one", "9876543210", "Ballari")
    j = _send(NH(), target="specific", target_phone="9876543210").json()
    assert j["status"] == "sent" and j["sent_count"] == 1 and fcm["tokens"] == ["t_one"]
