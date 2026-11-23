from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

SOURCE = "sportsevents24"
API_BASE = "https://sportsevents24.com/api/v1"
BOUNDARIES_DIR = PROJECT_ROOT / "boundaries"

SPORTS = {
    1: "Football",
    2: "Basketball",
    3: "Ice Hockey",
    4: "Volleyball",
    5: "Rugby",
    6: "Handball",
    7: "Floorball",
    8: "Baseball",
    9: "American Football",
    11: "Cricket",
    13: "Field Hockey",
    14: "Water Polo",
    20: "Netball",
    21: "Australian Rules Football",
    22: "Lacrosse",
    25: "Inline Hockey",
    27: "Futsal",
    42: "Softball",
    66: "Bandy",
    68: "Beach Soccer",
}

COUNTRY_NAMES = {
    "FR": "France",
    "GB": "United Kingdom",
    "US": "United States",
    "CA": "Canada",
    "HU": "Hungary",
    "BE": "Belgium",
    "NL": "Netherlands",
    "LU": "Luxembourg",
}

PAGE_SIZE = 100
MAX_WINDOW_DAYS = 30
MIN_SECONDS_BETWEEN_REQUESTS = 6.5
SHORT_RETRY_AFTER_SECONDS = 90
MAX_RETRIES = 3
CURSOR_LIFETIME = timedelta(hours=23)
USER_AGENT = "GetEvents-api-test/0.1"


# --------------------------------------------------------------------------- #
# API client – rotate IP on limit, keep going until done
# --------------------------------------------------------------------------- #


class AllProxiesExhausted(Exception):
    """Every proxy/IP has hit its allowance."""


class ApiError(Exception):
    def __init__(self, status, body):
        self.status = status
        super().__init__(f"HTTP {status}: {body}")


def parse_retry_after(value):
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
        return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return None


def normalize_proxy(proxy: str | None) -> dict | None:
    if not proxy or proxy.lower() in ("none", "direct", ""):
        return None
    proxy = proxy.strip()
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    return {"http": proxy, "https": proxy}


