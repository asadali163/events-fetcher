"""The scheduler: started every 10 minutes by ONE cron entry, it decides
which city jobs are due (from core/pipeline.yaml) and runs them.

Jobs per city:
  collect - due when `collect.every_days` days have passed (in the city's
            local calendar) since the last SUCCESSFUL collect, and the local
            time is past `collect.at`. Runs core.collection (one process per
            source) and then core.enrich for `collect.window_days`.
  daily   - due once per local day, after `daily.at`. Runs core.enrich for
            `daily.window_days` (catch-up), core.export, then core.upload (S3).
A job that has never succeeded is due straight away. A job that failed is
retried after `retry_after_minutes`. If collect and daily are both due, collect
runs first. Every run is recorded in core/run_log.db.

When some sources fail during collect, the job is recorded as failed and the
retry runs ONLY those sources, up to `collect.max_attempts` attempts in total;
after that the job succeeds and the failed sources wait for the next regular
collect (see job_collect()).

Only one scheduler runs at a time (file lock), so a long collection simply
makes the next ticks exit immediately until it is done.

Usage:
    python -m core.scheduler                         # one tick: run whatever is due
    python -m core.scheduler --status                # what ran, what is due next
    python -m core.scheduler --dry-run               # show what a tick would run
    python -m core.scheduler --run daily --city Paris      # run a job now
    python -m core.scheduler --run collect --city Paris    # (ignores the timing)

Cron (every 10 minutes). xvfb-run gives the HEADLESS = False scrapers
(Meetup, Eventbrite, ...) a virtual screen on a server; TZ makes every
date.today() in the pipeline use the city's calendar day:
    */10 * * * * cd /path/to/events-fetcher && TZ=Europe/Paris xvfb-run -a .venv/bin/python -m core.scheduler >> logs/scheduler.log 2>&1
"""

import argparse
import fcntl
import json
import sqlite3
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.pipeline_config import get_city_config, list_cities  # noqa: E402

RUN_LOG_DB = Path(__file__).parent / "run_log.db"
LOCK_PATH = Path(__file__).parent / ".scheduler.lock"
JOBS = ("collect", "daily")


# --------------------------------------------------------------------------- #
# Run log
# --------------------------------------------------------------------------- #


def _conn():
    conn = sqlite3.connect(RUN_LOG_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,
            job TEXT NOT NULL,
            local_date TEXT NOT NULL,      -- the city's calendar date when it started
            started_at TEXT NOT NULL,      -- UTC ISO
            finished_at TEXT,
            status TEXT NOT NULL,          -- running | success | failed
            summary_json TEXT,
            error TEXT
        )
        """)
    return conn


def _start_run(city, job, local_date):
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO job_runs (city, job, local_date, started_at, status) VALUES (?, ?, ?, ?, 'running')",
        (city, job, local_date.isoformat(), datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()
    run_id = cur.lastrowid
    conn.close()
    return run_id


def _finish_run(run_id, status, summary=None, error=None):
    conn = _conn()
    conn.execute(
        "UPDATE job_runs SET finished_at = ?, status = ?, summary_json = ?, error = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), status,
         json.dumps(summary, default=str) if summary is not None else None,
         (error or "")[:4000] or None, run_id),
    )
    conn.commit()
    conn.close()


def _last_run(city, job, status=None):
    conn = _conn()
    sql = "SELECT * FROM job_runs WHERE city = ? AND job = ?"
    params = [city, job]
    if status:
        sql += " AND status = ?"
        params.append(status)
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
    conn.close()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# When is a job due?
# --------------------------------------------------------------------------- #


def _at_time(value):
    hour, minute = (int(x) for x in str(value or "00:00").split(":")[:2])
    return hour, minute


def is_due(city, job, config, now_utc=None):
    """(due, reason)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    local = now_utc.astimezone(ZoneInfo(config.get("timezone", "UTC")))
    job_cfg = config.get(job) or {}
    hour, minute = _at_time(job_cfg.get("at"))
    past_start_time = (local.hour, local.minute) >= (hour, minute)

    # Optional `first_run_on: YYYY-MM-DD` (city's calendar): the job never runs
    # before that day + `at`. Afterwards the normal timing applies.
    first_day = job_cfg.get("first_run_on")
    if first_day:
        first_day = date.fromisoformat(str(first_day))
        if local.date() < first_day or (local.date() == first_day and not past_start_time):
            return False, f"first run on {first_day} at {hour:02d}:{minute:02d}"

    last_any = _last_run(city, job)
    if last_any and last_any["status"] == "failed":
        retry = timedelta(minutes=float(config.get("retry_after_minutes", 60)))
        failed_at = datetime.fromisoformat(last_any["finished_at"] or last_any["started_at"])
        if now_utc - failed_at < retry:
            return False, f"last run failed; retry after {(failed_at + retry).astimezone(local.tzinfo):%H:%M}"

    last_ok = _last_run(city, job, "success")
    if not last_ok:
        return True, "never succeeded yet"
    last_date = date.fromisoformat(last_ok["local_date"])
    if job == "collect":
        every = int(job_cfg.get("every_days", 15))
        next_date = last_date + timedelta(days=every)
        if local.date() < next_date:
            return False, f"next on {next_date} at {hour:02d}:{minute:02d}"
    else:
        if last_date >= local.date():
            return False, f"already ran today; next tomorrow at {hour:02d}:{minute:02d}"
    if not past_start_time:
        return False, f"due today at {hour:02d}:{minute:02d}"
    return True, "due"


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


