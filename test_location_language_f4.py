"""NOTE: sorts AFTER test_banner_media.py on purpose (that module swaps the shared fake `database` at import).
F4: one language for every resolved location field, for every provider path (pasted Maps URL, GPS reverse geocode, Places details)."""
import sys

import pytest
from fastapi.testclient import TestClient

import routers.maps as M
import routers.public as PUB
import server
from services import city_master as CM
from services import geo_names as G

fake = sys.modules["database"].db
c = TestClient(server.app)
URL = "https://www.google.com/maps/@{lat},{lng},17z/data=!3d{lat}!4d{lng}"

# country -> (lat, lng, ISO, what OSM (Nominatim) answers for lang=en / lang=ar, what Google Geocoding answers for en)
CASES = {
    # Jordan: the reported bug - OSM has no English name for the neighbourhood, so "en" returns Arabic city/area next to an English state
    "JO": dict(lat=31.7384, lng=35.9340, nom_en={"state": "Amman", "town": "خريبة السوق", "suburb": "منطقة المقابلين"},
               nom_ar={"state": "عمان", "town": "خريبة السوق", "suburb": "منطقة المقابلين"},
               g_en={"state": "Amman Governorate", "city": "Amman", "area": "Khreibet Al-Souq", "subs": ["Al-Muqabalain"]},
               g_ar={"state": "محافظة العاصمة", "city": "عمان", "area": "خريبة السوق", "subs": ["المقابلين"]},
               state_en="Amman", city_en="Amman", city_ar="عمان"),
    "SA": dict(lat=24.7136, lng=46.6753, nom_en={"state": "Riyadh Region", "city": "Riyadh", "suburb": "حي المنسية"},
               nom_ar={"state": "منطقة الرياض", "city": "الرياض", "suburb": "حي المنسية"},
               g_en={"state": "Riyadh Province", "city": "Riyadh", "area": "Al Munsiyah"}, state_en="Riyadh Region", city_en="Riyadh", city_ar="الرياض"),
    "IN": dict(lat=18.5204, lng=73.8567, nom_en={"state": "Maharashtra", "city": "Pune", "suburb": "Kothrud"},
               nom_ar={"state": "Maharashtra", "city": "Pune", "suburb": "Kothrud"},      # OSM has no Arabic names for Indian places
               g_en={}, state_en="Maharashtra", city_en="Pune", city_ar=None),
    "NP": dict(lat=27.7172, lng=85.3240, nom_en={"state": "Bagmati Province", "city": "काठमाडौँ", "suburb": "Baneshwor"},
               nom_ar={"state": "Bagmati Province", "city": "काठमाडौँ", "suburb": "Baneshwor"}, g_en={"state": "Bagmati Province", "city": "Kathmandu", "area": "Baneshwor"}, state_en="Bagmati Province", city_en="Kathmandu", city_ar=None),
    "PH": dict(lat=14.5995, lng=120.9842, nom_en={"state": "Metro Manila", "city": "Manila", "suburb": "Ermita"},
               nom_ar={"state": "Metro Manila", "city": "Manila", "suburb": "Ermita"}, g_en={}, state_en="Metro Manila", city_en="Manila", city_ar=None),
}


class _Resp:
    def __init__(self, body):
        self.body, self.status_code, self.text = body, 200, ""

    def json(self):
        return self.body


@pytest.fixture(autouse=True)
def _fresh_caches():
    G._CACHE.clear(); G._LOGGED.clear()
    yield


