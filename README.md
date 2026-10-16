# 苏晚 v2 —— 系统工程级重构版

> 状态：2026-10-16 第一轮本地可靠性验收通过，126 个测试与六项离线门禁通过，并完成真实 REPL/Tk 控件及备份恢复演练。真实 LLM、搜索、语音接口尚未验证。
> 本项目承接旧版模块拆分代码，采用 **asyncio 事件总线 + 器官(organ) 架构**。
> 本轮修复、验收范围及剩余限制见 [第一轮验收报告](docs/round1-report.md)；后续目标见 [阶段性迭代计划](docs/iteration-plan.md)。

## 历史重构路线（详见 docs/milestones.md；下列勾选为历史记录）
- M0 规划/基线盘点 ✅
- M1 工程骨架（包结构/配置/总线/事件模型）✅
- M2 核心器官（节律/情感/记忆/反思/目标/自主）✅
- M3 认知服务（对话/独白/自主行为）✅
- M4 适配器与持久化（LLM/TTS/浏览器/存储/迁移器/reflection/share/doc）✅
- M5 前端（REPL、tkinter 界面与桥接代码已具备）✅
- M6 验收：历史记录为主体完成、人工验收待完成；当前本地可靠性复验见第一轮验收报告

## 运行
```powershell
# 离线门禁：临时源码副本；禁用 dotenv/真实网络；编译、测试、静态守卫、冒烟、对照、自检
python scripts/run_tests.py

# 行为对照（本地兜底类别断言 6/6）
python scripts/parity.py

# 集成演练：10次重启、备份恢复、真实 REPL 进程和 Tk 控件（需可用桌面与 tkinter）
python scripts/acceptance_round1.py --output acceptance-result.json

# 交互 REPL（含 /tts <话> /voice <名> /api /net /doc /status /cfg）
$env:PYTHONPATH="src"; python -m suwan.frontends.repl --home <数据目录>

# 一问一答（冒烟）
python -m suwan.frontends.repl --smoke "你是谁？" --home <数据目录>

# 可选开发依赖（ruff/mypy/pytest 等；当前门禁未执行 ruff/mypy，测试数为 run_tests 实时输出为准）
# 也可直接双击根目录一键启动：start_repl.bat（对话）/ start_gui.bat（图形界面）
pip install -e ".[dev]"
```
> 保留旧版 `<home>/data` 到 `<home>/v2` 的导入入口，不修改旧文件；迁移前请备份。
> 正常迁移样例已测，全面故障验收留阶段 1。当前没有旧数据也会写迁移标记，之后补放旧数据不会自动重导。
> 数据默认根为 `D:/SuwanData`（`SUWAN_HOME` 可覆盖）。
> 浏览器白名单：默认内置清单；可用 `SUWAN_BROWSER_CONFIG=<json>` 覆盖（单一来源）。
> EventBus 持久化/重放为保留能力，默认不启用（重放会对副作用订阅者重复分发）。
> `.env` **权威来源=项目根 `.env`**（不受启动目录影响）；可用 `SUWAN_ENV_FILE=<路径>` 显式指定。
> 离线工具自动设置 `SUWAN_SKIP_DOTENV=1`；程序构造隔离配置可使用 `Settings.from_env(env={}, load_env=False, home_dir=...)`。
> 同一 `<home>/v2` 目录只允许一个运行实例。关键数据损坏时拒绝启动并保留原件；恢复步骤见验收报告。
> 分享文件确认记忆落盘后移入 `<home>/share/.processed/`，失败保留原件。第一轮已暂停破坏性记忆压缩。
> `docs/reference/` 只保留盘点清单（`suwan23-inventory.md`/`suwan-mvp-inventory.md`）；
> 历史源码可在当前工作区上级目录的 `suwan project`、`suwan_project 模块拆分`、`suwan_mvp` 查阅。

## 模型供应商配置
在项目根 `.env`（已 gitignore）填写密钥；`Settings.from_env()` 顶层自动加载。
供应商全部走 **OpenAI 兼容协议**；密钥留空=不可用（自动本地兜底）。
默认模型可用 `MODEL_<供应商大写>` 覆盖（如 `MODEL_OPENAI=gpt-4o`）。

| 供应商 | env 变量 | 默认模型 | base_url（默认） |
|---|---|---|---|
| deepseek | `DEEPSEEK_API_KEY` | deepseek-chat | https://api.deepseek.com |
| openai (ChatGPT) | `OPENAI_API_KEY` | gpt-4o-mini | https://api.openai.com/v1 |
| gemini | `GEMINI_API_KEY` | gemini-2.0-flash | https://generativelanguage.googleapis.com/v1beta/openai/ |
| qwen | `DASHSRCOPE_API_KEY` | qwen-turbo | https://dashscope.aliyuncs.com/compatible-mode/v1 |
| moonshot (Kimi) | `MOONSHOT_API_KEY` | kimi-k2-0711-preview | https://api.moonshot.cn/v1 |
| glm (智谱) | `ZHIPU_API_KEY` | glm-4-flash | https://open.bigmodel.cn/api/paas/v4/ |
| mimo (MiniMax) | `MINIMAX_API_KEY` | mimo-v2.5-pro | https://token-plan-cn.xiaomimimo.com/v1 |
| ollama（本地, 免密钥） | — | llama3.1 | http://127.0.0.1:11434/v1 |

切换模型：REPL `/model <名>`（`/cfg` 看当前）；tkinter 界面右上"模型"下拉。
`MINIMAX_API_KEY` 同时供 TTS（`/tts`）使用。样例模板见 `.env`（已含全部占位）。

## 基准决策
| 项 | 决定 |
|---|---|
| 改造基准 | 当前工作区代码（功能最新） |
| 目标架构 | B：事件总线 + 器官（asyncio，参照 suwan_mvp 升级） |
| 约束 | 开放（允许新依赖；数据格式可迁移；前端可 headless/CLI 优先） |
| 交付 | 另建目录开发，验证通过后替换 |
