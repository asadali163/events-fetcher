#!/usr/bin/env python3
r"""
Bandsintown city-listing scraper.

IMPORTANT -- URL PATH: use /choose-dates/, NOT /all-dates/. The /all-dates/
path silently IGNORES the ?date= filter and returns every future event, so a
scrape against it scrolls forever. This script uses /choose-dates/ and also
validates the returned dates, stopping early if the filter isn't applied.

Note: Bandsintown's robots.txt disallows /choose-dates/. That's their stated
preference for automated clients; weigh it against their Terms for your use
case before running this at any volume.

Flow:
  1. Open /all-dates/genre/all-genres with a city_id + date range filter
  2. Dismiss the cookie banner
  3. Click "View all"  (requires a logged-in session)
  4. Scroll to bottom repeatedly until no new events load
  5. Extract every event card, dedupe, write JSON + CSV

AUTH: "View all" is gated behind a login prompt. This script never asks for
or stores your password -- you log in ONCE by hand in a real browser window
and the session is reused afterwards.

GOOGLE SIGN-IN: Google refuses to accept credentials in an automated browser
("This browser or app may not be secure"). Two ways around it:

  A) PERSISTENT PROFILE (default, try this first)
       python bandsintown_scraper.py --login
     Drives your REAL Chrome install via a persistent profile dir, which
     Google usually accepts. Log in once; the profile stays logged in.

  B) ATTACH TO YOUR OWN CHROME (bulletproof fallback)
     Quit Chrome completely, then relaunch it yourself:

       macOS:
         /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
           --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-bit
       Windows:
         "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
           --remote-debugging-port=9222 --user-data-dir=C:\chrome-bit
       Linux:
         google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-bit

     Log into Bandsintown with Google in that window by hand. Then:

       python bandsintown_scraper.py --cdp http://localhost:9222 --city-id 2988507 --days 7

     You started the browser, so Google sees nothing automated. Playwright
     just attaches to the tab.

Design note: Bandsintown's class names (e.g. "HsqHp2xM2FkfSdjy1mlU") are
CSS-module build hashes and change on every deploy. This scraper deliberately
does NOT use them. It anchors on href pattern (/e/) and text shape instead,
which survives redeploys.

CITIES: Bandsintown's city_id is just a GeoNames ID (2988507 = Paris, FR), so
any city in the GeoNames database works. Beware duplicate names -- "Paris" is
also Paris, Texas (4717560). Look one up with --find-city, or read it off a
geonames.org URL:  geonames.org/2988507/paris.html  ->  2988507

Usage:
    pip install playwright
    playwright install chromium

    # STEP 1 -- one time only: log in by hand
    python bandsintown_scraper.py --login

    # STEP 2 -- scrape
    python bandsintown_scraper.py --city-id paris --days 7
    python bandsintown_scraper.py --city-id 2988507 --days 7      # same thing
    python bandsintown_scraper.py --days 7                        # account's city

    # Several cities, one browser session, one login
    python bandsintown_scraper.py --city-id paris,berlin,london --days 7 --split

    # Find an id for anywhere else
    python bandsintown_scraper.py --find-city "Glasgow" --geonames-user YOURNAME
    python bandsintown_scraper.py --list-cities

    # Explicit range
    python bandsintown_scraper.py --city-id berlin \
        --start 2026-07-17 --end 2026-07-21 --strict-dates

    # Run invisibly (for cron / servers) -- needs a valid session
    python bandsintown_scraper.py --city-id paris --days 7 --headless

Sessions expire eventually. When they do, the script says so and you re-run
--login. Treat auth.json and bit_profile/ like passwords: they grant access
to your account. Add both to .gitignore.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://www.bandsintown.com/choose-dates/genre/all-genres"
HOME = "https://www.bandsintown.com/?came_from=257"
AUTH_DEFAULT = "auth.json"
PROFILE_DEFAULT = "bit_profile"

# Bandsintown's city_id IS a GeoNames ID (2988507 = Paris, FR). That means any
# city in the GeoNames database works -- look one up at geonames.org, or use
# --find-city below. Handy shortcuts, all verified GeoNames IDs:
CITIES = {
    "paris": (2988507, "Paris, FR"),
    "london": (2643743, "London, GB"),
    "berlin": (2950159, "Berlin, DE"),
    "madrid": (3117735, "Madrid, ES"),
    "barcelona": (3128760, "Barcelona, ES"),
    "amsterdam": (2759794, "Amsterdam, NL"),
    "brussels": (2800866, "Brussels, BE"),
    "rome": (3169070, "Rome, IT"),
    "milan": (3173435, "Milan, IT"),
    "lisbon": (2267057, "Lisbon, PT"),
    "vienna": (2761369, "Vienna, AT"),
    "zurich": (2657896, "Zurich, CH"),
    "prague": (3067696, "Prague, CZ"),
    "warsaw": (756135, "Warsaw, PL"),
    "copenhagen": (2618425, "Copenhagen, DK"),
    "stockholm": (2673730, "Stockholm, SE"),
    "oslo": (3143244, "Oslo, NO"),
    "dublin": (2964574, "Dublin, IE"),
    "manchester": (2643123, "Manchester, GB"),
    "new-york": (5128581, "New York, US"),
    "los-angeles": (5368361, "Los Angeles, US"),
    "chicago": (4887398, "Chicago, US"),
    "san-francisco": (5391959, "San Francisco, US"),
    "austin": (4671654, "Austin, US"),
    "seattle": (5809844, "Seattle, US"),
    "toronto": (6167865, "Toronto, CA"),
    "montreal": (6077243, "Montreal, CA"),
    "mexico-city": (3530597, "Mexico City, MX"),
    "sao-paulo": (3448439, "Sao Paulo, BR"),
    "buenos-aires": (3435910, "Buenos Aires, AR"),
    "tokyo": (1850147, "Tokyo, JP"),
    "sydney": (2147714, "Sydney, AU"),
    "melbourne": (2158177, "Melbourne, AU"),
    "lahore": (1172451, "Lahore, PK"),
    "karachi": (1174872, "Karachi, PK"),
}


@dataclass(frozen=True)
class City:
    city_id: str | None
    name: str

    def __str__(self) -> str:
        return f"{self.name} ({self.city_id or 'account default'})"


UA = (
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

# "Jul 17 • 4:00 PM"  /  "Jul 17"  /  "Jul 17 • 4:00 PM - Jul 20"
DATE_RE = re.compile(
    r"^(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(?P<day>\d{1,2})"
    r"(?:\s*[•·]\s*(?P<time>\d{1,2}:\d{2}\s*[AP]M))?",
    re.IGNORECASE,
)
EVENT_ID_RE = re.compile(r"/e/(\d+)-")


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass
class Event:
    event_id: str = ""
    artist: str = ""
    venue: str = ""
    city_id: str = ""
    city_name: str = ""
    date_raw: str = ""
    event_date: str = ""  # YYYY-MM-DD
    event_time: str = ""  # HH:MM (24h)
    datetime_iso: str = ""  # YYYY-MM-DDTHH:MM:00
    event_url: str = ""  # UTM params stripped
    image_url: str = ""
    extra_labels: str = ""  # badges like "Free", "Sold Out", "Livestream"
    scraped_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )


# --------------------------------------------------------------------------
# In-page extraction
#
# Runs inside the browser. Walks each /e/ anchor's text nodes in document
# order, finds the one that looks like a date, and uses its position to
# identify artist (before) and venue (after). No class names involved.
# --------------------------------------------------------------------------

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

    // No date line => not an event card (nav link, promo tile, etc.)
    if (dateIdx === -1) continue;

    const before = texts.slice(0, dateIdx);
    const after  = texts.slice(dateIdx + 1);

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


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


def clean_url(url: str) -> str:
    """Strip UTM / came_from query junk, keep the canonical event URL."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_date_raw(raw: str, start: date, end: date) -> tuple[str, str, str]:
    """
    'Jul 17 • 4:00 PM' -> ('2026-07-17', '16:00', '2026-07-17T16:00:00')

    Bandsintown omits the year, so we infer it from the requested range.
    This matters for Dec->Jan rollover: a 'Jan 03' card on a range starting
    2026-12-28 is 2027, not 2026.
    """
    m = DATE_RE.match(raw.strip())
    if not m:
        return "", "", ""

    month = MONTHS[m.group("mon")[:3].title()]
    day = int(m.group("day"))

    candidates = []
    for year in (start.year, start.year + 1, start.year - 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue  # e.g. Feb 29 on a non-leap year
        candidates.append(d)

    if not candidates:
        return "", "", ""

    # Prefer a date inside the requested window; otherwise nearest to start.
    inside = [d for d in candidates if start <= d <= end]
    chosen = (
        inside[0] if inside else min(candidates, key=lambda d: abs((d - start).days))
    )

    time_str = ""
    if m.group("time"):
        try:
            t = datetime.strptime(
                re.sub(r"\s+", " ", m.group("time").strip().upper()), "%I:%M %p"
            )
            time_str = t.strftime("%H:%M")
        except ValueError:
            pass

    iso = f"{chosen.isoformat()}T{time_str or '00:00'}:00"
    return chosen.isoformat(), time_str, iso


def geonames_lookup(name: str, username: str, max_rows: int = 8) -> list[dict]:
    """
    Query the free GeoNames API. Requires a free account:
      https://www.geonames.org/login  -> register -> enable the free web service
    Then pass --geonames-user YOURNAME (or set GEONAMES_USER).
    """
    import urllib.parse
    import urllib.request

    qs = urllib.parse.urlencode(
        {
            "q": name,
            "maxRows": max_rows,
            "featureClass": "P",  # populated places only
            "orderby": "population",  # biggest first -- usually the one you meant
            "username": username,
        }
    )
    url = f"http://api.geonames.org/searchJSON?{qs}"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)

    if "status" in data:
        raise RuntimeError(f"GeoNames error: {data['status'].get('message')}")
    return data.get("geonames", [])


