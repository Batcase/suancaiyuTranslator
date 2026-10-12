"""诊断：把一段 wav 喂给真实 ASR，原样打印所有事件。

用来回答"为什么端到端只出两句字幕"这类问题——是识别没出东西，
还是出了但被管线丢了。

用法:
    python tools\\asr_event_dump.py samples\\fast_ja_90s.wav [秒数]
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


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    wav = sys.argv[1]
    limit_s = float(sys.argv[2]) if len(sys.argv) > 2 else 0

    cfg = load_config()
    with wave.open(wav, "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    pcm_all = np.frombuffer(raw, dtype=np.int16)
    dur = len(pcm_all) / sr
    if limit_s:
        pcm_all = pcm_all[: int(sr * limit_s)]
        dur = min(dur, limit_s)

    print(f"文件: {wav}  {dur:.1f}s @{sr}Hz")
    print(f"识别: {cfg.asr.provider}  语言提示: {cfg.asr.language}")
    print("-" * 70)

    counts: dict[str, int] = {}
    t_first: float | None = None

    async def run() -> None:
        nonlocal t_first
        asr = create_asr(cfg, sample_rate=sr)
        events = asr.run()
        chunk = sr // 50 * 2 * 2  # 20ms 的字节数
        stop = asyncio.Event()

        async def pump() -> None:
            i = 0
            t0 = time.monotonic()
            while i < len(pcm_all) and not stop.is_set():
                n = min(chunk, (len(pcm_all) - i) * 2)
                span = pcm_all[i : i + n // 2]
                ts = t0 + (i / sr)
                asr.send_audio(span.tobytes(), ts)
                i += n // 2
                await asyncio.sleep(0.02)
            # 送完等一会儿收尾
            await asyncio.sleep(3.0)
            stop.set()

        pump_task = asyncio.create_task(pump())
        try:
            while not stop.is_set():
                try:
                    ev = await asyncio.wait_for(events.__anext__(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except StopAsyncIteration:
                    break
                counts[ev.kind] = counts.get(ev.kind, 0) + 1
                if t_first is None:
                    t_first = time.monotonic()
                flag = "句尾" if ev.is_utterance_end else "    "
                txt = (ev.text or "").strip()
                print(f"  [{ev.kind:7s}] {flag} {txt[:60]}")
        finally:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
            await asr.aclose()

    t0 = time.monotonic()
    asyncio.run(run())
    print("-" * 70)
    print(f"耗时 {time.monotonic()-t0:.1f}s")
    print("事件统计:", counts)
    total_text = counts.get("partial", 0) + counts.get("final", 0)
    if total_text < dur / 5:
        print(
            f"⚠ 只有 {total_text} 条文本事件（{dur:.0f}s 音频）—— 明显偏少。\n"
            "  可能原因：音频里人声太弱/被 BGM 盖住、语种提示不对、或服务端 VAD 判定为无语音。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
