"""
GetEvents fetch dashboard (Streamlit).

Pick a city, pick how far ahead to fetch, choose which sources to run, and
watch each one's progress live. Scraping itself runs in a separate process
(dashboard/runner.py) that reuses the existing pipeline unchanged — see that
file's docstring.

Run from the project root:
    streamlit run dashboard/app.py
"""

import importlib
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.cities_loader import load_cities  # noqa: E402
from core.refetch_log import init_db as init_refetch_log  # noqa: E402
from core.refetch_log import is_covered, is_recently_fetched  # noqa: E402
from core.registry_loader import get_sources_for_country  # noqa: E402

RUNNER_PATH = Path(__file__).resolve().parent / "runner.py"
PROGRESS_PREFIX = "@@PROGRESS "  # must match runner.py
MAX_LOG_LINES = 500
UI_REFRESH_SECONDS = 0.5
DEFAULT_WINDOW_DAYS = 14

STATUS_LABELS = {
    "queued": "⏳ Queued",
    "discovering": "🔍 Scraping…",
    "processing": "⚙️ Processing",
    "done": "✅ Done",
    "skipped": "⏭️ Skipped",
    "failed": "❌ Failed",
    "stopped": "⏹️ Stopped",
}

st.set_page_config(page_title="GetEvents · Fetch", page_icon="🎫", layout="wide")


# --------------------------------------------------------------------------- #
# Source metadata
# --------------------------------------------------------------------------- #


@st.cache_data(show_spinner=False)
def connector_info(entry_point):
    """Facts about a connector that affect what a run will do, read from the
    connector class itself (instantiating it never opens a browser or hits
    the network — that only happens in discover())."""
    module_path, class_name = entry_point.rsplit(".", 1)
    try:
        connector_class = getattr(importlib.import_module(module_path), class_name)
        probe = connector_class(days_ahead=7)
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}
    return {
        "error": None,
        # Meetup/Universe force days_ahead to None: no date filter at all.
        "uses_window": getattr(probe, "days_ahead", None) is not None,
        "refetch_interval": getattr(connector_class, "REFETCH_INTERVAL_DAYS", None),
        "headless": getattr(connector_class, "HEADLESS", None),
    }


def skip_reason(source_id, city, info, days_ahead):
    """Mirrors core.dispatcher.run_connector()'s refetch-log checks, so the
    user can see up front which sources would be skipped."""
    if info.get("uses_window"):
        window_start = date.today().isoformat()
        window_end = (date.today() + timedelta(days=days_ahead - 1)).isoformat()
        if is_covered(source_id, city, window_start, window_end):
            return "already fetched for this whole date window"
    elif info.get("refetch_interval"):
        if is_recently_fetched(source_id, city, info["refetch_interval"]):
            return f"already fetched within the last {info['refetch_interval']} day(s)"
    return None


def describe_source(source, info):
    parts = [source.get("type", "?")]
    if info.get("error"):
        return f"⚠️ can't be loaded: {info['error']}"
    if info["uses_window"]:
        parts.append("uses the date window")
    else:
        interval = info.get("refetch_interval")
        parts.append(
            "no date filter — returns everything it finds"
            + (f", re-fetches every {interval} days" if interval else "")
        )
    if info.get("headless") is False:
        parts.append("opens a visible browser window")
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
# Run state + subprocess handling
# --------------------------------------------------------------------------- #


def new_run(city, country, end_date, days_ahead, source_ids, force):
    return {
        "city": city,
        "country": country,
        "end_date": end_date.isoformat(),
        "days_ahead": days_ahead,
        "force": force,
        "source_ids": source_ids,
        "rows": {
            sid: {
                "status": "queued",
                "done": 0,
                "total": None,
                "valid": None,
                "started": None,
                "seconds": None,
                "message": "",
                "error": None,
            }
            for sid in source_ids
        },
        "current": None,
        "phase": "Starting…",
        "status": "running",  # running | finished | stopped | failed
        "summary": None,
        "events": None,
        "log": [],
        "started": time.time(),
    }


