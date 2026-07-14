import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "raw_events.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            city TEXT,
            country TEXT,
            raw_json TEXT NOT NULL,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def save_raw_event(source, city, country, raw_event):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO raw_events (source, city, country, raw_json)
        VALUES (?, ?, ?, ?)
        """,
        (source, city, country, json.dumps(raw_event, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database ready at {DB_PATH}")