@pytest.fixture
def providers(monkeypatch):
    """Fake Nominatim + Google Geocoding; records every request so a test can see which providers were asked, and in which language."""
    G._CACHE.clear(); G._LOGGED.clear(); fake.cities.delete_many({})
    log = []
    state = {"country": "JO", "google": True}

    def fake_get(url, params=None, **kw):
        k = CASES[state["country"]]
        if "nominatim" in url:
            lang = url.split("accept-language=")[1].split("&")[0]
            log.append(("nominatim", lang))
            addr = dict(k["nom_ar"] if lang == "ar" else k["nom_en"], country_code=state["country"].lower())
            return _Resp({"address": addr, "display_name": "x"})
        if "maps.googleapis.com/maps/api/geocode" in url:
            log.append(("google", params["language"]))
            if state.get("google_answer") is not None:
                return _Resp(state["google_answer"])
            g = k.get("g_ar") if params["language"] == "ar" else k["g_en"]
            if not g:
                return _Resp({"status": "ZERO_RESULTS", "results": []})
            comps = [{"long_name": v, "types": t} for v, t in (
                (g.get("state"), ["administrative_area_level_1"]), (g.get("city"), ["locality", "political"]),
                (g.get("area"), ["sublocality_level_1", "sublocality"])) if v]
            comps += [{"long_name": v, "types": ["neighborhood"]} for v in g.get("subs", [])]
            return _Resp({"status": "OK", "results": [{"address_components": comps, "formatted_address": "English address" if params["language"] == "en" else "عنوان"}]})
        raise AssertionError("unexpected request " + url)

    monkeypatch.setattr(G.requests, "get", fake_get)
    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(G, "_google_key", lambda: "k" if state["google"] else None)
    monkeypatch.setattr(M, "_get_google_page", lambda u: (_ for _ in ()).throw(M.requests.RequestException("offline")))
    return log, state


def gps(country, lang):
    k = CASES[country]
    return c.get(f"/reverse-geocode?lat={k['lat']}&lng={k['lng']}&lang={lang}").json()


def pasted(country, lang):
    k = CASES[country]
    r = c.get("/resolve-maps-link", params={"url": URL.format(lat=k["lat"], lng=k["lng"]), "lang": lang})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("path", [gps, pasted])
def test_jordan_english_app_gets_english_city_state_and_area(providers, path):
    log, st = providers
    j = path("JO", "en")
    assert (j["state"], j["city"], j["area"]) == ("Amman", "Amman", "Khreibet Al-Souq"), j      # City = Google's locality; the neighbourhood is the Area
    assert j["country"] == "JO"
    assert ("google", "en") in log                                                    # the fallback was asked in English as well


@pytest.mark.parametrize("path", [gps, pasted])
def test_jordan_arabic_app_is_arabic_and_city_is_the_city_not_the_neighbourhood(providers, path):
    log, st = providers
    j = path("JO", "ar")
    assert (j["state"], j["city"], j["area"]) == ("عمان", "عمان", "منطقة المقابلين"), j
    assert [x for x in log if x[0] == "google"] == [("google", "ar")]                 # the cross-check is asked in the SAME language


@pytest.mark.parametrize("path", [gps, pasted])
def test_saudi_english_and_arabic(providers, path):
    log, st = providers
    st["country"] = "SA"
    en = path("SA", "en")
    assert (en["state"], en["city"]) == ("Riyadh Region", "Riyadh") and en["area"] == "Al Munsiyah" and en["country"] == "SA"
    ar = path("SA", "ar")
    assert (ar["state"], ar["city"], ar["area"]) == ("منطقة الرياض", "الرياض", "حي المنسية")


@pytest.mark.parametrize("path", [gps, pasted])
def test_india_unchanged_and_no_extra_provider_call(providers, path):
    log, st = providers
    st["country"] = "IN"
    j = path("IN", "en")
    assert (j["state"], j["city"], j["area"], j["country"]) == ("Maharashtra", "Pune", "Kothrud", "IN")
    assert [x for x in log if x[0] == "google"] == []                                 # already English: nothing re-queried, nothing changed


def test_nepal_non_latin_city_is_completed_in_english_and_philippines_untouched(providers):
    log, st = providers
    st["country"] = "NP"
    j = gps("NP", "en")
    assert (j["state"], j["city"], j["area"], j["country"]) == ("Bagmati Province", "Kathmandu", "Baneshwor", "NP")
    st["country"] = "PH"
    log.clear()
    j = gps("PH", "en")
    assert (j["state"], j["city"], j["area"], j["country"]) == ("Metro Manila", "Manila", "Ermita", "PH") and not [x for x in log if x[0] == "google"]


