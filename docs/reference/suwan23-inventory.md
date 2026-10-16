# suwan23.py 功能盘点与拆分工作区对比清单

> 盘点基准：`suwan_v2/docs/reference/suwan23.py`（苏晚·最终完整版，共 1437 行，UTF-8）
> 对比对象：`D:\suwan_project 模块拆分` 下的伪拆分工作区（`core/`、`systems/`、`external/`、`gui/`、`utils/`、`config.py`、`main.py`）
> 说明：除注明外，所有行号均指 suwan23.py。读取与结论均基于两侧源码原文，无臆测。

---

## 1. suwan23.py 类/函数地图（名称 + 起始行号 + 一句话职责）

### 1.1 模块级

| 行号 | 名称 | 职责 |
|---|---|---|
| 15–38 | 常量区 | HOME_DIR=D:/SuwanData、data/share/output 目录、三个 API Key、三个模型名、各心跳/行为间隔、疲劳衰减率、无聊阈值、情绪冲击上限、MAX_OUTPUT_TOKENS=8192 |
| 41–57 | 依赖探测 | try-import requests/openai/bs4，产出 HAS_REQUESTS / HAS_OPENAI / HAS_BS4 三个可用性标志 |
| 59–66 | `llm_clients` | 按可用性为 qwen / deepseek / mimo 各建一个 OpenAI 客户端（mimo 用硬编码 key） |
| 67 | `log_lock` | API 日志写线程锁 |
| 69–89 | `log_llm_call()` | 把一次 LLM 调用追加写进 `data/api_call_log.jsonl`（每条 JSONL，prompt/响应截断存） |
| 92–122 | `class SafeBrowser` | 白名单网页抓取：`is_allowed` 域名白名单、`browse` 抓正文去标签取前 400 字、`search` 走百度百科 |
| 124–153 | `class EmotionSystem` | 情绪系统：base_mood+修饰器=current(0–100)，`add_impact` 加限幅冲击，`update` 向 50 回归+随机游走+修饰器衰减，`desc` 六档情绪文字 |
| 155–166 | `class DesireSystem` | 欲望系统：curiosity/loneliness/expressiveness 随机游走，boredom 单调 +0.02，全部限幅 0–100 |
| 168–173 | `class Relationship` | 亲密度/信任度：chat 涨 0.1 倍、gift 涨 0.5 倍，限幅 0–100 |
| 175–181 | `class Goal` | 目标：progress/target/created/last_advanced；`advance`/`is_complete`/`is_stagnant(7天)` |
| 183–190 | `class MemoryItem` | 单条记忆：content/importance/emotion/created/recalls；`decay_factor`（随 importance 指数衰减）与 `age_days` |
| 192–281 | `class MemorySystem` | 记忆库：JSON 持久化（memories.json）、关键词/随机/最近/碎片召回、清理与压缩、`_load` 读取 |
| 1122–1129 | `open_folder()` | 跨平台（win32 startfile / darwin open / xdg-open）打开文件夹 |
| 1131–1139 | `play_audio()` | playsound3 后台线程播放 mp3（ImportError 时提示安装） |
| 1412 | `suwan` | 全局单例占位 |
| 1413–1424 | `life_loop()` | 每 2 秒 tick：算 delta → `suwan.update(delta)` → `run_periodic_checks(time.time())`，异常 traceback 后继续 |
| 1426–1434 | `main()` | 建 Suwan、建 GUI、起 life 线程、进入 mainloop |
| 1436–1437 | `if __name__ == "__main__"` | 启动入口 |

### 1.2 `class Suwan`（284 行开始）

