# OpenAgenda connector

**Type:** API (OpenAgenda v2) · **Coverage:** FR (possibly wider Europe, unconfirmed) ·
**Auth:** `OPENAGENDA_PUBLIC_KEY` in `.env`

Two-step API: search *agendas* (event calendars/publishers, e.g. a city's cultural office)
by city name text search, then fetch events for each found agenda within a geo bounding box
+ date window. The bounding box is why this connector — despite being a real API — still
needs a per-city center point, the same way the Playwright scrapers need a slug/path.

## How to add a new city

1. Look up the city's approximate center coordinates (any map service).
2. Add a line to `CITY_COORDINATES` in `connector.py`:
   ```python
   CITY_COORDINATES = {
       "Paris": (48.8566, 2.3522),
       # "Lyon": (45.7640, 4.8357),   <- currently commented out, disabled
       "NewCity": (lat, lon),
   }
   ```
3. Make sure the city is also in `core/cities.yaml` and its country is listed in this
   connector's `coverage` in `core/registry.yaml`.

**Currently only Paris is active.** Lyon is present in the dict but commented out —
uncomment it if you want to re-enable it (check why it was disabled before doing so, in
case there was a reason).

## Key implementation details

- **`nextTiming` is preferred over `firstTiming`** for date/time — important for recurring
  events (e.g. a weekly event spanning months), where `firstTiming`/`lastTiming` just bound
  the whole series rather than telling you when the next real occurrence actually is.
- **This is the only connector currently populating `end_date`/`end_time`** — both
  `nextTiming` and `firstTiming` blocks have a real `begin` *and* `end`, both run through
  `core.date_utils.split_iso_datetime()`.
- **`venue_country` is not hardcoded** — OpenAgenda's own `location` block doesn't reliably
  include a country field, so this falls back to the `country` ISO code the dispatcher
  already resolved and passed into `discover(city, country)` (stashed as `_search_country`
  on each raw event). This is the correct pattern — see Dice's README for what happens when
  a connector hardcodes a country instead.
- **Coordinates**: uses OpenAgenda's own `location.latitude`/`longitude` when present, saving
  them via `save_location()`; falls back to `core.geocoding.get_coordinates()` only on a
  genuine miss.
- **`type`** is a comma-joined list from `keywords.en`/`keywords.fr`, or `None` if empty.
- **`category_ids`** preserves the raw, undocumented numeric `types-devenement` field
  as-is — there's no ID-to-label reference to decode it against yet. Worth investigating
  OpenAgenda's docs further if real category names are needed later.
- **`days_ahead` default is 15**, **`DEFAULT_RADIUS_KM` is 10** (the bounding box radius
  around the city center).

## Required fields for validation

`source_event_id`, `event_name`, `date`.
