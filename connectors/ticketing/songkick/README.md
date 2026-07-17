# Songkick connector

**Type:** scraper (requests + BeautifulSoup) · **Coverage:** HU, FR, BE, CA · **Auth:** none

No browser needed — Songkick's metro-area listing pages embed a full Schema.org JSON-LD
`Event` object per listing, so one HTTP request per page (with pagination) gives everything
needed. This is the lowest-complexity scraper in the project.

## How to add a new city

1. Go to `songkick.com`, search for the city, and copy the metro-area path out of the URL —
   e.g. `https://www.songkick.com/metro-areas/29047-hungary-budapest` → the path is
   `/metro-areas/29047-hungary-budapest`.
2. Add a line to `CITY_METRO_PATHS` in `connector.py`:
   ```python
   CITY_METRO_PATHS = {
       "Budapest": "/metro-areas/29047-hungary-budapest",
       "Paris": "metro-areas/28909-france-paris",
       "Lyon": "metro-areas/28889-france-lyon",
       "Brussels": "metro-areas/182976-belgium-brussels",
       "Toronto": "metro-areas/27396-canada-toronto",
       "NewCity": "/metro-areas/ID-country-cityname",
   }
   ```
3. Make sure the city is also in `core/cities.yaml` and its country is listed in this
   connector's `coverage` in `core/registry.yaml`.

Currently mapped: Budapest, Paris, Lyon, Brussels, Toronto.

## Key implementation details

- **`venue_country` comes from the scraped page itself** (JSON-LD's
  `address.addressCountry`), not hardcoded — this connector already handles multiple
  countries correctly, unlike Dice's current hardcoded-`"France"` limitation (see Dice's
  README). Worth keeping this pattern when adding more cities.
- **Coordinates**: JSON-LD sometimes gives `geo.latitude`/`geo.longitude` directly (with
  `_clean_geo()` detecting and fixing a swapped lat/lon), falling back to
  `core.geocoding.get_coordinates()` using the street address only when JSON-LD lacks geo
  data.
- **Dates use the shared `core.date_utils.split_iso_datetime()`** — Songkick's
  `start_datetime_tz` (from a real `<time datetime="...">` attribute) is genuinely
  ISO 8601, unlike Dice. Falls back to the date-only `start_date` field if no specific time
  was announced.
- **`source_event_id`**: prefers the event's own URL from JSON-LD (`event_url`) when
  present; falls back to a deterministic hash of name+date+venue if not. Whether the real
  URL actually comes through consistently hasn't been fully confirmed — worth checking
  against real scraped data occasionally.
- **`type`** is a comma-joined list of performer genres from JSON-LD (`", ".join(genres)`),
  or `None` if no genres were given.
- **`days_ahead` default is 15.**

## Required fields for validation

`source_event_id`, `event_name`, `date`.