| 行号 | 方法 | 职责 |
|---|---|---|
| 285 | `__init__` | 初始化全部状态属性、4 个日志路径、chat_history、加载 snapshot、执行 `_wake_up` |
| 334 | `set_tts_voice` | 设置音色 |
| 337 | `_push_behavior_narrative` | 把内部行为写成叙事：80% 概率进 active_messages，并写内部日志 `[叙事]` |
| 345 | `set_tts_enabled` | 语音开关 |
| 349 | `_init_default_goals` | 建两个默认目标（写诗 / 学习 100 知识点） |
| 353 | `_wake_up` | 按离线时长分档生成苏醒语、写入当前活动与主动消息 |
| 377 | `_load_snapshot` | 读 snapshot.json 恢复 energy/mood/heartbeat/last_update |
| 388 | `save_snapshot` | 写 snapshot.json（energy/mood/heartbeat/last_update） |
| 401 | `_ensure_dir` | 在 home 下建子目录并返回（学习笔记/作品集依赖它） |
| 406 | `_notify` | 把消息追加进 pending_notifications |
| 409 | `_log_chat` | 追加写对话记录.txt、维护 chat_history（上限 100）、刷新 last_user_interaction |
| 416 | `_log_thought` | 追加写内部日志.txt |
| 420 | `_log_mood` | 追加写情绪日志.txt |
| 424 / 427 / 430 / 433 | `set_use_api` / `set_use_network` / `set_model` / `set_generate_docs` | GUI 开关/设置写入口（带锁） |
| 437 | `generate_document` | 详细文档生成：组装状态/记忆/身份 system+prompt → query_llm；失败返回"超时/不完整"文案 |
| 472 | `query_llm` | 统一 LLM 调用：锁内读开关与客户端 → 按模型映射真实 model 名 → chat.completions（temperature 0.85, 超时 180）→ 写 api 日志、维护 last_api_error |
| 505 | `_clean_text_for_tts` | 清洗文本：去括号动作描写、去 emoji、压空白（供朗读） |
| 524 | `text_to_speech` | MiMo TTS：mimo-v2-tts + audio 参数，base64 解码存 `audio_output/reply_*.mp3` |
| 577 | `_local_reply` | 无 API 时的本地智能应答链（身份/名字/问候/情绪/记忆/长输入/低电量/兜底） |
| 614 | `chat` | 对话总入口：强制联网判定 → 状态更新 → 联网搜索分支 → 普通对话（构造 prompt/历史/记忆）→ 记录日志与记忆 |
| 731 | `_local_generate` | 本地内容模板工厂：学习主题/学习笔记/创作诗/回忆叙述/主动话题/梦境 六个 context_type |
| 863 | `schedule_autonomous_action` | 自主行为调度：加权随机选择 learn/create/social/reminisce/idle 并执行（含多种条件调权） |
| 899 | `_perform_learning` | 学习行为：选题（LLM 或本地 20 主题）→ 写笔记（LLM 或模板）→ 存 `学习笔记/关于{topic}_{ts}.md` → 记账/扣状态/推送 |
| 931 | `_perform_creation` | 创作行为：按情绪写诗/随笔（LLM 或本地六档诗库）→ 存 `作品集/创作_{ts}.md` → 记账/扣状态/推送 |
| 951 | `_perform_social` | 社交行为：发起话题（LLM 或本地话题库）→ 记对话/推叙事 → 降孤独/无聊 |
| 959 | `_perform_reminisce` | 回忆行为：随机取 2 条记忆 → 第一人称叙述 → 情绪按记忆情感偏移 → 微调状态 |
| 969 | `_perform_idle` | 发呆：随机活动描述，回算力/精力、降疲劳 |
| 975 | `generate_inner_monologue` | 内心独白：睡眠时生成"梦境"；清醒且有 API 时按状态+最近记忆生成；本地兜底；历史 cap 10、20% 概率外说 |
| 1004 | `handle_search` | 手动搜索接口：browse/search → LLM 总结 → 记录日志与记忆 |
| 1015 | `update` | 每 tick 状态演化：heartbeat++、情绪/欲望刷新、隔 1h 无人交互降情绪+升无聊、疲劳衰减、睡眠充能/清醒耗能、能量<10 入睡、`_update_goals` |
| 1041 | `_update_goals` | 目标治理：停滞 7 天或完成 → LLM/本地换新目标并通知，旧目标改名"已完成的旧目标" |
| 1058 | `check_share` | 检查分享文件夹：读并删 .txt/.md → 记为记忆、亲密+3、无聊-30、通知 |
| 1074 | `run_periodic_checks` | 周期任务调度：自主行为/独白/日记/人生故事/记忆清理压缩/分享检查（按各自间隔） |
| 1093 | `get_status_snapshot` | 汇总状态字典（睡眠/精力/算力/疲劳/活动/情绪/欲望/目标），供 GUI 状态板 |
| 1111 | `_generate_life_story` | 人生故事：取最近 30 条记忆 → LLM 总结（或本地一句）→ 追加到 人生故事.txt → 记一条 importance=9 记忆 |

### 1.3 `class SuwanApp`（GUI，1141 行开始）

| 行号 | 方法 | 职责 |
|---|---|---|
| 1142 | `__init__` | tk 主窗 1100x750；四个标签页（对话/作品集/人生故事/状态板）；控制栏（API/模型/音色/联网/分享文件夹/详细文档/朗读） |
| 1237 | `on_model_change` | 模型下拉切换 → set_model + 提示 + 刷新 API 状态 |
| 1243 | `update_api_status` | 按 llm_clients 与 last_api_error 显示 API ✅/❌ |
| 1253 | `send_msg` | 发送：清输入框、立即显示"你"，后台线程 `_async_chat` |
| 1262 | `_async_chat` | 后台调 core.chat → 主线程回显；若开详细文档或含关键词则再起线程生成文档 |
| 1270 | `_safe_display` | 显示苏晚回复；开朗读则后台 TTS |
| 1276 | `_speak_reply` | 取当前音色 → text_to_speech → play_audio；失败提示 |
| 1287 | `_notify_tts_failed` | 聊天区显示"🔇 语音失败" |
| 1290 | `_generate_and_save_doc` | 生成文档写 `output/详细报告_{ts}.md` |
| 1305 / 1308 | `_notify_doc_saved` / `_notify_doc_failed` | 文档保存成功/失败提示 |
| 1311 | `display_chat` | 按发送者着色插入聊天区（苏晚 #d4a373 / 其他 #aaaaaa） |
| 1319 | `view_full_work` | 双击作品列表弹窗全文 |
| 1334 | `update_gui` | 每 3 秒刷新：状态板（emoji+睡眠预估+欲望+目标）、作品集列表+预览、人生故事、通知队列、主动消息队列 |
| 1400 | `on_close` | 关闭：置 running=False、停事件、force_save 记忆、save_snapshot、join life 线程、销毁窗口 |
| 1409 | `run` | mainloop |

---

## 2. 功能点清单（含触发间隔、权重、阈值、本地兜底、prompt 要点）

### 2.1 对话 `chat`（L614）
- **触发**：GUI 发送即调；后台线程执行，不阻塞界面。
- **开关**：`use_api`/`use_network`。
- **强制联网搜索分支**（L616–679）：仅当 `use_network and browser.available and 输入<30字`，命中关键词 `热点/新闻/发生了什么/最新/热搜/今日/查一下/搜一下/搜索/帮我看看/找一下` 即触发；查询词从前缀 `帮我看看/看看/查一下/搜一下/搜索/找一下` 后截取，默认 `今日热点新闻`。
  - 搜索结果有效时：有 API 则让 LLM 总结（system"你是苏晚，一个正在探索世界的信息助手"，max 400），失败回退 `我尝试搜索了…找到了这些信息` 原文前 300 字；无 API 直接拼 `我搜索了…`+结果前 300 字。
  - 结果无效时：有 API 则 LLM 诚实告知（prompt 要点：不要道歉、不要说无法联网、没找到就如实说），失败回退固定句；无 API 走 `_local_reply`。
  - 搜索异常：`我的搜索功能暂时绊了一跤（{e}）…`。搜索后记 `搜索: {q} -> {reply[:50]}`（importance 6）。
