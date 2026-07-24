# Bandsintown connector

**Type:** scraper (Selenium + undetected-chromedriver) · **Coverage:** FR, GB, US, CA, BE ·
**Auth:** manual, one-time login required (see below)

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
       # "NewCity": ("geoname_id", "XX"),
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
- **This runs inside `discover()`, not `fetch()`**, reusing the same single browser tab
  already open for the listing page — visits each event page one at a time via
  `driver.get(url)`. This used to run 5 Playwright tabs concurrently (via
  `goto(url, wait_until="commit")`, which returns almost immediately so several tabs could be
  kicked off before collecting any of them), but that was reverted to a single sequential tab
  while debugging the Cloudflare blocking below — see that section for why, and for why this
  connector no longer uses Playwright at all.
- **JS snippets (`EXTRACT_JS`, `DATES_JS`, `EVENT_LOCATION_JS`) are unchanged** from the
  Playwright version — they're plain `() => {...}` arrow functions. A small `_evaluate()`
  helper runs them via Selenium's `driver.execute_script(f"return ({js})();")`, which is the
  plain-JS equivalent of Playwright's `page.evaluate(arrowFn)`, so none of the extraction
  logic itself needed to change, only how it's invoked.
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
- **Cloudflare was hard-blocking the whole domain** ("Sorry, you have been blocked") and
  sometimes showing an interactive "Attention Required" / "verify you are human" challenge —
  even in headed mode, even for a completely fresh, human-driven `login.py` session with no
  scraping behavior at all. Meanwhile the exact same URL opened in a normal, non-automated
  browser tab on the same machine/IP loaded fine every time. That combination ruled out IP
  reputation entirely: the block was specifically about the **Playwright-controlled browser
  connection itself** — Cloudflare's bot-management was detecting the CDP (Chrome DevTools
  Protocol) remote-control channel Playwright uses, not anything JS-visible. Two mitigations
  were tried first and both failed: patching `navigator.webdriver` and related JS properties
  via an init script, then switching to [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)
  (a maintained Playwright fork that patches CDP-level leaks) — still blocked, on a fresh
  `login.py` session, in headed mode, with a single tab. Since the whole Playwright family
  shares the same fundamental CDP-based control channel, no amount of patching within that
  family was going to fix this.
  **Current fix: this connector (and `login.py`) now run on Selenium +
  [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver)**
  instead — a different automation stack built specifically to evade this class of detection
  by patching the actual chromedriver binary, not just JS-level properties. Requires
  `pip install selenium undetected-chromedriver` (added to `requirements.txt`). **Status: not
  yet confirmed working** — test this the same way (`login.py` first, then the dispatcher) and
  update this note with the result.
- **`robots.txt` disallows `/choose-dates/`**, the listing path this scraper uses. This is a
  deliberate, acknowledged tradeoff (see `connector.yaml`'s notes) — not an oversight, but
  worth remembering given this connector now also hits many individual event pages per run
  on top of that, a meaningfully larger request volume than just the listing page.

## Required fields for validation

`source_event_id`, `event_name`, `date`.
