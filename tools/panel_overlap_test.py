"""验证面板不会叠字（下一行压在上一行上）。

背景：译文是流式增长的。旧实现"边测边画、只重画变化的那块"会有个隐蔽问题：
canvas item 是"后创建的在上面"，**新画的底板矩形会压住上面那些还没重画的行**，
视觉上就是"下一行叠在上一行"。实测确认过。

修法是"先全量测量高度，再按最终位置一次性绘制"。这个脚本用来守住它。

同时验证滚动：`scroll_by` 能不能真的改变 yview（滚轮最终走的就是它）。
"""

from __future__ import annotations

import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livesub.config import load_config  # noqa: E402
from livesub.overlay import SubtitleOverlay  # noqa: E402
from livesub.state import SubtitleState  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILED.append(label)


def pump(root: tk.Tk, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.005)


#: 故意用长短差异很大的句子：短句 1 行、长句折成 3~4 行，
#: 这样"某一行突然变高"的情况一定会出现，最容易暴露叠字
MIXED = [
    ("はい。", "嗯。"),
    ("今日は皆さんに新しいゲームを紹介したいと思います。",
     "今天想给大家介绍一款新游戏，这款游戏我玩了大概有两个星期了，感觉真的非常有意思，所以今天特别想推荐给大家。"),
    ("そうですね。", "是啊。"),
    ("このゲームはオープンワールドで、グラフィックが本当にすごいんですよ、しかも動作も軽いんです。",
     "这款游戏是开放世界的，画面真的超厉害，而且运行起来还很流畅，我用的是一台比较老的电脑，居然也能跑得动。"),
    ("はい。", "对。"),
    ("とりあえず最初の30分だけ遊んでみますね。", "总之先玩30分钟看看。"),
    ("あ、そうだ、今日誕生日なんですよ、実は。",
     "啊对了，其实今天是我生日，刚才有人在聊天里说了我才想起来，谢谢大家。"),
    ("ありがとう。", "谢谢。"),
]


def main() -> int:
    print("=" * 70)
    print("面板叠字与滚动自检")
    print("=" * 70)

    cfg = load_config()
    cfg.overlay.panel_mode = True
    cfg.overlay.panel_width = 700      # 窄一点，更容易折行
    cfg.overlay.panel_height = 420

    root = tk.Tk()
    root.withdraw()
    state = SubtitleState(max_lines=2, history_lines=200)
    state.set_status(state="识别中", detail="测试")

    ov = SubtitleOverlay(cfg, state, root=root)
    ov._panel_mode = True
    ov.start()
    pump(root, 0.4)
    tv = ov.transcript

    print("\n  逐条灌入（含长短差异很大的句子，制造「某行突然变高」）…")
    for i, (src, dst) in enumerate(MIXED):
        sid = state.begin_sentence()
        # 先只放原文（模拟识别先到）
        state.set_source(sid, src)
        pump(root, 0.08)
        # 再放译文（模拟翻译后到，行数会突然变多）
        state.patch_translation(sid, dst, final=True, e2e_ms=400)
        pump(root, 0.12)

    pump(root, 0.3)

    # ---- 叠字检查：同类型文字的包围盒不应相交 ----
    canvas = tv.canvas
    texts = [i for i in canvas.find_all() if canvas.type(i) == "text"]
    print(f"\n  canvas 里 {len(texts)} 个文本项")

    # 只看"填充色文字"（描边是 8 份黑字，会和填充字故意重叠）
    fills = [i for i in texts if canvas.itemcget(i, "fill") != "#000000"]
    boxes = []
    for i in fills:
        bb = canvas.bbox(i)
        if bb:
            boxes.append((bb[1], bb[3], canvas.itemcget(i, "text")[:20], i))
    boxes.sort()
    print(f"  填充文字 {len(boxes)} 项")

    overlaps = []
    for a in range(len(boxes)):
        for b in range(a + 1, len(boxes)):
            top1, bot1, txt1, _ = boxes[a]
            top2, bot2, txt2, _ = boxes[b]
            # 纵向相交（留 1px 容差）
            if top2 < bot1 - 1 and top1 < bot2 - 1:
                overlaps.append((txt1, txt2, top1, bot1, top2, bot2))
    check(
        not overlaps,
        "没有任何两行文字纵向重叠",
        f"{len(overlaps)} 处重叠" if overlaps else "干净",
    )
    for o in overlaps[:4]:
        print(f"      重叠: {o[0]!r}({o[2]}~{o[3]}) 与 {o[1]!r}({o[4]}~{o[5]})")

    # ---- 行间距检查：相邻块的间隔不应为负 ----
    print("\n  检查块间距:")
    tv.canvas.yview_moveto(0)
    pump(root, 0.05)
    sorted_boxes = sorted([(canvas.bbox(i)[1], canvas.bbox(i)[3]) for i in fills if canvas.bbox(i)])
    if len(sorted_boxes) >= 2:
        gaps = [
            sorted_boxes[i + 1][0] - sorted_boxes[i][1]
            for i in range(len(sorted_boxes) - 1)
        ]
        negative = [g for g in gaps if g < -1]
        check(not negative, "相邻文字行间距非负", f"最小间距 {min(gaps)}px")
        for g in gaps[:8]:
            print(f"      间距 {g}px")

    # ---- 滚动检查 ----
    print("\n  滚动检查:")
    tv.canvas.yview_moveto(1.0)
    pump(root, 0.1)
    first, last = canvas.yview()
    print(f"    yview = {first:.3f}~{last:.3f}   内容高 {tv._content_h}px")

    tv._user_scrolled = False
    y0 = canvas.yview()[0]
    tv.scroll_by(-3)          # 滚轮上滚走的就是这个
    pump(root, 0.15)
    y1 = canvas.yview()[0]
    check(y1 < y0 - 0.005, "scroll_by 上滚生效（滚轮走的就是它）", f"{y0:.3f} → {y1:.3f}")

    tv.scroll_to_bottom()
    pump(root, 0.1)
    check(canvas.yview()[1] > 0.99, "能回到底部", f"yview={canvas.yview()[1]:.3f}")

    # ---- 全局鼠标钩子 ----
    print("\n  全局鼠标钩子:")
    check(ov._hook is not None, "WH_MOUSE_LL 已安装", f"handle={ov._hook}")

    ov.stop()
    root.destroy()

    print("\n" + "=" * 70)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
    else:
        print("全部通过")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
