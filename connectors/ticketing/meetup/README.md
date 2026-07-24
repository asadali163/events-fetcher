# Meetup connector

**Type:** scraper (Playwright) · **Coverage:** FR · **Auth:** none

Scrapes Meetup's `/find/` page. One shared browser session per `discover()` call — applies
both size filters, scrolls to load results for each, then visits every found event's own
page for full detail. `fetch()` is a no-op passthrough, same reasoning as Dice/Bandsintown:
a fresh browser per event would be far too slow.

## How to add a new city

1. Go to `meetup.com/find/`, search for the city, and copy the `location=` query param out
   of the resulting URL — e.g. `?location=fr--Paris&...` → the slug is `fr--Paris`.
2. Add a line to `CITY_LOCATION_SLUGS` in `connector.py`:
   ```python
   CITY_LOCATION_SLUGS = {
       "Paris": "fr--Paris",
       "NewCity": "xx--CityName",
   }
   ```
3. Make sure the city is also in `core/cities.yaml` and its country is listed in this
   connector's `coverage` in `core/registry.yaml`.

Currently mapped: Paris.

## Deliberate scope: 50+ attendee events only

`SIZE_FILTERS` only covers the "50-100 attendees" and "100+ attendees" buckets — there is no
"under 50" bucket. **This is intentional**, confirmed when this connector was built, not a
gap to fix: smaller meetups are considered out of scope for this source on purpose. If that
changes later, a third filter (or removing size filtering and finding another way to page
through the unfiltered list) would need to be added.

## ⚠️ Known gap: no date-range filtering — refetches on an interval instead

Unlike every other connector, `discover()` currently has **no confirmed way to filter by
date** — Meetup's `/find/` page URL only takes `location`/`source`/`distance` params in this
implementation, no date range was found. `__init__` always sets `self.days_ahead = None`
regardless of what's passed in — this deliberately opts Meetup out of the dispatcher's usual
window-containment refetch check (`core.refetch_log.is_covered`), since "covers this date
window" doesn't mean anything for a source that ignores date windows entirely.

Instead, this connector declares `REFETCH_INTERVAL_DAYS = 10` (a placeholder — tune later).
The dispatcher (`core/dispatcher.py`) checks this via `core.refetch_log.is_recently_fetched()`:
if Meetup already succeeded for this city within the last `REFETCH_INTERVAL_DAYS` days, the
run is skipped outright; otherwise `discover()` runs and the success is logged with today's
date. This avoids the duplicate-scrape problem that window-containment logic would otherwise
cause here (re-scraping the exact same "current upcoming events" every time `days_ahead` grows
between runs, since the number was never actually acted on). If Meetup's find page does support
real date filtering (via another UI control or an undiscovered URL param), that's worth
investigating as a proper fix instead of this interval workaround.

## Key implementation details

- **Selectors are a mix of stable and fragile**: `data-testid="RevampedFilterButtonAttendeeCount"`
  and `data-event-label="Event Card"` are stable across deploys; the `ds2-*` classes used for
  venue/address text are auto-generated and **do change between Meetup site deploys** — if a
  field comes back empty, that's the first place to check.
- **`start_time`** comes from a real `<time datetime="...">` attribute — genuine ISO 8601, so
  this uses the shared `core.date_utils.split_iso_datetime()`, same as Ticketmaster/Songkick/
  OpenAgenda.
- **No coordinates from the source** — every event goes through
  `core.geocoding.get_coordinates()`, using the venue address (falling back to the venue name
  if no address was found).
- **`venue_country` is not hardcoded** — comes from the `country` parameter the dispatcher
  already resolved and passed into `discover(city, country)`, same pattern as Songkick/
  OpenAgenda/Bandsintown (see Dice's README for what happens when a connector hardcodes this
  instead).
- **`attendees`** is captured and preserved as an extra field, not part of the common schema
  used by other connectors yet — potentially useful input for a future Attendance Estimator
  pipeline stage.
- **`robots.txt` disallows automated access to `/find/`**, and Meetup's Terms of Service
  restrict scraping. This is a deliberate, acknowledged tradeoff (see `connector.yaml`'s
  notes) — same category as Bandsintown, not an oversight. Meetup's official GraphQL API
  (`api.meetup.com`) is the stable, ToS-compliant route if this needs to run regularly at
  volume.
- **No parallel tabs yet** — visits event pages one at a time, unlike Bandsintown. If this
  turns out to be slow in practice, the same batched-tabs technique built for Bandsintown
  (`goto(..., wait_until="commit")` across several tabs before collecting any of them) could
  be applied here too.

## Required fields for validation

`source_event_id`, `event_name`, `date`.
