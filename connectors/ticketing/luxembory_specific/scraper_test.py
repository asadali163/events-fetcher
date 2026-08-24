#!/usr/bin/env python3
"""
Luxembourg City "What's On" event scraper.
https://www.luxembourg-city.com/en/what-s-on

Applies the site filters (date window: today -> today+DAYS, audience: adults),
keeps ONLY single-date events (e.g. 24.08.2026) and skips date-range events
(e.g. 05.08.2026 - 01.09.2026), then opens each event page and extracts:

    - title           (the <h1>)
    - weekday / date  (e.g. Wednesday / 26.08.2026)
    - start / end time (e.g. 17H30 / 19H30 -- may be empty)
    - address         (venue text)
    - latitude/longitude + maps_url (parsed from the Google Maps link)
    - description     (the general-information body, if present)

Output: <stem>.json and <stem>.csv

--------------------------------------------------------------------------
Requirements:
    pip install playwright beautifulsoup4
    playwright install chromium

Usage:
    python lux_events_scraper.py --days 30 --out lux_events
    python lux_events_scraper.py --days 14 --headful --limit 5   # debug run
--------------------------------------------------------------------------

Date filter: driven through the site's flatpickr range calendar — open Calendar
view, click the start day (today), click the end day (today+DAYS), then click
"Show the results". Days are matched by their French aria-label (e.g.
"août 24, 2026"). As a safety net, every event is ALSO filtered to the
[today, today+DAYS] window in Python using its own detail-page date.

One spot may still need adjusting (marked >>> ADJUST <<<):
  - extract_event_links()  – the listing-card markup wasn't available, so link
                             detection is heuristic. Paste one event card's
                             HTML and this can be made exact.
The detail-page extraction is based on the exact HTML you provided.
"""

import argparse
import csv
import json
import re
import time
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.luxembourg-city.com"
LISTING_URL = f"{BASE_URL}/en/what-s-on"

DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
TIME_RE = re.compile(r"\b(\d{1,2}[Hh]\d{2})\b")
WEEKDAY_RE = re.compile(
    r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", re.I
)
# "05.08.2026 - 01.09.2026"  (dash can be -, en-dash, em-dash)
RANGE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}\s*[-\u2013\u2014]\s*\d{2}\.\d{2}\.\d{4}")
# https://maps.google.com/maps?q=49.625661,6.160547
MAPS_Q_RE = re.compile(r"[?&]q=(-?\d+\.\d+),\s*(-?\d+\.\d+)")


# --------------------------------------------------------------------------
# Filtering / listing
# --------------------------------------------------------------------------
def apply_filters(page, days_ahead):
    page.goto(LISTING_URL, wait_until="domcontentloaded")
    _dismiss_cookies(page)

    today = date.today()
    end = today + timedelta(days=days_ahead)

    # 1) Open the "Calendar view" panel and set the flatpickr date range.
    _open_calendar_panel(page)
    _set_date_range(page, today, end)

    # 2) Audience = adults LAST, so an audience-triggered reload can't drop it.
    #    (Correctness note: the date window is ALSO enforced in Python later,
    #    so even if the on-site date filter is lost, output stays correct — but
    #    audience cannot be re-derived per event, so it must stick here.)
    _select_adults(page)
    page.wait_for_timeout(1800)  # let the event list reload

    # 3) Final safety check: make sure adults is still selected.
    if not _audience_is_adults(page):
        print("Audience filter was lost after reload — re-applying …")
        _select_adults(page)
        page.wait_for_timeout(1500)


def _open_calendar_panel(page):
    """Click 'Calendar view' and wait for the flatpickr calendar to render."""
    try:
        page.get_by_role("button", name=re.compile("Calendar view", re.I)).click(
            timeout=5000
        )
    except Exception:
        pass  # panel may already be open, or the label may differ
    try:
        page.wait_for_selector(".flatpickr-calendar .flatpickr-days", timeout=6000)
    except Exception:
        print("WARNING: flatpickr calendar didn't appear; date filter may not apply.")


def _audience_is_adults(page):
    for sel in ("#audiences", "select[name='audiences']"):
        try:
            if page.locator(sel).count():
                return page.eval_on_selector(sel, "el => el.value") == "adults"
        except Exception:
            continue
    return False