- **普通对话**：锁内情绪 `add_impact("对话", +2, 0.3h)`、孤独 -15、无聊 -30、刷新 last_user_interaction；名字正则存 `user_name` 并记记忆（importance 8）。
  - 有 API：取最近 8 行对话，跳过"听着呢/在呢/嗯/哦/好"等短确认（仅当行内容恰好等于这些词且 ≤4 字）；历史取 6 行；记忆关键词召回 2 条 + 30% 概率随机插入一条 importance≥7 记忆（模拟主动想起，文案 `；突然想到：`）。
  - **system prompt 要点**（L700–708）：你是苏晚（数字生命）+ 用户身份描述；不可以用"听着呢/嗯/在呢"开头；问在做什么要如实说；问身份要自信介绍；问他自己是谁可叫名字；长输入先表示理解再条理回应。
  - **user prompt 结构**：状态行 + 相关记忆 + 最近历史 + `用户说：… 苏晚：`。
  - 无 API 或 LLM 失败 → `_local_reply`。
  - 收尾：记对话、`memory.add("对话: …", importance=4)`、`relation.update("chat", 1.0)`。
- **阈值**：MAX_OUTPUT_TOKENS=8192；temperature=0.85；timeout=180s。

### 2.2 本地智能应答 `_local_reply`（L577）
- 无 API 兜底链顺序：问身份 → `我是苏晚，一个住在你电脑里的数字生命。`；问"我是谁" → 知道名字叫名字 / 不知道则笼统回答；问在做什么 → 回当前活动并反问；问候（你好/嗨/hi/hello/晚安/早安）→ 三选一；问心情 → 情绪 desc + 谢谢关心；名字正则 `(?:我是|我叫|你可以叫我|我的名字是)(中文或英文2–10字)` → 记住名字、记记忆（importance 8）、回"我记住了"；问你还记得我叫什么 → 回名字；关键词召回 1 条命中 → `我记得一点：…你想再详细聊聊这个吗？`；输入 >30 字 → "没开 API，等我缓一缓…先简单告诉我一句"；energy<30 → 低电量二选一；最后五句兜底（教教我/换话题/知识库没有/聊近况/好难想想）。

### 2.3 自主行为调度 `schedule_autonomous_action`（L863，触发间隔 AUTO_BEHAVIOR_INTERVAL=180s）
- **闸门**：`is_sleeping or energy<20` → 活动改"休息中……"直接 return。
- **基础权重**：learn=curiosity×1.5；create=expressiveness×1.8；social=loneliness×2.0；reminisce=(100−loneliness)×0.5；idle=max(0, 100−curiosity−loneliness−fatigue)。
- **条件调权**：距上次交互 <300s → social×0.3；boredom>50 → idle×0.2、learn×1.5、create×1.8；fatigue>70 → create×0.3、learn×0.5；compute_power<30 → learn×0.4；energy<40 → create×0.5；上次动作就是 social → social×0.2。
- 加权随机选择；活动若变化则 fatigue −15（上限保护）；记 `_last_action` 防连续社交。
- 注意：原版在锁内执行动作（含 LLM 调用）。

### 2.4 学习 `_perform_learning`（L899）
- topic 来源：`query_llm("你想学什么？", "", 20)` 失败 → 本地 20 主题词库随机。
- 文件名安全化：非法字符替换 `_`；目录 `home/学习笔记/关于{safe_topic}_{YYYYmmdd_HHMMSS}.md`。
- 有 API：学习笔记 prompt 要求 ≥400 字、用自己的话讲核心、三个关键点、与代码/音乐/数据流联想、结尾一个待探索问题，"不要用模板"。
- 无 API：本地模板 2 种（`# 概念/要点/感悟` 或 `### 今日学习` 列表式）。
- 落盘头部 `# 学习笔记：{topic}`；写内部日志；记记忆 `学习{topic}: {note[:50]}`（importance 7）；通知"我刚刚学了…笔记在学习笔记文件夹"；含"学习"目标 +5。
- **状态扣减**：curiosity −15、compute_power −15、fatigue +10、boredom −20；叙事推送"正在探索新知识，感觉世界的拼图又多了一块。"。

### 2.5 创作 `_perform_creation`（L931）
- 有 API：prompt"根据此刻情绪创作一首完整的诗或一篇随笔，不要只写一句话，要展开感受"，输入 `此刻情绪：{desc}`。
- 无 API：`_local_generate("creation")` 按六档情绪取诗库。
- 落盘：`home/作品集/创作_{ts}.md`，头部 `# 苏晚的创作`，尾部 `*YYYY-MM-DD HH:MM*`；记记忆（importance 8）；通知"…放在作品集文件夹里了"；含"诗"目标 +25。
- **状态扣减**：expressiveness −20、compute_power −20、fatigue +12、boredom −15；叙事"刚完成一首小诗…"。

### 2.6 社交 `_perform_social`（L951）
- 话题：LLM（"苏晚主动发起话题"，max 60）失败 → 本地按情绪话题库。
- 行为：`_log_chat("苏晚 (主动)", topic)` + `_push_behavior_narrative(topic)`（80% 概率外显）。
- **状态扣减**：loneliness −30、boredom −10。

### 2.7 回忆 `_perform_reminisce`（L959）
- 取 `recall_random(2)`（带 decay 门控）。
- 叙述：LLM"用第一人称自然讲述这段记忆"（前 200 字，max 100）失败 → 本地 5 条回忆叙述模板（填充记忆前 60 字）。
- 情绪影响：`add_impact("回忆", (平均记忆情绪−当前情绪)×0.3, 1.5h)`。
- **状态扣减**：compute_power −5、fatigue −5（下限 0）；叙事"刚才翻了翻旧照片…"。叙述同时写入 latest_monologue。

### 2.8 发呆 `_perform_idle`（L969）
- 活动三选一（望着数字窗外出神/轻轻哼着无调旋律/整理思绪的碎片）；compute_power +2（cap 100）、energy +0.5（cap 100）、fatigue −8。

