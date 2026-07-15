import importlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

# core/ sits directly under the project root; importlib.import_module()
# (used below to load connectors by their entry_point string, e.g.
# "connectors.ticketing.ticketmaster.connector") needs the project root
# itself on sys.path, not just this file's own folder.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.canonical_store import save_canonical_event
from core.cities_loader import get_country_for_city
from core.dedup import deduplicate_events
from core.normalized_store import save_normalized_event
from core.raw_store import save_raw_event
from core.refetch_log import is_covered, log_fetch
from core.registry_loader import get_sources_for_country


def load_connector(entry_point, days_ahead=None):
    module_path, class_name = entry_point.rsplit(".", 1)
    module = importlib.import_module(module_path)
    connector_class = getattr(module, class_name)
    if days_ahead is None:
        # No override given — fall back to whichever default that specific
        # connector class already defines (e.g. Ticketmaster's 30, Dice's 3).
        return connector_class()
    return connector_class(days_ahead=days_ahead)


def run_connector(source, city, country, days_ahead=None):
    events = []

    try:
        connector = load_connector(source["entry_point"], days_ahead)
    except Exception as error:
        print(f"[{source['id']}] could not be loaded, skipping: {error}")
        return events

    # Every connector we have so far stores its effective window as
    # self.days_ahead — this is a convention, not something BaseConnector
    # enforces, so fall back to "always run" if a future connector doesn't
    # set it, rather than crashing.
    effective_days_ahead = getattr(connector, "days_ahead", None)
    window_start = window_end = None
    if effective_days_ahead is not None:
        window_start = date.today().isoformat()
        window_end = (date.today() + timedelta(days=effective_days_ahead - 1)).isoformat()

        if is_covered(source["id"], city, window_start, window_end):
            print(
                f"[{source['id']}] already covered by a previous successful fetch "
                f"({window_start} to {window_end}) — skipping"
            )
            return events

    try:
        raw_events = connector.discover(city, country)
    except Exception as error:
        print(f"[{source['id']}] discover() failed, skipping: {error}")
        if window_start:
            log_fetch(source["id"], city, country, window_start, window_end, "failed")
        return events

    if window_start:
        log_fetch(source["id"], city, country, window_start, window_end, "success")

    for raw_event in raw_events:
        try:
            save_raw_event(source["id"], city, country, raw_event)
        except Exception as error:
            print(
                f"[{source['id']}] failed to save raw event, continuing anyway: {error}"
            )

        try:
            detailed = connector.fetch(raw_event)
            normalized = connector.normalize(detailed)
            is_valid, missing = connector.validate(normalized)
        except Exception as error:
            print(f"[{source['id']}] failed on one event, skipping it: {error}")
            continue

        try:
            save_normalized_event(source["id"], city, country, normalized)
        except Exception as error:
            print(
                f"[{source['id']}] failed to save normalized event, continuing anyway: {error}"
            )

        if is_valid:
            events.append(normalized)
        else:
            print(f"[{source['id']}] skipping event, missing: {missing}")

    return events


def get_events_for_city(city, days_ahead=None):
    country = get_country_for_city(city)
    sources = get_sources_for_country(country)

    print(
        f"{city} -> {country} -> {len(sources)} matching source(s): "
        f"{', '.join(s['id'] for s in sources)}"
    )

    all_events = []
    for source in sources:
        source_events = run_connector(source, city, country, days_ahead)
        print(f"[{source['id']}] returned {len(source_events)} valid events")
        all_events.extend(source_events)

    deduplicated_events = deduplicate_events(all_events)
    print(
        f"Deduplicated {len(all_events)} events from all sources down to "
        f"{len(deduplicated_events)}"
    )

    for merged_event in deduplicated_events:
        try:
            save_canonical_event(city, merged_event)
        except Exception as error:
            print(f"Failed to save canonical event, continuing anyway: {error}")

    return deduplicated_events


if __name__ == "__main__":
    city = sys.argv[1] if len(sys.argv) > 1 else "Chicago"
    days_ahead = int(sys.argv[2]) if len(sys.argv) > 2 else None

    events = get_events_for_city(city, days_ahead)
    print(f"Total combined events for {city}: {len(events)}")

    out_path = f"events_{city.lower()}.json"
    with open(out_path, "w") as f:
        json.dump(events, f, indent=2)
    print(f"Saved combined results to {out_path}")
