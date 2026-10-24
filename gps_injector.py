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
from tkinter import messagebox, simpledialog, ttk

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
# pymobiledevice3 logs one of these per GPX point it plays.
SET_LINE_RE = re.compile(r"set location to -?\d")


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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        try:
            self.iconbitmap(default=resource_path(os.path.join("assets", "icon.ico")))
        except tk.TclError:
            pass  # icon missing: keep Tk's default rather than fail
        self.geometry("880x820")
        self.minsize(720, 640)

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

        self._build_ui()
        self._refresh_bookmarks()
        self.after(100, self._drain_log)
        self.after(500, self._refresh_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        conn = ttk.LabelFrame(self, text="Connection")
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

        coords = ttk.LabelFrame(self, text="Location")
        coords.pack(fill="x", **pad)
        ttk.Label(coords, text="Address:").grid(row=0, column=0, sticky="w", **pad)
        addr_entry = ttk.Entry(coords, textvariable=self.address_var)
        addr_entry.grid(row=0, column=1, columnspan=3, sticky="ew", **pad)
        addr_entry.bind("<Return>", lambda _e: self.search_address())
        self.search_btn = ttk.Button(coords, text="Search", command=self.search_address)
        self.search_btn.grid(row=0, column=4, **pad)
        ttk.Button(coords, text="Search && Set", command=lambda: self.search_address(then_set=True)).grid(
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

        self.tabs = ttk.Notebook(self)
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
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self.roam_speed_var.set(str(ROAM_MODES[self.roam_mode_var.get()][0])))
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

        logf = ttk.LabelFrame(self, text="Log (stdout / stderr)")
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
        }.items():
            self.log_box.tag_configure(tag, foreground=color)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        ttk.Label(bottom, text="Status:").pack(side="left")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", padx=4)
        ttk.Button(bottom, text="Clear log", command=self.clear_log).pack(side="right")
        ttk.Button(bottom, text="Save log", command=self.save_log).pack(side="right", padx=4)

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
        self._log(f"Setting location to {lat:.6f}, {lon:.6f}")
        self.session_id += 1
        self.retries = 0
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
        points = [(lat, lon) for _, lat, lon in self.roam_waypoints]
        loop = self.roam_loop_var.get()
        if loop:
            points.append(points[0])
        profile = ROAM_MODES[self.roam_mode_var.get()][1]
        roads = self.roam_roads_var.get()
        self.roam_start_btn.configure(state="disabled")
        self._log(f"Planning route through {len(self.roam_waypoints)} waypoints at {kmh:g} km/h...")

        def worker():
            path = points
            if roads:
                try:
                    path = route_on_roads(points, profile)
                except Exception as exc:  # noqa: BLE001 - fall back rather than fail the roam
                    self._log(f"Road routing failed ({exc}); using straight lines.", "err")
            lap = densify(path, kmh / 3.6 * ROAM_STEP_SECONDS)
            dist = sum(haversine_m(a, b) for a, b in zip(path, path[1:]))
            self.log_queue.put(lambda: self._begin_roam(lap, dist, loop))

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
        total_points = int(minutes * 60 / ROAM_STEP_SECONDS)
        self.wander_btn.configure(state="disabled")
        self._log(f"Planning a {minutes:g} min wander within {radius:g} m at {kmh:g} km/h...")

        def worker():
            # Pick random stops until the straight-line distance covers the walk; roads only make it longer.
            need_m = kmh / 3.6 * minutes * 60
            stops, dist = [center], 0.0
            while dist < need_m and len(stops) < 500:
                nxt = random_point_near(center, radius)
                dist += haversine_m(stops[-1], nxt)
                stops.append(nxt)
            path = route_chunked(stops, profile, roads, self._log)
            track = densify(path, kmh / 3.6 * ROAM_STEP_SECONDS)
            if pauses:
                track = add_pauses(track)
            track = track[:total_points]
            self.log_queue.put(lambda: self._begin_roam(track, None, False, kind="Wander"))

        threading.Thread(target=worker, daemon=True).start()

    def _begin_roam(self, lap, dist, loop, kind="Route"):
        self.roam_start_btn.configure(state="normal")
        self.wander_btn.configure(state="normal")
        laps = 1
        if loop:
            laps = max(1, min(ROAM_MAX_POINTS, ROAM_LOOP_HOURS * 3600 // ROAM_STEP_SECONDS) // len(lap))
        self.roam_track = lap * laps
        self.roam_done = 0
        self.session_id += 1
        self.retries = 0
        self.roam_sid = sid = self.session_id
        mins = len(lap) * ROAM_STEP_SECONDS / 60
        if dist is None:
            dist = sum(haversine_m(a, b) for a, b in zip(lap, lap[1:]))
        self._log(f"{kind}: {dist / 1000:.2f} km, ~{mins:.0f} min" + (" per lap" if loop else "")
                  + (f", looping {laps}x" if laps > 1 else ""), "ok")
        self._start_session(self._roam_args, sid, "roam", lambda line: self._roam_line(sid, line))

    def _roam_args(self):
        # Resume from the first point not yet played; once finished, keep holding the last one.
        write_gpx(self.roam_track[self.roam_done:] or self.roam_track[-1:], GPX_PATH)
        return (self._base() + ["developer", "dvt", "simulate-location", "play"]
                + self._conn_args() + [GPX_PATH])

    def _roam_line(self, sid, line):
        """Runs on the reader thread: count played points and thin out the per-point log spam."""
        if sid != self.roam_sid:
            return True
        if SET_LINE_RE.search(line):
            self.roam_done = min(self.roam_done + 1, len(self.roam_track))
            return self.roam_done in (1, len(self.roam_track)) or self.roam_done % 60 == 0
        return "waiting for" not in line

    def _roam_position(self):
        if self.roam_done:
            return self.roam_track[self.roam_done - 1]
        return self.roam_track[0] if self.roam_track else None

    def stop_roam(self):
        if self.roam_sid != self.session_id:
            messagebox.showinfo("Roam", "Not roaming right now.")
            return
        pos = self._roam_position()
        self.roam_sid = None
        self._fill_coords("Roam stop", *pos)
        self._log(f"Roaming stopped; holding {pos[0]:.6f}, {pos[1]:.6f}")
        self.set_location()

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
                self.status_var.set("Roam finished - holding final point")
            else:
                left = (total - done) * ROAM_STEP_SECONDS
                self.status_var.set(f"Roaming {100 * done // total}% ({done}/{total} points, "
                                    f"{timedelta(seconds=left)} left)")
        else:
            self.status_var.set("Simulation session active" if active else "Idle")
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
