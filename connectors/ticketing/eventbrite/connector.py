import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from connector_sdk.core.base_connector import BaseConnector
from core.dedup import haversine_meters
from core.geocoding import geocode_place

DOMAIN = "https://www.eventbrite.com"

MONTHS = {
    "jan": 1,
    "january": 1,
    "janv": 1,
    "janvier": 1,
    "feb": 2,
    "february": 2,
    "févr": 2,
    "fevr": 2,
    "février": 2,
    "fevrier": 2,
    "mar": 3,
    "march": 3,
    "mars": 3,
    "apr": 4,
    "april": 4,
    "avr": 4,
    "avril": 4,
    "may": 5,
    "mai": 5,
    "jun": 6,
    "june": 6,
    "juin": 6,
    "jul": 7,
    "july": 7,
    "juil": 7,
    "juillet": 7,
    "aug": 8,
    "august": 8,
    "août": 8,
    "aout": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "septembre": 9,
    "oct": 10,
    "october": 10,
    "octobre": 10,
    "nov": 11,
    "november": 11,
    "novembre": 11,
    "dec": 12,
    "december": 12,
    "déc": 12,
    "décembre": 12,
    "decembre": 12,
}

# Unicode-aware "letters only" word, so French month names with accents
# (août, février, décembre, ...) match alongside English ones — the
# reference scraper's [A-Za-z] only matched ASCII letters.
_MONTH_WORD = r"[^\W\d_]{3,}"


def _extract_month_day(text):
    """Find a (month_name, day) pair in freeform date text, trying both
    'Month Day' order (e.g. 'Aug 27') and 'Day Month' order (e.g.
    '27 Aug', '28-30 août' — the first day of an embedded range is used).
    Needed because Eventbrite renders the same search differently
    depending on which country domain serves the page — confirmed in
    practice against real captured data: the reference scraper's
    Month-Day-only, English-only pattern silently dropped ~28% of a real
    test run (French month names, and English text in Day-Month order)."""
    text = text or ""
    for m in re.finditer(rf"({_MONTH_WORD})\s+(\d{{1,2}})\b", text):
        if _month_to_num(m.group(1)) is not None:
            return m.group(1), m.group(2)
    for m in re.finditer(
        rf"\b(\d{{1,2}})(?:\s*[-–—]\s*\d{{1,2}})?\s+({_MONTH_WORD})\b", text
    ):
        if _month_to_num(m.group(2)) is not None:
            return m.group(2), m.group(1)
    return None, None


# JS: COMPACT calendar (only bookable dates are shown, each with a time).
# Uses class *substrings* rather than the hashed class names, which change
# on every Eventbrite build.
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

# JS: SCROLLABLE full-grid calendar. Report each cell's day + bookable flag.
# NOTE: "unavailableDateCell" contains the substring "availableDateCell", so
# "bookable" is keyed on enabledDateCell/availableDateCell with an explicit
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


def _category_segment(category):
    c = (category or "").strip().strip("/")
    if c in ("all", "all-events"):
        return "all-events"
    if c.endswith("--events"):
        return c
    return f"{c}--events"


def _month_to_num(name):
    return MONTHS.get((name or "").strip().lower().rstrip("."))


def _parse_month_year(text, ref_year, ref_month):
    """'August 2026' -> (8, 2026); 'August' -> (8, inferred year)."""
    t = (text or "").strip()
    m = re.search(rf"({_MONTH_WORD})\s+(\d{{4}})", t)
    if m:
        return _month_to_num(m.group(1)), int(m.group(2))
    m2 = re.search(_MONTH_WORD, t)
    if m2:
        mn = _month_to_num(m2.group(0))
        if mn:
            year = ref_year if mn >= ref_month else ref_year + 1  # Dec -> Jan rollover
            return mn, year
    return None, None


def _build_iso(month_text, day, ref_year, ref_month):
    mn, yr = _parse_month_year(month_text, ref_year, ref_month)
    digits = re.sub(r"\D", "", str(day))
    if not mn or not digits:
        return ""
    try:
        return date(yr, mn, int(digits)).isoformat()
    except ValueError:
        return ""


def _parse_single_date(text, ref_year, ref_month):
    """'Mon, Aug 31 - 8 PM' -> (iso, time_raw). Also handles 'Day Month'
    order and French month names via _extract_month_day (e.g. 'Thu 27 Aug',
    'sam. 29 août', '28-30 août' — the range's first day is used)."""
    parts = re.split(r"[•·|]", text)
    date_part = parts[0].strip() if parts else text
    time_part = parts[1].strip() if len(parts) > 1 else ""
    iso = ""
    month_name, day = _extract_month_day(date_part)
    if month_name and day:
        iso = _build_iso(month_name, day, ref_year, ref_month)
    return iso, time_part


