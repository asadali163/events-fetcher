#!/usr/bin/env python3
"""
Meetup event scraper - Paris events, split by attendee-count bucket.

Opens the Meetup "find" page unfiltered, then uses the "Filter by size"
dropdown to select each attendee bucket in turn (50-100, then 100+). For each
bucket it scrolls until no new events load, opens every event, and pulls:
name, start time, venue name, address, and attendee count -> writes to a CSV.

--------------------------------------------------------------------------
PLEASE READ FIRST
  * Meetup's robots.txt disallows automated access to /find/, and their
    Terms of Service restrict scraping. This script only touches PUBLIC
    listing data (no personal data, no login circumvention), but you are
    responsible for how you use it. Keep it personal, low-volume, and slow.
    The official Meetup GraphQL API (api.meetup.com) is the stable route if
    you'll run this regularly.
  * The `ds2-*` CSS classes are auto-generated and DO change between site
    deploys. If a field comes back empty, update the selector in the
    matching block below - each one has a comment showing what it targets.
--------------------------------------------------------------------------

SETUP
    pip install playwright
    playwright install chromium

RUN
    python meetup_scraper.py

OUTPUT
    meetup_events.csv
"""

import csv
import re
import sys
import time
import random
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ------------------------------- CONFIG -------------------------------

# Start from the unfiltered search page, then apply the size filter via the UI.
BASE_URL = (
    "https://www.meetup.com/find/"
    "?location=fr--Paris&source=EVENTS&distance=anyDistance"
)

# Each filter to run, in order. Fields:
#   label    - the exact menu-item text to click in the "Filter by size" dropdown
#   bucket   - short tag written into the CSV so you know which run a row is from
#   url_hint - the attendeeCount value Meetup puts in the URL once the filter is
#              applied; used only to confirm the click took effect
SIZE_FILTERS = [
    {
        "label": "50-100 attendees",
        "bucket": "fiftyToHundred",
        "url_hint": "fiftyToHundred",
    },
    {"label": "100+ attendees", "bucket": "hundredPlus", "url_hint": "hundredplus"},
]

OUTPUT_CSV = "meetup_events.csv"

HEADLESS = True  # set False to watch the browser (useful for debugging)
MIN_DELAY = 2.5  # polite pause (seconds) between event-page loads
MAX_DELAY = 5.0
MAX_SCROLLS = 80  # safety cap on scroll rounds per search (generous;
# it stops early once no new events load)
NAV_TIMEOUT = 30_000  # ms

# Optional: reuse a logged-in session. Log in once in a normal browser,
# save cookies to storage_state.json, and point to it here. Leave as None to
# browse anonymously - public event data is visible without logging in.
STORAGE_STATE = None  # e.g. "storage_state.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# ----------------------------------------------------------------------

EVENT_ID_RE = re.compile(r"/events/(\d+)")


def canonical_event_url(href):
    """Strip tracking params -> https://www.meetup.com/<group>/events/<id>/"""
    m = EVENT_ID_RE.search(href or "")
    if not m:
        return None
    path = urlparse(href).path
    end = path.find(m.group(0)) + len(m.group(0))
    clean = path[:end]
    if not clean.endswith("/"):
        clean += "/"
    return f"https://www.meetup.com{clean}"


def dismiss_overlays(page):
    """Best-effort: close the EU cookie banner and any login modal."""
    for name in ["Accept", "Accept all", "Allow all", "Agree", "OK", "Got it"]:
        try:
            b = page.get_by_role("button", name=re.compile(name, re.I))
            if b.count() and b.first.is_visible():
                b.first.click(timeout=1500)
                page.wait_for_timeout(400)
                break
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def apply_size_filter(page, label, url_hint):
    """Open the 'Filter by size' dropdown and click the given size option.

    Selectors:
      button[data-testid="RevampedFilterButtonAttendeeCount"]  -> the dropdown
      div[role="menuitem"] containing the label text            -> the option
    (data-testid is stable across deploys; the label text is the visible option.)
    """
    print(f"  applying size filter: {label}")
    btn = page.locator('button[data-testid="RevampedFilterButtonAttendeeCount"]')
    btn.wait_for(state="visible", timeout=NAV_TIMEOUT)
    btn.click()

    # The radix dropdown opens; click the matching option. The labels are all
    # distinct and none is a substring of another, so has_text is unambiguous.
    option = page.get_by_role("menuitem").filter(has_text=label).first
    option.wait_for(state="visible", timeout=8000)
    option.click()

    # Confirm it took: Meetup adds the attendeeCount param to the URL. If the URL
    # doesn't change the way we expect, fall back to a plain wait rather than fail.
    try:
        page.wait_for_url(
            re.compile("attendeeCount=" + re.escape(url_hint)), timeout=8000
        )
    except PWTimeout:
        pass
    page.wait_for_timeout(2500)  # let the filtered list re-render


