"""Stage 4 Part A: country access for Dashboard users, enforced by the backend APIs.
(file name sorts after test_banner_media.py on purpose: tests that import `server` must be collected after it.)
Run:  python -m pytest test_dashboard_country_access.py -q -W ignore"""
import sys
from datetime import datetime, timedelta

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

import server
from services import dashboard_country as DC
from utils import countries as C

DB = sys.modules["database"].db
web = TestClient(server.app)
ALL_PERMS = {m: {a: True for a in ("view", "add", "edit", "delete", "approve", "export")}
             for m in ("Accounts", "Stores", "Users", "Merchants", "Payments", "Influencers", "Banners", "Reviews", "Reports",
                       "Notifications", "Deals", "Products", "User Management", "Live Chat", "Settings", "WhatsApp Campaigns")}
FUT = (datetime.utcnow() + timedelta(days=20)).strftime("%Y-%m-%d")


def mk_user(token, role="Manager", country=None, cities=("*",), perms=None, mobile=None):
    rid = DB.dashboard_roles.find_one({"role_name": role})
    rid = rid["_id"] if rid else DB.dashboard_roles.insert_one(
        {"role_name": role, "permissions": perms if perms is not None else ALL_PERMS, "is_system_role": role == "Super Admin"}).inserted_id
    doc = {"token": token, "status": "active", "full_name": token, "mobile": mobile or ("9" + str(abs(hash(token)))[:9].ljust(9, "0")),
           "role_id": rid, "assigned_cities": list(cities), "last_active_at": datetime.utcnow(), "created_at": datetime.utcnow()}
    if country is not None:
        doc["country"] = country
    return DB.dashboard_users.insert_one(doc).inserted_id


def H(token):
    return {"Authorization": "Bearer " + token}


def _chat_uses_the_shared_test_db(monkeypatch):
    """routers/wa_chat.py binds `db` when it is first imported. A plain `pytest` run imports it (via routers/wa_template_test.py) BEFORE
    test_banner_media.py replaces the shared `database` module, so it would otherwise read a different in-memory database than the one
    these tests fill. Point it at the shared one (production has a single database; this is test isolation only)."""
    import routers.wa_chat as WC
    monkeypatch.setattr(WC, "db", DB)


@pytest.fixture
def world():
    """India merchant (legacy bare phone, NO phone_country), Saudi merchant (phone_country), Nepal merchant (+977 without
    phone_country), a merchant whose phone says nothing. Each owns a store, subscription, invoice, product, deal, banner."""
    w = {}
    specs = {"in": ("Ravi", "9876543210", {}, "Ballari"), "sa": ("Sami", "+966501234567", {"phone_country": "SA"}, "Riyadh"),
             "np": ("Nima", "+9779812345678", {}, "Kathmandu"), "x": ("Nobody", "", {}, "Ballari")}
    for k, (name, phone, extra, city) in specs.items():
        a = DB.accounts.insert_one({"name": name, "phone": phone, "roles": ["merchant"], "status": "active", "city": city, **extra}).inserted_id
        mid = str(a)
        s = DB.stores.insert_one({"store_name": name + " Store", "city": city, "status": "active", "merchant_id": mid,
                                  "qr_code": "q", "created_at": datetime.utcnow()}).inserted_id
        sub = DB.subscriptions.insert_one({"merchant_id": mid, "store_id": str(s), "plan": "1month", "status": "paid", "total": 10,
                                           "city": city, "created_at": datetime.utcnow()}).inserted_id
        inv = DB.invoices.insert_one({"merchant_id": mid, "invoice_no": "INV-" + k, "store_name": name, "total": 10, "city": city,
                                      "created_at": datetime.utcnow()}).inserted_id
        prod = DB.merchant_products.insert_one({"title": name + " prod", "merchant_id": mid, "store_id": str(s), "status": "approved",
                                                "city": city, "end_date": datetime.utcnow() + timedelta(days=10),
                                                "created_at": datetime.utcnow()}).inserted_id
        deal = DB.deals.insert_one({"title": name + " deal", "store_id": str(s), "merchant_id": mid, "status": "active",
                                    "created_at": datetime.utcnow()}).inserted_id
        ban = DB.merchant_banners.insert_one({"title": name + " banner", "merchant_id": mid, "merchant_phone": phone, "city": city,
                                              "approval_status": "pending_approval", "created_at": datetime.utcnow()}).inserted_id
        rev = DB.reviews.insert_one({"store_id": str(s), "user_name": "u", "rating": 4, "text": "t", "created_at": datetime.utcnow()}).inserted_id
        wd = DB.withdraw_requests.insert_one({"user_id": mid, "phone": phone, "points": 200, "status": "pending", "city": city,
                                              "created_at": datetime.utcnow()}).inserted_id
        inf = DB.influencers.insert_one({"name": name + " Inf", "city": city, "phone": phone, "account_id": mid, "status": "active",
                                         "created_at": datetime.utcnow()}).inserted_id
        w[k] = dict(acct=mid, store=str(s), sub=str(sub), inv=str(inv), prod=str(prod), deal=str(deal), ban=str(ban), rev=str(rev),
                    wd=str(wd), inf=str(inf), city=city, name=name)
    mk_user("super", "Super Admin")
    mk_user("in-mgr", "Country Manager", country="IN")
    mk_user("sa-mgr", "Country Manager", country="SA")
    mk_user("legacy", "Country Manager")                                   # an existing user: no country field at all
    return w


# ═══════════════ the helper itself ═══════════════
PHONES = ["9876543210", "919876543210", "+919876543210", "09876543210", "0091 9876543210", "+966501234567", "00966501234567",
          "966501234567", "+9779812345678", "+962791234567", "+639171234567", "+14155552671", "", None, "abc", "12345"]


@pytest.mark.parametrize("pc", [None, "", "IN", "SA", "in", "NP", "XX"])
def test_mongo_account_filter_never_grants_more_than_account_country(pc):
    """account_query (Mongo side) must be a SUBSET of utils.countries.account_country for every phone shape."""
    ids = {}
    for i, ph in enumerate(PHONES):
        d = {"name": f"a{i}", "phone": ph}
        if pc is not None:
            d["phone_country"] = pc
        ids[DB.accounts.insert_one(d).inserted_id] = d
    for iso in sorted(C.supported_iso_codes()):
        got = {d["_id"] for d in DB.accounts.find(DC.account_query(iso))}
        for _id in got:
            acct = DB.accounts.find_one({"_id": _id})
            info = C.account_country(acct)
            assert info and info["iso"] == iso, (acct, iso)
    DB.accounts.delete_many({})


