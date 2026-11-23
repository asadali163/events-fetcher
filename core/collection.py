"""Collection job: every source for a city, each in its OWN process.

Why a process per source: a scraper can do worse than raise an exception -
hang forever, crash the interpreter (browser/driver segfault), call
sys.exit(), or eat all memory. In a separate process none of that can take
the rest of the run down. Each source gets source_timeout_minutes; if it
runs over, it is killed (with any browser it started) and its events are
discarded. Only sources that finish cleanly feed dedup and the canonical
save.

The refetch log is written only for sources that finished AND whose events
were saved to canonical, so a crashed or killed source is fetched again next
time instead of being marked "covered".

Inside each worker the existing pipeline runs unchanged:
core.dispatcher.run_connector() (refetch-log check, raw + normalized saves,
validate). The parent then runs deduplicate_events() over all good sources
and save_canonical_event(), exactly like dispatcher.get_events_for_city().

Usage:
    python -m core.collection Paris                    # window = collect.window_days
    python -m core.collection Paris --days 30 --sources openagenda,dice
    python -m core.collection Paris --force            # ignore the refetch log
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.cities_loader import get_country_for_city  # noqa: E402
from core.pipeline_config import get_city_config  # noqa: E402
from core.registry_loader import get_sources_for_country  # noqa: E402

KILL_GRACE_SECONDS = 20


# --------------------------------------------------------------------------- #
# Worker: runs ONE source, writes its result to a JSON file
# --------------------------------------------------------------------------- #


def _worker(city, source_id, days, out_path, force):
    from core import dispatcher

    result = {"status": "failed", "error": None, "events": [], "fetch_logs": []}

    def write():
        Path(out_path).write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")

    try:
        country = get_country_for_city(city)
        source = next((s for s in get_sources_for_country(country) if s["id"] == source_id), None)
        if source is None:
            result["error"] = f"'{source_id}' isn't registered for {city} ({country})"
            write()
            return

        # Hold the refetch-log write back: the parent writes it only after
        # this source's events reach canonical.
        dispatcher.log_fetch = lambda *args: result["fetch_logs"].append(list(args))
        if force:
            dispatcher.is_covered = lambda *a, **k: False
            dispatcher.is_recently_fetched = lambda *a, **k: False

        state = {"load_error": None, "discover_called": False, "discover_error": None}
        original_load = dispatcher.load_connector

        def tracked_load(entry_point, days_ahead=None):
            try:
                connector = original_load(entry_point, days_ahead)
            except Exception as error:
                state["load_error"] = f"{type(error).__name__}: {error}"
                raise
            original_discover = connector.discover

            def discover(*args, **kwargs):
                state["discover_called"] = True
                try:
                    return original_discover(*args, **kwargs)
                except Exception as error:
                    state["discover_error"] = f"{type(error).__name__}: {error}"
                    raise

            connector.discover = discover
            return connector

        dispatcher.load_connector = tracked_load
        events = dispatcher.run_connector(source, city, country, days)

        failed_log = next((log for log in result["fetch_logs"] if log[-1] == "failed"), None)
        if state["load_error"]:
            result["error"] = f"could not be loaded: {state['load_error']}"
        elif state["discover_error"] or failed_log:
            result["error"] = f"discover() failed: {state['discover_error'] or 'see log above'}"
        elif not state["discover_called"]:
            result["status"] = "skipped"  # already covered by an earlier fetch
        else:
            result["status"] = "ok"
        result["events"] = events if result["status"] == "ok" else []
    except BaseException as error:  # noqa: BLE001 - report anything, incl. SystemExit
        result["status"] = "failed"
        result["error"] = f"{type(error).__name__}: {error}"
        result["events"] = []
    write()


# --------------------------------------------------------------------------- #
# Parent: one worker process per source, then dedup + canonical save
# --------------------------------------------------------------------------- #


def _kill_group(proc):
    """Stop the worker and everything it started (browsers, drivers)."""
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_SECONDS), (signal.SIGKILL, 10)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _run_source(city, source_id, days, timeout_seconds, force):
    started = time.time()
    with tempfile.NamedTemporaryFile(prefix=f"collect_{source_id}_", suffix=".json", delete=False) as tmp:
        out_path = tmp.name
    cmd = [sys.executable, "-m", "core.collection", "--worker", city,
           "--source", source_id, "--out", out_path]
    if days is not None:
        cmd += ["--days", str(days)]
    if force:
        cmd.append("--force")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    outcome = {"source": source_id, "status": "failed", "error": None, "events": [], "fetch_logs": []}
    try:
        proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, env=env, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            outcome["error"] = f"timed out after {timeout_seconds / 60:g} min - killed, events discarded"
            return outcome
        finally:
            if proc.poll() is None:
                _kill_group(proc)
            else:
                # The worker may have left a browser running in its group.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

        try:
            result = json.loads(Path(out_path).read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            result = {}
        if code != 0 or not result:
            outcome["error"] = f"worker crashed (exit code {code}) - events discarded"
            return outcome
        outcome.update(
            status=result.get("status", "failed"),
            error=result.get("error"),
            events=result.get("events") or [],
            fetch_logs=result.get("fetch_logs") or [],
        )
        return outcome
    except Exception as error:  # could not even start the worker
        outcome["error"] = f"could not start worker: {type(error).__name__}: {error}"
        return outcome
    finally:
        outcome["seconds"] = round(time.time() - started, 1)
        try:
            os.unlink(out_path)
        except OSError:
            pass


def run_collection(city, days=None, source_ids=None, force=False):
    """Returns a summary dict. A failing source never raises; only a failure
    in dedup/canonical saving itself does (then no refetch log is written)."""
    from core.canonical_store import init_db as init_canonical, save_canonical_event
    from core.dedup import deduplicate_events
    from core.refetch_log import init_db as init_refetch, log_fetch

    config = get_city_config(city)
    collect_cfg = config.get("collect") or {}
    days = days or collect_cfg.get("window_days", 30)
    timeout_seconds = int(float(collect_cfg.get("source_timeout_minutes", 120)) * 60)
    excluded = set(collect_cfg.get("exclude_sources") or [])

    country = get_country_for_city(city)
    sources = [s["id"] for s in get_sources_for_country(country) if s["id"] not in excluded]
    if source_ids:
        unknown = sorted(set(source_ids) - set(sources))
        if unknown:
            print(f"[collect] ignoring source(s) not active for {city}: {', '.join(unknown)}")
        sources = [s for s in sources if s in source_ids]

    started = time.time()
    print(f"[collect] {city} ({country}), next {days} days, {len(sources)} source(s): {', '.join(sources)}")
    outcomes = []
    for source_id in sources:
        print(f"[collect] --- {source_id} ---", flush=True)
        outcome = _run_source(city, source_id, days, timeout_seconds, force)
        outcomes.append(outcome)
        print(
            f"[collect] {source_id}: {outcome['status']}, {len(outcome['events'])} valid event(s), "
            f"{outcome['seconds']}s" + (f" - {outcome['error']}" if outcome["error"] else ""),
            flush=True,
        )

    good = [o for o in outcomes if o["status"] == "ok"]
    all_events = [e for o in good for e in o["events"]]
    merged = deduplicate_events(all_events)

    init_canonical()
    saved, save_errors = 0, 0
    for event in merged:
        try:
            save_canonical_event(city, event)
            saved += 1
        except Exception as error:
            save_errors += 1
            print(f"[collect] failed to save one canonical event, continuing: {error}")

    init_refetch()
    for o in outcomes:
        if o["status"] in ("ok", "failed"):
            for log in o["fetch_logs"]:
                try:
                    log_fetch(*log)
                except Exception as error:
                    print(f"[collect] could not write refetch log for {o['source']}: {error}")

    summary = {
        "city": city,
        "days": days,
        "sources": {o["source"]: {"status": o["status"], "events": len(o["events"]),
                                  "seconds": o["seconds"], "error": o["error"]} for o in outcomes},
        "valid_events": len(all_events),
        "deduplicated": len(merged),
        "saved": saved,
        "save_errors": save_errors,
        "seconds": round(time.time() - started, 1),
    }
    ok = sum(1 for o in outcomes if o["status"] == "ok")
    failed = [o["source"] for o in outcomes if o["status"] == "failed"]
    print(
        f"[collect] done: {ok} ok, {len(failed)} failed{(' (' + ', '.join(failed) + ')') if failed else ''}, "
        f"{len(all_events)} valid -> {len(merged)} after dedup -> {saved} saved"
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description="Collect a city's events, one process per source.")
    parser.add_argument("city")
    parser.add_argument("--days", type=int, default=None, help="window (default: collect.window_days)")
    parser.add_argument("--sources", default=None, help="comma-separated source ids (default: all)")
    parser.add_argument("--force", action="store_true", help="ignore the refetch log")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--out", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker:
        _worker(args.city, args.source, args.days, args.out, args.force)
        return
    source_ids = [s.strip() for s in args.sources.split(",")] if args.sources else None
    summary = run_collection(args.city, args.days, source_ids, args.force)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
