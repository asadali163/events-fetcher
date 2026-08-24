# Luxembourg City connector

**Type:** scraper (Playwright + BeautifulSoup) · **Coverage:** LU · **Auth:** none

Scrapes luxembourg-city.com's "What's On" page. Unlike every other connector, this
site covers **exactly one city** — there's no multi-city search to configure.
`discover()` raises `ValueError` for any city other than `"Luxembourg City"`
(`SUPPORTED_CITY` in `connector.py`; the dispatcher already catches this and logs it
as a normal failed fetch, so it doesn't crash a multi-source run).

## ⚠️ `HEADLESS` must stay `False`

**Confirmed necessary, not just cautious**: with `HEADLESS = True`, the site's date
filter (the flatpickr calendar range picker) does not get applied, and `discover()`
ends up scraping the page's default/unfiltered event list instead of the requested
`[today, today + days_ahead]` window. Keep `HEADLESS = False` in `connector.py` — this
is the one connector so far where headless mode isn't just untested, it's confirmed
broken specifically for the date filter. The Python-side safety net (see below) still
drops any event whose own detail-page date falls outside the window, so a headless run
wouldn't return *wrong* dates — just far fewer events than expected, since most of an
unfiltered listing gets silently discarded by that safety net instead of being properly
requested from the site in the first place.

## Key implementation details

- **Two on-site filters get applied, in a specific order.** The date range (via the
  flatpickr calendar — see below) is applied *first*, then the "adults" audience filter
  is applied *last*, since selecting the audience can trigger a page reload that would
  otherwise drop the date range. After that reload, the audience filter is re-checked
  and reapplied if it got lost.
- **The audience filter fails loudly on purpose.** If `"adults"` can't be confirmed as
  selected, `_select_adults()` raises `RuntimeError` rather than silently scraping
  every audience — inherited directly from the reference scraper's own design ("the
  filter matters, so we fail loudly"). The dispatcher already catches this as a normal
  failed fetch.
- **The date filter is driven by clicking real calendar days**, not a URL parameter —
  there isn't one. `_click_flatpickr_day()` matches each day by its French aria-label
  (e.g. `"août 24, 2026"`, built by `_flatpickr_label()`), navigating forward
  month-by-month with the calendar's "next month" arrow until the target day is
  visible. This is why `HEADLESS = False` matters so much here specifically — more so
  than in other connectors, since there's no URL-based fallback if the UI interaction
  doesn't fully register.
- **A Python-side safety net re-checks every event's own date** against
  `[today, today + days_ahead]` after visiting its detail page, in case the on-site
  filter didn't fully apply. Events outside the window are dropped rather than kept
  with a wrong date.
- **Deliberately keeps only single-date events** (`_is_single_date_text()`) — skips
  date-range events like festivals spanning several weeks (e.g. `"05.08.2026 -
  01.09.2026"`). This is inherited as-is from the reference scraper's own explicit,
  documented scope choice, not an oversight to fix.
- **First-party coordinates, no geocoding needed.** Every event page has its own
  Google Maps link with an exact `lat,lon` in the URL (`MAPS_Q_RE`) — `normalize()`
  calls `core.locations_store.save_location()` directly to cache them, the same
  pattern as Bandsintown. No `core.geocoding` calls happen in this connector at all.
- **No clean venue name** — the address block on each event page (`_parse_location()`)
  is a single joined text blob (venue + street, comma-separated), not split into a
  separate venue name. `venue_name` is left `None` rather than guessing which part of
  the blob is the venue — same situation as Universe.
- **Event links are found heuristically** (`_extract_event_links()`): any internal
  anchor whose surrounding text (climbing up to 4 parent levels) contains a
  `DD.MM.YYYY`-shaped date. Inherited as-is from the reference scraper, which notes the
  real listing-card markup wasn't available to anchor on something more precise —
  worth revisiting if this ever needs tightening.
- **Extra fields, not part of the common schema yet**: `weekday`, `description`,
  `maps_url` — preserved since they're free and available.
- **Folder name has a typo** (`luxembory_specific`, missing the "u") — kept as-is since
  it's already the working folder; not renamed unprompted. The connector's `id` in
  `core/registry.yaml` is the correctly-spelled `luxembourg_city`.

## Required fields for validation

`source_event_id` (the event's own URL, since no separate numeric ID is exposed),
`event_name`, `date`.
