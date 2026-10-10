"""主控面板的自检：配置向导 / 启动停止 / 悬浮窗生命周期。

不需要人工点击——直接调用界面上的方法，然后检查内部状态。
会在主线程跑真正的 tkinter 事件循环，所以能覆盖到真实的窗口创建与销毁路径。

用法: python tools/app_selftest.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 这个自检只验证"界面与启动/停止/悬浮窗生命周期"，不验证真实识别效果。
# 所以用一次性假 Key 让它能启动（真调接口会失败，但降级链会继续跑，
# 流程照样能走完）。用环境变量而非写 .env，测完即失效。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk-selftest-dashscope-000000000000")
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-selftest-deepseek-000000000000")

from livesub import keystore  # noqa: E402
from livesub.app import AppPanel  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        FAILED.append(label)


def main() -> int:
    print("=" * 60)
    print("主控面板自检")
    print("=" * 60)

    panel = AppPanel()
    panel.root.update()
    steps: list[tuple[float, str]] = []
    t0 = time.monotonic()

    def at(delay: float, fn) -> None:
        steps.append((delay, fn))  # type: ignore[arg-type]

    def step_screen_check() -> None:
        print(f"\n[{(time.monotonic()-t0):.1f}s] 检查初始界面")
        has_keys = keystore.has_all_keys(("deepseek",))
        shown_setup = panel.screen_setup.winfo_ismapped()
        shown_main = panel.screen_main.winfo_ismapped()
        if has_keys:
            check(shown_main and not shown_setup, "已有 Key -> 直接进入主控界面")
        else:
            check(shown_setup and not shown_main, "没有 Key -> 进入首次配置向导")
        check(panel.root.winfo_width() > 400, f"窗口尺寸正常 ({panel.root.winfo_width()}x{panel.root.winfo_height()})")

    def step_start() -> None:
        print(f"\n[{(time.monotonic()-t0):.1f}s] 点击「启动翻译」")
        if not keystore.has_all_keys(("deepseek",)):
            print("  (跳过：没有翻译 Key，无法启动)")
            return
        panel.start_translation()
        # 失败时界面会把原因写在状态行里，打出来便于定位
        print(f"       状态行: {panel.l_state['text']!r}")
        check(panel.running, "running 标记已置位")
        check(str(panel.b_stop["state"]) == "normal", "停止按钮已启用")
        check(str(panel.b_start["state"]) == "disabled", "启动按钮已禁用")

    def step_overlay() -> None:
        print(f"\n[{(time.monotonic()-t0):.1f}s] 检查悬浮窗是否出现")
        ov = panel.overlay
        check(ov is not None, "悬浮窗实例已创建")
        if ov is not None:
            check(ov.win is not None and ov.win.winfo_exists() == 1, "悬浮窗窗口存在")
            check(not ov._closed, "悬浮窗处于运行状态")
        st = panel.state.snapshot()[1]
        print(f"       管线状态: {st.state!r}  错误 {st.errors}")

    def step_stop() -> None:
        print(f"\n[{(time.monotonic()-t0):.1f}s] 点击「停止翻译」")
        ov = panel.overlay
        panel.stop_translation()
        check(not panel.running, "running 标记已清除")
        check(panel.overlay is None, "悬浮窗引用已清空")
        check(str(panel.b_start["state"]) == "normal", "启动按钮恢复可用")
        if ov is not None:
            check(bool(ov._closed), "悬浮窗内部状态已关闭")

    def step_restart() -> None:
        print(f"\n[{(time.monotonic()-t0):.1f}s] 再次启动（验证可以反复启停）")
        if not keystore.has_all_keys(("deepseek",)):
            print("  (跳过)")
            return
        panel.start_translation()
        check(panel.running, "第二次启动成功")

    def step_settings() -> None:
        print(f"\n[{(time.monotonic()-t0):.1f}s] 打开「设置」页")
        panel.show_settings()
        panel.root.update()
        check(panel.screen_settings.winfo_ismapped(), "设置页已显示")
        check(bool(panel.l_key_status["text"]), "已显示脱敏后的现有 Key")
        print(f"       {panel.l_key_status['text'].replace(chr(10), ' | ')}")

    def step_finish() -> None:
        print(f"\n[{(time.monotonic()-t0):.1f}s] 收尾")
        if panel.running:
            panel.stop_translation()
        panel.root.quit()

    at(0.3, step_screen_check)
    at(0.6, step_start)
    at(3.0, step_overlay)
    at(3.6, step_stop)
    at(4.0, step_restart)
    at(5.5, step_stop)
    at(6.0, step_settings)
    at(6.4, step_finish)

    for delay, fn in steps:
        panel.root.after(int(delay * 1000), fn)

    panel.root.mainloop()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
    else:
        print("全部通过")
    print("=" * 60)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
