"""NOTE: file names sort AFTER test_banner_media.py on purpose - that module swaps the shared fake `database` at import.
Pricing & Plans module: country master, country-wise prices, tax, reward conversion, permissions, India unchanged.
Run:  python -m pytest test_pricing_multi_country.py -q -W ignore"""
import copy
import sys

import pytest
from fastapi.testclient import TestClient

import server

fake = sys.modules["database"].db
c = TestClient(server.app)
SA_H = {"Authorization": "Bearer sa_tok"}
VIEW_H = {"Authorization": "Bearer view_tok"}
EDIT_H = {"Authorization": "Bearer edit_tok"}
NONE_H = {"Authorization": "Bearer none_tok"}
JO_H = {"Authorization": "Bearer jo_tok"}
BASE = "/admin/pricing-config"

INDIA_DOC = {"gst_percent": 18, "plans": [{"id": "1month", "label": "1 Month", "price": 499}, {"id": "3months", "label": "3 Months", "price": 1299},
                                           {"id": "6months", "label": "6 Months", "price": 2299}, {"id": "12months", "label": "12 Months", "price": 3999}],
             "banner_price_per_day": 15, "product_price_per_day": 10, "free_product_limit": 10, "product_max_validity_days": 365,
             "influencer_subscription": {"fee": 999.0, "gst_percent": 18.0, "enabled": True}, "conversion_rate": 0.10, "min_withdraw_points": 200}


def _user(name, perms, token, **extra):
    rid = fake.dashboard_roles.insert_one({"role_name": name, "permissions": perms}).inserted_id
    fake.dashboard_users.insert_one({"token": token, "status": "active", "full_name": name, "mobile": "9" + token.ljust(9, "0")[:9],
                                     "role_id": rid, "assigned_cities": ["*"], **extra})


@pytest.fixture(autouse=True)
def _world():
    for col in ("dashboard_users", "dashboard_roles", "pricing", "pricing_countries", "country_prices", "pricing_history"):
        fake[col].delete_many({})
    _user("Super Admin", {}, "sa_tok")
    _user("Viewer", {"Pricing & GST": {"view": True}}, "view_tok")
    _user("Editor", {"Pricing & GST": {"view": True}, "Settings": {"edit": True}}, "edit_tok")
    _user("Nobody", {}, "none_tok")
    _user("Jordan Admin", {"Pricing & GST": {"view": True}, "Settings": {"edit": True}}, "jo_tok", country="JO")
    fake.pricing.insert_one(copy.deepcopy(INDIA_DOC))
    yield


def _india_doc():
    d = fake.pricing.find_one({})
    d.pop("_id")
    return d


def _add(code="SA", **kw):
    body = {"name": "Saudi Arabia", "code": code, "currency_name": "Saudi Riyal", "currency_code": "SAR", "currency_symbol": "SAR",
            "tax_label": "VAT", "tax_percent": 15}
    body.update(kw)
    return c.post(BASE + "/countries", json=body, headers=SA_H)


# ── country master ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_list_starts_with_built_in_india_and_reads_gst_from_existing_field():
    r = c.get(BASE + "/countries", headers=SA_H).json()
    assert [x["code"] for x in r["countries"]] == ["IN"]
    i = r["countries"][0]
    assert i["currency_code"] == "INR" and i["tax_label"] == "GST" and i["tax_percent"] == 18.0 and i["built_in"] is True and i["tax_configured"]
    assert r["can_add"] is True


def test_add_country_without_code_change_and_it_appears():
    r = _add()
    assert r.status_code == 200 and r.json()["country"]["code"] == "SA"
    codes = [x["code"] for x in c.get(BASE + "/countries", headers=SA_H).json()["countries"]]
    assert codes == ["IN", "SA"]
    assert fake.pricing_countries.count_documents({}) == 1


def test_add_country_works_for_a_code_not_in_any_code_list():
    assert _add("KE", name="Kenya", currency_name="Kenyan Shilling", currency_code="KES", currency_symbol="KSh", tax_label="VAT", tax_percent=16).status_code == 200


