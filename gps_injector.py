"""
GPS Injector - Tkinter front end for pymobiledevice3 location simulation.

Drives `pymobiledevice3 developer dvt simulate-location set/clear` against a
connected iOS device for integration testing of location-aware apps.

Requirements on the workstation:
  * pip install pymobiledevice3   (invoked as an external CLI, not bundled)
  * Device paired/trusted, Developer Mode enabled, DDI mounted
  * iOS 17+: a running tunnel daemon in an elevated shell:
        python -m pymobiledevice3 remote tunneld
"""

import json
import math
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import tkintermapview
    import tkintermapview.map_widget
except ImportError:  # the app still works without the map
    tkintermapview = None

APP_TITLE = "Spoofer"


def resource_path(rel):
    """Path to a bundled file, both from source and inside a PyInstaller --onefile exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

# Benchmark waypoints for QA. The last group covers edge cases that tend to
# break geo code (null island, antimeridian, near-polar, southern/western hemi).
PRESETS = [
    ("Apple Park, Cupertino", 37.334900, -122.009020),
    ("Times Square, NYC", 40.758000, -73.985500),
    ("Big Ben, London", 51.500729, -0.124625),
    ("Eiffel Tower, Paris", 48.858370, 2.294481),
    ("Tokyo Station", 35.681236, 139.767125),
    ("Sydney Opera House", -33.856784, 151.215297),
    ("Null Island (0, 0)", 0.0, 0.0),
    ("Antimeridian, Fiji", -16.500000, 179.999000),
    ("Near North Pole", 89.900000, 0.0),
    ("Ushuaia, Argentina", -54.801912, -68.302951),
]

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
TUNNELD_URL = "http://127.0.0.1:49151/"
RECONNECT_DELAY_MS = 5000
MAX_RECONNECTS = 10

# OpenStreetMap Nominatim: free, no key. Its usage policy requires an identifying
# User-Agent and at most 1 request per second.
GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_USER_AGENT = "Spoofer/1.0 (iOS location QA tool)"
GEOCODE_MIN_INTERVAL = 1.0

class _TileRequests:
    """tkintermapview fetches tiles with a generic User-Agent that OpenStreetMap's tile servers
    block (they answer with an 'Access blocked' image), so send an identifying one instead."""

    def __init__(self, real):
        self._real = real

    def get(self, url, **kwargs):
        kwargs["headers"] = {**kwargs.get("headers", {}), "User-Agent": GEOCODE_USER_AGENT}
        return self._real.get(url, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


if tkintermapview is not None:
    tkintermapview.map_widget.requests = _TileRequests(tkintermapview.map_widget.requests)

BOOKMARKS_PATH = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "Spoofer", "bookmarks.json"
)


def geocode(query, limit=5):
    """Return [(display_name, lat, lon), ...] for a free-text address."""
    params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": limit})
    req = urllib.request.Request(f"{GEOCODE_URL}?{params}", headers={"User-Agent": GEOCODE_USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        results = json.loads(resp.read().decode("utf-8"))
    return [(r["display_name"], float(r["lat"]), float(r["lon"])) for r in results]


# Roaming: routes come from the FOSSGIS OSRM servers (OpenStreetMap data, no key),
# then get resampled to one point per second and replayed as a GPX file.
ROUTE_URL = "https://routing.openstreetmap.de/{profile}/route/v1/driving/{coords}?overview=full&geometries=geojson"
ROAM_MODES = {  # name -> (default km/h, routing profile)
    "Walk": (5, "routed-foot"),
    "Run": (10, "routed-foot"),
    "Cycle": (18, "routed-bike"),
    "Drive (city)": (40, "routed-car"),
    "Drive (highway)": (100, "routed-car"),
}
ROAM_STEP_SECONDS = 1
ROAM_LOOP_HOURS = 8  # a looping route is repeated in the GPX up to this long...
ROAM_MAX_POINTS = 100_000  # ...or this many points, whichever is smaller
GPX_PATH = os.path.join(tempfile.gettempdir(), "Spoofer", "route.gpx")


ROUTE_MAX_COORDS = 25  # waypoints per routing request; longer wanders are routed in chunks
WANDER_PAUSE_EVERY_S = 180  # on average, stop for a moment about every 3 minutes
WANDER_PAUSE_RANGE_S = (10, 45)


def random_point_near(center, radius_m):
    """Uniformly random point within radius_m of center."""
    r = radius_m * math.sqrt(random.random())
    theta = random.uniform(0, 2 * math.pi)
    dlat = r * math.cos(theta) / 111_320
    dlon = r * math.sin(theta) / (111_320 * max(math.cos(math.radians(center[0])), 1e-6))
    return center[0] + dlat, center[1] + dlon


def route_chunked(points, profile, roads, log):
    """Route through many points, ROUTE_MAX_COORDS at a time; straight lines where routing fails."""
    path = []
    step = ROUTE_MAX_COORDS - 1
    for i in range(0, len(points) - 1, step):
        chunk = points[i:i + ROUTE_MAX_COORDS]
        part = chunk
        if roads:
            try:
                part = route_on_roads(chunk, profile)
            except Exception as exc:  # noqa: BLE001 - fall back rather than fail the roam
                log(f"Road routing failed ({exc}); using straight lines for this stretch.", "err")
        path.extend(part if not path else part[1:])
    return path


def add_pauses(track):
    """Insert short standstills at random, like someone stopping to look around."""
    out = []
    for p in track:
        out.append(p)
        if random.random() < ROAM_STEP_SECONDS / WANDER_PAUSE_EVERY_S:
            out.extend([p] * (random.randint(*WANDER_PAUSE_RANGE_S) // ROAM_STEP_SECONDS))
    return out


# Live sessions: instead of `simulate-location play <gpx>` (which reads the whole route up front),
# run pymobiledevice3's own CLI with `play` swapped for a loop that takes "lat lon" lines on stdin.
# Connection handling (--tunnel/--udid, errors) stays exactly the CLI's; Spoofer feeds one point per
# tick, so routes can be edited mid-run without dropping the DVT session.
LIVE_READY = "SPOOFER LIVE READY"
LIVE_HELPER = r"""
import asyncio, os, sys
from pymobiledevice3.services.dvt.instruments import location_simulation_base as lsb

async def _live(self, filename, disable_sleep=False, timing_randomness_range=0):
    loop = asyncio.get_running_loop()
    print("SPOOFER LIVE READY", flush=True)
    while True:
        parts = (await loop.run_in_executor(None, sys.stdin.readline)).split()
        if len(parts) != 2:
            os._exit(0)  # blank line or EOF: end the session
        await self.set(float(parts[0]), float(parts[1]))

