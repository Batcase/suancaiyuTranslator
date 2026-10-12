"""编排层：把「系统音频 → 流式识别 → 流式翻译 → 悬浮字幕」串起来。

线程/协程模型
-------------
* 主线程    ：tkinter 悬浮窗（GUI 框架要求）
* 后台线程  ：asyncio 事件循环，跑音频泵 + 识别 + 翻译调度
* 线程池    ：阻塞式音频读取、同步 HTTP 翻译调用

三条关键设计
-----------
1. **音频泵与识别解耦**：音频按采集节奏推给 ASR；网络慢不阻塞采集，
   宁可丢旧音频也不让延迟累积。
2. **翻译节流 + 单飞 + sid 校验**：中间结果节流翻译（让字幕跟着说话爬），
   定稿立即翻译；异步回来的译文必须校验行号，避免快速说话时错位。
3. **失败不致命**：一次翻译失败只标红一行，不打断字幕流；ASR 掉线自动重连。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import agc as agc_mod
from . import audio as audio_mod
from .asr import AsrEvent, create_asr
from .config import Config
from .state import SubtitleState
from .stats import LatencyStats
from .translate import create_translator


@dataclass
class RunControl:
    """跨线程共享的运行开关。"""

    stop: threading.Event = field(default_factory=threading.Event)
    paused: threading.Event = field(default_factory=threading.Event)
    reconnect: threading.Event = field(default_factory=threading.Event)


class TranscriptLogger:
    """把每句原文/译文写成 JSONL，方便事后对比效果、调提示词、算延迟。"""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._fh = None

    def __enter__(self) -> "TranscriptLogger":
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def write(self, **record) -> None:
        if not self._fh:
            return
        record.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()


class TranslatorActor:
    """翻译调度：单飞 + 合并，带 sid 校验。

    任意时刻只有一个翻译请求在飞；飞行期间新的请求只保留最新的一个（覆盖旧的），
    飞行结束立刻处理最新请求。过期结果按 seq 丢弃，绝不让旧译文覆盖新字幕。
    """

    def __init__(self, translator, state: SubtitleState, stats: LatencyStats, control: RunControl):
        self.translator = translator
        self.state = state
        self.stats = stats
        self.control = control
        self._next_seq = 0
        self._applied_seq = 0
        self._pending: tuple | None = None
        self._wake = asyncio.Event()
        self._ctx: list[tuple[str, str]] = []

    @property
    def timeout(self) -> float:
        return float(getattr(self.translator, "timeout", 8.0))

    def submit(
        self,
        text: str,
        context: list[tuple[str, str]],
        *,
        final: bool,
        captured_at: float,
        sid: int,
        source_text: str = "",
    ) -> None:
        if self.control.paused.is_set() or not text.strip():
            return
        self._next_seq += 1
        self._pending = (self._next_seq, text, list(context), final, captured_at, sid, source_text)
        self._wake.set()

    async def run(self) -> None:
        while not self.control.stop.is_set():
            await self._wake.wait()
            self._wake.clear()
            while self._pending is not None and not self.control.stop.is_set():
                job = self._pending
                self._pending = None
                await self._do(*job)

    async def _do(
        self,
        seq: int,
        text: str,
        context: list[tuple[str, str]],
        final: bool,
        captured_at: float,
        sid: int,
        source_text: str,
    ) -> None:
        if seq <= self._applied_seq:
            return
        try:
            result = await asyncio.wait_for(
                self.translator.translate(text, context), timeout=self.timeout + 2
            )
        except asyncio.TimeoutError:
            self.stats.on_translate(captured_at, self.timeout, ok=False)
            self.state.bump(errors=1)
            self.state.patch_translation(sid, "（翻译超时）", final=True, failed=True)
            return
        except Exception as exc:  # pragma: no cover
            self.state.bump(errors=1)
            self.state.patch_translation(sid, f"（翻译失败: {exc}）", final=True, failed=True)
            return

        if seq <= self._applied_seq:
            return
        self._applied_seq = seq
        self.state.bump(translations=1)
        self.stats.on_translate(captured_at, result.elapsed_s, result.ok)

        if not result.ok:
            self.state.bump(errors=1)
            self.state.patch_translation(sid, f"（翻译失败: {result.error}）", final=True, failed=True)
            return

        e2e = (time.monotonic() - captured_at) * 1000 if captured_at else 0.0
        hit = self.state.patch_translation(sid, result.text, final=final, e2e_ms=e2e)
        if hit:
            self.state.set_status(e2e_ms=e2e)
        # 定稿的句子进入上下文窗口，跨句保持人称/术语一致
        if final and hit and source_text.strip():
            self._push_context(source_text, result.text)

    def _push_context(self, source: str, translated: str) -> None:
        self._ctx.append((source, translated))
        window = getattr(self.translator, "context_turns", 3) or 3
        keep = max(4, window * 3 + 4)
        if len(self._ctx) > keep:
            del self._ctx[: len(self._ctx) - keep]

    def context(self) -> list[tuple[str, str]]:
        window = getattr(self.translator, "context_turns", 3) or 0
        return list(self._ctx[-window:]) if window else []


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        state: SubtitleState,
        control: RunControl,
        stats: LatencyStats,
        backend: str = "auto",
        replay_wav: str = "",
        log_path: str | Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.control = control
        self.stats = stats
        self.backend = backend
        self.replay_wav = replay_wav
        self.log_path = log_path

        self.source = audio_mod.create_source(cfg, backend=backend, replay_wav=replay_wav)
        self.translator = create_translator(cfg)
        self.actor = TranslatorActor(self.translator, state, stats, control)

        # 自动增益：不同直播的音轨电平能差十几倍（实测某场 RMS 0.0120 vs 正常 0.1393），
        # 不归一化的话识别侧会把它当静音，一句话都不出。
        self.agc = agc_mod.AutoGain() if getattr(cfg.audio, "auto_gain", True) else None
        self._agc_reported = False

        # 当前句
        self._sid = 0
        self._cur_source = ""
        self._cur_captured_at = 0.0
        self._last_spec_text = ""
        self._last_spec_at = 0.0
        self._spec_interval = 0.35   # 中间结果翻译的最小间隔(s)
        self._spec_min_growth = 6    # 至少多出这么多字符才值得再翻一次
        self._sample_rate = cfg.audio.sample_rate
        # 每句的"发起时刻"，用于统计
        self._sentence_started = 0.0

        # 等待断句的缓冲区（translate_on_sentence_end = True 时启用）。
        # 识别中间结果往这里攒，等这一句说完再整句发给翻译。
        self._pending = ""
        self._pending_since = 0.0     # 这一句第一段文字到达的时刻
        self._pending_last = 0.0      # 最近一次有新内容的时刻（用于静默兜底）

    # ------------------------------------------------------------------
    async def run(self) -> None:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="suancaiyu") as pool:
            loop = asyncio.get_running_loop()
            self.source.open()
            self.state.set_status(state="已连接", detail=self.source.description)
            print(f"[audio] 采集源: {self.source.description}")

            stop_watch = asyncio.create_task(self._watch_stop())
            actor_task = asyncio.create_task(self.actor.run())
            # 预热翻译模型：提前把"主要语言"背景告诉它，并建好 HTTP 连接。
            # 丢到线程池里做，不能挡住音频采集。
            loop.run_in_executor(pool, self._warmup_translator)
            try:
                await self._main_loop(loop, pool)
            finally:
                stop_watch.cancel()
                actor_task.cancel()
                await asyncio.gather(stop_watch, actor_task, return_exceptions=True)
                try:
                    self.source.close()
                except Exception:
                    pass

    def _warmup_translator(self) -> None:
        """在后台线程里做翻译预热（语言背景 + 连接建立）。"""
        warm = getattr(self.translator, "warmup", None)
        if warm is None:
            return
        try:
            warm()
        except Exception as exc:  # 预热失败不影响正常翻译
            print(f"[pipeline] 翻译预热失败（不影响使用）: {exc}", file=sys.stderr)

    async def _watch_stop(self) -> None:
        """外部要求停止时，关掉采集源让阻塞的读取尽快返回。"""
        while not self.control.stop.is_set():
            await asyncio.sleep(0.2)
        try:
            self.source.close()
        except Exception:
            pass

    async def _main_loop(self, loop: asyncio.AbstractEventLoop, pool: ThreadPoolExecutor) -> None:
        """识别会话主循环，带**自动降级**。

        为什么需要降级：云端服务会宕机（千问就出现过整晚不可用）。用户如果只配了
        一家，宕机时字幕就彻底没了。这里在"连不上 / 会话异常"时自动轮到下一个
        **已配置 Key** 的引擎（Paraformer → Deepgram → 本地），只有全部失败才退避重试。
        """
        backoff = 1.0
        with TranscriptLogger(self.log_path) as logger:
            while not self.control.stop.is_set():
                chain = self._attempt_chain()
                connected = False

                for cfg, label in chain:
                    if self.control.stop.is_set():
                        break
                    if self.control.reconnect.is_set():
                        self.control.reconnect.clear()
                        backoff = 1.0
                        break
                    try:
                        self.state.set_status(state=f"连接{label}…", detail="")
                        connected = await self._run_asr_session(cfg, label, loop, pool, logger)
                    except Exception as exc:
                        self.state.bump(errors=1)
                        self.state.set_status(state=f"{label} 异常", detail=str(exc)[:60])
                        print(f"[pipeline] {label} 会话异常: {type(exc).__name__}: {exc}", file=sys.stderr)
                        connected = False

                    if connected:
                        backoff = 1.0
                        break
                    if self.control.stop.is_set():
                        break
                    print(f"[pipeline] {label} 未能建立识别，尝试下一个…", file=sys.stderr)

                if self.control.stop.is_set():
                    break
                self.state.set_status(state=f"识别断开，{backoff:.0f}s 后重试", detail="")
                print(f"[pipeline] 本轮结束，{backoff:.0f}s 后重试", file=sys.stderr)
                waited = 0.0
                while waited < backoff and not self.control.stop.is_set():
                    if self.control.reconnect.is_set():
                        break
                    await asyncio.sleep(0.1)
                    waited += 0.1
                backoff = min(backoff * 2, 15.0)

    def _attempt_chain(self) -> list[tuple[Config, str]]:
        """本次要按顺序尝试的识别引擎列表。

        顺序由 `cfg.asr_chain()` 决定：先试用户选定的引擎，再依次降级到其它
        **已经配好 Key** 的引擎（本地识别不需要 Key，永远可用，放最后兜底）。
        """
        from dataclasses import replace

        from .keystore import ASR_BY_ID

        chain: list[tuple[Config, str]] = []
        for pid in self.cfg.asr_chain():
            conf = ASR_BY_ID.get(pid)
            label = conf["label"] if conf else pid
            chain.append((replace(self.cfg, asr=replace(self.cfg.asr, provider=pid)), label))
        if not chain:
            labels = dict(getattr(self.cfg, "asr_provider_labels", {}) or {})
            primary = (self.cfg.asr.provider or "paraformer").lower()
            chain.append((self.cfg, labels.get(primary, primary)))
        return chain

    async def _run_asr_session(
        self,
        cfg: Config,
        label: str,
        loop,
        pool: ThreadPoolExecutor,
        logger: TranscriptLogger,
    ) -> bool:
        """一次 ASR 连接的生命周期：建立 → 泵音频 → 处理事件。

        返回 True 表示"成功连上并正常跑完"（例如 wav 放完了），
        False 表示"连不上/中途异常"，应当降级到下一个引擎。
        """
        asr = create_asr(cfg, sample_rate=self._sample_rate)
        events = asr.run()
        last_speech = time.monotonic()
        idle_timeout = self.cfg.audio.idle_disconnect

        try:
            first = await events.__anext__()
        except StopAsyncIteration:
            return False
        except Exception as exc:
            self.state.bump(errors=1)
            self.state.set_status(state=f"{label} 连接失败", detail=str(exc)[:60])
            print(f"[pipeline] {label} 连接失败: {type(exc).__name__}: {exc}", file=sys.stderr)
            try:
                await asr.aclose()
            except Exception:
                pass
            return False
        self._on_event(first, logger)
        self.state.set_status(state=f"识别中（{label}）", detail=self.source.description[:40])

        async def audio_pump() -> None:
            nonlocal last_speech
            while not self.control.stop.is_set():
                chunk = await loop.run_in_executor(pool, self.source.read, 0.3)
                if chunk is None:
                    if isinstance(self.source, audio_mod.WavSource) and self.source.finished:
                        print("[pipeline] wav 回放结束")
                        self.control.stop.set()
                        return
                    continue
                if self.control.paused.is_set():
                    continue
                # 静默兜底：服务端迟迟不给 sentence_end（网络卡顿、或一句话说了很久）时，
                # 靠这个定时检查把攒着的整句发出去，避免字幕一直空着。
                self._maybe_flush_pending(force=False)
                self.stats.audio_seconds += chunk.samples / self._sample_rate
                if not chunk.silent:
                    last_speech = time.monotonic()
                pcm = chunk.pcm
                if self.agc is not None and not chunk.silent:
                    pcm = self.agc.process(pcm)
                asr.send_audio(pcm, chunk.captured_at)

        pump = asyncio.create_task(audio_pump())
        try:
            async for event in events:
                self._on_event(event, logger)
                if idle_timeout and time.monotonic() - last_speech > idle_timeout:
                    print("[pipeline] 长时间无声，主动结束会话以便重连")
                    break
        finally:
            # 会话结束前把攒着没翻的那半句也发出去，不然字幕会停在半句话上
            self._maybe_flush_pending(force=True)
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            await asr.aclose()
        # 跑到这里说明这次连接是好的（可能是 wav 放完了，或被主动停止）
        return True

    # ------------------------------------------------------------------
    def _on_event(self, event: AsrEvent, logger: TranscriptLogger) -> None:
        if event.kind in ("info", "warning"):
            if event.text:
                print(f"[asr] {event.text}")
            return
        if event.kind == "error":
            self.state.bump(errors=1)
            print(f"[asr] 错误: {event.text}", file=sys.stderr)
            return

        text = (event.text or "").strip()
        captured_at = event.audio_captured_at or time.monotonic()
        on_sentence_end = getattr(self.cfg.translate, "translate_on_sentence_end", True)

        if event.kind == "partial":
            if not text:
                return
            if not self._cur_source:
                self.stats.on_partial(captured_at)
                self._sid = self.state.begin_sentence()
                self._sentence_started = time.monotonic()
            self._cur_source = text
            self._cur_captured_at = captured_at
            self.state.bump(asr_events=1)
            self.state.set_source(self._sid, text)

            if on_sentence_end:
                # 只攒着，不动翻译。等断句（或兜底条件）再整句翻。
                self._pending = text
                now = time.monotonic()
                if not self._pending_since:
                    self._pending_since = now
                self._pending_last = now
                self._maybe_flush_pending(force=False)
            else:
                # 旧行为：中间结果也翻（延迟更低但会碎片化、更烧 token）
                self._maybe_speculate(text, captured_at)
            return

        if event.kind == "final":
            if not event.is_utterance_end:
                # 这段音频定稿了，但服务端认为句子可能还没说完
                if text:
                    if not self._cur_source:
                        self._sid = self.state.begin_sentence()
                        self._sentence_started = time.monotonic()
                    self._cur_source = text
                    self._cur_captured_at = captured_at
                    self.state.set_source(self._sid, text)
                    if on_sentence_end:
                        self._pending = text
                        now = time.monotonic()
                        if not self._pending_since:
                            self._pending_since = now
                        self._pending_last = now
                        # 音频段已定稿：这是最可靠的"半句边界"，
                        # 如果已经攒得够多就先翻掉，不然等真正的断句
                        self._maybe_flush_pending(force=False)
                    else:
                        self._maybe_speculate(text, captured_at, force=True)
                return

            # ---- 真正的断句：这一句说完了 ----
            final_text = text or self._cur_source
            self.stats.on_final(captured_at)
            self.state.bump(asr_events=1)

            if on_sentence_end and self._pending.strip():
                # 缓冲区比"最后一段"更完整（它含了整句），以它为准
                final_text = self._pending.strip()

            if not final_text.strip():
                if self._sid:
                    self.state.commit(self._sid)
                self._reset_sentence()
                return

            if not self._sid or not self._cur_source:
                self._sid = self.state.begin_sentence()
                self._sentence_started = time.monotonic()
            self._cur_source = final_text
            self._cur_captured_at = captured_at
            self.state.set_source(self._sid, final_text, final=True)
            self.state.commit(self._sid)
            self.actor.submit(
                final_text,
                self.actor.context(),
                final=True,
                captured_at=captured_at,
                sid=self._sid,
                source_text=final_text,
            )
            logger.write(
                kind="final",
                source=final_text,
                language=event.language,
                confidence=event.confidence,
            )
            self._reset_sentence()

    # ------------------------------------------------------------------
    #: 可以在句末安全切分、并且切出来仍是"完整小句"的标点。
    #: 中文句号/问号/叹号 + 日文 。！？ + 英文 .!?
    _SOFT_BREAK = "。！？!?…‥"

    def _maybe_flush_pending(self, force: bool = False) -> None:
        """判断攒着的这句话要不要现在就翻。

        触发条件（先满足哪个算哪个）：
          1. force —— 调用方明确要求（会话结束冲刷）
          2. 攒够 max_pending_chars —— 防止一口气说很久导致字幕一直空着
          3. 静默超过 pending_timeout_ms —— 服务端没给 sentence_end 时的兜底
          4. **标点软断句**：攒到一定长度且末尾刚好是句末标点

        关于第 4 条（重要）
        ------------------
        实测发现：带 BGM 的直播音频动态范围被压得极平（P10~P90 只有 1dB），
        **靠能量阈值根本找不到停顿**（Silero VAD 会判定 0 段），
        所以"静默兜底"在这种源上几乎不触发，只剩"攒够字符数硬切"——
        而那会在句子中间切断。

        好在 Paraformer 与 Deepgram 都开了标点预测。用"末尾是句末标点"
        作为断点，既不会在句子中间切，又不依赖音频能量。
        """
        text = self._pending.strip()
        if not text:
            return
        now = time.monotonic()

        if not force:
            hard_limit = int(getattr(self.cfg.translate, "max_pending_chars", 60))
            quiet_ms = (now - self._pending_last) * 1000 if self._pending_last else 0.0
            silent_too_long = quiet_ms >= float(
                getattr(self.cfg.translate, "pending_timeout_ms", 1200)
            )
            too_long = len(text) >= hard_limit
            # 软断句：至少 12 个字，且末尾是句末标点，且已攒到一半以上容量
            punct_ok = (
                len(text) >= 12
                and text[-1] in self._SOFT_BREAK
                and len(text) >= hard_limit * 0.5
            )
            if not (too_long or silent_too_long or punct_ok):
                return

        # 发起翻译，并把这一刻的缓冲区清掉
        sid = self._sid
        if not sid:
            return
        captured = self._cur_captured_at or now
        self.state.set_source(sid, text, final=True)
        self.state.commit(sid)
        self.actor.submit(
            text,
            self.actor.context(),
            final=True,
            captured_at=captured,
            sid=sid,
            source_text=text,
        )
        # 这一句已经提交，后面的内容算下一句
        self._pending = ""
        self._pending_since = 0.0
        self._pending_last = 0.0
        self._last_spec_text = text

    def _maybe_speculate(self, text: str, captured_at: float, force: bool = False) -> None:
        """中间结果翻译：节流，避免每个 token 都打一次 API。

        仅在 translate_on_sentence_end = False 时使用（旧行为）。
        """
        now = time.monotonic()
        grew_enough = len(text) - len(self._last_spec_text) >= self._spec_min_growth
        first_time = not self._last_spec_text
        if not (force or first_time or (grew_enough and now - self._last_spec_at >= self._spec_interval)):
            return
        self._last_spec_at = now
        self._last_spec_text = text
        self.actor.submit(
            text,
            self.actor.context(),
            final=False,
            captured_at=captured_at,
            sid=self._sid,
            source_text=text,
        )

    def _reset_sentence(self) -> None:
        self._cur_source = ""
        self._last_spec_text = ""
        self._last_spec_at = 0.0
        self._sid = 0
        self._pending = ""
        self._pending_since = 0.0
        self._pending_last = 0.0
