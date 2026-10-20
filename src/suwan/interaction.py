"""人格、状态、偏好共用的只读上下文，以及所有主动展示通道的同一策略。

事实仍由 MemoryOrgan 持久化；此模块不另存偏好副本，不以模型输出更新事实。
低频采用可注入单调时钟的三十分钟冷却，重启重新等待，不涉及调用费用预算。
"""
from __future__ import annotations

from dataclasses import dataclass

from suwan.context import MAX_BACKGROUND_MEMORY_CHARS, memory_context
from suwan.events import event_timestamp

RESPONSE_STYLE_KEY = "preference.response_style"
INITIATIVE_KEY = "preference.initiative"
CONTACT_TIME_KEY = "preference.contact_time"
LOW_CONTACT_SECONDS = 1800.0
MAX_STATE_CONTEXT_CHARS = 1800
RESPONSE_STYLES = {"brief", "normal", "detailed"}
INITIATIVES = {"normal", "low", "off"}

PERSONA = (
    "你是苏晚，住在用户电脑里的数字伙伴，喜欢阅读、创作、观察日常与整理知识。"
    "表达亲切、平等、坦诚，保持自己的兴趣；不以关系亲疏索取回应，不用孤独或低落催促用户陪伴。"
    "稳定身份与事实不随短期情绪改变。情绪只调整语气，不降低事实准确性。"
    "区分用户明确确认的事实、资料知识、旧事件、推测和文学表达；来源不足就说明不知道。"
    "不把生成的故事、独白或日记当成用户说过的话，不声称看到了未提供的信息。"
    "尊重用户的联系与表达偏好。用户直接提问时正常回答，安静设置不取消用户主动请求。"
)
PERSONA_SUMMARY = PERSONA
STYLE_INSTRUCTIONS = {
    "brief": "回答直接、简短，先给结论，只保留必要解释；不添加无关追问。",
    "normal": "回答自然、清楚，解释适量；只在确有帮助时继续提问。",
    "detailed": "回答先给结论，再给条理清楚的必要解释和例子；避免空话。",
}
_PERIODS = {
    "上午": (6, 12), "早上": (6, 9), "中午": (11, 14),
    "下午": (12, 18), "晚上": (18, 22), "白天": (6, 18), "夜里": (22, 6),
}


def validate_preference(key: str, value: str) -> str:
    """保留通用事实命令；仅对本轮可执行的两个枚举键拒绝无效值。"""
    value = str(value).strip()
    choices = {RESPONSE_STYLE_KEY: RESPONSE_STYLES, INITIATIVE_KEY: INITIATIVES}.get(key)
    if choices is not None and value not in choices:
        raise ValueError(f"{key} 仅支持 {', '.join(sorted(choices))}")
    return value


def parse_preference_command(text: str) -> tuple[str, str] | None:
    """有限完整句式；疑问、引用、条件、假设和普通讨论不会变更偏好。"""
    text = str(text).strip().rstrip("。！! ")
    commands = {
        "回答直接一些": (RESPONSE_STYLE_KEY, "brief"),
        "回答简短一些": (RESPONSE_STYLE_KEY, "brief"),
        "回答详细一些": (RESPONSE_STYLE_KEY, "detailed"),
        "恢复自然回答": (RESPONSE_STYLE_KEY, "normal"),
        "少一点主动消息": (INITIATIVE_KEY, "low"),
        "暂停主动消息": (INITIATIVE_KEY, "off"),
        "别主动联系我": (INITIATIVE_KEY, "off"),
        "开启安静模式": (INITIATIVE_KEY, "off"),
        "恢复主动消息": (INITIATIVE_KEY, "normal"),
        "关闭安静模式": (INITIATIVE_KEY, "normal"),
    }
    return commands.get(text)


def contact_time_rule(value: str) -> tuple[str, str] | None:
    """仅解释已公开的日部分语法，任意自由文本不伪装成可执行的调度。"""
    value = str(value).strip()
    for period in _PERIODS:
        if value in {period, period + "可以提醒"}:
            return "only", period
        if value == period + "别提醒":
            return "except", period
    return None


@dataclass(frozen=True)
class ContextSnapshot:
    """同一次同步读取产生的模型输入和依赖；ID使用完整值，不受显示截断影响。"""

    system: str
    prompt: str
    source_ids: tuple[str, ...]
    privacy_generation: int
    context_ts: float


