"""用 Edge TTS（微软在线语音）合成日语音频样本，用于验证日语直播场景。

为什么用它：本机只装了中文和英文语音包，没有日语，而
`System.Speech` 无法凭空生成日语。Edge TTS 的日语语音（Nanami / Keita）
质量接近真人，且支持调整语速，很适合构造测试样本。

用法:
    python tools/make_ja_sample.py                  # 合成默认日语句子
    python tools/make_ja_sample.py "好きな文章"      # 自定义文本
    python tools/make_ja_sample.py --voice ja-JP-KeitaNeural
"""

from __future__ import annotations

import asyncio
import io
import re
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEXT = (
    "はい、どうもこんにちは！今日はみなさんに新しいゲームを紹介したいと思います。"
    "このゲームはオープンワールドで、グラフィックが本当にすごいんですよ。"
    "とりあえず最初の三十分だけ遊んでみますね。"
    "どこから見てるか、チャットで教えてください。"
)


async def synth(text: str, voice: str, out: Path, rate: str = "+0%") -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate)
    mp3 = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3.extend(chunk["data"])
    if not mp3:
        raise RuntimeError("Edge TTS 没有返回音频数据")

    # edge-tts 输出 mp3，这里转成 16kHz 单声道 wav（识别引擎要的格式）
    from faster_whisper.audio import decode_audio  # 复用其内置的 ffmpeg 解码

    pcm = decode_audio(io.BytesIO(bytes(mp3)), sampling_rate=16000)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes((pcm * 32767).astype("int16").tobytes())
    print(f"saved: {out}  ({pcm.size / 16000:.1f}s, voice={voice})")


def main() -> int:
    args = [a for a in sys.argv[1:]]
    voice = "ja-JP-NanamiNeural"
    if "--voice" in args:
        i = args.index("--voice")
        voice = args[i + 1]
        del args[i : i + 2]
    text = " ".join(args) if args else DEFAULT_TEXT
    text = re.sub(r"\s+", "", text)  # 日语不需要空格

    out = ROOT / "samples" / "ja_test.wav"
    try:
        asyncio.run(synth(text, voice, out))
    except ImportError:
        print("需要 edge-tts 与 faster-whisper：")
        print("  python -m pip install edge-tts -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 1
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
