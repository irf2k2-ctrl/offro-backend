"""Stage 3: country mapping for Dashboard > Default Images (all categories + Service Unavailable): backend/API level.
Run:  python -m pytest test_default_images_country.py -q -W ignore
"""
import copy
import io
import sys
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import admin as ADM
from routers import public as PUB
from services import default_images as DI

DB = sys.modules["database"].db
app = FastAPI()
app.include_router(ADM.router, prefix="/admin")
app.include_router(PUB.router)
web = TestClient(app)

CATS = ["store", "product", "offer", "city", "merchant_banner"]
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture(autouse=True)
def _cloudinary_off(monkeypatch):
    for n in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
        monkeypatch.delenv(n, raising=False)


def user(token, role_name="Custom", perms=None, system=False):
    rid = DB.dashboard_roles.insert_one({"role_name": role_name, "permissions": perms or {}, "is_system_role": system}).inserted_id
    DB.dashboard_users.insert_one({"token": token, "role_id": rid, "full_name": token, "status": "active", "mobile": "9000000001",
                                   "last_active_at": datetime.utcnow()})
    return {"Authorization": "Bearer " + token}


def sa():
    return user("sa-token", "Super Admin", system=True)


def editor():
    return user("ed-token", "Settings Editor", {"Settings": {"view": True, "edit": True}})


def viewer():
    return user("vw-token", "Viewer", {"Settings": {"view": True}})


def img(n):
    return f"https://cdn.example/{n}.jpg"


def seed(**cats):
    doc = {"_type": "default_images"}
    doc.update(cats)
    DB.settings.insert_one(doc)


def add_url(h, cat, url, countries=None):
    body = {"type": cat, "url": url, "action": "add"}
    if countries is not None:
        body["countries"] = countries
    return web.put("/admin/default-images/urls", headers=h, json=body)


def upload(h, cat, countries=None, name="a.png"):
    data = {"countries": countries} if countries is not None else {}
    return web.put("/admin/default-images", headers=h, data=data, files={f"{cat}_file": (name, io.BytesIO(PNG + name.encode()), "image/png")})


def set_countries(h, cat, url, countries):
    return web.put("/admin/default-images/countries", headers=h, json={"type": cat, "url": url, "countries": countries})


def admin_get(h, country=None):
    r = web.get("/admin/default-images", headers=h, params={"country": country} if country is not None else None)
    assert r.status_code == 200, r.text
    return r.json()


def public(country=None):
    r = web.get("/default-images", params={"country": country} if country is not None else None)
    assert r.status_code == 200, r.text
    return r.json()


def urls_of(cat, data):
    v = data[cat]
    return v if isinstance(v, list) else ([v] if v else [])


def settings_doc():
    d = DB.settings.find_one({"_type": "default_images"}) or {}
    d.pop("_id", None)
    return copy.deepcopy(d)


# ── 1-2. upload an image mapped to one / several countries ─────────────────────────────────────────────────────────────
class TestUpload:
    @pytest.mark.parametrize("cat", CATS)
    def test_upload_mapped_to_one_country(self, cat):
        h = sa()
        r = upload(h, cat, "SA")
        assert r.status_code == 200 and r.json() == {"ok": True, "countries": ["SA"]}
        stored = settings_doc()[cat]
        assert len(stored) == 1 and stored[0].startswith("data:image/png;base64,")
        assert DB[DI.MAPPINGS].find_one({"category": cat})["countries"] == ["SA"]
        item = admin_get(h)["items"][cat][0]
        assert item["url"] == stored[0] and item["countries"] == ["SA"]

    @pytest.mark.parametrize("cat", CATS)
    def test_upload_mapped_to_several_countries(self, cat):
        h = sa()
        assert upload(h, cat, "SA, JO,NP").json()["countries"] == ["JO", "NP", "SA"]        # kept in master order
        for c in ("SA", "JO", "NP"):
            assert len(admin_get(h, c)["items"][cat]) == 1
        for c in ("IN", "PH"):
            assert admin_get(h, c)["items"][cat] == []

    def test_upload_without_countries_is_unchanged_and_global(self):
        h = sa()
        assert upload(h, "store").json() == {"ok": True, "countries": []}
        assert DB[DI.MAPPINGS].count_documents({}) == 0
        assert admin_get(h)["items"]["store"][0]["countries"] == []

    def test_uploads_append_never_replace(self):
        h = sa()
        upload(h, "store", "SA", "a.png"); upload(h, "store", "", "b.png"); upload(h, "store", "JO", "c.png")
        assert len(settings_doc()["store"]) == 3

    def test_invalid_or_duplicate_countries_reject_before_any_upload(self):
        h = sa()
        for bad in ("XX", "SA,XX", "SA,SA", "sa,SA", "USA", "91"):
            r = upload(h, "store", bad)
            assert r.status_code == 422, bad
        assert settings_doc() == {} and DB[DI.MAPPINGS].count_documents({}) == 0

    def test_legacy_single_string_field_is_converted_and_mapped(self):
        seed(store=img("old"))
        h = sa()
        upload(h, "store", "SA")
        stored = settings_doc()["store"]
        assert stored[0] == img("old") and len(stored) == 2
        assert admin_get(h, "global")["items"]["store"][0]["url"] == img("old")


