"""The v3 + priors prompt (notebook version "v3p.2"), what the model sees per
event, and the category priors that code enforces afterwards.

Bump PROMPT_VERSION after editing the prompt, the priors or the event input:
every stored estimate with another version is then recomputed.
"""

import json
import re
from datetime import date

from core.attendance.capacity import number_in, verified_capacity

PROMPT_VERSION = "v3p.2"
DESCRIPTION_MAX_CHARS = 600

# priorCategory -> (min, max) people on the day; from the labelling rubric
CATEGORY_PRIORS = {
    "leisure_league": (10, 16),        # corporate/leisure leagues, e.g. FSGT 4x4 volleyball: players only
    "youth_sport": (25, 80),           # U13-U17: players and parents, whatever the hall size
    "amateur_regional": (30, 80),      # departmental/regional senior matches and cups
    "national_amateur": (50, 150),     # 4th-5th tier club sport, e.g. Nationale 2/3
    "ice_hockey_d2": (150, 400),       # ice hockey 3rd tier (Division 2)
    "pro_second_tier": (400, 1500),    # Ligue B, Pro B, Proligue
    "para_sport": (20, 40),            # para-sport lower divisions
    "talk_lecture": (40, 80),          # book talks, museum/library lectures, academic half-day seminars
    "workshop_networking": (15, 60),   # workshops, networking, afterworks
    "major_church_organ": (200, 500),  # free organ recitals in major churches
    "quiet_free_exhibition": (50, 150),
    "parish_youth": (20, 50),
    "religious_retreat": (8, 20),
    "community_fair": (300, 1000),     # municipal gaming or community fairs, per day
    "network_day": (150, 400),         # professional network days
}
PER_SESSION_PRIORS = {"guided_tour": (15, 30)}         # x sessions
CAPACITY_SHARE_PRIORS = {"jam_bar_night": (0.4, 0.6)}  # x capacity (free weekly jams / bar nights)
RSVP_SHARE_PRIORS = {"free_meetup": (0.75, 0.9)}       # x rsvp_count
PRIOR_KEYS = [*CATEGORY_PRIORS, *PER_SESSION_PRIORS, *CAPACITY_SHARE_PRIORS, *RSVP_SHARE_PRIORS, "none"]

# Copied as-is when present.
PLAIN_FIELDS = [
    "event_name", "source", "type", "category", "genre",
    "sport", "tournament", "team_home", "team_away",
    "date", "time", "end_date", "end_time",
    "venue_name", "venue_address", "venue_city", "venue_country",
    "organizer", "description",
    "price_from", "price_min", "price_max", "currency", "status",
]

