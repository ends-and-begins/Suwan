# 重构历史里程碑与验收记录

> 本文保留旧重构过程的 M0–M6 计划与历史验收记录，部分条目仍包含当时的待办状态。
> 下一轮开发按 [阶段性迭代计划](iteration-plan.md)（2026-10-15）推进；本文勾选和测试数量不代表当前环境已重新验证。

> 门禁通则：每里程碑结束必须 `scripts/run_tests.py`（自 M1 起逐步建起）全绿 + 旧目录代码零改动。
> 旧代码仅在最终替换阶段（M6）由用户确认后处理。

## M0 —— 规划与基线盘点 ✅ 进行中
产出：
- [x] 决策记录（基准=工作区、架构=B 事件总线+器官、约束=开放、交付=另建目录）
- [x] 参考源拷贝：`docs/reference/suwan23.py`、`suwan_mvp/`（不入 git）
- [ ] suwan23 功能/数据/差异盘点清单（后台子代理产出中）
- [ ] suwan_mvp 架构/缺口盘点清单（后台子代理产出中）
- [x] `docs/architecture.md`、`docs/milestones.md`、`docs/bug-registry.md`、`README.md`、`.gitignore`
- [ ] git 初始提交（本目录）
验收：三份文档齐备、盘点清单入库、M0 commit 存在。

## M1 —— 工程骨架 ✅ 完成（commit 6cedf87）
产出：
- `pyproject.toml`（src layout；运行依赖=旧 5 个，dev extras=pytest/ruff/mypy 备用）
- `src/suwan/` 包：`config.py`（Settings + 顶层 dotenv）、`events.py`（topic 常量）、`clock.py`（Real/Manual 时钟）、`bus.py`（asyncio EventBus：真重放/优先级/隔离/背压/持久化/统计）
- `organs/base.py` Organ ABC + `tests/`（unittest 风格，14 用例）
- `scripts/run_tests.py`（compile+unittest+import 守卫一键）、`scripts/check_imports.py`（AST 守卫，已验证能抓"漏 import"）
验收：✅ 全绿；✅ 模块无循环 import；✅ 守卫抓到样例

## M2 —— 核心器官 ✅ 主体完成（M2a 9122077、M2b 08c1d82、M2c fadbdcb）
- [x] circadian / affect / drive / relation(trust 修复) / goals
- [x] sensory_buffer / memory_core（修 B20）
- [x] identity（B02 归属：monologue_history 初始化与唯一写入口）
- [x] scheduler（周期触发 90/180/900/86400s）
- [x] store（快照原子写 + restore_state 恢复往返，E2E 验证）
- [ ] reflection 内容侧（DIARY_TRIGGER/STORY_TRIGGER/MEMORY_MAINTENANCE 的实际执行）→ M4 补

## M3 —— 认知服务 ✅ 主体完成（fadbdcb）
- [x] assets/generation（本地生成池迁移）
- [x] ChatService（本地兜底/名字识别修复/LLM 路径/搜索分支框架）
- [x] MonologueService（梦境/独白/叙事）
- [x] AutonomousService（加权决策 + 学习/创作/社交/回忆/发呆产物落盘）
- [x] SuwanRuntime（装配+心跳+status 门面）；REPL --smoke 本地模式端到端通过
- [ ] ShareWatcher / 文档生成服务 → M4

## M4 —— 适配器与持久化
产出：
- LLMClient（mimo/qwen/deepseek，超时/重试/用量 JSONL 日志/可用性探测）
- TTSClient（mimo TTS；修复对 llm_clients 的错误引用；播放线程）
- SafeBrowser（白名单统一配置源；修复 baidu 不一致）
- FileStore（原子写/目录管理）＋ Migrator（只读导入旧 SuwanData：memories/snapshot/日志→新格式）
验收：无 key 环境下全部优雅降级并有明确错误事件；Migrator 在样例旧数据上幂等成功；网络/LLM 用桩完成路径测试。

## M5 —— 前端
产出：
- [x] `frontends/repl.py`：交互 + `--smoke`；/status /cfg /api /net /doc
- [x] CLI 冒烟并入门禁第 4 步（本地端到端）
- [x] `frontends/bridge.py`（RuntimeThread + TkBridge，事件转发不依赖 tk）+ 单测
- [x] `frontends/tkapp.py`：tkinter 薄桥（对话/状态板/作品集/人生故事/开关），隐藏窗口构建自检通过
验收：✅ REPL 与 CLI 冒烟进门禁；✅ 桥层单测；✅ GUI 可构建（人工运行：`python -m suwan.frontends.tkapp --home <数据目录>`）

## 加固（评审驱动，2026）
按架构-实现一致性评审逐项收口（目标 commit 见 git log "加固R*"）：
- ① 时间来源：9 处 datetime.now() → 注入 clock（autonomous/reflection/doc/tts/migrator/store）+ 时间驱动回归测试
- ③ B 类缺陷：名字单点写入 / importance≤0 即刻淡忘 / cleanup 冗余 / recall 死参数移除 / chat 死分支
- ④ 动力学：独处平衡移入 affect/drive（user.active 驱动，无漂移风暴）、疲劳率 0.003、长时程测试
- ⑤ 持久化：快照墙钟时刻 + apply_offline 离线补齐（沉睡=时间流逝）
- ⑥ 设施：Thresholds 注入、api_call_log、local_replies 抽取、TTS speak+/tts、白名单可选配置、parity.py
- ② 全事件化：rpc 请求/应答（memory 增删/名字注册/搜索/TTS）+ 通报补齐 + 死事件常量清理 + architecture §3 重写
- ⑦ 门禁：run_tests 打印实际用例数；事件常量零"仅定义未用"（脚本审计通过）；ruff/mypy 在 pyproject 预留但未入门禁（如实说明）

## M6 —— 验收与交付 ✅ 主体完成
- [x] `scripts/run_tests.py` 四步一键绿：compile + 49 单测 + import 守卫 + CLI 冒烟
- [x] README 用法、bug-registry 落地对照、里程碑文档
- [x] 已交付复验：`D:\suwan_project 重构版`（gate PASS + REPL --smoke 通过）
- [ ] 最终人工验收（用户跑通 GUI/REPL）后关闭目标