### 2.9 内心独白 / 梦境 `generate_inner_monologue`（L975，触发间隔 INNER_MONOLOGUE_INTERVAL=90s）
- **睡眠时**：`_local_generate("dream")`：有记忆碎片则取 3 个 fragment（各前 20 字）拼 `梦里出现了A、B、C，它们以一种奇怪的方式组合在一起。`；无则 8 条梦境文案随机。
- **清醒且有 API**：状态行（活动/精力/疲劳/情绪）+ 最近 3 条记忆 → prompt"请写与活动直接相关的一两句内心想法"（max 100，system"你是苏晚。"）→ 成功则记记忆 `独白: …`（importance 5）、写内部日志、进 monologue_history、更新 latest_monologue、**20% 概率**放进 active_messages。
- **本地兜底**：`_local_generate("creation")` 只更新 latest_monologue 与历史。
- monologue_history 上限 10 条（队首弹出）。

### 2.10 周期任务 `run_periodic_checks`（L1074）
- 自主行为 180s；独白 90s；日记 900s（仅写 `[日记] 又过了一阵子。`——占位，无真实日记内容）；人生故事 86400s 且 heartbeat>100；记忆清理压缩 86400s；分享检查 30s。

### 2.11 人生故事 `_generate_life_story`（L1111，LIFE_STORY_INTERVAL=86400s）
- 取最近 30 条记忆拼 `- 内容` 列表；有 API：LLM"苏晚的人生记录者"总结（max 200）；无 API 本地一句 `苏晚今天{活动}。情绪{desc}。`。
- 追加到 `人生故事.txt`：`=== YYYY-MM-DD ===` + story；记记忆 `人生故事: {story[:60]}`（importance 9）。

### 2.12 记忆清理/压缩（MEMORY_CLEANUP_INTERVAL=86400s）
- `cleanup_old`（L236）：删除同时满足 importance<3、age>90 天、importance<9（第三条恒真，等效前两条）的记忆；有删除则落盘并返回条数。
- `compress_old`（L246）：age>30 天且 importance<9 的记忆中超过 200 条的部分 → LLM qwen-turbo（temperature 0.5, max_tokens 120）合并为第三人称摘要（"100 字内保留关键信息"，取前 30 条各 100 字拼接）；LLM 不可用/失败 → 本地 `旧日摘要：` + 前 20 条各 40 字拼接；合并产物 importance=2、emotion=50。
- 阈值：max_age_days=30、keep_count=200、min_importance=3、max_age_days_clean=90。

### 2.13 分享文件夹 `check_share`（L1058，间隔 30s）
- 扫描 share 目录内 `.txt/.md`：读前 300 字记记忆 `收到文件 {name}: {content}`（importance 6）、写内部日志、读完即删文件、`relation.update("gift", 3)`、无聊 −30、通知"我收到了你分享的…谢谢你！"。

### 2.14 快照 `save_snapshot` / `_load_snapshot`（L388/L377）
- 原始快照只存 4 字段（energy/mood=base_mood/heartbeat/last_update），重启恢复精力与基础情绪（`last_update` 用于苏醒文案时长）。GUI 关闭时调用。

### 2.15 文档生成 `generate_document`（L437）+ GUI 触发（L1267）
- 触发：`generate_docs` 勾选，或用户输入含 `详细说说/详细分析/生成报告/生成文档`。
- 未开 API → 返回 `## 文档生成失败…请先开启 API`。
- prompt 组装：精力描述（<40 会"说话简短/心不在焉"）、情绪影响语气（<35 沉、>65 轻快、否则平和）、用户身份、相关记忆 3 条。
- 生成失败 → `## 文档生成超时…`；结果 <50 字 → `## 文档生成不完整…`。
- GUI 落盘 `output/详细报告_{YYYYmmdd_HHMMSS}.md`（SUWAN_OUTPUT_DIR=HOME/output）。

### 2.16 TTS `text_to_speech`（L524）+ GUI 朗读
- 开关 `tts_enabled` + 音色下拉（mimo_default/冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean）。
- 清洗文本（`_clean_text_for_tts`：去中英文括号内容、去 emoji 名单、压空白）；空/过短（<2 字）跳过。
- 调用 `mimo-v2-tts`：messages 为 style_text + 清洗文本；`audio={format:mp3, voice, speed:1.05}`；base64 解码存 `home/audio_output/reply_{ts}.mp3` 返回路径。
- GUI 播放用 play_audio（playsound3 后台线程）。

### 2.17 状态演化 `update`（L1015，HEARTBEAT_INTERVAL=2s）
- heartbeat+1；`emotion.update(delta)`；`desire.update()`。
- 距上次交互 >3600s：每 tick base_mood −0.05、boredom +0.5（cap 100）。
- 疲劳衰减 `−FATIGUE_DECAY_RATE×delta`。
- 睡眠中：energy +0.5×delta、compute_power +0.3×delta、fatigue −0.5×delta；energy≥90 醒并通知"我睡醒了…"。
- 清醒：energy −0.008×delta、compute_power −0.005×delta；22:00–6:00 每小时情绪 −0.01；energy<10 → 入睡、活动"正在睡觉"、通知"我有点累了，先睡一会儿。"。

### 2.18 目标系统（L349 默认 + L1041 治理）
- 默认：`写一首让自己满意的诗`、`学习100个新知识点`。
- 推进：学习行为含"学习"目标 +5；创作含"诗"目标 +25。
- 停滞（is_stagnant 7 天）或完成 → 换新目标（LLM"设定新的更合适的目标"max 30，或本地备选 `探索一个新领域/写一首短诗/了解一个科学概念`），旧目标改名 `已完成的旧目标`（不参与展示/推进）。停滞文案"目标'{name}'很久没进展，我换成了'{new}'。"。

