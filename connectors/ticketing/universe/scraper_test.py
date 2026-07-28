#!/usr/bin/env python3
"""
Universe.com event scraper
==========================

Scrapes every event from a Universe.com "explore" search (all pages) and writes
the results to CSV + JSON. It captures: title, host/organizer, start & end date,
start & end time, location, price range, event URL and image URL.

Universe's explore page is a JavaScript app, so we render it with a headless
browser (Playwright) and then parse the rendered HTML. The parsing logic in this
file was unit-tested against real card markup.

SETUP (run once):
    pip install playwright beautifulsoup4 lxml
    playwright install chromium

RUN:
    python universe_scraper.py
    # or point it at a different search:
    python universe_scraper.py --url "https://www.universe.com/explore?query=&loc=..."
    # watch it work (non-headless) if the site challenges the bot:
    python universe_scraper.py --show
    # log the site's own API calls (useful if you later want a faster version):
    python universe_scraper.py --debug-api

Be a good citizen: this hits the site politely (delay between pages). Check the
site's Terms of Service and robots.txt, don't run it in a tight loop, and only
use public, non-personal data.
"""

import argparse
import csv
import json
import random
import re
import sys
import time

from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# The search to scrape. Replace with any Universe.com explore URL you like.
# ----------------------------------------------------------------------------
DEFAULT_URL = (
    "https://www.universe.com/explore?query=&loc=Amsterdam%252C%2520Netherlands"
    "&ll=52.3675734%252C4.9041389"
)
# DEFAULT_URL = "https://www.universe.com/explore?query=&loc=Brussels%252C%2520Belgium&ll=50.84770289999999%252C4.357200100000001"

OUT_CSV = "universe_events.csv"
OUT_JSON = "universe_events.json"

MAX_PAGES = 50  # hard safety cap
MIN_DELAY, MAX_DELAY = 2.0, 4.0  # polite pause between pages (seconds)
NAV_TIMEOUT = 45_000  # ms
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# ============================================================================
# Parsing (validated offline against real card HTML)
# ============================================================================
CURRENCY_RE = re.compile(r"[€$£¥]")
_MONTHS_ALT = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
DATE_RE = re.compile(
    rf"(?:[A-Za-z]{{3,9}},?\s+)?({_MONTHS_ALT})[a-z]*\s+(\d{{1,2}}),\s*(\d{{4}})",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\d{1,2}(?::\d{2})?\s*[APap][Mm]")
MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ],
        start=1,
    )
}


def _iso(month_name, day, year):
    mi = MONTHS.get(month_name[:3].lower())
    return f"{int(year):04d}-{mi:02d}-{int(day):02d}" if mi else ""


def parse_dates(raw):
    """-> (start_date, end_date, start_time, end_time). Handles single/double
    dates, with or without a time range."""
    raw = (raw or "").strip()
    if not raw:
        return "", "", "", ""
    date_part, time_part = raw, ""
    m = re.split(r"\s+at\s+", raw, maxsplit=1, flags=re.IGNORECASE)
    if len(m) == 2:
        date_part, time_part = m
    dates = DATE_RE.findall(date_part)
    start_date = _iso(*dates[0]) if len(dates) >= 1 else ""
    end_date = _iso(*dates[1]) if len(dates) >= 2 else ""
    times = TIME_RE.findall(time_part)
    start_time = times[0].upper().replace(" ", "") if len(times) >= 1 else ""
    end_time = times[1].upper().replace(" ", "") if len(times) >= 2 else ""
    return start_date, end_date, start_time, end_time


def parse_price(raw):
    """-> (currency, price_min, price_max)."""
    raw = (raw or "").strip()
    if not raw:
        return "", None, None
    if re.search(r"free", raw, re.IGNORECASE):
        return "", 0.0, 0.0
    cur = CURRENCY_RE.search(raw)
    currency = cur.group(0) if cur else ""
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", raw)]
    if not nums:
        return currency, None, None
    return currency, min(nums), max(nums)


def _find_card(host_anchor):
    el = host_anchor
    for _ in range(8):
        if el is None:
            break
        txt = el.get_text(" ")
        if el.select_one('a[href*="/events/"]') and (
            CURRENCY_RE.search(txt) or "free" in txt.lower()
        ):
            return el
        el = el.parent
    return host_anchor.parent


def parse_card(card):
    host_a = card.select_one('a[href*="/users/"]')
    host = host_a.get_text(strip=True) if host_a else ""

    event_anchors = card.select('a[href*="/events/"]')
    title, event_url = "", ""
    for a in event_anchors:
        t = a.get_text(strip=True)
        if (
            t
            and not CURRENCY_RE.search(t)
            and not re.fullmatch(r"free", t, re.IGNORECASE)
        ):
            title, event_url = t, a.get("href", "")
            break
    if not event_url and event_anchors:
        event_url = event_anchors[0].get("href", "")

    price_raw = ""
    for a in event_anchors:
        t = a.get_text(strip=True)
        if CURRENCY_RE.search(t) or re.search(r"free", t, re.IGNORECASE):
            price_raw = t
            break
    if not price_raw:
        pe = card.select_one('[class*="price"]')
        if pe:
            price_raw = pe.get_text(strip=True)

    date_raw = ""
    for p in card.find_all("p"):
        if DATE_RE.search(p.get_text(" ", strip=True)):
            date_raw = p.get_text(" ", strip=True)
            break
    if not date_raw:
        for s in card.stripped_strings:
            if DATE_RE.search(s):
                date_raw = s
                break

    known = {host, title, price_raw, date_raw}
    candidates = []
    for s in card.stripped_strings:
        s = s.strip()
        if not s or s in known or CURRENCY_RE.search(s) or DATE_RE.search(s):
            continue
        if re.search(r"[A-Za-z]", s):
            candidates.append(s)
    with_comma = [c for c in candidates if "," in c]
    pool = with_comma or candidates
    location = max(pool, key=len) if pool else ""

    image_url = ""
    bg = card.select_one('[style*="background-image"]')
    if bg:
        mm = re.search(r'url\((["\']?)(.*?)\1\)', bg.get("style", ""))
        if mm:
            image_url = mm.group(2)
    if not image_url:
        img = card.select_one("img")
        if img:
            image_url = img.get("src", "")

    sd, ed, st, et = parse_dates(date_raw)
    currency, pmin, pmax = parse_price(price_raw)
    return {
        "title": title,
        "host": host,
        "start_date": sd,
        "end_date": ed,
        "start_time": st,
        "end_time": et,
        "date_raw": date_raw,
        "location": location,
        "price_raw": price_raw,
        "currency": currency,
        "price_min": pmin,
        "price_max": pmax,
        "event_url": event_url,
        "image_url": image_url,
    }