def _log(message):
    print(f"[scheduler {datetime.now().astimezone():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


class CollectIncomplete(Exception):
    """Some sources failed and will be retried: the run is recorded as failed
    (so the scheduler retries it after retry_after_minutes), but its summary
    is kept - the next attempt reads `retry_sources` / `attempt` from it."""

    def __init__(self, message, summary):
        super().__init__(message)
        self.summary = summary


def _previous_collect_attempt(city):
    """(attempt number, source ids) for this collect: continues a failed
    attempt that asked for a retry, otherwise starts fresh (1, None = all)."""
    conn = _conn()
    row = conn.execute(
        "SELECT status, summary_json FROM job_runs WHERE city = ? AND job = 'collect' "
        "AND status != 'running' ORDER BY id DESC LIMIT 1",
        (city,),
    ).fetchone()
    conn.close()
    if not row or row["status"] != "failed" or not row["summary_json"]:
        return 1, None
    try:
        previous = json.loads(row["summary_json"])
    except ValueError:
        return 1, None
    if not previous.get("retry_sources"):
        return 1, None
    return int(previous.get("attempt") or 1) + 1, list(previous["retry_sources"])


def job_collect(city, config, manual=False):
    """All sources on the first attempt. If some fail, the job is recorded as
    failed and the scheduler retries ONLY those sources after
    retry_after_minutes, up to collect.max_attempts attempts in total; after
    that the job succeeds anyway (failed sources listed in `gave_up_sources`)
    and they are fetched again at the next regular collect. A manual
    `--run collect` always starts a fresh full attempt."""
    from core.collection import run_collection
    from core.enrich import run_enrichment

    collect_cfg = config.get("collect") or {}
    max_attempts = max(1, int(collect_cfg.get("max_attempts", 3)))
    attempt, source_ids = (1, None) if manual else _previous_collect_attempt(city)
    if source_ids:
        _log(f"{city} collect: attempt {attempt}/{max_attempts}, retrying failed source(s): {', '.join(source_ids)}")

    summary = {"attempt": attempt,
               "collection": run_collection(city, collect_cfg.get("window_days"), source_ids)}
    try:  # an enrichment problem never turns a good collection into a failed job
        summary["enrichment"] = run_enrichment(city, collect_cfg.get("window_days"))
    except Exception as error:
        summary["enrichment"] = {"error": f"{type(error).__name__}: {error}"}
        _log(f"{city} collect: enrichment failed (will be retried by the daily job): {error}")

    failed = [s for s, info in summary["collection"]["sources"].items() if info["status"] == "failed"]
    if failed and attempt < max_attempts:
        summary["retry_sources"] = failed
        raise CollectIncomplete(
            f"{len(failed)} source(s) failed: {', '.join(failed)} - "
            f"retrying them (attempt {attempt + 1}/{max_attempts})", summary)
    if failed:
        summary["gave_up_sources"] = failed
        _log(f"{city} collect: giving up on {', '.join(failed)} after {attempt} attempt(s) "
             f"- they run again at the next regular collect")
    return summary


def job_daily(city, config):
    from core.enrich import run_enrichment
    from core.export import run_export
    from core.upload import run_upload

    summary = {}
    try:
        summary["enrichment"] = run_enrichment(city, (config.get("daily") or {}).get("window_days"))
    except Exception as error:  # still export whatever estimates exist
        summary["enrichment"] = {"error": f"{type(error).__name__}: {error}"}
        _log(f"{city} daily: enrichment failed, exporting what exists: {error}")
    summary["export"] = run_export(city, (config.get("daily") or {}).get("window_days"))
    # An upload error fails the job, so the scheduler retries it after
    # retry_after_minutes (the retry costs no LLM tokens: nothing left to estimate).
    summary["upload"] = run_upload(city, summary["export"]["public_file"])
    return summary


JOB_FUNCTIONS = {"collect": job_collect, "daily": job_daily}


def run_job(city, job, config, manual=False):
    local_date = datetime.now(ZoneInfo(config.get("timezone", "UTC"))).date()
    run_id = _start_run(city, job, local_date)
    _log(f"{city}: starting {job} (run {run_id})")
    try:
        if job == "collect":
            summary = job_collect(city, config, manual=manual)
        else:
            summary = JOB_FUNCTIONS[job](city, config)
    except CollectIncomplete as error:
        _finish_run(run_id, "failed", summary=error.summary, error=str(error))
        _log(f"{city}: {job} INCOMPLETE - {error}")
        return False
    except Exception as error:
        _finish_run(run_id, "failed", error=traceback.format_exc())
        _log(f"{city}: {job} FAILED - {type(error).__name__}: {error}")
        return False
    _finish_run(run_id, "success", summary=summary)
    _log(f"{city}: {job} finished")
    return True


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def _acquire_lock():
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(str(datetime.now()))
    handle.flush()
    return handle


def _mark_stale_runs():
    """A 'running' row left by a scheduler that was killed counts as failed
    (we hold the lock, so nothing else is running)."""
    conn = _conn()
    conn.execute(
        "UPDATE job_runs SET status = 'failed', error = 'scheduler stopped before the job finished', "
        "finished_at = COALESCE(finished_at, started_at) WHERE status = 'running'"
    )
    conn.commit()
    conn.close()


def tick(dry_run=False):
    lock = _acquire_lock()
    if lock is None:
        _log("another scheduler run is still working - exiting")
        return
    try:
        _mark_stale_runs()
        for city in list_cities():
            try:
                config = get_city_config(city)
            except Exception as error:
                _log(f"{city}: bad config, skipped - {error}")
                continue
            for job in JOBS:
                try:
                    due, reason = is_due(city, job, config)
                except Exception as error:
                    _log(f"{city} {job}: could not check timing - {error}")
                    continue
                if not due:
                    continue
                if dry_run:
                    _log(f"{city}: would run {job} ({reason})")
                    continue
                run_job(city, job, config)  # never raises
    finally:
        lock.close()


def status():
    for city in list_cities(enabled_only=False):
        config = get_city_config(city)
        print(f"\n{city}  (enabled: {config.get('enabled', True)}, timezone {config.get('timezone')})")
        for job in JOBS:
            due, reason = is_due(city, job, config)
            last = _last_run(city, job)
            last_ok = _last_run(city, job, "success")
            print(f"  {job:<8} due now: {'YES' if due else 'no '}  ({reason})")
            if last:
                print(f"           last run: {last['started_at']} -> {last['status']}"
                      + (f" | {str(last['error']).splitlines()[-1][:120]}" if last.get("error") else ""))
            if last_ok and last and last_ok["id"] != last["id"]:
                print(f"           last success: {last_ok['started_at']}")


def main():
    parser = argparse.ArgumentParser(description="Run whatever city jobs are due (meant for cron, every 10 min).")
    parser.add_argument("--status", action="store_true", help="show last runs and what is due")
    parser.add_argument("--dry-run", action="store_true", help="show what a tick would run, run nothing")
    parser.add_argument("--run", choices=[*JOBS, "all"], help="run a job now for --city, ignoring timing")
    parser.add_argument("--city", help="city for --run")
    args = parser.parse_args()

    if args.status:
        status()
        return
    if args.run:
        if not args.city:
            parser.error("--run needs --city")
        lock = _acquire_lock()
        if lock is None:
            _log("another scheduler run is still working - try again later")
            sys.exit(1)
        try:
            _mark_stale_runs()
            config = get_city_config(args.city)
            for job in (JOBS if args.run == "all" else [args.run]):
                run_job(args.city, job, config, manual=True)
        finally:
            lock.close()
        return
    tick(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
