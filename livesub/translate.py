"""流式翻译：把识别出来的外语句子实时译成中文。

为什么用"LLM 翻译"而不是传统 MT
------------------------------
直播口语里有大量俚语、游戏术语、人名梗和半截话，传统 MT 很容易翻崩，
而小模型（deepseek-chat / gpt-4o-mini）在这种场景下质量明显更好，
单句延迟通常在 0.3~0.8s，价格也远低于语音链路本身。

关键设计
--------
1. **中间结果也要翻译**：等 Deepgram 的端句（endpointing，通常 300ms 静音）
   才翻译，用户会感觉慢了半拍。这里对"还在生长的句子"做节流翻译，
   让字幕跟着说话节奏往前爬，定稿后再覆盖成高质量版本。
2. **单飞 + 合并**：任何时刻只允许一个翻译请求在飞，飞行期间来的新请求
   只保留最新的一个（旧的自然作废）。这样既不会排队积压，也不会烧钱。
3. **上下文滑窗**：带最近几轮译文，跨句的人称/术语更连贯；滑窗大小固定，
   命中服务端 prefix cache，省 token 也更快。
4. **术语表**：直播里主播名字、游戏术语可以预先钉死，避免每句翻法不一致。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol

import requests

from .config import Config


@dataclass
class TranslationResult:
    text: str
    elapsed_s: float
    ok: bool
    error: str = ""


class Translator(Protocol):
    async def translate(self, text: str, context: list[tuple[str, str]]) -> TranslationResult: ...


ROOT_SYSTEM_PROMPT = """你是直播实时字幕的翻译引擎。把用户给你的外语句子翻译成{target}。

规则：
1. 只输出译文本身，不要任何解释、引号、前后缀或"翻译："字样。
2. 保留口语感。直播里的话是随口说的，译文要像中国人顺口说的话，不要书面腔。
3. 人名、游戏名、产品名、专有名词保留原文或通用译法，前后保持一致。
4. 数字、时间、比分、装备名要准确。
5. 如果句子明显没说完（被截断），就照现有内容译，不要补齐、不要猜。
6. 语气词、笑声、脏话按中文习惯自然处理，不要过滤掉情绪。
7. **输入来自语音识别，可能有同音字、片假名拼写或专有名词错误**
   （例如把「オープンワールド」识别成「オープンマールド」、
   把「とりあえず」识别成「トリアイズ」）。请结合上下文判断说话人
   真正想表达的意思，按最合理的理解翻译，不要把明显是识别错误的部分
   生硬直译成读不通的中文。但也不要凭空添加原文没有的信息。"""

#: 语言提示模板。**在正式翻译之前单独发一条**，让模型提前建立语种预期。
#:
#: 措辞刻意用"大多数/优先"而不是"必须"——直播里不会 100% 是单一语言
#: （常有英文术语、外来语、人名），说死了反而会让模型把英文词硬翻错。
LANGUAGE_HINT_PROMPT = """接下来你会收到一段直播语音的实时识别文本，需要翻译成{target}。

重要背景：这段直播的**主要语言是{source}**。识别引擎已经按{source}进行识别，
你收到的文本绝大多数就是{source}。

请据此处理：
1. 以{source}的理解优先。遇到看起来像英文单词或罗马字的部分，先考虑它是不是
   {source}里的外来语、专有名词或口头禅，而不是直接当成英文去翻译。
2. 但也不要死板：如果文本里确实出现了其它语言（英文术语、人名、歌词等），
   按它实际的语言理解即可。这不是严格的单一语言场景。
3. 保持前后一致：同一个人名/术语在全场直播里始终用同一种译法。