# ── add by URL / edit / remove ─────────────────────────────────────────────────────────────────────────────────────────
class TestUrlsAndEdit:
    @pytest.mark.parametrize("cat", CATS)
    def test_add_url_with_countries_and_edit_them(self, cat):
        h = sa()
        assert add_url(h, cat, img("a"), ["SA"]).json() == {"ok": True, "countries": ["SA"]}
        assert set_countries(h, cat, img("a"), ["SA", "JO"]).json()["countries"] == ["JO", "SA"]
        assert [i["countries"] for i in admin_get(h, "JO")["items"][cat]] == [["JO", "SA"]]
        assert set_countries(h, cat, img("a"), ["NP"]).json()["countries"] == ["NP"]
        assert admin_get(h, "SA")["items"][cat] == [] and len(admin_get(h, "NP")["items"][cat]) == 1
        assert set_countries(h, cat, img("a"), []).json()["countries"] == []                   # back to All countries
        assert DB[DI.MAPPINGS].count_documents({}) == 0 and settings_doc()[cat] == [img("a")]  # the image itself is untouched

    def test_edit_validates_and_never_touches_the_image(self):
        h = sa()
        add_url(h, "store", img("a"), ["SA"])
        before = settings_doc()
        for bad in (["XX"], ["SA", "SA"], "SA,SA", [1], {"SA": 1}):
            assert set_countries(h, "store", img("a"), bad).status_code == 422, bad
        assert settings_doc() == before and DB[DI.MAPPINGS].find_one({})["countries"] == ["SA"]
        assert set_countries(h, "store", img("missing"), ["SA"]).status_code == 404
        assert set_countries(h, "bogus", img("a"), ["SA"]).status_code == 400
        assert set_countries(h, "store", "", ["SA"]).status_code == 404

    def test_edit_is_per_category(self):
        h = sa()
        add_url(h, "store", img("same"), ["SA"]); add_url(h, "offer", img("same"), ["JO"])
        assert admin_get(h, "SA")["items"]["offer"] == [] and len(admin_get(h, "JO")["items"]["offer"]) == 1
        assert set_countries(h, "store", img("same"), ["NP"]).status_code == 200
        assert admin_get(h, "JO")["items"]["offer"][0]["countries"] == ["JO"]

    def test_lowercase_codes_are_normalised(self):
        h = sa()
        assert add_url(h, "store", img("a"), ["sa", " jo "]).json()["countries"] == ["JO", "SA"]

    def test_removing_an_image_removes_its_mapping_and_a_re_added_url_is_global(self):
        h = sa()
        add_url(h, "store", img("a"), ["SA"])
        web.put("/admin/default-images/urls", headers=h, json={"type": "store", "url": img("a"), "action": "remove"})
        assert settings_doc()["store"] == [] and DB[DI.MAPPINGS].count_documents({}) == 0
        add_url(h, "store", img("a"))
        assert admin_get(h)["items"]["store"][0]["countries"] == []

    def test_adding_the_same_url_twice_does_not_duplicate_the_image(self):
        h = sa()
        add_url(h, "store", img("a"), ["SA"]); add_url(h, "store", img("a"), ["JO"])
        assert settings_doc()["store"] == [img("a")]
        assert admin_get(h)["items"]["store"][0]["countries"] == ["JO"]                      # the explicit new choice wins
        add_url(h, "store", img("a"))
        assert admin_get(h)["items"]["store"][0]["countries"] == ["JO"]                      # adding again without countries changes nothing

    def test_invalid_add_stores_nothing(self):
        h = sa()
        for bad in (["XX"], ["SA", "SA"], "nonsense,,,XX"):
            assert add_url(h, "store", img("a"), bad).status_code == 422
        assert settings_doc() == {} and DB[DI.MAPPINGS].count_documents({}) == 0


