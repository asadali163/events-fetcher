# Event Intelligence Platform — Architecture

This document defines the **project structure only**. No implementation code is included.
It explains the responsibility of every folder and how services communicate with each other.

The system is organized as a **DDD monorepo of independently deployable bounded contexts**,
not a single monolithic app and not 1000 separate microservices. The pipeline described in
the brief:

```
Connector → Raw Event Storage → Normalizer → Deduplicator → Entity Resolution → Geocoder
→ Category Classifier → Attendance Estimator → Impact Scoring → Canonical Event DB → API
```

maps 1:1 onto bounded contexts under `services/`, each communicating via domain events over
RabbitMQ (choreography), with Airflow reserved for scheduling/orchestration concerns rather
than the inter-stage data flow itself.

---

## 1. Strategic DDD Classification

Every folder investment level is justified by where it sits in this table:

| Subdomain type | Contexts | Rationale |
|---|---|---|
| **Core Domain** (invest most — this IS the product) | Deduplication, Entity Resolution, Classification, Attendance Estimation, Impact Scoring | These turn "a list of events" into "event intelligence." Hardest to buy off-the-shelf, biggest competitive moat. |
| **Supporting Subdomain** (bespoke, necessary, not the moat) | Ingestion, Normalization, Geocoding, Catalog, Search Indexer | Every events business needs these, but they don't differentiate PredictHQ-like products from each other. |
| **Generic Subdomain** (minimize investment, consider buying) | Identity/Access & Billing, Observability | Solved problems — Auth0/Kong/Stripe/OpenTelemetry-grade tooling exists; build only thin glue. |

This classification drives where senior engineering time goes: Core Domain contexts get the
richest domain models; Supporting and Generic contexts stay intentionally thin.

---

## 2. Top-Level Project Structure

