# SportsEvents24 connector

**Type:** API (free open beta, no key) · **Vertical:** sports · **Coverage:** FR (per
`core/registry.yaml`; a city also needs a boundary file — only Paris so far) · **Auth:** none

Fixtures for 20 sports from <https://sportsevents24.com/api>: football, basketball,
handball, rugby, volleyball, ice hockey, futsal and more (full list in `SPORTS` in
`connector.py`). `api_test.py` in this folder is the original test script, kept for
reference.

## How a city run works

The API can only be asked for **one country + one sport** at a time — there is no city
search. So a run for Paris:

1. **`discover()`** fetches every sport for the whole of France over the window and returns
   all of it. The dispatcher saves all of that to **`raw_events.db`**.
2. **`normalize()`** geocodes each venue (the API gives no coordinates) and the dispatcher
   saves every normalized event — still the whole country — to **`normalized_events.db`**.
3. **`validate()`** rejects any event whose venue is outside `boundaries/Paris.kml`. The
   dispatcher only sends valid events on to deduplication and **`canonical_events.db`**, so
   the canonical store gets **Paris only**.

That split needed no change to `core/`. Side effect: the dispatcher prints one
`skipping event, missing: ['venue inside Paris boundary']` line per out-of-city event —
expected, not an error.

## How to add a city

1. Draw the city's boundary as a polygon (e.g. in Google Earth) and save it as
   `boundaries/<City>.kml` — the file name must match the city name in `core/cities.yaml`
   exactly (e.g. `boundaries/Lyon.kml`).
2. Make sure the city is in `core/cities.yaml` and its country is in this connector's
   `coverage` in `core/registry.yaml`.

Without a boundary file, `discover()` raises and the run logs this source as failed — the
same way other connectors fail for a city they don't have mapped.

## Limits and how they're handled

Free allowance, **per IP address**: 100 data requests per UTC day, **5,000 records per UTC
month**, 10 requests per minute, max 100 records per page. Every record counts — including
events outside the city, which is most of a country-wide fetch.

- **Before fetching**, `/api/v1/account` (free) reports what's left on the current IP.
- **Budget:** `MAX_RECORDS_PER_IP` (1000) — a soft per-IP share of the monthly allowance (see
  *Proxy rotation* below for what happens when it's reached).
- **Requests are spaced 6.5 s apart** (10/minute limit).
- **Sports are fetched round-robin**, one page each per pass, and the first page of each sport
  gets an equal share of the budget — so all 20 sports are sampled even on a small budget,
  rather than football using it all.
- **429 with a short `Retry-After`** (per-minute limit): waited out and retried.
- **Every page is cached** in `sportsevents24_raw_<CC>_<from>_<to>.json` in the project root
  (gitignored by the existing `/*.json` rule), together with each sport's paging cursor. A
  later run for the same window resumes from the cursor and never pays for a page twice.
  Cursors expire after 24 h; an expired one restarts that sport from page 1. The file format
  is the same one `api_test.py` writes, so the two share pages.
- **An API error on one sport** is recorded in the cache but leaves the sport incomplete, so a
  later run retries it (it isn't treated as done).

### Proxy rotation (not covered by this README)

`connector.py` was changed to switch to another configured proxy/IP when a limit or the
per-IP budget is reached, instead of stopping there. That logic, its configuration and its
behaviour are not documented or tested here — see the module docstring in `connector.py`.
SportsEvents24 states its free limits per IP address; fetching past them through other IPs is
outside the allowance they publish.

### A partial fetch and the refetch log

When a run stops early, the log says **PARTIAL**. The dispatcher still logs the window as
successfully fetched, so a normal re-run would skip this source. To continue, run again with
**"Re-fetch even if already fetched recently"** ticked in the dashboard — the connector resumes
from the cache. (Re-runs re-return cached events too, so the append-only raw/normalized stores
get repeat rows, the same as re-running any other source; the canonical store upserts.)

## Geocoding

Locations are free text such as
`GYMNASE DU COMPLEXE SPORTIF, ROUTE DE HAM SOUS VARSBERG, 57890, PORCELETTE`. Each unique
location is tried, most precise first, via `core.geocoding.geocode_place()` (cached in
`locations.db`, LocationIQ only on a miss):

| Try | Query | `geo_precision` |
|---|---|---|
| 1 | the full text + country | `address` |
| 2 | without the venue name at the front | `street` |
| 3 | postcode + town | `town` (town centre — least precise) |

- A venue shared by many fixtures is looked up once per run.
- **Events whose location mentions the city are returned first**, so they're geocoded first:
  if LocationIQ's own daily limit (~5,000 free calls) runs out mid-run, the events that can
  reach the canonical store are already done.
- If 25 venues in a row can't be geocoded at all, the connector assumes LocationIQ's limit is
  reached and uses cached coordinates only for the rest of the run.
- Events with no location (about 16% in a real handball sample) can't be placed, so they never
  reach the canonical store.

## Normalized fields

Standard fields: `source_event_id`, `event_name` (`"HOME vs AWAY"`), `date` / `time` (local
time from `localStartsAt`, else UTC with a `UTC` suffix), `venue_name` (first part of the
location text), `venue_address`, `venue_city` / `venue_country` (from the geocoder),
`venue_latitude` / `venue_longitude`, `type` (sport slug, e.g. `handball`). `url` is always
`None` — the API doesn't give an event page link.

Sports extras: `sport`, `tournament`, `tournament_id`, `team_home`, `team_away`,
`starts_at_utc`, `timezone`, `result`, `status` (`upcoming` / …), `geo_precision`.

## Known limitations

- **The monthly allowance is the real constraint.** A real handball-only sample filled 100
  records with about 2.5 days of French fixtures; all 20 sports over 14 days for France is
  likely more than the whole month's 5,000. Fetching by `tournament_id` for Paris-relevant
  tournaments only would spend far less, but isn't built yet.
- **Same country data per city.** Every FR city run fetches the same France-wide data; the
  cache is shared by window, so a second city on the same day reuses it instead of paying again.
- **`days_ahead` is capped at 30** (API limit).