# ── 4-6. filter in the dashboard list ──────────────────────────────────────────────────────────────────────────────────
class TestAdminFilter:
    def setup_data(self, h):
        for cat in CATS:
            add_url(h, cat, img(cat + "_sa"), ["SA"])
            add_url(h, cat, img(cat + "_multi"), ["SA", "JO"])
            add_url(h, cat, img(cat + "_global"))

    @pytest.mark.parametrize("cat", CATS)
    def test_filter_by_country_global_and_all(self, cat):
        h = sa(); self.setup_data(h)
        assert urls_of(cat, admin_get(h, "SA")) == [img(cat + "_sa"), img(cat + "_multi")]
        assert urls_of(cat, admin_get(h, "JO")) == [img(cat + "_multi")]                       # multi-country image shows for each country
        assert urls_of(cat, admin_get(h, "IN")) == []
        assert urls_of(cat, admin_get(h, "global")) == [img(cat + "_global")]
        assert len(urls_of(cat, admin_get(h, "all"))) == 3 == len(urls_of(cat, admin_get(h)))   # no filter = everything, as before

    def test_filter_value_is_validated_and_case_insensitive(self):
        h = sa(); self.setup_data(h)
        assert admin_get(h, "sa")["filter"] == "SA" and admin_get(h, "ALL")["filter"] == "all" and admin_get(h, "Global")["filter"] == "global"
        for bad in ("XX", "India", "SA,JO"):
            assert web.get("/admin/default-images", headers=h, params={"country": bad}).status_code == 422

    def test_response_keeps_the_existing_shape(self):
        h = sa(); self.setup_data(h)
        d = admin_get(h)
        for k in CATS:
            assert isinstance(d[k], list) and all(isinstance(u, str) for u in d[k])
        for k in ("merchant_banner_explore_url", "no_service_url", "no_service_title", "no_service_message"):
            assert k in d
        assert [c["iso"] for c in d["countries"]] == ["IN", "JO", "NP", "PH", "SA"]            # the one country master

    def test_empty_database_response(self):
        d = admin_get(sa())
        assert d["store"] == [] and d["no_service_url"] == "" and d["items"] == {c: [] for c in CATS} and d["no_service_variants"] == []


