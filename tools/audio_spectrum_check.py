"""判断一段音频里到底有没有人声（频谱特征）。

为什么需要：
  1. RMS/能量看不出问题（实测 RMS=0.064 看着正常），但 ASR 一条都识别不出来
  2. 人声与纯音乐在频谱上有明显区别：
     - 人声能量集中在 100~4000Hz，且有明显的音节起伏（每秒 2~5 个音节）
     - 纯音乐往往有持续的低频和稳定的高频
  3. 还可能是"下载下来的是静音轨/纯 BGM 轨"

输出：分段频谱质心、语音频带能量占比、以及音节速率的估计。
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

SR = 16000


def band_energy(spec: np.ndarray, freqs: np.ndarray, lo: float, hi: float) -> float:
    m = (freqs >= lo) & (freqs < hi)
    return float(spec[m].mean())


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    start_s = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    dur_s = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0

    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        w.setpos(int(sr * start_s))
        raw = w.readframes(int(sr * dur_s))
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if a.size == 0:
        print("读不到数据（超出文件长度？）")
        return 1

    print(f"文件: {path.name}  分析区间 {start_s:.0f}s ~ {start_s+dur_s:.0f}s  ({a.size/sr:.1f}s)")
    print(f"整体: RMS={np.sqrt((a**2).mean()):.4f}  峰值={np.abs(a).max():.3f}  "
          f"直流偏置={a.mean():+.5f}")

    # --- 分段频谱 ---
    win = 1024
    hop = win // 2
    n_frames = max(1, (a.size - win) // hop)
    freqs = np.fft.rfftfreq(win, 1 / sr)
    hann = np.hanning(win)

    cents, speech_ratio = [], []
    for i in range(0, n_frames, max(1, n_frames // 40)):
        seg = a[i * hop : i * hop + win]
        if seg.size < win:
            break
        spec = np.abs(np.fft.rfft(seg * hann))
        tot = spec.sum() + 1e-9
        cents.append(float((freqs * spec).sum() / tot))
        sp = band_energy(spec, freqs, 200, 4000)
        speech_ratio.append(float(sp / (tot / len(freqs) + 1e-9)))

    cents = np.array(cents)
    print(f"\n频谱质心: 中位 {np.median(cents):7.0f} Hz  "
          f"范围 {cents.min():.0f}~{cents.max():.0f} Hz")
    print("  参考: 纯人声约 300~1200Hz；纯音乐常低于 300 或高于 2000")

    # --- 频带能量分布 ---
    spec = np.abs(np.fft.rfft(a[: win * 200] * np.hanning(win * 200)))[: win // 2 + 1]
    tot = spec.sum() + 1e-9
    print("\n频带能量占比:")
    for lo, hi, name in (
        (0, 100, "0-100Hz   低频轰鸣"),
        (100, 300, "100-300   男声基频/鼓"),
        (300, 1000, "300-1k    人声主要区"),
        (1000, 4000, "1k-4k     辅音/清晰度"),
        (4000, 8000, "4k-8k     齿音/镲片"),
    ):
        m = (freqs[: len(spec)] >= lo) & (freqs[: len(spec)] < hi)
        print(f"  {name:22s} {spec[m].sum()/tot*100:5.1f}%")

    # --- 音节速率：能量包络的起伏次数 ---
    env = np.abs(a)
    sm = np.convolve(env, np.ones(int(sr * 0.02)) / (sr * 0.02), mode="same")
    thr = sm.mean() * 0.7
    peaks = 0
    above = sm > thr
    for i in range(1, len(above)):
        if above[i] and not above[i - 1]:
            peaks += 1
    rate = peaks / (a.size / sr)
    print(f"\n包络起伏: {peaks} 次 / {a.size/sr:.1f}s = {rate:.1f} 次/秒")
    print("  参考: 正常说话 2~6 音节/秒；持续音乐通常 <1 或非常规律")

    verdict = []
    if np.median(cents) < 250:
        verdict.append("质心过低 → 更像低频音乐/轰鸣")
    if rate < 1.5:
        verdict.append("包络起伏太少 → 不像连续说话")
    if abs(a.mean()) > 0.01:
        verdict.append("有明显直流偏置 → 音频可能损坏")
    tail = "；".join(verdict) if verdict else "✅ 特征符合人声（有语音内容）"
    print("\n结论: " + tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
