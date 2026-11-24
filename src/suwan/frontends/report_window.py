"""Desktop controls for local report tasks and explicit model authorization."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit
from uuid import uuid4

from suwan.config import PROVIDER_LABELS
from suwan.frontends.warm_theme import ACCENT, BG, FG, FIELD, FONT, PANEL, SUB


_STATES = {
    "ready": "待处理",
    "running": "处理中",
    "paused": "已暂停",
    "waiting_approval": "待授权",
    "unknown": "调用结果待确认",
    "failed": "未完成",
    "cancelled": "已取消",
    "succeeded": "已完成",
}
_REASONS = {
    "model_unavailable": "模型不可用，请到连接设置检查 API。",
    "authorization_changed": "模型连接已变化，请核对当前模型并重新确认。",
    "call_unknown": "上次请求可能已发送，结果还未确认。",
    "call_complete_result_missing": "上次调用已完成，结果正文未保存。",
    "interrupted_step": "上次处理在保存结果前中断，请核对后再试。",
    "invalid_model_result": "模型结果的格式或引用未通过检查。",
    "report_facts_need_review": "生成结果与已提取的数据不一致，请核对后重试。",
    "prompt_too_long": "资料或已提取的结果超出当前处理上限。",
    "budget_exhausted": "本日模型调用额度已用完。",
    "model_failed": "模型请求未完成，请检查连接后继续。",
    "storage_unavailable": "本地任务记录暂时无法读写。",
    "storage_busy": "本地任务记录正忙，任务还未开始，请稍后再试。",
    "storage_recovery_pending": "正在核对中断的任务，请稍后查看。",
    "task_integrity": "这条任务的本地记录不完整，无法继续处理。",
    "invalid_report_input": "无法保存这些资料。请选择可读取的 UTF-8 文本，检查数量、大小和报告要求。",
    "report_closed": "苏晚正在退出。",
    "report_busy": "已有一份资料正在处理，请稍后再试。",
    "resume_not_allowed": "任务状态已变化，请查看最新进度。",
    "artifact_invalid": "报告已丢失或内容与登记记录不符。",
    "artifact_conflict": "报告位置已有其他文件，请保留文件后检查。",
    "artifact_write_failed": "报告暂时无法写入，请检查输出目录。",
    "source_changed": "这份资料已变更，请重新选择资料并保存为新任务。",
    "source_unreadable": "这份资料无法读取，请检查文件是否仍在原位置。",
    "source_result_discarded": "模型已返回，但资料随后变更或无法读取。本次结果已丢弃，请核对资料后再确认是否重试。",
    "paused_by_user": "已在检查点暂停。",
    "interrupted_publish": "上次退出时发布尚未完成，可继续核对产物。",
    "cancelled_by_user": "任务已取消。",
}
_TASK_PAGE_SIZE = 100


def _destination_text(destination: dict) -> str:
    provider = destination.get("provider", "")
    model = destination.get("model", "")
    return f"{PROVIDER_LABELS.get(provider, provider)} / {model}"


def _endpoint_details(endpoint: str) -> tuple[str, bool]:
    """Describe the target without placing URL credentials in the dialog."""
    url = urlsplit(endpoint)
    hostname = url.hostname or ""
    if url.scheme not in {"http", "https"} or not hostname:
        raise ValueError("invalid model endpoint")
    try:
        local = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        local = hostname.lower() == "localhost"
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = f":{url.port}" if url.port is not None else ""
    return f"{url.scheme}://{host}{port}{url.path}", local


class ReportWindow(tk.Toplevel):
    def __init__(self, app) -> None:
        super().__init__(app.root)
        self._app = app
        self._disposed = False
        self._poll_id: str | None = None
        self._view_pending = False
        self._operation_pending = False
        self._generation = 0
        self._paths: list[str] = []
        self._rows: dict[str, dict] = {}
        self._task_page = 0
        self._task_ids: list[str] = []
        self._visible_values: dict[str, tuple[str, str, str]] = {}
        self._draft_request: tuple[tuple[str, ...], str, str] | None = None
        self._preferred_id: str | None = None
        self._destination: dict = {}
        self._api_ready = False
        self._storage_ready = False

        self.title("苏晚 · 整理资料")
        self.minsize(760, 520)
        self.geometry("920x590")
        self.configure(bg=BG)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)
        self._destination_label = tk.StringVar(master=self, value="正在读取模型连接…")
        self._status = tk.StringVar(master=self)
        self._detail = tk.StringVar(master=self)
        self._content = tk.StringVar(master=self)

        title = ttk.Frame(self, padding=(20, 15, 20, 10))
        title.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(title, text="整理资料", font=(FONT, 16, "bold")).pack(side="left")
        ttk.Label(title, textvariable=self._destination_label, style="Muted.TLabel",
                  wraplength=520).pack(side="left", padx=(16, 0))
        self._build_form()
        self._build_tasks()
        status = ttk.Label(self, textvariable=self._status, style="Muted.TLabel", wraplength=680, justify="left")
        status.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(7, 12))
        self._sync_controls()
        self._refresh()

    def _build_form(self) -> None:
        form = ttk.Frame(self, style="Card.TFrame", padding=12)
        form.grid(row=1, column=0, sticky="nsew", padx=(20, 8))
        form.columnconfigure(0, weight=1)
        form.rowconfigure(3, weight=1)
        ttk.Label(form, text="新任务", style="Card.TLabel", font=(FONT, 11, "bold")).grid(row=0, sticky="w")
        ttk.Label(form, text="选择 TXT 或 Markdown", style="Card.TLabel").grid(row=1, sticky="w", pady=(9, 2))
        tools = ttk.Frame(form, style="Card.TFrame")
        tools.grid(row=2, sticky="ew", pady=(0, 6))
        self.add_button = ttk.Button(tools, text="添加资料", command=self._choose_sources)
        self.add_button.pack(side="left")
        self.remove_button = ttk.Button(tools, text="移除选中", command=self._remove_sources)
        self.remove_button.pack(side="left", padx=(5, 0))
        source_frame = ttk.Frame(form)
        source_frame.grid(row=3, sticky="nsew")
        source_frame.columnconfigure(0, weight=1)
        source_frame.rowconfigure(0, weight=1)
        self.source_list = tk.Listbox(
            source_frame, width=25, height=6, selectmode="extended", exportselection=False, bg=FIELD, fg=FG,
            font=(FONT, 9), relief="flat", highlightthickness=1, highlightbackground=FIELD,
        )
        self.source_list.grid(row=0, column=0, sticky="nsew")
        vert = ttk.Scrollbar(source_frame, orient="vertical", command=self.source_list.yview)
        horiz = ttk.Scrollbar(source_frame, orient="horizontal", command=self.source_list.xview)
        vert.grid(row=0, column=1, sticky="ns")
        horiz.grid(row=1, column=0, sticky="ew")
        self.source_list.configure(yscrollcommand=vert.set, xscrollcommand=horiz.set)
        self.source_list.bind("<<ListboxSelect>>", lambda _event: self._sync_controls())

        ttk.Label(form, text="报告要求", style="Card.TLabel").grid(row=4, sticky="w", pady=(10, 4))
        self.instruction = tk.Text(
            form, width=25, height=5, wrap="word", bg=FIELD, fg=FG, insertbackground=FG,
            relief="flat", padx=7, pady=6, font=(FONT, 10), highlightthickness=1,
            highlightbackground=FIELD, highlightcolor=ACCENT,
        )
        self.instruction.grid(row=5, sticky="ew")
        self.save_button = ttk.Button(form, text="保存到本机", style="Accent.TButton", command=self._save)
        self.save_button.grid(row=6, sticky="w", pady=(10, 0))

    def _build_tasks(self) -> None:
        panel = ttk.Frame(self, style="Card.TFrame", padding=12)
        panel.grid(row=1, column=1, sticky="nsew", padx=(8, 20))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        task_header = ttk.Frame(panel, style="Card.TFrame")
        task_header.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(task_header, text="任务", style="Card.TLabel", font=(FONT, 11, "bold")).pack(side="left")
        self._task_pagination = ttk.Frame(task_header, style="Card.TFrame")
        self._task_page_label = ttk.Label(self._task_pagination, style="Card.TLabel")
        self._tasks_prev = ttk.Button(
            self._task_pagination, text="上一页", width=6, command=lambda: self._change_task_page(-1),
        )
        self._tasks_next = ttk.Button(
            self._task_pagination, text="下一页", width=6, command=lambda: self._change_task_page(1),
        )
        self._tasks_prev.pack(side="left", padx=(0, 4))
        self._task_page_label.pack(side="left", padx=4)
        self._tasks_next.pack(side="left", padx=(4, 0))
        self.tree = ttk.Treeview(
            panel, columns=("instruction", "state", "progress"), show="headings",
            height=6, selectmode="browse",
        )
        for column, title, width in (("instruction", "报告要求", 156), ("state", "状态", 111), ("progress", "进度", 59)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=46, stretch=column == "instruction")
        self.tree.grid(row=1, column=0, sticky="nsew", pady=(8, 7))
        scrollbar = ttk.Scrollbar(panel, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns", pady=(8, 7))
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._show_selection())

        labels = ttk.Frame(panel, style="Card.TFrame")
        labels.grid(row=2, column=0, columnspan=2, sticky="ew")
        self._detail_label = ttk.Label(labels, textvariable=self._detail, style="Card.TLabel", justify="left")
        self._detail_label.pack(fill="x", pady=(0, 3))
        self._content_label = ttk.Label(labels, textvariable=self._content, style="Card.TLabel", justify="left")
        self._content_label.pack(fill="x", pady=(0, 5))
        labels.bind("<Configure>", self._wrap_details)
        self.progress = ttk.Progressbar(panel, mode="determinate", maximum=1)
        self.progress.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 9))

        actions = ttk.Frame(panel, style="Card.TFrame")
        actions.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.start_button = ttk.Button(actions, text="开始", command=self._confirm_start)
        self.pause_button = ttk.Button(actions, text="暂停", command=lambda: self._control("pause"))
        self.cancel_button = ttk.Button(actions, text="取消任务", command=self._confirm_cancel)
        self.open_button = ttk.Button(actions, text="打开报告", command=lambda: self._control("open"))
        for button in (self.start_button, self.pause_button, self.cancel_button, self.open_button):
            button.pack(side="left", padx=(0, 4))
        self._show_selection()

    def _wrap_details(self, event) -> None:
        length = max(140, event.width - 6)
        self._detail_label.configure(wraplength=length)
        self._content_label.configure(wraplength=length)

    def _choose_sources(self) -> None:
        chosen = filedialog.askopenfilenames(
            parent=self,
            title="选择要整理的资料",
            filetypes=[("TXT 和 Markdown", ("*.txt", "*.md", "*.markdown"))],
        )
        if chosen and self._alive():
            paths = list(dict.fromkeys([*self._paths, *chosen]))
            if len(paths) > 12:
                self._status.set("每个任务最多选择 12 份资料。")
                return
            self._paths = paths
            self._render_sources()

    def _remove_sources(self) -> None:
        for index in reversed(self.source_list.curselection()):
            del self._paths[index]
        self._render_sources()

    def _render_sources(self) -> None:
        self.source_list.delete(0, tk.END)
        for path in self._paths:
            self.source_list.insert(tk.END, path)
        self._sync_controls()

    def _selected(self) -> dict | None:
        selected = self.tree.selection()
        return self._rows.get(selected[0]) if selected else None

    def _state_text(self, row: dict) -> str:
        if row.get("pause_requested"):
            return "正在暂停"
        if row.get("quality") == "evidence_only" and row["state"] == "succeeded":
            return "已整理证据"
        return _STATES.get(row["state"], "待查看")

    def _show_selection(self) -> None:
        row = self._selected()
        if row is None:
            self._detail.set("目前没有选中的任务。" if self._rows else "保存资料后，可在这里查看进度。")
            self._content.set("")
            self.progress.configure(value=0, maximum=1)
        else:
            names = {source["id"]: source["name"] for source in row["sources"]}
            names_text = f"{len(names)} 份资料"
            for source_id, name in list(names.items())[:2]:
                preview = name if len(name) <= 34 else name[:17] + "…" + name[-16:]
                label = f"{source_id} {preview}" if len(names) > 1 else preview
                names_text += f" · {label}"
            reason = _REASONS.get(row.get("reason"))
            if row.get("quality") == "evidence_only" and row["state"] == "succeeded":
                reason = "已整理可核对的来源证据。完整模型汇总超出处理上限。"
            if row.get("pause_requested"):
                reason = "当前步骤收尾后暂停。"
            source = names.get(row.get("source_id"))
            if source and len(source) > 60:
                source = source[:59] + "…"
            status = f"{self._state_text(row)} · {row['done']} / {row['total']} 步已保存"
            if reason:
                status += f"\n{source + ' · ' if source else ''}{reason}"
            self._detail.set(status)
            self._content.set(names_text)
            self.progress.configure(value=row["done"], maximum=max(1, row["total"]))
        self._sync_controls()

    def _sync_controls(self) -> None:
        row = self._selected() or {}
        state = row.get("state")
        available = not self._operation_pending
        self.add_button.configure(state="normal" if available else "disabled")
        self.remove_button.configure(state="normal" if available and self.source_list.curselection() else "disabled")
        self.instruction.configure(state="normal" if available else "disabled")
        self.save_button.configure(state="normal" if available and self._storage_ready and self._paths else "disabled")
        text = "重试" if state == "unknown" else "继续" if state in {"paused", "waiting_approval"} or row.get("done") else "开始"
        self.start_button.configure(
            text=text,
            state="normal" if self._can_control("retry" if state == "unknown" else "start", row) else "disabled",
        )
        for action, button in (("pause", self.pause_button), ("cancel", self.cancel_button), ("open", self.open_button)):
            button.configure(state="normal" if self._can_control(action, row) else "disabled")

    def _can_control(self, action: str, row: dict) -> bool:
        if not self._storage_ready or self._operation_pending:
            return False
        state = row.get("state")
        if action == "start":
            return (
                not row.get("pause_requested")
                and state in {"ready", "paused", "waiting_approval"}
                and (self._api_ready or bool(row.get("local_only")) and state in {"ready", "paused"})
            )
        if action == "retry":
            return self._api_ready and state == "unknown" and not row.get("pause_requested")
        if action == "pause":
            return state == "running" and not row.get("pause_requested")
        if action == "cancel":
            return state in {"ready", "paused", "unknown", "running", "waiting_approval"}
        return action == "open" and state == "succeeded"

    def _save(self) -> None:
        if self._operation_pending or not self._storage_ready or not self._paths:
            return
        instruction = self.instruction.get("1.0", "end-1c").strip()
        if not instruction:
            self._status.set("请先填写报告要求。")
            self.instruction.focus_set()
            return
        if len(instruction) > 4000:
            self._status.set("报告要求最多 4000 个字符。")
            return
        paths = tuple(self._paths)
        if self._draft_request is None or self._draft_request[:2] != (paths, instruction):
            self._draft_request = (paths, instruction, uuid4().hex)
        request_key = self._draft_request[2]
        self._begin_operation(
            lambda: self._app.server.report_submit(paths, instruction, request_key), self._saved,
            "正在保存到本机…", "任务暂时无法保存，请检查资料是否可读取。",
        )

    def _saved(self, result: dict) -> None:
        if not result.get("id"):
            self._status.set(_REASONS.get(result.get("reason"), "任务暂时无法保存。"))
            return
        self._preferred_id = result["id"]
        self._draft_request = None
        self._paths.clear()
        self._render_sources()
        self.instruction.delete("1.0", tk.END)
        self._status.set("任务已保存在本机。核对模型后即可开始。")

    def _confirm_start(self) -> None:
        row = self._selected()
        if row is None:
            return
        action = "retry" if row["state"] == "unknown" else "start"
        if not self._can_control(action, row):
            return
        if row.get("local_only") and row["state"] in {"ready", "paused"}:
            self._control("start", task_id=row["id"])
            return
        destination = self._destination.copy()
        try:
            address, local = _endpoint_details(destination["endpoint"])
        except (KeyError, ValueError):
            self._status.set("模型地址暂不可用，请检查连接设置。")
            return
        target = "本机模型服务" if local else "外部模型服务"
        caution = "\n\n上次请求可能已计费。重试会发起新的请求。" if action == "retry" else ""
        names = "\n".join(
            f"• {source['id']}  " + " ".join(source["name"].splitlines())
            for source in row["sources"]
        )
        message = (
            f"{target}  {_destination_text(destination)}\n{address}\n\n"
            "本次处理会向该服务提交报告要求、以下资料的内容及提炼结果。\n"
            f"{names}{caution}\n\n是否继续？"
        )
        confirmed = messagebox.askyesno("确认模型调用", message, parent=self, default=messagebox.NO)
        if self._alive() and confirmed:
            self._control(action, task_id=row["id"], destination=destination)

    def _confirm_cancel(self) -> None:
        row = self._selected()
        if row is None or not self._can_control("cancel", row):
            return
        confirmed = messagebox.askyesno(
            "取消任务", "取消后此任务结束，已发出的模型请求可能仍会计费。是否取消？",
            parent=self, default=messagebox.NO,
        )
        if self._alive() and confirmed:
            self._control("cancel", task_id=row["id"])

    def _control(self, action: str, *, task_id: str | None = None, destination: dict | None = None) -> None:
        row = self._selected() if task_id is None else self._rows.get(task_id)
        if row is None or not self._can_control(action, row):
            return
        task_id = row["id"]
        self._begin_operation(
            lambda: self._app.server.report_control(task_id, action, destination=destination),
            lambda result: self._controlled(action, result),
            "正在处理…", "操作暂时无法完成，请稍后再试。",
        )

    def _controlled(self, action: str, result: dict) -> None:
        if not result.get("ok"):
            self._status.set(_REASONS.get(result.get("reason"), "操作暂时无法完成，请查看任务状态。"))
        elif action == "open":
            path = result.get("path")
            if path:
                label = "报告位置"
                if result.get("reason") == "source_changed_since_publication":
                    label = "原资料已更新，打开的是历史报告"
                self._status.set(f"{label}  {path}")
                try:
                    threading.Thread(target=self._open_file, args=(path,), name="suwan-report-open", daemon=True).start()
                except (OSError, RuntimeError):
                    self._file_open_failed(path)
        else:
            self._status.set({
                "start": "任务已提交处理。",
                "retry": "已确认重试。",
                "pause": "已请求暂停。",
                "cancel": "已取消任务。",
            }[action])

    def _open_file(self, path: str) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(path)
            else:
                launcher = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([launcher, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self._app._queue_ui(self._file_open_failed, path)

    def _file_open_failed(self, path: str) -> None:
        if self._alive():
            self._status.set(f"系统暂时无法打开报告。可在文件管理器里打开  {Path(path)}")

    def _begin_operation(self, request, on_success, status: str, error: str) -> None:
        self._generation += 1
        self._operation_pending = True
        self._status.set(status)
        self._sync_controls()

        def done(result: dict | None) -> None:
            self._operation_pending = False
            self._sync_controls()
            try:
                if result is None:
                    self._status.set(error)
                else:
                    on_success(result)
            finally:
                self._refresh()

        self._request(request, done)

    def _request(self, request, callback) -> None:
        try:
            future = request()
        except Exception:
            callback(None)
            return

        def done(completed) -> None:
            try:
                result = completed.result()
            except Exception:
                result = None
            self._app._queue_ui(self._deliver, callback, result)

        future.add_done_callback(done)

    def _deliver(self, callback, result: dict | None) -> None:
        if self._alive():
            callback(result)

    def _refresh(self) -> None:
        if not self._alive() or self._view_pending or self._operation_pending:
            return
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        self._view_pending = True
        generation = self._generation
        self._request(self._app.server.report_view, lambda result: self._view_arrived(generation, result))

    def _view_arrived(self, generation: int, result: dict | None) -> None:
        self._view_pending = False
        if generation != self._generation:
            self._refresh()
            return
        try:
            if result is None:
                self._mark_view_unavailable("暂时无法读取任务和模型连接")
            else:
                self._render_view(result)
        finally:
            if self._alive():
                self._poll_id = self.after(900, self._poll)

    def _poll(self) -> None:
        self._poll_id = None
        self._refresh()

    def _render_view(self, result: dict) -> None:
        if result.get("reason"):
            self._mark_view_unavailable(_REASONS.get(result["reason"], "暂时无法读取任务"))
            return
        self._destination = result["destination"]
        self._api_ready = result["api_ready"]
        self._storage_ready = True
        if self._api_ready:
            self._destination_label.set("当前模型  " + _destination_text(self._destination))
        else:
            self._destination_label.set("模型暂不可用，仍可在本机保存资料")
        self._rows = {row["id"]: row for row in result["tasks"]}
        self._task_ids = list(self._rows)
        selected = self.tree.selection()
        preferred = self._preferred_id in self._rows
        anchor = selected[0] if selected else None
        if preferred:
            anchor = self._preferred_id
        if anchor in self._rows:
            self._task_page = self._task_ids.index(anchor) // _TASK_PAGE_SIZE
        self._render_task_page()
        if preferred:
            self.tree.selection_set(anchor)
            self.tree.see(anchor)
            self._preferred_id = None
        self._show_selection()

    def _mark_view_unavailable(self, text: str) -> None:
        self._api_ready = False
        self._storage_ready = False
        self._destination = {}
        self._destination_label.set(text)
        self._sync_controls()

    def _change_task_page(self, offset: int) -> None:
        self._task_page += offset
        self._render_task_page()
        self._show_selection()

    def _render_task_page(self) -> None:
        pages = max(1, (len(self._task_ids) + _TASK_PAGE_SIZE - 1) // _TASK_PAGE_SIZE)
        self._task_page = max(0, min(self._task_page, pages - 1))
        if pages > 1:
            if not self._task_pagination.winfo_manager():
                self._task_pagination.pack(side="right")
            self._task_page_label.configure(text=f"{self._task_page + 1} / {pages}")
            self._tasks_prev.configure(state="normal" if self._task_page > 0 else "disabled")
            self._tasks_next.configure(state="normal" if self._task_page + 1 < pages else "disabled")
        else:
            self._task_pagination.pack_forget()

        start = self._task_page * _TASK_PAGE_SIZE
        visible = self._task_ids[start:start + _TASK_PAGE_SIZE]
        values_by_id = {}
        for task_id in visible:
            row = self._rows[task_id]
            preview = " ".join(row["instruction"].split())
            values_by_id[task_id] = (preview[:80], self._state_text(row), f"{row['done']} / {row['total']}")
        old_values = self._visible_values
        removed = list(old_values.keys() - values_by_id.keys())
        if removed:
            self.tree.delete(*removed)
        for task_id, values in values_by_id.items():
            if task_id not in old_values:
                self.tree.insert("", "end", iid=task_id, values=values)
            elif old_values[task_id] != values:
                self.tree.item(task_id, values=values)
        if list(old_values) != visible:
            self.tree.set_children("", *visible)
        self._visible_values = values_by_id

    def _alive(self) -> bool:
        return not self._disposed and not self._app._closing

    def destroy(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        if self._app.report_window is self:
            self._app.report_window = None
        super().destroy()
