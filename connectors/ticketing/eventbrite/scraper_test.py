#!/usr/bin/env python3
"""
Eventbrite events scraper (Paris example) — multi-category, two calendar types.

Flow:
  1. For each CATEGORY, build a search URL:
        /d/<LOCATION_SLUG>/<category>--events/?page=N&start_date=..&end_date=..
     start_date = today, end_date = today + DAYS_AHEAD.
  2. Load page 1, scroll to the bottom, read the pagination footer ("X of N")
     to learn how many pages of results there are.
  3. For every page 1..N, collect the link to each event card.
  4. Open each event page and pull title / venue / city / date:
       - Single concrete date ("Mon, Aug 31 - 8 PM")  -> one row, with time.
       - "Multiple dates" -> click "Check availability", then one of TWO widgets
         appears:
           a) COMPACT calendar  (data-testid="calendar-container")
              Every listed date is bookable. One row per date, WITH time.
           b) SCROLLABLE calendar (data-testid="scrollable-calendar-container")
              A full month grid. Only "available/enabled" cells are bookable.
              Keep only bookable dates that fall inside [start, end]; one row per
              such date, DATE ONLY (no time). If none fall in the window, SKIP the
              whole event.
  5. Write results to CSV + JSONL, appended as we go so a crash keeps progress.
     Every row is tagged with its category. An event seen in more than one
     category feed is fetched once and re-tagged (cached).

Setup:
    pip install playwright
    playwright install chromium

Run:
    python eventbrite_scraper.py
"""

from __future__ import annotations

import csv
import json
import random
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------------------- #
# Configuration - tweak these.
# --------------------------------------------------------------------------- #
DOMAIN = "https://www.eventbrite.com"
LOCATION_SLUG = "france--paris"  # the /d/<slug>/ portion of the search URL

# Bare category names. Each becomes "<category>--events" in the URL
# (the special value "all" becomes "all-events"). Add as many as you like.
CATEGORIES = [
    "music",
    "science-and-tech",
    # "film-and-media",
    # "food-and-drink",
    # "arts",
    # "business",
]

DAYS_AHEAD = 2  # end_date = today + this many days (inclusive window)
HEADLESS = False  # Eventbrite is friendlier with a visible browser
MAX_PAGES = None  # None = every page; or cap with an int
BLOCK_MEDIA = True  # skip images/fonts/media downloads for speed

# If True, the COMPACT calendar's dates are also filtered to [start, end]
# (and the event is skipped when none fall in the window). Default False keeps
# the original "record every listed date" behavior for the compact widget.
WINDOW_FILTER_COMPACT = False

PAGE_PAUSE = (1.5, 3.0)  # (min, max) seconds of jitter between event pages
NAV_TIMEOUT = 45_000  # ms

OUT_DIR = Path(__file__).parent
CSV_PATH = OUT_DIR / "eventbrite_events.csv"
JSONL_PATH = OUT_DIR / "eventbrite_events.jsonl"

CSV_FIELDS = [
    "category",
    "event_id",
    "title",
    "venue",
    "city",
    "location_raw",
    "date_iso",
    "date_raw",
    "time_raw",
    "url",
    "scraped_at",
]

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

# --- JS: COMPACT calendar (only bookable dates are shown, each with a time). ---
# Uses class *substrings* rather than the hashed class names, which change on
# every Eventbrite build.
COMPACT_JS = r"""
() => {
  const container = document.querySelector('[data-testid="calendar-container"]');
  if (!container) return null;
  const out = [];
  let currentMonth = '';
  container.querySelectorAll('*').forEach(el => {
    const cls = el.getAttribute('class') || '';
    if (cls.includes('monthName')) { currentMonth = el.textContent.trim(); return; }
    if (cls.includes('dateText')) {
      const card = el.closest('[class*="dateCell"], [class*="DateCell"]') || el.parentElement;
      const wd = card ? card.querySelector('[class*="weekday"]') : null;
      const tm = card ? card.querySelector('[class*="timeSlot"]') : null;
      out.push({
        month:   currentMonth,
        weekday: wd ? wd.textContent.trim() : '',
        day:     el.textContent.trim(),
        time:    tm ? tm.textContent.trim() : ''
      });
    }
  });
  return out;
}
"""