def test_duplicate_country_code_and_name_are_rejected():
    assert _add().status_code == 200
    d = _add()
    assert d.status_code == 409 and d.json()["detail"]["code"] == "duplicate_country"
    assert _add("sa").status_code == 409                                  # case-insensitive
    assert _add("AE", name="saudi arabia").status_code == 409             # same name
    assert _add("IN", name="Other India").status_code == 409              # India is built in
    assert fake.pricing_countries.count_documents({}) == 1


@pytest.mark.parametrize("patch,code", [
    ({"code": "S"}, "invalid_country_code"), ({"code": "SAU"}, "invalid_country_code"), ({"code": "1A"}, "invalid_country_code"),
    ({"currency_code": "SA"}, "invalid_currency_code"), ({"currency_code": "sr1"}, "invalid_currency_code"),
    ({"name": ""}, "required"), ({"currency_name": " "}, "required"), ({"currency_symbol": ""}, "required"),
    ({"tax_percent": 101}, "invalid"), ({"tax_percent": -1}, "invalid"), ({"tax_percent": "abc"}, "invalid"),
    ({"tax_label": ""}, "tax_incomplete"),
])
def test_validation_rejects_bad_country_input(patch, code):
    r = _add(**patch)
    assert r.status_code == 422 and r.json()["detail"]["code"] == code
    assert fake.pricing_countries.count_documents({}) == 0


def test_country_without_tax_is_allowed_but_reports_a_configuration_error_never_another_rate():
    assert _add(tax_label="", tax_percent=None).status_code == 200
    row = [x for x in c.get(BASE + "/countries", headers=SA_H).json()["countries"] if x["code"] == "SA"][0]
    assert row["tax_configured"] is False and "not configured" in row["tax_error"].lower()
    p = c.get(BASE + "/countries/SA/prices", headers=SA_H).json()
    assert p["tax"]["configured"] is False and p["tax"]["percent"] is None
    c.put(BASE + "/countries/SA/prices/store_plans", json={"plans": [{"id": "1month", "price": 29}]}, headers=SA_H)
    row = c.get(BASE + "/countries/SA/prices", headers=SA_H).json()["store_plans"][0]
    assert row["price"] == 29 and row["tax_amount"] is None and row["total"] is None       # no India 18% leaks in


def test_explicit_tax_free_is_a_valid_configuration():
    assert _add(tax_label="", tax_percent=None, tax_free=True).status_code == 200
    p = c.get(BASE + "/countries/SA/prices", headers=SA_H).json()
    assert p["tax"]["configured"] and p["tax"]["percent"] == 0.0 and p["tax"]["tax_free"]


def test_edit_country_and_activate_deactivate():
    _add()
    r = c.put(BASE + "/countries/SA", json={"name": "KSA", "tax_label": "VAT", "tax_percent": 14}, headers=SA_H)
    assert r.status_code == 200 and r.json()["country"]["name"] == "KSA" and r.json()["country"]["tax_percent"] == 14
    assert r.json()["country"]["currency_code"] == "SAR"                  # untouched fields kept
    assert c.post(BASE + "/countries/SA/status", json={"status": "inactive"}, headers=SA_H).json()["country"]["status"] == "inactive"
    assert c.post(BASE + "/countries/SA/status", json={"status": "active"}, headers=SA_H).json()["country"]["status"] == "active"
    assert c.post(BASE + "/countries/SA/status", json={"status": "bogus"}, headers=SA_H).status_code == 422
    assert c.post(BASE + "/countries/IN/status", json={"status": "inactive"}, headers=SA_H).status_code == 409
    assert c.put(BASE + "/countries/XX", json={"name": "Nope"}, headers=SA_H).status_code == 404


def test_edit_cannot_rename_into_an_existing_country():
    _add()
    _add("JO", name="Jordan", currency_name="Jordanian Dinar", currency_code="JOD", currency_symbol="JD", tax_label="VAT", tax_percent=16)
    assert c.put(BASE + "/countries/JO", json={"name": "Saudi Arabia"}, headers=SA_H).status_code == 409


# ── prices ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_new_country_starts_not_configured_for_every_section():
    _add()
    p = c.get(BASE + "/countries/SA/prices", headers=SA_H).json()
    assert all(not r["configured"] and r["price"] is None for r in p["store_plans"])
    assert not p["influencer"]["configured"] and not p["banner"]["configured"] and not p["product"]["configured"] and not p["reward"]["configured"]