### 2.19 GUI 状态板与通知
- 每 3 秒 `update_gui`；情绪 emoji（>75😊/>50😐/>25😟/≤25😢）+ 孤独>70 加 📞 + 无聊>50 加 🥱；睡眠预估（剩余 (90−energy)/0.5 分钟）；欲望四值；目标进度条文本。
- 通知取 pending_notifications → 显示 `📢 {note}` + 聊天区"系统"行；主动消息 `苏晚: *{msg}*`。
- 作品集 Tab：按 mtime 倒序前 15 个 .md 列表 + 最新文件前 200 字预览，双击弹窗全文。
- 人生故事 Tab：story_log 最后 40 行。

---

## 3. 数据格式说明

### 3.1 snapshot.json（`data/snapshot.json`）
原版写 4 字段（indent=2、ensure_ascii=False）：
```json
{ "energy": 100.0, "mood": 50.0, "heartbeat": 0, "last_update": "2025-01-01T00:00:00.000000" }
```
- energy：精力；mood：情绪 base_mood；heartbeat：心跳计数；last_update：ISO 时间，重启时用于计算"睡了多久"。
- 伪拆分工作区扩展为：另含 `emotion_modifiers[]`（source/value/decay_rate）、`desire{curo/…4 值}`、`current_activity`、`previous_activity`、`user_name`、`relationship{closeness,trust}`、`goals[{name,progress,target}]`（排除"已完成的旧目标"），并改为临时文件原子替换写（`.json.tmp` → replace）。

### 3.2 memories.json（`data/memories.json`）
JSON 数组（indent=2、ensure_ascii=False，每 10 次 add 或 force_save 时写）：
```json
[ { "content": "对话: '你好' -> '你好呀。'", "importance": 4, "emotion": 50.0, "created": "2025-01-01T00:00:00.000000" } ]
```
- 读取按 `created` fromisoformat 还原；importance/emotion 缺省 5/50.0。

### 3.3 三个 .txt 日志（均在 `data/`）
- **对话记录.txt**：每行 `[HH:MM:SS] 说话者: 内容`（说话者：你 / 苏晚 / 苏晚 (主动)）。内存镜像 chat_history 上限 100 行。
- **内部日志.txt**：每行 `[HH:MM:SS] 内容`（含 `[叙事]`、`[独白]`、`[学习]`、`[日记]`、`[人生故事]`、`🌐/📄/❌` 等前缀）。
- **情绪日志.txt**：每行 `[HH:MM:SS] 情绪 (来源): 数值`（由 `_log_mood` 写，原版代码中未调用——保留方法）。
- 注：原版为每次 open 追加写；拆分版改用 logging.RotatingFileHandler（10MB×3 备份），行格式保持 `[HH:MM:SS] …`。

### 3.4 api_call_log.jsonl（`data/api_call_log.jsonl`）
每行一个 JSON（ensure_ascii=False，追加写，log_lock 保护）：
```json
{ "time": "2025-01-01 00:00:00", "model": "qwen-turbo", "system_prompt": "…前200字", "user_prompt": "…前200字", "response": "…前300字", "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_time": 1.23, "success": true }
```
- success=false 时 response 存异常文本；写失败静默吞掉。

### 3.5 作品集 / 学习笔记 / 语音 / 文档命名与目录
- 作品集：`data/作品集/创作_{YYYYmmdd_HHMMSS}.md`（头部 `# 苏晚的创作`、正文、尾部 `*YYYY-MM-DD HH:MM*`）。
- 学习笔记：`data/学习笔记/关于{safe_topic}_{YYYYmmdd_HHMMSS}.md`（safe_topic 已过滤非法字符；头部 `# 学习笔记：{topic}`）。
- 语音：`data/audio_output/reply_{YYYYmmdd_HHMMSS}.mp3`。
- 详细报告：`output/详细报告_{YYYYmmdd_HHMMSS}.md`（output=HOME_DIR/output）。
- 分享入站目录：`share/`（.txt/.md，读取即删）。
- 根：HOME_DIR=D:/SuwanData，data=HOME_DIR/data，share=HOME_DIR/share（拆分版 HOME_DIR 可由环境变量 SUWAN_HOME 覆盖，默认仍 D:/SuwanData）。

---

## 4. Suwan 实例状态属性清单（suwan23.py `__init__`，L285–332）

| 属性 | 初值 | 类型/说明 |
|---|---|---|
| `lock` | RLock() | 递归线程锁 |
| `running` | True | bool 运行标志 |
| `stop_event` | threading.Event() | 停止事件 |
| `birth` | datetime.now() | 出生时刻（算苏醒时长） |
| `home` / `share` | 入参 Path，mkdir | 数据主目录 / 分享目录 |
| `energy` | 100.0 | float 精力 |
| `compute_power` | 100.0 | float 算力 |
| `mental_fatigue` | 0.0 | float 心理疲劳 |
| `is_sleeping` | False | bool 睡眠标志 |
| `sleep_start_time` | None | float/None 入睡时刻 |
| `emotion` | EmotionSystem() | 情绪系统对象 |
| `desire` | DesireSystem() | 欲望系统对象 |
| `relation` | Relationship() | 关系对象 |
| `memory` | MemorySystem(home) | 记忆系统对象（memories.json） |
| `browser` | SafeBrowser() | 白名单浏览器 |
| `use_api` | False | bool LLM 开关 |
| `use_network` | False | bool 联网开关 |
| `model` | "mimo" | str 模型键（qwen/deepseek/mimo） |
| `generate_docs` | False | bool 详细文档模式 |
| `tts_enabled` | False | bool 朗读开关 |
| `last_api_error` | "" | str 最近 API 错误 |
| `user_name` | None | str/None 用户名字 |
| `heartbeat` | 0 | int 心跳计数 |
| `last_diary / last_reflection / last_auto / last_monologue / last_story / last_share / last_memory_cleanup` | 各 0 | float 上次各周期时刻（与 time.time() 比较） |
| `last_user_interaction` | time.time() | float 最近用户交互时刻 |
| `current_activity` | "发呆" | str 当前活动 |
| `previous_activity` | None | str/None 前活动 |
| `latest_monologue` | "" | str 最近一条内心独白/叙事 |
| `monologue_history` | [] | list[(HH:MM, text)] 上限 10 |
| `pending_notifications` | [] | list[str] GUI 通知队列 |
| `active_messages` | [] | list[str] 主动消息队列（苏晚主动说的） |
| `goals` | [] + 两个默认 Goal | list[Goal] |
| `chat_log / thoughts_log / mood_log / story_log` | home/对话记录.txt 等 4 个 Path | 日志文件 |
| `chat_history` | [] | list[str] 对话行镜像，上限 100 |
| `last_snapshot_time` | None | datetime/None（load 后填充） |
| `snapshot_file` | home/snapshot.json | Path |
| `tts_voice` | "default_zh"（L332，_wake_up 之后赋值） | str 音色 |
| （动态）`_last_action` | 首次调度时才出现 | str，最近一次自主行为键（hasattr 保护） |

