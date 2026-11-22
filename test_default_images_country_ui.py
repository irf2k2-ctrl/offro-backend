"""Stage 3: the Default Images dashboard (country selector near uploads, country filter, edit countries, Service Unavailable screens) -
the real dashboard page and JS in a headless browser, against mocked API responses.
Run:  python -m pytest test_default_images_country_ui.py -q -W ignore"""
import json
import re

import pytest
from fastapi.testclient import TestClient

import server
from translation_test_support import DB

pw = pytest.importorskip("playwright.sync_api")
main_client = TestClient(server.app)
CATS = ["store", "product", "offer", "city", "merchant_banner"]
COUNTRIES = [{"iso": "IN", "name": "India", "dial": "91", "flag": "🇮🇳"}, {"iso": "JO", "name": "Jordan", "dial": "962", "flag": "🇯🇴"},
             {"iso": "NP", "name": "Nepal", "dial": "977", "flag": "🇳🇵"}, {"iso": "PH", "name": "Philippines", "dial": "63", "flag": "🇵🇭"},
             {"iso": "SA", "name": "Saudi Arabia", "dial": "966", "flag": "🇸🇦"}]
U = lambda n: f"https://cdn.example/{n}.jpg"
ITEMS = {c: [{"url": U(c + "_sa"), "countries": ["SA", "JO"]}, {"url": U(c + "_g"), "countries": []}] for c in CATS}


def payload(flt="all"):
    items = {}
    for c in CATS:
        items[c] = [i for i in ITEMS[c] if flt == "all" or (flt == "global" and not i["countries"]) or (flt not in ("all", "global") and flt in i["countries"])]
    d = {c: [i["url"] for i in items[c]] for c in CATS}
    d.update({"items": items, "filter": flt, "countries": COUNTRIES, "merchant_banner_explore_url": "", "no_service_url": U("ns"),
              "no_service_title": "No stores", "no_service_message": "Soon",
              "no_service_variants": [] if flt in ("global", "IN", "NP", "PH") else
              [{"id": "v1", "url": U("ns_sa"), "title": "SA title", "message": "SA msg", "countries": ["SA", "JO"]}]})
    return d


def dashboard():
    DB.dashboard_users.insert_one({"token": "ui-token", "status": "active", "full_name": "Test Admin", "mobile": "9000000009"})
    r = main_client.get("/admin/dashboard", cookies={"admin_token": "ui-token"})
    assert r.status_code == 200
    return r.text


def open_page(play, html, handler=None):
    b = play.chromium.launch(); pg = b.new_page(viewport={"width": 1280, "height": 900}); calls = []

    def route(rt):
        req = rt.request; url = req.url
        if url == "http://t.test/dash":
            return rt.fulfill(body=html, content_type="text/html")
        if not url.startswith("http://t.test/"):
            return rt.abort()
        path = url.split("http://t.test", 1)[1]
        if path.startswith("/static/"):
            try:
                body = open(server.__file__.rsplit("/", 1)[0] + path.split("?")[0], "rb").read()
            except OSError:
                return rt.fulfill(status=404, body="")
            return rt.fulfill(body=body, content_type="text/css" if path.endswith(".css") else "application/javascript")
        pd = req.post_data_buffer; calls.append((req.method, path, pd.decode('utf-8', 'replace') if pd else None))
        if handler:
            res = handler(req, path)
            if res is not None:
                return rt.fulfill(**res)
        if path.startswith("/admin/default-images") and req.method == "GET":
            flt = re.search(r"country=([^&]*)", path)
            return rt.fulfill(body=json.dumps(payload(flt.group(1) if flt else "all")), content_type="application/json")
        return rt.fulfill(status=200, body="[]", content_type="application/json")
    pg.route("**/*", route)
    pg.goto("http://t.test/dash")
    pg.evaluate("showSection('default_images', null)")
    pg.wait_for_selector("#section-default_images", state="visible")
    pg.wait_for_function("document.querySelectorAll('#di_store_countries input').length === 5")
    return b, pg, calls


@pytest.fixture
def html():
    return dashboard()


def test_selector_sits_next_to_every_upload_and_lists_only_the_country_master(html):
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html)
        for t in CATS + ["nsv"]:
            labels = pg.locator(f"#di_{t}_countries label").all_inner_texts()
            assert [l.strip().split(" ", 1)[1] for l in labels] == ["India", "Jordan", "Nepal", "Philippines", "Saudi Arabia"], t
        # the selector comes right before the upload row of each card (same card, above the URL / file inputs)
        for t in CATS:
            assert pg.evaluate(f"""() => {{ const box = document.getElementById('di_{t}_countries'), up = document.getElementById('di_{t}_url_new');
                const card = box.closest('.card'); return card === up.closest('.card') && (box.compareDocumentPosition(up) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0; }}""")
        assert "all countries" in pg.inner_text("#di_store_countries + .di-country-hint")
        b.close()


