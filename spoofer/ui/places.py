"""PlacesMixin: the Location frame (address search, lat/lon) and the Presets and Bookmarks tabs."""

import time
from tkinter import messagebox, simpledialog, ttk

from ..bookmarks import save_bookmarks
from ..net import GEOCODE_MIN_INTERVAL, geocode
from .widgets import list_panel, reports_errors

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


class PlacesMixin:
    def _build_location(self, parent, pad):
        coords = ttk.LabelFrame(parent, text="Location")
        coords.pack(fill="x", **pad)
        ttk.Label(coords, text="Address:").grid(row=0, column=0, sticky="w", **pad)
        addr_entry = ttk.Entry(coords, textvariable=self.address_var)
        addr_entry.grid(row=0, column=1, columnspan=3, sticky="ew", **pad)
        addr_entry.bind("<Return>", lambda _e: self.search_address())
        self.search_btn = ttk.Button(coords, text="Search", command=self.search_address)
        self.search_btn.grid(row=0, column=4, **pad)
        ttk.Button(coords, text="Search & Set", command=lambda: self.search_address(then_set=True)).grid(
            row=0, column=5, **pad)
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

    def _build_presets(self, tabs):
        presets = ttk.Frame(tabs, padding=4)
        tabs.add(presets, text="Presets")
        cols = 5
        for i, (name, lat, lon) in enumerate(PRESETS):
            ttk.Button(presets, text=name, command=lambda la=lat, lo=lon, n=name: self.apply_preset(n, la, lo)).grid(
                row=i // cols, column=i % cols, sticky="ew", padx=3, pady=3)
        for c in range(cols):
            presets.columnconfigure(c, weight=1)

    def _build_bookmarks(self, tabs):
        bm = ttk.Frame(tabs, padding=2)
        tabs.add(bm, text="Bookmarks")
        self.bm_list = list_panel(bm, [("Set", self.go_bookmark), ("Load", self.load_bookmark),
                                       ("Bookmark current…", self.add_bookmark), ("Rename…", self.rename_bookmark),
                                       ("Delete", self.delete_bookmark)])
        self.bm_list.bind("<Double-Button-1>", lambda _e: self.go_bookmark())
        self.bm_list.bind("<Return>", lambda _e: self.go_bookmark())
        self.bm_list.bind("<Delete>", lambda _e: self.delete_bookmark())

    # ---------- coordinates ----------
    def apply_preset(self, name, lat, lon):
        self._fill_coords(name, lat, lon)
        self._log(f"Preset selected: {name}")
        self.set_location()

    def _fill_coords(self, label, lat, lon):
        self.lat_var.set(f"{lat:.6f}")
        self.lon_var.set(f"{lon:.6f}")
        self.current_place = (label, self.lat_var.get(), self.lon_var.get())

    def _place_name(self):
        """Name of the place the current coords came from, if they haven't been edited since."""
        place = self.current_place
        if place and place[1:] == (self.lat_var.get(), self.lon_var.get()):
            return place[0]
        return None

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
        self.run_in_background(lambda: geocode(query),
                               lambda results: self._show_matches(query, results, None, then_set),
                               lambda error: self._show_matches(query, [], error, then_set))

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
        self._refresh_bookmarks()

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

    @reports_errors("Invalid input")
    def add_bookmark(self):
        lat, lon = self._read_coords()
        # Default the name to the place these coordinates came from, if unchanged since.
        address = self._place_name() or ""
        default = address.split(",")[0] if address else f"{lat:.5f}, {lon:.5f}"
        name = simpledialog.askstring("Bookmark", "Name for this location:", initialvalue=default, parent=self)
        if not name or not name.strip():
            return
        entry = {"name": name.strip(), "lat": lat, "lon": lon}
        if address:
            entry["address"] = address
        self.bookmarks.append(entry)
        self._persist_bookmarks()
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
        self.bm_list.selection_set(idx)

    def delete_bookmark(self):
        idx = self._selected_bookmark()
        if idx is None:
            return
        if not messagebox.askyesno("Delete bookmark", f"Delete '{self.bookmarks[idx]['name']}'?"):
            return
        del self.bookmarks[idx]
        self._persist_bookmarks()
