import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "locations.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lookup_key TEXT UNIQUE NOT NULL,
            place_type TEXT NOT NULL,   -- 'venue' or 'city'
            name TEXT NOT NULL,
            city TEXT,
            country TEXT,
            latitude REAL,
            longitude REAL,
            source TEXT,                -- where the coordinate came from
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
    conn.commit()
    conn.close()


def make_key(place_type, name, city=None, country=None):
    parts = [place_type, name, city or "", country or ""]
    return "|".join(p.strip().lower() for p in parts)


def get_location(place_type, name, city=None, country=None):
    key = make_key(place_type, name, city, country)
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM locations WHERE lookup_key = ?", (key,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_location(place_type, name, city, country, latitude, longitude, source):
    key = make_key(place_type, name, city, country)
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO locations (lookup_key, place_type, name, city, country, latitude, longitude, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lookup_key) DO NOTHING
        """,
        (key, place_type, name, city, country, latitude, longitude, source),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    # init_db()
    # print(f"Database ready at {DB_PATH}")
    # See total number of locations
    conn = get_connection()
    print(
        f"Total locations: {conn.execute('SELECT COUNT(*) FROM locations').fetchone()[0]}"
    )
    conn.close()
