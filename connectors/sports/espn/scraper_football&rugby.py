#!/usr/bin/env python3
"""
espn_scrape.py - config-driven ESPN scoreboard scraper.

Everything about WHAT to scrape lives in the SOURCES list below.
The only things you pass at runtime are WHEN and HOW:

    python espn_scrape.py --days 7
    python espn_scrape.py --from 20260801 --days 14
    python espn_scrape.py --days 3 --only football
    python espn_scrape.py --days 7 --headless -o out/events.csv

Every enabled source is scraped for every date in the window, so with
football + rugby enabled and --days 3 that is 6 page loads.

Install:
    pip install beautifulsoup4 playwright requests
    pip install lxml          # optional, faster parsing; falls back to html.parser
    playwright install chromium
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency. Run: pip install beautifulsoup4")


# =========================================================================== #
#  CONFIG - add or comment out sources here
# =========================================================================== #
#
#   name          required   sport path segment in the URL, also the value
#                            written to the `sport` column
#   section       required   scoreboard | scores | fixtures | leaderboard ...
#   date_format   required   strftime pattern for the date in the URL.
#                            ESPN mostly uses %Y%m%d, but some sections differ,
#                            so each source carries its own.
#
#   enabled       optional   default True; set False to skip without deleting
#   base          optional   default DEFAULT_BASE
#   url_template  optional   full override for odd URL shapes.
#                            Placeholders: {base} {sport} {section} {date}
#
DEFAULT_BASE = "https://www.espn.in"
DEFAULT_TEMPLATE = "{base}/{sport}/{section}/_/date/{date}"

SOURCES: list[dict] = [
    {
        "name": "football",
        "section": "scoreboard",
        "date_format": "%Y%m%d",
    },
    {
        "name": "rugby",
        "section": "scoreboard",
        "date_format": "%Y%m%d",
        "enabled": True,
    },
    # --- examples: flip "enabled" to True once you have checked the URL ---
    {
        "name": "cricket",
        "section": "scores",
        "date_format": "%Y%m%d",
        "enabled": False,
    },
    {
        "name": "golf",
        "section": "leaderboard",
        "date_format": "%Y-%m-%d",  # different date shape, handled per source
        "enabled": False,
    },
    {
        "name": "nba",
        "section": "scoreboard",
        "date_format": "%Y%m%d",
        "base": "https://www.espn.com",  # per-source site root
        "enabled": False,
    },
]
# =========================================================================== #


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

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
    "full_location",
    "league",
    "event_id",
    "source_url",
    "scraped_at",
]


def _pick_parser() -> str:
    """Prefer lxml, but fall back to stdlib so lxml stays optional."""
    for feature in ("lxml", "html5lib", "html.parser"):
        try:
            BeautifulSoup("<b>ok</b>", feature)
            return feature
        except Exception:
            continue
    return "html.parser"


PARSER = _pick_parser()


# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #
@dataclass
class Target:
    sport: str
    section: str
    date_iso: str
    url: str


def active_sources(only: list[str] | None) -> list[dict]:
    wanted = {s.lower() for s in only} if only else None
    out = []
    for src in SOURCES:
        name = src["name"]
        if wanted is not None:
            if name.lower() not in wanted:
                continue
        elif not src.get("enabled", True):
            continue
        out.append(src)
    if not out:
        known = ", ".join(s["name"] for s in SOURCES)
        sys.exit(f"No sources selected. Available: {known}")
    return out


def date_window(start: str | None, days: int) -> list[Date]:
    """Start date (default today) plus the next days-1 days, going forward."""
    first = (
        datetime.strptime(start, "%Y%m%d").date() if start else datetime.now().date()
    )
    return [first + timedelta(days=i) for i in range(max(days, 1))]


def build_targets(sources: list[dict], dates: list[Date]) -> list[Target]:
    targets: list[Target] = []
    for src in sources:
        sport = src.get("sport", src["name"])
        section = src["section"]
        template = src.get("url_template", DEFAULT_TEMPLATE)
        base = src.get("base", DEFAULT_BASE).rstrip("/")
        fmt = src.get("date_format", "%Y%m%d")
        for d in dates:
            targets.append(
                Target(
                    sport=src["name"],
                    section=section,
                    date_iso=d.isoformat(),
                    url=template.format(
                        base=base, sport=sport, section=section, date=d.strftime(fmt)
                    ),
                )
            )
    return targets


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def txt(node) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _event_id(row) -> str:
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
        teams.append(
            {
                "name": name,
                "score": txt(
                    li.select_one(".ScoreCell__Score")
                    or li.select_one(".ScoreboardScoreCell__Value")
                ),
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

    venue, location = "", ""
    if col2:
        details = [
            d for d in (txt(x) for x in col2.select(".LocationDetail__Item")) if d
        ]
        headline = txt(col2.select_one(".LocationDetail__Item--headline"))
        if headline:
            venue, rest = headline, [d for d in details if d != headline]
        elif details:
            venue, rest = details[0], details[1:]
        else:
            rest = []
        location = ", ".join(rest)

    parts: list[str] = []
    for part in (venue, location):
        if part and part.lower() not in (p.lower() for p in parts):
            parts.append(part)
    full_location = ", ".join(parts)

    return {
        "sport": ctx["sport"],
        "section": ctx["section"],
        "date": ctx["date"],
        "time": time_val,
        "status": status_val,
        "match": f"{a['name']} vs {b['name']}" if b["name"] else a["name"],
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
        "full_location": full_location,
        "league": league,
        "event_id": _event_id(row),
        "source_url": ctx["url"],
        "scraped_at": ctx["scraped_at"],
    }


def parse_html(html: str, ctx: dict) -> list[dict]:
    soup = BeautifulSoup(html, PARSER)

    row_sel = (
        "div.Scoreboard__Row__Main"
        if soup.select("div.Scoreboard__Row__Main")
        else "div.Scoreboard__Row"
    )
    header_sel = "h3, .Card__Header__Title, .Table__Title, .ScoreboardHeader__Title"

    league = ""
    rows: list[dict] = []
    for node in soup.select(f"{header_sel}, {row_sel}"):
        if "Scoreboard__Row" in " ".join(node.get("class", [])):
            rec = _parse_row(node, league, ctx)
            if rec:
                rows.append(rec)
        else:
            league = txt(node) or league
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
    stable = 0
    for _ in range(max_scrolls):
        before_rows = page.locator("div.Scoreboard__Row").count()
        before_h = page.evaluate("document.body.scrollHeight")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(int(pause * 1000))
        if (
            page.locator("div.Scoreboard__Row").count() == before_rows
            and page.evaluate("document.body.scrollHeight") == before_h
        ):
            stable += 1
            if stable >= stable_needed:
                break
        else:
            stable = 0
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)


def iter_pages_playwright(targets: list[Target], args):
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
            for i, t in enumerate(targets):
                print(
                    f"[{i+1}/{len(targets)}] {t.sport} {t.date_iso}  {t.url}",
                    file=sys.stderr,
                )
                page.goto(t.url, wait_until="domcontentloaded", timeout=args.timeout)
                dismiss_banners(page)
                try:
                    page.wait_for_selector("div.Scoreboard__Row", timeout=args.timeout)
                except Exception:
                    print("    no events found on this page", file=sys.stderr)
                autoscroll(page, args.max_scrolls, args.scroll_pause)
                html = page.content()
                if args.dump_html:
                    d = Path(args.dump_html)
                    d.mkdir(parents=True, exist_ok=True)
                    (d / f"{t.sport}_{t.date_iso}.html").write_text(
                        html, encoding="utf-8"
                    )
                yield t, html
                if i < len(targets) - 1:
                    time.sleep(args.delay)
        finally:
            context.close()
            browser.close()


def iter_pages_requests(targets: list[Target], args):
    import requests

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    for i, t in enumerate(targets):
        print(
            f"[{i+1}/{len(targets)}] {t.sport} {t.date_iso}  {t.url}", file=sys.stderr
        )
        r = sess.get(t.url, timeout=args.timeout / 1000)
        r.raise_for_status()
        yield t, r.text
        if i < len(targets) - 1:
            time.sleep(args.delay)


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def write_rows(rows: list[dict], out: str) -> None:
    path = Path(out)
    if path.parent != Path(""):
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
    print(f"\nWrote {len(rows)} rows -> {path}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def build_args(argv=None):
    p = argparse.ArgumentParser(
        description="Scrape ESPN scoreboards for every source in SOURCES.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Sources are configured in the SOURCES list at the top of this file.",
    )
    p.add_argument(
        "--days",
        type=int,
        default=7,
        help="days to cover, counting the start date (default: 7)",
    )
    p.add_argument(
        "--from",
        dest="date_from",
        metavar="YYYYMMDD",
        help="start date (default: today)",
    )
    p.add_argument(
        "--only",
        nargs="+",
        metavar="NAME",
        help="run just these sources, e.g. --only football",
    )

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
    p.add_argument("--timezone", default="", help="e.g. Asia/Kolkata")
    p.add_argument("--dump-html", metavar="DIR", help="save raw HTML for debugging")
    p.add_argument(
        "--parse-file",
        metavar="FILE",
        help="parse a saved HTML file instead of fetching",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="print the URLs that would be scraped, then exit",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = build_args(argv)

    sources = active_sources(args.only)
    dates = date_window(args.date_from, args.days)
    targets = build_targets(sources, dates)
    scraped_at = datetime.now().isoformat(timespec="seconds")

    print(
        f"{len(sources)} source(s): {', '.join(s['name'] for s in sources)}\n"
        f"{len(dates)} date(s): {dates[0]} .. {dates[-1]}\n"
        f"{len(targets)} page(s) to load\n",
        file=sys.stderr,
    )

    if args.list:
        for t in targets:
            print(t.url)
        return 0

    if args.parse_file:
        html = Path(args.parse_file).read_text(encoding="utf-8", errors="replace")
        pages = [(targets[0], html)]
    elif args.engine == "requests":
        pages = iter_pages_requests(targets, args)
    else:
        pages = iter_pages_playwright(targets, args)

    all_rows: list[dict] = []
    for t, html in pages:
        rows = parse_html(
            html,
            {
                "sport": t.sport,
                "section": t.section,
                "date": t.date_iso,
                "url": t.url,
                "scraped_at": scraped_at,
            },
        )
        print(f"    parsed {len(rows)} events", file=sys.stderr)
        all_rows.extend(rows)

    seen, unique = set(), []
    for r in all_rows:
        key = (r["date"], r["sport"], r["event_id"] or f"{r['match']}|{r['time']}")
        if key not in seen:
            seen.add(key)
            unique.append(r)

    write_rows(unique, args.out)

    by_sport: dict[str, int] = {}
    for r in unique:
        by_sport[r["sport"]] = by_sport.get(r["sport"], 0) + 1
    for name, n in sorted(by_sport.items()):
        print(f"  {name:12} {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