def apply_progress(run, event):
    kind = event["kind"]
    sid = event.get("source")
    row = run["rows"].get(sid) if sid else None

    if kind == "source_start" and row is not None:
        run["current"] = sid
        row.update(status="discovering", started=event["t"])
        run["phase"] = f"Running {sid}"
    elif kind == "stage":
        if row is not None:
            row["status"] = event["stage"]
            if event.get("total") is not None:
                row["total"] = event["total"]
        elif event["stage"] == "deduplicating":
            run["current"] = None
            run["phase"] = f"Deduplicating {event.get('total', 0)} events and saving…"
    elif kind == "item" and row is not None:
        row.update(status="processing", done=event["done"], total=event["total"])
    elif kind == "source_end" and row is not None:
        row.update(
            status=event["status"],
            valid=event["valid"],
            seconds=event["seconds"],
            error=event.get("error"),
        )
        if event["status"] == "skipped":
            row["message"] = "already fetched recently (refetch log)"
        elif event["status"] == "failed":
            row["message"] = event.get("error") or row["message"]
        if row["total"] is None:
            row["total"] = event.get("raw") or 0
        run["current"] = None
    elif kind == "finished":
        run["summary"] = event
        run["phase"] = "Finished"
    elif kind == "fatal":
        run["status"] = "failed"
        run["phase"] = f"Run failed: {event['error']}"


def apply_line(run, line):
    if run["status"] != "running":
        return  # stopped from the UI — ignore output still in flight
    line = line.rstrip("\n").split("\r")[-1]
    if line.startswith(PROGRESS_PREFIX):
        try:
            apply_progress(run, json.loads(line[len(PROGRESS_PREFIX):]))
        except (json.JSONDecodeError, KeyError):
            run["log"].append(line)
        return
    if not line.strip():
        return
    run["log"].append(line)
    del run["log"][:-MAX_LOG_LINES]
    if run["current"]:
        run["rows"][run["current"]]["message"] = line.strip()[:200]


def start_runner(run):
    fd, output_path = tempfile.mkstemp(prefix="getevents_dashboard_", suffix=".json")
    os.close(fd)
    command = [
        sys.executable,
        "-u",
        str(RUNNER_PATH),
        "--city",
        run["city"],
        "--days",
        str(run["days_ahead"]),
        "--sources",
        ",".join(run["source_ids"]),
        "--output",
        output_path,
    ]
    if run["force"]:
        command.append("--force")

    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        # Own process group, so Stop also kills any browsers it launched.
        start_new_session=True,
    )
    run["pid"] = process.pid
    lines = queue.Queue()

    def pump():
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    return process, lines, output_path


def kill_runner(process):
    if process.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def finalize_stopped(run):
    if run["status"] == "running":
        run["status"] = "stopped"
        run["phase"] = "Stopped by user"
        for row in run["rows"].values():
            if row["status"] in ("queued", "discovering", "processing"):
                row["status"] = "stopped"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def fmt_seconds(seconds):
    if seconds is None:
        return ""
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def row_fraction(row):
    status = row["status"]
    if status in ("done", "skipped", "failed"):
        return 1.0
    if status == "processing" and row["total"]:
        return 0.1 + 0.9 * row["done"] / row["total"]
    if status == "discovering":
        return 0.05
    return 0.0


def progress_table(run):
    records = []
    now = time.time()
    for sid, row in run["rows"].items():
        if row["seconds"] is not None:
            elapsed = row["seconds"]
        elif row["started"] and run["status"] == "running":
            elapsed = now - row["started"]
        else:
            elapsed = None

        if row["status"] == "processing" and row["total"]:
            detail = f"{row['done']} / {row['total']} events"
        elif row["status"] == "discovering":
            detail = "collecting events from the source…"
        elif row["total"] is not None:
            detail = f"{row['total']} found"
        else:
            detail = ""

        records.append(
            {
                "Source": sid,
                "Status": STATUS_LABELS.get(row["status"], row["status"]),
                "Progress": round(100 * row_fraction(row)),
                "Detail": detail,
                "Valid events": "" if row["valid"] is None else str(row["valid"]),
                "Time": fmt_seconds(elapsed),
                "Latest message": row["message"],
            }
        )
    return pd.DataFrame(records)


