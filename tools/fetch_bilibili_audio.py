"""从 B 站下载一个视频的**音频**，转成 16kHz 单声道 WAV（供测试用）。

为什么要自己写：本机没有 yt-dlp / ffmpeg，而 B 站的 API 是公开的，
拿音频流（durl/dash）不需要登录——只要能拿到 playurl 就行。

用法:
    python tools\\fetch_bilibili_audio.py BV12opD61EvU samples\\fast_ja.wav
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get(url: str, referer: str = "https://www.bilibili.com/") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    bvid = sys.argv[1]
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("samples") / f"{bvid}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] 查询视频信息 {bvid}")
    info = http_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    if info.get("code") != 0:
        print(f"  失败: {info.get('message')}")
        return 1
    data = info["data"]
    cid = data["cid"]
    duration = data.get("duration", 0)
    print(f"  标题: {data['title']}")
    print(f"  时长: {duration}s  cid={cid}")

    print("[2/4] 获取音频流地址")
    q = urllib.parse.urlencode({
        "bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1,
    })
    play = http_json(f"https://api.bilibili.com/x/player/playurl?{q}")
    if play.get("code") != 0:
        print(f"  失败: {play.get('message')}")
        return 1
    pdata = play["data"]

    # 优先 DASH 的纯音频流（体积最小），退回到 durl 的整段 mp4
    audio_url = None
    kind = ""
    dash = pdata.get("dash") or {}
    if dash.get("audio"):
        # 取**最高码率**：低码率流在高频上损失大，实测 90kbps 那档
        # 出来的频谱 56% 能量在 4k-8k，人声几乎没了（误当成音乐段）。
        audios = sorted(dash["audio"], key=lambda a: a.get("bandwidth", 0))
        pick = audios[-1]
        audio_url = pick["baseUrl"]
        kind = f"dash audio {pick.get('bandwidth', 0)//1000}kbps ({pick.get('codecs')})"
    elif pdata.get("durl"):
        audio_url = pdata["durl"][0]["url"]
        kind = "durl (含视频轨的 mp4)"
    if not audio_url:
        print("  没找到可用的音频流")
        return 1
    print(f"  选中的流: {kind}")

    print("[3/4] 下载")
    raw = out.with_suffix(out.suffix + ".part")
    blob = http_get(audio_url)
    raw.write_bytes(blob)
    print(f"  已下载 {len(blob)/1024/1024:.1f} MB -> {raw.name}")

    print("[4/4] 转成 16kHz 单声道 WAV")
    if convert_to_wav(raw, out):
        raw.unlink(missing_ok=True)
        print(f"  ✅ {out}  ({out.stat().st_size/1024/1024:.1f} MB)")
        return 0
    print(f"  ⚠ 没转成功，原始文件保留在 {raw}")
    return 1


def convert_to_wav(src: Path, dst: Path) -> bool:
    """把下载到的音频转成 16kHz 单声道 PCM16 WAV。

    优先用 ffmpeg；没有 ffmpeg 时，如果源本身就是 WAV 就直接用。
    （B 站给的是 m4a，所以基本都需要 ffmpeg 或其它解码器。）
    """
    ffmpeg = None
    for cand in ("ffmpeg", "ffmpeg.exe"):
        from shutil import which
        ffmpeg = which(cand)
        if ffmpeg:
            break
    if ffmpeg:
        cmd = [
            ffmpeg, "-y", "-i", str(src), "-vn",
            "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
            str(dst),
        ]
        print("  用 ffmpeg 转换…")
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0:
            return True
        print("  ffmpeg 失败:", r.stderr.decode("utf-8", "replace")[-300:])
        return False

    # 没有 ffmpeg：用 PyAV 解码 + 纯 numpy 重采样
    #
    # 为什么不直接用 av.AudioResampler：不同 PyAV 版本对 resample() 的入参要求不一致
    # （有的要单个 frame、有的要列表，混用会静默产出错位数据——实测把 3450s 的音频
    #  写成了 4204s，多 21.9%，导致 ASR 完全识别不出来）。自己算反而确定。
    try:
        import av  # type: ignore
    except ImportError:
        print("  没有 ffmpeg，也没有 PyAV —— 无法解码 m4a。")
        print("  解决：装一个 ffmpeg，或 pip install av")
        return False
    try:
        import wave

        import av  # type: ignore
        import numpy as np

        with av.open(str(src)) as container:
            stream = container.streams.audio[0]
            src_rate = stream.rate
            parts = []
            for frame in container.decode(stream):
                # to_ndarray 得到 (声道数, 样本数)；取均值转单声道
                arr = frame.to_ndarray()
                if arr.ndim == 2:
                    arr = arr.mean(axis=0)
                parts.append(arr.astype(np.float32))
            if not parts:
                print("  解码后没有音频数据")
                return False
            mono = np.concatenate(parts)

        # 归一化：av 给的整型样本需要按位深缩放
        if mono.dtype != np.float32 or np.abs(mono).max() > 1.5:
            bits = stream.format.bits if stream.format else 16
            mono = mono / float(1 << (bits - 1))

        out = resample_linear(mono, src_rate, 16000)

        pcm = np.clip(out * 32767.0, -32768, 32767).astype(np.int16)
        with wave.open(str(dst), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm.tobytes())

        # 自检：输出时长必须与源一致（差 1% 以内），否则说明转换逻辑又错了
        out_dur = len(pcm) / 16000
        src_dur = len(mono) / src_rate
        if abs(out_dur - src_dur) > max(2.0, src_dur * 0.01):
            print(f"  ⚠ 时长对不上：源 {src_dur:.1f}s → 输出 {out_dur:.1f}s")
            return False
        print(f"  源 {src_rate}Hz/{stream.channels}ch → 16000Hz/1ch，时长 {out_dur:.1f}s")
        return True
    except Exception as exc:
        print("  PyAV 转换失败:", exc)
        return False


def resample_linear(x: "np.ndarray", src_rate: int, dst_rate: int) -> "np.ndarray":
    """线性插值重采样，先做一次简单低通以避免降采样混叠。

    人声识别不需要高阶滤波器；但直接把 48k 降到 16k 而不滤波，
    会把 8k 以上的能量折叠回可听频段，反而降低识别率。
    """
    import numpy as np

    if src_rate == dst_rate:
        return x
    if dst_rate < src_rate:
        # 简单移动平均当低通：窗口 = 降采样倍数
        k = max(1, src_rate // dst_rate)
        if k > 1:
            kernel = np.ones(k, dtype=np.float32) / k
            x = np.convolve(x, kernel, mode="same")
    n_out = int(len(x) * dst_rate / src_rate)
    idx = np.linspace(0, len(x) - 1, n_out)
    return np.interp(idx, np.arange(len(x)), x).astype(np.float32)


if __name__ == "__main__":
    sys.exit(main())