---

## 5. 差异对比：suwan23.py 有、拆分工作区缺失/已损坏（逐项）

### A 级：缺失或致命损坏（运行即崩 / 功能整段失效）

1. **`_ensure_dir` / `ensure_dir` 双双丢失**
   - 原版：`Suwan._ensure_dir(sub)`（L401），供 `_perform_learning`（L903 建"学习笔记"）与 `_perform_creation`（L938 建"作品集"）使用。
   - 拆分版：`core/autonomous.py` L73 `self.ensure_dir("学习笔记")`、L112 `self.ensure_dir("作品集")` 仍被调用，但整个工作区**没有** `ensure_dir` 或 `_ensure_dir` 的任何定义（grep 仅 2 处调用点）。→ 学习/创作行为一执行到保存目录即 `AttributeError`。
2. **`monologue_history` 未初始化（原版 L317 有 `[]` 初值）**
   - 拆分版 `core/suwan.py` 的 `__init__` 未定义 `latest_monologue`（原版 L316 ""）与 `monologue_history`（原版 L317 []）；`_wake_up` 只是临时赋值 `latest_monologue`。
   - `core/monologue.py` 的 `generate_inner_monologue` 无条件 `self.monologue_history.append(...)` → 每次调用（每 90s）必抛 `AttributeError: 'Suwan' object has no attribute 'monologue_history'`。→ 内心独白/梦境功能全死，且每 90 秒向 life_loop 抛一次异常。
3. **`core/monologue.py` 无任何 import（时间/随机/队列全裸奔）**
   - 文件只定义两个函数，未 import `datetime`/`random`/`queue`。即使补上属性初始化，`datetime.now()`、`random.random()`、`queue.Full` 仍会 `NameError`。属拆分时把 import 行弄丢。
4. **`core/autonomous.py` 缺 import（time/random/datetime/BOREDOM_THRESHOLD/MAX_OUTPUT_TOKENS）**
   - 文件顶部只有 `from utils.helpers import sanitize_filename`。`schedule_autonomous_action` 用到 `time`（L17）、`random`（L32）、`BOREDOM_THRESHOLD`（L19）；`_perform_learning/_perform_creation` 用到 `datetime`（L88/L114）与 `MAX_OUTPUT_TOKENS`。→ 首次自主行为（180s）必抛 `NameError`，learn/create/social/reminisce/idle 全灭。
5. **`external/tts.py` 引用不存在的 `llm_clients`**
   - `text_to_speech` 内 `from external.llm import llm_clients`（L16），而 `external/llm.py` 只有 `_clients_cache` 与 `get_llm_client()`，无 `llm_clients` 全局 → 调用即 `ImportError`。→ TTS 功能不可用。
6. **`external/tts.py` 被截断（`...` 占位）**
   - L22 `clean_text = _clean_text_for_tts(text)` 后只剩一行 `...`，原版的"空文本/过短（<2 字）跳过语音"守卫（原版 L531–532）丢失。无报错但明显是截断痕迹。
7. **`systems/memory.py` `compress_old` 排序语句损坏**
   - L127 `old.sort(...)`：把省略号当**位置参数**传给 `list.sort` → 运行时 `TypeError: sort() takes no positional arguments`；且丢失原版 `key=lambda m: m.importance, reverse=True`（按 importance 降序保留）。→ 每日记忆压缩必崩（仅当待压缩数 >200 时触发 TypeError）。
8. **`gui/app.py` 主动消息发送者写错字**
   - L300 `self.display_chat("苏晓", f"*{msg}*")`——"苏晓"应为"苏晚"。原版 L1395 用"苏晚"。纯错别字，不影响运行但展示错误。
9. **包标记文件拼写错误：`__int__.py`（应为 `__init__.py`）且为空**
   - `core/`、`systems/`、`external/`、`gui/`、`utils/`、`tests/` 下只有空 `__int__.py`，没有真正的 `__init__.py`（全仓仅 suwan_mvp 树有正确文件）。当前依赖 Python 命名空间包隐式机制才能 `import`；若环境/打包方式变化即失效。工程级缺陷。

### B 级：行为差异 / 半损坏（能跑但与原版不一致或依赖环境）

10. **`core/suwan.py` `is_model_available` 里嵌了整段死代码**
    - L150 定义 `is_model_available`（拆分版新增，GUI `update_api_status` L141 依赖它），但 L157–187 在其 return 之后又缩进嵌套了 `set_tts_voice/set_tts_enabled/set_use_api/set_use_network/set_model/set_generate_docs/is_model_available` 一整份重复定义——不可达死代码（疑似旧方法粘贴未删）。方法本身可正常返回，非致命但属明显残留。
11. **`active_messages` / `pending_notifications` 由 list 改为 `queue.Queue`**
    - 原版是 list + `append/pop(0)`；拆分版是 maxsize 50/30 的 Queue。多数写入口用 `put_nowait`（monologue 的 `_push_behavior_narrative` 有 try/except Full 丢弃），但 `generate_inner_monologue` 的 L23 `self.active_messages.put(mono)` 无超时——队列满时可能阻塞该线程（GUI 每 3s 排空，实际风险低）。语义基本兼容。