def test_country_prices_are_independent_with_own_tax_and_no_conversion():
    _add()
    _add("JO", name="Jordan", currency_name="Jordanian Dinar", currency_code="JOD", currency_symbol="JD", tax_label="VAT", tax_percent=16)
    assert c.put(BASE + "/countries/SA/prices/store_plans", json={"plans": [{"id": "1month", "price": 29}, {"id": "3months", "price": 79}]}, headers=SA_H).status_code == 200
    assert c.put(BASE + "/countries/JO/prices/store_plans", json={"plans": [{"id": "1month", "price": 3.5}]}, headers=SA_H).status_code == 200
    sa = c.get(BASE + "/countries/SA/prices", headers=SA_H).json()["store_plans"]
    jo = c.get(BASE + "/countries/JO/prices", headers=SA_H).json()["store_plans"]
    assert sa[0]["price"] == 29 and sa[0]["tax_amount"] == 4.35 and sa[0]["total"] == 33.35
    assert jo[0]["price"] == 3.5 and jo[0]["tax_amount"] == 0.56 and jo[0]["total"] == 4.06
    assert sa[2]["configured"] is False and jo[1]["configured"] is False


def test_banner_product_influencer_sections_save_and_validate():
    _add()
    s = BASE + "/countries/SA/prices/"
    assert c.put(s + "banner", json={"price_per_day": 1.5}, headers=SA_H).json()["section"]["price"] == 1.5
    assert c.put(s + "product", json={"price_per_day": 1}, headers=SA_H).status_code == 422             # needs all three together
    ok = c.put(s + "product", json={"price_per_day": 1, "free_product_limit": 5, "product_max_validity_days": 90}, headers=SA_H).json()["section"]
    assert ok["price"] == 1 and ok["free_product_limit"] == 5 and ok["product_max_validity_days"] == 90
    assert c.put(s + "influencer", json={"fee": 59}, headers=SA_H).json()["section"]["total"] == 67.85
    for bad in ({"price_per_day": -1}, {"price_per_day": "x"}, {"price_per_day": ""}, {"price_per_day": 1e12}, {"price_per_day": True}, {}):
        assert c.put(s + "banner", json=bad, headers=SA_H).status_code == 422
    assert c.put(s + "product", json={"price_per_day": 1, "free_product_limit": 1.5, "product_max_validity_days": 90}, headers=SA_H).status_code == 422
    assert c.put(s + "product", json={"price_per_day": 1, "free_product_limit": 5, "product_max_validity_days": 0}, headers=SA_H).status_code == 422
    assert c.put(s + "nonsense", json={}, headers=SA_H).status_code == 404


def test_activate_deactivate_a_price_row_for_a_new_country():
    _add()
    s = BASE + "/countries/SA/prices/store_plans"
    assert c.put(s, json={"plans": [{"id": "1month", "active": True}]}, headers=SA_H).status_code == 422          # no price yet
    c.put(s, json={"plans": [{"id": "1month", "price": 29}]}, headers=SA_H)
    r = c.put(s, json={"plans": [{"id": "1month", "active": False}]}, headers=SA_H).json()["section"]
    assert r[0]["active"] is False and r[0]["price"] == 29
    assert c.put(s, json={"plans": [{"id": "1month", "active": True}]}, headers=SA_H).json()["section"][0]["active"] is True


# ── India unchanged ──────────────────────────────────────────────────────────────────────────────────────────────────
def test_reading_never_changes_india_and_matches_the_existing_endpoints():
    before = _india_doc()
    p = c.get(BASE + "/countries/IN/prices", headers=SA_H).json()
    c.get(BASE + "/countries", headers=SA_H)
    assert _india_doc() == before
    legacy = c.get("/admin/pricing", headers=SA_H).json()
    assert [x["price"] for x in p["store_plans"]] == [x["price"] for x in legacy["plans"]] == [499, 1299, 2299, 3999]
    assert p["tax"]["percent"] == legacy["gst_percent"] == 18
    assert p["store_plans"][0]["total"] == 588.82 and p["influencer"]["price"] == legacy["influencer_subscription"]["fee"]
    assert p["influencer"]["total"] == legacy["influencer_subscription"]["total"]
    banner = c.get("/admin/banner-pricing", headers=SA_H).json()
    assert p["banner"]["price"] == banner["banner_price_per_day"] and p["product"]["price"] == banner["product_price_per_day"]
    assert p["product"]["free_product_limit"] == legacy["free_product_limit"] and p["product"]["product_max_validity_days"] == legacy["product_max_validity_days"]
    assert p["reward"]["conversion_rate"] == legacy["conversion_rate"] and p["reward"]["min_withdraw_points"] == legacy["min_withdraw_points"]


