"""
Dice.fm event scraper (Paris browse page).

Flow:
  1. Open the browse URL.
  2. Scroll down and click "Load more" repeatedly until the button disappears.
  3. Collect all event links from the grid.
  4. Visit each event page and extract: title, type (venue link under title),
     time & date, venue address, capacity.
  5. Save results to events.csv and events.json.

Requirements:
    pip install playwright
    playwright install chromium

Run:
    python dice_scraper.py
"""

import csv
import json
import time
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Number of days after today
DAYS_AHEAD = 5  # Change to 7, 14, 30, etc.

today = datetime.today().date()
until = today + timedelta(days=DAYS_AHEAD)

SOURCE = "dice"
CITY = "Paris"  # label only — the browse URL below is Paris-specific either way

BROWSE_URL = (
    "https://dice.fm/browse/paris-5b23e8a0e63cc224a4c36a2d"
    f"?from={today:%Y-%m-%d}&until={until:%Y-%m-%d}"
)

BASE_URL = "https://dice.fm"


# NOTE on selectors: dice.fm uses styled-components, so class names look like
# "EventCard__Event-sc-583c2147-1 flSCYU". The hash suffix ("flSCYU") changes
# between site builds, but the semantic prefix ("EventCard__Event") is stable.
# We therefore match with [class*="..."] on the stable part only.

SEL_EVENT_CARD_LINK = '[class*="EventCard__Event"] a[href^="/event/"]'
SEL_LOAD_MORE_BTN = 'button:has-text("Load more")'

SEL_TITLE = 'h1[class*="EventDetailsTitle__Title"]'
SEL_TYPE = '[class*="EventDetailsTitle__Venues"]'
SEL_DATE = '[class*="EventDetailsTitle__Date"]'
SEL_VENUE = '[class*="EventDetailsVenue__Address"] span'
SEL_HIGHLIGHTS = '[class*="EventDetailsBase__Highlight"]'

HEADLESS = True  # set False to watch the browser work
LOAD_MORE_PAUSE = 2.0  # seconds to wait after each "Load more" click
DETAIL_PAGE_PAUSE = 1.0  # polite delay between event pages
MAX_LOAD_MORE = 200  # safety cap so we can never loop forever


def collect_event_links(page):
    """Scroll + click 'Load more' until it's gone, then return all event URLs."""
    page.goto(BROWSE_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector(SEL_EVENT_CARD_LINK, timeout=30_000)

    clicks = 0
    while clicks < MAX_LOAD_MORE:
        # Scroll to the bottom so the button is in view / lazy content loads
        page.mouse.wheel(0, 20_000)
        page.wait_for_timeout(800)

        btn = page.locator(SEL_LOAD_MORE_BTN)
        if btn.count() == 0 or not btn.first.is_visible():
            print("No 'Load more' button found — all events loaded.")
            break

        prev_count = page.locator(SEL_EVENT_CARD_LINK).count()
        try:
            btn.first.click(timeout=10_000)
        except PWTimeout:
            print("Could not click 'Load more' (timeout) — stopping.")
            break

        clicks += 1
        print(f"Clicked 'Load more' ({clicks}) …")

        # Wait until new cards appear (or give up after ~10s)
        deadline = time.time() + 10
        while time.time() < deadline:
            if page.locator(SEL_EVENT_CARD_LINK).count() > prev_count:
                break
            page.wait_for_timeout(500)
        page.wait_for_timeout(int(LOAD_MORE_PAUSE * 1000))

    # Collect and dedupe links, preserving order
    hrefs = page.locator(SEL_EVENT_CARD_LINK).evaluate_all(
        "els => els.map(e => e.getAttribute('href'))"
    )
    seen, links = set(), []
    for h in hrefs:
        if h and h not in seen:
            seen.add(h)
            links.append(BASE_URL + h if h.startswith("/") else h)

    print(f"Collected {len(links)} unique event links.")
    return links


def text_or_none(page, selector):
    """Return stripped inner text of the first match, or None if absent."""
    loc = page.locator(selector)
    if loc.count() == 0:
        return None
    txt = loc.first.inner_text().strip()
    return txt or None


def extract_capacity(page):
    """Capacity lives in one of several 'Highlight' rows; find the one
    whose text contains 'capacity'. Returns e.g. '175 capacity' or None."""
    rows = page.locator(SEL_HIGHLIGHTS)
    for i in range(rows.count()):
        txt = rows.nth(i).inner_text().strip()
        if "capacity" in txt.lower():
            return txt
    return None


def scrape_event(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector(SEL_TITLE, timeout=15_000)
    except PWTimeout:
        print(f"  ! Title never appeared on {url} — saving what we can.")

    return {
        "source": SOURCE,
        "city": CITY,
        "url": url,
        "title": text_or_none(page, SEL_TITLE),
        "type": text_or_none(page, SEL_TYPE),
        "date_and_time": text_or_none(page, SEL_DATE),
        "venue": text_or_none(page, SEL_VENUE),
        "capacity": extract_capacity(page),
    }


def save(events):
    with open("events.json", "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)

    fieldnames = [
        "source",
        "city",
        "url",
        "title",
        "type",
        "date_and_time",
        "venue",
        "capacity",
    ]
    with open("events.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(events)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()

        links = collect_event_links(page)

        events = []
        for i, url in enumerate(links, 1):
            print(f"[{i}/{len(links)}] {url}")
            try:
                events.append(scrape_event(page, url))
            except Exception as e:
                print(f"  ! Failed: {e}")
                events.append(
                    {
                        "source": SOURCE,
                        "city": CITY,
                        "url": url,
                        "title": None,
                        "type": None,
                        "date_and_time": None,
                        "venue": None,
                        "capacity": None,
                    }
                )
            # Save progress every 10 events so a crash loses little work
            if i % 10 == 0:
                save(events)
            time.sleep(DETAIL_PAGE_PAUSE)

        save(events)
        browser.close()

    print(f"\nDone. {len(events)} events saved to events.json and events.csv")


if __name__ == "__main__":
    main()
