# 苏晚 v2 目标架构（事件总线 + 器官）

> 文档状态：目标架构与实现说明。2026-10-16 第一轮本地验收与实际限制见 [round1-report.md](round1-report.md)。原则中的完全事件化、跨线程一致快照仍是逐步收敛目标。
> 当前补充：[严格审查修复报告](review-fix-report.md)。迁移后台线程只读准备，主循环持锁提交；TTS 请求携带开关代数，产物以唯一名称禁止覆盖发布；GUI 轮询实际退出状态并控制音频播放/停止。
> 第二版补充：[记忆规范](memory-v2.md)与 [验收报告](round2-report.md)。记忆文件是事实权威，MemoryManager 在所属循环同步提交管理动作并投影身份；自动写入带代数经总线确认。检索为确定性本地规则，生成内容与用户事实分型。该管理路径采用同一循环直调，完全事件化仍是目标，不把两个文件写入称为事务。
> 2026-10-17 补修：`context.py` 为所有 LLM 生产者提供字符预算与候选片段；`services/llm_request.py` 在所属循环固定请求代数后再排队线程。GUI 保留答复来源 epoch，经渲染、语音提交及后端入口复核；管理列表与聊天候选采用不同范围。事件与感官撤销水位使用同一进程单调时钟，正文的 `context_ts` 在 RPC 调度前固定；器官演化仍使用注入的 `Clock`。
> 设计来源：suwan23.py 单体功能 + suwan_mvp 的事件/器官原型 + 现行工作区的后续功能。
> 旧版所有已知 bug 的修复点见 `docs/bug-registry.md`，实现时逐条对照。

## 1. 设计原则
1. **器官自治**：每个器官是一个有状态子系统，只通过事件总线与外界通信；不互相直接调用、不共享可变对象。可独立单测（时钟、事件可注入）。
2. **事件是唯一耦合**：总线 = 发布/订阅（通配符、优先级、背压、持久化/重放、统计）。器官即订阅者+发布者。
3. **认知是流程，不是状态**：对话/独白/搜索/文档等一次性编排放在 service 层（可无状态），状态一律归器官。
4. **适配器隔离**：LLM / TTS / 浏览器 / 存储 全部走接口（protocol），测试用桩替换，绝不 inline 网络调用。
5. **时间注入**：所有节律/衰减依赖可注入时钟，测试可快进（杜绝 sleep 实测）。
6. **可 headless**：核心 100% 不依赖 GUI；REPL/CLI 优先，tkinter 作为前端最后接入。

## 2. 运行时拓扑
```
                 ┌────────────────────────────── asyncio loop ──────────────────────────────┐
  用户/文件/定时 ─┤  Sensor 层(REPL/tk 桥/ShareWatcher)                                       │
                 │     │  publish                                                    │       │
                 │     ▼                                                            │       │
                 │  ┌──────────────────── EventBus ────────────────────┐            │       │
                 │  │ topics: clock.* life.* affect.* drive.* relation.*│            │       │
                 │  │ sensor.* chat.* memory.* mono.* act.* goal.*      │            │       │
                 │  │ story.* tts.* doc.* notify.* gui.*                │            │       │
                 │  └────────────────────────────────────────────────────┘            │       │
                 │     ▲  订阅        ▲        ▲          ▲        ▲                  │       │
                 │ 器官: Circadian  Affect   Drive    Relation   Goals               │       │
                 │     SensoryBuf MemoryCore Reflection MemoryStore(State)           │       │
                 │     ▲                                                             │       │
                 │  service: ChatService MonologueService ActionService DocService ShareWatcher
                 │  adapter: LLMClient TTSClient SafeBrowser FileStore Migrator      │       │
                 └────────────────────────────────────────────────────────────────────┘      │
  输出：chat.reply / notify.* / tts.done / 作品与记忆落盘  ──────────────────────────────────┘
```
- 单 asyncio 事件循环承载一切；器官 `start/stop` 协程订阅总线。
- 前端（REPL / tk）通过线程安全桥接（`call_soon_threadsafe` + asyncio queue）或独立进程不决定架构。
- 持久化：器官自身写自身文件（原子写），外加 StateStore 周期快照器官状态，供重启恢复。

