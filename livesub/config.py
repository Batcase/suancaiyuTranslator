"""配置：TOML 文件 + 环境变量 + 命令行参数三层覆盖。

优先级：CLI > 环境变量 > config.toml > 内置默认值
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

try:  # py3.11 自带
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

# 程序目录统一由 keystore 提供：打包成 exe 后必须用 exe 所在目录，
# 而不是 PyInstaller 的临时解压目录，否则配置读不到、写下去也会丢。
from .keystore import ROOT

# 环境变量名 -> 配置路径。写成 "区块.字段" 的形式。
ENV_MAP: dict[str, str] = {
    "LIVESUB_ASR_PROVIDER": "asr.provider",
    "LIVESUB_SOURCE_LANGUAGE": "asr.language",
    "LIVESUB_DEEPGRAM_MODEL": "asr.deepgram.model",
    "LIVESUB_TARGET_LANGUAGE": "translate.target_language",
    "LIVESUB_TRANSLATE_PROVIDER": "translate.provider",
    "LIVESUB_TRANSLATE_BASE_URL": "translate.base_url_override",
    "LIVESUB_AUDIO_DEVICE": "audio.device",
    "LIVESUB_CLICK_THROUGH": "overlay.click_through",
}


@dataclass
class AudioConfig:
    device: str = ""
    sample_rate: int = 16000
    chunk_ms: int = 32
    vad_threshold: float = 0.006
    idle_disconnect_s: float = 0.0

    @property
    def chunk_samples(self) -> int:
        return max(1, round(self.sample_rate * self.chunk_ms / 1000))

    @property
    def idle_disconnect(self) -> float:
        return float(self.idle_disconnect_s or 0)


@dataclass
class DeepgramConfig:
    model: str = "nova-2"
    # 判定一句话结束所需静音时长(ms)。
    # **日语实测**：300 太激进，会把「このゲームはオープンワールドでグラフィックが
    # 本当にすごいんですよ」切成「…すごい」+「んですよ」两段，碎片翻译出来读不通；
    # 500 能完整切出 4 句；700 会把两句并成一句；1000 几乎不切句。
    # 所以日语默认 500。
    endpointing_ms: int = 500
    interim_results_ms: int = 300
    api_key: str = ""


@dataclass
class OpenAIRealtimeConfig:
    model: str = "gpt-4o-mini-transcribe"
    silence_duration_ms: int = 500
    api_key: str = ""


@dataclass
class ParaformerConfig:
    """阿里云百炼 · Paraformer 实时语音识别（国内直连）。

    为什么选它：
    * 国内网络直达（实测 WebSocket 握手 234ms，而 Deepgram 经常握手失败）
    * 专为「直播、会议」场景设计，支持中文/英文/日语/韩语/粤语等
    * 双向流式：中间结果 + VAD 断句，能力与 Deepgram 对等

    注意：Paraformer 目前只在**华北2（北京）**地域提供。
    """

    # paraformer-realtime-v2（推荐：支持多语种，且采样率任意）
    model: str = "paraformer-realtime-v2"
    # 识别语种提示，可多选；留空 = 服务端自动判断
    language_hints: list = field(default_factory=lambda: ["ja"])
    # VAD 断句静音阈值(ms)。官方默认 1300 对直播太慢，这里调小
    max_sentence_silence: int = 500
    # 语义断句：准确率更高但延迟更大；直播字幕用 VAD（false）更合适
    semantic_punctuation_enabled: bool = False
    # 加标点
    punctuation_prediction_enabled: bool = True
    # 过滤"嗯、啊"这类语气词。直播口语建议关掉，保留语气更自然
    disfluency_removal_enabled: bool = False
    # 心跳：持续发静音时保持连接不掉（直播里常有长时间不说话）
    heartbeat: bool = True
    api_key: str = ""
    # 业务空间专属域名（可选）。填了就改用
    # {workspace_id}.cn-beijing.maas.aliyuncs.com，官方说更稳定
    workspace_id: str = ""


@dataclass
class SiliconFlowAsrConfig:
    """硅基流动 · SenseVoice（整句识别，国内直连）。

    没有流式接口，只能整句提交，所以端到端会比 Paraformer 多 1~2 秒；
    好处是识别质量好、国内稳定，适合作为 Paraformer 的备用。
    """

    model: str = "FunAudioLLM/SenseVoiceSmall"
    base_url: str = "https://api.siliconflow.cn/v1/audio/transcriptions"
    # 静音多久算一句说完
    silence_end_s: float = 0.5
    # 一句话最长攒多久就强制提交（防止一直说不停）
    max_utterance_s: float = 6.0
    # 攒够多久做一次检查
    window_s: float = 1.2
    # 低于此电平视为静音
    silence_rms: float = 0.004
    timeout_s: float = 25.0
    api_key: str = ""


@dataclass
class LocalWhisperConfig:
    """本地离线识别（faster-whisper）。

    实测（本机 CPU、int8）：tiny 的 RTF≈0.13~0.29 能跟上实时；
    small 的 RTF≈0.7~1.6 会越算越落后，只适合离线转写，不建议直播用。
    """

    model: str = "tiny"
    # 模型目录。留空则按 model 名字自动从 ModelScope 下载（国内可达）
    model_path: str = ""
    device: str = "cpu"
    compute_type: str = "int8"
    # 连续说话时的软切窗：攒到这么多秒就定稿一次，避免字幕无限增长。
    # 中间结果只发不切，窗口只在定稿时滚动，所以这个值直接决定"最长多久出一次定稿"。
    max_utterance_s: float = 4.0
    # 静音超过这么久就判定一句话结束（本地识别没有服务端 endpointing，
    # 这个值决定"说完到出字幕"的等待时间，0.4~0.5 比较合适）
    silence_end_s: float = 0.45
    # 低于此电平视为静音（与 audio.vad_threshold 独立，避免互相影响）
    silence_rms: float = 0.004
    # 软切窗时保留的重叠音频秒数，给模型一点上下文，减少断词错误
    overlap_s: float = 0.5


@dataclass
class AsrConfig:
    # 默认用阿里云 Paraformer：国内直连、稳定（Deepgram 在国内经常握手失败）。
    # 其它可选：deepgram / local（免费离线）/ openai_realtime
    provider: str = "paraformer"
    # 源语言。默认按主要使用场景定成日语直播。
    # 强烈建议**固定语言**而不是用 auto：
    #   1) 更准：不用猜语种，日英混说的直播不会被判成英语；
    #   2) 更快：省掉语种检测；
    #   3) 多语种自动识别在日文场景下反而容易出汉字假名混排的错。
    language: str = "ja"
    paraformer: ParaformerConfig = field(default_factory=ParaformerConfig)
    deepgram: DeepgramConfig = field(default_factory=DeepgramConfig)
    sensevoice: SiliconFlowAsrConfig = field(default_factory=SiliconFlowAsrConfig)
    openai_realtime: OpenAIRealtimeConfig = field(default_factory=OpenAIRealtimeConfig)
    local: LocalWhisperConfig = field(default_factory=LocalWhisperConfig)


@dataclass
class TranslationTarget:
    """解析后的翻译接口目标（base_url / model / api_key）。"""

    base_url: str = ""
    model: str = ""
    api_key: str = ""
    provider: str = ""
    label: str = ""


@dataclass
class TranslateConfig:
    # 主翻译引擎。取值见 livesub/keystore.py 的 TRANSLATE_PROVIDERS：
    #   deepseek | qwen | kimi | siliconflow | zhipu | openai | mock
    #
    # 和识别一样支持**自动降级**：主引擎连不上时，自动轮到下一个已配 Key 的引擎。
    provider: str = "deepseek"
    # 降级顺序（逗号分隔的 provider id）。留空 = 用注册表里的默认顺序
    fallback_order: str = ""
    target_language: str = "简体中文"
    # 是否把"源语言"作为提示**提前告诉翻译模型**。
    #
    # 为什么需要：直播里日英混说、外来语、专有名词很多，模型拿到一个没有上下文的
    # 短句时容易误判语种（例如把日语的片假名当成英语硬译）。提前声明"主要语言是
    # 日语"，能显著提高识别与翻译的准确性——但毕竟不会 100% 是单一语言，所以提示
    # 里明确写了"大多数/优先"，而不是"必须"。
    send_language_hint: bool = True
    # 自定义术语表：主播名、游戏名等专有名词，钉死译法避免每句翻得不一致。
    # 写成 "原文=译文"，一行一条。
    glossary: list = field(default_factory=list)
    context_turns: int = 3
    context_window: int = 8
    timeout_s: float = 8.0
    # 环境变量直给的覆盖值（优先于注册表里的默认值）
    base_url_override: str = ""
    api_key_override: str = ""
    model_override: str = ""

    # ---------- 解析 ----------
    def target_for(self, provider: str) -> TranslationTarget:
        """按 provider id 解析出接口地址、模型与 Key。

        地址与模型来自 provider 注册表（livesub/keystore.py），Key 从环境变量读。
        这样"加一家新厂商"只需要在注册表里加一行，不用改配置结构。
        """
        from .keystore import TRANSLATE_BY_ID, get_key as _get_key, key_for_translate_provider

        pid = (provider or "").lower()
        conf = TRANSLATE_BY_ID.get(pid)
        if conf is None:
            # 未知 provider：当成 OpenAI 兼容的自定义接口
            return TranslationTarget(
                base_url=self.base_url_override or os.getenv("TRANSLATE_BASE_URL", ""),
                model=self.model_override or "gpt-4o-mini",
                api_key=self.api_key_override or os.getenv("TRANSLATE_API_KEY", ""),
                provider=pid,
                label=pid,
            )

        key_name = key_for_translate_provider(pid)
        key = self.api_key_override or os.getenv("TRANSLATE_API_KEY", "") or _get_key(key_name)
        # 同一家可能既做识别又做翻译（例如阿里云），这里复用同一个 Key
        if not key and pid == "qwen":
            key = _get_key("dashscope")
        base_url = self.base_url_override or os.getenv("TRANSLATE_BASE_URL", "") or conf["base_url"]
        model = self.model_override or conf["model"]
        return TranslationTarget(base_url=base_url, model=model, api_key=key, provider=pid, label=conf["label"])

    @property
    def target(self) -> TranslationTarget:
        """当前主引擎的解析结果。"""
        return self.target_for(self.provider)


@dataclass
class OverlayConfig:
    width_ratio: float = 0.72
    height_ratio: float = 0.30
    bottom_ratio: float = 0.07
    opacity: float = 0.72
    original_font_size: int = 15
    translated_font_size: int = 24
    max_lines: int = 2
    click_through: bool = True
    # True = 字幕后面加一层深色底板（更清晰，但会遮住一点画面）
    # False = 纯文字直接浮在画面上（推荐，配合文字描边效果最好看）
    background_panel: bool = False


@dataclass
class HotkeyConfig:
    toggle_click_through: str = "ctrl+shift+u"
    toggle_pause: str = "ctrl+shift+p"
    quit: str = "ctrl+shift+q"


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    translate: TranslateConfig = field(default_factory=TranslateConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    # 运行期开关
    log_audio: bool = False
    replay_wav: str = ""

    # ---------- 便捷访问 ----------
    @property
    def asr_api_key(self) -> str:
        if self.asr.provider == "deepgram":
            return self.asr.deepgram.api_key or os.getenv("DEEPGRAM_API_KEY", "")
        if self.asr.provider in ("paraformer", "dashscope", "aliyun"):
            return self.asr.paraformer.api_key or os.getenv("DASHSCOPE_API_KEY", "")
        if self.asr.provider in ("sensevoice", "siliconflow"):
            return self.asr.sensevoice.api_key or os.getenv("SILICONFLOW_API_KEY", "")
        if self.asr.provider == "openai_realtime":
            return self.asr.openai_realtime.api_key or os.getenv("OPENAI_API_KEY", "")
        # local / mock 不需要 Key
        return ""

    def asr_chain(self) -> list[str]:
        """识别引擎的尝试顺序：主引擎在前，然后其余**可用的**引擎。

        可用的定义：配了对应 Key；本地识别不需要 Key，永远可用（最后的兜底）。
        """
        from .keystore import ASR_PROVIDERS, get_key as _get_key, key_for_asr_provider

        primary = (self.asr.provider or "paraformer").lower()
        order: list[str] = [primary]
        for conf in ASR_PROVIDERS:
            if conf["id"] not in order:
                order.append(conf["id"])
        # 兼容旧写法
        for alias, canon in (("dashscope", "paraformer"), ("aliyun", "paraformer"), ("whisper", "local")):
            order = [canon if x == alias else x for x in order]

        chain: list[str] = []
        for pid in order:
            key_name = key_for_asr_provider(pid)
            if not key_name or _get_key(key_name).strip():
                chain.append(pid)
        return chain or [primary]

    def translate_chain(self) -> list[str]:
        """翻译引擎的尝试顺序：主引擎在前，然后是其余**已配 Key** 的引擎。

        云端服务会宕机（千问就出现过整晚不可用），所以主引擎失败时自动轮到下一个，
        和识别层的降级逻辑保持一致。
        """
        from .keystore import TRANSLATE_PROVIDERS, get_key as _get_key, key_for_translate_provider

        primary = (self.translate.provider or "deepseek").lower()
        order: list[str] = [primary]
        custom = [x.strip() for x in (self.translate.fallback_order or "").split(",") if x.strip()]
        for pid in custom:
            if pid not in order:
                order.append(pid)
        for conf in TRANSLATE_PROVIDERS:
            if conf["id"] not in order:
                order.append(conf["id"])

        chain: list[str] = []
        for pid in order:
            if pid == "mock":
                chain.append(pid)
                continue
            key_name = key_for_translate_provider(pid)
            if pid == "qwen":
                # 阿里云同一个 Key 既做识别又做翻译
                if _get_key("dashscope").strip() or _get_key(key_name).strip():
                    chain.append(pid)
                continue
            if _get_key(key_name).strip():
                chain.append(pid)
        return chain or [primary]


# ------------------------------------------------------------------
# 加载
# ------------------------------------------------------------------
def _set(obj: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        cur = getattr(cur, part)
    leaf = parts[-1]
    if not hasattr(cur, leaf):
        return
    target_type = type(getattr(cur, leaf))
    try:
        if target_type is bool:
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            else:
                value = bool(value)
        elif target_type is int:
            value = int(value)
        elif target_type is float:
            value = float(value)
        elif target_type is str:
            value = str(value)
    except (TypeError, ValueError):
        return
    setattr(cur, leaf, value)


def _fill_dataclass(obj: Any, data: dict[str, Any]) -> None:
    """把 TOML 字典递归写进 dataclass（忽略未知键）。"""
    if not is_dataclass(obj) or not isinstance(data, dict):
        return
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            continue
        cur = getattr(obj, key)
        if is_dataclass(cur):
            _fill_dataclass(cur, value)
        elif isinstance(value, dict) and key in ("base_url", "model"):
            continue
        else:
            cur_type = type(cur)
            try:
                setattr(obj, key, cur_type(value))
            except (TypeError, ValueError):
                # 允许 int 字段被写成 float（例如 TOML 里写 0.0 或 20.0）
                if cur_type is int:
                    try:
                        setattr(obj, key, int(float(value)))
                        continue
                    except (TypeError, ValueError):
                        pass
                try:
                    setattr(obj, key, value)
                except (TypeError, ValueError):
                    pass


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """加载配置。顺序：默认值 → TOML → 环境变量。"""
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    cfg = Config()

    toml_path = Path(path) if path else ROOT / "config.toml"
    if toml_path.exists():
        if tomllib is None:
            print(f"[warn] 当前 Python 无 tomllib，忽略 {toml_path}", file=sys.stderr)
        else:
            # 注意：Windows 记事本"另存为 UTF-8"会写入 BOM，而 tomllib 不接受
            # 带 BOM 的输入（报 "Invalid statement (at line 1, column 1)"）。
            # 这里用 utf-8-sig 解码，把 BOM 吃掉，避免用户踩这个坑。
            raw = toml_path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                print(f"[config] 检测到 {toml_path.name} 带 UTF-8 BOM，已自动忽略", file=sys.stderr)
            try:
                data = tomllib.loads(raw.decode("utf-8-sig"))
            except UnicodeDecodeError as exc:
                raise SystemExit(
                    f"配置文件 {toml_path} 不是 UTF-8 编码：{exc}\n"
                    "  请用「另存为 → 编码: UTF-8」重新保存。"
                ) from exc
            except Exception as exc:
                raise SystemExit(
                    f"配置文件 {toml_path} 解析失败：{exc}\n"
                    "  常见原因：多写了引号/括号、用了不支持的语法。\n"
                    f"  可以先删掉它跑默认配置，或者对照 config.example.toml 检查。"
                ) from exc
            _fill_dataclass(cfg, data)

    # 环境变量覆盖
    for env_name, cfg_path in ENV_MAP.items():
        raw = os.getenv(env_name)
        if raw is not None and raw != "":
            _set(cfg, cfg_path, raw)

    # API Key 直接从环境变量注入
    cfg.asr.deepgram.api_key = os.getenv("DEEPGRAM_API_KEY", "") or cfg.asr.deepgram.api_key
    cfg.asr.openai_realtime.api_key = (
        os.getenv("OPENAI_API_KEY", "") or cfg.asr.openai_realtime.api_key
    )

    # 合法性修正
    cfg.asr.language = (cfg.asr.language or "auto").lower()
    cfg.audio.sample_rate = int(cfg.audio.sample_rate)
    cfg.audio.chunk_ms = max(10, min(100, int(cfg.audio.chunk_ms)))
    cfg.overlay.opacity = max(0.15, min(1.0, float(cfg.overlay.opacity)))
    return cfg


def describe(cfg: Config) -> str:
    """生成一份脱敏的配置摘要，便于排错。"""
    tgt = cfg.translate.target
    chain = cfg.translate_chain()
    asr_chain = cfg.asr_chain()
    lines = [
        f"识别引擎   : {cfg.asr.provider}"
        + (
            f" (model={cfg.asr.paraformer.model}, hints={cfg.asr.paraformer.language_hints})"
            if cfg.asr.provider in ("paraformer", "dashscope", "aliyun")
            else f" (model={cfg.asr.deepgram.model})"
            if cfg.asr.provider == "deepgram"
            else f" (model={cfg.asr.local.model}, {cfg.asr.local.device}/{cfg.asr.local.compute_type})"
            if cfg.asr.provider in ("local", "whisper", "faster_whisper")
            else f" (model={cfg.asr.openai_realtime.model})"
            if cfg.asr.provider in ("openai", "openai_realtime")
            else ""
        ),
        f"识别降级链 : {' → '.join(asr_chain)}",
        f"源语言     : {cfg.asr.language}",
        f"翻译引擎   : {tgt.provider} (model={tgt.model}, base_url={tgt.base_url or 'n/a'})",
        f"翻译降级链 : {' → '.join(chain)}",
        f"目标语言   : {cfg.translate.target_language}",
        f"音频       : {cfg.audio.sample_rate}Hz / {cfg.audio.chunk_ms}ms/包 / VAD={cfg.audio.vad_threshold}",
        f"设备       : {cfg.audio.device or '(自动选择默认扬声器 loopback)'}",
        f"悬浮窗     : {int(cfg.overlay.width_ratio*100)}%x{int(cfg.overlay.height_ratio*100)}% 不透明度{cfg.overlay.opacity}",
    ]
    return "\n".join(lines)
