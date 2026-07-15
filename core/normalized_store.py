import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "normalized_events.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS normalized_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            city TEXT NOT NULL,
            country TEXT,
            source_event_id TEXT,
            event_name TEXT,
            date TEXT,
            time TEXT,
            venue_name TEXT,
            venue_city TEXT,
            venue_country TEXT,
            venue_latitude REAL,
            venue_longitude REAL,
            normalized_json TEXT NOT NULL,
            saved_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def save_normalized_event(source, city, country, normalized_event):
    """Append-only — every normalized event from every run gets its own
    row, duplicates across runs included. This is the audit/replay layer,
    not deduplicated; that's canonical_store.py's job."""
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO normalized_events (
            source, city, country, source_event_id, event_name, date, time,
            venue_name, venue_city, venue_country, venue_latitude, venue_longitude,
            normalized_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            city,
            country,
            normalized_event.get("source_event_id"),
            normalized_event.get("event_name"),
            normalized_event.get("date"),
            normalized_event.get("time"),
            normalized_event.get("venue_name"),
            normalized_event.get("venue_city"),
            normalized_event.get("venue_country"),
            normalized_event.get("venue_latitude"),
            normalized_event.get("venue_longitude"),
            json.dumps(normalized_event, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()


def get_all_entries():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM normalized_events ORDER BY saved_at").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def print_all_entries():
    entries = get_all_entries()
    if not entries:
        print("No entries yet.")
        return

    columns = ["id", "source", "city", "event_name", "date", "time", "venue_name", "saved_at"]
    widths = {c: max(len(c), max(len(str(e[c])) for e in entries)) for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("  ".join("-" * widths[c] for c in columns))
    for e in entries:
        print("  ".join(str(e[c]).ljust(widths[c]) for c in columns))


if __name__ == "__main__":
    init_db()
    print_all_entries()
