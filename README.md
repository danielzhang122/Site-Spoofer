<p align="center"><img src="assets/icon.png" width="128" alt="Globe Ghost icon"></p>

# Globe Ghost

**[⬇ Download Spoofer.exe](https://github.com/danielzhang122/globe-ghost/releases/latest)** (Windows)

A Windows desktop app (Tkinter) for setting a simulated GPS location on an iOS device
during integration testing of location-aware apps. It's a GUI wrapper around
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3)'s
`developer dvt simulate-location` commands.

## Features

- Latitude/longitude input with range validation, plus **Set** / **Clear** location
- **Address search** via OpenStreetMap Nominatim (no API key; pick from up to 5 matches)
- **Bookmarks** saved to `%APPDATA%\Spoofer\bookmarks.json` (set, load, rename, delete)
- **Roaming**: move along a multi-waypoint route at walk/run/cycle/drive speed, following real
  roads (OSM routing) or straight lines, optionally looping until stopped; resumes mid-route after a
  dropped connection. Walking and running prefer footpaths, park paths and trails over streets.
  No caps on speed, distance or duration
  **Edits apply live**: change the speed, mode, roads, loop or waypoints mid-roam and it re-plans from
  where the phone is now, without restarting or dropping the session
- **Wander**: walk randomly around a point within a radius for a set time, with occasional pauses
- **Live map**: click to pick coordinates; right-click to set the location, add a waypoint or place a
  geofence; watch the phone's position, planned route and travelled trail update in real time.
  Map tiles are cached in `%LOCALAPPDATA%\Spoofer\tiles` so places you've seen load instantly
- **Scenarios**: script a repeatable test (`start`, `wait`, `walk/drive to`, `wander`, `clear`) and run
  it as one continuous session, with each step logged as it happens
- **Geofence tester**: crosses a circular region at several speeds and headings and logs every
  ENTER / EXIT with a timestamp, to check an app's region-monitoring behaviour
- Preset waypoints, including edge cases (Null Island, antimeridian, near-polar)
- Commands run in background threads; stdout/stderr streamed to a colour-coded log (savable)
- Holds the DVT session open so the simulated location persists, and stops it cleanly on Clear/exit
- Works over **USB or Wi-Fi**, with optional auto-reconnect (keeps retrying) when a Wi-Fi session drops
- Helper buttons: start the iOS 17+ tunnel (elevated), check tunnel status, list devices, auto-mount the Developer Disk Image

## Requirements

- Windows. Python 3.10+ is needed even when using the .exe, because the app drives the
  pymobiledevice3 command-line tool rather than bundling it
- `pip install pymobiledevice3` (and `pip install tkintermapview` to run from source; the .exe
  already includes the map)
- Apple's USB driver: install **Apple Devices** (Microsoft Store) or iTunes
- iPhone trusted on this PC, with **Developer Mode** enabled
  (`python -m pymobiledevice3 amfi reveal-developer-mode`, then turn it on in
  Settings → Privacy & Security)

## Usage

Run `Spoofer.exe`, or from source:

```powershell
python gps_injector.py
```

Windows SmartScreen may warn about the unsigned .exe the first time; choose
**More info → Run anyway**.

1. **iOS 17+:** click **Start tunnel (admin)** and approve the UAC prompt (or run
   `python -m pymobiledevice3 remote tunneld` in an admin shell). Keep that window open and
   don't click inside it — QuickEdit selection pauses it. On iOS 16 and older, untick
   "iOS 17+" instead; no tunnel is needed.
2. Unlock the phone and click **Auto-mount DDI** (needed again after every phone reboot).
3. Enter coordinates and click **Set location**, or click a preset.
4. Click **Clear location** to return to real GPS.

The phone must be unlocked while the tunnel connects and while the disk image mounts; once a
location is set it can lock.

### Wi-Fi

