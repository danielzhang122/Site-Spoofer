"""
Globe Ghost - set a simulated GPS location on an iOS device for testing location-aware apps.

Entry point only; the app lives in the spoofer/ package (see spoofer/__init__.py for a map of it).
Run with:  python gps_injector.py      Build with:  pyinstaller Ghost.spec
"""

import os

from spoofer.ui import App

if __name__ == "__main__":
    if os.name == "nt":
        import ctypes

        # Give the app its own taskbar identity so Windows shows our icon, not python.exe's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Spoofer.GPS")
    App().mainloop()