def test_images_show_their_countries_and_all_countries_when_unmapped(html):
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html)
        for t in CATS:
            rows = pg.locator(f"#di_{t}_body tr")
            assert rows.count() == 2
            assert "Jordan" in rows.nth(0).inner_text() and "Saudi Arabia" in rows.nth(0).inner_text()
            assert "All countries" in rows.nth(1).inner_text()
            assert rows.nth(0).locator("button[title='Edit countries']").count() == 1
        b.close()


def test_country_filter_requests_the_backend_and_shows_only_that_countrys_images(html):
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html)
        opts = pg.locator("#di_country_filter option").all_inner_texts()
        assert opts[0].startswith("All images") and opts[1].startswith("Images for all countries") and any("Saudi Arabia" in o for o in opts)
        pg.select_option("#di_country_filter", "SA")
        pg.wait_for_function("document.querySelectorAll('#di_store_body tr').length === 1 && document.querySelector('#di_store_body').innerText.includes('Saudi')")
        assert any("/admin/default-images?country=SA" in c[1] for c in calls)
        for t in CATS:
            assert pg.locator(f"#di_{t}_body tr").count() == 1
        pg.select_option("#di_country_filter", "IN")
        pg.wait_for_function("document.querySelector('#di_store_body').innerText.includes('No images yet')")
        pg.select_option("#di_country_filter", "global")
        pg.wait_for_function("document.querySelector('#di_store_body').innerText.includes('All countries')")
        assert pg.locator("#di_store_body tr").count() == 1
        b.close()


def test_add_url_and_upload_send_the_selected_countries_and_reset_the_selection(html):
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html)
        pg.check("#di_store_countries input[value=SA]"); pg.check("#di_store_countries input[value=JO]")
        pg.fill("#di_store_url_new", "https://cdn.example/new.jpg"); pg.click("button[onclick=\"diAddUrl('store')\"]")
        pg.wait_for_function("document.querySelectorAll('#di_store_countries input:checked').length === 0")
        sent = [c for c in calls if c[0] == "PUT" and c[1] == "/admin/default-images/urls"]
        assert json.loads(sent[-1][2]) == {"type": "store", "url": "https://cdn.example/new.jpg", "action": "add", "countries": ["JO", "SA"]}
        pg.check("#di_offer_countries input[value=NP]")
        pg.set_input_files("#di_offer_file_new", {"name": "a.png", "mimeType": "image/png", "buffer": b"\x89PNG0000"})
        pg.click("button[onclick=\"diUploadFile('offer')\"]")
        pg.wait_for_function("document.querySelectorAll('#di_offer_countries input:checked').length === 0")
        up = [c for c in calls if c[0] == "PUT" and c[1] == "/admin/default-images"][-1]
        assert 'name="countries"' in up[2] and "NP" in up[2]
        pg.check("#di_merchant_banner_countries input[value=PH]")
        pg.set_input_files("#di_merchant_banner_file_new", {"name": "b.png", "mimeType": "image/png", "buffer": b"\x89PNG1111"})
        pg.click("#di_merchant_banner_upload_btn")
        pg.wait_for_function("document.querySelectorAll('#di_merchant_banner_countries input:checked').length === 0")
        up = [c for c in calls if c[0] == "PUT" and c[1] == "/admin/default-images"][-1]
        assert "PH" in up[2] and "merchant_banner_file" in up[2]
        b.close()


def test_nothing_selected_means_all_countries(html):
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html)
        pg.fill("#di_city_url_new", "https://cdn.example/c.jpg"); pg.click("button[onclick=\"diAddUrl('city')\"]")
        pg.wait_for_function("document.querySelector('#di_msg').innerText.includes('Added')")
        sent = [c for c in calls if c[0] == "PUT" and c[1] == "/admin/default-images/urls"][-1]
        assert json.loads(sent[2])["countries"] == []
        b.close()


def test_edit_countries_dialog_saves_through_the_api(html):
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html)
        pg.locator("#di_product_body tr").nth(0).locator("button[title='Edit countries']").click()
        pg.wait_for_selector("#di_country_modal")
        assert pg.is_checked("#di_cm_boxes input[value=SA]") and pg.is_checked("#di_cm_boxes input[value=JO]") and not pg.is_checked("#di_cm_boxes input[value=IN]")
        pg.uncheck("#di_cm_boxes input[value=SA]"); pg.check("#di_cm_boxes input[value=NP]")
        pg.click("#di_cm_save"); pg.wait_for_selector("#di_country_modal", state="detached")
        put = [c for c in calls if c[0] == "PUT" and c[1] == "/admin/default-images/countries"][-1]
        assert json.loads(put[2]) == {"type": "product", "url": U("product_sa"), "countries": ["JO", "NP"]}
        b.close()


