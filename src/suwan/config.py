"""配置：Settings 数据类 + 环境变量加载。

约定（吸取旧版教训，见 bug-registry B05/B13）：
- .env 在 Settings.from_env()/load_environment() 时统一加载；离线复验显式禁用；
- 没有"运行时才想起来 load"的隐式流程；key 一律从环境变量读取；
- 数据默认根仍为 D:/SuwanData（与旧版一致），v2 数据放 <root>/v2，不覆盖旧文件；
  旧数据只读导入（Migrator，见 M4）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

try:  # python-dotenv 是运行依赖；缺失时优雅降级（仅环境变量可用）
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

# 旧版默认数据根（环境变量 SUWAN_HOME 可覆盖）
DEFAULT_HOME_DIR = Path("D:/SuwanData")

# 供应商 -> (环境变量名, base_url, 默认模型)
# 全部走 OpenAI 兼容协议（Anthropic/原生 Gemini 等非兼容端点不在此列）。
# 默认模型可用环境变量 MODEL_<PROVIDER大写> 覆盖（如 MODEL_OPENAI=gpt-4o）。
ENDPOINT_ENV = {
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-chat"),
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini"),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.0-flash"),
    "qwen": ("DASHSRCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo"),
    "moonshot": ("MOONSHOT_API_KEY", "https://api.moonshot.cn/v1", "kimi-k2-0711-preview"),
    "glm": ("ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4/", "glm-4-flash"),
    "mimo": ("MINIMAX_API_KEY", "https://token-plan-cn.xiaomimimo.com/v1", "mimo-v2.5-pro"),
    "ollama": ("OLLAMA_API_KEY", "http://127.0.0.1:11434/v1", "llama3.1"),
}

# 不需要密钥即可调用的本地供应商（如 ollama）
KEYLESS_PROVIDERS = frozenset({"ollama"})

# GUI/界面展示用的供应商友好名（键同 ENDPOINT_ENV）
PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "openai": "ChatGPT/OpenAI",
    "gemini": "Gemini",
    "qwen": "通义千问",
    "moonshot": "Kimi(Moonshot)",
    "glm": "智谱GLM",
    "mimo": "MiniMax",
    "ollama": "Ollama(本地)",
}

# 供应商 -> 常用模型候选（GUI 二级下拉用；实际以所选为准，可用 MODEL_XX 追加）
MODEL_CANDIDATES = {
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "openai": ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "o3-mini"],
    "gemini": ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash"],
    "qwen": ["qwen-turbo", "qwen-plus", "qwen-max"],
    "moonshot": ["kimi-k2-0711-preview", "moonshot-v1-8k", "moonshot-v1-32k"],
    "glm": ["glm-4-flash", "glm-4-plus", "glm-4-air"],
    "mimo": ["mimo-v2.5-pro"],
    "ollama": ["llama3.1", "qwen2.5", "llama3"],
}


def load_environment(env_file: str | Path | None = None) -> bool:
    """加载 .env（权威来源=项目根 .env，不受启动目录影响）。

    优先级：显式 env_file 参数 > 环境变量 SUWAN_ENV_FILE 指定的文件 > 项目根/.env。
    不再回退到"当前工作目录/.env"，杜绝 docs/reference 等子目录副本被误读。
    .env 采用 override=True：即使系统/父进程已有同名（可能为空的）环境变量，
    也以 .env 文件里的值为准——否则会出现"明明填了 key 却没生效"。
    SUWAN_SKIP_DOTENV=1 时直接返回，不检查或读取候选文件。
    """
    # 测试/离线子进程必须能在任何隐式调用之前禁止读取真实配置。
    if os.environ.get("SUWAN_SKIP_DOTENV") == "1" or load_dotenv is None:
        return False
    project_root = Path(__file__).resolve().parents[2]
    candidates: list[Path] = []
    if env_file is not None:
        candidates.append(Path(env_file))
    override_env = os.environ.get("SUWAN_ENV_FILE", "").strip()
    if override_env:
        candidates.append(Path(override_env))
    candidates.append(project_root / ".env")
    for candidate in candidates:
        if candidate.exists():
            return bool(load_dotenv(candidate, override=True))
    return False


@dataclass(frozen=True)
class Intervals:
    """周期任务间隔（秒）。"""

    heartbeat: float = 2.0
    diary: float = 900.0
    reflection: float = 3600.0
    auto_behavior: float = 180.0
    inner_monologue: float = 90.0
    life_story: float = 86400.0
    memory_cleanup: float = 86400.0
    share_watch: float = 30.0


@dataclass(frozen=True)
class Thresholds:
    """状态阈值/速率（器官与服务的构造参数单一来源）。"""

    fatigue_decay_rate: float = 0.003
    boredom_threshold: float = 50.0
    max_emotion_impact: float = 15.0
    max_output_tokens: int = 8192
    sleep_energy_threshold: float = 10.0
    wake_energy_threshold: float = 90.0
    idle_boredom_rise_rate: float = 0.02    # 独处时无聊上升速率（/s）
    active_boredom_decay_rate: float = 0.01  # 互动时无聊回落速率（/s）


@dataclass
class Settings:
    """应用设置（构造后视为不可变约定；需要时经 .from_env() 重建）。"""

    home_dir: Path            # 数据根（默认 D:/SuwanData）
    data_dir: Path            # v2 数据目录（默认 <home>/v2）
    share_dir: Path           # 投喂文件夹（默认 <home>/share，与旧版共享语义）
    output_dir: Path          # 文档输出（默认 <home>/output）
    api_keys: dict[str, str]  # provider -> key
    base_urls: dict[str, str]
    model_names: dict[str, str]
    intervals: Intervals
    thresholds: Thresholds
    keyless_providers: frozenset = frozenset(KEYLESS_PROVIDERS)  # 无需密钥的本地供应商
    max_history_lines: int = 100

    # ---------- 派生路径 ----------
    @property
    def memories_file(self) -> Path:
        return self.data_dir / "memories.json"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def api_log_file(self) -> Path:
        return self.data_dir / "api_call_log.jsonl"

    @property
    def chat_log_file(self) -> Path:
        return self.data_dir / "chat.log"

    @property
    def thought_log_file(self) -> Path:
        return self.data_dir / "thought.log"

    @property
    def mood_log_file(self) -> Path:
        return self.data_dir / "mood.log"

    @property
    def story_log_file(self) -> Path:
        return self.data_dir / "story.txt"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio_output"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        env_file: str | Path | None = None,
        home_dir: str | Path | None = None,
        load_env: bool = True,
    ) -> "Settings":
        """从环境变量构造设置；load_env=False 时绝不读取 dotenv 文件。

        env={} 只选择配置映射，并不自动禁用 dotenv；离线调用应同时设置
        load_env=False。进程级 SUWAN_SKIP_DOTENV=1 也会阻止环境文件加载。
        """
        if load_env:
            load_environment(env_file)
        e: Mapping[str, str] = env if env is not None else os.environ

        root = Path(home_dir) if home_dir is not None else Path(e.get("SUWAN_HOME", str(DEFAULT_HOME_DIR)))
        data_dir = root / "v2"
        api_keys: dict[str, str] = {}
        base_urls: dict[str, str] = {}
        model_names: dict[str, str] = {}
        for provider, (env_name, base_url, default_model) in ENDPOINT_ENV.items():
            api_keys[provider] = e.get(env_name, "")
            base_urls[provider] = base_url
            model_names[provider] = e.get(f"MODEL_{provider.upper()}", default_model)
        return cls(
            home_dir=root,
            data_dir=data_dir,
            share_dir=root / "share",
            output_dir=root / "output",
            api_keys=api_keys,
            base_urls=base_urls,
            model_names=model_names,
            keyless_providers=frozenset(KEYLESS_PROVIDERS),
            intervals=Intervals(),
            thresholds=Thresholds(),
        )
