"""v3 + priors estimation: batch events into LLM requests, then check and
correct every answer in code.

Per answer, in order (same as the notebook's clean_estimate_v3p):
  whole numbers, low <= expected <= high
  -> capacity ceiling x sessions (verified capacity, else the model's guess)
  -> clamp into the chosen category prior's range (a real ceiling still wins)
  -> rubric rounding (<100: 5, 100-1,000: 10, >1,000: 100)
  -> radius from attendance (+35% outdoors)
"""

import hashlib
import json

from core.attendance.capacity import number_in
from core.attendance.llm import DailyTokenLimit, chat_json
from core.attendance.prompt import (
    CAPACITY_SHARE_PRIORS, CATEGORY_PRIORS, PER_SESSION_PRIORS, PRIOR_KEYS, PROMPT_VERSION,
    RSVP_SHARE_PRIORS, llm_input, system_prompt, user_prompt,
)
from core.attendance.radius import radius_from_attendance

CONFIDENCE_LEVELS = {"low", "medium", "high"}
LIKELY_IN_CITY = {"yes", "no", "unsure"}


def input_hash(model, data):
    """Changes whenever what the model would see, the prompt version or the
    model changes -> the stored estimate is out of date."""
    blob = json.dumps([PROMPT_VERSION, model, data], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def prepare(event, city_settings, model):
    data = llm_input(event, city_settings)
    return data, input_hash(model, data)


def _to_int(value):
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return None


def round_attendance(n, ceiling=None):
    """Rubric rounding; never above `ceiling` (round down instead)."""
    if not n:
        return n
    step = 5 if n < 100 else 10 if n <= 1000 else 100
    rounded = max(step, int(round(n / step) * step))
    if ceiling and rounded > ceiling:
        rounded = max(1, int(ceiling // step) * step) or int(ceiling)
    return rounded


def _prior_range(category, est, event):
    if category in CATEGORY_PRIORS:
        return CATEGORY_PRIORS[category]
    if category in PER_SESSION_PRIORS:
        lo, hi = PER_SESSION_PRIORS[category]
        return lo * est["sessions"], hi * est["sessions"]
    if category in CAPACITY_SHARE_PRIORS and est.get("capacityUsed"):
        lo, hi = CAPACITY_SHARE_PRIORS[category]
        return max(1, round(lo * est["capacityUsed"])), max(1, round(hi * est["capacityUsed"]))
    rsvps = number_in(event.get("attendees"))
    if category in RSVP_SHARE_PRIORS and rsvps:
        lo, hi = RSVP_SHARE_PRIORS[category]
        return max(1, round(lo * rsvps)), max(1, round(hi * rsvps))
    return None


def clean_estimate(raw, event, data):
    """A checked estimate dict from one raw model answer, or None if unusable.
    `data` is the event's llm_input (it carries the verified capacity)."""
    if not isinstance(raw, dict):
        return None
    expected = _to_int(raw.get("expectedAttendance"))
    if expected is None:
        return None
    low, high = _to_int(raw.get("low")), _to_int(raw.get("high"))
    low = expected if low is None else min(low, expected)
    high = expected if high is None else max(high, expected)
    sessions = min(max(_to_int(raw.get("sessions")) or 1, 1), 10)
    capacity_guess = _to_int(raw.get("venueCapacityGuess")) or None

    capacity, origin = data.get("venue_capacity"), data.get("venue_capacity_source")
    if not capacity and capacity_guess:
        capacity, origin = capacity_guess, "model's own capacity guess"
    ceiling = capacity * sessions if capacity else None
    if ceiling:  # one session can't exceed the capacity; several sessions can
        expected, low, high = min(expected, ceiling), min(low, ceiling), min(high, ceiling)

    outdoor = str(raw.get("outdoor")).lower() == "true"
    confidence = str(raw.get("confidence", "")).lower()
    in_city = str(raw.get("likelyInCity", "")).lower()
    est = {
        "status": "estimated",
        "expectedAttendance": round_attendance(expected, ceiling),
        "low": round_attendance(low, ceiling),
        "high": round_attendance(high, ceiling),
        "confidence": confidence if confidence in CONFIDENCE_LEVELS else "low",
        "venueCapacityGuess": capacity_guess,
        "likelyInCity": in_city if in_city in LIKELY_IN_CITY else "unsure",
        "eventKind": str(raw.get("eventKind") or "other"),
        "sessions": sessions,
        "outdoor": outdoor,
        "capacityUsed": capacity,
        "capacitySource": origin,
        "reason": str(raw.get("reason") or "").strip()[:400],
    }

    category = str(raw.get("priorCategory") or "none").strip().lower()
    category = category if category in PRIOR_KEYS else "none"
    bounds = _prior_range(category, est, event)
    model_value = est["expectedAttendance"]
    if bounds:
        lo, hi = bounds
        if ceiling:  # a real ceiling still wins over the prior
            hi = min(hi, ceiling)
            lo = min(lo, hi)
        est["expectedAttendance"] = round_attendance(min(max(model_value, lo), hi), ceiling)
        est["low"] = round_attendance(min(max(est["low"], lo), est["expectedAttendance"]), ceiling)
        est["high"] = round_attendance(max(min(est["high"], hi), est["expectedAttendance"]), ceiling)
    est.update(
        priorCategory=category,
        priorRange=list(bounds) if bounds else None,
        modelAttendance=model_value,
        radiusMeters=radius_from_attendance(est["expectedAttendance"], outdoor),
    )
    return est


def _parse(text):
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    items = payload.get("estimates", []) if isinstance(payload, dict) else []
    return {_to_int(item.get("id")): item for item in items if isinstance(item, dict)}


def estimate_with_llm(client, model, city, city_settings, items, batch_size, on_result, on_failure, retry_pass=True):
    """items: list of (key, event, llm_input). Calls on_result(key, estimate)
    or on_failure(key, error) for each one as its batch returns, so progress is
    saved even if a later batch fails. Raises DailyTokenLimit when the quota
    runs out (results so far are already handed over). Returns tokens used."""
    sys_prompt = system_prompt(city, city_settings)
    tokens = 0
    pending = list(items)
    for pass_number in range(2 if retry_pass else 1):
        missed = []
        for start in range(0, len(pending), batch_size):
            chunk = pending[start:start + batch_size]
            batch = [(n, data) for n, (_, _, data) in enumerate(chunk, start=1)]
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt(city, batch)},
            ]
            try:
                text, used = chat_json(client, model, messages)
            except DailyTokenLimit:
                raise
            except Exception as error:  # this batch is lost; the next one may work
                for key, _, _ in chunk:
                    on_failure(key, f"{type(error).__name__}: {error}")
                continue
            tokens += used
            by_id = _parse(text)
            for n, (key, event, data) in enumerate(chunk, start=1):
                cleaned = clean_estimate(by_id.get(n), event, data)
                if cleaned:
                    on_result(key, cleaned)
                else:
                    missed.append((key, event, data))
            print(f"[attendance] batch of {len(chunk)}: {len(chunk) - sum(1 for m in missed if m in chunk)} estimated")
        if not missed:
            break
        if pass_number == 0 and retry_pass:
            print(f"[attendance] {len(missed)} event(s) left out of the answers - asking again once")
            pending = missed
        else:
            for key, _, _ in missed:
                on_failure(key, "model gave no usable answer for this event")
    return tokens
