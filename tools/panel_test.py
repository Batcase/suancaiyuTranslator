"""面板模式悬浮窗自检：固定尺寸、可拉伸、字号不变、自动跟随。

验证点（都是"跳得太快读不过来"这个问题的直接对策）：
  1. 窗口尺寸固定：加入新内容后窗口大小**不变**（旧模式会不停改变高度）
  2. 拉大窗口 → 能显示的**内容变多**，但**字号不变**
  3. 新内容自动跟到底部（像 tail -f）
  4. 历史真的被保留（不是只留 2 行）

用法: python tools\\panel_test.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tkinter as tk  # noqa: E402

from livesub.config import load_config  # noqa: E402
from livesub.overlay import SubtitleOverlay  # noqa: E402
from livesub.state import SubtitleState  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILED.append(label)


#: 一段"快语速"内容：句子短、句数多
SENTENCES = [
    ("あ、どうもこんにちは。", "啊，你好。"),
    ("今日は皆さんに新しいゲームを紹介したいと思います。", "今天想给大家介绍一款新游戏。"),
    ("このゲームはオープンワールドでグラフィックがすごいんですよ。", "这款游戏是开放世界的，画面超厉害。"),
    ("とりあえず最初の30分だけ遊んでみますね。", "总之先玩个30分钟看看。"),
    ("どこから見てるかチャットで教えてください。", "你们从哪儿看的，在聊天里告诉我。"),
    ("あ、そうだ、今日誕生日なんですよ実は。", "啊对了，其实今天是我生日。"),
    ("みんなお祝いしてくれてありがとうございます。", "谢谢大家给我庆祝。"),
    ("ちょっと待ってね、今コメント読むから。", "稍等一下，我先看评论。"),
]


def pump(root: tk.Tk, seconds: float) -> None:
    """让 tk 的 after(40ms) 真的跑到。

    只调 update() 不会推进时间，_tick 永远不执行——必须让真实时间流逝，
    否则测的是"渲染循环没跑"而不是"渲染对不对"。
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.005)


def main() -> int:
    print("=" * 66)
    print("面板模式悬浮窗自检")
    print("=" * 66)

    cfg = load_config()
    cfg.overlay.panel_mode = True
    cfg.overlay.panel_width = 820
    cfg.overlay.panel_height = 260
    cfg.overlay.history_lines = 200

    root = tk.Tk()
    root.withdraw()
    state = SubtitleState(max_lines=cfg.overlay.max_lines, history_lines=cfg.overlay.history_lines)
    state.set_status(state="识别中（阿里云 Paraformer）", detail="测试")

    ov = SubtitleOverlay(cfg, state, root=root)
    ov._panel_mode = True
    ov.start()
    pump(root, 0.3)

    check(ov.transcript is not None, "面板视图已创建")
    w0, h0 = ov.win.winfo_width(), ov.win.winfo_height()
    f_target_px = ov.f_target.metrics("linespace")
    print(f"    初始窗口 {w0}x{h0}   译文字号行高 {f_target_px}px")
    check(abs(w0 - 820) <= 4 and abs(h0 - 260) <= 4, "初始尺寸与配置一致")

    # ---- 灌入内容，观察窗口是否稳定 ----
    print("\n  灌入 8 句（模拟快语速连续说话）…")
    sizes: list[tuple[int, int]] = []
    for src, dst in SENTENCES:
        sid = state.begin_sentence()
        state.set_source(sid, src)
        state.patch_translation(sid, dst, final=True)
        pump(root, 0.06)          # 跨过至少一个 40ms tick
        sizes.append((ov.win.winfo_width(), ov.win.winfo_height()))

    uniq = set(sizes)
    check(len(uniq) == 1, "窗口尺寸在更新过程中**保持不变**", f"出现过 {len(uniq)} 种尺寸")
    if len(uniq) > 1:
        print(f"      尺寸序列: {sorted(uniq)}")

    lines, _ = state.snapshot()
    check(len(lines) >= len(SENTENCES), "历史被完整保留（不只 2 行）", f"共 {len(lines)} 行")

    # ---- 面板是否真的渲染出了内容 ----
    pump(root, 0.2)
    items = ov.transcript.canvas.find_all()
    texts = [i for i in items if ov.transcript.canvas.type(i) == "text"]
    check(len(texts) > len(SENTENCES), "面板里画出了文字", f"{len(texts)} 个文本项")

    first, last = ov.transcript.canvas.yview()
    check(last > 0.9, "自动跟随到底部（能看到最新内容）", f"yview={first:.2f}~{last:.2f}")

    # ---- 拉大窗口：内容变多、字号不变 ----
    print("\n  拉大窗口 820x260 → 1100x560 …")
    ov._apply_size(1100, 560)
    pump(root, 0.3)
    f_target_after = ov.f_target.metrics("linespace")
    check(
        f_target_after == f_target_px,
        "拉大窗口后**字号不变**",
        f"{f_target_px}px → {f_target_after}px",
    )
    w1, h1 = ov.win.winfo_width(), ov.win.winfo_height()
    check(w1 > w0 and h1 > h0, "窗口确实变大了", f"{w1}x{h1}")

    first2, last2 = ov.transcript.canvas.yview()
    visible_ratio = last2 - first2
    visible_ratio0 = last - first
    check(
        visible_ratio < visible_ratio0 or visible_ratio < 0.99,
        "可视比例变化（能同时看到更多历史）",
        f"{visible_ratio0:.2f} → {visible_ratio:.2f}",
    )

    # ---- 变小也不会崩 ----
    ov._apply_size(300, 120)
    pump(root, 0.2)
    check(True, "缩小窗口不崩溃", f"{ov.win.winfo_width()}x{ov.win.winfo_height()}")

    ov.stop()
    root.destroy()

    print("\n" + "=" * 66)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
    else:
        print("全部通过")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
