#!/usr/bin/env python3
"""
Songkick metro-area event scraper
==================================

Scrapes events from a Songkick metro-area page (default: Budapest), bounded by
a date range, and extracts a compact set of details per event.

USAGE
-----
    # Next N days from today (single bounded search, like the site's picker).
    # e.g. today = 14 Jul, --window-days 15 -> 14 Jul .. 28 Jul only:
    python songkick_scraper.py --window-days 15

    # An explicit date range (inclusive), YYYY-MM-DD:
    python songkick_scraper.py --from 2026-07-14 --to 2026-07-30

    # All upcoming events:
    python songkick_scraper.py

    # Also write a real .xlsx (immune to any CSV separator issues):
    python songkick_scraper.py --window-days 15 --xlsx

Columns: name, additional_info, location, date, time, genre,
         latitude, longitude, street_address

DEPENDENCIES
------------
    pip install requests beautifulsoup4        # cloudscraper / openpyxl optional
"""

import argparse
import csv
import json
import re
import sys
import time as _time
from datetime import date, timedelta
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup

BASE = "https://www.songkick.com"
DEFAULT_METRO_PATH = "/metro-areas/29047-hungary-budapest"

# Separator for multi-value fields. Deliberately NOT ";" - a semicolon is the
# list separator in many Excel locales, which split fields and shifted columns.
MULTI_SEP = " | "

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Connection": "keep-alive",
}

RETRY_STATUSES = {403, 406, 429, 503}
QUERY_DATE_FORMAT = "%m/%d/%Y"  # what the site's date picker submits


def make_session():
    try:
        import cloudscraper  # pip install cloudscraper

        session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        print("[info] using cloudscraper session", file=sys.stderr)
    except Exception:
        session = requests.Session()
    session.headers.update(HEADERS)
    return session


def fetch(session, url, referer, retries=4, backoff=3.0):
    headers = {"Referer": referer} if referer else {}
    for attempt in range(1, retries + 1):
        resp = session.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp
        if resp.status_code in RETRY_STATUSES and attempt < retries:
            wait = backoff * attempt
            print(
                f"  !! HTTP {resp.status_code} (attempt {attempt}/{retries}); "
                f"retrying in {wait:.0f}s",
                file=sys.stderr,
            )
            _time.sleep(wait)
            continue
        return resp
    return resp


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_query_date(iso_date):
    from datetime import datetime

    return datetime.strptime(iso_date, "%Y-%m-%d").strftime(QUERY_DATE_FORMAT)


def build_page_url(metro_path, page, min_date=None, max_date=None):
    params = []
    if min_date:
        params.append(("filters[minDate]", to_query_date(min_date)))
    if max_date:
        params.append(("filters[maxDate]", to_query_date(max_date)))
    if page > 1:
        params.append(("page", page))
    url = urljoin(BASE, metro_path)
    if params:
        url += "?" + urlencode(params)
    return url


