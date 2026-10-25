"""ScenarioMixin: the Scenario tab - edit, open/save, check and run a scenario script (see scenario.py)."""

import os
import time
import tkinter as tk
from datetime import timedelta
from tkinter import filedialog, messagebox, ttk

from ..geo import path_length_m
from ..net import GEOCODE_MIN_INTERVAL, geocode
from ..scenario import SCENARIO_EXAMPLE, ScenarioError, compile_scenario, parse_scenario
from ..tracks import ROAM_STEP_SECONDS
from .widgets import button_column, reports_errors, scrolled


class ScenarioMixin:
    def _build_scenario(self, tabs):
        sc = ttk.Frame(tabs, padding=2)
        tabs.add(sc, text="Scenario")
        self.sc_text = scrolled(sc, tk.Text(sc, height=8, width=58, font=("Consolas", 9), undo=True, wrap="none"))
        self.sc_text.insert("1.0", SCENARIO_EXAMPLE)
        buttons = button_column(sc, [("Open…", self.open_scenario), ("Save…", self.save_scenario),
                                     ("Example", self.example_scenario),
                                     ("Check & preview", lambda: self.run_scenario(run=False)),
                                     ("Run scenario", self.run_scenario), ("Stop (hold here)", self.stop_roam)])
        self.planning_buttons += buttons[3:5]

    # ---------- file ----------
    def example_scenario(self):
        if self.sc_text.get("1.0", "end").strip() and not messagebox.askyesno(
                "Scenario", "Replace the current script with the example?"):
            return
        self.sc_text.delete("1.0", "end")
        self.sc_text.insert("1.0", SCENARIO_EXAMPLE)

    def open_scenario(self):
        path = filedialog.askopenfilename(filetypes=[("Globe Ghost scenario", "*.txt"), ("All files", "*.*")])
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
            defaultextension=".txt", filetypes=[("Globe Ghost scenario", "*.txt")],
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

    # ---------- run ----------
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

    @reports_errors("Scenario")  # ScenarioError is a ValueError too
    def run_scenario(self, run=True):
        steps = parse_scenario(self.sc_text.get("1.0", "end"))
        start = self._read_coords()
        if run:
            self._base()
        self._set_planning(True)
        self._log(f"{'Planning' if run else 'Checking'} scenario ({len(steps)} steps)...")
        resolve = self._make_resolver()
        self.run_in_background(lambda: compile_scenario(steps, start, resolve, self._log),
                               lambda res: self._scenario_ready(*res, run),
                               self._scenario_failed)

    def _scenario_failed(self, msg):
        self._set_planning(False)
        self._log(f"Scenario error: {msg}", "err")
        messagebox.showerror("Scenario", msg)

    def _scenario_ready(self, track, marks, clear_at_end, run):
        dist = path_length_m(track)
        if not run:
            self._set_planning(False)
            self._draw_route(track)
            self._log(f"Scenario OK: {len(marks)} steps, {dist / 1000:.2f} km, "
                      f"{timedelta(seconds=len(track) * ROAM_STEP_SECONDS)} long"
                      + (", clears at the end" if clear_at_end else ""), "ok")
            return
        self._begin_roam(track, dist, kind="Scenario", marks=marks,
                         on_finish=self._scenario_finished if clear_at_end else None)

    def _scenario_finished(self):
        self._log("Scenario finished - clearing simulated location", "ok")
        self.clear_location()
