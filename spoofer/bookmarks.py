"""Bookmarks: a JSON list of {"name", "lat", "lon", optional "address"} in %APPDATA%\\Spoofer."""

import json
import os

BOOKMARKS_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Spoofer", "bookmarks.json")


def load_bookmarks():
    try:
        with open(BOOKMARKS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return [b for b in data if {"name", "lat", "lon"} <= b.keys()]
    except FileNotFoundError:
        return []


def save_bookmarks(bookmarks):
    os.makedirs(os.path.dirname(BOOKMARKS_PATH), exist_ok=True)
    tmp = BOOKMARKS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bookmarks, f, indent=2, ensure_ascii=False)
    os.replace(tmp, BOOKMARKS_PATH)  # atomic, so a crash can't truncate the file
