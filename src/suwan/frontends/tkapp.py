"""苏晚 tkinter 前端（记忆管理、交流偏好与关系解释）。

布局（grid）：
  row0 标题栏（极简：品牌 + 当前供应商/模型）
  row1 Notebook：对话 | 状态板 | 设置 | 作品集 | 人生故事 | 系统日志
  row2 底部状态栏：API ✓/✕/◦ 指示 · 音色
"设置"页：API/联网开关、模型=供应商+具体模型二级下拉、音色/自动朗读/朗读上句、测试连接
"系统日志"页：日志正文 + 级别过滤(全部/INFO/OK/WARN/ERROR) + 清空
分流：对话=人话；其余系统提示全部进日志。
用法: python -m suwan.frontends.tkapp [--home <数据目录>]
"""
from __future__ import annotations

import argparse
import queue
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, simpledialog, ttk

from suwan.adapters.tts import VOICE_PRESETS
from suwan.adapters.playback import AudioPlayback
from suwan.config import ENDPOINT_ENV, MODEL_CANDIDATES, PROVIDER_LABELS, Settings
from suwan.frontends.bridge import RuntimeThread, TkBridge

try:
    from suwan.app import SuwanRuntime  # noqa: PLC0415
except ImportError:  # pragma: no cover
    SuwanRuntime = None  # type: ignore[assignment]

