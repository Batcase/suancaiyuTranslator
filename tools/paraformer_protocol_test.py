"""Paraformer（阿里云百炼）接入的离线自检。

不需要阿里云 Key：直接检验我们生成/解析的报文是否符合官方协议。
这类"协议层"错误如果只靠真机联调发现，排查成本很高。

用法: python tools\paraformer_protocol_test.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livesub.asr import ParaformerAsr, language_hints_for, paraformer_ws_url  # noqa: E402
from livesub.config import load_config  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        FAILED.append(label)


def main() -> int:
    cfg = load_config()
    cfg.asr.provider = "paraformer"
    cfg.asr.language = "ja"
    cfg.asr.paraformer.api_key = "sk-test-key-for-protocol-check"

    print("=" * 62)
    print("Paraformer 协议自检（离线，不联网）")
    print("=" * 62)

    # ---- 1. 接入地址 ----
    print("\n[1] 接入地址")
    url = paraformer_ws_url(cfg)
    check(url == "wss://dashscope.aliyuncs.com/api-ws/v1/inference", f"默认域名: {url}")
    cfg.asr.paraformer.workspace_id = "ws123"
    check(
        paraformer_ws_url(cfg) == "wss://ws123.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference",
        "填了 workspace_id 时切换到专属域名",
    )
    cfg.asr.paraformer.workspace_id = ""

    # ---- 2. 语种映射 ----
    print("\n[2] 语种映射")
    for code, expect in (("ja", ["ja"]), ("en", ["en"]), ("zh", ["zh"]), ("auto", []), ("xx", [])):
        got = language_hints_for(code)
        check(got == expect, f"language={code:5} -> hints={got}")

    # ---- 3. run-task 报文 ----
    print("\n[3] run-task 报文（官方要求的字段）")
    asr = ParaformerAsr(cfg, 16000)
    payload = asr._run_task_payload(language_hints_for("ja"))
    header = payload["header"]
    body = payload["payload"]
    params = body["parameters"]

    check(header["action"] == "run-task", "header.action = run-task")
    check(header["streaming"] == "duplex", "header.streaming = duplex")
    check(len(header["task_id"]) == 36, f"task_id 是 UUID（{header['task_id']}）")
    check(body["task_group"] == "audio", "payload.task_group = audio")
    check(body["task"] == "asr", "payload.task = asr")
    check(body["function"] == "recognition", "payload.function = recognition")
    check(body["model"] == "paraformer-realtime-v2", f"model = {body['model']}")
    check(body["input"] == {}, "payload.input = {}")
    check(params["format"] == "pcm", "parameters.format = pcm")
    check(params["sample_rate"] == 16000, "parameters.sample_rate = 16000")
    check(params["language_hints"] == ["ja"], "parameters.language_hints = ['ja']")
    # 不写死数值——阈值可配，且随"等断句整句翻"的策略调整过（500 → 700）。
    # 这里只验证"配置里的值被正确带进了报文"。
    expected_silence = cfg.asr.paraformer.max_sentence_silence
    check(
        params["max_sentence_silence"] == expected_silence,
        f"parameters.max_sentence_silence = {expected_silence}（与配置一致）",
    )
    check(params["heartbeat"] is True, "parameters.heartbeat = true")
    check(
        "max_sentence_silence" in params,
        "VAD 断句（semantic_punctuation_enabled=false）下才带 max_sentence_silence",
    )
    # 语义断句开启时不应带该参数
    cfg.asr.paraformer.semantic_punctuation_enabled = True
    asr2 = ParaformerAsr(cfg, 16000)
    check(
        "max_sentence_silence" not in asr2._run_task_payload(["ja"])["payload"]["parameters"],
        "语义断句开启时不带 max_sentence_silence（官方约束）",
    )
    cfg.asr.paraformer.semantic_punctuation_enabled = False
    check(json.dumps(payload, ensure_ascii=False) is not None, "报文可 JSON 序列化")

    # ---- 4. 服务端事件解析 ----
    print("\n[4] 服务端事件解析")
    asr = ParaformerAsr(cfg, 16000)
    # 模拟已推入 2 秒音频
    asr.send_audio(b"\x00\x00" * 16000, 1000.0)

    started = asr._parse({"header": {"event": "task-started", "task_id": asr._task_id}, "payload": {}})
    check(started == [], "task-started 不上抛事件（只表示可以发音频了）")

    partial = asr._parse(
        {
            "header": {"event": "result-generated", "task_id": asr._task_id},
            "payload": {
                "output": {
                    "sentence": {
                        "text": "今日は皆さん",
                        "sentence_end": False,
                        "begin_time": 100,
                        "end_time": 800,
                        "heartbeat": False,
                    }
                },
                "usage": None,
            },
        }
    )
    check(len(partial) == 1, "中间结果返回 1 个事件")
    check(partial[0].kind == "partial", "sentence_end=false -> partial")
    check(partial[0].text == "今日は皆さん", "文本正确")
    check(not partial[0].is_utterance_end, "不是句尾")

    final = asr._parse(
        {
            "header": {"event": "result-generated", "task_id": asr._task_id},
            "payload": {
                "output": {
                    "sentence": {
                        "text": "新しいゲームを紹介したいと思います。",
                        "sentence_end": True,
                        "begin_time": 800,
                        "end_time": 2000,
                        "heartbeat": False,
                    }
                },
                "usage": {"duration": 2},
            },
        }
    )
    check(len(final) == 1 and final[0].kind == "final", "sentence_end=true -> final")
    check(final[0].is_utterance_end, "标记为句尾")
    check(asr.task_duration_s == 2.0, f"记录计费时长 = {asr.task_duration_s}s")

    # 时间戳：end_time=2000ms，音频起点 1000.0s -> 应当是 1002.0s
    check(
        abs(final[0].audio_captured_at - 1002.0) < 0.01,
        f"用 end_time 定位播放时刻（得到 {final[0].audio_captured_at:.2f}，期望 1002.00）",
    )

    hb = asr._parse(
        {
            "header": {"event": "result-generated"},
            "payload": {"output": {"sentence": {"text": "", "heartbeat": True, "sentence_end": False}}},
        }
    )
    check(hb == [], "心跳包被跳过")

    failed = asr._parse(
        {
            "header": {
                "event": "task-failed",
                "error_code": "CLIENT_ERROR",
                "error_message": "request timeout after 23 seconds.",
            },
            "payload": {},
        }
    )
    check(len(failed) == 1 and failed[0].kind == "error", "task-failed 转成 error 事件")
    check("23 秒" in failed[0].text or "timeout" in failed[0].text, "错误信息里有可读提示")

    check(asr._parse({"header": {"event": "task-finished"}, "payload": {}}) == [], "task-finished 不上抛")

    print("\n" + "=" * 62)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
    else:
        print("全部通过（协议层无误；真机联调待填入阿里云 Key 后进行）")
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
