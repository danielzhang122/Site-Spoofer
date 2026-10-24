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
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

APP_TITLE = "GPS Injector (pymobiledevice3)"

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

    def _pump(self, stream, tag):
        for line in iter(stream.readline, ""):
            self.log(line.rstrip(), tag)
        stream.close()

    def _watch(self, proc, label, on_exit=None):
        readers = [
            threading.Thread(target=self._pump, args=(proc.stdout, "out"), daemon=True),
            threading.Thread(target=self._pump, args=(proc.stderr, "err"), daemon=True),
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

    def run_persistent(self, args, label, on_exit=None):
        """Start `simulate-location set`. The DVT session must stay open for the
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
            self._watch(proc, label, on_exit)

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
        self.geometry("820x640")
        self.minsize(680, 520)

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

        self._build_ui()
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

        coords = ttk.LabelFrame(self, text="Coordinates")
        coords.pack(fill="x", **pad)
        ttk.Label(coords, text="Latitude:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(coords, textvariable=self.lat_var, width=18).grid(row=0, column=1, **pad)
        ttk.Label(coords, text="Longitude:").grid(row=0, column=2, sticky="w", **pad)
        ttk.Entry(coords, textvariable=self.lon_var, width=18).grid(row=0, column=3, **pad)
        ttk.Button(coords, text="Set location", command=self.set_location).grid(row=0, column=4, **pad)
        ttk.Button(coords, text="Clear location", command=self.clear_location).grid(row=0, column=5, **pad)

        presets = ttk.LabelFrame(self, text="Preset waypoints")
        presets.pack(fill="x", **pad)
        cols = 5
        for i, (name, lat, lon) in enumerate(PRESETS):
            ttk.Button(
                presets, text=name, command=lambda la=lat, lo=lon, n=name: self.apply_preset(n, la, lo)
            ).grid(row=i // cols, column=i % cols, sticky="ew", padx=3, pady=3)
        for c in range(cols):
            presets.columnconfigure(c, weight=1)

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
        self._start_session(args, self.session_id)

    def _start_session(self, args, sid):
        started = time.monotonic()
        # on_exit runs on a worker thread; hand the follow-up to the UI thread via the queue.
        self.pm.run_persistent(
            args, "set", on_exit=lambda _code: self.log_queue.put(lambda: self._session_ended(args, sid, started))
        )

    def _session_ended(self, args, sid, started):
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
        self.after(RECONNECT_DELAY_MS, lambda: sid == self.session_id and self._start_session(args, sid))

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
        self.lat_var.set(f"{lat:.6f}")
        self.lon_var.set(f"{lon:.6f}")
        self._log(f"Preset selected: {name}")
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
        self.status_var.set("Simulation session active" if self.pm.is_active() else "Idle")
        self.after(500, self._refresh_status)

    def _on_close(self):
        if self.pm.is_active():
            if not messagebox.askyesno("Quit", "A simulation session is active. Stop it and quit?"):
                return
            self.pm._stop_active()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
