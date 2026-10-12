"""打包成独立 exe（目标机器不需要装 Python）。

为什么用 PyInstaller：它会把 Python 解释器、标准库和 numpy 等依赖一起打进去，
生成单个可执行文件。用户双击就能用，不用装任何东西。

用法:
    python tools\\build_exe.py            # 打包（onefile，约 40~70MB）
    python tools\\build_exe.py --onedir   # 打包成目录（启动更快，便于排错）

产物:
    dist\\酸菜鱼翻译器.exe

注意
----
* 用 `--noconsole`：GUI 程序不该带着黑窗口。启动失败会写 logs/error.log 并弹窗。
* 体积主要来自 numpy。本地 Whisper（faster-whisper）**故意不打进去**：
  它依赖一大坨二进制，会让体积翻好几倍，而且手机端也用不上。
  需要离线识别的话，单独 `pip install faster-whisper modelscope` 或跑源码版。
* 杀毒软件可能误报（PyInstaller 的常见问题），必要时加白名单。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "酸菜鱼翻译器"

#: exe 文件名带上版本号，方便同时留着多个版本对比（也免得"我测的是哪个"搞混）
try:
    _version = (ROOT / "livesub" / "__init__.py").read_text(encoding="utf-8")
    _version = _version.split('__version__ = "', 1)[1].split('"', 1)[0]
except Exception:
    _version = "dev"
EXE_NAME = f"{NAME}-{_version}"

#: 明确排除的重型/无关模块（能显著减小体积、缩短打包时间）
EXCLUDES = [
    "faster_whisper",      # 可选依赖，体积巨大，故意不打
    "ctranslate2",
    "modelscope",
    "tokenizers",
    "huggingface_hub",
    "torch", "torchaudio", "torchvision",
    "matplotlib", "scipy", "pandas", "PIL", "PyQt5", "PySide6",
    "tkinter.test", "test", "unittest", "pydoc_data",
    "IPython", "jupyter", "notebook",
]

#: 需要一起带上的非代码文件
DATAS = [
    (ROOT / ".env.example", "."),
    (ROOT / "config.example.toml", "."),
    (ROOT / "README.md", "."),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="打包成独立 exe")
    ap.add_argument("--onedir", action="store_true", help="打包成目录而不是单文件（启动更快）")
    ap.add_argument("--console", action="store_true", help="保留控制台窗口（便于排错）")
    ap.add_argument("--name", default=EXE_NAME, help=f"可执行文件名（默认 {EXE_NAME}）")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("需要先安装 PyInstaller：")
        print("  python -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 1

    # 清理上次产物
    for d in ("build", "dist"):
        p = ROOT / d
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", args.name,
        "--onedir" if args.onedir else "--onefile",
        "--console" if args.console else "--windowed",
        # 中文路径/输出的稳定性
        "--hidden-import", "livesub",
        "--hidden-import", "livesub.app",
        "--hidden-import", "livesub.overlay",
        "--hidden-import", "livesub.wasapi",
        "--hidden-import", "websockets.legacy",
        "--hidden-import", "websockets.legacy.client",
        "--collect-submodules", "websockets",
    ]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    for src, dest in DATAS:
        if src.exists():
            cmd += ["--add-data", f"{src}{os.pathsep}{dest}"]
    cmd.append(str(ROOT / "main.py"))

    print("打包命令：")
    print("  " + " ".join(cmd))
    print()

    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        print(f"\n[失败] PyInstaller 返回 {proc.returncode}")
        return proc.returncode

    # 报告产物
    print(f"\n打包完成，用时 {time.time() - t0:.0f}s")
    out_dir = ROOT / "dist"
    for item in sorted(out_dir.rglob("*")):
        if item.is_file() and (item.suffix.lower() == ".exe" or item.name == args.name):
            size = item.stat().st_size / 1024 / 1024
            print(f"  {item.relative_to(ROOT)}   {size:.1f} MB")
    print(
        "\n提示：\n"
        "  1) 这个 exe 可以在**没有装 Python** 的 Windows 上直接双击运行\n"
        "  2) 首次运行会弹 SmartScreen 警告（未签名程序都这样），点「更多信息 → 仍要运行」\n"
        "  3) 配置与密钥存在 exe 旁边的 .env / config.toml（绿色软件，整个文件夹可拷走）\n"
        "  4) 启动失败会写 logs/error.log 并弹窗提示"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
