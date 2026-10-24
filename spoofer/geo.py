"""Pure geometry on (lat, lon) tuples. No I/O."""

import math
import random

EARTH_RADIUS_M = 6371000


def haversine_m(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def path_length_m(points):
    return sum(haversine_m(a, b) for a, b in zip(points, points[1:]))


def densify(path, step_m):
    """Resample a polyline into points step_m apart, i.e. one point per tick at constant speed."""
    out = [path[0]]
    carry = 0.0  # distance travelled since the last emitted point
    for a, b in zip(path, path[1:]):
        seg = haversine_m(a, b)
        if seg == 0:
            continue
        pos = step_m - carry  # where along this segment the next point falls
        while pos <= seg:
            t = pos / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            pos += step_m
        carry = seg - (pos - step_m)
    # Always finish exactly on the destination: snap a near-miss, otherwise add a final short step.
    if carry > 0.5:
        out.append(path[-1])
    else:
        out[-1] = path[-1]
    return out


def random_point_near(center, radius_m):
    """Uniformly random point within radius_m of center."""
    r = radius_m * math.sqrt(random.random())
    theta = random.uniform(0, 2 * math.pi)
    dlat = r * math.cos(theta) / 111_320
    dlon = r * math.sin(theta) / (111_320 * max(math.cos(math.radians(center[0])), 1e-6))
    return center[0] + dlat, center[1] + dlon


def destination_point(origin, bearing_deg, dist_m):
    """Point dist_m from origin along a great circle at the given bearing."""
    lat1, lon1 = map(math.radians, origin)
    brg, d = math.radians(bearing_deg), dist_m / EARTH_RADIUS_M
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(brg))
    lon2 = lon1 + math.atan2(math.sin(brg) * math.sin(d) * math.cos(lat1), math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180


def circle_points(center, radius_m, n=72):
    return [destination_point(center, 360 * i / n, radius_m) for i in range(n)]


def split_at_jumps(points, min_gap_m=60):
    """Split a track into continuous segments wherever it teleports, so jumps aren't drawn as roads."""
    gaps = sorted(d for d in (haversine_m(a, b) for a, b in zip(points, points[1:])) if d > 0)
    limit = max(min_gap_m, 3 * gaps[len(gaps) // 2]) if gaps else min_gap_m
    segments = [[points[0]]] if points else []
    for a, b in zip(points, points[1:]):
        if haversine_m(a, b) > limit:
            segments.append([])
        segments[-1].append(b)
    return segments


def thin(points, max_points):
    """Evenly subsample a long point list for drawing, always keeping the last point."""
    if len(points) <= max_points:
        return list(points)
    step = len(points) / max_points
    return [points[int(i * step)] for i in range(max_points)] + [points[-1]]
