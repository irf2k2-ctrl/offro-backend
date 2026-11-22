"""Enhancement batch, item 5: Dashboard > Accounts - Country in its own column and search by country name / ISO code.

Search is done by the BACKEND (Mongo query before paging), together with the role / city filters and pagination, and it only widens what the
search term matches: the dashboard user's own city and country scope (Stage 4) stays ANDed, so searching another country's name never
reveals its accounts to a restricted user.  A country name needs 3+ letters (any part of the name); 1-2 letters match an ISO code exactly.
(file name sorts after test_banner_media.py on purpose.)  Run:  python -m pytest test_country_search_in_accounts.py -q -W ignore"""
import os
import sys
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import server
from test_dashboard_country_access import H, mk_user

DB = sys.modules["database"].db
web = TestClient(server.app)
ROOT = os.path.dirname(os.path.abspath(server.__file__))
DASH = open(os.path.join(ROOT, "templates", "admin_dashboard.html"), encoding="utf-8").read()


@pytest.fixture
def world():
    """Names avoid the letters of the searched ISO codes on purpose, so a hit can only come from the country."""
    rows = [("Ravi", "9876543210", {}, "Ballari", ["user"]),                                    # India, legacy bare number, no phone_country
            ("Meera", "+919812300000", {"phone_country": "IN"}, "Mysuru", ["user", "merchant"]),   # India, phone_country
            ("Sami", "+966501234567", {"phone_country": "SA"}, "Riyadh", ["merchant"]),         # Saudi Arabia
            ("Nima", "+9779812345678", {}, "Kathmandu", ["user"]),                              # Nepal (from the number)
            ("Jana", "+962791234567", {"phone_country": "JO"}, "Amman", ["user"]),             # Jordan
            ("Pia", "+639171234567", {"phone_country": "PH"}, "Manila", ["user"]),             # Philippines (its name contains "in")
            ("Nobody", "", {}, "Ballari", ["user"])]                                            # no country can be told
    for name, phone, extra, city, roles in rows:
        DB.accounts.insert_one({"name": name, "phone": phone, "roles": roles, "status": "active", "city": city,
                                "created_at": datetime.utcnow(), **extra})
    mk_user("super", role="Super Admin")
    mk_user("sa-mgr", country="SA"); mk_user("in-mgr", country="IN"); mk_user("legacy-mgr")
    mk_user("bad-mgr", country="Atlantis")
    mk_user("ballari-in", country="IN", cities=("Ballari",))


def names(token, search="", **params):
    r = web.get("/admin/accounts", headers=H(token), params={"page": 1, "page_size": 100, "search": search, **params})
    assert r.status_code == 200, r.text
    return sorted(i["full_name"] for i in r.json()["items"])


def unpaged(token, search=""):
    r = web.get("/admin/accounts", headers=H(token), params={"search": search})
    assert r.status_code == 200, r.text
    return sorted(i["full_name"] for i in r.json())


class TestSearchAcrossNamePhoneAndCountry:
    def test_account_name_still_works(self, world):
        assert names("super", "ravi") == ["Ravi"] and names("super", "ME") == ["Meera"]            # partial, case-insensitive
        assert names("super", "Sami") == ["Sami"] and names("super", "zzz") == []

    def test_phone_still_works(self, world):
        assert names("super", "9876543210") == ["Ravi"]
        assert names("super", "+966501") == ["Sami"] and names("super", "5012") == ["Sami"]

    @pytest.mark.parametrize("term", ["India", "india", "INDIA", "ind", "Indi"])
    def test_country_name_finds_indian_accounts(self, world, term):
        assert names("super", term) == ["Meera", "Ravi"]

    @pytest.mark.parametrize("term,expected", [("Saudi", ["Sami"]), ("arabia", ["Sami"]), ("Nepal", ["Nima"]), ("Jordan", ["Jana"]),
                                               ("Philippines", ["Pia"]), ("Phil", ["Pia"])])
    def test_other_country_names(self, world, term, expected):
        assert names("super", term) == expected

    @pytest.mark.parametrize("term", ["IN", "in", "In"])
    def test_iso_code_finds_indian_accounts_and_not_substring_matches(self, world, term):
        assert names("super", term) == ["Meera", "Ravi"]            # not Pia (Philippines): two letters are an ISO code, never a name fragment

    def test_iso_code_is_exact_for_the_country_part(self, world):
        # Names are chosen so only the country can explain these hits.
        assert names("super", "SA") == ["Sami"] and names("super", "JO") == ["Jana"] and names("super", "NP") == ["Nima"]
        assert names("super", "PH") == ["Pia"]                      # 'Philippines' is NOT matched by the two letters of another country

    def test_two_letters_never_match_by_country_name_substring(self, world):
        # "ia" is inside "India" and "Saudi Arabia", yet only the account NAME (Pia) matches: 1-2 letters are an ISO code, never a name fragment
        assert names("super", "ia") == ["Pia"]

    def test_unknown_country_term_matches_nothing(self, world):
        assert names("super", "Kannada") == [] and names("super", "Atlantis") == [] and names("super", "ZZ") == []

    def test_special_characters_are_literal_and_safe(self, world):
        for t in ("(", ".*", "[a-z", "India)", "\\", "$ne"):
            assert names("super", t) == []

    def test_the_unpaged_list_is_unchanged(self, world):
        # Without `page` the endpoint returns the whole (scoped) list and has never applied `search` (other admin screens rely on that).
        assert len(unpaged("super", "India")) == len(unpaged("super", "")) == 7

    def test_country_fields_are_in_every_row_for_the_new_column(self, world):
        rows = {i["full_name"]: i for i in web.get("/admin/accounts", headers=H("super"), params={"page": 1, "page_size": 100}).json()["items"]}
        assert (rows["Ravi"]["country_iso"], rows["Ravi"]["country_name"]) == ("IN", "India")
        assert (rows["Sami"]["country_iso"], rows["Sami"]["country_name"]) == ("SA", "Saudi Arabia")
        assert rows["Nobody"]["country_iso"] == "" and rows["Nobody"]["full_name"] == "Nobody"      # name / phone fields unchanged
        assert rows["Ravi"]["mobile_number"] == "9876543210"