def do_find_city(name: str, username: str | None) -> int:
    """Print candidate GeoNames IDs for a city name."""
    key = name.strip().lower().replace(" ", "-")
    if key in CITIES:
        cid, label = CITIES[key]
        print(f"Built-in match: {label} -> --city-id {cid}\n")

    if not username:
        print("For a full lookup, register free at https://www.geonames.org/login")
        print("then re-run with --geonames-user YOURNAME")
        print("Or just search https://www.geonames.org/ and read the ID from the URL:")
        print("  https://www.geonames.org/2988507/paris.html  ->  2988507")
        return 0 if key in CITIES else 1

    try:
        hits = geonames_lookup(name, username)
    except Exception as e:
        print(f"Lookup failed: {e}")
        return 1

    if not hits:
        print(f"No populated places matched {name!r}.")
        return 1

    print(f"Candidates for {name!r} (biggest first):\n")
    print(f"  {'city_id':<10} {'population':>12}  place")
    print(f"  {'-'*10} {'-'*12}  {'-'*40}")
    for h in hits:
        place = ", ".join(
            x for x in (h.get("name"), h.get("adminName1"), h.get("countryName")) if x
        )
        print(f"  {h['geonameId']:<10} {int(h.get('population', 0)):>12,}  {place}")
    print("\nPick the right one -- city names repeat across countries.")
    print(f"  python {Path(sys.argv[0]).name} --city-id <id> --days 7")
    return 0


