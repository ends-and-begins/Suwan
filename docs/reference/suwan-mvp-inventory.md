# suwan_mvp 原型盘点（供 suwan_v2 升级为完整版工程依据）

> 盘点对象：`suwan_v2/docs/reference/suwan_mvp/`（asyncio 事件驱动原型）
> 文件：`event_bus.py`(336 行)、`config.py`(33 行)、`main.py`(252 行)、`organs/{circadian,sensory_buffer,memory_core,affect_core,reflection_loop}.py`、`llm/client.py`(134 行)、`suwan_data/events.jsonl`(23 行，抽样全部)。
> 结论前置：**骨架（EventBus + 器官 + 总线解耦 + 参数化 params）值得整体复用**；事件语义/优先级/重放/持久化等"生产级"宣称有相当部分只是摆设；与完整版 suwan23.py 相比缺少全部面向用户与外部世界的能力（对话、搜索、文档、TTS、GUI、分享、快照等）。下文逐条给出行号依据。

---

## 1. EventBus 能力盘点（event_bus.py）

### 1.1 构造与公共 API 签名

| API | 签名 | 行号 |
|---|---|---|
| 构造 | `EventBus(max_queue_size: int = 1000, persist_path: Optional[Path] = None, replay_on_start: bool = False)` | 30–35 |
| 订阅 | `subscribe(event_pattern: str, callback: Callable, priority: int = 0) -> Subscriber`（回调约定 `(event, bus)`，同步/异步皆可） | 70–85 |
| 退订 | `unsubscribe(subscriber) -> bool`（线性扫描所有 pattern 删除，空则删 pattern） | 87–96 |
| 发布 | `async publish(event_type: str, data: dict = None, priority: int = 0, delay: float = 0)` | 100–136 |
| 统计 | `stats() -> dict`：published/delivered/dropped/errors + subscribers/patterns/log_size | 217–224 |
| 调试 | `recent_events(n=50, event_type=None) -> List[dict]`（来自内存环形日志） | 226–238 |
| 关闭 | `async shutdown()`：停持久化→清订阅者队列任务→取消定时器 | 242–261 |

辅助对象：`Event`（`__slots__`，字段 type/data/priority/timestamp，timestamp 为 `time.time()` 秒级浮点；`to_dict()` 输出 ISO 时间）320–336；`Subscriber`（每订阅者一个 `asyncio.Queue(maxsize=max_queue_size)` + 一个独立处理任务，惰性启动）264–317。

### 1.2 各宣称能力的实现方式与不足

**异步发布/订阅**
- 实现：`publish` 同步构造事件→入持久化队列→入内存日志→`_deliver` 扇出到各匹配订阅者队列；订阅者各自 `_process_loop` 消费（297–317）。
- 不足：`_deliver` 对每个事件遍历全部 pattern 并逐条 `sorted`（143–157），O(patterns·subs)；回调队列消费是串行逐条 await（304–307），慢订阅者天然拖慢整条该订阅者队列，但**不会**阻塞其它订阅者（隔离在此）。

**通配符**
- 实现：`_match`（159–168）三种情形：`"*"` 全匹配、精确相等、以 `".*"` 结尾做前缀 `startswith`（即点分树匹配）。示例 `"memory.*"` 匹配 `"memory.surfaced"` 等。
- 不足：**事件命名与通配语法不一致**——真实事件多用下划线 `memory_fragment`/`internal_reflection`（见 §2），`"memory.*"` 永远匹配不到它们；没有 `*` 中间/多级通配，无负向/集合匹配。

**优先级**
- 实现：两层——订阅侧 `Subscriber.priority` 排序仅发生在同一 pattern 内、仅影响扇出顺序（148）；事件侧 `Event.priority` 只被持久化，**从未参与投递排序**。
- 不足：publish 文档写"数字越大优先级越高"（107）而 subscribe 文档写"数值越小越先执行"（78），语义互相矛盾；事件 priority 字段实际无效；无优先级抢占/饥饿控制。

**背压**
- 实现：订阅者队列 `maxsize=max_queue_size`，`enqueue` 用 `put_nowait`（277），满则 `QueueFull` 抛给 `_deliver` 记 dropped 丢弃（154–156）——即"溢出即丢"型背压。
- 不足：无阻塞式背压/无确认(ack)/无 NACK/无重投；丢弃静默只记计数。另：**持久化队列是无界 `asyncio.Queue()`**（58），`put_nowait` 永不触发 `QueueFull`，123–127 的"持久化队列满丢弃"是死代码。

**故障隔离**
- 实现：每订阅者独立队列+独立任务；回调异常在 `_process_loop` 内捕获记日志继续（308–309）；`CancelledError` 退出循环（314–315）。
- 不足：无熔断/退避/毒丸处理——持续抛错的订阅者会无限刷日志并占着投递名额；无超时保护（慢回调无 per-call 超时）。