def render_progress(run, overall_ph, table_ph, log_ph):
    fractions = [row_fraction(r) for r in run["rows"].values()]
    overall = sum(fractions) / len(fractions) if fractions else 0.0
    if run["status"] == "running" and run["summary"] is None:
        overall = min(overall, 0.99)
    elapsed = fmt_seconds(time.time() - run["started"]) if run["status"] == "running" else ""
    finished = sum(
        r["status"] in ("done", "skipped", "failed") for r in run["rows"].values()
    )
    text = f"{run['phase']} — {finished}/{len(run['rows'])} sources finished"
    if elapsed:
        text += f" · {elapsed} elapsed"
    overall_ph.progress(overall, text=text)

    table_ph.dataframe(
        progress_table(run),
        hide_index=True,
        width="stretch",
        column_config={
            "Progress": st.column_config.ProgressColumn(
                "Progress", min_value=0, max_value=100, format="%d%%"
            ),
            "Latest message": st.column_config.TextColumn(width="large"),
        },
    )
    log_ph.code("\n".join(run["log"][-200:]) or "(no output yet)", language=None)


def events_dataframe(events):
    records = []
    for event in events:
        found_on = sorted(
            {s.get("source") for s in event.get("sources") or [] if s.get("source")}
        ) or [event.get("source")]
        records.append(
            {
                "Date": event.get("date"),
                "Time": event.get("time"),
                "Event": event.get("event_name"),
                "Venue": event.get("venue_name"),
                "Venue city": event.get("venue_city"),
                "Type": event.get("type"),
                "Found on": ", ".join(filter(None, found_on)),
                "URL": event.get("url"),
            }
        )
    frame = pd.DataFrame(records)
    if not frame.empty:
        frame = frame.sort_values(["Date", "Time"], na_position="last")
    return frame


def render_results(run):
    summary = run["summary"] or {}
    events = run["events"] or []

    if run["status"] == "finished":
        st.success(
            f"Done. {summary.get('total_valid', 0)} valid events from the selected "
            f"sources, {summary.get('deduplicated', 0)} after removing duplicates "
            f"(saved to the canonical database)."
        )
    elif run["status"] == "stopped":
        st.warning(
            "Run stopped. Raw and normalized events from sources that already ran "
            "are saved, but nothing was deduplicated or written to the canonical "
            "database for this run."
        )
    elif run["status"] == "failed":
        st.error(run["phase"])

    failed = {sid: r["error"] for sid, r in run["rows"].items() if r["status"] == "failed"}
    for sid, error in failed.items():
        st.error(f"**{sid}** failed: {error}")

    if not events:
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("Valid events", summary.get("total_valid", len(events)))
    c2.metric("After deduplication", len(events))
    c3.metric("Merged duplicates", summary.get("total_valid", len(events)) - len(events))

    frame = events_dataframe(events)
    st.dataframe(
        frame,
        hide_index=True,
        width="stretch",
        column_config={"URL": st.column_config.LinkColumn("URL", display_text="open")},
    )

    stem = f"events_{run['city'].lower().replace(' ', '_')}_{date.today().isoformat()}"
    d1, d2, _ = st.columns([1, 1, 4])
    d1.download_button(
        "Download JSON",
        json.dumps(events, indent=2, default=str),
        file_name=f"{stem}.json",
        mime="application/json",
    )
    d2.download_button(
        "Download CSV",
        frame.to_csv(index=False),
        file_name=f"{stem}.csv",
        mime="text/csv",
    )


def execute_run(run):
    """Launch the runner and stream its progress into the page. Blocks until
    the runner exits. Clicking Stop (or any widget) makes Streamlit interrupt
    this function, and the finally block then kills the runner."""
    st.subheader(f"Fetching {run['city']} · today → {run['end_date']}")
    overall_ph = st.empty()
    table_ph = st.empty()
    st.button("⏹ Stop", on_click=request_stop, type="secondary")
    with st.expander("Live log", expanded=False):
        log_ph = st.empty()

    process, lines, output_path = start_runner(run)
    try:
        last_render = 0.0
        eof = False
        while not eof:
            try:
                line = lines.get(timeout=UI_REFRESH_SECONDS)
                while line is not None:
                    apply_line(run, line)
                    line = lines.get_nowait()
                eof = True
            except queue.Empty:
                pass
            if eof or time.time() - last_render >= UI_REFRESH_SECONDS:
                render_progress(run, overall_ph, table_ph, log_ph)
                last_render = time.time()

        return_code = process.wait()
        if run["summary"] is not None:
            run["status"] = "finished"
            try:
                with open(output_path) as f:
                    run["events"] = json.load(f)
            except (OSError, json.JSONDecodeError) as error:
                run["log"].append(f"Could not read results file: {error}")
        elif run["status"] == "running":
            run["status"] = "failed"
            run["phase"] = f"Runner exited unexpectedly (exit code {return_code}) — see the log"
    finally:
        kill_runner(process)
        finalize_stopped(run)
        st.session_state.pending_run = None
        try:
            os.remove(output_path)
        except OSError:
            pass

    st.rerun()


