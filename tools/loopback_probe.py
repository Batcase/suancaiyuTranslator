"""逐个播放设备测回环音量，找出"哪个设备真的能采到声音"。

为什么需要
----------
用户反馈：**插上耳机就采不到任何音频，拔掉耳机就有了**。

WASAPI 回环是"跟着某个播放设备走"的——回环只反映**该设备正在播放的内容**。
如果默认设备切到了耳机，而采集逻辑打开的却是另一个设备（或耳机端点本身
在独占模式/蓝牙链路下拿不到数据），就会一条音频都没有。

更隐蔽的是：`IAudioClient::Initialize` 可能**成功**，但读出来全是静音。
只看"打开成功"是不够的，必须量实际电平。

用法：
    1. 先让某个设备真的在放声音（播个视频/音乐，别停）
    2. python tools\\loopback_probe.py [每个设备测几秒]
    3. 看哪个设备 peak 明显大于 0 —— 就用那个（配置里 device=名字子串）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from livesub import audio as audio_mod  # noqa: E402
from livesub.wasapi import list_render_endpoints_default_first  # noqa: E402


def probe_device(name: str, seconds: float) -> dict:
    """在指定设备上开回环，返回电平统计。"""
    from livesub.config import load_config

    c = load_config()
    src = None
    try:
        src = audio_mod.WasapiSource(
            sample_rate=c.audio.sample_rate,
            chunk_ms=c.audio.chunk_ms,
            device=name,
            vad_threshold=c.audio.vad_threshold,
        )
        src.open()
        peak = 0.0
        total = 0
        voiced = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            chunk = src.read(0.25)
            if chunk is None:
                continue
            total += 1
            x = np.frombuffer(chunk.pcm, dtype=np.int16).astype(np.float32) / 32768.0
            if x.size:
                p = float(np.abs(x).max())
                rms = float(np.sqrt((x ** 2).mean()))
                peak = max(peak, p)
                if rms > 0.005:
                    voiced += 1
        return {
            "ok": True,
            "peak": peak,
            "chunks": total,
            "voiced": voiced,
            "desc": getattr(src, "description", ""),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            if src is not None:
                src.close()
        except Exception:
            pass


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0

    print("=" * 74)
    print("WASAPI 回环逐设备探测")
    print("=" * 74)
    eps = list_render_endpoints_default_first()
    if not eps:
        print("找不到任何活动播放设备")
        return 1

    print(f"发现 {len(eps)} 个活动播放设备：\n")
    for i, ep in enumerate(eps):
        mark = "★默认" if ep.is_default else "     "
        print(f"  [{i}] {mark} {ep.name}")

    print(
        f"\n开始逐个测 {seconds:.0f} 秒。\n"
        ">>> 请**现在就让声音放起来**（视频/音乐都行），并且别停 <<<\n"
    )

    results = []
    for i, ep in enumerate(eps):
        print(f"[{i}] 测 {ep.name} … ", end="", flush=True)
        r = probe_device(ep.name, seconds)
        r["name"] = ep.name
        r["is_default"] = ep.is_default
        results.append(r)
        if r["ok"]:
            verdict = "✅ 有声音" if r["peak"] > 0.01 else "❌ 全静音"
            print(f"{verdict}  peak={r['peak']:.4f}  "
                  f"有声分块={r['voiced']}/{r['chunks']}")
        else:
            print(f"打开失败: {r['error']}")

    print("\n" + "=" * 74)
    print("结论")
    print("=" * 74)
    good = [r for r in results if r.get("ok") and r["peak"] > 0.01]
    silent = [r for r in results if r.get("ok") and r["peak"] <= 0.01]
    failed = [r for r in results if not r.get("ok")]

    if good:
        print("以下设备**真的能采到声音**，按 peak 从高到低：")
        for r in sorted(good, key=lambda x: -x["peak"]):
            tag = "（默认设备）" if r["is_default"] else ""
            print(f"  ✅ {r['name']}  peak={r['peak']:.4f} {tag}")
        print(
            "\n推荐做法：在 config.toml 里把 device 设成能采到声音的那个（名字子串即可）\n"
            f'  [audio]\n  device = "{good[0]["name"][:30]}"'
        )
    else:
        print("⚠ 没有任何设备采到声音。请确认：")
        print("  1. 测试期间确实有声音在播放（不是静音、不是暂停）")
        print("  2. 系统音量不为 0")
        print("  3. 播放的内容没有启用独占模式（有些播放器/游戏会独占音频设备）")

    if silent:
        print("\n以下设备打开成功但全是静音（这就是「插耳机就没声音」的典型表现）：")
        for r in silent:
            tag = "（默认设备）" if r["is_default"] else ""
            print(f"  ❌ {r['name']} {tag}")

    if failed:
        print("\n以下设备打开失败：")
        for r in failed:
            print(f"  ⚠ {r['name']}: {r['error']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