**持久化**
- 实现：`persist_path` 给出时后台 `_persist_loop`（177–193）从队列取事件逐行 `json.dumps(ensure_ascii=False)` 追加写 UTF-8 文件；每事件 schema 为 `{type,data,priority,timestamp}`。实测 `events.jsonl` 即此产物，跨多次运行追加。
- 不足：**每条事件单独 open/close 一次文件**（188–189），高吞吐时开销大；无批处理/刷盘控制；文件无限增长无轮转；异常仅记日志不重试。事件在投递尝试**之前**已持久化（122–127），与"已投递"语义无关。

**重放**
- 实现：`_load_and_replay`（195–213）启动时把历史事件读进 `_event_log`。
- 不足：**只进内存日志，从不重新投递给订阅者**——名为重放实为"加载历史";真实事件重放需自研。且 main.py 传 `replay_on_start=False`（41–42），功能从未启用。

**延迟事件**
- 实现：`publish(delay>0)` 时 `asyncio.create_task(self._delayed_publish(...))`（112），睡 delay 秒后重新 publish（138–141）。
- 不足：**任务句柄未记入 `_pending_timers`**（54 定义、258 关闭时遍历，但全文件无任何 append）→ shutdown 无法取消延迟任务，可能在总线关闭后仍在发布；每次延迟发布泄漏一个无引用任务。无调度器/取消/周期定时抽象。

**统计/监控**
- 实现：`stats()` 聚合 published/delivered/dropped + 订阅者/pattern/日志规模。
- 不足：**`errors` 计数全文件从未自增**（仅 52 初始化）恒为 0；无 per-subscriber 统计、无投递延迟/队列水位/处理失败明细；`recent_events` 供器官反向查询（memory_core L231、reflection_loop L277/284/295/321 都在用），是总线被当"上下文数据库"用的信号。

**其它缺陷**
- `subscribe` 允许多次同 pattern 同回调注册，不会去重（80–85），`run()` 若被调用两次会重复订阅。
- 事件日志 `deque(maxlen=10000)` 只在内存；`unsubscribe` O(全部订阅)。
- 无事件 schema 校验/版本号，data 为任意 dict。

---

## 2. 事件体系：全部 topic、payload、流向

### 2.1 事件全集（topic → 发布者[行号] → 订阅者[行号]）

| topic | 发布者(行号) | 订阅者(行号) | 无人消费？ |
|---|---|---|---|
| `circadian.startup` | Circadian.run (74) | 仅 Sensory `"*"`(78) | 语义上无定向消费者 |
| `circadian.tick` | Circadian.run 心跳每 10 tick (89–90) | 同上（"*" 窃听） | 基本是噪音 |
| `fatigue_signal` | Circadian._update_awake (108–118)/_update_tired (141–145) | AffectCore.on_fatigue_signal(176)、ReflectionLoop.on_any_signal(134–136)、Sensory("*") | — |
| `sleep_state` | Circadian._update_tired (135–139)/_update_sleeping (169–179) | AffectCore.on_sleep_state(177)、Reflection(134–136)、Sensory("*") | — |
| `wake_up` | Circadian._update_sleeping (163–166) | AffectCore.on_wake_up(178)、Reflection(134–136)、Sensory("*") | — |
| `sensory.startup` | SensoryBuffer.run (79) | Sensory("*") 自听 | — |
| `memory_fragment` | SensoryBuffer._consolidate (103) | MemoryCore.on_memory_fragment(132) | — |
| `memory_core.startup` | MemoryCore.run (136) | Sensory("*") | — |
| `memory_surfaced` | MemoryCore._emit_memory_surfaced (367) | MemoryCore 自强化(134)、AffectCore.on_memory_surfaced(175)、Reflection(134–136)、Sensory("*") | — |
| `memory_faded` | MemoryCore._forget_memory (428) | 仅 Sensory("*") | 无语义消费者 |
| `affect.startup` | AffectCore.run (184) | Sensory("*") | — |
| `affect_shift` | AffectCore._update_affect→_emit_affect_shift (383/520) | ReflectionLoop(134–136)、Sensory("*") | MemoryCore **不**订阅（见 2.3 注释） |
| `reflection.startup` | ReflectionLoop.run (138) | Sensory("*") | — |
| `internal_reflection` | ReflectionLoop._process_reflection_output (348) | MemoryCore.on_internal_reflection(133)、AffectCore.on_internal_reflection(179)、Sensory("*") | — |
| `behavior_inclination` | ReflectionLoop._process_reflection_output (355) | 无任何订阅（行为层缺失） | **死端事件** |

### 2.2 payload 结构（按 topic）

- `circadian.*`/`fatigue_signal`/`sleep_state`/`wake_up`：`{description, state, fatigue, sleep_debt, tick}`（`_emit` 57–68）；`fatigue_signal` 另带 `severity: "mild"|"severe"`（117/144）。
  - ⚠️ schema 冲突：`_emit` 先写 `state=器官状态`（awake/tired/sleeping），随后 `data.update(extra)`（66–67）在 `sleep_state` 事件里用 `{"state":"falling_asleep"|"deep_sleep"|"dreaming"}` 覆盖同一键——同一字段在不同 topic 语义不同，无 schema 文档。
