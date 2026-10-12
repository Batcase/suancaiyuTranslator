"""验证"鼠标穿透是否导致滚轮失效"——直接发 WM_MOUSEWHEEL 给窗口。

为什么要这样测：tkinter 的 <MouseWheel> 事件依赖窗口能收到鼠标消息。
如果窗口带 WS_EX_TRANSPARENT，鼠标消息会被系统直接跳过，事件永远不来。

这里绕过 tk 事件系统，用 SendMessageW 直接投递真实滚轮消息，
看 canvas 的 yview 有没有变化 —— 能把"事件没到窗口"和"到了但没处理"分开。
"""

from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livesub.config import load_config  # noqa: E402
from livesub.overlay import SubtitleOverlay  # noqa: E402
from livesub.state import SubtitleState  # noqa: E402

user32 = ctypes.windll.user32
WM_MOUSEWHEEL = 0x020A
WS_EX_TRANSPARENT = 0x20
GWL_EXSTYLE = -20


def pump(root: tk.Tk, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.005)


def fill(state: SubtitleState, n: int) -> None:
    for i in range(n):
        sid = state.begin_sentence()
        state.set_source(sid, f"テスト{i}")
        state.patch_translation(sid, f"测试第{i}句", final=True)


def wheel(hwnd: int, delta: int, count: int = 3) -> None:
    """向窗口投递真实滚轮消息。delta 正=上滚。"""
    lparam = 0
    for _ in range(count):
        user32.SendMessageW(hwnd, WM_MOUSEWHEEL, (delta << 16), lparam)


def main() -> int:
    cfg = load_config()
    root = tk.Tk()
    root.withdraw()
    state = SubtitleState(max_lines=2, history_lines=200)
    state.set_status(state="识别中", detail="测试")

    ov = SubtitleOverlay(cfg, state, root=root)
    ov._panel_mode = True
    ov.start()
    pump(root, 0.3)
    fill(state, 25)
    pump(root, 0.4)

    tv = ov.transcript
    hwnd = ov._hwnd
    ex0 = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    print(f"  窗口 exstyle      = 0x{ex0:X}   穿透={bool(ex0 & WS_EX_TRANSPARENT)}")
    print(f"  canvas 绑定滚轮    = {bool(tv.canvas.bind('<MouseWheel>'))}")
    before = tv.canvas.yview()
    print(f"  初始 yview        = {before[0]:.3f}~{before[1]:.3f}")

    # 1) 穿透状态下发滚轮
    tv._user_scrolled = False
    tv.canvas.yview_moveto(1.0)
    pump(root, 0.1)
    y0 = tv.canvas.yview()[0]
    wheel(hwnd, 120, 3)
    pump(root, 0.5)
    y1 = tv.canvas.yview()[0]
    print(f"\n  [穿透=开] 上滚 3 格: {y0:.3f} -> {y1:.3f}   "
          f"{'✅ 生效' if y1 < y0 - 0.01 else '❌ 无效 ← 复现了用户的问题'}")

    # 2) 手动关掉穿透后再发
    tv.canvas.yview_moveto(1.0)
    pump(root, 0.1)
    ov._apply_click_through(False)
    pump(root, 0.2)
    ex1 = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    y2 = tv.canvas.yview()[0]
    wheel(hwnd, 120, 3)
    pump(root, 0.5)
    y3 = tv.canvas.yview()[0]
    print(f"  [穿透=关] (exstyle=0x{ex1:X}) 上滚 3 格: {y2:.3f} -> {y3:.3f}   "
          f"{'✅ 生效' if y3 < y2 - 0.01 else '❌ 仍无效'}")

    # 3) 试 <Enter> 事件在穿透状态下会不会触发（我的原方案就靠它）
    print("\n  测试 <Enter> 事件（穿透状态下）:")
    print("    —— 若这里不触发，说明「靠悬停关穿透」的方案本身就不可能工作")
    tv.canvas.yview_moveto(1.0)
    pump(root, 0.1)

    ov.stop()
    root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