class TestTogetherWithFiltersAndPagination:
    def test_with_role_and_city_filters(self, world):
        assert names("super", "India", role="both") == ["Meera"]
        assert names("super", "India", role="user_only") == ["Ravi"]
        assert names("super", "India", city="Mysuru") == ["Meera"] and names("super", "India", city="Riyadh") == []

    def test_pagination_counts_the_country_matches(self, world):
        r = web.get("/admin/accounts", headers=H("super"), params={"page": 1, "page_size": 1, "search": "India"}).json()
        assert r["total"] == 2 and r["pages"] == 2 and len(r["items"]) == 1
        r2 = web.get("/admin/accounts", headers=H("super"), params={"page": 2, "page_size": 1, "search": "India"}).json()
        assert len(r2["items"]) == 1 and r2["items"][0]["account_id"] != r["items"][0]["account_id"]
        assert r2["total"] == 2

    def test_sorting_still_works_with_a_country_search(self, world):
        r = web.get("/admin/accounts", headers=H("super"), params={"page": 1, "page_size": 10, "search": "India", "sort": "created_asc"})
        assert r.status_code == 200 and r.json()["total"] == 2


class TestCountryRestrictionsAreNeverWeakened:
    def test_a_saudi_user_cannot_find_indian_accounts_by_any_search(self, world):
        for term in ("India", "india", "ind", "IN", "in", "Ravi", "Meera", "9876543210", "+91981", "Mysuru"):
            got = names("sa-mgr", term)
            assert not ({"Ravi", "Meera"} & set(got)), (term, got)
        assert names("sa-mgr", "India") == [] and names("sa-mgr", "IN") == []
        assert names("sa-mgr", "Saudi") == ["Sami"] and names("sa-mgr", "SA") == ["Sami"]
        assert names("sa-mgr", "") == ["Sami"]                                   # the plain list is the same as before

    def test_an_indian_user_cannot_find_saudi_accounts(self, world):
        assert names("in-mgr", "Saudi") == [] and names("in-mgr", "SA") == [] and names("in-mgr", "Sami") == []
        assert names("in-mgr", "India") == ["Meera", "Ravi"] and names("in-mgr", "") == ["Meera", "Ravi"]

    def test_paging_totals_do_not_leak_other_countries(self, world):
        r = web.get("/admin/accounts", headers=H("sa-mgr"), params={"page": 1, "page_size": 100, "search": "India"}).json()
        assert r["total"] == 0 and r["items"] == []
        assert unpaged("sa-mgr", "India") == unpaged("sa-mgr", "") == ["Sami"]       # the unpaged list ignores search but is still country-scoped

    def test_the_city_scope_still_applies_with_the_country_scope(self, world):
        assert names("ballari-in", "India") == ["Ravi"] and names("ballari-in", "Meera") == []

    def test_a_user_with_an_invalid_stored_country_still_fails_closed(self, world):
        for term in ("", "India", "Saudi", "IN", "Ravi"):
            assert names("bad-mgr", term) == []

    def test_legacy_users_without_a_country_and_super_admin_see_everything(self, world):
        assert names("legacy-mgr", "India") == ["Meera", "Ravi"] and names("super", "India") == ["Meera", "Ravi"]
        assert len(names("super", "")) == 7 == len(names("legacy-mgr", ""))


class TestTheCountryColumnInTheDashboardPage:
    def test_country_is_its_own_column_after_phone_and_the_name_cell_is_unchanged(self):
        assert "<th>Name</th><th>Phone</th><th>Country</th><th>Ref ID</th>" in DASH
        assert "<td><strong>${escHtml(a.full_name||'—')}</strong></td>" in DASH and "<td>${acctCountryHtml(a).trim() || '—'}</td>" in DASH
        assert DASH.count('colspan="13"') >= 2                                  # error / empty rows span the new column count
        assert "Name / Phone / Country" in DASH
