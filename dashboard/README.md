# Fetch dashboard (Streamlit)

A small UI on top of the existing pipeline: pick a city, pick how far ahead
to fetch, choose which sources to run, and watch each one's progress live.
It is purely additive — no file outside `dashboard/` was changed to build it.

```bash
source .venv/bin/activate
pip install -r dashboard/requirements.txt   # once — adds streamlit
streamlit run dashboard/app.py               # run from the project root
```

## What it does

1. **City** — any city in `core/cities.yaml`.
2. **Date** — "from today until" a chosen date. This becomes `days_ahead`
   (`end date − today + 1`), passed to every selected source exactly like
   `python core/dispatcher.py <City> <days>`.
3. **Sources** — every source in `core/registry.yaml` covering that city's
   country, all selected by default. Each one shows:
   - whether it actually uses the date window (Meetup and Universe don't —
     they return everything and re-fetch on a timer instead),
   - whether it opens a visible browser window (`HEADLESS = False`),
   - whether the refetch log means it **will be skipped** for this window.
     Tick *Re-fetch even if already fetched recently* to ignore the refetch
     log for that run (it is still written to afterwards).
4. **Start fetching** — runs the selected sources one after another and
   shows per source: status (queued / scraping / processing / done /
   skipped / failed), a progress bar, `processed X / Y events`, valid-event
   count, elapsed time, and the latest log line. The full log is in the
   *Live log* expander. **Stop** kills the run, including any browsers it
   opened.
5. **Results** — deduplicated events in a table, with JSON/CSV download.
   Results are also upserted into `canonical_events.db`, exactly as the
   dispatcher does.

## How it works

| File | Role |
|---|---|
| `app.py` | The Streamlit page. Never scrapes itself — it launches `runner.py` as a subprocess and renders the progress it reports. |
| `runner.py` | Runs the selected sources through the **unchanged** `core.dispatcher.run_connector()`, then `deduplicate_events()` and `save_canonical_event()` — the same steps as `get_events_for_city()`, but for a chosen subset of sources. Prints progress as `@@PROGRESS {json}` lines. Can be run standalone: `python dashboard/runner.py --city Paris --days 15 --sources ticketmaster,dice --output out.json`. |

Progress is gathered without touching connector code: the runner swaps
`dispatcher.load_connector` for a wrapper that loads the connector the
normal way, then wraps `discover()` (to know when scraping starts/ends and
how many raw events came back) and `fetch()` (called once per raw event by
`run_connector()`, to count progress) on that one instance.

Running the scrape in a separate process keeps Playwright/Selenium out of
Streamlit's own process, so a crashing scraper or a Stop can't take the
dashboard down.

## Things to know

- **Progress during scraping is indeterminate.** While a connector is inside
  `discover()` there's no way to know how far along it is — the dashboard
  shows elapsed time and that connector's latest printed message instead.
  The exact `X / Y` count starts once `discover()` returns.
- **A stopped run** keeps whatever raw/normalized events were already saved,
  but skips deduplication and the canonical upsert for that run.
- **Sources run one at a time**, same as the dispatcher.
