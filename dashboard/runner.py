"""
Background worker for the Streamlit dashboard (dashboard/app.py).

Runs the selected sources for one city and reports progress as JSON lines
on stdout, so the dashboard can show live status. Runs as its own process
(not inside Streamlit) so that Playwright/Selenium browsers, crashes, and a
user pressing "Stop" can't take the dashboard down with them.

It reuses the existing pipeline unchanged: each source still goes through
core.dispatcher.run_connector(), so the refetch log, raw/normalized saving,
validation, deduplication and canonical upsert behave exactly as they do
for `python core/dispatcher.py <City>`. Progress hooks are added from the
outside only, by wrapping the connector instance run_connector() loads.

Usage (normally launched by the dashboard, but works standalone too):
    python dashboard/runner.py --city Paris --days 15 \
        --sources ticketmaster,dice --output /tmp/out.json [--force]

Every progress line is prefixed with PROGRESS_PREFIX; any other output is
the connectors' and dispatcher's own print() logging, passed through as-is.
"""

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import dispatcher  # noqa: E402
from core.canonical_store import save_canonical_event  # noqa: E402
from core.cities_loader import get_country_for_city  # noqa: E402
from core.dedup import deduplicate_events  # noqa: E402
from core.registry_loader import get_sources_for_country  # noqa: E402

PROGRESS_PREFIX = "@@PROGRESS "

# Per-event progress lines are throttled to at most one per this many
# seconds (the last event of a source is always reported).
ITEM_EMIT_INTERVAL_SECONDS = 0.25


def emit(kind, **data):
    payload = {"kind": kind, "t": time.time(), **data}
    print(PROGRESS_PREFIX + json.dumps(payload, default=str), flush=True)


class SourceTracker:
    """What happened inside run_connector() for the source currently running.
    run_connector() swallows its own errors (by design), so this is how the
    runner tells "skipped", "failed" and "done" apart afterwards."""

    def __init__(self, source_id):
        self.source_id = source_id
        self.load_error = None
        self.discover_called = False
        self.discover_error = None
        self.total = 0
        self.done = 0
        self.last_item_emit = 0.0


_current = None
_original_load_connector = dispatcher.load_connector


def _tracked_load_connector(entry_point, days_ahead=None):
    """Drop-in replacement for dispatcher.load_connector(): loads the
    connector exactly the same way, then wraps discover()/fetch() on that one
    instance to report progress. The connector's class is untouched."""
    tracker = _current
    try:
        connector = _original_load_connector(entry_point, days_ahead)
    except Exception as error:
        tracker.load_error = f"{type(error).__name__}: {error}"
        raise

    original_discover = connector.discover
    original_fetch = connector.fetch

    def discover(city, country):
        tracker.discover_called = True
        emit("stage", source=tracker.source_id, stage="discovering")
        try:
            raw_events = original_discover(city, country)
        except Exception as error:
            tracker.discover_error = f"{type(error).__name__}: {error}"
            raise
        if raw_events is not None and not hasattr(raw_events, "__len__"):
            raw_events = list(raw_events)
        tracker.total = len(raw_events) if raw_events is not None else 0
        emit(
            "stage",
            source=tracker.source_id,
            stage="processing",
            total=tracker.total,
        )
        return raw_events

    def fetch(raw_event):
        tracker.done += 1
        now = time.time()
        if (
            tracker.done == tracker.total
            or now - tracker.last_item_emit >= ITEM_EMIT_INTERVAL_SECONDS
        ):
            tracker.last_item_emit = now
            emit(
                "item",
                source=tracker.source_id,
                done=tracker.done,
                total=tracker.total,
            )
        return original_fetch(raw_event)

    connector.discover = discover
    connector.fetch = fetch
    return connector


def run(city, days_ahead, source_ids, output_path, force):
    global _current

    dispatcher.load_connector = _tracked_load_connector
    if force:
        # Ignore the refetch log for this run only (it's still written to).
        dispatcher.is_covered = lambda *args, **kwargs: False
        dispatcher.is_recently_fetched = lambda *args, **kwargs: False

    country = get_country_for_city(city)
    available = get_sources_for_country(country)
    selected = [s for s in available if s["id"] in source_ids]
    unknown = sorted(set(source_ids) - {s["id"] for s in available})
    if unknown:
        print(f"Ignoring source(s) not registered for {country}: {', '.join(unknown)}")

    emit(
        "start",
        city=city,
        country=country,
        days_ahead=days_ahead,
        sources=[s["id"] for s in selected],
        force=force,
    )

    all_events = []
    for source in selected:
        _current = SourceTracker(source["id"])
        emit("source_start", source=source["id"])
        started = time.time()

        events = dispatcher.run_connector(source, city, country, days_ahead)

        if _current.load_error:
            status, error = "failed", f"could not be loaded: {_current.load_error}"
        elif not _current.discover_called:
            status, error = "skipped", None
        elif _current.discover_error:
            status, error = "failed", _current.discover_error
        else:
            status, error = "done", None

        emit(
            "source_end",
            source=source["id"],
            status=status,
            error=error,
            raw=_current.total,
            valid=len(events),
            seconds=round(time.time() - started, 1),
        )
        all_events.extend(events)

    emit("stage", source=None, stage="deduplicating", total=len(all_events))
    deduplicated = deduplicate_events(all_events)

    saved = 0
    for merged_event in deduplicated:
        try:
            save_canonical_event(city, merged_event)
            saved += 1
        except Exception as error:
            print(f"Failed to save canonical event, continuing anyway: {error}")

    with open(output_path, "w") as f:
        json.dump(deduplicated, f, indent=2, default=str)

    emit(
        "finished",
        total_valid=len(all_events),
        deduplicated=len(deduplicated),
        saved=saved,
        output=str(output_path),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--city", required=True)
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Window length in days starting today; omit to use each source's own default",
    )
    parser.add_argument("--sources", required=True, help="Comma-separated source ids")
    parser.add_argument("--output", required=True, help="Where to write the deduplicated events JSON")
    parser.add_argument("--force", action="store_true", help="Ignore the refetch log for this run")
    args = parser.parse_args()

    source_ids = [s.strip() for s in args.sources.split(",") if s.strip()]
    try:
        run(args.city, args.days, source_ids, args.output, args.force)
    except Exception as error:
        traceback.print_exc()
        emit("fatal", error=f"{type(error).__name__}: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
