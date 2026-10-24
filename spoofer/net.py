"""HTTP helpers shared by routing and address search, plus the map-tile cache.

All services are free OpenStreetMap-based ones with no API key; their usage policies ask for an
identifying User-Agent, which every request here sends.
"""

import io
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Spoofer/1.0 (iOS location QA tool)"

# OpenStreetMap Nominatim: at most 1 request per second (callers enforce GEOCODE_MIN_INTERVAL).
GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_MIN_INTERVAL = 1.0

TILE_SERVER = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "Spoofer", "tiles")
TILE_MAX_AGE_S = 7 * 86400  # OSM's tile policy asks clients to keep tiles at least this long


def http_json(url, body=None, timeout=20):
    """GET url (or POST body as JSON) and return the parsed JSON reply.
    Routers answer "no route" with HTTP 400 and a JSON body saying why, so an error reply that is
    JSON is returned like any other; other HTTP errors raise."""
    headers = {"User-Agent": USER_AGENT}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except ValueError:
            raise exc from None


def geocode(query, limit=5):
    """Return [(display_name, lat, lon), ...] for a free-text address."""
    params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": limit})
    results = http_json(f"{GEOCODE_URL}?{params}", timeout=10)
    return [(r["display_name"], float(r["lat"]), float(r["lon"])) for r in results]


class _Tile:
    """Just enough of a requests.Response for tkintermapview, which only reads .raw."""

    def __init__(self, data):
        self.raw = io.BytesIO(data)


class TileRequests:
    """Stand-in for the `requests` module inside tkintermapview.map_widget.

    tkintermapview fetches tiles with a generic User-Agent that OpenStreetMap's tile servers block
    (they answer with an 'Access blocked' image), opens a new HTTPS connection per tile and caches
    nothing between runs. This sends an identifying User-Agent, shares one keep-alive session across
    its loader threads and keeps tiles on disk."""

    def __init__(self, real):
        self._real = real
        self._session = real.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._session.mount("https://", real.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=32))

    def get(self, url, **_kwargs):
        parts = urllib.parse.urlsplit(url)
        path = os.path.join(TILE_CACHE_DIR, parts.netloc, *parts.path.strip("/").split("/"))
        try:
            fresh = time.time() - os.path.getmtime(path) < TILE_MAX_AGE_S
            with open(path, "rb") as f:
                cached = f.read()
        except OSError:
            fresh, cached = False, None
        if fresh:
            return _Tile(cached)
        try:
            resp = self._session.get(url, timeout=10)
        except self._real.RequestException:
            if cached is None:
                raise
            return _Tile(cached)  # offline: a stale tile beats a blank one
        if resp.status_code != 200 or not resp.headers.get("Content-Type", "").startswith("image/"):
            return _Tile(cached if cached is not None else resp.content)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{threading.get_ident()}.tmp"  # loader threads may race on the same tile
            with open(tmp, "wb") as f:
                f.write(resp.content)
            os.replace(tmp, path)
        except OSError:
            pass  # caching is best-effort
        return _Tile(resp.content)

    def __getattr__(self, name):
        return getattr(self._real, name)