class SportsEvents24Api:
    def __init__(self, proxies: list[str] | None = None):
        raw = proxies or ["none"]
        # preserve order, drop empty
        self.proxy_list = [p.strip() for p in raw if p and p.strip()]
        if not self.proxy_list:
            self.proxy_list = ["none"]

        self._index = 0
        self.exhausted: set[str] = set()  # proxies that hit long 429
        self.current_proxy = self.proxy_list[self._index]
        self.session = self._make_session(self.current_proxy)

        self._last_request_at = 0.0
        self.requests_remaining = None
        self.rows_remaining = None
        self.requests_made = 0
        self.proxy_rotations = 0
        self.total_rows_fetched = 0

    def _make_session(self, proxy: str) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        proxies = normalize_proxy(proxy)
        if proxies:
            session.proxies.update(proxies)
        return session

    def _alive_proxies(self) -> list[str]:
        return [p for p in self.proxy_list if p not in self.exhausted]

    def rotate_ip(self, reason: str = "", mark_exhausted: bool = False):
        """Switch to the next non-exhausted proxy. Optionally mark current as dead."""
        old = self.current_proxy
        if mark_exhausted:
            self.exhausted.add(old)

        alive = self._alive_proxies()
        if not alive:
            raise AllProxiesExhausted(
                f"All {len(self.proxy_list)} proxy/IP(s) exhausted. "
                f"Last reason: {reason or 'allowance limit'}"
            )

        # pick next alive proxy after current position
        start = self._index
        for _ in range(len(self.proxy_list)):
            self._index = (self._index + 1) % len(self.proxy_list)
            candidate = self.proxy_list[self._index]
            if candidate not in self.exhausted:
                break
        else:
            raise AllProxiesExhausted("No usable proxies left")

        self.current_proxy = self.proxy_list[self._index]
        self.session.close()
        self.session = self._make_session(self.current_proxy)
        self.proxy_rotations += 1
        self.requests_remaining = None
        self.rows_remaining = None
        self._last_request_at = 0.0
        print(
            f"    ⟳ Rotated IP  ({old} → {self.current_proxy})"
            + (f"  [{reason}]" if reason else "")
            + (
                f"  | exhausted so far: {len(self.exhausted)}/{len(self.proxy_list)}"
                if self.exhausted
                else ""
            )
        )

    def _throttle(self):
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.time() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.time()

    def _remember_usage(self, response, body):
        usage = body.get("usage") if isinstance(body, dict) else None
        if isinstance(usage, dict):
            self.requests_remaining = usage.get(
                "requestsRemaining", self.requests_remaining
            )
            self.rows_remaining = usage.get("rowsRemaining", self.rows_remaining)
            return
        day = response.headers.get("x-ratelimit-remaining-day")
        month = response.headers.get("x-records-remaining-month")
        if day is not None and day.isdigit():
            self.requests_remaining = int(day)
        if month is not None and month.isdigit():
            self.rows_remaining = int(month)

    def get(self, path, params=None):
        url = f"{API_BASE}{path}"
        # generous attempts: retries × number of proxies
        max_attempts = max(len(self.proxy_list), 1) * (MAX_RETRIES + 2)

        for attempt in range(max_attempts):
            # current IP already out of daily requests → rotate before calling
            if self.requests_remaining is not None and self.requests_remaining <= 0:
                self.rotate_ip(
                    reason="no requests left today on this IP", mark_exhausted=True
                )
                continue

            self._throttle()
            try:
                response = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as error:
                print(f"    network error ({error}) — rotating IP")
                try:
                    self.rotate_ip(reason=f"network: {error}")
                except AllProxiesExhausted:
                    raise
                time.sleep(2)
                continue

            self.requests_made += 1
            try:
                body = response.json()
            except ValueError:
                body = {"raw": response.text[:500]}
            self._remember_usage(response, body)

            if response.status_code == 200:
                return body

            if response.status_code == 429:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                short = (
                    retry_after is not None and retry_after <= SHORT_RETRY_AFTER_SECONDS
                )

                if short:
                    wait = (retry_after or 10) + 1
                    print(f"    429 (per-minute) — wait {wait}s, same IP")
                    time.sleep(wait)
                    continue

                # long 429 = this IP's daily/monthly allowance is gone
                print(
                    f"    429 (allowance exhausted, Retry-After={retry_after}s) — switch IP"
                )
                self.rotate_ip(reason="429 allowance exhausted", mark_exhausted=True)
                time.sleep(1)
                continue

            if response.status_code >= 500:
                print(f"    HTTP {response.status_code} — rotating IP")
                try:
                    self.rotate_ip(reason=f"HTTP {response.status_code}")
                except AllProxiesExhausted:
                    raise
                time.sleep(3)
                continue

            raise ApiError(response.status_code, json.dumps(body)[:300])

        raise ApiError(-1, "retries + proxy rotations exhausted")

    def account(self):
        body = self.get("/account")
        usage = body.get("usage", body) if isinstance(body, dict) else {}
        if isinstance(usage, dict):
            self.requests_remaining = usage.get(
                "requestsRemaining", self.requests_remaining
            )
            self.rows_remaining = usage.get("rowsRemaining", self.rows_remaining)
        return usage


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def cache_path(country, date_from, date_to):
    return PROJECT_ROOT / f"sportsevents24_raw_{country}_{date_from}_{date_to}.json"


def load_cache(path, country, date_from, date_to):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"country": country, "from": date_from, "to": date_to, "sports": {}}


def save_cache(path, cache):
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def sport_entry(cache, sport_id):
    return cache["sports"].setdefault(
        str(sport_id),
        {
            "name": SPORTS[sport_id],
            "pages": [],
            "next_cursor": None,
            "cursor_saved_at": None,
            "complete": False,
            "error": None,
        },
    )


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Fetching – keep going until every sport is complete
# --------------------------------------------------------------------------- #


