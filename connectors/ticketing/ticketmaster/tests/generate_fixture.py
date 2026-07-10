import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from connectors.ticketing.ticketmaster.connector import TicketmasterConnector

if __name__ == "__main__":
    city = sys.argv[1] if len(sys.argv) > 1 else "Chicago"

    connector = TicketmasterConnector()
    raw_events = connector.discover(city)

    if not raw_events:
        print(f"No events found for {city}, nothing to save.")
    else:
        fixtures_dir = Path(__file__).parent / "fixtures"
        fixtures_dir.mkdir(exist_ok=True)
        out_path = fixtures_dir / "sample_event.json"
        with open(out_path, "w") as f:
            json.dump(raw_events[0], f, indent=2)
        print(f"Saved one real raw event to {out_path}")
