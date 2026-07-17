# Bandsintown connector

**Type:** scraper (Playwright) · **Coverage:** FR, GB, US, CA, BE · **Auth:** manual,
one-time login required (see below)

The most involved connector in the project. Start with the "Bandsintown, in detail" section
of the root `README.md` for the full story; this file focuses specifically on how to extend
and operate it day to day.

## How to add a new city

1. Look up the city's **GeoNames ID** — either search `geonames.org` directly, or read it
   off a URL: `geonames.org/2988507/paris.html` → the ID is `2988507`. **Watch for duplicate
   city names across countries** (there's a Paris, Texas and a Paris, France with different
   IDs) — pick the one you actually mean.
2. Add a line to `CITY_GEONAME_IDS` in `connector.py`, as a `(geoname_id, iso_country_code)`
   tuple:
   ```python
   CITY_GEONAME_IDS = {
       "Paris": ("2988507", "FR"),
       "London": ("2643743", "GB"),
       "Chicago": ("4887398", "US"),
       "Toronto": ("6167865", "CA"),
       "Brussels": ("2800866", "BE"),
       "Lyon": ("2996944", "FR"),
       "NewCity": ("geoname_id", "XX"),
   }
   ```
3. Make sure the city is also in `core/cities.yaml` and its country is listed in this
   connector's `coverage` in `core/registry.yaml`.

Currently mapped: Paris, London, Chicago, Toronto, Brussels, Lyon.

## One-time setup: logging in

Bandsintown gates its full "View all" result list behind a login wall. Before this connector
can return more than the first page of results for any city:

```
python connectors/ticketing/bandsintown/login.py
```

This opens a **real, visible** browser window — log in by hand (however you normally do),
then press Enter in the terminal when done. The session is saved to `bit_profile/`
(gitignored — treat it like a password). Re-run this script any time `discover()` reports
the session looks expired. This script is **never** called automatically by the dispatcher —
it's interactive by nature and has to stay that way.

## Key implementation details

- **The listing card's text alone isn't reliable.** The "artist before the date, venue after
  the date" heuristic (inherited from the original reference script) works for plain concert
  cards, but festival-type listings can show the festival or artist name where a venue
  should be, and never gives coordinates at all.
- **So `discover()` also visits every individual event's own page**, extracting a structured
  location block (venue name, street address, city, and exact coordinates — pulled straight
  out of a `latitude=`/`longitude=` query string on that page's city link, anchored on that
  href pattern rather than any CSS class, since Bandsintown's classes are build hashes that
  change every deploy). `normalize()` prefers this precise data, falling back to the listing
  card's guess only if a specific event's page didn't yield it.
- **This runs inside `discover()`, not `fetch()`**, using `DETAIL_TAB_COUNT` (5) tabs at once
  in the same already-open browser context. The trick: `goto(url, wait_until="commit")`
  returns almost immediately (navigation started, not finished), so kicking off navigation on
  every tab in a batch before collecting any of them lets the browser load them all
  concurrently in the background — same total requests as one-at-a-time, far less wall-clock
  waiting. No threads, no second browser instance (which would conflict with `bit_profile/`
  being locked by the first one).
- **`venue_country` comes from the event page's own city text** (e.g.
  `"Herk-de-stad, Belgium"` → country `"Belgium"`), falling back to the search country only
  if the detail page didn't yield one — this correctly handles events that turn out to be in
  a different city/country than the one searched (Bandsintown's "Brussels" search, for
  example, returns festivals from across Belgium, not just Brussels itself).
- **Dates use a private parser** (`_parse_bandsintown_date()`) — Bandsintown's format
  (`"Jul 17 • 4:00 PM"`) isn't ISO 8601 and omits the year, inferred from the requested
  window (handles Dec→Jan rollover).
- **No `type` field populated** — no genre/category data is available in what's scraped.

## Known open issues

- **`HEADLESS = True` currently returns 0 events**, while a visible browser (`HEADLESS =
  False`, the current setting) works fine for the same city. This is the classic signature
  of a site treating headless Chrome differently (bot detection, a blocked/challenge page,
  or different login behavior) — not yet diagnosed. `_save_debug_snapshot()` saves a
  screenshot + the page's HTML to `debug_snapshots/` (gitignored) whenever a run returns
  suspiciously few/zero events — check there first before investigating further. **Remember
  to flip `HEADLESS` back to `True` once this is resolved** — it's currently left visible
  specifically for debugging this issue.
- **`robots.txt` disallows `/choose-dates/`**, the listing path this scraper uses. This is a
  deliberate, acknowledged tradeoff (see `connector.yaml`'s notes) — not an oversight, but
  worth remembering given this connector now also hits many individual event pages per run
  on top of that, a meaningfully larger request volume than just the listing page.

## Required fields for validation

`source_event_id`, `event_name`, `date`.