```
GetEvents/
├── README.md
├── ARCHITECTURE.md
│
├── docs/                        # ADRs, C4 diagrams, ubiquitous language glossary
├── infra/                       # IaC: Dockerfiles, Kubernetes manifests, Helm charts
│
├── shared_kernel/               # DDD Shared Kernel — cross-context contracts, changed by agreement only
│   ├── domain/                  # Shared value objects: GeoPoint, TimeRange, EventId, SourceRef, Confidence
│   ├── domain_events/           # Canonical event-schema contracts published on the bus
│   ├── contracts/               # OpenAPI/Avro/JSON-Schema definitions between contexts
│   └── observability/           # Structured logging/tracing/metrics conventions
│
├── platform_libs/               # Generic technical libraries — zero domain knowledge
│   ├── db/                      # Postgres/PostGIS session & engine factories
│   ├── messaging/               # RabbitMQ publisher/consumer wrappers, retry/DLQ helpers
│   ├── caching/                 # Redis client wrappers, cache-aside helpers
│   ├── search/                  # Elasticsearch client wrapper, index helpers
│   ├── object_storage/          # MinIO/S3 client wrapper
│   └── config/                  # Settings/secrets loading
│
├── connector_sdk/                     # The Plugin Framework — makes 1000+ connectors tractable
│   ├── core/                          # BaseConnector ABC, plugin registry, execution lifecycle
│   ├── manifest_schema/               # Schema for each connector's connector.yaml manifest
│   ├── testing/                       # Contract-test harness every connector must pass
│   └── cli/                           # Scaffolding generator for new connectors
│
├── connectors/                         # Plugin implementations — grows to 1000+, by vertical
│   ├── ticketing/                      # ticketmaster/, eventbrite/, bandsintown/, songkick/, meetup/
│   ├── government_calendars/           # us_federal_holidays/, uk_bank_holidays/, ...
│   ├── academic_calendars/             # university calendars
│   ├── tourism_boards/
│   ├── venues_and_convention_centers/
│   ├── weather_and_disaster/           # noaa/, gdacs/, ...
│   ├── airport_delays/                 # faa/, eurocontrol/, ...
│   └── health_advisories/              # who/, ...
│
├── scraping_platform/                  # Shared scraping infra used by connectors' fetch()
│   ├── scrapy_project/                 # shared Scrapy settings/middlewares/pipelines
│   ├── playwright_pool/                # headless browser pool, session/proxy rotation
│   └── rate_limiting/                  # per-domain politeness policies
│
├── services/                           # Bounded Contexts — independently deployable
│   ├── ingestion/                      # Runs connectors, owns Raw Event Storage
│   ├── normalization/                  # Raw → canonical schema mapping
│   ├── deduplication/                  # Fingerprinting & duplicate clustering        [CORE]
│   ├── entity_resolution/              # Venue/Performer/Organizer MDM               [CORE]
│   ├── geocoding/                      # Address → lat/lng/timezone/boundary
│   ├── classification/                 # Category/taxonomy classifier                [CORE]
│   ├── attendance_estimation/          # Attendance/demand forecasting               [CORE]
│   ├── impact_scoring/                 # Composite impact score                      [CORE]
│   ├── catalog/                        # Canonical Event DB — golden record, lifecycle
│   ├── search_indexer/                 # Projects catalog → Elasticsearch (CQRS read model)
│   ├── api_gateway/                    # Public/partner FastAPI surface
│   └── identity_access/                # Tenants, API keys, quotas, usage metering
│
├── ml_platform/                        # Shared ML plumbing for the 5 CORE contexts
│   ├── feature_store/
│   ├── training_pipelines/
│   └── model_registry/
│
├── orchestration/                       # Apache Airflow
│   ├── dags/                            # per-connector schedules, backfills, pipeline health
│   ├── plugins/                         # custom operators/sensors
│   └── config/
│
├── data_platform/                       # Schema & topology ownership per bounded context
│   ├── postgres/migrations/             # one alembic-style migration set per context schema
│   ├── elasticsearch/index_templates/
│   ├── minio/bucket_policies/           # raw/, archive/, ml-datasets/ lifecycle rules
│   └── rabbitmq/topology/               # exchange/queue/binding/DLQ definitions
│
├── tools/                               # connector scaffolder wiring, load-test scripts, seed data
│
└── tests/
    ├── contract/                        # inter-service contract tests
    ├── integration/
    └── e2e/                             # full pipeline smoke tests (connector → API)
```

Each bounded context under `services/<context>/` follows the same internal DDD layering
(see §4), so it is shown once here rather than repeated per context.

---

## 3. Root & Cross-Cutting Folder Responsibilities

### `docs/`
Architecture Decision Records, C4-style diagrams, and the **ubiquitous language glossary**
(precise definitions of "Event," "Occurrence," "Venue," "Duplicate Cluster," "Impact Score,"
etc.) that every bounded context's model must agree with.

### `infra/`
Infrastructure-as-code only: Dockerfiles per service, Kubernetes base manifests + per-env
overlays (dev/staging/prod), Helm charts for stateful dependencies (Postgres, Redis, ES,
RabbitMQ, MinIO, Airflow). No application logic lives here.

### `shared_kernel/`
The DDD **Shared Kernel** — the small, deliberately-minimized set of concepts every bounded
context must agree on: value objects (`GeoPoint`, `TimeRange`, `EventId`, `SourceRef`,
`Confidence`), the domain-event contracts that travel over RabbitMQ, and cross-context API
contracts. Changing anything here requires cross-team sign-off, because it ripples through
every context — this is why it is kept intentionally small.

### `platform_libs/`
Generic technical libraries with **no domain knowledge** — DB session handling, messaging
wrappers, cache helpers, search client, object storage client, config loading. Distinguish
from `shared_kernel/`: this folder could be extracted and open-sourced tomorrow with zero
business logic leaking out.

### `connector_sdk/`
The plugin framework itself (not a bounded context — an SDK). Defines `BaseConnector` and
its required `discover()` / `fetch()` / `normalize()` / `validate()` contract, the plugin
registry/loader (manifest-driven discovery, not per-connector deployables), the contract-test
harness every connector must pass before merge, and a CLI scaffolder so **adding a connector
means running one command and filling in four methods** — nothing else in the platform needs
to change.

