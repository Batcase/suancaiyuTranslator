"""本地密钥与配置的持久化。

设计取舍
--------
* **密钥存 `.env`**：进程环境变量是标准做法，也天然不会被提交（已在 .gitignore）。
  写入时**只改对应那一行**，保留文件里其它内容与注释，避免把用户手写的东西冲掉。
* **普通配置存 `config.toml`**：语言、引擎、悬浮窗样式这些。
* 两者都放在**程序目录**下，做成"绿色软件"——拷走整个文件夹就能用。

打包成 exe 后特别注意
---------------------
PyInstaller 的 onefile 会把程序解压到临时目录（``sys._MEIPASS``），
而 ``__file__`` 就指向那里。如果照它写配置，用户**重启程序后配置就丢了**，
还找不到文件在哪。所以运行期数据目录必须优先用 exe 自己所在的目录。
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path


def _app_dir() -> Path:
    """程序所在目录（用户看得见、可写、配置就放这里）。

    * 打包成 exe：用 exe 自身所在目录（``sys.executable`` 的父目录）
    * 跑源码：用项目根目录
    """
    if getattr(sys, "frozen", False):  # PyInstaller 打的包
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


#: 运行期数据目录（.env / config.toml / models / logs 都在这里）
ROOT = _app_dir()

#: 只读资源目录（模板文件；onefile 下在临时解压目录里）
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", ROOT))

#: 界面上展示的名字 -> .env 里的变量名
KEY_FIELDS: dict[str, str] = {
    "dashscope": "DASHSCOPE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "openai": "OPENAI_API_KEY",
}

# ----------------------------------------------------------------------
# 识别引擎注册表
# ----------------------------------------------------------------------
#: provider -> (界面上限文案, 需要的 Key, 说明)
ASR_PROVIDERS: list[dict] = [
    {
        "id": "paraformer",
        "label": "阿里云 Paraformer · 国内直连（推荐）",
        "key": "dashscope",
        "base_url": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        "model": "paraformer-realtime-v2",
        "modes": "流式",
        "note": "中文/英文/日语/韩语/粤语等，专为直播会议设计",
        "apply": "bailian.console.aliyun.com",
    },
    {
        "id": "deepgram",
        "label": "Deepgram · 海外",
        "key": "deepgram",
        "base_url": "wss://api.deepgram.com/v1/listen",
        "model": "nova-2",
        "modes": "流式",
        "note": "多语种，日语用 nova-2（nova-3 会丢句首）；国内网络有时连不上",
        "apply": "console.deepgram.com",
    },
    {
        "id": "sensevoice",
        "label": "硅基流动 SenseVoice · 国内",
        "key": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1/audio/transcriptions",
        "model": "FunAudioLLM/SenseVoiceSmall",
        "modes": "整句",
        "note": "中文/日语/英语/韩语，识别准；按整句提交，比流式慢一点",
        "apply": "cloud.siliconflow.cn",
    },
    {
        "id": "local",
        "label": "本地 Whisper · 免费离线",
        "key": "",
        "base_url": "",
        "model": "tiny",
        "modes": "整句",
        "note": "不需要 Key、不联网；慢约 5 倍，准确率较低",
        "apply": "",
    },
]

# ----------------------------------------------------------------------
# 翻译引擎注册表（都是 OpenAI 兼容的 /chat/completions）
# ----------------------------------------------------------------------
TRANSLATE_PROVIDERS: list[dict] = [
    {
        "id": "deepseek",
        "label": "DeepSeek · 深度求索（推荐）",
        "key": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "note": "便宜、快、中文口语自然",
        "apply": "platform.deepseek.com",
    },
    {
        "id": "qwen",
        "label": "通义千问 · 阿里云百炼",
        "key": "dashscope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "note": "和 Paraformer 用同一个 Key，不用再注册",
        "apply": "bailian.console.aliyun.com",
    },
    {
        "id": "kimi",
        "label": "Kimi · 月之暗面",
        "key": "moonshot",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "note": "中文表达好",
        "apply": "platform.moonshot.cn",
    },
    {
        "id": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "key": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "note": "一个 Key 能用多家模型",
        "apply": "cloud.siliconflow.cn",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "key": "zhipu",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "note": "glm-4-flash 有免费额度",
        "apply": "open.bigmodel.cn",
    },
    {
        "id": "openai",
        "label": "OpenAI · 海外",
        "key": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "note": "国内通常连不上，仅备用/海外用户",
        "apply": "platform.openai.com",
    },
]

ASR_BY_ID: dict[str, dict] = {p["id"]: p for p in ASR_PROVIDERS}
TRANSLATE_BY_ID: dict[str, dict] = {p["id"]: p for p in TRANSLATE_PROVIDERS}

#: 兼容旧的写法
ASR_PROVIDER_KEY: dict[str, str] = {
    **{p["id"]: p["key"] for p in ASR_PROVIDERS},
    "dashscope": "dashscope",
    "aliyun": "dashscope",
    "whisper": "",
    "faster_whisper": "",
    "openai_realtime": "openai",
    "mock": "",
}

#: 识别引擎下拉项（顺序 = 降级优先级）
ASR_PROVIDER_LABELS: list[tuple[str, str]] = [(p["id"], p["label"]) for p in ASR_PROVIDERS]

TRANSLATE_PROVIDER_LABELS: list[tuple[str, str]] = [(p["id"], p["label"]) for p in TRANSLATE_PROVIDERS]

#: 源语言 -> Paraformer 是否支持（用于界面提示）
PARAFORMER_LANGS = {"zh", "en", "ja", "ko", "yue", "de", "fr", "ru"}


def key_for_asr_provider(provider: str) -> str:
    """识别引擎需要哪个 Key；返回空串表示不需要（本地识别）。"""
    return ASR_PROVIDER_KEY.get((provider or "").lower(), "dashscope")


def key_for_translate_provider(provider: str) -> str:
    """翻译引擎需要哪个 Key。"""
    conf = TRANSLATE_BY_ID.get((provider or "").lower())
    return conf["key"] if conf else "deepseek"

#: 用户数据（可写，放在 exe 旁边）
ENV_PATH = ROOT / ".env"
TOML_PATH = ROOT / "config.toml"
#: 模板（只读；onefile 打包后在临时解压目录里，找不到就退回数据目录）
EXAMPLE_ENV = RESOURCE_DIR / ".env.example"
EXAMPLE_TOML = RESOURCE_DIR / "config.example.toml"


# ----------------------------------------------------------------------
# 读取
# ----------------------------------------------------------------------
def read_env() -> dict[str, str]:
    """读 .env 里已有的键值（不依赖 python-dotenv，保持简单可控）。"""
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        out[name.strip()] = value.strip().strip('"').strip("'")
    return out


def get_key(name: str) -> str:
    """取某个 Key：优先 .env 文件，其次进程环境变量。"""
    var = KEY_FIELDS.get(name, name)
    value = read_env().get(var, "")
    return value or os.getenv(var, "")


def has_all_keys(required: tuple[str, ...] = ("deepseek",)) -> bool:
    return all(get_key(k).strip() for k in required)


def mask_key(value: str) -> str:
    """脱敏展示：sk-310…c804（只露头尾）。"""
    v = (value or "").strip()
    if not v:
        return "(未设置)"
    if len(v) <= 10:
        return v[0] + "…" + v[-1]
    return f"{v[:6]}…{v[-4:]}"


# ----------------------------------------------------------------------
# 写入
# ----------------------------------------------------------------------
def set_keys(updates: dict[str, str]) -> None:
    """把若干 Key 写进 .env，只改动对应行。

    保留原有注释与顺序；不存在的变量追加到末尾。
    """
    var_updates = {KEY_FIELDS.get(k, k): v for k, v in updates.items()}

    if not ENV_PATH.exists():
        if EXAMPLE_ENV.exists():
            ENV_PATH.write_text(EXAMPLE_ENV.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            ENV_PATH.write_text(
                "# 酸菜鱼翻译器 本地密钥（本文件不会被提交，请勿分享）\n", encoding="utf-8"
            )

    lines = ENV_PATH.read_text(encoding="utf-8-sig").splitlines()
    written: set[str] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.partition("=")[0].strip()
        if name in var_updates:
            lines[i] = f"{name}={var_updates[name]}"
            written.add(name)

    for name, value in var_updates.items():
        if name not in written:
            lines.append(f"{name}={value}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _restrict_permissions(ENV_PATH)
    # 同步到当前进程环境，省得重启才能生效
    for name, value in var_updates.items():
        if value:
            os.environ[name] = value


def _restrict_permissions(path: Path) -> None:
    """尽量把文件权限收紧到"仅本人可读写"。

    注：Windows 上 chmod 只能控制只读位，真正的 ACL 继承自目录，
    所以这里主要是对 Linux/macOS 有意义，失败也不影响功能。
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ----------------------------------------------------------------------
# Key 格式的粗校验（省一次网络请求，也能拦住明显贴错的东西）
# ----------------------------------------------------------------------
_FORMATS: dict[str, tuple[re.Pattern[str], str]] = {
    # 各家 Key 前缀不完全一致（阿里云是 sk-ws- 且含点），所以只校验前缀与长度，
    # 不限制字符集——太严格的规则会把合法 Key 误判掉。
    "dashscope": (re.compile(r"^sk-.{12,}$"), "以 sk- 开头"),
    "deepgram": (re.compile(r"^[0-9a-fA-F]{32,64}$"), "40 位十六进制字符串"),
    "deepseek": (re.compile(r"^sk-.{12,}$"), "以 sk- 开头"),
    "siliconflow": (re.compile(r"^sk-.{12,}$"), "以 sk- 开头"),
    "moonshot": (re.compile(r"^sk-.{12,}$"), "以 sk- 开头"),
    "zhipu": (re.compile(r"^[A-Za-z0-9_\-\.]{20,}$"), "一串字母数字（形如 xxxx.yyyy）"),
    "openai": (re.compile(r"^sk-.{12,}$"), "以 sk- 开头"),
}


