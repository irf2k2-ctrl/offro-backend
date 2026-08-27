"""
Google Maps link resolver for OffrO merchant app.

Supports:
- maps.app.goo.gl short/share links
- standard Google Maps place URLs
- @latitude,longitude
- !3dLAT!4dLNG
- q/query/ll/center/viewpoint coordinate parameters
- Google Maps Resolution API
- Google Places API (New)

Endpoint:
    GET /resolve-maps-link?url=<google-maps-url>

Response format is kept compatible with the existing OffrO Flutter app.
"""

import json
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

import requests
from fastapi import APIRouter, HTTPException, Query


router = APIRouter(tags=["Maps"])


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GOOGLE_MAPS_API_KEY = (
    os.getenv("GOOGLE_MAPS_API_KEY")
    or os.getenv("MAPS_API_KEY")
    or os.getenv("GOOGLE_MAPS_PLATFORM_API_KEY")
)

HTTP_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def _valid_coordinates(lat, lng):
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return None, None

    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return lat, lng

    return None, None


def _extract_coordinates(text: str):
    """
    Extract latitude/longitude from common Google Maps URL/page formats.

    Supported examples:
        @15.12345,75.12345
        !3d15.12345!4d75.12345
        ?q=15.12345,75.12345
        ?query=15.12345,75.12345
        ?ll=15.12345,75.12345
        ?center=15.12345,75.12345
        ?viewpoint=15.12345,75.12345
        /search/15.12345,75.12345
    """

    if not text:
        return None, None

    candidates = [text]

    try:
        decoded = unquote(text)

        if decoded != text:
            candidates.append(decoded)

            decoded2 = unquote(decoded)

            if decoded2 != decoded:
                candidates.append(decoded2)

    except Exception:
        pass

    for candidate in candidates:

        # ---------------------------------------------------------------
        # 1. Standard Maps URL:
        #    .../@15.12345,75.12345,17z
        # ---------------------------------------------------------------

        match = re.search(
            r'@(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)',
            candidate,
        )

        if match:
            lat, lng = _valid_coordinates(
                match.group(1),
                match.group(2),
            )

            if lat is not None:
                return lat, lng

        # ---------------------------------------------------------------
        # 2. Google Maps data format:
        #    !3d15.12345!4d75.12345
        # ---------------------------------------------------------------

        match = re.search(
            r'!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)',
            candidate,
            re.IGNORECASE,
        )

        if match:
            lat, lng = _valid_coordinates(
                match.group(1),
                match.group(2),
            )

            if lat is not None:
                return lat, lng

        # ---------------------------------------------------------------
        # 3. Query-string coordinate parameters
        # ---------------------------------------------------------------

        try:
            parsed = urlparse(candidate)
            params = parse_qs(parsed.query)

            for key in (
                "q",
                "query",
                "ll",
                "center",
                "viewpoint",
            ):
                for value in params.get(key, []):

                    value = unquote(value)

                    match = re.search(
                        r'(-?\d+(?:\.\d+)?)\s*,\s*'
                        r'(-?\d+(?:\.\d+)?)',
                        value,
                    )

                    if match:
                        lat, lng = _valid_coordinates(
                            match.group(1),
                            match.group(2),
                        )

                        if lat is not None:
                            return lat, lng

        except Exception:
            pass

        # ---------------------------------------------------------------
        # 4. Generic coordinate pair fallback
        # ---------------------------------------------------------------

        match = re.search(
            r'(?<!\d)(-?\d{1,3}\.\d{4,})\s*,\s*'
            r'(-?\d{1,3}\.\d{4,})(?!\d)',
            candidate,
        )

        if match:
            lat, lng = _valid_coordinates(
                match.group(1),
                match.group(2),
            )

            if lat is not None:
                return lat, lng

    return None, None


# ---------------------------------------------------------------------------
# Google Maps URL helpers
# ---------------------------------------------------------------------------

def _is_google_maps_url(url: str):
    try:
        host = urlparse(url).netloc.lower().split(":")[0]

        return (
            host == "maps.app.goo.gl"
            or host == "goo.gl"
            or host == "maps.google.com"
            or host == "google.com"
            or host.endswith(".google.com")
            or host.endswith(".google.co.in")
        )

    except Exception:
        return False


