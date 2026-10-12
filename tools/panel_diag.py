"""诊断：面板模式为什么没有文字。

复现用户实际路径：无 config.toml → 填好 Key → 启动翻译 → 看面板。
这里直接用真实配置 + 真实悬浮窗，灌入几条字幕，然后检查：
  * 窗口 alpha / 尺寸
  * canvas 里的 item 数量与类型
  * 各控件的背景色与可见性
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


def pump(root: tk.Tk, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.005)


def main() -> int:
    cfg = load_config()          # 用**真实配置**，不覆盖任何字段
    ov_cfg = cfg.overlay
    print("配置：")
    for k in ("panel_mode", "panel_width", "panel_height", "panel_bg",
              "panel_bg_live", "panel_opacity", "auto_scroll", "show_scrollbar",
              "translated_font_size", "original_font_size", "max_lines",
              "history_lines", "click_through", "background_panel"):
        print(f"  {k:22s} = {getattr(ov_cfg, k, '(缺失)')}")

    root = tk.Tk()
    root.withdraw()
    state = SubtitleState(max_lines=ov_cfg.max_lines, history_lines=ov_cfg.history_lines)
    state.set_status(state="识别中（阿里云 Paraformer）", detail="测试 418ms")

    ov = SubtitleOverlay(cfg, state, root=root)
    print(f"\n面板模式: {ov._panel_mode}")
    print(f"transcript: {ov.transcript}")
    ov.start()
    pump(root, 0.4)

    win = ov.win
    print("\n窗口：")
    print(f"  geometry     = {win.winfo_geometry()}")
    print(f"  可见 mapped  = {win.winfo_ismapped()}")
    try:
        print(f"  alpha        = {win.attributes('-alpha')}")
    except Exception as e:
        print(f"  alpha 读取失败: {e}")
    print(f"  topmost      = {win.attributes('-topmost')}")

    tv = ov.transcript
    if tv is None:
        print("\n❌ transcript 为空 —— 面板模式没生效")
        root.destroy()
        return 1

    print("\n控件：")
    print(f"  body   bg={ov.body.cget('bg')}  y={ov.body.winfo_y()} h={ov.body.winfo_height()}")
    print(f"  canvas bg={tv.canvas.cget('bg')} w={tv.canvas.winfo_width()} h={tv.canvas.winfo_height()}")
    print(f"  canvas mapped={tv.canvas.winfo_ismapped()}")
    print(f"  _width_seen={tv._width_seen}  _rendered={len(tv._rendered)}  _content_h={tv._content_h}")

    # 灌内容
    for i, (s, d) in enumerate([
        ("あ、どうもこんにちは。", "啊，你好。"),
        ("今日は皆さんに新しいゲームを紹介したいと思います。", "今天想给大家介绍一款新游戏。"),
    ]):
        sid = state.begin_sentence()
        state.set_source(sid, s)
        state.patch_translation(sid, d, final=True, e2e_ms=420)
        pump(root, 0.15)

    print("\n灌入 2 句后：")
    print(f"  快照行数      = {len(state.snapshot()[0])}")
    print(f"  _rendered     = {len(tv._rendered)}")
    print(f"  _last_key     = {'有' if ov._last_key else '空'}")
    items = tv.canvas.find_all()
    print(f"  canvas items  = {len(items)}")
    kinds: dict[str, int] = {}
    for i in items:
        t = tv.canvas.type(i)
        kinds[t] = kinds.get(t, 0) + 1
    print(f"  类型分布      = {kinds}")
    for i in items[:6]:
        t = tv.canvas.type(i)
        if t == "text":
            print(f"    text: {tv.canvas.itemcget(i, 'text')[:30]!r} fill={tv.canvas.itemcget(i,'fill')}")

    # 手动强制一次渲染，看是否恢复
    if len(items) == 0:
        print("\n⚠ canvas 是空的，尝试手动调用 _render()…")
        ov._last_key = ()
        try:
            ov._render()
            pump(root, 0.2)
            print(f"  手动渲染后 items = {len(tv.canvas.find_all())}")
        except Exception as exc:
            import traceback
            traceback.print_exc()

    win.lift()
    win.attributes("-topmost", True)
    pump(root, 3.0)   # 停留，便于外部截图

    ov.stop()
    root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