def check_key_format(name: str, value: str) -> str | None:
    """返回错误说明；格式没问题返回 None。"""
    v = (value or "").strip()
    if not v:
        return "还没填"
    pattern, hint = _FORMATS.get(name, (re.compile(r".+"), "非空"))
    if not pattern.match(v):
        return f"格式看起来不对（应当是{hint}）"
    return None


# ----------------------------------------------------------------------
# config.toml 保存（只写我们管理的字段，保持可读）
# ----------------------------------------------------------------------
def save_config(cfg) -> None:
    """把当前配置写成 config.toml（覆盖式，带注释模板）。"""
    local = cfg.asr.local
    text = f"""# 酸菜鱼翻译器 配置（由程序自动生成，也可以手工修改）
# 密钥不在这里，在旁边的 .env 文件里（那个文件不会被提交）

[asr]
# paraformer = 阿里云百炼（国内直连，推荐）
# deepgram   = 海外服务，国内可能连不上
# local      = 本地离线，免 Key 但慢
provider = "{cfg.asr.provider}"
language = "{cfg.asr.language}"

[asr.paraformer]
# paraformer-realtime-v2 支持中文/英文/日语/韩语/粤语等，采样率任意
model = "{cfg.asr.paraformer.model}"
language_hints = {cfg.asr.paraformer.language_hints!r}
# VAD 断句静音阈值(ms)。官方默认 1300 对直播太慢；日语实测 500 较合适
max_sentence_silence = {cfg.asr.paraformer.max_sentence_silence}
semantic_punctuation_enabled = {str(cfg.asr.paraformer.semantic_punctuation_enabled).lower()}
punctuation_prediction_enabled = {str(cfg.asr.paraformer.punctuation_prediction_enabled).lower()}
disfluency_removal_enabled = {str(cfg.asr.paraformer.disfluency_removal_enabled).lower()}
heartbeat = {str(cfg.asr.paraformer.heartbeat).lower()}
# 业务空间专属域名（可选，官方说更稳定）
workspace_id = "{cfg.asr.paraformer.workspace_id}"

[asr.deepgram]
model = "{cfg.asr.deepgram.model}"
endpointing_ms = {cfg.asr.deepgram.endpointing_ms}

[asr.local]
model = "{local.model}"
device = "{local.device}"
compute_type = "{local.compute_type}"
max_utterance_s = {local.max_utterance_s}
silence_end_s = {local.silence_end_s}

[translate]
# deepseek | openai | mock
provider = "{cfg.translate.provider}"
target_language = "{cfg.translate.target_language}"
context_turns = {cfg.translate.context_turns}
timeout_s = {cfg.translate.timeout_s}

[audio]
sample_rate = {cfg.audio.sample_rate}
chunk_ms = {cfg.audio.chunk_ms}
vad_threshold = {cfg.audio.vad_threshold}

[overlay]
width_ratio = {cfg.overlay.width_ratio}
height_ratio = {cfg.overlay.height_ratio}
bottom_ratio = {cfg.overlay.bottom_ratio}
opacity = {cfg.overlay.opacity}
original_font_size = {cfg.overlay.original_font_size}
translated_font_size = {cfg.overlay.translated_font_size}
max_lines = {cfg.overlay.max_lines}
click_through = {str(cfg.overlay.click_through).lower()}
background_panel = {str(cfg.overlay.background_panel).lower()}
"""
    TOML_PATH.write_text(text, encoding="utf-8")


def data_dir() -> Path:
    """数据目录（日志、模型缓存都在这里）。"""
    return ROOT
