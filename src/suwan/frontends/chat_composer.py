"""Native multiline input inside the chat page's composer card."""
from __future__ import annotations

from math import isqrt
import tkinter as tk
from tkinter import ttk

from suwan.frontends.warm_theme import (
    ACCENT, ACCENT_SOFT, CHAT_BG, FG, FONT, LINE, PANEL, RAIL, SUB, rounded_rect,
)


def _send_button_style(master: tk.Misc) -> str:
    style = ttk.Style(master)
    name = "ComposerSend.TButton"
    border = "SuwanComposerSend.border"
    if border in style.element_names():
        return name

    radius = 10
    size = radius * 2 + 1
    images = []
    for color in (ACCENT, LINE, RAIL, "#895672"):
        image = tk.PhotoImage(master=master, width=size, height=size)
        for y in range(size):
            offset = y - radius
            inset = radius - isqrt(radius * radius - offset * offset)
            image.put(color, to=(inset, y, size - inset, y + 1))
        images.append(image)
    master.winfo_toplevel()._composer_send_images = images
    normal, disabled, pressed, hover = (str(image) for image in images)
    style.element_create(border, "image", normal, ("disabled", disabled), ("pressed", pressed),
                         ("active", hover), border=radius, sticky="nsew")
    style.layout(name, [(border, {"sticky": "nsew", "children": [
        ("Button.focus", {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"}),
            ]}),
        ]}),
    ]})])
    style.configure(name, foreground=PANEL, focuscolor=PANEL, padding=(15, 9),
                    width=5, font=(FONT, 10, "bold"))
    style.map(name, foreground=[("disabled", SUB), ("!disabled", PANEL)])
    return name


class ChatComposer(tk.Frame):
    """Keep keyboard handling and the request lifecycle with the chat page."""

    def __init__(self, parent: tk.Misc, *, send, cancel) -> None:
        super().__init__(parent, bg=CHAT_BG, padx=24, pady=10)
        self.columnconfigure(0, weight=1)
        shell = self._shell = tk.Canvas(self, bg=CHAT_BG, highlightthickness=0, borderwidth=0)
        shell.grid(row=0, column=0, sticky="ew")
        controls = tk.Frame(shell, bg=PANEL)
        controls.columnconfigure(0, weight=1)
        controls.rowconfigure(0, weight=1)
        self._controls_window = shell.create_window(14, 9, anchor="nw", window=controls)
        self.entry = tk.Text(
            controls, height=2, width=15, wrap="word", font=(FONT, 11), bg=PANEL, fg=FG,
            insertbackground=ACCENT, relief="flat", bd=0, highlightthickness=0, padx=0, pady=3,
            selectbackground=ACCENT_SOFT, selectforeground=FG,
        )
        self.entry.grid(row=0, column=0, sticky="nsew")
        self._placeholder = tk.Label(controls, text="发消息给苏晚", font=(FONT, 11),
                                     fg=SUB, bg=PANEL, padx=0, pady=0)
        self._placeholder.bind("<Button-1>", lambda _event: self.entry.focus_set())
        self.entry.bind("<<Modified>>", self._sync_placeholder, add="+")
        self.entry.bind("<FocusIn>", self._on_focus, add="+")
        self.entry.bind("<FocusOut>", self._on_focus, add="+")
        self.send_button = ttk.Button(controls, text="发送", command=send,
                                      style=_send_button_style(self), cursor="hand2")
        self.send_button.grid(row=0, column=1, sticky="e", padx=(14, 0))
        shell.config(height=max(self.entry.winfo_reqheight(), self.send_button.winfo_reqheight()) + 18)
        shell.bind("<Configure>", self._fit_shell)
        self._sync_placeholder()

        self.status = tk.StringVar(master=self, value="Enter 发送 · Shift+Enter 换行")
        footer = tk.Frame(self, bg=CHAT_BG)
        footer.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        footer.columnconfigure(0, weight=1)
        hint = tk.Label(footer, textvariable=self.status, bg=CHAT_BG, fg=SUB, anchor="w",
                        font=(FONT, 8), wraplength=465, justify="left")
        hint.grid(row=0, column=0, sticky="ew", padx=(2, 8))
        style = ttk.Style(self)
        style.configure("ComposerCancel.Quiet.TButton", background=CHAT_BG, padding=(8, 2))
        self.cancel_button = ttk.Button(footer, text="取消等待", style="ComposerCancel.Quiet.TButton",
                                        state="disabled", command=cancel)
        footer.rowconfigure(0, minsize=self.cancel_button.winfo_reqheight())
        self.cancel_button.grid(row=0, column=1, sticky="e")
        self.cancel_button.grid_remove()
        self._cancel_sync = None
        self._status_trace = self.status.trace_add("write", self._queue_cancel_sync)

    def _fit_shell(self, event) -> None:
        self._shell.itemconfigure(self._controls_window, width=max(1, event.width - 28),
                                  height=max(1, event.height - 18))
        self._paint_shell(event.width, event.height)

    def _paint_shell(self, width: int, height: int) -> None:
        self._shell.delete("card")
        if width > 2 and height > 2:
            has_focus = self.focus_get() is self.entry
            rounded_rect(self._shell, 1, 1, width - 1, height - 1, 13,
                         fill=PANEL, outline=ACCENT if has_focus else LINE, tags="card")
            self._shell.tag_lower("card")

    def _on_focus(self, _event) -> None:
        self._sync_placeholder()
        self._paint_shell(self._shell.winfo_width(), self._shell.winfo_height())

    def _sync_placeholder(self, _event=None) -> None:
        if self.focus_get() is self.entry or self.entry.compare("1.0", "<", "end-1c"):
            self._placeholder.place_forget()
        else:
            self._placeholder.place(in_=self.entry, x=0, y=3)
        if self.entry.edit_modified():
            self.entry.edit_modified(False)

    def _queue_cancel_sync(self, *_args) -> None:
        if self._cancel_sync is None:
            self._cancel_sync = self.after_idle(self._sync_cancel_visibility)

    def _sync_cancel_visibility(self) -> None:
        self._cancel_sync = None
        if self.cancel_button.instate(["disabled"]):
            self.cancel_button.grid_remove()
        else:
            self.cancel_button.grid()

    def destroy(self) -> None:
        if self._cancel_sync is not None:
            self.after_cancel(self._cancel_sync)
        self.status.trace_remove("write", self._status_trace)
        super().destroy()
