# 苏晚 v3 —— 人格、交流偏好与有来源的关系

> 状态：2026-10-17 第三版8项严格审查问题已补修：411测试、六项离线门禁及冻结20个互动场景通过；补齐生成来源与历史生成隔离、搜索发送前撤销、旧关系迁移去重、状态读取缓存、低频实际交付和界面回调保护。固定记忆检索 Top-3 为20/20、Top-1为14/20。真实接口、声卡、人工自然度和长期运行尚未验证。
> 本项目承接旧版模块拆分代码，采用 **asyncio 事件总线 + 器官(organ) 架构**。
> 当前实现与限制见 [第三版补修报告](docs/round3-fix-report.md)、[第三版报告](docs/round3-report.md)、[人格与交流使用说明](docs/personality-v3.md)、[第三版设计](docs/round3-design.md)。记忆沿用 [记忆使用与升级说明](docs/memory-v2.md)；此前验证见 [第二版补修报告](docs/round2-fix-report.md)、[第二版验收报告](docs/round2-report.md)、[第一版严格审查修复报告](docs/review-fix-report.md)和 [第一轮验收报告](docs/round1-report.md)，后续目标见 [阶段性迭代计划](docs/iteration-plan.md)。

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

# 审查修复集成演练：另覆盖真实同步工作线程退出、GUI自动播放和停止（假SDK/播放器）
python scripts/acceptance_review_fixes.py --output review-fix-result.json

# 第二版固定30场景（检索与存储），以及实际记忆页/REPL演练
python scripts/acceptance_round2_retrieval.py --output memory-result.json
python scripts/acceptance_round2_ui.py --output memory-ui-result.json

# 第三版冻结20个互动约束场景（本地运行时与假模型提示词）
python scripts/acceptance_round3.py --output interaction-result.json

# 交互 REPL（含 /tts <话> /voice <名> /api /net /doc /status /cfg）
$env:PYTHONPATH="src"; python -m suwan.frontends.repl --home <数据目录>

# 一问一答（冒烟）
python -m suwan.frontends.repl --smoke "你是谁？" --home <数据目录>

# 可选开发依赖（ruff/mypy/pytest 等；当前门禁未执行 ruff/mypy，测试数为 run_tests 实时输出为准）
# 也可直接双击根目录一键启动：start_repl.bat（对话）/ start_gui.bat（图形界面）
pip install -e ".[dev]"
```
> 保留旧版 `<home>/data` 到 `<home>/v2` 的导入入口，不修改旧文件；迁移前请备份。
> 迁移先严格校验，再持锁提交；目标落盘后写版本化标记，失败重试有收据去重。无旧源时不写标记。
> 旧版/无效标记会显示恢复警告并保留原件，不自动重新导入；恢复方法见严格审查修复报告。
> 数据默认根为 `D:/SuwanData`（`SUWAN_HOME` 可覆盖）。
> 浏览器白名单：默认内置清单；可用 `SUWAN_BROWSER_CONFIG=<json>` 覆盖。显式空列表拒绝全部，显式配置读错/格式错会拒绝启动。
> EventBus 持久化/重放为保留能力，默认不启用（重放会对副作用订阅者重复分发）。
> `.env` **权威来源=项目根 `.env`**（不受启动目录影响）；可用 `SUWAN_ENV_FILE=<路径>` 显式指定。
> 离线工具自动设置 `SUWAN_SKIP_DOTENV=1`；程序构造隔离配置可使用 `Settings.from_env(env={}, load_env=False, home_dir=...)`。
> 同一 `<home>/v2` 目录只允许一个运行实例。关键数据损坏时拒绝启动并保留原件；恢复步骤见验收报告。
> 分享文件确认记忆落盘后移入 `<home>/share/.processed/`，失败保留原件。第一轮已暂停破坏性记忆压缩。
> GUI“记忆”页或 REPL `/memory help` 可查看、搜索、设置、纠正、遗忘及暂停。旧列表首次升级前保留原字节副本；旧代码不能直接读取新版记忆文件。
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
| mimo（小米 MiMo） | `MIMO_API_KEY` | mimo-v2.6-pro | https://api.xiaomimimo.com/v1 |
| minimax（MiniMax） | `MINIMAX_API_KEY` + 明确确认 | MiniMax-M2.7 | https://api.minimax.cn/v1 |
| ollama（本地, 免密钥） | — | llama3.1 | http://127.0.0.1:11434/v1 |

切换供应商：REPL `/model <供应商标识>`（`/cfg` 看当前）；GUI“设置”页选择供应商和具体模型。
TTS 独立使用小米 MiMo 通用 API 的 `MIMO_API_KEY`，生成 WAV；GUI 支持播放和停止。
旧版 `MINIMAX_API_KEY` 有平台归属歧义，默认暂停调用；确认它属于 MiniMax 后另设 `SUWAN_MINIMAX_KEY_CONFIRMED=1`。MiMo Token Plan 凭据不能混用。详见 [供应商配置与历史迁移](docs/provider-configuration.md)。

## 基准决策
| 项 | 决定 |
|---|---|
| 改造基准 | 当前工作区代码（功能最新） |
| 目标架构 | B：事件总线 + 器官（asyncio，参照 suwan_mvp 升级） |
| 约束 | 开放（允许新依赖；数据格式可迁移；前端可 headless/CLI 优先） |
| 交付 | 另建目录开发，验证通过后替换 |
