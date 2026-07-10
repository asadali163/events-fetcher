# data_platform/

Declares schema/topology *ownership* per bounded context — every context owns its own schema;
no context reaches into another's tables directly.

- `postgres/migrations/` — one migration set per bounded-context schema.
- `elasticsearch/index_templates/` — index templates for the Catalog → Search Indexer
  read model.
- `minio/bucket_policies/` — raw/, archive/, ml-datasets/ lifecycle rules.
- `rabbitmq/topology/` — exchange/queue/binding/DLQ definitions per domain event.
