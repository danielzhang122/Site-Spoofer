"""ConnectionMixin: the Connection frame, Set/Clear location, and keeping the device session alive.

A session is one persistent pymobiledevice3 process holding the simulated location (a fixed `set`, or a
live roam). session_id is bumped whenever a new session replaces the old one, so a stale session's
exit never triggers a reconnect.
"""

import json
import os
import socket
import threading
import time
import urllib.request
from tkinter import messagebox, ttk

from ..device import TUNNELD_PORT, TUNNELD_URL, split_command
from .widgets import reports_errors

RECONNECT_DELAY_MS = 5000


class ConnectionMixin:
    def _build_connection(self, parent, pad):
        conn = ttk.LabelFrame(parent, text="Connection")
        conn.pack(fill="x", **pad)
        ttk.Label(conn, text="CLI command:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(conn, textvariable=self.cli_var).grid(row=0, column=1, columnspan=3, sticky="ew", **pad)
        ttk.Label(conn, text="UDID (optional):").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(conn, textvariable=self.udid_var, width=40).grid(row=1, column=1, sticky="w", **pad)
        ttk.Checkbutton(conn, text="iOS 17+ (use tunneld)", variable=self.tunnel_var).grid(
            row=1, column=2, sticky="w", **pad)
        ttk.Checkbutton(conn, text="Auto-reconnect (Wi-Fi)", variable=self.reconnect_var).grid(
            row=1, column=3, sticky="w", **pad)
        btns = ttk.Frame(conn)
        btns.grid(row=2, column=0, columnspan=4, sticky="w", **pad)
        for text, cmd in [("Start tunnel (admin)", self.start_tunnel), ("Check tunnel", self.check_tunnel),
                          ("List devices", self.list_devices), ("Auto-mount DDI", self.mount_ddi)]:
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=3)
        conn.columnconfigure(1, weight=1)

    def _base(self):
        """The pymobiledevice3 command as an argv prefix."""
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

    def _cli(self, *command):
        """base + command + connection args, e.g. self._cli("mounter", "auto-mount")."""
        return self._base() + list(command) + self._conn_args()

    # ---------- set / clear ----------
    @reports_errors("Invalid input")
    def set_location(self):
        lat, lon = self._read_coords()
        args = self._cli("developer", "dvt", "simulate-location", "set") + ["--", f"{lat:.6f}", f"{lon:.6f}"]
        if self._live_ok():
            # A live session is connected: just jump there instead of reconnecting (no gap).
            with self.roam_lock:
                del self.roam_track[self.roam_done:]
                self.roam_track.append((lat, lon))
                self.roam_marks, self.roam_plan, self.roam_on_finish, self.roam_lap = {}, None, None, None
            self._clear_route_drawing()
            self._log(f"Moved to {lat:.6f}, {lon:.6f} (live)")
            return
        self._log(f"Setting location to {lat:.6f}, {lon:.6f}")
        self.session_id += 1
        self.retries = 0
        self.sim_pos = (lat, lon)
        self._clear_route_drawing()
        self._start_session(lambda: args, self.session_id)

    @reports_errors("Invalid input")
    def clear_location(self):
        args = self._cli("developer", "dvt", "simulate-location", "clear")
        self.session_id += 1
        self.sim_pos = None
        self._clear_route_drawing()
        self._log("Clearing simulated location")
        self.pm.stop_active_async(then=lambda: self.pm.run(args, "clear"))

    # ---------- session lifecycle ----------
    def _start_session(self, make_args, sid, label="set", line_cb=None):
        """make_args is called on every (re)start, so a roam can resume where it stopped."""
        try:
            args = make_args()
        except (OSError, ValueError) as exc:
            self._log(f"[{label}] could not prepare command: {exc}", "err")
            return
        started = time.monotonic()
        # on_exit runs on a worker thread; hand the follow-up to the UI thread via the queue.
        self.pm.run(
            args, label, persistent=True, line_cb=line_cb,
            on_exit=lambda _code: self.log_queue.put(
                lambda: self._session_ended(make_args, sid, started, label, line_cb)),
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
        self.retries += 1
        self._log(f"Session lost - reconnecting in {RECONNECT_DELAY_MS // 1000}s (attempt {self.retries})", "info")
        self.after(RECONNECT_DELAY_MS,
                   lambda: sid == self.session_id and self._start_session(make_args, sid, label, line_cb))

    # ---------- device helpers ----------
    @reports_errors("Invalid input")
    def list_devices(self):
        self.pm.run(self._base() + ["usbmux", "list"], "usbmux list")

    @reports_errors("Invalid input")
    def mount_ddi(self):
        # Go through the tunnel when enabled so mounting also works over Wi-Fi
        # (plain usbmux defaults to the first USB-connected device).
        self.pm.run(self._cli("mounter", "auto-mount"), "auto-mount")

    @reports_errors("Invalid input")
    def start_tunnel(self):
        """Open an elevated PowerShell window running tunneld (triggers a UAC prompt)."""
        if os.name != "nt":
            messagebox.showinfo("Start tunnel", "Run `sudo python3 -m pymobiledevice3 remote tunneld` manually.")
            return
        import ctypes

        try:
            with socket.create_connection(("127.0.0.1", TUNNELD_PORT), timeout=0.5):
                pass
            self._log(f"A tunnel is already running on port {TUNNELD_PORT} - not starting another. "
                      "Use 'Check tunnel' to see its state.", "info")
            return
        except OSError:
            pass  # nothing listening, go ahead
        ps_cmd = "& " + " ".join("'" + t.replace("'", "''") + "'" for t in self._base()) + " remote tunneld"
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", f'-NoExit -Command "{ps_cmd}"',
                                                 None, 1)
        if rc <= 32:
            self._log(f"Could not start elevated tunnel (ShellExecute code {rc}, UAC declined?)", "err")
        else:
            self._log("Tunnel window launched. Don't click inside it (QuickEdit pauses it).", "ok")

    def check_tunnel(self):
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
                    # usbmux USB tunnels end in "-USB"; Network/mobdev2/wifi ones are all over the LAN.
                    kind = "USB" if t.get("interface", "").endswith("-USB") else "Wi-Fi"
                    self._log(f"Tunnel to {udid} via {kind} ({t.get('tunnel-address')}:{t.get('tunnel-port')})", "ok")

        threading.Thread(target=worker, daemon=True).start()
