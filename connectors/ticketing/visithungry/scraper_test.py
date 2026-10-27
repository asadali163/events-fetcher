#!/usr/bin/env python3
"""
VisitHungary events scraper
===========================

Scrapes events from https://visithungary.com/collection/events for a date range
that starts *today* and ends a configurable number of days ahead.

Why Playwright and not plain requests?
--------------------------------------
visithungary.com is an Angular single-page app. The event cards are rendered by
JavaScript *after* the initial HTML loads, so a plain `requests.get()` returns an
empty shell with no events in it. Playwright loads the page in a real (headless)
browser, waits for the events to render, clicks "Load more" until everything is
loaded, and then we parse the resulting HTML with BeautifulSoup.

For each event it extracts:
    - date        (e.g. "10-14 September, 2026")
    - location    (e.g. "Pécs region")
    - title       (e.g. "20th Pécs International Dance Festival")
    - tag         (e.g. "Event")
    - url         (full link to the event page)
    - slug        (the last part of the url, handy as a stable id)
    - image       (full image url)

Output: writes both a CSV and a JSON file.

Install
-------
    pip install playwright beautifulsoup4
    playwright install chromium

Run
---
    python visithungary_events_scraper.py                 # today -> today + 16 days
    python visithungary_events_scraper.py --days 30       # today -> today + 30 days
    python visithungary_events_scraper.py --headful       # watch the browser work
    python visithungary_events_scraper.py --start 2026-09-13 --end 2026-09-29
"""

import argparse
import csv
import json
import re
import sys
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit(
        "Playwright is not installed.\n"
        "  pip install playwright beautifulsoup4\n"
        "  playwright install chromium"
    )

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_URL = "https://visithungary.com"
DEFAULT_DAYS_AHEAD = 16  # end date = today + this many days
PAGE_TIMEOUT_MS = 30_000  # per-navigation timeout
LOAD_MORE_PAUSE_MS = 1_500  # wait after each "Load more" click
MAX_LOAD_MORE_CLICKS = 100  # safety cap so the loop can never run forever
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_BG_URL_RE = re.compile(r'url\(\s*["\']?(.*?)["\']?\s*\)')


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def build_url(start: date, end: date) -> str:
    """Build the collection URL with the date filters the site expects."""
    return (
        f"{BASE_URL}/collection/events"
        f"?eventDateGt_filter={start.isoformat()}"
        f"&eventDateLt_filter={end.isoformat()}"
    )


def extract_bg_url(style: str | None) -> str | None:
    """Pull the URL out of a CSS `background-image: url("...")` value."""
    if not style:
        return None
    m = _BG_URL_RE.search(style)
    return m.group(1) if m else None


def text_or_none(node) -> str | None:
    return node.get_text(strip=True) if node else None


def dismiss_cookie_banner(page) -> None:
    """Best-effort dismissal of a GDPR/cookie consent banner if one appears."""
    candidates = [
        "button:has-text('Accept')",
        "button:has-text('Accept all')",
        "button:has-text('I agree')",
        "button:has-text('Elfogadom')",  # Hungarian: "I accept"
        "button:has-text('Rendben')",  # Hungarian: "OK"
        "button:has-text('OK')",
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=2_000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def click_all_load_more(page) -> None:
    """Keep clicking 'Load more' until every event is on the page."""
    for _ in range(MAX_LOAD_MORE_CLICKS):
        before = page.locator("div.poser").count()

        load_more = page.get_by_text("Load more", exact=False).first
        if load_more.count() == 0 or not load_more.is_visible():
            return

        try:
            load_more.scroll_into_view_if_needed(timeout=5_000)
            load_more.click(timeout=5_000)
        except Exception:
            return

        # Wait for new cards to appear (or give up if the count stops growing).
        try:
            page.wait_for_function(
                "prev => document.querySelectorAll('div.poser').length > prev",
                arg=before,
                timeout=PAGE_TIMEOUT_MS,
            )
        except PWTimeout:
            return
        page.wait_for_timeout(LOAD_MORE_PAUSE_MS)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_events(html: str) -> list[dict]:
    """Extract every event card (.poser) from the fully-rendered HTML."""
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for card in soup.select("div.poser"):
        date_span = card.select_one(".card-header b span")
        date_text = None
        if date_span:
            date_text = date_span.get_text(strip=True) or date_span.get("title")

        location = text_or_none(card.select_one(".location-name"))
        title = text_or_none(card.select_one("span.title"))
        tag = text_or_none(card.select_one(".tag"))

        link = card.select_one("a[href]")
        href = link["href"] if link and link.has_attr("href") else None
        event_url = urljoin(BASE_URL, href) if href else None
        slug = href.rstrip("/").rsplit("/", 1)[-1] if href else None

        bg = card.select_one("span.bg")
        image = extract_bg_url(bg.get("style")) if bg else None
        image = urljoin(BASE_URL, image) if image else None

        # Skip empty/placeholder cards that have no identifying info.
        if not (title or event_url):
            continue

        events.append(
            {
                "title": title,
                "date": date_text,
                "location": location,
                "tag": tag,
                "url": event_url,
                "slug": slug,
                "image": image,
            }
        )

    return events


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #
def scrape(url: str, headless: bool = True) -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        page = context.new_page()

        print(f"Loading: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

        dismiss_cookie_banner(page)

        # Wait for the first event card to render.
        try:
            page.wait_for_selector("div.poser", timeout=PAGE_TIMEOUT_MS)
        except PWTimeout:
            print(
                "No event cards appeared. Either there are no events in this "
                "date range, or the site's structure changed."
            )
            browser.close()
            return []

        click_all_load_more(page)
        html = page.content()
        browser.close()

    events = parse_events(html)
    return events


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def save_csv(events: list[dict], path: str) -> None:
    fields = ["title", "date", "location", "tag", "url", "slug", "image"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(events)


def save_json(events: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Scrape visithungary.com events.")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS_AHEAD,
        help=f"How many days ahead of today to include "
        f"(default: {DEFAULT_DAYS_AHEAD}).",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Override start date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="Override end date (YYYY-MM-DD). Defaults to today+days.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Show the browser window instead of running headless.",
    )
    parser.add_argument(
        "--csv", type=str, default="visithungary_events.csv", help="CSV output path."
    )
    parser.add_argument(
        "--json", type=str, default="visithungary_events.json", help="JSON output path."
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    start = date.fromisoformat(args.start) if args.start else date.today()
    end = (
        date.fromisoformat(args.end) if args.end else start + timedelta(days=args.days)
    )

    if end < start:
        sys.exit("End date is before start date.")

    url = build_url(start, end)
    print(f"Date range: {start.isoformat()} -> {end.isoformat()}")

    events = scrape(url, headless=not args.headful)

    print(f"\nFound {len(events)} event(s).")
    for e in events:
        print(f"  - {e['date']:<32} | {e['location'] or '':<24} | {e['title']}")

    save_csv(events, args.csv)
    save_json(events, args.json)
    print(f"\nSaved:\n  {args.csv}\n  {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