# ── 5. Service Unavailable ────────────────────────────────────────────────────────────────────────────────────────────
class TestServiceUnavailable:
    def base(self):
        seed(no_service_url=img("ns_base"), no_service_title="No stores", no_service_message="Soon")

    def variant(self, h, countries, **kw):
        body = {"url": img("ns_sa"), "title": "SA title", "message": "SA message", "countries": countries}
        body.update(kw)
        return web.post("/admin/default-images/no-service-variants", headers=h, json=body)

    def test_create_filter_and_list_variants(self):
        h = sa(); self.base()
        r = self.variant(h, ["SA", "JO"])
        assert r.status_code == 200 and r.json()["variant"]["countries"] == ["JO", "SA"]
        assert len(admin_get(h)["no_service_variants"]) == 1
        assert len(admin_get(h, "SA")["no_service_variants"]) == 1 and len(admin_get(h, "JO")["no_service_variants"]) == 1
        assert admin_get(h, "IN")["no_service_variants"] == [] and admin_get(h, "global")["no_service_variants"] == []

    def test_public_returns_the_country_specific_screen_and_falls_back_to_the_default(self):
        h = sa(); self.base(); self.variant(h, ["SA", "JO"])
        for c in ("SA", "JO", "sa"):
            d = public(c)
            assert (d["no_service_url"], d["no_service_title"], d["no_service_message"]) == (img("ns_sa"), "SA title", "SA message")
            assert d["country_specific"]["no_service"] is True
        for c in ("IN", "PH", "NP", None, "", "ZZ"):
            d = public(c)
            assert (d["no_service_url"], d["no_service_title"], d["no_service_message"]) == (img("ns_base"), "No stores", "Soon")
            assert d["country_specific"]["no_service"] is False

    def test_blank_variant_text_uses_the_default_text(self):
        h = sa(); self.base(); self.variant(h, ["SA"], title="", message="")
        d = public("SA")
        assert d["no_service_url"] == img("ns_sa") and d["no_service_title"] == "No stores" and d["no_service_message"] == "Soon"

    def test_variant_works_even_with_no_default_configured(self):
        h = sa(); self.variant(h, ["SA"])
        assert public("SA")["no_service_url"] == img("ns_sa")
        assert public("IN") == {"store": "", "product": "", "offer": "", "city": "", "merchant_banner": "", "merchant_banner_explore_url": "",
                                "no_service_url": "", "no_service_title": "", "no_service_message": ""}

    def test_update_and_delete_variant(self):
        h = sa(); self.base()
        vid = self.variant(h, ["SA"]).json()["variant"]["id"]
        r = web.put(f"/admin/default-images/no-service-variants/{vid}", headers=h, json={"countries": ["NP"], "title": "NP title"})
        assert r.status_code == 200 and r.json()["variant"]["countries"] == ["NP"] and r.json()["variant"]["title"] == "NP title"
        assert public("SA")["no_service_url"] == img("ns_base") and public("NP")["no_service_title"] == "NP title"
        assert web.delete(f"/admin/default-images/no-service-variants/{vid}", headers=h).status_code == 200
        assert public("NP")["no_service_url"] == img("ns_base")
        assert web.delete(f"/admin/default-images/no-service-variants/{vid}", headers=h).status_code == 404
        assert web.put("/admin/default-images/no-service-variants/nope", headers=h, json={"title": "x"}).status_code == 404

    def test_validation(self):
        h = sa(); self.base()
        assert self.variant(h, []).status_code == 422 and self.variant(h, None).status_code == 422          # at least one country
        assert self.variant(h, ["XX"]).status_code == 422 and self.variant(h, ["SA", "SA"]).status_code == 422
        assert self.variant(h, ["SA"], url="").status_code == 422 and self.variant(h, ["SA"], url="javascript:alert(1)").status_code == 422
        assert self.variant(h, ["SA"], title="x" * 121).status_code == 422
        assert DB[DI.VARIANTS].count_documents({}) == 0
        vid = self.variant(h, ["SA"]).json()["variant"]["id"]
        assert web.put(f"/admin/default-images/no-service-variants/{vid}", headers=h, json={"countries": ["SA", "SA"]}).status_code == 422
        assert web.put(f"/admin/default-images/no-service-variants/{vid}", headers=h, json={"countries": []}).status_code == 422
        assert DB[DI.VARIANTS].find_one({})["countries"] == ["SA"]

    def test_existing_single_config_endpoints_are_unchanged(self):
        h = sa()
        assert web.put("/admin/default-images/no-service", headers=h, json={"url": img("x"), "title": "T", "message": "M"}).status_code == 200
        r = web.post("/admin/default-images/no-service", headers=h, data={"no_service_url": img("y"), "no_service_title": "T2", "no_service_message": "M2"})
        assert r.status_code == 200
        d = admin_get(h)
        assert (d["no_service_url"], d["no_service_title"], d["no_service_message"]) == (img("y"), "T2", "M2")
        assert public()["no_service_url"] == img("y") and DB[DI.VARIANTS].count_documents({}) == 0

    def test_first_matching_variant_is_used_deterministically(self):
        h = sa(); self.base()
        self.variant(h, ["SA"], url=img("first"), title="first")
        self.variant(h, ["SA", "JO"], url=img("second"), title="second")
        assert public("SA")["no_service_url"] == img("first") and public("JO")["no_service_url"] == img("second")


