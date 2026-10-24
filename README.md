<p align="center"><img src="assets/icon.png" width="128" alt="Spoofer icon"></p>

# Site Spoofer

**[⬇ Download Spoofer.exe](https://github.com/danielzhang122/Site-Spoofer/releases/latest)** (Windows)

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

The executable lands in `dist\Spoofer.exe`, with the icon from `assets/icon.ico` embedded.
pymobiledevice3 still has to be installed on the machine running it.


## Troubleshooting

| Log message | Fix |
|---|---|
| `Failed to connect to usbmuxd socket` | Install Apple Devices / iTunes |
| `Unable to connect to tunneld` | Start the tunnel; if it's running, press Esc in its window (QuickEdit) |
| `Device is password protected` / `DeviceLocked` | Unlock the phone and retry |
| `Failed to start service` | Enable Developer Mode and click Auto-mount DDI |
| `Device is not connected` | Phone not visible: check List devices / Check tunnel, see Wi-Fi notes |
