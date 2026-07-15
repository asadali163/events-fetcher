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
   three live sources before more are added. **This README documents layer 2 in full
   detail**, since that's what actually runs today.

**Keep this file up to date.** Every time a new source is added, update the connectors
table, the folder tree if new shared modules appear, and the limitations section if
something on it gets resolved.

---

## Setup

Requirements: Python 3.12+, a [Ticketmaster Developer](https://developer.ticketmaster.com/)
API key, a [LocationIQ](https://locationiq.com) API key (free tier).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # only needed for Playwright-based scrapers (Dice)
```

Create a `.env` file in the project root (not committed — see `.gitignore`):
```
TICKETMASTER_API_KEY=your_real_key_here
LOCATIONIQ_API_KEY=your_real_key_here
```

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
| [Songkick](connectors/ticketing/songkick/) | ticketing | scraper (requests + BeautifulSoup) | HU, FR | No browser needed — JSON-LD embedded in listing pages gives full detail |

Cities currently mapped in `core/cities.yaml`: Chicago, Paris, London, Toronto, Lyon,
Budapest.

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
├── connectors/ticketing/
│   ├── ticketmaster/
│   │   ├── connector.yaml / connector.py     TicketmasterConnector
│   │   ├── ticketmaster_test.py              original prototype, kept for reference
│   │   └── tests/generate_fixture.py + fixtures/sample_event.json
│   ├── dice/
│   │   ├── connector.yaml / connector.py     DiceConnector
│   │   └── scraper.py                        original prototype, kept for reference
│   └── songkick/
│       ├── connector.yaml / connector.py     SongkickConnector
│       └── scrapper_test.py                  original prototype, kept for reference
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
| Connector | ✅ Done — 3 sources |
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
venues within ~150m of each other, and similar-enough event names (see `core/dedup.py`).
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
  `DISTANCE_THRESHOLD_METERS` (currently 50m) via `haversine_meters()`, and event names
  scoring above `NAME_SIMILARITY_THRESHOLD` (currently 0.6) via `difflib.SequenceMatcher`.
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
     `fetch()` a passthrough.
   - `fetch(raw_event)` — a second per-event call, only if `discover()` doesn't already give
     full detail.
   - `normalize(raw_event)` — map onto the common fields (`source`, `source_event_id`,
     `event_name`, `date`, `time`, `venue_name`, `venue_city`, `venue_country`,
     `venue_latitude`, `venue_longitude`, `url`). For dates: reuse
     `core.date_utils.split_iso_datetime()` if the source is ISO 8601, otherwise write a
     small private parser next to this connector. For coordinates: use `core.geocoding.get_coordinates()`
     if the source doesn't supply its own; if it does, still call `core.locations_store.save_location()`
     directly to cache them for reuse.
   - `validate(normalized_event)` — check `REQUIRED_FIELDS` (typically
     `["source_event_id", "event_name", "date"]`) are present.
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
- **Only a handful of cities/sources exist.** Dice and Songkick each only know a few cities
  (see their `CITY_SLUGS`/`CITY_METRO_PATHS`); most of the world isn't covered yet.
- **Not yet wired into the target architecture** in `ARCHITECTURE.md` — no Airflow
  scheduling, no RabbitMQ messaging, no Postgres/PostGIS, no API layer. This prototype exists
  specifically to validate the approach before that investment.

---

## Further reading

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — the full target DDD architecture and how it's
  meant to scale to 1000+ connectors and millions of events.
- [`docs/How_GetEvents_Works.pdf`](./docs/How_GetEvents_Works.pdf) — a plain-English
  walkthrough of the pipeline concept, no code. Predates the current 3-source/dedup state —
  treat specifics as dated.
- [`docs/GetEvents_Project_Status.pdf`](./docs/GetEvents_Project_Status.pdf) — a status and
  roadmap briefing. Also predates the current state.