- `memory_fragment`：`{content, source_type, timestamp(源事件 epoch 秒), emotional_texture, salience_score, context:{state, fatigue}}`（175–188）。event.jsonl 实测 context.state/fatigue 常为 `""`/`null`。
- `memory_surfaced`：`{memory_id("mem_NNNNNN"), content, emotional_texture, surface_score, access_count, created_at(ISO), description}`（367–375）。
- `memory_faded`：`{memory_id, content_preview, reason, description}`（428–433）。
- `affect_shift`：`{previous, current, trigger, intensity, description, emotional_texture}`（520–527）。
- `internal_reflection`：`{content, emotional_texture, reflection_count, is_private, private_entry?}`（337–348）。
- `behavior_inclination`：`{description, strength, source, reflection_content}`（355–360）。
- 各 `*.startup`：`{description}` 或 `{description, current_affect}` / `{description, loaded_memories}`。

### 2.3 事件流向（谁发谁收的闭环）

- **启动扇出**：5 个器官 `run()` 各自发 `*.startup`，除 Sensory `"*"` 窃听外无定向接收者。
- **生理 → 情感/反思**：Circadian 生理事件 → AffectCore 浸染情感(102/121/142)；→ ReflectionLoop 收为 physio 信号(115–124)。
- **情感 → 反思（且间接进记忆）**：`affect_shift` → ReflectionLoop 信号(102–113)；同事件经 Sensory 的 `"*"` 窃听 → 可能固化为 `memory_fragment` → MemoryCore —— 即情感经感觉缓冲"绕道"入记忆（affect 事件 payload 里的 `emotional_texture` 注释"供记忆系统使用"，526，但 MemoryCore 并不直接订阅 affect_shift）。
- **记忆 → 情感/反思/自强化**：`memory_surfaced` → AffectCore 情感浸染(76–100)、ReflectionLoop 记忆信号(92–100)、MemoryCore 自增 access_count 与 retention_weight(110–119)。
- **反思 → 记忆/情感**：`internal_reflection` → MemoryCore 以 salience=0.7 硬编码存为记忆(97–108)、AffectCore 浸染(149–165)。
- **感觉 → 记忆**：Sensory 固化的 `memory_fragment` → MemoryCore 存储。
- **自反馈噪音**：Sensory 订阅 `"*"`（78）连自己产的 `memory_fragment` 和全部 `*.startup` 都收；events.jsonl 第 10–11 行可见固化出 content 为 `"事件: memory_fragment"` 的垃圾记忆（`_summarize_event` 190–205 对未知类型走默认文案）——系统事件自我固化、污染记忆库。
- **死端**：`circadian.tick`、`memory_faded`、`behavior_inclination` 除 Sensory 窃听/事件日志外无下游（ReflectionLoop 提供了 `pop_inclination()` 421–425 供"行为层"取用，但 MVP 无行为层）。

---

## 3. 各 Organ 盘点

### 共性约定（非基类，是"惯例"）
- 构造函数 `(bus, params: dict = None)`，params 与类级 `DEFAULT_PARAMS` 浅合并 `{**DEFAULT_PARAMS, **(params or {})}`（各器官重复书写）。
- 每个器官有 `run()`（async 主循环）与 `stop()`（async）；多数在 `run()` 内才 `bus.subscribe`；提供 `get_stats()`/`get_state()` 查询接口供 main 的状态监视打印。
- **没有器官基类/ABC**；`run/stop/参数合并/订阅管理`四处重复，且 stop 语义不一致（见 3.6 缺陷清单）。

### 3.1 CircadianOrgan（生理节奏器，circadian.py）
- 职责（docstring 8–23）：模拟昼夜节律 awake→tired→sleeping 状态机、非线性疲劳/睡眠债；只发事件、不持有他器官引用。
- `DEFAULT_PARAMS`（26–34）：awake_duration=60 / tired_duration=20 / sleep_duration=30 / tick_interval=2.0 / fatigue_rate=0.05 / recovery_rate=0.08 / sleep_debt_factor=0.2。main.py 实例参数（59–67）：60/20/30、tick 2.0（"加速模式"）。
- 状态字段（45–53）：`state`(awake|tired|sleeping)、`phase_elapsed`、`fatigue`、`sleep_debt`、`tick_count`、`cycle_count`。
- 订阅：无（纯生产者）。
- 发布：`circadian.startup`(74)、`fatigue_signal`(108/115/142)、`sleep_state`(136/171/176)、`wake_up`(164)、`circadian.tick`(90)。
- run/节奏：`run()` 无参，`while True` 睡 tick_interval(77)；**不检查 `self._running`**——`stop()`(194–195) 只是置位，对循环无效，必须靠外部 task.cancel 才能停。
- 持久化：无（状态全部瞬时，重启即归零）。