def test_server_validation_errors_are_shown_in_the_dialog(html):
    def handler(req, path):
        if req.method == "PUT" and path == "/admin/default-images/countries":
            return {"status": 422, "body": json.dumps({"detail": {"code": "duplicate_country", "message": "SA was selected more than once"}}), "content_type": "application/json"}
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html, handler)
        pg.locator("#di_store_body tr").nth(0).locator("button[title='Edit countries']").click()
        pg.click("#di_cm_save")
        pg.wait_for_function("document.querySelector('#di_cm_err').innerText.includes('more than once')")
        assert pg.locator("#di_country_modal").count() == 1                       # stays open so nothing is lost
        b.close()


def test_service_unavailable_default_and_country_specific_screens(html):
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html)
        assert pg.input_value("#di_no_service_title") == "No stores"
        row = pg.locator("#di_ns_var_body tr").nth(0)
        assert "SA title" in row.inner_text() and "Saudi Arabia" in row.inner_text() and "Jordan" in row.inner_text()
        # add one: needs a country
        pg.fill("#di_nsv_url", U("new_ns")); pg.fill("#di_nsv_title", "NP title")
        pg.click("button[onclick='diAddNsVariant()']")
        assert not any(c[0] == "POST" and c[1].endswith("no-service-variants") for c in calls)
        pg.check("#di_nsv_countries input[value=NP]"); pg.click("button[onclick='diAddNsVariant()']")
        pg.wait_for_function("document.querySelectorAll('#di_nsv_countries input:checked').length === 0")
        post = [c for c in calls if c[0] == "POST" and c[1] == "/admin/default-images/no-service-variants"][-1]
        assert json.loads(post[2]) == {"url": U("new_ns"), "title": "NP title", "message": "", "countries": ["NP"]}
        # edit countries / delete
        pg.locator("#di_ns_var_body tr").nth(0).locator("text=Edit").click()
        pg.wait_for_selector("#di_country_modal"); pg.check("#di_cm_boxes input[value=PH]"); pg.click("#di_cm_save")
        pg.wait_for_selector("#di_country_modal", state="detached")
        put = [c for c in calls if c[0] == "PUT" and "/no-service-variants/v1" in c[1]][-1]
        assert json.loads(put[2]) == {"countries": ["JO", "PH", "SA"]}
        pg.on("dialog", lambda d: d.accept())
        pg.locator("#di_ns_var_body tr").nth(0).locator("button:has(.fa-trash-alt)").click()
        pg.wait_for_function("window.__x = 1; true")
        assert any(c[0] == "DELETE" and c[1].endswith("/no-service-variants/v1") for c in calls)
        b.close()


def test_server_values_are_escaped_in_the_new_ui(html):
    evil = "<img src=x onerror=window.__pwned=1>"
    def handler(req, path):
        if path.startswith("/admin/default-images") and req.method == "GET":
            d = payload("all"); d["countries"] = [{"iso": "IN", "name": evil, "dial": "91", "flag": ""}] + COUNTRIES[1:]
            d["items"]["store"] = [{"url": U("s"), "countries": ["IN"]}]; d["store"] = [U("s")]
            d["no_service_variants"] = [{"id": "v1", "url": U("x"), "title": evil, "message": evil, "countries": ["IN"]}]
            return {"body": json.dumps(d), "content_type": "application/json"}
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html, handler)
        pg.wait_for_function("document.querySelector('#di_ns_var_body').innerText.includes('<img')", timeout=5000)
        assert pg.evaluate("window.__pwned") is None
        assert evil in pg.inner_text("#di_store_body") and evil in pg.inner_text("#di_ns_var_body")
        b.close()


def test_responsive_layout_has_no_horizontal_overflow(html):
    with pw.sync_playwright() as p:
        b, pg, calls = open_page(p, html)
        pg.set_viewport_size({"width": 390, "height": 800})
        assert pg.evaluate("document.getElementById('section-default_images').scrollWidth <= document.documentElement.clientWidth + 2 || getComputedStyle(document.querySelector('#section-default_images .card')).overflow === 'hidden'")
        b.close()


def test_other_sections_and_existing_controls_are_still_there(html):
    for needle in ("diAddUrl('store')", "diUploadFile('offer')", "diUploadMerchantBanner()", "diSaveNoService()", "diSetExplore(", "diRemoveUrl(",
                   "di_no_service_title", "Default Merchant Banner Image", "No Service Available"):
        assert needle in html
