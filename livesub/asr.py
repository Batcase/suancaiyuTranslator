"""流式语音识别客户端。

统一抽象
--------
所有引擎都实现 `AsrClient.run()`（异步生成器，产出 `AsrEvent`）+ `send_audio()`。
上层的编排器只认这两个方法，换引擎不需要动别的代码。

三个引擎的分工
--------------
* **Deepgram**（推荐）：成熟、延迟稳。`interim_results` 给中间结果，
  字幕能"边说边出"；`endpointing` 决定句子什么时候定稿。需要 Key。
* **local**（本地 faster-whisper）：完全免费、不联网，模型从 ModelScope 下。
  代价是吃 CPU、延迟比云端高，适合验证与离线场景。
* **OpenAI Realtime**：走 `/v1/realtime?intent=transcription`，用
  `input_audio_buffer.append` 推流，服务端 VAD 负责断句。

音频格式三边都一样：**16000Hz / 单声道 / PCM16**，不需要本地编码。
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Protocol

import numpy as np
import requests
import websockets

from .config import Config

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"


@dataclass
class AsrEvent:
    """识别结果事件。

    kind:
      * ``partial`` —— 中间结果，text 会不断变化
      * ``final``   —— 该句已定稿
      * ``error``   —— 出错（不一定致命）
      * ``info``    —— 状态信息（连接建立、服务端元数据等）
    """

    kind: str
    text: str = ""
    is_utterance_end: bool = False
    language: str = ""
    confidence: float = 0.0
    audio_captured_at: float = 0.0  # 该段音频"被播放"的时刻，用于算真实延迟
    raw: dict = field(default_factory=dict)


class AsrClient(Protocol):
    def send_audio(self, pcm: bytes, captured_at: float) -> None: ...
    def run(self) -> AsyncIterator[AsrEvent]: ...
    async def aclose(self) -> None: ...


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    return ctx


class _AudioTimeline:
    """记录"发送过的音频"的时间轴，用来把识别结果对回真实播放时刻。

    云端返回结果时并不会告诉你"这是第几毫秒的音频"。字幕要算真实端到端延迟，
    就必须自己维护这条对应关系。

    **不能用墙上时钟做基准**：wav 回放（以及任何比实时快的送音频方式）会在
    很短的墙上时间里推入大量音频，用它当基准会算出"首字延迟 7ms"这种荒唐数字。
    正确做法是拿**每包音频自带的 captured_at**（真实播放时刻）当锚点。
    """

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate
        self._sent_samples = 0
        self._anchor_at = time.monotonic()   # 最近一次音频的 captured_at
        self._anchor_samples = 0
        #: 本次识别音频的起点（第一帧的 captured_at），用于把服务端给的
        #: "相对毫秒"换算回绝对时刻
        self._started_at = self._anchor_at

    def advance(self, n_samples: int, captured_at: float | None = None) -> None:
        if captured_at:
            if self._sent_samples == 0:
                self._started_at = captured_at
            # 每包都带真实播放时刻，直接用它当锚点最准
            self._anchor_at = captured_at
            self._anchor_samples = self._sent_samples
        self._sent_samples += n_samples

    def now(self) -> float:
        """当前已发送音频的末尾对应的"播放时刻"。"""
        return self._anchor_at + (self._sent_samples - self._anchor_samples) / self.sample_rate

    def time_at_ms(self, ms: float) -> float:
        """把"相对本次识别音频起点的毫秒数"换算成绝对播放时刻。

        Paraformer 的结果里带 begin_time / end_time（相对音频起点的 ms），
        用它可以精确定位每句话对应的播放时刻，比用"当前游标"更准。
        """
        return self._started_at + ms / 1000.0


# ----------------------------------------------------------------------
# Deepgram
# ----------------------------------------------------------------------
class DeepgramAsr:
    """Deepgram Live Audio (Listen v1) 流式识别。"""

    def __init__(self, cfg: Config, sample_rate: int = 16000):
        self.cfg = cfg
        self.sample_rate = sample_rate
        self.api_key = cfg.asr_api_key
        self.timeline = _AudioTimeline(sample_rate)
        self._ws: websockets.ClientConnection | None = None
        self._closed = False
        self._last_send = 0.0
        self._pending: list[tuple[bytes, float]] = []

    def _url(self) -> str:
        d = self.cfg.asr.deepgram
        lang = self.cfg.asr.language
        # Deepgram 用 language=multi 表示多语种自动识别（Nova-3）
        lang_param = "multi" if lang in ("", "auto", "multi") else lang
        params = {
            "model": d.model,
            "encoding": "linear16",
            "sample_rate": str(self.sample_rate),
            "channels": "1",
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
            "language": lang_param,
            "endpointing": str(d.endpointing_ms),
            "vad_events": "true",
        }
        return DEEPGRAM_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    async def connect(self) -> None:
        if not self.api_key:
            raise RuntimeError("缺少 DEEPGRAM_API_KEY（写在 .env 里）")
        self._ws = await websockets.connect(
            self._url(),
            additional_headers={"Authorization": f"Token {self.api_key}"},
            ssl=_ssl_ctx(),
            ping_interval=10,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        )

    def send_audio(self, pcm: bytes, captured_at: float) -> None:
        """把一包音频放进发送队列（非阻塞）。"""
        if self._ws is None or self._closed:
            return
        self._pending.append((pcm, captured_at))
        # 游标在**入队之后**才推进：收到的识别结果最多只对应到"入队前"的音频，
        # 先推进会让延迟算成负数。
        self.timeline.advance(len(pcm) // 2, captured_at)

    async def run(self) -> AsyncIterator[AsrEvent]:
        await self.connect()
        assert self._ws is not None
        yield AsrEvent(kind="info", text=f"Deepgram 已连接 (model={self.cfg.asr.deepgram.model})")
        sender = asyncio.create_task(self._sender_loop())
        keepalive = asyncio.create_task(self._keepalive_loop())
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    continue
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue
                event = self._parse(payload)
                if event is not None:
                    yield event
        except websockets.ConnectionClosed as exc:
            yield AsrEvent(kind="error", text=f"Deepgram 连接关闭: {exc}")
        finally:
            for task in (sender, keepalive):
                task.cancel()
            await asyncio.gather(sender, keepalive, return_exceptions=True)

    # ---- 内部 ----
    async def _sender_loop(self) -> None:
        assert self._ws is not None
        try:
            while True:
                if self._pending:
                    # 一次把积压的全部发掉，减少 await 次数
                    batch = self._pending[:]
                    del self._pending[:]
                    for pcm, _ts in batch:
                        await self._ws.send(pcm)
                    self._last_send = time.monotonic()
                else:
                    await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed:
            return
        except Exception as exc:  # pragma: no cover
            print(f"[asr] sender 异常: {exc}", file=sys.stderr)

    async def _keepalive_loop(self) -> None:
        """长时间没音频时发 KeepAlive，避免服务端因空闲断连。"""
        assert self._ws is not None
        try:
            while True:
                await asyncio.sleep(3.0)
                if time.monotonic() - self._last_send > 4.0:
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _parse(self, payload: dict) -> AsrEvent | None:
        ptype = payload.get("type", "")
        if ptype == "Results":
            alt = (payload.get("channel", {}).get("alternatives") or [{}])[0]
            text = (alt.get("transcript") or "").strip()
            is_final = bool(payload.get("is_final"))
            speech_final = bool(payload.get("speech_final"))
            # start/duration 是该段音频在流里的位置，用它反推时间更准
            if not text and not speech_final:
                return None
            return AsrEvent(
                kind="final" if is_final else "partial",
                text=text,
                is_utterance_end=speech_final,
                language=(payload.get("metadata") or {}).get("language", "")
                or (alt.get("languages") or [""])[0],
                confidence=float(alt.get("confidence") or 0.0),
                audio_captured_at=self.timeline.now(),
                raw=payload,
            )
        if ptype == "UtteranceEnd":
            return AsrEvent(kind="final", text="", is_utterance_end=True, audio_captured_at=self.timeline.now())
        if ptype == "SpeechStarted":
            return AsrEvent(kind="info", text="speech_started", audio_captured_at=self.timeline.now())
        if ptype == "Metadata":
            req = payload.get("request_id", "")
            return AsrEvent(kind="info", text=f"session={req[:8]}")
        if ptype == "Error":
            return AsrEvent(kind="error", text=f"{payload.get('variant')}: {payload.get('description')}")
        return None

    async def aclose(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:
                pass
            self._ws = None


# ----------------------------------------------------------------------
# OpenAI Realtime (transcription intent)
# ----------------------------------------------------------------------
class OpenAIRealtimeAsr:
    """OpenAI Realtime 转写会话：只做转写，不做对话。

    用 ``?intent=transcription`` 拿到专用的转写会话，配合服务端 VAD 断句。
    """

    def __init__(self, cfg: Config, sample_rate: int = 16000):
        self.cfg = cfg
        self.sample_rate = sample_rate
        self.api_key = cfg.asr_api_key
        self.timeline = _AudioTimeline(sample_rate)
        self._ws: websockets.ClientConnection | None = None
        self._closed = False
        self._last_send = 0.0
        self._pending: list[tuple[bytes, float]] = []
        self._seg_started = False
        # 兼容两种命名：会话级 vs item 级转写事件
        self._item_text: dict[str, str] = {}

    async def connect(self) -> None:
        if not self.api_key:
            raise RuntimeError("缺少 OPENAI_API_KEY（写在 .env 里）")
        model = self.cfg.asr.openai_realtime.model
        url = f"{OPENAI_REALTIME_URL}?intent=transcription&model={model}"
        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            ssl=_ssl_ctx(),
            ping_interval=10,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        )
        await self._ws.send(json.dumps(self._session_update()))

    def _session_update(self) -> dict:
        conf = self.cfg.asr.openai_realtime
        lang = self.cfg.asr.language
        transcription: dict = {"model": conf.model}
        if lang not in ("", "auto", "multi"):
            transcription["language"] = lang
        return {
            "type": "transcription_session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_transcription": transcription,
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": conf.silence_duration_ms,
                },
                "input_audio_noise_reduction": {"type": "near_field"},
            },
        }

    def send_audio(self, pcm: bytes, captured_at: float) -> None:
        if self._ws is None or self._closed:
            return
        self._pending.append((pcm, captured_at))
        # 同上：游标必须在入队之后推进，否则延迟会算成负数
        self.timeline.advance(len(pcm) // 2, captured_at)

    async def run(self) -> AsyncIterator[AsrEvent]:
        await self.connect()
        assert self._ws is not None
        yield AsrEvent(
            kind="info", text=f"OpenAI Realtime 已连接 (model={self.cfg.asr.openai_realtime.model})"
        )
        sender = asyncio.create_task(self._sender_loop())
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    continue
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue
                for event in self._parse(payload):
                    yield event
        except websockets.ConnectionClosed as exc:
            yield AsrEvent(kind="error", text=f"OpenAI Realtime 连接关闭: {exc}")
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def _sender_loop(self) -> None:
        import base64

        assert self._ws is not None
        try:
            while True:
                if self._pending:
                    batch = self._pending[:]
                    del self._pending[:]
                    for pcm, _ts in batch:
                        await self._ws.send(
                            json.dumps(
                                {
                                    "type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(pcm).decode("ascii"),
                                }
                            )
                        )
                    self._last_send = time.monotonic()
                else:
                    await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _parse(self, payload: dict) -> list[AsrEvent]:
        ptype = payload.get("type", "")
        out: list[AsrEvent] = []

        # 会话级 / item 级增量
        if ptype in (
            "conversation.item.input_audio_transcription.delta",
            "transcript.text.delta",
        ):
            delta = payload.get("delta") or ""
            item_id = payload.get("item_id") or "default"
            self._item_text[item_id] = self._item_text.get(item_id, "") + delta
            text = self._item_text[item_id].strip()
            if text:
                out.append(
                    AsrEvent(kind="partial", text=text, audio_captured_at=self.timeline.now(), raw=payload)
                )

        elif ptype in (
            "conversation.item.input_audio_transcription.completed",
            "transcript.text.done",
        ):
            text = (payload.get("transcript") or payload.get("text") or "").strip()
            item_id = payload.get("item_id") or "default"
            self._item_text.pop(item_id, None)
            if text:
                out.append(
                    AsrEvent(
                        kind="final",
                        text=text,
                        is_utterance_end=True,
                        audio_captured_at=self.timeline.now(),
                        raw=payload,
                    )
                )

        elif ptype in ("conversation.item.input_audio_transcription.failed", "error"):
            err = payload.get("error") or payload
            out.append(AsrEvent(kind="error", text=f"{err.get('code', '')}: {err.get('message', '')}"))

        elif ptype == "input_audio_buffer.speech_started":
            out.append(AsrEvent(kind="info", text="speech_started", audio_captured_at=self.timeline.now()))

        elif ptype == "input_audio_buffer.speech_stopped":
            out.append(AsrEvent(kind="info", text="speech_stopped", audio_captured_at=self.timeline.now()))

        elif ptype == "transcription_session.created":
            out.append(AsrEvent(kind="info", text="transcription session created"))
        elif ptype == "transcription_session.updated":
            out.append(AsrEvent(kind="info", text="transcription session updated"))
        return out

    async def aclose(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None


# ----------------------------------------------------------------------
# 阿里云百炼 · Paraformer 实时识别（国内直连）
# ----------------------------------------------------------------------
DASHSCOPE_WS_HOST = "dashscope.aliyuncs.com"


def paraformer_ws_url(cfg: Config) -> str:
    """接入地址。

    官方建议使用「业务空间专属域名」以获得更稳定性能；填了 workspace_id 就用它。
    Paraformer 目前只在华北2（北京）地域提供。
    """
    ws = (cfg.asr.paraformer.workspace_id or "").strip()
    host = f"{ws}.cn-beijing.maas.aliyuncs.com" if ws else DASHSCOPE_WS_HOST
    return f"wss://{host}/api-ws/v1/inference"


def language_hints_for(language: str) -> list[str]:
    """把 language 配置映射成 Paraformer 的 language_hints。"""
    lang = (language or "").lower()
    if lang in ("", "auto", "multi"):
        return []          # 不传 = 服务端自动判断
    if lang in ("zh", "yue", "en", "ja", "ko", "de", "fr", "ru"):
        return [lang]
    return []              # 不支持的语种交给自动判断，别硬塞导致报错


class ParaformerAsr:
    """阿里云百炼 Paraformer 实时语音识别（原生 WebSocket，不依赖 SDK）。

    与 Deepgram 的语义对应
    ---------------------
    * ``sentence_end=false`` -> partial（中间结果，会不断变化）
    * ``sentence_end=true``  -> final + is_utterance_end（服务端 VAD 判定句子结束）

    协议流程（据官方文档）
    ---------------------
    1. 连接时在请求头带 ``Authorization: Bearer <key>``（握手阶段就校验）
    2. 发 ``run-task`` 文本帧启动任务，等 ``task-started`` 后才能发音频
    3. 持续发**二进制**音频帧（单声道 PCM）
    4. 发 ``finish-task`` 结束，等 ``task-finished``
    """

    def __init__(self, cfg: Config, sample_rate: int = 16000):
        self.cfg = cfg
        self.conf = cfg.asr.paraformer
        self.sample_rate = sample_rate
        self.api_key = cfg.asr_api_key
        self.timeline = _AudioTimeline(sample_rate)
        self._ws: "websockets.ClientConnection | None" = None
        self._closed = False
        self._pending: list[bytes] = []
        self._task_id = str(uuid.uuid4())
        self.task_duration_s = 0.0   # 服务端返回的计费时长，便于观察成本

    # ---- 音频入口（同步，不能阻塞）----
    def send_audio(self, pcm: bytes, captured_at: float) -> None:
        # 注意：时间轴**必须无条件推进**，即使当前没有连接。
        # 否则"连接还没建好/刚断开"这段时间的音频不会计入时间轴，
        # 之后算延迟就会偏（实测会算出接近墙上时钟的荒唐值）。
        self.timeline.advance(len(pcm) // 2, captured_at)
        if self._closed or self._ws is None:
            return
        self._pending.append(pcm)

    async def run(self) -> AsyncIterator[AsrEvent]:
        if not self.api_key:
            raise RuntimeError("缺少 DASHSCOPE_API_KEY（请在设置里填写）")

        url = paraformer_ws_url(self.cfg)
        hints = language_hints_for(self.cfg.asr.language)
        try:
            self._ws = await websockets.connect(
                url,
                additional_headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "user-agent": "SuanCaiYu-Translator/1.0",
                },
                open_timeout=12,
                ping_interval=None,   # 服务端自带心跳，别让客户端 ping 干扰
                max_size=8 * 1024 * 1024,
            )
        except Exception as exc:
            msg = str(exc)
            if "401" in msg or "403" in msg:
                raise RuntimeError("DashScope API Key 无效（401/403）") from exc
            raise RuntimeError(f"连接阿里云百炼失败：{msg[:160]}") from exc

        await self._ws.send(json.dumps(self._run_task_payload(hints), ensure_ascii=False))
        yield AsrEvent(kind="info", text=f"Paraformer 已连接 ({self.conf.model})")

        sender = asyncio.create_task(self._sender_loop())
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    continue
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue
                for event in self._parse(payload):
                    if event.kind == "error":
                        # 任务级错误会让连接失效，直接结束本次会话让上层重连
                        yield event
                        return
                    yield event
        except websockets.ConnectionClosed as exc:
            yield AsrEvent(kind="error", text=f"Paraformer 连接关闭：{exc}")
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    def _run_task_payload(self, hints: list[str]) -> dict:
        params: dict = {
            "format": "pcm",
            "sample_rate": self.sample_rate,
            "disfluency_removal_enabled": self.conf.disfluency_removal_enabled,
            "semantic_punctuation_enabled": self.conf.semantic_punctuation_enabled,
            "punctuation_prediction_enabled": self.conf.punctuation_prediction_enabled,
            "heartbeat": self.conf.heartbeat,
        }
        # max_sentence_silence 只在 VAD 断句（语义断句关闭）时生效
        if not self.conf.semantic_punctuation_enabled:
            params["max_sentence_silence"] = self.conf.max_sentence_silence
        if hints:
            params["language_hints"] = hints
        return {
            "header": {
                "action": "run-task",
                "task_id": self._task_id,
                "streaming": "duplex",
            },
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.conf.model,
                "parameters": params,
                "input": {},
            },
        }

    async def _sender_loop(self) -> None:
        """把音频排空发出。官方建议每次 100ms 左右、1~16KB 一帧。"""
        assert self._ws is not None
        try:
            while True:
                if not self._pending:
                    await asyncio.sleep(0.005)
                    continue
                buf = b"".join(self._pending)
                del self._pending[:]
                # 32ms/包 = 1024 字节；合并成约 2KB（64ms）一帧，
                # 既在建议范围内，也不会把延迟堆高
                frame = 2048
                for i in range(0, len(buf), frame):
                    await self._ws.send(buf[i : i + frame])
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _parse(self, payload: dict) -> list[AsrEvent]:
        header = payload.get("header") or {}
        event = header.get("event") or ""

        if event == "result-generated":
            body = payload.get("payload") or {}
            output = body.get("output") or {}
            sentence = output.get("sentence") or {}
            if sentence.get("heartbeat"):
                return []          # 心跳包，跳过
            text = (sentence.get("text") or "").strip()
            if not text:
                return []
            usage = body.get("usage") or {}
            if usage.get("duration"):
                self.task_duration_s = float(usage["duration"])
            # begin_time/end_time 是相对音频起点的毫秒数。
            # 用句子**结束**时间定位"说完的时刻"，得到的是最保守（偏大）的延迟——
            # 宁可保守，也不要报喜不报忧。
            end_ms = sentence.get("end_time")
            at = (
                self.timeline.time_at_ms(float(end_ms))
                if isinstance(end_ms, (int, float))
                else self.timeline.now()
            )
            sentence_end = bool(sentence.get("sentence_end"))
            return [
                AsrEvent(
                    kind="final" if sentence_end else "partial",
                    text=text,
                    is_utterance_end=sentence_end,
                    audio_captured_at=at,
                    raw={"usage": usage},
                )
            ]

        if event == "task-failed":
            code = header.get("error_code", "")
            msg = header.get("error_message", "")
            hint = ""
            if "timeout" in msg.lower():
                hint = "（连接后 23 秒内没收到音频，或音频发送中断；检查网络稳定性）"
            return [AsrEvent(kind="error", text=f"Paraformer 任务失败 {code}: {msg}{hint}")]

        # task-started / task-finished 不需要上抛
        return []

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.send(
                    json.dumps(
                        {
                            "header": {
                                "action": "finish-task",
                                "task_id": self._task_id,
                                "streaming": "duplex",
                            },
                            "payload": {"input": {}},
                        }
                    )
                )
                await asyncio.sleep(0.15)
                await self._ws.close()
            except Exception:
                pass
            self._ws = None


# ----------------------------------------------------------------------
# 硅基流动 · SenseVoice（整句识别，国内直连）
# ----------------------------------------------------------------------
class RemoteBatchAsr:
    """把"一整句话"发给云端转写接口的识别方式（国内可用）。

    和流式（Paraformer / Deepgram）的区别
    ------------------------------------
    * 它没有流式接口，只能一次提交一段完整音频，所以这里自己做了"静音断句"：
      攒够 ``max_utterance_s`` 秒、或静音超过 ``silence_end_s`` 秒，就把这段发出去。
    * 因此端到端会多出"等待说完 + 一次 HTTP 往返"的时间（实测约 1~2s），
      但**识别质量好**（中文/日语/英语/韩语都准），且国内直连稳定。

    用它的典型场景：Paraformer 额度用完或临时不可用时的备用。
    """

    def __init__(self, cfg: Config, sample_rate: int = 16000):
        self.cfg = cfg
        self.conf = cfg.asr.sensevoice
        self.sample_rate = sample_rate
        self.api_key = (
            self.conf.api_key
            or os.getenv("SILICONFLOW_API_KEY", "")
        )
        self.timeline = _AudioTimeline(sample_rate)
        self._audio: asyncio.Queue[tuple[np.ndarray, float]] = asyncio.Queue(maxsize=256)
        self._closed = False
        self.dropped = 0

    def send_audio(self, pcm: bytes, captured_at: float) -> None:
        if self._closed:
            return
        self.timeline.advance(len(pcm) // 2, captured_at)
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            self._audio.put_nowait((arr, captured_at))
        except asyncio.QueueFull:
            try:
                self._audio.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass

    async def run(self) -> AsyncIterator[AsrEvent]:
        if not self.api_key:
            raise RuntimeError("缺少 SILICONFLOW_API_KEY（请在设置里填写）")
        loop = asyncio.get_running_loop()
        yield AsrEvent(kind="info", text=f"硅基流动 SenseVoice 已就绪 ({self.conf.model})")

        buf = np.zeros(0, dtype=np.float32)
        buf_start = 0.0
        silence_s = 0.0
        speech_s = 0.0
        try:
            while not self._closed:
                try:
                    chunk, captured = await asyncio.wait_for(self._audio.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    chunk, captured = None, 0.0

                if chunk is not None and chunk.size:
                    if buf.size == 0:
                        buf_start = captured
                    buf = np.concatenate([buf, chunk]) if buf.size else chunk
                    level = float(np.sqrt(np.mean(chunk * chunk)))
                    dur = chunk.size / self.sample_rate
                    if level < self.conf.silence_rms:
                        silence_s += dur
                    else:
                        silence_s = 0.0
                        speech_s += dur

                if buf.size == 0:
                    continue

                buf_seconds = buf.size / self.sample_rate
                quiet = silence_s >= self.conf.silence_end_s
                too_long = buf_seconds >= self.conf.max_utterance_s
                if not (quiet or too_long or buf_seconds >= self.conf.window_s):
                    continue

                # 几乎没人说话就别浪费一次请求（也避免模型在静音上乱编）
                if speech_s < 0.3:
                    buf = np.zeros(0, dtype=np.float32)
                    silence_s = speech_s = 0.0
                    continue

                # 中间结果：先说"正在识别"，让界面知道在做事
                yield AsrEvent(kind="partial", text="…", audio_captured_at=buf_start)
                audio = buf
                text = await loop.run_in_executor(None, self._transcribe, audio)

                if text:
                    yield AsrEvent(
                        kind="final",
                        text=text,
                        is_utterance_end=True,
                        audio_captured_at=buf_start,
                    )
                buf = np.zeros(0, dtype=np.float32)
                silence_s = speech_s = 0.0
        finally:
            self._closed = True

    def _transcribe(self, audio: np.ndarray) -> str:
        """同步 HTTP 调用（在 executor 里跑）。"""
        import io as _io
        import wave as _wave

        pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
        buf = _io.BytesIO()
        with _wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(pcm.tobytes())

        url = self.conf.base_url or "https://api.siliconflow.cn/v1/audio/transcriptions"
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={
                    "file": ("audio.wav", buf.getvalue(), "audio/wav"),
                    "model": (None, self.conf.model),
                },
                timeout=self.conf.timeout_s,
            )
        except requests.Timeout:
            print("[asr] 硅基流动转写超时", file=sys.stderr)
            return ""
        except Exception as exc:
            print(f"[asr] 硅基流动转写失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return ""
        if resp.status_code != 200:
            print(f"[asr] 硅基流动 HTTP {resp.status_code}: {resp.text[:160]}", file=sys.stderr)
            return ""
        try:
            data = resp.json()
        except Exception:
            return ""
        # 兼容两种返回：{"text": ...} 或 OpenAI 风格
        text = (data.get("text") or "").strip()
        if not text and isinstance(data.get("results"), list):
            text = " ".join(str(r.get("text", "")) for r in data["results"]).strip()
        return text

    async def aclose(self) -> None:
        self._closed = True


# ----------------------------------------------------------------------
# 本地离线识别（faster-whisper）
# ----------------------------------------------------------------------
#: 模型名 -> ModelScope 仓库（按顺序尝试）
WHISPER_REPOS: dict[str, list[str]] = {
    "tiny": ["Systran/faster-whisper-tiny", "pengzhendong/faster-whisper-tiny"],
    "base": ["Systran/faster-whisper-base", "pengzhendong/faster-whisper-base"],
    "small": ["pengzhendong/faster-whisper-small", "Systran/faster-whisper-small"],
    "medium": ["pengzhendong/faster-whisper-medium", "Systran/faster-whisper-medium"],
    "large-v3": ["pengzhendong/faster-whisper-large-v3", "Systran/faster-whisper-large-v3"],
}


def _models_root() -> Path:
    """本地 Whisper 模型的缓存目录（放在程序目录下，打包后也不丢）。"""
    from .keystore import ROOT as _ROOT

    return _ROOT / "models"


def find_cached_whisper(model: str) -> str | None:
    """在本工程的 models/ 下找已经下载好的模型目录，避免每次启动都查远端。"""
    cache = _models_root() / "models"
    if not cache.is_dir():
        return None
    for d in sorted(cache.glob(f"*faster-whisper-{model}*")):
        for cand in (d / "snapshots" / "master", d):
            if (cand / "model.bin").exists():
                return str(cand)
    return None


def resolve_whisper_model(cfg: Config) -> str:
    """把配置里的模型名解析成可用的本地目录，必要时从 ModelScope 下载。

    为什么走 ModelScope：HuggingFace 在国内网络经常连不上（本机实测超时），
    而 ModelScope 可达且速度很好（484MB 的 small 模型 22 秒下完）。
    """
    conf = cfg.asr.local
    if conf.model_path:
        return conf.model_path

    cached = find_cached_whisper(conf.model)
    if cached:
        return cached

    try:
        from modelscope.hub.snapshot_download import snapshot_download
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "本地识别需要 faster-whisper 与 modelscope：\n"
            "  python -m pip install faster-whisper modelscope "
            "-i https://pypi.tuna.tsinghua.edu.cn/simple"
        ) from exc

    candidates = WHISPER_REPOS.get(conf.model, [f"pengzhendong/faster-whisper-{conf.model}"])
    last_err: Exception | None = None
    for candidate in candidates:
        try:
            print(
                f"[asr] 首次使用，正在从 ModelScope 下载模型 {candidate} …"
                "（tiny 约 75MB / base 约 145MB / small 约 484MB）",
                flush=True,
            )
            path = snapshot_download(candidate, cache_dir=str(_models_root()))
            snap = Path(path) / "snapshots" / "master"
            return str(snap if (snap / "model.bin").exists() else path)
        except Exception as exc:
            last_err = exc
    raise SystemExit(
        f"模型下载失败：{last_err}\n"
        "  也可以自己下好模型，然后把 config.toml 里的 asr.local.model_path 指向该目录；\n"
        "  或者先用云端识别 --asr deepgram"
    )


class LocalWhisperAsr:
    """本地 Whisper 流式识别（滑窗 + 静音断句）。

    与 Deepgram 的语义差异（很重要）
    -------------------------------
    Deepgram 的 ``is_final`` 表示"这段音频定稿了"，一句话可以分多次定稿；
    本地识别是"整句累积后一次性定稿"，所以这里**只发**
    ``is_utterance_end=True`` 的 final，与管线的语义保持一致。

    工作方式
    --------
    * 音频先攒进"当前句子"缓冲；缓冲达到 ``window_s`` 秒就转写一次，
      结果作为**中间结果**发出去（字幕开始往前爬）。
    * 连续静音超过 ``silence_end_s`` 秒 -> 判定句子结束，把整段音频转写后
      发 **final**，清空缓冲开始下一句。
    * 因为同一段音频会被重复转写（Whisper 每次输出略有差异），只有在
      "有新音频进来"或"文本确实变长"时才发中间结果，避免字幕抖动。
    * 单句超过 ``max_utterance_s`` 秒强制切断，防止延迟无限增长。

    性能前提（本机 CPU 实测）：tiny RTF 约 0.13~0.29 才跟得上实时；
    small RTF 约 0.7~1.6 会越算越落后，只适合离线转写整段文件。
    """

    def __init__(self, cfg: Config, sample_rate: int = 16000):
        self.cfg = cfg
        self.conf = cfg.asr.local
        self.sample_rate = sample_rate
        self._model = None
        self._closed = False
        self._audio: asyncio.Queue[tuple[np.ndarray, float]] = asyncio.Queue(maxsize=96)
        self.dropped = 0
        self.timeline = _AudioTimeline(sample_rate)

    # ---- 供音频泵调用（同步，绝不阻塞）----
    def send_audio(self, pcm: bytes, captured_at: float) -> None:
        if self._closed:
            return
        self.timeline.advance(len(pcm) // 2, captured_at)
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            self._audio.put_nowait((arr, captured_at))
        except asyncio.QueueFull:
            # 本地识别跟不上时丢最旧的，绝不让延迟累积
            try:
                self._audio.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass

    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "本地识别需要 faster-whisper：python -m pip install faster-whisper"
            ) from exc
        path = resolve_whisper_model(self.cfg)
        print(f"[asr] 加载本地模型 {path} ({self.conf.device}/{self.conf.compute_type})…", flush=True)
        t0 = time.monotonic()
        self._model = WhisperModel(
            path, device=self.conf.device, compute_type=self.conf.compute_type
        )
        print(f"[asr] 模型加载完成 {time.monotonic() - t0:.1f}s", flush=True)
        return self._model

    async def run(self) -> AsyncIterator[AsrEvent]:
        """生产者/消费者解耦的实时识别循环。

        为什么必须解耦：``transcribe()`` 一次要阻塞 0.5s（tiny、CPU），这期间
        音频还在源源不断地进来。如果在一个循环里"取一包音频 -> 转写"，队列会
        持续积压、窗口永远停在起始位置（实测音频时间戳 12 秒只推进了 0.5 秒）。
        所以拆成两个任务：生产者只负责把队列里的音频**排空**进缓冲，消费者按
        缓冲时长/静音情况决定什么时候转写。
        """
        loop = asyncio.get_running_loop()
        model = await loop.run_in_executor(None, self._load_model)
        lang = None if self.cfg.asr.language in ("", "auto", "multi") else self.cfg.asr.language
        yield AsrEvent(kind="info", text=f"Local Whisper 已就绪 (model={self.conf.model})")

        def transcribe(audio: np.ndarray) -> str:
            segments, _info = model.transcribe(
                audio,
                language=lang,
                beam_size=1,                       # 贪心解码，最快
                vad_filter=False,                  # 静音自己判断，省一层开销
                condition_on_previous_text=False,  # 避免上一句污染下一句
                without_timestamps=True,
            )
            return " ".join(s.text.strip() for s in segments).strip()

        # 用 list 包一层，让生产者和消费者共享可变状态（局部变量无法跨任务赋值）
        buf: list[np.ndarray] = [np.zeros(0, dtype=np.float32)]
        start_at: list[float] = [0.0]   # 缓冲里第一帧音频的采集时刻
        silence_s: list[float] = [0.0]
        speech_s: list[float] = [0.0]   # 缓冲里"真正有人说话"的累计时长
        ready = asyncio.Event()

        async def produce() -> None:
            """只负责排空音频队列，绝不阻塞在转写上。"""
            while not self._closed:
                try:
                    item = await asyncio.wait_for(self._audio.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                # 一次性把已经排队的音频全部取走，避免积压
                batch = [item]
                while True:
                    try:
                        batch.append(self._audio.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                for chunk, captured in batch:
                    if chunk.size == 0:
                        continue
                    if buf[0].size == 0:
                        start_at[0] = captured
                    buf[0] = np.concatenate([buf[0], chunk]) if buf[0].size else chunk
                    level = float(np.sqrt(np.mean(chunk * chunk)))
                    dur = chunk.size / self.sample_rate
                    if level < self.conf.silence_rms:
                        silence_s[0] += dur
                    else:
                        # Whisper 在纯静音上会"幻觉"出不存在的台词
                        # （实测会凭空冒出 "Thanks for watching."），
                        # 所以必须记录窗口里真正有人说话的时长，用作输出门限。
                        silence_s[0] = 0.0
                        speech_s[0] += dur
                ready.set()

        prod = asyncio.create_task(produce())
        shown = ""     # 上次发给字幕的文本，用来去重
        # 结转音频的样本数：软切窗后 buffer 里保留的重叠音频**已经转写过**，
        # 下一窗口必须把它们裁掉再识别，否则上一句的尾巴会被算进下一句
        # （实测会出现"…in 4K gaming."和"…in 4K gaming. Let me know…"两句串在一起）。
        carry = 0
        try:
            while not self._closed:
                ready.clear()
                cur = buf[0]
                if cur.size == 0:
                    await ready.wait()
                    continue
                buf_seconds = cur.size / self.sample_rate
                quiet = silence_s[0] >= self.conf.silence_end_s
                too_long = buf_seconds >= self.conf.max_utterance_s
                if not (quiet or too_long or buf_seconds >= self.conf.window_s):
                    await ready.wait()
                    continue

                fresh = cur[carry:] if carry else cur
                # 窗口里几乎没人说话就别送模型：既能省 CPU，也能避开 Whisper
                # 在静音上凭空编台词的问题
                has_speech = speech_s[0] >= 0.3 and fresh.size > int(0.3 * self.sample_rate)
                if has_speech:
                    text = (await loop.run_in_executor(None, transcribe, fresh)).strip()
                else:
                    text = ""
                # 时间戳对齐"这次真正新转写的那段音频"的开头
                win_audio_at = start_at[0] + carry / self.sample_rate
                carry = 0
                cur = buf[0]

                if quiet or too_long:
                    # ---- 定稿这一段 ----
                    if has_speech and text and text != shown:
                        shown = text
                        yield AsrEvent(
                            kind="final",
                            text=text,
                            is_utterance_end=True,
                            audio_captured_at=win_audio_at,
                        )
                    if quiet:
                        # 真静音 -> 整句结束，彻底清空
                        buf[0] = np.zeros(0, dtype=np.float32)
                        silence_s[0] = 0.0
                        speech_s[0] = 0.0
                        start_at[0] = 0.0
                        shown = ""
                    else:
                        # 只是说太久 -> 保留一点重叠音频继续，减少断词
                        keep = min(
                            max(0, int(self.conf.overlap_s * self.sample_rate)),
                            max(0, cur.size - 1),
                        )
                        buf[0] = cur[cur.size - keep :].copy() if keep else np.zeros(0, dtype=np.float32)
                        # 既然要裁掉重叠部分，就不必依赖文本拼接去重了
                        shown = ""
                        carry = keep
                        silence_s[0] = 0.0
                        speech_s[0] = 0.0
                        start_at[0] = (
                            start_at[0] + (cur.size - keep) / self.sample_rate if keep else 0.0
                        )
                elif not quiet and not too_long:
                    # 中间结果：只发不切。
                    # 关键：这里**不能**裁掉已转写的音频，否则缓冲永远长不到
                    # max_utterance_s，最后一句话就永远不会定稿（实测会漏）。
                    # 重复转写同一段音频的代价可以接受——tiny 的 RTF 只有 0.3。
                    if has_speech and text and text != shown:
                        shown = text
                        yield AsrEvent(kind="partial", text=text, audio_captured_at=start_at[0])
        finally:
            self._closed = True
            prod.cancel()
            await asyncio.gather(prod, return_exceptions=True)

    async def aclose(self) -> None:
        self._closed = True
        self._model = None


# ----------------------------------------------------------------------
# Mock（无需任何 Key，用于验证管线与悬浮窗）
# ----------------------------------------------------------------------
class MockAsr:
    """不联网的假识别：每 2 秒产出一句假台词，验证 UI 与延迟统计链路。"""

    SENTENCES = [
        "Hello everyone, welcome back to the stream.",
        "Today we are going to talk about the new graphics card.",
        "Let me know in the chat where you are watching from.",
        "This is a mock transcript for testing the pipeline.",
    ]

    def __init__(self, cfg: Config, sample_rate: int = 16000):
        self.cfg = cfg
        self.sample_rate = sample_rate
        self._i = 0
        self._closed = False

    def send_audio(self, pcm: bytes, captured_at: float) -> None:  # noqa: ARG002
        return

    async def run(self) -> AsyncIterator[AsrEvent]:
        yield AsrEvent(kind="info", text="Mock ASR（不联网）")
        while not self._closed:
            # 把"这句话开始播"的时刻记为 2 秒前，这样延迟统计能反映真实的
            # 音频→字幕 链路耗时，而不是只显示翻译本身花了多久
            started = time.monotonic()
            await asyncio.sleep(2.0)
            text = self.SENTENCES[self._i % len(self.SENTENCES)]
            self._i += 1
            yield AsrEvent(kind="partial", text=text[: len(text) // 2], audio_captured_at=started)
            await asyncio.sleep(0.4)
            yield AsrEvent(kind="final", text=text, is_utterance_end=True, audio_captured_at=started)

    async def aclose(self) -> None:
        self._closed = True


def create_asr(cfg: Config, sample_rate: int = 16000) -> AsrClient:
    provider = (cfg.asr.provider or "").lower()
    if provider in ("paraformer", "dashscope", "aliyun"):
        return ParaformerAsr(cfg, sample_rate)
    if provider == "deepgram":
        return DeepgramAsr(cfg, sample_rate)
    if provider in ("sensevoice", "siliconflow"):
        return RemoteBatchAsr(cfg, sample_rate)
    if provider in ("openai", "openai_realtime"):
        return OpenAIRealtimeAsr(cfg, sample_rate)
    if provider in ("local", "whisper", "faster_whisper"):
        return LocalWhisperAsr(cfg, sample_rate)
    if provider == "mock":
        return MockAsr(cfg, sample_rate)
    raise SystemExit(
        f"未知的识别引擎: {cfg.asr.provider}"
        "（可选 paraformer / deepgram / sensevoice / local / openai_realtime / mock）"
    )