def resolve_cities(raw: str | None, username: str | None) -> list[City]:
    """
    Turn --city-id into a list of City. Accepts a comma-separated mix of
    numeric GeoNames IDs and known names:
        --city-id 2988507
        --city-id paris,berlin,london
        --city-id 2988507,berlin
    Empty/None means "use whatever city the account defaults to".
    """
    if not raw:
        return [City(None, "account default")]

    out: list[City] = []
    for tok in (t.strip() for t in raw.split(",") if t.strip()):
        if tok.isdigit():
            label = next((lbl for cid, lbl in CITIES.values() if str(cid) == tok), tok)
            out.append(City(tok, label))
            continue

        key = tok.lower().replace(" ", "-")
        if key in CITIES:
            cid, label = CITIES[key]
            out.append(City(str(cid), label))
            continue

        if username:
            hits = geonames_lookup(tok, username, max_rows=1)
            if hits:
                h = hits[0]
                place = ", ".join(x for x in (h.get("name"), h.get("countryName")) if x)
                print(f"  resolved {tok!r} -> {place} ({h['geonameId']})")
                out.append(City(str(h["geonameId"]), place))
                continue

        raise SystemExit(
            f"Don't know city {tok!r}. Run:  --find-city {tok}\n"
            f"(or pass a numeric GeoNames ID directly)"
        )
    return out


