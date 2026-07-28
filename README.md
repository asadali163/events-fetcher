# GetEvents — Event Intelligence Platform

An event intelligence API (PredictHQ-style): pulls events — concerts, sports, conferences,
festivals, holidays, disasters, and more — from many heterogeneous sources worldwide,
cleans and enriches them, and will eventually serve them through an API. The customer is
other software (travel apps, pricing engines, staffing tools), not a person browsing a
listings page.

**This repository currently contains two layers**, and it's important to know which is
which:

1. **The target architecture** — a full DDD-organized folder skeleton (`services/`,
   `connector_sdk/`, `shared_kernel/`, etc.), documented in [`ARCHITECTURE.md`](./ARCHITECTURE.md).
   Mostly unimplemented — it's where the system is headed once it needs to run at scale
   (Postgres/PostGIS, RabbitMQ, Airflow, Kubernetes...).
2. **A working prototype** — real, running code that proves the core idea end to end with
   seven live sources before more are added. **This README documents layer 2 in full
   detail**, since that's what actually runs today.

**Keep this file up to date.** Every time a new source is added, update the connectors
table, the folder tree if new shared modules appear, and the limitations section if
something on it gets resolved.

---

## Setup

Requirements: Python 3.12+, a [Ticketmaster Developer](https://developer.ticketmaster.com/)
API key, a [LocationIQ](https://locationiq.com) API key (free tier), an
[OpenAgenda](https://developers.openagenda.com/) public key (free).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # needed for Playwright-based scrapers (Dice, Meetup)
# Bandsintown runs on Selenium + undetected-chromedriver instead (see its README) —
# undetected-chromedriver drives your real installed Chrome, no separate browser install needed.
```

Create a `.env` file in the project root (not committed — see `.gitignore`):
```
TICKETMASTER_API_KEY=your_real_key_here
LOCATIONIQ_API_KEY=your_real_key_here
OPENAGENDA_PUBLIC_KEY=your_real_key_here
```

Bandsintown additionally needs a one-time **manual** login before it can return more than
the first page of results — see its row in the sources table below.

Run the whole flow for a city:
```bash
python core/dispatcher.py Paris          # uses each source's own default date window
python core/dispatcher.py Paris 15       # forces a 15-day window across every source
```

---

## Sources implemented so far

| Source | Vertical | Type | Coverage | Notes |
|---|---|---|---|---|
| [Ticketmaster](connectors/ticketing/ticketmaster/) | ticketing | API | `global` | Discovery API, paginated, capped at 1,000 results/query |
| [Dice](connectors/ticketing/dice/) | ticketing | scraper (Playwright) | FR, CA | Browser session shared across the whole run; per-city browse-page slug |
| [Songkick](connectors/ticketing/songkick/) | ticketing | scraper (requests + BeautifulSoup) | HU, FR, BE, CA | No browser needed — JSON-LD embedded in listing pages gives full detail |
| [OpenAgenda](connectors/ticketing/openagenda/) | ticketing | API | FR | Searches agendas (calendars/publishers) by city name, then events per agenda via a geo bounding box; needs a per-city center point |
| [Bandsintown](connectors/ticketing/bandsintown/) | ticketing | scraper (Selenium + undetected-chromedriver) | FR, GB, US, CA, BE | See "Bandsintown, in detail" below — needs a one-time manual login, visits each event's own page for precise venue/coordinates, and knowingly operates outside `robots.txt`'s stated preference for its listing path |
| [Meetup](connectors/ticketing/meetup/) | ticketing | scraper (Playwright) | FR | Deliberately scoped to 50+ attendee events only; no date-range filter, so it refetches on a 10-day timer instead of a window; knowingly operates outside `robots.txt`'s stated preference for `/find/` |
| [Universe](connectors/ticketing/universe/) | ticketing | scraper (Playwright + BeautifulSoup) | NL, BE, CA (only Amsterdam mapped so far) | No date filter at all — `discover()` returns everything found, and `days_ahead` is forced to `None`; refetches on a 10-day timer instead (`REFETCH_INTERVAL_DAYS`), same pattern as Meetup; venue location resolved via `core.geocoding.geocode_place()`, which trusts the geocoder's own city/country over the searched city |

Cities currently mapped in `core/cities.yaml`: Chicago, Paris, London, Toronto, Lyon,
Budapest, Brussels, Amsterdam. Note each Playwright/API connector also keeps its *own*
city→id mapping (`CITY_SLUGS`, `CITY_METRO_PATHS`, `CITY_COORDINATES`, `CITY_GEONAME_IDS`,
`CITY_LOCATION_SLUGS`) — a city must be in **both** `cities.yaml` and a given connector's
own mapping for that
connector to actually cover it.

---

## What's actually in use today

```
GetEvents/
├── core/                                     the platform runtime
│   ├── cities.yaml / cities_loader.py        city name -> ISO country code
│   ├── registry.yaml / registry_loader.py    which sources cover which countries + how to load them
│   ├── locations_store.py / locations.db     cached venue coordinates (SQLite)
│   ├── geocoding.py                          cache-first coordinate lookup, LocationIQ fallback
│   ├── date_utils.py                         shared ISO 8601 date/time splitter
│   ├── raw_store.py / raw_events.db          untouched raw events, append-only ("bronze")
│   ├── normalized_store.py / normalized_events.db   every normalized event, append-only ("silver")
│   ├── dedup.py                              cross-source duplicate matching + merging
│   ├── canonical_store.py / canonical_events.db     one row per real event, upserted ("gold")
│   ├── refetch_log.py / refetch_log.db       tracks which (source, city) windows already ran
│   └── dispatcher.py                         ties everything together — the real entry point
│
├── connector_sdk/core/base_connector.py      BaseConnector — the contract every connector follows
│
├── connectors/ticketing/    each folder also has its own README.md — connector-specific
│   │                        details (how to add a city, quirks, known issues)
│   ├── ticketmaster/
│   │   ├── connector.yaml / connector.py     TicketmasterConnector
│   │   ├── ticketmaster_test.py              original prototype, kept for reference
│   │   └── tests/generate_fixture.py + fixtures/sample_event.json
│   ├── dice/
│   │   ├── connector.yaml / connector.py     DiceConnector
│   │   └── scraper.py                        original prototype, kept for reference
│   ├── songkick/
│   │   ├── connector.yaml / connector.py     SongkickConnector
│   │   └── scrapper_test.py                  original prototype, kept for reference
│   ├── openagenda/
│   │   ├── connector.yaml / connector.py     OpenAgendaConnector
│   │   └── openagenda_test.py                original prototype, kept for reference
│   ├── bandsintown/
│   │   ├── connector.yaml / connector.py     BandsintownConnector
│   │   ├── login.py                          manual, interactive — (re-)establishes bit_profile/
│   │   ├── bit_profile/                      gitignored — saved logged-in browser profile
│   │   ├── debug_snapshots/                  gitignored — screenshots/HTML saved when scraping returns 0 events
│   │   └── scraper_test.py                   original reference script, kept for reference
│   ├── meetup/
│   │   ├── connector.yaml / connector.py     MeetupConnector
│   │   └── scraper_test.py                   original reference script, kept for reference
│   └── universe/
│       ├── connector.yaml / connector.py     UniverseConnector
│       └── scraper_test.py                   original reference script, kept for reference
│
├── .env                                      API keys (not committed)
├── .gitignore
├── requirements.txt
├── ARCHITECTURE.md                           the full target architecture (mostly not built yet)
└── README.md                                 this file
```

Everything else at the root (`services/`, `shared_kernel/`, `platform_libs/`, `docs/`, etc.)
is the target-architecture skeleton from `ARCHITECTURE.md` and isn't wired up yet.

---

## Pipeline stages implemented so far

| Stage | Status |
|---|---|
| Connector | ✅ Done — 7 sources |
| Raw Event Storage | ✅ Done — `raw_store.py` |
| Normalizer | ⚠️ Logic exists per-connector, not yet a separate queue-driven stage (deliberate — see below) |
| Deduplicator | ⚠️ Done, but scoped to *within one dispatcher run* — see `core/dedup.py` |
| Entity Resolution | ❌ Not started |
| Geocoder | ⚠️ Logic exists (`core/geocoding.py`), not yet a separate stage |
| Category Classifier / Attendance Estimator / Impact Scoring | ❌ Not started |
| Canonical Event Database | ⚠️ `canonical_store.py` exists and is upserted, but keying is exact-match, not fuzzy cross-run — see Limitations |
| API | ❌ Not started |

The ⚠️ items are deliberate simplifications, not oversights: separating Normalizer/Geocoder
into independent stages only makes sense once there's a real message queue to justify it
(there isn't yet), and cross-run fuzzy deduplication is a harder problem than within-run
deduplication, tackled once there's a real reason to need it.

---

## The end-to-end flow, step by step

Running `python core/dispatcher.py Paris 15` does the following:

**1. Resolve the city to a country.** `cities_loader.get_country_for_city("Paris")` reads
`core/cities.yaml` → `"FR"`. Raises a clear error if the city isn't listed — never guesses.

**2. Find every source that covers that country.** `registry_loader.get_sources_for_country("FR")`
reads `core/registry.yaml`, returning every source whose `coverage` includes `"FR"` or is
`"global"`.

**3. For each matching source, check the refetch log first.** `refetch_log.is_covered(source, city, window_start, window_end)`
checks whether an earlier *successful* run for that exact source+city already covered this
entire date window. If so, that source is skipped entirely for this run — no network call,
no wasted API budget. Coverage is tracked per **source**, not per city, so a brand-new
source always runs on its first invocation regardless of other sources' history.

**4. Dynamically load and run the connector.** `dispatcher.load_connector(entry_point, days_ahead)`
imports the class from its `entry_point` string (e.g.
`"connectors.ticketing.dice.connector.DiceConnector"`) via `importlib` and instantiates it —
the dispatcher never hardcodes a connector's name, only reads it from `registry.yaml`. Then,
per raw event returned by `.discover(city, country)`:
- The raw event is saved untouched to `raw_store.py` (bronze layer) — nothing downstream
  ever sees unprocessed data directly.
- `.fetch()` → `.normalize()` → `.validate()` run in sequence. `normalize()` also handles
  that source's own date format (see "Date/time normalization" below) and resolves venue
  coordinates (from the source directly, or via `core/geocoding.py` on a cache miss).
- Every normalized event (valid or not) is saved to `normalized_store.py` (silver layer) —
  an audit trail of what normalization actually produced.
- Valid events are kept for this run's combined list.

A failure at any single step (a broken connector, one bad event, a failed fetch) is caught,
logged, and skipped — it never stops the rest of the run. Fetch outcomes (success/failure)
are logged back to the refetch log regardless.

**5. Deduplicate across all sources.** Once every source has run, `dedup.deduplicate_events()`
groups the full combined list into clusters where **all three** signals agree: same date,
venues within ~50m of each other, and similar-enough event names (see `core/dedup.py`).
Each cluster merges into one record (the most complete source's version, with a `sources`
list recording every contributor for traceability).

**6. Save and export.** Each merged event is upserted into `canonical_store.py` (gold layer)
keyed by city+date+event name+venue name. The same deduplicated list is also written to
`events_<city>.json` — convenient for a quick look, but `canonical_events.db` is the real,
persistent source of truth now.

---

## Date/time normalization

Every source's raw date format is different, and this is handled deliberately without one
"magic" auto-detecting parser — a generic helper for standard formats, and a source-specific
one for anything else:

- **`core/date_utils.split_iso_datetime()`** — for any source whose dates are already ISO
  8601 (Ticketmaster's `2026-08-01T00:00:00Z`, Songkick's `2026-07-15T20:00:00+0200`).
  Normalizes the small variations (trailing `Z`, offsets without a colon) before parsing, and
  correctly returns an empty `time` for a date-only value rather than a misleading midnight.
- **Source-specific parsers** — for anything that isn't standard (Dice's
  `"Mon 20 Jul, 7:15 pm CEST"`), a small private parser lives *inside that connector's own
  file* (`_split_dice_datetime()` in `dice/connector.py`), not in the shared module. A future
  source with yet another format gets its own parser the same way — the shared helper never
  needs to change, and no existing connector is at risk of breaking.

Every connector's `normalize()` outputs a common `date` (ISO `YYYY-MM-DD`, directly
comparable across sources — this is what deduplication's date-matching signal relies on) and
`time` (a source-formatted string, or `""` when no time was announced).

---

## Deduplication, in detail

`core/dedup.py` — see `deduplicate_events()`. Key things worth knowing:

- **All three signals must agree**, not any one alone: same `date`, venues within
  `DISTANCE_THRESHOLD_METERS` (currently 50m — tightened down from an initial 150m) via
  `haversine_meters()`, and event names scoring above `NAME_SIMILARITY_THRESHOLD` (currently
  0.6) via `difflib.SequenceMatcher`.
- **Clustering uses union-find**, not a single linear pass — so if A matches B and B matches
  C, all three end up in one cluster even if A and C don't directly match each other, and the
  result doesn't depend on input order.
- **An event with no coordinates on either side can never be matched on distance** — it'll
  never merge with anything, even if it's genuinely the same event another source reported.
  This is a real, known limitation, not a bug — worth remembering when investigating why two
  obviously-the-same events didn't merge.
- **This is scoped to within one dispatcher run.** It does not compare against events from
  previous runs — that's a harder problem (cross-run identity), deliberately not built yet.

---

## The three-tier storage model (bronze / silver / gold)

| Layer | File | What it holds | Write pattern |
|---|---|---|---|
| Bronze | `raw_store.py` / `raw_events.db` | Untouched raw events, exactly as each connector's `discover()` returned them (JSON blob) | Append-only |
| Silver | `normalized_store.py` / `normalized_events.db` | Every normalized event, valid or not, from every run | Append-only |
| Gold | `canonical_store.py` / `canonical_events.db` | One row per real-world event, post-deduplication | Upsert (exact-key: city+date+name+venue) |

All three have a `print_all_entries()` you can call directly, e.g. `python core/canonical_store.py`.
The gold layer's upsert key is an **exact** match, not fuzzy — it stops the same event from
piling up duplicate rows across separate runs as long as its name/venue text stays
consistent between runs, but it won't catch a reworded title next week. True cross-run fuzzy
matching would need to reuse `dedup.py`'s matching logic against the gold table itself — not
built yet.

**Schema migrations so far**: `end_date`/`end_time` and `type` were added to the silver and
gold tables after the fact. Both stores' `init_db()` includes a small `_add_column_if_missing()`
migration, so an existing database gets the new columns without losing rows — but you still
need to actually run `python core/normalized_store.py` and `python core/canonical_store.py`
once after pulling a schema change, since the dispatcher never calls `init_db()` itself.
`type` is only populated by sources that actually have genre/category data (Ticketmaster,
Songkick, OpenAgenda) — Dice and Bandsintown leave it `None`, matching "only if present."
`end_date`/`end_time` are currently only populated by OpenAgenda (its `nextTiming`/`firstTiming`
blocks have a real `end`); the others leave them `None` rather than guess.

---

## The locations cache, in detail

`core/locations_store.py` — SQLite, one table (`locations`), each row a `venue` (with
coordinates) or, in the future, a `city` (mapping to a country — not populated yet;
`cities.yaml` handles that role directly today).

- **Deduplication is automatic** via a `UNIQUE` `lookup_key` (normalized
  `place_type|name|city|country`) and `ON CONFLICT DO NOTHING` — saving the same venue twice
  never creates a duplicate row.
- **First-writer-wins** — a venue's coordinates, once saved, are never overwritten.
- **`core/geocoding.py`** wraps this with a cache-then-geocode pattern: check the cache
  first, and only call the LocationIQ API on a genuine miss, then save the result. Used by
  any connector whose source doesn't supply coordinates directly (Dice, Songkick when
  JSON-LD lacks geo data).
- **`geocode_place(address, name)`** (added for Universe) is a second entry point for
  sources that give one full address blob with no separately known city/country — instead
  of trusting the searched city, it returns whatever city/country LocationIQ itself
  resolves for that address (via `addressdetails=1`). This matters: Universe's explore
  search isn't confined to the literal searched city (an event scraped under an
  Amsterdam-area search actually listed a Haarlem address), the same lesson learned from
  Bandsintown's Brussels/Tomorrowland case. `get_coordinates()` is unchanged for every
  other connector; this is a separate function, not a modification to the existing one.

---

## Bandsintown, in detail

This connector is meaningfully more involved than the other four, for reasons worth
understanding before touching it:

- **Requires a one-time manual login.** Bandsintown gates its full "View all" result list
  behind a login wall. `login.py` (in this connector's folder) is a separate, interactive
  script you run **yourself, by hand** — never called by the dispatcher — that opens a real
  visible browser, lets you log in once, and saves the session into `bit_profile/`
  (gitignored, treat it like a password). Without it, `discover()` degrades gracefully to
  only the first page of results rather than failing outright.
- **The listing card alone isn't reliable.** Its "artist before the date, venue after the
  date" text-walking heuristic works for plain concert cards, but festival-type listings can
  show the festival or artist name where a venue would normally be, and it never gives
  coordinates at all. So `discover()` also **visits every individual event's own page**,
  extracting a structured location block (venue name, street address, city, and exact
  lat/long — pulled straight out of a `latitude=`/`longitude=` query string on that page's
  city link, no geocoding call needed when this succeeds). `normalize()` prefers this
  precise data and only falls back to the listing card's guess if a specific event's page
  didn't yield it.
- **This is done inside `discover()`, not `fetch()`**, one tab, one event at a time. It used
  to run 5 tabs concurrently (`goto()` with `wait_until="commit"` returns almost immediately,
  so kicking off navigation on every tab in a batch before collecting any of them let the
  browser load them all in the background at once) — reverted to sequential while debugging
  the Cloudflare issue below, to isolate request velocity as a variable.
- **This connector runs on Selenium + undetected-chromedriver, not Playwright.** Cloudflare
  was hard-blocking the whole domain, and eventually blocked even a completely fresh,
  human-driven `login.py` session in headed mode — while the same URL loaded fine in a normal
  browser tab on the same machine/IP. That combination meant it wasn't an IP-reputation issue
  at all, but Cloudflare detecting the Playwright/CDP automation connection itself, which no
  amount of JS-level patching (custom `navigator.webdriver` overrides, then
  [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)) could fix, since
  the whole Playwright family shares that same control channel. Switching to
  [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver)
  (a different automation stack, patched at the chromedriver-binary level) was the next
  thing tried — see `connectors/ticketing/bandsintown/README.md` for the full timeline and
  current status.
- **`robots.txt` disallows the `/choose-dates/` listing path this scraper uses.** This is a
  deliberate, acknowledged tradeoff (see `connector.yaml`'s notes), not an oversight — worth
  keeping in mind given this connector also now hits many additional individual event pages
  per run on top of that.

---

## How to add a new source (do this yourself, keep this section current)

1. **Get access first.** Sign up for the API, or open the site in a browser and look at real
   data, before writing any code.
2. **Decide the vertical and folder.** `connectors/<vertical>/<source_name>/` — reuse an
   existing vertical (`ticketing/`) if it fits, or create a new one if it doesn't.
3. **Add the city → country mapping** to `core/cities.yaml` if the city isn't already listed.
4. **Write `connector.py`** — a class inheriting `BaseConnector`
   (`connector_sdk/core/base_connector.py`), implementing:
   - `discover(city, country)` — fetch/scrape raw events. If the source needs a shared
     browser/session for the whole run (like Dice), do the *entire* scrape here and make
     `fetch()` a passthrough. If the listing itself isn't reliable enough and each event
     needs its own page visited for accurate detail (like Bandsintown), do that inside
     `discover()` too — several tabs at once in the same context if there are many events,
     using `goto(..., wait_until="commit")` to kick off navigation on each before collecting
     any of them, rather than a fresh browser per event.
   - `fetch(raw_event)` — a second per-event call, only if `discover()` doesn't already give
     full detail and per-event visits aren't already handled above.
   - `normalize(raw_event)` — map onto the common fields (`source`, `source_event_id`,
     `event_name`, `date`, `time`, `venue_name`, `venue_city`, `venue_country`,
     `venue_latitude`, `venue_longitude`, `url`, and optionally `type`/`end_date`/`end_time`
     when the source actually provides them — leave absent otherwise, never guess). For
     dates: reuse `core.date_utils.split_iso_datetime()` if the source is ISO 8601, otherwise
     write a small private parser next to this connector. For coordinates: use
     `core.geocoding.get_coordinates()` if the source doesn't supply its own; if it does,
     still call `core.locations_store.save_location()` directly to cache them for reuse.
   - `validate(normalized_event)` — check `REQUIRED_FIELDS` (typically
     `["source_event_id", "event_name", "date"]`) are present.
   - If the source requires a login, keep the interactive part **completely separate** from
     `discover()` — a manual script (see `bandsintown/login.py`) the user runs by hand once,
     never something the dispatcher triggers. `discover()` should check whether a saved
     session exists and degrade gracefully (fewer results, not a crash) if it doesn't.
5. **Write `connector.yaml`** — `id`, `type` (`api`/`scraper`), `coverage` (list of ISO
   country codes, or `"global"`), `entry_point` (the dotted class path).
6. **Register it in `core/registry.yaml`** — same `id`/`type`/`coverage`/`entry_point`.
7. **Run it** — `python core/dispatcher.py <City>` — and sanity-check `events_<city>.json`
   and `canonical_events.db` for that source's events before considering it done.
8. **Update this README**: add a row to the "Sources implemented so far" table, and update
   the folder tree if you added any new shared module.

---

## Current limitations (intentional, not oversights)

- **No cross-run fuzzy deduplication.** The gold layer's upsert key is an exact match — a
  slightly reworded event title between runs won't be recognized as the same event.
- **No "already fetched recently" tracking beyond the refetch log's own scope** — it prevents
  redundant `discover()` calls, but doesn't feed into deduplication decisions.
- **No retry/backoff on transient failures** (rate limits, timeouts) — a failed request is
  logged and skipped, not retried.
- **Only a handful of cities/sources exist.** Each Playwright/API connector only knows a few
  cities (see their own `CITY_SLUGS`/`CITY_METRO_PATHS`/`CITY_COORDINATES`/`CITY_GEONAME_IDS`);
  most of the world isn't covered yet.
- **`type`/`end_date`/`end_time` are only populated where a source actually provides them** —
  they're `None` for the rest, by design, not a bug (see the storage-model section above).
- **Bandsintown was being hard-blocked by Cloudflare regardless of headless/headed mode**,
  even for a fresh manual `login.py` session — traced to Cloudflare detecting the
  Playwright/CDP automation connection itself, not an IP or fingerprint issue. Now runs on
  Selenium + undetected-chromedriver instead; not yet confirmed working (see "Bandsintown, in
  detail" above). `_save_debug_snapshot()` still captures evidence in `debug_snapshots/` when
  a run returns suspiciously few/zero events.
- **Bandsintown knowingly operates outside `robots.txt`'s stated preference** for its listing
  path — an acknowledged tradeoff, not an oversight (see "Bandsintown, in detail" above).
- **Meetup has no date-range filter, so it refetches on a timer instead of a window** — no
  confirmed date-range filter was found on its find page, so `days_ahead` is always forced to
  `None` and the dispatcher instead skips re-running for a city until
  `REFETCH_INTERVAL_DAYS` (10, placeholder) days have passed since its last successful fetch.
  Also **deliberately scoped to 50+ attendee events only** — both are documented in
  `connectors/ticketing/meetup/README.md`, not oversights.
- **Meetup also knowingly operates outside `robots.txt`'s stated preference** for `/find/` —
  same acknowledged tradeoff category as Bandsintown.
- **Universe has no date filtering at all, by design** — `discover()` returns every event
  it finds (results aren't even chronologically ordered, so a partial client-side filter
  wouldn't be safe anyway) and `days_ahead` is forced to `None`, same as Meetup. Also
  refetches on a 10-day timer (`REFETCH_INTERVAL_DAYS`) instead of a date window, same
  reasoning as Meetup. Documented in `connectors/ticketing/universe/README.md`.
- **Not yet wired into the target architecture** in `ARCHITECTURE.md` — no Airflow
  scheduling, no RabbitMQ messaging, no Postgres/PostGIS, no API layer. This prototype exists
  specifically to validate the approach before that investment.

---

## Further reading

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — the full target DDD architecture and how it's
  meant to scale to 1000+ connectors and millions of events.
- [`docs/How_GetEvents_Works.pdf`](./docs/How_GetEvents_Works.pdf) — a plain-English
  walkthrough of the pipeline concept, no code. Predates the current 5-source/dedup state —
  treat specifics as dated.
- [`docs/GetEvents_Project_Status.pdf`](./docs/GetEvents_Project_Status.pdf) — a status and
  roadmap briefing. Also predates the current state.
