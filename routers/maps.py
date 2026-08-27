"""
Google Maps link resolver for OffrO merchant app.

Accepts Google Maps URLs, including maps.app.goo.gl short links,
and attempts to extract:
- latitude
- longitude
- place name
- address
- Google Maps URL
"""

from fastapi import APIRouter, HTTPException, Query
from urllib.parse import urlparse, parse_qs, unquote
import re
import requests


router = APIRouter(tags=["Maps"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_coordinates(text: str):
    """
    Extract latitude/longitude from common Google Maps URL formats.

    Supported examples:
      @15.12345,75.12345
      ?q=15.12345,75.12345
      ?ll=15.12345,75.12345
      /search/15.12345,75.12345
    """

    if not text:
        return None, None

    # Format: @15.12345,75.12345
    match = re.search(
        r'@(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)',
        text
    )

    if match:
        return float(match.group(1)), float(match.group(2))

    # Format: q=15.12345,75.12345
    parsed = urlparse(text)
    params = parse_qs(parsed.query)

    for key in ("q", "query", "ll", "center"):
        values = params.get(key, [])
        for value in values:
            match = re.search(
                r'(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)',
                unquote(value)
            )
            if match:
                return float(match.group(1)), float(match.group(2))

    # Generic fallback: look for a coordinate pair in the URL
    match = re.search(
        r'(?<!\d)(-?\d{1,3}\.\d{4,})\s*,\s*(-?\d{1,3}\.\d{4,})(?!\d)',
        text
    )

    if match:
        lat = float(match.group(1))
        lng = float(match.group(2))

        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return lat, lng

    return None, None


def _extract_place_name(url: str):
    """
    Try to extract a place name from common Google Maps URL formats.
    """

    if not url:
        return ""

    decoded = unquote(url)

    # Google Maps often contains:
    # /maps/place/PLACE_NAME/
    match = re.search(
        r'/maps/place/([^/]+)',
        decoded,
        re.IGNORECASE
    )

    if match:
        name = match.group(1)
        name = name.replace("+", " ")
        name = name.replace("%20", " ")
        return name.strip()

    # Fallback: q=PLACE_NAME
    parsed = urlparse(decoded)
    params = parse_qs(parsed.query)

    for key in ("q", "query"):
        values = params.get(key, [])
        if values:
            value = values[0]

            # Don't return coordinates as place name
            if not re.fullmatch(
                r'-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?',
                value
            ):
                return value.replace("+", " ").strip()

    return ""


def _is_google_maps_url(url: str):
    try:
        host = urlparse(url).netloc.lower()

        return (
            host == "maps.app.goo.gl"
            or host.endswith(".google.com")
            or host == "google.com"
            or host.endswith(".googleusercontent.com")
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/resolve-maps-link")
def resolve_maps_link(
    url: str = Query(..., description="Google Maps URL")
):
    """
    Resolve a Google Maps link and extract location information.
    """

    url = url.strip()

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Google Maps link is required."
        )

    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="Please provide a valid Google Maps URL."
        )

    if not _is_google_maps_url(url):
        raise HTTPException(
            status_code=400,
            detail="Please provide a valid Google Maps link."
        )

    # -----------------------------------------------------------------------
    # First try extracting coordinates directly from the supplied URL.
    # -----------------------------------------------------------------------

    lat, lng = _extract_coordinates(url)

    resolved_url = url
    place_name = _extract_place_name(url)

    # -----------------------------------------------------------------------
    # Follow redirects.
    #
    # This is especially important for:
    # https://maps.app.goo.gl/...
    #
    # Google normally redirects this to a longer Maps URL containing
    # the actual place/location information.
    # -----------------------------------------------------------------------

    try:
        response = requests.get(
            url,
            allow_redirects=True,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/151.0 Safari/537.36"
                )
            },
        )

        resolved_url = response.url or url

        # Try again using the final redirected URL.
        if lat is None or lng is None:
            lat, lng = _extract_coordinates(resolved_url)

        if not place_name:
            place_name = _extract_place_name(resolved_url)

    except requests.RequestException as exc:
        # Don't expose internal network details to the merchant.
        print(f"[MAPS] Failed to follow Google Maps URL: {exc}")

    # -----------------------------------------------------------------------
    # Try extracting a readable title/address from the returned HTML.
    # This is best-effort only.
    # -----------------------------------------------------------------------

    address = ""

    try:
        html = response.text if "response" in locals() else ""

        # Look for common Google page title formats.
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>",
            html,
            re.IGNORECASE | re.DOTALL,
        )

        if title_match:
            title = re.sub(r"\s+", " ", title_match.group(1)).strip()

            # Google title often contains:
            # Place Name - Google Maps
            title = re.sub(
                r"\s*-\s*Google Maps\s*$",
                "",
                title,
                flags=re.IGNORECASE,
            ).strip()

            if title and not place_name:
                place_name = title

    except Exception as exc:
        print(f"[MAPS] HTML parsing warning: {exc}")

    # -----------------------------------------------------------------------
    # Validate coordinates.
    # -----------------------------------------------------------------------

    if lat is not None and lng is not None:
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            lat = None
            lng = None

    # -----------------------------------------------------------------------
    # Final response
    # -----------------------------------------------------------------------

    if lat is None or lng is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not extract the location from this Google Maps link. "
                "Please open the location in Google Maps and copy the "
                "Share link again."
            ),
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
    }