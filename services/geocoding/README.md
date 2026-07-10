# services/geocoding/

Resolves addresses/venues to lat/lng, timezone, and administrative boundaries, with Redis caching to avoid re-billing geocoding providers. Consumes EntityResolved, publishes EventGeocoded.

Internal layering (standard across every bounded context — see root ARCHITECTURE.md §4):

- `domain/` — entities/aggregates, value objects, domain events this context emits,
  repository interfaces (ports only).
- `application/` — command handlers (writes), query handlers (reads), inbound event
  handlers from other contexts.
- `infrastructure/` — repository implementations (adapters), messaging adapters,
  external API clients.
- `interface/` — FastAPI router (if applicable) and message-consumer entrypoints.
- `tests/` — unit and integration tests local to this context.

`domain/` never imports from `infrastructure/` or `interface/`.
