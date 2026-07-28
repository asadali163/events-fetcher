# Universe connector

**Type:** scraper (Playwright + BeautifulSoup) · **Coverage:** NL · **Auth:** none

Scrapes Universe.com's `/explore` search page. One browser session per `discover()`
call — loads page 1, reads the reported total page count, then pages through up to
`MAX_PAGES`, parsing each page's rendered HTML with BeautifulSoup. `fetch()` is a
no-op passthrough — the listing card's own text already has everything needed.

## How to add a new city

1. Look up the city's approximate center coordinates (e.g. search it on Google Maps
   and read the lat/lon out of the URL).
2. Add a line to `CITY_COORDINATES` in `connector.py`:
   ```python
   CITY_COORDINATES = {
       "Amsterdam": (52.3675734, 4.9041389),
       "NewCity": (lat, lon),
   }
   ```
3. Make sure the city is also in `core/cities.yaml` and its country is listed in this
   connector's `coverage` in `core/registry.yaml`.

Currently mapped: Amsterdam.

## Key implementation details

- **No server-side date-range filter exists on this search** — the explore page's URL
  only takes `query=`, `loc=`, `ll=`, and `page=` params, no date bounds. Worse, results
  are **not chronologically ordered** (verified against real scraped output — August and
  October dates interleave across cards), so pagination can't stop early once results run
  past the requested window. `discover()` scans every page up to `MAX_PAGES` and enforces
  `days_ahead` itself, client-side, by comparing each card's parsed `start_date` against
  the requested window before keeping it.
- **The search URL is double-URL-encoded.** Universe's own search UI encodes the `loc=`
  and `ll=` values twice (verified against a real explore URL) — `_build_url()` applies
  `urllib.parse.quote()` twice to reproduce this exactly. If a new city's search ever
  looks wrong, check this first before assuming the coordinates are bad.
- **The listing card's "venue" text is a full address blob, not a clean venue name** —
  e.g. `"Nieuwezijds Kolk 25, 1012 PV Amsterdam, Nederland"`. It isn't isolated from the
  rest of the card by any dedicated class; it's picked out heuristically as the longest
  comma-containing leftover string (ported as-is from the tested reference scraper). The
  segment order isn't even consistent between cards — sometimes
  `"street, postcode city, country"`, sometimes `"street, city, postcode, country"` — so
  `normalize()` doesn't try to split it. `venue_name` is left `None` rather than guessing;
  the full blob is geocoded as-is via `core.geocoding.geocode_place()`.
- **`geocode_place()` (new, in `core/geocoding.py`) resolves city/country from the
  geocoder's own answer, not from the searched city.** This was a real, verified
  necessity, not caution for its own sake: an event scraped under an Amsterdam-area
  search actually listed `"...Haarlem, Nederland"` as its address — the same
  "search city ≠ event's true city" trap Bandsintown hit with Brussels/Tomorrowland.
  `get_coordinates()` (used by every other connector) is unchanged; `geocode_place()` is
  a new, separate function for exactly this situation, caching under the raw address
  text itself since city/country aren't known until after geocoding.
- **Extra fields not part of the common schema yet** (same precedent as Meetup's
  `attendees`): `organizer` (the event host/promoter), `currency`, `price_min`,
  `price_max` — Universe is the only source that currently provides pricing, kept since
  it's free and could feed a future pricing-aware feature.
- **No `type` field populated** — no genre/category data is available in what's scraped.

## Required fields for validation

`source_event_id`, `event_name`, `date`.