def test_mongo_account_filter_finds_the_expected_accounts():
    for ph, pc in [("9876543210", None), ("+919876543210", None), ("+966501234567", None), ("+966501234567", "SA"), ("+9779812345678", None),
                   ("+639171234567", None), ("+962791234567", None), ("", None)]:
        DB.accounts.insert_one({"phone": ph, **({"phone_country": pc} if pc else {})})
    n = lambda iso: DB.accounts.count_documents(DC.account_query(iso))
    assert (n("IN"), n("SA"), n("NP"), n("PH"), n("JO")) == (2, 2, 1, 1, 1)


def test_scope_rules():
    assert DC.scope({"role_name": "Super Admin", "country": "SA"}) == ""             # Super Admin is never restricted
    assert DC.scope({"role": {"role_name": "Super Admin"}, "country": "SA"}) == ""
    assert DC.scope({"role_name": "Manager", "country": "sa"}) == "SA"
    assert DC.scope({"role_name": "Manager"}) == "" and DC.scope({"role_name": "Manager", "country": ""}) == ""
    assert DC.scope({"role_name": "Manager", "country": "ZZ"}) == DC.INVALID_SCOPE   # not in the master -> FAIL CLOSED, never unrestricted
    assert DC.scope(None) == ""


def test_scope_resolves_the_role_of_a_raw_dashboard_user_document():
    rid = DB.dashboard_roles.insert_one({"role_name": "Super Admin", "permissions": {}}).inserted_id
    assert DC.scope({"role_id": rid, "country": "SA"}) == ""
    rid2 = DB.dashboard_roles.insert_one({"role_name": "Other", "permissions": {}}).inserted_id
    assert DC.scope({"role_id": rid2, "country": "SA"}) == "SA"


def test_clean_country_validates_against_the_country_master():
    from fastapi import HTTPException
    assert DC.clean_country(None) == "" and DC.clean_country("") == "" and DC.clean_country(" sa ") == "SA"
    for bad in ("ZZ", "India", "966", 5, ["SA"]):
        with pytest.raises(HTTPException) as e:
            DC.clean_country(bad)
        assert e.value.status_code == 422


# ═══════════════ Country field on dashboard users ═══════════════
class TestUserCountryField:
    def create(self, token="super", **extra):
        body = {"full_name": "New Person", "mobile": "9111122222", "pin": "1234", "role_id": str(DB.dashboard_roles.find_one({"role_name": "Country Manager"})["_id"])}
        body.update(extra)
        return web.post("/admin/auth/users", headers=H(token), json=body)

    def test_create_with_country_persists(self, world):
        r = self.create(country="SA")
        assert r.status_code == 200, r.text
        assert DB.dashboard_users.find_one({"mobile": "9111122222"})["country"] == "SA"

    def test_country_is_normalised_to_upper_case(self, world):
        assert self.create(country="np").status_code == 200
        assert DB.dashboard_users.find_one({"mobile": "9111122222"})["country"] == "NP"

    def test_create_without_country_means_no_restriction(self, world):
        assert self.create().status_code == 200
        assert DB.dashboard_users.find_one({"mobile": "9111122222"})["country"] == ""

    @pytest.mark.parametrize("bad", ["ZZ", "India", "966", 7, ["SA"]])
    def test_invalid_country_is_rejected_and_nothing_is_stored(self, world, bad):
        r = self.create(country=bad)
        assert r.status_code == 422, r.text
        assert DB.dashboard_users.find_one({"mobile": "9111122222"}) is None

    def test_edit_country_persists_and_can_be_cleared(self, world):
        uid = str(DB.dashboard_users.find_one({"token": "legacy"})["_id"])
        assert web.put(f"/admin/auth/users/{uid}", headers=H("super"), json={"country": "JO"}).status_code == 200
        assert DB.dashboard_users.find_one({"token": "legacy"})["country"] == "JO"
        assert web.put(f"/admin/auth/users/{uid}", headers=H("super"), json={"country": ""}).status_code == 200
        assert DB.dashboard_users.find_one({"token": "legacy"})["country"] == ""

    def test_edit_with_invalid_country_changes_nothing(self, world):
        uid = str(DB.dashboard_users.find_one({"token": "legacy"})["_id"])
        assert web.put(f"/admin/auth/users/{uid}", headers=H("super"), json={"country": "ZZ", "designation": "X"}).status_code == 422
        u = DB.dashboard_users.find_one({"token": "legacy"})
        assert "country" not in u and not u.get("designation")

    def test_edit_without_country_key_leaves_it_alone(self, world):
        uid = str(DB.dashboard_users.find_one({"token": "in-mgr"})["_id"])
        assert web.put(f"/admin/auth/users/{uid}", headers=H("super"), json={"designation": "Lead"}).status_code == 200
        assert DB.dashboard_users.find_one({"token": "in-mgr"})["country"] == "IN"

    def test_list_and_me_expose_the_country(self, world):
        rows = {u["full_name"]: u for u in web.get("/admin/auth/users", headers=H("super")).json()}
        assert rows["in-mgr"]["country"] == "IN" and rows["in-mgr"]["country_name"] == "India"
        assert rows["legacy"]["country"] == "" and rows["legacy"]["country_name"] == ""      # legacy user serialises cleanly
        assert web.get("/admin/auth/me", headers=H("sa-mgr")).json()["country"] == "SA"
        assert web.get("/admin/auth/me", headers=H("legacy")).json()["country"] == ""

    def test_country_selector_list_is_the_country_master(self, world):
        got = web.get("/admin/auth/countries", headers=H("legacy")).json()
        assert got == C.supported_countries()
        assert web.get("/admin/auth/countries").status_code == 401

    def test_existing_city_assignment_is_stored_independently(self, world):
        r = self.create(country="IN", assigned_cities=["Ballari"])
        assert r.status_code == 200
        u = DB.dashboard_users.find_one({"mobile": "9111122222"})
        assert u["assigned_cities"] == ["Ballari"] and u["country"] == "IN"

    def test_permission_to_manage_users_is_unchanged(self, world):
        mk_user("noperm", "NoPerm", perms={})
        assert self.create(token="noperm", country="IN").status_code == 403