def test_no_english_name_anywhere_keeps_the_provider_name_not_a_wrong_city(providers):
    log, st = providers
    st["google"] = False                                                              # no fallback provider available at all
    j = gps("JO", "en")
    assert j["state"] == "Amman" and j["city"] == "خريبة السوق"                      # graceful: original name kept, nothing invented
    st["google"] = True
    CASES["JO"]["g_en"], saved = {"state": "Amman", "city": "خريبة السوق", "area": "منطقة المقابلين"}, CASES["JO"]["g_en"]    # provider also only has the Arabic name
    try:
        assert gps("JO", "en")["city"] == "خريبة السوق"
    finally:
        CASES["JO"]["g_en"] = saved


def test_coordinates_never_change_and_pasted_url_keeps_exact_pin(providers):
    j = pasted("JO", "en")
    k = CASES["JO"]
    assert (j["lat"], j["lng"], j["latitude"], j["longitude"]) == (k["lat"], k["lng"], k["lat"], k["lng"])


def test_places_partial_english_result_is_completed_per_field(providers):
    """Places gave the state in English but no city/area: Nominatim filled them in Arabic - the merged result is brought to English."""
    log, st = providers
    out = G.complete_names({"state": "Amman", "city": "خريبة السوق", "area": "منطقة المقابلين", "address": "Amman"}, 31.7384, 35.9340, "en")
    assert out["state"] == "Amman" and out["city"] == "Amman" and out["area"] == "Khreibet Al-Souq"


def test_language_check_unit():
    assert G.in_language("Khreibet Al-Souq", "en") and G.in_language("Zürich 5", "en") and G.in_language("", "en") and G.in_language("12", "en")
    assert not G.in_language("خريبة السوق", "en") and not G.in_language("काठमाडौँ", "en") and not G.in_language("Amman عمان", "en")
    assert G.in_language("خريبة السوق", "ar") and not G.in_language("Riyadh", "ar")


def test_provider_failure_is_swallowed_and_never_blanks_a_field():
    def boom(*a):
        raise RuntimeError("down")
    out = G.complete_names({"state": "Amman", "city": "خريبة السوق", "area": "", "address": ""}, 1, 1, "en", providers=(boom,))
    assert out["city"] == "خريبة السوق" and out["state"] == "Amman"


def test_city_master_is_neither_rewritten_nor_fuzzy_matched():
    fake.cities.delete_many({})
    fake.cities.insert_one({"name": "Amman", "country": "JO", "name_key": CM.name_key("Amman")})
    before = list(fake.cities.find({}, {"_id": 0}))
    assert CM.check_city_in_country("Amman", "JO") is None                           # the canonical entry matches by exact name
    assert CM.check_city_in_country("Khreibet Al-Souq", "JO") is not None            # an unknown English name is not mapped to a "similar" city
    assert CM.check_city_in_country("Ammann", "JO") is not None
    assert list(fake.cities.find({}, {"_id": 0})) == before                          # resolution never writes to the master


# ───────────── all five countries x GPS / pasted link x English / Arabic ─────────────
EXPECT = {   # (country, lang) -> (state, city, area) the app must show; None = whatever the provider has (nothing to translate)
    ("JO", "en"): ("Amman", "Amman", "Khreibet Al-Souq"), ("JO", "ar"): ("عمان", "عمان", "منطقة المقابلين"),
    ("SA", "en"): ("Riyadh Region", "Riyadh", "Al Munsiyah"),          ("SA", "ar"): ("منطقة الرياض", "الرياض", "حي المنسية"),
    ("IN", "en"): ("Maharashtra", "Pune", "Kothrud"),                  ("IN", "ar"): ("Maharashtra", "Pune", "Kothrud"),
    ("NP", "en"): ("Bagmati Province", "Kathmandu", "Baneshwor"),      ("NP", "ar"): ("Bagmati Province", "काठमाडौँ", "Baneshwor"),
    ("PH", "en"): ("Metro Manila", "Manila", "Ermita"),                ("PH", "ar"): ("Metro Manila", "Manila", "Ermita"),
}


