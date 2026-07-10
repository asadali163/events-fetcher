# shared_kernel/

The DDD **Shared Kernel** — the small, deliberately-minimized set of concepts every bounded
context agrees on. Changes here require cross-team sign-off since they ripple across every
context; keep this folder as small as possible.

- `domain/` — shared value objects: GeoPoint, TimeRange, EventId, SourceRef, Confidence.
- `domain_events/` — canonical schemas for events published on the RabbitMQ bus between
  contexts (RawEventIngested, EventNormalized, EventDeduplicated, EntityResolved,
  EventGeocoded, EventClassified, AttendanceEstimated, ImpactScored, EventCanonicalized...).
- `contracts/` — cross-context API contracts (OpenAPI/Avro/JSON-Schema).
- `observability/` — structured logging, tracing, and metrics conventions used by all
  services.