收到后回复"{confirm}"，然后我们开始逐句翻译。"""

#: 让模型回复的确认词（用于预热请求的 max_tokens 控制，越短越省）
CONFIRM_WORD = "好"

#: 语种代码 -> 中文名（用于语言提示，让模型看到的是人话而不是 ja/en）
LANGUAGE_NAMES: dict[str, str] = {
    "ja": "日语",
    "en": "英语",
    "zh": "中文",
    "yue": "粤语",
    "ko": "韩语",
    "de": "德语",
    "fr": "法语",
    "ru": "俄语",
    "es": "西班牙语",
    "pt": "葡萄牙语",
    "th": "泰语",
    "vi": "越南语",
}


def language_display(code: str) -> str:
    """把语种代码变成给人/模型看的中文名。"""
    c = (code or "").strip().lower()
    return LANGUAGE_NAMES.get(c, code or "未知语言")


class OpenAICompatTranslator:
    """调用任何 OpenAI 兼容的 /chat/completions。

    国内主流厂商（DeepSeek / 通义千问 / Kimi / 硅基流动 / 智谱）都提供这个接口，
    所以"接入一家新厂商"只需要在 keystore.TRANSLATE_PROVIDERS 里加一行配置，
    不用写新代码。
    """

    def __init__(self, cfg: Config, provider: str | None = None):
        self.cfg = cfg
        self.provider = (provider or cfg.translate.provider or "deepseek").lower()
        tgt = cfg.translate.target_for(self.provider)
        self.label = tgt.label or self.provider
        self.base_url = (tgt.base_url or "").rstrip("/")
        self.model = tgt.model
        self.api_key = tgt.api_key
        self.target = cfg.translate.target_language
        self.timeout = cfg.translate.timeout_s
        self.context_turns = cfg.translate.context_turns
        self.session = requests.Session()
        self._system = self._build_system(cfg)
        self.hint_delivered = False   # 语言提示是否已成功送达
        #: 是否已经被判为"不可用"（给降级链用）
        self.dead = False

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OpenAICompatTranslator {self.provider} model={self.model}>"

    # ---- system 提示：基础规则 + 语言提示 + 术语表 ----
    def _build_system(self, cfg: Config) -> str:
        """组装 system 提示。

        这里就是"提前告诉 AI 接下来主要是什么语言"的落点：把源语言背景写进
        system 消息，模型从第一句起就带着语种预期，而不是每句都去猜。
        """
        parts = [ROOT_SYSTEM_PROMPT.format(target=self.target)]

        src = (cfg.asr.language or "").lower()
        if cfg.translate.send_language_hint and src not in ("", "auto", "multi"):
            # 注意：写"主要语言"而不是"唯一语言"，避免把英文术语硬翻错
            parts.append(
                f"【本次任务的语言背景】这场直播的主要语言是{language_display(src)}"
                f"（识别引擎已按该语言识别）。请以该语言的理解优先；"
                f"若句中确实夹有其它语言，按实际语言处理。"
            )

        glossary = [g for g in (cfg.translate.glossary or []) if g and "=" in str(g)]
        if glossary:
            lines = "\n".join(f"- {str(g).split('=', 1)[0].strip()} → {str(g).split('=', 1)[1].strip()}" for g in glossary)
            parts.append(f"【术语表（必须遵守，固定的译法）】\n{lines}")

        return "\n\n".join(parts)

    # ---- 上下文组装 ----
    def _build_messages(self, text: str, context: list[tuple[str, str]]) -> list[dict]:
        messages = [{"role": "system", "content": self._system}]
        for src, dst in context[-self.context_turns :] if self.context_turns else []:
            messages.append({"role": "user", "content": src})
            messages.append({"role": "assistant", "content": dst})
        messages.append({"role": "user", "content": text})
        return messages

    def warmup(self) -> None:
        """预热：提前把"主要语言"背景发一次，并建立 HTTP 连接。

        好处有两个：
        1. 模型在收到第一句台词之前就已经知道语种背景（用户要的那步"提前发送"）
        2. 顺带把 TCP/TLS 握手做掉，第一句台词省掉 300~600ms 的建连时间
        """
        if self.hint_delivered:
            return
        src = (self.cfg.asr.language or "").lower()
        if not self.cfg.translate.send_language_hint or src in ("", "auto", "multi"):
            # 自动识别模式下没什么可提示的，只做连接预热
            try:
                self.session.get(self.base_url, timeout=3)
            except Exception:
                pass
            self.hint_delivered = True
            return
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system},
                {
                    "role": "user",
                    "content": LANGUAGE_HINT_PROMPT.format(
                        target=self.target, source=language_display(src), confirm=CONFIRM_WORD
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 8,
            "stream": False,
        }
        try:
            resp = self.session.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=min(self.timeout, 10),
            )
            self.hint_delivered = True
            if resp.status_code == 200:
                print(f"[translate] 已提前告知语言背景：主要语言 {language_display(src)}")
            else:
                print(f"[translate] 语言提示未生效（HTTP {resp.status_code}），翻译仍可继续")
        except Exception as exc:
            # 预热失败不该阻止翻译，正式请求时还会带上 system 提示
            self.hint_delivered = True
            print(f"[translate] 预热失败（{type(exc).__name__}），首次翻译会稍慢", file=sys.stderr)

    async def translate(self, text: str, context: list[tuple[str, str]]) -> TranslationResult:
        if not text.strip():
            return TranslationResult("", 0.0, True)
        if not self.api_key and self.cfg.translate.provider != "mock":
            return TranslationResult("", 0.0, False, "缺少 API Key")
        loop = asyncio.get_running_loop()
        # 实测遇到过一次 SSLEOFError（连接被中途掐断）。如果不重试，**那一句字幕
        # 就永久丢了**——实时字幕里出现空洞比慢一点更难受。所以对瞬时网络错误
        # （SSL/连接重置/超时）退避重试；只对明确的 HTTP 错误码直接放弃（重试没用）。
        last: TranslationResult | None = None
        for attempt, delay in enumerate((0.0, 0.25, 0.6), start=1):
            if delay:
                await asyncio.sleep(delay)
            result = await loop.run_in_executor(None, self._blocking, text, list(context))
            if result.ok or result.error.startswith("HTTP "):
                return result
            last = result
            if os.getenv("LIVESUB_TRANSLATE_DEBUG"):
                print(f"[translate] 第 {attempt} 次失败（{result.error}），重试…", file=sys.stderr)
        return last or TranslationResult("", 0.0, False, "未知错误")

    def _blocking(self, text: str, context: list[tuple[str, str]]) -> TranslationResult:
        t0 = time.perf_counter()
        payload = {
            "model": self.model,
            "messages": self._build_messages(text, context),
            "temperature": 0.2,
            "max_tokens": 512,
            "stream": False,
        }
        try:
            resp = self.session.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            elapsed = time.perf_counter() - t0
            if resp.status_code != 200:
                return TranslationResult(
                    "", elapsed, False, f"HTTP {resp.status_code}: {resp.text[:160]}"
                )
            data = resp.json()
            content = (data["choices"][0]["message"].get("content") or "").strip()
            return TranslationResult(_clean(content), elapsed, True)
        except requests.Timeout:
            return TranslationResult("", time.perf_counter() - t0, False, "超时")
        except Exception as exc:  # 网络抖动不应打断字幕流
            return TranslationResult("", time.perf_counter() - t0, False, f"{type(exc).__name__}: {exc}")


class FallbackTranslator:
    """多翻译引擎的自动降级。

    云端服务会宕机（千问就出现过整晚不可用）。如果只配一家，宕机时字幕就彻底没了；
    这里在"某家返回失败/超时"时自动轮到下一个**已配 Key** 的引擎，并记住哪家坏了
    （避免每句都去撞一次已经挂掉的服务）。

    注意：**不会降级到纯 mock**——那会把译文换成假数据，反而更糟。
    """

    #: 连续失败多少次就判定这家"已挂"，本轮不再尝试
    DEAD_AFTER = 2

    def __init__(self, cfg: Config, providers: list[str] | None = None):
        self.cfg = cfg
        self.chain: list[OpenAICompatTranslator] = []
        self.active = 0
        for pid in providers if providers is not None else cfg.translate_chain():
            if pid == "mock":
                continue
            t = OpenAICompatTranslator(cfg, pid)
            if t.api_key and t.base_url:
                self.chain.append(t)
        if not self.chain:
            raise SystemExit(
                "没有可用的翻译引擎：请至少配置一个翻译 Key"
                "（DeepSeek / 通义千问 / Kimi / 硅基流动 / 智谱）"
            )
        self.failures = [0] * len(self.chain)
        #: 翻译本身是有状态的（上下文滑窗在 Actor 里），这里串行化，避免多请求交错
        self._lock = asyncio.Lock()
        self.context_turns = cfg.translate.context_turns
        self.timeout = cfg.translate.timeout_s

    @property
    def current(self) -> OpenAICompatTranslator:
        return self.chain[self.active]

    @property
    def label(self) -> str:
        return self.current.label

    def warmup(self) -> None:
        """预热**所有**引擎：各自提前收到语言背景 + 建好连接。

        顺带也是一次可用性探测——首句翻译时就不用再为建连买单了。
        """
        for t in self.chain:
            try:
                t.warmup()
            except Exception as exc:  # 单家失败不影响其它家
                print(f"[translate] {t.label} 预热失败：{exc}", file=sys.stderr)

    async def translate(self, text: str, context: list[tuple[str, str]]) -> TranslationResult:
        async with self._lock:
            order = [self.active] + [i for i in range(len(self.chain)) if i != self.active]
            last: TranslationResult | None = None
            for idx in order:
                t = self.chain[idx]
                if self.failures[idx] >= self.DEAD_AFTER:
                    continue
                result = await t.translate(text, context)
                if result.ok:
                    if idx != self.active:
                        print(f"[translate] 已切换到 {t.label}", file=sys.stderr)
                    self.active = idx
                    self.failures[idx] = 0
                    return result
                last = result
                self.failures[idx] += 1
                print(f"[translate] {t.label} 失败（{result.error[:60]}）", file=sys.stderr)

            # 全都失败：给"最不坏"的那个重来一次，避免字幕直接空掉
            for idx in range(len(self.chain)):
                self.failures[idx] = 0
            if self.chain:
                result = await self.chain[0].translate(text, context)
                if result.ok:
                    self.active = 0
                    return result
                last = result
            return last or TranslationResult("", 0.0, False, "所有翻译引擎均不可用")


class MockTranslator:
    """不联网：用固定前缀标记，验证"识别→翻译→悬浮窗"通路与延迟统计。"""

    def __init__(self, cfg: Config):
        self.target = cfg.translate.target_language

    async def translate(self, text: str, context: list[tuple[str, str]]) -> TranslationResult:
        await asyncio.sleep(0.25)  # 模拟网络往返
        return TranslationResult(f"[模拟译文] {text}", 0.25, True)


def _clean(text: str) -> str:
    """模型偶尔会画蛇添足，这里做最小限度的清洗。"""
    t = text.strip()
    t = re.sub(r"^(翻译[:：]|译文[:：]|Translation[:：])\s*", "", t)
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'“”‘’「」":
        t = t[1:-1]
    return t.strip()


def create_translator(cfg: Config) -> Translator:
    """创建翻译器。

    除了 mock 之外，一律用 FallbackTranslator 包一层：这样多配几家 Key 就能自动
    容错，只配一家时行为与以前完全一致（就是只有一个成员）。
    """
    provider = (cfg.translate.provider or "").lower()
    if provider == "mock":
        return MockTranslator(cfg)

    chain = cfg.translate_chain()
    usable = [p for p in chain if p != "mock"]
    if not usable:
        raise SystemExit(
            "没有可用的翻译引擎：请在设置里至少配置一个翻译 Key"
            "（DeepSeek / 通义千问 / Kimi / 硅基流动 / 智谱）"
        )
    return FallbackTranslator(cfg, usable)