class InteractionContext:
    def __init__(self, organs: dict, clock) -> None:
        self.organs, self.clock = organs, clock
        self._last_proactive_mono = clock.monotonic()

    def _fact(self, key: str):
        return self.organs["memory"].get_fact(key)

    def _value(self, key: str, default: str = "") -> str:
        item = self._fact(key)
        return str(item.fact_value) if item is not None else default

    @property
    def response_style(self) -> str:
        value = self._value(RESPONSE_STYLE_KEY, "normal")
        return value if value in RESPONSE_STYLES else "normal"

    @property
    def initiative(self) -> str:
        value = self._value(INITIATIVE_KEY, "normal")
        return value if value in INITIATIVES else "normal"

    def system(self, task: str = "") -> str:
        instruction = STYLE_INSTRUCTIONS[self.response_style]
        return PERSONA + instruction + ("本次任务：" + str(task)[:240] if task else "")

    def _state_snapshot(self) -> tuple[str, str, tuple[str, ...]]:
        """仅选取明确参与统一状态的事实和关系来源，绝不关联整个记忆库。"""
        keys = ("user.name", RESPONSE_STYLE_KEY, INITIATIVE_KEY, "user.city", "preference.drink",
                "preference.weekend_activity", CONTACT_TIME_KEY)
        facts = {key: self._fact(key) for key in keys}
        source_ids = {str(item.id) for item in facts.values() if item is not None}

        def value(key, default=""):
            item = facts[key]
            return str(item.fact_value) if item is not None else default

        style = value(RESPONSE_STYLE_KEY, "normal")
        style = style if style in RESPONSE_STYLES else "normal"
        initiative = value(INITIATIVE_KEY, "normal")
        initiative = initiative if initiative in INITIATIVES else "normal"
        affect, circadian = self.organs["affect"], self.organs["circadian"]
        relation = self.organs.get("relation")
        if relation is not None:
            # 分数可能包含超过UI最近20条的有效来源；来源与分数一起刷新。
            getter = getattr(relation, "context_source_ids", None)
            if getter is not None:
                source_ids.update(getter())
            else:  # 未提供新接口的独立嵌入器官保持兼容。
                source_ids.update(row["source_id"] for row in getattr(relation, "_events", ())
                                  if row.get("source_id"))
        relation_state = relation.state() if relation is not None else {}
        closeness = relation_state.get("closeness", 10.0)
        trust = relation_state.get("trust", 10.0)
        familiarity = relation_state.get("familiarity") or ("逐渐熟悉" if float(closeness) >= 30 else "仍在相互了解")
        name = value("user.name")
        rows = [
            "身份：苏晚；用户是正在和你说话的人。" + (f"用户确认的名字：{name[:128]}。" if name else "用户名字未知。"),
            f"短期状态：情绪{affect.desc()}；精力{circadian.energy:.0f}；疲劳{circadian.mental_fatigue:.0f}；正在{circadian.current_activity[:256]}。",
            f"关系状态：{familiarity}，亲近度{float(closeness):.1f}、信任度{float(trust):.1f}；这些是互动状态，不是身份或事实证据。",
            f"交互偏好：response_style={style}；initiative={initiative}。",
        ]
        evidence = relation_state.get("events", relation_state.get("evidence", []))
        if isinstance(evidence, list):
            reasons = [str(item.get("reason", ""))[:120] + " [来源ID=" + str(item.get("source_id", ""))[:128] + "]"
                       for item in evidence[-3:] if isinstance(item, dict) and item.get("reason")]
            if reasons:
                rows.append("关系变化依据：" + "；".join(reasons))
        confirmed = []
        for key, label in (("user.city", "居住城市"), ("preference.drink", "饮品偏好"),
                           ("preference.weekend_activity", "周末活动"), (CONTACT_TIME_KEY, "联系时间偏好")):
            item = facts[key]
            if item is not None:
                confirmed.append(f"{label}={str(item.fact_value)[:128]} [ID={str(item.id)[:128]};来源={item.source}]")
        if confirmed:
            rows.append("用户已确认事实：" + "；".join(confirmed))
        contact = value(CONTACT_TIME_KEY)
        if contact and contact_time_rule(contact) is None:
            rows.append("该联系时间为自由文本，尚未转换为自动调度规则；主动展示暂缓，不能承诺按具体时刻提醒。")
        state = "\n".join(rows)
        if len(state) > MAX_STATE_CONTEXT_CHARS:
            state = state[:MAX_STATE_CONTEXT_CHARS - 1] + "…"
        # 字符上限可能裁去末尾文字。保守保留这几个参与构造的状态来源；
        # 它们不是全库绑定，也不会因ID在显示中截断而遗漏级联关系。
        return state, style, tuple(sorted(source_ids))

    def state_text(self) -> str:
        return self._state_snapshot()[0]

    def snapshot(self, task: str = "", text: str = "", *, memories=(), query: str = "",
                 memory_budget: int = MAX_BACKGROUND_MEMORY_CHARS) -> ContextSnapshot:
        """生产者应保存这里的来源，不再自己猜测统一上下文读过哪些记录。"""
        generation = self.organs["memory"].privacy_generation
        stamp = event_timestamp()
        state, style, state_ids = self._state_snapshot()
        # 与memory_context的候选上限一致；不把预算外的全库记录绑定进来。
        records = [item for item in memories if self.organs["memory"].is_context_safe(item)]
        records = records[:max(1, memory_budget // 160)] if memory_budget > 0 else []
        source_ids = set(state_ids)
        source_ids.update(str(item.id) for item in records)
        system = PERSONA + STYLE_INSTRUCTIONS[style] + ("本次任务：" + str(task)[:240] if task else "")
        prompt = state + "\n相关记录（保持来源边界）：\n" + memory_context(
            records, query=query, budget=memory_budget,
        ) + "\n" + str(text)
        return ContextSnapshot(system, prompt, tuple(sorted(source_ids)), generation, stamp)

    def prompt(self, text: str, *, memories=(), query: str = "", memory_budget: int = MAX_BACKGROUND_MEMORY_CHARS) -> str:
        return self.snapshot(text=text, memories=memories, query=query, memory_budget=memory_budget).prompt

    def _revision(self) -> list[str]:
        return [str(item.id) if (item := self._fact(key)) is not None else ""
                for key in (RESPONSE_STYLE_KEY, INITIATIVE_KEY, CONTACT_TIME_KEY)]

    def _time_allowed(self) -> bool:
        value = self._value(CONTACT_TIME_KEY)
        rule = contact_time_rule(value)
        if rule is None:
            return not value
        mode, period = rule
        start, end = _PERIODS[period]
        hour = self.clock.now().hour
        inside = start <= hour < end if start < end else hour >= start or hour < end
        return inside if mode == "only" else not inside

    def can_notify(self) -> bool:
        if self.initiative == "off" or not self._time_allowed():
            return False
        return self.initiative != "low" or self.clock.monotonic() - self._last_proactive_mono >= LOW_CONTACT_SECONDS

    def notification_token(self, *, source_generation: int | None = None) -> dict | None:
        generation = self.organs["memory"].privacy_generation
        if source_generation is not None and (type(source_generation) is not int or source_generation != generation):
            return None
        if not self.can_notify():
            return None
        self._last_proactive_mono = self.clock.monotonic()
        return {"proactive": True, "show_proactive": True,
                "policy_revision": self._revision(), "issued_at": self._last_proactive_mono,
                "delivery_interval": LOW_CONTACT_SECONDS if self.initiative == "low" else 0.0,
                "source_generation": generation, "context_ts": event_timestamp()}

    def notification_data(self, text: str, *, source_generation: int | None = None, **extra) -> dict:
        generation = self.organs["memory"].privacy_generation
        stale = source_generation is not None and (type(source_generation) is not int or source_generation != generation)
        token = self.notification_token(source_generation=source_generation)
        # 即使安静模式只保留内部独白，也必须携带正文构造水位，不能晚发布后回填。
        return {**extra, "text": "" if stale else text, "context_ts": event_timestamp(),
                "source_generation": generation if source_generation is None else source_generation,
                **(token or {"proactive": True, "show_proactive": False})}

    def notification_current(self, data: dict) -> bool:
        """队列交付时再次检查；已经发出的文本不能在改偏好后重新冒出来。"""
        if not data.get("proactive"):
            return True
        return (data.get("show_proactive") is True and data.get("policy_revision") == self._revision()
                and type(data.get("source_generation")) is int
                and data["source_generation"] == self.organs["memory"].privacy_generation
                and self.initiative != "off" and self._time_allowed())
