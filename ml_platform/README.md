# ml_platform/

Shared ML plumbing for the three ML-heavy CORE contexts: Classification, Attendance
Estimation, Impact Scoring. Kept separate from those services so models can be retrained and
promoted without touching serving code.

- `feature_store/` — shared feature definitions/storage.
- `training_pipelines/` — offline training jobs (triggered via Airflow).
- `model_registry/` — versioned model registry client.
