import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "refetch_log.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            city TEXT NOT NULL,
            country TEXT,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            status TEXT NOT NULL,      -- 'success' or 'failed'
            run_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
    conn.commit()
    conn.close()


def is_covered(source, city, window_start, window_end):
    """True if some earlier successful fetch for this source+city already
    covered the entire requested window on its own (a failed run never
    counts, and a run that only partially overlaps never counts either)."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT COUNT(*) FROM fetch_log
        WHERE source = ? AND city = ? AND status = 'success'
          AND window_start <= ? AND window_end >= ?
        """,
        (source, city, window_start, window_end),
    ).fetchone()
    conn.close()
    return row[0] > 0


def log_fetch(source, city, country, window_start, window_end, status):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO fetch_log (source, city, country, window_start, window_end, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source, city, country, window_start, window_end, status),
    )
    conn.commit()
    conn.close()


def get_all_entries():
    """Every row in fetch_log, oldest first, as a list of dicts."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM fetch_log ORDER BY run_at").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def print_all_entries():
    entries = get_all_entries()
    if not entries:
        print("No entries yet.")
        return

    columns = [
        "id",
        "source",
        "city",
        "country",
        "window_start",
        "window_end",
        "status",
        "run_at",
    ]
    widths = {c: max(len(c), max(len(str(e[c])) for e in entries)) for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("  ".join("-" * widths[c] for c in columns))
    for e in entries:
        print("  ".join(str(e[c]).ljust(widths[c]) for c in columns))


def delete_all_entries():
    conn = get_connection()
    conn.execute("DELETE FROM fetch_log")
    conn.commit()
    print("Deleted all entries")
    conn.close()


if __name__ == "__main__":
    init_db()
    # print_all_entries()
    delete_all_entries()
