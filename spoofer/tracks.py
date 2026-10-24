"""Turn routes into tracks: lists of (lat, lon) with one point per ROAM_STEP_SECONDS, ready to play."""

import random

from .geo import densify, destination_point, haversine_m, random_point_near
from .routing import route_chunked

ROAM_STEP_SECONDS = 1
WANDER_PAUSE_EVERY_S = 180  # on average, stop for a moment about every 3 minutes
WANDER_PAUSE_RANGE_S = (10, 45)
GEOFENCE_SETTLE_S = 30  # hold still outside before each pass so the OS settles on "outside"
GOLDEN_ANGLE = 137.508  # successive headings that never repeat and cover the circle evenly


def step_m(kmh):
    """Distance covered per tick at kmh."""
    return kmh / 3.6 * ROAM_STEP_SECONDS


def plan_legs(start, targets, profile, roads, kmh, log):
    """Route start -> each (name, lat, lon) target in turn.
    Returns (track, ends) where ends[i] = (index in track where targets[i] is reached, target)."""
    track, ends = [start], []
    for tgt in targets:
        path = route_chunked([track[-1], tgt[1:]], profile, roads, log)
        track.extend(densify(path, step_m(kmh))[1:])
        ends.append((len(track) - 1, tgt))
    return track, ends


def build_roam_track(pos, remaining, lap, loop, profile, roads, kmh, log):
    """Track from pos through the remaining waypoints (back to lap[0] when looping).
    Returns (track, ends, lap) where lap is (lap_track, lap_ends) to repeat forever, or None."""
    track, ends = plan_legs(pos, remaining + ([lap[0]] if loop and lap else []), profile, roads, kmh, log)
    if loop and len(lap) >= 2:
        lap_track, lap_ends = plan_legs(lap[0][1:], lap[1:] + [lap[0]], profile, roads, kmh, log)
        if len(lap_track) > 1:
            return track, ends, (lap_track, lap_ends)
    return track, ends, None


def add_pauses(track):
    """Insert short standstills at random, like someone stopping to look around."""
    out = []
    for p in track:
        out.append(p)
        if random.random() < ROAM_STEP_SECONDS / WANDER_PAUSE_EVERY_S:
            out.extend([p] * (random.randint(*WANDER_PAUSE_RANGE_S) // ROAM_STEP_SECONDS))
    return out


def generate_wander(center, radius_m, secs, kmh, profile, roads, pauses, log):
    """Random walk around center for `secs` seconds."""
    # Pick random stops until the straight-line distance covers the walk; roads only make it longer.
    need_m = kmh / 3.6 * secs
    stops, dist = [center], 0.0
    while dist < need_m:
        nxt = random_point_near(center, radius_m)
        dist += haversine_m(stops[-1], nxt)
        stops.append(nxt)
    track = densify(route_chunked(stops, profile, roads, log), step_m(kmh))
    if pauses:
        track = add_pauses(track)
    return track[:max(1, int(secs // ROAM_STEP_SECONDS))]


def build_geofence_track(center, radius_m, speeds_kmh, passes, dwell_s, margin_m):
    """Straight passes through a circle, alternating speeds, at spread-out headings.
    Returns (track, marks): the route plus (index, text, tag) marks for each pass and boundary crossing."""
    track, marks = [], []
    outer = radius_m + margin_m
    total = passes * len(speeds_kmh)
    inside = False
    settle = GEOFENCE_SETTLE_S // ROAM_STEP_SECONDS
    for k in range(total):
        kmh = speeds_kmh[k % len(speeds_kmh)]
        bearing = (k * GOLDEN_ANGLE) % 360
        start = destination_point(center, bearing, outer)
        end = destination_point(center, bearing + 180, outer)
        heading = (bearing + 180) % 360
        marks.append((len(track), f"Geofence pass {k + 1}/{total}: {kmh:g} km/h heading {heading:.0f}°", "step"))
        # Jumping between passes is safe: both ends are outside, so no crossing is skipped.
        track.extend([start] * settle)
        step = step_m(kmh)
        if dwell_s:
            leg = (densify([start, center], step) + [center] * int(dwell_s // ROAM_STEP_SECONDS)
                   + densify([center, end], step)[1:])
        else:
            leg = densify([start, end], step)
        for p in leg[1:]:
            now_inside = haversine_m(center, p) <= radius_m
            if now_inside != inside:
                verb, tag = ("ENTER", "enter") if now_inside else ("EXIT", "exit")
                marks.append((len(track), f"{verb} geofence (pass {k + 1}, {kmh:g} km/h)", tag))
                inside = now_inside
            track.append(p)
    track.extend([track[-1]] * settle)
    return track, marks
