# 测试音频说明

这个目录里的 `*.wav` **不进版本库**（见根目录 `.gitignore` 的 `*.wav`）。

## 已有的样本

| 文件 | 内容 | 用途 |
|---|---|---|
| `ja_test.wav` | 合成的日语语音（21 秒，5 句），无背景音乐 | 端到端延迟测试的基准样本 |
| `en_test.wav` | 合成的英语语音 | 英语链路测试 |

这两个是干净的单人朗读，用来测**延迟**最合适（没有音乐干扰，识别稳定）。

重新生成日语样本：

```powershell
python tools\make_ja_sample.py
```

## 用真实直播录像测试（更接近实战）

合成样本测不出真实直播的问题（背景音乐、快语速、低电平）。
要复现这些，用 B 站的真实直播回放：

```powershell
# 下载音频并转成 16kHz 单声道 wav
python tools\fetch_bilibili_audio.py BV12opD61EvU samples\live.wav
```

> `fetch_bilibili_audio.py` 会自动取最高码率的音频流，并用 PyAV + numpy
> 重采样（不需要装 ffmpeg）。它自带**时长自检**：输出时长与源容器对不上
> 就报错 —— 这个检查是必要的，早期版本因为 `AudioResampler` 的调用方式
> 在不同 PyAV 版本里不一致，把 3450 秒的音频写成了 4204 秒（多 21.9%），
> 内容完全错位，导致识别一条都出不来。

## 实测踩过的坑（都是真实录像才暴露的）

| 现象 | 原因 |
|---|---|
| **一句话都识别不出来** | 该直播音轨 RMS 只有 0.0120（正常样本 0.1393，低 11.6 倍），识别侧与云端 VAD 都判成"没在说话"。已加自动增益（`livesub/agc.py`）解决 |
| 靠静音找不到断句 | 带 BGM 的直播音频动态范围被压得极平（P10~P90 仅 1dB），Silero VAD 甚至判定 0 句话。已加标点软断句 |
| 音频时长对不上 | 上面说的重采样 bug |

## 截取片段

整段 57 分钟的录像跑一遍很慢，测的时候通常截一小段：

```python
import wave
with wave.open("samples/live.wav", "rb") as r:
    sr = r.getframerate()
    r.setpos(sr * 900)              # 从第 15 分钟开始（避开开场）
    data = r.readframes(sr * 90)    # 取 90 秒
with wave.open("samples/live90.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes(data)
```

> 挑片段时注意：那段直播**全程有 BGM**，且不同时间点电平不同。
> 如果不确定哪段有人声，先用 `tools/audio_spectrum_check.py` 看频谱。
