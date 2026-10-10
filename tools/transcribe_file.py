"""离线整段转写：不跑实时管线，直接看某个模型对一段音频的完整识别效果。

用途
----
* 对比 tiny / small / medium 在自己机器上的**准确率**（实时性看 asr_probe.py）
* 快速确认某段音频到底说的是什么（排查识别错误）
* 判断"要不要花钱上云端识别"——把同一段音频的本地结果和 Deepgram 结果对比

用法:
    python tools/transcribe_file.py samples\\ja_test.wav
    python tools/transcribe_file.py samples\\ja_test.wav --model small
    python tools/transcribe_file.py samples\\ja_test.wav --lang en --model base
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from livesub.config import load_config  # noqa: E402


def load_wav_16k(path: str) -> tuple[np.ndarray, float]:
    with wave.open(path, "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise SystemExit(f"只支持 16bit PCM wav，当前 {sw*8}bit")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        idx = np.linspace(0, a.size - 1, int(a.size * 16000 / sr))
        a = np.interp(idx, np.arange(a.size), a).astype(np.float32)
    return a, a.size / 16000


def main() -> int:
    p = argparse.ArgumentParser(description="离线整段转写（看准确率，不看延迟）")
    p.add_argument("wav", help="16bit PCM wav 文件")
    p.add_argument("--model", default="", help="tiny | base | small | medium（默认取配置）")
    p.add_argument("--lang", default="", help="源语言，默认取配置")
    p.add_argument("--beam", type=int, default=5, help="beam size，越大越准越慢（默认 5）")
    args = p.parse_args()

    cfg = load_config()
    model_name = args.model or cfg.asr.local.model
    lang = args.lang or cfg.asr.language
    if lang in ("auto", "multi", ""):
        lang = None

    audio, dur = load_wav_16k(args.wav)
    print(f"文件   : {args.wav}  ({dur:.1f}s @16kHz)")
    print(f"模型   : {model_name}   语言: {lang or 'auto'}   beam: {args.beam}")

    from faster_whisper import WhisperModel

    from livesub.asr import resolve_whisper_model
    from dataclasses import replace

    conf = replace(cfg.asr.local, model=model_name)
    cfg2 = replace(cfg, asr=replace(cfg.asr, local=conf))
    path = resolve_whisper_model(cfg2)
    print(f"目录   : {path}")

    t0 = time.monotonic()
    model = WhisperModel(path, device=cfg.asr.local.device, compute_type=cfg.asr.local.compute_type)
    load_s = time.monotonic() - t0

    t0 = time.monotonic()
    segments, info = model.transcribe(
        audio,
        language=lang,
        beam_size=args.beam,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    segs = list(segments)
    took = time.monotonic() - t0

    print(f"加载   : {load_s:.1f}s    整段转写: {took:.1f}s  (RTF={took/dur:.2f})")
    if info and getattr(info, "language", None):
        prob = getattr(info, "language_probability", 0.0)
        print(f"判定语种: {info.language} (p={prob:.2f})")
    print("-" * 60)
    for s in segs:
        print(f"[{s.start:6.2f} → {s.end:6.2f}] {s.text.strip()}")
    print("-" * 60)
    print("全文:")
    print(" ".join(s.text.strip() for s in segs).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