# ═══════════════ who may change what ═══════════════
class TestCountryAssignmentRules:
    def uid(self, token):
        return str(DB.dashboard_users.find_one({"token": token})["_id"])

    def test_nobody_changes_their_own_country(self, world):
        r = web.put(f"/admin/auth/users/{self.uid('in-mgr')}", headers=H("in-mgr"), json={"country": "SA"})
        assert r.status_code == 403
        r = web.put(f"/admin/auth/users/{self.uid('in-mgr')}", headers=H("in-mgr"), json={"country": ""})            # cannot lift the restriction
        assert r.status_code == 403
        assert DB.dashboard_users.find_one({"token": "in-mgr"})["country"] == "IN"

    def test_an_unrestricted_non_super_user_cannot_restrict_or_free_itself_either(self, world):
        assert web.put(f"/admin/auth/users/{self.uid('legacy')}", headers=H("legacy"), json={"country": "SA"}).status_code == 403
        assert "country" not in DB.dashboard_users.find_one({"token": "legacy"})

    def test_saving_ones_own_unchanged_country_is_allowed(self, world):
        r = web.put(f"/admin/auth/users/{self.uid('in-mgr')}", headers=H("in-mgr"), json={"country": "IN", "designation": "Lead"})
        assert r.status_code == 200

    def test_super_admin_may_change_any_country_including_its_own_and_stays_unrestricted(self, world):
        assert web.put(f"/admin/auth/users/{self.uid('super')}", headers=H("super"), json={"country": "SA"}).status_code == 200
        assert DB.dashboard_users.find_one({"token": "super"})["country"] == "SA"
        assert len(web.get("/admin/accounts", headers=H("super")).json()) == 4            # Super Admin ignores it
        assert len(web.get("/admin/stores", headers=H("super")).json()) == 4

    def test_restricted_user_cannot_move_another_user_to_a_different_country_or_unrestrict_it(self, world):
        mk_user("in-2", "Country Manager", country="IN")
        for body in ({"country": "SA"}, {"country": ""}):
            assert web.put(f"/admin/auth/users/{self.uid('in-2')}", headers=H("in-mgr"), json=body).status_code == 403
        assert DB.dashboard_users.find_one({"token": "in-2"})["country"] == "IN"

    def test_restricted_user_creates_users_only_in_its_own_country(self, world):
        rid = str(DB.dashboard_roles.find_one({"role_name": "Country Manager"})["_id"])
        base = {"full_name": "N", "mobile": "9123456789", "pin": "1234", "role_id": rid}
        assert web.post("/admin/auth/users", headers=H("in-mgr"), json={**base, "country": "SA"}).status_code == 403
        r = web.post("/admin/auth/users", headers=H("in-mgr"), json=base)                 # omitted -> forced to its own country
        assert r.status_code == 200
        assert DB.dashboard_users.find_one({"mobile": "9123456789"})["country"] == "IN"

    def test_restricted_user_cannot_create_or_assign_super_admin(self, world):
        sar = str(DB.dashboard_roles.find_one({"role_name": "Super Admin"})["_id"])
        r = web.post("/admin/auth/users", headers=H("in-mgr"), json={"full_name": "N", "mobile": "9123456780", "pin": "1234", "role_id": sar})
        assert r.status_code == 403
        mk_user("in-2", "Country Manager", country="IN")
        assert web.put(f"/admin/auth/users/{self.uid('in-2')}", headers=H("in-mgr"), json={"role_id": sar}).status_code == 403

    def test_restricted_user_sees_and_manages_only_users_of_its_own_country(self, world):
        mk_user("in-2", "Country Manager", country="IN")
        names = {u["full_name"] for u in web.get("/admin/auth/users", headers=H("in-mgr")).json()}
        assert names == {"in-mgr", "in-2"}                                                # not super / sa-mgr / legacy
        for tok in ("sa-mgr", "legacy", "super"):
            u = self.uid(tok)
            assert web.put(f"/admin/auth/users/{u}", headers=H("in-mgr"), json={"designation": "x"}).status_code == 404
            assert web.put(f"/admin/auth/users/{u}/reset-pin", headers=H("in-mgr"), json={"new_pin": "4321"}).status_code == 404
            assert web.delete(f"/admin/auth/users/{u}", headers=H("in-mgr")).status_code == 404
        assert web.put(f"/admin/auth/users/{self.uid('in-2')}", headers=H("in-mgr"), json={"designation": "ok"}).status_code == 200

    def test_activity_logs_are_limited_to_the_country(self, world):
        for tok in ("in-mgr", "sa-mgr"):
            DB.activity_logs.insert_one({"user_name": tok, "mobile": DB.dashboard_users.find_one({"token": tok})["mobile"],
                                         "module": "Stores", "action": "EDIT", "timestamp": datetime.utcnow()})
        r = web.get("/admin/auth/activity-logs", headers=H("in-mgr"))
        assert r.status_code == 200
        body = r.json()
        rows = body if isinstance(body, list) else body.get("logs", body.get("items", []))
        assert {l["user_name"] for l in rows} == {"in-mgr"}
        rows = web.get("/admin/auth/activity-logs", headers=H("super")).json()
        rows = rows if isinstance(rows, list) else rows.get("logs", rows.get("items", []))
        assert {l["user_name"] for l in rows} == {"in-mgr", "sa-mgr"}


# ═══════════════ data access: same country, other country, legacy, Super Admin ═══════════════
def ids(resp, key="_id"):
    j = resp.json()
    rows = j if isinstance(j, list) else j.get("items", j.get("reviews", []))
    return {r.get(key) for r in rows}


