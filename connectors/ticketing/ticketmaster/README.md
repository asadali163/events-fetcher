# Ticketmaster connector

**Type:** API (Discovery API v2) · **Coverage:** `global` · **Auth:** `TICKETMASTER_API_KEY` in `.env`

The simplest connector in the project — a real API that already resolves city names
globally, paginates cleanly, and gives full detail (including coordinates) in one call.

## How to add a new city

Nothing to do here at all. Unlike every other connector in this project, Ticketmaster has
**no city-mapping dictionary** — `discover(city, country)` passes the city name straight to
Ticketmaster's own `city` search parameter, and their API resolves it. The only thing that
needs updating is `core/cities.yaml` (the city → country mapping the *dispatcher* uses to
decide this connector applies at all) — if the city is already in there, Ticketmaster will
automatically be queried for it on the next run, no code change required.

## Key implementation details

- **Pagination cap**: Ticketmaster hard-rejects (HTTP 400) any request past the 1,000th
  result of a single search rather than returning an empty page. `MAX_ACCESSIBLE_RESULTS`
  stops the loop before that happens and prints a warning if the true result count was
  larger than what could be retrieved — narrowing `days_ahead` makes this less likely for
  very large cities.
- **`fetch()` is a no-op** — the Discovery API's search results already include full event
  detail, so there's nothing extra to fetch per event.
- **Coordinates come directly from the API** (`venue.location.latitude/longitude`) — no
  geocoding call needed. `_save_venue_location()` still caches them via
  `core.locations_store.save_location()` so other connectors can reuse them for the same
  venue.
- **`type`** comes from `classifications[0].segment.name` (e.g. `"Music"`, `"Sports"`,
  `"Arts & Theatre"`) — a real category straight from Ticketmaster, not inferred.
- **Dates** use the shared `core.date_utils.split_iso_datetime()` — Ticketmaster's
  `dates.start.dateTime` is standard ISO 8601.
- **`days_ahead` default is 30** (wider than most other connectors, since Ticketmaster's
  pagination cap is generous enough to absorb it for most cities).

## Required fields for validation

`source_event_id`, `event_name`, `date` — `venue_name` is deliberately *not* required, since
some listings genuinely lack one.