def build_url(city_id: str | None, start: date, end: date) -> str:
    """
    NOTE: the path matters. /all-dates/ IGNORES the date param -- it returns
    every future event, which is why an unfiltered run scrolls forever.
    /choose-dates/ is the one that actually honors the range.

    city_id is optional: signed in, Bandsintown infers your city from the
    account. Pass --city-id only to force a different one.
    """
    params = [
        ("came_from", "257"),
        ("utm_medium", "web"),
        ("utm_source", "venue_page"),
        ("utm_campaign", "discover_more"),
        ("calendarTrigger", "false"),
        ("date", f"{start.isoformat()}T00:00:00,{end.isoformat()}T23:00:00"),
    ]
    if city_id:
        params.append(("city_id", str(city_id)))
    # The #search fragment scrolls the page to the results section.
    return f"{BASE}?{urlencode(params)}#search"


def range_labels(start: date, end: date) -> set[tuple[int, int]]:
    """(month, day) pairs inside the requested window, for filter validation."""
    out, d, guard = set(), start, 0
    while d <= end and guard < 2000:
        out.add((d.month, d.day))
        d += timedelta(days=1)
        guard += 1
    return out


def month_day(raw: str) -> tuple[int, int] | None:
    m = DATE_RE.match(raw.strip())
    if not m:
        return None
    return MONTHS[m.group("mon")[:3].title()], int(m.group("day"))


# --------------------------------------------------------------------------
# Browser steps
# --------------------------------------------------------------------------


def is_logged_in(page) -> bool:
    """
    Heuristic: the header shows 'Log In' / 'Sign Up' only when signed out.
    Once authenticated those are replaced by the account avatar/menu.
    """
    try:
        for sel in ("text=/^Log In$/", "text=/^Sign Up$/"):
            if page.locator(sel).first.is_visible(timeout=1500):
                return False
    except Exception:
        pass
    return True


def login_wall_visible(page) -> bool:
    """Detect the auth modal that intercepts the 'View all' click."""
    for sel in (
        "div[role='dialog']",
        "text=/Log in to see/i",
        "text=/Sign up to see/i",
        "text=/Continue with Google/i",
        "text=/Continue with Apple/i",
    ):
        try:
            if page.locator(sel).first.is_visible(timeout=1200):
                return True
        except Exception:
            continue
    return False


def open_context(
    p,
    *,
    headless: bool,
    auth_file: str | None,
    profile_dir: str | None,
    cdp_url: str | None,
    use_chrome: bool,
):
    """
    Build a browser context in one of three auth modes.
    Returns (context, page, close_fn).

    MODE 3 (--cdp): attach to a Chrome you launched yourself. Google sees a
      genuine, human-started browser, so OAuth just works. Most reliable.
    MODE 2 (--profile-dir): a persistent Chrome profile. Login survives in
      the profile itself. Using channel="chrome" (your real Chrome install,
      not bundled Chromium) is what makes Google cooperate.
    MODE 1 (default): storage_state from auth.json. Fine once you HAVE a
      session, but Google will likely refuse to let you create one this way.
    """
    if cdp_url:
        browser = p.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("CDP browser has no contexts open.")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # Deliberately do NOT close: it's the user's own browser.
        return ctx, page, (lambda: None)

    if profile_dir:
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        kwargs = dict(
            user_data_dir=profile_dir,
            headless=headless,
            locale="en-US",
            args=[] if headless else ["--start-maximized"],
        )
        if headless:
            kwargs["viewport"] = {"width": 1440, "height": 900}
        else:
            kwargs["no_viewport"] = True
        if use_chrome:
            kwargs["channel"] = "chrome"
        try:
            ctx = p.chromium.launch_persistent_context(**kwargs)
        except Exception as e:
            if use_chrome:
                print(
                    f"  Chrome channel unavailable ({e.__class__.__name__}); "
                    "falling back to bundled Chromium."
                )
                kwargs.pop("channel", None)
                ctx = p.chromium.launch_persistent_context(**kwargs)
            else:
                raise
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return ctx, page, (lambda: ctx.close())

    have_auth = bool(auth_file) and Path(auth_file).is_file()
    browser = p.chromium.launch(
        headless=headless,
        args=[] if headless else ["--start-maximized"],
    )
    ctx = browser.new_context(
        viewport=({"width": 1440, "height": 900} if headless else None),
        no_viewport=(not headless),
        locale="en-US",
        user_agent=UA,
        storage_state=(auth_file if have_auth else None),
    )
    page = ctx.new_page()
    return ctx, page, (lambda: browser.close())


def save_auth(ctx, auth_file: str) -> None:
    ctx.storage_state(path=auth_file)
    try:
        os.chmod(auth_file, 0o600)  # this file is as good as a password
    except OSError:
        pass
    print(f"\nSession saved to {auth_file} (permissions 0600).")
    print("Do NOT commit this file. Re-run --login if it stops working.")