### 3.2 SensoryBuffer（瞬时记忆缓冲，sensory_buffer.py）
- 职责（docstring 10–26）：工作记忆；订阅全部事件、有限容量循环缓冲、按显著性/情感/新颖性打分、定期把高分事件固化为 `memory_fragment`。
- `DEFAULT_PARAMS`（29–37）：buffer_capacity=12、consolidation_interval=5.0、min_salience=0.3、recency_weight=0.4、emotion_weight=0.4、novelty_weight=0.2、max_consolidation_per_cycle=3。main（70–76）：capacity 12、consolidation 5.0。
- 状态字段：`buffer: deque(maxlen=capacity)`(44，存 {event, arrival_time, access_count, emotional_tag})；统计 total_events/total_consolidated/total_discarded(47–49)。
- 订阅：`"*"` 通配全部事件(78) → `on_any_event`(54–63)。
- 发布：`sensory.startup`(79)、`memory_fragment`(103)。
- run/节奏：`run()` 内先 subscribe 再发 startup(76–79)；每 consolidation_interval 秒 `_consolidate`(81–83)。巩固算法 `_consolidate`(85–124)：按 `_score_event`(126–145，近因指数衰减+情感强度+新颖性加权)过滤 ≥min_salience，取 top N 发 `memory_fragment`，超 3×interval 的旧低分条目丢弃(116–124)。
- 持久化：无。
- 备注：情感强度靠中文关键词启发式（152–153，如"崩溃/撕心裂肺"）；`_extract_emotional_tag`(65–72) 从 emotional_texture/affect/emotion/description 取前 80 字。**无 topic 过滤、无自事件排除、无去重**——见 §7 缺陷。

### 3.3 MemoryCore（长期记忆层，memory_core.py）
- 职责（docstring 17–36）：存记忆片段；生命周期 存储→休眠→浮现→强化→衰减→遗忘；情境化浮现；关联图；磁盘持久化与重启加载。
- `DEFAULT_PARAMS`（39–57）：surface_interval=10.0、max_surface_per_cycle=2、surface_temperature=0.6；base_decay_rate=0.01、decay_interval=30.0、min_retention_weight=0.05；max_memories=200、max_memory_content_length=500；save_interval=60.0、save_path=None。main（79–87）：surface 10.0 / decay 30.0 / save_path=suwan_data/memories.json / save 60.0。
- 状态字段（63–82）：`memories: Dict[id,dict]`、`memory_ids: deque`、`associations: Dict[id,Dict[关联id,strength]]`；`_next_memory_id`、`_last_surface/_decay/_save_time`、`_dirty`；统计 total_stored/surfaced/forgotten/associated。
- 订阅（132–134）：`memory_fragment`(on_memory_fragment 90–95)、`internal_reflection`(on_internal_reflection 97–108)、`memory_surfaced`(on_memory_surfaced 自强化 110–119)。
- 发布：`memory_core.startup`(136)、`memory_surfaced`(367)、`memory_faded`(428)。
- run/节奏：1 秒基础 tick(142)，按间隔分派 浮现/衰减/保存 三段（146–158）。
- 记忆记录结构 `_create_memory`(162–183)：id `mem_000001`…、content/source_type/emotional_texture/salience_score/context、created_at/last_accessed/access_count/retention_weight(初值0.5)/decay_resistance(=salience)、tags(关键词法 _extract_tags 199–221)。
- 浮现：`_surface_memories`(225–252) 以 `bus.recent_events(20)` 为情境(231)，`_calculate_surface_score`(254–286) = 近因0.3+访问频率0.2+情感共鸣0.3+标签匹配0.1+随机0.1，`_temperature_sample` softmax(306–358) 采样。
- 衰减/遗忘：`_apply_decay`(379–402) 按 base_decay_rate·(1−decay_resistance·0.8) 每 decay_interval 衰减，超 1h 未访问加速(390–391)；retention_weight<min → 遗忘(397–402)；`_forget_memory`(404–433) 清理关联、decay_resistance>0.6 时发 `memory_faded`。超容量 `_enforce_capacity`(435–449) 忘权重最低者。
- 关联：`_associate_new_memory`(453–467) 情感 0.4+标签 Jaccard 0.3+时间邻近 0.3（`_calculate_association` 469–493）。
- 持久化：`_save_to_disk`(526–561) 整体 JSON（含统计+ISO 时间戳），**临时文件 `.json.tmp` + `replace` 原子替换**(554–557)；`_load_from_disk`(563–584) 重启恢复；`force_save`(586–589) 关闭时调用。构造时若 save_path 存在即加载(85–86)。