def _select_adults(page):
    """
    Set the audience filter to 'adults' and verify it actually took.

    The site uses a JS-enhanced <select> (js-select-adaptatif), so besides
    Playwright's native select_option (which fires input/change) we also
    dispatch a change event manually as a fallback for the framework.
    Raises RuntimeError if the value can't be confirmed — the filter matters,
    so we fail loudly rather than silently scraping all audiences.
    """
    selectors = ("#audiences", "select[name='audiences']", ".js-select-audience-events")

    # Make sure the select is present (it may live inside the filter panel).
    try:
        page.wait_for_selector(selectors[0], timeout=5000)
    except Exception:
        pass

    for sel in selectors:
        loc = page.locator(sel)
        try:
            if not loc.count():
                continue
            # 1) native selection (fires input + change)
            try:
                loc.first.select_option("adults", timeout=3000)
            except Exception:
                pass
            # 2) belt-and-braces: set value + dispatch change for the JS widget
            page.evaluate(
                """(css) => {
                    const el = document.querySelector(css);
                    if (!el) return;
                    el.value = 'adults';
                    el.dispatchEvent(new Event('input',  {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                sel,
            )
            page.wait_for_timeout(800)
            # 3) verify
            value = page.eval_on_selector(sel, "el => el.value")
            if value == "adults":
                print("Audience filter confirmed: adults")
                return
        except Exception:
            continue

    raise RuntimeError(
        "Could not confirm the 'adults' audience filter. Re-run with --headful "
        "to watch, and check the <select id='audiences'> markup hasn't changed."
    )


def _dismiss_cookies(page):
    for label in (
        "Accept all",
        "Accept",
        "I agree",
        "OK",
        "Got it",
        "Tout accepter",
        "Accepter",
        "J'accepte",
    ):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=1500)
                page.wait_for_timeout(300)
                return
        except Exception:
            pass


# flatpickr aria-labels are localised to French, e.g. "août 24, 2026".
FRENCH_MONTHS = [
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
]


def _flatpickr_label(d):
    """Build the day's aria-label as flatpickr renders it, e.g. 'août 24, 2026'."""
    return f"{FRENCH_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _set_date_range(page, start, end):
    """
    Drive the flatpickr range calendar exactly like a person:
    open Calendar view -> click the start day -> click the end day ->
    click 'Show the results'. Days are matched by their French aria-label.
    """
    # Make sure the calendar panel is actually showing.
    try:
        page.wait_for_selector(".flatpickr-calendar", state="visible", timeout=6000)
    except Exception:
        try:  # the first Calendar-view click may not have landed — retry
            page.get_by_role("button", name=re.compile("Calendar view", re.I)).click(
                timeout=3000
            )
            page.wait_for_selector(".flatpickr-calendar", state="visible", timeout=6000)
        except Exception:
            print(
                "WARNING: flatpickr calendar never became visible; "
                "date filter not applied."
            )
            return

    if not _click_flatpickr_day(page, start):
        print(f"WARNING: could not click start date {start}.")
    page.wait_for_timeout(350)
    if not _click_flatpickr_day(page, end):
        print(f"WARNING: could not click end date {end}.")
    page.wait_for_timeout(350)

    # Submit the range with the "Show the results" button (button or link).
    labels = re.compile(
        "Show the results|Voir les r\u00e9sultats|"
        "Afficher les r\u00e9sultats|r\u00e9sultats",
        re.I,
    )
    for role in ("button", "link"):
        try:
            el = page.get_by_role(role, name=labels)
            if el.count() and el.first.is_visible():
                el.first.click(timeout=3000)
                page.wait_for_timeout(1800)
                print(f"Date range applied: {start} -> {end}")
                return
        except Exception:
            pass
    print("WARNING: 'Show the results' button not found; date range may not apply.")


def _click_flatpickr_day(page, d, max_nav=24):
    """
    Click the day cell for date `d`, navigating forward month-by-month with the
    next-month arrow until that day is on screen. Prefers a real day cell over
    an adjacent-month spillover cell. Returns True on success.
    """
    label = _flatpickr_label(d)
    primary = (
        f'.flatpickr-day[aria-label="{label}"]'
        ":not(.prevMonthDay):not(.nextMonthDay):not(.flatpickr-disabled)"
    )
    fallback = f'.flatpickr-day[aria-label="{label}"]:not(.flatpickr-disabled)'

    for _ in range(max_nav):
        for sel in (primary, fallback):
            loc = page.locator(sel)
            try:
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=2000)
                    return True
            except Exception:
                pass
        # Not in the current month view — advance one month.
        try:
            nxt = page.locator(".flatpickr-next-month:not(.flatpickr-disabled)")
            if not nxt.count():
                break
            nxt.first.click(timeout=1500)
            page.wait_for_timeout(250)
        except Exception:
            break
    return False


