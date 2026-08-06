#!/usr/bin/env python3
"""
StubHub "explore" scraper
=========================

Loads a StubHub /explore page, repeatedly clicks "Show More" until every event
is loaded, then extracts all available data for each event into JSON + CSV.

WHY A LOCAL SCRIPT: StubHub's explore list is rendered with JavaScript and the
"Show More" button is a client-side control, so it needs a real browser to drive.

--------------------------------------------------------------------------------
SETUP (run once in your own environment):

    pip install playwright beautifulsoup4
    playwright install chromium

RUN:

    python stubhub_scraper.py

Edit the CONFIG block below to change the city, date range, or output paths.
You give plain decimal lat/lon and calendar dates — the script handles the
base64 encoding and epoch-millisecond conversion that StubHub's URL expects.
--------------------------------------------------------------------------------

A note on courtesy: this opens a visible browser and pauses between clicks on
purpose. Please check StubHub's robots.txt / Terms of Service, keep the volume
low, and only collect the public listing data you actually need.
"""

import base64
import csv
import json
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None

# ============================== CONFIG ========================================
# Location — plain decimal degrees. The script base64-encodes these for you.
LAT = 48.856614  # Paris city center
LON = 2.352222
# LAT, LON = 47.497912, 19.040235   # <- this is what YOUR url had (Budapest)

TIMEZONE = "Europe/Paris"  # used to compute start/end of day correctly

# Date range (inclusive). Use "today" for the start, or "YYYY-MM-DD" for either.
START_DATE = "today"
END_DATE = "2026-08-19"

# Browser / crawl behaviour
HEADLESS = False  # False lets you watch it work; True runs hidden
MAX_SHOW_MORE = 300  # safety cap on "Show More" clicks
CLICK_PAUSE_SEC = 1.5  # pause after each scroll/click (be polite)
PAGE_TIMEOUT_MS = 60_000

# Output
OUT_JSON = "stubhub_events.json"
OUT_CSV = "stubhub_events.csv"
SAVE_RAW_HTML = "stubhub_page.html"  # set to None to skip
# ==============================================================================

BASE = "https://www.stubhub.com"


# ------------------------------- URL building --------------------------------
def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _resolve_date(spec: str, tz):
    if spec.strip().lower() == "today":
        now = datetime.now(tz) if tz else datetime.now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    d = datetime.strptime(spec, "%Y-%m-%d")
    return d.replace(tzinfo=tz) if tz else d


def build_url() -> str:
    tz = ZoneInfo(TIMEZONE) if ZoneInfo else None
    start = _resolve_date(START_DATE, tz)
    end = _resolve_date(END_DATE, tz).replace(
        hour=23, minute=59, second=59, microsecond=999000
    )
    params = {
        "from": _epoch_ms(start),
        "to": _epoch_ms(end),
        "lat": _b64(f"{LAT}"),
        "lon": _b64(f"{LON}"),
    }
    url = f"{BASE}/explore?{urlencode(params)}"
    print(f"[url] {url}")
    print(f"[range] {start:%Y-%m-%d} -> {end:%Y-%m-%d}  @ ({LAT}, {LON})")
    return url


# ------------------------------- extraction ----------------------------------
_DATE_HINT = re.compile(
    r"(mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|•|\b\d{1,2}:\d{2}\b|am|pm",
    re.IGNORECASE,
)
_PRICE_HINT = re.compile(r"[€$£]\s?\d")


def _looks_like_date(text: str) -> bool:
    return bool(_DATE_HINT.search(text))


