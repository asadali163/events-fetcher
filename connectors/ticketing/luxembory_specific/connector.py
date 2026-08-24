import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from connector_sdk.core.base_connector import BaseConnector
from core.locations_store import save_location

BASE_URL = "https://www.luxembourg-city.com"
LISTING_URL = f"{BASE_URL}/en/what-s-on"

DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
TIME_RE = re.compile(r"\b(\d{1,2}[Hh]\d{2})\b")
WEEKDAY_RE = re.compile(
    r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", re.I
)
# "05.08.2026 - 01.09.2026" (dash can be -, en-dash, em-dash)
RANGE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}\s*[-–—]\s*\d{2}\.\d{2}\.\d{4}")
# https://maps.google.com/maps?q=49.625661,6.160547
MAPS_Q_RE = re.compile(r"[?&]q=(-?\d+\.\d+),\s*(-?\d+\.\d+)")

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


def _is_single_date_text(text):
    """True for single-date cards, False for ranges like '05.08.2026 - 01.09.2026'
    — deliberate scope from the reference scraper: range events (festivals
    spanning weeks) are skipped, only single-date events are kept."""
    if RANGE_RE.search(text):
        return False
    return len(set(DATE_RE.findall(text))) == 1


def _parse_ddmmyyyy(raw):
    """'26.08.2026' -> date(2026, 8, 26), or None if malformed."""
    if not raw:
        return None
    try:
        d, m, y = raw.split(".")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def _reformat_time(raw):
    """'17H30' -> '17:30' — pure separator reformat, the digits themselves
    are never touched."""
    if not raw:
        return ""
    match = re.match(r"^(\d{1,2})[Hh](\d{2})$", raw)
    if not match:
        return raw  # unexpected shape — keep as-is rather than drop it
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _parse_datetime(soup):
    """Find the date/time <p>. Prefer one with BOTH a weekday and a date;
    fall back to the first <p> with a date. Skips range paragraphs."""
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
    """Address text + coordinates, from the div holding the Google Maps
    link — first-party coordinates straight from the page, no geocoding
    API call needed."""
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
                address = ", ".join(
                    s.strip() for s in p.get_text("\n").split("\n") if s.strip()
                )
    return address, lat, lng, maps_url


def _parse_description(soup):
    node = soup.select_one(".description-textimg-formatted")
    if node is None:
        return ""
    for tag in node.select("img, script, style"):
        tag.decompose()
    paras = [p.get_text(" ", strip=True) for p in node.find_all("p")]
    paras = [p for p in paras if p]
    return "\n\n".join(paras) if paras else node.get_text(" ", strip=True)