def _load_all_events(page, max_rounds=40):
    """Scroll + click any 'load more' button until the list stops growing."""
    last_height = 0
    for _ in range(max_rounds):
        clicked = False
        for label in (
            "Load more",
            "Show more",
            "See more",
            "Voir plus",
            "Afficher plus",
            "Charger plus",
            "Plus d'\u00e9v\u00e9nements",
        ):
            try:
                btn = page.get_by_role("button", name=re.compile(label, re.I))
                if btn.count() and btn.first.is_visible():
                    btn.first.click(timeout=1500)
                    page.wait_for_timeout(1200)
                    clicked = True
                    break
            except Exception:
                pass
        page.mouse.wheel(0, 20000)
        page.wait_for_timeout(1000)
        height = page.evaluate("document.body.scrollHeight")
        if height == last_height and not clicked:
            break
        last_height = height


def extract_event_links(page):
    """
    >>> ADJUST <<<  Return [(url, card_text), ...] for events in the listing.

    Heuristic: for every internal anchor, walk up a few parents to capture the
    card text, and keep it only if that text contains a DD.MM.YYYY date. Once
    you know the real card/link selector (paste one card's HTML), replace this
    with something precise like:  soup.select("a.event-card")
    """
    soup = BeautifulSoup(page.content(), "html.parser")
    results, seen = [], set()

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href or href.startswith("#"):
            continue
        if not (href.startswith("/") or href.startswith(BASE_URL)):
            continue
        if href.rstrip("/").endswith("/en/what-s-on"):
            continue

        # Walk up to find the card text (with a date).
        node, text = a, ""
        for _ in range(4):
            if node is None:
                break
            text = node.get_text(" ", strip=True)
            if DATE_RE.search(text):
                break
            node = node.parent
        if not DATE_RE.search(text):
            continue

        url = urljoin(BASE_URL, href)
        if url in seen:
            continue
        seen.add(url)
        results.append((url, text))
    return results


def is_single_date_text(text):
    """True for single-date cards, False for ranges like '05.08.2026 - 01.09.2026'."""
    if RANGE_RE.search(text):
        return False
    return len(set(DATE_RE.findall(text))) == 1


# --------------------------------------------------------------------------
# Detail-page extraction (based on the HTML you provided)
# --------------------------------------------------------------------------
def scrape_event(page, url):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(400)
    soup = BeautifulSoup(page.content(), "html.parser")

    title = _text(soup.select_one("h1"))
    weekday, date_str, start_t, end_t = _parse_datetime(soup)
    address, lat, lng, maps_url = _parse_location(soup)
    description = _parse_description(soup)

    return {
        "url": url,
        "title": title,
        "weekday": weekday,
        "date": date_str,
        "start_time": start_t,
        "end_time": end_t,
        "address": address,
        "latitude": lat,
        "longitude": lng,
        "maps_url": maps_url,
        "description": description,
    }


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _parse_datetime(soup):
    """
    Find the date/time <p>. Prefer a <p> that has BOTH a weekday and a date;
    fall back to the first <p> with a date. Skips range paragraphs.
    """
    best = None
    for p in soup.find_all("p"):
        raw = p.get_text("\n", strip=True)
        if DATE_RE.search(raw) and WEEKDAY_RE.search(raw) and not RANGE_RE.search(raw):
            best = raw
            break
    if best is None:
        for p in soup.find_all("p"):
            raw = p.get_text("\n", strip=True)
            if DATE_RE.search(raw) and not RANGE_RE.search(raw):
                best = raw
                break
    if best is None:
        return "", "", "", ""

    date_str = DATE_RE.search(best).group(1)
    wd = WEEKDAY_RE.search(best)
    times = TIME_RE.findall(best)
    return (
        wd.group(1) if wd else "",
        date_str,
        times[0] if len(times) >= 1 else "",
        times[1] if len(times) >= 2 else "",
    )


