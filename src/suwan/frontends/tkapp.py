"""苏晚 tkinter 前端，以会话为主的桌面工作区。

左侧导航，右侧保留七页 Notebook 的选择及生命周期行为。
对话空态不写入聊天正文；侧栏、快捷入口不复制任何记忆或业务状态。
模型、偏好、撤销、保存与退出仍由原有运行时和桥接处理。
"设置"页：API/联网开关、模型=供应商+具体模型二级下拉、音色/自动朗读/朗读上句、测试连接
"系统日志"页：日志正文 + 级别过滤(全部/INFO/OK/WARN/ERROR) + 清空
分流：对话=人话；其余系统提示全部进日志。
用法: python -m suwan.frontends.tkapp [--home <数据目录>]
"""
from __future__ import annotations

import argparse
import logging
import queue
import time
import tkinter as tk
import weakref
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, simpledialog, ttk

from suwan.adapters.tts import VOICE_PRESETS
from suwan.adapters.playback import AudioPlayback
from suwan.config import ENDPOINT_ENV, MODEL_CANDIDATES, PROVIDER_LABELS, Settings
from suwan.frontends.bridge import RuntimeThread, TkBridge
from suwan.frontends.chat_composer import ChatComposer
from suwan.frontends.chat_transcript import ChatTranscript
from suwan.frontends.report_window import ReportWindow
from suwan.frontends.warm_theme import (
    ACCENT, ACCENT_SOFT, BG, CHAT_BG, ERR, FG, FIELD,
    FONT, LINE, LOG_COLORS, OK, PANEL, RAIL, RAIL_TEXT, SIDEBAR, SUB, WARN,
    apply_theme, draw_mark,
)

try:
    from suwan.app import SuwanRuntime  # noqa: PLC0415
except ImportError:  # pragma: no cover
    SuwanRuntime = None  # type: ignore[assignment]

LOG_LEVELS = ("ALL", "INFO", "OK", "WARN", "ERROR")
API_OFF_HINT = "● API 未开启，请勾选「启用模型 API」"
_LOG = logging.getLogger(__name__)