def parse_events(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    events: dict[str, dict] = {}

    for a in soup.select('a[href*="/event/"]'):
        href = a.get("href", "")
        m = re.search(r"/event/(\d+)", href)
        if not m:
            continue
        event_id = m.group(1)

        # climb to the enclosing list item / card
        card = a.find_parent("li") or a.find_parent("div") or a.parent

        # name: prefer the heading, fall back to the link's aria-label
        h3 = card.find("h3") if card else None
        name = (h3.get_text(strip=True) if h3 else None) or a.get("aria-label")

        # candidate text lines, minus the follow-button UI ("Follow"/"Followed"/count)
        raw = [p.get_text(strip=True) for p in (card.find_all("p") if card else [])]
        cand = [
            t
            for t in raw
            if t and t.lower() not in ("follow", "followed") and not t.isdigit()
        ]

        date_text = next((t for t in cand if _looks_like_date(t)), None)
        venue = next((t for t in cand if t != date_text), None)
        # fallback if the heuristic misfired but we still have two lines
        if date_text is None and len(cand) >= 1:
            date_text = cand[0]
            venue = venue or (cand[1] if len(cand) > 1 else None)

        price = next((t for t in cand if _PRICE_HINT.search(t)), None)

        img = card.find("img") if card else None
        img_url = img.get("src") if img else None

        follow_p = None
        if card:
            for p in card.find_all("p"):
                if p.get_text(strip=True).isdigit():
                    follow_p = p.get_text(strip=True)
                    break

        events[event_id] = {
            "event_id": event_id,
            "name": name,
            "date_text": date_text,
            "venue": venue,
            "price_from": price,
            "follow_count": follow_p,
            "image_url": img_url,
            "url": urljoin(BASE, href),
        }

    return list(events.values())


# ------------------------------- crawling ------------------------------------
def _dismiss_cookies(page):
    for sel in (
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
        "button:has-text('Got it')",
    ):
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500):
                loc.click(timeout=2000)
                print("[cookies] dismissed banner")
                return
        except Exception:
            continue


def _count_events(page) -> int:
    try:
        return page.locator('a[href*="/event/"]').count()
    except Exception:
        return 0


def load_all_events(page) -> int:
    _dismiss_cookies(page)
    clicks = 0
    stalls = 0
    last_count = -1

    while clicks < MAX_SHOW_MORE:
        page.mouse.wheel(0, 25_000)
        page.wait_for_timeout(int(CLICK_PAUSE_SEC * 1000))

        count = _count_events(page)
        print(f"[progress] events loaded: {count} (show-more clicks: {clicks})")

        # locate a visible "Show More" button by text (class names are unstable)
        btn = page.get_by_role("button", name=re.compile("show more", re.IGNORECASE))
        clicked = False
        try:
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.scroll_into_view_if_needed()
                btn.first.click()
                clicks += 1
                clicked = True
                page.wait_for_timeout(int(CLICK_PAUSE_SEC * 1000))
        except (PWTimeout, Exception):
            clicked = False

        if not clicked:
            # no button this round — confirm we've actually stopped growing
            if count == last_count:
                stalls += 1
                if stalls >= 2:
                    break
            else:
                stalls = 0
        last_count = count

    print(
        f"[done] finished after {clicks} 'Show More' click(s), {_count_events(page)} events on page"
    )
    return clicks


# --------------------------------- output ------------------------------------
def save(events: list[dict]):
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    print(f"[saved] {OUT_JSON} ({len(events)} events)")

    if events:
        fields = [
            "event_id",
            "name",
            "date_text",
            "venue",
            "price_from",
            "follow_count",
            "image_url",
            "url",
        ]
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for e in events:
                w.writerow({k: e.get(k) for k in fields})
        print(f"[saved] {OUT_CSV}")


# ---------------------------------- main -------------------------------------
def main():
    url = build_url()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        print("[nav] loading page...")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        load_all_events(page)

        html = page.content()
        if SAVE_RAW_HTML:
            with open(SAVE_RAW_HTML, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"[saved] {SAVE_RAW_HTML} (raw HTML for debugging)")

        events = parse_events(html)
        browser.close()

    if not events:
        print(
            "[warn] no events parsed. StubHub may have changed its markup, or a "
            "bot check/cookie wall blocked the list. Inspect the saved HTML."
        )
    save(events)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[abort] interrupted by user")
        sys.exit(1)
