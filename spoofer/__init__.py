"""
Globe Ghost - Tkinter front end for pymobiledevice3 location simulation on iOS devices.

Package layout (no module imports the UI except gps_injector.py):
  geo         pure geometry: distances, resampling, bearings
  net         HTTP helpers, address search, the map-tile cache
  routing     road/footpath routing (OSRM, Valhalla) with fallbacks
  tracks      routes -> one-point-per-second tracks (roam, wander, geofence passes)
  scenario    the little scenario language: parse and compile into a track
  device      running pymobiledevice3 (process runner, live-session helper)
  bookmarks   bookmarks.json load/save
  ui          the Tkinter app, one file per area of the window
"""
