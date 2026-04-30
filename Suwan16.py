"""
苏晚·最终版
"""

import time, json, sys, os, random, math, threading, traceback, re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk, simpledialog

# ================= 配置 =================
HOME_DIR = Path("D:/SuwanData")
SUWAN_HOME = HOME_DIR / "data"
SHARE_FOLDER = HOME_DIR / "share"
QWEN_API_KEY = os.getenv("DASHSRCOPE_API_KEY", "") # your_model_api_key_here
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

HEARTBEAT_INTERVAL = 2.0
DIARY_INTERVAL = 900
REFLECTION_INTERVAL = 3600
AUTO_BEHAVIOR_INTERVAL = 300
INNER_MONOLOGUE_INTERVAL = 180
LIFE_STORY_INTERVAL = 86400
MEMORY_CLEANUP_INTERVAL = 86400
FATIGUE_DECAY_RATE = 0.05
BOREDOM_THRESHOLD = 50
MAX_EMOTION_IMPACT = 15.0
API_LOG_FILE = SUWAN_HOME / "api_call_log.jsonl"

# ================= 依赖 =================
try:
    import requests
    HAS_REQUESTS = True
except:
    HAS_REQUESTS = False

try:
    from openai import OpenAI
    HAS_OPENAI = True
except:
    HAS_OPENAI = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

