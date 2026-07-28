import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.locations_store import get_location, save_location

load_dotenv()

# LocationIQ — same underlying OpenStreetMap data as Nominatim, but a real
# API-key based service meant for programmatic use (unlike the free public
# Nominatim demo server, which returned 403s for exactly this kind of
# automated traffic). Free tier: ~5,000 requests/day.
LOCATIONIQ_API_KEY = os.environ["LOCATIONIQ_API_KEY"]
LOCATIONIQ_URL = "https://us1.locationiq.com/v1/search"
REQUEST_DELAY_SECONDS = 0.5


def get_coordinates(address, name, city=None, country=None, source="geocoded"):
    """
    Look up coordinates for a venue, geocoding and caching them only on a
    cache miss. Reusable by any connector — pass the venue's name plus
    whatever address text the source gives you.

    Returns (latitude, longitude), or (None, None) if nothing could be found.
    """
    cached = get_location("venue", name, city, country)
    if cached and cached.get("latitude") is not None and cached.get("longitude") is not None:
        return cached["latitude"], cached["longitude"]

    if not address:
        return None, None

    result = _geocode(address)
    if not result:
        return None, None
    try:
        latitude, longitude = float(result["lat"]), float(result["lon"])
    except (KeyError, TypeError, ValueError):
        return None, None

    save_location("venue", name, city, country, latitude, longitude, source=source)
    return latitude, longitude


def geocode_place(address, name=None, source="geocoded"):
    """
    Like get_coordinates(), but for sources that give a single full address
    blob with no separately known city/country (e.g. Universe.com's scraped
    location text, or a sports venue address) — geocodes the address and
    returns the city/country the geocoder itself resolved, rather than
    trusting whatever city was searched for. That distinction matters:
    a search for one city can still return events actually located in a
    neighboring city (seen in practice with both Bandsintown and Universe),
    so the geocoder's own answer is more trustworthy than the search input.

    Caches under the raw address text itself (city/country aren't known
    until after geocoding, so they can't be part of the cache key the way
    get_coordinates() uses them).

    Returns (latitude, longitude, city, country); any of these may be None.
    """
    cache_key_name = name or address
    cached = get_location("venue", cache_key_name, None, None)
    if cached and cached.get("latitude") is not None and cached.get("longitude") is not None:
        return (
            cached["latitude"],
            cached["longitude"],
            cached.get("city"),
            cached.get("country"),
        )

    if not address:
        return None, None, None, None

    result = _geocode(address)
    if not result:
        return None, None, None, None
    try:
        latitude, longitude = float(result["lat"]), float(result["lon"])
    except (KeyError, TypeError, ValueError):
        return None, None, None, None

    components = result.get("address", {})
    city = (
        components.get("city")
        or components.get("town")
        or components.get("village")
        or components.get("municipality")
        or components.get("county")
    )
    country = components.get("country")

    save_location("venue", cache_key_name, city, country, latitude, longitude, source=source)
    return latitude, longitude, city, country


def _geocode(address):
    """Runs the actual LocationIQ lookup and returns its first raw result
    dict (with addressdetails), or None. Shared by get_coordinates() (which
    only needs lat/lon) and geocode_place() (which also needs the
    structured address components)."""
    try:
        response = requests.get(
            LOCATIONIQ_URL,
            params={
                "key": LOCATIONIQ_API_KEY,
                "q": address,
                "format": "json",
                "limit": 1,
                "addressdetails": 1,
            },
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
    except Exception as error:
        print(f"[geocoding] request failed for '{address}': {error}")
        return None
    finally:
        # Stay under the free tier's rate limit regardless of outcome.
        time.sleep(REQUEST_DELAY_SECONDS)

    return results[0] if results else None
