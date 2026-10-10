"""延迟统计：把"从声音播放到字幕出现"的真实耗时量化出来。

这是整个项目最该盯的数字。真实端到端延迟 = 
    音频采集时刻 → ASR final/翻译返回时刻
用 `time.monotonic()` 在采集线程打时间戳，一路带到字幕线程，避免用
"某段代码执行了多久"糊弄自己。
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def _clamp_age(ms: float) -> float:
    """把"音频→结果"的年龄里的负值归零。

    为什么会有负值：本地记录的是"已发送"的音频位置，而云端返回结果时它的
    服务端游标已经**领先**了这个位置（在途音频已经收到并处理完）。所以用本地
    位置去减，会算出 -30~-50ms 这种数——它的真实含义是"结果几乎与音频同步
    产生，延迟只在网络往返量级"（100~300ms），而不是"提前于音频"。

    归零后：接近 0 表示"受网络往返限制，已经是流式识别的物理下限"。
    """
    return max(0.0, ms)


@dataclass
class LatencyStats:
    """累计各类延迟样本（单位 ms）。"""

    partial_ms: list[float] = field(default_factory=list)      # 音频→首个中间识别结果
    final_ms: list[float] = field(default_factory=list)        # 音频→该句最终识别
    translate_ms: list[float] = field(default_factory=list)    # 翻译请求耗时
    e2e_ms: list[float] = field(default_factory=list)          # 音频→翻译出字（真正的端到端）
    interim_events: int = 0
    final_events: int = 0
    translate_calls: int = 0
    translate_errors: int = 0
    audio_seconds: float = 0.0
    started_at: float = field(default_factory=time.monotonic)

    # ---- 记录 ----
    def on_partial(self, captured_at: float) -> None:
        self.interim_events += 1
        self.partial_ms.append(_clamp_age((time.monotonic() - captured_at) * 1000))

    def on_final(self, captured_at: float) -> None:
        self.final_events += 1
        self.final_ms.append(_clamp_age((time.monotonic() - captured_at) * 1000))

    def on_translate(self, captured_at: float, elapsed_s: float, ok: bool) -> None:
        self.translate_calls += 1
        self.translate_ms.append(elapsed_s * 1000)
        if ok:
            self.e2e_ms.append((time.monotonic() - captured_at) * 1000)
        else:
            self.translate_errors += 1

    # ---- 输出 ----
    def summary(self) -> dict[str, float]:
        return {
            "首字延迟(中位)": pct(self.partial_ms, 50),
            "首字延迟(p90)": pct(self.partial_ms, 90),
            "识别定稿(中位)": pct(self.final_ms, 50),
            "翻译耗时(中位)": pct(self.translate_ms, 50),
            "端到端(中位)": pct(self.e2e_ms, 50),
            "端到端(p90)": pct(self.e2e_ms, 90),
        }

    def report(self) -> str:
        lines = ["=" * 56, "延迟统计（单位 ms）", "-" * 56]
        summary = self.summary()
        for key, val in summary.items():
            lines.append(f"  {key:<16}: {val:8.0f}")
        lines.append("-" * 56)
        lines.append(
            f"  样本: 中间结果 {self.interim_events} / 定稿 {self.final_events} / "
            f"翻译 {self.translate_calls}(失败 {self.translate_errors})"
        )
        if self.translate_ms:
            lines.append(
                f"  翻译 min/max: {min(self.translate_ms):.0f} / {max(self.translate_ms):.0f}"
            )
        up = time.monotonic() - self.started_at
        if up > 0:
            lines.append(f"  运行时长: {up:.0f}s   音频: {self.audio_seconds:.0f}s")
        # 云端流式识别的"首字/定稿延迟"会被网络往返压到 0 附近，这里说明一下，
        # 免得看到 0 以为是测量坏了
        if self.partial_ms and pct(self.partial_ms, 50) < 80:
            lines.append("  注: 首字延迟接近 0 = 识别已到网络往返的物理下限，看'端到端'更实际")
        lines.append("=" * 56)
        return "\n".join(lines)


class Stopwatch:
    """with 用法测量一段耗时。"""

    def __enter__(self) -> "Stopwatch":
        self._t0 = time.perf_counter()
        self.elapsed = 0.0
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed = time.perf_counter() - self._t0
