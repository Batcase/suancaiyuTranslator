"""系统音频采集：统一接口 + 可插拔后端。

后端
----
* ``wasapi``      —— Windows Core Audio 回环（ctypes，推荐，本机已验证）
* ``sounddevice`` —— PortAudio，仅在其构建包含 WASAPI loopback 时可用
* ``wav``         —— 回放 WAV 文件，用于无直播时验证整条链路
* ``tone``        —— 正弦音，仅验证管线连通性

统一约定：对外只吐 **16000Hz / 单声道 / int16** 的 `AudioChunk`，
云端 ASR 直接吃这个格式，本地不做编码。

采集线程只做"拷贝 + 算 RMS + 入队"，队列满时丢**最旧**的包，
保证延迟不随时间累积——直播字幕宁可贵一点实时性，也不能越拖越久。
"""

from __future__ import annotations

import queue
import sys
import time
import wave
from dataclasses import dataclass
from typing import Iterator, Protocol

import numpy as np

from . import wasapi

IS_WINDOWS = sys.platform == "win32"

sounddevice = None  # 惰性导入，避免没有声卡时整个包都用不了
_sd_error: Exception | None = None


def _load_sounddevice():
    global sounddevice, _sd_error
    if sounddevice is not None or _sd_error is not None:
        return sounddevice
    try:
        import sounddevice as _sd  # type: ignore

        sounddevice = _sd
    except Exception as exc:  # pragma: no cover
        _sd_error = exc
    return sounddevice


def sd_supports_loopback() -> bool:
    """判断 sounddevice 自带的 PortAudio 是否支持 WASAPI 回环。"""
    sd = _load_sounddevice()
    if sd is None:
        return False
    try:
        import inspect

        sig = inspect.signature(sd.WasapiSettings.__init__)
        return "loopback" in sig.parameters
    except Exception:
        return False


@dataclass
class AudioChunk:
    """一包待送往云端的音频。"""

    pcm: bytes
    rms: float
    captured_at: float  # time.monotonic()，用于测量真实端到端延迟
    silent: bool = False
    seq: int = 0

    @property
    def samples(self) -> int:
        return len(self.pcm) // 2


def rms_int16(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    f = x.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(f * f)))


