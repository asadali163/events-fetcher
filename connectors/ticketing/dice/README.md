# Dice connector

**Type:** scraper (Playwright) · **Coverage:** FR (registry.yaml also lists CA — see note
below) · **Auth:** none

Scrapes Dice.fm's city browse page. One shared browser session per `discover()` call —
opening a fresh browser per event would be far too slow, so `discover()` does the entire
scrape (collect event links, then visit and scrape every event page) and `fetch()` is a
no-op passthrough.

## How to add a new city

1. Go to `dice.fm`, browse to the city you want, and copy the slug out of the URL —
   e.g. `https://dice.fm/browse/paris-5b23e8a0e63cc224a4c36a2d` → the slug is
   `paris-5b23e8a0e63cc224a4c36a2d`.
2. Add a line to `CITY_SLUGS` in `connector.py`:
   ```python
   CITY_SLUGS = {
       "Paris": "paris-5b23e8a0e63cc224a4c36a2d",
       "Lyon": "lyon-5e3c1c5327ee96d00f784966",
       "Toronto": "toronto-5dfb2d3d7d9fd902f7ecdd5d",
       "NewCity": "new-city-slug-here",
   }
   ```
3. Make sure the city is also in `core/cities.yaml` and that country is listed in this
   connector's `coverage` in `core/registry.yaml`.

Currently mapped: Paris, Lyon, Toronto.

## ⚠️ Known issue: `venue_country` is hardcoded to `"France"`

`normalize()` currently sets `venue_country = "France"` unconditionally — this was fine when
only Paris/Lyon were mapped, but **`CITY_SLUGS` now includes Toronto (Canada)**, so a Toronto
event scraped today would be incorrectly labeled as being in France. This wasn't fixed as
part of writing this doc since it's a code change beyond documentation — worth fixing before
relying on Dice for Toronto: use the `country` parameter already passed into `discover()`
(same fix already applied to OpenAgenda and Bandsintown) instead of the hardcoded string.

## Key implementation details

- **Selectors avoid Dice's build-hash class names** — matched via
  `[class*="EventCard__Event"]`-style partial matches on the stable semantic prefix, since
  the hash suffix changes on every deploy.
- **A field-mapping gotcha already found and fixed once**: `SEL_TYPE`
  (`EventDetailsTitle__Venues`) actually holds the **venue name**, and `SEL_VENUE`
  (`EventDetailsVenue__Address`) holds the **full address** — the names look backwards.
  `normalize()` already accounts for this; don't "fix" it again by swapping them back.
- **Dates use a private parser** (`_split_dice_datetime()`), not the shared
  `core.date_utils` helper — Dice's format (`"Mon 20 Jul, 7:15 pm CEST"`) isn't ISO 8601. No
  year is given, so the current year is attached with rollover to next year if that lands in
  the past.
- **No coordinates from the source at all** — every event goes through
  `core.geocoding.get_coordinates()`, using the venue address as the query.
- **No `type` field populated** — Dice gives no genre/category data in what's currently
  scraped.
- **`days_ahead` default is 3** — deliberately narrow, since this is a full browser scrape
  and slower per-city than the API-based connectors.

## Required fields for validation

`source_event_id`, `event_name`, `date`.