def test_saving_other_countries_never_touches_india():
    before = _india_doc()
    _add()
    c.put(BASE + "/countries/SA/prices/store_plans", json={"plans": [{"id": "1month", "price": 29}]}, headers=SA_H)
    c.put(BASE + "/countries/SA/prices/reward", json={"conversion_rate": 0.5, "min_withdraw_points": 50}, headers=SA_H)
    c.put(BASE + "/countries/SA", json={"tax_label": "VAT", "tax_percent": 5}, headers=SA_H)
    assert _india_doc() == before


def test_india_edits_write_only_the_existing_fields_and_nothing_else():
    r = c.put(BASE + "/countries/IN/prices/store_plans", json={"plans": [{"id": "1month", "price": 549}]}, headers=SA_H)
    assert r.status_code == 200
    d = _india_doc()
    assert [p["price"] for p in d["plans"]] == [549, 1299, 2299, 3999]
    expected = copy.deepcopy(INDIA_DOC)
    expected["plans"][0]["price"] = 549
    assert d == expected
    assert c.put(BASE + "/countries/IN", json={"tax_percent": 12}, headers=SA_H).json()["country"]["tax_percent"] == 12
    assert fake.pricing.find_one({})["gst_percent"] == 12.0
    assert fake.pricing_countries.count_documents({}) == 0 and fake.country_prices.count_documents({}) == 0


def test_india_sections_save_through_existing_fields():
    s = BASE + "/countries/IN/prices/"
    c.put(s + "banner", json={"price_per_day": 20}, headers=SA_H)
    c.put(s + "product", json={"price_per_day": 12, "free_product_limit": 8, "product_max_validity_days": 300}, headers=SA_H)
    c.put(s + "influencer", json={"fee": 1200, "enabled": False}, headers=SA_H)
    c.put(s + "reward", json={"conversion_rate": 0.12, "min_withdraw_points": 250}, headers=SA_H)
    d = _india_doc()
    assert d["banner_price_per_day"] == 20 and d["product_price_per_day"] == 12 and d["free_product_limit"] == 8 and d["product_max_validity_days"] == 300
    assert d["influencer_subscription"] == {"fee": 1200.0, "gst_percent": 18.0, "enabled": False}
    assert d["conversion_rate"] == 0.12 and d["min_withdraw_points"] == 250
    assert c.get("/admin/pricing", headers=SA_H).json()["conversion_rate"] == 0.12      # the existing screen sees the same value


def test_india_defaults_are_reported_when_the_pricing_document_is_empty():
    fake.pricing.delete_many({})
    p = c.get(BASE + "/countries/IN/prices", headers=SA_H).json()
    assert [x["price"] for x in p["store_plans"]] == [499, 1299, 2299, 3999] and p["tax"]["percent"] == 18
    assert p["banner"]["price"] == 15 and p["product"]["price"] == 10 and p["reward"]["conversion_rate"] == 0.10 and p["reward"]["min_withdraw_points"] == 200
    assert fake.pricing.count_documents({}) == 0                           # reading wrote nothing