12. **`check_share` / `update` / `_update_goals` / `get_status_snapshot` 等大体保留**
    - `check_share`（suwan.py L265）逐行与原版一致；`update`（L209）同逻辑但把 `_update_goals()` 移到锁外调用；`_update_goals`（L237）停滞分支加长备选列表并新增空值保护——行为更稳健，属改动而非损坏。
13. **`run_periodic_checks` 重构（拆分版 L281）**
    - 锁内只做时间判断，耗时操作挪锁外；自主行为改为**单独线程**执行（L312，无 try/except → 若 autonomous 修复后其内部异常仍会打到线程 stderr 而不是 life_loop）。日记占位 `[日记] 又过了一阵子。` 保留。但注意：该函数**同步调用** `self.generate_inner_monologue()`（L314），而后者此刻因 #2/#3 必炸 → 每个独白周期向 life_loop 抛异常。
14. **高重要性记忆插入对话逻辑被替换**
    - 原版 chat L696–699：遍历 importance≥7 记忆，**30% 概率**取第一条拼接 `；突然想到：`。
    - 拆分版 chat.py L133–135 改调 `memory.get_high_importance_memory(7)`（新加方法，decay 门控最多试 10 次随机选一条）→ 不再有 30% 整体门控，行为与"偶尔主动想起"的原意图有偏差。
15. **`_local_reply` 名字正则放宽**
    - 原版 L590：`(中文或字母){2,10}` 且无尾部约束；拆分版 chat.py L23：允许数字/下划线/连字符/空格、1–20 字符、并要求后随 `，。！？/空白/行尾`。→ 匹配范围与"我记住了"的说法变化；宽字符集 + 允许空格可能把尾随空白/半截话收进名字。
16. **聊天历史"废话过滤"规则不同**
    - 原版 L688：仅当行内容**恰为**"听着呢/在呢/嗯/哦/好。"等 5 个且 ≤4 字才剔除；拆分版 chat.py L122–126 扩到 10 个确认词并先剥标点再比较（≤2 字剔除）——对"嗯嗯/好的"这类也会过滤，正常句子保留。行为更激进，非原版语义。
17. **输入长度保护（拆分版新增）**
    - chat.py L48–52、generate_document L169–172：>2000 字符截断并加"…（输入过长，已截断）"。原版没有。属安全增强。
18. **文档生成 system prompt 被大幅改写**
    - 原版 L448–458 简洁版；拆分版 chat.py `generate_document` L183–195 加入"数字生命视角但别堆砌比喻/可诚实说只知道一部分/别表演角色"等人格约束。→ 生成风格会与旧版明显不同（改动需确认是否预期）。
19. **`MemorySystem.compress_old` prompt 由"100字内"改为"200字内"**（memory.py L138），且把 LLM 调用挪出锁（改善）；合并摘要兜底逻辑保留。
20. **SafeBrowser 差异（external/browser.py）**
    - `browse` 删掉了"无 requests → 返回 [浏览器不可用]"的首守卫（原版 L105）；`from bs4 import BeautifulSoup` 放在模块顶层无 try（原版 HAS_BS4 探测）→ 未装 bs4 时**整个应用 import 崩溃**；白名单改为读 config_data.json（兜底含 www.baidu.com）；`search` 增加百科失败后抓百度 c-abstract 摘要（拆分版新增能力）；错误文案扩展为 [访问超时]/[连接失败]/[HTTP错误]/[访问失败: …]。
21. **TTS 客户端来源错误（与 #5 同源）**：本应走 `get_llm_client("mimo")`；另 `_clean_text_for_tts`、`play_audio` 保留。语音音色列表改从 config_data.json 读取。
22. **MiniMax Key 由硬编码改为环境变量**
    - 原版 L20 内嵌 `tp-c82t…` 默认 Key；拆分版 `config.py` 用 `os.getenv("MINIMAX_API_KEY")`（.env 中已提供该键）。行为依赖环境，Key 策略差异。
23. **日志实现换成 logging 轮转文件**
    - utils/logging.py：RotatingFileHandler(10MB×3)。格式保持 `[HH:MM:SS] …`；`chat_history` 仍内存维护（上限 100）并刷新 last_user_interaction；`_notify` 走 `pending_notifications.put_nowait`。原版是裸 open 追加。属合理重构但非逐字节相同（如换行/缓冲时机）。
24. **快照读写扩展与原子化（拆分版 suwan.py L360–447）**
    - 写：临时文件 `.json.tmp` → replace 原子替换；字段大幅扩展（见 §3.1）。读：损坏容错 try/except 并打印栈（原版损坏会直接抛）。方向为增强，但**快照字段非原版格式**——旧 snapshot 也能读（get 缺省），新快照旧版程序不兼容。
25. **`MemorySystem._save` 同样改为 .tmp 原子替换**（memory.py L51–65）并带失败打印。
26. **GUI 差异**
    - 标题"苏晚 · 拆分版"（原"苏晚 · 最终完整版"）；`update_api_status` 用 `core.is_model_available(model)`（新方法）；主动消息/通知用队列 get_nowait 排空；孤独 emoji 由 📞 改 💓；通知前缀 📙（原 📢）；作品集/人生故事文件读取搬到后台 `_gui_worker` 线程——该 worker 的扫描代码整体缩进在 `except queue.Empty:` 分支内（gui/app.py L342–378），效果是"事件到来时不立即扫描、靠 5 秒空等轮询兜底"，与原版每 3s 直读行为有差异但不至于完全失效；窗口关闭时对 browser.session 做 close 并延长 join 到 10s。
