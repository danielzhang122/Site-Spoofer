"""GeofenceMixin: the Geofence tab - cross a circular region at several speeds and headings and log
every ENTER / EXIT, to compare with an app's region-monitoring events (track built by tracks.py)."""

from datetime import timedelta
from tkinter import ttk

from ..tracks import ROAM_STEP_SECONDS, build_geofence_track
from .widgets import labeled_entries, reports_errors


class GeofenceMixin:
    def _build_geofence(self, tabs):
        gf = self.gf_tab = ttk.Frame(tabs, padding=6)
        tabs.add(gf, text="Geofence")
        ttk.Label(gf, text="Centre:").grid(row=0, column=0, sticky="w")
        ttk.Label(gf, textvariable=self.gf_center_var).grid(row=0, column=1, columnspan=3, sticky="w")
        ttk.Button(gf, text="Use current coords", command=self.gf_use_current).grid(row=0, column=4, sticky="ew", padx=3)
        labeled_entries(gf, [(1, 0, "Radius (m):", self.gf_radius_var),
                             (1, 2, "Start this far outside (m):", self.gf_margin_var),
                             (2, 0, "Speeds (km/h):", self.gf_speeds_var),
                             (2, 2, "Passes per speed:", self.gf_passes_var),
                             (3, 0, "Dwell inside (s):", self.gf_dwell_var)], width=10)
        preview = ttk.Button(gf, text="Preview on map", command=lambda: self.run_geofence(run=False))
        preview.grid(row=1, column=4, sticky="ew", padx=3)
        run = ttk.Button(gf, text="Run geofence test", command=self.run_geofence)
        run.grid(row=2, column=4, sticky="ew", padx=3)
        ttk.Button(gf, text="Stop (hold here)", command=self.stop_roam).grid(row=3, column=4, sticky="ew", padx=3)
        ttk.Label(gf, foreground="#666", wraplength=560, justify="left",
                  text="Each pass waits outside, crosses the circle at the next speed and heading, and leaves the far "
                       "side. Every ENTER / EXIT is logged with its time so you can compare it with your app's "
                       "region events.").grid(row=4, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self.planning_buttons += [preview, run]

    def _set_gf_center(self, coords):
        self.gf_center = coords
        self.gf_center_var.set(f"{coords[0]:.6f}, {coords[1]:.6f}")
        self._redraw_fence()

    @reports_errors("Geofence")
    def gf_use_current(self):
        self._set_gf_center(self._read_coords())

    def _redraw_fence(self):
        try:
            radius = float(self.gf_radius_var.get())
        except ValueError:
            return
        if self.gf_center:
            self._draw_fence(self.gf_center, radius)

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
            raise ValueError("Radius, distance, passes and dwell must be numbers; speeds a comma-separated list.") from None
        if not (radius > 0 and margin > 0):
            raise ValueError("Radius and start distance must be above 0 m.")
        if not speeds or not all(s > 0 for s in speeds):
            raise ValueError("Give at least one speed above 0 km/h.")
        if not (passes >= 1 and dwell >= 0):
            raise ValueError("Passes must be at least 1 and dwell 0 s or more.")
        return radius, margin, speeds, passes, dwell

    @reports_errors("Geofence")
    def run_geofence(self, run=True):
        radius, margin, speeds, passes, dwell = self._gf_params()
        if run:
            self._base()
        self._redraw_fence()
        track, marks = build_geofence_track(self.gf_center, radius, speeds, passes, dwell, margin)
        crossings = sum(tag in ("enter", "exit") for _, _, tag in marks)
        if not run:
            self._draw_route(track)
            self._log(f"Geofence test: {passes * len(speeds)} passes, {crossings} boundary crossings, "
                      f"{timedelta(seconds=len(track) * ROAM_STEP_SECONDS)} long", "ok")
            return

        def finished():
            self._log(f"Geofence test finished: {crossings // 2} ENTER and {crossings // 2} EXIT events expected. "
                      "Compare with your app's log, then Save log to keep this run.", "ok")

        self._begin_roam(track, kind="Geofence test", marks=marks, on_finish=finished)
