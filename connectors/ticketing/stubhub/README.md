# StubHub connector

**Type:** scraper (Playwright + BeautifulSoup) · **Coverage:** FR, HU, BE, NL, CA, LU
(per `core/registry.yaml`; only Paris is actually mapped so far — see below) ·
**Auth:** none

Scrapes StubHub's `/explore` search. One browser session per `discover()` call —
loads the page, scrolls and clicks "Show More" until the list stops growing, then
parses the rendered HTML with BeautifulSoup. `fetch()` is a no-op passthrough.

**Currently disabled in `core/registry.yaml`** (commented out) — pending investigation
into the city-mismatch issue described below.

## How to add a new city

1. Look up the city's approximate center coordinates and its IANA timezone name.
2. Add a line to `CITY_SEARCH_PARAMS` in `connector.py`:
   ```python
   CITY_SEARCH_PARAMS = {
       "Paris": (48.856614, 2.352222, "Europe/Paris"),
       "NewCity": (lat, lon, "Region/City"),
   }
   ```
3. Make sure the city is also in `core/cities.yaml` and its country is listed in this
   connector's `coverage` in `core/registry.yaml`.

Currently mapped: Paris.

## Key implementation details

- **Genuinely supports server-side date filtering** — unlike Meetup/Universe, the
  `/explore` URL takes real `from`/`to` epoch-millisecond params, so `days_ahead`
  works the normal way and the dispatcher's usual window-containment refetch check
  applies (no `REFETCH_INTERVAL_DAYS` needed here).
- **Lat/lon are base64-encoded, and the from/to window needs a real IANA timezone** —
  `CITY_SEARCH_PARAMS` bundles `(lat, lon, timezone)` per city so "start of day"/"end
  of day" boundaries are correct in the *city's* own timezone regardless of what
  timezone the machine running this is in.
- **Only a bare venue name is available, no separate street address.** `normalize()`
  geocodes it via `core.geocoding.geocode_place()`, appending the searched city/country
  to the query text itself (not just the cache key) to help disambiguate generic venue
  names, while still trusting `geocode_place()`'s own resolved city/country over the
  search input.
- **A distance sanity check discards implausible geocoding matches.** A bare venue
  name with no address can match the wrong place entirely — verified in practice:
  `"Olympic Aquatics Centre, Paris, France"` once geocoded to Vaux-sur-Mer, a coastal
  town 400+ km from Paris. `MAX_VENUE_DISTANCE_METERS` (50 km) discards any geocoded
  point that lands further than that from the searched city's own center — the event
  is kept, just without coordinates, rather than trusted with a wrong location.
- **A parsing bug (price text landing in `venue_name`) was found and fixed.** The
  reference scraper's venue-candidate selection just picked "the first text that isn't
  the date," with no exclusion for price-looking text — when a price string like
  `"From $151"` happened to appear before the real venue in a card's DOM order, it got
  grabbed as `venue_name` instead. Fixed by excluding price-matching text from the
  venue candidate search too. Verified against a synthetic reproduction of the exact
  failure and re-validated against the original reference HTML with 0 regressions.

## ⚠️ Known unresolved issue: a whole run returned the wrong city's events

**Observed once, root cause not yet confirmed.** A run for Paris came back with
**every one of 720 events** actually belonging to US cities (URLs like
`usher-east-rutherford-tickets-...`, `olivia-dean-new-york-tickets-...`) — not a few
mismatched cards, the entire result set. The URL-building logic itself was verified
correct (decoded the base64 `lat`/`lon` params — they matched Paris's real
coordinates exactly), so the request we sent was right; the page StubHub served back
apparently wasn't the geo-filtered explore results. Possible causes not yet
distinguished: StubHub not honoring the geo params that run, a session/cookie-state
difference, or something detecting the automated browser and serving a fallback/
generic "trending" listing instead.

**Mitigation in place, not a fix**: `discover()` now checks whether the searched
city's name appears anywhere in each event's own URL (`_url_matches_city()` — StubHub's
URLs reliably name the real city). If more than 80% of a run's events don't mention the
searched city at all, it's treated as a strong signal this run's results are wrong:
a warning is printed and `_save_debug_snapshot()` captures a screenshot + HTML to
`debug_snapshots/` (gitignored) for diagnosis. Individual events are **not** dropped
based on this check — a legitimate suburb/arena name (e.g. "La Défense Arena," genuinely
in the Paris region) might not literally contain the word "paris," so filtering
per-event on this basis risked silently losing real events. This is a detection/evidence
mechanism, not a resolution — check `debug_snapshots/` if the warning fires again, and
diagnose from there rather than guessing further.

## Required fields for validation

`source_event_id`, `event_name`, `date`.
