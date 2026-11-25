"""City boundaries: KML polygons + point-in-polygon checks.

Shared by the SportsEvents24 connector (validate() keeps only in-city
events) and core/export.py (the daily public list keeps only events whose
coordinates are inside upload.boundary_file).

A boundary file is a KML with one or more <Polygon> (holes supported), e.g.
drawn in Google Earth and saved as boundaries/<City>.kml.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOUNDARIES_DIR = PROJECT_ROOT / "boundaries"


def _ring(coordinates_text):
    ring = []
    for triple in coordinates_text.split():
        lon, lat = triple.split(",")[:2]
        ring.append((float(lon), float(lat)))
    return ring


def load_boundary_file(path):
    """Every <Polygon> in a KML file as (outer_ring, [holes]), rings as
    (lon, lat) lists. A relative path is taken from the project root.
    Raises ValueError if the file is missing or has no polygon."""
    path = Path(path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise ValueError(f"Boundary file not found: {path}")
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


def load_boundary(city):
    """boundaries/<city>.kml. Raises ValueError if there's no usable file —
    the same way connectors raise for a city they don't have mapped."""
    path = BOUNDARIES_DIR / f"{city}.kml"
    if not path.exists():
        raise ValueError(
            f"No boundary for '{city}'. Add a KML polygon at boundaries/{city}.kml."
        )
    return load_boundary_file(path)


def _point_in_ring(lon, lat, ring):
    """Ray casting: count the ring edges a ray going east crosses."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            if lon < xi + (lat - yi) * (xj - xi) / (yj - yi):
                inside = not inside
        j = i
    return inside


def in_boundary(lat, lon, polygons):
    if lat is None or lon is None:
        return False
    return any(
        _point_in_ring(lon, lat, outer)
        and not any(_point_in_ring(lon, lat, hole) for hole in holes)
        for outer, holes in polygons
    )


def bounding_box(polygons):
    """(min_lat, min_lon, max_lat, max_lon) of all outer rings."""
    points = [p for outer, _ in polygons for p in outer]
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    return min(lats), min(lons), max(lats), max(lons)
