"""苏晚 v2 tkinter 前端（设置页 + 二级模型 + 日志过滤版）。

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
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import scrolledtext, ttk

from suwan.adapters.tts import VOICE_PRESETS
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
        self._log_msg("苏晚 v2 已启动 · API/联网/模型/音色等设置在“设置”页", "info")
        self._schedule_views()

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
        for key, text in (("chat", "对话"), ("status", "状态板"), ("settings", "设置"),
                          ("works", "作品集"), ("story", "人生故事"), ("log", "系统日志")):
            frame = tk.Frame(nb, bg=PANEL)
            nb.add(frame, text=text)
            tabs[key] = frame
        self._tab_chat, self._tab_status = tabs["chat"], tabs["status"]
        self._tab_settings = tabs["settings"]
        self._tab_works, self._tab_story, self._tab_log = tabs["works"], tabs["story"], tabs["log"]
        self._build_chat()
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
        self._label(canvas, "状态板为只读视图，每 2s 刷新", color="#5f6670", size=8).pack(anchor="w", pady=(8, 0))

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
        self.voice_var = tk.StringVar(value="mimo_default")
        voice_cb = ttk.Combobox(row3, textvariable=self.voice_var, values=list(VOICE_PRESETS), state="readonly", width=16)
        voice_cb.pack(side=tk.LEFT, padx=(8, 4))
        voice_cb.bind("<<ComboboxSelected>>", self._on_voice_selected)
        self.read_var = tk.BooleanVar(value=False)
        read_group = self._add_dot_toggle(row3, self.read_var, "自动朗读", "#c792ea", None)
        read_group.pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(row3, text="🔊 朗读上句", command=self._speak_last).pack(side=tk.LEFT, padx=(12, 0))
        self._label(row3, "（朗读需要 MiniMax 密钥；模型候选可用 .env 的 MODEL_XX 追加）", color="#5f6670", size=8).pack(side=tk.LEFT, padx=(16, 0))

        self._label(self._tab_settings,
                    "提示：.env 的 MODEL_<供应商>（如 MODEL_OPENAI=gpt-4o）会作为该供应商的默认模型；切换供应商/模型即时生效。",
                    color="#5f6670", size=9).pack(anchor="w", padx=14, pady=(4, 0))

    def _toggle_api(self, _event=None) -> None:
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
            self.root.after(500, self._test_api)
        else:
            self._log_msg("已关闭 API，将使用本地模式", "warn")

    def _on_net(self) -> None:
        self.server.set_network(self.net_var.get())
        self._log_msg("联网已开启" if self.net_var.get() else "联网已关闭", "info")

    def _on_voice_selected(self, _event=None) -> None:
        self.server.set_tts_voice(self.voice_var.get())
        self._log_msg(f"音色已切换：{self.voice_var.get()}", "info")

    def send(self, _event=None) -> None:
        text = self.entry_var.get().strip()
        if not text:
            return
        self.entry_var.set("")
        self._show_chat("你", text)
        future = self.server.ask(text)
        future.add_done_callback(self._on_reply_done)

    def _on_reply_done(self, future) -> None:
        try:
            reply = future.result(timeout=0.1)
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda: self._log_msg(f"回复失败：{type(exc).__name__} {str(exc)[:80]}", "error"))
            return
        self.root.after(0, lambda: self._render_reply(reply))

    def _render_reply(self, reply: str) -> None:
        self._last_reply = reply
        self._show_chat("苏晚", reply)
        if self.read_var.get() and reply:
            self._speak(reply)

    def _speak_last(self) -> None:
        if not self._last_reply:
            self._log_msg("还没有可朗读的回复", "warn")
            return
        self._speak(self._last_reply)

    def _speak(self, text: str) -> None:
        voice = self.voice_var.get() or "mimo_default"
        future = self.server.speak(text, voice=voice)

        def done(fut) -> None:
            try:
                path, error = fut.result(timeout=0.1)
            except Exception as exc:  # noqa: BLE001
                path, error = None, f"{type(exc).__name__}: {str(exc)[:80]}"
            if error:
                self.root.after(0, lambda: self._log_msg(f"语音失败：{error}", "error"))
            elif path:
                self.root.after(0, lambda: self._log_msg(f"语音已生成：{Path(path).name}", "ok"))

        future.add_done_callback(done)

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
        self.api_state_lbl.configure(text="● 测试中…", fg=WARN)
        self._set_api_dot_color(WARN)
        future = self.server.api_check()
        future.add_done_callback(self._on_api_check_done)

    def _on_api_check_done(self, future) -> None:
        try:
            ok, msg = future.result(timeout=0.1)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"{type(exc).__name__}: {str(exc)[:80]}"
        self.root.after(0, lambda: self._render_api_result(ok, msg))

    def _render_api_result(self, ok: bool, msg: str) -> None:
        self._log_msg(msg, "ok" if ok else "error")
        if ok:
            self.api_state_lbl.configure(text=f"● 已连接 · {msg}", fg=OK)
            self._set_api_dot_color(OK)
        else:
            self.api_state_lbl.configure(text=f"● 连接失败 · {msg}", fg=ERR)
            self._set_api_dot_color(ERR)

    # ---------- 总线事件拉取 ----------
    def _pump(self) -> None:
        for item in self.server.bridge.drain():
            kind, data = item["kind"], item["data"]
            text = data.get("text", data.get("name", ""))
            if kind == "doc":
                self._log_msg(f"文档已生成：{text}", "ok")
            elif kind == "toast":
                self._log_msg(text, "info")
            elif kind == "inner":
                self._show_chat("独白", f"（{text}）")
            elif kind == "narrative":
                self._show_chat("苏晚·主动", text)
        self.root.after(100, self._pump)

    # ---------- 轮询视图 ----------
    def _schedule_views(self) -> None:
        self._refresh_status()
        self._refresh_files()
        self.root.after(2000, self._schedule_views)

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
        try:
            self.server.stop()
        finally:
            self.root.destroy()

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