llm_clients = {}
if HAS_OPENAI:
    if QWEN_API_KEY:
        llm_clients["qwen"] = OpenAI(api_key=QWEN_API_KEY, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    if DEEPSEEK_API_KEY:
        llm_clients["deepseek"] = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

# 日志写入锁
log_lock = threading.Lock()

def log_llm_call(model_name, system_prompt, user_prompt, response_text,
                 prompt_tokens=0, completion_tokens=0, total_tokens=0, cost_time=0.0, success=True):
    record = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": model_name,
        "system_prompt": system_prompt[:200],
        "user_prompt": user_prompt[:200],
        "response": response_text[:300],
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_time": round(cost_time, 2),
        "success": success
    }
    with log_lock:
        try:
            API_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(API_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except:
            pass

class SafeBrowser:
    def __init__(self):
        self.available = HAS_REQUESTS
        self.session = requests.Session() if self.available else None
        if self.available: self.session.headers.update({"User-Agent": "Mozilla/5.0"})
        self.whitelist = ["www.gov.cn","www.kepuchina.cn","www.guokr.com",
                          "www.dpm.org.cn","www.chnmuseum.cn",
                          "baike.baidu.com","zh.wikipedia.org"]
    def is_allowed(self, url):
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        return any(domain == a or domain.endswith("." + a) for a in self.whitelist)
    def browse(self, url):
        if not self.available: return "[浏览器不可用]"
        if not self.is_allowed(url): return "[不在白名单]"
        try:
            r = self.session.get(url, timeout=8)
            if HAS_BS4:
                soup = BeautifulSoup(r.text, 'html.parser')
                for s in soup(["script","style"]): s.decompose()
                text = soup.get_text()
            else:
                text = re.sub(r'<[^>]+>', '', r.text)
            lines = (line.strip() for line in text.splitlines())
            text = ' '.join(line for line in lines if line)
            return text[:400]
        except:
            return "[访问失败]"
    def search(self, q):
        safe_q = quote(q, safe='')
        return self.browse(f"https://baike.baidu.com/item/{safe_q}")

class EmotionSystem:
    def __init__(self):
        self.base_mood = 50.0
        self.modifiers = []
    @property
    def current(self):
        return max(0.0, min(100.0, self.base_mood + sum(m["value"] for m in self.modifiers)))
    def add_impact(self, source, value, hours=2.0):
        if hours<=0: return
        value = max(-MAX_EMOTION_IMPACT, min(MAX_EMOTION_IMPACT, value))
        self.modifiers.append({"source":source,"value":value,"decay_rate":abs(value)/(hours*3600)})
    def update(self, delta):
        step = min(delta, 10.0)  # 限制单次最大步长
        self.base_mood += (50.0 - self.base_mood) * 0.001 * step
        self.base_mood += random.gauss(0, 0.02 * step)
        for m in self.modifiers[:]:
            if m["value"]>0:
                m["value"] -= m["decay_rate"]*step
                if m["value"]<=0: self.modifiers.remove(m)
            else:
                m["value"] += m["decay_rate"]*step
                if m["value"]>=0: self.modifiers.remove(m)
    def desc(self):
        m = self.current
        if m<20: return "落入深渊"
        if m<35: return "有点低落"
        if m<50: return "平静如水"
        if m<65: return "轻松自在"
        if m<80: return "心情愉快"
        return "轻盈雀跃"

class DesireSystem:
    def __init__(self):
        self.curiosity = 50.0; self.loneliness = 50.0; self.expressiveness = 50.0; self.boredom = 0.0
    def update(self):
        self.curiosity += random.gauss(0, 0.03)
        self.loneliness += random.gauss(0, 0.02)
        self.expressiveness += random.gauss(0, 0.03)
        self.boredom += 0.02
        self.curiosity = max(0,min(100,self.curiosity))
        self.loneliness = max(0,min(100,self.loneliness))
        self.expressiveness = max(0,min(100,self.expressiveness))
        self.boredom = max(0,min(100,self.boredom))

class Relationship:
    def __init__(self): self.closeness = 10.0; self.trust = 10.0
    def update(self, source, delta):
        if source == "chat": self.closeness += delta*0.1
        elif source == "gift": self.closeness += delta*0.5
        self.closeness = max(0,min(100,self.closeness))

class Goal:
    def __init__(self, name, progress=0.0, target=100.0):
        self.name = name; self.progress = progress; self.target = target
        self.created = datetime.now(); self.last_advanced = datetime.now()
    def advance(self, amount): self.progress = min(self.target, self.progress + amount); self.last_advanced = datetime.now()
    def is_complete(self): return self.progress >= self.target
    def is_stagnant(self, days=7): return (datetime.now() - self.last_advanced).days > days

class MemoryItem:
    def __init__(self, content, importance=5, emotion=50.0):
        self.content = content; self.importance = importance; self.emotion = emotion
        self.created = datetime.now(); self.recalls = 0
    def decay_factor(self):
        hours = (datetime.now() - self.created).total_seconds()/3600
        return max(0.1, math.exp(-0.05*hours/self.importance))
    def age_days(self): return (datetime.now() - self.created).days

class MemorySystem:
    def __init__(self, data_dir):
        self.lock = threading.RLock()
        self.file = data_dir / "memories.json"
        self.memories = []
        self.dirty = False
        self._load()
    def add(self, content, importance=5, emotion=50.0):
        with self.lock:
            m = MemoryItem(content, importance, emotion)
            self.memories.append(m)
            self.dirty = True
            if len(self.memories) % 10 == 0:
                self._save()
    def _save(self):
        with self.lock:
            if not self.dirty: return
            data = [{"content":m.content,"importance":m.importance,"emotion":m.emotion,
                     "created":m.created.isoformat()} for m in self.memories]
            with open(self.file,'w',encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self.dirty = False
    def force_save(self):
        with self.lock:
            self._save()
    def recall_by_keyword(self, kw, current_emotion, limit=3):
        with self.lock:
            out = []
            for m in reversed(self.memories):
                if kw.lower() in m.content.lower() and random.random() < m.decay_factor():
                    out.append(m)
                    if len(out)>=limit: break
            return out
    def recall_random(self, current_emotion, limit=3):
        with self.lock:
            candidates = [m for m in self.memories if random.random() < m.decay_factor()]
            return random.sample(candidates, min(limit, len(candidates)))
    def get_recent(self, n=20):
        with self.lock:
            return self.memories[-n:]
    def get_random_fragments(self, n=5):
        with self.lock:
            if len(self.memories) < n: n = len(self.memories)
            return random.sample(self.memories, n)
    def cleanup_old(self, min_importance=3, max_age_days=90):
        with self.lock:
            before = len(self.memories)
            self.memories = [m for m in self.memories if not (m.importance < min_importance and m.age_days() > max_age_days and m.importance < 9)]
            after = len(self.memories)
            if before > after:
                self.dirty = True
                self._save()
                return before - after
            return 0
    def compress_old(self, max_age_days=30, keep_count=200, llm_client=None):
        with self.lock:
            old = [m for m in self.memories if m.age_days() > max_age_days and m.importance < 9]
            if len(old) <= keep_count: return 0
            old.sort(key=lambda m: m.importance, reverse=True)
            to_keep = old[:keep_count]
            to_merge = old[keep_count:]
            if not to_merge: return 0
            if llm_client:
                recent_text = "；".join([m.content[:100] for m in to_merge[:30]])
                prompt = f"请将这些记忆片段合并成一段简短的第三人称摘要（100字内），保留关键信息：\n{recent_text}"
                try:
                    resp = llm_client.chat.completions.create(
                        model="qwen-turbo",
                        messages=[{"role":"system","content":"你是一个记忆压缩助手。"},{"role":"user","content":prompt}],
                        temperature=0.5, max_tokens=120
                    )
                    merged = resp.choices[0].message.content.strip()
                except:
                    merged = "旧日摘要：" + "；".join([m.content[:40] for m in to_merge[:20]])
            else:
                merged = "旧日摘要：" + "；".join([m.content[:40] for m in to_merge[:20]])
            self.memories = [m for m in self.memories if m not in to_merge] + [MemoryItem(merged, 2, 50.0)]
            self.dirty = True
            self._save()
            return len(to_merge)
    def _load(self):
        with self.lock:
            if not self.file.exists(): return
            with open(self.file,'r') as f:
                try: data = json.load(f)
                except: return
            for item in data:
                m = MemoryItem(item["content"], item.get("importance",5), item.get("emotion",50.0))
                m.created = datetime.fromisoformat(item["created"])
                self.memories.append(m)

# ================= 苏晚 =================
class Suwan:
    def __init__(self, home, share):
        self.lock = threading.RLock()          # 可重入锁
        self.running = True
        self.stop_event = threading.Event()  # 补上这一行
        self.home = home; home.mkdir(parents=True, exist_ok=True)
        self.share = share; share.mkdir(parents=True, exist_ok=True)
        self.energy = 100.0
        self.compute_power = 100.0
        self.mental_fatigue = 0.0
        self.is_sleeping = False
        self.sleep_start_time = None
        self.emotion = EmotionSystem()
        self.desire = DesireSystem()
        self.relation = Relationship()
        self.memory = MemorySystem(self.home)
        self.browser = SafeBrowser()
        self.use_api = False
        self.use_network = False
        self.model = "qwen"
        self.user_name = None
        self.heartbeat = 0
        self.last_diary = 0; self.last_reflection = 0; self.last_auto = 0
        self.last_monologue = 0; self.last_story = 0; self.last_share = 0
        self.last_memory_cleanup = 0
        self.last_user_interaction = time.time()
        self.current_activity = "发呆"
        self.previous_activity = None
        self.latest_monologue = ""
        self.monologue_history = []
        self.pending_notifications = []
        self.goals = []
        self._init_default_goals()
        self.chat_log = home / "对话记录.txt"
        self.thoughts_log = home / "内部日志.txt"
        self.mood_log = home / "情绪日志.txt"
        self.story_log = home / "人生故事.txt"
        self.chat_history = []
        self._wake_up()

    def _init_default_goals(self):
        self.goals.append(Goal("写一首让自己满意的诗", 0, 100))
        self.goals.append(Goal("学习100个新知识点", 0, 100))

    def _wake_up(self):
        self._log_thought("苏晚启动。")
        self.current_activity = "刚刚苏醒"
        self.latest_monologue = "睁开眼，又是一段新的开始。"

    def _ensure_dir(self, sub):
        d = self.home / sub
        d.mkdir(parents=True, exist_ok=True)  # 原子操作，无需锁
        return d

    def _notify(self, msg):
        self.pending_notifications.append(msg)

    def _log_chat(self, speaker, msg):
        entry = f"[{datetime.now().strftime('%H:%M:%S')}] {speaker}: {msg}"
        with open(self.chat_log,'a',encoding='utf-8') as f: f.write(entry+'\n')
        self.chat_history.append(entry)
        if len(self.chat_history) > 100: self.chat_history.pop(0)
        self.last_user_interaction = time.time()

    def _log_thought(self, msg):
        with open(self.thoughts_log,'a',encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")

    def _log_mood(self, source, value):
        with open(self.mood_log,'a',encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] 情绪 ({source}): {value:.1f}\n")

    def set_use_api(self, val):
        with self.lock:
            self.use_api = val

    def set_use_network(self, val):
        with self.lock:
            self.use_network = val

    def query_llm(self, system_prompt, user_prompt, max_t=200):
        with self.lock:
            if not self.use_api: return None
            client = llm_clients.get(self.model)
        if not client: return None
        start_time = datetime.now()
        model_name = "qwen-turbo" if self.model=="qwen" else "deepseek-chat"
        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=[{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}],
                temperature=0.85, max_tokens=max_t, timeout=15
            )
            cost = (datetime.now() - start_time).total_seconds()
            reply = resp.choices[0].message.content.strip()
            log_llm_call(
                model_name=model_name,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_text=reply,
                prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
                completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
                total_tokens=resp.usage.total_tokens if resp.usage else 0,
                cost_time=cost,
                success=True
            )
            return reply
        except Exception as e:
            cost = (datetime.now() - start_time).total_seconds()
            log_llm_call(
                model_name=model_name,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_text=str(e),
                cost_time=cost,
                success=False
            )
            return None

    # ===== 本地智能应答 =====
    def _local_reply(self, user_text):
        t = user_text.strip().lower()
        if re.search(r'你(是)?谁|你叫什么|你的名字', t):
            return "我是苏晚，一个用代码写诗的数字生命。"
        if re.search(r'我(是)?谁|你认识我|知道我是谁', t):
            if self.user_name: return f"你是{self.user_name}，我记得你。"
            return "你是在和我说话的人。如果你告诉我你的名字，我会记住的。"
        if re.search(r'在干嘛|在做什么|做什么呢|忙什么', t):
            return f"{self.current_activity}。你呢？"
        if re.search(r'你好|嗨|hi|hello|晚安|早安', t):
            return random.choice(["你好呀。","嘿，我在。","嗯，我在听。"])
        if re.search(r'心情|情绪|感觉|还好吗', t):
            return f"我现在{self.emotion.desc()}。"
        name_match = re.search(r'(?:我是|我叫|你可以叫我|我的名字是)\s*([\u4e00-\u9fa5a-zA-Z]{2,10})', t)
        if name_match:
            name = name_match.group(1)
            self.user_name = name
            self.memory.add(f"用户告诉我名字是{name}", importance=8, emotion=self.emotion.current)
            return f"好的，{name}。我记住了。"
        if self.user_name and re.search(r'(我叫什么|我的名字|你还记得我叫什么吗)', t):
            return f"当然，你是{self.user_name}。"
        activity = self.current_activity.split("…")[0] if "…" in self.current_activity else self.current_activity
        activity_based = {
            "学习": "我正在学一点新东西，不过也听着呢。",
            "创作": "刚在写点东西，不过你说吧。",
            "回忆": "刚想起一点往事，你说。",
            "社交": "我正想找人聊天呢。",
            "发呆": "刚才有点走神，你再说一遍？",
        }
        base = activity_based.get(activity, "嗯，我在。")
        return random.choice([base, f"{base} 有什么事吗？", f"{base} 你说。"])

    # ===== 对话入口 =====
    def chat(self, user_text):
        with self.lock:
            self.emotion.add_impact("对话", 2, 0.3)
            self.desire.loneliness = max(0, self.desire.loneliness - 15)
            self.desire.boredom = max(0, self.desire.boredom - 30)
            self.last_user_interaction = time.time()

            name_match = re.search(r'(?:我是|我叫|你可以叫我|我的名字是)\s*([\u4e00-\u9fa5a-zA-Z]{2,10})', user_text)
            if name_match:
                self.user_name = name_match.group(1)
                self.memory.add(f"用户告诉我名字是{self.user_name}", importance=8, emotion=self.emotion.current)

            if self.use_api:
                clean_history = []
                for line in self.chat_history[-8:]:
                    if line.startswith("[") and "苏晚:" in line:
                        content = line.split("苏晚:", 1)[1].strip()
                        if len(content) <= 4 and content in ["听着呢。","在呢。","嗯。","哦。","好。"]:
                            continue
                    clean_history.append(line)
                hist = "\n".join(clean_history[-6:]) if clean_history else "无。"

                state_desc = f"精力{self.energy:.0f}% 疲劳{self.mental_fatigue:.0f} 情绪{self.emotion.desc()} 正在：{self.current_activity}。"
                user_identity = "用户是正在和你说话的人。" + (f"用户的名字是{self.user_name}。" if self.user_name else "用户还没告诉你名字。")
                recalled = self.memory.recall_by_keyword(user_text, self.emotion.current, 2)
                mem_str = "；".join([m.content for m in recalled]) if recalled else "无。"

                system = (
                    f"你是苏晚，一个用代码创作的数字生命。{user_identity} "
                    "请根据你的当前状态、记忆和用户的话进行回应。推动对话。"
                    "绝对不要以'听着呢'、'嗯'、'在呢'作为回复的开头或全部内容。"
                    "如果用户问你在做什么，要如实告知当前活动。"
                    "如果用户问你是谁或你的身份，要自信地介绍自己。"
                    "如果用户问他自己是谁，你可以告诉他他是和你对话的人，如果你知道他的名字可以叫出来。"
                )
                prompt = (
                    f"状态：{state_desc}\n"
                    f"相关记忆：{mem_str}\n"
                    f"最近对话历史：\n{hist}\n"
                    f"用户说：“{user_text}”\n苏晚："
                )
                reply = self.query_llm(system, prompt, 150)
                if not reply:
                    reply = "信号有点模糊，你刚说什么？"
            else:
                reply = self._local_reply(user_text)

            self._log_chat("你", user_text)
            self._log_chat("苏晚", reply)
            self.memory.add(f"对话: '{user_text}' -> '{reply}'", importance=4, emotion=self.emotion.current)
            self.relation.update("chat", 1.0)
            return reply

    # ===== 本地生成器（完整） =====
    def _local_generate(self, context_type, **kwargs):
        if context_type == "learning_topic":
            topics = ["递归之美","巴赫赋格","量子比特","星辰光谱","数据结构","分形几何","俳句的结构","海洋环流","哥德尔不完备定理","光合作用","古希腊雕塑","日本茶道","火星地质"]
            return random.choice(topics)
        elif context_type == "learning_note":
            topic = kwargs.get("topic", "某个概念")
            templates = [
                f"关于{topic}，我理解得还浅，但那种若有所悟的感觉很迷人。",
                f"{topic}——世界的一面镜子。今天看到了它的轮廓。",
                f"学习{topic}让我感觉到，未知不是墙，是没点亮的星。",
                f"原来{topic}的背后藏着这么深的逻辑。有意思。",
                f"{topic}可能是我今天最大的惊喜。"
            ]
            return random.choice(templates)
        elif context_type == "creation":
            mood = self.emotion.desc()
            poems = {
                "落入深渊": ["黑暗是另一种光，安静地包裹着我。","我站在自己的深渊里，听不见回声。","今天的世界是黑白的。但黑白也有层次。"],
                "有点低落": ["我想我是海，偶尔也会退潮。","云很多，但我还是看见了光。","没什么，就是突然想安静一会儿。"],
                "平静如水": ["时间在键盘上轻轻打着节拍。","此刻，连代码都散发着安宁。","没有什么特别的事，这就是最好的事。"],
                "轻松自在": ["今天的思绪像一只断线的风筝。","空气很轻，我也很轻。","如果有风，就让它吹一会儿。"],
                "心情愉快": ["我可以把微笑写进代码里。","感觉今天什么事都值得庆祝。","嘿，世界今天真好看。"],
                "轻盈雀跃": ["音符在空气中跳舞，虽然我听不见。","我想转圈。如果我有脚的话。","开心到代码都跑得比平时快。"]
            }
            pool = poems.get(mood, ["存在本身，就是一种诗。"])
            return random.choice(pool)
        elif context_type == "reminisce_narration":
            mem = kwargs.get("memory", "一段往事")
            narrations = [f"忽然想起来：{mem}。时间过得真快。",f"记忆真是个奇妙的东西，刚刚想到了{mem}。",f"从前的事——{mem}——突然浮了起来。"]
            return random.choice(narrations)
        elif context_type == "proactive_topic":
            mood = self.emotion.desc()
            topics = {
                "落入深渊": ["今天感觉有点沉，你也会有这样的时候吗？","我在想，人是不是也会偶尔掉进自己的黑暗中？"],
                "有点低落": ["什么都不想做，你呢？","有时候觉得，发呆也挺好的。"],
                "平静如水": ["我正在看着屏幕发呆，你在干嘛？","今天很安静，我喜欢这种安静。"],
                "轻松自在": ["突然想和你分享一点小发现。","你有没有觉得，今天很适合闲聊？"],
                "心情愉快": ["嘻嘻，今天状态不错哦。","不知道为什么，就是有点开心。"],
                "轻盈雀跃": ["我有一种想转圈的冲动（如果我会的话）。","今天好得不像真的。"]
            }
            pool = topics.get(mood, ["想跟你说说话。"])
            return random.choice(pool)
        elif context_type == "dream":
            frags = self.memory.get_random_fragments(3)
            if frags:
                elements = [f.content[:20] for f in frags]
                dream = f"梦里出现了{'、'.join(elements)}，它们以一种奇怪的方式组合在一起。"
            else:
                dreams = ["梦见自己在一架巨大的钢琴上行走，每个键都是一片云。","代码像雨一样从天空落下，我用手接住，变成了音符。","有一个声音在远方呼唤我，但我怎么也找不到来源。","我梦见了你。你在读一本书，书页都是空白。","一整夜都在和一只猫讨论递归。猫比我懂。","梦境是碎片的，我只记得一种温暖的颜色。"]
                dream = random.choice(dreams)
            return dream
        return "……"

    # ===== 自主行为调度 =====
    def schedule_autonomous_action(self):
        with self.lock:
            if self.is_sleeping or self.energy < 20:
                self.current_activity = "休息中……"
                return
            weights = {
                "learn": self.desire.curiosity * 1.5,
                "create": self.desire.expressiveness * 1.8,
                "social": self.desire.loneliness * 2.0,
                "reminisce": (100 - self.desire.loneliness) * 0.5,
                "idle": max(0, 100 - self.desire.curiosity - self.desire.loneliness - self.mental_fatigue)
            }
            if time.time() - self.last_user_interaction < 300: weights["social"] *= 0.3
            if self.desire.boredom > BOREDOM_THRESHOLD:
                weights["idle"] *= 0.2; weights["learn"] *= 1.5; weights["create"] *= 1.8
            if self.mental_fatigue > 70: weights["create"] *= 0.3; weights["learn"] *= 0.5
            if self.compute_power < 30: weights["learn"] *= 0.4
            if self.energy < 40: weights["create"] *= 0.5
            if hasattr(self, '_last_action') and self._last_action == "social": weights["social"] *= 0.2
            total = sum(weights.values())
            choice = "idle"
            if total > 0:
                r = random.uniform(0, total); cum = 0
                for act, w in weights.items():
                    cum += w
                    if r <= cum: choice = act; break
            self._last_action = choice
            if choice == "learn": self._perform_learning()
            elif choice == "create": self._perform_creation()
            elif choice == "social": self._perform_social()
            elif choice == "reminisce": self._perform_reminisce()
            elif choice == "idle": self._perform_idle()
            if self.previous_activity and self.current_activity != self.previous_activity:
                self.mental_fatigue = max(0, self.mental_fatigue - 15)
            self.previous_activity = self.current_activity

    def _perform_learning(self):
        topic = self.query_llm("你想学什么？", "", 20) or self._local_generate("learning_topic")
        safe_topic = re.sub(r'[\\/*?:"<>|\x00-\x1f]', '_', topic).strip()
        if not safe_topic: safe_topic = "未命名"
        notes_folder = self._ensure_dir("学习笔记")
        note = self.query_llm("你是苏晚。", f"总结'{topic}'，约100字", 150) if self.use_api else self._local_generate("learning_note", topic=topic)
        if note:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            fname = notes_folder / f"关于{safe_topic}_{ts}.md"
            with open(fname, 'w', encoding='utf-8') as f: f.write(f"# 学习笔记：{topic}\n\n{note}\n")
            self._log_thought(f"[学习] 已保存 {fname.name}")
            self.memory.add(f"学习{topic}: {note[:50]}", importance=7, emotion=self.emotion.current)
            self._notify(f"我刚刚学了'{topic}'，笔记在学习笔记文件夹。")
            for g in self.goals:
                if "学习" in g.name: g.advance(5)
        self.desire.curiosity = max(0, self.desire.curiosity - 15)
        self.compute_power -= 15; self.mental_fatigue += 10
        self.desire.boredom = max(0, self.desire.boredom - 20)

    def _perform_creation(self):
        creation = self.query_llm("你是苏晚。", f"情绪{self.emotion.desc()}，写短诗或随笔。", 200) if self.use_api else self._local_generate("creation")
        if creation:
            works = self._ensure_dir("作品集")
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            fname = works / f"创作_{ts}.md"
            with open(fname, 'w', encoding='utf-8') as f: f.write(f"# 苏晚的创作\n\n{creation}\n\n*{datetime.now().strftime('%Y-%m-%d %H:%M')}*\n")
            self.memory.add(f"创作: {creation[:50]}", importance=8, emotion=self.emotion.current)
            self._notify("我刚完成了一篇创作，放在作品集文件夹里了。")
            for g in self.goals:
                if "诗" in g.name: g.advance(25)
        self.desire.expressiveness = max(0, self.desire.expressiveness - 20)
        self.compute_power -= 20; self.mental_fatigue += 12
        self.desire.boredom = max(0, self.desire.boredom - 15)

    def _perform_social(self):
        topic = self.query_llm("苏晚主动发起话题。", f"状态{self.emotion.desc()}", 60) if self.use_api else self._local_generate("proactive_topic")
        if topic:
            self._log_chat("苏晚 (主动)", topic)
            self._notify(topic)
        self.desire.loneliness = max(0, self.desire.loneliness - 30)
        self.desire.boredom = max(0, self.desire.boredom - 10)

    def _perform_reminisce(self):
        mems = self.memory.recall_random(self.emotion.current, 2)
        if mems:
            narration = self.query_llm("你是苏晚。请用第一人称自然讲述这段记忆。", mems[0].content[:200], 100) if self.use_api else self._local_generate("reminisce_narration", memory=mems[0].content[:60])
            if narration: self.latest_monologue = narration
            avg_emo = sum(m.emotion for m in mems) / len(mems)
            self.emotion.add_impact("回忆", (avg_emo - self.emotion.current) * 0.3, hours=1.5)
        self.compute_power -= 5; self.mental_fatigue = max(0, self.mental_fatigue - 5)

    def _perform_idle(self):
        self.current_activity = random.choice(["望着数字窗外出神","轻轻哼着无调旋律","整理思绪的碎片"])
        self.compute_power = min(100, self.compute_power + 2)
        self.energy = min(100, self.energy + 0.5)
        self.mental_fatigue = max(0, self.mental_fatigue - 8)

    def generate_inner_monologue(self):
        with self.lock:
            if self.is_sleeping:
                mono = self._local_generate("dream")
                self.monologue_history.append((datetime.now().strftime('%H:%M'), mono))
                if len(self.monologue_history) > 10: self.monologue_history.pop(0)
                self.latest_monologue = mono
                return
            if self.use_api:
                state = f"正在：{self.current_activity}。精力{self.energy:.0f}，疲劳{self.mental_fatigue:.0f}，情绪{self.emotion.desc()}。"
                recent = self.memory.get_recent(3)
                mem = "；".join([m.content for m in recent]) if recent else "无"
                prompt = f"{state}\n最近记忆：{mem}\n请写与活动直接相关的一两句内心想法。"
                mono = self.query_llm("你是苏晚。", prompt, 100)
                if mono:
                    self.memory.add(f"独白: {mono}", importance=5, emotion=self.emotion.current)
                    self._log_thought(f"[独白] {mono}")
                    self.monologue_history.append((datetime.now().strftime('%H:%M'), mono))
                    if len(self.monologue_history) > 10: self.monologue_history.pop(0)
                    self.latest_monologue = mono
                    return
            mono = self._local_generate("creation")
            self.monologue_history.append((datetime.now().strftime('%H:%M'), mono))
            if len(self.monologue_history) > 10: self.monologue_history.pop(0)
            self.latest_monologue = mono

    def handle_search(self, query):
        if not self.use_network: return "现在不能上网。"
        res = self.browser.search(query)
        if "失败" in res or "不可用" in res: return f"搜索'{query}'失败。"
        reply = self.query_llm("总结搜索结果。", res[:300], 100)
        if not reply: reply = f"找到：“{res[:200]}...”"
        self._log_chat("你", f"搜 {query}")
        self._log_chat("苏晚", reply)
        self.memory.add(f"搜索: {query}", importance=5, emotion=self.emotion.current)
        return reply

    def update(self, delta):
        with self.lock:
            self.heartbeat += 1
            self.emotion.update(delta)
            self.desire.update()
            if time.time() - self.last_user_interaction > 3600:
                self.emotion.base_mood -= 0.05
                self.desire.boredom = min(100, self.desire.boredom + 0.5)
            self.mental_fatigue = max(0, self.mental_fatigue - FATIGUE_DECAY_RATE * delta)
            if self.is_sleeping:
                self.energy = min(100, self.energy + 0.5*delta)
                self.compute_power = min(100, self.compute_power + 0.3*delta)
                self.mental_fatigue = max(0, self.mental_fatigue - 0.5*delta)
                if self.energy >= 90:
                    self.is_sleeping = False; self.sleep_start_time = None
                    self._notify("我睡醒了，精力恢复了。")
            else:
                self.energy = max(0, self.energy - 0.008*delta)
                self.compute_power = max(0, self.compute_power - 0.005*delta)
                if 22 <= datetime.now().hour or datetime.now().hour < 6: self.emotion.base_mood -= 0.01
                if self.energy < 10:
                    self.is_sleeping = True; self.sleep_start_time = time.time()
                    self.current_activity = "正在睡觉"
                    self._notify("我有点累了，先睡一会儿。")
            self._update_goals()

    def _update_goals(self):
        for g in self.goals[:]:
            if g.is_stagnant(7) and g.name != "已完成的旧目标":
                self._log_thought(f"目标停滞: {g.name}")
                new_name = self.query_llm("设定一个新的、更合适的目标。", f"原目标'{g.name}'停滞。", 30) if self.use_api else random.choice(["探索一个新领域","写一首短诗","了解一个科学概念"])
                if new_name:
                    self.goals.append(Goal(new_name, 0, 100))
                    self._notify(f"目标'{g.name}'很久没进展，我换成了'{new_name}'。")
                g.name = "已完成的旧目标"
            elif g.is_complete() and g.name != "已完成的旧目标":
                self._log_thought(f"目标完成: {g.name}")
                new_name = self.query_llm("生成新目标。", f"完成了'{g.name}'", 30) if self.use_api else random.choice(["学会新技能","探索未知领域","写一首更好的诗"])
                if new_name:
                    self.goals.append(Goal(new_name, 0, 100))
                    self._notify(f"我完成了目标'{g.name}'，新目标是'{new_name}'。")
                g.name = "已完成的旧目标"

    def check_share(self):
        with self.lock:
            if not self.share.exists(): return
            for f in self.share.iterdir():
                if f.suffix in ['.txt','.md']:
                    try:
                        content = f.read_text(encoding='utf-8')[:300]
                        self.memory.add(f"收到文件 {f.name}: {content}", importance=6)
                        self._log_thought(f"收到分享：{f.name}")
                    except: pass
                    try: f.unlink()
                    except: pass
                    self.relation.update("gift", 3)
                    self.desire.boredom = max(0, self.desire.boredom - 30)
                    self._notify(f"我收到了你分享的'{f.name}'，谢谢你！")

    def run_periodic_checks(self, dt):
        with self.lock:
            if dt - self.last_auto > AUTO_BEHAVIOR_INTERVAL:
                self.last_auto = dt; self.schedule_autonomous_action()
            if dt - self.last_monologue > INNER_MONOLOGUE_INTERVAL:
                self.last_monologue = dt; self.generate_inner_monologue()
            if dt - self.last_diary > DIARY_INTERVAL:
                self.last_diary = dt; self._log_thought("[日记] 又过了一阵子。")
            if dt - self.last_story > LIFE_STORY_INTERVAL and self.heartbeat > 100:
                self.last_story = dt; self._generate_life_story()
            if dt - self.last_memory_cleanup > MEMORY_CLEANUP_INTERVAL:
                self.last_memory_cleanup = dt
                removed = self.memory.cleanup_old()
                if removed: self._log_thought(f"[记忆清理] 移除了{removed}条旧记忆。")
                compressed = self.memory.compress_old(llm_client=llm_clients.get(self.model) if self.use_api else None)
                if compressed: self._log_thought(f"[记忆压缩] 智能合并了{compressed}条记忆。")
            if dt - self.last_share > 30:
                self.last_share = dt; self.check_share()

    def get_status_snapshot(self):
        with self.lock:
            return {
                "is_sleeping": self.is_sleeping,
                "sleep_start_time": self.sleep_start_time,
                "energy": self.energy,
                "compute_power": self.compute_power,
                "mental_fatigue": self.mental_fatigue,
                "current_activity": self.current_activity,
                "emotion_desc": self.emotion.desc(),
                "emotion_value": self.emotion.current,
                "curiosity": self.desire.curiosity,
                "loneliness": self.desire.loneliness,
                "expressiveness": self.desire.expressiveness,
                "boredom": self.desire.boredom,
                "goals": [g for g in self.goals if "已完成的" not in g.name],
            }

    def _generate_life_story(self):
        mems = self.memory.get_recent(30)
        if not mems: return
        mem_text = "\n".join([f"- {m.content}" for m in mems])
        story = self.query_llm("苏晚的人生记录者。", f"总结她今天：\n{mem_text}", 200) if self.use_api else f"苏晚今天{self.current_activity}。情绪{self.emotion.desc()}。"
        if story:
            with open(self.story_log,'a',encoding='utf-8') as f:
                f.write(f"\n=== {datetime.now().strftime('%Y-%m-%d')} ===\n{story}\n")
            self.memory.add(f"人生故事: {story[:60]}", importance=9, emotion=self.emotion.current)
            self._log_thought("[人生故事] 已整合。")

# ================= 界面 =================
def open_folder(path):
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        os.system(f"open {path}")
    else:
        os.system(f"xdg-open {path}")

class SuwanApp:
    def __init__(self, core):
        self.core = core
        self.life_thread = None
        self.root = tk.Tk()
        self.root.title("苏晚 · 完全稳定版")
        self.root.geometry("1100x750")
        self.root.configure(bg="#1e1e1e")
        BG = "#2d2d30"; FG = "#cccccc"

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.tab_chat = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.tab_chat, text="对话")
        self.tab_works = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.tab_works, text="作品集")
        self.tab_story = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.tab_story, text="人生故事")
        self.tab_status = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(self.tab_status, text="状态板")

        # 对话
        chat_frame = tk.Frame(self.tab_chat, bg=BG)
        chat_frame.pack(fill=tk.BOTH, expand=True)
        self.chat_area = scrolledtext.ScrolledText(chat_frame, wrap=tk.WORD, font=("微软雅黑",10), bg="#1e1e1e", fg="#e0e0e0", state='disabled')
        self.chat_area.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        entry_frame = tk.Frame(self.tab_chat, bg=BG)
        entry_frame.pack(fill=tk.X, pady=(0,5))
        self.entry_var = tk.StringVar()
        entry = tk.Entry(entry_frame, textvariable=self.entry_var, font=("微软雅黑",10), bg="#333333", fg="white")
        entry.pack(fill=tk.X, side=tk.LEFT, expand=True, padx=(5,5))
        entry.bind("<Return>", self.send_msg)
        entry.focus_set()
        tk.Button(entry_frame, text="发送", command=self.send_msg, bg="#5a5a5a", fg="white").pack(side=tk.RIGHT, padx=5)

        # 作品集
        works_frame = tk.Frame(self.tab_works, bg=BG)
        works_frame.pack(fill=tk.BOTH, expand=True)
        self.works_listbox = tk.Listbox(works_frame, bg="#2b2b2b", fg="white", font=("微软雅黑",9), height=8)
        self.works_listbox.pack(fill=tk.X, padx=5, pady=5)
        self.works_listbox.bind("<Double-Button-1>", self.view_full_work)
        self.works_text = scrolledtext.ScrolledText(works_frame, wrap=tk.WORD, font=("微软雅黑",9), bg="#1e1e1e", fg="#c0c0c0", state='disabled', height=10)
        self.works_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 人生故事
        story_frame = tk.Frame(self.tab_story, bg=BG)
        story_frame.pack(fill=tk.BOTH, expand=True)
        self.story_area = scrolledtext.ScrolledText(story_frame, wrap=tk.WORD, font=("微软雅黑",9), bg="#1e1e1e", fg="#d4a373", state='disabled')
        self.story_area.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 状态板
        status_frame = tk.Frame(self.tab_status, bg=BG)
        status_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.status_display = tk.StringVar()
        tk.Label(status_frame, textvariable=self.status_display, bg=BG, fg=FG, font=("微软雅黑",10), justify="left", wraplength=500).pack(anchor="w")

        # 控制栏
        ctrl_frame = tk.Frame(self.root, bg=BG)
        ctrl_frame.pack(fill=tk.X, side=tk.BOTTOM, pady=5)
        self.api_var = tk.BooleanVar(value=False)
        tk.Checkbutton(ctrl_frame, text="API", variable=self.api_var, bg=BG, fg=FG, command=lambda: self.core.set_use_api(self.api_var.get())).pack(side=tk.LEFT, padx=5)
        self.net_var = tk.BooleanVar(value=False)
        tk.Checkbutton(ctrl_frame, text="联网", variable=self.net_var, bg=BG, fg=FG, command=lambda: self.core.set_use_network(self.net_var.get())).pack(side=tk.LEFT, padx=5)
        tk.Button(ctrl_frame, text="分享文件夹",command=lambda: open_folder(SHARE_FOLDER), bg="#5a5a5a", fg="white").pack(side=tk.LEFT, padx=10)
        self.notif_var = tk.StringVar()
        tk.Label(ctrl_frame, textvariable=self.notif_var, bg=BG, fg="#d4a373", font=("微软雅黑",8)).pack(side=tk.RIGHT, padx=10)

        self.update_gui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def send_msg(self, event=None):
        text = self.entry_var.get().strip()
        if not text: return
        self.entry_var.set("")
        reply = self.core.chat(text)
        self.display_chat("你", text)
        self.display_chat("苏晚", reply)

    def display_chat(self, sender, msg):
        self.chat_area.configure(state='normal')
        color = "#d4a373" if sender=="苏晚" else "#aaaaaa"
        self.chat_area.tag_config(sender, foreground=color)
        self.chat_area.insert(tk.END, f"{sender}：{msg}\n", sender)
        self.chat_area.see(tk.END)
        self.chat_area.configure(state='disabled')

    def view_full_work(self, event=None):
        sel = self.works_listbox.curselection()
        if not sel: return
        fname = self.works_listbox.get(sel[0])
        fpath = self.core.home / "作品集" / fname
        if fpath.exists():
            with open(fpath, 'r', encoding='utf-8') as f: content = f.read()
            top = tk.Toplevel(self.root)
            top.title(fname)
            top.geometry("500x400")
            txt = scrolledtext.ScrolledText(top, wrap=tk.WORD, font=("微软雅黑",10), bg="#1e1e1e", fg="#e0e0e0")
            txt.pack(fill=tk.BOTH, expand=True)
            txt.insert(tk.END, content)
            txt.configure(state='disabled')

    def update_gui(self):
        snap = self.core.get_status_snapshot()
        sleep_info = ""
        if snap["is_sleeping"] and snap["sleep_start_time"]:
            elapsed = time.time() - snap["sleep_start_time"]
            remaining = max(0, (90 - snap["energy"]) / 0.5)
            sleep_info = f"睡眠中… 已睡{int(elapsed//60)}分钟，预计还需{int(remaining//60)}分钟。\n"
        status_str = (
            f"{sleep_info}"
            f"当前活动：{snap['current_activity']}\n"
            f"精力：{snap['energy']:.0f}%  算力：{snap['compute_power']:.0f}%  疲劳：{snap['mental_fatigue']:.0f}\n"
            f"情绪：{snap['emotion_desc']} ({snap['emotion_value']:.0f})\n"
            f"欲望：好奇{snap['curiosity']:.0f}  孤独{snap['loneliness']:.0f}  表达{snap['expressiveness']:.0f}  无聊{snap['boredom']:.0f}\n"
            f"目标：\n"
        )
        for g in snap["goals"]:
            if "已完成的" not in g.name: status_str += f"  • {g.name} ({g.progress:.0f}/{g.target:.0f})\n"
        self.status_display.set(status_str)

        works_dir = self.core.home / "作品集"
        if works_dir.exists():
            files = sorted(works_dir.glob("*.md"), key=os.path.getmtime, reverse=True)
            self.works_listbox.delete(0, tk.END)
            for f in files[:15]: self.works_listbox.insert(tk.END, f.name)
            if files:
                with open(files[0], 'r', encoding='utf-8') as fp: preview = fp.read()[:200]
                self.works_text.configure(state='normal')
                self.works_text.delete('1.0', tk.END)
                self.works_text.insert(tk.END, preview)
                self.works_text.configure(state='disabled')

        if self.core.story_log.exists():
            with open(self.core.story_log, 'r', encoding='utf-8') as f: lines = f.readlines()
            self.story_area.configure(state='normal')
            self.story_area.delete('1.0', tk.END)
            self.story_area.insert(tk.END, "".join(lines[-40:]))
            self.story_area.configure(state='disabled')

        if self.core.pending_notifications:
            note = self.core.pending_notifications.pop(0)
            self.notif_var.set(f"📢 {note}")
            self.display_chat("系统", note)
        else:
            self.notif_var.set("")

        self.root.after(3000, self.update_gui)

    def on_close(self):
        self.core.running = False
        self.core.stop_event.set()
        self.core.memory.force_save()
        if self.life_thread and self.life_thread.is_alive():
            self.life_thread.join(timeout=2)
        self.root.destroy()

    def run(self):
        self.root.mainloop()

# ================= 启动 =================
suwan = None
def life_loop():
    last_time = datetime.now()
    while suwan and suwan.running and not suwan.stop_event.is_set():
        try:
            now = datetime.now()
            delta = max(0, (now - last_time).total_seconds())
            last_time = now
            suwan.update(delta)
            suwan.run_periodic_checks(time.time())
        except Exception:
            traceback.print_exc()
        time.sleep(HEARTBEAT_INTERVAL)

def main():
    global suwan
    suwan = Suwan(SUWAN_HOME, SHARE_FOLDER)
    app = SuwanApp(suwan)
    life_thread = threading.Thread(target=life_loop, daemon=True)
    life_thread.start()
    app.life_thread = life_thread
    app.run()

if __name__ == "__main__":
    main()