def do_login(
    auth_file: str,
    city_id: str | None,
    start: date,
    end: date,
    profile_dir: str | None,
    cdp_url: str | None,
    use_chrome: bool,
) -> int:
    """
    Open a real browser, let the human log in, then persist the session.
    No credentials ever touch this script.

    For Google sign-in we default to a persistent profile driven through your
    REAL Chrome install (channel="chrome"). Google's bot checks reject
    Playwright's bundled Chromium with "This browser or app may not be
    secure", which would make the OAuth step impossible.
    """
    url = build_url(city_id, start, end)

    with sync_playwright() as p:
        ctx, page, close = open_context(
            p,
            headless=False,
            auth_file=None,
            profile_dir=profile_dir,
            cdp_url=cdp_url,
            use_chrome=use_chrome,
        )

        print("Opening Bandsintown...")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        dismiss_cookies(page)

        print("\n" + "=" * 70)
        print("  LOG IN IN THE BROWSER WINDOW  (Continue with Google)")
        print()
        print("  The OAuth popup opens as a separate window -- finish it there.")
        print("  Take as long as you need; 2FA is fine. Nothing is timed.")
        print()
        print("  If Google says 'This browser or app may not be secure',")
        print("  see --cdp in the header comments. That path always works.")
        print("=" * 70)
        input("\nPress Enter once you are logged in... ")

        # Reload so the header reflects the new session before we check.
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2000)
            dismiss_cookies(page)
        except Exception:
            pass

        if not is_logged_in(page):
            print("\nWARNING: still seeing a 'Log In' button -- login may not")
            print("have taken. Saving anyway; if the scrape hits the wall,")
            print("re-run --login, or use --cdp.")
        else:
            print("\nLogged in.")

        save_auth(ctx, auth_file)
        if profile_dir:
            print(
                f"Profile also retained at {profile_dir}/ -- pass "
                f"--profile-dir {profile_dir} to reuse it directly."
            )
        close()
    return 0


def dismiss_cookies(page) -> None:
    """OneTrust banner intercepts clicks if left open."""
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
                print("  cookie banner dismissed")
                return
        except Exception:
            continue


def click_view_all(page) -> bool:
    """
    Click the 'View all' control. It's a bare <div>, not a <button>, so we
    match on text rather than role. Returns True if clicked.
    """
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
                print("  clicked 'View all'")
                return True
        except Exception:
            continue
    print("  no 'View all' button found (may already be expanded)")
    return False


def count_events(page) -> int:
    return len(page.evaluate(DATES_JS))


def out_of_range_count(page, allowed: set[tuple[int, int]]) -> int:
    """How many visible cards fall outside the requested window."""
    bad = 0
    for raw in page.evaluate(DATES_JS):
        md = month_day(raw)
        if md and md not in allowed:
            bad += 1
    return bad


