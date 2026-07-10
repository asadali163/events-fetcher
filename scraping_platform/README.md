# scraping_platform/

Shared scraping infrastructure that any connector's `fetch()` implementation can depend on,
so individual connectors don't reimplement anti-blocking/politeness logic.

- `scrapy_project/` — shared Scrapy settings, middlewares, pipelines.
- `playwright_pool/` — headless browser pool manager, session/proxy rotation.
- `rate_limiting/` — per-domain politeness/rate-limit policies.
