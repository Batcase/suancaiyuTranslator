"""分层定位"回环打开成功但读不到数据"的问题。

分三层测，看问题出在哪一层：
  L1  WasapiLoopback 直接读（原生 48k float32）
  L2  WasapiSource 读（16k int16）
  L3  指定不同设备时，实际打开的是哪个

为什么要分层：`Initialize` 成功不代表能拿到数据。可能是
事件回调没触发、可能是端点本身没有音频流、也可能是转换层出错。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from livesub import wasapi as W  # noqa: E402


def l1_native(hint: str, seconds: float) -> None:
    print(f"\n--- L1 WasapiLoopback 直接读  hint={hint[:40]!r} ---")
    cap = W.WasapiLoopback(sample_rate=16000, device_hint=hint)
    try:
        t0 = time.monotonic()
        cap.open()
        print(f"    打开耗时 {time.monotonic()-t0:.2f}s")
        print(f"    device_name = {cap.device_name!r}")
        print(f"    format_desc = {cap.format_desc}")
        print(f"    mode        = {cap.mode_desc}")
        print(f"    use_event   = {cap._use_event}")
        nonempty = 0
        total = 0
        peak = 0.0
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            arr = cap.read(timeout=0.3)
            total += 1
            if arr is not None and arr.size:
                nonempty += 1
                x = arr.astype(np.float32)
                if x.dtype != np.float32:
                    x = x / 32768.0
                peak = max(peak, float(np.abs(x).max()))
        print(f"    读取 {total} 次，非空 {nonempty} 次，峰值 {peak:.4f}")
    except Exception as exc:
        print(f"    ❌ {type(exc).__name__}: {exc}")
    finally:
        try:
            cap.close()
        except Exception:
            pass


def l2_source(hint: str, seconds: float) -> None:
    print(f"\n--- L2 WasapiSource 读  device={hint[:40]!r} ---")
    from livesub.audio import WasapiSource

    src = WasapiSource(sample_rate=16000, chunk_ms=32, device=hint)
    try:
        src.open()
        print(f"    description = {src.description}")
        got = 0
        peak = 0.0
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            ch = src.read(0.3)
            if ch is None:
                continue
            got += 1
            x = np.frombuffer(ch.pcm, dtype=np.int16).astype(np.float32) / 32768.0
            if x.size:
                peak = max(peak, float(np.abs(x).max()))
        print(f"    拿到 {got} 块，峰值 {peak:.4f}")
        if got == 0:
            print("    ❌ 一块都没拿到 —— 这就是「没字幕」的直接原因")
    except Exception as exc:
        print(f"    ❌ {type(exc).__name__}: {exc}")
    finally:
        try:
            src.close()
        except Exception:
            pass


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0

    print("=" * 72)
    print("回环采集分层诊断")
    print("=" * 72)
    print(">>> 请确保**现在有声音在播放**（视频/音乐），不要停 <<<")

    eps = W.list_render_endpoints_default_first()
    print(f"\n活动播放设备 {len(eps)} 个：")
    for i, e in enumerate(eps):
        print(f"  [{i}] {'★默认' if e.is_default else '     '} {e.name}")

    # 默认设备（不传 hint）
    l1_native("", seconds)
    l2_source("", seconds)

    # 指定默认设备的完整名
    if eps:
        l1_native(eps[0].name, seconds * 0.5)
        l2_source(eps[0].name, seconds * 0.5)

    print("\n" + "=" * 72)
    print("解读")
    print("=" * 72)
    print("  L1 非空=0  → 底层就没数据：端点没在播放，或事件回调不触发")
    print("  L1 有数据、L2 为 0 → 转换层/分块层有问题")
    print("  指定设备名后 device_name 不是那个设备 → 设备选择有问题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