### `connectors/`
Where the 1000+ plugins live, grouped by vertical for discoverability (ticketing, government
calendars, academic calendars, tourism boards, venues, weather/disaster, airports, health).
Each connector folder is self-contained: its own manifest, code, config/mapping tables, and
fixtures for contract tests. Operationally, these are **not** built as 1000 separate
container images — see §6.

### `scraping_platform/`
Shared Playwright/Scrapy runtime infrastructure (browser pool, proxy/session rotation,
per-domain rate limiting) that any connector's `fetch()` can depend on, so individual
connectors don't reimplement politeness/anti-blocking logic.

### `ml_platform/`
Shared plumbing for the three ML-heavy CORE contexts (Classification, Attendance
Estimation, Impact Scoring): a feature store, offline training pipelines (run via Airflow),
and a model registry client for versioned model promotion — kept separate from the contexts
themselves so a model can be retrained/promoted without touching serving code.

### `orchestration/`
Apache Airflow DAGs, **but scoped specifically to scheduling and batch concerns**: triggering
each connector's `discover()`/`fetch()` on its cadence, backfills, and pipeline-health/DLQ
replay DAGs. Airflow does **not** orchestrate the enrichment pipeline stage-to-stage — that is
event-driven (see §5).

### `data_platform/`
Declares *ownership*, not implementation: which Postgres schema/migrations, Elasticsearch
index templates, MinIO bucket policies, and RabbitMQ exchange/queue/DLQ topology belong to
which bounded context. Every context owns its own schema; no context reaches into another's
tables directly.

### `tools/` and `tests/`
Developer tooling (scaffolder wiring, load tests, seed data) and cross-context test suites:
contract tests (does Context A still satisfy the schema Context B expects?), integration
tests, and end-to-end pipeline smoke tests (raw payload in → API response out).

---

## 4. Internal Structure of Every `services/<context>/`

Every bounded context — core or supporting — follows the same DDD tactical layering:

```
services/<context>/
├── domain/
│   ├── entities/            # aggregates & entities (e.g., Event, DuplicateCluster, Venue)
│   ├── value_objects/       # context-local value objects
│   ├── domain_events/       # events this context emits (definitions only; schema lives in shared_kernel/)
│   └── repositories/        # repository INTERFACES (ports) — no implementation
├── application/
│   ├── commands/            # write use-cases (CommandHandlers)
│   ├── queries/             # read use-cases (QueryHandlers)
│   └── handlers/            # inbound domain-event subscribers from other contexts
├── infrastructure/
│   ├── persistence/         # repository IMPLEMENTATIONS (adapters) — Postgres/ES/Redis
│   ├── messaging/           # RabbitMQ publisher/consumer adapters
│   └── external_clients/    # 3rd-party API clients (e.g., geocoding provider, ML registry)
├── interface/
│   ├── rest/                # FastAPI router, if this context exposes a synchronous API
│   └── consumers/           # message-consumer entrypoints
└── tests/
    ├── unit/
    └── integration/
```

`domain/` never imports from `infrastructure/` or `interface/` — dependencies point inward,
classic Hexagonal/Ports-and-Adapters within each bounded context. Not every context populates
every subfolder (e.g., `deduplication` is pure event-driven and may have an empty
`interface/rest/`; `catalog` and `api_gateway` are REST-heavy).

---

## 5. How Services Communicate

Two distinct communication mechanisms are used deliberately for two distinct concerns:

- **Airflow (orchestration)** — for *when connectors run*: schedules, backfills, retries,
  operational visibility over 1000+ connector jobs.
- **RabbitMQ domain events (choreography)** — for *the enrichment pipeline itself*: each
  bounded context reacts to the previous stage's event and emits its own. This avoids a
  central orchestrator bottleneck at millions-of-events throughput and lets each stage scale
  independently.