BG = "#14161a"
PANEL = "#1e2126"
FIELD = "#2b2f36"
FG = "#d7dae0"
SUB = "#8b919c"
ACCENT = "#e8b26a"
OK = "#7fc97f"
ERR = "#ff6b6b"
WARN = "#f0b35e"
COLORS = {"你": "#9fb4c7", "苏晚": ACCENT, "苏晚·主动": "#ffd28a", "独白": "#7cc0ff"}
LOG_COLORS = {"info": SUB, "ok": OK, "warn": WARN, "error": ERR}
LOG_LEVELS = ("ALL", "INFO", "OK", "WARN", "ERROR")


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
        self.root.title("苏晚 · 数字生命控制台")
        self.root.geometry("1140x820")
        self.root.minsize(960, 660)
        self.root.configure(bg=BG)
        self._style()

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
        self._api_probe_generation = 0
        self._log_entries: list[tuple[str, str]] = []  # (level, text)
        self._toggles: list = []  # (canvas, oval, var, on_color, on_toggle)

        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        self._build_header(0)
        self._build_notebook(1)
        self._build_statusbar(2)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._pump()
        self._sync_settings_from_runtime()
        self._refresh_api_indicator()
        self._log_msg("苏晚 v3 已启动 · 交流偏好、API、模型与声音可在“设置”页调整", "info")
        for warning in getattr(runtime.settings, "configuration_warnings", ()):
            self._log_msg(warning, "warn")
        for warning in getattr(runtime, "startup_warnings", ()):
            self._log_msg(warning, "warn")
        self._schedule_views()
        self._memory_list()

    # ---------- 样式 ----------
    def _style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=FG, padding=(14, 8), font=("Microsoft YaHei UI", 10))
        style.map("TNotebook.Tab", background=[("selected", ACCENT)], foreground=[("selected", "#14161a")])
        style.configure("TCheckbutton", background=BG, foreground=FG, font=("Microsoft YaHei UI", 9))
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG, arrowcolor=SUB)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#14161a", font=("Microsoft YaHei UI", 9, "bold"), padding=(12, 6))
        style.map("Accent.TButton", background=[("active", "#f3c586")])
        style.configure("TButton", background=FIELD, foreground=FG, padding=(10, 5), font=("Microsoft YaHei UI", 9))
        style.map("TButton", background=[("active", ACCENT)], foreground=[("active", "#14161a")])
        style.configure("Radiobutton", background=BG, foreground=FG, font=("Microsoft YaHei UI", 9))
        style.map("Radiobutton", background=[("active", BG)])

    def _label(self, parent, text: str, *, color: str = FG, size: int = 9, bg: str = BG) -> tk.Label:
        return tk.Label(parent, text=text, font=("Microsoft YaHei UI", size), fg=color, bg=bg)

    def _scrolled(self, parent, *, bg: str = "#121317", fg: str = "#e6e8ec") -> scrolledtext.ScrolledText:
        return scrolledtext.ScrolledText(parent, wrap=tk.WORD, font=("Microsoft YaHei UI", 10), bg=bg, fg=fg,
                                         insertbackground=FG, relief=tk.FLAT, padx=10, pady=8)

    # ---------- row0 标题（极简） ----------
    def _build_header(self, row: int) -> None:
        bar = tk.Frame(self.root, bg=BG)
        bar.grid(row=row, column=0, sticky="ew", padx=16, pady=(12, 4))
        tk.Label(bar, text="苏晚", font=("Microsoft YaHei UI", 20, "bold"), fg=ACCENT, bg=BG).pack(side=tk.LEFT)
        self.subtitle = self._label(bar, "住在你电脑里的数字生命", color=SUB, size=10)
        self.subtitle.pack(side=tk.LEFT, padx=(12, 0), pady=(3, 0))
        self.mode_lbl = self._label(bar, "", color=SUB, size=9)
        self.mode_lbl.pack(side=tk.RIGHT)

    # ---------- row1 主区 ----------
    def _build_notebook(self, row: int) -> None:
        nb = ttk.Notebook(self.root)
        nb.grid(row=row, column=0, sticky="nsew", padx=12)
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
        self._build_chat()
        self._build_memory()
        self._build_status()
        self._build_settings()
        self._build_works()
        self._build_story()
        self._build_log()

    def _build_chat(self) -> None:
        self.chat_area = self._scrolled(self._tab_chat)
        self.chat_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 6))
        tip = self._label(self._tab_chat, "回车发送 · /api on|off · /net on|off · /doc 主题 · /tts 文本", color="#5f6670", size=8)
        tip.pack(fill=tk.X, padx=12, pady=(0, 4))
        row = tk.Frame(self._tab_chat, bg=PANEL)
        row.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.entry_var = tk.StringVar()
        entry = tk.Entry(row, textvariable=self.entry_var, font=("Microsoft YaHei UI", 11), bg=FIELD, fg=FG,
                         insertbackground=FG, relief=tk.FLAT)
        entry.pack(fill=tk.X, side=tk.LEFT, expand=True, ipady=7, padx=(0, 8))
        entry.bind("<Return>", self.send)
        ttk.Button(row, text="发送", style="Accent.TButton", command=self.send).pack(side=tk.RIGHT)
        entry.focus_set()

    def _build_status(self) -> None:
        canvas = tk.Frame(self._tab_status, bg=PANEL)
        canvas.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)
        self.status_text = tk.StringVar(value="（读取中…）")
        tk.Label(canvas, textvariable=self.status_text, justify="left", anchor="nw", font=("Cascadia Mono", 10), fg=FG, bg=PANEL).pack(fill=tk.BOTH, expand=True)
        self.personality_text = tk.StringVar(value="人格说明：正在读取…")
        tk.Label(canvas, textvariable=self.personality_text, justify="left", anchor="w", wraplength=880,
                 font=("Microsoft YaHei UI", 9), fg=ACCENT, bg=PANEL).pack(fill=tk.X, pady=(8, 6))
        self._label(canvas, "关系变化（来源可在“记忆”页按 ID 查看）", color=SUB, bg=PANEL).pack(anchor="w")
        self.relation_details = self._scrolled(canvas)
        self.relation_details.configure(height=6, state="disabled")
        self.relation_details.pack(fill=tk.X, pady=(4, 0))
        self._label(canvas, "状态板为只读视图，每 2s 刷新", color="#5f6670", size=8).pack(anchor="w", pady=(8, 0))

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
        self._label(self._tab_memory, "每页100条活动记忆；列表可筛选类型，管理搜索包含生成内容与推测。", color=SUB).pack(anchor="w", padx=12)
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
        for label, callback in (("补充经历", self._memory_add), ("设置事实", self._memory_set), ("纠正选中记录", self._memory_correct), ("遗忘选中及派生", self._memory_forget)):
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
        ctl = tk.LabelFrame(self._tab_settings, text="连接与声音", bg=PANEL, fg=ACCENT,
                            font=("Microsoft YaHei UI", 9, "bold"), padx=14, pady=10)
        ctl.pack(fill=tk.X, padx=12, pady=(12, 6))

        row1 = tk.Frame(ctl, bg=PANEL)
        row1.pack(fill=tk.X, pady=4)
        # API 开关 = 圆点 + 文字（无 ✕/✗ 字形），点击切换
        self.api_var = tk.BooleanVar(value=False)
        api_group = tk.Frame(row1, bg=PANEL)
        api_group.pack(side=tk.LEFT)
        self.api_dot = tk.Canvas(api_group, width=18, height=18, bg=PANEL, highlightthickness=0)
        self.api_dot.pack(side=tk.LEFT)
        self._api_dot_oval = self.api_dot.create_oval(4, 4, 14, 14, fill="#5a5f66", outline="")
        api_lbl = tk.Label(api_group, text="API", bg=PANEL, fg=FG, font=("Microsoft YaHei UI", 9), cursor="hand2")
        api_lbl.pack(side=tk.LEFT, padx=(4, 0))
        for widget in (self.api_dot, api_lbl):
            widget.bind("<Button-1>", self._toggle_api)
        # 联网：同款圆点开关
        self.net_var = tk.BooleanVar(value=False)
        net_group = self._add_dot_toggle(row1, self.net_var, "联网", "#5aa7ff", self._on_net)
        net_group.pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(row1, text="⚡ 测试连接", style="Accent.TButton", command=self._test_api).pack(side=tk.RIGHT)

        row2 = tk.Frame(ctl, bg=PANEL)
        row2.pack(fill=tk.X, pady=4)
        self._label(row2, "模型", color=SUB).pack(side=tk.LEFT)
        provider_labels = [PROVIDER_LABELS[p] for p in ENDPOINT_ENV]
        self.provider_var = tk.StringVar()
        self.provider_cb = ttk.Combobox(row2, textvariable=self.provider_var, values=provider_labels, state="readonly", width=20)
        self.provider_cb.pack(side=tk.LEFT, padx=(8, 4))
        self.provider_cb.bind("<<ComboboxSelected>>", self._on_provider_selected)
        self._label(row2, "具体模型", color=SUB).pack(side=tk.LEFT, padx=(12, 0))
        self.model_var = tk.StringVar()
        self.model_cb = ttk.Combobox(row2, textvariable=self.model_var, state="readonly", width=26)
        self.model_cb.pack(side=tk.LEFT, padx=(8, 0))
        self.model_cb.bind("<<ComboboxSelected>>", self._on_model_selected)

        row3 = tk.Frame(ctl, bg=PANEL)
        row3.pack(fill=tk.X, pady=(8, 4))
        self._label(row3, "音色", color=SUB).pack(side=tk.LEFT)
        self.voice_var = tk.StringVar(value=VOICE_PRESETS[0])
        voice_cb = ttk.Combobox(row3, textvariable=self.voice_var, values=list(VOICE_PRESETS), state="readonly", width=16)
        voice_cb.pack(side=tk.LEFT, padx=(8, 4))
        voice_cb.bind("<<ComboboxSelected>>", self._on_voice_selected)
        self.read_var = tk.BooleanVar(value=False)
        read_group = self._add_dot_toggle(row3, self.read_var, "自动朗读", "#c792ea", self._on_auto_read)
        read_group.pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(row3, text="🔊 朗读上句", command=self._speak_last).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(row3, text="停止朗读", command=self._stop_speaking).pack(side=tk.LEFT, padx=(6, 0))
        self._label(row3, "（朗读需要 MiMo 的 MIMO_API_KEY；模型候选可用 MODEL_XX 追加）", color="#5f6670", size=8).pack(side=tk.LEFT, padx=(16, 0))

        self._label(self._tab_settings,
                    "提示：.env 的 MODEL_<供应商>（如 MODEL_OPENAI=gpt-4o）会作为该供应商的默认模型；切换供应商/模型即时生效。",
                    color="#5f6670", size=9).pack(anchor="w", padx=14, pady=(4, 0))
        preferences = tk.LabelFrame(self._tab_settings, text="交流偏好", bg=PANEL, fg=ACCENT,
                                    font=("Microsoft YaHei UI", 9, "bold"), padx=14, pady=10)
        preferences.pack(fill=tk.X, padx=12, pady=(16, 6))
        row = tk.Frame(preferences, bg=PANEL)
        row.pack(fill=tk.X, pady=4)
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
        tk.Label(preferences, textvariable=self.interaction_state, justify="left", anchor="w", wraplength=850,
                 bg=PANEL, fg=ACCENT, font=("Microsoft YaHei UI", 9)).pack(fill=tk.X, pady=(8, 4))
        self._label(preferences, "选择后立即保存，重启后保持。停止主动消息时，你发来的消息仍会正常回应。",
                    color=SUB, bg=PANEL, size=8).pack(anchor="w")

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
        self.personality_text.set("人格说明：正在读取…")
        self.relation_details.configure(state="normal")
        self.relation_details.delete("1.0", tk.END)
        self.relation_details.configure(state="disabled")
        self._request_interaction()

    def _toggle_api(self, _event=None) -> None:
        if self._closing:
            return
        self.api_var.set(not self.api_var.get())
        self._on_api()

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

    def _refresh_model_options(self, provider: str | None) -> None:
        if not provider:
            return
        runtime_model = getattr(self.server.runtime.llm, "model", "")
        options = list(MODEL_CANDIDATES.get(provider, []))
        if runtime_model and runtime_model not in options:
            options.insert(0, runtime_model)
        elif not options and runtime_model:
            options = [runtime_model]
        self.model_cb.configure(values=options)
        self.model_var.set(runtime_model if runtime_model in options else (options[0] if options else ""))

    # ---------- 作品/故事 ----------
    def _build_works(self) -> None:
        self.works_list = tk.Listbox(self._tab_works, bg="#121317", fg=FG, font=("Microsoft YaHei UI", 10),
                                     relief=tk.FLAT, highlightthickness=0, selectbackground="#3a4048")
        self.works_list.pack(fill=tk.X, padx=10, pady=(10, 4), ipady=4)
        self.works_list.bind("<<ListboxSelect>>", self._on_works_select)
        self.works_preview = self._scrolled(self._tab_works)
        self.works_preview.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    def _build_story(self) -> None:
        self.story_area = self._scrolled(self._tab_story, fg="#e6c9a0")
        self.story_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

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
        self.log_area = self._scrolled(self._tab_log, bg="#0f1012", fg="#c8ccd2")
        self.log_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    def _matches_filter(self, level: str) -> bool:
        current = self.filter_var.get() if hasattr(self, "filter_var") else "ALL"
        return current == "ALL" or current == level.upper()

    def _clear_log(self) -> None:
        self._log_entries.clear()
        self._render_log()

    # ---------- row2 状态栏 ----------
    def _build_statusbar(self, row: int) -> None:
        bar = tk.Frame(self.root, bg="#1a1c21")
        bar.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 10), ipady=3)
        self.api_state_lbl = tk.Label(bar, text="● API 未开启", font=("Microsoft YaHei UI", 9, "bold"), bg="#1a1c21", fg="#5a5f66")
        self.api_state_lbl.pack(side=tk.LEFT, padx=10)
        self.voice_state_lbl = tk.Label(bar, text="音色：mimo_default", bg="#1a1c21", fg=SUB, font=("Microsoft YaHei UI", 8))
        self.voice_state_lbl.pack(side=tk.LEFT, padx=8)
        self.hint_lbl = tk.Label(bar, text="设置在“设置”页 · 日志在“系统日志”页", bg="#1a1c21", fg="#5f6670", font=("Microsoft YaHei UI", 8))
        self.hint_lbl.pack(side=tk.RIGHT, padx=10)

    # ---------- 输出分流 ----------
    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _show_chat(self, sender: str, msg: str) -> None:
        self.chat_area.configure(state="normal")
        self.chat_area.tag_config(sender, foreground=COLORS.get(sender, FG))
        self.chat_area.insert(tk.END, f"{sender}：{msg}\n", sender)
        self.chat_area.see(tk.END)
        self.chat_area.configure(state="disabled")

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
        self.server.set_provider(provider)
        runtime_model = getattr(self.server.runtime.llm, "model", "")
        self._log_msg(f"模型供应商切换为 {PROVIDER_LABELS[provider]}", "info")
        self._refresh_model_options(provider)
        if runtime_model:
            self.model_var.set(runtime_model)
        self._refresh_api_indicator()

    def _on_model_selected(self, _event=None) -> None:
        model = self.model_var.get().strip()
        provider = self._current_provider()
        if not model or not provider:
            return
        self.server.set_model(model)
        self._log_msg(f"具体模型切换为 {provider}/{model}", "info")
        self._refresh_api_indicator()

    def _on_api(self) -> None:
        self.server.set_api(self.api_var.get())
        self._refresh_api_indicator()
        if self.api_var.get():
            self._log_msg("已开启 API，正在自动探测连接…", "info")
            self._after(500, self._test_api)
        else:
            self._stop_speaking()
            self._log_msg("已关闭 API，将使用本地模式", "warn")

    def _on_net(self) -> None:
        self.server.set_network(self.net_var.get())
        self._log_msg("联网已开启" if self.net_var.get() else "联网已关闭", "info")

    def _on_voice_selected(self, _event=None) -> None:
        self.server.set_tts_voice(self.voice_var.get())
        self._log_msg(f"音色已切换：{self.voice_var.get()}", "info")

    def send(self, _event=None) -> None:
        if self._closing:
            return
        text = self.entry_var.get().strip()
        if not text:
            return
        self.entry_var.set("")
        self._show_chat("你", text)
        if self._route_command(text):
            return
        future = self.server.ask_with_context(text)
        future.add_done_callback(self._on_reply_done)

    def _route_command(self, text: str) -> bool:
        command, _, argument = text.partition(" ")
        if command in {"/api", "/net"}:
            value = argument.strip().lower()
            if value not in {"on", "off", "1", "0", "true", "false"}:
                self._log_msg(f"用法：{command} on|off", "warn")
                return True
            enabled = value in {"on", "1", "true"}
            if command == "/api":
                self.api_var.set(enabled)
                self.server.set_api(enabled)
                if not enabled:
                    self._stop_speaking()
                self._refresh_api_indicator()
            else:
                self.net_var.set(enabled)
                self.server.set_network(enabled)
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

    def _on_reply_done(self, future) -> None:
        try:
            result = future.result(timeout=0.1)
            if not isinstance(result, dict) or "text" not in result or "epoch" not in result:
                raise ValueError("回复缺少上下文版本")
        except Exception as exc:  # noqa: BLE001
            message = f"回复失败：{type(exc).__name__} {str(exc)[:80]}"
            self._queue_ui(self._log_msg, message, "error")
            return
        self._queue_ui(self._render_reply, result["text"], result["epoch"])

    def _render_reply(self, reply: str, epoch) -> None:
        if self._closing or not self.server.context_current(epoch):
            return
        self._sync_ui_context()
        self._last_reply = reply
        self._last_reply_epoch = epoch
        self._show_chat("苏晚", reply)
        if self.read_var.get() and reply:
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
        epoch = self.server.context_epoch() if expected_epoch is None else expected_epoch
        if not self.server.context_current(epoch):
            return
        self._stop_speaking()
        generation = self._speech_generation
        voice = self.voice_var.get() or VOICE_PRESETS[0]
        future = self.server.speak(text, voice=voice, expected_epoch=epoch)
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
                    on_started=lambda: self._queue_ui(self._audio_feedback, generation, f"正在朗读：{Path(path).name}", "ok", epoch),
                    on_error=lambda message: self._queue_ui(self._audio_feedback, generation, f"播放失败：{message}", "error", epoch),
                )
            except Exception as exc:
                self._log_msg(f"播放失败：{exc}", "error")

    def _audio_feedback(self, generation: int, message: str, level: str, epoch=None) -> None:
        if not self._closing and generation == self._speech_generation and epoch is not None and self.server.context_current(epoch):
            self._log_msg(message, level)

    # ---------- API 指示 ----------
    def _set_api_dot_color(self, color: str) -> None:
        if hasattr(self, "api_dot"):
            self.api_dot.itemconfig(self._api_dot_oval, fill=color)

    def _refresh_api_indicator(self) -> None:
        try:
            state, msg = self.server.runtime.api_indicator()
        except Exception:  # noqa: BLE001
            state, msg = None, "状态未知"
        if state is True:
            self.api_state_lbl.configure(text=f"● 已连接 · {msg}", fg=OK)
            self._set_api_dot_color(OK)
        elif state is False:
            self.api_state_lbl.configure(text=f"● {msg}", fg=ERR)
            self._set_api_dot_color(ERR)
        else:
            self.api_state_lbl.configure(text=f"● {msg}", fg=WARN)
            self._set_api_dot_color(WARN)
        if not self.api_var.get():
            self.api_state_lbl.configure(text="● API 未开启", fg="#5a5f66")
            self._set_api_dot_color("#5a5f66")
        self.voice_state_lbl.configure(text=f"音色：{self.voice_var.get()}")
        provider = getattr(self.server.runtime.llm, "provider", "")
        model = getattr(self.server.runtime.llm, "model", "")
        self.mode_lbl.configure(text=f"{PROVIDER_LABELS.get(provider, provider)} / {model}")

    def _test_api(self) -> None:
        if self._closing:
            return
        self._api_probe_generation += 1
        context = self._api_probe_context()
        self.api_state_lbl.configure(text="● 测试中…", fg=WARN)
        self._set_api_dot_color(WARN)
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
        self._render_api_result(ok, msg)

    def _render_api_result(self, ok: bool, msg: str) -> None:
        self._log_msg(msg, "ok" if ok else "error")
        if ok:
            self.api_state_lbl.configure(text=f"● 已连接 · {msg}", fg=OK)
            self._set_api_dot_color(OK)
        else:
            self.api_state_lbl.configure(text=f"● 连接失败 · {msg}", fg=ERR)
            self._set_api_dot_color(ERR)

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
        items = self.server.bridge.drain()
        allows_notification = getattr(self.server.bridge, "allows_notification", None)
        commit_notification = getattr(self.server.bridge, "commit_notification", None)
        # Check the runtime directly as well: a reset notification may arrive late.
        self._sync_ui_context()
        while True:
            try:
                callback, args = self._ui_callbacks.get_nowait()
            except queue.Empty:
                break
            callback(*args)
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
        self._after(100, self._pump)

    def _sync_ui_context(self) -> None:
        epoch = self.server.context_epoch()
        if epoch == self._ui_context_epoch:
            return
        self._ui_context_epoch = epoch
        self._last_reply, self._last_reply_epoch = "", None
        self._stop_speaking()
        self._clear_interaction_view()
        self.chat_area.configure(state="normal")
        self.chat_area.delete("1.0", tk.END)
        self.chat_area.configure(state="disabled")
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
        self._refresh_files()
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
            self._sync_toggle_dots()  # 联网/自动朗读圆点颜色跟随（每 2s 刷新）
        except Exception as exc:  # noqa: BLE001
            self.status_text.set(f"（状态读取失败：{type(exc).__name__} {str(exc)[:60]}）")

    def _refresh_files(self) -> None:
        try:
            data_dir = self.server.runtime.settings.data_dir
            works = sorted((data_dir / "作品集").glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            names = [w.name for w in works[:40]]
            current = list(self.works_list.get(0, tk.END))
            if names != current:
                self.works_list.delete(0, tk.END)
                for n in names:
                    self.works_list.insert(tk.END, n)
                if names and not self.works_list.curselection():
                    self.works_list.selection_set(0)
                    self._preview_works(works[0])
            story = data_dir / "story.txt"
            if story.exists():
                lines = story.read_text(encoding="utf-8").splitlines()[-60:]
                self.story_area.configure(state="normal")
                self.story_area.delete("1.0", tk.END)
                self.story_area.insert(tk.END, "\n".join(lines))
                self.story_area.configure(state="disabled")
        except Exception as exc:  # noqa: BLE001
            self._log_msg(f"文件视图异常：{type(exc).__name__} {str(exc)[:60]}", "warn")

    def _on_works_select(self, _event=None) -> None:
        sel = self.works_list.curselection()
        if not sel:
            return
        name = self.works_list.get(sel[0])
        path = self.server.runtime.settings.data_dir / "作品集" / name
        if path.exists():
            self._preview_works(path)

    def _preview_works(self, path) -> None:
        try:
            content = path.read_text(encoding="utf-8")[:2000]
        except OSError:
            content = "（读取失败）"
        self.works_preview.configure(state="normal")
        self.works_preview.delete("1.0", tk.END)
        self.works_preview.insert(tk.END, content)
        self.works_preview.configure(state="disabled")

    # ---------- 关闭 ----------
    def on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop_speaking()
        self._playback.stop(close=True)
        for after_id in self._after_ids:
            self.root.after_cancel(after_id)
        self._after_ids.clear()
        self._disable_inputs(self.root)
        self._close_started = time.monotonic()
        self.server.request_stop()
        self.root.title("苏晚 · 正在保存并退出")
        self._poll_close()

    def _disable_inputs(self, parent) -> None:
        for widget in parent.winfo_children():
            if isinstance(widget, (tk.Entry, ttk.Button, ttk.Combobox, ttk.Radiobutton, ttk.Checkbutton)):
                widget.configure(state="disabled")
            self._disable_inputs(widget)

    def _poll_close(self) -> None:
        state = self.server.shutdown_status()
        if state["finished"] and self._playback.idle:
            if state["error"] is not None:
                messagebox.showerror("退出保存未完成", f"{state['error']}\n\n请保留现有数据与备份，检查问题后再启动。", parent=self.root)
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
