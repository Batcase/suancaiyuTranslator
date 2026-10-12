"""首次配置向导的端到端自检。

真的走一遍：删除 .env -> 启动面板应进入向导 -> 填入 Key -> 点「验证并保存」
-> 等验证通过 -> 确认已写入 .env 并进入主控界面 -> 还原现场。

用法: python tools\wizard_test.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livesub import keystore  # noqa: E402
from livesub.app import AppPanel  # noqa: E402
from livesub.config import load_config  # noqa: E402

ENV = keystore.ENV_PATH
BACKUP = ENV.with_suffix(".env.bak-test")
TOML = keystore.TOML_PATH
TOML_BACKUP = TOML.with_suffix(".toml.bak-test")

FAILED: list[str] = []
SAVED_KEYS: dict[str, str] = {}


def check(cond: bool, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        FAILED.append(label)


def main() -> int:
    print("=" * 60)
    print("首次配置向导自检")
    print("=" * 60)

    # 这个自检要真的点「测试」并**验证通过**，所以必须要真实的 Key。
    # 注意：它会在你配置好的 .env 上操作（先备份、测完还原），并且会真的调几次接口。
    #
    # 没有真实 Key 时**明确跳过**，而不是假装通过 —— 假 Key 到不了"校验通过"这一步。
    if not ENV.exists():
        print("没有 .env，无法测试向导")
        print("跳过：这个自检需要真实的 API Key（它要验证「测试」按钮能通过）")
        return 0
    keys = keystore.read_env()
    missing = []
    if not keys.get("DEEPSEEK_API_KEY"):
        missing.append("DEEPSEEK_API_KEY（翻译）")
    if not (keys.get("DASHSCOPE_API_KEY") or keys.get("DEEPGRAM_API_KEY")):
        missing.append("DASHSCOPE_API_KEY 或 DEEPGRAM_API_KEY（识别）")
    if missing:
        print("缺少：" + "、".join(missing))
        print("跳过：这个自检需要真实的 API Key（它要验证「测试」按钮能通过）")
        print("      只验证 Key 格式校验（不联网）请跑 tools\\validate_format_test.py")
        return 0
    for name in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "DEEPGRAM_API_KEY"):
        if keys.get(name):
            SAVED_KEYS[name] = keys[name]

    # 备份现场，然后移除 .env 模拟"全新安装"
    shutil.copy2(ENV, BACKUP)
    ENV.unlink()
    if TOML.exists():
        shutil.copy2(TOML, TOML_BACKUP)
    print(f"已备份并移除 {ENV.name}，模拟首次启动\n")

    panel = AppPanel()
    t0 = time.monotonic()
    state = {"stage": "start"}

    def log(msg: str) -> None:
        print(f"[{(time.monotonic()-t0):5.1f}s] {msg}")

    def step_initial() -> None:
        log("检查是否进入配置向导")
        check(panel.screen_setup.winfo_ismapped(), "没有 Key 时自动显示向导")
        check(not panel.screen_main.winfo_ismapped(), "主控界面此时不显示")
        # 填入之前保存的 Key
        panel.e_setup_deepseek.insert(0, SAVED_KEYS["DEEPSEEK_API_KEY"])
        if SAVED_KEYS.get("DASHSCOPE_API_KEY"):
            panel.e_setup_dashscope.insert(0, SAVED_KEYS["DASHSCOPE_API_KEY"])
        if SAVED_KEYS.get("DEEPGRAM_API_KEY"):
            panel.e_setup_deepgram.insert(0, SAVED_KEYS["DEEPGRAM_API_KEY"])
        log("已填入翻译 Key 与识别 Key，测试翻译 Key")
        panel._test_key("deepseek", "setup")
        state["stage"] = "validating_one"

    def poll_one() -> None:
        if state["stage"] != "validating_one":
            panel.root.after(200, poll_one)
            return
        label = panel.l_setup_deepseek["text"]
        if label.startswith(("✓", "✗")):
            log(f"单个校验结果: {label[:60]}")
            check(label.startswith("✓"), "点「测试」能给出校验结论")
            # 选一个识别引擎来测（优先阿里云）
            engine = "dashscope" if SAVED_KEYS.get("DASHSCOPE_API_KEY") else "deepgram"
            state["engine"] = engine
            log(f"接着校验识别 Key: {engine}")
            panel._test_key(engine, "setup")
            state["stage"] = "validating_two"
            state["ticks"] = 0
            return
        state["ticks"] = state.get("ticks", 0) + 1
        if state["ticks"] % 25 == 0:
            log(f"等待翻译 Key 校验结论… ({label[:40]})")
        panel.root.after(200, poll_one)

    def poll_two() -> None:
        if state["stage"] != "validating_two":
            # 注意：stage 还没切过来时也必须继续排期，否则轮询链会断掉
            panel.root.after(200, poll_two)
            return
        engine = state["engine"]
        label = getattr(panel, f"l_setup_{engine}")["text"]
        if label.startswith(("✓", "✗")):
            log(f"识别 Key 校验结果: {label[:70]}")
            check(label.startswith("✓"), "识别 Key 校验通过")
            panel._save_from_setup()
            state["stage"] = "saving"
            state["ticks2"] = 0
            return
        state["ticks2"] = state.get("ticks2", 0) + 1
        if state["ticks2"] % 25 == 0:
            log(f"等待识别 Key 校验结论… ({label[:40]})")
        panel.root.after(200, poll_two)

    def poll_saved() -> None:
        if state["stage"] != "saving":
            panel.root.after(250, poll_saved)
            return
        if panel.screen_main.winfo_ismapped():
            log("已进入主控界面")
            check(True, "保存后自动进入主控界面")
            # 校验 .env 落盘
            env = keystore.read_env()
            check(env.get("DEEPSEEK_API_KEY") == SAVED_KEYS["DEEPSEEK_API_KEY"], "翻译 Key 已写入 .env")
            engine = state["engine"]
            env_name = {"dashscope": "DASHSCOPE_API_KEY", "deepgram": "DEEPGRAM_API_KEY"}[engine]
            check(env.get(env_name) == SAVED_KEYS[env_name], f"识别 Key({engine}) 已写入 .env")
            check(TOML.exists(), "config.toml 已生成")
            # 引擎应选到刚验证通过的那家
            cfg = load_config()
            expected = "paraformer" if engine == "dashscope" else "deepgram"
            check(cfg.asr.provider == expected, f"首选引擎为 {expected}（实际 {cfg.asr.provider}）")
            check(cfg.asr.language == "ja", f"源语言已保存为日语（实际 {cfg.asr.language}）")
            # 模拟"重启"：新建一个面板，应当直接进主控
            log("模拟重启（重新创建面板）")
            p2 = AppPanel()
            p2.root.update()
            check(p2.screen_main.winfo_ismapped(), "重启后自动读取配置，直接进主控界面")
            check(
                keystore.mask_key(keystore.get_key("deepseek")).startswith("sk-"),
                "重启后能读到已保存的 Key",
            )
            p2.root.destroy()
            state["stage"] = "done"
            panel.root.quit()
            return
        panel.root.after(250, poll_saved)

    panel.root.after(300, step_initial)
    panel.root.after(400, poll_one)
    panel.root.after(400, poll_two)
    panel.root.after(600, poll_saved)
    # 兜底：最多等 60 秒
    panel.root.after(60000, panel.root.quit)
    panel.root.mainloop()

    # 还原现场。
    # 注意：如果测试前**本来就没有** config.toml，测试生成的必须删掉——
    # 否则会留下一个"测试残留配置"，之后所有运行都被它覆盖（默认值改了也不生效，
    # 这个坑实际踩过：改小了字号但界面没变，因为残留 config.toml 里还是旧值）。
    shutil.copy2(BACKUP, ENV)
    BACKUP.unlink()
    if TOML_BACKUP.exists():
        shutil.copy2(TOML_BACKUP, TOML)
        TOML_BACKUP.unlink()
        print(f"\n已还原原有 {ENV.name} / config.toml")
    else:
        if TOML.exists():
            TOML.unlink()
        print(f"\n已还原原有 {ENV.name}；测试生成的 config.toml 已删除（测试前不存在）")

    print("=" * 60)
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
