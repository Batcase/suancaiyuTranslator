"""API Key 真实可用性校验。

为什么不能只查格式
------------------
格式对但 Key 失效、被停用、额度用完的情况很常见。如果只在保存时查格式，
用户会在"点了启动翻译却一句话都不出"的时候才发现问题——那时候最难排查。
所以这里在保存前就**真的调一次各家接口**：

* **Deepgram**：走流式 WebSocket（`/v1/listen`），因为翻译时用的就是它。
  只测 REST 会漏掉"Key 有 REST 权限但没有流式权限"这类情况。
  发 0.2 秒静音即可，校验鉴权不消耗有意义额度。
* **DeepSeek / OpenAI 兼容**：发一句最短的对话请求，验证鉴权与额度。

所有函数都不回显 Key，返回结构化的 `ValidationResult` 供界面展示。
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import uuid
import wave
from dataclasses import dataclass

import requests

from .keystore import check_key_format

DEEPGRAM_HOST = "api.deepgram.com"
DASHSCOPE_WS_HOST = "dashscope.aliyuncs.com"


@dataclass
class ValidationResult:
    ok: bool
    message: str
    detail: str = ""
    elapsed_ms: float = 0.0
    #: 需要用户特别留意的提示（例如"这个 Key 只能非日语识别"）
    warning: str = ""


def _silent_wav(seconds: float = 0.2, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _describe_http(status: int, body: str) -> str:
    """把常见 HTTP 错误翻译成人话。"""
    snippet = (body or "").strip().replace("\n", " ")[:160]
    if status == 401:
        return "Key 无效或已被撤销（401）"
    if status == 402:
        return "账户余额不足（402）"
    if status == 403:
        return "Key 没有该权限，或所在地区被限制（403）"
    if status == 429:
        return "请求过于频繁或额度已用完（429）"
    if status >= 500:
        return f"服务端错误（{status}），请稍后重试"
    return f"HTTP {status}：{snippet}"


# ----------------------------------------------------------------------
# Deepgram
# ----------------------------------------------------------------------
async def _deepgram_stream_probe(key: str, timeout: float = 12.0) -> tuple[bool, str, str]:
    """真正开一次流式连接并推一点音频，确认能拿到服务端响应。

    "握手成功"本身就说明 Key 对 `/v1/listen` 有效（无效 Key 会在握手阶段
    被拒），所以即使没等到 Metadata 也算通过；收到 Metadata 则更确定。

    返回 (是否通过, 提示信息, 需要注意的警告)
    """
    import websockets

    params = (
        "model=nova-2&encoding=linear16&sample_rate=16000&channels=1"
        "&interim_results=true&language=ja&endpointing=500"
    )
    url = f"wss://{DEEPGRAM_HOST}/v1/listen?{params}"
    t0 = time.monotonic()
    try:
        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {key}"},
            open_timeout=timeout,
            close_timeout=3,
        ) as ws:
            connect_ms = (time.monotonic() - t0) * 1000
            # 推 0.6 秒静音：足以让服务端回一条 Metadata（也可能回空 Results）
            for _ in range(3):
                await ws.send(b"\x00\x00" * 3200)
            got_metadata = False
            got_anything = False
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                got_anything = True
                text = raw if isinstance(raw, str) else raw.decode("utf-8", "ignore")
                if '"type":"Error"' in text or '"type": "Error"' in text:
                    return False, f"服务端拒绝：{text[:160]}", ""
                if "Metadata" in text:
                    got_metadata = True
                    break
            warn = "" if got_metadata else "未收到服务端回执（握手已通过，功能应当正常）"
            return True, f"流式识别已就绪（握手 {connect_ms:.0f}ms）", warn
    except Exception as exc:
        msg = str(exc)
        low = msg.lower()
        if "401" in msg or "unauthorized" in low:
            return False, "Key 无效（401）", ""
        if "403" in msg or "forbidden" in low:
            return False, "Key 被拒绝或所在地区受限（403）", ""
        if "invalid" in low and "key" in low:
            return False, "Key 无效", ""
        if "timeout" in low or "timed out" in low:
            return False, f"连接超时（{timeout:.0f}s），网络或防火墙可能拦了 WebSocket", ""
        return False, f"连接失败：{msg[:160]}", ""


def validate_deepgram(key: str) -> ValidationResult:
    key = (key or "").strip()
    fmt_err = check_key_format("deepgram", key)
    if fmt_err:
        return ValidationResult(False, fmt_err)
    t0 = time.perf_counter()
    try:
        ok, message, warning = asyncio.run(_deepgram_stream_probe(key))
    except Exception as exc:  # pragma: no cover
        return ValidationResult(False, f"校验异常：{exc}", elapsed_ms=(time.perf_counter() - t0) * 1000)
    return ValidationResult(
        ok, message if ok else f"校验失败：{message}", warning=warning, elapsed_ms=(time.perf_counter() - t0) * 1000
    )


# ----------------------------------------------------------------------
# 阿里云百炼（Paraformer 实时识别）
# ----------------------------------------------------------------------
async def _dashscope_stream_probe(
    key: str, workspace_id: str = "", timeout: float = 12.0
) -> tuple[bool, str, str]:
    """真的跑一次 run-task → task-started，确认 Key 能用于实时识别。

    只查 REST 是不够的：实时识别走的是独立的 WebSocket 服务，Key 没开通
    百炼服务 / 没开通该模型时，REST 可能通而流式被拒。
    """
    import websockets

    host = f"{workspace_id}.cn-beijing.maas.aliyuncs.com" if workspace_id else DASHSCOPE_WS_HOST
    url = f"wss://{host}/api-ws/v1/inference"
    t0 = time.monotonic()
    try:
        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {key}", "user-agent": "SuanCaiYu-Translator/1.0"},
            open_timeout=timeout,
            close_timeout=3,
            ping_interval=None,
        ) as ws:
            handshake_ms = (time.monotonic() - t0) * 1000
            await ws.send(
                json.dumps(
                    {
                        "header": {"action": "run-task", "task_id": str(uuid.uuid4()), "streaming": "duplex"},
                        "payload": {
                            "task_group": "audio",
                            "task": "asr",
                            "function": "recognition",
                            "model": "paraformer-realtime-v2",
                            "parameters": {"format": "pcm", "sample_rate": 16000, "heartbeat": True},
                            "input": {},
                        },
                    }
                )
            )
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                text = raw if isinstance(raw, str) else raw.decode("utf-8", "ignore")
                if "task-started" in text:
                    return True, f"实时识别已就绪（握手 {handshake_ms:.0f}ms）", ""
                if "task-failed" in text:
                    try:
                        header = json.loads(text).get("header", {})
                    except Exception:
                        header = {}
                    return (
                        False,
                        f"服务端拒绝任务：{header.get('error_code', '')} {header.get('error_message', '')}"[:160],
                        "",
                    )
            # 握手过了但没等到 task-started：Key 有效但服务可能抽风
            return True, f"连接成功（握手 {handshake_ms:.0f}ms，未等到 task-started）", "服务端未及时响应，功能可能不稳定"
    except Exception as exc:
        msg = str(exc)
        low = msg.lower()
        if "401" in msg:
            return False, "Key 无效（401）", ""
        if "403" in msg:
            return False, "Key 被拒绝或未开通百炼服务（403）", ""
        if "name or service not known" in low or "getaddrinfo" in low:
            return False, "域名解析失败；如果填了业务空间 ID，请确认它是否正确", ""
        if "timeout" in low or "timed out" in low:
            return False, f"连接超时（{timeout:.0f}s）", ""
        return False, f"连接失败：{msg[:150]}", ""


def validate_dashscope(key: str, workspace_id: str = "") -> ValidationResult:
    key = (key or "").strip()
    fmt_err = check_key_format("dashscope", key)
    if fmt_err:
        return ValidationResult(False, fmt_err)
    t0 = time.perf_counter()
    try:
        ok, message, warning = asyncio.run(_dashscope_stream_probe(key, workspace_id))
    except Exception as exc:  # pragma: no cover
        return ValidationResult(False, f"校验异常：{exc}", elapsed_ms=(time.perf_counter() - t0) * 1000)
    return ValidationResult(
        ok,
        message if ok else f"校验失败：{message}",
        warning=warning,
        elapsed_ms=(time.perf_counter() - t0) * 1000,
    )


# ----------------------------------------------------------------------
# DeepSeek / OpenAI 兼容
# ----------------------------------------------------------------------
def validate_openai_compatible(
    key: str, base_url: str, model: str, *, provider: str = "deepseek", timeout: float = 20.0
) -> ValidationResult:
    key = (key or "").strip()
    fmt_err = check_key_format(provider, key)
    if fmt_err:
        return ValidationResult(False, fmt_err)

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 4,
        "temperature": 0,
        "stream": False,
    }
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.Timeout:
        return ValidationResult(
            False,
            f"请求超时（{timeout:.0f}s）",
            detail=f"接口地址：{url}",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )
    except requests.ConnectionError as exc:
        return ValidationResult(
            False,
            "连不上接口地址",
            detail=f"{url}\n{exc}"[:300],
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )
    except Exception as exc:
        return ValidationResult(False, f"请求异常：{type(exc).__name__}", detail=str(exc)[:200])

    elapsed = (time.perf_counter() - t0) * 1000
    if resp.status_code == 200:
        try:
            content = resp.json()["choices"][0]["message"].get("content", "")
        except Exception:
            content = ""
        return ValidationResult(
            True,
            "翻译接口可用",
            detail=f"模型 {model} 已响应（{content.strip()[:20] or '空回复'}）",
            elapsed_ms=elapsed,
        )
    hint = _describe_http(resp.status_code, resp.text)
    detail = f"接口地址：{url}"
    if resp.status_code == 401 and provider == "deepseek":
        hint += "。如果是用 OpenAI 的 Key，请在设置里把翻译接口地址改成 https://api.openai.com/v1"
    return ValidationResult(False, hint, detail=detail, elapsed_ms=elapsed)


# ----------------------------------------------------------------------
# 硅基流动（SenseVoice 整句识别）
# ----------------------------------------------------------------------
def validate_siliconflow(key: str, timeout: float = 20.0) -> ValidationResult:
    """用一段 0.3 秒静音 wav 调一次转写接口，验证 Key 与权限。"""
    key = (key or "").strip()
    fmt_err = check_key_format("siliconflow", key)
    if fmt_err:
        return ValidationResult(False, fmt_err)
    url = "https://api.siliconflow.cn/v1/audio/transcriptions"
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            files={
                "file": ("probe.wav", _silent_wav(0.3), "audio/wav"),
                "model": (None, "FunAudioLLM/SenseVoiceSmall"),
            },
            timeout=timeout,
        )
    except requests.Timeout:
        return ValidationResult(False, f"请求超时（{timeout:.0f}s）", elapsed_ms=(time.perf_counter() - t0) * 1000)
    except Exception as exc:
        return ValidationResult(False, f"连不上：{type(exc).__name__}", detail=str(exc)[:200])
    elapsed = (time.perf_counter() - t0) * 1000
    if resp.status_code == 200:
        return ValidationResult(True, "语音识别接口可用", detail="静音探测通过", elapsed_ms=elapsed)
    return ValidationResult(False, _describe_http(resp.status_code, resp.text), detail=url, elapsed_ms=elapsed)


# ----------------------------------------------------------------------
# 统一入口
# ----------------------------------------------------------------------
#: 翻译 provider id -> 它需要的 Key 名（与 keystore.TRANSLATE_PROVIDERS 对应）
_TRANSLATE_KEY_PROVIDER = {
    "deepseek": "deepseek",
    "qwen": "dashscope",
    "kimi": "moonshot",
    "siliconflow": "siliconflow",
    "zhipu": "zhipu",
    "openai": "openai",
}


def validate_key(name: str, key: str, cfg=None) -> ValidationResult:
    """按 Key 的名字校验。cfg 用于取自定义接口地址/模型。

    名字既可以是 Key 名（deepseek/dashscope/…），也可以是翻译 provider id
    （qwen/kimi/…），方便界面直接按用户选的那一家去测。
    """
    name = (name or "").strip().lower()

    # —— 识别 ——
    if name in ("dashscope", "paraformer", "aliyun"):
        ws = cfg.asr.paraformer.workspace_id or "" if cfg is not None else ""
        return validate_dashscope(key, ws)
    if name == "deepgram":
        return validate_deepgram(key)
    if name == "siliconflow":
        # 硅基流动既能做识别也能做翻译，这里用识别接口验证（更能说明问题）
        return validate_siliconflow(key)

    # —— 翻译（按 provider id 或 Key 名）——
    pid = name if name in _TRANSLATE_KEY_PROVIDER else None
    if pid is None:
        for k, v in _TRANSLATE_KEY_PROVIDER.items():
            if v == name:
                pid = k
                break
    if pid is not None:
        if pid in ("zhipu", "openai") and pid == name:
            pass  # 下面用注册表默认值
        from .keystore import TRANSLATE_BY_ID

        conf = TRANSLATE_BY_ID.get(pid)
        if conf is None:
            return ValidationResult(False, f"未知的翻译引擎：{pid}")
        return validate_openai_compatible(
            key, conf["base_url"], conf["model"], provider=pid
        )

    return ValidationResult(False, f"未知的 Key 类型：{name}")