def scroll_until_exhausted(
    page,
    max_scrolls: int,
    stall_limit: int,
    delay: float,
    allowed: set[tuple[int, int]] | None = None,
) -> int:
    """
    Scroll to bottom, wait for the count to grow, repeat. Stop after
    `stall_limit` consecutive scrolls that add nothing.

    Safety net: if cards start showing dates outside the requested window,
    the date filter isn't being applied (e.g. wrong URL path) and this list
    is effectively infinite. Bail out loudly rather than scroll all night.
    """
    stalls = 0
    last = count_events(page)
    print(f"  {last} events before scrolling")

    for i in range(1, max_scrolls + 1):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        # jitter: don't hammer with a metronome-perfect cadence
        page.wait_for_timeout(int((delay + random.uniform(0, 0.4)) * 1000))

        # A "View all" / "Show more" can reappear mid-list on some ranges.
        if stalls == 1:
            click_view_all(page)

        now = count_events(page)

        if allowed and now > last:
            bad = out_of_range_count(page, allowed)
            if bad > 5:
                print(
                    f"\n  STOPPING: {bad} events fall outside the requested "
                    "date range."
                )
                print(
                    "  The date filter is not being applied -- check that the "
                    "URL path is /choose-dates/ and not /all-dates/."
                )
                print("  Without the filter this list never ends.\n")
                return now

        if now > last:
            stalls = 0
            print(f"  scroll {i}: {now} events (+{now - last})")
            last = now
        else:
            stalls += 1
            print(f"  scroll {i}: no new events ({stalls}/{stall_limit})")
            if stalls >= stall_limit:
                print("  reached the end")
                break
    else:
        print(f"  WARNING: hit --max-scrolls ({max_scrolls}); list may be incomplete")

    return last


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def scrape_city(
    page,
    ctx,
    city: City,
    start: date,
    end: date,
    *,
    headless: bool,
    max_scrolls: int,
    stall_limit: int,
    delay: float,
    auth_file: str,
    cdp_url: str | None,
    have_auth: bool,
    strict_dates: bool,
) -> list[Event]:
    """Scrape one city on an already-open page. Returns its events."""
    url = build_url(city.city_id, start, end)
    print(f"\n{'=' * 70}\n  {city}\n{'=' * 70}")
    print(f"URL: {url}")

    page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    try:
        page.wait_for_selector('a[href*="/e/"]', timeout=30_000)
    except PWTimeout:
        print(
            "  No event cards appeared. Either this city has nothing in "
            "range, or the id is wrong. Skipping."
        )
        return []

    dismiss_cookies(page)

    # Confirm the session actually survived before we spend time scrolling.
    if have_auth and not is_logged_in(page):
        print("\n  WARNING: saved session looks EXPIRED (still seeing 'Log In').")
        print("  Re-run:  python bandsintown_scraper.py --login\n")
    elif have_auth:
        print("  session active")

    print("Expanding list...")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.6)")
    page.wait_for_timeout(800)
    before_click = count_events(page)
    click_view_all(page)
    page.wait_for_timeout(1500)

    if login_wall_visible(page) and count_events(page) <= before_click:
        print("\n" + "!" * 68)
        print("  LOGIN WALL hit on 'View all'. Only the first page is visible.")
        print("  Fix:  python bandsintown_scraper.py --login")
        print("!" * 68 + "\n")
        if not headless:
            print("  (You can also just log in in this window right now,")
            print("   then press Enter to carry on with this run.)")
            input("  Press Enter to continue... ")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            dismiss_cookies(page)
            if is_logged_in(page):
                if not cdp_url:
                    save_auth(ctx, auth_file)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.6)")
                page.wait_for_timeout(800)
                click_view_all(page)

    print("Scrolling...")
    allowed = range_labels(start, end)
    scroll_until_exhausted(page, max_scrolls, stall_limit, delay, allowed)

    print("Extracting...")
    raw_rows = page.evaluate(EXTRACT_JS)

    events: dict[str, Event] = {}
    for r in raw_rows:
        m = EVENT_ID_RE.search(r["href"])
        if not m:
            continue
        eid = m.group(1)
        if eid in events:
            continue  # dedupe: same event can render in multiple carousels

        d, t, iso = parse_date_raw(r["date_raw"], start, end)
        events[eid] = Event(
            event_id=eid,
            artist=r["artist"].strip(),
            venue=r["venue"].strip(),
            city_id=city.city_id or "",
            city_name=city.name,
            date_raw=r["date_raw"].strip(),
            event_date=d,
            event_time=t,
            datetime_iso=iso,
            event_url=clean_url(r["href"]),
            image_url=r["image_url"],
            extra_labels=" | ".join(r["extra"]),
        )

    out = list(events.values())

    # Did the date filter actually work? Report honestly either way.
    stray = [e for e in out if month_day(e.date_raw) not in allowed]
    if stray:
        print(
            f"\n  NOTE: {len(stray)} of {len(out)} events fall outside "
            f"{start} -> {end}."
        )
        print(f"  Examples: {', '.join(sorted({e.date_raw for e in stray})[:6])}")
        if strict_dates:
            out = [e for e in out if month_day(e.date_raw) in allowed]
            print(f"  --strict-dates: dropped them, {len(out)} remain.")
        else:
            print("  Kept them. Use --strict-dates to drop them.")

    print(f"  -> {len(out)} events for {city.name}")
    return out