lsb.LocationSimulationBase.play_gpx_file = _live
from pymobiledevice3.__main__ import main
sys.argv = ["pymobiledevice3", "developer", "dvt", "simulate-location", "play"] + sys.argv[1:]
sys.exit(main())
"""


def helper_python(base):
    """The Python that runs pymobiledevice3, derived from the CLI command."""
    exe = base[0]
    if os.path.basename(exe).lower().startswith("python"):
        return exe  # "python -m pymobiledevice3"
    for cand in (os.path.join(os.path.dirname(exe), "python.exe"),  # venv: Scripts\python.exe
                 os.path.join(os.path.dirname(os.path.dirname(exe)), "python.exe")):  # Python\Scripts\..
        if os.path.isfile(cand):
            return cand
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python") or "python"


def plan_legs(start, targets, profile, roads, kmh, log):
    """Route start -> each (name, lat, lon) target in turn.
    Returns (track, ends) where ends[i] = (index in track where targets[i] is reached, target)."""
    track, ends = [start], []
    step = kmh / 3.6 * ROAM_STEP_SECONDS
    for tgt in targets:
        path = route_chunked([track[-1], tgt[1:]], profile, roads, log)
        track.extend(densify(path, step)[1:])
        ends.append((len(track) - 1, tgt))
        if len(track) > ROAM_MAX_POINTS:
            break
    return track, ends


def build_roam_track(pos, remaining, lap, loop, profile, roads, kmh, log):
    """Track from pos through the remaining waypoints; when looping, back to lap[0] and then full laps."""
    track, ends = plan_legs(pos, remaining + ([lap[0]] if loop and lap else []), profile, roads, kmh, log)
    if loop and len(lap) >= 2:
        lap_track, lap_ends = plan_legs(lap[0][1:], lap[1:] + [lap[0]], profile, roads, kmh, log)
        cap = min(ROAM_MAX_POINTS, ROAM_LOOP_HOURS * 3600 // ROAM_STEP_SECONDS)
        while len(lap_track) > 1 and len(track) + len(lap_track) - 1 <= cap:
            offset = len(track) - 1
            track.extend(lap_track[1:])
            ends.extend((i + offset, t) for i, t in lap_ends)
    return track, ends


def generate_wander(center, radius_m, secs, kmh, profile, roads, pauses, log):
    """Random walk around center for `secs` seconds, one point per ROAM_STEP_SECONDS."""
    # Pick random stops until the straight-line distance covers the walk; roads only make it longer.
    need_m = kmh / 3.6 * secs
    stops, dist = [center], 0.0
    while dist < need_m and len(stops) < 500:
        nxt = random_point_near(center, radius_m)
        dist += haversine_m(stops[-1], nxt)
        stops.append(nxt)
    track = densify(route_chunked(stops, profile, roads, log), kmh / 3.6 * ROAM_STEP_SECONDS)
    if pauses:
        track = add_pauses(track)
    return track[:max(1, int(secs // ROAM_STEP_SECONDS))]


def destination_point(origin, bearing_deg, dist_m):
    """Point dist_m from origin along a great circle at the given bearing."""
    lat1, lon1 = map(math.radians, origin)
    brg, d = math.radians(bearing_deg), dist_m / 6371000
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


# Geofence tester: straight passes through a circle, alternating speeds, at spread-out headings.
GEOFENCE_SETTLE_S = 30  # hold still outside before each pass so the OS settles on "outside"
GOLDEN_ANGLE = 137.508  # successive headings that never repeat and cover the circle evenly


def build_geofence_track(center, radius_m, speeds_kmh, passes, dwell_s, margin_m):
    """Return (track, marks): the route plus (index, text, tag) marks for each pass and boundary crossing."""
    track, marks = [], []
    outer = radius_m + margin_m
    total = passes * len(speeds_kmh)
    inside = False
    for k in range(total):
        kmh = speeds_kmh[k % len(speeds_kmh)]
        bearing = (k * GOLDEN_ANGLE) % 360
        start = destination_point(center, bearing, outer)
        end = destination_point(center, bearing + 180, outer)
        heading = (bearing + 180) % 360
        marks.append((len(track), f"Geofence pass {k + 1}/{total}: {kmh:g} km/h heading {heading:.0f}°", "step"))
        # Jumping between passes is safe: both ends are outside, so no crossing is skipped.
        track.extend([start] * (GEOFENCE_SETTLE_S // ROAM_STEP_SECONDS))
        step = kmh / 3.6 * ROAM_STEP_SECONDS
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
    track.extend([track[-1]] * (GEOFENCE_SETTLE_S // ROAM_STEP_SECONDS))
    return track, marks


# Scenarios: a tiny line-based script compiled into one continuous track.
class ScenarioError(ValueError):
    pass


SCENARIO_EXAMPLE = """\
# Spoofer scenario - one command per line, '#' starts a comment.
# Places: "an address", a bookmark as @Name, or lat, lon
start "Times Square, New York"
wait 30s
walk to "Bryant Park, New York"
wait 2m
wander 150m for 5m
walk to 40.748441, -73.985664      # Empire State Building
drive to "Central Park, New York" at 30 km/h
wait 1m
clear
"""

SCENARIO_MOVES = {"walk": "Walk", "run": "Run", "cycle": "Cycle", "bike": "Cycle", "drive": "Drive (city)"}
_COORD_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)$")
_SPEED_RE = re.compile(r"\s+at\s+(\d+(?:\.\d+)?)\s*(?:km/?h|kph)?\s*$", re.I)
_DUR_UNITS = {"": 1, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
              "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
              "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}


def parse_duration(text):
    """'90', '30s', '2m', '1h30m', '1.5 hours' -> seconds."""
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]*)", text.lower())
    if not parts or re.sub(r"(\d+(?:\.\d+)?)\s*([a-z]*)", "", text.lower()).strip():
        raise ScenarioError(f"can't read duration '{text}' (try 30s, 2m, 1h)")
    secs = 0.0
    for num, unit in parts:
        if unit not in _DUR_UNITS:
            raise ScenarioError(f"unknown time unit '{unit}' (use s, m or h)")
        secs += float(num) * _DUR_UNITS[unit]
    if secs <= 0:
        raise ScenarioError("duration must be more than 0")
    return int(round(secs))


def parse_distance(text):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m|km)?\s*", text.lower())
    if not m:
        raise ScenarioError(f"can't read distance '{text}' (try 300m or 1.5km)")
    return float(m.group(1)) * (1000 if m.group(2) == "km" else 1)


def parse_place(text):
    text = text.strip()
    if not text:
        raise ScenarioError("missing place")
    m = _COORD_RE.match(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ScenarioError(f"coordinates out of range: {text}")
        return ("coords", (lat, lon))
    if text.startswith("@"):
        return ("bookmark", text[1:].strip())
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return ("address", text)


def _strip_comment(line):
    quote = None
    for i, ch in enumerate(line):
        if ch in "\"'" and quote in (None, ch):
            quote = None if quote else ch
        elif ch == "#" and quote is None:
            return line[:i]
    return line


def _take_speed(rest):
    m = _SPEED_RE.search(rest)
    if not m:
        return rest, None
    kmh = float(m.group(1))
    if not 0 < kmh <= 1000:
        raise ScenarioError("speed must be between 0 and 1000 km/h")
    return rest[:m.start()], kmh


def parse_scenario(text):
    """Validate a scenario script; returns a list of step dicts (no network access)."""
    steps = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if steps and steps[-1]["op"] == "clear":
            raise ScenarioError(f"Line {n}: 'clear' must be the last command")
        verb, _, rest = line.partition(" ")
        verb, rest = verb.lower(), rest.strip()
        try:
            if verb in ("start", "goto", "teleport"):
                step = {"op": "jump", "place": parse_place(rest)}
            elif verb == "wait":
                step = {"op": "wait", "secs": parse_duration(rest)}
            elif verb in SCENARIO_MOVES:
                if not rest.lower().startswith("to "):
                    raise ScenarioError(f"expected '{verb} to <place>'")
                rest, kmh = _take_speed(rest[3:])
                mode = SCENARIO_MOVES[verb]
                step = {"op": "move", "mode": mode, "kmh": kmh or ROAM_MODES[mode][0], "place": parse_place(rest)}
            elif verb == "wander":
                rest, kmh = _take_speed(rest)
                radius, sep, dur = rest.partition(" for ")
                if not sep:
                    raise ScenarioError("expected 'wander <radius> for <duration>'")
                step = {"op": "wander", "radius": parse_distance(radius), "secs": parse_duration(dur),
                        "kmh": kmh or ROAM_MODES["Walk"][0]}
            elif verb == "roads":
                if rest.lower() not in ("on", "off"):
                    raise ScenarioError("expected 'roads on' or 'roads off'")
                step = {"op": "roads", "on": rest.lower() == "on"}
            elif verb == "clear":
                if rest:
                    raise ScenarioError("'clear' takes no arguments")
                step = {"op": "clear"}
            else:
                raise ScenarioError(f"unknown command '{verb}' (start, wait, walk/run/cycle/drive to, "
                                    "wander, roads, clear)")
        except ScenarioError as exc:
            raise ScenarioError(f"Line {n}: {exc}") from None
        step.update(line=n, text=line)
        steps.append(step)
    if not steps:
        raise ScenarioError("The scenario is empty.")
    return steps


def compile_scenario(steps, start, resolve, log):
    """Turn parsed steps into (track, marks, clear_at_end). resolve(place) -> (lat, lon) may use the network."""
    track, marks = [], []
    pos, roads, clear_at_end = start, True, False
    for i, st in enumerate(steps, 1):
        label = f"Step {i}/{len(steps)} (line {st['line']}): {st['text']}"
        if st["op"] == "jump":
            pos = resolve(st["place"])
            marks.append((len(track), label, "step"))
            track.append(pos)
            continue
        if not track and st["op"] in ("wait", "move", "wander"):
            track.append(pos)  # movement before any 'start' begins from the current coords
        marks.append((len(track), label, "step"))
        if st["op"] == "wait":
            track.extend([pos] * (st["secs"] // ROAM_STEP_SECONDS))
        elif st["op"] == "move":
            dest = resolve(st["place"])
            profile = ROAM_MODES[st["mode"]][1]
            path = route_chunked([pos, dest], profile, roads, log)
            track.extend(densify(path, st["kmh"] / 3.6 * ROAM_STEP_SECONDS)[1:])
        elif st["op"] == "wander":
            track.extend(generate_wander(pos, st["radius"], st["secs"], st["kmh"], "routed-foot", roads, True, log))
        elif st["op"] == "roads":
            roads = st["on"]
        elif st["op"] == "clear":
            clear_at_end = True
        if track:
            pos = track[-1]
        if len(track) > ROAM_MAX_POINTS:
            raise ScenarioError(f"Scenario is longer than {ROAM_MAX_POINTS * ROAM_STEP_SECONDS // 3600} hours.")
    if not track:
        raise ScenarioError("The scenario never goes anywhere - add a start, wait or move command.")
    # A mark past the end (e.g. a trailing 'roads' or 'clear') would never fire; pin it to the last point.
    marks = [(min(idx, len(track) - 1), text, tag) for idx, text, tag in marks]
    return track, marks, clear_at_end


def haversine_m(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def route_on_roads(waypoints, profile):
    """Return the road-following polyline [(lat, lon), ...] through the waypoints."""
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)
    req = urllib.request.Request(
        ROUTE_URL.format(profile=profile, coords=coords), headers={"User-Agent": GEOCODE_USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(data.get("message") or data.get("code") or "no route")
    return [(lat, lon) for lon, lat in data["routes"][0]["geometry"]["coordinates"]]


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


def write_gpx(points, path):
    start = datetime.now(timezone.utc)
    rows = [
        f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}"><time>'
        f"{(start + timedelta(seconds=i * ROAM_STEP_SECONDS)).strftime('%Y-%m-%dT%H:%M:%SZ')}</time></trkpt>"
        for i, (lat, lon) in enumerate(points)
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<gpx version="1.1" creator="Spoofer" xmlns="http://www.topografix.com/GPX/1/1">'
                "<trk><trkseg>\n" + "\n".join(rows) + "\n</trkseg></trk></gpx>\n")


def load_bookmarks():
    try:
        with open(BOOKMARKS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return [b for b in data if {"name", "lat", "lon"} <= b.keys()]
    except FileNotFoundError:
        return []


def save_bookmarks(bookmarks):
    os.makedirs(os.path.dirname(BOOKMARKS_PATH), exist_ok=True)
    tmp = BOOKMARKS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bookmarks, f, indent=2, ensure_ascii=False)
    os.replace(tmp, BOOKMARKS_PATH)  # atomic, so a crash can't truncate the file


def default_cli_command():
    """Locate pymobiledevice3: prefer the console script on PATH, else `python -m`."""
    exe = shutil.which("pymobiledevice3")
    if exe:
        return f'"{exe}"'
    # When frozen by PyInstaller sys.executable is our own .exe, so fall back
    # to whatever Python is on PATH.
    py = sys.executable if not getattr(sys, "frozen", False) else (shutil.which("python") or "python")
    return f'"{py}" -m pymobiledevice3'


def split_command(cmd):
    """Split a user-editable command string, honouring double quotes (Windows paths)."""
    parts, cur, in_q = [], "", False
    for ch in cmd.strip():
        if ch == '"':
            in_q = not in_q
        elif ch.isspace() and not in_q:
            if cur:
                parts.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts


class ProcessManager:
    """Runs CLI commands in background threads and streams output to a queue."""

    def __init__(self, log_queue):
        self.log_queue = log_queue
        self.active = None  # long-running `simulate-location set` process
        self.lock = threading.Lock()

    def log(self, text, tag="info"):
        self.log_queue.put((text, tag))

    def _spawn(self, args):
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.log("$ " + subprocess.list2cmdline(args), "cmd")
        return subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )

    def _pump(self, stream, tag, line_cb=None):
        for line in iter(stream.readline, ""):
            line = line.rstrip()
            # line_cb can inspect each line and return False to keep it out of the log.
            if line_cb is None or line_cb(line):
                self.log(line, tag)
        stream.close()

    def _watch(self, proc, label, on_exit=None, line_cb=None):
        readers = [
            threading.Thread(target=self._pump, args=(proc.stdout, "out", line_cb), daemon=True),
            threading.Thread(target=self._pump, args=(proc.stderr, "err", line_cb), daemon=True),
        ]
        for r in readers:
            r.start()
        code = proc.wait()
        for r in readers:
            r.join(timeout=2)
        self.log(f"[{label}] exited with code {code}", "ok" if code == 0 else "err")
        with self.lock:
            if self.active is proc:
                self.active = None
        if on_exit:
            on_exit(code)

    def run_once(self, args, label, on_exit=None):
        """Run a short command (clear, list, mount) in the background."""

        def worker():
            try:
                proc = self._spawn(args)
            except (OSError, ValueError) as exc:
                self.log(f"[{label}] failed to start: {exc}", "err")
                if on_exit:
                    on_exit(-1)
                return
            self._watch(proc, label, on_exit)

        threading.Thread(target=worker, daemon=True).start()

    def run_persistent(self, args, label, on_exit=None, line_cb=None):
        """Start `simulate-location set`/`play`. The DVT session must stay open for the
        simulated location to persist, so the process is kept alive until stopped."""

        def worker():
            self._stop_active()
            try:
                proc = self._spawn(args)
            except (OSError, ValueError) as exc:
                self.log(f"[{label}] failed to start: {exc}", "err")
                if on_exit:
                    on_exit(-1)
                return
            with self.lock:
                self.active = proc
            self._watch(proc, label, on_exit, line_cb)

        threading.Thread(target=worker, daemon=True).start()

    def _stop_active(self):
        with self.lock:
            proc = self.active
            self.active = None
        if not proc or proc.poll() is not None:
            return
        self.log("Stopping active simulation session...", "info")
        try:
            # pymobiledevice3 waits for ENTER / Ctrl+C before closing the session.
            proc.stdin.write("\n")
            proc.stdin.flush()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop_active_async(self, then=None):
        def worker():
            self._stop_active()
            if then:
                then()

        threading.Thread(target=worker, daemon=True).start()

    def is_active(self):
        with self.lock:
            return self.active is not None and self.active.poll() is None

    def send(self, text):
        """Write a line to the active session's stdin (live sessions). False if it isn't running."""
        with self.lock:
            proc = self.active
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.stdin.write(text + "\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        try:
            self.iconbitmap(default=resource_path(os.path.join("assets", "icon.ico")))
        except tk.TclError:
            pass  # icon missing: keep Tk's default rather than fail
        self.geometry("1500x880")
        self.minsize(1150, 720)

        self.log_queue = queue.Queue()
        self.pm = ProcessManager(self.log_queue)

        self.lat_var = tk.StringVar(value="37.334900")
        self.lon_var = tk.StringVar(value="-122.009020")
        self.cli_var = tk.StringVar(value=default_cli_command())
        self.udid_var = tk.StringVar()
        self.tunnel_var = tk.BooleanVar(value=True)
        self.reconnect_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Idle")
        # Bumped on every Set/Clear so stale sessions never trigger a reconnect.
        self.session_id = 0
        self.established_sid = None
        self.retries = 0

        self.address_var = tk.StringVar()
        self.matches = []  # geocoder results for the match dropdown
        self.last_geocode = 0.0
        # (label, lat_str, lon_str) of the last place picked, used to name new bookmarks.
        self.current_place = None
        try:
            self.bookmarks = load_bookmarks()
        except (OSError, ValueError) as exc:
            self.bookmarks = []
            messagebox.showwarning("Bookmarks", f"Could not read {BOOKMARKS_PATH}:\n{exc}")

        # Roaming state. roam_done is advanced by the play process's reader thread.
        self.roam_waypoints = []  # [(name, lat, lon), ...]
        self.roam_track = []  # densified points, one per ROAM_STEP_SECONDS
        self.roam_done = 0
        self.roam_sid = None  # session_id of the active roam, if any
        self.roam_mode_var = tk.StringVar(value="Walk")
        self.roam_speed_var = tk.StringVar(value=str(ROAM_MODES["Walk"][0]))
        self.roam_roads_var = tk.BooleanVar(value=True)
        self.roam_loop_var = tk.BooleanVar(value=False)
        self.wander_radius_var = tk.StringVar(value="300")
        self.wander_minutes_var = tk.StringVar(value="60")
        self.wander_pause_var = tk.BooleanVar(value=True)
        # (index, text, tag) marks logged as playback reaches them; on_finish runs once at the end.
        self.roam_marks = {}
        self.roam_step = ""
        self.roam_on_finish = None
        self.roam_plan = None  # what a live edit can re-plan: {"kind": "Route"/"Wander", "ends": [...]}
        self.roam_paused = False
        self.roam_lock = threading.Lock()  # roam_track/roam_done are shared with the player thread
        self.live_ready = threading.Event()  # set once the live helper has connected
        self._edit_job = None
        self.planning_buttons = []  # disabled while a route is being planned

        self.scenario_path = None
        self.gf_center = None
        self.gf_radius_var = tk.StringVar(value="150")
        self.gf_margin_var = tk.StringVar(value="150")
        self.gf_speeds_var = tk.StringVar(value="5, 40")
        self.gf_passes_var = tk.StringVar(value="2")
        self.gf_dwell_var = tk.StringVar(value="30")
        self.gf_center_var = tk.StringVar(value="not set - right-click the map or use current coords")

        # Map state: where the phone is, what's drawn, and what was drawn last.
        self.sim_pos = None  # last fixed location set (non-roaming)
        self.follow_var = tk.BooleanVar(value=True)
        self.map = None
        self.map_items = {}  # name -> marker/path/polygon
        self.wp_markers = []
        self.drawn_pos = None
        self.drawn_trail = -1

        self._build_ui()
        self._refresh_bookmarks()
        for var in (self.roam_speed_var, self.roam_roads_var, self.roam_loop_var):
            var.trace_add("write", self._live_edit)
        threading.Thread(target=self._player, daemon=True).start()
        self.after(100, self._drain_log)
        self.after(500, self._refresh_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", **pad)
        ttk.Label(bottom, text="Status:").pack(side="left")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", padx=4)
        ttk.Button(bottom, text="Clear log", command=self.clear_log).pack(side="right")
        ttk.Button(bottom, text="Save log", command=self.save_log).pack(side="right", padx=4)

        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=0)
        paned.add(right, weight=1)
        self._build_map(right)

        conn = ttk.LabelFrame(left, text="Connection")
        conn.pack(fill="x", **pad)
        ttk.Label(conn, text="CLI command:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(conn, textvariable=self.cli_var).grid(row=0, column=1, columnspan=3, sticky="ew", **pad)
        ttk.Label(conn, text="UDID (optional):").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(conn, textvariable=self.udid_var, width=40).grid(row=1, column=1, sticky="w", **pad)
        ttk.Checkbutton(conn, text="iOS 17+ (use tunneld)", variable=self.tunnel_var).grid(
            row=1, column=2, sticky="w", **pad
        )
        ttk.Checkbutton(conn, text="Auto-reconnect (Wi-Fi)", variable=self.reconnect_var).grid(
            row=1, column=3, sticky="w", **pad
        )
        btns = ttk.Frame(conn)
        btns.grid(row=2, column=0, columnspan=4, sticky="w", **pad)
        ttk.Button(btns, text="Start tunnel (admin)", command=self.start_tunnel).pack(side="left", padx=3)
        ttk.Button(btns, text="Check tunnel", command=self.check_tunnel).pack(side="left", padx=3)
        ttk.Button(btns, text="List devices", command=self.list_devices).pack(side="left", padx=3)
        ttk.Button(btns, text="Auto-mount DDI", command=self.mount_ddi).pack(side="left", padx=3)
        conn.columnconfigure(1, weight=1)

        coords = ttk.LabelFrame(left, text="Location")
        coords.pack(fill="x", **pad)
        ttk.Label(coords, text="Address:").grid(row=0, column=0, sticky="w", **pad)
        addr_entry = ttk.Entry(coords, textvariable=self.address_var)
        addr_entry.grid(row=0, column=1, columnspan=3, sticky="ew", **pad)
        addr_entry.bind("<Return>", lambda _e: self.search_address())
        self.search_btn = ttk.Button(coords, text="Search", command=self.search_address)
        self.search_btn.grid(row=0, column=4, **pad)
        ttk.Button(coords, text="Search & Set", command=lambda: self.search_address(then_set=True)).grid(
            row=0, column=5, **pad
        )
        ttk.Label(coords, text="Matches:").grid(row=1, column=0, sticky="w", **pad)
        self.match_box = ttk.Combobox(coords, state="readonly")
        self.match_box.grid(row=1, column=1, columnspan=5, sticky="ew", **pad)
        self.match_box.bind("<<ComboboxSelected>>", self._on_match_selected)

        ttk.Label(coords, text="Latitude:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(coords, textvariable=self.lat_var, width=18).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(coords, text="Longitude:").grid(row=2, column=2, sticky="w", **pad)
        ttk.Entry(coords, textvariable=self.lon_var, width=18).grid(row=2, column=3, sticky="w", **pad)
        ttk.Button(coords, text="Set location", command=self.set_location).grid(row=2, column=4, **pad)
        ttk.Button(coords, text="Clear location", command=self.clear_location).grid(row=2, column=5, **pad)
        coords.columnconfigure(1, weight=1)
        coords.columnconfigure(3, weight=1)

        self.tabs = ttk.Notebook(left)
        self.tabs.pack(fill="x", **pad)
        presets = ttk.Frame(self.tabs, padding=4)
        self.tabs.add(presets, text="Presets")
        cols = 5
        for i, (name, lat, lon) in enumerate(PRESETS):
            ttk.Button(
                presets, text=name, command=lambda la=lat, lo=lon, n=name: self.apply_preset(n, la, lo)
            ).grid(row=i // cols, column=i % cols, sticky="ew", padx=3, pady=3)
        for c in range(cols):
            presets.columnconfigure(c, weight=1)

        bm = ttk.Frame(self.tabs, padding=2)
        self.tabs.add(bm, text="Bookmarks")
        self.bm_list = tk.Listbox(bm, height=6, activestyle="none", exportselection=False)
        bm_sb = ttk.Scrollbar(bm, command=self.bm_list.yview)
        self.bm_list.configure(yscrollcommand=bm_sb.set)
        self.bm_list.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
        bm_sb.pack(side="left", fill="y", pady=4)
        self.bm_list.bind("<Double-Button-1>", lambda _e: self.go_bookmark())
        self.bm_list.bind("<Return>", lambda _e: self.go_bookmark())
        self.bm_list.bind("<Delete>", lambda _e: self.delete_bookmark())
        bm_btns = ttk.Frame(bm)
        bm_btns.pack(side="left", fill="y", padx=6, pady=4)
        for text, cmd in [
            ("Set", self.go_bookmark),
            ("Load", self.load_bookmark),
            ("Bookmark current…", self.add_bookmark),
            ("Rename…", self.rename_bookmark),
            ("Delete", self.delete_bookmark),
        ]:
            ttk.Button(bm_btns, text=text, command=cmd).pack(fill="x", pady=1)

        roam = ttk.Frame(self.tabs, padding=2)
        self.tabs.add(roam, text="Roam")
        self.wp_list = tk.Listbox(roam, height=6, activestyle="none", exportselection=False)
        wp_sb = ttk.Scrollbar(roam, command=self.wp_list.yview)
        self.wp_list.configure(yscrollcommand=wp_sb.set)
        self.wp_list.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
        wp_sb.pack(side="left", fill="y", pady=4)
        self.wp_list.bind("<Delete>", lambda _e: self.remove_waypoint())
        wp_btns = ttk.Frame(roam)
        wp_btns.pack(side="left", fill="y", padx=6, pady=4)
        for text, cmd in [
            ("Add current coords", self.add_waypoint_current),
            ("Add selected bookmark", self.add_waypoint_bookmark),
            ("Remove", self.remove_waypoint),
            ("Move up", lambda: self.move_waypoint(-1)),
            ("Move down", lambda: self.move_waypoint(1)),
        ]:
            ttk.Button(wp_btns, text=text, command=cmd).pack(fill="x", pady=1)

        opts = ttk.Frame(roam)
        opts.pack(side="left", fill="y", padx=6, pady=4)
        ttk.Label(opts, text="Mode:").grid(row=0, column=0, sticky="w")
        mode_box = ttk.Combobox(opts, textvariable=self.roam_mode_var, values=list(ROAM_MODES), state="readonly", width=16)
        mode_box.grid(row=0, column=1, sticky="w", pady=1)
        # Picking a mode sets its speed, which (via the speed trace) also re-plans a live roam.
        mode_box.bind("<<ComboboxSelected>>", lambda _e: (self.roam_speed_var.set(str(ROAM_MODES[self.roam_mode_var.get()][0])),
                                                          self._live_edit()))
        ttk.Label(opts, text="Speed (km/h):").grid(row=1, column=0, sticky="w")
        ttk.Entry(opts, textvariable=self.roam_speed_var, width=8).grid(row=1, column=1, sticky="w", pady=1)
        ttk.Checkbutton(opts, text="Follow roads", variable=self.roam_roads_var).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(opts, text="Loop back to start", variable=self.roam_loop_var).grid(row=3, column=0, columnspan=2, sticky="w")
        self.roam_start_btn = ttk.Button(opts, text="Start roaming", command=self.start_roam)
        self.roam_start_btn.grid(row=4, column=0, sticky="ew", pady=(6, 1))
        ttk.Button(opts, text="Stop (hold here)", command=self.stop_roam).grid(row=4, column=1, sticky="ew", pady=(6, 1))

        ttk.Separator(roam, orient="vertical").pack(side="left", fill="y", padx=4, pady=4)
        wander = ttk.Frame(roam)
        wander.pack(side="left", fill="y", padx=6, pady=4)
        ttk.Label(wander, text="Wander around current coords").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(wander, text="Radius (m):").grid(row=1, column=0, sticky="w")
        ttk.Entry(wander, textvariable=self.wander_radius_var, width=8).grid(row=1, column=1, sticky="w", pady=1)
        ttk.Label(wander, text="Duration (min):").grid(row=2, column=0, sticky="w")
        ttk.Entry(wander, textvariable=self.wander_minutes_var, width=8).grid(row=2, column=1, sticky="w", pady=1)
        ttk.Checkbutton(wander, text="Pause now and then", variable=self.wander_pause_var).grid(
            row=3, column=0, columnspan=2, sticky="w"
        )
        self.wander_btn = ttk.Button(wander, text="Start wandering", command=self.start_wander)
        self.wander_btn.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 1))

        sc = ttk.Frame(self.tabs, padding=2)
        self.tabs.add(sc, text="Scenario")
        self.sc_text = tk.Text(sc, height=8, width=58, font=("Consolas", 9), undo=True, wrap="none")
        sc_sb = ttk.Scrollbar(sc, command=self.sc_text.yview)
        self.sc_text.configure(yscrollcommand=sc_sb.set)
        self.sc_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
        sc_sb.pack(side="left", fill="y", pady=4)
        self.sc_text.insert("1.0", SCENARIO_EXAMPLE)
        sc_btns = ttk.Frame(sc)
        sc_btns.pack(side="left", fill="y", padx=6, pady=4)
        self.sc_check_btn = ttk.Button(sc_btns, text="Check & preview", command=lambda: self.run_scenario(run=False))
        self.sc_run_btn = ttk.Button(sc_btns, text="Run scenario", command=self.run_scenario)
        for btn in [ttk.Button(sc_btns, text="Open…", command=self.open_scenario),
                    ttk.Button(sc_btns, text="Save…", command=self.save_scenario),
                    ttk.Button(sc_btns, text="Example", command=self.example_scenario),
                    self.sc_check_btn, self.sc_run_btn]:
            btn.pack(fill="x", pady=1)
        ttk.Button(sc_btns, text="Stop (hold here)", command=self.stop_roam).pack(fill="x", pady=1)

        gf = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(gf, text="Geofence")
        ttk.Label(gf, text="Centre:").grid(row=0, column=0, sticky="w")
        ttk.Label(gf, textvariable=self.gf_center_var).grid(row=0, column=1, columnspan=3, sticky="w")
        ttk.Button(gf, text="Use current coords", command=self.gf_use_current).grid(row=0, column=4, sticky="ew", padx=3)
        for row, col, label, var in [(1, 0, "Radius (m):", self.gf_radius_var),
                                     (1, 2, "Start this far outside (m):", self.gf_margin_var),
                                     (2, 0, "Speeds (km/h):", self.gf_speeds_var),
                                     (2, 2, "Passes per speed:", self.gf_passes_var),
                                     (3, 0, "Dwell inside (s):", self.gf_dwell_var)]:
            ttk.Label(gf, text=label).grid(row=row, column=col, sticky="w", pady=2)
            ttk.Entry(gf, textvariable=var, width=10).grid(row=row, column=col + 1, sticky="w", padx=(2, 12))
        self.gf_preview_btn = ttk.Button(gf, text="Preview on map", command=lambda: self.run_geofence(run=False))
        self.gf_preview_btn.grid(row=1, column=4, sticky="ew", padx=3)
        self.gf_run_btn = ttk.Button(gf, text="Run geofence test", command=self.run_geofence)
        self.gf_run_btn.grid(row=2, column=4, sticky="ew", padx=3)
        ttk.Button(gf, text="Stop (hold here)", command=self.stop_roam).grid(row=3, column=4, sticky="ew", padx=3)
        ttk.Label(gf, foreground="#666", wraplength=560, justify="left",
                  text="Each pass waits outside, crosses the circle at the next speed and heading, and leaves the far "
                       "side. Every ENTER / EXIT is logged with its time so you can compare it with your app's "
                       "region events.").grid(row=4, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self.planning_buttons = [self.roam_start_btn, self.wander_btn, self.sc_check_btn, self.sc_run_btn,
                                 self.gf_preview_btn, self.gf_run_btn]

        logf = ttk.LabelFrame(left, text="Log (stdout / stderr)")
        logf.pack(fill="both", expand=True, **pad)
        self.log_box = tk.Text(logf, wrap="word", state="disabled", font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        sb = ttk.Scrollbar(logf, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=sb.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in {
            "cmd": "#569cd6",
            "out": "#d4d4d4",
            "err": "#f48771",
            "ok": "#6a9955",
            "info": "#dcdcaa",
            "step": "#9cdcfe",
            "enter": "#4ec9b0",
            "exit": "#c586c0",
        }.items():
            self.log_box.tag_configure(tag, foreground=color)

    def _build_map(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(bar, text="Left-click: pick coords   ·   Right-click: set here / add waypoint / geofence centre",
                  foreground="#666").pack(side="left")
        ttk.Checkbutton(bar, text="Follow phone", variable=self.follow_var).pack(side="right")
        ttk.Button(bar, text="Fit route", command=self._fit_route).pack(side="right", padx=4)
        if tkintermapview is None:
            ttk.Label(parent, text="Map unavailable: pip install tkintermapview").pack(expand=True)
            return
        self.map = tkintermapview.TkinterMapView(parent, corner_radius=0)
        self.map.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        try:
            lat, lon = self._read_coords()
        except ValueError:
            lat, lon = PRESETS[0][1:]
        self.map.set_position(lat, lon)
        self.map.set_zoom(15)
        self.map.add_left_click_map_command(self._on_map_click)
        self.map.add_right_click_menu_command("Set location here", self._map_set_here, pass_coords=True)
        self.map.add_right_click_menu_command("Add as roam waypoint", self._map_add_waypoint, pass_coords=True)
        self.map.add_right_click_menu_command("Set geofence centre here", self._map_gf_center, pass_coords=True)

    # ---------- helpers ----------
    def _base(self):
        base = split_command(self.cli_var.get())
        if not base:
            raise ValueError("CLI command is empty.")
        return base

    def _conn_args(self):
        udid = self.udid_var.get().strip()
        if self.tunnel_var.get():
            # `--tunnel ""` picks the first device registered with tunneld.
            return ["--tunnel", udid]
        return ["--udid", udid] if udid else []

    def _read_coords(self):
        try:
            lat = float(self.lat_var.get().strip())
            lon = float(self.lon_var.get().strip())
        except ValueError:
            raise ValueError("Latitude and longitude must be decimal numbers.")
        if not -90.0 <= lat <= 90.0:
            raise ValueError("Latitude must be between -90 and 90.")
        if not -180.0 <= lon <= 180.0:
            raise ValueError("Longitude must be between -180 and 180.")
        return lat, lon

    def _log(self, text, tag="info"):
        self.log_queue.put((text, tag))

    # ---------- actions ----------
    def set_location(self):
        try:
            lat, lon = self._read_coords()
            args = (
                self._base()
                + ["developer", "dvt", "simulate-location", "set"]
                + self._conn_args()
                + ["--", f"{lat:.6f}", f"{lon:.6f}"]
            )
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return
        if self._live_ok():
            # A live session is connected: just jump there instead of reconnecting (no gap).
            with self.roam_lock:
                del self.roam_track[self.roam_done:]
                self.roam_track.append((lat, lon))
                self.roam_marks, self.roam_plan, self.roam_on_finish = {}, None, None
            self._clear_route_drawing()
            self._log(f"Moved to {lat:.6f}, {lon:.6f} (live)")
            return
        self._log(f"Setting location to {lat:.6f}, {lon:.6f}")
        self.session_id += 1
        self.retries = 0
        self.sim_pos = (lat, lon)
        self._clear_route_drawing()
        self._start_session(lambda: args, self.session_id)

    def _start_session(self, make_args, sid, label="set", line_cb=None):
        """make_args is called on every (re)start, so a roam can resume where it stopped."""
        try:
            args = make_args()
        except (OSError, ValueError) as exc:
            self._log(f"[{label}] could not prepare command: {exc}", "err")
            return
        started = time.monotonic()
        # on_exit runs on a worker thread; hand the follow-up to the UI thread via the queue.
        self.pm.run_persistent(
            args,
            label,
            on_exit=lambda _code: self.log_queue.put(
                lambda: self._session_ended(make_args, sid, started, label, line_cb)
            ),
            line_cb=line_cb,
        )

    def _session_ended(self, make_args, sid, started, label, line_cb):
        """Restart a session that died on its own (e.g. Wi-Fi dropped)."""
        if sid != self.session_id or not self.reconnect_var.get():
            return
        ran_for = time.monotonic() - started
        if ran_for > 10:
            self.established_sid = sid  # it really connected, so later drops are worth retrying
        if self.established_sid != sid:
            self._log("Could not start the session - check 'List devices' / 'Check tunnel'. Not retrying.", "err")
            return
        if ran_for > 30:
            self.retries = 0  # it had been holding fine, so this is a fresh drop
        if self.retries >= MAX_RECONNECTS:
            self._log(f"Gave up reconnecting after {MAX_RECONNECTS} attempts.", "err")
            return
        self.retries += 1
        self._log(f"Session lost - reconnecting in {RECONNECT_DELAY_MS // 1000}s "
                  f"(attempt {self.retries}/{MAX_RECONNECTS})", "info")
        self.after(
            RECONNECT_DELAY_MS,
            lambda: sid == self.session_id and self._start_session(make_args, sid, label, line_cb),
        )

    def clear_location(self):
        try:
            args = self._base() + ["developer", "dvt", "simulate-location", "clear"] + self._conn_args()
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return
        self.session_id += 1
        self.sim_pos = None
        self._clear_route_drawing()
        self._log("Clearing simulated location")
        self.pm.stop_active_async(then=lambda: self.pm.run_once(args, "clear"))

    def apply_preset(self, name, lat, lon):
        self._fill_coords(name, lat, lon)
        self._log(f"Preset selected: {name}")
        self.set_location()

    def _fill_coords(self, label, lat, lon):
        self.lat_var.set(f"{lat:.6f}")
        self.lon_var.set(f"{lon:.6f}")
        self.current_place = (label, self.lat_var.get(), self.lon_var.get())

    # ---------- address search ----------
    def search_address(self, then_set=False):
        query = self.address_var.get().strip()
        if not query:
            messagebox.showerror("Address", "Type an address or place name first.")
            return
        wait = GEOCODE_MIN_INTERVAL - (time.monotonic() - self.last_geocode)
        if wait > 0:  # respect Nominatim's 1 request/second policy
            self.after(int(wait * 1000) + 50, lambda: self.search_address(then_set))
            return
        self.last_geocode = time.monotonic()
        self.search_btn.configure(state="disabled")
        self._log(f"Searching address: {query}")

        def worker():
            try:
                results = geocode(query)
                error = None
            except Exception as exc:  # noqa: BLE001 - network/HTTP/parse errors all go to the log
                results, error = [], exc
            self.log_queue.put(lambda: self._show_matches(query, results, error, then_set))

        threading.Thread(target=worker, daemon=True).start()

    def _show_matches(self, query, results, error, then_set):
        self.search_btn.configure(state="normal")
        if error:
            self._log(f"Address lookup failed: {error}", "err")
            return
        self.matches = results
        self.match_box["values"] = [name for name, _, _ in results]
        if not results:
            self.match_box.set("")
            self._log(f"No matches for '{query}'. Try adding a city or country.", "err")
            return
        self.match_box.current(0)
        self._apply_match(0)
        self._log(f"{len(results)} match(es) found; using the first. Pick another under Matches if needed.", "ok")
        if then_set:
            self.set_location()

    def _on_match_selected(self, _event=None):
        idx = self.match_box.current()
        if idx >= 0:
            self._apply_match(idx)

    def _apply_match(self, idx):
        name, lat, lon = self.matches[idx]
        self._fill_coords(name, lat, lon)
        self._log(f"Match: {name} -> {lat:.6f}, {lon:.6f}", "info")

    # ---------- bookmarks ----------
    def _refresh_bookmarks(self):
        self.bm_list.delete(0, "end")
        for b in self.bookmarks:
            self.bm_list.insert("end", f"{b['name']}   ({b['lat']:.6f}, {b['lon']:.6f})")

    def _persist_bookmarks(self):
        try:
            save_bookmarks(self.bookmarks)
        except OSError as exc:
            messagebox.showerror("Bookmarks", f"Could not save bookmarks:\n{exc}")

    def _selected_bookmark(self):
        sel = self.bm_list.curselection()
        if not sel:
            messagebox.showinfo("Bookmarks", "Select a bookmark first.")
            return None
        return sel[0]

    def load_bookmark(self):
        idx = self._selected_bookmark()
        if idx is None:
            return None
        b = self.bookmarks[idx]
        self._fill_coords(b["name"], b["lat"], b["lon"])
        self.address_var.set(b.get("address", ""))
        return b

    def go_bookmark(self):
        b = self.load_bookmark()
        if b:
            self._log(f"Bookmark selected: {b['name']}")
            self.set_location()

    def add_bookmark(self):
        try:
            lat, lon = self._read_coords()
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return
        # Default the name to the place these coordinates came from, if unchanged since.
        place = self.current_place
        address = ""
        if place and place[1:] == (self.lat_var.get(), self.lon_var.get()):
            address = place[0]
            default = address.split(",")[0]
        else:
            default = f"{lat:.5f}, {lon:.5f}"
        name = simpledialog.askstring("Bookmark", "Name for this location:", initialvalue=default, parent=self)
        if not name or not name.strip():
            return
        entry = {"name": name.strip(), "lat": lat, "lon": lon}
        if address:
            entry["address"] = address
        self.bookmarks.append(entry)
        self._persist_bookmarks()
        self._refresh_bookmarks()
        self.bm_list.selection_set("end")
        self.bm_list.see("end")
        self._log(f"Bookmarked '{entry['name']}'", "ok")

    def rename_bookmark(self):
        idx = self._selected_bookmark()
        if idx is None:
            return
        b = self.bookmarks[idx]
        name = simpledialog.askstring("Rename bookmark", "New name:", initialvalue=b["name"], parent=self)
        if not name or not name.strip():
            return
        b["name"] = name.strip()
        self._persist_bookmarks()
        self._refresh_bookmarks()
        self.bm_list.selection_set(idx)

    def delete_bookmark(self):
        idx = self._selected_bookmark()
        if idx is None:
            return
        b = self.bookmarks[idx]
        if not messagebox.askyesno("Delete bookmark", f"Delete '{b['name']}'?"):
            return
        del self.bookmarks[idx]
        self._persist_bookmarks()
        self._refresh_bookmarks()

    # ---------- roaming ----------
    def _refresh_waypoints(self, select=None):
        self.wp_list.delete(0, "end")
        for i, (name, lat, lon) in enumerate(self.roam_waypoints, 1):
            self.wp_list.insert("end", f"{i}. {name}   ({lat:.5f}, {lon:.5f})")
        if select is not None and self.roam_waypoints:
            self.wp_list.selection_set(select)
            self.wp_list.see(select)
        self._live_edit()
        if self.map:
            for m in self.wp_markers:
                m.delete()
            self.wp_markers = [
                self.map.set_marker(lat, lon, text=str(i), marker_color_circle="#fff3e0",
                                    marker_color_outside="#ef6c00", text_color="#bf360c")
                for i, (_, lat, lon) in enumerate(self.roam_waypoints, 1)
            ]

    def add_waypoint_current(self):
        try:
            lat, lon = self._read_coords()
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return
        place = self.current_place
        if place and place[1:] == (self.lat_var.get(), self.lon_var.get()):
            name = place[0].split(",")[0]
        else:
            name = f"{lat:.5f}, {lon:.5f}"
        self.roam_waypoints.append((name, lat, lon))
        self._refresh_waypoints(select="end")

    def add_waypoint_bookmark(self):
        sel = self.bm_list.curselection()
        if not sel:
            messagebox.showinfo("Roam", "Select a bookmark on the Bookmarks tab first.")
            return
        b = self.bookmarks[sel[0]]
        self.roam_waypoints.append((b["name"], b["lat"], b["lon"]))
        self._refresh_waypoints(select="end")

    def remove_waypoint(self):
        sel = self.wp_list.curselection()
        if sel:
            del self.roam_waypoints[sel[0]]
            self._refresh_waypoints(select=min(sel[0], len(self.roam_waypoints) - 1))

    def move_waypoint(self, delta):
        sel = self.wp_list.curselection()
        if not sel:
            return
        i, j = sel[0], sel[0] + delta
        if 0 <= j < len(self.roam_waypoints):
            wps = self.roam_waypoints
            wps[i], wps[j] = wps[j], wps[i]
            self._refresh_waypoints(select=j)

    def start_roam(self):
        if len(self.roam_waypoints) < 2:
            messagebox.showinfo("Roam", "Add at least two waypoints (start and destination).")
            return
        try:
            kmh = self._roam_speed()
            self._base()
        except ValueError:
            messagebox.showerror("Roam", "Speed must be a number of km/h between 0 and 1000.")
            return
        wps = list(self.roam_waypoints)
        loop = self.roam_loop_var.get()
        profile = ROAM_MODES[self.roam_mode_var.get()][1]
        roads = self.roam_roads_var.get()
        self._set_planning(True)
        self._log(f"Planning route through {len(wps)} waypoints at {kmh:g} km/h...")

        def worker():
            track, ends = build_roam_track(wps[0][1:], wps[1:], wps, loop, profile, roads, kmh, self._log)
            plan = {"kind": "Route", "ends": [(0, wps[0])] + ends}
            self.log_queue.put(lambda: self._begin_roam(track, None, False, plan=plan))

        threading.Thread(target=worker, daemon=True).start()

    def _roam_speed(self):
        kmh = float(self.roam_speed_var.get())
        if not 0 < kmh <= 1000:
            raise ValueError
        return kmh

    def start_wander(self):
        try:
            center = self._read_coords()
        except ValueError as exc:
            messagebox.showerror("Wander", f"Set the centre point in Latitude/Longitude first.\n{exc}")
            return
        try:
            self._base()
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return
        try:
            kmh = self._roam_speed()
        except ValueError:
            messagebox.showerror("Wander", "Speed must be a number of km/h between 0 and 1000.")
            return
        try:
            radius = float(self.wander_radius_var.get())
            minutes = float(self.wander_minutes_var.get())
            if not (20 <= radius <= 20_000 and 1 <= minutes <= ROAM_LOOP_HOURS * 60):
                raise ValueError
        except ValueError:
            messagebox.showerror("Wander", f"Radius must be 20-20000 m and duration 1-{ROAM_LOOP_HOURS * 60} min.")
            return
        profile = ROAM_MODES[self.roam_mode_var.get()][1]
        roads, pauses = self.roam_roads_var.get(), self.wander_pause_var.get()
        self._set_planning(True)
        self._log(f"Planning a {minutes:g} min wander within {radius:g} m at {kmh:g} km/h...")

        def worker():
            track = generate_wander(center, radius, minutes * 60, kmh, profile, roads, pauses, self._log)
            self.log_queue.put(lambda: self._begin_roam(track, None, False, kind="Wander", plan={"kind": "Wander"}))

        threading.Thread(target=worker, daemon=True).start()

    def _set_planning(self, busy):
        for btn in self.planning_buttons:
            btn.configure(state="disabled" if busy else "normal")

    def _live_ok(self):
        """True while a live session is connected and taking points."""
        return self.roam_sid == self.session_id and self.pm.is_active() and self.live_ready.is_set()

    def _begin_roam(self, track, dist, loop, kind="Route", marks=(), on_finish=None, plan=None):
        """Start playing a track. Reuses a connected live session, so switching routes has no gap."""
        self._set_planning(False)
        with self.roam_lock:
            self.roam_track = list(track)
            self.roam_done = 0
            self.roam_marks = {}
            for idx, text, tag in marks:
                self.roam_marks.setdefault(idx, []).append((text, tag))
            self.roam_step = ""
            self.roam_on_finish = on_finish
            self.roam_plan = plan
            self.roam_paused = False
        self.sim_pos = None
        self._draw_route(self.roam_track)
        if dist is None:
            dist = sum(haversine_m(a, b) for a, b in zip(track, track[1:]))
        self._log(f"{kind}: {dist / 1000:.2f} km, {timedelta(seconds=len(track) * ROAM_STEP_SECONDS)}", "ok")
        if self._live_ok():
            return  # the player thread picks up the new track on its next tick
        self.session_id += 1
        self.retries = 0
        self.roam_sid = sid = self.session_id
        self._start_session(self._live_args, sid, "live", self._live_line)

    def _live_args(self):
        self.live_ready.clear()
        write_gpx([(0.0, 0.0)], GPX_PATH)  # `play` insists the file exists; the helper never reads it
        base = self._base()
        return [helper_python(base), "-c", LIVE_HELPER] + self._conn_args() + [GPX_PATH]

    def _live_line(self, line):
        """Reader-thread hook for the live helper's output."""
        if LIVE_READY in line:
            self.live_ready.set()
            return False
        return True

    def _player(self):
        """Background thread: feed the live session one point per tick while a track is playing."""
        next_t = time.monotonic()
        while True:
            next_t += ROAM_STEP_SECONDS
            time.sleep(max(0.0, next_t - time.monotonic()))
            if time.monotonic() - next_t > 5:
                next_t = time.monotonic()  # fell far behind (PC slept?) - resume, don't burst
            if self.roam_sid == self.session_id and not self.roam_paused and self.live_ready.is_set():
                self._tick()

    def _tick(self):
        """Send the next point of the track and fire any marks / end-of-track callback it reaches."""
        with self.roam_lock:
            done = self.roam_done
            if done >= len(self.roam_track):
                return  # finished: the device keeps the last point
            lat, lon = self.roam_track[done]
        if not self.pm.send(f"{lat:.7f} {lon:.7f}"):
            return
        with self.roam_lock:
            self.roam_done = done = done + 1
            hits = self.roam_marks.pop(done - 1, ())
            finished = done == len(self.roam_track)
        for text, tag in hits:
            self._log(text, tag)
            if tag == "step":
                self.roam_step = text
        if finished and self.roam_on_finish:
            finish, self.roam_on_finish = self.roam_on_finish, None
            self.log_queue.put(finish)  # run on the UI thread

    def _roam_position(self):
        if self.roam_done:
            return self.roam_track[self.roam_done - 1]
        return self.roam_track[0] if self.roam_track else None

    def stop_roam(self):
        if self.roam_sid != self.session_id or self.roam_done >= len(self.roam_track):
            messagebox.showinfo("Roam", "Not moving right now.")
            return
        with self.roam_lock:
            # Drop the rest of the route; the live session keeps holding the current point (no gap).
            del self.roam_track[max(self.roam_done, 1):]
            self.roam_marks, self.roam_plan, self.roam_on_finish = {}, None, None
        pos = self._roam_position()
        self._fill_coords("Roam stop", *pos)
        self._draw_route(self.roam_track, fit=False)
        self._log(f"Stopped; holding {pos[0]:.6f}, {pos[1]:.6f}")

    # ---------- live edits ----------
    def _live_edit(self, *_):
        """Roam settings or waypoints changed: re-plan from the current position (debounced)."""
        if not self.roam_plan or self.roam_sid != self.session_id:
            return
        if self._edit_job:
            self.after_cancel(self._edit_job)
        self._edit_job = self.after(800, self._apply_live_edit)

    def _apply_live_edit(self):
        self._edit_job = None
        plan = self.roam_plan
        if not plan or self.roam_sid != self.session_id or self.roam_done >= len(self.roam_track):
            return
        try:
            kmh = self._roam_speed()
        except ValueError:
            return  # half-typed speed; wait for a valid one
        if plan["kind"] == "Wander":
            # Same path, new pace: resample what's left from the current position.
            with self.roam_lock:
                done = max(self.roam_done, 1)
                rest = densify(self.roam_track[done - 1:], kmh / 3.6 * ROAM_STEP_SECONDS)
                self.roam_track[done:] = rest[1:]
            self._draw_route(self.roam_track, fit=False)
            self._log(f"Live update: wandering at {kmh:g} km/h from here", "ok")
            return
        # Route: work out which waypoints are still ahead, then route there from where we are now.
        done = max(self.roam_done, 1)
        reached = [t for i, t in plan["ends"] if i < done]
        wps = list(self.roam_waypoints)
        last = reached[-1] if reached else None
        remaining = wps[wps.index(last) + 1:] if last in wps else wps[min(len(reached), len(wps)):]
        loop, roads = self.roam_loop_var.get(), self.roam_roads_var.get()
        profile = ROAM_MODES[self.roam_mode_var.get()][1]
        self.roam_paused = True  # hold still for the second or so it takes to re-route
        pos = self._roam_position()

        def worker():
            track, ends = build_roam_track(pos, remaining, wps, loop and len(wps) >= 2, profile, roads, kmh,
                                           self._log)
            self.log_queue.put(lambda: self._splice_route(track, ends, reached, len(remaining), kmh, loop))

        threading.Thread(target=worker, daemon=True).start()

    def _splice_route(self, track, ends, reached, n_remaining, kmh, loop):
        """Replace everything after the current point with the re-planned track."""
        with self.roam_lock:
            done = max(self.roam_done, 1)
            base = done - 1  # track[0] is the current position, i.e. old index done-1
            self.roam_track[done:] = track[1:]
            if self.roam_plan:
                self.roam_plan["ends"] = ([(i, t) for i, t in self.roam_plan["ends"] if i < done]
                                          + [(i + base, t) for i, t in ends])
            self.roam_paused = False
        self._draw_route(self.roam_track, fit=False)
        if len(track) > 1:
            left = timedelta(seconds=(len(track) - 1) * ROAM_STEP_SECONDS)
            self._log(f"Live update: {n_remaining} waypoint(s) ahead at {kmh:g} km/h"
                      + (", looping" if loop else "") + f" - {left} to go", "ok")
        else:
            self._log("Live update: no waypoints left ahead - holding here", "ok")

    # ---------- map ----------
    def _map_item(self, name, item=None):
        """Replace (or with item=None, remove) a named marker/path/polygon on the map."""
        old = self.map_items.pop(name, None)
        if old is not None:
            old.delete()
        if item is not None:
            self.map_items[name] = item

    def _clear_route_drawing(self):
        if self.map:
            for name in [n for n in self.map_items if n.startswith(("route", "trail"))]:
                self._map_item(name)
        self.drawn_trail = -1

    def _draw_paths(self, prefix, points, **style):
        """Draw points as one path per continuous segment (teleports leave a gap)."""
        for name in [n for n in self.map_items if n.startswith(prefix)]:
            self._map_item(name)
        for i, seg in enumerate(s for s in split_at_jumps(points) if len(s) >= 2):
            self._map_item(f"{prefix}{i}", self.map.set_path(seg, **style))

    def _draw_route(self, track, fit=True):
        if not self.map:
            return
        self._clear_route_drawing()
        pts = thin(track, 3000)
        self._draw_paths("route", pts, color="#9aa0a6", width=4)
        if fit:
            self._fit_points(pts)

    def _fit_points(self, pts):
        if not self.map or not pts:
            return
        lats, lons = [p[0] for p in pts], [p[1] for p in pts]
        if max(lats) - min(lats) < 0.002 and max(lons) - min(lons) < 0.002:
            self.map.set_position(pts[0][0], pts[0][1])
            self.map.set_zoom(17)
            return
        pad_lat, pad_lon = (max(lats) - min(lats)) * 0.08, (max(lons) - min(lons)) * 0.08
        self.map.fit_bounding_box((max(lats) + pad_lat, min(lons) - pad_lon), (min(lats) - pad_lat, max(lons) + pad_lon))

    def _fit_route(self):
        if self.roam_track:
            self._fit_points(thin(self.roam_track, 3000))
        elif self.drawn_pos:
            self.map.set_position(*self.drawn_pos)

    def _update_map(self, pos):
        """Move the phone marker and extend the travelled trail (called twice a second)."""
        if not self.map:
            return
        if pos != self.drawn_pos:
            if pos is None:
                self._map_item("phone")
            elif "phone" in self.map_items:
                self.map_items["phone"].set_position(*pos)
            else:
                self._map_item("phone", self.map.set_marker(*pos, text="iPhone", marker_color_circle="white",
                                                            marker_color_outside="#1a73e8", text_color="#0b57d0"))
            if pos is not None and self.follow_var.get():
                self.map.set_position(*pos)
            self.drawn_pos = pos
        roaming = self.roam_sid == self.session_id and self.roam_track
        if roaming and self.roam_done >= 2 and self.roam_done != self.drawn_trail:
            self._draw_paths("trail", thin(self.roam_track[:self.roam_done], 1500), color="#1a73e8", width=5)
            self.drawn_trail = self.roam_done

    def _on_map_click(self, coords):
        self._fill_coords("Map point", *coords)
        self._map_item("pick", self.map.set_marker(*coords, marker_color_circle="white", marker_color_outside="#5f6368"))

    def _map_set_here(self, coords):
        self._on_map_click(coords)
        self.set_location()

    def _map_add_waypoint(self, coords):
        self._on_map_click(coords)
        self.add_waypoint_current()
        self.tabs.select(2)

    def _map_gf_center(self, coords):
        self._set_gf_center(coords)
        self.tabs.select(4)

    # ---------- scenarios ----------
    def example_scenario(self):
        if self.sc_text.get("1.0", "end").strip() and not messagebox.askyesno(
                "Scenario", "Replace the current script with the example?"):
            return
        self.sc_text.delete("1.0", "end")
        self.sc_text.insert("1.0", SCENARIO_EXAMPLE)

    def open_scenario(self):
        path = filedialog.askopenfilename(filetypes=[("Spoofer scenario", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            messagebox.showerror("Scenario", str(exc))
            return
        self.sc_text.delete("1.0", "end")
        self.sc_text.insert("1.0", text)
        self.scenario_path = path
        self._log(f"Opened scenario {os.path.basename(path)}")

    def save_scenario(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", filetypes=[("Spoofer scenario", "*.txt")],
            initialfile=os.path.basename(self.scenario_path) if self.scenario_path else "scenario.txt")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.sc_text.get("1.0", "end-1c"))
        except OSError as exc:
            messagebox.showerror("Scenario", str(exc))
            return
        self.scenario_path = path
        self._log(f"Saved scenario {os.path.basename(path)}", "ok")

    def _make_resolver(self):
        """resolve(place) for compile_scenario: coords as-is, @bookmarks, else rate-limited geocoding."""
        cache, bookmarks = {}, {b["name"].lower(): (b["lat"], b["lon"]) for b in self.bookmarks}

        def resolve(place):
            kind, value = place
            if kind == "coords":
                return value
            if kind == "bookmark":
                if value.lower() not in bookmarks:
                    raise ScenarioError(f"No bookmark named '{value}'.")
                return bookmarks[value.lower()]
            if value not in cache:
                wait = GEOCODE_MIN_INTERVAL - (time.monotonic() - self.last_geocode)
                if wait > 0:
                    time.sleep(wait)
                self.last_geocode = time.monotonic()
                try:
                    results = geocode(value, limit=1)
                except Exception as exc:  # noqa: BLE001 - surface as a scenario error
                    raise ScenarioError(f"Address lookup failed for '{value}': {exc}") from None
                if not results:
                    raise ScenarioError(f"No match for address '{value}'.")
                name, lat, lon = results[0]
                self._log(f"  '{value}' -> {name}", "info")
                cache[value] = (lat, lon)
            return cache[value]

        return resolve

    def run_scenario(self, run=True):
        try:
            steps = parse_scenario(self.sc_text.get("1.0", "end"))
            start = self._read_coords()
            if run:
                self._base()
        except ValueError as exc:  # ScenarioError is a ValueError too
            messagebox.showerror("Scenario", str(exc))
            return
        self._set_planning(True)
        self._log(f"{'Planning' if run else 'Checking'} scenario ({len(steps)} steps)...")
        resolve = self._make_resolver()

        def worker():
            try:
                track, marks, clear_at_end = compile_scenario(steps, start, resolve, self._log)
            except ScenarioError as exc:
                self.log_queue.put(lambda: self._scenario_failed(str(exc)))
                return
            self.log_queue.put(lambda: self._scenario_ready(track, marks, clear_at_end, run))

        threading.Thread(target=worker, daemon=True).start()

    def _scenario_failed(self, msg):
        self._set_planning(False)
        self._log(f"Scenario error: {msg}", "err")
        messagebox.showerror("Scenario", msg)

    def _scenario_ready(self, track, marks, clear_at_end, run):
        dur = timedelta(seconds=len(track) * ROAM_STEP_SECONDS)
        dist = sum(haversine_m(a, b) for a, b in zip(track, track[1:]))
        if not run:
            self._set_planning(False)
            self._draw_route(track)
            self._log(f"Scenario OK: {len(marks)} steps, {dist / 1000:.2f} km, {dur} long"
                      + (", clears at the end" if clear_at_end else ""), "ok")
            return
        self._begin_roam(track, dist, False, kind="Scenario", marks=marks,
                         on_finish=self._scenario_finished if clear_at_end else None)

    def _scenario_finished(self):
        self._log("Scenario finished - clearing simulated location", "ok")
        self.clear_location()

    # ---------- geofence ----------
    def _set_gf_center(self, coords):
        self.gf_center = coords
        self.gf_center_var.set(f"{coords[0]:.6f}, {coords[1]:.6f}")
        self._draw_fence()

    def gf_use_current(self):
        try:
            self._set_gf_center(self._read_coords())
        except ValueError as exc:
            messagebox.showerror("Geofence", str(exc))

    def _gf_params(self):
        if not self.gf_center:
            raise ValueError("Set the geofence centre first (right-click the map, or 'Use current coords').")
        try:
            radius = float(self.gf_radius_var.get())
            margin = float(self.gf_margin_var.get())
            speeds = [float(s) for s in self.gf_speeds_var.get().replace(";", ",").split(",") if s.strip()]
            passes = int(self.gf_passes_var.get())
            dwell = float(self.gf_dwell_var.get())
        except ValueError:
            raise ValueError("Radius, distance, passes and dwell must be numbers; speeds a comma-separated list.")
        if not (10 <= radius <= 50_000 and 10 <= margin <= 50_000):
            raise ValueError("Radius and start distance must be between 10 m and 50 km.")
        if not speeds or not all(0 < s <= 1000 for s in speeds):
            raise ValueError("Give at least one speed between 0 and 1000 km/h.")
        if not (1 <= passes <= 50 and 0 <= dwell <= 3600):
            raise ValueError("Passes must be 1-50 and dwell 0-3600 s.")
        return radius, margin, speeds, passes, dwell

    def _draw_fence(self):
        if not self.map or not self.gf_center:
            return
        try:
            radius = float(self.gf_radius_var.get())
        except ValueError:
            return
        self._map_item("fence", self.map.set_polygon(circle_points(self.gf_center, radius), fill_color=None,
                                                     outline_color="#d93025", border_width=3))
        self._map_item("fence_c", self.map.set_marker(*self.gf_center, text="geofence", marker_color_circle="white",
                                                      marker_color_outside="#d93025", text_color="#a50e0e"))

    def run_geofence(self, run=True):
        try:
            radius, margin, speeds, passes, dwell = self._gf_params()
            if run:
                self._base()
        except ValueError as exc:
            messagebox.showerror("Geofence", str(exc))
            return
        self._draw_fence()
        track, marks = build_geofence_track(self.gf_center, radius, speeds, passes, dwell, margin)
        crossings = sum(tag in ("enter", "exit") for _, _, tag in marks)
        dur = timedelta(seconds=len(track) * ROAM_STEP_SECONDS)
        if not run:
            self._draw_route(track)
            self._log(f"Geofence test: {passes * len(speeds)} passes, {crossings} boundary crossings, {dur} long", "ok")
            return

        def finished():
            self._log(f"Geofence test finished: {crossings // 2} ENTER and {crossings // 2} EXIT events expected. "
                      "Compare with your app's log, then Save log to keep this run.", "ok")

        self._begin_roam(track, None, False, kind="Geofence test", marks=marks, on_finish=finished)

    def list_devices(self):
        try:
            self.pm.run_once(self._base() + ["usbmux", "list"], "usbmux list")
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))

    def mount_ddi(self):
        try:
            # Go through the tunnel when enabled so mounting also works over Wi-Fi
            # (plain usbmux defaults to the first USB-connected device).
            args = self._base() + ["mounter", "auto-mount"] + self._conn_args()
            self.pm.run_once(args, "auto-mount")
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))

    def start_tunnel(self):
        """Open an elevated PowerShell window running tunneld (triggers a UAC prompt)."""
        if os.name != "nt":
            messagebox.showinfo("Start tunnel", "Run `sudo python3 -m pymobiledevice3 remote tunneld` manually.")
            return
        import ctypes
        import socket

        try:
            with socket.create_connection(("127.0.0.1", 49151), timeout=0.5):
                pass
            self._log("A tunnel is already running on port 49151 - not starting another. "
                      "Use 'Check tunnel' to see its state.", "info")
            return
        except OSError:
            pass  # nothing listening, go ahead

        try:
            base = self._base()
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return
        ps_cmd = "& " + " ".join("'" + t.replace("'", "''") + "'" for t in base) + " remote tunneld"
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "powershell.exe", f'-NoExit -Command "{ps_cmd}"', None, 1
        )
        if rc <= 32:
            self._log(f"Could not start elevated tunnel (ShellExecute code {rc}, UAC declined?)", "err")
        else:
            self._log("Tunnel window launched. Don't click inside it (QuickEdit pauses it).", "ok")

    def check_tunnel(self):
        import urllib.request

        def worker():
            try:
                with urllib.request.urlopen(TUNNELD_URL, timeout=4) as resp:
                    body = resp.read().decode("utf-8", "replace").strip()
            except Exception as exc:  # noqa: BLE001 - report any failure to the log
                self._log(f"tunneld not reachable at {TUNNELD_URL}: {exc}", "err")
                return
            try:
                tunnels = json.loads(body or "{}")
            except ValueError:
                self._log(f"tunneld replied: {body}", "info")
                return
            if not tunnels:
                self._log("tunneld is running but has no device tunnels yet (phone locked / not found?)", "err")
            for udid, entries in tunnels.items():
                for t in entries:
                    iface = t.get("interface", "")
                    # usbmux USB tunnels end in "-USB"; Network/mobdev2/wifi ones are all over the LAN.
                    kind = "USB" if iface.endswith("-USB") else "Wi-Fi"
                    self._log(f"Tunnel to {udid} via {kind} ({t.get('tunnel-address')}:{t.get('tunnel-port')})", "ok")

        threading.Thread(target=worker, daemon=True).start()

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def save_log(self):
        from tkinter import filedialog

        path = filedialog.asksaveasfilename(
            defaultextension=".log",
            initialfile=time.strftime("gps_injector_%Y%m%d_%H%M%S.log"),
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt")],
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_box.get("1.0", "end"))
            self._log(f"Log saved to {path}", "ok")

    # ---------- loop ----------
    def _drain_log(self):
        try:
            batch = []
            while True:
                batch.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        # Callables are UI work queued from worker threads; tuples are log lines.
        callbacks = [item for item in batch if callable(item)]
        lines = [item for item in batch if not callable(item)]
        if lines:
            self.log_box.configure(state="normal")
            for text, tag in lines:
                self.log_box.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n", tag)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        for cb in callbacks:
            cb()
        self.after(100, self._drain_log)

    def _refresh_status(self):
        active = self.pm.is_active()
        if active and self.roam_sid == self.session_id and self.roam_track:
            total, done = len(self.roam_track), self.roam_done
            if done >= total:
                self.status_var.set("Holding position (live session)")
            else:
                left = (total - done) * ROAM_STEP_SECONDS
                self.status_var.set(f"Roaming {100 * done // total}% ({timedelta(seconds=left)} left)"
                                    + (f"   ·   {self.roam_step}" if self.roam_step else ""))
            pos = self._roam_position()
        else:
            self.status_var.set("Simulation session active" if active else "Idle")
            pos = self.sim_pos if active else None
        self._update_map(pos)
        self.after(500, self._refresh_status)

    def _on_close(self):
        if self.pm.is_active():
            if not messagebox.askyesno("Quit", "A simulation session is active. Stop it and quit?"):
                return
            self.pm._stop_active()
        self.destroy()


if __name__ == "__main__":
    if os.name == "nt":
        import ctypes

        # Give the app its own taskbar identity so Windows shows our icon, not python.exe's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Spoofer.GPS")
    App().mainloop()