class TestListsAreFilteredByTheBackend:
    def test_accounts_list_paged_and_unpaged(self, world):
        for params in ({}, {"page": 1, "page_size": 50}):
            r = web.get("/admin/accounts", headers=H("in-mgr"), params=params).json()
            rows = r if isinstance(r, list) else r["items"]
            assert {x["account_id"] for x in rows} == {world["in"]["acct"]}
            r = web.get("/admin/accounts", headers=H("sa-mgr"), params=params).json()
            rows = r if isinstance(r, list) else r["items"]
            assert {x["account_id"] for x in rows} == {world["sa"]["acct"]}

    def test_super_admin_and_legacy_user_see_everything(self, world):
        for tok in ("super", "legacy"):
            rows = web.get("/admin/accounts", headers=H(tok)).json()
            assert len(rows) == 4

    def test_a_search_cannot_widen_the_scope(self, world):
        rows = web.get("/admin/accounts", headers=H("in-mgr"), params={"page": 1, "search": "Sami"}).json()["items"]
        assert rows == []
        rows = web.get("/admin/accounts", headers=H("in-mgr"), params={"page": 1, "search": "966"}).json()["items"]
        assert rows == []

    def test_account_without_resolvable_country_is_hidden_from_restricted_users_only(self, world):
        for tok in ("in-mgr", "sa-mgr"):
            rows = web.get("/admin/accounts", headers=H(tok)).json()
            assert world["x"]["acct"] not in {r["account_id"] for r in rows}
        assert world["x"]["acct"] in {r["account_id"] for r in web.get("/admin/accounts", headers=H("legacy")).json()}

    def test_nepal_account_without_phone_country_is_resolved_from_its_number(self, world):
        mk_user("np-mgr", "Country Manager", country="NP")
        rows = web.get("/admin/accounts", headers=H("np-mgr")).json()
        assert {r["account_id"] for r in rows} == {world["np"]["acct"]}

    def test_stores(self, world):
        assert ids(web.get("/admin/stores", headers=H("in-mgr"))) == {world["in"]["store"]}
        assert ids(web.get("/admin/stores", headers=H("sa-mgr"))) == {world["sa"]["store"]}
        assert len(web.get("/admin/stores", headers=H("super")).json()) == 4
        assert ids(web.get("/admin/stores/slim", headers=H("in-mgr"))) == {world["in"]["store"]}

    def test_merchant_dropdown_list(self, world):
        assert ids(web.get("/admin/merchants", headers=H("sa-mgr"))) == {world["sa"]["acct"]}

    def test_legacy_users_endpoint(self, world):
        assert ids(web.get("/admin/users", headers=H("in-mgr"))) == {world["in"]["acct"]}

    def test_subscriptions_transactions_invoices(self, world):
        r = web.get("/admin/subscriptions", headers=H("sa-mgr")).json()
        assert len(r) == 1
        r = web.get("/admin/merchant-transactions", headers=H("sa-mgr")).json()
        assert {x["invoice_no"] for x in r} == {"INV-sa"}
        r = web.get("/admin/invoices/full", headers=H("in-mgr")).json()
        assert {x.get("invoice_no") for x in r} == {"INV-in"}
        assert len(web.get("/admin/merchant-transactions", headers=H("super")).json()) == 4

    def test_withdraw_requests(self, world):
        assert ids(web.get("/admin/withdraw-requests", headers=H("in-mgr"))) == {world["in"]["wd"]}
        assert len(web.get("/admin/withdraw-requests", headers=H("super")).json()) == 4

    def test_influencers(self, world):
        assert ids(web.get("/admin/influencers", headers=H("sa-mgr"))) == {world["sa"]["inf"]}

    def test_banners(self, world):
        rows = web.get("/admin/merchant-banners", headers=H("in-mgr")).json()
        rows = rows if isinstance(rows, list) else rows.get("banners", rows.get("items", []))
        assert {r.get("_id") or r.get("id") for r in rows} == {world["in"]["ban"]}

    def test_deals(self, world):
        r = web.get("/admin/deals", headers=H("in-mgr")).json()
        got = {d["id"] if "id" in d else d["_id"] for d in (r["items"] if isinstance(r, dict) else r)}
        assert got == {world["in"]["deal"]}

    def test_products(self, world):
        assert {p["id"] for p in web.get("/admin/products", headers=H("sa-mgr")).json()} == {world["sa"]["prod"]}

    def test_reviews_and_review_stats(self, world):
        r = web.get("/admin/reviews", headers=H("in-mgr")).json()
        assert {x["_id"] for x in (r if isinstance(r, list) else r["reviews"])} == {world["in"]["rev"]}
        stats = web.get("/admin/reviews/stats", headers=H("sa-mgr")).json()["stats"]
        assert {s["store_id"] for s in stats} == {world["sa"]["store"]}

    def test_stats_counts_only_the_country(self, world):
        s = web.get("/admin/stats", headers=H("in-mgr")).json()
        assert s["total_accounts"] == 1 and s["total_merchants"] == 1 and s["total_stores"] == 1
        s = web.get("/admin/stats", headers=H("super")).json()
        assert s["total_accounts"] == 4 and s["total_stores"] == 4

    def test_product_reports(self, world):
        from services import products as P
        P.ensure_settings()
        assert web.get("/admin/products/summary", headers=H("in-mgr")).json()["total"] == 1
        assert web.get("/admin/products/summary", headers=H("super")).json()["total"] == 4
        tm = web.get("/admin/products/top-merchants", headers=H("sa-mgr")).json()
        assert {t["merchant_id"] for t in tm} == {world["sa"]["acct"]}
        ex = web.get("/admin/products/expiring", headers=H("sa-mgr"), params={"days": 30}).json()
        assert {e["id"] for e in ex} == {world["sa"]["prod"]}


class TestCityRestrictionStillAppliesInsideTheCountry:
    def test_city_and_country_combine(self, world):
        DB.stores.insert_one({"store_name": "Ravi Hubli", "city": "Hubli", "status": "active", "merchant_id": world["in"]["acct"]})
        mk_user("in-ballari", "Country Manager", country="IN", cities=("Ballari",))
        got = {s["store_name"] for s in web.get("/admin/stores", headers=H("in-ballari")).json()}
        assert got == {"Ravi Store"}                                  # India AND Ballari; not Hubli (city), not Riyadh (country)
        mk_user("in-all", "Country Manager", country="IN", cities=("*",))
        assert {s["store_name"] for s in web.get("/admin/stores", headers=H("in-all")).json()} == {"Ravi Store", "Ravi Hubli"}

    def test_user_without_city_assignment_still_sees_nothing(self, world):
        mk_user("in-nocity", "Country Manager", country="IN", cities=())
        assert web.get("/admin/stores", headers=H("in-nocity")).json() == []

    def test_city_only_user_is_unchanged(self, world):
        mk_user("ballari-only", "Country Manager", cities=("Ballari",))                        # legacy: no country
        got = {s["store_name"] for s in web.get("/admin/stores", headers=H("ballari-only")).json()}
        assert got == {"Ravi Store", "Nobody Store"}

    def test_country_user_in_the_wrong_city_set_sees_nothing(self, world):
        mk_user("sa-ballari", "Country Manager", country="SA", cities=("Ballari",))
        assert web.get("/admin/stores", headers=H("sa-ballari")).json() == []