def parse_events_from_html(html):
    soup = BeautifulSoup(html, "lxml")
    events, seen = [], set()
    for host_a in soup.select('a[href*="/users/"]'):
        card = _find_card(host_a)
        if card is None:
            continue
        row = parse_card(card)
        key = row.get("event_url") or (row.get("title"), row.get("date_raw"))
        if not row.get("title") or key in seen:
            continue
        seen.add(key)
        events.append(row)
    return events


def get_total_pages(html):
    """Read the 'X of Y' pagination label; default to 1 if absent."""
    soup = BeautifulSoup(html, "lxml")
    label = soup.select_one(".pagination-component__label")
    if label:
        m = re.search(r"of\s+(\d+)", label.get_text())
        if m:
            return int(m.group(1))
    # Fallback: highest numbered page link
    nums = [
        int(a.get_text())
        for a in soup.select(".pagination-component__page--link")
        if a.get_text().strip().isdigit()
    ]
    return max(nums) if nums else 1


# ============================================================================
# Fetching
# ============================================================================
def page_url(base, n):
    sep = "&" if "?" in base else "?"
    base = re.sub(r"([?&])page=\d+", "", base)  # drop existing page=
    base = re.sub(r"[?&]$", "", base)
    return f"{base}{sep}page={n}"


def scrape(base_url, headless=False, debug_api=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Playwright not installed. Run:\n"
            "  pip install playwright beautifulsoup4 lxml\n"
            "  playwright install chromium"
        )

    all_events, seen_urls = [], set()
    api_calls = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)

        if debug_api:

            def _log(req):
                if req.resource_type in ("xhr", "fetch"):
                    api_calls.append(req.url)

            page.on("request", _log)

        # --- page 1: also discover total page count ---
        first = page_url(base_url, 1)
        print(f"[page 1] {first}")
        page.goto(first, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('a[href*="/events/"]', timeout=15_000)
        except Exception:
            print(
                "  ! No event links appeared on page 1. The site may have "
                "challenged the browser — try --show, or the layout changed."
            )
        html = page.content()
        total = min(get_total_pages(html), MAX_PAGES)
        print(f"  total pages reported: {total}")

        for n in range(1, total + 1):
            if n > 1:
                url = page_url(base_url, n)
                print(f"[page {n}] {url}")
                page.goto(url, wait_until="domcontentloaded")
                try:
                    page.wait_for_selector('a[href*="/events/"]', timeout=15_000)
                except Exception:
                    print(f"  ! No events on page {n}; stopping.")
                    break
                html = page.content()

            events = parse_events_from_html(html)
            new = 0
            for e in events:
                key = e["event_url"] or (e["title"], e["date_raw"])
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                all_events.append(e)
                new += 1
            print(
                f"  parsed {len(events)} cards, {new} new "
                f"(running total: {len(all_events)})"
            )

            if n < total:
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        browser.close()

    if debug_api and api_calls:
        with open("api_calls.log", "w") as f:
            f.write("\n".join(dict.fromkeys(api_calls)))
        print(
            f"\nLogged {len(set(api_calls))} unique XHR/fetch URLs to "
            f"api_calls.log (look here for a JSON endpoint you could hit "
            f"directly with `requests` for a faster scraper)."
        )

    return all_events


# ============================================================================
# Output
# ============================================================================
FIELDS = [
    "title",
    "host",
    "start_date",
    "end_date",
    "start_time",
    "end_time",
    "location",
    "price_raw",
    "currency",
    "price_min",
    "price_max",
    "date_raw",
    "event_url",
    "image_url",
]


def write_outputs(events):
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(events)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {len(events)} events to {OUT_CSV} and {OUT_JSON}")


def main():
    ap = argparse.ArgumentParser(
        description="Scrape events from a Universe.com explore search."
    )
    ap.add_argument("--url", default=DEFAULT_URL, help="Universe.com explore URL")
    ap.add_argument(
        "--show", action="store_true", help="Run the browser visibly (not headless)"
    )
    ap.add_argument(
        "--debug-api", action="store_true", help="Log the site's XHR/fetch calls"
    )
    args = ap.parse_args()

    events = scrape(args.url, headless=not args.show, debug_api=args.debug_api)
    if not events:
        print(
            "No events scraped. Try --show to watch what happens, or "
            "--debug-api to inspect the site's requests."
        )
        return
    write_outputs(events)


if __name__ == "__main__":
    main()