# ── 11. retrieval: the correct country-specific image ──────────────────────────────────────────────────────────────────
class TestRetrieval:
    def data(self):
        h = sa()
        for cat in CATS:
            add_url(h, cat, img(cat + "_g1")); add_url(h, cat, img(cat + "_g2"))
            add_url(h, cat, img(cat + "_sa"), ["SA"]); add_url(h, cat, img(cat + "_sajo"), ["SA", "JO"])
        return h

    @pytest.mark.parametrize("cat", ["product", "city", "merchant_banner"])
    def test_most_specific_images_win_and_global_is_the_fallback(self, cat):
        self.data()
        assert urls_of(cat, public("SA")) == [img(cat + "_sa"), img(cat + "_sajo")]
        assert urls_of(cat, public("JO")) == [img(cat + "_sajo")]
        for c in ("IN", "NP", "PH"):                                                           # no image mapped to them: the global ones
            assert urls_of(cat, public(c)) == [img(cat + "_g1"), img(cat + "_g2")]
        assert public("SA")["country_specific"][cat] is True and public("IN")["country_specific"][cat] is False

    @pytest.mark.parametrize("cat", ["store", "offer"])
    def test_specific_flag_for_single_image_categories(self, cat):
        self.data()
        assert public("SA")["country_specific"][cat] is True and public("IN")["country_specific"][cat] is False

    @pytest.mark.parametrize("cat", ["store", "offer"])
    def test_single_image_categories_return_the_first_matching_image(self, cat):
        self.data()
        assert public("SA")[cat] == img(cat + "_sa") and public("JO")[cat] == img(cat + "_sajo") and public("IN")[cat] == img(cat + "_g1")

    def test_no_country_or_unknown_country_gets_only_global_images(self):
        self.data()
        for c in (None, "", "ZZ", "XX", "India", "SA,JO", "<script>"):
            d = public(c)
            assert d["country"] == "" and d["product"] == [img("product_g1"), img("product_g2")] and d["store"] == img("store_g1")
            assert not any("_sa" in u for cat in CATS for u in urls_of(cat, d))

    def test_country_code_is_case_insensitive(self):
        self.data()
        assert public("sa")["country"] == "SA" and public("sa")["store"] == img("store_sa")

    def test_a_category_mapped_only_to_other_countries_is_empty_for_this_one(self):
        h = sa(); add_url(h, "city", img("only_sa"), ["SA"])
        assert public("SA")["city"] == [img("only_sa")] and public("IN")["city"] == [] and public()["city"] == []   # the app's own fallback applies

    def test_every_country_in_the_master_can_be_targeted(self):
        h = sa()
        for c in ("IN", "JO", "NP", "PH", "SA"):
            add_url(h, "product", img("p_" + c), [c])
        for c in ("IN", "JO", "NP", "PH", "SA"):
            assert public(c)["product"] == [img("p_" + c)]

    def test_explore_flag_is_hidden_when_its_media_is_not_shown(self):
        h = sa()
        add_url(h, "merchant_banner", img("b_sa"), ["SA"]); add_url(h, "merchant_banner", img("b_g"))
        assert web.put("/admin/default-images/merchant-banner-explore", headers=h, json={"url": img("b_sa")}).status_code == 200
        assert public("SA")["merchant_banner_explore_url"] == img("b_sa")
        assert public("IN")["merchant_banner_explore_url"] == "" and public()["merchant_banner_explore_url"] == ""
        assert admin_get(h)["merchant_banner_explore_url"] == img("b_sa")

    def test_https_only_rule_for_single_image_categories_still_applies(self):
        h = sa(); add_url(h, "store", "https://cdn.example/ok.jpg", ["SA"])
        DB.settings.update_one({"_type": "default_images"}, {"$push": {"store": "data:image/png;base64,AAAA"}})
        assert public("SA")["store"] == "https://cdn.example/ok.jpg"