class TestDirectApiAttemptsOnOtherCountryRecords:
    """Every id-based endpoint: another country's record answers 404 and NOTHING changes; own country works."""

    def test_account_endpoints(self, world):
        sa = world["sa"]["acct"]
        assert web.get(f"/admin/accounts/{sa}", headers=H("in-mgr")).status_code == 404
        assert web.get(f"/admin/accounts/{world['in']['acct']}", headers=H("in-mgr")).status_code == 200
        assert web.patch(f"/admin/accounts/{sa}/status", headers=H("in-mgr"), json={"status": "disabled"}).status_code == 404
        assert DB.accounts.find_one({"_id": ObjectId(sa)})["status"] == "active"
        assert web.patch(f"/admin/accounts/{world['in']['acct']}/status", headers=H("in-mgr"), json={"status": "inactive"}).status_code == 200
        DB.accounts.update_one({"_id": ObjectId(sa)}, {"$set": {"status": "delete_requested"}})
        assert web.post(f"/admin/accounts/{sa}/process-deletion", headers=H("in-mgr"), json={"action": "approve"}).status_code == 404
        assert DB.accounts.find_one({"_id": ObjectId(sa)}) is not None

    def test_merchant_endpoints(self, world):
        sa = world["sa"]["acct"]
        assert web.put(f"/admin/merchants/{sa}", headers=H("in-mgr"), json={"name": "Hacked"}).status_code == 404
        assert web.put(f"/admin/merchants/{sa}/status", headers=H("in-mgr")).status_code == 404
        assert web.delete(f"/admin/merchants/{sa}", headers=H("in-mgr")).status_code == 404
        assert DB.accounts.find_one({"_id": ObjectId(sa)})["name"] == "Sami" and DB.stores.count_documents({"merchant_id": sa}) == 1

    def test_a_restricted_user_cannot_move_its_merchant_to_another_country_by_changing_the_phone(self, world):
        r = web.put(f"/admin/merchants/{world['in']['acct']}", headers=H("in-mgr"), json={"phone": "+966509999999"})
        assert r.status_code == 403
        assert DB.accounts.find_one({"_id": ObjectId(world["in"]["acct"])})["phone"] == "9876543210"
        assert web.put(f"/admin/merchants/{world['in']['acct']}", headers=H("in-mgr"), json={"name": "Ravi K"}).status_code == 200

    def test_store_endpoints(self, world):
        s = world["sa"]["store"]
        assert web.get(f"/admin/stores/{s}", headers=H("in-mgr")).status_code == 404
        assert web.get(f"/admin/stores/{s}/qr", headers=H("in-mgr")).status_code == 404
        assert web.put(f"/admin/stores/{s}", headers=H("in-mgr"), json={"store_name": "Hacked"}).status_code == 404
        assert web.put(f"/admin/stores/{s}/status", headers=H("in-mgr")).status_code == 404
        assert web.put(f"/admin/stores/{s}/approve", headers=H("in-mgr")).status_code == 404
        assert web.put(f"/admin/stores/{s}/rating", headers=H("in-mgr"), json={"admin_rating": 1}).status_code == 404
        assert web.get(f"/admin/stores/{s}/renewal-info", headers=H("in-mgr")).status_code == 404
        assert web.delete(f"/admin/stores/{s}", headers=H("in-mgr")).status_code == 404
        doc = DB.stores.find_one({"_id": ObjectId(s)})
        assert doc["store_name"] == "Sami Store" and doc["status"] == "active" and "is_deleted" not in doc and "admin_rating" not in doc
        own = world["in"]["store"]
        assert web.get(f"/admin/stores/{own}", headers=H("in-mgr")).status_code == 200
        assert web.get(f"/admin/stores/{own}/qr", headers=H("in-mgr")).status_code == 200
        assert web.put(f"/admin/stores/{own}", headers=H("in-mgr"), json={"store_name": "Ravi 2"}).status_code == 200
        assert web.put(f"/admin/stores/{own}/rating", headers=H("in-mgr"), json={"admin_rating": 3}).status_code == 200

    def test_cannot_create_a_store_for_another_countrys_merchant_or_move_one_there(self, world):
        r = web.post("/admin/stores", headers=H("in-mgr"), json={"merchant_id": world["sa"]["acct"], "store_name": "X"})
        assert r.status_code == 404 and DB.stores.count_documents({"store_name": "X"}) == 0
        r = web.put(f"/admin/stores/{world['in']['store']}", headers=H("in-mgr"), json={"merchant_id": world["sa"]["acct"]})
        assert r.status_code == 404
        assert DB.stores.find_one({"_id": ObjectId(world["in"]["store"])})["merchant_id"] == world["in"]["acct"]
        r = web.post("/admin/stores", headers=H("in-mgr"), json={"merchant_id": world["in"]["acct"], "store_name": "Mine"})
        assert r.status_code == 200

    def test_user_points_endpoints(self, world):
        sa = world["sa"]["acct"]
        assert web.get(f"/admin/users/{sa}/history", headers=H("in-mgr")).status_code == 404
        assert web.post(f"/admin/users/{sa}/adjust-points", headers=H("in-mgr"), json={"points": 5, "type": "credit"}).status_code == 404
        assert DB.point_adjustments.count_documents({}) == 0
        assert web.post(f"/admin/users/{world['in']['acct']}/adjust-points", headers=H("in-mgr"), json={"points": 5, "type": "credit"}).status_code == 200

    def test_withdraw_fulfil(self, world):
        assert web.post(f"/admin/withdraw-requests/{world['sa']['wd']}/fulfill", headers=H("in-mgr"), json={"voucher_code": "ABC"}).status_code == 404
        assert DB.withdraw_requests.find_one({"_id": ObjectId(world["sa"]["wd"])})["status"] == "pending"
        assert web.post(f"/admin/withdraw-requests/{world['in']['wd']}/fulfill", headers=H("in-mgr"), json={"voucher_code": "ABC"}).status_code == 200

    def test_influencers(self, world):
        i = world["sa"]["inf"]
        assert web.put(f"/admin/influencers/{i}", headers=H("in-mgr"), json={"name": "H"}).status_code == 404
        assert web.delete(f"/admin/influencers/{i}", headers=H("in-mgr")).status_code == 404
        assert DB.influencers.find_one({"_id": ObjectId(i)}) is not None
        r = web.post("/admin/influencers", headers=H("in-mgr"), json={"name": "Foreign", "city": "Ballari", "phone": "+966501111111"})
        assert r.status_code == 403
        assert DB.influencers.count_documents({"name": "Foreign"}) == 0

    def test_banners(self, world):
        b = world["sa"]["ban"]
        for m, u in (("put", f"/admin/merchant-banners/{b}/approve"), ("put", f"/admin/merchant-banners/{b}/reject"),
                     ("put", f"/admin/merchant-banners/{b}"), ("delete", f"/admin/merchant-banners/{b}"),
                     ("delete", f"/admin/merchant-banners/{b}/hard"), ("put", f"/admin/merchant-banners/{b}/toggle")):
            r = getattr(web, m)(u, headers=H("in-mgr"), **({"json": {"title": "x"}} if u.endswith(b) and m == "put" else {}))
            assert r.status_code == 404, (u, r.status_code, r.text)
        assert DB.merchant_banners.find_one({"_id": ObjectId(b)})["approval_status"] == "pending_approval"
        assert web.post("/admin/merchant-banners/cleanup-duplicates", headers=H("in-mgr")).status_code == 403
        assert web.put(f"/admin/merchant-banners/{world['in']['ban']}/reject", headers=H("in-mgr"), json={"reason": "r"}).status_code == 200

    def test_deals(self, world):
        d = world["sa"]["deal"]
        assert web.get(f"/admin/deals/{d}", headers=H("in-mgr")).status_code in (403, 404)
        assert web.put(f"/admin/deals/{d}", headers=H("in-mgr"), json={"title": "H"}).status_code in (403, 404)
        assert web.delete(f"/admin/deals/{d}", headers=H("in-mgr")).status_code in (403, 404)
        assert DB.deals.find_one({"_id": ObjectId(d)})["title"] == "Sami deal"
        assert web.get(f"/admin/deals/{world['in']['deal']}", headers=H("in-mgr")).status_code == 200

    def test_products(self, world):
        p = world["sa"]["prod"]
        for m, u, b in (("put", f"/admin/products/{p}", {"title": "H"}), ("put", f"/admin/products/{p}/approve", None),
                        ("put", f"/admin/products/{p}/reject", {"reason": "x"}), ("delete", f"/admin/products/{p}", None),
                        ("put", f"/admin/products/{p}/deactivate", None), ("put", f"/admin/products/{p}/activate", None)):
            r = getattr(web, m)(u, headers=H("in-mgr"), **({"json": b} if b is not None else {}))
            assert r.status_code == 404, (u, r.status_code, r.text)
        assert DB.merchant_products.find_one({"_id": ObjectId(p)})["title"] == "Sami prod"
        r = web.post("/admin/products", headers=H("in-mgr"), json={"merchant_id": world["sa"]["acct"], "store_id": world["sa"]["store"], "title": "X", "end_date": FUT})
        assert r.status_code in (403, 404)
        assert DB.merchant_products.count_documents({"title": "X"}) == 0

    def test_reviews(self, world):
        r = world["sa"]["rev"]
        assert web.delete(f"/admin/reviews/{r}", headers=H("in-mgr")).status_code == 404
        assert web.patch(f"/admin/reviews/{r}/visibility", headers=H("in-mgr"), json={"visible": False}).status_code == 404
        assert DB.reviews.find_one({"_id": ObjectId(r)}).get("visible", True) is True
        assert web.delete(f"/admin/reviews/{world['in']['rev']}", headers=H("in-mgr")).status_code == 200

    def test_super_admin_and_legacy_user_are_not_blocked(self, world):
        for tok in ("super", "legacy"):
            assert web.get(f"/admin/stores/{world['sa']['store']}", headers=H(tok)).status_code == 200
            assert web.get(f"/admin/accounts/{world['sa']['acct']}", headers=H(tok)).status_code == 200

    def test_record_whose_owner_country_is_unknown_is_not_reachable_by_a_restricted_user(self, world):
        assert web.get(f"/admin/stores/{world['x']['store']}", headers=H("in-mgr")).status_code == 404
        assert web.get(f"/admin/accounts/{world['x']['acct']}", headers=H("in-mgr")).status_code == 404
        assert web.get(f"/admin/stores/{world['x']['store']}", headers=H("super")).status_code == 200

    def test_unauthenticated_and_underprivileged_requests_are_unchanged(self, world):
        assert web.get("/admin/stores").status_code == 401
        mk_user("noperm", "NoPerm", country="IN", perms={})
        assert web.put(f"/admin/stores/{world['in']['store']}", headers=H("noperm"), json={"store_name": "x"}).status_code == 403