# --- JS: SCROLLABLE full-grid calendar. Report each cell's day + bookable flag. -
# NOTE: "unavailableDateCell" contains the substring "availableDateCell", so we
# key "bookable" on enabledDateCell / availableDateCell with an explicit
# unavailable guard.
SCROLLABLE_JS = r"""
() => {
  const container = document.querySelector('[data-testid="scrollable-calendar-container"]');
  if (!container) return null;
  const months = [];
  container.querySelectorAll('[data-testid="month"]').forEach(mEl => {
    const nameEl = mEl.querySelector('[class*="monthName"]');
    const monthName = nameEl ? nameEl.textContent.trim() : '';
    const cells = [];
    mEl.querySelectorAll('[class*="calendarCell"]').forEach(cell => {
      const cls = cell.getAttribute('class') || '';
      const dt = cell.querySelector('[class*="dateText"]');
      const day = dt ? dt.textContent.trim() : '';
      const loading     = cls.includes('defaultLoadingDateCell');
      const unavailable = cls.includes('unavailableDateCell');
      const enabled     = cls.includes('enabledDateCell');
      const available   = cls.includes('availableDateCell') && !unavailable;
      const bookable    = !unavailable && (enabled || available);
      cells.push({ day, bookable, loading });
    });
    months.push({ monthName, cells });
  });
  return months;
}
"""


# --------------------------------------------------------------------------- #
# URL / date helpers
# --------------------------------------------------------------------------- #
def category_segment(category: str) -> str:
    c = (category or "").strip().strip("/")
    if c in ("all", "all-events"):
        return "all-events"
    if c.endswith("--events"):
        return c
    return f"{c}--events"


def build_search_url(cat_seg: str, page_no: int, start: date, end: date) -> str:
    return (
        f"{DOMAIN}/d/{LOCATION_SLUG}/{cat_seg}/"
        f"?page={page_no}&start_date={start:%Y-%m-%d}&end_date={end:%Y-%m-%d}"
    )


def month_to_num(name: str):
    return MONTHS.get((name or "").strip().lower().rstrip("."))


def parse_month_year(text: str, ref_year: int, ref_month: int):
    """'August 2026' -> (8, 2026); 'August' -> (8, inferred year)."""
    t = (text or "").strip()
    m = re.search(r"([A-Za-z]+)\s+(\d{4})", t)
    if m:
        return month_to_num(m.group(1)), int(m.group(2))
    m2 = re.search(r"([A-Za-z]+)", t)
    if m2:
        mn = month_to_num(m2.group(1))
        if mn:
            year = ref_year if mn >= ref_month else ref_year + 1  # Dec -> Jan rollover
            return mn, year
    return None, None


def build_iso(month_text, day, ref_year, ref_month):
    mn, yr = parse_month_year(month_text, ref_year, ref_month)
    digits = re.sub(r"\D", "", str(day))
    if not mn or not digits:
        return ""
    try:
        return date(yr, mn, int(digits)).isoformat()
    except ValueError:
        return ""


# --------------------------------------------------------------------------- #
# Page interaction helpers
# --------------------------------------------------------------------------- #
def dismiss_overlays(page) -> None:
    for sel in (
        "#onetrust-accept-btn-handler",
        "button[data-testid='cookie-banner-accept']",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1200):
                btn.click(timeout=2000)
                page.wait_for_timeout(400)
                break
        except Exception:
            pass


def scroll_to_bottom(page, max_rounds: int = 30) -> None:
    last = -1
    for _ in range(max_rounds):
        page.mouse.wheel(0, 4200)
        page.wait_for_timeout(550)
        height = page.evaluate("document.body.scrollHeight")
        if height == last:
            break
        last = height


def get_total_pages(page) -> int:
    try:
        txt = page.locator("[data-testid='pagination-parent']").first.inner_text(
            timeout=5000
        )
        m = re.search(r"of\s+(\d+)", txt)  # "1 of 11"
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 1