# ── reward redemption ────────────────────────────────────────────────────────────────────────────────────────────────
def test_reward_is_per_country_and_never_inherits_india():
    _add()
    r = c.get(BASE + "/countries/SA/prices", headers=SA_H).json()["reward"]
    assert r["configured"] is False and r["conversion_rate"] is None
    lst = {x["code"]: x for x in c.get(BASE + "/countries", headers=SA_H).json()["countries"]}
    assert lst["SA"]["reward"]["configured"] is False and lst["IN"]["reward"]["conversion_rate"] == 0.10
    assert c.put(BASE + "/countries/SA/prices/reward", json={"conversion_rate": 0.5}, headers=SA_H).status_code == 422
    ok = c.put(BASE + "/countries/SA/prices/reward", json={"conversion_rate": 0.5, "min_withdraw_points": 50}, headers=SA_H).json()["section"]
    assert ok["configured"] and ok["conversion_rate"] == 0.5 and ok["min_withdraw_points"] == 50
    assert c.get(BASE + "/countries/IN/prices", headers=SA_H).json()["reward"]["conversion_rate"] == 0.10
    assert c.put(BASE + "/countries/SA/prices/reward", json={"conversion_rate": 0, "min_withdraw_points": 50}, headers=SA_H).status_code == 422
    assert c.put(BASE + "/countries/SA/prices/reward", json={"conversion_rate": 1, "min_withdraw_points": 0}, headers=SA_H).status_code == 422


def test_existing_wallet_reward_logic_is_unchanged_by_other_countries():
    _add()
    c.put(BASE + "/countries/SA/prices/reward", json={"conversion_rate": 9, "min_withdraw_points": 1}, headers=SA_H)
    doc = fake.pricing.find_one({})
    assert doc["conversion_rate"] == 0.10 and doc["min_withdraw_points"] == 200


# ── permissions ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_permissions_view_edit_and_no_access():
    assert c.get(BASE + "/countries", headers=NONE_H).status_code == 403
    assert c.get(BASE + "/countries").status_code == 401
    assert c.get(BASE + "/countries", headers=VIEW_H).status_code == 200
    assert c.get(BASE + "/countries/IN/prices", headers=VIEW_H).status_code == 200
    assert c.get(BASE + "/history", headers=NONE_H).status_code == 403
    body = {"name": "Saudi Arabia", "code": "SA", "currency_name": "Riyal", "currency_code": "SAR", "currency_symbol": "SAR"}
    assert c.post(BASE + "/countries", json=body, headers=VIEW_H).status_code == 403
    assert c.post(BASE + "/countries", json=body, headers=NONE_H).status_code == 403
    assert c.put(BASE + "/countries/IN/prices/banner", json={"price_per_day": 99}, headers=VIEW_H).status_code == 403
    assert c.put(BASE + "/countries/IN", json={"tax_percent": 5}, headers=VIEW_H).status_code == 403
    assert c.post(BASE + "/countries/IN/status", json={"status": "active"}, headers=VIEW_H).status_code == 403
    assert _india_doc() == INDIA_DOC
    assert c.post(BASE + "/countries", json=body, headers=EDIT_H).status_code == 200


def test_country_restricted_admin_only_sees_and_changes_their_country():
    _add()
    _add("JO", name="Jordan", currency_name="Jordanian Dinar", currency_code="JOD", currency_symbol="JD", tax_label="VAT", tax_percent=16)
    r = c.get(BASE + "/countries", headers=JO_H).json()
    assert [x["code"] for x in r["countries"]] == ["JO"] and r["can_add"] is False
    assert c.get(BASE + "/countries/SA/prices", headers=JO_H).status_code == 403
    assert c.get(BASE + "/countries/IN/prices", headers=JO_H).status_code == 403
    assert c.put(BASE + "/countries/SA/prices/banner", json={"price_per_day": 5}, headers=JO_H).status_code == 403
    assert c.put(BASE + "/countries/IN/prices/banner", json={"price_per_day": 5}, headers=JO_H).status_code == 403
    assert c.put(BASE + "/countries/JO/prices/banner", json={"price_per_day": 0.2}, headers=JO_H).status_code == 200
    assert c.post(BASE + "/countries", json={"name": "Egypt", "code": "EG", "currency_name": "Pound", "currency_code": "EGP", "currency_symbol": "E"}, headers=JO_H).status_code == 403
    assert _india_doc() == INDIA_DOC


# ── history ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_history_records_who_what_old_and_new():
    _add()
    c.put(BASE + "/countries/SA/prices/banner", json={"price_per_day": 1.5}, headers=SA_H)
    c.put(BASE + "/countries/SA/prices/banner", json={"price_per_day": 2}, headers=SA_H)
    h = c.get(BASE + "/history?country=SA", headers=SA_H).json()["history"]
    assert [x["action"] for x in h][:2] == ["update", "update"]
    top = h[0]
    assert top["section"] == "banner" and top["old"] == {"price": 1.5} and top["new"] == {"price": 2} and top["by"] and top["at"].endswith("Z")
    assert any(x["section"] == "country" and x["action"] == "create" for x in h)
    assert c.get(BASE + "/history?country=IN", headers=SA_H).json()["history"] == []


