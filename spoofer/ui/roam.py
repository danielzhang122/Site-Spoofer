"""RoamMixin: the Roam tab (waypoint routes and wandering) and the live player behind every moving run.

Every moving run (roam, wander, scenario, geofence test) is a track: one (lat, lon) per second. _begin_roam
hands it to a live pymobiledevice3 session (see device.LIVE_HELPER) and the _player thread sends the next
point each tick. Because points are fed one at a time, the rest of the track can be replaced mid-run
(live edits, Stop, Set location) without dropping the session.
"""

import time
import tkinter as tk
from datetime import timedelta
from tkinter import messagebox, ttk

from ..device import LIVE_READY, live_command
from ..geo import densify, path_length_m
from ..routing import ROAM_MODES
from ..tracks import ROAM_STEP_SECONDS, build_roam_track, generate_wander, step_m
from .widgets import button_column, labeled_entries, parse_float, reports_errors, scrolled


class RoamMixin:
    def _build_roam(self, tabs):
        roam = self.roam_tab = ttk.Frame(tabs, padding=2)
        tabs.add(roam, text="Roam")
        self.wp_list = scrolled(roam, tk.Listbox(roam, height=6, activestyle="none", exportselection=False))
        self.wp_list.bind("<Delete>", lambda _e: self.remove_waypoint())
        button_column(roam, [("Add current coords", self.add_waypoint_current),
                             ("Add selected bookmark", self.add_waypoint_bookmark),
                             ("Remove", self.remove_waypoint),
                             ("Move up", lambda: self.move_waypoint(-1)),
                             ("Move down", lambda: self.move_waypoint(1))])

        opts = ttk.Frame(roam)
        opts.pack(side="left", fill="y", padx=6, pady=4)
        ttk.Label(opts, text="Mode:").grid(row=0, column=0, sticky="w")
        mode_box = ttk.Combobox(opts, textvariable=self.roam_mode_var, values=list(ROAM_MODES), state="readonly",
                                width=16)
        mode_box.grid(row=0, column=1, sticky="w", pady=1)
        # Picking a mode sets its speed, which (via the speed trace) also re-plans a live roam.
        mode_box.bind("<<ComboboxSelected>>", lambda _e: (
            self.roam_speed_var.set(str(ROAM_MODES[self.roam_mode_var.get()][0])), self._live_edit()))
        labeled_entries(opts, [(1, 0, "Speed (km/h):", self.roam_speed_var)])
        ttk.Checkbutton(opts, text="Follow roads", variable=self.roam_roads_var).grid(
            row=2, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(opts, text="Loop back to start", variable=self.roam_loop_var).grid(
            row=3, column=0, columnspan=2, sticky="w")
        self.roam_start_btn = ttk.Button(opts, text="Start roaming", command=self.start_roam)
        self.roam_start_btn.grid(row=4, column=0, sticky="ew", pady=(6, 1))
        ttk.Button(opts, text="Stop (hold here)", command=self.stop_roam).grid(row=4, column=1, sticky="ew", pady=(6, 1))

        ttk.Separator(roam, orient="vertical").pack(side="left", fill="y", padx=4, pady=4)
        wander = ttk.Frame(roam)
        wander.pack(side="left", fill="y", padx=6, pady=4)
        ttk.Label(wander, text="Wander around current coords").grid(row=0, column=0, columnspan=2, sticky="w")
        labeled_entries(wander, [(1, 0, "Radius (m):", self.wander_radius_var),
                                 (2, 0, "Duration (min):", self.wander_minutes_var)])
        ttk.Checkbutton(wander, text="Pause now and then", variable=self.wander_pause_var).grid(
            row=3, column=0, columnspan=2, sticky="w")
        self.wander_btn = ttk.Button(wander, text="Start wandering", command=self.start_wander)
        self.wander_btn.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 1))
        self.planning_buttons += [self.roam_start_btn, self.wander_btn]

    # ---------- waypoints ----------
    def _refresh_waypoints(self, select=None):
        self.wp_list.delete(0, "end")
        for i, (name, lat, lon) in enumerate(self.roam_waypoints, 1):
            self.wp_list.insert("end", f"{i}. {name}   ({lat:.5f}, {lon:.5f})")
        if select is not None and self.roam_waypoints:
            self.wp_list.selection_set(select)
            self.wp_list.see(select)
        self._live_edit()
        self._draw_waypoints()

    @reports_errors("Invalid input")
    def add_waypoint_current(self):
        lat, lon = self._read_coords()
        place = self._place_name()
        self.roam_waypoints.append((place.split(",")[0] if place else f"{lat:.5f}, {lon:.5f}", lat, lon))
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

    # ---------- starting a run ----------
    def _roam_speed(self):
        kmh = parse_float(self.roam_speed_var.get(), "Speed must be a number of km/h above 0.")
        if not kmh > 0:
            raise ValueError("Speed must be a number of km/h above 0.")
        return kmh

    def _roam_profile(self):
        return ROAM_MODES[self.roam_mode_var.get()][1]

    @reports_errors("Roam")
    def start_roam(self):
        if len(self.roam_waypoints) < 2:
            messagebox.showinfo("Roam", "Add at least two waypoints (start and destination).")
            return
        kmh = self._roam_speed()
        self._base()
        wps, loop = list(self.roam_waypoints), self.roam_loop_var.get()
        profile, roads = self._roam_profile(), self.roam_roads_var.get()
        self._set_planning(True)
        self._log(f"Planning route through {len(wps)} waypoints at {kmh:g} km/h...")
        self.run_in_background(
            lambda: build_roam_track(wps[0][1:], wps[1:], wps, loop, profile, roads, kmh, self._log),
            lambda res: self._begin_roam(res[0], plan={"kind": "Route", "ends": [(0, wps[0])] + res[1]}, lap=res[2]),
            lambda msg: self._plan_failed("Roam", msg))

    @reports_errors("Wander")
    def start_wander(self):
        try:
            center = self._read_coords()
        except ValueError as exc:
            raise ValueError(f"Set the centre point in Latitude/Longitude first.\n{exc}") from None
        self._base()
        kmh = self._roam_speed()
        error = "Radius (m) and duration (min) must be numbers above 0."
        radius = parse_float(self.wander_radius_var.get(), error)
        minutes = parse_float(self.wander_minutes_var.get(), error)
        if not (radius > 0 and minutes > 0):
            raise ValueError(error)
        profile, roads, pauses = self._roam_profile(), self.roam_roads_var.get(), self.wander_pause_var.get()
        self._set_planning(True)
        self._log(f"Planning a {minutes:g} min wander within {radius:g} m at {kmh:g} km/h...")
        self.run_in_background(
            lambda: generate_wander(center, radius, minutes * 60, kmh, profile, roads, pauses, self._log),
            lambda track: self._begin_roam(track, kind="Wander", plan={"kind": "Wander"}),
            lambda msg: self._plan_failed("Wander", msg))

    def _plan_failed(self, title, msg):
        """A route couldn't be planned: undo the 'planning' state and tell the user why."""
        self._set_planning(False)
        self.roam_paused = False
        self._log(f"{title}: {msg.splitlines()[0]}", "err")
        messagebox.showerror(title, msg)

    def _set_planning(self, busy):
        for btn in self.planning_buttons:
            btn.configure(state="disabled" if busy else "normal")

    def _live_ok(self):
        """True while a live session is connected and taking points."""
        return self.roam_sid == self.session_id and self.pm.is_active() and self.live_ready.is_set()

    def _begin_roam(self, track, dist=None, kind="Route", marks=(), on_finish=None, plan=None, lap=None):
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
            self.roam_lap = lap
            self.roam_paused = False
        self.sim_pos = None
        self._draw_route(self._planned_route())
        if dist is None:
            dist = path_length_m(track)
        self._log(f"{kind}: {dist / 1000:.2f} km, {timedelta(seconds=len(track) * ROAM_STEP_SECONDS)}"
                  + (", then looping until stopped" if lap else ""), "ok")
        if self._live_ok():
            return  # the player thread picks up the new track on its next tick
        self.session_id += 1
        self.retries = 0
        self.roam_sid = sid = self.session_id
        self._start_session(self._live_args, sid, "live", self._live_line)

    def _live_args(self):
        self.live_ready.clear()
        return live_command(self._base(), self._conn_args())

    def _live_line(self, line):
        """Reader-thread hook for the live helper's output."""
        if LIVE_READY in line:
            self.live_ready.set()
            return False
        return True

    # ---------- playback (player thread) ----------
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
            if done >= len(self.roam_track) and self.roam_lap:
                # Looping: run another lap (it starts where the track ends, at the first waypoint).
                lap_track, lap_ends = self.roam_lap
                offset = len(self.roam_track) - 1
                self.roam_track.extend(lap_track[1:])
                if self.roam_plan:
                    self.roam_plan["ends"].extend((i + offset, t) for i, t in lap_ends)
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

    def _roam_moving(self):
        return self.roam_done < len(self.roam_track) or self.roam_lap is not None

    def _planned_route(self):
        """The track still to play, plus one lap when looping, for drawing."""
        lap = self.roam_lap
        return self.roam_track + (lap[0][1:] if lap else [])

    def stop_roam(self):
        if self.roam_sid != self.session_id or not self._roam_moving():
            messagebox.showinfo("Roam", "Not moving right now.")
            return
        with self.roam_lock:
            # Drop the rest of the route; the live session keeps holding the current point (no gap).
            del self.roam_track[max(self.roam_done, 1):]
            self.roam_marks, self.roam_plan, self.roam_on_finish, self.roam_lap = {}, None, None, None
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
        if not plan or self.roam_sid != self.session_id or not self._roam_moving():
            return
        try:
            kmh = self._roam_speed()
        except ValueError:
            return  # half-typed speed; wait for a valid one
        if plan["kind"] == "Wander":
            # Same path, new pace: resample what's left from the current position.
            with self.roam_lock:
                done = max(self.roam_done, 1)
                self.roam_track[done:] = densify(self.roam_track[done - 1:], step_m(kmh))[1:]
            self._draw_route(self.roam_track, fit=False)
            self._log(f"Live update: wandering at {kmh:g} km/h from here", "ok")
            return
        # Route: work out which waypoints are still ahead, then route there from where we are now.
        done = max(self.roam_done, 1)
        reached = [t for i, t in plan["ends"] if i < done]
        wps = list(self.roam_waypoints)
        last = reached[-1] if reached else None
        remaining = wps[wps.index(last) + 1:] if last in wps else wps[min(len(reached), len(wps)):]
        loop, roads, profile = self.roam_loop_var.get() and len(wps) >= 2, self.roam_roads_var.get(), self._roam_profile()
        self.roam_paused = True  # hold still for the second or so it takes to re-route
        pos = self._roam_position()
        self.run_in_background(
            lambda: build_roam_track(pos, remaining, wps, loop, profile, roads, kmh, self._log),
            lambda res: self._splice_route(*res, len(remaining), kmh),
            lambda msg: self._plan_failed("Live update", f"{msg}\n\nKept the current route."))

    def _splice_route(self, track, ends, lap, n_remaining, kmh):
        """Replace everything after the current point with the re-planned track."""
        with self.roam_lock:
            done = max(self.roam_done, 1)
            base = done - 1  # track[0] is the current position, i.e. old index done-1
            self.roam_track[done:] = track[1:]
            if self.roam_plan:
                self.roam_plan["ends"] = ([(i, t) for i, t in self.roam_plan["ends"] if i < done]
                                          + [(i + base, t) for i, t in ends])
            self.roam_lap = lap
            self.roam_paused = False
        self._draw_route(self._planned_route(), fit=False)
        if lap:
            self._log(f"Live update: {n_remaining} waypoint(s) ahead at {kmh:g} km/h, looping until stopped", "ok")
        elif len(track) > 1:
            left = timedelta(seconds=(len(track) - 1) * ROAM_STEP_SECONDS)
            self._log(f"Live update: {n_remaining} waypoint(s) ahead at {kmh:g} km/h - {left} to go", "ok")
        else:
            self._log("Live update: no waypoints left ahead - holding here", "ok")
