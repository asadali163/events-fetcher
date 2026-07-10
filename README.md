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
   one live source (Ticketmaster) before more sources are added. **This README documents
   layer 2 in full detail**, since that's what actually runs today.

---

## Setup

Requirements: Python 3.12+, a [Ticketmaster Developer](https://developer.ticketmaster.com/)
API key.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root (not committed — see `.gitignore`):
```
TICKETMASTER_API_KEY=your_real_key_here
```

Run the whole flow for a city:
```bash
python core/dispatcher.py Chicago
```

---

## What's actually in use today

```
GetEvents/
├── core/                                    the platform runtime — see below
│   ├── cities.yaml / cities_loader.py       city name -> ISO country code
│   ├── registry.yaml / registry_loader.py   which sources cover which countries
│   ├── locations_store.py / locations.db    cached venue coordinates (SQLite)
│   └── dispatcher.py                        ties everything together — the real entry point
│
├── connector_sdk/core/base_connector.py     BaseConnector — the contract every connector follows
│
├── connectors/ticketing/ticketmaster/
│   ├── connector.yaml                       manifest: id, type, coverage, entry_point
│   ├── connector.py                         TicketmasterConnector — the real, formal plugin
│   ├── ticketmaster_test.py                 original throwaway prototype, kept for reference
│   └── tests/
│       ├── generate_fixture.py              one-off script to save a real API response
│       └── fixtures/sample_event.json       a real captured Ticketmaster event, for testing
│
├── .env                                     TICKETMASTER_API_KEY (not committed)
├── .gitignore
├── requirements.txt
├── ARCHITECTURE.md                          the full target architecture (mostly not built yet)
└── README.md                                this file
```

Everything else at the root (`services/`, `shared_kernel/`, `platform_libs/`, `docs/`, etc.)
is the target-architecture skeleton from `ARCHITECTURE.md` and isn't wired up yet.

---

## The end-to-end flow, step by step

Running `python core/dispatcher.py Chicago` does the following, in order:

**Step 1 — resolve the city to a country.**
`dispatcher.get_events_for_city("Chicago")` calls `core/cities_loader.get_country_for_city("Chicago")`,
which reads `core/cities.yaml` and returns `"US"`. If the city isn't listed yet, it raises a
clear error telling you to add a line for it — it never guesses.

**Step 2 — find every source that covers that country.**
`core/registry_loader.get_sources_for_country("US")` reads `core/registry.yaml` and returns
every source entry whose `coverage` includes `"US"`, or is the literal string `"global"`.
Today that's just Ticketmaster; adding a second source here means it's included in this
list automatically, with no changes to any code.

**Step 3 — dynamically load each matching source's code.**
For every matching entry, `dispatcher.load_connector(entry_point)` takes the `entry_point`
string from the registry (e.g. `"connectors.ticketing.ticketmaster.connector.TicketmasterConnector"`),
splits it into a module path and a class name, imports the module with `importlib`, and
instantiates the class. This is what lets the dispatcher run any connector without ever
hardcoding its name — it only ever knows connectors as strings in data.

**Step 4 — run the same four-step contract on each connector.**
`dispatcher.run_connector(source, city)` calls, in order, on the instantiated connector:
- **`.discover(city)`** — hits the source's real API (or, for a future scraper, its website)
  and returns raw events for that city.
- **`.fetch(raw_event)`** — a second call for full detail, if the source's `discover()`
  doesn't already include it. For Ticketmaster this is a pure passthrough, since its search
  results already contain everything needed.
- **`.normalize(raw_event)`** — reshapes one raw event into the platform's common event
  format (name, start time, venue, category, coordinates, etc.). As a side effect, it also
  calls `core.locations_store.save_location()` to cache the venue's coordinates — so the
  next time any event references that same venue, no re-lookup is needed.
- **`.validate(normalized_event)`** — checks required fields are present, returns
  `(is_valid, missing_fields)`.

Every one of these calls is wrapped so that **one bad event, or one entirely broken source,
never stops the whole run** — a failure is logged and skipped, and everything else keeps
going.

**Step 5 — merge and save.**
All valid, normalized events from every matching source are combined into a single list and
written to `events_<city>.json` in the project root (overwritten on every run — this is a
temporary output format, see Limitations below).

---

## The Ticketmaster connector, in detail

`connectors/ticketing/ticketmaster/connector.py` defines `TicketmasterConnector`, a subclass
of `BaseConnector`. Key implementation details worth knowing:

- **Pagination is capped at 1,000 results.** Ticketmaster hard-rejects (HTTP 400) any request
  past the 1,000th result of a single search — it doesn't return an empty page, it errors.
  `MAX_ACCESSIBLE_RESULTS` stops the loop before that happens, and prints a warning if the
  true result count for a city/window was larger than what could be retrieved. In practice,
  this means very large cities over long date windows may not return every event — narrowing
  the date window (`days_ahead`) makes this far less likely.
- **`fetch()` is a no-op** for this source specifically — Ticketmaster's Discovery API returns
  full event detail directly from search, so there's no second call to make. Other connectors
  (especially scrapers, where a listing page often links to a separate detail page) will
  likely need a real implementation here.
- **`REQUIRED_FIELDS`** for validation are currently just `source_event_id`, `event_name`, and
  `start_datetime` — venue is *not* required, since some listings lack one.
- **`connector.yaml`** records this connector's metadata: `coverage: global` (Ticketmaster
  covers most countries, so it's simpler to treat it as global rather than enumerate every
  country code), and `entry_point`, the exact string the dispatcher uses to load this class.

