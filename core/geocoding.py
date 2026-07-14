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

    latitude, longitude = _geocode_address(address)
    if latitude is None or longitude is None:
        return None, None

    save_location("venue", name, city, country, latitude, longitude, source=source)
    return latitude, longitude


def _geocode_address(address):
    try:
        response = requests.get(
            LOCATIONIQ_URL,
            params={"key": LOCATIONIQ_API_KEY, "q": address, "format": "json", "limit": 1},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
    except Exception as error:
        print(f"[geocoding] request failed for '{address}': {error}")
        return None, None
    finally:
        # Stay under the free tier's rate limit regardless of outcome.
        time.sleep(REQUEST_DELAY_SECONDS)

    if not results:
        return None, None

    try:
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (KeyError, TypeError, ValueError):
        return None, None
