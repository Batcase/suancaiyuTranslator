"""对比两种翻译时机的效果（用 mock 识别 + 计数翻译器，不打真实接口）。

指标：
  * 翻译请求次数（= token 成本）
  * 每次请求的字符数（越大越接近"整句"）
  * 译文片段数（越少越连贯）

用法: python tools\\compare_sentence_mode.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livesub.asr import AsrEvent  # noqa: E402
from livesub.state import SubtitleState  # noqa: E402

# 模拟一个"语速很快的主播"：ASR 以 350ms 一个中间结果的节奏往外吐，
# 每个结果比上一个长 6~12 个字符，然后给一个 sentence_end。
# 这是实测到的真实节奏（Deepgram 日语 + interim_results）。
SENTENCES = [
    "今日は皆さんに新しいゲームを紹介したいと思います",
    "このゲームはオープンワールドでグラフィックが本当にすごいんですよ",
    "とりあえず最初の30分だけ遊んでみますね",
]


def make_events() -> list[AsrEvent]:
    """把三句话切成"快速说话"节奏的中间结果流。"""
    events: list[AsrEvent] = []
    t0 = time.monotonic()
    clock = t0
    for sent in SENTENCES:
        # 每 7 个字符吐一个中间结果
        step = 7
        for i in range(step, len(sent), step):
            events.append(
                AsrEvent(
                    kind="partial",
                    text=sent[:i],
                    audio_captured_at=clock,
                )
            )
            clock += 0.35
        # 最后一次定稿（非句尾，模拟"这段音频定稿但句子可能没完"）
        events.append(
            AsrEvent(
                kind="final",
                text=sent,
                is_utterance_end=False,
                audio_captured_at=clock,
            )
        )
        clock += 0.2
        # 真正的句尾
        events.append(
            AsrEvent(
                kind="final",
                text=sent,
                is_utterance_end=True,
                audio_captured_at=clock,
            )
        )
        clock += 0.3
    return events


def run_with_mode(sentence_mode: bool) -> tuple[list[str], list[str], float]:
    import livesub.pipeline as pl

    # 打桩：不建真实翻译器
    orig_init = pl.TranslatorActor.__init__

    def fake_init(self, translator, state, stats, control):
        self.requests = []
        self.finals = []
        self._fake = True

    def fake_submit(self, text, context, final, captured_at, sid, source_text=""):
        self.requests.append(text)
        if final:
            self.finals.append(text)

    def fake_context(self):
        return []

    pl.TranslatorActor.__init__ = fake_init
    pl.TranslatorActor.submit = fake_submit
    pl.TranslatorActor.context = fake_context

    try:
        from livesub.config import load_config

        cfg = load_config()
        cfg.translate.translate_on_sentence_end = sentence_mode
        cfg.audio.backend = "tone"      # 不真的开设备
        cfg.overlay.enabled = False

        state = SubtitleState()
        from livesub.stats import LatencyStats

        stats = LatencyStats()
        control = pl.RunControl()

        class FakeSource:
            description = "fake"

            def open(self):
                pass

            def read(self, timeout):
                return None

            def close(self):
                pass

        pipe = pl.Pipeline.__new__(pl.Pipeline)
        # 手工初始化需要的字段（绕开 __init__ 里的音频设备/翻译器构造）
        pipe.cfg = cfg
        pipe.state = state
        pipe.stats = stats
        pipe.control = control
        pipe.source = FakeSource()
        pipe.translator = None
        pipe.actor = pl.TranslatorActor(None, state, stats, control)
        pipe._sid = 0
        pipe._cur_source = ""
        pipe._cur_captured_at = 0.0
        pipe._last_spec_text = ""
        pipe._last_spec_at = 0.0
        pipe._spec_interval = 0.35
        pipe._spec_min_growth = 6
        pipe._sample_rate = 16000
        pipe._sentence_started = 0.0
        pipe._pending = ""
        pipe._pending_since = 0.0
        pipe._pending_last = 0.0

        class NullLogger:
            def write(self, **kw):
                pass

        logger = NullLogger()
        events = make_events()
        t0 = time.monotonic()
        for ev in events:
            pipe._on_event(ev, logger)
        # 会话结束冲刷
        pipe._maybe_flush_pending(force=True)
        elapsed = time.monotonic() - t0
        return pipe.actor.requests, pipe.actor.finals, elapsed
    finally:
        pl.TranslatorActor.__init__ = orig_init
        # submit/context 是类属性，重设回原函数比较复杂，这里进程内只用一次即可


def main() -> int:
    print("=" * 78)
    print("翻译时机对比（mock 识别流：3 句话，快语速节奏 350ms/片段）")
    print("=" * 78)
    results = {}
    for mode, label in ((True, "等断句整句翻（新模式）"), (False, "边说边翻（旧模式）")):
        reqs, finals, _ = run_with_mode(mode)
        results[mode] = (reqs, finals)
        avg = sum(len(r) for r in reqs) / len(reqs) if reqs else 0
        print(f"\n【{label}】")
        print(f"  翻译请求次数      : {len(reqs)}")
        print(f"  平均每次字符数    : {avg:.1f}")
        print(f"  定稿句子数        : {len(finals)}")
        print("  请求内容:")
        for i, r in enumerate(reqs, 1):
            print(f"    {i:2d}. [{len(r):3d}字] {r[:46]}")

    new_reqs, new_finals = results[True]
    old_reqs, old_finals = results[False]
    print("\n" + "=" * 78)
    print("对比结论")
    print("=" * 78)
    if old_reqs:
        cut = (1 - len(new_reqs) / len(old_reqs)) * 100
        print(f"  翻译请求  : {len(old_reqs)} → {len(new_reqs)}   （减少 {cut:.0f}%）")
    old_avg = sum(len(r) for r in old_reqs) / len(old_reqs) if old_reqs else 0
    new_avg = sum(len(r) for r in new_reqs) / len(new_reqs) if new_reqs else 0
    print(f"  平均长度  : {old_avg:.1f} → {new_avg:.1f} 字")
    print(f"  定稿句数  : {len(old_finals)} → {len(new_finals)}")
    chars_old = sum(len(r) for r in old_reqs)
    chars_new = sum(len(r) for r in new_reqs)
    print(f"  总输入字符: {chars_old} → {chars_new}   （减少 {(1 - chars_new / chars_old) * 100:.0f}%）")

    test_punctuation_softbreak()
    return 0


def test_punctuation_softbreak() -> None:
    """标点软断句：超长句里遇到句末标点就先翻，避免在句子中间硬切。

    为什么需要它：带 BGM 的直播音频动态范围被压得极平（实测 P10~P90 仅 1dB），
    靠能量找不到停顿，Silero VAD 甚至判定 0 段。此时"静默兜底"失效，
    只剩"攒够字符数硬切"，必然切在句子中间。用标点断就自然多了。
    """
    print("\n" + "=" * 78)
    print("标点软断句测试（模拟带 BGM 的连续语音，没有停顿可依赖）")
    print("=" * 78)

    import livesub.pipeline as pl

    orig_init = pl.TranslatorActor.__init__

    def fake_init(self, translator, state, stats, control):
        self.requests = []

    def fake_submit(self, text, context, final, captured_at, sid, source_text=""):
        self.requests.append(text)

    def fake_context(self):
        return []

    pl.TranslatorActor.__init__ = fake_init
    pl.TranslatorActor.submit = fake_submit
    pl.TranslatorActor.context = fake_context

    from livesub.config import load_config
    from livesub.state import SubtitleState
    from livesub.stats import LatencyStats

    cfg = load_config()
    cfg.translate.translate_on_sentence_end = True
    cfg.translate.max_pending_chars = 40
    cfg.translate.pending_timeout_ms = 99999   # 故意关掉静默兜底，只测标点

    state = SubtitleState()
    stats = LatencyStats()
    control = pl.RunControl()

    pipe = pl.Pipeline.__new__(pl.Pipeline)
    pipe.cfg = cfg
    pipe.state = state
    pipe.stats = stats
    pipe.control = control
    pipe.actor = pl.TranslatorActor(None, state, stats, control)
    pipe._sid = 0
    pipe._cur_source = ""
    pipe._cur_captured_at = 0.0
    pipe._last_spec_text = ""
    pipe._last_spec_at = 0.0
    pipe._spec_interval = 0.35
    pipe._spec_min_growth = 6
    pipe._sample_rate = 16000
    pipe._sentence_started = 0.0
    pipe._pending = ""
    pipe._pending_since = 0.0
    pipe._pending_last = 0.0

    class NullLogger:
        def write(self, **kw):
            pass

    long_speech = [
        "えっとね、今日はですね、",
        "えっとね、今日はですね、みんなに",
        "えっとね、今日はですね、みんなに新しいゲームを",
        "えっとね、今日はですね、みんなに新しいゲームを紹介します。",
        "えっとね、今日はですね、みんなに新しいゲームを紹介します。それで",
        "えっとね、今日はですね、みんなに新しいゲームを紹介します。それでこのゲームは",
        "えっとね、今日はですね、みんなに新しいゲームを紹介します。それでこのゲームはね、",
    ]
    for t in long_speech:
        pipe._on_event(AsrEvent(kind="partial", text=t, audio_captured_at=0.0), NullLogger())
    pipe._maybe_flush_pending(force=True)

    reqs = pipe.actor.requests
    print(f"  输入：{len(long_speech)} 个中间结果，最后一条 {len(long_speech[-1])} 字，全程无停顿")
    print(f"  翻译请求 {len(reqs)} 次：")
    for i, r in enumerate(reqs, 1):
        print(f"    {i}. [{len(r):2d}字] {r}")
    ok = all(
        r.endswith(("。", "！", "？")) or len(r) >= cfg.translate.max_pending_chars
        for r in reqs
    )
    print(f"\n  {'✓' if ok else '✗'} 每次都是「以标点结尾」或「到达长度上限」，没有在句子中间硬切")

    pl.TranslatorActor.__init__ = orig_init


if __name__ == "__main__":
    sys.exit(main())