`ticketmaster_test.py`, in the same folder, is the original hand-written prototype this
connector was formalized from. It's kept only as a reference for how the logic was first
proven out — `connector.py` is the one actually used by the dispatcher.

---

## The locations cache, in detail

`core/locations_store.py` is a small SQLite database (`locations.db`, gitignored) with one
table, `locations`. Each row is either a `venue` (with coordinates) or, in the future, a
`city` (mapping to a country — not populated yet; `cities.yaml` currently handles that role
directly). Key behaviors:

- **Deduplication is automatic.** Every row's `lookup_key` (a normalized, lowercased
  `place_type|name|city|country` string) has a `UNIQUE` constraint, and inserts use
  `ON CONFLICT(lookup_key) DO NOTHING`. Saving the same venue twice — even across separate
  runs of the dispatcher — never creates a duplicate row.
- **First-writer-wins.** If a venue's coordinates are ever saved again with slightly
  different values, the original saved values are kept, not overwritten.
- **Why so few rows relative to event count is normal.** Many events share the same venue
  (a stadium hosts dozens of games, a theater hosts many shows), so the count of unique
  venues will always be much smaller than the count of events — this is the cache working
  as intended, not data loss.

---

## How to extend this

**Add a new city:** add one line to `core/cities.yaml` (`CityName: XX`, its ISO country
code). Nothing else needs to change — no code, no other file.

**Add a new source that covers a country not yet listed:** add an entry to
`core/registry.yaml` with its `id`, `type` (`api` or `scraper`), `coverage`, and — once
you've built its connector — its `entry_point`. If it's a scraper with a different URL per
country, list them under `urls:` (see the commented-out examples in that file).

**Build a new connector:** create a folder under `connectors/<vertical>/<source_name>/`
with a `connector.py` (a class inheriting `BaseConnector` from
`connector_sdk/core/base_connector.py`, implementing `discover()/fetch()/normalize()/validate()`)
and a `connector.yaml` manifest, following the Ticketmaster connector as a template. One
nuance for scraper-type sources: `registry.yaml`'s `urls` are per-*country*, not per-city, so
a scraper's `discover(city)` will typically need to fetch broadly for the country and then
filter results down to the requested city locally, rather than passing city as a native
query parameter the way Ticketmaster's API allows.

---

## Current limitations (intentional, not oversights)

- **Only one real connector exists** (Ticketmaster). `registry.yaml` has commented-out
  scraper examples showing the intended shape for the next ones.
- **Output is a JSON file per city, overwritten every run** — not yet a persistent database.
  The plan is to replace this with a proper events store (likely SQLite initially, same
  pattern as `locations_store.py`, before any move to Postgres).
- **No "already fetched recently" tracking.** Running the dispatcher for the same city twice
  in a row re-fetches everything from scratch. A fetch-history log is planned, deliberately
  deferred until the testing phase is over so repeated manual runs aren't blocked.
- **No deduplication across sources.** If two different sources report the same real-world
  event, both currently appear separately in the combined output — merging them is the
  future Deduplicator pipeline stage, not the dispatcher's job.
- **No retry/backoff on transient failures** (rate limits, timeouts) — a failed request is
  currently logged and skipped, not retried.
- **Not yet wired into the target architecture** in `ARCHITECTURE.md` — no Airflow
  scheduling, no RabbitMQ messaging, no Postgres/PostGIS canonical database. This prototype
  exists specifically to validate the approach before that investment.

---

## Further reading

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — the full target DDD architecture and how it's
  meant to scale to 1000+ connectors and millions of events.
- [`docs/How_GetEvents_Works.pdf`](./docs/How_GetEvents_Works.pdf) — a plain-English
  walkthrough of the pipeline concept, no code.
- [`docs/GetEvents_Project_Status.pdf`](./docs/GetEvents_Project_Status.pdf) — a status and
  roadmap briefing.