class TkApp:
    def __init__(self, runtime) -> None:
        self._ui_callbacks = queue.SimpleQueue()
        self._closing = False
        self._after_ids: set[str] = set()
        self._speech_generation = 0
        self._speech_future = None
        self._playback = AudioPlayback()
        self.server = RuntimeThread(runtime, TkBridge())
        self.server.start()
        self.root = tk.Tk()
        self.root.title("苏晚 · 把日子慢慢说")
        self.root.geometry("1180x820")
        self.root.minsize(960, 660)
        self.root.configure(bg=BG)
        self._style()
        artwork_path = Path(__file__).resolve().parents[1] / "assets" / "evening-window.png"
        artwork = tk.PhotoImage(master=self.root, file=str(artwork_path))
        self._welcome_art = artwork.subsample(3)
        self._review_art_wide = artwork.subsample(8)
        self._review_art_narrow = artwork.subsample(10)
        self._review_has_art = False
        self.api_status_text = tk.StringVar(value=API_OFF_HINT)

        self._provider_by_label = {PROVIDER_LABELS[p]: p for p in ENDPOINT_ENV}
        self._last_reply = ""
        self._last_reply_epoch = None
        self._ui_context_epoch = self.server.context_epoch()
        self._memory_generation = 0
        self._memory_rows: dict[str, dict] = {}
        self._memory_page = self._memory_pages = 1
        self._memory_mode = "list"
        self._memory_search_text = ""
        self._interaction_generation = 0
        self._interaction_pending = False
        self._interaction_mutating = False
        self._activity_generation = 0
        self._activity_pending = False
        self._activity_mutating = False
        self._activity_rows: dict[str, dict] = {}
        self._learning_generation = 0
        self._works_generation = 0
        self._api_probe_generation = 0
        self._api_probe_indicator = None
        self._chat_generation = 0
        self._chat_future = None
        self._chat_started = 0.0
        self._desktop_generation = 0
        self._desktop_pending = False
        self._desktop_settings_loaded = False
        self._settings_generation = 0
        self._settings_pending = False
        self._settings_flush_id = None
        self._pending_settings_patch: dict = {}
        self._pending_settings_revisions: dict[str, int] = {}
        self._desktop_draft_revisions: dict[str, int] = {}
        self._desktop_dirty_fields: set[str] = set()
        self._desktop_applying_settings = False
        self._desktop_committed_settings: dict = {}
        self._settings_future = None
        self._playback_volume = getattr(runtime, "tts_volume", 100)
        self._playback_auto_read = bool(getattr(runtime, "tts_enabled", False))
        self._review_rows: dict[str, dict] = {}
        self.review_window = None
        self.report_window: ReportWindow | None = None
        self._log_entries: list[tuple[str, str]] = []  # (level, text)
        self._toggles: list = []  # (canvas, oval, var, on_color, on_toggle)

        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(1, weight=1)
        self._build_sidebar()
        self._build_header(0)
        self._build_notebook(1)
        self._build_statusbar(2)
        self._navigate("chat")
        self.root.bind("<Configure>", self._resize_shell, add="+")
        for number, key in enumerate(self._pages, 1):
            self.root.bind(f"<Alt-Key-{number}>", lambda _event, target=key: self._navigate(target))
        self.root.bind("<Control-f>", self._open_chat_find)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._pump()
        self._sync_settings_from_runtime()
        self._desktop_draft_revisions["model"] = 0
        self.model_var.trace_add("write", lambda *_args: self._desktop_draft_edited("model"))
        self._refresh_api_indicator()
        self._log_msg("苏晚 v7 候选版已启动 · 今日回顾可查询共同经历、活动和学习；设置集中管理声音、安静时段与调用预算", "info")
        for warning in getattr(runtime.settings, "configuration_warnings", ()):
            self._log_msg(warning, "warn")
        for warning in getattr(runtime, "startup_warnings", ()):
            self._log_msg(warning, "warn")
        self._schedule_views()
        self._memory_list()

    # ---------- 样式 ----------
    def _style(self) -> None:
        apply_theme(self.root)

    def _label(self, parent, text: str, *, color: str = FG, size: int = 9, bg: str | None = None) -> tk.Label:
        return tk.Label(parent, text=text, font=(FONT, size), fg=color, bg=bg or parent.cget("background"))

    def _scrolled(self, parent, *, bg: str = PANEL, fg: str = FG) -> scrolledtext.ScrolledText:
        area = scrolledtext.ScrolledText(parent, wrap=tk.WORD, font=(FONT, 10), bg=bg, fg=fg,
                                         insertbackground=FG, relief=tk.FLAT, borderwidth=0,
                                         highlightthickness=0, padx=16, pady=12,
                                         selectbackground=ACCENT_SOFT, selectforeground=FG,
                                         spacing1=3, spacing3=5)
        area.frame.configure(background=bg)
        area.vbar.configure(background=bg, activebackground=LINE, troughcolor=bg,
                            relief=tk.FLAT, borderwidth=0, width=10)
        return area

    def _build_sidebar(self) -> None:
        self._pages = {
            "chat": ("苏晚", "本次会话"),
            "memory": ("记忆", "搜索、修正与遗忘"),
            "status": ("近况", "活动进度与调用预算"),
            "works": ("作品", "阅读已保存的作品"),
            "story": ("故事", "按时间回看生活记录"),
            "settings": ("设置", "模型连接、声音和偏好"),
            "log": ("运行记录", "查看操作结果与错误"),
        }
        self.sidebar = tk.Frame(self.root, bg=SIDEBAR, width=254)
        self.sidebar.grid(row=0, column=0, rowspan=3, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(1, weight=1)
        self.sidebar.grid_rowconfigure(0, weight=1)
        rail = tk.Frame(self.sidebar, bg=RAIL, width=64)
        rail.grid(row=0, column=0, sticky="ns")
        rail.pack_propagate(False)
        mark = tk.Canvas(rail, width=34, height=34, bg=RAIL, highlightthickness=0)
        mark.pack(pady=(16, 16))
        draw_mark(mark)
        tools = tk.Frame(rail, bg=RAIL)
        tools.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 12))
        rail_labels = {"chat": "会话", "log": "日志"}
        self.nav_buttons = {}
        self._rail_tiles = {}
        for key, (title, _caption) in self._pages.items():
            target = tools if key in {"settings", "log"} else rail
            tile = tk.Frame(target, bg=RAIL)
            tile.pack(fill=tk.X, padx=7, pady=2)
            icon = tk.Canvas(tile, width=24, height=22, bg=RAIL, highlightthickness=0, cursor="hand2")
            icon.pack(pady=(4, 0))
            self._draw_rail_icon(icon, key)
            button = tk.Button(tile, text=rail_labels.get(key, title), font=(FONT, 8),
                               bg=RAIL, fg=RAIL_TEXT, activebackground=RAIL, activeforeground=RAIL_TEXT,
                               relief=tk.FLAT, bd=0, padx=0, pady=1, cursor="hand2",
                               highlightthickness=1, highlightbackground=RAIL, highlightcolor=RAIL_TEXT,
                               takefocus=True, command=lambda target=key: self._navigate(target))
            button.pack(fill=tk.X, padx=3, pady=(0, 2))
            marker = tk.Frame(tile, bg=RAIL, width=2)
            marker.place(x=0, rely=.19, relheight=.62)
            for surface in (tile, icon, marker):
                surface.bind("<ButtonRelease-1>", lambda _event, control=button: control.invoke())
            self.nav_buttons[key] = button
            self._rail_tiles[key] = (tile, icon, marker)

        inbox = self._chat_inbox = tk.Frame(self.sidebar, bg=SIDEBAR)
        inbox.grid(row=0, column=1, sticky="nsew")
        inbox.grid_columnconfigure(0, weight=1)
        inbox.grid_rowconfigure(2, weight=1)
        self._label(inbox, "会话", color=FG, size=16).grid(row=0, column=0, sticky="w", padx=14, pady=(19, 15))

        self._chat_sidebar_preview = tk.StringVar(value="还没有消息")
        self._chat_sidebar_time = tk.StringVar()
        conversation = tk.Frame(inbox, bg=ACCENT_SOFT, cursor="hand2", height=62)
        conversation.grid(row=1, column=0, sticky="ew", padx=(8, 9))
        conversation.grid_propagate(False)
        conversation.grid_columnconfigure(1, weight=1)
        conversation.grid_rowconfigure(0, weight=1)
        avatar = tk.Canvas(conversation, width=34, height=34, bg=ACCENT_SOFT, highlightthickness=0)
        avatar.grid(row=0, column=0, padx=(8, 6))
        draw_mark(avatar)
        details = tk.Frame(conversation, bg=ACCENT_SOFT)
        details.grid(row=0, column=1, sticky="ew", padx=(0, 7))
        details.grid_columnconfigure(0, weight=1)
        name = tk.Label(details, text="苏晚", bg=ACCENT_SOFT, fg=FG, font=(FONT, 10, "bold"))
        name.grid(row=0, column=0, sticky="w")
        clock = tk.Label(details, textvariable=self._chat_sidebar_time, bg=ACCENT_SOFT, fg=SUB, font=(FONT, 7))
        clock.grid(row=0, column=1, sticky="e")
        preview = self._chat_sidebar_preview_label = tk.Label(
            details, bg=ACCENT_SOFT, fg=SUB, font=(FONT, 8), anchor="w", width=1,
        )
        preview.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(1, 0))
        preview.bind("<Configure>", self._fit_sidebar_preview)
        self._chat_sidebar_preview.trace_add("write", self._fit_sidebar_preview)
        for widget in (conversation, avatar, details, name, clock, preview):
            widget.bind("<ButtonRelease-1>", lambda _event: self._navigate("chat"))

        self.review_button = ttk.Button(inbox, text="今日回顾  ›", style="Review.TButton",
                                        compound=tk.TOP, command=self._open_review)
        self.review_button.grid(row=3, column=0, sticky="ew", padx=(8, 9), pady=(10, 9))
        foot = tk.Frame(inbox, bg=SIDEBAR)
        foot.grid(row=4, column=0, sticky="ew", padx=13, pady=(2, 12))
        tk.Frame(foot, bg=LINE, height=1).pack(fill=tk.X, pady=(0, 9))
        self._label(foot, "当前模型", size=8, color=FG).pack(anchor="w")
        self.mode_lbl = tk.Label(foot, text="", font=(FONT, 8), fg=SUB, bg=SIDEBAR,
                                 wraplength=156, justify="left", anchor="w")
        self.mode_lbl.pack(anchor="w", pady=(2, 0))
        tk.Frame(inbox, bg=LINE, width=1).place(relx=1, x=-1, relheight=1)

    def _fit_sidebar_preview(self, *_args) -> None:
        label = self._chat_sidebar_preview_label
        width = label.winfo_width() - 4
        if width < 1:
            return
        font = label.cget("font")
        text = self._chat_sidebar_preview.get()
        if int(label.tk.call("font", "measure", font, text)) > width:
            text = text.rstrip("…")
            while text and int(label.tk.call("font", "measure", font, text + "…")) > width:
                text = text[:-1]
            text += "…"
        label.configure(text=text)

    @staticmethod
    def _draw_rail_icon(icon: tk.Canvas, key: str) -> None:
        def line(*points):
            icon.create_line(*points, fill=RAIL_TEXT, width=1.5, capstyle=tk.ROUND,
                             joinstyle=tk.ROUND, tags="stroke")

        def shape(kind, *box):
            kind(*box, outline=RAIL_TEXT, width=1.5, tags="outline")

        if key == "chat":
            line(4, 4, 20, 4, 20, 15, 11, 15, 7, 19, 7, 15, 4, 15, 4, 4)
            line(8, 9, 16, 9)
        elif key == "memory":
            line(12, 6, 8, 4, 4, 4, 4, 16, 8, 16, 12, 18, 16, 16, 20, 16, 20, 4, 16, 4, 12, 6, 12, 18)
        elif key == "status":
            line(3, 12, 8, 12, 11, 5, 14, 17, 17, 12, 21, 12)
        elif key == "works":
            line(4, 8, 4, 5, 10, 5, 12, 8, 20, 8, 20, 18, 4, 18, 4, 8, 12, 8)
        elif key == "story":
            line(6, 3, 15, 3, 19, 7, 19, 19, 6, 19, 6, 3)
            line(15, 3, 15, 7, 19, 7)
            line(9, 11, 16, 11)
            line(9, 15, 15, 15)
        elif key == "settings":
            line(5, 5, 19, 5)
            line(5, 11, 19, 11)
            line(5, 17, 19, 17)
            line(9, 3, 9, 7)
            line(15, 9, 15, 13)
            line(10, 15, 10, 19)
        else:
            shape(icon.create_rectangle, 4, 4, 20, 18)
            line(8, 8, 11, 11, 8, 14)
            line(13, 14, 17, 14)

    def _navigate(self, key: str) -> str:
        if self._closing or key not in self._pages:
            return "break"
        self.notebook.select(self._page_frames[key])
        self._sync_navigation()
        return "break"

    def _sync_navigation(self, _event=None) -> None:
        selected = self.notebook.select()
        for key, frame in self._page_frames.items():
            active = str(frame) == selected
            tile, icon, marker = self._rail_tiles[key]
            background = "#62455B" if active else RAIL
            foreground = "#FFF4EC" if active else RAIL_TEXT
            tile.configure(bg=background)
            icon.configure(bg=background)
            icon.itemconfigure("stroke", fill=foreground)
            icon.itemconfigure("outline", outline=foreground)
            marker.configure(bg="#EBB8A6" if active else background)
            self.nav_buttons[key].configure(bg=background, fg=foreground, activebackground=background,
                                            activeforeground=foreground, highlightbackground=background)
            if not active:
                continue
            self.page_title.set(self._pages[key][0])
            self.subtitle.configure(text=self._pages[key][1])
            if key == "chat":
                self._header_avatar.grid()
                self._chat_header_actions.grid()
                self._header_title.grid_configure(padx=(0, 10))
                self._chat_inbox.grid()
            else:
                self._header_avatar.grid_remove()
                self._chat_header_actions.grid_remove()
                self._header_title.grid_configure(padx=(21, 10))
                self._chat_inbox.grid_remove()
        self._set_shell_width(self.root.winfo_width(), selected == str(self._tab_chat))

    def _resize_shell(self, event) -> None:
        if event.widget is not self.root or self._closing:
            return
        self._set_shell_width(event.width, self.notebook.select() == str(self._tab_chat))

    def _set_shell_width(self, width: int, chat: bool) -> None:
        compact = width < 1080
        self.sidebar.configure(width=(223 if compact else 254) if chat else 64)
        self.mode_lbl.configure(wraplength=126 if compact else 156)
        self._sync_review_art(width)

    def _sync_review_art(self, width: int) -> None:
        art = self._review_art_wide if width >= 1080 else self._review_art_narrow
        self.review_button.configure(image=art if self._review_has_art else "")

    def _use_prompt(self, text: str) -> None:
        if not self._closing:
            self.entry_var.set(text)
            self.chat_entry.focus_set()

    # ---------- 会话栏 ----------
    def _build_header(self, row: int) -> None:
        bar = tk.Frame(self.root, bg=PANEL)
        bar.grid(row=row, column=1, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        self._header_avatar = tk.Canvas(bar, width=36, height=36, bg=PANEL, highlightthickness=0)
        self._header_avatar.grid(row=0, column=0, padx=(20, 9), pady=10)
        draw_mark(self._header_avatar)
        title = self._header_title = tk.Frame(bar, bg=PANEL)
        title.grid(row=0, column=1, sticky="w", padx=(0, 10), pady=8)
        self.page_title = tk.StringVar(value="苏晚")
        tk.Label(title, textvariable=self.page_title, bg=PANEL, fg=FG,
                 font=(FONT, 13, "bold")).pack(anchor="w")
        self.subtitle = self._label(title, "本次会话", color=SUB, size=8)
        self.subtitle.pack(anchor="w", pady=(1, 0))
        self._chat_header_actions = tk.Frame(bar, bg=PANEL)
        self._chat_header_actions.grid(row=0, column=2, padx=(0, 16), sticky="e")
        ttk.Button(self._chat_header_actions, text="搜索", style="Quiet.TButton", width=5,
                   command=self._open_chat_find).pack(side=tk.LEFT)
        self.report_button = ttk.Button(self._chat_header_actions, text="整理资料", style="Quiet.TButton",
                                        width=7, command=self._open_reports)
        self.report_button.pack(side=tk.LEFT)
        self.today_works_button = ttk.Button(self._chat_header_actions, text="作品", style="Quiet.TButton",
                                             width=5, command=self._show_works)
        self.today_works_button.pack(side=tk.LEFT)
        tk.Frame(self._chat_header_actions, width=1, height=19, bg=LINE).pack(side=tk.LEFT, padx=8)
        ttk.Button(self._chat_header_actions, text="连接设置", width=7,
                   command=lambda: self._navigate("settings")).pack(side=tk.LEFT)
        tk.Frame(bar, height=1, bg=LINE).grid(row=1, column=0, columnspan=3, sticky="ew")

    def _open_reports(self) -> None:
        if self._closing:
            return
        if self.report_window is None:
            self.report_window = ReportWindow(self)
        else:
            self.report_window.deiconify()
            self.report_window.lift()
            self.report_window.focus_set()

    # ---------- row1 主区 ----------
    def _build_notebook(self, row: int) -> None:
        nb = ttk.Notebook(self.root, style="Warm.TNotebook", takefocus=False)
        self.notebook = nb
        nb.grid(row=row, column=1, sticky="nsew")
        tabs = {}
        for key, text in (("chat", "对话"), ("memory", "记忆"), ("status", "状态板"), ("settings", "设置"),
                          ("works", "作品集"), ("story", "人生故事"), ("log", "系统日志")):
            frame = tk.Frame(nb, bg=PANEL)
            nb.add(frame, text=text)
            tabs[key] = frame
        self._tab_chat, self._tab_status = tabs["chat"], tabs["status"]
        self._tab_settings = tabs["settings"]
        self._tab_memory = tabs["memory"]
        self._tab_works, self._tab_story, self._tab_log = tabs["works"], tabs["story"], tabs["log"]
        self._page_frames = tabs
        self._build_chat()
        self._build_memory()
        self._build_status()
        self._build_settings()
        self._build_works()
        self._build_story()
        self._build_log()
        nb.bind("<<NotebookTabChanged>>", self._sync_navigation)

    def _build_chat(self) -> None:
        self._tab_chat.configure(bg=CHAT_BG)
        self._tab_chat.grid_columnconfigure(0, weight=1)
        self._tab_chat.grid_rowconfigure(1, weight=1)
        self.today_summary = tk.StringVar(value="正在读取今日概览…")
        self._build_chat_find()
        conversation = tk.Frame(self._tab_chat, bg=CHAT_BG)
        conversation.grid(row=1, column=0, sticky="nsew")
        self.chat_area = ChatTranscript(conversation)
        self._chat_scrollbar = ttk.Scrollbar(conversation, orient=tk.VERTICAL, command=self.chat_area.yview)
        self._chat_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat_area.pack(fill=tk.BOTH, expand=True)
        self._has_new_chat = False
        self.chat_jump = ttk.Button(conversation, text="回到最新消息 ↓", style="Accent.TButton",
                                    command=self._jump_to_latest)
        self.chat_area.configure(yscrollcommand=self._on_chat_scroll)
        self.chat_empty_frame = tk.Frame(conversation, bg=CHAT_BG)
        self.chat_empty_frame.place(relx=.5, rely=.43, anchor="center")
        tk.Label(self.chat_empty_frame, image=self._welcome_art, bg=CHAT_BG,
                 borderwidth=0, highlightthickness=0).pack(pady=(0, 22))
        tk.Label(self.chat_empty_frame, text="和苏晚开始聊天", bg=CHAT_BG, fg=FG,
                 font=(FONT, 20, "bold")).pack()
        ttk.Button(self.chat_empty_frame, text="聊聊今天", style="Accent.TButton",
                   command=lambda: self._use_prompt("今天想和你聊聊。")).pack(pady=(12, 0))
        composer = ChatComposer(self._tab_chat, send=self.send, cancel=self._cancel_chat)
        composer.grid(row=2, column=0, sticky="ew")
        self.entry_var = tk.StringVar()
        self.chat_entry = composer.entry
        self.send_button = composer.send_button
        self.chat_wait_var = composer.status
        self.chat_cancel_button = composer.cancel_button
        self.chat_entry.bind("<Return>", self._send_from_keyboard)
        self.chat_entry.bind("<Shift-Return>", self._composer_newline)
        self.entry_var.trace_add("write", self._set_composer_text)
        self.chat_entry.focus_set()

    def _set_composer_text(self, *_args) -> None:
        self.chat_entry.delete("1.0", tk.END)
        self.chat_entry.insert("1.0", self.entry_var.get())

    def _send_from_keyboard(self, _event) -> str:
        self.send()
        return "break"

    def _composer_newline(self, _event) -> str:
        if self.chat_entry.tag_ranges(tk.SEL):
            self.chat_entry.delete("sel.first", "sel.last")
        self.chat_entry.insert(tk.INSERT, "\n")
        return "break"

    def _build_chat_find(self) -> None:
        row = self._chat_find_row = tk.Frame(self._tab_chat, bg=PANEL)
        row.grid(row=0, column=0, sticky="ew")
        row.grid_remove()
        self.chat_find_query = tk.StringVar()
        self.chat_find_status = tk.StringVar()
        self._label(row, "本次会话", color=SUB, size=8).pack(side=tk.LEFT, padx=(18, 0))
        self._chat_find_entry = ttk.Entry(row, textvariable=self.chat_find_query, width=26)
        self._chat_find_entry.pack(side=tk.LEFT, padx=(8, 6), pady=7)
        self._chat_find_entry.bind("<Return>", lambda _event: self._find_chat_message())
        self._chat_find_entry.bind("<Escape>", self._close_chat_find)
        self.chat_find_query.trace_add("write", self._reset_chat_find)
        for text, command in (("上一个", lambda: self._find_chat_message(backwards=True)),
                              ("下一个", self._find_chat_message), ("关闭查找", self._close_chat_find)):
            ttk.Button(row, text=text, style="Quiet.TButton", command=command).pack(side=tk.LEFT)
        tk.Label(row, textvariable=self.chat_find_status, bg=PANEL, fg=SUB,
                 font=(FONT, 8)).pack(side=tk.LEFT, padx=6)

    def _open_chat_find(self, _event=None) -> str | None:
        if self._closing or self.notebook.select() != str(self._tab_chat):
            return None
        self._chat_find_row.grid()
        self._chat_find_entry.focus_set()
        self._chat_find_entry.selection_range(0, tk.END)
        return "break"

    def _close_chat_find(self, _event=None) -> str:
        self.chat_find_query.set("")
        self._chat_find_row.grid_remove()
        self.chat_entry.focus_set()
        return "break"

    def _reset_chat_find(self, *_args) -> None:
        self.chat_area.clear_match()
        self.chat_find_status.set("")

    def _find_chat_message(self, *, backwards: bool = False) -> str:
        query = self.chat_find_query.get().strip()
        if not query:
            return "break"
        result = self.chat_area.find_message(query, backwards=backwards)
        self.chat_find_status.set(f"{result[0]} / {result[1]}" if result else "未找到")
        return "break"

    def _build_status(self) -> None:
        canvas = tk.Frame(self._tab_status, bg=PANEL)
        canvas.pack(fill=tk.BOTH, expand=True, padx=16, pady=6)
        canvas.grid_columnconfigure(0, weight=1)
        canvas.grid_rowconfigure(0, weight=1)
        self.status_text = tk.StringVar(value="（读取中…）")
        self.status_area = self._scrolled(canvas)
        self.status_area.configure(height=1, state="disabled", font=(FONT, 10), bg=FIELD)
        self.status_area.grid(row=0, column=0, sticky="nsew")
        self.personality_text = tk.StringVar(value="正在整理苏晚的近况…")
        personality_area = self._scrolled(canvas, fg=ACCENT)
        personality_area.configure(height=2, font=(FONT, 8), padx=0, pady=2, spacing1=0, spacing3=0)
        personality_area.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        def update_personality(*_args):
            personality_area.configure(state="normal")
            personality_area.delete("1.0", tk.END)
            personality_area.insert(tk.END, self.personality_text.get())
            personality_area.configure(state="disabled")
        self.personality_text.trace_add("write", update_personality)
        update_personality()
        self._label(canvas, "关系变化（来源可在“记忆”页按 ID 查看）", color=SUB, bg=PANEL).grid(
            row=2, column=0, sticky="w")
        self.relation_details = self._scrolled(canvas)
        self.relation_details.configure(height=3, pady=6, spacing1=0, spacing3=0, state="disabled")
        self.relation_details.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self._build_activity_surface(canvas)

    def _build_activity_surface(self, parent) -> None:
        frame = tk.LabelFrame(parent, text="活动与调用预算", bg=PANEL, fg=ACCENT,
                              font=("Microsoft YaHei UI", 9))
        frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.activity_budget = tk.StringVar(value="正在读取调用账目…")
        budget_label = tk.Label(frame, textvariable=self.activity_budget, justify="left", anchor="w", wraplength=680,
                                font=(FONT, 8), bg=PANEL, fg=SUB)
        budget_label.pack(fill=tk.X, padx=6, pady=(3, 2))
        frame.bind("<Configure>", lambda event: budget_label.configure(wraplength=max(240, event.width - 16)))
        self.activity_tree = ttk.Treeview(frame, columns=("action", "status", "phase"), show="headings", height=2)
        for key, label, width in (("action", "活动", 160), ("status", "状态", 100), ("phase", "阶段", 230)):
            self.activity_tree.heading(key, text=label)
            self.activity_tree.column(key, width=width, stretch=True)
        self.activity_tree.pack(fill=tk.X, padx=6)
        self.activity_tree.bind("<<TreeviewSelect>>", self._activity_selected)
        self.activity_detail = tk.StringVar(value="选择活动可查看原因与稳定 ID。")
        self.activity_detail_area = self._scrolled(frame)
        self.activity_detail_area.configure(height=2, font=(FONT, 8), pady=2, spacing1=0, spacing3=0, state="disabled")
        self.activity_detail_area.pack(fill=tk.X, padx=6, pady=3)
        self.activity_detail.trace_add("write", self._update_activity_detail)
        self._update_activity_detail()
        row = tk.Frame(frame, bg=PANEL)
        row.pack(fill=tk.X, padx=6, pady=(0, 5))
        self.activity_pause_button = ttk.Button(row, text="暂停活动", command=lambda: self._request_activities("pause"))
        self.activity_resume_button = ttk.Button(row, text="恢复活动", command=lambda: self._request_activities("resume"))
        self.activity_continue_button = ttk.Button(row, text="继续所选", command=lambda: self._activity_action("continue"))
        self.activity_like_button = ttk.Button(row, text="喜欢话题", command=lambda: self._activity_action("like"))
        self.activity_dislike_button = ttk.Button(row, text="少聊话题", command=lambda: self._activity_action("dislike"))
        self.activity_discard_button = ttk.Button(row, text="取消未完成", command=lambda: self._activity_action("discard"))
        self.learning_view_button = ttk.Button(row, text="查看学习", command=self._request_learning)
        controls = (self.activity_pause_button, self.activity_resume_button, self.activity_continue_button,
                    self.activity_discard_button, self.activity_like_button, self.activity_dislike_button,
                    self.learning_view_button)
        ttk.Style(self.root).configure("Activity.TButton", padding=(8, 4))
        for column in range(4):
            row.grid_columnconfigure(column, weight=1, uniform="activity_actions")
        for index, control in enumerate(controls):
            control.configure(style="Activity.TButton")
            control.grid(row=index // 4, column=index % 4, sticky="ew",
                         padx=(0, 4) if index % 4 < 3 else 0, pady=(0, 4) if index < 4 else 0)
        self._activity_controls = [self.activity_pause_button, self.activity_resume_button, self.activity_continue_button,
                                   self.activity_like_button, self.activity_dislike_button, self.activity_discard_button]

    def _update_activity_detail(self, *_args) -> None:
        self.activity_detail_area.configure(state="normal")
        self.activity_detail_area.delete("1.0", tk.END)
        self.activity_detail_area.insert(tk.END, self.activity_detail.get())
        self.activity_detail_area.configure(state="disabled")

    def _request_learning(self, command: str | None = None) -> None:
        if self._closing:
            return
        self._flush_desktop_settings()
        self._learning_generation += 1
        generation = self._learning_generation
        epoch = self.server.context_epoch()
        try:
            future = self.server.learning_command(command or "/learn list")
        except Exception as exc:
            self._log_msg(f"学习操作失败：{type(exc).__name__}", "error")
            return
        def done(fut):
            try:
                result = fut.result()
            except Exception as exc:
                result = {"ok": False, "text": f"学习操作失败：{type(exc).__name__}", "_context_epoch": epoch}
            self._queue_ui(self._render_learning, result, generation)
        future.add_done_callback(done)

    def _render_learning(self, result: dict, generation: int) -> None:
        if self._closing or generation != self._learning_generation:
            return
        epoch = result.get("_context_epoch")
        if epoch is None or not self.server.context_current(epoch):
            return
        self._sync_ui_context()
        text = str(result.get("text", "学习操作未完成。"))
        self._show_chat("苏晚", text)

    def _activity_selected(self, _event=None) -> None:
        selected = self.activity_tree.selection()
        row = self._activity_rows.get(selected[0]) if selected else None
        if row is None:
            self.activity_detail.set("选择活动可查看原因与稳定 ID。")
            return
        reason = str(row.get("reason", "暂无原因"))[:140]
        calendar_entry = row.get("action") in {"diary", "story"}
        self.activity_detail.set(f"{reason}\nID：{row.get('id', '')}" +
                                 (f" · {str(row['error'])[:80]}" if row.get("error") else "") +
                                 ("\n请求可能已经发送，不自动重发。" +
                                  ("当日日历记录保留占位，此处仅查看。" if calendar_entry else
                                   "可取消解除活动等待；已占用调用额度不会返还。")
                                  if row.get("status") == "unknown" else ""))

    def _activity_action(self, action: str) -> None:
        selected = self.activity_tree.selection()
        if not selected:
            self.activity_detail.set("请先选择一项活动。")
            return
        row = self._activity_rows.get(selected[0], {})
        if row.get("action") in {"diary", "story"}:
            self.activity_detail.set("日历记录在此处仅供查看；当天已占位，不自动补发。")
            return
        if action == "discard" and row.get("status") not in {"active", "paused", "failed", "unknown"}:
            self.activity_detail.set("只能取消未完成活动，已完成成果保留。")
            return
        self._request_activities(action, selected[0])

    def _request_activities(self, action: str | None = None, activity_id: str | None = None) -> None:
        if action is not None:
            self._flush_desktop_settings()
        if self._closing or not hasattr(self, "activity_tree"):
            return
        if self._activity_pending and (action is None or self._activity_mutating):
            return
        self._activity_generation += 1
        generation = self._activity_generation
        self._activity_pending = True
        self._activity_mutating = action is not None
        if action is not None:
            for control in self._activity_controls:
                control.configure(state="disabled")
        try:
            future = (self.server.activity_command(action, activity_id) if action is not None else self.server.activity_view())
        except (RuntimeError, AttributeError) as exc:
            self._activity_pending = self._activity_mutating = False
            self.activity_detail.set(f"活动操作失败：{type(exc).__name__}")
            for control in self._activity_controls:
                control.configure(state="normal")
            return

        def done(fut):
            try:
                result = fut.result()
            except Exception as exc:
                result = {"ok": False, "text": f"活动操作失败：{type(exc).__name__}"}
            self._queue_ui(self._render_activities, result, generation, action is not None)
        future.add_done_callback(done)

    def _render_activities(self, result: dict, generation: int, changed: bool = False) -> None:
        if self._closing or generation != self._activity_generation:
            return
        self._activity_pending = self._activity_mutating = False
        for control in self._activity_controls:
            control.configure(state="normal")
        epoch = result.get("_context_epoch")
        if epoch is not None and not self.server.context_current(epoch):
            self._request_activities()
            return
        if epoch is None or "activities" not in result:
            self.activity_detail.set(result.get("text", "活动视图缺少来源版本，已忽略。"))
            if changed:
                self._request_activities()
            return
        selected = self.activity_tree.selection()
        rows = result.get("activities", [])
        self._activity_rows = {str(row["id"]): dict(row) for row in rows if isinstance(row, dict) and row.get("id")}
        self.activity_tree.delete(*self.activity_tree.get_children())
        labels = {"learning": "学习", "creation": "创作", "social": "交流", "reminisce": "回忆", "idle": "休息", "diary": "日记", "story": "生活记录"}
        statuses = {"active": "进行中", "paused": "已暂停", "unknown": "结果未知", "failed": "失败", "completed": "已完成", "cancelled": "已取消",
                    "reserved": "已预留", "in_flight": "请求在途", "running": "整理中", "legacy_completed": "旧版已完成"}
        phases = {"intention": "形成意图", "topic": "确定主题", "draft": "草稿已保存", "saved": "成果已保存", "feedback": "记录完成反馈", "done": "结束",
                  "model": "模型整理", "local_fallback": "本地整理", "legacy": "旧版记录"}
        for activity_id, row in self._activity_rows.items():
            action = row.get("action", row.get("kind", "活动"))
            status, phase = row.get("status", ""), row.get("phase", "")
            self.activity_tree.insert("", "end", iid=activity_id, values=(labels.get(action, action), statuses.get(status, status), phases.get(phase, phase)))
        if selected and selected[0] in self._activity_rows:
            self.activity_tree.selection_set(selected[0])
        self._activity_selected()
        if result.get("text"):
            self.activity_detail.set(str(result["text"])[:200])
        self.activity_budget.set(self._budget_description(result.get("budget", {}), bool(result.get("paused")), result.get("proactive", {})))

    @staticmethod
    def _budget_description(budget: dict, paused: bool, proactive: dict | None = None) -> str:
        def count(value):
            return "未知" if value is None else str(value)
        usage = budget.get("known_actual_usage", {})
        description = (f"后台：{'暂停' if paused else '可运行'} · {budget.get('day', '')} · 调用分配 {count(budget.get('used_calls'))}/{count(budget.get('daily_calls'))} · "
                       f"完成 {count(budget.get('completed_calls'))} / 预留 {count(budget.get('reserved_calls'))} / 在途 {count(budget.get('in_flight_calls'))} / 结果未知 {count(budget.get('today_unknown_calls'))}\n"
                       f"文本输出分配 {count(budget.get('allocated_output_tokens'))}/{count(budget.get('daily_output_tokens'))}（预留 {count(budget.get('reserved_output_tokens'))}）；"
                       f"已知实际输出 {count(usage.get('completion_tokens'))}，用量未知 {count(budget.get('actual_output_unknown_calls'))} 次；非总 token / 费用预算。")
        if budget.get("ledger_error"):
            description += "\n账本写保护：新调用已阻止。"
        if proactive:
            description += (f"\n主动消息已认领 {count(proactive.get('claimed'))}/{count(proactive.get('limit'))}；"
                            f"安静时段 {count(proactive.get('quiet_start'))}:00—{count(proactive.get('quiet_end'))}:00。")
        return description

    def _clear_activity_view(self) -> None:
        self._activity_generation += 1
        self._activity_pending = self._activity_mutating = False
        self._activity_rows.clear()
        self.activity_tree.delete(*self.activity_tree.get_children())
        self.activity_detail.set("来源已变更，正在刷新活动。")
        self.activity_budget.set("正在重新读取调用账目…")
        self._request_activities()

    def _build_memory(self) -> None:
        head = tk.Frame(self._tab_memory, bg=PANEL)
        head.pack(fill=tk.X, padx=10, pady=10)
        self.memory_query = tk.StringVar()
        entry = tk.Entry(head, textvariable=self.memory_query, bg=FIELD, fg=FG, insertbackground=FG)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)
        entry.bind("<Return>", lambda _event: self._memory_search())
        ttk.Button(head, text="搜索", command=self._memory_search).pack(side=tk.LEFT, padx=4)
        ttk.Button(head, text="列表 / 刷新", command=self._memory_list).pack(side=tk.LEFT)
        self.memory_state = tk.StringVar(value="正在读取记忆…")
        navigation = tk.Frame(self._tab_memory, bg=PANEL)
        navigation.pack(fill=tk.X, padx=10)
        self._memory_kinds = {"全部类型": None, "事实": "fact", "经历": "event", "知识": "knowledge", "生成内容": "generated", "推测": "inference", "历史记录": "legacy"}
        self.memory_kind = tk.StringVar(value="全部类型")
        kinds = ttk.Combobox(navigation, textvariable=self.memory_kind, values=list(self._memory_kinds), state="readonly", width=13)
        kinds.pack(side=tk.LEFT)
        kinds.bind("<<ComboboxSelected>>", lambda _event: self._memory_list())
        self.memory_previous = ttk.Button(navigation, text="上一页", command=lambda: self._memory_turn(-1))
        self.memory_previous.pack(side=tk.LEFT, padx=(8, 4))
        self.memory_next = ttk.Button(navigation, text="下一页", command=lambda: self._memory_turn(1))
        self.memory_next.pack(side=tk.LEFT)
        self.memory_page_label = tk.StringVar(value="第 1 / 1 页")
        tk.Label(navigation, textvariable=self.memory_page_label, bg=PANEL, fg=SUB).pack(side=tk.LEFT, padx=8)
        ttk.Button(navigation, text="按 ID 定位", command=self._memory_locate).pack(side=tk.RIGHT)
        self._label(self._tab_memory, "每页100条 · 可以搜索、修正，也可以让苏晚忘记。", color=SUB).pack(anchor="w", padx=12)
        self.memory_tree = ttk.Treeview(self._tab_memory, columns=("kind", "source", "time", "content"), show="headings", height=10)
        for key, text, width in (("kind", "类型", 75), ("source", "来源", 90), ("time", "记录时间", 155), ("content", "内容", 560)):
            self.memory_tree.heading(key, text=text)
            self.memory_tree.column(key, width=width, minwidth=55, stretch=key == "content")
        self.memory_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
        self.memory_tree.bind("<<TreeviewSelect>>", self._memory_selected)
        self.memory_detail = self._scrolled(self._tab_memory)
        self.memory_detail.configure(height=6, state="disabled")
        self.memory_detail.pack(fill=tk.X, padx=10, pady=(0, 6))
        row = tk.Frame(self._tab_memory, bg=PANEL)
        row.pack(fill=tk.X, padx=10, pady=4)
        for label, callback in (("补充经历", self._memory_add), ("设置事实", self._memory_set), ("纠正记录", self._memory_correct), ("遗忘及派生", self._memory_forget)):
            ttk.Button(row, text=label, command=callback).pack(side=tk.LEFT, padx=(0, 6))
        self.memory_pause = ttk.Button(row, text="暂停记录", command=self._memory_toggle)
        self.memory_pause.pack(side=tk.RIGHT)
        tk.Label(self._tab_memory, textvariable=self.memory_state, bg=PANEL, fg=ACCENT, anchor="w", wraplength=1050).pack(fill=tk.X, padx=12, pady=4)
        self._label(self._tab_memory, "遗忘清理活动记忆和会话缓存；备份、分享归档及已导出作品仍保留。暂停期间显式管理仍可用。", color=SUB, size=8).pack(anchor="w", padx=12, pady=(0, 10))

    def _memory_search(self) -> None:
        query = self.memory_query.get().strip()
        if not query:
            self._memory_list()
            return
        self._memory_mode, self._memory_search_text, self._memory_page = "find", query, 1
        self._memory_load_page()

    def _memory_list(self) -> None:
        self._memory_mode, self._memory_page = "list", 1
        self._memory_load_page()

    def _memory_turn(self, delta: int) -> None:
        page = self._memory_page + delta
        if 1 <= page <= self._memory_pages:
            self._memory_page = page
            self._memory_load_page()

    def _memory_load_page(self) -> None:
        if self._memory_mode == "find":
            command = f"/memory find --all --page {self._memory_page} {self._memory_search_text}"
        else:
            command = f"/memory list {self._memory_page}"
            kind = self._memory_kinds.get(self.memory_kind.get())
            if kind:
                command += f" --kind {kind}"
        self._memory_request(command)

    def _memory_locate(self) -> None:
        item_id = simpledialog.askstring("按 ID 定位", "输入完整记忆 ID：", parent=self.root)
        if item_id and item_id.strip():
            self._memory_request("/memory show " + item_id.strip())

    def _memory_request(self, command: str) -> None:
        if self._closing:
            return
        self._memory_generation += 1
        generation = self._memory_generation
        try:
            future = self.server.memory_command(command)
        except RuntimeError as exc:
            self.memory_state.set(str(exc))
            return

        def done(fut):
            try:
                result = fut.result()
            except Exception as exc:
                result = {"ok": False, "text": f"记忆操作失败：{type(exc).__name__}"}
            self._queue_ui(self._render_memory, result, generation)
        future.add_done_callback(done)

    def _render_memory(self, result: dict, generation: int) -> None:
        if self._closing or generation != self._memory_generation:
            return
        if "_context_epoch" in result and not self.server.context_current(result["_context_epoch"]):
            return
        self._memory_recording = result.get("recording_enabled", True)
        self.memory_pause.configure(text="暂停记录" if self._memory_recording else "恢复记录")
        if "items" in result:
            self._memory_page = result.get("page", 1)
            self._memory_pages = max(1, result.get("pages", 1))
            self.memory_page_label.set(f"第 {self._memory_page} / {self._memory_pages} 页 · 共 {result.get('total', len(result['items']))} 条")
            self.memory_previous.configure(state="normal" if self._memory_page > 1 else "disabled")
            self.memory_next.configure(state="normal" if self._memory_page < self._memory_pages else "disabled")
            self._memory_rows = {row["id"]: row for row in result["items"]}
            self.memory_tree.delete(*self.memory_tree.get_children())
            for item_id, row in self._memory_rows.items():
                self.memory_tree.insert("", "end", iid=item_id, values=(row["kind"], row["source"], (row["created"] or "未知")[:19], row["content"][:200]))
            self.memory_state.set(f"自动长期记录：{'开启' if self._memory_recording else '暂停'} · 显示 {len(self._memory_rows)} 条")
            self.memory_detail.configure(state="normal")
            self.memory_detail.delete("1.0", tk.END)
            self.memory_detail.configure(state="disabled")
            if result.get("action") == "show" and result["items"]:
                self.memory_tree.selection_set(result["items"][0]["id"])
                self._memory_selected()
        else:
            self.memory_state.set(result.get("text", ""))
            if result.get("ok") and result.get("action") not in {"status", "help"}:
                self._last_reply = ""
                self._last_reply_epoch = None
                self._stop_speaking()
                self._memory_list()

    def _memory_selected(self, _event=None) -> None:
        selection = self.memory_tree.selection()
        if not selection:
            return
        row = self._memory_rows.get(selection[0])
        if not row:
            return
        detail = f"ID: {row['id']}\n类型/来源: {row['kind']} / {row['source']}\n来源引用: {row['source_ref'] or '无'}\n事实键: {row['fact_key'] or '无'}\n派生来源: {', '.join(row['derived_from']) or '无'}\n\n{row['content']}"
        self.memory_detail.configure(state="normal")
        self.memory_detail.delete("1.0", tk.END)
        self.memory_detail.insert(tk.END, detail)
        self.memory_detail.configure(state="disabled")

    def _memory_add(self) -> None:
        content = simpledialog.askstring("补充经历", "输入要保存的经历：", parent=self.root)
        if content and content.strip():
            self._memory_request("/memory add " + content.strip())

    def _memory_set(self) -> None:
        text = simpledialog.askstring("设置事实", "输入 键=值，例如 user.name=小明 或 user.city=苏州：", parent=self.root)
        if text:
            self._memory_request("/memory set " + text)

    def _memory_correct(self) -> None:
        selection = self.memory_tree.selection()
        if not selection:
            self.memory_state.set("请先选中一条记忆。")
            return
        row = self._memory_rows[selection[0]]
        text = simpledialog.askstring("纠正记录", "输入新的事实值：" if row["fact_key"] else "输入纠正后的完整内容：", initialvalue=row["fact_value"] or row["content"], parent=self.root)
        if text and text.strip():
            self._memory_request(f"/memory correct {selection[0]} {text.strip()}")

    def _memory_forget(self) -> None:
        selection = self.memory_tree.selection()
        if selection:
            self._memory_request("/memory forget " + selection[0])
        else:
            self.memory_state.set("请先选中一条记忆；遗忘将同时移除关联修订和派生记忆。")

    def _memory_toggle(self) -> None:
        self._memory_request("/memory pause" if getattr(self, "_memory_recording", True) else "/memory resume")

    # ---------- 设置页（控制面板搬家） ----------
    def _build_settings(self) -> None:
        self.settings_canvas = tk.Canvas(self._tab_settings, bg=BG, highlightthickness=0)
        self.settings_scrollbar = ttk.Scrollbar(self._tab_settings, orient="vertical", command=self.settings_canvas.yview)
        self.settings_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.settings_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.settings_canvas.configure(yscrollcommand=self.settings_scrollbar.set)
        body = tk.Frame(self.settings_canvas, bg=BG)
        window = self.settings_canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _event: self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all")))
        self.settings_canvas.bind("<Configure>", lambda event: self.settings_canvas.itemconfigure(window, width=event.width))
        self.root.bind("<MouseWheel>", self._settings_mousewheel, add="+")

        def card(title: str) -> tuple[tk.Frame, tk.Frame]:
            panel = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
            panel.pack(fill=tk.X, padx=18, pady=(0, 10))
            heading = tk.Frame(panel, bg=PANEL)
            heading.pack(fill=tk.X, padx=16, pady=(10, 0))
            tk.Label(heading, text=title, bg=PANEL, fg=FG, font=(FONT, 11, "bold")).pack(side=tk.LEFT)
            content = tk.Frame(panel, bg=PANEL)
            content.pack(fill=tk.X, padx=16, pady=(8, 12))
            return heading, content

        def wrap(label: tk.Label) -> tk.Label:
            def resize(event) -> None:
                width = max(100, event.width - 16)
                if width != int(label.cget("wraplength")):
                    label.configure(wraplength=width)

            label.bind("<Configure>", resize)
            return label

        tk.Frame(body, height=12, bg=BG).pack(fill=tk.X)
        model_header, model_content = card("模型连接")
        self.api_var = tk.BooleanVar(value=False)
        self.api_test_button = ttk.Button(model_header, text="测试连接", style="Quiet.TButton", command=self._test_api)
        self.api_test_button.pack(side=tk.RIGHT)
        self.api_checkbox = ttk.Checkbutton(model_header, text="启用模型 API", variable=self.api_var, command=self._on_api)
        self.api_checkbox.pack(side=tk.RIGHT, padx=(0, 10))

        fields_row = tk.Frame(model_content, bg=PANEL)
        fields_row.pack(fill=tk.X)
        fields_row.grid_columnconfigure(0, weight=1)
        fields_row.grid_columnconfigure(1, weight=2)
        self._label(fields_row, "供应商", color=SUB, size=8).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self._label(fields_row, "模型", color=SUB, size=8).grid(row=0, column=1, sticky="w", pady=(0, 4))
        provider_labels = [PROVIDER_LABELS[p] for p in ENDPOINT_ENV]
        self.provider_var = tk.StringVar()
        self.provider_cb = ttk.Combobox(fields_row, textvariable=self.provider_var, values=provider_labels,
                                        state="readonly", width=14)
        self.provider_cb.grid(row=1, column=0, sticky="ew", padx=(0, 10))
        self.provider_cb.bind("<<ComboboxSelected>>", self._on_provider_selected)
        self.model_var = tk.StringVar()
        self.model_cb = ttk.Combobox(fields_row, textvariable=self.model_var, state="normal", width=18)
        self.model_cb.grid(row=1, column=1, sticky="ew", padx=(0, 10))
        self.model_cb.bind("<<ComboboxSelected>>", self._on_model_selected)
        self.model_cb.bind("<Return>", self._on_model_selected)
        ttk.Button(fields_row, text="应用模型", command=self._on_model_selected).grid(row=1, column=2, sticky="ns")
        wrap(tk.Label(model_content, textvariable=self.api_status_text, bg=FIELD, fg=FG,
                      font=(FONT, 9), anchor="w", justify="left", padx=8, pady=7)).pack(fill=tk.X, pady=(10, 0))

        voice_header, voice_content = card("声音")
        self.read_var = tk.BooleanVar(value=False)
        read_group = self._add_dot_toggle(voice_header, self.read_var, "自动朗读", ACCENT, self._on_auto_read)
        read_group.pack(side=tk.RIGHT)
        voice_row = tk.Frame(voice_content, bg=PANEL)
        voice_row.pack(fill=tk.X)
        self._label(voice_row, "音色", color=SUB).pack(side=tk.LEFT)
        self.voice_var = tk.StringVar(value=VOICE_PRESETS[0])
        voice_cb = ttk.Combobox(voice_row, textvariable=self.voice_var, values=list(VOICE_PRESETS),
                               state="readonly", width=14)
        voice_cb.pack(side=tk.LEFT, padx=(8, 10))
        voice_cb.bind("<<ComboboxSelected>>", self._on_voice_selected)
        ttk.Button(voice_row, text="朗读上句", command=self._speak_last).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(voice_row, text="停止朗读", command=self._stop_speaking).pack(side=tk.LEFT)
        self._label(voice_content, "语音需要 MiMo 通用 API 密钥", color=SUB, size=8).pack(anchor="w", pady=(6, 0))

        preferences_header, preferences = card("交流偏好")
        self.net_var = tk.BooleanVar(value=False)
        net_group = self._add_dot_toggle(preferences_header, self.net_var, "网页搜索", ACCENT, self._on_net)
        net_group.pack(side=tk.RIGHT)
        row = tk.Frame(preferences, bg=PANEL)
        row.pack(fill=tk.X)
        self._response_styles = {"自然": "normal", "直接": "brief", "详细": "detailed"}
        self._initiative_levels = {"正常": "normal", "少": "low", "停": "off"}
        self.response_style_var = tk.StringVar(value="自然")
        self.initiative_var = tk.StringVar(value="正常")
        self._label(row, "回答风格", color=SUB, bg=PANEL).pack(side=tk.LEFT)
        self.response_style_cb = ttk.Combobox(row, textvariable=self.response_style_var,
                                             values=list(self._response_styles), state="readonly", width=12)
        self.response_style_cb.pack(side=tk.LEFT, padx=(8, 24))
        self.response_style_cb.bind("<<ComboboxSelected>>", lambda _event: self._set_interaction_preference("preference.response_style"))
        self._label(row, "主动消息", color=SUB, bg=PANEL).pack(side=tk.LEFT)
        self.initiative_cb = ttk.Combobox(row, textvariable=self.initiative_var,
                                        values=list(self._initiative_levels), state="readonly", width=12)
        self.initiative_cb.pack(side=tk.LEFT, padx=8)
        self._initiative_controls = (self.response_style_cb, self.initiative_cb)
        self.initiative_cb.bind("<<ComboboxSelected>>", lambda _event: self._set_interaction_preference("preference.initiative"))
        self.interaction_state = tk.StringVar(value="正在读取交流偏好…")
        wrap(tk.Label(preferences, textvariable=self.interaction_state, justify="left", anchor="w",
                      bg=PANEL, fg=SUB, font=(FONT, 8))).pack(fill=tk.X, pady=(7, 0))

        _advanced_header, advanced = card("安静时段、限额与音量")
        # Direct attributes own Tcl variables.  The lookup must not keep an
        # extra reference after normal owner-thread variable cleanup.
        self.desktop_setting_vars = weakref.WeakValueDictionary()
        fields = (("quiet_start", "安静开始（0–23时）", "22"), ("quiet_end", "安静结束（0–23时）", "8"),
                  ("proactive_limit", "每日主动消息上限", "8"), ("volume", "朗读音量（0–100）", "100"),
                  ("daily_calls", "每日调用次数上限", "40"), ("daily_output_tokens", "每日分配输出 token 上限", "32768"),
                  ("foreground_reserved_calls", "前台保留调用次数", "4"),
                  ("foreground_reserved_output_tokens", "前台保留输出 token", "4096"))
        for index, (key, label, default) in enumerate(fields):
            r, c = divmod(index, 2)
            self._label(advanced, label, color=SUB, bg=PANEL).grid(row=r, column=c * 2, sticky="w", pady=3, padx=(0, 8))
            value = tk.StringVar(value=default)
            setattr(self, "_desktop_" + key + "_var", value)
            self.desktop_setting_vars[key] = value
            self._desktop_draft_revisions[key] = 0
            value.trace_add("write", lambda *_args, field=key: self._desktop_draft_edited(field))
            ttk.Entry(advanced, textvariable=value, width=7).grid(
                row=r, column=c * 2 + 1, sticky="ew", padx=(0, 14 if c == 0 else 0), pady=3)
        self.volume_var = self.desktop_setting_vars["volume"]
        for column in (1, 3):
            advanced.grid_columnconfigure(column, weight=1)
        wrap(tk.Label(advanced, text="安静时段的起止相同时，不限制主动消息。音量在下次播放时生效。",
                      bg=PANEL, fg=SUB, font=(FONT, 8), anchor="w", justify="left")).grid(
                          row=4, column=0, columnspan=4, sticky="ew", pady=(6, 3))
        self.desktop_save_button = ttk.Button(advanced, text="保存时段与限额", command=self._save_desktop_controls)
        self.desktop_save_button.grid(row=5, column=0, sticky="w", pady=3)
        self.desktop_settings_state = tk.StringVar(value="正在读取桌面设置…")
        wrap(tk.Label(advanced, textvariable=self.desktop_settings_state, bg=PANEL, fg=ACCENT,
                      font=(FONT, 8), anchor="w", justify="left")).grid(
                          row=5, column=1, columnspan=3, sticky="ew", padx=(8, 0))
        self.desktop_committed_state = tk.StringVar(value="当前生效设置正在读取；输入框中的修改需保存后生效。")
        wrap(tk.Label(advanced, textvariable=self.desktop_committed_state, bg=PANEL, fg=SUB,
                      font=(FONT, 8), anchor="w", justify="left")).grid(
                          row=6, column=0, columnspan=4, sticky="ew", pady=(3, 0))

        _data_header, data = card("本地数据")
        self.data_info = tk.StringVar(value="正在读取本地数据位置…")
        wrap(tk.Label(data, textvariable=self.data_info, bg=PANEL, fg=FG, font=(FONT, 8),
                      justify="left", anchor="w")).pack(fill=tk.X)
        self.retention_info = tk.StringVar(value="正在读取保留范围说明…")
        wrap(tk.Label(data, textvariable=self.retention_info, bg=PANEL, fg=SUB, font=(FONT, 8),
                      justify="left", anchor="w")).pack(fill=tk.X, pady=(6, 3))
        self.export_button = ttk.Button(data, text="导出数据快照（JSON）", command=self._export_data)
        self.export_button.pack(anchor="w", pady=(6, 3))
        self._label(data, "完整恢复请使用备份。", color=SUB, size=8).pack(anchor="w")
        self.export_state = tk.StringVar(value="尚未导出。")
        wrap(tk.Label(data, textvariable=self.export_state, bg=PANEL, fg=ACCENT,
                      justify="left", anchor="w", font=(FONT, 8))).pack(fill=tk.X, pady=(4, 0))

    def _settings_mousewheel(self, event):
        if str(event.widget).startswith(str(self._tab_settings)):
            self.settings_canvas.yview_scroll(-int(event.delta / 120), "units")
            return "break"
        return None

    def _save_desktop_controls(self) -> None:
        try:
            patch = {key: int(value.get().strip()) for key, value in self.desktop_setting_vars.items()}
        except ValueError:
            self.desktop_settings_state.set("这些选项须填写整数；开始和结束是 0–23 时，音量是 0–100。")
            return
        self._stop_speaking()
        self._save_desktop_settings(patch)

    def _desktop_draft_edited(self, key: str) -> None:
        if self._desktop_applying_settings:
            return
        self._desktop_draft_revisions[key] += 1
        self._desktop_dirty_fields.add(key)
        self._refresh_desktop_committed_state()

    def _refresh_desktop_committed_state(self) -> None:
        if not hasattr(self, "desktop_committed_state") or not self._desktop_committed_settings:
            return
        values = self._desktop_committed_settings
        self.desktop_committed_state.set(
            f"当前生效模型：{values['provider']} / {values['model']}\n"
            f"安静 {values['quiet_start']}–{values['quiet_end']} 时 · 主动上限 {values['proactive_limit']} · 音量 {values['volume']}\n"
            f"每日调用 {values['daily_calls']} · 输出 token {values['daily_output_tokens']} · 前台保留 {values['foreground_reserved_calls']} 次/{values['foreground_reserved_output_tokens']} token"
            + ("（输入框有未保存草稿）" if self._desktop_dirty_fields else ""))

    def _save_desktop_settings(self, patch: dict) -> None:
        if self._closing:
            return
        self._settings_generation += 1
        self._settings_pending = True
        self.desktop_settings_state.set("正在保存桌面设置…")
        if set(patch) & {"provider", "model", "api_enabled"}:
            self._api_probe_generation += 1
            self._api_probe_indicator = None
            # Revoke the old display at the user's choice boundary, before
            # the asynchronous settings writer changes runtime configuration.
            self._refresh_api_indicator()
        # Coalesce edits made in one Tk turn.  Apart from avoiding transient
        # on/off states, this keeps saving asynchronous even on a busy disk.
        self._pending_settings_patch.update(patch)
        # Capture when the user clicks save, not when after_idle later submits
        # the batch: the user can already be editing the next draft by then.
        revision_keys = set(patch) | ({"model"} if "provider" in patch else set())
        self._pending_settings_revisions.update({key: self._desktop_draft_revisions[key]
                                                for key in revision_keys if key in self._desktop_draft_revisions})
        if self._settings_flush_id is None:
            self._settings_flush_id = self.root.after_idle(self._flush_desktop_settings)
            self._after_ids.add(self._settings_flush_id)

    def _flush_desktop_settings(self) -> None:
        if not hasattr(self, "_pending_settings_patch"):
            return
        if self._settings_flush_id is not None:
            self.root.after_cancel(self._settings_flush_id)
            self._after_ids.discard(self._settings_flush_id)
            self._settings_flush_id = None
        if self._closing or not self._pending_settings_patch:
            return
        patch, self._pending_settings_patch = self._pending_settings_patch, {}
        revisions, self._pending_settings_revisions = self._pending_settings_revisions, {}
        generation = self._settings_generation
        epoch = self.server.context_epoch()
        try:
            future = self.server.update_desktop_settings(patch)
            self._settings_future = future
        except Exception as exc:
            self._settings_pending = False
            self.desktop_settings_state.set(f"桌面设置未保存：{type(exc).__name__}")
            return
        def done(fut):
            try:
                result = fut.result()
            except Exception as exc:
                result = {"ok": False, "text": f"桌面设置未保存：{type(exc).__name__}", "_context_epoch": epoch}
            self._queue_ui(self._render_desktop_settings, result, generation, patch, revisions)
        future.add_done_callback(done)

    def _apply_desktop_settings(self, settings: dict, committed_revisions: dict | None = None) -> None:
        self._desktop_settings_loaded = True
        provider = str(settings.get("provider", "mimo"))
        model_submitted_current = (committed_revisions is not None and "model" in committed_revisions
                                   and committed_revisions["model"] == self._desktop_draft_revisions["model"])
        model_dirty = "model" in self._desktop_dirty_fields and not model_submitted_current
        # A newer model draft still belongs to the provider the user selected,
        # including when the earlier provider save failed and runtime stayed put.
        draft_provider = self._current_provider() if model_dirty else None
        display_provider = draft_provider or provider
        self.provider_var.set(PROVIDER_LABELS.get(display_provider, display_provider))
        model = self.model_var.get() if model_dirty else str(settings.get("model", ""))
        self._desktop_applying_settings = True
        try:
            self._refresh_model_options(display_provider, model)
            self.model_var.set(model)
        finally:
            self._desktop_applying_settings = False
        if not model_dirty:
            self._desktop_dirty_fields.discard("model")
        self.voice_var.set(str(settings.get("voice", VOICE_PRESETS[0])))
        self.read_var.set(bool(settings.get("auto_read", False)))
        self._playback_auto_read = self.read_var.get()
        self._playback_volume = int(settings.get("volume", 100))
        self.api_var.set(bool(settings.get("api_enabled", False)))
        self.net_var.set(bool(settings.get("network_enabled", False)))
        self._desktop_committed_settings.update(settings)
        self._desktop_applying_settings = True
        try:
            for key, value in self.desktop_setting_vars.items():
                if key not in settings:
                    continue
                submitted_current = (committed_revisions is not None and key in committed_revisions
                                     and committed_revisions[key] == self._desktop_draft_revisions[key])
                if key not in self._desktop_dirty_fields or submitted_current:
                    value.set(str(settings[key]))
                    self._desktop_dirty_fields.discard(key)
        finally:
            self._desktop_applying_settings = False
        self._refresh_desktop_committed_state()
        self._sync_toggle_dots()
        self._refresh_api_indicator()

    def _render_desktop_settings(self, result: dict, generation: int, patch: dict,
                                 revisions: dict | None = None) -> None:
        if self._closing:
            return
        if generation != self._settings_generation:
            # A newer unrelated save owns the full snapshot, but an earlier
            # successful save can still acknowledge its unchanged draft.  Do
            # not label that already saved input as permanently unsaved.
            epoch = result.get("_context_epoch")
            if result.get("ok") and epoch is not None and self.server.context_current(epoch):
                for key, revision in (revisions or {}).items():
                    if self._desktop_draft_revisions.get(key) == revision:
                        self._desktop_dirty_fields.discard(key)
                self._refresh_desktop_committed_state()
            return
        self._settings_pending = False
        epoch = result.get("_context_epoch")
        if epoch is None or not self.server.context_current(epoch):
            # A submitted model choice whose receipt lost its source context
            # must be reconciled with the current runtime.  A newer unsubmitted
            # draft and the independent numeric drafts remain editable.
            if ("model" in patch and revisions is not None
                    and revisions.get("model") == self._desktop_draft_revisions["model"]):
                self._desktop_dirty_fields.discard("model")
            self.desktop_settings_state.set("设置期间来源已变更，正在重新读取。")
            self._desktop_settings_loaded = False
            self._request_desktop()
            return
        if isinstance(result.get("settings"), dict):
            self._apply_desktop_settings(result["settings"], revisions if result.get("ok") else None)
        self.desktop_settings_state.set(str(result.get("text") or ("桌面设置已保存。" if result.get("ok") else "桌面设置未保存。")))
        if result.get("settings_readonly"):
            self.desktop_save_button.configure(state="disabled")
        self._request_desktop()

    def _export_data(self) -> None:
        if self._closing:
            return
        self.export_state.set("正在导出本地逻辑数据…")
        self.export_button.configure(state="disabled")
        epoch = self.server.context_epoch()
        try:
            future = self.server.export_data()
        except Exception as exc:
            self.export_button.configure(state="normal")
            self.export_state.set(f"导出未完成：{type(exc).__name__}")
            return
        def done(fut):
            try:
                result = fut.result()
            except Exception as exc:
                result = {"ok": False, "text": f"导出未完成：{type(exc).__name__}", "_context_epoch": epoch}
            self._queue_ui(self._render_export, result)
        future.add_done_callback(done)

    def _render_export(self, result: dict) -> None:
        if self._closing:
            return
        self.export_button.configure(state="normal")
        epoch = result.get("_context_epoch")
        if epoch is None or not self.server.context_current(epoch):
            self.export_state.set("来源已变更，旧导出回执已忽略。")
            return
        message = str(result.get("text", "导出未完成。"))
        if result.get("ok") and result.get("path"):
            message += "\n" + str(result["path"])
        self.export_state.set(message)

    # ---------- 今日概览与跨来源回顾 ----------
    def _show_works(self) -> None:
        self.notebook.select(self._tab_works)
        self._refresh_files()

    def _review_open(self) -> bool:
        return self.review_window is not None and bool(self.review_window.winfo_exists())

    def _open_review(self) -> None:
        if self._closing:
            return
        if self._review_open():
            self.review_window.lift()
            self._request_desktop()
            return
        window = self.review_window = tk.Toplevel(self.root)
        window.title("苏晚 · 今日回顾与时间线")
        window.geometry("960x740")
        window.minsize(780, 560)
        window.configure(bg=PANEL)
        window.protocol("WM_DELETE_WINDOW", self._close_review)
        tk.Label(window, textvariable=self.today_summary, bg=PANEL, fg=ACCENT, justify="left",
                 anchor="w", wraplength=900, font=("Microsoft YaHei UI", 10)).pack(fill=tk.X, padx=12, pady=10)
        self.review_today = tk.StringVar(value="正在读取近期活动与待办…")
        tk.Label(window, textvariable=self.review_today, bg=PANEL, fg=FG, justify="left", anchor="w",
                 wraplength=900, font=("Microsoft YaHei UI", 8)).pack(fill=tk.X, padx=12, pady=(0, 6))
        filters = tk.Frame(window, bg=PANEL)
        filters.pack(fill=tk.X, padx=12)
        self.review_query_var = tk.StringVar()
        query_entry = tk.Entry(filters, textvariable=self.review_query_var, bg=FIELD, fg=FG,
                               insertbackground=FG, relief=tk.FLAT, font=("Microsoft YaHei UI", 10))
        query_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        query_entry.bind("<Return>", lambda _event: self._request_desktop())
        self._review_kinds = {"全部": "all", "记忆": "memory", "活动": "activity", "学习": "learning"}
        self.review_kind_var = tk.StringVar(value="全部")
        kind = ttk.Combobox(filters, textvariable=self.review_kind_var, values=list(self._review_kinds),
                            state="readonly", width=9)
        kind.pack(side=tk.LEFT, padx=8)
        kind.bind("<<ComboboxSelected>>", lambda _event: self._request_desktop())
        self.review_search_button = ttk.Button(filters, text="查询时间线", command=self._request_desktop)
        self.review_search_button.pack(side=tk.RIGHT)
        self.review_tree = ttk.Treeview(window, columns=("kind", "time", "title", "state"), show="headings", height=10)
        for key, label, width in (("kind", "类型", 65), ("time", "发生时间", 150), ("title", "共同经历与进展", 400),
                                   ("state", "状态 / 保存", 180)):
            self.review_tree.heading(key, text=label)
            self.review_tree.column(key, width=width, minwidth=50, stretch=True)
        self.review_tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=(8, 4))
        self.review_tree.bind("<<TreeviewSelect>>", self._review_selected)
        self.review_detail = self._scrolled(window)
        self.review_detail.configure(height=6, state="disabled")
        self.review_detail.pack(fill=tk.X, padx=12, pady=4)
        self.review_status = tk.StringVar(value="请选择记录查看来源 ID 与保存状态。")
        tk.Label(window, textvariable=self.review_status, bg=PANEL, fg=SUB, justify="left", anchor="w",
                 wraplength=900, font=("Microsoft YaHei UI", 8)).pack(fill=tk.X, padx=12)
        self._label(window, "近期对话（仅本次会话，不在退出后恢复；正式记忆另列于上方时间线）", color=ACCENT,
                    bg=PANEL, size=8).pack(anchor="w", padx=12, pady=(8, 0))
        self.review_recent = self._scrolled(window)
        self.review_recent.configure(height=4, state="disabled", font=("Microsoft YaHei UI", 8))
        self.review_recent.pack(fill=tk.X, padx=12, pady=(4, 12))
        self._request_desktop()

    def _close_review(self) -> None:
        self._desktop_generation += 1
        self._desktop_pending = False
        self._review_rows.clear()
        if self._review_open():
            self.review_window.destroy()
        self.review_window = None

    def _request_desktop(self, *, background: bool = False) -> None:
        if self._closing or (background and self._desktop_pending):
            return
        self._desktop_generation += 1
        generation = self._desktop_generation
        self._desktop_pending = True
        epoch = self.server.context_epoch()
        query = self.review_query_var.get().strip() if self._review_open() else ""
        kind = self._review_kinds.get(self.review_kind_var.get(), "all") if self._review_open() else "all"
        try:
            future = self.server.desktop_view(query=query, kind=kind, limit=50)
        except Exception as exc:
            self._desktop_pending = False
            self.today_summary.set(f"今日概览暂不可读：{type(exc).__name__}")
            return
        def done(fut):
            try:
                result = fut.result()
            except Exception as exc:
                result = {"ok": False, "text": f"回顾读取失败：{type(exc).__name__}", "_context_epoch": epoch}
            self._queue_ui(self._render_desktop, result, generation)
        future.add_done_callback(done)

    @staticmethod
    def _persistence_label(row: dict) -> str:
        if row.get("pending_save") or row.get("persistence") == "pending":
            return "待保存"
        return {"saved": "本地已保存", "session_only": "仅本次会话", "metadata_only": "仅进度记录"}.get(
            str(row.get("persistence", "")), str(row.get("persistence", "未确认保存")))

    def _render_desktop(self, result: dict, generation: int) -> None:
        if self._closing or generation != self._desktop_generation:
            return
        self._desktop_pending = False
        epoch = result.get("_context_epoch")
        if epoch is None or not self.server.context_current(epoch):
            return
        if not result.get("ok"):
            self.today_summary.set(str(result.get("text", "今日概览暂不可读。")))
            return
        today = result.get("today", {})
        summary = str(today.get("summary") or "今天还没有已记录的活动。")
        self.today_summary.set(summary)
        if not self._desktop_settings_loaded and not self._settings_pending and isinstance(result.get("settings"), dict):
            self._apply_desktop_settings(result["settings"])
            self.desktop_settings_state.set("桌面设置已读取。")
        if result.get("settings_readonly"):
            self.desktop_save_button.configure(state="disabled")
            self.desktop_settings_state.set("设置文件异常，已保留原文件并暂停设置写入；请修复后重启。")
        storage = result.get("storage", {})
        paths = (("home_dir", "数据根目录"), ("data_dir", "持久数据"), ("share_dir", "投喂资料"),
                 ("output_dir", "作品输出"), ("settings_file", "桌面设置"), ("export_dir", "逻辑导出"))
        self.data_info.set("\n".join(f"{label}：{storage[key]}" for key, label in paths if storage.get(key)))
        retention = result.get("retention", {})
        self.retention_info.set("数据保留范围：\n" + "\n".join(str(value) for value in retention.values()))
        if not self._review_open():
            return
        describe = lambda rows: "；".join(f"{str(row.get('title', '活动'))[:30]}（{row.get('status', '')}）"
                                        for row in rows[:3]) or "暂无记录"
        self.review_today.set("今日活动：" + describe(today.get("activities", [])) +
                              "\n待办事项：" + describe(today.get("todos", [])))
        selected = self.review_tree.selection()
        self._review_rows.clear()
        self.review_tree.delete(*self.review_tree.get_children())
        labels = {"memory": "记忆", "activity": "活动", "learning": "学习"}
        for row in result.get("timeline", []):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                continue
            iid = row["id"]
            if iid in self._review_rows:
                continue
            self._review_rows[iid] = dict(row)
            self.review_tree.insert("", tk.END, iid=iid, values=(labels.get(row.get("kind"), row.get("kind", "")),
                str(row.get("time", ""))[:19], str(row.get("title", "")),
                f"{row.get('status', '')} · {self._persistence_label(row)}"))
        if selected and selected[0] in self._review_rows:
            self.review_tree.selection_set(selected[0])
        self._review_selected()
        warnings = [str(value) for value in result.get("warnings", [])]
        self.review_status.set(f"显示 {len(self._review_rows)} 条记录，最多 50 条；可按关键词与类型筛选。" +
                               ("\n" + "；".join(warnings) if warnings else ""))
        recent = []
        for row in result.get("recent_chat", [])[-8:]:
            recent.append(f"{row.get('kind', '对话')} · {str(row.get('time', ''))[:19]} · 仅本次会话\n{row.get('text', '')}")
        self._replace_text(self.review_recent, "\n\n".join(recent) if recent else "本次会话还没有近期对话。")

    def _review_selected(self, _event=None) -> None:
        selected = self.review_tree.selection()
        row = self._review_rows.get(selected[0]) if selected else None
        if row is None:
            self._replace_text(self.review_detail, "选择一条记录可查看正文、来源与保存状态。")
            return
        sources = list(row.get("source_ids", []))
        if row.get("source_id") and row["source_id"] not in sources:
            sources.insert(0, row["source_id"])
        text = f"{row.get('title', '')}\n类型：{row.get('kind', '')} · 状态：{row.get('status', '')} · {self._persistence_label(row)}\n"
        text += "来源 ID：" + ("、".join(map(str, sources)) if sources else "无；此记录没有可引用的原文来源")
        text += "\n记录 ID：" + str(row.get("entity_id", row.get("id", "")))
        text += "\n\n" + str(row.get("text", ""))
        self._replace_text(self.review_detail, text)

    @staticmethod
    def _replace_text(widget, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, text)
        widget.configure(state="disabled")

    def _clear_desktop_view(self) -> None:
        self._desktop_generation += 1
        self._desktop_pending = False
        self._review_rows.clear()
        self.today_summary.set("来源已变更，正在重新读取今日概览…")
        self.export_state.set("来源已变更；后续导出使用当前有效数据。")
        if self._review_open():
            self.review_tree.delete(*self.review_tree.get_children())
            self.review_today.set("来源已变更，旧活动与待办概览已清空。")
            self._replace_text(self.review_detail, "来源已变更，旧回顾已清空。")
            self._replace_text(self.review_recent, "来源已变更，旧近期对话已清空。")
        self._request_desktop()

    def _set_interaction_preference(self, key: str) -> None:
        if key == "preference.response_style":
            value = self._response_styles[self.response_style_var.get()]
        else:
            value = self._initiative_levels[self.initiative_var.get()]
        self._request_interaction(key, value)

    def _request_interaction(self, key: str | None = None, value: str | None = None) -> None:
        if self._closing or not hasattr(self, "interaction_state"):
            return
        if self._interaction_pending and (key is None or self._interaction_mutating):
            return
        self._interaction_generation += 1
        generation = self._interaction_generation
        self._interaction_pending = True
        self._interaction_mutating = key is not None
        if key is not None:
            self.interaction_state.set("正在保存交流偏好…")
            for control in self._initiative_controls:
                control.configure(state="disabled")
        try:
            future = (self.server.set_interaction_preference(key, value) if key is not None
                      else self.server.interaction_view())
        except (RuntimeError, AttributeError) as exc:
            self._interaction_pending = False
            self._interaction_mutating = False
            self.interaction_state.set(f"交流偏好读取失败：{type(exc).__name__}")
            for control in self._initiative_controls:
                control.configure(state="readonly")
            return

        def done(fut):
            try:
                result = fut.result()
            except Exception as exc:
                result = {"ok": False, "text": f"交流偏好操作失败：{type(exc).__name__}"}
            self._queue_ui(self._render_interaction, result, generation, key is not None)
        future.add_done_callback(done)

    def _render_interaction(self, result: dict, generation: int, changed: bool = False) -> None:
        if self._closing or generation != self._interaction_generation:
            return
        self._interaction_pending = False
        self._interaction_mutating = False
        for control in self._initiative_controls:
            control.configure(state="readonly")
        epoch = result.get("_context_epoch")
        if epoch is not None and not self.server.context_current(epoch):
            self._request_interaction()
            return
        if "preferences" not in result:
            self.interaction_state.set(result.get("text", "交流偏好读取失败。"))
            if changed:
                self._request_interaction()
            return
        if epoch is None:
            self.interaction_state.set("交流状态缺少来源版本，已忽略。")
            return
        preferences = result["preferences"]
        response_style = preferences.get("response_style", "normal")
        initiative = preferences.get("initiative", "normal")
        self.response_style_var.set(next((label for label, value in self._response_styles.items() if value == response_style), "自然"))
        self.initiative_var.set(next((label for label, value in self._initiative_levels.items() if value == initiative), "正常"))
        message = result.get("text") or "交流偏好已同步。"
        if result.get("warning") and str(result["warning"]) not in message:
            message += "\n" + str(result["warning"])
        self.interaction_state.set(message)
        self.personality_text.set("人格说明：" + str(result.get("personality", {}).get("summary", "暂无说明。")))
        events = result.get("relation", {}).get("events", [])
        lines = []
        for event in events[-5:]:
            lines.append(f"{event.get('reason', event.get('kind', '互动'))} · 亲密 {event.get('closeness_delta', 0):+.2f} / 信任 {event.get('trust_delta', 0):+.2f}\n"
                         f"来源 ID：{event.get('source_id', '无')} · {str(event.get('occurred_at', ''))[:19]}")
        self.relation_details.configure(state="normal")
        self.relation_details.delete("1.0", tk.END)
        self.relation_details.insert(tk.END, "\n".join(lines) if lines else "尚无有来源的关系变化记录。")
        self.relation_details.configure(state="disabled")

    def _clear_interaction_view(self) -> None:
        if not hasattr(self, "interaction_state"):
            return
        self.response_style_var.set("")
        self.initiative_var.set("")
        self.interaction_state.set("记忆或偏好已变更，正在读取…")
        self.status_text.set("状态已变更，正在刷新…")
        self.status_area.configure(state="normal")
        self.status_area.delete("1.0", tk.END)
        self.status_area.insert(tk.END, self.status_text.get())
        self.status_area.configure(state="disabled")
        self.personality_text.set("人格说明：正在读取…")
        self.relation_details.configure(state="normal")
        self.relation_details.delete("1.0", tk.END)
        self.relation_details.configure(state="disabled")
        self._request_interaction()

    def _add_dot_toggle(self, parent, var, text: str, on_color: str, on_toggle) -> tk.Frame:
        """通用圆点开关：灰色=关，彩色=开；点击切换并回调。"""
        group = tk.Frame(parent, bg=PANEL)
        canvas = tk.Canvas(group, width=18, height=18, bg=PANEL, highlightthickness=0)
        oval = canvas.create_oval(4, 4, 14, 14, fill="#5a5f66", outline="")
        canvas.pack(side=tk.LEFT)
        lbl = tk.Label(group, text=text, bg=PANEL, fg=FG, font=("Microsoft YaHei UI", 9), cursor="hand2")
        lbl.pack(side=tk.LEFT, padx=(4, 0))

        def flip(_event=None) -> None:
            if self._closing:
                return
            var.set(not var.get())
            self._sync_toggle_dots()
            if on_toggle:
                on_toggle()

        for widget in (canvas, lbl):
            widget.bind("<Button-1>", flip)
        self._toggles.append((canvas, oval, var, on_color, on_toggle))
        return group

    def _sync_toggle_dots(self) -> None:
        """让联网/自动朗读圆点颜色跟随开关状态。"""
        for canvas, oval, var, on_color, _on_toggle in self._toggles:
            canvas.itemconfig(oval, fill=on_color if var.get() else "#5a5f66")

    def _sync_settings_from_runtime(self) -> None:
        """启动时按运行时实际状态回填控件（含自动选中的供应商）。"""
        provider = getattr(self.server.runtime.llm, "provider", "mimo")
        self.provider_var.set(PROVIDER_LABELS.get(provider, provider))
        self._refresh_model_options(provider)
        flags = self.server.status().get("flags", {})
        self.api_var.set(bool(flags.get("api", False)))
        self.net_var.set(bool(flags.get("network", False)))

    def _current_provider(self) -> str | None:
        return self._provider_by_label.get(self.provider_var.get())

    def _refresh_model_options(self, provider: str | None, model: str | None = None) -> None:
        if not provider:
            return
        if model is None:
            llm = self.server.runtime.llm
            # A provider save can still be waiting for its physical writer.
            # Only use the live model when it belongs to this provider.
            model = (getattr(llm, "model", "") if getattr(llm, "provider", None) == provider
                     else self.server.runtime.settings.model_names.get(provider, ""))
        options = list(MODEL_CANDIDATES.get(provider, []))
        if model and model not in options:
            options.insert(0, model)
        self.model_cb.configure(values=options)
        self.model_var.set(model if model in options else (options[0] if options else ""))

    # ---------- 作品/故事 ----------
    def _build_works(self) -> None:
        body = tk.Frame(self._tab_works, bg=PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        body.grid_rowconfigure(1, weight=1)
        body.grid_columnconfigure(1, weight=1)
        self._label(body, "灵感手记", color=ACCENT, size=10).grid(row=0, column=0, sticky="w", pady=(0, 14))
        self._label(body, "留下一些，值得重读的文字。", color=SUB, size=9).grid(row=0, column=1, sticky="w", padx=24, pady=(0, 14))
        self.works_list = tk.Listbox(body, bg=FIELD, fg=FG, font=(FONT, 10), width=23,
                                     relief=tk.FLAT, highlightthickness=0, selectbackground=ACCENT_SOFT,
                                     selectforeground=FG, activestyle="none")
        self.works_list.grid(row=1, column=0, sticky="nsw", ipadx=8, ipady=8)
        self.works_list.bind("<<ListboxSelect>>", self._on_works_select)
        reading = tk.Frame(body, bg=PANEL)
        reading.grid(row=1, column=1, sticky="nsew", padx=(14, 0))
        self.works_preview = self._scrolled(reading)
        self.works_preview.configure(state="disabled", font=(FONT, 11), spacing3=8)
        self.works_preview.pack(fill=tk.BOTH, expand=True)
        self.works_empty = tk.Label(reading, text="灵感会在这里，慢慢长大。\n\n完成的文章与创作，都会收在这一页。",
                                    bg=PANEL, fg=SUB, font=(FONT, 10), justify="center", wraplength=270)
        self.works_empty.place(relx=.5, rely=.4, anchor="center")

    def _build_story(self) -> None:
        heading = tk.Frame(self._tab_story, bg=PANEL)
        heading.pack(fill=tk.X, padx=28, pady=(24, 8))
        self._label(heading, "我们的慢时光", size=16, color=ACCENT).pack(anchor="w")
        self._label(heading, "一些经历，一些成长。故事从每一次相处开始。", color=SUB, size=9).pack(anchor="w", pady=(8, 0))
        reading = tk.Frame(self._tab_story, bg=PANEL)
        reading.pack(fill=tk.BOTH, expand=True)
        self.story_area = self._scrolled(reading, fg=FG)
        self.story_area.configure(font=(FONT, 11), padx=28, pady=24, spacing1=5, spacing3=8)
        self.story_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.story_empty = tk.Label(reading, text="故事还在慢慢写。\n\n等有了新的篇章，再来这里读一读。",
                                    bg=PANEL, fg=SUB, font=(FONT, 10), justify="center")
        self.story_empty.place(relx=.5, rely=.4, anchor="center")

    # ---------- 系统日志页（过滤 + 清空） ----------
    def _build_log(self) -> None:
        head = tk.Frame(self._tab_log, bg=PANEL)
        head.pack(fill=tk.X, padx=12, pady=(10, 4))
        self._label(head, "级别：", color=SUB, size=9).pack(side=tk.LEFT)
        self.filter_var = tk.StringVar(value="ALL")
        for level in LOG_LEVELS:
            rb = ttk.Radiobutton(head, text=level, variable=self.filter_var, value=level, command=self._render_log)
            rb.pack(side=tk.LEFT, padx=(2, 8))
        ttk.Button(head, text="清空日志", command=self._clear_log).pack(side=tk.RIGHT)
        self.log_area = self._scrolled(self._tab_log, bg=PANEL, fg=SUB)
        self.log_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    def _matches_filter(self, level: str) -> bool:
        current = self.filter_var.get() if hasattr(self, "filter_var") else "ALL"
        return current == "ALL" or current == level.upper()

    def _clear_log(self) -> None:
        self._log_entries.clear()
        self._render_log()

    # ---------- row2 状态栏 ----------
    def _build_statusbar(self, row: int) -> None:
        bar = tk.Frame(self.root, bg=PANEL)
        bar.grid(row=row, column=1, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.api_state_lbl = tk.Label(bar, text=API_OFF_HINT, font=(FONT, 8), bg=PANEL, fg=SUB,
                                      wraplength=450, justify="left", anchor="w")
        self.api_state_lbl.grid(row=0, column=0, sticky="w", padx=(20, 8), pady=(4, 7))
        self.voice_state_lbl = tk.Label(bar, text="音色：mimo_default", bg=PANEL, fg=SUB, font=(FONT, 8))
        self.voice_state_lbl.grid(row=0, column=1, sticky="e", padx=(0, 18), pady=(4, 7))
        self.hint_lbl = tk.Label(bar, text="", bg=PANEL, fg=SUB, font=(FONT, 8),
                                wraplength=175, justify="right")
        self.hint_lbl.grid(row=0, column=2, sticky="e", padx=(0, 12))

    # ---------- 输出分流 ----------
    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _show_chat(self, sender: str, msg: str) -> None:
        self.chat_empty_frame.place_forget()
        follow = sender == "你" or self.chat_area.yview()[1] >= 0.995
        now = datetime.now()
        self.chat_area.append(sender, now, msg)
        if not self._review_has_art:
            self._review_has_art = True
            self._sync_review_art(self.root.winfo_width())
        preview = " ".join(msg.split())
        self._chat_sidebar_preview.set(preview[:80] + "…" if len(preview) > 80 else preview or sender)
        self._chat_sidebar_time.set(now.strftime("%H:%M"))
        if follow:
            self._jump_to_latest()
        else:
            self._has_new_chat = True
            self.chat_jump.place(relx=1, rely=1, x=-28, y=-14, anchor="se")

    def _on_chat_scroll(self, first: str, last: str) -> None:
        self._chat_scrollbar.set(first, last)
        if self._has_new_chat and float(last) >= 0.995:
            self._has_new_chat = False
            self.chat_jump.place_forget()

    def _jump_to_latest(self) -> None:
        self._has_new_chat = False
        self.chat_area.yview_moveto(1)
        self.chat_jump.place_forget()

    def _log_msg(self, text: str, level: str = "info") -> None:
        self._log_entries.append((level, text))
        if self._matches_filter(level):
            self._append_log_line(level, text)

    def _append_log_line(self, level: str, text: str) -> None:
        self.log_area.configure(state="normal")
        tag = "log_" + level
        self.log_area.tag_config(tag, foreground=LOG_COLORS.get(level, SUB))
        self.log_area.insert(tk.END, f"[{self._ts()}] [{level.upper()}] {text}\n", tag)
        self.log_area.see(tk.END)
        self.log_area.configure(state="disabled")

    def _render_log(self) -> None:
        self.log_area.configure(state="normal")
        self.log_area.delete("1.0", tk.END)
        for level, text in self._log_entries:
            if not self._matches_filter(level):
                continue
            tag = "log_" + level
            self.log_area.tag_config(tag, foreground=LOG_COLORS.get(level, SUB))
            self.log_area.insert(tk.END, f"[{self._ts()}] [{level.upper()}] {text}\n", tag)
        self.log_area.see(tk.END)
        self.log_area.configure(state="disabled")

    # ---------- 控制回调 ----------
    def _on_provider_selected(self, _event=None) -> None:
        provider = self._current_provider()
        if not provider:
            return
        # A model draft belongs to the previous provider.  Do not carry it
        # into this provider's implicit default, even if its old save was still
        # waiting for after_idle to flush.
        self._desktop_dirty_fields.discard("model")
        self._desktop_draft_revisions["model"] += 1
        self._pending_settings_patch.pop("model", None)
        self._pending_settings_revisions.pop("model", None)
        self._desktop_applying_settings = True
        try:
            self._refresh_model_options(provider)
        finally:
            self._desktop_applying_settings = False
        self._save_desktop_settings({"provider": provider})
        # A provider selection is already a completed choice.  Submit it now
        # so an in-flight probe is followed by the new owner's configuration.
        self._flush_desktop_settings()

    def _on_model_selected(self, _event=None) -> None:
        model = self.model_var.get().strip()
        provider = self._current_provider()
        if not model or not provider:
            return
        # Preserve the visible draft's provider ownership if an earlier
        # provider transaction failed or another switch is still queued.
        self._save_desktop_settings({"provider": provider, "model": model})

    def _on_api(self) -> None:
        if self._closing:
            return
        if not self.api_var.get():
            self._stop_speaking()
        self._save_desktop_settings({"api_enabled": self.api_var.get()})
        self._refresh_api_indicator()

    def _on_net(self) -> None:
        self._save_desktop_settings({"network_enabled": self.net_var.get()})

    def _on_voice_selected(self, _event=None) -> None:
        self._stop_speaking()
        self._save_desktop_settings({"voice": self.voice_var.get()})

    def send(self, _event=None) -> None:
        if self._closing:
            return
        text = self.chat_entry.get("1.0", "end-1c").strip()
        if not text:
            return
        if (self._chat_future is not None and not self._chat_future.done()
                and text.partition(" ")[0] not in {"/api", "/net", "/tts", "/doc", "/learn"}):
            self.chat_wait_var.set("正在等待回复；可先取消本次等待，再发送新消息。")
            return
        self.entry_var.set("")
        self._show_chat("你", text)
        if self._route_command(text):
            return
        self._chat_generation += 1
        generation = self._chat_generation
        self._flush_desktop_settings()
        try:
            future = self.server.ask_with_context(text)
        except Exception as exc:
            self._log_msg(f"发送未完成：{type(exc).__name__}", "error")
            return
        self._chat_future = future
        self._chat_started = time.monotonic()
        self.send_button.configure(state="disabled")
        self.chat_cancel_button.configure(state="normal")
        self._update_chat_wait(generation)
        future.add_done_callback(lambda done: self._on_reply_done(done, generation))

    def _update_chat_wait(self, generation: int) -> None:
        if self._closing or generation != self._chat_generation or self._chat_future is None:
            return
        seconds = int(time.monotonic() - self._chat_started)
        self.chat_wait_var.set(f"正在等苏晚回应 · {seconds} 秒；可以取消本次等待。")
        self._after(1000, lambda: self._update_chat_wait(generation))

    def _cancel_chat(self, *, log: bool = True) -> None:
        future = getattr(self, "_chat_future", None)
        self._chat_generation = getattr(self, "_chat_generation", 0) + 1
        self._chat_future = None
        if future is not None:
            future.cancel()
        if hasattr(self, "chat_wait_var"):
            self.chat_wait_var.set("已取消本次等待；已经发送的请求仍计入调用额度。")
            self.chat_cancel_button.configure(state="disabled")
            self.send_button.configure(state="normal")
        if log and future is not None:
            self._log_msg("已取消本次等待，迟到的回复不会显示；已经发送的请求仍计入调用额度。", "info")

    def _route_command(self, text: str) -> bool:
        command, _, argument = text.partition(" ")
        if command == "/learn":
            self._request_learning(text)
            return True
        if command in {"/api", "/net"}:
            value = argument.strip().lower()
            if value not in {"on", "off", "1", "0", "true", "false"}:
                self._log_msg(f"用法：{command} on|off", "warn")
                return True
            enabled = value in {"on", "1", "true"}
            if command == "/api":
                self.api_var.set(enabled)
                self._on_api()
            else:
                self.net_var.set(enabled)
                self._on_net()
            self._sync_toggle_dots()
            self._log_msg(f"{'API' if command == '/api' else '联网'} -> {'开启' if enabled else '关闭'}", "info")
            return True
        if command == "/tts":
            if argument.strip():
                self._speak(argument.strip(), expected_epoch=self.server.context_epoch())
            else:
                self._log_msg("用法：/tts 文本", "warn")
            return True
        if command == "/doc":
            if not argument.strip():
                self._log_msg("用法：/doc 主题", "warn")
                return True
            epoch = self.server.context_epoch()
            self._flush_desktop_settings()
            future = self.server.document(argument.strip())
            def done(fut):
                try:
                    path = fut.result()
                    text = f"文档已生成：{path}" if path else "文档生成失败（需先开启 API）"
                    level = "ok" if path else "warn"
                except Exception as exc:
                    text, level = f"文档操作失败：{type(exc).__name__}", "error"
                self._queue_ui(self._render_command_result, text, level, epoch)
            future.add_done_callback(done)
            return True
        return False

    def _render_command_result(self, text: str, level: str, epoch) -> None:
        if not self._closing and self.server.context_current(epoch):
            self._log_msg(text, level)

    def _on_reply_done(self, future, generation: int | None = None) -> None:
        try:
            result = future.result(timeout=0.1)
            if not isinstance(result, dict) or "text" not in result or "epoch" not in result:
                raise ValueError("回复缺少上下文版本")
        except Exception as exc:  # noqa: BLE001
            message = f"回复失败：{type(exc).__name__}"
            if generation is None:
                self._queue_ui(self._log_msg, message, "error")
            else:
                self._queue_ui(self._render_chat_completion, None, message, generation)
            return
        if generation is None:
            self._queue_ui(self._render_reply, result["text"], result["epoch"])
        else:
            self._queue_ui(self._render_chat_completion, result, None, generation)

    def _render_chat_completion(self, result: dict | None, error: str | None, generation: int) -> None:
        if self._closing or generation != self._chat_generation:
            return
        self._chat_future = None
        self.send_button.configure(state="normal")
        self.chat_cancel_button.configure(state="disabled")
        self.chat_wait_var.set("Enter 发送 · Shift+Enter 换行")
        if error:
            self._log_msg(error, "error")
        elif result is not None:
            self._render_reply(str(result["text"]), result["epoch"])
        self._request_desktop()

    def _render_reply(self, reply: str, epoch) -> None:
        if self._closing or not self.server.context_current(epoch):
            return
        self._sync_ui_context()
        self._last_reply = reply
        self._last_reply_epoch = epoch
        self._show_chat("苏晚", reply)
        if self._playback_auto_read and self.read_var.get() and reply:
            self._speak(reply, expected_epoch=epoch)

    def _speak_last(self) -> None:
        if not self._last_reply:
            self._log_msg("还没有可朗读的回复", "warn")
            return
        epoch = getattr(self, "_last_reply_epoch", None)
        if epoch is None or not self.server.context_current(epoch):
            self._last_reply, self._last_reply_epoch = "", None
            self._log_msg("记忆设置已变更，请先获取新回复。", "warn")
            return
        self._speak(self._last_reply, expected_epoch=epoch)

    def _speak(self, text: str, *, expected_epoch=None) -> None:
        if self._closing:
            return
        self._flush_desktop_settings()
        epoch = self.server.context_epoch() if expected_epoch is None else expected_epoch
        if not self.server.context_current(epoch):
            return
        self._stop_speaking()
        generation = self._speech_generation
        # Resolve the committed voice on the owner loop, after any queued save.
        future = self.server.speak(text, voice=None, expected_epoch=epoch)
        self._speech_future = future

        def done(fut) -> None:
            try:
                path, error = fut.result(timeout=0.1)
            except Exception as exc:  # noqa: BLE001
                path, error = None, f"{type(exc).__name__}: {str(exc)[:80]}"
            self._queue_ui(self._render_audio, path, error, generation, epoch)

        future.add_done_callback(done)

    def _on_auto_read(self) -> None:
        if not self.read_var.get():
            self._stop_speaking()
        self._save_desktop_settings({"auto_read": self.read_var.get()})

    def _stop_speaking(self) -> None:
        self._speech_generation += 1
        if self._speech_future is not None:
            self._speech_future.cancel()
            self._speech_future = None
        self._playback.stop()

    def _render_audio(self, path, error, generation: int, epoch=None) -> None:
        if self._closing or generation != self._speech_generation:
            return
        if epoch is None or not self.server.context_current(epoch):
            return
        if error:
            self._log_msg(f"语音失败：{error}", "error")
        elif path:
            try:
                self._playback.play(
                    path,
                    volume=getattr(self, "_playback_volume", 100),
                    on_started=lambda: self._queue_ui(self._audio_feedback, generation, f"正在朗读：{Path(path).name}", "ok", epoch),
                    on_error=lambda message: self._queue_ui(self._audio_feedback, generation, f"播放失败：{message}", "error", epoch),
                )
            except Exception as exc:
                self._log_msg(f"播放失败：{exc}", "error")

    def _audio_feedback(self, generation: int, message: str, level: str, epoch=None) -> None:
        if not self._closing and generation == self._speech_generation and epoch is not None and self.server.context_current(epoch):
            self._log_msg(message, level)

    # ---------- API 指示 ----------
    def _paint_api_status(self, text: str, color: str) -> None:
        self.api_status_text.set(text)
        self.api_state_lbl.configure(text=text, fg=color)

    def _refresh_api_indicator(self) -> None:
        try:
            state, msg = self.server.runtime.api_indicator()
        except Exception:  # noqa: BLE001
            state, msg = None, "状态未知"
        if not self.api_var.get():
            self._paint_api_status(API_OFF_HINT, SUB)
        elif state is True:
            self._paint_api_status(f"● 已连接 · {msg}", OK)
        elif state is False:
            self._paint_api_status(f"● {msg}", ERR)
        else:
            self._paint_api_status(f"● {msg}", WARN)
        self.voice_state_lbl.configure(text=f"音色：{self.voice_var.get()}")
        provider = getattr(self.server.runtime.llm, "provider", "")
        model = getattr(self.server.runtime.llm, "model", "")
        self.mode_lbl.configure(text=f"{PROVIDER_LABELS.get(provider, provider)} / {model}")
        selected = self._current_provider()
        if self._settings_pending and selected and selected != provider:
            settings = self.server.runtime.settings
            available = selected in settings.keyless_providers or bool(settings.api_keys.get(selected))
            self._paint_api_status("● 正在切换供应商…" if available else "● 未配置密钥",
                                   WARN if available else ERR)
        indicator = getattr(self, "_api_probe_indicator", None)
        if indicator is not None:
            context, ok, message = indicator
            if context == self._api_probe_context():
                # Initial reads and unrelated settings receipts can arrive
                # after this probe. They do not revoke its observed state.
                self._show_api_probe_indicator(ok, message)
            else:
                self._api_probe_indicator = None

    def _test_api(self) -> None:
        if self._closing:
            return
        self._flush_desktop_settings()
        if self._settings_future is not None and not self._settings_future.done():
            self._after(50, self._test_api)
            return
        if not self.server.runtime.api_enabled:
            self._api_probe_indicator = None
            self._paint_api_status(API_OFF_HINT, SUB)
            return
        self._api_probe_generation += 1
        context = self._api_probe_context()
        self._api_probe_indicator = (context, None, "")
        self._show_api_probe_indicator(None, "")
        future = self.server.api_check()
        future.add_done_callback(lambda done: self._on_api_check_done(done, context))

    def _api_probe_context(self) -> tuple:
        runtime, llm = self.server.runtime, self.server.runtime.llm
        return (self._api_probe_generation, runtime._model_config_generation,
                id(llm), runtime.api_enabled, llm.provider, llm.model, llm.request_generation)

    def _on_api_check_done(self, future, context=None) -> None:
        try:
            ok, msg = future.result(timeout=0.1)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"{type(exc).__name__}: {str(exc)[:80]}"
        if context is None:  # 独立兼容入口；生产探测始终携带配置与请求来源。
            self._queue_ui(self._render_api_result, ok, msg)
        else:
            self._queue_ui(self._render_api_completion, ok, msg, context)

    def _render_api_completion(self, ok: bool, msg: str, context: tuple) -> None:
        if self._closing:
            return
        current = self._api_probe_context()
        if context != current:
            if context[0] == current[0]:
                self._refresh_api_indicator()  # 当前探测已撤销，不无限停留在“测试中”。
            return
        self._api_probe_indicator = (context, ok, msg)
        self._render_api_result(ok, msg)

    def _render_api_result(self, ok: bool, msg: str) -> None:
        self._log_msg(msg, "ok" if ok else "error")
        self._show_api_probe_indicator(ok, msg)

    def _show_api_probe_indicator(self, ok: bool | None, msg: str) -> None:
        if ok is None:
            self._paint_api_status("● 测试中…", WARN)
        elif ok:
            self._paint_api_status(f"● 已连接 · {msg}", OK)
        else:
            self._paint_api_status(f"● 连接失败 · {msg}", ERR)

    # ---------- 总线事件拉取 ----------
    def _after(self, delay: int, callback) -> None:
        if self._closing:
            return

        def invoke() -> None:
            self._after_ids.discard(after_id)
            if not self._closing:
                callback()

        after_id = self.root.after(delay, invoke)
        self._after_ids.add(after_id)

    def _queue_ui(self, callback, *args) -> None:
        # Future 回调可能位于后台线程；不能在此调用任何 Tk API，含 root.after。
        if not self._closing:
            self._ui_callbacks.put((callback, args))

    def _pump(self) -> None:
        if self._closing:
            return
        try:
            bridge = self.server.bridge
            items = bridge.drain()
            allows_notification = getattr(bridge, "allows_notification", None)
            commit_notification = getattr(bridge, "commit_notification", None)
            # A reset notification may arrive late, so check the runtime before rendering.
            self._sync_ui_context()
            while not self._closing:
                try:
                    callback, args = self._ui_callbacks.get_nowait()
                except queue.Empty:
                    break
                try:
                    callback(*args)
                except Exception as exc:
                    _LOG.warning("桌面回调处理失败（%s）", type(exc).__name__)
            if self._closing:
                return
            for item in items:
                kind, data = item["kind"], item["data"]
                text = data.get("text", data.get("name", ""))
                if kind == "memory_reset":
                    self._sync_ui_context()
                elif not self.server.context_current(item.get("epoch")):
                    continue
                elif callable(allows_notification) and not allows_notification(kind, data):
                    continue
                elif callable(commit_notification) and not commit_notification(kind, data):
                    continue
                elif kind == "doc":
                    self._log_msg(f"文档已生成：{text}", "ok")
                elif kind == "toast":
                    self._log_msg(text, "info")
                elif kind == "inner":
                    self._show_chat("独白", f"（{text}）")
                elif kind == "narrative":
                    self._show_chat("苏晚·主动", text)
        finally:
            self._after(100, self._pump)

    def _sync_ui_context(self) -> None:
        epoch = self.server.context_epoch()
        if epoch == self._ui_context_epoch:
            return
        self._ui_context_epoch = epoch
        self._last_reply, self._last_reply_epoch = "", None
        self._stop_speaking()
        self._clear_interaction_view()
        self._clear_activity_view()
        self._learning_generation += 1
        self._cancel_chat(log=False)
        self._clear_desktop_view()
        self._clear_works_view()
        self._set_story_view()
        self.chat_area.clear()
        self._review_has_art = False
        self._sync_review_art(self.root.winfo_width())
        self._chat_sidebar_preview.set("还没有消息")
        self._chat_sidebar_time.set("")
        self.chat_find_query.set("")
        self._chat_find_row.grid_remove()
        self._has_new_chat = False
        self.chat_jump.place_forget()
        if hasattr(self, "chat_empty_frame"):
            self.chat_empty_frame.place(relx=.5, rely=.43, anchor="center")
        self._memory_rows.clear()
        self.memory_tree.delete(*self.memory_tree.get_children())
        self.memory_detail.configure(state="normal")
        self.memory_detail.delete("1.0", tk.END)
        self.memory_detail.configure(state="disabled")
        self._memory_list()

    # ---------- 轮询视图 ----------
    def _schedule_views(self) -> None:
        self._refresh_status()
        self._request_interaction()
        self._request_activities()
        self._refresh_files()
        self._request_desktop(background=True)
        self._after(2000, self._schedule_views)

    def _refresh_status(self) -> None:
        try:
            status = self.server.status()
            v = status["vitals"]
            flags = status["flags"]
            line = (
                f"状态：{'睡眠中' if v['is_sleeping'] else '清醒'}  |  精力 {v['energy']:.0f}%  算力 {v['compute_power']:.0f}%  疲劳 {v['mental_fatigue']:.0f}\n"
                f"当前活动：{v['current_activity']}\n"
                f"情绪：{status['affect']['desc']}（{status['affect']['current']:.0f}）\n"
                f"欲望：好奇 {status['drive']['curiosity']:.0f}  孤独 {status['drive']['loneliness']:.0f}  "
                f"表达 {status['drive']['expressiveness']:.0f}  无聊 {status['drive']['boredom']:.0f}\n"
                f"关系：亲密度 {status['relation']['closeness']:.0f}  信任 {status['relation']['trust']:.0f}\n"
                f"记忆：{status['memory']['count']} 条  |  用户：{status['identity']['user_name'] or '未知'}\n"
                f"API={flags['api']}  联网={flags['network']}  模型={flags['llm_provider']}\n目标：\n"
            )
            for g in status["goals"]["goals"]:
                line += f"  • {g['name']}（{g['progress']:.0f}/{g['target']:.0f}）\n"
            self.status_text.set(line)
            self.status_area.configure(state="normal")
            self.status_area.delete("1.0", tk.END)
            self.status_area.insert(tk.END, line)
            self.status_area.configure(state="disabled")
            self._sync_toggle_dots()  # 联网/自动朗读圆点颜色跟随（每 2s 刷新）
        except Exception as exc:  # noqa: BLE001
            self.status_text.set(f"（状态读取失败：{type(exc).__name__} {str(exc)[:60]}）")

    def _refresh_files(self) -> None:
        try:
            self._sync_ui_context()
            epoch = self._ui_context_epoch
            data_dir = self.server.runtime.settings.data_dir
        except Exception as exc:  # noqa: BLE001
            self._clear_works_preview()
            self._set_story_view(error=True)
            self._log_msg(f"文件视图异常：{type(exc).__name__}", "warn")
            return
        try:
            works = sorted((data_dir / "作品集").glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            names = [w.name for w in works[:40]]
            current = list(self.works_list.get(0, tk.END))
            selection = self.works_list.curselection()
            selected = self.works_list.get(selection[0]) if selection else None
            if not self.server.context_current(epoch):
                self._sync_ui_context()
                self._set_story_view()
                return
            if names != current:
                self.works_list.delete(0, tk.END)
                for n in names:
                    self.works_list.insert(tk.END, n)
            if names:
                index = names.index(selected) if selected in names else 0
                self.works_list.selection_clear(0, tk.END)
                self.works_list.selection_set(index)
                # A filename can stay unchanged when its source or body changes.
                self._preview_works(works[index], expected_epoch=epoch)
            else:
                self._clear_works_preview()
        except Exception as exc:  # noqa: BLE001
            self._clear_works_preview()
            self._log_msg(f"作品视图异常：{type(exc).__name__}", "warn")
        # A story read must never clear an unrelated, valid work preview.
        try:
            try:
                content = (data_dir / "story.txt").read_text(encoding="utf-8")
            except FileNotFoundError:
                content = ""
            if not self.server.context_current(epoch):
                self._sync_ui_context()
                self._set_story_view()
                return
            self._set_story_view("\n".join(content.splitlines()[-60:]))
        except Exception as exc:  # noqa: BLE001
            self._set_story_view(error=True)
            self._log_msg(f"故事视图异常：{type(exc).__name__}", "warn")

    def _set_story_view(self, content: str = "", *, error: bool = False) -> None:
        self.story_area.configure(state="normal")
        self.story_area.delete("1.0", tk.END)
        if content:
            self.story_area.insert(tk.END, content)
        self.story_area.configure(state="disabled")
        if hasattr(self, "story_empty"):
            self.story_empty.configure(text=("故事暂时无法读取。\n\n原文件已保留，请检查文件编码或访问权限。" if error else
                                             "故事还在慢慢写。\n\n等有了新的篇章，再来这里读一读。"))
            if content.strip():
                self.story_empty.place_forget()
            else:
                self.story_empty.place(relx=.5, rely=.4, anchor="center")

    def _on_works_select(self, _event=None) -> None:
        self._sync_ui_context()
        sel = self.works_list.curselection()
        if not sel:
            self._clear_works_preview()
            return
        name = self.works_list.get(sel[0])
        path = self.server.runtime.settings.data_dir / "作品集" / name
        self._preview_works(path, expected_epoch=self._ui_context_epoch)

    def _clear_works_preview(self) -> None:
        self._works_generation += 1
        self.works_preview.configure(state="normal")
        self.works_preview.delete("1.0", tk.END)
        self.works_preview.configure(state="disabled")
        if hasattr(self, "works_empty"):
            self.works_empty.place(relx=.5, rely=.4, anchor="center")

    def _clear_works_view(self) -> None:
        self._clear_works_preview()
        self.works_list.delete(0, tk.END)

    def _work_selected(self, path: Path) -> bool:
        selection = self.works_list.curselection()
        return bool(selection) and path == (self.server.runtime.settings.data_dir / "作品集"
                                            / self.works_list.get(selection[0]))

    def _preview_works(self, path, *, expected_epoch=None) -> None:
        epoch = self._ui_context_epoch if expected_epoch is None else expected_epoch
        self._works_generation += 1
        self._render_works_preview(Path(path), epoch, self._works_generation)

    def _render_works_preview(self, path: Path, epoch, generation: int) -> None:
        if self._closing or not self.server.context_current(epoch):
            self._sync_ui_context()
            return
        if generation != self._works_generation or not self._work_selected(path):
            return
        try:
            content = path.read_text(encoding="utf-8")[:2000]
        except (OSError, UnicodeError):
            self._clear_works_preview()
            return
        # The runtime can revoke the source while this UI thread reads the file.
        if not self.server.context_current(epoch):
            self._sync_ui_context()
            return
        if generation != self._works_generation or not self._work_selected(path):
            return
        self.works_preview.configure(state="normal")
        self.works_preview.delete("1.0", tk.END)
        self.works_preview.insert(tk.END, content)
        self.works_preview.configure(state="disabled")
        if hasattr(self, "works_empty"):
            self.works_empty.place_forget()

    # ---------- 关闭 ----------
    def on_close(self) -> None:
        if self._closing:
            return
        if getattr(self, "_settings_flush_id", None) is not None:
            self._flush_desktop_settings()
        self._closing = True
        self._cancel_chat(log=False)
        self._stop_speaking()
        self._playback.stop(close=True)
        for after_id in self._after_ids:
            self.root.after_cancel(after_id)
        self._after_ids.clear()
        self._disable_inputs(self.root)
        self._close_started = time.monotonic()
        self._close_stop_requested = False
        self._close_settings_error = None
        self.root.title("苏晚 · 正在保存并退出")
        self._poll_close()

    def _disable_inputs(self, parent) -> None:
        for widget in parent.winfo_children():
            if isinstance(widget, (tk.Entry, tk.Button, ttk.Button, ttk.Combobox, ttk.Radiobutton, ttk.Checkbutton)):
                widget.configure(state="disabled")
            self._disable_inputs(widget)

    def _poll_close(self) -> None:
        if not self._close_stop_requested:
            settings_future = getattr(self, "_settings_future", None)
            if settings_future is not None and not settings_future.done():
                self.hint_lbl.configure(text="正在保存桌面设置，然后保存运行数据…")
                self.root.after(100, self._poll_close)
                return
            if settings_future is not None:
                try:
                    result = settings_future.result()
                    if not result.get("ok"):
                        self._close_settings_error = RuntimeError(str(result.get("text", "桌面设置未保存。")))
                except Exception as exc:
                    self._close_settings_error = exc
            self.server.request_stop()
            self._close_stop_requested = True
        state = self.server.shutdown_status()
        if state["finished"] and self._playback.idle:
            error = state["error"] or self._close_settings_error
            if error is not None:
                messagebox.showerror("退出保存未完成", f"{error}\n\n请保留现有数据与备份，检查问题后再启动。", parent=self.root)
            if self._playback.stop_error:
                messagebox.showerror("停止播放失败", self._playback.stop_error, parent=self.root)
            self.root.destroy()
            return
        if state["finished"]:
            message = "数据收尾已完成，正在停止音频播放…"
        elif state["runtime_stopped"]:
            message = "数据收尾已完成，正在等待外部请求线程退出…" if state["error"] is None else "保存发生错误，正在等待线程退出后显示详情…"
        else:
            message = "正在取消请求并保存数据…"
        if time.monotonic() - self._close_started >= 15:
            message += " 窗口将继续等待，可以移动窗口。"
        self.hint_lbl.configure(text=message)
        self.root.after(100, self._poll_close)

    def run(self) -> None:
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="suwan-tk")
    parser.add_argument("--home", default=None)
    args = parser.parse_args(argv)
    settings = Settings.from_env(home_dir=Path(args.home) if args.home else None)
    app = TkApp(SuwanRuntime(settings))
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
