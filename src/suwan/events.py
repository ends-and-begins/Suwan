"""事件模型：topic 常量与事件结构。"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Any

# ---------- Topic 常量（M1 定稿，后续器官按需扩展） ----------

# 节律与生命状态
CLOCK_TICK = "clock.tick"                     # {dt: float} 心跳节拍
LIFE_SLEEP = "life.sleep"                     # {} 入睡
LIFE_AWAKE = "life.awake"                     # {} 苏醒
LIFE_STATE_CHANGED = "life.state.changed"     # {energy, compute, fatigue, sleeping, activity}

# 情感 / 欲望 / 关系 / 目标
AFFECT_MOOD_CHANGED = "affect.mood.changed"   # {value, desc}
AFFECT_IMPACT = "affect.impact"               # {source, value, hours} 请求：加一个衰减的情绪冲击
AFFECT_MOOD_SHIFT = "affect.mood.shift"       # {delta} 请求：直接平移 base_mood
DRIVE_CHANGED = "drive.changed"               # {curiosity, loneliness, expressiveness, boredom}
DRIVE_ADJUST = "drive.adjust"                 # {changes: {curiosity?/loneliness?/expressiveness?/boredom?}} 请求
RELATION_CHANGED = "relation.changed"         # {closeness, trust}
GOAL_UPDATED = "goal.updated"                 # {goals: list[dict]}
GOAL_COMPLETED = "goal.completed"             # {name}
USER_ACTIVE = "user.active"                   # {} 用户刚互动（更新时间戳）

# 输入（传感器）
SENSOR_USER_MESSAGE = "sensor.user_message"   # {text, source, ts}
SENSOR_RECORDED = "sensor.recorded"           # {rid, kind} 感官已记录回执
SENSOR_SHARE_FILE = "sensor.share_file"       # {name, path, content}

# 认知
CHAT_REPLY = "chat.reply"                     # {text, via, user_text}
SEARCH_REQUESTED = "search.requested"         # {query, rid} 请求（SearchService 应答）
SEARCH_RESULT = "search.result"               # {query, rid, ok, text}
MONOLOGUE_GENERATED = "monologue.generated"   # {text, kind}
ACT_CHOSEN = "act.chosen"                     # {action} 决策广播
ACT_DONE = "act.done"                         # {action, note}

# 记忆（增删走总线请求/应答；纯查询直调器官方法）
MEMORY_ADD_REQUEST = "memory.add.request"     # {content, importance, emotion, rid, persist?, receipt?}
MEMORY_ADDED = "memory.added"                 # {rid, content, importance, emotion, ok, error?} 应答/通报
MEMORY_CLEANED = "memory.cleaned"             # {n} 通报
MEMORY_COMPACTED = "memory.compacted"         # {n} 通报

# 产物
STORY_GENERATED = "story.generated"           # {date, text}
DOC_GENERATED = "doc.generated"               # {name, path}
TTS_REQUEST = "tts.request"                   # {text, voice, rid}
TTS_DONE = "tts.done"                         # {rid, file, ok, error}

# 对外输出
NOTIFY_TOAST = "notify.toast"                 # {text}
NOTIFY_NARRATIVE = "notify.narrative"         # {text} 主动推送的一句话（行为叙事/主动话题）
GUI_REFRESH = "gui.refresh"                   # {kind}

# 周期触发（由 SchedulerOrgan 按间隔发布）
AUTONOMY_TRIGGER = "autonomy.trigger"         # {} 到点自主行为
MONOLOGUE_TRIGGER = "monologue.trigger"       # {} 到点内心独白
DIARY_TRIGGER = "reflection.diary"            # {} 到点写日记
STORY_TRIGGER = "reflection.story"            # {} 到点人生故事
MEMORY_MAINTENANCE = "memory.maintenance"     # {} 到点记忆清理/压缩

# 身份
NAME_UPDATED = "identity.name_updated"        # {rid, name} 应答/通报
NAME_REGISTER_REQUEST = "identity.name.register.request"  # {name, rid} 请求（Identity 应答）


@dataclass
class Event:
    """总线上的事件：topic + 任意 JSON 安全 payload + 时间戳。"""

    topic: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=_time.monotonic)

    def to_jsonable(self) -> dict[str, Any]:
        return {"topic": self.topic, "data": self.data, "ts": self.ts}
