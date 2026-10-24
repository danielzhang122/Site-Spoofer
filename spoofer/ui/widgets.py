"""Small Tk helpers shared by the tabs."""

import functools
import tkinter as tk
from tkinter import messagebox, ttk


def reports_errors(title):
    """Decorator for button handlers: a ValueError raised inside is shown as an error dialog.
    Validators raise ValueError with the message the user should see."""

    def wrap(fn):
        @functools.wraps(fn)
        def handler(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ValueError as exc:
                messagebox.showerror(title, str(exc))

        return handler

    return wrap


def parse_float(text, error):
    try:
        return float(text)
    except ValueError:
        raise ValueError(error) from None


def scrolled(parent, widget):
    """Pack widget (a Listbox or Text inside parent) with a vertical scrollbar."""
    sb = ttk.Scrollbar(parent, command=widget.yview)
    widget.configure(yscrollcommand=sb.set)
    widget.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
    sb.pack(side="left", fill="y", pady=4)
    return widget


def button_column(parent, buttons):
    """A column of full-width buttons from [(text, command), ...]; returns the buttons."""
    col = ttk.Frame(parent)
    col.pack(side="left", fill="y", padx=6, pady=4)
    made = [ttk.Button(col, text=text, command=cmd) for text, cmd in buttons]
    for btn in made:
        btn.pack(fill="x", pady=1)
    return made


def list_panel(parent, buttons):
    """Scrolling listbox with a button column beside it; returns the listbox."""
    lb = scrolled(parent, tk.Listbox(parent, height=6, activestyle="none", exportselection=False))
    button_column(parent, buttons)
    return lb


def labeled_entries(parent, rows, width=8):
    """Grid label + entry pairs from [(row, col, label, var), ...]."""
    for row, col, label, var in rows:
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", pady=1)
        ttk.Entry(parent, textvariable=var, width=width).grid(row=row, column=col + 1, sticky="w", padx=(2, 12))
