#!/usr/bin/env python3
"""
espn_scrape.py - generic ESPN scoreboard / scores / leaderboard scraper.

URL pattern (fully configurable):
    {base}/{sport}/{section}/_/date/{date}
    e.g. https://www.espn.in/football/scoreboard/_/date/20260724
         https://www.espn.in/rugby/scoreboard/_/date/20260731

Loads each page in a real browser, scrolls to the bottom until no new events
appear (ESPN lazy-loads), then parses every Scoreboard__Row.

Install:
    pip install beautifulsoup4 lxml playwright requests
    playwright install chromium

Examples:
    python espn_scrape.py --sport football                  # today + next 6 days
    python espn_scrape.py --sport rugby --days 14 -o rugby.csv
    python espn_scrape.py --sport cricket --section scores --days 3
    python espn_scrape.py --sport football --dates 20260724 20260801   # specific days
    python espn_scrape.py --sport football --from 20260801 --to 20260807
    python espn_scrape.py --parse-file saved.html --sport football --dates 20260724

The browser window is visible by default; pass --headless to hide it.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency. Run: pip install beautifulsoup4 lxml")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_TEMPLATE = "{base}/{sport}/{section}/_/date/{date}"

# Text that looks like a kickoff time, e.g. "6:30 PM", "18:30"
TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*(?:AM|PM)?$", re.I)

COLUMNS = [
    "sport",
    "section",
    "date",
    "time",
    "status",
    "match",
    "team_a",
    "team_b",
    "score_a",
    "score_b",
    "home_team",
    "away_team",
    "record_a",
    "record_b",
    "venue",
    "location",
    "league",
    "event_id",
    "source_url",
    "scraped_at",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def txt(node) -> str:
    """Collapsed text of a BeautifulSoup node, or '' if node is None."""
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def daterange(start: str, end: str, step: int = 1):
    """Yield YYYYMMDD strings from start to end inclusive."""
    d0 = datetime.strptime(start, "%Y%m%d").date()
    d1 = datetime.strptime(end, "%Y%m%d").date()
    if d1 < d0:
        d0, d1 = d1, d0
    cur = d0
    while cur <= d1:
        yield cur.strftime("%Y%m%d")
        cur += timedelta(days=step)


def iso_date(yyyymmdd: str) -> str:
    try:
        return datetime.strptime(yyyymmdd, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return yyyymmdd


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _event_id(row) -> str:
    """Best-effort event id: numeric ancestor id, or gameId/eventId in a link."""
    node = row
    for _ in range(6):
        node = node.parent
        if node is None or not getattr(node, "get", None):
            break
        val = node.get("id") or ""
        if val and val.isdigit():
            return val
    for a in row.select("a[href]"):
        m = re.search(r"/(?:gameId|eventId|match)/(?:_/id/)?(\d+)", a["href"])
        if m:
            return m.group(1)
    return ""


def _parse_row(row, league: str, ctx: dict) -> dict | None:
    col1 = row.select_one(".Scoreboard__Column--1") or row
    col2 = row.select_one(".Scoreboard__Column--2")

    # --- column 1: time/status + competitors ------------------------------- #
    raw_time = txt(col1.select_one(".ScoreCell__Time"))
    if TIME_RE.match(raw_time):
        time_val, status_val = raw_time, "Scheduled"
    else:
        time_val, status_val = "", raw_time

    items = col1.select("li.ScoreboardScoreCell__Item") or col1.select(
        "li.ScoreCell__Item, .ScoreboardScoreCell__Item"
    )

    teams: list[dict] = []
    for li in items:
        classes = " ".join(li.get("class", []))
        name = txt(
            li.select_one(".ScoreCell__TeamName")
            or li.select_one(".ScoreCell__Truncate")
            or li.select_one(".ScoreCell__Team")
        )
        if not name:
            continue
        score = txt(
            li.select_one(".ScoreCell__Score")
            or li.select_one(".ScoreboardScoreCell__Value")
        )
        teams.append(
            {
                "name": name,
                "score": score,
                "record": txt(li.select_one(".ScoreboardScoreCell__Record")),
                "side": (
                    "home"
                    if "--home" in classes
                    else "away" if "--away" in classes else ""
                ),
            }
        )

    if not teams:
        return None

    a = teams[0]
    b = (
        teams[1]
        if len(teams) > 1
        else {"name": "", "score": "", "record": "", "side": ""}
    )

    home = next((t["name"] for t in teams if t["side"] == "home"), "")
    away = next((t["name"] for t in teams if t["side"] == "away"), "")

    # --- column 2: venue / location ---------------------------------------- #
    venue, location = "", ""
    if col2:
        details = [txt(d) for d in col2.select(".LocationDetail__Item")]
        details = [d for d in details if d]
        headline = txt(col2.select_one(".LocationDetail__Item--headline"))
        if headline:
            venue = headline
            rest = [d for d in details if d != headline]
        elif details:
            venue, rest = details[0], details[1:]
        else:
            rest = []
        location = ", ".join(rest)

    match = f"{a['name']} vs {b['name']}" if b["name"] else a["name"]

    return {
        "sport": ctx["sport"],
        "section": ctx["section"],
        "date": ctx["date"],
        "time": time_val,
        "status": status_val,
        "match": match,
        "team_a": a["name"],
        "team_b": b["name"],
        "score_a": a["score"],
        "score_b": b["score"],
        "home_team": home,
        "away_team": away,
        "record_a": a["record"],
        "record_b": b["record"],
        "venue": venue,
        "location": location,
        "league": league,
        "event_id": _event_id(row),
        "source_url": ctx["url"],
        "scraped_at": ctx["scraped_at"],
    }


def parse_html(html: str, ctx: dict) -> list[dict]:
    """Walk the document in order so each row inherits its league heading."""
    soup = BeautifulSoup(html, "lxml")

    main_rows = soup.select("div.Scoreboard__Row__Main")
    row_sel = "div.Scoreboard__Row__Main" if main_rows else "div.Scoreboard__Row"
    header_sel = "h3, .Card__Header__Title, .Table__Title, .ScoreboardHeader__Title"

    league = ""
    rows: list[dict] = []
    for node in soup.select(f"{header_sel}, {row_sel}"):
        classes = " ".join(node.get("class", []))
        if "Scoreboard__Row" in classes:
            rec = _parse_row(node, league, ctx)
            if rec:
                rows.append(rec)
        else:
            t = txt(node)
            if t:
                league = t
    return rows


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #
def dismiss_banners(page) -> None:
    for sel in (
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept')",
        "button:has-text('I Accept')",
        "[aria-label='Close']",
    ):
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=800):
                el.click(timeout=1500)
                page.wait_for_timeout(300)
        except Exception:
            pass


def autoscroll(page, max_scrolls: int, pause: float, stable_needed: int = 3) -> None:
    """Scroll to the bottom until row count and page height stop changing."""
    stable = 0
    for _ in range(max_scrolls):
        before_rows = page.locator("div.Scoreboard__Row").count()
        before_h = page.evaluate("document.body.scrollHeight")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(int(pause * 1000))
        after_rows = page.locator("div.Scoreboard__Row").count()
        after_h = page.evaluate("document.body.scrollHeight")
        if after_rows == before_rows and after_h == before_h:
            stable += 1
            if stable >= stable_needed:
                break
        else:
            stable = 0
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)


def iter_pages_playwright(urls, args):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(
            user_agent=UA,
            locale="en-GB",
            viewport={"width": 1440, "height": 900},
            timezone_id=args.timezone or None,
        )
        page = context.new_page()
        try:
            for i, url in enumerate(urls):
                print(f"[{i+1}/{len(urls)}] {url}", file=sys.stderr)
                page.goto(url, wait_until="domcontentloaded", timeout=args.timeout)
                dismiss_banners(page)
                try:
                    page.wait_for_selector("div.Scoreboard__Row", timeout=args.timeout)
                except Exception:
                    print("    no events found on this page", file=sys.stderr)
                autoscroll(page, args.max_scrolls, args.scroll_pause)
                html = page.content()
                if args.dump_html:
                    Path(args.dump_html).mkdir(parents=True, exist_ok=True)
                    name = re.sub(r"\W+", "_", url.split("//", 1)[-1]) + ".html"
                    (Path(args.dump_html) / name).write_text(html, encoding="utf-8")
                yield url, html
                if i < len(urls) - 1:
                    time.sleep(args.delay)
        finally:
            context.close()
            browser.close()


def iter_pages_requests(urls, args):
    import requests

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    for i, url in enumerate(urls):
        print(f"[{i+1}/{len(urls)}] {url}", file=sys.stderr)
        r = sess.get(url, timeout=args.timeout / 1000)
        r.raise_for_status()
        yield url, r.text
        if i < len(urls) - 1:
            time.sleep(args.delay)


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def write_rows(rows: list[dict], out: str) -> None:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()

    if suffix == ".json":
        path.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    elif suffix in (".xlsx", ".xls"):
        import pandas as pd

        pd.DataFrame(rows, columns=COLUMNS).to_excel(path, index=False)
    else:
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    print(f"Wrote {len(rows)} rows -> {path}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def build_args(argv=None):
    p = argparse.ArgumentParser(description="Scrape ESPN scoreboards.")
    p.add_argument(
        "--sport",
        default="football",
        help="football, rugby, cricket, nba, ... (default: football)",
    )
    p.add_argument(
        "--section",
        default="scoreboard",
        help="scoreboard | scores | leaderboard (default: scoreboard)",
    )
    p.add_argument("--base", default="https://www.espn.in", help="site root")
    p.add_argument(
        "--url-template",
        default=DEFAULT_TEMPLATE,
        help="placeholders: {base} {sport} {section} {date}",
    )

    p.add_argument(
        "--dates",
        nargs="+",
        metavar="YYYYMMDD",
        help="explicit dates; overrides the default forward window",
    )
    p.add_argument(
        "--from",
        dest="date_from",
        metavar="YYYYMMDD",
        help="start date (default: today)",
    )
    p.add_argument("--to", dest="date_to", metavar="YYYYMMDD")
    p.add_argument(
        "--days",
        type=int,
        default=7,
        help="how many days to cover, counting the start date "
        "(default: 7 = today + next 6 days)",
    )
    p.add_argument("--step", type=int, default=1, help="day step (default: 1)")

    p.add_argument(
        "-o",
        "--out",
        default="espn_events.csv",
        help=".csv, .json or .xlsx (default: espn_events.csv)",
    )
    p.add_argument("--engine", choices=["playwright", "requests"], default="playwright")
    p.add_argument(
        "--headless",
        action="store_true",
        help="hide the browser window (default: visible)",
    )
    p.add_argument("--timeout", type=int, default=30000, help="ms (default: 30000)")
    p.add_argument("--delay", type=float, default=3.0, help="seconds between pages")
    p.add_argument("--max-scrolls", type=int, default=40)
    p.add_argument("--scroll-pause", type=float, default=0.7)
    p.add_argument("--timezone", default="", help="e.g. Asia/Kolkata, Europe/London")
    p.add_argument("--dump-html", metavar="DIR", help="save raw HTML for debugging")
    p.add_argument(
        "--parse-file",
        metavar="FILE",
        help="parse a saved HTML file instead of fetching",
    )
    return p.parse_args(argv)


def resolve_dates(args) -> list[str]:
    """Default window: today and the next --days-1 days going forward."""
    dates: list[str] = []

    if args.dates:
        dates.extend(args.dates)
    else:
        start = args.date_from or datetime.now().strftime("%Y%m%d")
        if args.date_to:
            end = args.date_to
        else:
            span = max(args.days, 1) - 1
            end = (
                datetime.strptime(start, "%Y%m%d")
                + timedelta(days=span * max(args.step, 1))
            ).strftime("%Y%m%d")
        dates.extend(daterange(start, end, max(args.step, 1)))

    seen, ordered = set(), []
    for d in dates:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered


def main(argv=None) -> int:
    args = build_args(argv)
    dates = resolve_dates(args)
    scraped_at = datetime.now().isoformat(timespec="seconds")

    urls = [
        args.url_template.format(
            base=args.base.rstrip("/"),
            sport=args.sport,
            section=args.section,
            date=d,
        )
        for d in dates
    ]
    url_to_date = dict(zip(urls, dates))

    if args.parse_file:
        html = Path(args.parse_file).read_text(encoding="utf-8", errors="replace")
        pages = [(urls[0], html)]
    elif args.engine == "requests":
        pages = iter_pages_requests(urls, args)
    else:
        pages = iter_pages_playwright(urls, args)

    all_rows: list[dict] = []
    for url, html in pages:
        ctx = {
            "sport": args.sport,
            "section": args.section,
            "date": iso_date(url_to_date.get(url, "")),
            "url": url,
            "scraped_at": scraped_at,
        }
        rows = parse_html(html, ctx)
        print(f"    parsed {len(rows)} events", file=sys.stderr)
        all_rows.extend(rows)

    # de-duplicate
    seen, unique = set(), []
    for r in all_rows:
        key = (r["date"], r["sport"], r["event_id"] or f"{r['match']}|{r['time']}")
        if key not in seen:
            seen.add(key)
            unique.append(r)

    write_rows(unique, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