def fetch_page(api, cache, path, sport_id, max_rows):
    entry = sport_entry(cache, sport_id)
    cursor = entry["next_cursor"]
    if cursor and entry["cursor_saved_at"]:
        saved = datetime.fromisoformat(entry["cursor_saved_at"])
        if datetime.now(timezone.utc) - saved > CURSOR_LIFETIME:
            print(
                f"  [{entry['name']}] saved cursor expired — restarting this sport from page 1"
            )
            cursor = None

    params = {
        "country": cache["country"],
        "sport_id": sport_id,
        "from": cache["from"],
        "to": cache["to"],
        "limit": max(1, min(PAGE_SIZE, max_rows)),
    }
    if cursor:
        params["cursor"] = cursor

    try:
        body = api.get("/events", params)
    except AllProxiesExhausted:
        raise
    except ApiError as error:
        if cursor and error.status == 400:
            print(
                f"  [{entry['name']}] cursor rejected ({error}) — will restart this sport"
            )
            entry["next_cursor"] = None
            save_cache(path, cache)
            return 0
        entry["error"] = str(error)
        entry["complete"] = True
        save_cache(path, cache)
        print(f"  [{entry['name']}] failed: {error}")
        return 0

    rows = body.get("data") or []
    next_cursor = body.get("nextCursor")
    entry["pages"].append({"fetched_at": now_iso(), "count": len(rows), "data": rows})
    entry["next_cursor"] = next_cursor
    entry["cursor_saved_at"] = now_iso() if next_cursor else None
    entry["complete"] = not next_cursor or not rows
    entry["error"] = None
    save_cache(path, cache)

    total = sum(p["count"] for p in entry["pages"])
    print(
        f"  [{entry['name']}] page {len(entry['pages'])}: {len(rows)} events "
        f"(sport total {total}{', done' if entry['complete'] else ', more to fetch'}) · "
        f"left today: {api.requests_remaining} req, this month: {api.rows_remaining} rows "
        f"| proxy: {api.current_proxy}"
    )
    return len(rows)