def _extract_place_name(url: str):
    if not url:
        return ""

    decoded = unquote(unquote(url))

    # /maps/place/PLACE_NAME/
    match = re.search(
        r'/maps/place/([^/?#]+)',
        decoded,
        re.IGNORECASE,
    )

    if match:
        name = match.group(1)
        name = name.replace("+", " ")

        return name.strip()

    # Query parameter fallback
    try:
        parsed = urlparse(decoded)
        params = parse_qs(parsed.query)

        for key in ("q", "query"):

            values = params.get(key, [])

            if values:
                value = values[0]

                if not re.fullmatch(
                    r'-?\d+(?:\.\d+)?\s*,\s*'
                    r'-?\d+(?:\.\d+)?',
                    value,
                ):
                    return value.replace("+", " ").strip()

    except Exception:
        pass

    return ""


def _extract_place_id(text: str):
    """
    Extract a Google Place ID when it is visible in a Maps URL/page.
    """

    if not text:
        return None

    decoded = unquote(unquote(text))

    # Common URL forms:
    #
    # query_place_id=ChIJ...
    # place_id=ChIJ...
    # !1sChIJ...

    for pattern in (
        r'(?:query_place_id|place_id)=([A-Za-z0-9_-]+)',
        r'!1s(ChIJ[A-Za-z0-9_-]+)',
    ):

        match = re.search(
            pattern,
            decoded,
            re.IGNORECASE,
        )

        if match:
            return match.group(1)

    # Generic ChIJ token

    match = re.search(
        r'\b(ChIJ[A-Za-z0-9_-]{10,})\b',
        decoded,
    )

    if match:
        return match.group(1)

    return None


# ---------------------------------------------------------------------------
# HTTP / Google helpers
# ---------------------------------------------------------------------------

def _get_google_page(url: str):
    """
    Follow Google Maps redirects and return the final URL + response.
    """

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/151.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    return requests.get(
        url,
        allow_redirects=True,
        timeout=HTTP_TIMEOUT,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Google Maps Resolution API
# ---------------------------------------------------------------------------

def _resolve_maps_url_with_resolution_api(url: str):
    """
    Resolve a Google Maps URL using Google's Maps Tools
    Resolution API.

    This API supports:
        https://www.google.com/maps/place/...
        https://maps.app.goo.gl/...

    Returns:
        Place ID string if successful
        None if Google could not resolve the URL
    """

    if not GOOGLE_MAPS_API_KEY:
        print(
            "[MAPS] Resolution API skipped: "
            "GOOGLE_MAPS_API_KEY is not configured."
        )
        return None

    endpoint = (
        "https://mapstools.googleapis.com/"
        "v1alpha:resolveMapsUrls"
    )

    print("[MAPS] =======================================")
    print("[MAPS] Resolution API request")
    print(f"[MAPS] URL: {url}")
    print("[MAPS] =======================================")

    try:

        response = requests.post(
            endpoint,
            params={
                "key": GOOGLE_MAPS_API_KEY,
            },
            json={
                "urls": [url],
            },
            timeout=HTTP_TIMEOUT,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "User-Agent": "Offro/1.0",
            },
        )

        print(
            f"[MAPS] Resolution API HTTP status: "
            f"{response.status_code}"
        )

        # ---------------------------------------------------------------
        # HTTP-level failure
        # ---------------------------------------------------------------

        if response.status_code != 200:

            print(
                "[MAPS] Resolution API ERROR RESPONSE:"
            )

            print(
                response.text[:3000]
            )

            return None

        # ---------------------------------------------------------------
        # Parse JSON
        # ---------------------------------------------------------------

        try:
            data = response.json()

        except ValueError:

            print(
                "[MAPS] Resolution API returned "
                "non-JSON response:"
            )

            print(
                response.text[:3000]
            )

            return None

        # ---------------------------------------------------------------
        # IMPORTANT:
        #
        # Google Resolution API can return:
        #
        # {
        #   "entities": [
        #       {}
        #   ],
        #   "failedRequests": {
        #       "0": {
        #           "code": 3,
        #           "message": "Invalid URL."
        #       }
        #   }
        # }
        #
        # Do NOT silently discard failedRequests.
        # ---------------------------------------------------------------

        print(
            "[MAPS] Resolution API response:"
        )

        try:
            print(
                json.dumps(
                    data,
                    indent=2,
                    ensure_ascii=False,
                )[:5000]
            )

        except Exception:
            print(str(data)[:5000])

        # ---------------------------------------------------------------
        # Check failedRequests
        # ---------------------------------------------------------------

        failed_requests = (
            data.get("failedRequests")
            or {}
        )

        if failed_requests:

            print(
                "[MAPS] Resolution API failedRequests:"
            )

            print(
                json.dumps(
                    failed_requests,
                    indent=2,
                    ensure_ascii=False,
                )
            )

        # ---------------------------------------------------------------
        # Get entities
        # ---------------------------------------------------------------

        entities = (
            data.get("entities")
            or []
        )

        if not entities:

            print(
                "[MAPS] Resolution API returned "
                "no entities."
            )

            return None

        # Google guarantees 1:1 alignment between
        # input URLs and entities.

        entity = (
            entities[0]
            if len(entities) > 0
            else {}
        )

        if not isinstance(entity, dict):
            entity = {}

        place = entity.get("place")

        # ---------------------------------------------------------------
        # Some API responses may wrap entity differently.
        # ---------------------------------------------------------------

        if not place:

            nested_entity = entity.get("entity")

            if isinstance(nested_entity, dict):
                place = nested_entity.get("place")

        if not place:

            print(
                "[MAPS] Resolution API did not "
                "return a Place ID."
            )

            return None

        # ---------------------------------------------------------------
        # Expected format:
        #
        # places/ChIJ...
        # ---------------------------------------------------------------

        if isinstance(place, str):

            if place.startswith("places/"):
                place_id = place.split(
                    "/",
                    1,
                )[1]

            else:
                place_id = place

            print(
                f"[MAPS] Resolution API Place ID: "
                f"{place_id}"
            )

            return place_id

        print(
            "[MAPS] Unexpected place value:"
        )

        print(
            repr(place)
        )

        return None

    except requests.RequestException as exc:

        print(
            f"[MAPS] Resolution API request failed: "
            f"{exc}"
        )

        return None

    except Exception as exc:

        print(
            f"[MAPS] Resolution API unexpected error: "
            f"{type(exc).__name__}: {exc}"
        )

        return None


