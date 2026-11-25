"""
SportsEvents24 connector — fixtures for every sport in one country, from the
free SportsEvents24 API (https://sportsevents24.com/api). No key needed.

Unlike the ticketing sources, this API can't search by city: one request is
always "one country + one sport". So for a city run (e.g. Paris):

  - discover() fetches every sport for the whole country (FR), and returns
    all of it — the dispatcher therefore stores the WHOLE COUNTRY in the raw
    and normalized databases;
  - normalize() geocodes each venue (the API has no coordinates);
  - validate() rejects every event whose venue isn't inside
    boundaries/<City>.kml — and since the dispatcher only sends valid events
    on to deduplication + canonical_events.db, only events inside the city
    boundary reach the canonical (gold) store.
  No change to core/ was needed for that split.

Free open beta limits, per IP address:
  100 data requests / UTC day, 5,000 records / UTC month,
  10 requests / minute, 100 records per page.

This connector stays within them per IP, but when any limit is hit it
automatically switches to the next configured proxy/IP and keeps fetching
until every selected sport is complete (or every proxy is exhausted).
Every page is cached to disk as it arrives, so the next run resumes from
the saved cursor and never pays for a page twice.

Proxies: set SPORTSEVENTS24_PROXIES to a comma-separated list or a path to a
file (one proxy URL per line). Use "none" for the direct connection.
Alternatively pass proxies= to the constructor.

See README.md in this folder for details and known limitations.
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from connector_sdk.core.base_connector import BaseConnector  # noqa: E402
from core.geocoding import geocode_place  # noqa: E402
from core.locations_store import get_location  # noqa: E402

API_BASE = "https://sportsevents24.com/api/v1"

# From the API documentation page.
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

# Appended to geocoding queries so a bare town resolves in the right country.
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

POSTCODE_RE = re.compile(r"^\d{4,5}$")


class AllowanceExhausted(Exception):
    """Daily or monthly free allowance used up on every available proxy/IP."""


class AllProxiesExhausted(Exception):
    """Every configured proxy/IP has hit its allowance."""


class ApiError(Exception):
    def __init__(self, status, body):
        self.status = status
        super().__init__(f"HTTP {status}: {body}")


# Boundary helpers (KML polygon + point-in-polygon) now live in core/boundary.py,
# shared with the daily export; re-exported here so existing imports
# (api_test.py, notebooks) keep working.
from core.boundary import _point_in_ring, _ring, in_boundary, load_boundary  # noqa: E402,F401


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _plain(text):
    """Lowercase, accents stripped — for loose text matching."""
    return (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    )


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _parse_retry_after(value):
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


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_proxy(proxy):
    if not proxy or proxy.lower() in ("none", "direct", ""):
        return None
    proxy = proxy.strip()
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    return {"http": proxy, "https": proxy}


def _load_proxies(value):
    """Comma-separated list or path to a file (one proxy per line)."""
    if not value or not str(value).strip():
        return ["none"]
    value = str(value).strip()
    path = Path(value)
    if path.is_file():
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        return [ln for ln in lines if ln and not ln.startswith("#")] or ["none"]
    return [p.strip() for p in value.split(",") if p.strip()] or ["none"]


def geocode_queries(location, country_name):
    """Most precise first. Locations are free text like
    'GYMNASE DU COMPLEXE SPORTIF, ROUTE DE HAM SOUS VARSBERG, 57890, PORCELETTE'
    — the venue name at the front can make the geocoder miss, so fall back
    to dropping it, then to just postcode + town."""
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


# --------------------------------------------------------------------------- #
# Connector
# --------------------------------------------------------------------------- #


class SportsEvents24Connector(BaseConnector):
    SOURCE = "sportsevents24"
    REQUIRED_FIELDS = ["source_event_id", "event_name", "date"]

    PAGE_SIZE = 100  # API maximum
    MAX_WINDOW_DAYS = 30  # API: `to` at most today + 30 days
    # Soft per-IP share of the monthly allowance. When this many records are
    # pulled on the *current* IP, we rotate to the next proxy and continue.
    # With multiple proxies the connector keeps going until every sport is
    # complete — there is no global hard stop.
    MAX_RECORDS_PER_IP = 1000
    MIN_SECONDS_BETWEEN_REQUESTS = 6.5  # API: 10 requests/minute
    SHORT_RETRY_AFTER_SECONDS = 90  # wait out up to this; longer = rotate IP
    MAX_RETRIES = 3
    CURSOR_LIFETIME = timedelta(hours=23)  # API says 24h; keep a margin
    # If this many venues in a row can't be geocoded at all, assume the
    # geocoder's own daily limit is hit and stop calling it for this run
    # (cached coordinates are still used).
    GEOCODE_GIVE_UP_AFTER_MISSES = 25
    USER_AGENT = "GetEvents/0.1"

    def __init__(self, days_ahead=14, proxies=None):
        if days_ahead > self.MAX_WINDOW_DAYS:
            print(
                f"[{self.SOURCE}] days_ahead={days_ahead} is beyond the API's "
                f"{self.MAX_WINDOW_DAYS}-day limit — using {self.MAX_WINDOW_DAYS}"
            )
        self.days_ahead = max(1, min(days_ahead, self.MAX_WINDOW_DAYS))
        self.city = None
        self.country = None
        self.polygons = None

        # Proxies: explicit arg > env SPORTSEVENTS24_PROXIES > direct ("none")
        if proxies is not None:
            if isinstance(proxies, str):
                self._proxy_list = _load_proxies(proxies)
            else:
                self._proxy_list = list(proxies) or ["none"]
        else:
            self._proxy_list = _load_proxies(os.environ.get("SPORTSEVENTS24_PROXIES"))

        self._proxy_index = 0
        self._exhausted_proxies = set()
        self._current_proxy = self._proxy_list[self._proxy_index]
        self._session = self._make_session(self._current_proxy)
        self._last_request_at = 0.0
        self.requests_remaining = None  # today, this IP
        self.rows_remaining = None  # this month, this IP
        self._proxy_rotations = 0
        self._rows_on_current_ip = 0  # records fetched on the current proxy

        self._geo_memo = {}
        self._geo_misses_in_a_row = 0
        self._geocoding_paused = False

        if len(self._proxy_list) > 1 or self._proxy_list[0].lower() not in (
            "none",
            "direct",
            "",
        ):
            print(
                f"[{self.SOURCE}] proxies loaded: {len(self._proxy_list)} · "
                f"starting with: {self._current_proxy}"
            )

    # ------------------------------------------------------------------ API

    def _make_session(self, proxy):
        session = requests.Session()
        session.headers.update(
            {"User-Agent": self.USER_AGENT, "Accept": "application/json"}
        )
        proxies = _normalize_proxy(proxy)
        if proxies:
            session.proxies.update(proxies)
        return session

    def _alive_proxies(self):
        return [p for p in self._proxy_list if p not in self._exhausted_proxies]

    def _rotate_ip(self, reason="", mark_exhausted=False):
        """Switch to the next non-exhausted proxy/IP and reset per-IP counters."""
        old = self._current_proxy
        if mark_exhausted:
            self._exhausted_proxies.add(old)

        alive = self._alive_proxies()
        if not alive:
            raise AllProxiesExhausted(
                f"All {len(self._proxy_list)} proxy/IP(s) exhausted"
                + (f" ({reason})" if reason else "")
            )

        for _ in range(len(self._proxy_list)):
            self._proxy_index = (self._proxy_index + 1) % len(self._proxy_list)
            candidate = self._proxy_list[self._proxy_index]
            if candidate not in self._exhausted_proxies:
                break
        else:
            raise AllProxiesExhausted("No usable proxies left")

        self._current_proxy = self._proxy_list[self._proxy_index]
        self._session.close()
        self._session = self._make_session(self._current_proxy)
        self._proxy_rotations += 1
        self.requests_remaining = None
        self.rows_remaining = None
        self._rows_on_current_ip = 0
        self._last_request_at = 0.0
        print(
            f"[{self.SOURCE}] ⟳ rotated IP ({old} → {self._current_proxy})"
            + (f"  [{reason}]" if reason else "")
            + (
                f"  | exhausted: {len(self._exhausted_proxies)}/{len(self._proxy_list)}"
                if self._exhausted_proxies
                else ""
            )
        )

    def _throttle(self):
        wait = self.MIN_SECONDS_BETWEEN_REQUESTS - (time.time() - self._last_request_at)
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
        if day and day.isdigit():
            self.requests_remaining = int(day)
        if month and month.isdigit():
            self.rows_remaining = int(month)

    def _get(self, path, params=None):
        """GET with automatic IP rotation on any limit / hard error."""
        url = f"{API_BASE}{path}"
        max_attempts = max(len(self._proxy_list), 1) * (self.MAX_RETRIES + 2)

        for attempt in range(max_attempts):
            # Current IP already out of daily requests → rotate before calling
            if self.requests_remaining is not None and self.requests_remaining <= 0:
                try:
                    self._rotate_ip(
                        reason="no requests left today on this IP", mark_exhausted=True
                    )
                except AllProxiesExhausted:
                    raise AllowanceExhausted(
                        "no API requests left today on any proxy (resets at 00:00 UTC)"
                    )
                continue

            self._throttle()
            try:
                response = self._session.get(url, params=params, timeout=30)
            except requests.RequestException as error:
                if len(self._alive_proxies()) <= 1 and attempt >= self.MAX_RETRIES:
                    raise ApiError(-1, f"network error: {error}") from error
                print(f"[{self.SOURCE}] network error ({error}) — rotating IP")
                try:
                    self._rotate_ip(reason=f"network: {error}")
                except AllProxiesExhausted:
                    raise ApiError(-1, f"network error: {error}") from error
                time.sleep(2)
                continue

            try:
                body = response.json()
            except ValueError:
                body = {"raw": response.text[:300]}
            self._remember_usage(response, body)

            if response.status_code == 200:
                return body

            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                short = (
                    retry_after is not None
                    and retry_after <= self.SHORT_RETRY_AFTER_SECONDS
                )
                if short and attempt < max_attempts - 1:
                    print(
                        f"[{self.SOURCE}] per-minute limit — waiting {retry_after + 1}s"
                    )
                    time.sleep(retry_after + 1)
                    continue

                # Long 429 = this IP's daily/monthly allowance is gone → rotate
                print(
                    f"[{self.SOURCE}] 429 allowance exhausted "
                    f"(Retry-After={retry_after}s) — switching IP"
                )
                try:
                    self._rotate_ip(
                        reason="429 allowance exhausted", mark_exhausted=True
                    )
                except AllProxiesExhausted:
                    raise AllowanceExhausted(
                        f"retry after {retry_after}s"
                        if retry_after is not None
                        else "no Retry-After"
                    )
                time.sleep(1)
                continue

            if response.status_code >= 500:
                if attempt < max_attempts - 1:
                    print(f"[{self.SOURCE}] HTTP {response.status_code} — rotating IP")
                    try:
                        self._rotate_ip(reason=f"HTTP {response.status_code}")
                    except AllProxiesExhausted:
                        if attempt >= self.MAX_RETRIES:
                            raise ApiError(response.status_code, json.dumps(body)[:300])
                        delay = 5 * 3 ** min(attempt, 3)
                        time.sleep(delay)
                        continue
                    time.sleep(3)
                    continue
                raise ApiError(response.status_code, json.dumps(body)[:300])

            raise ApiError(response.status_code, json.dumps(body)[:300])

        raise ApiError(-1, "retries + proxy rotations exhausted")

    # ---------------------------------------------------------------- cache

    def _cache_path(self, date_from, date_to):
        # Project root, so the existing /*.json .gitignore rule covers it.
        # Same file name and format as api_test.py, so the two share pages.
        return (
            PROJECT_ROOT
            / f"sportsevents24_raw_{self.country}_{date_from}_{date_to}.json"
        )

    def _load_cache(self, path, date_from, date_to):
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return {"country": self.country, "from": date_from, "to": date_to, "sports": {}}

    @staticmethod
    def _save_cache(path, cache):
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    @staticmethod
    def _entry(cache, sport_id):
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

    def _is_pending(self, cache, sport_id):
        entry = self._entry(cache, sport_id)
        # An entry that ended in an error is retried on a later run (older
        # api_test.py versions marked those 'complete'; treat them as not).
        return not entry["complete"] or bool(entry.get("error"))

    # ------------------------------------------------------------- fetching

    def _fetch_page(self, cache, path, sport_id, max_rows):
        entry = self._entry(cache, sport_id)
        cursor = entry["next_cursor"]  # resume after an earlier error too
        if cursor and entry["cursor_saved_at"]:
            saved = datetime.fromisoformat(entry["cursor_saved_at"])
            if datetime.now(timezone.utc) - saved > self.CURSOR_LIFETIME:
                print(
                    f"[{self.SOURCE}] {entry['name']}: saved cursor expired — "
                    "restarting from page 1"
                )
                cursor = None
                entry["pages"] = []

        params = {
            "country": self.country,
            "sport_id": sport_id,
            "from": cache["from"],
            "to": cache["to"],
            "limit": max(1, min(self.PAGE_SIZE, max_rows)),
        }
        if cursor:
            params["cursor"] = cursor

        try:
            body = self._get("/events", params)
        except ApiError as error:
            if cursor and error.status == 400:
                # Most likely a rejected cursor: restart this sport next run.
                entry["next_cursor"] = None
                entry["pages"] = []
            # Record the error but leave the sport incomplete, so a later
            # run retries it instead of treating it as done.
            entry["error"] = str(error)
            entry["complete"] = False
            self._save_cache(path, cache)
            raise

        rows = body.get("data") or []
        next_cursor = body.get("nextCursor")
        if not entry["next_cursor"] and entry["pages"] and not cursor:
            entry["pages"] = []  # restarted from page 1 — drop the stale pages
        entry["pages"].append(
            {"fetched_at": _now_iso(), "count": len(rows), "data": rows}
        )
        entry["next_cursor"] = next_cursor
        entry["cursor_saved_at"] = _now_iso() if next_cursor else None
        entry["complete"] = not next_cursor or not rows
        entry["error"] = None
        self._save_cache(path, cache)

        total = sum(p["count"] for p in entry["pages"])
        print(
            f"[{self.SOURCE}] {entry['name']}: page {len(entry['pages'])}, "
            f"{len(rows)} events (total {total}{', done' if entry['complete'] else ''}) · "
            f"allowance left: {self.requests_remaining} requests today, "
            f"{self.rows_remaining} records this month · proxy: {self._current_proxy}"
        )
        return len(rows)

    def _fetch_all(self, cache, path):
        """Round-robin until every sport is complete.

        On any per-IP limit (daily requests, monthly rows, soft
        MAX_RECORDS_PER_IP, long 429) the connector rotates to the next
        proxy and continues. It only stops when:
          - every sport is fully fetched, or
          - every proxy is exhausted.
        """
        print(
            f"[{self.SOURCE}] will keep fetching (rotating IPs on limits) "
            f"until all sports are complete · soft cap per IP: {self.MAX_RECORDS_PER_IP} records"
        )

        spent = 0
        failed_this_run = set()

        while True:
            pending = [
                s
                for s in SPORTS
                if self._is_pending(cache, s) and s not in failed_this_run
            ]
            if not pending:
                print(f"[{self.SOURCE}] ✓ all sports fully fetched")
                return spent, None

            unstarted = [s for s in pending if not self._entry(cache, s)["pages"]]
            # Fair share of the remaining soft per-IP budget among unstarted sports
            remaining_on_ip = max(0, self.MAX_RECORDS_PER_IP - self._rows_on_current_ip)
            first_page_share = (
                max(1, remaining_on_ip // len(unstarted))
                if unstarted and remaining_on_ip
                else None
            )

            for sport_id in pending:
                # --- decide how many rows we may still pull on this IP ---
                left = self.PAGE_SIZE
                if self.rows_remaining is not None:
                    left = min(left, self.rows_remaining)
                left = min(
                    left, max(0, self.MAX_RECORDS_PER_IP - self._rows_on_current_ip)
                )
                if first_page_share is not None and sport_id in unstarted:
                    left = min(left, first_page_share)

                if left <= 0:
                    # Soft per-IP cap or monthly rows exhausted on this IP → rotate
                    reason = (
                        "0 rows remaining this month"
                        if (
                            self.rows_remaining is not None and self.rows_remaining <= 0
                        )
                        else f"soft per-IP cap ({self.MAX_RECORDS_PER_IP}) reached"
                    )
                    print(f"[{self.SOURCE}] {reason} — rotating IP")
                    try:
                        self._rotate_ip(reason=reason, mark_exhausted=True)
                    except AllProxiesExhausted:
                        return spent, "free allowance used up on all proxies"
                    # After rotation, continue the outer while with a fresh IP
                    break

                if self.requests_remaining is not None and self.requests_remaining <= 0:
                    try:
                        self._rotate_ip(
                            reason="no requests left today on this IP",
                            mark_exhausted=True,
                        )
                    except AllProxiesExhausted:
                        return (
                            spent,
                            "no API requests left today on any proxy (resets at 00:00 UTC)",
                        )
                    break

                try:
                    n = self._fetch_page(cache, path, sport_id, left)
                    spent += n
                    self._rows_on_current_ip += n
                except AllowanceExhausted as error:
                    return spent, f"free allowance used up ({error})"
                except AllProxiesExhausted as error:
                    return spent, str(error)
                except ApiError as error:
                    print(
                        f"[{self.SOURCE}] {SPORTS[sport_id]}: {error} — will retry next run"
                    )
                    failed_this_run.add(sport_id)

    # ------------------------------------------------------- BaseConnector

    def discover(self, city, country):
        self.polygons = load_boundary(city)  # raises for an unmapped city
        self.city = city
        self.country = country.upper()

        today = datetime.now(timezone.utc).date()
        date_from = today.isoformat()
        date_to = (today + timedelta(days=self.days_ahead)).isoformat()  # exclusive
        path = self._cache_path(date_from, date_to)
        cache = self._load_cache(path, date_from, date_to)

        try:
            usage = self._get("/account")  # doesn't use the data allowance
            usage = usage.get("usage", usage)
            self.requests_remaining = usage.get("requestsRemaining")
            self.rows_remaining = usage.get("rowsRemaining")
            print(
                f"[{self.SOURCE}] allowance: {self.requests_remaining} requests left today, "
                f"{self.rows_remaining} records left this month"
            )
        except (ApiError, AllowanceExhausted, AllProxiesExhausted) as error:
            print(f"[{self.SOURCE}] couldn't read allowance ({error}) — continuing")

        spent, stop_reason = self._fetch_all(cache, path)

        raw_events, seen = [], set()
        for sport_id in SPORTS:
            entry = cache["sports"].get(str(sport_id))
            for page in (entry or {}).get("pages", []):
                for raw in page["data"]:
                    if raw.get("id") not in seen:
                        seen.add(raw.get("id"))
                        raw_events.append(raw)

        incomplete = [SPORTS[s] for s in SPORTS if self._is_pending(cache, s)]
        print(
            f"[{self.SOURCE}] {spent} new record(s) this run; {len(raw_events)} event(s) "
            f"for {self.country} {date_from}..{date_to} (exclusive) in the cache"
            + (
                f" · {self._proxy_rotations} IP rotation(s)"
                if self._proxy_rotations
                else ""
            )
        )
        if stop_reason or incomplete:
            print(
                f"[{self.SOURCE}] PARTIAL: stopped because {stop_reason or 'some sports failed'}; "
                f"{len(incomplete)} sport(s) not complete. Returning what's cached. The refetch "
                f"log will treat this window as covered — to continue, run again with "
                f"'Re-fetch' ticked; it resumes from the cache without re-paying for pages."
            )
        if not raw_events and stop_reason:
            raise RuntimeError(f"No events fetched: {stop_reason}")

        # Likely-in-city venues first, so they're geocoded first — if the
        # geocoder's own daily limit runs out mid-run, the events that can
        # reach the canonical store are already done.
        city_text = _plain(city)
        raw_events.sort(
            key=lambda e: (
                0
                if re.search(
                    rf"\b{re.escape(city_text)}\b", _plain(e.get("location") or "")
                )
                else 1
            )
        )
        return raw_events

    def fetch(self, raw_event):
        # The list endpoint already returns everything the API has per event.
        return raw_event

    def normalize(self, raw_event):
        sport_name = SPORTS.get(
            raw_event.get("sportId"), f"sport {raw_event.get('sportId')}"
        )
        local = raw_event.get("localStartsAt") or ""
        starts_utc = raw_event.get("startsAt") or ""
        if local:
            event_date, event_time = local[:10], local[11:16]
        else:
            event_date = starts_utc[:10]
            event_time = f"{starts_utc[11:16]} UTC" if starts_utc else ""

        home = (raw_event.get("teamHome") or "").strip()
        away = (raw_event.get("teamAway") or "").strip()
        name = (
            f"{home} vs {away}"
            if home and away
            else (home or away or (raw_event.get("tournament") or "").strip())
        )

        location = (raw_event.get("location") or "").strip()
        lat = lon = venue_city = venue_country = precision = None
        geocoded = self._geocode(location)
        if geocoded:
            lat, lon, venue_city, venue_country, precision = geocoded

        return {
            "source": self.SOURCE,
            "source_event_id": str(raw_event.get("id")),
            "event_name": name,
            "date": event_date,
            "time": event_time,
            "end_date": None,
            "end_time": None,
            "venue_name": location.split(",")[0].strip() if location else None,
            "venue_address": location or None,
            "venue_city": venue_city,
            "venue_country": venue_country,
            "venue_latitude": lat,
            "venue_longitude": lon,
            "type": _slug(sport_name),
            "url": None,  # the API doesn't return an event page link
            # --- sports-specific extras (kept in the normalized/canonical JSON) ---
            "sport": sport_name,
            "tournament": raw_event.get("tournament"),
            "tournament_id": raw_event.get("tournamentId"),
            "team_home": home or None,
            "team_away": away or None,
            "starts_at_utc": starts_utc or None,
            "timezone": raw_event.get("timeZone"),
            "result": raw_event.get("result") or None,
            "status": raw_event.get("category"),
            "geo_precision": precision,
        }

    def validate(self, normalized_event):
        missing = [f for f in self.REQUIRED_FIELDS if not normalized_event.get(f)]
        if not missing and self.polygons is not None:
            lat = normalized_event.get("venue_latitude")
            lon = normalized_event.get("venue_longitude")
            if lat is None or lon is None:
                missing.append(f"coordinates (needed for the {self.city} boundary)")
            elif not in_boundary(lat, lon, self.polygons):
                missing.append(f"venue inside {self.city} boundary")
        return len(missing) == 0, missing

    # ------------------------------------------------------------ geocoding

    def _geocode(self, location):
        """(lat, lon, city, country, precision) or None. Cache-first via
        core.geocoding.geocode_place(); memoized per run so a venue shared by
        many fixtures costs at most one lookup chain."""
        if not location:
            return None
        if location in self._geo_memo:
            return self._geo_memo[location]

        country_name = COUNTRY_NAMES.get(self.country, self.country)
        result = None
        for precision, query in geocode_queries(location, country_name):
            if self._geocoding_paused:
                # Cache only — geocode_place() caches under the query text.
                cached = get_location("venue", query, None, None)
                if cached and cached.get("latitude") is not None:
                    result = (
                        cached["latitude"],
                        cached["longitude"],
                        cached.get("city"),
                        cached.get("country"),
                        precision,
                    )
                    break
                continue
            lat, lon, city, country = geocode_place(query, source=self.SOURCE)
            if lat is not None and lon is not None:
                result = (lat, lon, city, country, precision)
                break

        if not self._geocoding_paused:
            if result:
                self._geo_misses_in_a_row = 0
            else:
                self._geo_misses_in_a_row += 1
                if self._geo_misses_in_a_row >= self.GEOCODE_GIVE_UP_AFTER_MISSES:
                    self._geocoding_paused = True
                    print(
                        f"[{self.SOURCE}] {self._geo_misses_in_a_row} venues in a row couldn't be "
                        f"geocoded — the geocoder's daily limit is probably reached. Using cached "
                        f"coordinates only for the rest of this run."
                    )
        self._geo_memo[location] = result
        return result
