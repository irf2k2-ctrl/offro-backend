"""
Google Maps link resolver for OffrO merchant app.

Supports:
- maps.app.goo.gl short/share links
- standard Google Maps place URLs
- @latitude,longitude
- !3dLAT!4dLNG
- q/query/ll/center/viewpoint coordinate parameters
- Google Maps Resolution API (when GOOGLE_MAPS_API_KEY is configured)
- Google Places API (New) to retrieve final coordinates/details

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

    # Decode more than once because Google links can contain nested encoding.
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
            lat, lng = _valid_coordinates(match.group(1), match.group(2))
            if lat is not None:
                return lat, lng

        # ---------------------------------------------------------------
        # 2. Google Maps data format:
        #    !3d15.12345!4d75.12345
        #
        # This is important for many Google Maps place/share URLs.
        # ---------------------------------------------------------------
        match = re.search(
            r'!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)',
            candidate,
            re.IGNORECASE,
        )
        if match:
            lat, lng = _valid_coordinates(match.group(1), match.group(2))
            if lat is not None:
                return lat, lng

        # ---------------------------------------------------------------
        # 3. Query-string coordinate parameters
        # ---------------------------------------------------------------
        try:
            parsed = urlparse(candidate)
            params = parse_qs(parsed.query)

            for key in ("q", "query", "ll", "center", "viewpoint"):
                for value in params.get(key, []):
                    value = unquote(value)

                    match = re.search(
                        r'(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)',
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
            lat, lng = _valid_coordinates(match.group(1), match.group(2))
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
                    r'-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?',
                    value,
                ):
                    return value.replace("+", " ").strip()
    except Exception:
        pass

    return ""


def _extract_place_id(text: str):
    """
    Extract a Google Place ID when it is visible in a Maps URL/page.

    Place IDs normally begin with ChI..., but Google also has other
    historical identifier formats. We therefore use the common ChI form
    here and let the Resolution API handle short links.
    """
    if not text:
        return None

    decoded = unquote(unquote(text))

    # Common URL forms:
    # query_place_id=ChIJ...
    # place_id=ChIJ...
    for pattern in (
        r'(?:query_place_id|place_id)=([A-Za-z0-9_-]+)',
        r'!1s(ChIJ[A-Za-z0-9_-]+)',
    ):
        match = re.search(pattern, decoded, re.IGNORECASE)
        if match:
            return match.group(1)

    # Generic ChI... token in page/URL.
    match = re.search(r'\b(ChIJ[A-Za-z0-9_-]{10,})\b', decoded)
    if match:
        return match.group(1)

    return None


# ---------------------------------------------------------------------------
# HTTP / Google API helpers
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


def _resolve_maps_url_with_resolution_api(url: str):
    """
    Resolve a Google Maps URL, including maps.app.goo.gl short links,
    using Google's Maps Tools Resolution API.

    Returns a Place ID or None.

    The API is currently experimental/pre-GA.
    """
    if not GOOGLE_MAPS_API_KEY:
        return None

    endpoint = "https://mapstools.googleapis.com/v1alpha:resolveMapsUrls"

    try:
        response = requests.post(
            endpoint,
            params={"key": GOOGLE_MAPS_API_KEY},
            json={"urls": [url]},
            timeout=HTTP_TIMEOUT,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Offro/1.0",
            },
        )

        if response.status_code != 200:
            print(
                f"[MAPS] Resolution API returned "
                f"{response.status_code}: {response.text[:500]}"
            )
            return None

        data = response.json()

        entities = data.get("entities") or []
        if not entities:
            return None

        entity = entities[0] or {}
        place = entity.get("place")

        if not place:
            return None

        # Expected:
        # places/ChIJ...
        if place.startswith("places/"):
            return place.split("/", 1)[1]

        return place

    except requests.RequestException as exc:
        print(f"[MAPS] Resolution API request failed: {exc}")
        return None
    except (ValueError, TypeError, KeyError) as exc:
        print(f"[MAPS] Resolution API response parse failed: {exc}")
        return None


def _get_place_details(place_id: str):
    """
    Get coordinates/name/address for a Google Place ID using Places API (New).
    """
    if not GOOGLE_MAPS_API_KEY or not place_id:
        return None

    endpoint = f"https://places.googleapis.com/v1/places/{place_id}"

    try:
        response = requests.get(
            endpoint,
            params={"key": GOOGLE_MAPS_API_KEY},
            timeout=HTTP_TIMEOUT,
            headers={
                "X-Goog-FieldMask": (
                    "id,displayName,formattedAddress,location,googleMapsUri"
                ),
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "User-Agent": "Offro/1.0",
            },
        )

        if response.status_code != 200:
            print(
                f"[MAPS] Places API returned "
                f"{response.status_code}: {response.text[:500]}"
            )
            return None

        data = response.json()

        location = data.get("location") or {}
        lat, lng = _valid_coordinates(
            location.get("latitude"),
            location.get("longitude"),
        )

        display_name = data.get("displayName") or {}
        place_name = display_name.get("text") or ""

        return {
            "place_id": data.get("id") or place_id,
            "lat": lat,
            "lng": lng,
            "place_name": place_name,
            "address": data.get("formattedAddress") or "",
            "url": data.get("googleMapsUri") or "",
        }

    except requests.RequestException as exc:
        print(f"[MAPS] Places API request failed: {exc}")
        return None
    except (ValueError, TypeError, KeyError) as exc:
        print(f"[MAPS] Places API response parse failed: {exc}")
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

    # Run our normal URL parser over the page source too.
    lat, lng = _extract_coordinates(html)
    if lat is not None:
        return lat, lng

    # Additional JSON-ish patterns Google may embed.
    patterns = [
        r'"latitude"\s*:\s*(-?\d+(?:\.\d+)?)'
        r'.{0,300}'
        r'"longitude"\s*:\s*(-?\d+(?:\.\d+)?)',

        r'"lat"\s*:\s*(-?\d+(?:\.\d+)?)'
        r'.{0,300}'
        r'"lng"\s*:\s*(-?\d+(?:\.\d+)?)',
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
    url: str = Query(..., description="Google Maps URL"),
):
    """
    Resolve a Google Maps link and return location information.

    The response keeps the existing Offro Flutter-compatible fields:
      success
      lat / lng
      latitude / longitude
      place_name
      address
      url
    """

    url = url.strip()

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Google Maps link is required.",
        )

    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="Please provide a valid Google Maps URL.",
        )

    if not _is_google_maps_url(url):
        raise HTTPException(
            status_code=400,
            detail="Please provide a valid Google Maps link.",
        )

    # -----------------------------------------------------------------------
    # STEP 1: Try coordinates directly from the original URL.
    # -----------------------------------------------------------------------
    lat, lng = _extract_coordinates(url)

    resolved_url = url
    place_name = _extract_place_name(url)
    address = ""
    place_id = _extract_place_id(url)

    # -----------------------------------------------------------------------
    # STEP 2: Follow redirects.
    #
    # Critical for:
    #   https://maps.app.goo.gl/...
    # -----------------------------------------------------------------------
    response = None

    try:
        response = _get_google_page(url)
        resolved_url = response.url or url

        if lat is None or lng is None:
            lat, lng = _extract_coordinates(resolved_url)

        if not place_name:
            place_name = _extract_place_name(resolved_url)

        if not place_id:
            place_id = _extract_place_id(resolved_url)

    except requests.RequestException as exc:
        print(f"[MAPS] Redirect request failed: {exc}")

    # -----------------------------------------------------------------------
    # STEP 3: Parse returned HTML for coordinates/title.
    # -----------------------------------------------------------------------
    if response is not None:
        try:
            html = response.text or ""

            if lat is None or lng is None:
                lat, lng = _extract_coordinates_from_html(html)

            if not place_name:
                place_name = _extract_html_title(html)

            if not place_id:
                place_id = _extract_place_id(html)

        except Exception as exc:
            print(f"[MAPS] HTML parsing warning: {exc}")

    # -----------------------------------------------------------------------
    # STEP 4: If coordinates still aren't available, use Google's
    # Maps Tools Resolution API.
    #
    # This is the important fix for many maps.app.goo.gl share links.
    # -----------------------------------------------------------------------
    if lat is None or lng is None:
        resolved_place_id = _resolve_maps_url_with_resolution_api(url)

        if resolved_place_id:
            place_id = resolved_place_id

            details = _get_place_details(resolved_place_id)

            if details:
                lat = details.get("lat")
                lng = details.get("lng")

                if details.get("place_name"):
                    place_name = details["place_name"]

                if details.get("address"):
                    address = details["address"]

                if details.get("url"):
                    resolved_url = details["url"]

    # -----------------------------------------------------------------------
    # STEP 5: If a Place ID was already present, try Place Details directly.
    # -----------------------------------------------------------------------
    if (lat is None or lng is None) and place_id:
        details = _get_place_details(place_id)

        if details:
            lat = details.get("lat")
            lng = details.get("lng")

            if details.get("place_name"):
                place_name = details["place_name"]

            if details.get("address"):
                address = details["address"]

            if details.get("url"):
                resolved_url = details["url"]

    # -----------------------------------------------------------------------
    # STEP 6: Validate final coordinates.
    # -----------------------------------------------------------------------
    lat, lng = _valid_coordinates(lat, lng)

    if lat is None or lng is None:
        detail = (
            "Could not extract the location from this Google Maps link. "
            "Please copy the location using Google Maps → Share → Copy link."
        )

        if not GOOGLE_MAPS_API_KEY:
            detail += (
                " The server also needs GOOGLE_MAPS_API_KEY configured "
                "for reliable resolution of maps.app.goo.gl links."
            )

        raise HTTPException(
            status_code=422,
            detail=detail,
        )

    # -----------------------------------------------------------------------
    # Final response - compatible with existing Offro app.
    # -----------------------------------------------------------------------
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
