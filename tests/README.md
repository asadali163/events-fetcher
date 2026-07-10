# tests/

Cross-context test suites (as opposed to each service's own `tests/unit` and
`tests/integration`):

- `contract/` — does context A still satisfy the schema context B expects?
- `integration/` — multi-context integration tests.
- `e2e/` — full pipeline smoke tests, raw payload in → API response out.
