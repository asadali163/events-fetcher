# platform_libs/

Generic technical libraries with **no domain knowledge** — could be extracted and
open-sourced without leaking any business logic. Distinct from `shared_kernel/`, which is
domain concepts; this is pure infra glue.

- `db/` — Postgres/PostGIS session & engine factories.
- `messaging/` — RabbitMQ publisher/consumer wrappers, retry/DLQ helpers.
- `caching/` — Redis client wrappers, cache-aside helpers.
- `search/` — Elasticsearch client wrapper, index helpers.
- `object_storage/` — MinIO/S3 client wrapper.
- `config/` — settings and secrets loading.