def resample_linear(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """线性插值重采样。人声识别不需要更高阶的滤波器，够用且极快。"""
    if src_rate == dst_rate or x.size == 0:
        return x
    n_dst = int(round(x.size * dst_rate / src_rate))
    if n_dst <= 0:
        return np.zeros(0, dtype=x.dtype)
    src_idx = np.linspace(0.0, x.size - 1, n_dst, dtype=np.float64)
    left = np.floor(src_idx).astype(np.int64)
    right = np.minimum(left + 1, x.size - 1)
    frac = (src_idx - left).astype(np.float32)
    if np.issubdtype(x.dtype, np.integer):
        out = x[left].astype(np.float32) * (1.0 - frac) + x[right].astype(np.float32) * frac
        return out.astype(x.dtype)
    out = x[left] * (1.0 - frac) + x[right] * frac
    return out.astype(x.dtype)


class AudioSource(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def read(self, timeout: float = 0.5) -> AudioChunk | None: ...
    @property
    def description(self) -> str: ...


# ----------------------------------------------------------------------
# 后端 1：Windows Core Audio 回环（ctypes）
# ----------------------------------------------------------------------
class WasapiSource:
    """把 WasapiLoopback 的原生块切成固定 chunk_ms 的包。

    时间戳的准确性很关键：本类采集到的包，`captured_at` 不是"读到的时刻"，
    而是根据**设备已播放的采样数**反推出来的"这段声音真正被播放的时刻"。
    这样才能得到有意义的端到端延迟（否则会把缓冲区里的历史音频算成刚刚发生）。
    """

    def __init__(self, sample_rate: int, chunk_ms: int, device: str = "", vad_threshold: float = 0.006):
        self.sample_rate = int(sample_rate)
        self.chunk_samples = max(1, round(self.sample_rate * chunk_ms / 1000))
        self.vad_threshold = float(vad_threshold)
        self._cap: wasapi.WasapiLoopback | None = None
        self._buf = np.zeros(0, dtype=np.int16)
        self._seq = 0
        self._device_hint = device
        self.dropped = 0
        # 原生采样计数与它对应的"时钟锚点"
        self._native_total = 0
        self._anchor_wall = time.monotonic()
        self._anchor_count = 0

    def open(self) -> None:
        self._cap = wasapi.WasapiLoopback(sample_rate=self.sample_rate, device_hint=self._device_hint)
        self._cap.open()
        self._buf = np.zeros(0, dtype=np.int16)
        self._native_total = 0
        self._anchor_count = 0
        self._anchor_wall = time.monotonic()
        print(f"[audio] WASAPI 回环: {self._cap.format_desc}")

    def close(self) -> None:
        if self._cap is not None:
            self._cap.close()
            self._cap = None

    @property
    def description(self) -> str:
        if self._cap is None:
            return "(未打开)"
        return f"{self._cap.format_desc} [{self._cap.mode_desc}]"

    def _time_of(self, native_index: int) -> float:
        """native_index（按 16k 输出计的累计采样位置）对应的时间点。"""
        return self._anchor_wall + (native_index - self._anchor_count) / self.sample_rate

    def read(self, timeout: float = 0.5) -> AudioChunk | None:
        if self._cap is None:
            raise RuntimeError("采集未打开")
        deadline = time.monotonic() + timeout
        while self._buf.size < self.chunk_samples:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            native = self._cap.read(timeout=min(remain, 0.2))
            if native is None:
                return None
            if native.size:
                self._buf = native if self._buf.size == 0 else np.concatenate([self._buf, native])

        piece = self._buf[: self.chunk_samples]
        self._buf = self._buf[self.chunk_samples :]
        self._native_total += piece.size

        # 每处理 1 秒音频重置一次锚点，避免长时间运行后时钟漂移累积
        if self._native_total - self._anchor_count >= self.sample_rate:
            self._anchor_wall = time.monotonic()
            self._anchor_count = self._native_total

        # 减去一个输出包自身的时长：这个包是"刚播完"的那段
        captured_at = self._time_of(self._native_total) - self.chunk_samples / self.sample_rate

        level = rms_int16(piece)
        self._seq += 1
        return AudioChunk(
            pcm=piece.tobytes(),
            rms=level,
            captured_at=captured_at,
            silent=level < self.vad_threshold,
            seq=self._seq,
        )


# ----------------------------------------------------------------------
# 后端 2：sounddevice / PortAudio（需要 loopback 支持）
# ----------------------------------------------------------------------
def list_sounddevices() -> list[dict]:
    sd = _load_sounddevice()
    if sd is None:
        return []
    rows = []
    for idx, dev in enumerate(sd.query_devices()):
        api = sd.query_hostapis(dev["hostapi"])["name"]
        rows.append(
            {
                "index": idx,
                "api": api,
                "name": dev["name"],
                "in": dev["max_input_channels"],
                "out": dev["max_output_channels"],
                "loopback": "loopback" in dev["name"].lower(),
            }
        )
    return rows


class SoundDeviceSource:
    def __init__(self, sample_rate: int, chunk_ms: int, device: str = "", vad_threshold: float = 0.006):
        sd = _load_sounddevice()
        if sd is None:
            raise RuntimeError(f"sounddevice 不可用: {_sd_error}")
        self.sd = sd
        self.sample_rate = int(sample_rate)
        self.chunk_samples = max(1, round(self.sample_rate * chunk_ms / 1000))
        self.vad_threshold = float(vad_threshold)
        self.device_hint = device
        self._queue: queue.Queue[AudioChunk] = queue.Queue(maxsize=256)
        self._stream = None
        self._seq = 0
        self._native_rate = self.sample_rate
        self.dropped = 0

    def _find_device(self) -> tuple[int, str]:
        sd = self.sd
        cand: list[tuple[int, str]] = []
        for row in list_sounddevices():
            if row["in"] <= 0 or "WASAPI" not in row["api"]:
                continue
            cand.append((row["index"], row["name"]))
        if not cand:
            raise RuntimeError("没有可用的 WASAPI 输入设备；建议改用 --backend wasapi")
        if self.device_hint:
            hint = self.device_hint.lower()
            for idx, name in cand:
                if hint in name.lower():
                    return idx, name
        loop = [c for c in cand if "loopback" in c[1].lower()]
        if not loop:
            raise RuntimeError(
                "sounddevice 的 PortAudio 构建不支持 WASAPI 回环，请改用 --backend wasapi"
            )
        return loop[0]

    def open(self) -> None:
        sd = self.sd
        idx, name = self._find_device()
        self._native_rate = int(sd.query_devices(idx)["default_samplerate"]) or self.sample_rate
        extra = None
        try:
            extra = sd.WasapiSettings(loopback=True)
        except Exception:
            extra = None
        self._stream = sd.InputStream(
            device=idx,
            channels=1,
            samplerate=self._native_rate,
            dtype="int16",
            blocksize=0,
            latency="low",
            callback=self._callback,
            extra_settings=extra,
        )
        self._stream.start()
        print(f"[audio] sounddevice 设备 {idx}: {name} @ {self._native_rate}Hz")

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        mono = np.asarray(indata, dtype=np.int16).reshape(-1)
        if self._native_rate != self.sample_rate:
            mono = resample_linear(mono, self._native_rate, self.sample_rate)
        n = self.chunk_samples
        level = rms_int16(mono)
        for start in range(0, mono.size - n + 1, n):
            piece = mono[start : start + n]
            self._seq += 1
            chunk = AudioChunk(
                pcm=piece.tobytes(),
                rms=level,
                captured_at=time.monotonic(),
                silent=level < self.vad_threshold,
                seq=self._seq,
            )
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None

    @property
    def description(self) -> str:
        return f"sounddevice {self._native_rate}Hz -> {self.sample_rate}Hz mono"

    def read(self, timeout: float = 0.5) -> AudioChunk | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


# ----------------------------------------------------------------------
# 后端 3/4：WAV 回放 与 正弦音
# ----------------------------------------------------------------------
class WavSource:
    def __init__(self, path: str, sample_rate: int = 16000, chunk_ms: int = 32, realtime: bool = True):
        self.path = path
        self.sample_rate = int(sample_rate)
        self.chunk_samples = max(1, round(self.sample_rate * chunk_ms / 1000))
        self.realtime = realtime
        self._pcm: np.ndarray | None = None
        self._pos = 0
        self._seq = 0
        self._t0 = 0.0

    def open(self) -> None:
        with wave.open(self.path, "rb") as wf:
            ch, width, rate, n = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
            raw = wf.readframes(n)
        if width != 2:
            raise RuntimeError(f"仅支持 16bit PCM wav，当前 {width*8}bit")
        data = np.frombuffer(raw, dtype=np.int16)
        if ch > 1:
            data = data.reshape(-1, ch).mean(axis=1).astype(np.int16)
        if rate != self.sample_rate:
            data = resample_linear(data, rate, self.sample_rate)
        self._pcm = data
        self._pos = 0
        self._seq = 0
        self._t0 = time.monotonic()
        print(f"[audio] 回放 {self.path}: {data.size/self.sample_rate:.1f}s @ {self.sample_rate}Hz")

    def close(self) -> None:
        self._pcm = None

    @property
    def description(self) -> str:
        return f"wav replay {self.path}"

    @property
    def finished(self) -> bool:
        return self._pcm is None or self._pos >= self._pcm.size

    def read(self, timeout: float = 0.5) -> AudioChunk | None:
        if self._pcm is None or self._pos >= self._pcm.size:
            return None
        if self.realtime:
            target = self._t0 + self._pos / self.sample_rate
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(min(delay, 0.5))
        piece = self._pcm[self._pos : self._pos + self.chunk_samples]
        self._pos += self.chunk_samples
        self._seq += 1
        level = rms_int16(piece)
        return AudioChunk(
            pcm=piece.tobytes(),
            rms=level,
            captured_at=time.monotonic(),
            silent=level < 0.006,
            seq=self._seq,
        )


class ToneSource:
    def __init__(self, sample_rate: int = 16000, chunk_ms: int = 32, freq: float = 440.0):
        self.sample_rate = sample_rate
        self.chunk_samples = max(1, round(sample_rate * chunk_ms / 1000))
        self.freq = freq
        self._phase = 0.0
        self._seq = 0

    def open(self) -> None: ...
    def close(self) -> None: ...

    @property
    def description(self) -> str:
        return f"tone {self.freq}Hz"

    def read(self, timeout: float = 0.5) -> AudioChunk | None:
        t = np.arange(self.chunk_samples, dtype=np.float32) / self.sample_rate
        w = 0.2 * np.sin(2 * np.pi * self.freq * t + self._phase)
        self._phase = (self._phase + 2 * np.pi * self.freq * self.chunk_samples / self.sample_rate) % (
            2 * np.pi
        )
        pcm = (w * 32767).astype(np.int16)
        self._seq += 1
        return AudioChunk(
            pcm=pcm.tobytes(), rms=rms_int16(pcm), captured_at=time.monotonic(), silent=False, seq=self._seq
        )


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------
def create_source(cfg, backend: str = "auto", replay_wav: str = "") -> AudioSource:
    """按配置创建采集源。backend: auto|wasapi|sounddevice|wav|tone"""
    sr, ms = cfg.audio.sample_rate, cfg.audio.chunk_ms
    vad = cfg.audio.vad_threshold

    if replay_wav:
        return WavSource(replay_wav, sample_rate=sr, chunk_ms=ms)
    if backend == "tone":
        return ToneSource(sample_rate=sr, chunk_ms=ms)
    if backend == "wav":
        if not replay_wav:
            raise SystemExit("--backend wav 需要同时给出 --wav <文件>")
        return WavSource(replay_wav, sample_rate=sr, chunk_ms=ms)
    if backend == "sounddevice":
        return SoundDeviceSource(sr, ms, device=cfg.audio.device, vad_threshold=vad)
    if backend == "wasapi":
        return WasapiSource(sr, ms, device=cfg.audio.device, vad_threshold=vad)

    # auto：优先原生 WASAPI 回环
    if IS_WINDOWS:
        return WasapiSource(sr, ms, device=cfg.audio.device, vad_threshold=vad)
    if sd_supports_loopback():
        return SoundDeviceSource(sr, ms, device=cfg.audio.device, vad_threshold=vad)
    raise SystemExit(
        "当前平台没有可用的系统内录后端。\n"
        "  Windows : 直接用（内置 WASAPI 回环）\n"
        "  其他平台: 需要 PortAudio + loopback，或用 --wav 回放测试"
    )


def probe_backends() -> None:
    """打印本机可用的后端，便于排错。"""
    print("=" * 60)
    print("采集后端自检")
    print("-" * 60)
    if IS_WINDOWS:
        try:
            eps = wasapi.list_render_endpoints()
            print(f"[OK]   WASAPI 回环可用，发现 {len(eps)} 个活动播放设备")
            for ep in eps:
                print(f"         {'*' if ep.is_default else ' '} {ep.name}")
        except Exception as exc:
            print(f"[FAIL] WASAPI 回环不可用: {exc}")
    else:
        print("[--]   非 Windows，跳过 WASAPI")

    sd = _load_sounddevice()
    if sd is None:
        print(f"[--]   sounddevice 未安装/不可用: {_sd_error}")
    else:
        print(f"[OK]   sounddevice {sd.__version__}，loopback 支持: {sd_supports_loopback()}")
    print("=" * 60)


if __name__ == "__main__":
    probe_backends()
