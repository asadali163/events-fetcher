"""enrichment.db: attendance + impact-radius estimates for canonical events.

Kept apart from canonical_events.db on purpose: save_canonical_event()
rewrites canonical_json on every scrape, so anything stored there would be
wiped. Rows here are keyed by canonical_key and only recomputed when the
event's LLM input (input_hash), the prompt version or the model changes, or
when an earlier attempt failed.

Each row also carries footfall/demand uplift percentages
(core/attendance/uplift.py). They are pure arithmetic on the stored estimate,
so they are recomputed without an LLM call when UPLIFT_VERSION changes.

Also holds a cache of French sports-facility register lookups, so the same
venue is never looked up twice.
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "enrichment.db"

ESTIMATE_COLUMNS = [
    "status", "expected_attendance", "low", "high", "confidence", "radius_meters",
    "event_kind", "prior_category", "capacity_used", "capacity_source",
    "likely_in_city", "sessions", "outdoor", "reason",
]
UPLIFT_COLUMNS = ["footfall_uplift_percent", "demand_uplift_percent", "uplift_version", "uplift_json"]

# Columns added after the table was first created: (name, SQL type).
_ADDED_COLUMNS = [
    ("footfall_uplift_percent", "REAL"),
    ("demand_uplift_percent", "REAL"),
    ("uplift_version", "TEXT"),
    ("uplift_json", "TEXT"),
]


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attendance_estimates (
            canonical_key TEXT PRIMARY KEY,
            city TEXT NOT NULL,
            event_date TEXT,
            input_hash TEXT,
            prompt_version TEXT,
            model TEXT,
            status TEXT,               -- estimated | online | cancelled | outside_city | failed
            expected_attendance INTEGER,
            low INTEGER,
            high INTEGER,
            confidence TEXT,
            radius_meters INTEGER,
            event_kind TEXT,
            prior_category TEXT,
            capacity_used INTEGER,
            capacity_source TEXT,
            likely_in_city TEXT,
            sessions INTEGER,
            outdoor INTEGER,
            reason TEXT,
            estimate_json TEXT,
            footfall_uplift_percent REAL,  -- 0-20, see core/attendance/uplift.py
            demand_uplift_percent REAL,    -- 0-30
            uplift_version TEXT,
            uplift_json TEXT,              -- the factors behind the two numbers
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            estimated_at TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(attendance_estimates)")}
    for name, sql_type in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE attendance_estimates ADD COLUMN {name} {sql_type}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_estimates_city_date ON attendance_estimates (city, event_date)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS register_cache (
            cache_id TEXT PRIMARY KEY,
            result_json TEXT,
            looked_up_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
    conn.commit()
    conn.close()


def get_estimates(city):
    """{canonical_key: row dict} for every stored estimate of a city."""
    init_db()
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM attendance_estimates WHERE city = ?", (city,)
    ).fetchall()
    conn.close()
    return {row["canonical_key"]: dict(row) for row in rows}


def _uplift_values(uplift):
    uplift = uplift or {}
    factors = uplift.get("factors")
    return {
        "footfall_uplift_percent": uplift.get("footfall"),
        "demand_uplift_percent": uplift.get("demand"),
        "uplift_version": uplift.get("version"),
        "uplift_json": json.dumps(factors) if factors else None,
    }


def save_estimate(canonical_key, city, event_date, input_hash, prompt_version, model, estimate, uplift=None):
    """Store a finished estimate (from the LLM or from a rule) together with
    its uplift (core.attendance.uplift.compute_uplift result). Resets the
    failure counter."""
    values = {
        "status": estimate.get("status"),
        "expected_attendance": estimate.get("expectedAttendance"),
        "low": estimate.get("low"),
        "high": estimate.get("high"),
        "confidence": estimate.get("confidence"),
        "radius_meters": estimate.get("radiusMeters"),
        "event_kind": estimate.get("eventKind"),
        "prior_category": estimate.get("priorCategory"),
        "capacity_used": estimate.get("capacityUsed"),
        "capacity_source": estimate.get("capacitySource"),
        "likely_in_city": estimate.get("likelyInCity"),
        "sessions": estimate.get("sessions"),
        "outdoor": None if estimate.get("outdoor") is None else int(bool(estimate.get("outdoor"))),
        "reason": estimate.get("reason"),
        **_uplift_values(uplift),
    }
    columns = ESTIMATE_COLUMNS + UPLIFT_COLUMNS
    conn = get_connection()
    conn.execute(
        f"""
        INSERT INTO attendance_estimates (
            canonical_key, city, event_date, input_hash, prompt_version, model,
            {", ".join(columns)}, estimate_json, attempts, last_error,
            estimated_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, {", ".join("?" for _ in columns)}, ?, 0, NULL,
                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(canonical_key) DO UPDATE SET
            city = excluded.city,
            event_date = excluded.event_date,
            input_hash = excluded.input_hash,
            prompt_version = excluded.prompt_version,
            model = excluded.model,
            {", ".join(f"{c} = excluded.{c}" for c in columns)},
            estimate_json = excluded.estimate_json,
            attempts = 0,
            last_error = NULL,
            estimated_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            canonical_key, city, event_date, input_hash, prompt_version, model,
            *[values[c] for c in columns],
            json.dumps(estimate, ensure_ascii=False, default=str),
        ),
    )
    conn.commit()
    conn.close()


