"""按 exe 的真实启动路径测面板：AppPanel -> start_translation -> 看字幕。

为什么单独写这个：`main.py --wav` 走的是 CLI 路径，它自己建 SubtitleOverlay；
而用户点的是 GUI 上的「启动翻译」，走的是 AppPanel 那条路径。
两者创建悬浮窗的时机与参数可能不同——"没有任何文字"正是在 GUI 路径上出现的。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livesub.app import AppPanel  # noqa: E402
from livesub.config import load_config  # noqa: E402


def pump(root, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.005)


def main() -> int:
    cfg = load_config()
    print("配置:")
    o = cfg.overlay
    print(f"  panel_mode={o.panel_mode} size={o.panel_width}x{o.panel_height} "
          f"bg={o.panel_bg} alpha={o.panel_opacity} click_through={o.click_through}")
    print(f"  fonts: 译文={o.translated_font_size} 原文={o.original_font_size}")

    panel = AppPanel(cfg)
    pump(panel.root, 0.5)

    print(f"\n启动后界面: 向导={panel.screen_setup.winfo_ismapped()} "
          f"主控={panel.screen_main.winfo_ismapped()}")

    # 直接调用 GUI 的启动入口
    print("\n调用 start_translation()（等同于点「启动翻译」）…")
    panel.start_translation()
    pump(panel.root, 4.0)

    ov = getattr(panel, "overlay", None)
    print(f"  overlay 对象: {ov}")
    if ov is None:
        print("  ❌ overlay 未创建")
    else:
        print(f"  overlay.win  : {ov.win}")
        print(f"  _panel_mode  : {ov._panel_mode}")
        print(f"  transcript   : {ov.transcript}")
        if ov.win is not None:
            w = ov.win
            print(f"  win geometry : {w.winfo_geometry()}  mapped={w.winfo_ismapped()}")
            try:
                print(f"  win alpha    : {w.attributes('-alpha')}")
            except Exception as e:
                print(f"  alpha 读不到 : {e}")
        if ov.transcript is not None:
            tv = ov.transcript
            items = tv.canvas.find_all()
            print(f"  canvas       : {tv.canvas.winfo_width()}x{tv.canvas.winfo_height()} "
                  f"items={len(items)} rendered={len(tv._rendered)}")
            texts = [i for i in items if tv.canvas.type(i) == "text"]
            print(f"  text items   : {len(texts)}")
            if texts:
                for i in texts[-3:]:
                    print(f"    {tv.canvas.itemcget(i,'text')[:24]!r} fill={tv.canvas.itemcget(i,'fill')}")

    # 往里灌几条真实内容，模拟识别结果到达
    print("\n灌入 3 条字幕…")
    st = panel.state
    for s, d in [
        ("あ、どうもこんにちは。", "啊，你好。"),
        ("今日は皆さんに新しいゲームを紹介したいと思います。", "今天想给大家介绍一款新游戏。"),
        ("このゲームはオープンワールドで、グラフィックが本当にすごいんですよ。", "这款游戏是开放世界的，画面真的超厉害。"),
    ]:
        sid = st.begin_sentence()
        st.set_source(sid, s)
        st.patch_translation(sid, d, final=True, e2e_ms=420)
        pump(panel.root, 0.4)

    if ov is not None and ov.transcript is not None:
        tv = ov.transcript
        items = tv.canvas.find_all()
        texts = [i for i in items if tv.canvas.type(i) == "text"]
        print(f"  灌入后: items={len(items)} text={len(texts)} rendered={len(tv._rendered)}")
        if texts:
            for i in texts[-2:]:
                print(f"    {tv.canvas.itemcget(i,'text')[:30]!r} fill={tv.canvas.itemcget(i,'fill')}")
        else:
            print("  ❌ 仍然没有文字")

    print("\n保持显示 8 秒供截图…")
    pump(panel.root, 8.0)

    try:
        panel.stop_translation()
    except Exception:
        pass
    panel.root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