# ---------------------------------------------------------------------------
# Google Places API (New)
# ---------------------------------------------------------------------------

def _get_place_details(place_id: str):
    """
    Get coordinates/name/address for a Google Place ID
    using Places API (New).
    """

    if not GOOGLE_MAPS_API_KEY:

        print(
            "[MAPS] Places API skipped: "
            "GOOGLE_MAPS_API_KEY is not configured."
        )

        return None

    if not place_id:
        return None

    # Remove places/ prefix if supplied.

    if place_id.startswith("places/"):
        place_id = place_id.split(
            "/",
            1,
        )[1]

    endpoint = (
        f"https://places.googleapis.com/v1/places/"
        f"{place_id}"
    )

    print(
        f"[MAPS] Places API lookup: {place_id}"
    )

    try:

        response = requests.get(
            endpoint,
            params={
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=HTTP_TIMEOUT,
            headers={
                "X-Goog-FieldMask": (
                    "id,"
                    "displayName,"
                    "formattedAddress,"
                    "location,"
                    "googleMapsUri"
                ),
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "User-Agent": "Offro/1.0",
            },
        )

        print(
            f"[MAPS] Places API HTTP status: "
            f"{response.status_code}"
        )

        if response.status_code != 200:

            print(
                "[MAPS] Places API ERROR:"
            )

            print(
                response.text[:3000]
            )

            return None

        try:
            data = response.json()

        except ValueError:

            print(
                "[MAPS] Places API returned "
                "non-JSON response."
            )

            return None

        location = (
            data.get("location")
            or {}
        )

        lat, lng = _valid_coordinates(
            location.get("latitude"),
            location.get("longitude"),
        )

        display_name = (
            data.get("displayName")
            or {}
        )

        place_name = (
            display_name.get("text")
            or ""
        )

        result = {
            "place_id": (
                data.get("id")
                or place_id
            ),
            "lat": lat,
            "lng": lng,
            "place_name": place_name,
            "address": (
                data.get("formattedAddress")
                or ""
            ),
            "url": (
                data.get("googleMapsUri")
                or ""
            ),
        }

        print(
            "[MAPS] Places API result:"
        )

        print(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
            )
        )

        return result

    except requests.RequestException as exc:

        print(
            f"[MAPS] Places API request failed: "
            f"{exc}"
        )

        return None

    except Exception as exc:

        print(
            f"[MAPS] Places API unexpected error: "
            f"{type(exc).__name__}: {exc}"
        )

        return None