class LuxembourgCityConnector(BaseConnector):
    SOURCE = "luxembourg_city"
    REQUIRED_FIELDS = ["source_event_id", "event_name", "date"]

    # luxembourg-city.com's "What's On" is a single-city tourism site — it
    # has no other city to search, unlike every other connector.
    SUPPORTED_CITY = "Luxembourg City"

    HEADLESS = False  # If true, the date filter will not be applied to it.
    LOAD_MORE_MAX_ROUNDS = 40
    DETAIL_PAGE_DELAY = 1.5  # politeness pause between event-page visits

    def __init__(self, days_ahead=30):
        self.days_ahead = days_ahead

    def _dismiss_cookies(self, page):
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

    def _open_calendar_panel(self, page):
        try:
            page.get_by_role("button", name=re.compile("Calendar view", re.I)).click(
                timeout=5000
            )
        except Exception:
            pass  # panel may already be open, or the label may differ
        try:
            page.wait_for_selector(".flatpickr-calendar .flatpickr-days", timeout=6000)
        except Exception:
            print("[luxembourg_city] WARNING: flatpickr calendar didn't appear.")

    def _click_flatpickr_day(self, page, d, max_nav=24):
        """Click the day cell for date `d`, navigating forward month-by-month
        with the next-month arrow until that day is on screen. Prefers a
        real day cell over an adjacent-month spillover cell."""
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
            try:
                nxt = page.locator(".flatpickr-next-month:not(.flatpickr-disabled)")
                if not nxt.count():
                    break
                nxt.first.click(timeout=1500)
                page.wait_for_timeout(250)
            except Exception:
                break
        return False

    def _set_date_range(self, page, start, end):
        """Drive the flatpickr range calendar exactly like a person: open
        Calendar view -> click the start day -> click the end day -> click
        'Show the results'. Days are matched by their French aria-label."""
        try:
            page.wait_for_selector(".flatpickr-calendar", state="visible", timeout=6000)
        except Exception:
            try:  # the first Calendar-view click may not have landed — retry
                page.get_by_role(
                    "button", name=re.compile("Calendar view", re.I)
                ).click(timeout=3000)
                page.wait_for_selector(
                    ".flatpickr-calendar", state="visible", timeout=6000
                )
            except Exception:
                print(
                    "[luxembourg_city] WARNING: flatpickr calendar never became "
                    "visible; date filter not applied."
                )
                return

        if not self._click_flatpickr_day(page, start):
            print(f"[luxembourg_city] WARNING: could not click start date {start}.")
        page.wait_for_timeout(350)
        if not self._click_flatpickr_day(page, end):
            print(f"[luxembourg_city] WARNING: could not click end date {end}.")
        page.wait_for_timeout(350)

        labels = re.compile(
            "Show the results|Voir les résultats|" "Afficher les résultats|résultats",
            re.I,
        )
        for role in ("button", "link"):
            try:
                el = page.get_by_role(role, name=labels)
                if el.count() and el.first.is_visible():
                    el.first.click(timeout=3000)
                    page.wait_for_timeout(1800)
                    print(f"[luxembourg_city] date range applied: {start} -> {end}")
                    return
            except Exception:
                pass
        print(
            "[luxembourg_city] WARNING: 'Show the results' button not found; "
            "date range may not apply."
        )

    def _audience_is_adults(self, page):
        for sel in ("#audiences", "select[name='audiences']"):
            try:
                if page.locator(sel).count():
                    return page.eval_on_selector(sel, "el => el.value") == "adults"
            except Exception:
                continue
        return False

    def _select_adults(self, page):
        """Set the audience filter to 'adults' and verify it actually took.
        Raises RuntimeError if it can't be confirmed — this filter matters,
        so this fails loudly rather than silently scraping all audiences
        (the dispatcher already catches discover() exceptions and logs a
        failed fetch, so this doesn't crash the whole run)."""
        selectors = (
            "#audiences",
            "select[name='audiences']",
            ".js-select-audience-events",
        )

        try:
            page.wait_for_selector(selectors[0], timeout=5000)
        except Exception:
            pass

        for sel in selectors:
            loc = page.locator(sel)
            try:
                if not loc.count():
                    continue
                try:
                    loc.first.select_option("adults", timeout=3000)
                except Exception:
                    pass
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
                value = page.eval_on_selector(sel, "el => el.value")
                if value == "adults":
                    return
            except Exception:
                continue

        raise RuntimeError(
            "Could not confirm the 'adults' audience filter — the "
            "<select id='audiences'> markup may have changed."
        )

    def _apply_filters(self, page, days_ahead):
        page.goto(LISTING_URL, wait_until="domcontentloaded")
        self._dismiss_cookies(page)

        today = date.today()
        end = today + timedelta(days=days_ahead)

        self._open_calendar_panel(page)
        self._set_date_range(page, today, end)

        # Audience = adults LAST, so an audience-triggered reload can't drop
        # the date range. The date window is also enforced in Python later
        # as a safety net, but audience can't be re-derived per event, so
        # it must stick here.
        self._select_adults(page)
        page.wait_for_timeout(1800)

        if not self._audience_is_adults(page):
            print(
                "[luxembourg_city] audience filter was lost after reload — re-applying"
            )
            self._select_adults(page)
            page.wait_for_timeout(1500)

    def _load_all_events(self, page):
        """Scroll + click any 'load more' button until the list stops growing."""
        last_height = 0
        for _ in range(self.LOAD_MORE_MAX_ROUNDS):
            clicked = False
            for label in (
                "Load more",
                "Show more",
                "See more",
                "Voir plus",
                "Afficher plus",
                "Charger plus",
                "Plus d'événements",
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

    def _extract_event_links(self, page):
        """For every internal anchor, walk up a few parents to capture the
        card text, and keep it only if that text contains a DD.MM.YYYY
        date. Anchors on the presence of a date in the surrounding text
        rather than any CSS class, since the listing-card markup wasn't
        confirmed precise (inherited from the reference scraper)."""
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

    def _scrape_event(self, page, url):
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

    def discover(self, city, country):
        if city != self.SUPPORTED_CITY:
            raise ValueError(
                f"LuxembourgCityConnector only covers '{self.SUPPORTED_CITY}' "
                f"(luxembourg-city.com's own site scope), not '{city}'."
            )

        today = date.today()
        end = today + timedelta(days=self.days_ahead)

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.HEADLESS, args=["--start-maximized"]
            )
            ctx = browser.new_context(locale="en-US", no_viewport=True)
            page = ctx.new_page()

            self._apply_filters(page, self.days_ahead)
            self._load_all_events(page)

            links = self._extract_event_links(page)
            single = [(u, t) for (u, t) in links if _is_single_date_text(t)]
            print(
                f"[luxembourg_city] {len(links)} candidate link(s), "
                f"{len(single)} look single-date"
            )

            raw_events = []
            for i, (url, _) in enumerate(single, 1):
                try:
                    data = self._scrape_event(page, url)
                except Exception as error:
                    print(f"[luxembourg_city] failed on {url}: {error}")
                    continue

                # Safety net: enforce the date window using the detail
                # page's own date, in case the on-site filter didn't fully
                # apply (same approach as the reference scraper).
                parsed_date = _parse_ddmmyyyy(data["date"])
                if parsed_date and not (today <= parsed_date <= end):
                    continue

                data["_search_city"] = city
                data["_search_country"] = country
                raw_events.append(data)

                if i % 20 == 0:
                    print(f"[luxembourg_city]   {i}/{len(single)} event pages visited")
                time.sleep(self.DETAIL_PAGE_DELAY)

            browser.close()

        return raw_events

    def fetch(self, raw_event):
        # discover() already scraped full detail for every event.
        return raw_event

    def normalize(self, raw_event):
        parsed_date = _parse_ddmmyyyy(raw_event.get("date"))
        event_date = parsed_date.isoformat() if parsed_date else None
        event_time = _reformat_time(raw_event.get("start_time"))
        end_time = _reformat_time(raw_event.get("end_time"))

        venue_address = raw_event.get("address") or None
        venue_city = raw_event.get("_search_city")
        venue_country = raw_event.get("_search_country")

        latitude = longitude = None
        raw_lat, raw_lon = raw_event.get("latitude"), raw_event.get("longitude")
        if raw_lat and raw_lon:
            try:
                latitude, longitude = float(raw_lat), float(raw_lon)
            except ValueError:
                latitude = longitude = None
            else:
                # First-party coordinates straight from the page's own
                # Google Maps link — cache them directly, no geocoding
                # API call needed (same pattern as Bandsintown).
                if venue_address:
                    save_location(
                        "venue",
                        venue_address,
                        venue_city,
                        venue_country,
                        latitude,
                        longitude,
                        source=self.SOURCE,
                    )

        return {
            "source": self.SOURCE,
            "source_event_id": raw_event.get("url"),
            "event_name": raw_event.get("title") or None,
            "date": event_date,
            "time": event_time,
            "end_date": None,  # single-date events only — see _is_single_date_text
            "end_time": end_time,
            # No venue name is separated from the address text anywhere on
            # the page — left None rather than guessing, per this
            # project's convention (same situation as Universe).
            "venue_name": None,
            "venue_address": venue_address,
            "venue_city": venue_city,
            "venue_country": venue_country,
            "venue_latitude": latitude,
            "venue_longitude": longitude,
            "url": raw_event.get("url"),
            # Extra fields, not part of the common schema used elsewhere
            # yet — preserved since they're free and available.
            "weekday": raw_event.get("weekday") or None,
            "description": raw_event.get("description") or None,
            "maps_url": raw_event.get("maps_url") or None,
        }

    def validate(self, normalized_event):
        missing = [f for f in self.REQUIRED_FIELDS if not normalized_event.get(f)]
        return (len(missing) == 0, missing)