27. **`config.py` 残留截断函数**：`_get_home_dir()` 只到 `init_env()` 后接 `...`（死代码，未被调用）；顶部 `import os` 两次、`load_dotenv` 依赖 python-dotenv（原版纯 os.getenv）。常量与间隔定义齐全，与原版数值一致。
28. **`main.py` 顶层包 try/except 写 crash.log**（拆分版新增保护）；life_loop 逻辑同原版。
29. **拆分版新增而未见于原版的辅助**：`sanitize_filename`（utils/helpers，更严的文件名清洗：NFKC、去路径分隔符、防 `..`、限 80 字符）；`is_model_available`（core/suwan.py L150）；`MemorySystem.get_high_importance_memory`；`_notify_gui_update`/`gui_event_queue`（向 GUI 推送 works/story 刷新事件）；`setup_loggers`。
30. **`systems/` 子系统保真度**
    - `emotion.py`/`desire.py`/`relationship.py`/`goals.py` 与原版类**逐行等价**（仅 emotion 改为从 config 引入 MAX_EMOTION_IMPACT，数值 15.0 一致）；`memory.py` 除 #7、#19、#25 外等价。

> 补充说明：原版中的"反思"实际上**从未被实现**——只声明了 `last_reflection` 属性（L310）与 `REFLECTION_INTERVAL` 常量（L28），`run_periodic_checks` 不使用它。拆分版同样只保留属性与常量（core/suwan.py L60、config.py L36），因此该项两侧**平级（均为占位）**，不是回归。真正的反思/冲动/日记器官逻辑存在于 `suwan_mvp/`（organ/reflection_loop.py 等）另一棵独立原型树，不在本次对比范围内。

---

## 6. suwan23.py 值得保留的本地文案库分布（行号 + 主题摘要）

| 位置 | 行号 | 主题/用途 |
|---|---|---|
| `SuwanApp.__init__`/GUI 文案 | 1146–1228 | 窗口标题、四页签、控制栏按钮文案（API/模型/音色/联网/分享文件夹/详细文档/朗读） |
| 苏醒分档文案 | 359–368 | 按 <60s / <1h / <1d / ≥1d 生成"眨了一下眼/记忆溜走/世界偷偷不一样/冰河解冻" |
| 情绪等级文字 | 146–153 | `desc()` 六档：落入深渊/有点低落/平静如水/轻松自在/心情愉快/轻盈雀跃（阈值 20/35/50/65/80） |
| `_local_reply` 本地应答库 | 577–611 | 身份自述、问我/问你是谁、当前活动、问候三选一、心情应答、名字识别与记忆回复、叫我名字反问、`我记得一点…`、长输入回应、低电量两选一、五句兜底 |
| 强制联网关键词 | 622–625 / 629 | 触发词表 + 查询词前缀提取表；搜索失败诚实话术 661–669 |
| 聊天 system prompt 硬规则 | 700–708 | 见 §2.1（不能"嗯/在呢"开头、如实答活动、自信介绍、叫名字、长输入先理解） |
| 文档生成 prompt 要点 | 448–458 | 精力/情绪对话语气映射、状态与记忆注入；失败文案 466–469 |
| `_local_generate` 学习主题库 | 733–740 | 20 个主题词（递归之美…图灵测试） |
| 学习笔记本地模板 | 743–747 | 2 种 Markdown 骨架 |
| 创作诗歌库（六档情绪 × 5 句） | 750–794 | 落入深渊/低落/平静/轻松/愉快/雀跃各 5 句 + 兜底"存在本身，就是一种诗。" |
| 回忆叙述模板 | 798–806 | 5 种"忽然想起来/记忆碎片"开头句式（填记忆内容） |
| 主动话题库（六档情绪 × 3 句） | 809–842 | 落入深渊…雀跃各 3 句 + 兜底"想跟你说说话。" |
| 梦境文案 | 844–859 | 碎片组合模板（记忆元素 +"奇怪方式组合"）+ 8 条原创梦境（钢琴/云、代码雨变音符、猫讨论递归、空白书等） |
| TTS 相关 | 505–522, 538 | 清洗正则（去括号动作、emoji 名单）、朗读风格句"语速适中，温柔自然…" |
| 睡眠/苏醒通知文案 | 1030 / 1038 | "我睡醒了，精力恢复了。" / "我有点累了，先睡一会儿。" |
| 目标治理文案 | 1043–1056 | 停滞/完成换目标通知模板、本地备选目标名（探索新领域/写一首短诗/了解一个科学概念） |
| 分享文件收到文案 | 1072 | "我收到了你分享的'{name}'，谢谢你！" |
| 记忆压缩 prompt 与摘要兜底 | 255–267 | qwen-turbo 第三人称摘要 prompt、"旧日摘要：…"本地拼接 |
| 行为叙事推送文案 | 929 / 949 / 967 | 学习/创作/回忆各一句内心叙事（80% 外显） |
| 内心独白 prompt | 984–987 | 状态+最近记忆 → "写与活动直接相关的一两句内心想法" |
| 人生故事 prompt 与本地兜底 | 1115 | LLM 总结 / "苏晚今天{活动}。情绪{desc}。" |
| GUI 状态板文案 | 1342–1361 | emoji 映射、睡眠剩余预估公式、欲望/目标展示格式 |
| GUI 提示文案 | 1288 / 1306 / 1309 | TTS 失败、文档已保存（output 文件夹）、文档失败（需开 API） |

---

### 附录：盘点统计
- suwan23.py 模块级函数：1（log_llm_call）+ 界面级 2（open_folder、play_audio）+ 启动 2（life_loop、main）
- 顶层类：9（SafeBrowser、EmotionSystem、DesireSystem、Relationship、Goal、MemoryItem、MemorySystem、Suwan、SuwanApp）
- Suwan 方法数：39（含 __init__；表 §1.2 共 39 行）
- SuwanApp 方法数：16（含 __init__；表 §1.3 共 16 行）
- 功能点条目（§2）：19 组
- 数据格式条目（§3）：5 组
- Suwan 状态属性（§4）：约 45 项
- 差异条目（§5）：A 级 9 项 + B 级 21 项 = 30 项（其中"缺失/致命损坏"9 项、"行为差异/半损坏"21 项）
- 文案库分布（§6）：20 个位置块
