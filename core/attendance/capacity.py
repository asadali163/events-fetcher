"""Verified venue capacities, used as hard ceilings on attendance:

1. DICE's own capacity text ("175 capacity").
2. France's national sports-facility register (fixed stand seats) for
   sportsevents24 venues - looked up once per venue, cached in enrichment.db.
3. The city's known-venues table in core/pipeline.yaml.
"""

import json
import re
import unicodedata

import requests

from core.enrichment_store import get_register_cache, set_register_cache

REGISTER_API = "https://equipements.sports.gouv.fr/api/explore/v2.1/catalog/datasets/data-es/records"

_GENERIC_WORDS = {
    "GYMNASE", "CENTRE", "SPORTIF", "SPORTIVE", "COMPLEXE", "SALLE", "SALLES", "STADE", "PARIS", "DE", "DU",
    "DES", "LA", "LE", "LES", "EX", "ET", "D", "L", "SUR", "PATINOIRE", "PISCINE", "ESPACE", "TERRAIN", "CITY",
}
# Words in a facility's type/name that make it suitable for a sport (default: an indoor hall).
_SPORT_FACILITY_WORDS = {
    "ice-hockey": ("patinoire", "glace"),
    "football": ("grands jeux", "football"),
    "rugby": ("grands jeux", "rugby"),
    "american-football": ("grands jeux", "football americain"),
    "beach-soccer": ("sable", "plage"),
    "baseball": ("baseball", "softball"),
    "softball": ("baseball", "softball"),
}
_INDOOR_WORDS = ("salle", "gymnase", "dojo", "palais", "arena")


def number_in(value):
    """First integer in a value like 175, '1,200', 'Doors open...\\n175 capacity'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"(\d[\d,\s]*)\s*capacity", str(value)) or re.search(r"\d[\d,]*", str(value))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1) if match.lastindex else match.group(0))
    return int(digits) if digits else None


def dice_capacity(event):
    return number_in(event.get("capacity")) or None


def ascii_upper(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().upper()


def _venue_words(venue):
    text = ascii_upper(venue)
    text = re.sub(r"[()]", " ", text)
    text = re.sub(r"\b\d{1,2}H\d{0,2}\b|\bPARIS\s?\d+E?\b|\b\d+E\b", " ", text)  # times, "Paris13", "16e"
    return [w for w in re.split(r"[^A-Z0-9]+", text) if len(w) > 1 and not w.isdigit() and w not in _GENERIC_WORDS]


def register_capacity(event, city_settings):
    """Fixed stand seats from the national register for a sports event's venue, or None."""
    department = city_settings.get("register_department")
    if event.get("source") != "sportsevents24" or not event.get("venue_name") or not department:
        return None
    sport = (event.get("type") or "").lower()
    address = str(event.get("venue_address") or "")
    cache_id = json.dumps([event["venue_name"], address, sport, str(department)], ensure_ascii=False)
    found_before, cached = get_register_cache(cache_id)
    if found_before:
        return cached

    words = _venue_words(event["venue_name"])
    postcode = re.search(r"\b(\d{5})\b", address)
    where_place = f'inst_cp="{postcode.group(1)}"' if postcode else f'dep_code="{department}"'
    suitable = _SPORT_FACILITY_WORDS.get(sport, _INDOOR_WORDS)
    queries = [" ".join(words)] + sorted(words, key=len, reverse=True)[:2] if words else []

    found, network_failed = None, False
    for query in dict.fromkeys(queries):
        if " " not in query and not postcode and len(query) < 5:
            continue  # a short single word without a postcode is too vague
        name_match = " and ".join(
            f"(search(inst_nom, {json.dumps(w)}) or search(equip_nom, {json.dumps(w)}))" for w in query.split()
        )
        params = {
            "where": f"({name_match}) and {where_place}",
            "select": "equip_numero,inst_nom,equip_nom,equip_type_name,equip_type_famille,equip_trib_nb",
            "limit": 50,
        }
        try:
            rows = requests.get(REGISTER_API, params=params, timeout=20).json().get("results", [])
        except (requests.RequestException, ValueError):
            network_failed = True
            break
        fitting = [
            r for r in rows
            if any(
                w in ascii_upper(f'{r.get("equip_type_name")} {r.get("equip_type_famille")} {r.get("equip_nom")}').lower()
                for w in suitable
            )
        ]
        if fitting:
            best = max(
                fitting,
                key=lambda r: (r.get("equip_trib_nb") or 0, "gymnase" in str(r.get("equip_type_name")).lower()),
            )
            found = {
                "seats": best.get("equip_trib_nb") or None,  # 0 = not recorded / no fixed stands
                "facility": f'{best.get("inst_nom")} - {best.get("equip_nom")}',
                "equip_numero": best.get("equip_numero"),
                "matched_query": query,
            }
            break

    if not network_failed:  # a network error is retried next time, not cached as "no match"
        set_register_cache(cache_id, found)
    return found


def _plain_words(text):
    """'La Taverne de l’Olympia' -> 'la taverne de l olympia' (ASCII, lower
    case, every non-alphanumeric run -> one space)."""
    text = str(text or "").replace("’", "'")
    return " ".join(re.findall(r"[a-z0-9]+", ascii_upper(text).lower()))


# "<something> de/du/des/d' [l'/la/le/les] <known venue>" is a smaller place
# next to or inside the venue (La Taverne de l'Olympia, Bar du Zenith), not
# the venue's main hall - its capacity must not be used as a ceiling.
_ANNEX_PREFIX = r"\b(?:de|du|des|d)(?: (?:l|la|le|les))? "


def known_venue_capacity(event, city_settings):
    """Capacity from the city's known-venues table when the venue name
    contains a known venue as whole words (so "Adidas Arena - Paris" or
    "Zenith Paris - La Villette" match) - but not when it is only an annex
    named after it ("La Taverne de l'Olympia"), see _ANNEX_PREFIX."""
    name = _plain_words(event.get("venue_name"))
    if not name:
        return None
    for venue, cap in (city_settings.get("known_venues") or {}).items():
        key = _plain_words(venue)
        if not key or not re.search(rf"\b{re.escape(key)}\b", name):
            continue
        if re.search(rf"{_ANNEX_PREFIX}{re.escape(key)}\b", name):
            continue
        return int(cap)
    return None


def verified_capacity(event, city_settings):
    """(capacity, where it comes from) from real data, or (None, None)."""
    dice = dice_capacity(event)
    if dice:
        return dice, "ticketing page (DICE)"
    try:
        reg = register_capacity(event, city_settings)
    except Exception as error:  # a register problem must never stop enrichment
        print(f"[attendance] register lookup failed for {event.get('venue_name')!r}: {error}")
        reg = None
    if reg and reg.get("seats"):
        return reg["seats"], f'French sports-facility register: fixed stand seats ({reg["facility"]})'
    known = known_venue_capacity(event, city_settings)
    if known:
        return known, "known venue table"
    return None, None