### 3.4 AffectCore（情感基质，affect_core.py）
- 职责（docstring 14–32）：情感是**质性文本**非数值；事件"浸染"融合而非替换；惯性渐变；无刺激回归基线；只管理情感本身不负责表达。
- 状态：`current`/`baseline`（默认基线"一种安静的、中性的平静，像午后的光线"，35）、`history: deque(maxlen=max_history)`、`components: Dict[成分,{intensity,decay_rate,source,last_updated}]`、`_last_significant_shift`。
- `DEFAULT_PARAMS`（38–44）：decay_interval=15.0、decay_strength=0.1、return_to_baseline_rate=0.05、max_history=50、significant_shift_threshold=0.3。main（90–96）：decay 15.0、阈值 0.3。
- 订阅（175–179）：`memory_surfaced`/`fatigue_signal`/`sleep_state`/`wake_up`/`internal_reflection` → 各 on_* 回调(76–165)。
- 发布：`affect.startup`(184)、`affect_shift`(520，经 383 的 create_task)。
- run/节奏：订阅→记初始历史(182)→发 startup(184–187)；每 decay_interval 秒 `_natural_decay`(189–191)（依据距上次显著变化时间分档弱/中/强衰减 457–463，随机贴一句回归短语 `_blend_toward_baseline` 490–508；成分按各自 decay_rate 衰减 484–488）。
- 情感融合：`_blend_affects`(195–244) 按 blend_ratio（来源权重 0.2–0.5，286–291）×文本强度(296–299)分三档：<0.25 旧情调为主、<0.5 旧底新主、≥0.5 新主旧底；相似度>0.5 缩幅(210–214)。关键词情绪强度/成分检测靠硬编码词库(324–338、424–434)。
- `_update_affect`(358–383)：算 intensity=1−相似度，写 current/记录历史/更新成分；intensity≥阈值时 `asyncio.create_task(_emit_affect_shift)`(383)。
- 持久化：无（重启情感重置为基线；无快照）。
- 备注：`on_wake_up`(142–147) 用强固定模板重置情感，intensity=0.8 硬编码。

### 3.5 ReflectionLoop（内部反思循环，reflection_loop.py）
- 职责（docstring 14–27）："前额叶/意识流"——定期收集内部信号→LLM 生成第一人称内心独白→解析为情感转向/行为冲动/自我反思；部分标私密。
- `DEFAULT_PARAMS`（29–37）：reflection_interval=12.0、min_interval_between=8.0（**定义了全文件未使用**）、**max_tokens=1000000（100 万，明显笔误默认）**、temperature=0.9、context_window=5、private_probability=0.3、inclination_threshold=0.6。main（99–107）覆盖为 interval 12.0 / max_tokens 200 / temperature 0.9。
- 状态字段（50–61）：`reflection_history: deque(maxlen=context_window)`、`pending_signals: List`、`pending_inclinations: deque(maxlen=5)`、`_last_reflection_time`、`_reflection_count`；统计 total_reflections/inclinations/private_entries。
- 订阅（134–136）：`memory_surfaced`/`affect_shift`/`fatigue_signal`/`sleep_state`/`wake_up` → `on_any_signal`(71–90) 按类型字典抽取为 {type,content,emotional_texture,significance}（affect intensity<0.2 忽略，106–107）。
- 发布：`reflection.startup`(138)、`internal_reflection`(348)、`behavior_inclination`(355)。
- run/节奏：订阅→发 startup→`while self._running` 每 2 秒检查(143)：距上次 ≥interval 且 signals≥1 → `_perform_reflection`(162–199)；≥2×interval 即使无信号 → `_perform_idle_reflection` 走神(201–226，max_tokens=100)。
- LLM 调用：`self.llm.query(prompt, max_tokens=...)`(175–178/209)；失败仅记日志跳过。prompt 全部 user 角色、无 system_prompt（230–267）；"变化摘要"由 `bus.recent_events(30)` 逆向查事件构造（`_format_transition` 269–304、`_format_affect_context` 318–325）——再次把总线当查询库。
- 输出处理 `_process_reflection_output`(329–360)：情感质感取前 80 字(362–370)；`random.random()<0.3` 标私密并附 `private_entry`(335–346)；关键词检测行为冲动(372–402) 发 `behavior_inclination`。
- 持久化：无（history 只存内存；"私密日记"只有计数 total_private_entries 与字段，无落盘）。

### 3.6 器官层一致性检查结果
- `stop()` 语义分裂：Circadian `while True`(76) 无视 `_running`；Sensory/Memory/Affect/Reflection 均 `while self._running`——但 Sensory 在 `run()` 里订阅、Memory/Affect/Reflection 亦在 `run()` 里订阅，若 stop 后再次 run 会重复订阅（`subscribe` 不去重）。
- main.py `stop()`(144–169) 顺序：先器官 stop → 再 task.cancel → 最后 `bus.shutdown()`；Circadian 只能靠 cancel 停下（organ.stop 无效）。
- 各器官 `_running` 初始化位置不一：Circadian 未在 `__init__` 初始化（stop 时现设）；其余在 `__init__` 中置 False。

---

## 4. LLMClient（llm/client.py）