@pytest.mark.parametrize("path", [gps, pasted], ids=["gps", "pasted-link"])
@pytest.mark.parametrize("country,lang", sorted(EXPECT))
def test_every_country_both_paths_both_languages(providers, path, country, lang):
    log, st = providers
    st["country"] = country
    k = CASES[country]
    j = path(country, lang)
    assert (j["state"], j["city"], j["area"]) == EXPECT[(country, lang)], j
    assert j["country"] == country                                                        # detected country never changes
    if path is pasted:
        assert (j["lat"], j["lng"]) == (k["lat"], k["lng"])
    # every Nominatim request carried the app language; the fallback (if used) asked in the same language
    assert all(l == lang for prov, l in log), log


def test_arabic_request_for_a_latin_only_place_makes_no_fallback_call_and_changes_nothing(providers):
    log, st = providers
    for country in ("IN", "PH"):
        st["country"] = country
        log.clear()
        before = gps(country, "ar")
        assert [x for x in log if x[0] == "google"] == []
        assert before["city"] == CASES[country]["city_en"]


# ───────────── Google Geocoding fallback: configuration and failure handling ─────────────
KEY = "AIzaSyTESTONLYSECRET0123456789"


class _Boom(Exception):
    pass


@pytest.mark.parametrize("answer", [
    {"status": "REQUEST_DENIED", "error_message": "This API project is not authorized to use this API."},      # API disabled / key restricted
    {"status": "REQUEST_DENIED", "error_message": "The provided API key is invalid."},                          # invalid key
    {"status": "OVER_QUERY_LIMIT"}, {"status": "OVER_DAILY_LIMIT"}, {"status": "ZERO_RESULTS", "results": []}, {"status": "UNKNOWN_ERROR"}, {},
])
def test_google_failures_keep_the_original_names(monkeypatch, capsys, answer):
    monkeypatch.setattr(G, "_google_key", lambda: KEY)
    monkeypatch.setattr(G.requests, "get", lambda url, **k: _Resp(answer))
    f = {"state": "Amman", "city": "خريبة السوق", "area": "خريبة السوق", "address": "x"}
    out = G.complete_names(f, 31.7, 35.9, "en")
    assert out == f
    cap = capsys.readouterr()
    assert KEY not in cap.out + cap.err


@pytest.mark.parametrize("exc", [G.requests.Timeout("t"), G.requests.ConnectionError("c"), ValueError("not json"), _Boom(f"https://x?key={KEY}")])
def test_google_exceptions_are_swallowed_and_never_log_the_key(monkeypatch, capsys, exc):
    def boom(url, **k):
        raise exc
    monkeypatch.setattr(G, "_google_key", lambda: KEY)
    monkeypatch.setattr(G.requests, "get", boom)
    f = {"state": "Amman", "city": "خريبة السوق", "area": "", "address": ""}
    assert G.complete_names(f, 1, 1, "en") == f
    cap = capsys.readouterr()
    assert KEY not in cap.out + cap.err


def test_http_error_page_instead_of_json_is_handled(monkeypatch):
    class Bad:
        status_code = 503
        def json(self):
            raise ValueError("html")
    monkeypatch.setattr(G, "_google_key", lambda: KEY)
    monkeypatch.setattr(G.requests, "get", lambda url, **k: Bad())
    f = {"state": "Amman", "city": "خريبة السوق", "area": "", "address": ""}
    assert G.complete_names(f, 1, 1, "en") == f