## 3. 事件模型（初稿，M1 定稿）
### 事件目录（与实际实现一致，2026 加固后核对：无"仅定义未用"常量）

**约定（消除"事件 vs 直调"两套并存的不确定）**
1. **纯查询**（无状态变更，如记忆取回/状态读取）→ 器官公开方法**直调**（单写者读自己状态，同步、可测）。
2. **变更**（改他人状态/执行外部 IO 且调用方要等结果）→ **总线 请求/应答**（`rpc.request`，带 rid；请求 `*.request`，应答同语义主题）。
3. **通报**（已完成的事实，供观察者/展示/联动，无人订阅也无害）→ 发布 `*.changed / *.added / *.done` 等。
4. **周期触发** → Scheduler 按间隔发布 `*.trigger`。

```
# 触发（Scheduler → 器官/服务）
autonomy.trigger / monologue.trigger / reflection.diary / reflection.story / memory.maintenance

# 输入（前端/传感器 → 服务）
sensor.user_message {text,observe_only,turn_id,rid} 用户输入；chat直调时发布观察事件，observe_only阻止重复处理
sensor.recorded {rid,kind}          感官已记录输入/回复，服务等待对应回执
user.active                          "用户在场"（affect/drive/节律据此切换平衡点）
sensor.share_file {name,path,content} 收到投喂（=礼物，Relation 加亲近/信任）

# 请求→应答（变更；rpc）
identity.name.register.request → identity.name_updated {rid,name}
memory.add.request {rid,content,importance,emotion,persist,receipt}
  → memory.added {rid,ok,content,receipt,persisted,error}（persist时确认落盘后才成功）
search.requested {query,rid} → search.result {rid,query,ok,text}
tts.request {text,voice,rid} → tts.done {rid,file,ok,error}

# 通报（发布方 → 观察者/联动）
chat.reply {rid,turn_id,text,via,user_text} 对话回复（Sensory/Relation 联动；GUI 展示）
act.chosen {action} / act.done {action,note}
monologue.generated {text,kind}      独白/梦境/回忆叙事
story.generated {date,text}  / doc.generated {name,path}
affect.mood.changed / drive.changed / relation.changed / goal.updated
memory.cleaned {n} / memory.compacted {n} / life.sleep / life.awake
notify.toast {text} / notify.narrative {text}（前端气泡）
life.state.changed {energy,compute,fatigue,sleeping,activity}（仅状态变化时）
```

> 说明：AFFECT_MOOD_SHIFT 等"请求式"主题保留为外部/未来命令式入口；总线的
> 持久化/重放能力保留实现与测试，**运行时默认不启用**（重放会对带副作用的
> 订阅者重复分发，需启用者自行设计幂等），启用方式见 README。

## 4. 包结构（src layout）
```
suwan_v2/
├── pyproject.toml               # 依赖、pytest/ruff/mypy 配置
├── src/suwan/
│   ├── __init__.py              # __version__、公开 API 面
│   ├── config.py                # Settings(数据类) + load_dotenv() 顶层执行
│   ├── bus.py                   # EventBus（asyncio 版，M1 自研/升级 mvp）
│   ├── events.py                # 事件 topic 常量 + 事件数据类
│   ├── clock.py                 # Clock 接口 + RealClock/ManualClock
│   ├── context.py               # 所有模型入口的字符预算与候选片段
│   ├── organs/                  # 有状态子系统（只经总线耦合）
│   │   ├── base.py / identity.py / circadian.py / affect.py / drive.py
│   │   ├── relation.py / goals.py / sensory.py / memory.py
│   │   ├── scheduler.py / store.py（快照/恢复+离线补齐）
│   ├── services/                # 编排：订阅事件、经 rpc 请求他人状态
│   │   ├── chat.py / monologue.py / autonomous.py / reflection.py
│   │   ├── share.py / doc.py / search.py / tts.py
│   │   ├── memory_manager.py / llm_request.py（管理入口与请求代数）
│   ├── adapters/                # 外部能力与存储
│   │   ├── llm.py（含 api_call_log）/ tts.py / browser.py / filestore.py / migrator.py
│   ├── assets/                  # 本地文案资产（从旧代码抽出）
│   │   ├── local_replies.py / generation.py
│   ├── frontends/               # repl.py（交互+--smoke）/ bridge.py / tkapp.py
│   └── rpc.py                   # 总线 请求/应答 工具（rid+超时）
├── tests/                       # 单测(器官/服务/总线/rpc) + 动力学 + 持久化 + 迁移 + e2e
├── scripts/
│   ├── check_imports.py         # AST：模块内使用未 import 名字守卫
│   ├── run_tests.py             # 门禁：compile + unittest(打印实际用例数) + import 守卫 + CLI 冒烟
│   ├── smoke_repl.py            # CLI 冒烟（本地端到端）
│   └── parity.py                # 行为对照（6 类别断言）
└── docs/
```

