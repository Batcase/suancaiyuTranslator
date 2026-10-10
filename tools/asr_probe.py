"""直接驱动识别引擎，打印它产出的每个事件与"音频→结果"的真实延迟。

用法:
    python tools\\asr_probe.py samples\\ja_test.wav                 # 用配置里的引擎（默认 local）
    python tools\\asr_probe.py samples\\ja_test.wav --asr deepgram   # 测云端流式
    python tools\\asr_probe.py samples\\en_test.wav --lang en
"""

from __future__ import annotations

import asyncio
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from livesub.asr import create_asr  # noqa: E402
from livesub.config import load_config  # noqa: E402


async def main() -> int:
    argv = sys.argv[1:]
    lang_override = None
    asr_override = None
    for flag, name in (("--lang", "lang"), ("--asr", "asr")):
        if flag in argv:
            i = argv.index(flag)
            value = argv[i + 1]
            del argv[i : i + 2]
            if name == "lang":
                lang_override = value
            else:
                asr_override = value
    wav = argv[0] if argv else "samples/ja_test.wav"

    cfg = load_config()
    if asr_override:
        cfg.asr.provider = asr_override
    if lang_override:
        cfg.asr.language = lang_override
    print(f"识别引擎: {cfg.asr.provider}  语言: {cfg.asr.language}")

    with wave.open(wav, "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if sr != 16000:
        idx = np.linspace(0, a.size - 1, int(a.size * 16000 / sr))
        a = np.interp(idx, np.arange(a.size), a).astype(np.int16)
    print(f"音频 {a.size/16000:.1f}s @16kHz")

    asr = create_asr(cfg, sample_rate=16000)
    chunk = 320  # 20ms
    t0 = time.monotonic()
    # 回放时钟：captured_at 必须按"播放位置"给，而不是当天的墙上时钟，
    # 否则延迟统计会算出负数。这里用 t0 + 已推时长 严格对齐实时。
    replay_clock = t0

    async def pump() -> None:
        nonlocal replay_clock
        pos = 0
        while pos < a.size:
            piece = a[pos : pos + chunk]
            pos += chunk
            replay_clock = t0 + pos / 16000
            asr.send_audio(piece.tobytes(), replay_clock)
            # 按实时节奏推，模拟真实采集
            delay = replay_clock - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        print("[pump] 音频推完")

    pump_task = asyncio.create_task(pump())
    events = asr.run()
    try:
        while True:
            try:
                ev = await asyncio.wait_for(events.__anext__(), timeout=6.0)
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
            age = (time.monotonic() - ev.audio_captured_at) * 1000 if ev.audio_captured_at else 0.0
            print(f"  [{ev.kind:<7}] end={ev.is_utterance_end} audio_age={age:7.0f}ms  {ev.text!r}")
    finally:
        pump_task.cancel()
        await asr.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
