# connectors/

Plugin implementations, grouped by vertical for discoverability. Each connector folder is
self-contained: its own `connector.yaml` manifest, code, config/mapping tables, and fixtures
for contract tests — implementing the contract defined in `connector_sdk/`.

Verticals present today (illustrative, not exhaustive — designed to grow past 1000):
- `ticketing/` — Ticketmaster, Eventbrite, Bandsintown, Songkick, Meetup
- `government_calendars/` — public holiday feeds
- `academic_calendars/` — university calendars
- `tourism_boards/`
- `venues_and_convention_centers/`
- `weather_and_disaster/` — NOAA, GDACS
- `airport_delays/` — FAA, Eurocontrol
- `health_advisories/` — WHO
