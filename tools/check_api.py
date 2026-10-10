"""连通性自检：逐个验证识别/翻译引擎的 Key 与真实调用是否可用。

只打印状态与耗时，**不回显任何 Key**。

用法:
    python tools/check_api.py            # 检查当前配置里用到的引擎
    python tools/check_api.py --asr mock # 只检查翻译
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from livesub.config import Config, describe, load_config  # noqa: E402

# 一句真实的直播口播，用来同时看翻译质量
SAMPLE = "Hey chat, welcome back! Today we're testing the new RTX 5090, and I'll show you the thermals first."


def mask(key: str) -> str:
    """只显示长度与前 6 位，避免泄露。"""
    if not key:
        return "(未设置)"
    return f"{key[:6]}…(len={len(key)})"


def check_translate(cfg: Config) -> bool:
    tgt = cfg.translate.target
    print(f"\n[翻译] provider={tgt.provider} model={tgt.model}")
    print(f"       base_url={tgt.base_url}  key={mask(tgt.api_key)}")
    print(f"       降级链: {' → '.join(cfg.translate_chain())}")
    if not tgt.api_key:
        print("       [跳过] 没有 Key")
        return False

    from livesub.translate import create_translator

    translator = create_translator(cfg)
    import asyncio

    async def go():
        # 第一次带上文，第二次不带，看看效果差异
        t0 = time.perf_counter()
        r1 = await translator.translate(SAMPLE, [])
        t1 = time.perf_counter() - t0
        context = [(SAMPLE, r1.text)]
        t0 = time.perf_counter()
        r2 = await translator.translate(
            "And yeah, the fans barely spin up at idle, which is honestly surprising.",
            context,
        )
        t2 = time.perf_counter() - t0
        return r1, t1, r2, t2

    try:
        r1, t1, r2, t2 = asyncio.run(go())
    except Exception as exc:
        print(f"       [FAIL] 调用异常: {type(exc).__name__}: {exc}")
        return False

    ok = True
    for label, res, dt in (("第1句", r1, t1), ("第2句(带上文)", r2, t2)):
        if res.ok:
            print(f"       [OK] {label} {dt*1000:.0f}ms")
            print(f"            原文: {SAMPLE if label.startswith('第1') else 'And yeah, the fans barely spin up at idle, which is honestly surprising.'}")
            print(f"            译文: {res.text}")
        else:
            ok = False
            print(f"       [FAIL] {label} {dt*1000:.0f}ms -> {res.error}")
    return ok


def check_asr(cfg: Config) -> bool:
    provider = (cfg.asr.provider or "").lower()
    key = cfg.asr_api_key
    print(f"\n[识别] provider={provider} key={mask(key)}")
    if provider == "mock":
        print("       [跳过] mock 不需要 Key")
        return True
    if not key:
        print("       [跳过] 没有 Key")
        return False

    if provider == "deepgram":
        # 用一次极短的 REST 转写验证 Key 是否有效（比开 WebSocket 更省事）
        try:
            t0 = time.perf_counter()
            resp = requests.post(
                "https://api.deepgram.com/v1/listen?model=nova-2&language=en",
                headers={
                    "Authorization": f"Token {key}",
                    "Content-Type": "audio/wav",
                },
                # 0.5 秒的 16bit 静音 WAV，只为验证鉴权与配额
                data=_silent_wav(0.5),
                timeout=20,
            )
            dt = time.perf_counter() - t0
        except Exception as exc:
            print(f"       [FAIL] 网络异常: {type(exc).__name__}: {exc}")
            return False
        if resp.status_code == 200:
            print(f"       [OK] 鉴权通过，REST 往返 {dt*1000:.0f}ms")
            return True
        print(f"       [FAIL] HTTP {resp.status_code}: {resp.text[:200]}")
        return False

    if provider in ("openai", "openai_realtime"):
        try:
            resp = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=20,
            )
        except Exception as exc:
            print(f"       [FAIL] 网络异常: {type(exc).__name__}: {exc}")
            return False
        if resp.status_code == 200:
            print("       [OK] 鉴权通过")
            return True
        print(f"       [FAIL] HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    return False


def _silent_wav(seconds: float) -> bytes:
    import io
    import struct
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


def main() -> int:
    cfg = load_config()
    print("=" * 60)
    print("API 连通性自检（不会显示你的 Key）")
    print("=" * 60)
    print(describe(cfg))

    asr_ok = check_asr(cfg)
    tr_ok = check_translate(cfg)

    print("\n" + "=" * 60)
    print(f"识别: {'可用' if asr_ok else '不可用/未配置'}    翻译: {'可用' if tr_ok else '不可用/未配置'}")
    if tr_ok and not asr_ok:
        print("提示: 翻译已就绪，可以先用 `--asr mock` 验证译文链路：")
        print("      python main.py --asr mock")
    print("=" * 60)
    return 0 if (asr_ok or tr_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
