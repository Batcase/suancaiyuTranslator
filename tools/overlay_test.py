"""悬浮窗渲染自检：不联网、不采集，直接灌入几条字幕看 UI 效果。

用法:
    python tools/overlay_test.py            # 显示后 6 秒自动关闭
    python tools/overlay_test.py 30         # 显示 30 秒（方便手动截图/试交互）
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livesub.config import load_config  # noqa: E402
from livesub.overlay import SubtitleOverlay  # noqa: E402
from livesub.state import SubtitleState  # noqa: E402

DEMO = [
    ("Welcome back to the stream everyone.", "欢迎回到直播间，各位。"),
    ("Today I'm testing a brand new graphics card.", "今天我要测一块全新的显卡。"),
    ("Let me know in the chat where you're watching from.", "在聊天区告诉我你们是从哪看的。"),
]


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    cfg = load_config()
    state = SubtitleState(max_lines=cfg.overlay.max_lines)
    stop = threading.Event()

    def inject() -> None:
        # 先放两句已定稿的历史
        for src, dst in DEMO[:2]:
            sid = state.begin_sentence()
            state.set_source(sid, src, final=True)
            state.patch_translation(sid, dst, final=True, e2e_ms=780)
            time.sleep(0.4)
        # 再来一句"正在生成中"的：只有原文 + 半截译文
        src, dst = DEMO[2]
        sid = state.begin_sentence()
        state.set_source(sid, src)
        time.sleep(0.8)
        state.patch_translation(sid, dst[:10], final=False, e2e_ms=620)
        state.set_status(state="识别中", detail="48000Hz 2ch float32", e2e_ms=620)
        time.sleep(0.8)
        state.patch_translation(sid, dst, final=True, e2e_ms=910)
        state.set_status(state="识别中", e2e_ms=910)
        time.sleep(2.0)
        stop.set()

    threading.Thread(target=inject, daemon=True).start()
    overlay = SubtitleOverlay(cfg, state, on_quit=stop.set)

    def probe() -> None:
        """打印窗口与各行的真实坐标，便于确认布局是否符合预期。"""
        try:
            win = overlay.win
            print(
                f"[probe] win x={win.winfo_x()} y={win.winfo_y()} "
                f"w={win.winfo_width()} h={win.winfo_height()} "
                f"screen={win.winfo_screenwidth()}x{win.winfo_screenheight()} "
                f"alpha={win.attributes('-alpha')} topmost={win.attributes('-topmost')}"
            )
            print(
                f"[probe] body y={overlay.body.winfo_y()} h={overlay.body.winfo_height()} "
                f"bar y={overlay.status_bar.winfo_y()} h={overlay.status_bar.winfo_height()} "
                f"reqh={win.winfo_reqheight()}"
            )
            for i, row in enumerate(overlay.rows):
                f = row["frame"]
                print(
                    f"[probe] row{i} sid={row['sid']} y={f.winfo_y()} "
                    f"h={f.winfo_height()} live={row['live']}"
                )
        except Exception as exc:
            print(f"[probe] 失败: {exc}")

    def probe_later() -> None:
        time.sleep(3.0)
        overlay.root.after(0, probe)

    threading.Thread(target=probe_later, daemon=True).start()

    def watchdog() -> None:
        time.sleep(seconds)
        overlay.request_action("quit")

    threading.Thread(target=watchdog, daemon=True).start()
    print(f"悬浮窗将显示 {seconds:.0f} 秒…（F1 穿透 / F2 暂停 / F3 行数 / 拖动移动）")
    overlay.run()
    print("悬浮窗已关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
