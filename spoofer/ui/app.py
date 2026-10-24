"""The main window. App is assembled from one mixin per area of the window:

  ConnectionMixin  (connection.py)   Connection frame; Set/Clear; the device session and reconnects
  PlacesMixin      (places.py)       Location frame (address search, coords); Presets and Bookmarks tabs
  RoamMixin        (roam.py)         Roam tab; the live player that feeds the phone one point per second
  ScenarioMixin    (scenario_tab.py) Scenario tab
  GeofenceMixin    (geofence_tab.py) Geofence tab
  MapMixin         (map_view.py)     the map on the right

All state lives on the App instance and is created in __init__ below. Worker threads never touch Tk:
they put log lines or callables on log_queue, which _drain_log runs on the UI thread.
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import timedelta
from tkinter import filedialog, messagebox, ttk

from ..bookmarks import BOOKMARKS_PATH, load_bookmarks
from ..device import ProcessManager, default_cli_command
from ..routing import ROAM_MODES
from ..tracks import ROAM_STEP_SECONDS
from .connection import ConnectionMixin
from .geofence_tab import GeofenceMixin
from .map_view import MapMixin
from .places import PlacesMixin
from .roam import RoamMixin
from .scenario_tab import ScenarioMixin

APP_TITLE = "Spoofer"
LOG_COLORS = {"cmd": "#569cd6", "out": "#d4d4d4", "err": "#f48771", "ok": "#6a9955",
              "info": "#dcdcaa", "step": "#9cdcfe", "enter": "#4ec9b0", "exit": "#c586c0"}


def resource_path(rel):
    """Path to a bundled file, both from source and inside a PyInstaller --onefile exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    return os.path.join(base, rel)


class App(ConnectionMixin, PlacesMixin, RoamMixin, ScenarioMixin, GeofenceMixin, MapMixin, tk.Tk):
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

        # Connection / session
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

        # Places
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

        # Roaming. roam_done is advanced by the player thread; roam_lock guards roam_track/roam_done.
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
        self.roam_lap = None  # (lap_track, lap_ends) appended each time a looping route runs out
        self.roam_paused = False
        self.roam_lock = threading.Lock()
        self.live_ready = threading.Event()  # set once the live helper has connected
        self._edit_job = None
        self.planning_buttons = []  # disabled while a route is being planned

        # Scenario / geofence
        self.scenario_path = None
        self.gf_center = None
        self.gf_radius_var = tk.StringVar(value="150")
        self.gf_margin_var = tk.StringVar(value="150")
        self.gf_speeds_var = tk.StringVar(value="5, 40")
        self.gf_passes_var = tk.StringVar(value="2")
        self.gf_dwell_var = tk.StringVar(value="30")
        self.gf_center_var = tk.StringVar(value="not set - right-click the map or use current coords")

        # Map: where the phone is, what's drawn, and what was drawn last.
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

    # ---------- layout ----------
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
        left, right = ttk.Frame(paned), ttk.Frame(paned)
        paned.add(left, weight=0)
        paned.add(right, weight=1)
        self._build_map(right)

        self._build_connection(left, pad)
        self._build_location(left, pad)
        self.tabs = ttk.Notebook(left)
        self.tabs.pack(fill="x", **pad)
        for build in (self._build_presets, self._build_bookmarks, self._build_roam,
                      self._build_scenario, self._build_geofence):
            build(self.tabs)

        logf = ttk.LabelFrame(left, text="Log (stdout / stderr)")
        logf.pack(fill="both", expand=True, **pad)
        self.log_box = tk.Text(logf, wrap="word", state="disabled", font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        sb = ttk.Scrollbar(logf, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=sb.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in LOG_COLORS.items():
            self.log_box.tag_configure(tag, foreground=color)

    # ---------- shared helpers ----------
    def _log(self, text, tag="info"):
        self.log_queue.put((text, tag))

    def _read_coords(self):
        try:
            lat = float(self.lat_var.get().strip())
            lon = float(self.lon_var.get().strip())
        except ValueError:
            raise ValueError("Latitude and longitude must be decimal numbers.") from None
        if not -90.0 <= lat <= 90.0:
            raise ValueError("Latitude must be between -90 and 90.")
        if not -180.0 <= lon <= 180.0:
            raise ValueError("Longitude must be between -180 and 180.")
        return lat, lon

    def run_in_background(self, work, done, fail):
        """Run work() on a thread, then done(result) or fail(error message) on the UI thread."""

        def worker():
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - every failure is reported, none leaves the UI stuck
                msg = str(exc)
                self.log_queue.put(lambda: fail(msg))
                return
            self.log_queue.put(lambda: done(result))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- log ----------
    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def save_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".log",
            initialfile=time.strftime("gps_injector_%Y%m%d_%H%M%S.log"),
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt")],
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_box.get("1.0", "end"))
            self._log(f"Log saved to {path}", "ok")

    # ---------- loops ----------
    def _drain_log(self):
        batch = []
        try:
            while True:
                batch.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        # Callables are UI work queued from worker threads; tuples are log lines.
        lines = [item for item in batch if not callable(item)]
        if lines:
            self.log_box.configure(state="normal")
            for text, tag in lines:
                self.log_box.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n", tag)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        for cb in batch:
            if callable(cb):
                cb()
        self.after(100, self._drain_log)

    def _refresh_status(self):
        active = self.pm.is_active()
        if active and self.roam_sid == self.session_id and self.roam_track:
            step = f"   ·   {self.roam_step}" if self.roam_step else ""
            total, done = len(self.roam_track), self.roam_done
            if self.roam_lap:
                self.status_var.set("Roaming (looping until stopped)" + step)
            elif done >= total:
                self.status_var.set("Holding position (live session)")
            else:
                left = (total - done) * ROAM_STEP_SECONDS
                self.status_var.set(f"Roaming {100 * done // total}% ({timedelta(seconds=left)} left)" + step)
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
            self.pm.stop_active()
        self.destroy()
