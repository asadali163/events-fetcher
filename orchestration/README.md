# orchestration/

Apache Airflow, scoped specifically to **scheduling and batch** concerns — not the
inter-stage enrichment pipeline, which is event-driven over RabbitMQ (see root
ARCHITECTURE.md §5).

- `dags/` — per-connector discover/fetch schedules, backfill DAGs, pipeline-health and
  DLQ-replay DAGs.
- `plugins/` — custom Airflow operators/sensors (e.g., a Kubernetes connector-run operator).
- `config/` — environment-specific Airflow configuration.
