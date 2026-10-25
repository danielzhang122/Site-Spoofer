"""Talking to the phone: everything goes through the pymobiledevice3 CLI, run as a child process.

Requirements on the workstation:
  * pip install pymobiledevice3   (invoked as an external CLI, not bundled)
  * Device paired/trusted, Developer Mode enabled, DDI mounted
  * iOS 17+: a running tunnel daemon in an elevated shell:  python -m pymobiledevice3 remote tunneld
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
TUNNELD_PORT = 49151
TUNNELD_URL = f"http://127.0.0.1:{TUNNELD_PORT}/"

# Live sessions: instead of `simulate-location play <gpx>` (which reads the whole route up front),
# run pymobiledevice3's own CLI with `play` swapped for a loop that takes "lat lon" lines on stdin.
# Connection handling (--tunnel/--udid, errors) stays exactly the CLI's; Globe Ghost feeds one point per
# tick, so routes can be edited mid-run without dropping the DVT session.
LIVE_READY = "SPOOFER LIVE READY"
LIVE_HELPER = r"""
import asyncio, os, sys
from pymobiledevice3.services.dvt.instruments import location_simulation_base as lsb

async def _live(self, filename, disable_sleep=False, timing_randomness_range=0):
    loop = asyncio.get_running_loop()
    print("SPOOFER LIVE READY", flush=True)
    while True:
        parts = (await loop.run_in_executor(None, sys.stdin.readline)).split()
        if len(parts) != 2:
            os._exit(0)  # blank line or EOF: end the session
        await self.set(float(parts[0]), float(parts[1]))

lsb.LocationSimulationBase.play_gpx_file = _live
from pymobiledevice3.__main__ import main
sys.argv = ["pymobiledevice3", "developer", "dvt", "simulate-location", "play"] + sys.argv[1:]
sys.exit(main())
"""
# `play` insists its GPX file exists, but the live helper never reads it.
GPX_PATH = os.path.join(tempfile.gettempdir(), "Spoofer", "route.gpx")
DUMMY_GPX = ('<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="Globe Ghost" '
             'xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg><trkpt lat="0" lon="0"/></trkseg></trk></gpx>\n')


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


def helper_python(base):
    """The Python that runs pymobiledevice3, derived from the CLI command."""
    exe = base[0]
    if os.path.basename(exe).lower().startswith("python"):
        return exe  # "python -m pymobiledevice3"
    for cand in (os.path.join(os.path.dirname(exe), "python.exe"),  # venv: Scripts\python.exe
                 os.path.join(os.path.dirname(os.path.dirname(exe)), "python.exe")):  # Python\Scripts\..
        if os.path.isfile(cand):
            return cand
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python") or "python"


def live_command(base, conn_args):
    """Command line for a live session (see LIVE_HELPER)."""
    os.makedirs(os.path.dirname(GPX_PATH), exist_ok=True)
    with open(GPX_PATH, "w", encoding="utf-8") as f:
        f.write(DUMMY_GPX)
    return [helper_python(base), "-c", LIVE_HELPER] + conn_args + [GPX_PATH]


class ProcessManager:
    """Runs CLI commands in background threads and streams their output to a log queue."""

    def __init__(self, log_queue):
        self.log_queue = log_queue
        self.active = None  # the long-running session process (set / live play)
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

    def run(self, args, label, on_exit=None, persistent=False, line_cb=None):
        """Run a command in the background. A persistent one (`simulate-location set` / live play)
        replaces the active session and is kept alive until stopped, because the DVT session must
        stay open for the simulated location to persist."""

        def worker():
            if persistent:
                self.stop_active()
            try:
                proc = self._spawn(args)
            except (OSError, ValueError) as exc:
                self.log(f"[{label}] failed to start: {exc}", "err")
                if on_exit:
                    on_exit(-1)
                return
            if persistent:
                with self.lock:
                    self.active = proc
            self._watch(proc, label, on_exit, line_cb)

        threading.Thread(target=worker, daemon=True).start()

    def stop_active(self):
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
            self.stop_active()
            if then:
                then()

        threading.Thread(target=worker, daemon=True).start()

    def is_active(self):
        with self.lock:
            return self.active is not None and self.active.poll() is None

    def send(self, text):
        """Write a line to the active session's stdin (live sessions). False if it isn't running."""
        with self.lock:
            proc = self.active
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.stdin.write(text + "\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False
