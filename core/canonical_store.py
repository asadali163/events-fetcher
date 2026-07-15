import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "canonical_events.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS canonical_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_key TEXT UNIQUE NOT NULL,
            city TEXT NOT NULL,
            event_name TEXT,
            date TEXT,
            time TEXT,
            venue_name TEXT,
            venue_city TEXT,
            venue_country TEXT,
            venue_latitude REAL,
            venue_longitude REAL,
            sources_json TEXT,
            canonical_json TEXT NOT NULL,
            first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def make_canonical_key(city, event_date, event_name, venue_name):
    parts = [city, event_date or "", event_name or "", venue_name or ""]
    return "|".join(p.strip().lower() for p in parts)


def save_canonical_event(city, merged_event):
    """Insert this merged (already-deduplicated-within-run) event, or update
    the existing row if one already exists for the same (city, date, event
    name, venue name).

    This is an *exact*-key upsert, not the fuzzy matching used to build the
    merged event in the first place — good enough to stop the same event
    accumulating duplicate rows across separate dispatcher runs, as long as
    its name and venue text stay consistent between runs. True cross-run
    fuzzy matching (e.g. catching a slightly reworded title tomorrow) is a
    future enhancement, not built here yet.
    """
    key = make_canonical_key(
        city,
        merged_event.get("date"),
        merged_event.get("event_name"),
        merged_event.get("venue_name"),
    )
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO canonical_events (
            canonical_key, city, event_name, date, time, venue_name, venue_city,
            venue_country, venue_latitude, venue_longitude, sources_json, canonical_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_key) DO UPDATE SET
            event_name = excluded.event_name,
            date = excluded.date,
            time = excluded.time,
            venue_name = excluded.venue_name,
            venue_city = excluded.venue_city,
            venue_country = excluded.venue_country,
            venue_latitude = excluded.venue_latitude,
            venue_longitude = excluded.venue_longitude,
            sources_json = excluded.sources_json,
            canonical_json = excluded.canonical_json,
            last_updated_at = CURRENT_TIMESTAMP
        """,
        (
            key,
            city,
            merged_event.get("event_name"),
            merged_event.get("date"),
            merged_event.get("time"),
            merged_event.get("venue_name"),
            merged_event.get("venue_city"),
            merged_event.get("venue_country"),
            merged_event.get("venue_latitude"),
            merged_event.get("venue_longitude"),
            json.dumps(merged_event.get("sources", []), ensure_ascii=False),
            json.dumps(merged_event, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()


def get_all_entries():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM canonical_events ORDER BY last_updated_at").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def print_all_entries():
    entries = get_all_entries()
    if not entries:
        print("No entries yet.")
        return

    columns = ["id", "city", "event_name", "date", "time", "venue_name", "last_updated_at"]
    widths = {c: max(len(c), max(len(str(e[c])) for e in entries)) for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("  ".join("-" * widths[c] for c in columns))
    for e in entries:
        print("  ".join(str(e[c]).ljust(widths[c]) for c in columns))


if __name__ == "__main__":
    init_db()
    print_all_entries()
