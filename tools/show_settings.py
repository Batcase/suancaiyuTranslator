"""打开主控面板并直接跳到设置页（用于截图/手工检查设置界面）。

用法: python tools\show_settings.py [宽度] [高度]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livesub.app import AppPanel  # noqa: E402


def main() -> int:
    w = int(sys.argv[1]) if len(sys.argv) > 1 else 640
    h = int(sys.argv[2]) if len(sys.argv) > 2 else 780
    panel = AppPanel()
    panel.show_settings()
    panel.root.geometry(f"{w}x{h}+640+120")
    panel.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
