"""面板模式悬浮窗的视觉演示（截图用）。

灌入一段模拟的日译中字幕，让面板显示出来，方便肉眼看排版对不对。

用法: python tools\\panel_demo.py [宽度] [高度]
"""

from __future__ import annotations

import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livesub.config import load_config  # noqa: E402
from livesub.overlay import SubtitleOverlay  # noqa: E402
from livesub.state import SubtitleState  # noqa: E402

SAMPLE = [
    ("あ、どうもこんにちは。", "啊，你好。"),
    ("今日は皆さんに新しいゲームを紹介したいと思います。", "今天想给大家介绍一款新游戏。"),
    ("このゲームはオープンワールドで、グラフィックが本当にすごいんですよ。", "这款游戏是开放世界的，画面真的超厉害。"),
    ("とりあえず、最初の30分だけ遊んでみますね。", "总之，先玩个30分钟试试。"),
    ("どこから見てるか、チャットで教えてください。", "你们从哪儿看的，在聊天里告诉我。"),
    ("あ、そうだ、今日誕生日なんですよ、実は。", "啊对了，其实今天是我生日。"),
    ("みんなお祝いしてくれてありがとうございます。", "谢谢大家给我庆祝。"),
]


def pump(root: tk.Tk, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.005)


def main() -> int:
    w = int(sys.argv[1]) if len(sys.argv) > 1 else 900
    h = int(sys.argv[2]) if len(sys.argv) > 2 else 340

    cfg = load_config()
    cfg.overlay.panel_mode = True
    cfg.overlay.panel_width = w
    cfg.overlay.panel_height = h
    cfg.overlay.click_through = False       # 演示时不穿透，方便后续截图定位

    root = tk.Tk()
    root.withdraw()
    state = SubtitleState(max_lines=2, history_lines=200)
    state.set_status(state="识别中（阿里云 Paraformer）", detail="端到端 418ms")

    ov = SubtitleOverlay(cfg, state, root=root)
    ov._panel_mode = True
    ov.start()
    pump(root, 0.3)

    for src, dst in SAMPLE:
        sid = state.begin_sentence()
        state.set_source(sid, src)
        state.patch_translation(sid, dst, final=True, e2e_ms=430)
        pump(root, 0.08)

    # 让最后一行处于"正在翻译"的状态，看得到暖黄高亮
    sid = state.begin_sentence()
    state.set_source(sid, "えっと、次は何をしようかな。")
    pump(root, 0.3)

    ov.win.update_idletasks()
    x, y = ov.win.winfo_rootx(), ov.win.winfo_rooty()
    ww, wh = ov.win.winfo_width(), ov.win.winfo_height()
    print(f"PANEL_RECT={x},{y},{ww},{wh}", flush=True)
    ov.win.lift()
    ov.win.attributes("-topmost", True)

    # 保持显示若干秒，便于外部截图（也方便人眼直接看）
    hold = float(sys.argv[3]) if len(sys.argv) > 3 else 12.0
    print(f"面板保持显示 {hold:.0f} 秒…", flush=True)
    pump(root, hold)

    ov.stop()
    root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