class TestNotificationsAndMessaging:
    def test_topic_targets_are_refused_for_a_restricted_user(self, world):
        # Phase 2 Stage 1: "offers" stays refused. "city" is no longer refused outright - it is a direct-token send limited to the
        # city's own country - so a restricted user is refused only for a city of ANOTHER country (see test_phase2_stage1_cde.py).
        r = web.post("/admin/notifications/send", headers=H("in-mgr"), json={"title": "t", "body": "b", "target": "offers", "target_city": "Ballari"})
        assert r.status_code == 403
        DB.cities.insert_one({"name": "Riyadh", "slug": "riyadh", "country": "SA", "status": "active"})
        r = web.post("/admin/notifications/send", headers=H("in-mgr"), json={"title": "t", "body": "b", "target": "city", "target_city": "Riyadh"})
        assert r.status_code == 403
        r = web.post("/admin/notifications/send", headers=H("in-mgr"), json={"title": "t", "body": "b", "target": "specific", "target_phone": "+966501234567"})
        assert r.status_code == 404

    def test_fcm_token_diagnostic_lists_only_the_country(self, world):
        DB.accounts.update_one({"_id": ObjectId(world["in"]["acct"])}, {"$set": {"fcm_token": "a" * 40}})
        DB.accounts.update_one({"_id": ObjectId(world["sa"]["acct"])}, {"$set": {"fcm_token": "b" * 40}})
        r = web.get("/admin/fcm-tokens", headers=H("in-mgr")).json()
        assert r["total_tokens"] == 1 and r["tokens"][0]["phone"] == "9876543210"
        assert web.get("/admin/fcm-tokens", headers=H("super")).json()["total_tokens"] == 2

    def test_broadcast_to_all_users_is_accepted_for_the_country_and_its_recipient_query_is_country_only(self, world):
        DB.accounts.update_one({"_id": ObjectId(world["in"]["acct"])}, {"$set": {"fcm_token": "in-token"}})
        DB.accounts.update_one({"_id": ObjectId(world["sa"]["acct"])}, {"$set": {"fcm_token": "sa-token"}})
        r = web.post("/admin/notifications/send", headers=H("in-mgr"), json={"title": "t", "body": "b", "target": "all_users"})
        assert r.status_code == 200                                       # (Firebase is not configured in tests: nothing is sent)
        q = {"$and": [{"fcm_token": {"$exists": True, "$ne": ""}}, DC.account_query("IN")]}
        assert {d["fcm_token"] for d in DB.accounts.find(q)} == {"in-token"}


