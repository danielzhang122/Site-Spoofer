"""The scenario language: a tiny line-based script compiled into one continuous track.

parse_scenario() only validates (no network); compile_scenario() resolves places and routes.
"""

import re

from .geo import densify
from .routing import ROAM_MODES, route_chunked
from .tracks import ROAM_STEP_SECONDS, generate_wander, step_m

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
_DUR_RE = r"(\d+(?:\.\d+)?)\s*([a-z]*)"
_DUR_UNITS = {"": 1, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
              "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
              "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}


class ScenarioError(ValueError):
    pass


def parse_duration(text):
    """'90', '30s', '2m', '1h30m', '1.5 hours' -> seconds."""
    parts = re.findall(_DUR_RE, text.lower())
    if not parts or re.sub(_DUR_RE, "", text.lower()).strip():
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
    """-> ("coords", (lat, lon)) | ("bookmark", name) | ("address", text)."""
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
    if kmh <= 0:
        raise ScenarioError("speed must be more than 0 km/h")
    return rest[:m.start()], kmh


def _parse_step(verb, rest):
    if verb in ("start", "goto", "teleport"):
        return {"op": "jump", "place": parse_place(rest)}
    if verb == "wait":
        return {"op": "wait", "secs": parse_duration(rest)}
    if verb in SCENARIO_MOVES:
        if not rest.lower().startswith("to "):
            raise ScenarioError(f"expected '{verb} to <place>'")
        rest, kmh = _take_speed(rest[3:])
        mode = SCENARIO_MOVES[verb]
        return {"op": "move", "mode": mode, "kmh": kmh or ROAM_MODES[mode][0], "place": parse_place(rest)}
    if verb == "wander":
        rest, kmh = _take_speed(rest)
        radius, sep, dur = rest.partition(" for ")
        if not sep:
            raise ScenarioError("expected 'wander <radius> for <duration>'")
        return {"op": "wander", "radius": parse_distance(radius), "secs": parse_duration(dur),
                "kmh": kmh or ROAM_MODES["Walk"][0]}
    if verb == "roads":
        if rest.lower() not in ("on", "off"):
            raise ScenarioError("expected 'roads on' or 'roads off'")
        return {"op": "roads", "on": rest.lower() == "on"}
    if verb == "clear":
        if rest:
            raise ScenarioError("'clear' takes no arguments")
        return {"op": "clear"}
    raise ScenarioError(f"unknown command '{verb}' (start, wait, walk/run/cycle/drive to, wander, roads, clear)")


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
        try:
            step = _parse_step(verb.lower(), rest.strip())
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
            path = route_chunked([pos, resolve(st["place"])], ROAM_MODES[st["mode"]][1], roads, log)
            track.extend(densify(path, step_m(st["kmh"]))[1:])
        elif st["op"] == "wander":
            track.extend(generate_wander(pos, st["radius"], st["secs"], st["kmh"], "routed-foot", roads, True, log))
        elif st["op"] == "roads":
            roads = st["on"]
        elif st["op"] == "clear":
            clear_at_end = True
        if track:
            pos = track[-1]
    if not track:
        raise ScenarioError("The scenario never goes anywhere - add a start, wait or move command.")
    # A mark past the end (e.g. a trailing 'roads' or 'clear') would never fire; pin it to the last point.
    marks = [(min(idx, len(track) - 1), text, tag) for idx, text, tag in marks]
    return track, marks, clear_at_end
