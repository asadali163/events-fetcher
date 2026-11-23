"""Impact radius from attendance: the labelling rubric's tiers, no LLM.

Inside a tier the radius grows with log(attendance); rounded to 50 m,
minimum 100 m; open-air events get +35%.
"""

import math

# (attendance from, attendance to, radius from, radius to)
RADIUS_TIERS = [
    (0, 50, 100, 100), (50, 200, 100, 150), (200, 500, 200, 300), (500, 1500, 300, 500),
    (1500, 3000, 500, 700), (3000, 8000, 800, 1000), (8000, 20000, 1000, 1500), (20000, 80000, 1500, 2500),
]
OUTDOOR_RADIUS_FACTOR = 1.35
MIN_RADIUS = 100


def radius_from_attendance(attendance, outdoor=False):
    if attendance is None or (isinstance(attendance, float) and math.isnan(attendance)):
        return None
    if attendance <= 0:
        return 0
    top = RADIUS_TIERS[-1][1]
    a = min(attendance, top)
    radius = RADIUS_TIERS[-1][3]
    for a0, a1, r0, r1 in RADIUS_TIERS:
        if a < a1 or a1 == top:
            position = 0 if a0 == 0 else math.log(max(a, a0) / a0) / math.log(a1 / a0)
            radius = r0 + (r1 - r0) * position
            break
    if outdoor:
        radius *= OUTDOOR_RADIUS_FACTOR
    return max(MIN_RADIUS, int(round(radius / 50) * 50))