def _num(v):
    """Coerce to float, or None if it isn't a real number."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def clean_geo(lat, lon):
    """Return (lat, lon) as validated floats, or (None, None).

    Guards against bad/missing coords and against a lat/lon that arrive the
    wrong way round: latitude must be within +/-90, longitude within +/-180."""
    lat, lon = _num(lat), _num(lon)
    if lat is None or lon is None:
        return None, None
    lat_ok = -90 <= lat <= 90
    lon_ok = -180 <= lon <= 180
    if lat_ok and lon_ok:
        return lat, lon
    # Latitude out of range but would be valid as a longitude -> they're swapped.
    if not lat_ok and -180 <= lat <= 180 and -90 <= lon <= 90:
        return lon, lat
    return None, None


def clock_time(dt):
    """Return 'HH:MM:SS+ZZZZ' only when a real start time exists.

    Songkick emits a date-only value for events with no announced time (e.g.
    '2026-07-20'); those must yield an empty time, not a date."""
    if not dt or "T" not in dt:
        return ""
    part = dt.split("T", 1)[1].strip()
    # Must begin with an actual clock time.
    return part if re.match(r"^\d{2}:\d{2}", part) else ""


# --------------------------------------------------------------------------- #
# Parsing a single event <li>
# --------------------------------------------------------------------------- #
def parse_event(li):
    data = {}
    data["listing_title"] = li.get("title")  # e.g. "Friday 24 July 2026"

    t = li.find("time")
    data["start_datetime_tz"] = t.get("datetime") if t else None

    # --- Headline + support acts, taken from the listing exactly as displayed.
    # The <strong> holds the headline (for a festival that's the festival name,
    # e.g. "Monolit Festival 2026"); any remaining text is the support lineup.
    headline_html, support_html = None, None
    artists_p = li.select_one("p.artists")
    if artists_p:
        strong = artists_p.find("strong")
        if strong:
            headline_html = strong.get_text(" ", strip=True)
            span = strong.find_parent("span") or artists_p
            full = span.get_text(" ", strip=True)
            rest = full.replace(headline_html, "", 1).strip(" ,;·-–")
            support_html = re.sub(r"\s+", " ", rest).strip()
    data["headline_html"] = headline_html
    data["support_html"] = support_html

    ven = li.select_one("a.venue-link[href]")
    venue_name_html = ven.get_text(strip=True) if ven else None

    city = li.select_one("span.city-name")
    data["city_name"] = city.get_text(strip=True) if city else None

    # --- JSON-LD ---
    jsonld = {}
    script = li.find("script", attrs={"type": "application/ld+json"})
    if script:
        raw = script.string or script.get_text()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                jsonld = next(
                    (
                        o
                        for o in parsed
                        if isinstance(o, dict) and "Event" in str(o.get("@type", ""))
                    ),
                    parsed[0] if parsed else {},
                )
            elif isinstance(parsed, dict):
                jsonld = parsed
        except Exception as e:
            data["jsonld_parse_error"] = str(e)

    data["jsonld_name"] = jsonld.get("name")
    data["start_date"] = jsonld.get("startDate")

    loc = jsonld.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address") or {}
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    geo = loc.get("geo") or {}
    if isinstance(geo, list):
        geo = geo[0] if geo else {}

    data["venue_name"] = loc.get("name") or venue_name_html
    data["street_address"] = addr.get("streetAddress")
    data["locality"] = addr.get("addressLocality")
    data["country"] = addr.get("addressCountry")

    # Read strictly by key, then validate.
    data["latitude"], data["longitude"] = clean_geo(
        geo.get("latitude"), geo.get("longitude")
    )

    performers = jsonld.get("performer") or []
    if isinstance(performers, dict):
        performers = [performers]
    names, genres = [], []
    for p in performers:
        if not isinstance(p, dict):
            continue
        if p.get("name"):
            names.append(p["name"])
        g = p.get("genre") or []
        if isinstance(g, str):
            g = [g]
        genres.extend(g)
    data["performers"] = names
    data["genres"] = list(dict.fromkeys(genres))

    return data


def parse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = [parse_event(li) for li in soup.select("li.event-listings-element")]
    has_next = soup.select_one("div.pagination a.next_page") is not None
    return events, has_next


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def scrape_range(metro_path, min_date, max_date, max_pages, delay):
    session = make_session()
    all_events, page, referer = [], 1, BASE + "/"
    while page <= max_pages:
        url = build_page_url(metro_path, page, min_date, max_date)
        print(f"[page {page}] GET {url}", file=sys.stderr)
        resp = fetch(session, url, referer)
        if resp.status_code != 200:
            print(
                f"  !! HTTP {resp.status_code} after retries; stopping.",
                file=sys.stderr,
            )
            break
        events, has_next = parse_page(resp.text)
        print(f"  -> {len(events)} events", file=sys.stderr)
        if not events:
            break
        all_events.extend(events)
        if not has_next:
            break
        referer = url
        page += 1
        _time.sleep(delay)
    return all_events


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
OUTPUT_COLUMNS = [
    "name",
    "additional_info",
    "location",
    "date",
    "time",
    "genre",
    "latitude",
    "longitude",
    "street_address",
]


def _headline_from_name(full_name):
    """'Parno Graszt @ Budapest Park' -> 'Parno Graszt'"""
    if not full_name:
        return None
    return full_name.split(" @ ")[0].strip()


def slim(e):
    # Name: prefer the displayed headline (correct for festivals), then JSON-LD.
    name = e.get("headline_html") or _headline_from_name(e.get("jsonld_name"))
    performers = e.get("performers") or []
    if not name and performers:
        name = performers[0]

    # Additional info: the displayed support lineup, else JSON-LD performers
    # minus whatever is already the headline.
    additional = e.get("support_html") or ""
    if not additional and performers:
        extra = [
            p
            for p in performers
            if not name or p.strip().lower() != name.strip().lower()
        ]
        additional = MULTI_SEP.join(extra)

    # Location, no URL.
    vname = e.get("venue_name") or ""
    city, country = e.get("locality"), e.get("country")
    if city and country:
        place = f"{city}, {country}"
    elif e.get("city_name"):
        place = e.get("city_name")
    else:
        place = ", ".join([b for b in (city, country) if b])
    location = ", ".join([b for b in (vname, place) if b])

    return {
        "name": name or "",
        "additional_info": additional,
        "location": location,
        "date": e.get("listing_title") or "",
        "time": clock_time(e.get("start_datetime_tz") or e.get("start_date")),
        "genre": MULTI_SEP.join(e.get("genres") or []),
        "latitude": e.get("latitude"),
        "longitude": e.get("longitude"),
        "street_address": e.get("street_address") or "",
    }


def save_outputs(events, out_base, xlsx=False):
    with open(out_base + ".json", "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)

    # QUOTE_ALL: every field is quoted, so no locale's list separator (comma,
    # semicolon, tab) can ever split a cell and shift the columns.
    with open(out_base + ".csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        for e in events:
            w.writerow(
                {k: ("" if e.get(k) is None else e.get(k)) for k in OUTPUT_COLUMNS}
            )

    if xlsx:
        try:
            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            ws.title = "events"
            ws.append(OUTPUT_COLUMNS)
            for e in events:
                ws.append([e.get(k) for k in OUTPUT_COLUMNS])  # lat/lon stay numeric
            wb.save(out_base + ".xlsx")
            print(f"[info] wrote {out_base}.xlsx", file=sys.stderr)
        except ImportError:
            print(
                "[warn] --xlsx needs openpyxl (pip install openpyxl); " "skipped.",
                file=sys.stderr,
            )


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Scrape Songkick metro-area events.")
    ap.add_argument(
        "--metro",
        default=DEFAULT_METRO_PATH,
        help="Metro-area path, e.g. /metro-areas/29047-hungary-budapest",
    )
    ap.add_argument(
        "--from",
        dest="min_date",
        default=None,
        help="Start date, YYYY-MM-DD (default: today with --window-days)",
    )
    ap.add_argument(
        "--to",
        dest="max_date",
        default=None,
        help="End date, YYYY-MM-DD (ignored if --window-days is set)",
    )
    ap.add_argument(
        "--window-days",
        type=int,
        default=0,
        help="Look this many days ahead from the start date, "
        "inclusive (e.g. 15 = today .. today+14).",
    )
    ap.add_argument(
        "--max-pages", type=int, default=100, help="Safety cap on pages (default 100)"
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Seconds between page requests (default 2.0)",
    )
    ap.add_argument(
        "--xlsx",
        action="store_true",
        help="Also write .xlsx (no separator ambiguity at all)",
    )
    ap.add_argument(
        "--out",
        default="songkick_events",
        help="Output filename base (default songkick_events)",
    )
    args = ap.parse_args()

    if args.window_days > 0:
        start = date.fromisoformat(args.min_date) if args.min_date else date.today()
        end = start + timedelta(days=args.window_days - 1)
        min_date, max_date = start.isoformat(), end.isoformat()
        print(
            f"[info] window: {min_date} .. {max_date} ({args.window_days} days)",
            file=sys.stderr,
        )
    else:
        min_date, max_date = args.min_date, args.max_date

    events = scrape_range(args.metro, min_date, max_date, args.max_pages, args.delay)
    slimmed = [slim(e) for e in events]
    save_outputs(slimmed, args.out, xlsx=args.xlsx)
    print(
        f"\nSaved {len(slimmed)} events -> {args.out}.json / {args.out}.csv",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
