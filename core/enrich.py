"""Enrichment job: attendance + impact radius for a city's canonical events.

Picks the city's canonical events in the next N days (plus events that
started earlier and are still running), and estimates the ones that need it:
  - no estimate yet,
  - the event's LLM input changed (new capacity, status, description...),
    or the prompt version / model changed,
  - an earlier attempt failed (up to enrich.max_attempts per input).
Soonest dates go first, and at most enrich.max_llm_events_per_key x (number of Groq keys) go to the
LLM per run, so a quota limit only delays later dates. Rules (online,
cancelled, outside the city) settle events without any LLM call.

Every saved estimate also gets footfall/demand uplift percentages
(core/attendance/uplift.py, plain arithmetic). At the end of each run the
uplift of every event in the window is refreshed, so rows stored before the
uplift existed - or under an older UPLIFT_VERSION - are filled in without any
LLM call.

Results go to core/enrichment.db. Nothing here can break collection: it only
reads canonical_events.db.

Usage:
    python -m core.enrich Paris                 # window = daily.window_days
    python -m core.enrich Paris --days 30
    python -m core.enrich Paris --days 14 --max 20   # cap LLM events (testing)
    python -m core.enrich Paris --force          # re-estimate everything in the window
    python -m core.enrich Paris --uplift-only    # (re)compute uplift for ALL stored rows, no LLM
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import enrichment_store  # noqa: E402
from core.attendance.estimator import estimate_with_llm, prepare  # noqa: E402
from core.attendance.llm import DailyTokenLimit, LLMUnavailable, groq_keys, make_client  # noqa: E402
from core.attendance.prompt import PROMPT_VERSION  # noqa: E402
from core.attendance.rules import apply_rules  # noqa: E402
from core.attendance.uplift import uplift_from_estimate, uplift_from_row  # noqa: E402
from core.canonical_store import DB_PATH as CANONICAL_DB  # noqa: E402
from core.pipeline_config import get_city_config  # noqa: E402


def load_window_events(city, days, today=None):
    """Canonical events of `city` happening in [today, today + days), as
    (canonical_key, event dict) sorted by date."""
    today = today or date.today()
    start, end = today.isoformat(), (today + timedelta(days=days - 1)).isoformat()
    conn = sqlite3.connect(f"file:{CANONICAL_DB}?mode=ro", uri=True, timeout=30)
    rows = conn.execute(
        """
        SELECT canonical_key, canonical_json FROM canonical_events
        WHERE city = ?
          AND ((date >= ? AND date <= ?) OR (date < ? AND end_date >= ?))
        ORDER BY date, canonical_key
        """,
        (city, start, end, start, start),
    ).fetchall()
    conn.close()
    events = []
    for key, blob in rows:
        try:
            events.append((key, json.loads(blob)))
        except (TypeError, ValueError):
            print(f"[enrich] unreadable canonical_json for {key!r} - skipped")
    return events


def load_events_by_key(city, keys):
    """{canonical_key: event dict} for the given keys (any date)."""
    keys = list(keys)
    conn = sqlite3.connect(f"file:{CANONICAL_DB}?mode=ro", uri=True, timeout=30)
    found = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        rows = conn.execute(
            f"SELECT canonical_key, canonical_json FROM canonical_events "
            f"WHERE city = ? AND canonical_key IN ({', '.join('?' for _ in chunk)})",
            (city, *chunk),
        ).fetchall()
        for key, blob in rows:
            try:
                found[key] = json.loads(blob)
            except (TypeError, ValueError):
                pass
    conn.close()
    return found


def _same(a, b):
    return (a is None and b is None) or (a is not None and b is not None and abs(float(a) - float(b)) < 1e-9)


def refresh_uplift(city, events=None):
    """Recompute uplift for stored rows and write only the ones that changed.
    events: [(canonical_key, event)]; None = every stored row of the city.
    Returns the number of rows updated."""
    stored = enrichment_store.get_estimates(city)
    if events is None:
        by_key = load_events_by_key(city, stored)
        events = list(by_key.items())
    updated = 0
    for key, event in events:
        row = stored.get(key)
        if not row:
            continue
        try:
            uplift = uplift_from_row(event, row)
        except Exception as error:  # one odd event never stops the run
            print(f"[enrich] could not compute uplift for {key!r}: {error}")
            continue
        if (row.get("uplift_version") == uplift["version"]
                and _same(row.get("footfall_uplift_percent"), uplift["footfall"])
                and _same(row.get("demand_uplift_percent"), uplift["demand"])):
            continue
        enrichment_store.save_uplift(key, uplift)
        updated += 1
    return updated


def run_enrichment(city, days=None, max_llm_events=None, force=False):
    """Returns a summary dict (also printed). Never raises for per-event
    problems; raises only if the city config itself is missing."""
    config = get_city_config(city)
    enrich_cfg = config.get("enrich") or {}
    settings = config.get("attendance") or {}
    days = days or (config.get("daily") or {}).get("window_days", 14)
    model = enrich_cfg.get("model", "openai/gpt-oss-20b")
    batch_size = int(enrich_cfg.get("batch_size", 8))
    max_attempts = int(enrich_cfg.get("max_attempts", 3))
    if max_llm_events is None:
        per_key = enrich_cfg.get("max_llm_events_per_key")
        if per_key:  # each Groq key (organization) has its own daily quota
            max_llm_events = int(per_key) * max(1, len(groq_keys()))
        else:
            max_llm_events = int(enrich_cfg.get("max_llm_events_per_run", 220))

    summary = {
        "city": city, "days": days, "model": model, "prompt_version": PROMPT_VERSION,
        "in_window": 0, "up_to_date": 0, "given_up": 0, "ruled": 0,
        "llm_queued": 0, "llm_estimated": 0, "llm_failed": 0, "left_for_next_run": 0,
        "tokens": 0, "uplift_updated": 0, "stopped": None,
    }
    if not enrich_cfg.get("enabled", True):
        summary["stopped"] = "enrichment disabled for this city in pipeline.yaml"
        print(f"[enrich] {summary['stopped']}")
        return summary

    started = time.time()
    enrichment_store.init_db()
    events = load_window_events(city, days)
    stored = enrichment_store.get_estimates(city)
    summary["in_window"] = len(events)

    to_llm = []
    for key, event in events:
        try:
            data, digest = prepare(event, settings, model)
        except Exception as error:  # one bad event never stops the run
            print(f"[enrich] could not prepare {key!r}: {error}")
            continue
        row = stored.get(key)
        if row and not force and row.get("input_hash") == digest:
            if row.get("status") != "failed":
                summary["up_to_date"] += 1
                continue
            if (row.get("attempts") or 0) >= max_attempts:
                summary["given_up"] += 1
                continue

        ruled = apply_rules(event, settings)
        if ruled:
            enrichment_store.save_estimate(key, city, event.get("date"), digest, PROMPT_VERSION, "rule", ruled,
                                           uplift=uplift_from_estimate(event, ruled))
            summary["ruled"] += 1
        else:
            to_llm.append((key, event, data, digest))

    queued = to_llm[:max_llm_events]
    summary["llm_queued"] = len(queued)
    summary["left_for_next_run"] = len(to_llm) - len(queued)
    print(
        f"[enrich] {city}: {len(events)} events in the next {days} days - "
        f"{summary['up_to_date']} up to date, {summary['ruled']} settled by rules, "
        f"{len(queued)} to the LLM ({model}), {summary['left_for_next_run']} left for the next run"
    )

    if queued:
        by_key = {key: (event, digest) for key, event, _, digest in queued}

        def on_result(key, estimate):
            event, digest = by_key[key]
            enrichment_store.save_estimate(key, city, event.get("date"), digest, PROMPT_VERSION, model, estimate,
                                           uplift=uplift_from_estimate(event, estimate))
            summary["llm_estimated"] += 1

        def on_failure(key, error):
            event, digest = by_key[key]
            enrichment_store.record_failure(key, city, event.get("date"), digest, PROMPT_VERSION, model, error)
            summary["llm_failed"] += 1

        client = None
        try:
            client = make_client(enrich_cfg.get("provider", "groq"))
            summary["groq_keys"] = len(client.keys)
            summary["tokens"] = estimate_with_llm(
                client, model, city, settings,
                [(key, event, data) for key, event, data, _ in queued],
                batch_size, on_result, on_failure,
            )
        except DailyTokenLimit as error:
            done = summary["llm_estimated"] + summary["llm_failed"]
            summary["left_for_next_run"] += len(queued) - done
            summary["stopped"] = f"daily LLM quota reached: {error}"
            print(f"[enrich] STOPPED - {summary['stopped'][:200]}")
        except LLMUnavailable as error:
            summary["left_for_next_run"] += len(queued) - summary["llm_estimated"] - summary["llm_failed"]
            summary["stopped"] = f"LLM unavailable: {error}"
            print(f"[enrich] STOPPED - {summary['stopped'][:200]}")
        if client is not None:  # tokens per key, also when the run stopped early
            summary["tokens"] = sum(client.tokens)
            summary["key_usage"] = client.report()

    try:
        summary["uplift_updated"] = refresh_uplift(city, events)
    except Exception as error:
        print(f"[enrich] uplift refresh failed, continuing: {error}")

    summary["seconds"] = round(time.time() - started, 1)
    print(f"[enrich] done: {json.dumps(summary, default=str)}")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Estimate attendance + impact radius for a city's events.")
    parser.add_argument("city")
    parser.add_argument("--days", type=int, default=None, help="window size (default: daily.window_days)")
    parser.add_argument("--max", type=int, default=None, help="max events sent to the LLM this run")
    parser.add_argument("--force", action="store_true", help="re-estimate even up-to-date events")
    parser.add_argument("--uplift-only", action="store_true",
                        help="only (re)compute footfall/demand uplift for every stored row - no LLM")
    args = parser.parse_args()
    if args.uplift_only:
        enrichment_store.init_db()
        print(f"[enrich] uplift updated for {refresh_uplift(args.city)} row(s)")
        return
    run_enrichment(args.city, args.days, args.max, args.force)


if __name__ == "__main__":
    main()