def request_stop():
    """Stop button callback. Streamlit starts the next script run before the
    interrupted one has finished unwinding, so the interrupted run's finally
    block can't be relied on to update the page in time — mark the run
    stopped and kill the runner here, before the page re-renders."""
    st.session_state.pending_run = None
    run = st.session_state.get("last_run")
    if run and run["status"] == "running":
        finalize_stopped(run)
        if run.get("pid"):
            try:
                os.killpg(run["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass


def request_run(config):
    st.session_state.pending_run = config


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

init_refetch_log()
st.session_state.setdefault("pending_run", None)
st.session_state.setdefault("last_run", None)
running = st.session_state.pending_run is not None

st.title("🎫 GetEvents — fetch events")
st.caption(
    "Runs the same pipeline as `python core/dispatcher.py`: raw → normalized → "
    "deduplicated → canonical database, with the refetch log respected."
)

cities = load_cities()
today = date.today()

col_city, col_date = st.columns(2)
with col_city:
    city = st.selectbox("1 · City", list(cities), disabled=running)
    country = cities[city]
    st.caption(f"Country: **{country}**")
with col_date:
    end_date = st.date_input(
        "2 · Fetch events from today until",
        value=today + timedelta(days=DEFAULT_WINDOW_DAYS - 1),
        min_value=today,
        format="YYYY-MM-DD",
        disabled=running,
    )
    days_ahead = (end_date - today).days + 1
    st.caption(f"Window: **{today.isoformat()} → {end_date.isoformat()}** ({days_ahead} day(s))")

sources = get_sources_for_country(country)
st.markdown(f"**3 · Sources covering {country}** ({len(sources)})")

force = st.checkbox(
    "Re-fetch even if already fetched recently (ignore the refetch log)",
    value=False,
    disabled=running,
)


def set_all_sources(value, keys):
    for key in keys:
        st.session_state[key] = value


source_keys = {s["id"]: f"source::{city}::{s['id']}" for s in sources}
for key in source_keys.values():
    st.session_state.setdefault(key, True)

if not sources:
    st.info(f"No registered source covers {country} yet — see core/registry.yaml.")
else:
    with st.container(horizontal=True):
        st.button("Select all", on_click=set_all_sources, args=(True, list(source_keys.values())), disabled=running)
        st.button("Select none", on_click=set_all_sources, args=(False, list(source_keys.values())), disabled=running)

selected_ids = []
for source in sources:
    info = connector_info(source["entry_point"])
    reason = None if force or info.get("error") else skip_reason(source["id"], city, info, days_ahead)
    checked = st.checkbox(
        source["id"],
        key=source_keys[source["id"]],
        disabled=running,
        help=source.get("notes"),
    )
    caption = describe_source(source, info)
    if reason:
        caption += f" · ⏭️ will be skipped: {reason}"
    st.caption(caption)
    if checked:
        selected_ids.append(source["id"])

st.button(
    f"▶ Start fetching ({len(selected_ids)} source{'s' if len(selected_ids) != 1 else ''})",
    type="primary",
    disabled=running or not selected_ids,
    on_click=request_run,
    args=(
        {
            "city": city,
            "country": country,
            "end_date": end_date,
            "days_ahead": days_ahead,
            "source_ids": selected_ids,
            "force": force,
        },
    ),
)

st.divider()

if running:
    config = st.session_state.pending_run
    run = new_run(**config)
    st.session_state.last_run = run
    execute_run(run)
elif st.session_state.last_run:
    run = st.session_state.last_run
    st.subheader(f"Last run: {run['city']} · today → {run['end_date']}")
    render_progress(run, st.empty(), st.empty(), st.expander("Log", expanded=False).empty())
    render_results(run)