def fetch_all(api, cache, path, sport_ids, global_budget: int | None):
    """
    Round-robin until every sport is complete.
    Rotates IP on limits. Only stops early if:
      - global_budget is reached, or
      - all proxies are exhausted.
    """
    spent = 0
    pass_no = 0

    while True:
        pending = [s for s in sport_ids if not sport_entry(cache, s)["complete"]]
        if not pending:
            print("\n✓ All selected sports fully fetched.")
            return spent

        pass_no += 1
        print(f"\nPass {pass_no}: {len(pending)} sport(s) still incomplete")

        unstarted = [s for s in pending if not sport_entry(cache, s)["pages"]]
        # share remaining global budget fairly among unstarted sports
        first_page_share = None
        if unstarted and global_budget is not None:
            remaining_budget = max(0, global_budget - spent)
            first_page_share = (
                max(1, remaining_budget // len(unstarted)) if remaining_budget else 0
            )

        for sport_id in pending:
            if global_budget is not None and spent >= global_budget:
                print(
                    f"\nGlobal record budget ({global_budget}) reached — stopping. "
                    "Re-run with a higher --max-records (or omit it) to continue."
                )
                return spent

            # how many rows we are allowed to request on this page
            left = PAGE_SIZE
            if global_budget is not None:
                left = min(left, global_budget - spent)
            if api.rows_remaining is not None:
                left = min(left, api.rows_remaining)
            if first_page_share is not None and sport_id in unstarted:
                left = min(left, first_page_share)

            if left <= 0:
                # this IP is out of monthly rows → force rotation and retry later
                if api.rows_remaining is not None and api.rows_remaining <= 0:
                    print("    current IP has 0 rows left this month — rotating")
                    try:
                        api.rotate_ip(
                            reason="0 rows remaining this month", mark_exhausted=True
                        )
                    except AllProxiesExhausted:
                        print("\nAll proxies exhausted — stopping. Progress is cached.")
                        return spent
                    continue
                # global budget exhausted
                print("\nNo more rows allowed under current budget — stopping.")
                return spent

            try:
                n = fetch_page(api, cache, path, sport_id, left)
                spent += n
                api.total_rows_fetched += n
            except AllProxiesExhausted as e:
                print(f"\n{e}")
                print("Progress is cached. Add more proxies or re-run later.")
                return spent

    return spent


# --------------------------------------------------------------------------- #
# Normalization / geocoding / boundary / output (unchanged logic)
# --------------------------------------------------------------------------- #


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def normalize(event, sport_name):
    local = event.get("localStartsAt") or ""
    starts_utc = event.get("startsAt") or ""
    if local:
        event_date, event_time = local[:10], local[11:16]
    else:
        event_date, event_time = starts_utc[:10], (
            starts_utc[11:16] + " UTC" if starts_utc else ""
        )

    home = (event.get("teamHome") or "").strip()
    away = (event.get("teamAway") or "").strip()
    if home and away:
        name = f"{home} vs {away}"
    else:
        name = home or away or (event.get("tournament") or "").strip()

    location = (event.get("location") or "").strip()
    return {
        "source": SOURCE,
        "source_event_id": str(event.get("id")),
        "event_name": name,
        "date": event_date,
        "time": event_time,
        "end_date": None,
        "end_time": None,
        "venue_name": location.split(",")[0].strip() if location else None,
        "venue_address": location or None,
        "venue_city": None,
        "venue_country": None,
        "venue_latitude": None,
        "venue_longitude": None,
        "type": slug(sport_name),
        "url": None,
        "sport": sport_name,
        "sport_id": event.get("sportId"),
        "tournament": event.get("tournament"),
        "tournament_id": event.get("tournamentId"),
        "team_home": home or None,
        "team_away": away or None,
        "starts_at_utc": starts_utc or None,
        "timezone": event.get("timeZone"),
        "timezone_confidence": event.get("timeZoneConfidence"),
        "result": event.get("result") or None,
        "status": event.get("category"),
        "country_code": event.get("country"),
        "updated_at": event.get("updatedAt"),
        "geo_precision": None,
    }


def validate(event):
    missing = [f for f in ("source_event_id", "event_name", "date") if not event.get(f)]
    return not missing, missing


POSTCODE_RE = re.compile(r"^\d{4,5}$")


def geocode_queries(location, country_name):
    parts = [p.strip() for p in location.split(",") if p.strip()]
    suffix = f", {country_name}" if country_name else ""
    queries = [("address", ", ".join(parts) + suffix)]
    if len(parts) > 2:
        queries.append(("street", ", ".join(parts[1:]) + suffix))
    postcode = next((p for p in parts if POSTCODE_RE.match(p)), None)
    town = parts[-1] if parts and not POSTCODE_RE.match(parts[-1]) else None
    if postcode and town:
        queries.append(("town", f"{postcode} {town}{suffix}"))
    elif town and len(parts) > 1:
        queries.append(("town", f"{town}{suffix}"))
    seen, unique = set(), []
    for precision, query in queries:
        if query not in seen:
            seen.add(query)
            unique.append((precision, query))
    return unique


def geocode_events(events, country_code):
    from core.geocoding import geocode_place

    country_name = COUNTRY_NAMES.get(country_code, country_code)
    memo = {}
    locations = sorted({e["venue_address"] for e in events if e["venue_address"]})
    print(f"\nGeocoding {len(locations)} unique location(s)…")
    for i, location in enumerate(locations, 1):
        result = None
        for precision, query in geocode_queries(location, country_name):
            lat, lon, city, country = geocode_place(query, source=SOURCE)
            if lat is not None and lon is not None:
                result = (lat, lon, city, country, precision)
                break
        memo[location] = result
        if i % 25 == 0 or i == len(locations):
            found = sum(1 for r in memo.values() if r)
            print(f"  {i}/{len(locations)} done, {found} found")

    for event in events:
        result = memo.get(event["venue_address"])
        if result:
            lat, lon, city, country, precision = result
            event.update(
                venue_latitude=lat,
                venue_longitude=lon,
                venue_city=city,
                venue_country=country,
                geo_precision=precision,
            )


def _ring(coordinates_text):
    ring = []
    for triple in coordinates_text.split():
        lon, lat = triple.split(",")[:2]
        ring.append((float(lon), float(lat)))
    return ring


def load_boundary(city):
    path = BOUNDARIES_DIR / f"{city}.kml"
    if not path.exists():
        raise FileNotFoundError(f"No boundary file at {path}")
    root = ET.parse(path).getroot()
    polygons = []
    for polygon in root.iter():
        if not polygon.tag.endswith("Polygon"):
            continue
        outer, holes = None, []
        for boundary in polygon:
            coords = next(
                (c.text for c in boundary.iter() if c.tag.endswith("coordinates")), None
            )
            if not coords:
                continue
            if boundary.tag.endswith("outerBoundaryIs"):
                outer = _ring(coords)
            elif boundary.tag.endswith("innerBoundaryIs"):
                holes.append(_ring(coords))
        if outer:
            polygons.append((outer, holes))
    if not polygons:
        raise ValueError(f"No polygon found in {path}")
    return polygons


def _point_in_ring(lon, lat, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            x_cross = xi + (lat - yi) * (xj - xi) / (yj - yi)
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def in_boundary(lat, lon, polygons):
    if lat is None or lon is None:
        return False
    for outer, holes in polygons:
        if _point_in_ring(lon, lat, outer) and not any(
            _point_in_ring(lon, lat, h) for h in holes
        ):
            return True
    return False


CSV_COLUMNS = [
    "date",
    "time",
    "sport",
    "event_name",
    "tournament",
    "team_home",
    "team_away",
    "venue_name",
    "venue_address",
    "venue_city",
    "venue_country",
    "venue_latitude",
    "venue_longitude",
    "geo_precision",
    "status",
    "result",
    "timezone",
    "starts_at_utc",
    "source_event_id",
    "tournament_id",
]


def write_outputs(events, stem):
    json_path = PROJECT_ROOT / f"{stem}.json"
    csv_path = PROJECT_ROOT / f"{stem}.csv"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(events)
    print(f"  {len(events):5d} events → {json_path.name} / {csv_path.name}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def load_proxies(value: str | None) -> list[str]:
    if not value:
        return ["none"]
    path = Path(value)
    if path.is_file():
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        return [ln for ln in lines if ln and not ln.startswith("#")] or ["none"]
    return [p.strip() for p in value.split(",") if p.strip()] or ["none"]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Fetch SportsEvents24 data. On rate-limit / allowance exhausted, "
        "automatically switch IP (proxy) and continue until all selected sports are complete.",
    )
    parser.add_argument("--country", default="FR", help="ISO code (default: FR)")
    parser.add_argument(
        "--city",
        default="Paris",
        help="boundary file name in boundaries/ (default: Paris)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help=f"window from today, max {MAX_WINDOW_DAYS} (default: 14)",
    )
    parser.add_argument(
        "--sports",
        nargs="+",
        type=int,
        metavar="ID",
        help="sport IDs to fetch (default: all). "
        + ", ".join(f"{k}={v}" for k, v in SPORTS.items()),
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=None,
        help="optional safety ceiling for this run (total rows across all IPs). "
        "Omit to keep going until every sport is fully fetched.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="no API calls — only re-process the cache",
    )
    parser.add_argument(
        "--no-geocode",
        action="store_true",
        help="skip geocoding (and the boundary filter)",
    )
    parser.add_argument(
        "--account", action="store_true", help="print remaining allowance and exit"
    )
    parser.add_argument(
        "--proxies",
        default=None,
        help="Comma-separated proxies or path to a file (one per line). "
        'Example: "http://user:pass@1.2.3.4:8080,http://user:pass@5.6.7.8:8080". '
        'Use "none" for your real IP.',
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    country = args.country.upper()
    if not 1 <= args.days <= MAX_WINDOW_DAYS:
        sys.exit(f"--days must be between 1 and {MAX_WINDOW_DAYS}")
    sport_ids = args.sports or list(SPORTS)
    unknown = [s for s in sport_ids if s not in SPORTS]
    if unknown:
        sys.exit(f"Unknown sport id(s): {unknown}. Known: {sorted(SPORTS)}")

    today = datetime.now(timezone.utc).date()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=args.days)).isoformat()
    path = cache_path(country, date_from, date_to)
    cache = load_cache(path, country, date_from, date_to)

    proxies = load_proxies(args.proxies)
    api = SportsEvents24Api(proxies=proxies)

    if args.account:
        print(json.dumps(api.account(), indent=2))
        print(f"(using proxy: {api.current_proxy})")
        return 0

    print(
        f"{country} · {len(sport_ids)} sport(s) · {date_from} → {date_to} (exclusive) · cache: {path.name}"
    )
    print(f"Proxies: {len(proxies)} loaded · starting with: {api.current_proxy}")
    if args.max_records is None:
        print(
            "No --max-records set → will keep fetching (and rotating IPs) until everything is complete."
        )
    else:
        print(f"Global safety ceiling this run: {args.max_records} records")

    if not args.offline:
        try:
            usage = api.account()
            print(
                f"Allowance on current IP: {usage.get('requestsRemaining')} requests left today, "
                f"{usage.get('rowsRemaining')} records left this month."
            )
        except AllProxiesExhausted as e:
            print(e)
            return 1

        spent = fetch_all(api, cache, path, sport_ids, args.max_records)
        print(
            f"\nFetched {spent} new record(s) in {api.requests_made} request(s) "
            f"({api.proxy_rotations} IP rotation(s), {len(api.exhausted)} proxy(ies) marked exhausted)."
        )

    # Build events from cache
    events, seen, invalid = [], set(), 0
    for sport_id in sport_ids:
        entry = cache["sports"].get(str(sport_id))
        if not entry:
            continue
        for page in entry["pages"]:
            for raw in page["data"]:
                if raw.get("id") in seen:
                    continue
                seen.add(raw.get("id"))
                event = normalize(raw, entry["name"])
                is_valid, _ = validate(event)
                if is_valid:
                    events.append(event)
                else:
                    invalid += 1
    print(
        f"\n{len(events)} unique valid event(s) in cache ({invalid} invalid skipped)."
    )
    if not events:
        return 0

    city_events = []
    if not args.no_geocode:
        geocode_events(events, country)
        polygons = load_boundary(args.city)
        city_events = [
            e
            for e in events
            if in_boundary(e["venue_latitude"], e["venue_longitude"], polygons)
        ]

    events.sort(key=lambda e: (e["date"], e["time"], e["sport"]))
    city_events.sort(key=lambda e: (e["date"], e["time"], e["sport"]))

    print("\nWriting outputs:")
    write_outputs(events, f"sportsevents24_events_{country.lower()}")
    if not args.no_geocode:
        write_outputs(
            city_events, f"sportsevents24_events_{args.city.lower().replace(' ', '_')}"
        )

    print(
        f"\n{'Sport':26} {'events':>7} {'w/ location':>12} {'geocoded':>9} {'in ' + args.city:>10}  status"
    )
    for sport_id in sport_ids:
        entry = cache["sports"].get(str(sport_id))
        sport_events = [e for e in events if e["sport_id"] == sport_id]
        if not entry:
            status = "not fetched"
        elif entry.get("error"):
            status = f"error: {entry['error'][:40]}"
        else:
            status = "complete" if entry["complete"] else "partial (re-run to continue)"
        print(
            f"{SPORTS[sport_id]:26} {len(sport_events):7d} "
            f"{sum(1 for e in sport_events if e['venue_address']):12d} "
            f"{sum(1 for e in sport_events if e['venue_latitude'] is not None):9d} "
            f"{sum(1 for e in city_events if e['sport_id'] == sport_id):10d}  {status}"
        )

    if not args.offline:
        print(
            f"\nFinal: {api.requests_remaining} requests left on last IP, "
            f"{api.rows_remaining} rows left this month. "
            f"Rotations: {api.proxy_rotations}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