def collect_event_links(page):
    """Return [(event_id, url), ...] for every event card on the current page."""
    links = page.eval_on_selector_all(
        "a.event-card-link[href*='/e/']",
        "els => els.map(a => ({ id: a.getAttribute('data-event-id') || '', href: a.href }))",
    )
    seen, out = set(), []
    for l in links:
        key = l["id"] or l["href"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append((l["id"], l["href"]))
    return out


def parse_location(page):
    try:
        data = page.eval_on_selector(
            "[data-testid='event-venue']",
            """el => {
                const parts = [];
                el.childNodes.forEach(n => {
                    if (n.nodeType === Node.TEXT_NODE) {
                        const t = n.textContent.trim();
                        if (t) parts.push(t);
                    }
                });
                return { raw: el.innerText.trim(), parts };
            }""",
        )
    except Exception:
        return {"venue": "", "city": "", "raw": ""}
    parts = data.get("parts", [])
    return {
        "venue": parts[0] if len(parts) >= 1 else "",
        "city": parts[1] if len(parts) >= 2 else "",
        "raw": data.get("raw", ""),
    }


def parse_single_date(text: str, ref_year: int, ref_month: int):
    """'Mon, Aug 31 - 8 PM' -> (iso, time_raw)."""
    parts = re.split(r"[•·|]", text)
    date_part = parts[0].strip() if parts else text
    time_part = parts[1].strip() if len(parts) > 1 else ""
    iso = ""
    m = re.search(r"([A-Za-z]{3,})\s+(\d{1,2})", date_part)
    if m:
        iso = build_iso(m.group(1), m.group(2), ref_year, ref_month)
    return iso, time_part


# --------------------------------------------------------------------------- #
# Calendar handling (after clicking "Check availability")
# --------------------------------------------------------------------------- #
def find_calendar(page, timeout_ms: int = 9000):
    """Poll the page and all its frames for either calendar widget."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for ctx in [page, *page.frames]:
            try:
                if ctx.query_selector("[data-testid='calendar-container']"):
                    return ctx, "compact"
                if ctx.query_selector("[data-testid='scrollable-calendar-container']"):
                    return ctx, "scrollable"
            except Exception:
                continue
        page.wait_for_timeout(300)
    return None, None


def ensure_months_loaded(ctx, end: date, max_scrolls: int = 8) -> None:
    """Scroll the scrollable calendar until the window's end month is loaded
    (future months render as empty 'loading' cells until scrolled into view)."""
    target = f"{end.strftime('%B')} {end.year}"  # e.g. "September 2026"
    for _ in range(max_scrolls):
        loaded = ctx.evaluate(
            """(target) => {
                const months = document.querySelectorAll('[data-testid="month"]');
                for (const m of months) {
                    const n = m.querySelector('[class*="monthName"]');
                    if (n && n.textContent.trim() === target) {
                        return m.querySelectorAll('[class*="dateText"]').length > 0;
                    }
                }
                return false;
            }""",
            target,
        )
        if loaded:
            return
        ctx.evaluate("""() => {
                const list = document.querySelector(
                    '[data-testid="scrollable-calendar-container"] [class*="scrollableList"]'
                ) || document.querySelector('[data-testid="scrollable-calendar-container"]');
                if (list) list.scrollBy(0, 700);
            }""")
        time.sleep(0.5)


def parse_compact_rows(ctx, start, end, ref_year, ref_month):
    """Compact widget: one entry per listed date, WITH time.
    Returns (rows, all_out_of_window_flag)."""
    raw = ctx.evaluate(COMPACT_JS) or []
    rows, in_window = [], 0
    for c in raw:
        iso = build_iso(c["month"], c["day"], ref_year, ref_month)
        if WINDOW_FILTER_COMPACT and iso:
            d = date.fromisoformat(iso)
            if not (start <= d <= end):
                continue
        in_window += 1
        date_raw = " ".join(
            x for x in (c["weekday"], c["month"], c["day"]) if x
        ).strip()
        rows.append({"date_iso": iso, "date_raw": date_raw, "time_raw": c["time"]})
    return rows


def parse_scrollable_rows(ctx, start, end, ref_year, ref_month):
    """Scrollable widget: keep only BOOKABLE dates within [start, end], DATE ONLY."""
    months = ctx.evaluate(SCROLLABLE_JS) or []
    rows = []
    for m in months:
        mn, yr = parse_month_year(m["monthName"], ref_year, ref_month)
        if not mn:
            continue
        for cell in m["cells"]:
            if cell["loading"] or not cell["day"] or not cell["bookable"]:
                continue
            digits = re.sub(r"\D", "", cell["day"])
            if not digits:
                continue
            try:
                d = date(yr, mn, int(digits))
            except ValueError:
                continue
            if start <= d <= end:
                rows.append(
                    {
                        "date_iso": d.isoformat(),
                        "date_raw": d.strftime("%a %b %d"),
                        "time_raw": "",
                    }
                )
    # de-dupe (same date could appear twice if the grid re-rendered)
    seen, uniq = set(), []
    for r in rows:
        if r["date_iso"] in seen:
            continue
        seen.add(r["date_iso"])
        uniq.append(r)
    return uniq


def resolve_multiple_dates(page, start, end, ref_year, ref_month):
    """Click 'Check availability' and read whichever calendar appears.
    Returns a dict: {"kind": "compact"|"scrollable"|"none", "rows": [...]}."""
    try:
        page.locator("[data-testid='conversion-bar-checkout-button']").first.click(
            timeout=6000
        )
    except Exception:
        return {"kind": "none", "rows": []}

    ctx, kind = find_calendar(page)
    if not ctx:
        return {"kind": "none", "rows": []}

    if kind == "compact":
        return {
            "kind": "compact",
            "rows": parse_compact_rows(ctx, start, end, ref_year, ref_month),
        }

    # scrollable
    try:
        ensure_months_loaded(ctx, end)
    except Exception:
        pass
    return {
        "kind": "scrollable",
        "rows": parse_scrollable_rows(ctx, start, end, ref_year, ref_month),
    }


# --------------------------------------------------------------------------- #
# Per-event extraction (category-agnostic; category is attached by the caller)
# --------------------------------------------------------------------------- #
def extract_event(page, url, start, end, ref_year, ref_month):
    """Return a list of row dicts (WITHOUT the 'category' field).
    An empty list means: skip this event (nothing bookable in the window)."""
    page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    dismiss_overlays(page)

    try:
        title = (
            page.locator("[data-testid='event-title']")
            .first.inner_text(timeout=8000)
            .strip()
        )
    except Exception:
        title = ""

    loc = parse_location(page)

    event_id = ""
    m = re.search(r"-(\d+)/?$", urlparse(url).path)  # /e/...-<id>
    if m:
        event_id = m.group(1)

    date_text = ""
    try:
        date_text = (
            page.locator("[data-testid='conversion-bar-date']")
            .first.inner_text(timeout=6000)
            .strip()
        )
    except Exception:
        pass

    base = {
        "event_id": event_id,
        "title": title,
        "venue": loc["venue"],
        "city": loc["city"],
        "location_raw": loc["raw"],
        "url": url,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }

    if "multiple" in date_text.lower():
        res = resolve_multiple_dates(page, start, end, ref_year, ref_month)
        if res["rows"]:
            return [dict(base, **r) for r in res["rows"]]
        # No usable dates:
        #  - scrollable with nothing bookable in window  -> intentional skip
        #  - compact filtered to nothing / could not read -> also skip (clean output)
        if res["kind"] == "scrollable":
            print(f"      · skipped (no bookable date in window) - {title[:50]}")
        else:
            print(f"      · skipped (calendar unreadable/empty) - {title[:50]}")
        return []

    # single concrete date
    iso, tm = parse_single_date(date_text, ref_year, ref_month)
    return [dict(base, date_iso=iso, date_raw=date_text, time_raw=tm)]


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def append_rows(rows) -> None:
    if not rows:
        return
    new_csv = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new_csv:
            w.writeheader()
        w.writerows(rows)
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    start = date.today()
    end = start + timedelta(days=DAYS_AHEAD)
    ref_year, ref_month = start.year, start.month
    print(f"Search window: {start} -> {end}  |  categories: {', '.join(CATEGORIES)}")

    event_cache: dict[str, list] = {}  # event_id -> extracted base rows (no category)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        if BLOCK_MEDIA:
            context.route(
                re.compile(
                    r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4|webm)(\?|$)", re.I
                ),
                lambda route: route.abort(),
            )
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)

        grand_total = 0
        for category in CATEGORIES:
            cat_seg = category_segment(category)
            print(f"\n=== Category: {category}  ({cat_seg}) ===")

            # page 1 -> discover how many pages
            page.goto(
                build_search_url(cat_seg, 1, start, end), wait_until="domcontentloaded"
            )
            dismiss_overlays(page)
            scroll_to_bottom(page)
            total = get_total_pages(page)
            if MAX_PAGES:
                total = min(total, MAX_PAGES)
            print(f"  {total} page(s) of results.")

            # walk pages -> gather links
            links = []
            for pno in range(1, total + 1):
                if pno > 1:
                    page.goto(
                        build_search_url(cat_seg, pno, start, end),
                        wait_until="domcontentloaded",
                    )
                    dismiss_overlays(page)
                scroll_to_bottom(page)
                found = collect_event_links(page)
                print(f"    page {pno}: {len(found)} events")
                links.extend(found)

            seen, unique = set(), []
            for eid, url in links:
                key = eid or url.split("?")[0]
                if key in seen:
                    continue
                seen.add(key)
                unique.append((eid, url))
            print(f"  {len(unique)} unique events.")

            # visit each event
            for i, (eid, url) in enumerate(unique, 1):
                cache_key = eid or url.split("?")[0]
                try:
                    if cache_key in event_cache:
                        base_rows = event_cache[cache_key]  # reuse across categories
                    else:
                        base_rows = extract_event(
                            page, url, start, end, ref_year, ref_month
                        )
                        event_cache[cache_key] = base_rows
                        time.sleep(random.uniform(*PAGE_PAUSE))

                    rows = [dict(r, category=category) for r in base_rows]
                    append_rows(rows)
                    grand_total += len(rows)
                    title = base_rows[0]["title"] if base_rows else ""
                    print(f"    [{i}/{len(unique)}] {len(rows)} row(s) - {title[:55]}")
                except PWTimeout:
                    print(f"    [{i}/{len(unique)}] TIMEOUT - {url}")
                except Exception as e:
                    print(f"    [{i}/{len(unique)}] ERROR ({e}) - {url}")

        browser.close()

    print(f"\nDone. {grand_total} rows written to:\n  {CSV_PATH}\n  {JSONL_PATH}")


if __name__ == "__main__":
    main()