def scrape(
    cities: list[City],
    start: date,
    end: date,
    headless: bool,
    max_scrolls: int,
    stall_limit: int,
    delay: float,
    keep_open: bool,
    auth_file: str,
    profile_dir: str | None = None,
    cdp_url: str | None = None,
    use_chrome: bool = True,
    strict_dates: bool = False,
    city_pause: float = 3.0,
) -> list[Event]:
    """Open ONE browser session and walk every requested city through it."""
    if cdp_url:
        print(f"Attaching to your Chrome at {cdp_url}")
        have_auth = True
    elif profile_dir:
        print(f"Using Chrome profile {profile_dir}/")
        have_auth = Path(profile_dir).is_dir()
    else:
        have_auth = Path(auth_file).is_file()
        if have_auth:
            print(f"Using saved session from {auth_file}")
        else:
            print(f"No {auth_file} found -- running signed out. 'View all' will")
            print("hit a login wall, so expect only the first page of events.")
            print("Run:  python bandsintown_scraper.py --login\n")

    all_events: list[Event] = []
    seen: set[str] = set()

    with sync_playwright() as p:
        ctx, page, close = open_context(
            p,
            headless=headless,
            auth_file=auth_file,
            profile_dir=profile_dir,
            cdp_url=cdp_url,
            use_chrome=use_chrome,
        )

        for i, city in enumerate(cities):
            # Be a decent citizen: pause between cities.
            if i:
                time.sleep(city_pause + random.uniform(0, 1.0))
            try:
                rows = scrape_city(
                    page,
                    ctx,
                    city,
                    start,
                    end,
                    headless=headless,
                    max_scrolls=max_scrolls,
                    stall_limit=stall_limit,
                    delay=delay,
                    auth_file=auth_file,
                    cdp_url=cdp_url,
                    have_auth=have_auth,
                    strict_dates=strict_dates,
                )
            except Exception as e:
                # One bad city shouldn't throw away the cities already done.
                print(f"  ERROR on {city.name}: {e.__class__.__name__}: {e}")
                print("  Continuing with the next city.")
                continue

            # An event can legitimately appear in two nearby cities' listings.
            for ev in rows:
                key = f"{ev.city_id}:{ev.event_id}"
                if key not in seen:
                    seen.add(key)
                    all_events.append(ev)

        if keep_open and not headless and not cdp_url:
            input("\nBrowser left open for inspection. Press Enter to close...")
        close()

    all_events.sort(
        key=lambda e: (e.city_name, e.datetime_iso or "9999", e.artist.lower())
    )
    return all_events