- 构造：`LLMClient(api_key: str, base_url: str, params: dict = None)`（33–45）——同步 OpenAI SDK 客户端封装；`params` 覆盖 `DEFAULT_PARAMS`（26–31）。
- 默认参数：timeout=180 秒 / max_retries=2（即最多 2 次尝试）/ retry_delay=3.0 / default_model="deepseek-chat"。
- `async query(prompt: str, max_tokens: int = 150, temperature: float = 0.9, system_prompt: str = None) -> Optional[str]`（59–122）：
  - 组装 messages：有 system_prompt 才加 system 角色，否则仅 user(81–84)；
  - `loop.run_in_executor(None, lambda: client.chat.completions.create(model=self.model, messages=..., temperature=..., max_tokens=..., timeout=...))` 同步转异步（88–99）；
  - 失败计数 + 固定延迟重试，最终失败返回 **None**（110–122）——无指数退避、无错误分类、无本地兜底；
  - usage.total_tokens 累计（105–106）。
- `set_model(model_name)`（54–57）支持运行中切模型；`get_stats()`（124–134）total_calls/errors/tokens/model/last_call_time。
- 统计字段（49–52）：total_calls/total_errors/total_tokens/last_call_time。
- 调用方：仅 ReflectionLoop（max_tokens=200 或 100）；main 实际装配的是 **Mimo 模型客户端**（minimax token-plan URL），`default_model=mimo-v2.5-pro`（51–55）。
- 与"多模型"宣称的差距：单客户端单 model 字段，多提供商需多个 LLMClient 实例手动切；无流式、无多模态、无 function/tool 支持；无调用日志落盘（config 里的 API_LOG_FILE 未被 MVP 使用）。

---

## 5. main.py 装配与启动流程

1. 日志 basicConfig INFO（19–23）。
2. `SuwanMVP.__init__(api_key, data_dir=None)`（33–111）：
   - 建 `suwan_data` 目录(35–36)；
   - **EventBus(persist_path=…/events.jsonl, replay_on_start=False)**（39–42）；
   - ⚠️ **LLMClient 创建两次**：先 deepseek 版(45–49)被立即覆盖为 **Mimo 版**(51–55)——前一段是死代码，配置语义混乱（api_key 入参实际只喂给被丢弃的 deepseek 客户端）；
   - 器官创建顺序（全为 `bus` + params）：CircadianOrgan(59–67) → SensoryBuffer(70–76) → MemoryCore(79–87, save_path=…/memories.json) → AffectCore(90–96) → ReflectionLoop(99–107, 注入 `self.llm`)；
   - `self._tasks`/`self._running` 初始化(110–111)。
3. `start()`（113–142）：置 running → `create_task` 五个 `organ.run()`（命名 circadian/sensory/memory/affect/reflection，122–128）+ `_status_monitor`(131–133) → `asyncio.gather` 等待。
4. `stop()`（144–169）：置 running=False → 按 reflection→affect→memory→sensory→circadian **逆序** organ.stop(150–155) → cancel 剩余任务(158–164) → `bus.shutdown()`(167)。
5. `_status_monitor`(171–175) 每 30 秒调用 `_print_status`(177–207)：读各器官 get_state/get_stats + `bus.stats()` 打印（CLI 调试用途）。
6. **重复的 `async def main()`**：第 210–216 行的第一版（检查 DEEPSEEK_API_KEY）被 218–248 行第二版（检查 MINIMAX_API_KEY）覆盖，第一版永不执行——明显残留 bug。
7. 有效 main()（218–248）：校验 MINIMAX_API_KEY → `SuwanMVP(api_key=MINIMAX_API_KEY, data_dir=Path("./suwan_data"))` → 为 SIGINT/SIGTERM 注册 `suwan.stop()` 信号处理器（`NotImplementedError` 时静默跳过，即 Windows 上退化为靠 KeyboardInterrupt，230–240）→ `await suwan.start()`，KeyboardInterrupt/异常时调 stop(242–248)。
8. `if __name__ == "__main__": asyncio.run(main())`（251–252）。

---

## 6. 对照"完整苏晚功能"的缺口清单（suwan_mvp 缺失项）

suwan_mvp 只有"体内闭环"：生理节奏→感觉缓冲→记忆→情感→反思，全部事件由 Circadian/Sensory/Reflection 内部产生；**没有任何用户输入或外部世界触点**。对照 `suwan23.py`（完整版参考）逐项列缺口：