def save_uplift(canonical_key, uplift):
    """Update only the uplift columns of an existing row."""
    values = _uplift_values(uplift)
    conn = get_connection()
    conn.execute(
        f"UPDATE attendance_estimates SET {', '.join(f'{c} = ?' for c in UPLIFT_COLUMNS)}, "
        f"updated_at = CURRENT_TIMESTAMP WHERE canonical_key = ?",
        (*[values[c] for c in UPLIFT_COLUMNS], canonical_key),
    )
    conn.commit()
    conn.close()


def record_failure(canonical_key, city, event_date, input_hash, prompt_version, model, error):
    """The model gave nothing usable for this event: count the attempt so it is
    retried on a later run (up to enrich.max_attempts), without touching an
    older good estimate's numbers."""
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO attendance_estimates (
            canonical_key, city, event_date, input_hash, prompt_version, model,
            status, attempts, last_error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'failed', 1, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(canonical_key) DO UPDATE SET
            event_date = excluded.event_date,
            attempts = CASE
                WHEN attendance_estimates.input_hash IS excluded.input_hash
                 AND attendance_estimates.status = 'failed'
                THEN attendance_estimates.attempts + 1 ELSE 1 END,
            status = 'failed',
            input_hash = excluded.input_hash,
            prompt_version = excluded.prompt_version,
            model = excluded.model,
            last_error = excluded.last_error,
            updated_at = CURRENT_TIMESTAMP
        """,
        (canonical_key, city, event_date, input_hash, prompt_version, model, str(error)[:500]),
    )
    conn.commit()
    conn.close()


def get_register_cache(cache_id):
    """(found, result) - found is False when this venue was never looked up."""
    conn = get_connection()
    row = conn.execute(
        "SELECT result_json FROM register_cache WHERE cache_id = ?", (cache_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return False, None
    return True, json.loads(row["result_json"]) if row["result_json"] else None


def set_register_cache(cache_id, result):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO register_cache (cache_id, result_json) VALUES (?, ?)
        ON CONFLICT(cache_id) DO UPDATE SET result_json = excluded.result_json,
            looked_up_at = CURRENT_TIMESTAMP
        """,
        (cache_id, json.dumps(result, ensure_ascii=False) if result else None),
    )
    conn.commit()
    conn.close()


def summary(city=None):
    init_db()
    conn = get_connection()
    where, params = ("WHERE city = ?", (city,)) if city else ("", ())
    rows = conn.execute(
        f"SELECT city, status, COUNT(*) AS n FROM attendance_estimates {where} "
        f"GROUP BY city, status ORDER BY city, status",
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import sys

    init_db()
    for row in summary(sys.argv[1] if len(sys.argv) > 1 else None):
        print(f"{row['city']:<12} {str(row['status']):<14} {row['n']}")