# ── 7. existing images without a country mapping ──────────────────────────────────────────────────────────────────────
class TestLegacyData:
    def legacy(self):
        seed(store=[img("s1"), img("s2")], product=[img("p1")], offer=img("o_single"), city=[img("c1")], merchant_banner=[img("m1")],
             merchant_banner_explore_url=img("m1"), no_service_url=img("ns"), no_service_title="T", no_service_message="M")

    def test_unmapped_images_are_visible_to_every_country_and_to_old_apps(self):
        self.legacy()
        for c in (None, "SA", "IN", "JO", "NP", "PH", "ZZ"):
            d = public(c)
            assert d["store"] == img("s1") and d["product"] == [img("p1")] and d["offer"] == img("o_single") and d["city"] == [img("c1")]
            assert d["merchant_banner"] == [img("m1")] and d["merchant_banner_explore_url"] == img("m1")
            assert (d["no_service_url"], d["no_service_title"], d["no_service_message"]) == (img("ns"), "T", "M")

    def test_public_response_without_mappings_is_the_old_response_plus_two_additive_keys(self):
        self.legacy()
        d = public()
        old_keys = {"store", "product", "offer", "city", "merchant_banner", "merchant_banner_explore_url", "no_service_url", "no_service_title", "no_service_message"}
        assert set(d) == old_keys | {"country", "country_specific"}
        assert d["country"] == "" and not any(d["country_specific"].values())

    def test_public_with_no_default_images_configured_is_unchanged(self):
        d = public("SA")
        assert d == {"store": "", "product": "", "offer": "", "city": "", "merchant_banner": "", "merchant_banner_explore_url": "",
                     "no_service_url": "", "no_service_title": "", "no_service_message": ""}

    def test_admin_lists_legacy_images_as_all_countries_and_in_the_global_filter(self):
        self.legacy(); h = sa()
        d = admin_get(h)
        assert d["offer"] == [img("o_single")] and all(i["countries"] == [] for cat in CATS for i in d["items"][cat])
        g = admin_get(h, "global")
        assert g["store"] == [img("s1"), img("s2")] and g["offer"] == [img("o_single")] and g["merchant_banner"] == [img("m1")]
        assert admin_get(h, "SA")["store"] == []

    def test_mapping_one_image_does_not_hide_the_others(self):
        self.legacy(); h = sa()
        set_countries(h, "store", img("s1"), ["SA"])
        assert public("SA")["store"] == img("s1")                    # mapped image wins for SA
        assert public("IN")["store"] == img("s2")                    # everyone else still sees the unmapped one
        assert public()["store"] == img("s2")
        assert admin_get(h)["store"] == [img("s1"), img("s2")]       # nothing was removed

    def test_no_data_is_changed_by_reading_or_by_mapping(self):
        self.legacy(); h = sa()
        before = settings_doc()
        for c in (None, "SA", "IN"):
            public(c); admin_get(h, c)
        set_countries(h, "store", img("s1"), ["SA"]); set_countries(h, "store", img("s1"), [])
        assert settings_doc() == before and DB[DI.MAPPINGS].count_documents({}) == 0

    def test_two_new_collections_only(self):
        h = sa(); self.legacy()
        add_url(h, "store", img("n"), ["SA"]); web.post("/admin/default-images/no-service-variants", headers=h,
                                                        json={"url": img("v"), "countries": ["SA"]})
        assert {DI.MAPPINGS, DI.VARIANTS} <= set(DB.list_collection_names())


# ── 10. existing APIs keep working ─────────────────────────────────────────────────────────────────────────────────────
class TestBackwardCompatibleApis:
    def test_old_clients_calls_without_countries_still_work(self):
        h = sa()
        assert web.put("/admin/default-images/urls", headers=h, json={"type": "store", "url": img("a"), "action": "add"}).json() == {"ok": True, "countries": []}
        assert web.put("/admin/default-images/urls", headers=h, json={"type": "store", "url": img("a"), "action": "remove"}).json()["ok"] is True
        assert upload(h, "product").status_code == 200
        assert web.put("/admin/default-images/urls", headers=h, json={"type": "bogus", "url": img("a")}).status_code == 400

    def test_explore_flag_endpoint_unchanged(self):
        h = sa(); add_url(h, "merchant_banner", img("m"))
        assert web.put("/admin/default-images/merchant-banner-explore", headers=h, json={"url": img("m")}).json()["merchant_banner_explore_url"] == img("m")
        assert web.put("/admin/default-images/merchant-banner-explore", headers=h, json={"url": img("not-there")}).status_code == 400
        web.put("/admin/default-images/urls", headers=h, json={"type": "merchant_banner", "url": img("m"), "action": "remove"})
        assert admin_get(h)["merchant_banner_explore_url"] == ""

    def test_old_admin_get_works_without_any_argument(self):
        seed(store=[img("a")])
        assert web.get("/admin/default-images", headers=sa()).json()["store"] == [img("a")]