class TestWhatsApp:
    def test_merchant_audience_is_limited_to_the_country(self, world):
        from services import wa_audience as AUD
        for k in ("in", "sa"):
            DB.accounts.update_one({"_id": ObjectId(world[k]["acct"])}, {"$set": {"roles": ["merchant"]}})
        admin = {"role": {"role_name": "Manager"}, "assigned_cities": ["*"], "country": "IN"}
        rows = AUD.resolve_merchants(ids=[world["in"]["acct"], world["sa"]["acct"]], admin=admin)
        by = {r["merchant_id"]: r for r in rows}
        assert by[world["in"]["acct"]]["reason"] != "merchant_out_of_scope"
        assert by[world["sa"]["acct"]]["qualifies"] is False and by[world["sa"]["acct"]]["reason"] == "merchant_out_of_scope"
        sup = {"role": {"role_name": "Super Admin"}, "assigned_cities": ["*"], "country": "IN"}
        assert {r["merchant_id"]: r["reason"] for r in AUD.resolve_merchants(ids=[world["sa"]["acct"]], admin=sup)}[world["sa"]["acct"]] != "merchant_out_of_scope"
        legacy = {"role": {"role_name": "Manager"}, "assigned_cities": ["*"]}
        assert {r["merchant_id"]: r["reason"] for r in AUD.resolve_merchants(ids=[world["sa"]["acct"]], admin=legacy)}[world["sa"]["acct"]] != "merchant_out_of_scope"

    def test_live_chat_conversations_are_limited_to_the_country(self, world, monkeypatch):
        _chat_uses_the_shared_test_db(monkeypatch)
        T = lambda t: {"X-Admin-Token": t}                      # the live-chat router reads this header
        DB.customers_whatsapp.insert_one({"name": "Indian", "phone_number": "919876543210", "last_message_time": datetime.utcnow()})
        b = DB.customers_whatsapp.insert_one({"name": "Saudi", "phone_number": "966501234567", "last_message_time": datetime.utcnow()}).inserted_id
        r = web.get("/admin/whatsapp/chats", headers=T("in-mgr"))
        assert r.status_code == 200, r.text
        assert [c["name"] for c in r.json()["customers"]] == ["Indian"]
        assert web.get(f"/admin/whatsapp/chats/{b}", headers=T("in-mgr")).status_code == 404
        assert web.post(f"/admin/whatsapp/chats/{b}/read", headers=T("in-mgr")).status_code == 404
        assert web.delete(f"/admin/whatsapp/chats/{b}", headers=T("in-mgr")).status_code == 404
        assert DB.customers_whatsapp.find_one({"_id": b}) is not None
        assert web.get(f"/admin/whatsapp/chats/{b}", headers=T("legacy")).status_code == 200
        assert web.get(f"/admin/whatsapp/chats/{b}", headers=T("super")).status_code == 200
        assert len(web.get("/admin/whatsapp/chats", headers=T("super")).json()["customers"]) == 2


# ═══════════════ an invalid stored country must FAIL CLOSED ═══════════════
BAD_COUNTRIES = ["ZZ", "India", "SAU", "S A", "966", 5, 0, True, ["SA"], {"iso": "SA"}, "!invalid", "  zz "]


@pytest.mark.parametrize("bad", BAD_COUNTRIES)
def test_scope_of_an_invalid_stored_country_is_never_unrestricted(bad):
    got = DC.scope({"role_name": "Manager", "country": bad})
    assert got == DC.INVALID_SCOPE and got != ""
    assert got not in C.supported_iso_codes()


def test_scope_missing_blank_valid_and_super_admin_cases():
    for legacy in ({}, {"country": None}, {"country": ""}, {"country": "   "}):
        assert DC.scope({"role_name": "Manager", **legacy}) == ""                      # agreed legacy behaviour
    assert DC.scope({"role_name": "Manager", "country": " sa "}) == "SA"
    for bad in BAD_COUNTRIES:
        assert DC.scope({"role_name": "Super Admin", "country": bad}) == ""            # Super Admin stays unrestricted
        assert DC.scope({"role": {"role_name": "Super Admin"}, "country": bad}) == ""


def test_invalid_scope_matches_no_account_and_no_record(world=None):
    DB.accounts.insert_one({"phone": "9876543210"}); DB.accounts.insert_one({"phone": "+966501234567", "phone_country": "SA"})
    assert DB.accounts.count_documents(DC.account_query(DC.INVALID_SCOPE)) == 0
    assert DC.owner_ids(DC.INVALID_SCOPE) == ([], [])
    assert DC.account_in_scope({"role_name": "M", "country": "ZZ"}, {"phone": "9876543210"}) is False
    assert DC.record_in_scope({"role_name": "M", "country": "ZZ"}, {"merchant_id": "x"}) is False