- PC and phone must be on the same network, and Windows must treat that network as
  **Private** (Public blocks Apple's device discovery).
- Plug the phone in over USB once (about 10 s) and unplug it; it then shows up with
  `"ConnectionType": "Network"` under **List devices**, and **Check tunnel** reports `via Wi-Fi`.
- Guest/office networks with client isolation won't work; use USB or a hotspot.

## Scenarios

One command per line; `#` starts a comment. Places can be `"an address"`, a bookmark as
`@Name`, or `lat, lon`.

```text
start "Times Square, New York"        # jump here
wait 30s                              # 30s, 2m, 1h30m ...
walk to "Bryant Park, New York"       # walk / run / cycle / drive to <place>
wander 150m for 5m                    # random walk around the current spot
drive to @Office at 30 km/h           # optional speed override
roads off                             # straight lines from here on (roads on to undo)
clear                                 # return to real GPS when done (must be last)
```

**Check & preview** validates the script, looks up the places and draws the route on the map
without touching the phone. **Run scenario** plays it; the status bar shows the current step.
Save scenarios as `.txt` files to rerun the exact same test later.

## Geofence testing

Set a centre (right-click the map → *Set geofence centre here*), a radius, the speeds to test and
how many passes per speed. Each pass waits outside, crosses the circle, optionally dwells inside,
and exits on the far side, at a new heading each time. The log records every expected
`ENTER geofence` / `EXIT geofence` with its time; compare those with your app's region events,
then **Save log** to keep the run. iOS region monitoring can lag by tens of seconds, so use the
dwell time and a generous start distance for realistic results.

## Building an .exe

```powershell
pip install pyinstaller
pyinstaller Spoofer.spec
```

The executable lands in `dist\Spoofer.exe` (it keeps its original name), with the icon from `assets/icon.ico` embedded.
pymobiledevice3 still has to be installed on the machine running it.

## Project layout

`gps_injector.py` is just the entry point; the app is the `spoofer/` package:

| Module | What it does |
|---|---|
| `geo.py` | Distance, resampling and bearing maths on (lat, lon) points |
| `net.py` | HTTP helper, address search, map-tile cache |
| `routing.py` | Road routing (OSRM) and footpath routing (Valhalla), with fallbacks |
| `tracks.py` | Turns routes into one-point-per-second tracks: roam, wander, geofence passes |
| `scenario.py` | The scenario language: parse and compile into a track |
| `device.py` | Runs pymobiledevice3 and the live-session helper |
| `bookmarks.py` | Loads and saves `bookmarks.json` |
| `ui/` | The window: `app.py` assembles one mixin per area (`connection`, `places`, `roam`, `scenario_tab`, `geofence_tab`, `map_view`) plus shared `widgets` |

## Troubleshooting

| Log message | Fix |
|---|---|
| `Failed to connect to usbmuxd socket` | Install Apple Devices / iTunes |
| `Unable to connect to tunneld` | Start the tunnel; if it's running, press Esc in its window (QuickEdit) |
| `Device is password protected` / `DeviceLocked` | Unlock the phone and retry |
| `Failed to start service` | Enable Developer Mode and click Auto-mount DDI |
| `Device is not connected` | Phone not visible: check List devices / Check tunnel, see Wi-Fi notes |

## Potential to do

Ideas that might be added later, with no promises on whether or when:

- **Always-on station**: Globe Ghost could run around the clock on a Raspberry Pi or an old PC with an old
  iPad/iPhone plugged in over USB, so a location or route keeps playing without tying up a main computer.
  It would probably need:
  - Headless mode: start a location, route or scenario from the command line or a config file, without the window
  - Linux support (Raspberry Pi OS): run tunneld as a system service in place of the Windows-only
    "Start tunnel (admin)" button
  - Start on boot and recover by itself: re-mount the DDI after the device reboots, reconnect after USB drops
  - Remote control: a small web page on the local network to change the location or route and see the status
  - Setup guide: wiring, powered USB hub, keeping the device charged, and stopping the device from sleeping

## Credits

Globe Ghost is a front end; the hard parts are done by these projects and services:

- **[pymobiledevice3](https://github.com/doronz88/pymobiledevice3)** by doronz88 and contributors
  (GPL-3.0): all communication with the iPhone, including location simulation and the iOS 17+ tunnel.
- **[OpenStreetMap](https://www.openstreetmap.org/copyright)**: map data © OpenStreetMap contributors,
  available under the [Open Database License](https://opendatacommons.org/licenses/odbl/). Used for the
  map tiles, address search and every route.
- **[Nominatim](https://nominatim.org/)**: address search, via the OpenStreetMap Foundation's public server.
- **[FOSSGIS e.V.](https://www.fossgis.de/)**: hosts the free public routing servers Globe Ghost uses:
  - [OSRM](https://github.com/Project-OSRM/osrm-backend) for driving, cycling and fallback walking routes
  - [Valhalla](https://github.com/valhalla/valhalla) for walking routes on footpaths and trails
- **[TkinterMapView](https://github.com/TomSchimansky/TkinterMapView)** by Tom Schimansky (CC0): the map widget.
- **[Requests](https://github.com/psf/requests)** (Apache-2.0) and **[Pillow](https://github.com/python-pillow/Pillow)**
  (MIT-CMU): tile downloading and image handling for the map.
- **[PyInstaller](https://pyinstaller.org/)** (GPL-2.0 with bootloader exception): builds `Spoofer.exe`.

Please respect these services' usage policies
([OSM tiles](https://operations.osmfoundation.org/policies/tiles/),
[Nominatim](https://operations.osmfoundation.org/policies/nominatim/)). Globe Ghost sends an identifying
User-Agent, limits address searches to one per second and caches map tiles to keep its load light.
