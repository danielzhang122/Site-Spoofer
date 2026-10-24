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
  roads (OSM routing) or straight lines, optionally looping; resumes mid-route after a dropped connection
- **Wander**: walk randomly around a point within a radius for a set time, with occasional pauses
- Preset waypoints, including edge cases (Null Island, antimeridian, near-polar)
- Commands run in background threads; stdout/stderr streamed to a colour-coded log (savable)
- Holds the DVT session open so the simulated location persists, and stops it cleanly on Clear/exit
- Works over **USB or Wi-Fi**, with optional auto-reconnect when a Wi-Fi session drops
- Helper buttons: start the iOS 17+ tunnel (elevated), check tunnel status, list devices, auto-mount the Developer Disk Image

## Requirements

- Windows. Python 3.10+ is needed even when using the .exe, because the app drives the
  pymobiledevice3 command-line tool rather than bundling it
- `pip install pymobiledevice3`
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
