# connector_sdk/

The plugin framework that makes 1000+ connectors tractable. This is an SDK, not a bounded
context.

- `core/` — `BaseConnector` contract (`discover()`, `fetch()`, `normalize()`, `validate()`),
  the plugin registry/loader (manifest-driven discovery), and execution lifecycle
  (retry/backoff, per-source rate limiting).
- `manifest_schema/` — schema for each connector's `connector.yaml` (source metadata,
  schedule, category, auth requirements, rate limits).
- `testing/` — contract-test harness + fixtures every connector must pass before merge.
- `cli/` — scaffolding generator so adding a connector is one command plus four methods.

Connectors are loaded dynamically by a single generic ingestion worker at runtime — they are
config/data, not separate deployables. See root ARCHITECTURE.md §6.