def write_output(events: list[Event], stem: str, split: bool = False) -> None:
    def dump(rows: list[Event], base: str) -> None:
        with open(f"{base}.json", "w", encoding="utf-8") as f:
            json.dump([asdict(e) for e in rows], f, indent=2, ensure_ascii=False)
        if rows:
            with open(f"{base}.csv", "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
                w.writeheader()
                for e in rows:
                    w.writerow(asdict(e))
        print(f"Wrote {base}.json and {base}.csv  ({len(rows)} events)")

    dump(events, stem)

    if split:
        by_city: dict[str, list[Event]] = {}
        for e in events:
            by_city.setdefault(e.city_name, []).append(e)
        for city_name, rows in by_city.items():
            slug = re.sub(r"[^a-z0-9]+", "-", city_name.lower()).strip("-")
            dump(rows, f"{stem}_{slug}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape Bandsintown city event listings.")
    ap.add_argument(
        "--city-id",
        default=None,
        metavar="IDS",
        help="Comma-separated GeoNames IDs and/or known names: "
        "'2988507' | 'paris,berlin' | '2988507,london'. "
        "Omit to use your account's city.",
    )
    ap.add_argument(
        "--find-city",
        metavar="NAME",
        help="Look up the city_id for a place name, then exit",
    )
    ap.add_argument(
        "--list-cities",
        action="store_true",
        help="Print the built-in city shortcuts and exit",
    )
    ap.add_argument(
        "--geonames-user",
        default=os.environ.get("GEONAMES_USER"),
        help="GeoNames username for name lookups "
        "(free: geonames.org/login). Or set GEONAMES_USER.",
    )
    ap.add_argument(
        "--split", action="store_true", help="Also write one file pair per city"
    )
    ap.add_argument(
        "--city-pause",
        type=float,
        default=3.0,
        help="Seconds to wait between cities (default: 3)",
    )
    ap.add_argument(
        "--days", type=int, default=7, help="Days ahead from start (default: 7)"
    )
    ap.add_argument("--start", help="Start date YYYY-MM-DD (default: today)")
    ap.add_argument("--end", help="End date YYYY-MM-DD (overrides --days)")
    ap.add_argument(
        "--strict-dates",
        action="store_true",
        help="Drop any event outside the requested range",
    )
    ap.add_argument("--out", default="bandsintown_events", help="Output filename stem")
    ap.add_argument(
        "--login",
        action="store_true",
        help="Open a browser, log in by hand, save the session, exit",
    )
    ap.add_argument(
        "--auth-file",
        default=AUTH_DEFAULT,
        help=f"Where the saved session lives (default: {AUTH_DEFAULT})",
    )
    ap.add_argument(
        "--profile-dir",
        default=PROFILE_DEFAULT,
        help=f"Persistent Chrome profile dir (default: {PROFILE_DEFAULT}). "
        "Best for Google login. Pass '' to use auth.json instead.",
    )
    ap.add_argument(
        "--cdp",
        default=None,
        metavar="URL",
        help="Attach to your own running Chrome, e.g. http://localhost:9222 "
        "-- use this if Google blocks the automated browser",
    )
    ap.add_argument(
        "--chromium",
        dest="use_chrome",
        action="store_false",
        default=True,
        help="Use bundled Chromium instead of your real Chrome install "
        "(Google will likely reject it)",
    )
    # Visible by default. Pass --headed to say so explicitly, or --headless to hide it.
    vis = ap.add_mutually_exclusive_group()
    vis.add_argument(
        "--headed",
        dest="headed",
        action="store_true",
        default=None,
        help="Open a visible browser window (this is the default)",
    )
    vis.add_argument(
        "--headless",
        dest="headed",
        action="store_false",
        help="Run with no visible window, e.g. for cron",
    )
    ap.add_argument(
        "--keep-open",
        action="store_true",
        help="Pause before closing the browser so you can inspect the page",
    )
    ap.add_argument(
        "--max-scrolls", type=int, default=400, help="Safety cap on scroll iterations"
    )
    ap.add_argument(
        "--stall-limit",
        type=int,
        default=4,
        help="Empty scrolls before declaring the end",
    )
    ap.add_argument(
        "--delay", type=float, default=1.2, help="Base seconds between scrolls"
    )
    args = ap.parse_args()

    if args.list_cities:
        print("Built-in shortcuts (any GeoNames ID also works directly):\n")
        for key, (cid, label) in sorted(CITIES.items(), key=lambda kv: kv[1][1]):
            print(f"  {key:<15} {cid:<9} {label}")
        print(f"\nAnything else:  --find-city 'Glasgow'")
        return 0

    if args.find_city:
        return do_find_city(args.find_city, args.geonames_user)

    start = date.fromisoformat(args.start) if args.start else date.today()
    end = (
        date.fromisoformat(args.end) if args.end else start + timedelta(days=args.days)
    )

    if end < start:
        print("ERROR: --end is before --start")
        return 1

    profile_dir = args.profile_dir or None
    if args.cdp:
        profile_dir = None  # CDP owns the session; profile is irrelevant

    # Login mode always runs headed -- you can't type into a hidden window.
    if args.login:
        return do_login(
            args.auth_file, None, start, end, profile_dir, args.cdp, args.use_chrome
        )

    cities = resolve_cities(args.city_id, args.geonames_user)

    # args.headed: None = not specified (default visible), True = --headed, False = --headless
    headless = args.headed is False
    mode = "cdp" if args.cdp else ("profile" if profile_dir else "auth.json")
    print(f"{len(cities)} city(s): {', '.join(c.name for c in cities)}")
    print(
        f"{start} -> {end} | browser: {'hidden' if headless else 'visible'} "
        f"| auth: {mode}"
    )

    t0 = time.time()
    events = scrape(
        cities,
        start,
        end,
        headless,
        args.max_scrolls,
        args.stall_limit,
        args.delay,
        args.keep_open,
        args.auth_file,
        profile_dir,
        args.cdp,
        args.use_chrome,
        args.strict_dates,
        args.city_pause,
    )

    if not events:
        print("\nNo events found.")
        return 1

    print()
    write_output(events, args.out, args.split)

    by_city: dict[str, int] = {}
    for e in events:
        by_city[e.city_name] = by_city.get(e.city_name, 0) + 1

    dated = sum(1 for e in events if e.event_date)
    print(
        f"\n{len(events)} events in {time.time() - t0:.1f}s "
        f"({dated} with a parsed date)"
    )
    if len(by_city) > 1:
        print("\nPer city:")
        for name, n in sorted(by_city.items(), key=lambda kv: -kv[1]):
            print(f"  {name:<28} {n:>5}")

    print("\nFirst 5:")
    for e in events[:5]:
        print(
            f"  {e.city_name[:16]:<18} {e.datetime_iso or e.date_raw:<20} "
            f"{e.artist[:24]:<26} {e.venue[:30]}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
