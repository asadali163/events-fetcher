# infra/

Infrastructure-as-code only — no application logic.

- `docker/` — Dockerfiles per deployable service.
- `k8s/base/` — Kustomize base manifests, one set per bounded context service.
- `k8s/overlays/{dev,staging,prod}/` — per-environment overrides.
- `helm/` — Helm charts for stateful dependencies (Postgres, Redis, Elasticsearch, RabbitMQ,
  MinIO, Airflow).