def scroll_and_harvest(page):
    """Scroll the already-filtered results page until no new events load."""
    seen = {}  # event_id -> canonical url
    stagnant = 0

    def harvest():
        # Selector: <a data-event-label="Event Card"> on each card
        anchors = page.locator('a[data-event-label="Event Card"]')
        for j in range(anchors.count()):
            cu = canonical_event_url(anchors.nth(j).get_attribute("href"))
            if cu:
                seen[EVENT_ID_RE.search(cu).group(1)] = cu

    harvest()
    for i in range(MAX_SCROLLS):
        before = len(seen)

        # Some layouts use a button instead of pure infinite scroll - click it
        # if it's there; otherwise scroll to the true bottom of the document,
        # which is what triggers Meetup's lazy loader.
        clicked = False
        for label in ["See more", "Show more", "See all", "Load more"]:
            try:
                btn = page.get_by_role("button", name=re.compile(label, re.I))
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # Wait for new cards to actually appear before judging. Poll for up to
        # ~7s: the moment the count grows we move on; if nothing new arrives in
        # that window, this round counts as stagnant.
        deadline = time.time() + 7
        while time.time() < deadline:
            page.wait_for_timeout(700)
            harvest()
            if len(seen) > before:
                break

        print(f"    round {i + 1}: {len(seen)} events found")

        if len(seen) == before:
            stagnant += 1
            if stagnant >= 3:  # no new events for 3 rounds -> reached the end
                break
        else:
            stagnant = 0

    return seen


def _first_text(page, selectors):
    for sel in selectors:
        loc = page.locator(sel)
        if loc.count():
            try:
                t = loc.first.inner_text(timeout=2000).strip()
                if t:
                    return t
            except Exception:
                continue
    return ""


def scrape_event(page, url):
    page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    try:
        page.keyboard.press("Escape")  # dismiss any modal
    except Exception:
        pass

    row = {"event_url": url}

    # --- Name : <h1 class="ds2-b32 ...">  (first H1 on the page) ---
    row["name"] = _first_text(page, ["h1"])

    # --- Start time : the datetime attr of the first <time> (the "from" time)
    #     <time datetime="2026-07-21T20:30:00+02:00"> ... </time>
    row["start_time"] = ""
    tloc = page.locator("time[datetime]")
    if tloc.count():
        row["start_time"] = tloc.first.get_attribute("datetime") or ""

    # --- Venue name : ds2-k16 primary <p> that does NOT wrap a <time>
    #     (the time <p> shares the same classes, hence the time check)
    venue = ""
    prim = page.locator("p.ds2-k16.text-ds2-text-fill-primary-enabled")
    for k in range(prim.count()):
        p = prim.nth(k)
        if p.locator("time").count() == 0:
            txt = (p.inner_text() or "").strip()
            if txt:
                venue = txt
                break
    row["venue"] = venue

    # --- Address : ds2-r16 tertiary <p>  ("3 Bd Beaumarchais, 75004 Paris ...")
    addr = page.locator("p.ds2-r16.text-ds2-text-fill-tertiary-enabled")
    row["address"] = addr.first.inner_text().strip() if addr.count() else ""

    # --- Attendees : the number badge next to the "Attendees" heading ---
    attendees = ""
    try:
        badge = page.locator(
            'xpath=//h2[normalize-space()="Attendees"]/..'
            "/following-sibling::div//span"
        )
        if badge.count():
            m = re.search(r"\d[\d,]*", badge.first.inner_text())
            if m:
                attendees = m.group(0).replace(",", "")
    except Exception:
        pass
    if not attendees:  # fallback: "N attendees" text anywhere
        m = re.search(r"([\d,]+)\s+attendees", page.locator("body").inner_text(), re.I)
        if m:
            attendees = m.group(1).replace(",", "")
    row["attendees"] = attendees

    return row


def main():
    fields = [
        "search_bucket",
        "name",
        "start_time",
        "venue",
        "address",
        "attendees",
        "event_url",
    ]
    seen_global = set()  # avoid re-scraping an event that shows in both buckets

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=HEADLESS)
            ctx_kwargs = {"user_agent": USER_AGENT}
            if STORAGE_STATE:
                ctx_kwargs["storage_state"] = STORAGE_STATE
            context = browser.new_context(**ctx_kwargs)
            context.set_default_timeout(NAV_TIMEOUT)
            page = context.new_page()

            for flt in SIZE_FILTERS:
                bucket = flt["bucket"]
                print(f"\n=== Filter: {flt['label']} ({bucket}) ===")

                # Fresh load of the unfiltered page, then apply the size filter
                # through the dropdown. Loading fresh each time keeps state clean.
                page.goto(BASE_URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                dismiss_overlays(page)
                apply_size_filter(page, flt["label"], flt["url_hint"])

                events = scroll_and_harvest(page)
                print(f"  total unique events: {len(events)}")

                for i, (eid, ev_url) in enumerate(events.items(), 1):
                    if eid in seen_global:
                        continue
                    seen_global.add(eid)
                    print(f"  [{i}/{len(events)}] {ev_url}")
                    try:
                        data = scrape_event(page, ev_url)
                    except PWTimeout:
                        print("      timeout - skipped")
                        continue
                    except Exception as e:
                        print(f"      error: {e} - skipped")
                        continue
                    data["search_bucket"] = bucket
                    writer.writerow(data)
                    fh.flush()  # save progress as we go
                    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

            browser.close()

    print(f"\nDone -> {OUTPUT_CSV}")


if __name__ == "__main__":
    sys.exit(main())