def test_no_key_means_no_google_call_and_original_names(monkeypatch):
    for v in ("GOOGLE_MAPS_API_KEY", "MAPS_API_KEY", "GOOGLE_MAPS_PLATFORM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    def never(*a, **k):
        raise AssertionError("must not call Google without a key")
    monkeypatch.setattr(G.requests, "get", never)
    f = {"state": "Amman", "city": "خريبة السوق", "area": "", "address": ""}
    assert G.complete_names(f, 1, 1, "en") == f


def test_key_comes_from_the_same_variables_as_the_other_google_services(monkeypatch):
    for v in ("GOOGLE_MAPS_API_KEY", "MAPS_API_KEY", "GOOGLE_MAPS_PLATFORM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("GOOGLE_MAPS_PLATFORM_API_KEY", "k3")
    assert G._google_key() == "k3"
    monkeypatch.setenv("MAPS_API_KEY", "k2")
    assert G._google_key() == "k2"
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k1")
    assert G._google_key() == "k1"


def test_google_request_asks_for_english_and_for_arabic(monkeypatch):
    seen = []
    monkeypatch.setattr(G, "_google_key", lambda: KEY)
    monkeypatch.setattr(G.requests, "get", lambda url, params=None, **k: (seen.append((url, params["language"], params["latlng"])), _Resp({"status": "ZERO_RESULTS"}))[1])
    G.google_reverse(31.7, 35.9, "en")
    G.google_reverse(31.7, 35.9, "ar")
    assert [x[1] for x in seen] == ["en", "ar"] and all(x[2] == "31.7,35.9" and "maps.googleapis.com/maps/api/geocode/json" in x[0] for x in seen)


def test_response_never_contains_the_key(providers, monkeypatch):
    monkeypatch.setattr(G, "_google_key", lambda: KEY)
    j = gps("JO", "en")
    assert KEY not in str(j)


# ───────────── Google REQUEST_DENIED: reasons, safe logging, fallback ─────────────
DENIED = {
    "billing_not_enabled": "You must enable Billing on the Google Cloud Project at https://console.cloud.google.com/project/_/billing/enable Learn more at https://developers.google.com/maps/gmp-get-started",
    "referrer_restricted": "API keys with referer restrictions cannot be used with this API.",
    "ip_or_app_restricted": "This IP, site or mobile application is not authorized to use this API key. Request received from IP address 203.0.113.9, with empty referer",
    "api_not_enabled_or_not_allowed_for_key": "This API project is not authorized to use this API.",
    "invalid_key": "The provided API key is invalid.",
    "unrecognised": "Something else",
}


@pytest.mark.parametrize("code", sorted(DENIED))
def test_request_denied_reason_is_classified_logged_safely_and_names_are_kept(providers, monkeypatch, capsys, code):
    log, st = providers
    st["google_answer"] = {"status": "REQUEST_DENIED", "error_message": DENIED[code]}
    monkeypatch.setattr(G, "_google_key", lambda: KEY)
    j = gps("JO", "en")
    assert (j["state"], j["city"], j["area"]) == ("Amman", "خريبة السوق", "منطقة المقابلين")            # provider names preserved, nothing invented
    assert j["country"] == "JO"
    out = capsys.readouterr().out
    assert f"reason={code}" in out and "REQUEST_DENIED" in out
    assert KEY not in out and "203.0.113.9" not in out and "console.cloud.google.com" not in out and "key=" not in out     # no key / IP / raw message


def test_request_denied_is_not_retried_on_every_request(providers, monkeypatch):
    log, st = providers
    st["google_answer"] = {"status": "REQUEST_DENIED", "error_message": DENIED["invalid_key"]}
    monkeypatch.setattr(G, "_google_key", lambda: KEY)
    gps("JO", "en"); gps("JO", "en")
    assert len([x for x in log if x[0] == "google"]) == 1                                               # negative cache (60 s)


@pytest.mark.parametrize("path", [gps, pasted], ids=["gps", "pasted-link"])
def test_jordan_link_with_denied_google_still_resolves_with_original_names(providers, monkeypatch, path):
    log, st = providers
    st["google_answer"] = {"status": "REQUEST_DENIED", "error_message": DENIED["api_not_enabled_or_not_allowed_for_key"]}
    monkeypatch.setattr(G, "_google_key", lambda: KEY)
    for lang, want in (("en", ("Amman", "خريبة السوق", "منطقة المقابلين")), ("ar", ("عمان", "خريبة السوق", "منطقة المقابلين"))):
        j = path("JO", lang)
        assert (j["state"], j["city"], j["area"]) == want and j["country"] == "JO"


def test_classify_google_denial_vocabulary():
    for code, msg in DENIED.items():
        assert G.classify_google_denial(msg)[0] == code
    assert G.classify_google_denial(None)[0] == "unrecognised"


# ───────────── city level: a neighbourhood must not be reported as the City ─────────────
def test_city_level_demotes_a_name_google_lists_as_a_neighbourhood():
    google = lambda lat, lng, lang: {"city": "Amman", "subs": ["Khreibet Al-Souq", "Al-Muqabalain"]}
    f = {"state": "Amman", "city": "Khreibet Al-Souq", "area": "Al-Muqabalain", "address": "a"}
    out = G.fix_city_level(f, 31.7, 35.9, "en", "JO", "town", provider=google)
    assert out["city"] == "Amman" and out["area"] == "Al-Muqabalain" and out["state"] == "Amman"
    out = G.fix_city_level({**f, "area": ""}, 31.7, 35.9, "en", "JO", "village", provider=google)
    assert out["city"] == "Amman" and out["area"] == "Khreibet Al-Souq"                                  # the finer name is kept, not lost


def test_city_level_leaves_everything_else_alone():
    google = lambda lat, lng, lang: {"city": "Amman", "subs": ["Khreibet Al-Souq"]}
    f = {"state": "S", "city": "Khreibet Al-Souq", "area": "A", "address": ""}
    assert G.fix_city_level(f, 1, 1, "en", "JO", "city", provider=google) == f                           # a real `city` key is trusted
    assert G.fix_city_level({**f, "city": "Zarqa"}, 1, 1, "en", "JO", "town", provider=google)["city"] == "Zarqa"    # Google does not call it a neighbourhood
    assert G.fix_city_level(f, 1, 1, "en", "JO", "town", provider=lambda *a: {})["city"] == "Khreibet Al-Souq"       # no Google answer: unchanged
    assert G.fix_city_level(f, 1, 1, "en", "JO", "town", provider=lambda *a: {"city": "", "subs": ["Khreibet Al-Souq"]})["city"] == "Khreibet Al-Souq"   # no locality: never guess
    boom = lambda *a: (_ for _ in ()).throw(RuntimeError("x"))
    assert G.fix_city_level(f, 1, 1, "en", "JO", "town", provider=boom) == f


def test_city_level_never_overrides_a_city_the_city_master_lists():
    fake.cities.delete_many({})
    fake.cities.insert_one({"name": "Khreibet Al-Souq", "country": "JO", "name_key": CM.name_key("Khreibet Al-Souq")})
    google = lambda lat, lng, lang: {"city": "Amman", "subs": ["Khreibet Al-Souq"]}
    f = {"state": "S", "city": "Khreibet Al-Souq", "area": "A", "address": ""}
    assert G.fix_city_level(f, 1, 1, "en", "JO", "town", provider=google) == f
    assert G.fix_city_level(f, 1, 1, "en", "SA", "town", provider=google)["city"] == "Amman"             # the master entry is for JO only
    assert fake.cities.count_documents({}) == 1                                                          # master untouched
    fake.cities.delete_many({})


def test_india_town_level_result_is_unchanged_when_google_has_no_opinion(providers, monkeypatch):
    log, st = providers
    st["country"] = "IN"
    CASES["IN"]["nom_en"] = {"state": "Maharashtra", "town": "Baramati", "suburb": "Kothrud"}
    st["google_answer"] = {"status": "ZERO_RESULTS", "results": []}
    monkeypatch.setattr(G, "_google_key", lambda: KEY)
    try:
        j = gps("IN", "en")
        assert (j["state"], j["city"], j["area"], j["country"]) == ("Maharashtra", "Baramati", "Kothrud", "IN")
    finally:
        CASES["IN"]["nom_en"] = {"state": "Maharashtra", "city": "Pune", "suburb": "Kothrud"}
