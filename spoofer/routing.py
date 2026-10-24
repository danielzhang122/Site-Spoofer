"""Road and footpath routing against the free FOSSGIS servers (OpenStreetMap data, no key).

Walking/running goes through Valhalla, whose pedestrian model can be told to prefer footways, park
paths and trails over walking beside roads. Everything else, and walking when Valhalla can't route
(e.g. past its 100 km pedestrian limit), goes through OSRM.
"""

from .geo import haversine_m
from .net import http_json

OSRM_URL = "https://routing.openstreetmap.de/{profile}/route/v1/driving/{coords}?overview=full&geometries=geojson"
VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"
FOOT_COSTING = {  # factors below 1 make that kind of way preferred
    "walkway_factor": 0.5,
    "sidewalk_factor": 0.8,
    "use_tracks": 1.0,
    "use_living_streets": 0.8,
    "max_hiking_difficulty": 6,  # the default (1) skips most hiking trails
}

ROAM_MODES = {  # name -> (default km/h, OSRM profile)
    "Walk": (5, "routed-foot"),
    "Run": (10, "routed-foot"),
    "Cycle": (18, "routed-bike"),
    "Drive (city)": (40, "routed-car"),
    "Drive (highway)": (100, "routed-car"),
}
PROFILE_NAMES = {"routed-foot": "walking", "routed-bike": "cycling", "routed-car": "driving"}
ROUTE_MAX_COORDS = 25  # waypoints per routing request; longer wanders are routed in chunks


class RoutingError(Exception):
    """The routing server couldn't route; code is the server's (e.g. OSRM's 'NoRoute')."""

    def __init__(self, code, message):
        super().__init__(f"{message} ({code})")
        self.code = code


def route_on_roads(waypoints, profile):
    """OSRM: the road-following polyline [(lat, lon), ...] through the waypoints."""
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)
    data = http_json(OSRM_URL.format(profile=profile, coords=coords))
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RoutingError(data.get("code") or "Error", data.get("message") or "no route")
    return [(lat, lon) for lon, lat in data["routes"][0]["geometry"]["coordinates"]]


def decode_polyline6(shape):
    """Decode a Valhalla polyline (precision 6) into [(lat, lon), ...]."""
    coords, idx, lat, lon = [], 0, 0, 0
    while idx < len(shape):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(shape[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat, lon = lat + deltas[0], lon + deltas[1]
        coords.append((lat / 1e6, lon / 1e6))
    return coords


def route_on_foot(waypoints):
    """Valhalla: walking route through the waypoints, preferring paths and trails to streets."""
    data = http_json(VALHALLA_URL, {
        "locations": [{"lat": lat, "lon": lon} for lat, lon in waypoints],
        "costing": "pedestrian",
        "costing_options": {"pedestrian": FOOT_COSTING},
        "directions_type": "none",
    })
    if "trip" not in data:
        raise RoutingError(data.get("error_code", "Error"), data.get("error") or "no route")
    path = []
    for leg in data["trip"]["legs"]:
        pts = decode_polyline6(leg["shape"])
        path.extend(pts if not path else pts[1:])
    return path


def route_with_fallback(points, profile, log):
    """Route on the chosen profile; if walking/cycling can't connect the points (typically a ferry
    crossing, which only the driving graph includes), follow driving roads instead."""
    if profile == "routed-foot":
        try:
            return route_on_foot(points)
        except Exception as exc:  # noqa: BLE001 - street routing below still has a go
            log(f"No footpath route here ({exc}) - trying street routing.", "info")
    try:
        return route_on_roads(points, profile)
    except RoutingError as exc:
        if exc.code != "NoRoute" or profile == "routed-car":
            raise
    path = route_on_roads(points, "routed-car")
    log(f"No {PROFILE_NAMES.get(profile, profile)} route between these points (usually a ferry crossing) - "
        "following driving roads at your speed.", "info")
    return path


def _bridge_ends(path, start, end, log):
    """Routers snap to the nearest road they can reach; if that's far from the requested start/end
    (an island, the ocean), add the requested point so the track still ends where it should."""
    gaps = [haversine_m(path[0], start), haversine_m(path[-1], end)]
    if gaps[0] > 1000:
        path = [start] + path
    if gaps[1] > 1000:
        path = path + [end]
    if max(gaps) > 5000:
        log(f"No road reaches part of this route - crossing {max(gaps) / 1000:,.0f} km in a straight line.", "err")
    return path


def route_chunked(points, profile, roads, log):
    """Route through many points, ROUTE_MAX_COORDS at a time; straight lines only where no road route exists."""
    path = []
    step = ROUTE_MAX_COORDS - 1
    for i in range(0, len(points) - 1, step):
        chunk = points[i:i + ROUTE_MAX_COORDS]
        part = chunk
        if roads:
            try:
                part = _bridge_ends(route_with_fallback(chunk, profile, log), chunk[0], chunk[-1], log)
            except Exception as exc:  # noqa: BLE001 - fall back rather than fail the roam
                log(f"No road route here ({exc}) - moving in a straight line for this stretch.", "err")
        path.extend(part if not path else part[1:])
    return path
