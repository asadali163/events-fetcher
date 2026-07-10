# services/

Bounded Contexts — each independently deployable and scalable, each owning its own schema and
communicating with the others only through domain events (RabbitMQ) or well-defined sync APIs.
See root `ARCHITECTURE.md` §4 for the shared internal DDD layering and §5 for the full
communication map.

| Context | Responsibility | Subdomain type |
|---|---|---|
| `ingestion/` | Runs connectors, owns Raw Event Storage | Supporting |
| `normalization/` | Raw → canonical schema mapping | Supporting |
| `deduplication/` | Fingerprinting & duplicate clustering | **Core** |
| `entity_resolution/` | Venue/Performer/Organizer master data | **Core** |
| `geocoding/` | Address → lat/lng/timezone/boundary | Supporting |
| `classification/` | Category/taxonomy classifier | **Core** |
| `attendance_estimation/` | Attendance/demand forecasting | **Core** |
| `impact_scoring/` | Composite impact score | **Core** |
| `catalog/` | Canonical Event DB — golden record, lifecycle | Supporting |
| `search_indexer/` | Projects catalog → Elasticsearch (CQRS read model) | Supporting |
| `api_gateway/` | Public/partner FastAPI surface | Supporting |
| `identity_access/` | Tenants, API keys, quotas, usage metering | Generic |