class EventbriteConnector(BaseConnector):
    SOURCE = "eventbrite"
    REQUIRED_FIELDS = ["source_event_id", "event_name", "date"]

    # Eventbrite's search is per-category, not one combined feed — bare
    # category names, each becomes "<category>--events" in the URL. Add
    # more as you find they're worth searching.
    CATEGORIES = [
        "music",
        "science-and-tech",
        "business",
        "food-and-drink",
        "auto-boat-and-air",
        "charity-and-causes",
        "community",
        "fashion",
        "film-and-media",
        "hobbies",
        "arts",
        "sports-and-fitness",
    ]

    # Eventbrite's /d/<slug>/ search path needs a city-specific slug, same
    # reasoning as Dice's CITY_SLUGS.
    CITY_LOCATION_SLUGS = {
        "Paris": "france--paris",
        "Budapest": "hungary--budapest",
    }

    # Used only as a sanity check when geocoding a bare venue name/address
    # (see MAX_VENUE_DISTANCE_METERS below), same reasoning as Universe's
    # and OpenAgenda's CITY_COORDINATES.
    CITY_COORDINATES = {
        "Paris": (48.8566, 2.3522),
    }

    HEADLESS = False  # Eventbrite was noticeably friendlier with a visible browser
    MAX_PAGES = None  # None = every page
    BLOCK_MEDIA = True  # skip images/fonts/video for speed
    PAGE_PAUSE_MIN, PAGE_PAUSE_MAX = 1.5, 3.0  # jitter between fresh event-page visits
    NAV_TIMEOUT = 45_000

    # If True, the COMPACT calendar's dates are also filtered to the
    # requested window (and the event skipped if none fall inside it).
    # Default False keeps every listed compact-calendar date, matching the
    # reference scraper's own default behavior.
    WINDOW_FILTER_COMPACT = False

    # A bare venue name/address (no separate city confirmation) can match
    # the wrong place entirely when geocoded — same lesson learned from
    # StubHub (a real venue once resolved 400+ km away). Discard a geocoded
    # match that lands further than this from the searched city's center.
    MAX_VENUE_DISTANCE_METERS = 50_000

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    def __init__(self, days_ahead=2):
        self.days_ahead = days_ahead

    def _location_slug(self, city):
        slug = self.CITY_LOCATION_SLUGS.get(city)
        if not slug:
            raise ValueError(
                f"No Eventbrite location slug known for '{city}'. "
                f"Add it to EventbriteConnector.CITY_LOCATION_SLUGS."
            )
        return slug

    def _build_search_url(self, city, cat_seg, page_no, start, end):
        slug = self._location_slug(city)
        return (
            f"{DOMAIN}/d/{slug}/{cat_seg}/"
            f"?page={page_no}&start_date={start:%Y-%m-%d}&end_date={end:%Y-%m-%d}"
        )

    @staticmethod
    def _dismiss_overlays(page):
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

    @staticmethod
    def _scroll_to_bottom(page, max_rounds=30):
        last = -1
        for _ in range(max_rounds):
            page.mouse.wheel(0, 4200)
            page.wait_for_timeout(550)
            height = page.evaluate("document.body.scrollHeight")
            if height == last:
                break
            last = height

    @staticmethod
    def _get_total_pages(page):
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

    @staticmethod
    def _collect_event_links(page):
        """Return [(event_id, url), ...] for every event card on the page."""
        links = page.eval_on_selector_all(
            "a.event-card-link[href*='/e/']",
            "els => els.map(a => ({ id: a.getAttribute('data-event-id') || '', href: a.href }))",
        )
        seen, out = set(), []
        for link in links:
            key = link["id"] or link["href"].split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            out.append((link["id"], link["href"]))
        return out

    @staticmethod
    def _parse_location(page):
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

    @staticmethod
    def _find_calendar(page, timeout_ms=9000):
        """Poll the page and all its frames for either calendar widget."""
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            for ctx in [page, *page.frames]:
                try:
                    if ctx.query_selector("[data-testid='calendar-container']"):
                        return ctx, "compact"
                    if ctx.query_selector(
                        "[data-testid='scrollable-calendar-container']"
                    ):
                        return ctx, "scrollable"
                except Exception:
                    continue
            page.wait_for_timeout(300)
        return None, None

    @staticmethod
    def _ensure_months_loaded(ctx, end, max_scrolls=8):
        """Scroll the scrollable calendar until the window's end month is
        loaded (future months render as empty 'loading' cells until
        scrolled into view)."""
        target = f"{end.strftime('%B')} {end.year}"
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

    def _parse_compact_rows(self, ctx, start, end, ref_year, ref_month):
        """Compact widget: one entry per listed date, WITH time."""
        raw = ctx.evaluate(COMPACT_JS) or []
        rows = []
        for c in raw:
            iso = _build_iso(c["month"], c["day"], ref_year, ref_month)
            if self.WINDOW_FILTER_COMPACT and iso:
                d = date.fromisoformat(iso)
                if not (start <= d <= end):
                    continue
            date_raw = " ".join(
                x for x in (c["weekday"], c["month"], c["day"]) if x
            ).strip()
            rows.append({"date_iso": iso, "date_raw": date_raw, "time_raw": c["time"]})
        return rows

    @staticmethod
    def _parse_scrollable_rows(ctx, start, end, ref_year, ref_month):
        """Scrollable widget: keep only BOOKABLE dates within [start, end],
        DATE ONLY — no time is shown on this widget."""
        months = ctx.evaluate(SCROLLABLE_JS) or []
        rows = []
        for m in months:
            mn, yr = _parse_month_year(m["monthName"], ref_year, ref_month)
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
        seen, uniq = set(), []
        for r in rows:
            if r["date_iso"] in seen:
                continue
            seen.add(r["date_iso"])
            uniq.append(r)
        return uniq

    def _resolve_multiple_dates(self, page, start, end, ref_year, ref_month):
        """Click 'Check availability' and read whichever calendar appears."""
        try:
            page.locator("[data-testid='conversion-bar-checkout-button']").first.click(
                timeout=6000
            )
        except Exception:
            return {"kind": "none", "rows": []}

        ctx, kind = self._find_calendar(page)
        if not ctx:
            return {"kind": "none", "rows": []}

        if kind == "compact":
            return {
                "kind": "compact",
                "rows": self._parse_compact_rows(ctx, start, end, ref_year, ref_month),
            }

        try:
            self._ensure_months_loaded(ctx, end)
        except Exception:
            pass
        return {
            "kind": "scrollable",
            "rows": self._parse_scrollable_rows(ctx, start, end, ref_year, ref_month),
        }

    def _extract_event(self, page, url, start, end, ref_year, ref_month):
        """Returns a list of row dicts (WITHOUT 'category') — an empty list
        means skip this event (nothing bookable in the window). One event
        can legitimately expand into several rows here: a "multiple dates"
        event with a compact calendar gets one row per listed date."""
        page.goto(url, timeout=self.NAV_TIMEOUT, wait_until="domcontentloaded")
        self._dismiss_overlays(page)

        try:
            title = (
                page.locator("[data-testid='event-title']")
                .first.inner_text(timeout=8000)
                .strip()
            )
        except Exception:
            title = ""

        loc = self._parse_location(page)

        event_id = ""
        match = re.search(r"-(\d+)/?$", urlparse(url).path)  # /e/...-<id>
        if match:
            event_id = match.group(1)

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
            res = self._resolve_multiple_dates(page, start, end, ref_year, ref_month)
            if res["rows"]:
                return [dict(base, **r) for r in res["rows"]]
            if res["kind"] == "scrollable":
                print(
                    f"[eventbrite] skipped (no bookable date in window): {title[:50]}"
                )
            else:
                print(f"[eventbrite] skipped (calendar unreadable/empty): {title[:50]}")
            return []

        iso, tm = _parse_single_date(date_text, ref_year, ref_month)
        return [dict(base, date_iso=iso, date_raw=date_text, time_raw=tm)]

    def discover(self, city, country):
        start = date.today()
        end = start + timedelta(days=self.days_ahead)
        ref_year, ref_month = start.year, start.month

        raw_events = []
        # Scoped to this one discover() call only — an event seen in more
        # than one category feed is fetched once and reused, same as the
        # reference scraper. Not persisted across separate dispatcher runs.
        event_cache = {}

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.HEADLESS,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                locale="en-US",
                viewport={"width": 1366, "height": 900},
                user_agent=self.USER_AGENT,
            )
            if self.BLOCK_MEDIA:
                ctx.route(
                    re.compile(
                        r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4|webm)(\?|$)", re.I
                    ),
                    lambda route: route.abort(),
                )
            page = ctx.new_page()
            page.set_default_navigation_timeout(self.NAV_TIMEOUT)

            for category in self.CATEGORIES:
                cat_seg = _category_segment(category)

                page.goto(
                    self._build_search_url(city, cat_seg, 1, start, end),
                    wait_until="domcontentloaded",
                )
                self._dismiss_overlays(page)
                self._scroll_to_bottom(page)
                total = self._get_total_pages(page)
                if self.MAX_PAGES:
                    total = min(total, self.MAX_PAGES)
                print(f"[eventbrite] category '{category}': {total} page(s) reported")

                links = []
                for page_no in range(1, total + 1):
                    if page_no > 1:
                        page.goto(
                            self._build_search_url(city, cat_seg, page_no, start, end),
                            wait_until="domcontentloaded",
                        )
                        self._dismiss_overlays(page)
                    self._scroll_to_bottom(page)
                    found = self._collect_event_links(page)
                    links.extend(found)

                seen, unique = set(), []
                for eid, url in links:
                    key = eid or url.split("?")[0]
                    if key in seen:
                        continue
                    seen.add(key)
                    unique.append((eid, url))
                print(
                    f"[eventbrite] category '{category}': {len(unique)} unique event(s)"
                )

                for i, (eid, url) in enumerate(unique, 1):
                    cache_key = eid or url.split("?")[0]
                    try:
                        if cache_key in event_cache:
                            base_rows = event_cache[cache_key]
                        else:
                            base_rows = self._extract_event(
                                page, url, start, end, ref_year, ref_month
                            )
                            event_cache[cache_key] = base_rows
                            time.sleep(
                                random.uniform(self.PAGE_PAUSE_MIN, self.PAGE_PAUSE_MAX)
                            )

                        for row in base_rows:
                            row = dict(row, category=category)
                            row["_search_city"] = city
                            row["_search_country"] = country
                            raw_events.append(row)

                        if i % 20 == 0:
                            print(
                                f"[eventbrite]   {i}/{len(unique)} event pages visited"
                            )
                    except PWTimeout:
                        print(f"[eventbrite] timeout on {url} — skipped")
                    except Exception as error:
                        print(f"[eventbrite] failed on {url}: {error}")

            browser.close()

        return raw_events

    def fetch(self, raw_event):
        # discover() already scraped full detail for every event.
        return raw_event

    def normalize(self, raw_event):
        venue_name = (raw_event.get("venue") or "").strip() or None
        # "Paris, IDF" -> "Paris" — the region code isn't useful as a city.
        city_text = (raw_event.get("city") or "").strip()
        venue_city = city_text.split(",")[0].strip() if city_text else None
        venue_city = venue_city or raw_event.get("_search_city")
        venue_country = raw_event.get("_search_country")

        latitude = longitude = None
        if venue_name:
            query = f"{venue_name}, {venue_city}, {venue_country}"
            latitude, longitude, geocoded_city, geocoded_country = geocode_place(
                query, name=venue_name, source=self.SOURCE
            )

            # Sanity check: a bare venue name/address with no confirmed
            # city can match the wrong place entirely (see
            # MAX_VENUE_DISTANCE_METERS) — discard an implausible match
            # rather than trust it.
            search_city = raw_event.get("_search_city")
            if latitude is not None and search_city in self.CITY_COORDINATES:
                city_lat, city_lon = self.CITY_COORDINATES[search_city]
                distance = haversine_meters(latitude, longitude, city_lat, city_lon)
                if distance is not None and distance > self.MAX_VENUE_DISTANCE_METERS:
                    latitude = longitude = None
                    geocoded_city = geocoded_country = None

            venue_city = geocoded_city or venue_city
            venue_country = geocoded_country or venue_country

        return {
            "source": self.SOURCE,
            "source_event_id": raw_event.get("event_id"),
            "event_name": raw_event.get("title") or None,
            "date": raw_event.get("date_iso") or None,
            # Time format varies by which Eventbrite locale rendered the
            # page (e.g. "6:30 PM" vs "20:45" on a French-locale page) —
            # kept as the exact raw text rather than risking a reformat
            # that could misparse an ambiguous string, same reasoning as
            # Dice's time field.
            "time": raw_event.get("time_raw") or None,
            "venue_name": venue_name,
            "venue_address": None,
            "venue_city": venue_city,
            "venue_country": venue_country,
            "venue_latitude": latitude,
            "venue_longitude": longitude,
            "type": raw_event.get("category") or None,
            "url": raw_event.get("url"),
            # Extra fields, not part of the common schema used elsewhere
            # yet — preserved since they're free and available.
            "location_raw": raw_event.get("location_raw") or None,
            "date_raw": raw_event.get("date_raw") or None,
        }

    def validate(self, normalized_event):
        missing = [f for f in self.REQUIRED_FIELDS if not normalized_event.get(f)]
        return (len(missing) == 0, missing)