# ---------------------------------------------------------------------------
# HTML fallback
# ---------------------------------------------------------------------------

def _extract_html_title(html: str):

    if not html:
        return ""

    try:

        title_match = re.search(
            r"<title[^>]*>(.*?)</title>",
            html,
            re.IGNORECASE | re.DOTALL,
        )

        if not title_match:
            return ""

        title = re.sub(
            r"\s+",
            " ",
            title_match.group(1),
        ).strip()

        title = re.sub(
            r"\s*-\s*Google Maps\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip()

        return title

    except Exception:
        return ""


def _extract_coordinates_from_html(html: str):

    if not html:
        return None, None

    # First run normal coordinate extraction.

    lat, lng = _extract_coordinates(html)

    if lat is not None:
        return lat, lng

    # Additional JSON-like patterns.

    patterns = [

        (
            r'"latitude"\s*:\s*'
            r'(-?\d+(?:\.\d+)?)'
            r'.{0,300}'
            r'"longitude"\s*:\s*'
            r'(-?\d+(?:\.\d+)?)'
        ),

        (
            r'"lat"\s*:\s*'
            r'(-?\d+(?:\.\d+)?)'
            r'.{0,300}'
            r'"lng"\s*:\s*'
            r'(-?\d+(?:\.\d+)?)'
        ),

    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            html,
            re.IGNORECASE | re.DOTALL,
        )

        if match:

            lat, lng = _valid_coordinates(
                match.group(1),
                match.group(2),
            )

            if lat is not None:
                return lat, lng

    return None, None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/resolve-maps-link")
def resolve_maps_link(
    url: str = Query(
        ...,
        description="Google Maps URL",
    ),
):
    """
    Resolve a Google Maps link and return location information.

    Response fields:

        success
        lat
        lng
        latitude
        longitude
        place_name
        address
        url
        place_id
    """

    url = url.strip()

    # -----------------------------------------------------------------------
    # Validate URL
    # -----------------------------------------------------------------------

    if not url:

        raise HTTPException(
            status_code=400,
            detail="Google Maps link is required.",
        )

    if not url.startswith(
        (
            "http://",
            "https://",
        )
    ):

        raise HTTPException(
            status_code=400,
            detail="Please provide a valid Google Maps URL.",
        )

    if not _is_google_maps_url(url):

        raise HTTPException(
            status_code=400,
            detail="Please provide a valid Google Maps link.",
        )

    print("")
    print(
        "[MAPS] ======================================="
    )
    print(
        "[MAPS] NEW GOOGLE MAPS RESOLUTION REQUEST"
    )
    print(
        f"[MAPS] Input URL: {url}"
    )
    print(
        f"[MAPS] API key configured: "
        f"{bool(GOOGLE_MAPS_API_KEY)}"
    )
    print(
        "[MAPS] ======================================="
    )

    # -----------------------------------------------------------------------
    # STEP 1:
    # Try coordinates directly from original URL.
    # -----------------------------------------------------------------------

    lat, lng = _extract_coordinates(url)

    resolved_url = url

    place_name = _extract_place_name(url)

    address = ""

    place_id = _extract_place_id(url)

    if lat is not None and lng is not None:

        print(
            f"[MAPS] Direct coordinates found: "
            f"{lat}, {lng}"
        )

    # -----------------------------------------------------------------------
    # STEP 2:
    # Follow redirects.
    #
    # Especially important for:
    #
    # https://maps.app.goo.gl/...
    # -----------------------------------------------------------------------

    response = None

    try:

        response = _get_google_page(url)

        resolved_url = (
            response.url
            or url
        )

        print(
            f"[MAPS] Final redirected URL: "
            f"{resolved_url}"
        )

        if lat is None or lng is None:

            lat, lng = _extract_coordinates(
                resolved_url
            )

        if not place_name:

            place_name = _extract_place_name(
                resolved_url
            )

        if not place_id:

            place_id = _extract_place_id(
                resolved_url
            )

    except requests.RequestException as exc:

        print(
            f"[MAPS] Redirect request failed: "
            f"{exc}"
        )

    # -----------------------------------------------------------------------
    # STEP 3:
    # Parse returned HTML.
    # -----------------------------------------------------------------------

    if response is not None:

        try:

            html = response.text or ""

            if lat is None or lng is None:

                lat, lng = (
                    _extract_coordinates_from_html(
                        html
                    )
                )

            if not place_name:

                place_name = _extract_html_title(
                    html
                )

            if not place_id:

                place_id = _extract_place_id(
                    html
                )

        except Exception as exc:

            print(
                f"[MAPS] HTML parsing warning: "
                f"{exc}"
            )

    # -----------------------------------------------------------------------
    # STEP 4:
    # Google Maps Resolution API.
    #
    # This is the important path for maps.app.goo.gl
    # links where the redirect does not expose coordinates.
    # -----------------------------------------------------------------------

    if lat is None or lng is None:

        print(
            "[MAPS] Coordinates still missing."
        )

        print(
            "[MAPS] Trying Google Maps "
            "Resolution API..."
        )

        resolved_place_id = (
            _resolve_maps_url_with_resolution_api(
                url
            )
        )

        if resolved_place_id:

            place_id = resolved_place_id

            details = _get_place_details(
                resolved_place_id
            )

            if details:

                lat = details.get("lat")
                lng = details.get("lng")

                if details.get("place_name"):

                    place_name = details[
                        "place_name"
                    ]

                if details.get("address"):

                    address = details[
                        "address"
                    ]

                if details.get("url"):

                    resolved_url = details[
                        "url"
                    ]

    # -----------------------------------------------------------------------
    # STEP 5:
    # If a Place ID was already found, try Places API directly.
    # -----------------------------------------------------------------------

    if (
        (lat is None or lng is None)
        and place_id
    ):

        print(
            "[MAPS] Trying Places API using "
            "existing Place ID..."
        )

        details = _get_place_details(
            place_id
        )

        if details:

            lat = details.get("lat")
            lng = details.get("lng")

            if details.get("place_name"):

                place_name = details[
                    "place_name"
                ]

            if details.get("address"):

                address = details[
                    "address"
                ]

            if details.get("url"):

                resolved_url = details[
                    "url"
                ]

    # -----------------------------------------------------------------------
    # STEP 6:
    # Validate final coordinates.
    # -----------------------------------------------------------------------

    lat, lng = _valid_coordinates(
        lat,
        lng,
    )

    # -----------------------------------------------------------------------
    # FAILURE
    # -----------------------------------------------------------------------

    if lat is None or lng is None:

        print(
            "[MAPS] ======================================="
        )

        print(
            "[MAPS] FINAL RESULT: FAILED"
        )

        print(
            f"[MAPS] Input URL: {url}"
        )

        print(
            f"[MAPS] Resolved URL: {resolved_url}"
        )

        print(
            f"[MAPS] Place ID: {place_id}"
        )

        print(
            "[MAPS] ======================================="
        )

        detail = (
            "Could not extract the location from this "
            "Google Maps link. "
            "Please copy the location using "
            "Google Maps → Share → Copy link."
        )

        if not GOOGLE_MAPS_API_KEY:

            detail += (
                " The server also needs "
                "GOOGLE_MAPS_API_KEY configured "
                "for reliable resolution of "
                "maps.app.goo.gl links."
            )

        raise HTTPException(
            status_code=422,
            detail=detail,
        )

    # -----------------------------------------------------------------------
    # SUCCESS
    # -----------------------------------------------------------------------

    print(
        "[MAPS] ======================================="
    )

    print(
        "[MAPS] FINAL RESULT: SUCCESS"
    )

    print(
        f"[MAPS] Latitude: {lat}"
    )

    print(
        f"[MAPS] Longitude: {lng}"
    )

    print(
        f"[MAPS] Place: {place_name}"
    )

    print(
        f"[MAPS] Address: {address}"
    )

    print(
        f"[MAPS] Place ID: {place_id}"
    )

    print(
        "[MAPS] ======================================="
    )

    return {
        "success": True,
        "lat": lat,
        "lng": lng,
        "latitude": lat,
        "longitude": lng,
        "place_name": place_name,
        "address": address,
        "url": resolved_url,
        "place_id": place_id or "",
    }