## 5. 旧功能 → 新模块映射（功能真值以 suwan23.py 与盘点清单为准）
| 旧（伪拆分版/单体） | 新归属 |
|---|---|
| `Suwan.__init__/update/run_periodic_checks` | CircadianOrgan + Reflection + 总线 tick |
| `_wake_up/_load_snapshot/save_snapshot` | StateStore（快照/恢复，含"苏醒独白"） |
| EmotionSystem / DesireSystem / Relationship | affect / drive / relation 器官 |
| MemorySystem(全部) | sensory_buffer + memory_core 器官 |
| Goal / `_update_goals` | goals 器官 |
| chat/`_local_reply`/generate_document/handle_search | ChatService + assets |
| schedule_autonomous_action/_perform_* / `_local_generate` | AutonomousService + ActionService + assets |
| generate_inner_monologue/_push_behavior_narrative | MonologueService |
| query_llm/get_llm_client/log_llm_call | adapters/llm.py |
| SafeBrowser | adapters/browser.py（白名单统一由配置管理） |
| text_to_speech/play_audio/`_clean_text_for_tts` | adapters/tts.py（修复 llm_clients 引用） |
| check_share | services/share.py |
| SuwanApp（tkinter 全部） | frontends/tkapp.py（薄桥：对话与自治叙事经 TkBridge 事件队列渲染；状态板/作品集/人生故事为 **轮询式只读视图**——2s 状态、5s 文件列表，作品预览显示最新一篇；headless 优先下的展示层取舍，未做事件化刷新） |
| main.py/life_loop | src/suwan/app.py（装配+bootstrap） |
| config.py/`__int__.py` 等 | src/suwan/config.py（dataclass Settings + dotenv 顶层执行） |

## 6. 配置与数据
- `Settings`：pydantic(或纯数据类) + `load_dotenv()` 在模块顶层执行（修复旧版 .env 从不加载）。
- key 来源统一走环境变量；`.env` 不入库。
- 数据根：`SUWAN_HOME`（默认 `D:/SuwanData`，与旧版一致可落盘参照）。
  - 新版数据放 `<SUWAN_HOME>/v2/` 下（`memories.json`、`state.json`、日志、作品集/学习笔记/人生故事），**不覆盖旧版文件**。
  - `Migrator`：首次启动只读导入旧版 `memories.json`、`snapshot.json`、三个日志文本 → 新格式（一次性、可幂等）。
- 白名单/音色等静态配置 → `settings.json`（schema 化），GUI/浏览器共用同一来源。

## 7. 质量门禁（M5 前落地）
- `python scripts/run_tests.py`：py_compile 全部 + ruff + mypy(可选) + pytest + import 守卫，一键绿。
- `scripts/parity.py`：给定脚本化输入，对比旧版/新版关键行为输出（本地兜底回复、自主动作产物、独白）一致或显式偏差。
- 事件流集成测试：发布 `sensor.user_message` → 断言最终 `chat.reply` 与记忆写入。
- 时钟注入测试：器官在 ManualClock 下快进 N 分钟断言状态转移。

## 8. 已内建的旧版 bug 修复承诺（详见 bug-registry）
缺失 import / monologue_history 未初始化 / ensure_dir 丢失 / tts 引用 llm_clients /
.env 未加载 / GUI worker 缩进错位 / `__int__.py` 拼写 / 死代码 / 白名单不一致 /
trust 永不更新 / 日记空壳 / 名字不一致(苏晚/苏婉/苏晓) / 硬编码路径 等 ——
在新架构中按归属位置逐一消灭，并在测试中固化。