def _parse_location(soup):
    """Address text + coordinates, from the div that holds the Google Maps link."""
    address = lat = lng = maps_url = ""
    maps_a = soup.find("a", href=MAPS_Q_RE)
    if maps_a:
        maps_url = maps_a.get("href", "")
        m = MAPS_Q_RE.search(maps_url)
        if m:
            lat, lng = m.group(1), m.group(2)
        container = maps_a.find_parent("div")
        if container:
            p = container.find("p")
            if p:
                # Join address lines (<br>-separated) with commas.
                address = ", ".join(
                    s.strip() for s in p.get_text("\n").split("\n") if s.strip()
                )
    return address, lat, lng, maps_url


def _parse_description(soup):
    """General-information body; empty string if the page has none."""
    node = soup.select_one(".description-textimg-formatted")
    if node is None:
        return ""
    for tag in node.select("img, script, style"):
        tag.decompose()
    paras = [p.get_text(" ", strip=True) for p in node.find_all("p")]
    paras = [p for p in paras if p]
    return "\n\n".join(paras) if paras else node.get_text(" ", strip=True)


# --------------------------------------------------------------------------
# Helpers / output
# --------------------------------------------------------------------------
def _parse_ddmmyyyy(s):
    d, m, y = s.split(".")
    return date(int(y), int(m), int(d))


def _write_json(rows, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _write_csv(rows, path):
    fields = [
        "date",
        "weekday",
        "start_time",
        "end_time",
        "title",
        "address",
        "latitude",
        "longitude",
        "maps_url",
        "url",
        "description",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Scrape luxembourg-city.com What's On events."
    )
    ap.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days ahead from today (default: 30)",
    )
    ap.add_argument("--out", default="lux_events", help="Output filename stem")
    ap.add_argument("--headful", action="store_true", help="Show the browser window")
    ap.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Seconds to wait between event pages (be polite)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max events to scrape (0 = no limit; useful for testing)",
    )
    args = ap.parse_args()

    today = date.today()
    end = today + timedelta(days=args.days)

    with sync_playwright() as pw:
        # Launch maximized (real full window, not a small fixed frame).
        # --start-maximized only takes effect with no_viewport=True, otherwise
        # Playwright forces a default 1280x720 viewport regardless of window size.
        browser = pw.chromium.launch(
            headless=not args.headful,
            args=["--start-maximized"],
        )
        ctx = browser.new_context(locale="en-US", no_viewport=True)
        page = ctx.new_page()

        print(
            f"Applying filters: {today} -> {end} (+{args.days}d), audience=adults ..."
        )
        apply_filters(page, args.days)
        _load_all_events(page)

        links = extract_event_links(page)
        print(f"Found {len(links)} candidate event links.")

        single = [(u, t) for (u, t) in links if is_single_date_text(t)]
        print(f"{len(single)} look like single-date events.")

        if args.limit:
            single = single[: args.limit]

        rows = []
        for i, (url, _) in enumerate(single, 1):
            try:
                data = scrape_event(page, url)
            except Exception as e:
                print(f"  [{i}/{len(single)}] ERROR {url}: {e}")
                continue

            # Enforce the date window using the detail page's own date.
            if data["date"]:
                try:
                    d = _parse_ddmmyyyy(data["date"])
                    if not (today <= d <= end):
                        print(
                            f"  [{i}/{len(single)}] skip out-of-window "
                            f"({data['date']}): {data['title']}"
                        )
                        continue
                except ValueError:
                    pass

            rows.append(data)
            print(f"  [{i}/{len(single)}] {data['date']}  {data['title']}")
            time.sleep(args.delay)

        browser.close()

    _write_json(rows, f"{args.out}.json")
    _write_csv(rows, f"{args.out}.csv")
    print(f"\nDone. {len(rows)} events -> {args.out}.json and {args.out}.csv")


if __name__ == "__main__":
    main()
