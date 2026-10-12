"""酸菜鱼翻译器 —— 直播实时翻译

用法示例
--------
    # 0) 自检：本机能不能内录、有没有可用后端
    python -m livesub.audio

    # 1) 不联网先看悬浮窗长什么样（识别和翻译都用 mock）
    python main.py --asr mock --translate mock

    # 2) 真实链路：Deepgram 识别 + DeepSeek 翻译（需 .env 里配 Key）
    python main.py

    # 3) 无界面模式（只打印，便于压测延迟/排查）
    python main.py --no-gui

    # 4) 用 wav 复现问题（不需要正在直播）
    python main.py --wav samples\test.wav --asr mock --translate mock

    # 5) 只测识别、不花钱翻译
    python main.py --translate mock

退出：终端 Ctrl+C，或悬浮窗上按「关闭」（--no-gui 下为 Ctrl+C）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import threading
import time
from pathlib import Path

# 允许 python main.py 直接跑（把包目录加进 path）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from livesub import __version__, audio as audio_mod  # noqa: E402
from livesub.config import Config, describe, load_config  # noqa: E402
from livesub.pipeline import Pipeline, RunControl  # noqa: E402
from livesub.state import SubtitleState  # noqa: E402
from livesub.stats import LatencyStats  # noqa: E402

BANNER = r"""
   ____                              _              _
  / ___| _   _  __ _ _ __   ___ __ _(_)_   _ _   _  | |
  \___ \| | | |/ _` | '_ \ / __/ _` | | | | | | | | | |
   ___) | |_| | (_| | | | | (_| (_| | | |_| | |_| | |_|
  |____/ \__,_|\__,_|_| |_|\___\__,_|_|\__, |\__,_| (_)
                                        |___/     v{ver}
                        酸菜鱼翻译器 · SuanCaiYu Translator
"""


def _is_beta(ver: str) -> bool:
    """beta / alpha / rc 之类的测试版。"""
    return any(t in ver.lower() for t in ("beta", "alpha", "rc", "dev"))


def _version_text() -> str:
    """版本说明，附带运行方式与（打包后的）构建时间。

    为什么要把"运行方式/构建时间"也带上：用户报问题时最常见的一句是
    "我测的是哪个包"——带上这些能直接对上是源码版还是 exe 版、什么时候打的。
    """
    import sys as _sys

    tag = "（测试版）" if _is_beta(__version__) else ""
    if getattr(_sys, "frozen", False):
        how = "独立 exe 版"
        # PyInstaller 会把 exe 的修改时间作为构建时间的近似
        try:
            from pathlib import Path as _P
            import datetime as _dt

            mt = _P(_sys.executable).stat().st_mtime
            built = _dt.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")
        except Exception:
            built = "未知"
    else:
        how = "源码版"
        built = "—"
    return f"酸菜鱼翻译器 {__version__}{tag}\n运行方式: {how}\n构建时间: {built}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="酸菜鱼翻译器",
        description="酸菜鱼翻译器 · 挂在直播软件前面的实时翻译字幕",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", help="配置文件路径（默认 ./config.toml）")
    p.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "wasapi", "sounddevice", "wav", "tone"],
        help="音频采集后端（默认 auto：Windows 用原生 WASAPI 回环）",
    )
    p.add_argument("--wav", default="", help="回放 wav 文件而不是内录（用于测试）")
    p.add_argument(
        "--asr",
        default="",
        help="覆盖识别引擎: deepgram（云，最快） | local（本地离线，免 Key） | openai_realtime | mock",
    )
    p.add_argument("--translate", default="", help="覆盖翻译引擎: deepseek | openai | mock")
    p.add_argument("--whisper-model", default="", help="本地识别模型: tiny | base | small（默认 tiny）")
    p.add_argument("--source-lang", default="", help="源语言，auto 为自动识别")
    p.add_argument("--target-lang", default="", help="目标语言，例如 简体中文 / English")
    p.add_argument("--device", default="", help="采集设备名子串（默认扬声器）")
    p.add_argument("--no-gui", action="store_true", help="不开悬浮窗，只往终端打印")
    p.add_argument("--log", nargs="?", const="auto", default="", help="把字幕写成 JSONL（默认 logs/）")
    p.add_argument("--duration", type=float, default=0.0, help="运行指定秒数后自动退出")
    p.add_argument("--check", action="store_true", help="只做环境自检然后退出")
    p.add_argument("--version", action="version", version=_version_text())
    return p.parse_args(argv)


def apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    if args.asr:
        cfg.asr.provider = args.asr
    if args.whisper_model:
        cfg.asr.local.model = args.whisper_model
        cfg.asr.local.model_path = ""  # 换了模型就重新按名字解析
    if args.translate:
        cfg.translate.provider = args.translate
    if args.source_lang:
        cfg.asr.language = args.source_lang.lower()
    if args.target_lang:
        cfg.translate.target_language = args.target_lang
    if args.device:
        cfg.audio.device = args.device
    if args.wav:
        cfg.replay_wav = args.wav


def validate(cfg: Config) -> list[str]:
    """返回致命问题列表（空列表表示可以启动）。

    注意：识别部分**不因为"首选引擎没 Key"就拦截**——运行时会自动降级到另一个
    已配 Key 的引擎（云端服务会宕机，这是刻意设计的容错）。只有"一个识别引擎都
    用不了"才算致命。
    """
    problems: list[str] = []
    provider = (cfg.asr.provider or "").lower()

    known = (
        "paraformer",
        "dashscope",
        "aliyun",
        "deepgram",
        "openai",
        "openai_realtime",
        "local",
        "whisper",
        "faster_whisper",
        "mock",
    )
    if provider not in known:
        problems.append(f"未知识别引擎: {cfg.asr.provider}")
        return problems

    if provider in ("local", "whisper", "faster_whisper"):
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            problems.append(
                "本地识别需要 faster-whisper：python -m pip install faster-whisper modelscope "
                "-i https://pypi.tuna.tsinghua.edu.cn/simple"
            )
    elif provider != "mock":
        # 只要有一个识别引擎可用就行（首选不行会自动降级）
        available: list[str] = []
        if os.getenv("DASHSCOPE_API_KEY", "").strip():
            available.append("阿里云 Paraformer")
        if os.getenv("DEEPGRAM_API_KEY", "").strip():
            available.append("Deepgram")
        if not available:
            problems.append(
                f"识别引擎是 {cfg.asr.provider}，但没有可用的识别 Key。"
                "请在 .env 里填 DASHSCOPE_API_KEY（阿里云，国内推荐）或 DEEPGRAM_API_KEY，"
                "或者用 --asr local 走本地识别"
            )

    tprovider = (cfg.translate.provider or "").lower()
    if tprovider == "mock":
        return problems
    # 翻译同样支持降级：只要有一家配了 Key 就能启动
    chain = [p for p in cfg.translate_chain() if p != "mock"]
    if not chain:
        problems.append(
            f"翻译引擎 {cfg.translate.provider} 没有可用的 Key。"
            "请至少在 .env 里配置一个：DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / "
            "MOONSHOT_API_KEY / SILICONFLOW_API_KEY / ZHIPU_API_KEY"
        )
    return problems


def run_headless(pipeline: Pipeline, control: RunControl, state: SubtitleState, duration: float) -> None:
    """无界面模式：把字幕打到终端，便于压测与排错。"""
    last_sid = -1
    t0 = time.monotonic()
    while not control.stop.is_set():
        lines, status = state.snapshot()
        if lines:
            newest = lines[-1]
            if newest.sid != last_sid and (newest.source or newest.translated):
                if newest.final and newest.translated:
                    print(f"\n原文  : {newest.source}")
                    print(f"译文  : {newest.translated}   [{newest.e2e_ms:.0f}ms]" if newest.e2e_ms
                          else f"译文  : {newest.translated}")
                    last_sid = newest.sid
                elif not newest.final:
                    print(f"\r▸ {newest.source[:70]:<70}", end="", flush=True)
        if duration and time.monotonic() - t0 > duration:
            control.stop.set()
        time.sleep(0.1)


def run_cli(args: argparse.Namespace) -> int:
    """命令行路径：自检 / 无界面压测 / wav 回放调试。"""
    print(BANNER.format(ver=__version__))

    if args.check:
        audio_mod.probe_backends()
        cfg = load_config(args.config)
        apply_overrides(cfg, args)
        print("\n配置摘要：")
        print(describe(cfg))
        problems = validate(cfg)
        if problems:
            print("\n启动前需要处理的问题：")
            for p in problems:
                print(f"  [!] {p}")
        else:
            print("\n[OK] 配置看起来可以启动")
        return 0 if not problems else 1

    cfg = load_config(args.config)
    apply_overrides(cfg, args)

    problems = validate(cfg)
    if problems:
        print("启动失败，请先处理：", file=sys.stderr)
        for p in problems:
            print(f"  [!] {p}", file=sys.stderr)
        print("\n提示：想先看效果可以跑 `python main.py --asr mock --translate mock`", file=sys.stderr)
        return 2

    state = SubtitleState(max_lines=cfg.overlay.max_lines, history_lines=cfg.overlay.history_lines)
    control = RunControl()
    stats = LatencyStats()

    log_path = None
    if args.log:
        log_dir = Path("logs")
        log_path = log_dir / f"subtitle-{time.strftime('%Y%m%d-%H%M%S')}.jsonl" if args.log == "auto" else Path(args.log)

    print("配置摘要：")
    print(describe(cfg))
    print(f"界面模式 : {'无界面（终端输出）' if args.no_gui else '悬浮字幕窗'}")
    if log_path:
        print(f"字幕日志 : {log_path}")
    print("-" * 56)
    print("提示：悬浮窗默认「鼠标穿透」（不挡直播操作）。")
    print("      F1 切换穿透 / F2 暂停 / F3 行数 / F4 重连 / 双击切换穿透 / 拖动移动")
    print("      终端按 Ctrl+C 退出。")
    print("-" * 56)

    pipeline = Pipeline(
        cfg,
        state,
        control,
        stats,
        backend=args.backend,
        replay_wav=args.wav,
        log_path=log_path,
    )

    def pipeline_thread() -> None:
        try:
            asyncio.run(pipeline.run())
        except Exception as exc:
            print(f"\n[pipeline] 致命错误: {type(exc).__name__}: {exc}", file=sys.stderr)
            import traceback

            traceback.print_exc()
        finally:
            control.stop.set()

    worker = threading.Thread(target=pipeline_thread, name="pipeline", daemon=True)
    worker.start()

    def handle_sigint(signum, frame) -> None:  # noqa: ANN001
        print("\n收到中断，正在退出…")
        control.stop.set()

    try:
        signal.signal(signal.SIGINT, handle_sigint)
    except (ValueError, OSError):
        pass

    overlay = None
    try:
        if args.no_gui:
            run_headless(pipeline, control, state, args.duration)
        else:
            # 旧式命令行 + 悬浮窗：保留给调试用；正常使用请直接跑 `python main.py`
            # 走主控面板（有首次配置向导和启动/停止按钮）。
            from livesub.overlay import SubtitleOverlay

            overlay = SubtitleOverlay(cfg, state, on_quit=control.stop.set)
            if args.duration:

                def auto_stop() -> None:
                    time.sleep(args.duration)
                    overlay.request_action("quit")

                threading.Thread(target=auto_stop, daemon=True).start()
            overlay.run()
    except KeyboardInterrupt:
        pass
    finally:
        control.stop.set()
        worker.join(timeout=4.0)

    try:
        state.close_current()
    except Exception:
        pass

    print("\n" + stats.report())
    return 0


def run_gui() -> int:
    """启动主控面板（普通用户的入口）。"""
    from livesub.app import AppPanel

    panel = AppPanel()
    panel.run()
    return 0


def _log_path() -> Path:
    """崩溃日志写哪里。

    打包成 exe（--noconsole）后没有控制台，出错信息必须落盘，否则用户只会
    看到"双击没反应"，那是最难排查的情况。
    """
    try:
        log_dir = Path(os.getcwd()) / "logs"
    except Exception:  # pragma: no cover - 极端环境
        return Path("scystream_error.log")
    return log_dir / "error.log"


def _report_fatal(exc: BaseException) -> None:
    """把致命错误写到日志，并尽量弹个窗告诉用户。"""
    import traceback

    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n{'=' * 60}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n{detail}")
    except Exception:
        pass

    # 无控制台时，弹窗是唯一能让用户看到原因的方式
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "酸菜鱼翻译器 · 启动失败",
            f"{type(exc).__name__}: {exc}\n\n"
            f"详细信息已写入：\n{_log_path()}\n\n"
            "常见原因：缺少依赖（先跑一下 pip install -r requirements.txt）、"
            "杀毒软件拦截、或端口/权限问题。",
        )
        root.destroy()
    except Exception:
        pass

    # 有控制台时也打一份
    try:
        print(detail, file=sys.stderr)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 普通用户路径：不带任何参数 = 打开主控面板
    headless_intent = bool(args.no_gui or args.duration or args.wav or args.asr or args.check)
    if not headless_intent:
        print(BANNER.format(ver=__version__))
        try:
            return run_gui()
        except SystemExit:
            raise
        except BaseException as exc:  # 打包后没有控制台，必须落盘 + 弹窗
            _report_fatal(exc)
            return 1

    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
