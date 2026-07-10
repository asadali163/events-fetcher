import importlib
import json
import sys
from pathlib import Path

# core/ sits directly under the project root; importlib.import_module()
# (used below to load connectors by their entry_point string, e.g.
# "connectors.ticketing.ticketmaster.connector") needs the project root
# itself on sys.path, not just this file's own folder.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.cities_loader import get_country_for_city
from core.registry_loader import get_sources_for_country


def load_connector(entry_point):
    module_path, class_name = entry_point.rsplit(".", 1)
    module = importlib.import_module(module_path)
    connector_class = getattr(module, class_name)
    return connector_class()


def run_connector(source, city):
    events = []

    try:
        connector = load_connector(source["entry_point"])
    except Exception as error:
        print(f"[{source['id']}] could not be loaded, skipping: {error}")
        return events

    try:
        raw_events = connector.discover(city)
    except Exception as error:
        print(f"[{source['id']}] discover() failed, skipping: {error}")
        return events

    for raw_event in raw_events:
        try:
            detailed = connector.fetch(raw_event)
            normalized = connector.normalize(detailed)
            is_valid, missing = connector.validate(normalized)
        except Exception as error:
            print(f"[{source['id']}] failed on one event, skipping it: {error}")
            continue

        if is_valid:
            events.append(normalized)
        else:
            print(f"[{source['id']}] skipping event, missing: {missing}")

    return events


def get_events_for_city(city):
    country = get_country_for_city(city)
    sources = get_sources_for_country(country)

    print(f"{city} -> {country} -> {len(sources)} matching source(s): "
          f"{', '.join(s['id'] for s in sources)}")

    all_events = []
    for source in sources:
        source_events = run_connector(source, city)
        print(f"[{source['id']}] returned {len(source_events)} valid events")
        all_events.extend(source_events)

    return all_events


if __name__ == "__main__":
    city = sys.argv[1] if len(sys.argv) > 1 else "Chicago"

    events = get_events_for_city(city)
    print(f"Total combined events for {city}: {len(events)}")

    out_path = f"events_{city.lower()}.json"
    with open(out_path, "w") as f:
        json.dump(events, f, indent=2)
    print(f"Saved combined results to {out_path}")
