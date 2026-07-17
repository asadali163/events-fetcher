import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from connector_sdk.core.base_connector import BaseConnector
from core.geocoding import get_coordinates
from core.locations_store import save_location

BASE_URL = "https://www.bandsintown.com/choose-dates/genre/all-genres"

# A saved, logged-in browser profile — created once, by hand, via login.py
# in this same folder. Without it, "View all" hits a login wall and only
# the first page of events is visible; discover() degrades gracefully
# rather than failing when this is missing.
PROFILE_DIR = Path(__file__).parent / "bit_profile"

# Bandsintown's city_id is a GeoNames ID — verified ones only, add more as
# you confirm them (geonames.org, or the original reference script's
# --find-city). Only cities with a confirmed id are usable here.
CITY_GEONAME_IDS = {
    "Paris": ("2988507", "FR"),
    "London": ("2643743", "GB"),
    "Chicago": ("4887398", "US"),
    "Toronto": ("6167865", "CA"),
    "Brussels": ("2800866", "BE"),
    "Lyon": ("2996944", "FR"),
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

# "Jul 17 • 4:00 PM"  /  "Jul 17"
DATE_RE = re.compile(
    r"^(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(?P<day>\d{1,2})"
    r"(?:\s*[•·]\s*(?P<time>\d{1,2}:\d{2}\s*[AP]M))?",
    re.IGNORECASE,
)
EVENT_ID_RE = re.compile(r"/e/(\d+)-")

# Anchors each event card by href pattern (/e/) and text shape, not CSS
# class names — Bandsintown's classes are build hashes that change on every
# deploy, so this is what survives redeploys.
EXTRACT_JS = r"""
() => {
  const DATE_RE = /^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b/i;

  const textNodes = (root) => {
    const out = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          const t = child.textContent.trim();
          if (t) out.push(t);
        } else if (child.nodeType === Node.ELEMENT_NODE) {
          walk(child);
        }
      }
    };
    walk(root);
    return out;
  };

  const anchors = Array.from(document.querySelectorAll('a[href*="/e/"]'));
  const rows = [];

  for (const a of anchors) {
    const texts = textNodes(a);
    const dateIdx = texts.findIndex(t => DATE_RE.test(t));
    if (dateIdx === -1) continue;

    const before = texts.slice(0, dateIdx);
    const after = texts.slice(dateIdx + 1);
    const img = a.querySelector('img');
    const source = a.querySelector('source[srcset]');

    rows.push({
      href: a.href,
      artist: before.length ? before[0] : (img ? img.alt : ''),
      date_raw: texts[dateIdx],
      venue: after.length ? after[0] : '',
      extra: before.slice(1).concat(after.slice(1)),
      image_url: source ? source.srcset.split(' ')[0] : (img ? img.src : ''),
    });
  }
  return rows;
}
"""

DATES_JS = r"""
() => {
  const DATE_RE = /^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b/i;
  const out = [];
  document.querySelectorAll('a[href*="/e/"]').forEach(a => {
    const line = (a.innerText || '').split('\n')
      .map(s => s.trim())
      .find(s => DATE_RE.test(s));
    if (line) out.push(line);
  });
  return out;
}
"""

# Runs on an individual event page. Anchors on the href pattern
# (contains "latitude=" and "longitude=") rather than any class name —
# those are build hashes and change on every deploy, same principle as
# EXTRACT_JS above. Structure around that link (not classes) gives the
# venue name and street address:
#   <div>                          <- outer
#     <a>VENUE NAME</a>            <- first non-city link inside outer
#     <div>                        <- addressContainer (the city link's parent)
#       <span>Street address</span>
#       <a href="...latitude=..&longitude=..">City, Country</a>   <- cityLink
#     </div>
#   </div>
EVENT_LOCATION_JS = r"""
() => {
  const cityLink = document.querySelector('a[href*="latitude="][href*="longitude="]');
  if (!cityLink) return null;

  let latitude = null, longitude = null;
  try {
    const url = new URL(cityLink.href);
    const lat = url.searchParams.get('latitude');
    const lon = url.searchParams.get('longitude');
    latitude = lat !== null ? parseFloat(lat) : null;
    longitude = lon !== null ? parseFloat(lon) : null;
    if (Number.isNaN(latitude)) latitude = null;
    if (Number.isNaN(longitude)) longitude = null;
  } catch (e) {}

  const cityText = (cityLink.textContent || '').trim();

  const addressContainer = cityLink.parentElement;
  let streetAddress = null;
  if (addressContainer) {
    const parts = Array.from(addressContainer.childNodes)
      .filter(n => n !== cityLink)
      .map(n => (n.textContent || '').trim())
      .filter(t => t && t !== ',');
    streetAddress = parts.join(' ').replace(/\s+/g, ' ').trim() || null;
  }

  let venueName = null;
  const outer = addressContainer ? addressContainer.parentElement : null;
  if (outer) {
    const candidates = Array.from(outer.querySelectorAll('a')).filter(a => a !== cityLink);
    if (candidates.length) venueName = candidates[0].textContent.trim() || null;
  }

  return {
    venue_name: venueName,
    street_address: streetAddress,
    city_text: cityText,
    latitude: latitude,
    longitude: longitude,
  };
}
"""


def _clean_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _parse_bandsintown_date(raw, start, end):
    """'Jul 17 • 4:00 PM' -> ('2026-07-17', '16:00'). Bandsintown omits the
    year, so it's inferred from the requested window (handles Dec->Jan
    rollover). Returns (None, "") if unparseable."""
    match = DATE_RE.match(raw.strip())
    if not match:
        return None, ""

    month = MONTHS[match.group("mon")[:3].title()]
    day = int(match.group("day"))

    candidates = []
    for year in (start.year, start.year + 1, start.year - 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue  # e.g. Feb 29 on a non-leap year
    if not candidates:
        return None, ""

    inside = [d for d in candidates if start <= d <= end]
    chosen = (
        inside[0] if inside else min(candidates, key=lambda d: abs((d - start).days))
    )

    time_str = ""
    if match.group("time"):
        try:
            t = datetime.strptime(
                re.sub(r"\s+", " ", match.group("time").strip().upper()), "%I:%M %p"
            )
            time_str = t.strftime("%H:%M")
        except ValueError:
            pass

    return chosen.isoformat(), time_str


def _split_city_country(city_text):
    """'Herk-de-stad, Belgium' -> ('Herk-de-stad', 'Belgium')."""
    if not city_text:
        return None, None
    parts = [p.strip() for p in city_text.rsplit(",", 1)]
    if len(parts) == 2:
        return parts[0] or None, parts[1] or None
    return city_text.strip() or None, None


def _range_labels(start, end):
    out, d, guard = set(), start, 0
    while d <= end and guard < 2000:
        out.add((d.month, d.day))
        d += timedelta(days=1)
        guard += 1
    return out


def _month_day(raw):
    match = DATE_RE.match(raw.strip())
    if not match:
        return None
    return MONTHS[match.group("mon")[:3].title()], int(match.group("day"))


class BandsintownConnector(BaseConnector):
    SOURCE = "bandsintown"
    REQUIRED_FIELDS = ["source_event_id", "event_name", "date"]

    HEADLESS = False  # visible for now to verify behavior — flip back to True when done
    MAX_SCROLLS = 400
    STALL_LIMIT = 4
    SCROLL_DELAY = 1.2
    DETAIL_PAGE_DELAY = 1.0  # politeness pause between batches of event-page visits
    DETAIL_TAB_COUNT = 5  # tabs kept in flight at once when visiting event pages

    def __init__(self, days_ahead=7):
        self.days_ahead = days_ahead

    def _city_geoname(self, city):
        entry = CITY_GEONAME_IDS.get(city)
        if not entry:
            raise ValueError(
                f"No verified GeoNames id for '{city}'. "
                f"Add it to CITY_GEONAME_IDS in connector.py (look one up at geonames.org)."
            )
        return entry

    @staticmethod
    def _build_url(city_id, start, end):
        # /choose-dates/ actually honors the date filter; /all-dates/
        # silently ignores it and returns every future event.
        params = [
            ("came_from", "257"),
            ("calendarTrigger", "false"),
            ("date", f"{start.isoformat()}T00:00:00,{end.isoformat()}T23:00:00"),
            ("city_id", str(city_id)),
        ]
        return f"{BASE_URL}?{urlencode(params)}#search"

    @staticmethod
    def _dismiss_cookies(page):
        for sel in (
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
        ):
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=2500):
                    btn.click()
                    page.wait_for_timeout(600)
                    return
            except Exception:
                continue

    @staticmethod
    def _is_logged_in(page):
        try:
            for sel in ("text=/^Log In$/", "text=/^Sign Up$/"):
                if page.locator(sel).first.is_visible(timeout=1500):
                    return False
        except Exception:
            pass
        return True

    @staticmethod
    def _click_view_all(page):
        for sel in (
            "div:text-is('View all')",
            "text=/^View all$/",
            "button:has-text('View all')",
        ):
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible(timeout=2500):
                    el.scroll_into_view_if_needed()
                    page.wait_for_timeout(400)
                    el.click()
                    page.wait_for_timeout(1500)
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _count_events(page):
        return len(page.evaluate(DATES_JS))

    def _out_of_range_count(self, page, allowed):
        bad = 0
        for raw in page.evaluate(DATES_JS):
            md = _month_day(raw)
            if md and md not in allowed:
                bad += 1
        return bad

    def _scroll_until_exhausted(self, page, allowed):
        """Scroll to bottom, wait for the count to grow, repeat. Safety net:
        if cards outside the requested date range start appearing, the date
        filter isn't being honored (wrong URL path) and this list never
        ends — bail out loudly instead of scrolling forever."""
        stalls = 0
        last = self._count_events(page)

        for i in range(1, self.MAX_SCROLLS + 1):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(
                int((self.SCROLL_DELAY + random.uniform(0, 0.4)) * 1000)
            )

            if stalls == 1:
                self._click_view_all(page)

            now = self._count_events(page)

            if now > last:
                bad = self._out_of_range_count(page, allowed)
                if bad > 5:
                    print(
                        "[bandsintown] stopping: events outside the requested date "
                        "range are appearing — the date filter may not be applied."
                    )
                    return now
                stalls = 0
                last = now
            else:
                stalls += 1
                if stalls >= self.STALL_LIMIT:
                    break

        return last

    def discover(self, city, country):
        city_id, _ = self._city_geoname(city)
        start = date.today()
        end = start + timedelta(days=self.days_ahead)
        url = self._build_url(city_id, start, end)

        with sync_playwright() as p:
            have_auth = PROFILE_DIR.is_dir()
            if have_auth:
                try:
                    ctx = p.chromium.launch_persistent_context(
                        user_data_dir=str(PROFILE_DIR),
                        headless=self.HEADLESS,
                        channel="chrome",
                        locale="en-US",
                    )
                except Exception:
                    ctx = p.chromium.launch_persistent_context(
                        user_data_dir=str(PROFILE_DIR),
                        headless=self.HEADLESS,
                        locale="en-US",
                    )
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            else:
                print(
                    f"[bandsintown] no saved login session at {PROFILE_DIR} — "
                    "scraping signed out, only the first page of results will be "
                    "visible. Run login.py once (by hand) to fix this."
                )
                browser = p.chromium.launch(headless=self.HEADLESS)
                ctx = browser.new_context(locale="en-US", user_agent=USER_AGENT)
                page = ctx.new_page()

            try:
                raw_events = self._scrape(
                    ctx, page, url, city, country, start, end, have_auth
                )
            finally:
                ctx.close()

        return raw_events

    def _save_debug_snapshot(self, page, label):
        """Temporary diagnostic — saves what the page actually looked like,
        so a headless-vs-headed difference (bot detection, a different
        layout, a blocked/challenge page) can be seen instead of guessed
        at. Safe to remove once the headless issue is understood."""
        debug_dir = Path(__file__).parent / "debug_snapshots"
        debug_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = debug_dir / f"{label}_{stamp}.png"
        html_path = debug_dir / f"{label}_{stamp}.html"
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception as error:
            print(f"[bandsintown] could not save screenshot: {error}")
        try:
            html_path.write_text(page.content(), encoding="utf-8")
        except Exception as error:
            print(f"[bandsintown] could not save page HTML: {error}")
        print(
            f"[bandsintown] debug snapshot saved: {screenshot_path.name} / {html_path.name}"
        )

    def _scrape(self, ctx, page, url, city, country, start, end, have_auth):
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_selector('a[href*="/e/"]', timeout=30_000)
        except PWTimeout:
            print(f"[bandsintown] no event cards appeared for {city} — skipping.")
            self._save_debug_snapshot(page, f"no_cards_{city}")
            return []

        self._dismiss_cookies(page)

        if have_auth and not self._is_logged_in(page):
            print(
                "[bandsintown] saved session looks EXPIRED (still seeing 'Log In'). "
                "Re-run login.py."
            )

        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.6)")
        page.wait_for_timeout(800)
        self._click_view_all(page)
        page.wait_for_timeout(1500)

        allowed = _range_labels(start, end)
        self._scroll_until_exhausted(page, allowed)

        raw_rows = page.evaluate(EXTRACT_JS)
        if not raw_rows:
            print(
                f"[bandsintown] event cards were present but extraction found 0 for {city}."
            )
            self._save_debug_snapshot(page, f"empty_extract_{city}")

        events, seen = [], set()
        for row in raw_rows:
            match = EVENT_ID_RE.search(row["href"])
            if not match:
                continue
            event_id = match.group(1)
            if event_id in seen:
                continue
            seen.add(event_id)
            row["event_id"] = event_id
            row["_search_city"] = city
            row["_search_country"] = country
            row["_range_start"] = start.isoformat()
            row["_range_end"] = end.isoformat()
            events.append(row)

        # The listing card's artist/venue text split isn't reliable for
        # every card type (festival listings especially), and never gives
        # coordinates at all. Each event's own page has a structured
        # location block with exact lat/long, so visit each one here —
        # kept inside discover() rather than fetch(), same reasoning as
        # Dice: a fresh browser per event would be far too slow.
        print(
            f"[bandsintown] visiting {len(events)} event pages for precise venue/location "
            f"({self.DETAIL_TAB_COUNT} tabs at a time)..."
        )
        self._fetch_event_locations(ctx, events)

        return events

    def _fetch_event_locations(self, ctx, events):
        """Visit every event's own page for its precise location block,
        using several tabs at once. The trick that makes this genuinely
        concurrent without threads or a second browser instance: goto()
        with wait_until="commit" returns almost immediately (navigation
        started, not finished), so kicking it off on every tab in a batch
        before collecting any of them lets the browser load all of them in
        the background at the same time — same total requests as visiting
        one at a time, just far less wall-clock waiting."""
        tabs = [
            ctx.new_page() for _ in range(min(self.DETAIL_TAB_COUNT, len(events)) or 1)
        ]
        try:
            for batch_start in range(0, len(events), len(tabs)):
                batch = events[batch_start : batch_start + len(tabs)]

                for tab, event in zip(tabs, batch):
                    try:
                        tab.goto(event["href"], wait_until="commit", timeout=30_000)
                    except Exception as error:
                        print(
                            f"[bandsintown] failed to start loading {event['href']}: {error}"
                        )

                for tab, event in zip(tabs, batch):
                    try:
                        tab.wait_for_selector('a[href*="latitude="]', timeout=15_000)
                        tab.wait_for_timeout(300)
                        location = tab.evaluate(EVENT_LOCATION_JS)
                        if location:
                            event.update(location)
                    except PWTimeout:
                        pass  # no location block on this page — normalize() falls back
                    except Exception as error:
                        print(f"[bandsintown] failed to read {event['href']}: {error}")

                done = batch_start + len(batch)
                print(f"[bandsintown]   {done}/{len(events)} event pages visited")
                time.sleep(self.DETAIL_PAGE_DELAY)
        finally:
            for tab in tabs:
                try:
                    tab.close()
                except Exception:
                    pass

    def fetch(self, raw_event):
        # The listing card's text nodes already give everything needed —
        # no separate event-page visit required.
        return raw_event

    def normalize(self, raw_event):
        range_start = date.fromisoformat(raw_event["_range_start"])
        range_end = date.fromisoformat(raw_event["_range_end"])
        event_date, event_time = _parse_bandsintown_date(
            raw_event.get("date_raw", ""), range_start, range_end
        )

        # Prefer the event page's own structured location block (fetched in
        # discover(), stored as venue_name/street_address/city_text/
        # latitude/longitude on raw_event) over the listing card's guess.
        # The listing card's artist/venue split isn't reliable for every
        # card type (festival listings especially reported the artist or
        # festival name as "venue"), and never had coordinates at all —
        # the event page has both, precisely.
        detail_city, detail_country = _split_city_country(raw_event.get("city_text"))

        venue_name = (raw_event.get("venue_name") or "").strip() or None
        if not venue_name:
            venue_name = (
                raw_event.get("venue") or ""
            ).strip() or None  # listing-card fallback

        venue_city = detail_city or raw_event.get("_search_city")
        venue_country = detail_country or raw_event.get("_search_country")
        street_address = raw_event.get("street_address")

        latitude = raw_event.get("latitude")
        longitude = raw_event.get("longitude")

        if latitude is not None and longitude is not None:
            if venue_name:
                save_location(
                    "venue",
                    venue_name,
                    venue_city,
                    venue_country,
                    latitude,
                    longitude,
                    source=self.SOURCE,
                )
        elif venue_name:
            # No coordinate-bearing city link found on the event page (rare —
            # e.g. a TBD venue) — fall back to geocoding by name.
            address = ", ".join(p for p in (venue_name, venue_city, venue_country) if p)
            latitude, longitude = get_coordinates(
                address=address,
                name=venue_name,
                city=venue_city,
                country=venue_country,
                source=self.SOURCE,
            )

        return {
            "source": self.SOURCE,
            "source_event_id": raw_event.get("event_id"),
            "event_name": (raw_event.get("artist") or "").strip() or None,
            "date": event_date,
            "time": event_time,
            "venue_name": venue_name,
            "venue_address": street_address,
            "venue_city": venue_city,
            "venue_country": venue_country,
            "venue_latitude": latitude,
            "venue_longitude": longitude,
            "url": _clean_url(raw_event["href"]) if raw_event.get("href") else None,
            "image_url": raw_event.get("image_url"),
        }

    def validate(self, normalized_event):
        missing = [f for f in self.REQUIRED_FIELDS if not normalized_event.get(f)]
        return (len(missing) == 0, missing)
