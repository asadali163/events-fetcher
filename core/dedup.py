import math
from difflib import SequenceMatcher

DISTANCE_THRESHOLD_METERS = 50
NAME_SIMILARITY_THRESHOLD = 0.6

EARTH_RADIUS_METERS = 6371000


def haversine_meters(lat1, lon1, lat2, lon2):
    """Distance in meters between two lat/long points, or None if any
    coordinate is missing."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def name_similarity(name_a, name_b):
    """0.0-1.0 similarity between two event names, case/whitespace-insensitive."""
    if not name_a or not name_b:
        return 0.0
    return SequenceMatcher(None, name_a.lower().strip(), name_b.lower().strip()).ratio()


def is_duplicate(event_a, event_b):
    """Two normalized events count as the same real-world event only when
    all three signals agree: same date, venues close together, similar
    names. Any one signal alone is too weak to decide this on its own."""
    if not event_a.get("date") or event_a.get("date") != event_b.get("date"):
        return False

    distance = haversine_meters(
        event_a.get("venue_latitude"),
        event_a.get("venue_longitude"),
        event_b.get("venue_latitude"),
        event_b.get("venue_longitude"),
    )
    if distance is None or distance > DISTANCE_THRESHOLD_METERS:
        return False

    if (
        name_similarity(event_a.get("event_name"), event_b.get("event_name"))
        < NAME_SIMILARITY_THRESHOLD
    ):
        return False

    return True


def _completeness(event):
    return sum(1 for v in event.values() if v not in (None, "", [], {}))


def _merge_cluster(cluster):
    """The most complete record becomes the merged event's base; every
    cluster member (including the winner) is recorded in 'sources' so no
    traceability is lost."""
    best = max(cluster, key=_completeness)
    merged = dict(best)
    merged["sources"] = [
        {"source": e.get("source"), "source_event_id": e.get("source_event_id")}
        for e in cluster
    ]
    return merged


def deduplicate_events(events):
    """Group events into clusters where every pair matches (via union-find,
    so transitive matches cluster correctly regardless of input order), then
    merge each cluster into one record. Returns a new list, same length or
    shorter than the input."""
    n = len(events)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        root_i, root_j = find(i), find(j)
        if root_i != root_j:
            parent[root_i] = root_j

    for i in range(n):
        for j in range(i + 1, n):
            if is_duplicate(events[i], events[j]):
                union(i, j)

    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(events[i])

    return [_merge_cluster(cluster) for cluster in clusters.values()]