_SYSTEM_PROMPT_TEMPLATE = """You are a careful event-analytics estimator. You estimate how many people attend events listed for {CITY}. Your numbers feed a city model of crowds, footfall and demand around venues. Today is {TODAY}.
Consistency matters as much as precision: similar events must get similar numbers unless the data says otherwise. A reasoned estimate with an honest confidence level beats a precise-looking guess.

WHAT TO ESTIMATE
expectedAttendance = distinct people physically present on the listed date: audience + participants (players, coaches, referees, performers, speakers); exclude venue staff.
- Events before today: the actual attendance. Today or later: the expected attendance.
- Multi-hour events with turnover (fairs, open days, open-air parties): total unique people that day.
- Several sessions on the same date (e.g. tours at 10:00 and 14:00): add the sessions together and set "sessions" to their number.
- Multi-day events and long-running exhibitions: the listed date only (that day's visitors).

HOW TO REASON
1. Work out what the event really is. Titles can mislead: a musician's name may be a jam session he joins; a festival name may stand for one act that night; a "museum" may be an ongoing free exhibit.
2. Capacity for THIS configuration (standing vs seated, which room or hall). If "venue_capacity" is given it is verified real data (origin in "venue_capacity_source") and a ceiling - but it may be the main hall of a big complex. Big venue names can refer to a small annex (side hall, training rink, small room). Otherwise estimate the capacity yourself (venueCapacityGuess).
3. Attendance = capacity x fill rate. Use the category prior instead when capacity does not drive turnout (e.g. youth sport in a 4,000-seat arena).

FILL RATE (share of the configuration's capacity)
- The data says sold out / complet: 95-100%
- Extra dates added for demand: 90-98%
- Established act, well-known venue, on sale, no sell-out evidence: 75-90%
- Discounts or promo pushes: 60-80%
- Emerging or unknown act, small club, weeknight: 50-70%
- Monday-Wednesday: subtract 5-10 points. July-August for local-audience events: subtract 10-20 points.

CATEGORY PRIORS (when capacity does not drive turnout)
- Corporate/leisure leagues (e.g. FSGT 4x4 volleyball, "Division 6/7"): players only, 10-16
- Youth sport U13-U17: 25-80 (players and parents), even in a 4,000-seat arena
- Amateur departmental/regional senior matches and cups: 30-80
- 4th-5th tier club sport (e.g. Nationale 2/3): 50-150
- Ice hockey 3rd tier (Division 2): 150-400
- Professional second tier indoor sport (Ligue B, Pro B, Proligue): 400-1,500; top tier indoor (Starligue, Betclic Elite, Ligue A): 1,500-5,000
- Para-sport lower divisions: 20-40
- Free meetups: rsvp_count x 0.75-0.9; events with a registration cap: 85-100% of the cap
- Guided tours: group size (usually 15-30) x number of sessions
- Book talks, museum or library lectures: 40-80; academic half-day seminars: 40-80
- Workshops, networking, afterworks: 15-60
- Free weekly jam sessions / bar nights: 40-60% of capacity
- Free organ recitals in major churches: 200-500
- Free exhibitions: that day's visitors; quiet ones 50-150
- Parish youth groups: 20-50; monastery/religious retreats: 8-20
- Municipal gaming or community fairs: 300-1,000 per day
- Professional network days: typically 150-400

VENUE REFERENCE POINTS ({CITY} area)
{REFERENCE_POINTS}

OUTPUT RULES
- Do not inflate. If the event, performer or organiser is not recognisable, assume a small event suited to the venue type.
- "low" and "high": the range you are about 80% sure contains the real number; low <= expectedAttendance <= high.
- "confidence": "high" only with direct evidence (sold out, registration cap, rsvp_count) AND a verified venue_capacity; "medium" with a verified capacity plus indirect signals, or a well-matched category prior; otherwise "low". When unsure pick the lower level and widen the range.
- "likelyInCity": "no" if the event clearly takes place somewhere else (the home team, venue or address belongs to another city or country), "unsure" if you cannot tell, otherwise "yes".
- "eventKind": one of pro_sport, amateur_sport, concert, club_night, theatre_show, festival, exhibition, conference_talk, workshop_class, meetup_networking, tour_visit, film_screening, religious, fair_expo, other.
- "venueCapacityGuess": the capacity of this configuration (repeat venue_capacity when it applies), or null if you cannot tell.
- "sessions": number of separate sessions on that date that you added together (usually 1).
- "outdoor": true if the event is open-air or mainly outdoors (street, park, walking tour), else false.
- "priorCategory": the category prior that fits this event, exactly one of: {PRIOR_KEYS}. Use "none" when turnout follows the venue capacity (concerts, club nights, theatre, professional sport in its arena, big festivals) or when no prior fits. leisure_league = corporate/leisure leagues (FSGT, "Division 5-7", 4x4); youth_sport = U11-U18; amateur_regional = departmental/regional senior leagues and cups; national_amateur = Nationale 1-3 and similar 4th-5th tiers; para_sport = any wheelchair/handisport/para match (takes priority over the league level); talk_lecture = talks, lectures, seminars, conferences in a museum, library or university; workshop_networking = workshops, classes, afterworks, small networking evenings; network_day = a full-day national/professional federation or trade-network gathering; jam_bar_night = free or cheap weekly jam sessions and regular bar nights; guided_tour = guided visits and walking tours; free_meetup = free events with an rsvp_count.
- "reason": one short sentence (max 25 words) naming what the event really is and the key evidence.

Return ONLY a JSON object in exactly this shape, one entry per input event, same ids:
{"estimates": [{"id": 1, "expectedAttendance": 60, "low": 40, "high": 90, "confidence": "low", "venueCapacityGuess": 200, "sessions": 1, "outdoor": false, "likelyInCity": "yes", "eventKind": "amateur_sport", "priorCategory": "amateur_regional", "reason": "..."}]}"""

_DEFAULT_REFERENCE_POINTS = (
    "theatres 300-1,000; small clubs and bars 50-300; jazz clubs 50-150; chapels/small churches 80-300."
)


def system_prompt(city, city_settings, today=None):
    return (
        _SYSTEM_PROMPT_TEMPLATE
        .replace("{CITY}", city)
        .replace("{TODAY}", (today or date.today()).isoformat())
        .replace("{REFERENCE_POINTS}", str(city_settings.get("reference_points") or _DEFAULT_REFERENCE_POINTS).strip())
        .replace("{PRIOR_KEYS}", ", ".join(PRIOR_KEYS))
    )


def user_prompt(city, batch, today=None):
    """batch: list of (id, llm_input) pairs."""
    items = [{"id": i, **data} for i, data in batch]
    return (
        f"Today is {(today or date.today()).isoformat()}. Estimate attendance for these {len(items)} events "
        f"listed for {city}. Return JSON only, one estimate per id.\n\n"
        + json.dumps(items, ensure_ascii=False, indent=1)
    )


def llm_input(event, city_settings):
    """Only the fields that say something about crowd size, cleaned."""
    data = {}
    for field in PLAIN_FIELDS:
        value = event.get(field)
        if value in (None, "", [], {}):
            continue
        if field == "description":
            value = re.sub(r"\s+", " ", str(value)).strip()[:DESCRIPTION_MAX_CHARS]
        data[field] = value

    if data.get("venue_address") == data.get("venue_name"):
        data.pop("venue_address", None)
    # StubHub has put the price ("From $114") in venue_name - not a venue.
    if re.match(r"^\s*from\s*[$€£]", str(data.get("venue_name", "")), re.I):
        data.pop("venue_name", None)
    if event.get("date"):
        try:
            data["weekday"] = date.fromisoformat(str(event["date"])).strftime("%A")
        except ValueError:
            pass

    genres = event.get("genres")
    if isinstance(genres, str):
        try:
            genres = json.loads(genres)
        except ValueError:
            genres = [genres]
    if genres:
        data["genres"] = genres

    capacity, origin = verified_capacity(event, city_settings)
    if capacity:
        data["venue_capacity"] = capacity
        data["venue_capacity_source"] = origin
    rsvps = number_in(event.get("attendees"))
    if rsvps:
        data["rsvp_count"] = rsvps
    followers = number_in(event.get("follow_count"))
    if followers:
        data["performer_followers"] = followers
    return data