def test_history_names_the_admin_and_records_only_changed_fields():
    _add()
    c.put(BASE + "/countries/SA/prices/store_plans", json={"plans": [{"id": "1month", "price": 29}]}, headers=SA_H)
    c.put(BASE + "/countries/SA/prices/store_plans", json={"plans": [{"id": "3months", "price": 79}]}, headers=SA_H)
    h = c.get(BASE + "/history?country=SA", headers=SA_H).json()["history"]
    assert h[0]["by"] == "Super Admin"
    assert h[0]["old"] == {"3months.price": None, "3months.configured": False, "3months.active": False} or "3months.price" in h[0]["new"]
    assert "1month.price" not in (h[0]["new"] or {})


def test_failed_saves_write_no_history_and_no_data():
    _add()
    n = fake.pricing_history.count_documents({})
    assert c.put(BASE + "/countries/SA/prices/banner", json={"price_per_day": -5}, headers=SA_H).status_code == 422
    assert fake.pricing_history.count_documents({}) == n and fake.country_prices.count_documents({}) == 0


# ── isolation / structure ────────────────────────────────────────────────────────────────────────────────────────────
def test_existing_pricing_endpoints_are_untouched():
    assert c.get("/admin/pricing", headers=SA_H).json()["plans"][0]["price"] == 499
    assert c.put("/admin/pricing", json={"gst_percent": 18}, headers=SA_H).status_code == 200
    assert c.get("/merchant/plans").status_code == 401        # no verified country -> India prices are not exposed (Phase 4)


def test_dashboard_template_wires_the_module_in_place_of_the_old_screen():
    t = open("templates/admin_dashboard.html", encoding="utf-8").read()
    assert "showSection('pricing',this)" not in t and "showSection('pricing_plans',this)" in t
    assert t.index("{% include \"partials/pricing_plans.html\" %}") > 0 and "/static/js/pricing_plans.js" in t and "/static/css/pricing_plans.css" in t
    assert '"Pricing & GST":     ["pricing_plans"]' in t and 'PricingPlans.open()' in t
    p = open("templates/partials/pricing_plans.html", encoding="utf-8").read()
    assert 'id="section-pricing_plans"' in p


def test_module_js_has_the_seven_tabs_in_the_approved_order_and_no_removed_features():
    j = open("static/js/pricing_plans.js", encoding="utf-8").read()
    order = ["Store Plans", "Influencer Plans", "Banners & Ads", "Product Settings", "GST & VAT", "Countries & Currencies", "Reward Redemption Points"]
    pos = [j.index("'" + n + "'") for n in order]
    assert pos == sorted(pos)
    for banned in ("Bulk update", "Bulk Update", "Import / Export", "Countries Priced", "Active Plans", "Tax Configured", "Last Change", "razorpay", "Razorpay"):
        assert banned not in j
    t = open("templates/partials/pricing_plans.html", encoding="utf-8").read()
    for banned in ("Bulk", "Import", "Countries Priced", "Last Change"):
        assert banned not in t


# ── ISO validation (format alone is not enough) ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("code", ["ZZ", "QQ", "XX", "AA", "KX", "UK", "XK"])
def test_country_code_must_be_a_real_iso_3166_code(code):
    r = _add(code, name="Fictional " + code)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_country_code" and "ISO 3166" in r.json()["detail"]["message"]
    assert fake.pricing_countries.count_documents({}) == 0


@pytest.mark.parametrize("code", ["SA", "JO", "NP", "PH", "US", "AE", "KE", "GB", "EG"])
def test_real_iso_country_codes_are_accepted(code):
    assert _add(code, name="Country " + code).status_code == 200


