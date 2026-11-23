"""Palette and controls shared by Suwan's desktop conversations.

This module only describes presentation. It does not read configuration, start
services or alter any saved user preference.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


FONT = "Microsoft YaHei UI"
BG = "#F5F0EB"
PANEL = "#FFFCF8"
FIELD = "#F6EFEA"
FG = "#352A29"
SUB = "#71615E"
ACCENT = "#74485F"
ACCENT_SOFT = "#EFE1E8"
LINE = "#E9DED7"
SIDEBAR = "#F7F0EA"
OK = "#356151"
ERR = "#9B443F"
WARN = "#875D2C"
RAIL = "#332836"
RAIL_TEXT = "#FCF4EE"
CHAT_BG = "#FAF7F4"
CHAT_IN = "#FFFFFF"
CHAT_OUT = "#E9E9F7"
LOG_COLORS = {"info": SUB, "ok": OK, "warn": WARN, "error": ERR}


def apply_theme(root: tk.Misc) -> ttk.Style:
    """Apply the desktop palette and return the owning ttk Style.

    ``Warm.TNotebook`` keeps the normal notebook pages and selection behavior;
    only its tab strip is hidden because navigation lives in the sidebar.
    """
    root.option_add("*Font", (FONT, 10))
    for widget in ("Entry", "Text", "Listbox"):
        root.option_add(f"*{widget}.background", FIELD if widget == "Entry" else PANEL)
        root.option_add(f"*{widget}.foreground", FG)
        root.option_add(f"*{widget}.selectBackground", ACCENT_SOFT)
        root.option_add(f"*{widget}.selectForeground", FG)
        root.option_add(f"*{widget}.insertBackground", FG)
    root.option_add("*TCombobox*Listbox.background", PANEL)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
    root.option_add("*TCombobox*Listbox.selectForeground", FG)
    root.option_add("*Menu.background", PANEL)
    root.option_add("*Menu.foreground", FG)
    root.option_add("*Menu.activeBackground", ACCENT_SOFT)
    root.option_add("*Menu.activeForeground", FG)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", font=(FONT, 10), background=BG, foreground=FG,
                    bordercolor=LINE, lightcolor=PANEL, darkcolor=LINE,
                    focuscolor=ACCENT)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=PANEL, borderwidth=1, relief="solid")
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Card.TLabel", background=PANEL, foreground=FG)
    style.configure("Muted.TLabel", background=BG, foreground=SUB)
    style.configure("TLabelframe", background=PANEL, borderwidth=1, relief="solid",
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
    style.configure("TLabelframe.Label", background=PANEL, foreground=ACCENT,
                    font=(FONT, 10, "bold"))

    style.configure("TButton", background=FIELD, foreground=FG, borderwidth=0,
                    focusthickness=1, focuscolor=ACCENT, anchor="center",
                    padding=(14, 8), font=(FONT, 9))
    style.map("TButton", background=[("disabled", FIELD), ("pressed", LINE), ("active", ACCENT_SOFT)],
              foreground=[("disabled", SUB), ("active", FG)],
              bordercolor=[("focus", ACCENT)])
    style.configure("Accent.TButton", background=ACCENT, foreground=PANEL,
                    padding=(20, 9), font=(FONT, 10, "bold"))
    style.map("Accent.TButton", background=[("disabled", LINE), ("pressed", RAIL),
                                            ("active", "#895672")],
              foreground=[("disabled", SUB), ("active", PANEL), ("pressed", PANEL)])
    style.configure("Quiet.TButton", background=PANEL, foreground=ACCENT,
                    padding=(10, 7), font=(FONT, 9))
    style.map("Quiet.TButton", background=[("disabled", PANEL), ("pressed", FIELD),
                                           ("active", ACCENT_SOFT)],
              foreground=[("disabled", SUB), ("active", ACCENT)])
    style.configure("Review.TButton", background=PANEL, foreground=ACCENT, padding=(5, 7),
                    font=(FONT, 9, "bold"))
    style.map("Review.TButton", background=[("active", "#F5E5DC"), ("pressed", FIELD)])

    for name in ("TCheckbutton", "TRadiobutton", "Radiobutton"):
        style.configure(name, background=PANEL, foreground=FG, font=(FONT, 9),
                        focuscolor=ACCENT, padding=(2, 3))
        style.map(name, background=[("active", PANEL), ("disabled", PANEL)],
                  foreground=[("disabled", SUB)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", PANEL)])

    style.configure("TEntry", fieldbackground=FIELD, foreground=FG,
                    insertcolor=FG, bordercolor=LINE, padding=(8, 6))
    style.map("TEntry", bordercolor=[("focus", ACCENT)],
              fieldbackground=[("disabled", BG)], foreground=[("disabled", SUB)])
    style.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                    foreground=FG, arrowcolor=ACCENT, bordercolor=LINE,
                    lightcolor=FIELD, darkcolor=FIELD, padding=(8, 5),
                    selectbackground=ACCENT_SOFT, selectforeground=FG, arrowsize=12)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", BG)],
              background=[("active", ACCENT_SOFT)], foreground=[("disabled", SUB), ("readonly", FG)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)],
              bordercolor=[("focus", ACCENT)])

    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                    borderwidth=0, relief="flat", rowheight=30, font=(FONT, 9))
    style.map("Treeview", background=[("selected", ACCENT_SOFT)],
              foreground=[("selected", FG)])
    style.configure("Treeview.Heading", background=FIELD, foreground=SUB,
                    borderwidth=0, relief="flat", padding=(10, 8), font=(FONT, 9))
    style.map("Treeview.Heading", background=[("active", ACCENT_SOFT)],
              foreground=[("active", FG)])

    for direction in ("Vertical", "Horizontal"):
        style.configure(f"{direction}.TScrollbar", background=LINE, troughcolor=PANEL,
                        bordercolor=PANEL, lightcolor=LINE, darkcolor=LINE,
                        arrowcolor=SUB, arrowsize=10, gripcount=0, borderwidth=0)
        style.map(f"{direction}.TScrollbar", background=[("active", "#D6C5BA"),
                                                       ("pressed", "#C8B2A5")])
        style.configure(f"{direction}.TSeparator", background=LINE)
    style.configure("TProgressbar", background=ACCENT, troughcolor=FIELD,
                    bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT)
    style.configure("TScale", background=PANEL, troughcolor=FIELD,
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)

    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background=FIELD, foreground=SUB,
                    padding=(16, 9), font=(FONT, 10))
    style.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", ACCENT)])
    style.configure("Warm.TNotebook", background=BG, borderwidth=0,
                    tabmargins=(0, 0, 0, 0), padding=0)
    style.layout("Warm.TNotebook.Tab", [])
    return style


def rounded_points(x1: float, y1: float, x2: float, y2: float, radius: float) -> tuple[float, ...]:
    radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    return (
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    )


def rounded_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                 radius: float, **options) -> int:
    return canvas.create_polygon(*rounded_points(x1, y1, x2, y2, radius),
                                 smooth=True, splinesteps=16, **options)


def draw_mark(canvas: tk.Canvas) -> None:
    """Draw a quiet sun on the horizon using local vector primitives only."""
    canvas.delete("warm_mark")
    width = max(1, float(canvas.cget("width")))
    height = max(1, float(canvas.cget("height")))
    size = min(width, height)
    left, top = (width - size) / 2, (height - size) / 2

    def box(x1, y1, x2, y2):
        return (left + x1 * size, top + y1 * size,
                left + x2 * size, top + y2 * size)

    canvas.create_oval(*box(.06, .06, .94, .94), fill=ACCENT_SOFT, outline="", tags="warm_mark")
    canvas.create_oval(*box(.31, .22, .69, .60), fill="#DEB18C", outline="", tags="warm_mark")
    canvas.create_arc(*box(.14, .43, .86, 1.0), start=13, extent=154, style=tk.ARC,
                      outline=ACCENT, width=max(1.5, size * .035), tags="warm_mark")
    canvas.create_line(*box(.23, .69, .77, .69), fill=ACCENT,
                       width=max(1.2, size * .025), capstyle=tk.ROUND, tags="warm_mark")