# ── 12. permissions ───────────────────────────────────────────────────────────────────────────────────────────────────
class TestPermissions:
    WRITES = [
        ("put", "/admin/default-images/countries", {"type": "store", "url": "u", "countries": ["SA"]}),
        ("post", "/admin/default-images/no-service-variants", {"url": "https://x/y.jpg", "countries": ["SA"]}),
        ("put", "/admin/default-images/no-service-variants/abc", {"title": "x"}),
        ("delete", "/admin/default-images/no-service-variants/abc", None),
        ("put", "/admin/default-images/urls", {"type": "store", "url": "https://x/y.jpg", "action": "add", "countries": ["SA"]}),
    ]

    def call(self, method, url, body, headers=None):
        kw = {"headers": headers or {}}
        if body is not None:
            kw["json"] = body
        return getattr(web, method)(url, **kw)

    @pytest.mark.parametrize("method,url,body", WRITES)
    def test_no_login_is_refused(self, method, url, body):
        assert self.call(method, url, body).status_code in (401, 403)

    @pytest.mark.parametrize("method,url,body", WRITES)
    def test_a_role_without_settings_edit_cannot_change_mappings(self, method, url, body):
        h = viewer()
        assert self.call(method, url, body, h).status_code == 403
        assert DB[DI.MAPPINGS].count_documents({}) == 0 and DB[DI.VARIANTS].count_documents({}) == 0 and DB.settings.count_documents({}) == 0

    def test_upload_with_countries_needs_settings_edit(self):
        r = upload(viewer(), "store", "SA")
        assert r.status_code == 403 and DB.settings.count_documents({}) == 0
        assert web.put("/admin/default-images", files={"store_file": ("a.png", io.BytesIO(PNG), "image/png")}).status_code in (401, 403)

    def test_settings_editor_and_super_admin_can_map(self):
        for h in (editor(), sa()):
            assert add_url(h, "store", img("x" + str(len(h["Authorization"]))), ["SA"]).status_code == 200
            assert upload(h, "offer", "JO", name=h["Authorization"] + ".png").status_code == 200

    def test_a_renamed_super_admin_role_without_permissions_is_not_enough(self):
        h = user("fake", "Super Admin", perms={}, system=False)
        # the existing rule (role_name == "Super Admin") is the platform's; a role with another name and no permission is refused
        h2 = user("plain", "Support", perms={})
        assert add_url(h2, "store", img("a"), ["SA"]).status_code == 403

    def test_reading_follows_the_existing_rule_and_never_exposes_more(self):
        seed(store=[img("a")])
        assert web.get("/admin/default-images").status_code in (401, 403)
        assert admin_get(viewer())["store"] == [img("a")]                              # unchanged: any signed-in dashboard user could read
        d = web.get("/default-images").json()                                          # public endpoint: no login, no admin data
        assert "countries" not in d and "items" not in d and "no_service_variants" not in d


# ── data safety / environment ─────────────────────────────────────────────────────────────────────────────────────────
class TestSafety:
    def test_module_has_no_cross_environment_or_network_access(self):
        import inspect
        src = inspect.getsource(DI)
        for forbidden in ("requests", "httpx", "urllib", "MONGO_URL", "os.environ", "drop(", "delete_many", "remove("):
            assert forbidden not in src

    def test_mappings_live_in_this_database_only(self):
        h = sa(); add_url(h, "store", img("a"), ["SA"])
        assert DB[DI.MAPPINGS].count_documents({}) == 1
        DB.drop_collection(DI.MAPPINGS)                                                # another environment's (empty) database
        assert public("SA")["store"] == img("a") and public("SA")["country_specific"]["store"] is False

    def test_indexes_are_created_safely(self):
        assert DI.ensure_indexes_safe() is True
        add_url(sa(), "store", img("a"), ["SA"])
        with pytest.raises(Exception):
            DB[DI.MAPPINGS].insert_one({"category": "store", "url_key": DI.url_key(img("a")), "countries": ["JO"]})

    def test_country_master_is_the_only_source_of_countries(self):
        from utils import phone
        assert DI.clean_countries([c.iso for c in phone.SUPPORTED_COUNTRIES]) == [c.iso for c in phone.SUPPORTED_COUNTRIES]
        with pytest.raises(DI.DefaultImageError):
            DI.clean_countries(["US"])
