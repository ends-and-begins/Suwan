"""Scrollable, selectable message cards for the desktop conversation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
import tkinter as tk

from suwan.frontends.warm_theme import (
    ACCENT, CHAT_BG, CHAT_IN, CHAT_OUT, FG, FONT, LINE, SUB, rounded_points, rounded_rect,
)


@dataclass(frozen=True)
class _Message:
    body: int
    bubble: int
    caption: int
    outgoing: bool
    day_label: int | None


class ChatTranscript(tk.Canvas):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=CHAT_BG, highlightthickness=0, borderwidth=0, takefocus=True,
                         selectbackground=ACCENT, selectforeground=CHAT_IN, yscrollincrement=24)
        self._messages: list[_Message] = []
        self._body_at_item: dict[int, int] = {}
        self._last_day: date | None = None
        self._layout_width = 0
        self._layout_height = 0
        self._next_y = 15
        self._match: tuple[int, int, int] | None = None
        self._selection_body: int | None = None
        self._selection_start = 0
        self._popup = tk.Menu(self, tearoff=False)
        self.bind("<Configure>", self._resize)
        self.bind("<Button-1>", self._start_selection)
        self.bind("<B1-Motion>", self._extend_selection)
        self.bind("<Button-3>", self._copy_menu)
        self.bind("<Control-c>", self._copy_selection)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda _event: self.yview_scroll(-3, "units"))
        self.bind("<Button-5>", lambda _event: self.yview_scroll(3, "units"))
        for key, step, unit in (("Prior", -1, "pages"), ("Next", 1, "pages"),
                                ("Up", -1, "units"), ("Down", 1, "units")):
            self.bind(f"<{key}>", lambda _event, step=step, unit=unit: self.yview_scroll(step, unit))
        self.bind("<Home>", lambda _event: self.yview_moveto(0))
        self.bind("<End>", lambda _event: self.yview_moveto(1))

    def append(self, sender: str, now: datetime, text: str) -> None:
        day = now.date()
        day_label = None
        if self._last_day != day:
            day_label = self.create_text(0, 0, text=f"{day.month}月{day.day}日", anchor="n",
                                         font=(FONT, 8, "bold"), fill=ACCENT)
            self._last_day = day
        outgoing = sender == "你"
        body = self.create_text(0, 0, text=text, font=(FONT, 11), fill=FG, anchor="nw")
        caption = self.create_text(0, 0, text=f"{sender}  ·  {now:%H:%M}", font=(FONT, 8), fill=SUB,
                                   anchor="ne" if outgoing else "nw")
        bubble = rounded_rect(self, 0, 0, 1, 1, 0, fill=CHAT_OUT if outgoing else CHAT_IN,
                              outline="#DCDDF0" if outgoing else LINE, width=1)
        self.tag_lower(bubble, body)
        message = _Message(body, bubble, caption, outgoing, day_label)
        self._messages.append(message)
        self._body_at_item.update({body: body, bubble: body, caption: body})
        self._place_message(message, max(300, self.winfo_width()))
        self._set_region()

    def text(self) -> str:
        """Read the content of the visible transcript in chronological order."""
        return "\n\n".join(f"{self.itemcget(row.caption, 'text')}\n{self.itemcget(row.body, 'text')}"
                           for row in self._messages)

    def clear(self) -> None:
        self._popup.unpost()
        self._popup.delete(0, tk.END)
        self.clear_match()
        self.select_clear()
        self.delete("all")
        self._messages.clear()
        self._body_at_item.clear()
        self._last_day = None
        self._selection_body = None
        self._next_y = 15
        self._set_region()
        self.yview_moveto(0)

    def clear_match(self) -> None:
        if self._match is not None:
            self.select_clear()
            self._match = None

    def find_message(self, query: str, *, backwards: bool = False) -> tuple[int, int] | None:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        hits = [(row.body, hit.start(), hit.end()) for row in self._messages
                for hit in pattern.finditer(self.itemcget(row.body, "text"))]
        previous = self._match
        self.clear_match()
        if not hits:
            return None
        step = -1 if backwards else 1
        index = (hits.index(previous) + step) % len(hits) if previous in hits else (len(hits) - 1 if backwards else 0)
        body, start, after = self._match = hits[index]
        text = self.itemcget(body, "text")
        self.select_from(body, self.tk.call("string", "length", text[:start]))
        self.select_to(body, self.tk.call("string", "length", text[:after]) - 1)
        self._reveal(body, text[:start])
        return index + 1, len(hits)

    def _place_message(self, row: _Message, width: int) -> None:
        if row.day_label is not None:
            self.coords(row.day_label, width / 2, self._next_y)
            self._next_y = self.bbox(row.day_label)[3] + 10
        margin = max(22, min(44, width // 22))
        pad = 13
        self.itemconfigure(row.body, width=min(560, int(width * .70)) - pad * 2)
        bx1, by1, bx2, by2 = self.bbox(row.body)
        body_width, body_height = bx2 - bx1, by2 - by1
        left = width - margin - body_width - pad * 2 if row.outgoing else margin
        right = left + body_width + pad * 2
        self.coords(row.caption, right if row.outgoing else left, self._next_y)
        top = self.bbox(row.caption)[3] + 5
        bottom = top + body_height + 20
        self.coords(row.body, left + pad, top + 10)
        self.coords(row.bubble, *rounded_points(left, top, right, bottom, 14))
        self._next_y = bottom + 14

    def _set_region(self) -> None:
        self.configure(scrollregion=(0, 0, self.winfo_width(), max(self._next_y, self.winfo_height())))

    def _resize(self, event) -> None:
        top = self.canvasy(0)
        old_height = self._layout_height
        follow = old_height == 0 or top + old_height >= .995 * max(self._next_y, old_height)
        self._layout_height = event.height
        if event.width != self._layout_width:
            self._layout_width = event.width
            self._next_y = 15
            for row in self._messages:
                self._place_message(row, max(300, event.width))
        self._set_region()
        self.yview_moveto(1 if follow else top / max(self._next_y, event.height))

    def _reveal(self, body: int, prefix: str) -> None:
        _x1, y1, _x2, y2 = self.bbox(body)
        top = self.canvasy(0)
        height = self.winfo_height()
        if y2 - y1 > height - 48 and prefix:
            # For a match within a tall card, use the same wrapping and font as the card.
            probe = self.create_text(0, 0, text=prefix, font=self.itemcget(body, "font"),
                                     width=self.itemcget(body, "width"), anchor="nw")
            _px1, py1, _px2, py2 = self.bbox(probe)
            self.delete(probe)
            y1 += min(y2 - y1, py2 - py1)
            y2 = y1 + 24
        if y1 < top or y2 > top + height:
            self.yview_moveto(max(0, y1 - 35) / max(self._next_y, height))

    def _wheel(self, event) -> str:
        if event.delta:
            units = -int(event.delta / 40) or (-1 if event.delta > 0 else 1)
            self.yview_scroll(units, "units")
        return "break"

    def _current_body(self) -> int | None:
        current = self.find_withtag(tk.CURRENT)
        return self._body_at_item.get(current[0]) if current else None

    def _cursor_at(self, event) -> str:
        return f"@{int(self.canvasx(event.x))},{int(self.canvasy(event.y))}"

    def _start_selection(self, event) -> None:
        self.focus_set()
        self.clear_match()
        self.select_clear()
        self._selection_body = self._current_body()
        if self._selection_body is not None:
            self._selection_start = self.index(self._selection_body, self._cursor_at(event))

    def _extend_selection(self, event) -> None:
        body = self._selection_body
        if body is not None:
            end = self.index(body, self._cursor_at(event))
            self.select_from(body, self._selection_start)
            self.select_to(body, end)

    def _put_clipboard(self, text: str) -> None:
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)

    def selected_text(self) -> str:
        body = self.select_item()
        if body is None:
            return ""
        text = self.itemcget(body, "text")
        return self.tk.call("string", "range", text, self.index(body, tk.SEL_FIRST),
                            self.index(body, tk.SEL_LAST))

    def _copy_selection(self, _event=None) -> str:
        self._put_clipboard(self.selected_text())
        return "break"

    def _copy_menu(self, event) -> str:
        body = self._current_body()
        self._popup.delete(0, tk.END)
        if self.select_item() is not None:
            self._popup.add_command(label="复制选中文字", command=self._copy_selection)
        if body is not None:
            self._popup.add_command(label="复制这条消息",
                                    command=lambda: self._put_clipboard(self.itemcget(body, "text")))
        if self._messages:
            self._popup.add_command(label="复制本次会话", command=lambda: self._put_clipboard(self.text()))
            self._popup.tk_popup(event.x_root, event.y_root)
            self._popup.grab_release()
        return "break"
