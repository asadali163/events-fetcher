import json
from core.raw_store import get_connection

conn = get_connection()
rows = conn.execute("SELECT * FROM raw_events").fetchall()
for row in rows:
    print(row["id"], row["source"], row["city"], row["fetched_at"])
    print(json.dumps(json.loads(row["raw_json"]), indent=2))
    print("---")
conn.close()
