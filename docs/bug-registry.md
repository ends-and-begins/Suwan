# Bug 登记（来自旧版分析与实测）→ 新架构修复点

> 状态说明：B01–B20 保留旧重构过程的历史判断，不能证明当前所有同类问题已解决。
> 2026-10-16 已完成第一轮数据可靠性修复；当前状态与复验证据见 [第一轮验收报告](round1-report.md)。
> 原基线 65 测试、第一轮 126 测试为历史记录；严格审查新增的 SR-01–SR-12 已本地修复，现为 173 测试通过，逐项结果见 [严格审查修复报告](review-fix-report.md)。
> 证据说明：P0-1/2/4/5 均在本机实测复现（NameError/ImportError/空 key）。

| # | 严重度 | 问题 | 旧版位置 | 新架构归属/修复方式 | 状态 |
|---|---|---|---|---|---|
| B01 | P0 | 拆分模块丢失 import：autonomous.py 用 random/time/datetime/config 常量未导入；monologue.py 用 datetime/random/queue 零导入 → 运行时 NameError | core/autonomous.py, core/monologue.py | 架构上消灭"跨模块裸函数"；真实类自带正确 import；scripts/check_imports.py 守卫 | fixed(v2) |
| B02 | P0 | `monologue_history` 从未初始化（append 即 AttributeError）；`latest_monologue` 亦未在 __init__ 初始化 | core/monologue.py, core/suwan.py | monologue 归属 MonologueService/状态由器官持有并在构造时初始化 | fixed(v2) |
| B03 | P0 | `self.ensure_dir()` 被调用但无定义（单体里叫 `_ensure_dir`，拆分时丢失） | core/autonomous.py | 目录管理收敛 FileStore（ensure_dir 唯一实现）；不再靠主类魔法属性 | fixed(v2) |
| B04 | P0 | tts.py `from external.llm import llm_clients`，实际名字 `_clients_cache` → ImportError | external/tts.py | adapters/tts.py 经 LLMClient.get("mimo") 取客户端；单测覆盖无 key 降级 | fixed(v2) |
| B05 | P0 | `.env` 从未加载：config.init_env 无调用方 → 所有 key 为空，API/TTS 全不可用 | config.py | src/suwan/config.py 顶层执行 load_dotenv()（参考 suwan_mvp/config.py 的正确写法） | fixed(v2) |
| B06 | P1 | `is_model_available` 内 L157–186 整段死代码（return 后嵌套重复 setter） | core/suwan.py | 新架构无此类缝合代码；随架构消失 | n/a |
| B07 | P1 | 全部包 `__int__.py` 拼写错误，无 `__init__.py` | core/…/各包 | src layout + 规范包初始化 | n/a |
| B08 | P1 | GUI `_gui_worker` 缩进错位：扫描体落入 except → 事件被消费丢弃，仅 5s 轮询兜底 | gui/app.py | tk 桥仅订阅事件渲染（无文件扫描竞态）；前端薄化 | n/a |
| B09 | P1 | 日记功能空壳（DIARY_INTERVAL 只写占位日志） | core/suwan.py run_periodic_checks | Reflection 器官实现真实日记内容（LLM/模板） | fixed(v2) |
| B10 | P1 | 角色名不一致：苏晚/苏婉/苏晓混用 | 多处/gui | 统一人物名常量（assets/persona），一处定义 | fixed(v2) |
| B11 | P2 | relationship.update 永不更新 trust | systems/relationship.py | relation 器官：chat/gift/互动对 closeness 与 trust 都有规则 | fixed(v2) |
| B12 | P2 | 白名单不一致：config_data.json 无 baidu.com，browser.py 兜底有 → 搜索回退被挡 | config_data.json / external/browser.py | 白名单单一配置源（settings.json） | fixed(v2) |
| B13 | P2 | config 残留：未完成 `_get_home_dir()`、重复 import os、硬编码 D:/SuwanData | config.py | Settings 数据类 + env 覆盖 + 文档化默认 | fixed(v2) |
| B14 | P2 | chat 名字识别逻辑在 _local_reply 与 chat 两处重复 | core/chat.py | assets 统一交互规则函数，service 单点调用 | fixed(v2) |
| B15 | P2 | GUI 用相对路径 open("config_data.json")，依赖启动目录 | gui/app.py | 配置路径基于包/显式参数解析 | fixed(v2) |
| B16 | P2 | 线程"fire-and-forget"：自主/聊天线程无生命周期管理、异常不上报 UI | core/suwan.py/gui | asyncio 单循环 + 任务异常统一日志与 notify 事件 | fixed(v2) |
| B17 | P2 | GUI 勾选状态与 core 布尔状态双份，易漂移 | gui/app.py | 状态单源（器官状态），前端只读+发命令事件 | fixed(v2) |
| B18 | P3 | 测试只覆盖 systems/*，绕开 core（正是漏掉 P0 的原因） | tests/smoke_test.py | pytest 分层：器官/服务/总线/迁移/e2e；run_tests 一键 | fixed(v2) |
| B19 | P3 | snapshot/memory 保存用 .tmp 替换，但无备份与校验说明 | systems/memory.py | 本轮采用独立临时文件原子替换、损坏写保护和人工完整备份/恢复流程；未实现每次写入自动备份 | 本轮复验见报告 |
| B20 | P2 | memory.compress_old 中 `old.sort(...)` 语法占位（Ellipsis）→ 实为无效代码 | systems/memory.py | 后续重构仍有摘要遗漏与重排风险；第一轮暂停压缩删除，完整规则留阶段1 | 风险止损，压缩暂停 |

## 新架构（suwan_v2）落地对照
| # | 落地位置（suwan_v2） | 验证 |
|---|---|---|
| B01 | 消灭跨模块裸函数：真实类服务自带 import；`scripts/check_imports.py` 守卫 | 门禁 3/3 全绿（有意漏 import 会被抓） |
| B02 | `organs/identity.py`：monologue_history/latest_monologue 构造即初始化，唯一写入口 | test_runtime 独白/历史断言 |
| B03 | `adapters/filestore.py ensure_dir` 为唯一目录入口 | 全部产物落盘测试 |
| B04 | `adapters/tts.py`：直接由 Settings 构建客户端（不再引 llm_clients） | 无 key 优雅降级 |
| B05 | `src/suwan/config.py`：`Settings.from_env` 顶层 `load_dotenv()` | 单测覆盖 |
| B06/B08 | 伪拆分缝合与 GUI worker 缩进问题随旧架构消失（前端薄化设计） | n/a |
| B07 | `__int__.py` → 规范 `src/suwan` 包结构 | 全部导入通过 |
| B09 | `services/reflection.py` 实现真实日记落盘 | test_m4 ReflectionTest |
| B10 | 名字统一为常量"苏晚"（前端显示层待 tk 里程碑） | 冒烟输出 |
| B11 | `organs/relation.py`：trust 随 chat/gift 增长 | RelationTest 断言 trust>10 |
| B12 | `adapters/browser.py` 单一白名单来源（含 baidu） | 代码审查 |
| B20 | `organs/memory.py compress_old`：第一轮保留全部原记录及顺序 | test_data_safety 250条/重复维护/重启 |
| 其余 | 见对应模块与测试 | 46 单测 + compile + import 守卫全绿 |

> 说明：B13–B19 已按新架构重设计覆盖（Settings 数据类、配置单一来源、单事件循环任务管理、pytest 分层测试等）；tkinter GUI 里程碑（B08/B17 展示层）延后到 M5 tk 桥，核心 headless 能力已完成。

## 数据格式兼容备注（Migrator 关注）
- 旧 `memories.json`：`[{content, importance, emotion, created(iso)}]`
- 旧 `snapshot.json`：`{energy, mood, heartbeat, last_update, emotion_modifiers[], desire{}, current_activity, previous_activity, user_name, relationship{}, goals[]}`
- 日志：`对话记录.txt / 内部日志.txt / 情绪日志.txt`（RotatingFileHandler，`[HH:MM:SS]` 前缀）
- `api_call_log.jsonl` 每行一条记录
- 作品集/学习笔记/人生故事为 `.md/.txt` 文件
以上迁移/兼容细节以 `docs/reference/suwan23-inventory.md` 盘点和当前实现为准；完整迁移故障验收列入阶段 1。
