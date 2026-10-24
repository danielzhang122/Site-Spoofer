"""MapMixin: the OpenStreetMap view on the right - the phone marker, planned route, travelled trail,
waypoints and geofence. Everything drawn is kept in map_items by name so it can be replaced or removed.
Works without tkintermapview installed (self.map stays None and drawing is skipped)."""

from tkinter import ttk

from ..geo import circle_points, split_at_jumps, thin
from ..net import TILE_SERVER, TileRequests
from .places import PRESETS

try:
    import tkintermapview
    import tkintermapview.map_widget
except ImportError:  # the app still works without the map
    tkintermapview = None
else:
    tkintermapview.map_widget.requests = TileRequests(tkintermapview.map_widget.requests)

ROUTE_STYLE = {"color": "#9aa0a6", "width": 4}
TRAIL_STYLE = {"color": "#1a73e8", "width": 5}


class MapMixin:
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
        self.map.set_tile_server(TILE_SERVER, max_zoom=19)
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

    # ---------- drawing ----------
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

    def _draw_paths(self, prefix, points, style):
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
        self._draw_paths("route", pts, ROUTE_STYLE)
        if fit:
            self._fit_points(pts)

    def _draw_waypoints(self):
        if not self.map:
            return
        for m in self.wp_markers:
            m.delete()
        self.wp_markers = [
            self.map.set_marker(lat, lon, text=str(i), marker_color_circle="#fff3e0",
                                marker_color_outside="#ef6c00", text_color="#bf360c")
            for i, (_, lat, lon) in enumerate(self.roam_waypoints, 1)
        ]

    def _draw_fence(self, center, radius):
        if not self.map:
            return
        self._map_item("fence", self.map.set_polygon(circle_points(center, radius), fill_color=None,
                                                     outline_color="#d93025", border_width=3))
        self._map_item("fence_c", self.map.set_marker(*center, text="geofence", marker_color_circle="white",
                                                      marker_color_outside="#d93025", text_color="#a50e0e"))

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
        elif self.drawn_pos and self.map:
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
            self._draw_paths("trail", thin(self.roam_track[:self.roam_done], 1500), TRAIL_STYLE)
            self.drawn_trail = self.roam_done

    # ---------- clicks ----------
    def _on_map_click(self, coords):
        self._fill_coords("Map point", *coords)
        self._map_item("pick", self.map.set_marker(*coords, marker_color_circle="white", marker_color_outside="#5f6368"))

    def _map_set_here(self, coords):
        self._on_map_click(coords)
        self.set_location()

    def _map_add_waypoint(self, coords):
        self._on_map_click(coords)
        self.add_waypoint_current()
        self.tabs.select(self.roam_tab)

    def _map_gf_center(self, coords):
        self._set_gf_center(coords)
        self.tabs.select(self.gf_tab)
