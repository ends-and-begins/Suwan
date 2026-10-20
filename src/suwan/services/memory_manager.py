"""同一运行时循环内的记忆管理入口；记忆文件是事实权威。"""
from __future__ import annotations

import re

from suwan.organs.memory import KINDS
from suwan.interaction import validate_preference

PAGE_SIZE = 100


class MemoryManager:
    def __init__(self, memory, identity, sensory, *, persist_state=None, on_change=None):
        self.memory, self.identity, self.sensory = memory, identity, sensory
        self.persist_state, self.on_change = persist_state, on_change
        self.epoch = 0

    def initialise(self) -> None:
        self.memory.bootstrap_identity(self.identity.user_name)
        self.project_identity()
        # 旧快照的独白没有来源关系，不能证明它早于哪次纠错/遗忘。
        # 保留带来源的长期记忆，启动时丢弃这层短期派生缓存。
        self.identity.clear_monologues()

    def project_identity(self) -> None:
        fact = self.memory.get_fact("user.name")
        self.identity.register_name(fact.fact_value if fact else None)

    def reset_context(self) -> None:
        """Revoke transient derivatives without changing durable source facts."""
        self.epoch += 1
        self.sensory.clear()
        self.identity.clear_monologues()

    def changed(self, *, clear_context=True) -> str | None:
        if clear_context:
            self.reset_context()
        self.project_identity()
        if self.on_change:
            self.on_change(clear_context=clear_context)
        if self.persist_state:
            try:
                self.persist_state()
            except (OSError, ValueError) as exc:
                return f"记忆已提交；会话快照保存失败（{type(exc).__name__}），重启将从记忆恢复事实。"
        return None

    @staticmethod
    def _row(item) -> dict:
        return {"id": item.id, "kind": item.kind, "source": item.source,
                "source_ref": item.source_ref, "content": item.content,
                "created": item.created.isoformat() if item.created else None,
                "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None,
                "fact_key": item.fact_key, "fact_value": item.fact_value,
                "superseded_by": item.superseded_by,
                "derived_from": list(item.derived_from)}

    def execute(self, command: str) -> dict:
        """命令和 GUI 共享此入口，不使用模型解析隐私操作。"""
        text = str(command).strip()
        if text == "/memory":
            text += " help"
        if not text.startswith("/memory "):
            return {"ok": False, "text": "无效记忆命令。"}
        action, _, arg = text[len("/memory "):].strip().partition(" ")
        arg = arg.strip()
        result = {"ok": True, "action": action}
        changed = False
        try:
            if action in {"help", "status"}:
                result["text"] = (f"长期记录：{'开启' if self.memory.recording_enabled else '暂停'}；"
                    f"当前记忆 {self.memory.count_items()} 条。\n"
                    "/memory list [页码] [--kind 类型] | find [--all] [--page 页码] 词语 | show ID | "
                    "add 内容 | set 键=值 | correct ID 新内容 | "
                    "forget ID | forget-key 键 | pause | resume\n"
                    "暂停停止自动长期记录；显式管理可用。遗忘不清除备份、归档和已导出文件。")
            elif action in {"list", "find"}:
                if action == "find" and not arg:
                    raise ValueError("请提供搜索词")
                if action == "find":
                    include_generated, page, saw_page, query = False, 1, False, arg
                    while query.startswith("--"):
                        option, _, rest = query.partition(" ")
                        if option == "--all" and not include_generated:
                            include_generated = True
                            query = rest.strip()
                        elif option == "--page" and not saw_page:
                            number, _, query = rest.strip().partition(" ")
                            if (not number.isascii() or not number.isdecimal() or
                                    len(number) > 9 or int(number) < 1):
                                raise ValueError("页码必须为正整数")
                            page, saw_page, query = int(number), True, query.strip()
                        else:
                            raise ValueError("格式：/memory find [--all] [--page 正整数页码] 词语")
                    if not query:
                        raise ValueError("请提供搜索词")
                    # 管理范围显式包含生成/推断，不改变聊天候选规则。
                    hits = self.memory.search(query, limit=max(PAGE_SIZE, self.memory.count_items()),
                                              include_generated=include_generated)
                    total = len(hits)
                    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
                    if page > pages:
                        raise ValueError(f"页码超出范围（共 {pages} 页）")
                    rows = [dict(self._row(hit.item), score=hit.score, reasons=list(hit.reasons))
                            for hit in hits[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]]
                    result.update(total=total, include_generated=include_generated,
                                  page=page, page_size=PAGE_SIZE, pages=pages)
                else:
                    tokens = arg.split()
                    page, kind, saw_page = 1, None, False
                    while tokens:
                        token = tokens.pop(0)
                        if token == "--kind":
                            if kind is not None or not tokens or tokens[0] not in KINDS:
                                raise ValueError("--kind 需要有效且唯一的记忆类型")
                            kind = tokens.pop(0)
                        elif token.isascii() and token.isdecimal() and not saw_page:
                            # 防止无限长整数的解析异常穿过命令边界。
                            if len(token) > 9 or int(token) < 1:
                                raise ValueError("页码必须为正整数")
                            page, saw_page = int(token), True
                        else:
                            raise ValueError("格式：/memory list [正整数页码] [--kind 类型]")
                    total = self.memory.count_items(kind=kind)
                    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
                    if page > pages:
                        raise ValueError(f"页码超出范围（共 {pages} 页）")
                    rows = [self._row(item) for item in self.memory.list_items(
                        limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, kind=kind)]
                    result.update(page=page, page_size=PAGE_SIZE, total=total, pages=pages, kind=kind)
                result.update(items=rows, text="\n".join(
                    f"[{r['id']}] {r['kind']}/{r['source']} {r['content']}" for r in rows) or "没有匹配的记忆。")
                result["text"] = f"第 {page}/{pages} 页，共 {total} 条。\n" + result["text"]
            elif action == "show":
                if not arg or len(arg.split()) != 1:
                    raise ValueError("格式：/memory show ID")
                item = self.memory.get_item(arg)
                if item is None:
                    raise KeyError("记忆ID不存在或已被遗忘")
                row = self._row(item)
                result.update(item=row, items=[row], text=f"[{item.id}] {item.kind}/{item.source} {item.content}")
            elif action == "set":
                key, sep, value = arg.partition("=")
                key, value = key.strip(), value.strip()
                if not sep or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", key) or not value:
                    raise ValueError("格式：/memory set user.name=小明（键限英文、数字、点、横线）")
                value = validate_preference(key, value)
                item = self.memory.update_fact(key, value, source="manual", explicit=True)
                result.update(id=item.id, text=f"已保存事实 [{item.id}] {key}={value}")
                changed = True
            elif action == "add":
                if not arg:
                    raise ValueError("请提供内容")
                item = self.memory.add(arg, kind="event", source="manual", explicit=True)
                result.update(id=item.id, text=f"已保存记忆 [{item.id}]。")
                changed = True
            elif action == "correct":
                item_id, _, content = arg.partition(" ")
                if not content.strip():
                    raise ValueError("格式：/memory correct ID 新内容；事实记录填写新的事实值")
                current = next((i for i in self.memory.items if i.id == item_id), None)
                if current and current.kind == "fact":
                    content = validate_preference(current.fact_key, content.strip())
                item = self.memory.correct(item_id, content.strip(),
                    fact_value=content.strip() if current and current.kind == "fact" else None, explicit=True)
                result.update(id=item.id, text=f"已纠正为 [{item.id}]。")
                changed = True
            elif action in {"forget", "forget-key"}:
                if not arg:
                    raise ValueError("请提供记忆 ID 或事实键")
                ids = self.memory.delete(arg) if action == "forget" else self.memory.forget_fact(arg)
                result.update(deleted=ids, text=f"已遗忘 {len(ids)} 条活动记忆及关联修订/派生记录；会话缓存已清理。")
                result["scope_note"] = "未关联的同文副本可用 /memory find --all 词语 查找、核对后另行选择遗忘。"
                result["text"] += "\n" + result["scope_note"]
                changed = True
            elif action in {"pause", "resume"}:
                self.memory.set_recording_enabled(action == "resume")
                result["text"] = "自动长期记录已暂停。" if action == "pause" else "自动长期记录已恢复。"
                changed = True
            else:
                raise ValueError("未知操作；使用 /memory help 查看命令")
            if changed:
                warning = self.changed()
                if warning:
                    result["warning"] = warning
                    result["text"] += "\n" + warning
            result["recording_enabled"] = self.memory.recording_enabled
        except (ValueError, TypeError, OSError, KeyError) as exc:
            result.update(ok=False, text=f"记忆操作未完成：{exc}")
        return result

    @staticmethod
    def format_result(result: dict) -> str:
        return str(result.get("text", "记忆操作完成。"))