@pytest.mark.parametrize("bad", ["ZZ", "India", 5, ["SA"], True])
class TestInvalidStoredCountryCannotReachAnyCountry:
    @pytest.fixture
    def bad_user(self, world, bad):
        mk_user("bad-mgr", "Country Manager", country=bad)
        return "bad-mgr"

    def test_every_list_is_empty(self, world, bad_user):
        h = H(bad_user)
        r = web.get("/admin/accounts", headers=h); assert r.status_code == 200 and r.json() == []
        r = web.get("/admin/accounts", headers=h, params={"page": 1}); assert r.json()["items"] == []
        for url in ("/admin/stores", "/admin/stores/slim", "/admin/merchants", "/admin/users", "/admin/subscriptions",
                    "/admin/merchant-transactions", "/admin/invoices/full", "/admin/withdraw-requests", "/admin/influencers",
                    "/admin/products"):
            r = web.get(url, headers=h)
            assert r.status_code == 200 and r.json() == [], (url, r.text)
        for url in ("/admin/deals", "/admin/reviews", "/admin/merchant-banners"):
            j = web.get(url, headers=h).json()
            rows = j if isinstance(j, list) else next((v for v in j.values() if isinstance(v, list)), [])
            assert rows == [], url
        assert web.get("/admin/reviews/stats", headers=h).json()["stats"] == []
        s = web.get("/admin/stats", headers=h).json()
        assert s["total_accounts"] == 0 and s["total_stores"] == 0 and s["total_merchants"] == 0
        assert web.get("/admin/fcm-tokens", headers=h).json()["total_tokens"] == 0

    def test_every_record_of_every_country_is_denied(self, world, bad_user):
        h = H(bad_user)
        for k in ("in", "sa", "np", "x"):
            w = world[k]
            assert web.get(f"/admin/accounts/{w['acct']}", headers=h).status_code == 404
            assert web.get(f"/admin/stores/{w['store']}", headers=h).status_code == 404
            assert web.put(f"/admin/stores/{w['store']}", headers=h, json={"store_name": "H"}).status_code == 404
            assert web.delete(f"/admin/stores/{w['store']}", headers=h).status_code == 404
            assert web.patch(f"/admin/accounts/{w['acct']}/status", headers=h, json={"status": "disabled"}).status_code == 404
            assert web.put(f"/admin/merchants/{w['acct']}", headers=h, json={"name": "H"}).status_code == 404
            assert web.post(f"/admin/users/{w['acct']}/adjust-points", headers=h, json={"points": 5, "type": "credit"}).status_code == 404
            assert web.put(f"/admin/influencers/{w['inf']}", headers=h, json={"name": "H"}).status_code == 404
            assert web.delete(f"/admin/reviews/{w['rev']}", headers=h).status_code == 404
            assert web.put(f"/admin/products/{w['prod']}", headers=h, json={"title": "H"}).status_code == 404
            assert web.put(f"/admin/merchant-banners/{w['ban']}/approve", headers=h).status_code == 404
            assert web.post(f"/admin/withdraw-requests/{w['wd']}/fulfill", headers=h, json={"voucher_code": "A"}).status_code == 404
            assert web.get(f"/admin/deals/{w['deal']}", headers=h).status_code in (403, 404)
        assert DB.stores.count_documents({"store_name": "H"}) == 0 and DB.stores.count_documents({"status": "deleted"}) == 0
        assert DB.accounts.count_documents({"status": "disabled"}) == 0 and DB.accounts.count_documents({"name": "H"}) == 0
        assert DB.merchant_banners.count_documents({"approval_status": "approved"}) == 0
        assert DB.withdraw_requests.count_documents({"status": "fulfilled"}) == 0

    def test_it_cannot_create_users_or_stores_or_manage_dashboard_users(self, world, bad_user, bad):
        h = H(bad_user)
        rid = str(DB.dashboard_roles.find_one({"role_name": "Country Manager"})["_id"])
        for c in (None, "IN", "ZZ"):
            body = {"full_name": "N", "mobile": "9123456789", "pin": "1234", "role_id": rid, **({"country": c} if c is not None else {})}
            assert web.post("/admin/auth/users", headers=h, json=body).status_code in (403, 422)
        assert DB.dashboard_users.find_one({"mobile": "9123456789"}) is None
        assert web.post("/admin/stores", headers=h, json={"merchant_id": world["in"]["acct"], "store_name": "X"}).status_code == 404
        assert [u for u in web.get("/admin/auth/users", headers=h).json()] == []          # sees no dashboard user, not even itself
        uid = str(DB.dashboard_users.find_one({"token": "in-mgr"})["_id"])
        assert web.put(f"/admin/auth/users/{uid}", headers=h, json={"country": "IN"}).status_code == 404
        assert web.delete(f"/admin/auth/users/{uid}", headers=h).status_code == 404
        own = str(DB.dashboard_users.find_one({"token": bad_user})["_id"])
        assert web.put(f"/admin/auth/users/{own}", headers=h, json={"country": ""}).status_code in (403, 404)     # cannot lift it itself
        assert DB.dashboard_users.find_one({"token": bad_user})["country"] == bad
        assert web.get("/admin/auth/activity-logs", headers=h).status_code == 200

    def test_messaging_and_bulk_endpoints_are_refused_or_empty(self, world, bad_user, bad, monkeypatch):
        _chat_uses_the_shared_test_db(monkeypatch)
        h = H(bad_user)
        for t in ("offers", "city"):
            assert web.post("/admin/notifications/send", headers=h, json={"title": "t", "body": "b", "target": t}).status_code == 403
        assert web.post("/admin/notifications/send", headers=h, json={"title": "t", "body": "b", "target": "specific", "target_phone": "9876543210"}).status_code == 404
        assert web.post("/admin/merchant-banners/cleanup-duplicates", headers=h).status_code == 403
        T = {"X-Admin-Token": bad_user}
        DB.customers_whatsapp.insert_one({"name": "Indian", "phone_number": "919876543210", "last_message_time": datetime.utcnow()})
        assert web.get("/admin/whatsapp/chats", headers=T).json()["customers"] == []
        from services import wa_audience as AUD
        rows = AUD.resolve_merchants(ids=[world["in"]["acct"]], admin={"role": {"role_name": "Manager"}, "assigned_cities": ["*"], "country": bad})
        assert rows[0]["qualifies"] is False and rows[0]["reason"] == "merchant_out_of_scope"

    def test_the_valid_country_users_are_unaffected(self, world, bad_user):
        assert len(web.get("/admin/accounts", headers=H("in-mgr")).json()) == 1
        assert len(web.get("/admin/accounts", headers=H("legacy")).json()) == 4
        assert len(web.get("/admin/accounts", headers=H("super")).json()) == 4


def test_a_corrupted_super_admin_country_does_not_restrict_super_admin(world):
    DB.dashboard_users.update_one({"token": "super"}, {"$set": {"country": "ZZ"}})
    assert len(web.get("/admin/accounts", headers=H("super")).json()) == 4
    assert web.get(f"/admin/stores/{world['sa']['store']}", headers=H("super")).status_code == 200


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_missing_or_blank_country_keeps_the_legacy_behaviour(world, blank):
    mk_user("blank-mgr", "Country Manager", country=blank)
    assert len(web.get("/admin/accounts", headers=H("blank-mgr")).json()) == 4
    assert web.get(f"/admin/stores/{world['sa']['store']}", headers=H("blank-mgr")).status_code == 200