| # | 缺口能力 | 完整版参考实现位置 | 说明 / 工作量提示 |
|---|---|---|---|
| 1 | **对话/聊天（chat）** | suwan23.py `Suwan.chat`(614) | MVP 无用户消息事件、无对话 topic、无上下文管理、无对话日志（_log_chat 409） |
| 2 | **本地兜底回复（离线/无 Key 时）** | `_local_reply`(577)、`_local_generate`(731) | MVP 无兜底：ReflectionLoop 失败仅记日志返回(180–181) |
| 3 | **搜索/网页浏览** | `handle_search`(1004)、`SafeBrowser`(92–122，带白名单+bs4 正文抽取) | MVP 无网络层；需新增 search/browse 事件与工具 |
| 4 | **文档生成（写入 output 目录）** | `generate_document`(437)、`SUWAN_OUTPUT_DIR`(config 11) | config 已定义 SUWAN_OUTPUT_DIR 但 MVP 从不使用 |
| 5 | **TTS（MiMo TTS + 播放）** | `text_to_speech`(524)、`_clean_text_for_tts`(505)、`play_audio`(1131)、语音开关/音色选择 | MVP 无任何音频/语音路径 |
| 6 | **GUI（tkinter SuwanApp）** | `SuwanApp`(1141)、run(1409)、open_folder(1123) | MVP 仅 print 状态；目标 README 已定 CLI/REPL→tkinter 桥路线 |
| 7 | **分享文件夹轮询** | `check_share`(1058)、run_periodic_checks(1074) | 配置有 SHARE_FOLDER(10) 但无逻辑 |
| 8 | **快照/状态持久化** | `save_snapshot`(388)/`_load_snapshot`(377)/snapshot.json、`get_status_snapshot`(1093) | MVP 各器官除 Memory 外无落盘：circadian/affect/reflection 重启全丢；bus 事件仅追加日志 |
| 9 | **目标系统（Goal）** | `Goal`(175)、`_init_default_goals`(349)、`_update_goals`(1041)、is_stagnant(181) | 无目标/进展/停滞检测 |
| 10 | **欲望/自主行为调度** | `DesireSystem`(155)、`schedule_autonomous_action`(863)、AUTO_BEHAVIOR_INTERVAL | ReflectionLoop 产出 `behavior_inclination` 后**无消费者**（行为层空位）——正好是接自主行为的接口点 |
| 11 | **关系系统** | `Relationship`(168) | 无用户关系/亲密度/信任度 |
| 12 | **数值情感 + 影响建模** | `EmotionSystem`(124)、MAX_EMOTION_IMPACT(30)、`_log_mood`(420) | MVP 是质性文本情感，与完整版数值情感**模型不同**——数据模型需二选一或做双轨迁移 |
| 13 | **记忆检索接口** | `recall_by_keyword`(217)、`recall_random`(225)、`compress_old`(246)、`cleanup_old`(236) | MVP 只有自浮现，**无按关键词/按用户查询的检索 API**（对话要用记忆必须先补） |
| 14 | **内心独白/日记/人生故事** | `generate_inner_monologue`(975)、INNER_MONOLOGUE_INTERVAL(25)、`_generate_life_story`(1111)、DIARY_INTERVAL | MVP `is_private` 只计数不落盘，无日记文件、无人生故事；config 常量(21–27)已定义全部间隔但 **MVP 代码从不引用** |
| 15 | **学习/创作/社交类自主行为** | `_perform_learning`(899)/`_perform_creation`(931)/`_perform_social`(951)/`_perform_reminisce`(959)/`_perform_idle`(969) | MVP 无自主行为类型分派 |
| 16 | **LLM API 调用日志** | `log_llm_call`(69)、API_LOG_FILE(config 33) | MVP LLMClient 只累计内存统计，不落盘 |
| 17 | **多提供商/开关控制** | 多 OpenAI 客户端(59–66)、set_use_api/network/model(424–433)、GUI 下拉 | MVP 只装配 Mimo 一家；config 里 QWEN/DEEPSEEK Key、QWEN_MODEL/DEEPSEEK_MODEL(13–19) 全部闲置 |
| 18 | **API/网络可用性开关** | set_use_api(424)、set_use_network(427) | 无开关无降级路径 |
| 19 | **事件→行为的执行层** | （完整版为过程式主循环 life_loop 1413） | MVP 无"行为器官"执行冲动/社交/创作/学习 |
| 20 | **测试与数据校验** | 无 | MVP 无任何测试、无事件 schema 校验、无数据迁移器（README M4/M6 目标） |

其它配置落差：`config.py`(21–33) 的 HEARTBEAT/DIARY/REFLECTION/AUTO_BEHAVIOR/INNER_MONOLOGUE/LIFE_STORY/MEMORY_CLEANUP/FATIGUE_DECAY/BOREDOM/MAX_EMOTION_IMPACT/MAX_OUTPUT_TOKENS/API_LOG_FILE 等常量**在本 MVP 全部未被 import**（器官各自硬编码节奏），完整版周期逻辑若移植需重新接线。

---

## 7. 值得复用的工程模式 vs 明显缺陷

