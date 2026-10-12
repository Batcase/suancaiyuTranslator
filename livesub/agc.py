"""流式自动增益（AGC）。

为什么需要
----------
实测：某场 B 站日语直播回放的音轨 RMS 只有 0.0120，而正常测试样本是 0.1393
——**低了 11.6 倍**。后果是灾难性的：

* 识别侧静音判定把它当成"没在说话"，一句话都不出
* 服务端 VAD 也判成无语音，返回 0 个结果
* 同一段音频手动增益 4.3 倍后，识别立刻恢复正常

而用户没法自己调这个——不同的直播、不同的播放器音量，电平能差十几倍。
所以在送进识别之前统一做一次归一化。

做法与取舍
----------
* 用**滑动峰值**而不是均值来定增益：均值会被长时间的背景音乐拉高，
  导致有人说话时反而不增益。峰值对"有没有声音"更敏感。
* 增益**只升不降得太快**（快攻慢放）：突然降低增益会让正在说的话变小。
* 上限 [max_gain]：静音时不能无脑放大，否则噪声底噪被抬起，
  服务端 VAD 会一直以为有人在说话。
* 只在**有实际信号**时调整（低于 [noise_floor] 视为静音，不更新峰值）。

没有任何外部依赖，纯 numpy。
"""

from __future__ import annotations

import numpy as np

#: 目标峰值。0.35 ≈ -9 dBFS，留足余量避免削波
TARGET_PEAK = 0.35
#: 噪声底：低于这个峰值视为静音，不参与增益计算
NOISE_FLOOR = 0.02


class AutoGain:
    """流式自动增益。对 int16 的 PCM 字节操作。"""

    def __init__(
        self,
        target_peak: float = TARGET_PEAK,
        max_gain: float = 12.0,
        min_gain: float = 1.0,
        attack: float = 0.25,
        release: float = 0.02,
    ) -> None:
        self.target_peak = target_peak
        self.max_gain = max_gain
        self.min_gain = min_gain
        #: 增益变大时的平滑系数（快）
        self.attack = attack
        #: 增益变小时的平滑系数（慢）
        self.release = release
        self._gain = 1.0
        self._peak = 0.0
        self._frames = 0
        self._boosted_frames = 0

    # ------------------------------------------------------------------
    @property
    def gain(self) -> float:
        """当前增益倍数（便于日志/界面展示）。"""
        return self._gain

    @property
    def stats(self) -> dict:
        return {
            "gain": round(self._gain, 2),
            "peak": round(self._peak, 4),
            "frames": self._frames,
            "boosted": self._boosted_frames,
        }

    # ------------------------------------------------------------------
    def process(self, pcm: bytes) -> bytes:
        """输入 int16 PCM 字节，返回增益后的字节。

        音频很短或非 16bit 对齐时原样返回，绝不因为增益把数据搞坏。
        """
        if len(pcm) < 4 or len(pcm) % 2 != 0:
            return pcm
        samples = np.frombuffer(pcm, dtype=np.int16)
        x = samples.astype(np.float32) / 32768.0

        peak = float(np.abs(x).max())
        self._frames += 1

        if peak >= NOISE_FLOOR:
            # 滑动峰值：上升快（立刻响应新出现的响亮片段），下降慢
            if peak > self._peak:
                self._peak = 0.7 * self._peak + 0.3 * peak
            else:
                self._peak = 0.995 * self._peak + 0.005 * peak

            desired = self.target_peak / max(self._peak, 1e-6)
            desired = float(np.clip(desired, self.min_gain, self.max_gain))
            # 增益变化平滑：要变大用 attack，要变小用 release
            coef = self.attack if desired > self._gain else self.release
            self._gain += (desired - self._gain) * coef
            self._gain = float(np.clip(self._gain, self.min_gain, self.max_gain))
            if self._gain > 1.05:
                self._boosted_frames += 1

        if abs(self._gain - 1.0) < 0.02:
            return pcm   # 增益可忽略，不做无谓的运算与精度损失

        # 乘完增益再硬限幅：增益上升期可能略微过冲，宁可削掉一点点尖峰，
        # 也不能让 int16 溢出（溢出会wrap成反相，听感上是很响的爆音，识别直接崩）
        out = np.clip(x * self._gain, -1.0, 1.0) * 32767.0
        return out.astype(np.int16).tobytes()