### Pipeline event flow (choreography over RabbitMQ)

| Stage (producer) | Event published | Consumer | Call type |
|---|---|---|---|
| Airflow | triggers `discover()`/`fetch()` | Ingestion | sync (HTTP/K8s job trigger) |
| Ingestion | `RawEventIngested` | Normalization | async |
| Normalization | `EventNormalized` | Deduplication | async |
| Deduplication | `EventDeduplicated` | Entity Resolution | async |
| Entity Resolution | `EntityResolved` | Geocoding | async |
| Geocoding | `EventGeocoded` | Classification | async |
| Classification | `EventClassified` | Attendance Estimation | async |
| Attendance Estimation | `AttendanceEstimated` | Impact Scoring | async |
| Impact Scoring | `ImpactScored` | Catalog | async |
| Catalog | `EventCanonicalized` / `EventUpdated` / `EventRetracted` | Search Indexer | async |
| API Gateway | — | Catalog (by-ID/admin ops) | sync (REST/gRPC) |
| API Gateway | — | Elasticsearch (search/filter/geo-radius) | sync, direct read |
| API Gateway | — | Identity/Access (auth, quota) | sync, in-request |

Notes:
- **Raw Event Storage** is not a separate context — it's owned by Ingestion's
  `infrastructure/persistence` (MinIO bucket + a lightweight raw-event log table). Nothing
  downstream ever reads raw payloads directly; every later stage consumes only normalized
  domain events. This is what satisfies "raw data must never directly enter the canonical
  database."
- **Entity Resolution** also makes a synchronous lookup against its own Venue/Performer/
  Organizer master-data store while processing, in addition to consuming/emitting events.
- **Search Indexer** is a one-way projector (Catalog → Elasticsearch). The API Gateway reads
  Elasticsearch directly for search/filter queries and reads Catalog's Postgres directly for
  single-record/admin lookups — classic CQRS split between write model (Postgres) and read
  model (Elasticsearch).
- **Redis** is used across multiple contexts independently, not as a shared context: Geocoding
  (address→geo cache to avoid re-billing geocoding APIs), Deduplication (fingerprint/near-
  duplicate lookup cache), API Gateway (hot-query response cache), Identity/Access (rate-limit
  and quota counters).
- **Idempotency**: RabbitMQ delivers at-least-once, so every consumer handler must be
  idempotent (dedupe on event id/version). Catalog upserts are keyed by canonical `EventId` +
  version so replays never double-create events.
- **Dead-letter queues**: every consumer has a DLQ; failed messages are replayed via an
  Airflow remediation DAG, not silently dropped — required at this scale, since a single flaky
  geocoding provider must not stall the whole pipeline.

---

## 6. Scaling to 1000+ Connectors

Connectors are **data/config, not deployables**. A single generic `ingestion` worker image
loads a connector plugin dynamically at runtime by reading its `connector.yaml` manifest and
importing its module (`connector_sdk/core` registry) — this keeps the operational surface at
"one deployable, many configs" rather than 1000 container images. Heavier connectors (e.g.
those requiring dedicated Playwright browser pools) can be routed to a separate worker pool /
queue via `scraping_platform` and Kubernetes resource classes without changing this model.
Adding a connector = new folder under `connectors/<vertical>/`, filled in via the
`connector_sdk` scaffolder — no core platform code changes required.

## 7. Scaling to Millions of Events

- Ingestion/enrichment workers scale horizontally via K8s HPA/KEDA on RabbitMQ queue depth.
- Canonical Postgres uses time-based partitioning (event start date) under `catalog`.
- Elasticsearch absorbs all high-QPS public API read traffic so canonical Postgres isn't the
  bottleneck for search/filter/geo-radius queries.
- MinIO raw storage is partitioned by source + date, with lifecycle policies (hot → archive)
  defined in `data_platform/minio/bucket_policies`.
- Identity/Access enforces per-tenant rate limits/quotas so no single API consumer can degrade
  shared infrastructure for others.