### 7.1 可复用模式（升级骨架建议保留）
1. **总线注入 + `(bus, params=None)` + DEFAULT_PARAMS 覆盖合并**：器官只依赖总线、不互相 import（grep 证实零器官间 import），解耦干净，可直接推广为完整版器官基类。
2. **事件 JSONL 持久化 schema**：`{"type","data","priority","timestamp"}`、UTF-8、ensure_ascii=False——简单可审计，可升级为带 `event_id/source/version/ttl` 的正式 schema 并加迁移器。
3. **时钟/节奏参数化**：所有间隔都是参数（秒），main 里可"加速演示"——完整版应保持该风格并进一步抽象出**可注入 clock**（当前混用 `time.time()`/`datetime`/`asyncio.sleep`，无法确定性测试）。
4. **订阅者隔离 + 每订阅者独立有界队列**：一个器官崩溃/卡死不影响其它器官（308–309 捕获），升级时保留并补 per-subscriber 健康指标与超时。
5. **get_stats()/get_state() 查询面 + main 状态监视器**：统一的器官观测接口，是现成可复用的状态面板数据源。
6. **原子落盘**：MemoryCore `.json.tmp` + `replace`(554–557)——快照/记忆落盘可直接复用该写法。
7. **温度 softmax 采样**（MemoryCore 306–358）：候选记忆浮现的带随机选择，可直接移到完整版记忆服务。
8. **质性情感文本 + 融合引擎**：AffectCore 用文本而非数值、按来源权重分档混合(195–244)——审美上符合"苏晚"人设；但需与完整版 EmotionSystem 数值模型做桥接决策。
9. **总线反向查询 recent_events 作"情境上下文"**：Memory/Reflection 都用它取最近事件拼上下文——概念可行，但应抽成正式"情境快照服务"而非器官直接翻总线。

### 7.2 明显设计缺陷 / 待改进点（修复优先级从高到低）
1. **main.py 有重复 `async def main()`**（210–216 死代码被 218–248 覆盖）与 **LLMClient 双重创建**（45–49 死代码）；config.py 大量常量未被接线——遗留残骸，升级前必须清理。
2. **stop 语义分裂**：Circadian `while True`(76) 无视 `_running`；器官都在 `run()` 里 subscribe（Sensory 78、Memory 132、Affect 175、Reflection 134），stop 后重启会重复订阅。→ 抽 **Organ 基类**统一 run/stop/订阅生命周期，订阅移到构造或由基类管理。
3. **事件命名与通配符不匹配**：`.startup` 点分 vs `memory_fragment` 下划线；`"memory.*"` 匹配不到任何真实事件。→ 统一分层命名（如 `memory.fragment`）并定义 topic 注册表。
4. **priority 语义自相矛盾且事件 priority 不参与投递**（subscribe 小数优先 vs publish 大数优先；Event.priority 仅持久化）——要么实现真优先级队列，要么删掉宣称。
5. **"重放"名不副实**：只回填 `_event_log` 不重投；且 main 里 replay_on_start=False。真正重放需按序重建器官状态，属完整版 M4 迁移器范围。
6. **延迟事件任务泄漏**：`_pending_timers` 从不 append，shutdown 无法取消延迟发布（254–259）；`asyncio.create_task` 散落各处（112、194、197、383）无任务登记/取消机制——MVP 里这些"fire-and-forget"任务在快速关闭时会断。
7. **Sensory 自反馈污染**：订阅 `"*"` 把 startup/心跳/自身 memory_fragment 全部再固化，event.jsonl 实证产生 `"事件: memory_fragment"` 垃圾记忆与无界追加——需 topic 白名单/来源排除/去重。同理 Circadian 心跳 `circadian.tick`(89) 纯噪音也进了持久化。
8. **`sleep_state` 的 `state` 键语义被 extra 覆盖**（66–67 vs 46）与 `fatigue_signal` 的 `state` 语义不同——事件 data 无 schema 文档，长此必踩坑。
9. **EventBus 内部死代码/假指标**：`_stats["errors"]` 从不自增；持久化队列无界导致"满则丢"分支不可达；`errors` 上报缺失。
10. **ReflectionLoop 参数错误**：DEFAULT max_tokens=1_000_000(32) 属笔误；`min_interval_between`(31) 未使用；私密反思不落盘（"秘密日记"名存实亡）。
11. **行为冲动是死端**：`behavior_inclination` 无订阅者；`pop_inclination`(421) 无调用方——完整版自主行为层应作为该事件的新订阅者接入（含冲动阈值/冷却）。
12. **memory 关联图建成未用**：`associations` 只增（453–467）不参与 `_calculate_surface_score`(254–286)，也不被任何检索消费——纯统计装饰。
13. **情绪词库重复**：Sensory 强弱词(152–153)与 Affect 强/中/弱词(324–338、424–434)三处硬编码近似重复——抽公共情感词库模块。
14. **LLM 失败无兜底**：query 最终返回 None，Reflection 静默跳过——完整版需接入本地兜底回复（缺口 #2）与重试策略升级。
15. **Windows 信号处理缺口**：`add_signal_handler` NotImplementedError 被吞（238–240），优雅关闭在 Windows 仅靠 KeyboardInterrupt——跨平台关闭需另行方案。
16. **无 schema 校验/版本化/测试**：事件与持久化文件都无版本字段，器官无单测——迁移前应建立事件契约测试与持久化格式迁移器（README M4/M6）。
17. **启动时序竞态**：各器官 startup 事件由 `run()` 首行发出，而 Sensory 等订阅发生在 run 内更晚位置（78 vs 79）——同批 create_task 启动时谁先调度决定 startup 是否被缓冲，依赖运气（event.jsonl 两轮运行都能收到，但属未约定行为）。订阅统一到构造期可根治。

---

*盘点基于 2025 行源码 + 23 行事件样本；所有行号指向盘点目录内文件。*