@pytest.mark.parametrize("cur", ["XYZ", "ABC", "ZZZ", "AAA", "XXX", "XAU", "XTS", "XDR"])
def test_currency_code_must_be_a_real_iso_4217_currency(cur):
    r = _add(currency_code=cur)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_currency_code" and "ISO 4217" in r.json()["detail"]["message"]
    assert fake.pricing_countries.count_documents({}) == 0


@pytest.mark.parametrize("cur", ["INR", "SAR", "JOD", "NPR", "PHP", "USD", "EUR", "AED", "KES", "GBP"])
def test_real_iso_currency_codes_are_accepted_and_lowercase_is_normalised(cur):
    r = _add(currency_code=cur.lower())
    assert r.status_code == 200 and r.json()["country"]["currency_code"] == cur


def test_editing_a_country_cannot_set_a_fake_currency_code():
    _add()
    r = c.put(BASE + "/countries/SA", json={"currency_code": "QQQ"}, headers=SA_H)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_currency_code"
    assert fake.pricing_countries.find_one({"code": "SA"})["currency_code"] == "SAR"


def test_unknown_country_code_in_a_lookup_is_404_not_a_validation_error():
    assert c.get(BASE + "/countries/ZZ/prices", headers=SA_H).status_code == 404


def test_iso_lists_are_complete_enough_and_exclude_non_currencies():
    from utils import iso_codes as I
    assert len(I.COUNTRY_CODES) == 249 and {"IN", "SA", "JO", "NP", "PH", "US"} <= I.COUNTRY_CODES
    assert {"INR", "SAR", "JOD", "NPR", "PHP", "USD", "EUR"} <= I.CURRENCY_CODES and not ({"XAU", "XAG", "XXX", "XTS", "XDR"} & I.CURRENCY_CODES)


# ── clarifications: product window per country, reward flows untouched, phone list separate ──────────────────────────
def test_free_product_window_and_product_day_price_are_independent_per_country():
    _add()
    _add("JO", name="Jordan", currency_name="Jordanian Dinar", currency_code="JOD", currency_symbol="JD", tax_label="VAT", tax_percent=16)
    c.put(BASE + "/countries/SA/prices/product", json={"price_per_day": 1, "free_product_limit": 5, "product_max_validity_days": 90}, headers=SA_H)
    c.put(BASE + "/countries/JO/prices/product", json={"price_per_day": 0.15, "free_product_limit": 20, "product_max_validity_days": 180}, headers=SA_H)
    sa = c.get(BASE + "/countries/SA/prices", headers=SA_H).json()["product"]
    jo = c.get(BASE + "/countries/JO/prices", headers=SA_H).json()["product"]
    ind = c.get(BASE + "/countries/IN/prices", headers=SA_H).json()["product"]
    assert (sa["price"], sa["free_product_limit"]) == (1, 5) and (jo["price"], jo["free_product_limit"]) == (0.15, 20)
    assert (ind["price"], ind["free_product_limit"], ind["product_max_validity_days"]) == (10, 10, 365)
    assert c.put(BASE + "/countries/SA/prices/product", json={"free_product_limit": 7}, headers=SA_H).json()["section"]["free_product_limit"] == 7
    assert c.get(BASE + "/countries/JO/prices", headers=SA_H).json()["product"]["free_product_limit"] == 20
    assert _india_doc()["free_product_limit"] == 10


def test_a_country_never_inherits_the_india_free_window_or_day_price():
    _add()
    p = c.get(BASE + "/countries/SA/prices", headers=SA_H).json()["product"]
    assert p["configured"] is False and p["price"] is None and p["free_product_limit"] is None


def test_this_module_does_not_touch_wallet_withdraw_or_the_phone_country_list():
    import inspect
    from services import pricing_config as PC
    from utils import phone as ph
    before = sorted(x.iso for x in ph.SUPPORTED_COUNTRIES)
    _add("KE", name="Kenya", currency_name="Kenyan Shilling", currency_code="KES", currency_symbol="KSh", tax_label="VAT", tax_percent=16)
    assert sorted(x.iso for x in ph.SUPPORTED_COUNTRIES) == before and "KE" not in before
    src = inspect.getsource(PC)
    assert "withdraw_requests" not in src and "db.accounts" not in src and "db.users" not in src and "import razorpay" not in src and "razorpay_" not in src
    assert fake.withdraw_requests.count_documents({}) == 0
