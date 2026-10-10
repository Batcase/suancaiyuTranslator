"""主控面板：首次配置向导 + 启动/停止翻译 + 设置。

设计要点
--------
* **一个根窗三种界面**（向导 / 主控 / 设置），切换靠隐藏与显示 Frame，
  不反复销毁窗口——tkinter 反复建销窗口容易留下事件循环残留。
* **首次使用强制走向导**：填 Key → 真实验证 → 通过才保存。验证失败的 Key
  绝不会写进 `.env`，避免"存了个坏 Key，下次启动一脸懵"。
* **主控面板持有 Tk 根窗与事件循环**，悬浮窗（SubtitleOverlay）是可插拔的
  子窗口；点"停止"只销毁悬浮窗并停掉管线，面板本身继续留着。
* 管线跑在后台线程（asyncio），界面状态靠 `SubtitleState` + `after()` 轮询，
  绝不跨线程碰 tkinter 控件。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from . import keystore
from .config import Config, load_config
from .state import SubtitleState
from .stats import LatencyStats

# ---- 配色（和悬浮窗保持一致的深色风格）----
BG = "#15181C"
CARD = "#1E2227"
FG = "#E8EAED"
FG_DIM = "#9AA0A6"
ACCENT = "#4C8DFF"
OK = "#3DD68C"
WARN = "#FFB020"
ERR = "#FF6B6B"

#: 识别/翻译引擎下拉项（顺序 = 降级优先级；第一项是默认）
ENGINE_CHOICES: list[tuple[str, str]] = list(keystore.ASR_PROVIDER_LABELS)
TRANSLATE_CHOICES: list[tuple[str, str]] = list(keystore.TRANSLATE_PROVIDER_LABELS)
ASR_BY_ID: dict[str, dict] = keystore.ASR_BY_ID
TRANSLATE_BY_ID: dict[str, dict] = keystore.TRANSLATE_BY_ID

#: 设置页里要展示的 Key（按 provider 表推导，加新厂商不用改界面）
ALL_KEY_FIELDS: list[tuple[str, str]] = []
_seen_keys: set[str] = set()
for _conf in list(keystore.ASR_PROVIDERS) + list(keystore.TRANSLATE_PROVIDERS):
    _k = _conf.get("key")
    if _k and _k not in _seen_keys:
        _seen_keys.add(_k)
        ALL_KEY_FIELDS.append((_k, _conf["label"].split(" · ")[0]))

#: 各引擎的说明（界面里给用户看清楚"为什么要多配一家"）
ENGINE_NOTES: dict[str, str] = {
    "paraformer": "阿里云百炼 · 国内直连、稳定，日语/英语/中文/韩语都支持",
    "deepgram": "Deepgram · 海外服务，国内网络有时连不上，可作备用",
    "local": "本地 Whisper · 免费离线，不需要 Key，但慢约 5 倍",
}

LANGUAGES = [
    ("ja", "日语"),
    ("en", "英语"),
    ("ko", "韩语"),
    ("zh", "中文"),
    ("yue", "粤语"),
    ("auto", "自动识别"),
]

TARGETS = ["简体中文", "繁體中文", "English", "日本語"]

#: 需要填 Key 的识别引擎（local 不需要）
ASR_KEY_ENGINES = [("dashscope", "阿里云 Paraformer（推荐）"), ("deepgram", "Deepgram（备用）")]


class AppPanel:
    """主控面板。"""

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or load_config()
        self.root = tk.Tk()
        self.root.title("酸菜鱼翻译器")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 管线与悬浮窗
        self.state = SubtitleState(max_lines=self.cfg.overlay.max_lines)
        self.stats = LatencyStats()
        self.control = None          # RunControl，由 Pipeline 使用
        self.pipeline = None
        self.worker: threading.Thread | None = None
        self.overlay = None
        self.running = False

        self._ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._validation: dict[str, object] = {}

        self._init_fonts()
        self._init_style()

        self.container = tk.Frame(self.root, bg=BG)
        self.container.pack(fill="both", expand=True)

        self.screen_setup = self._build_setup_screen()
        self.screen_main = self._build_main_screen()
        self.screen_settings = self._build_settings_screen()

        # 判断首次启动：没有可用的翻译 Key 就走向导
        if keystore.has_all_keys(("deepseek",)):
            self.show_main()
        else:
            self.show_setup()

        self.root.after(100, self._pump_ui_queue)
        self.root.after(200, self._refresh_status)

    # ==================================================================
    # 基础设施
    # ==================================================================
    def _init_fonts(self) -> None:
        available = set(tkfont.families())
        family = next(
            (f for f in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI") if f in available),
            "TkDefaultFont",
        )
        self.f_title = tkfont.Font(family=family, size=17, weight="bold")
        self.f_h2 = tkfont.Font(family=family, size=12, weight="bold")
        self.f_body = tkfont.Font(family=family, size=10)
        self.f_small = tkfont.Font(family=family, size=9)
        self.f_mono = tkfont.Font(family="Consolas", size=9) if "Consolas" in available else self.f_small
        self.f_big = tkfont.Font(family=family, size=22, weight="bold")

    def _init_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=FG)
        style.configure("TCheckbutton", background=BG, foreground=FG)

    def _card(self, parent, **kw) -> tk.Frame:
        f = tk.Frame(parent, bg=CARD, **kw)
        return f

    def _label(self, parent, text: str, font=None, fg=FG, bg=None, **kw) -> tk.Label:
        return tk.Label(parent, text=text, font=font or self.f_body, fg=fg, bg=bg or BG, **kw)

    def _button(self, parent, text: str, command, *, kind: str = "normal", width: int = 0) -> tk.Button:
        colors = {
            "normal": (CARD, FG),
            "primary": (ACCENT, "#FFFFFF"),
            "danger": ("#3A2224", ERR),
            "ok": ("#1E3A2C", OK),
        }[kind]
        kw = {"width": width} if width else {}
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=self.f_body,
            bg=colors[0],
            fg=colors[1],
            activebackground=colors[0],
            activeforeground=colors[1],
            relief="flat",
            bd=0,
            padx=14,
            pady=7,
            cursor="hand2",
            **kw,
        )

    def _entry(self, parent, show: str = "") -> tk.Entry:
        return tk.Entry(
            parent,
            font=self.f_mono,
            bg="#0F1216",
            fg=FG,
            insertbackground=FG,
            relief="flat",
            show=show,
            bd=6,
        )

    # ==================================================================
    # 界面：首次配置向导
    # ==================================================================
    def _build_setup_screen(self) -> tk.Frame:
        f = tk.Frame(self.container, bg=BG)
        pad = {"padx": 22}

        self._label(f, "欢迎使用 酸菜鱼翻译器", self.f_title, **pad).pack(anchor="w", pady=(20, 2))
        self._label(
            f,
            "直播实时翻译 · 首次使用请填入 API Key（只保存一次，以后自动读取）",
            self.f_small,
            FG_DIM,
            **pad,
        ).pack(anchor="w", pady=(0, 14))

        # —— 翻译 Key（必需）——
        card1 = self._card(f)
        card1.pack(fill="x", **pad, pady=(0, 10))
        self._label(card1, "① 翻译 Key（必需）", self.f_h2).pack(anchor="w", padx=14, pady=(12, 2))
        self._label(
            card1,
            "DeepSeek：platform.deepseek.com 注册后在「API Keys」里创建",
            self.f_small,
            FG_DIM,
        ).pack(anchor="w", padx=14, pady=(0, 8))
        row1 = tk.Frame(card1, bg=CARD)
        row1.pack(fill="x", padx=14, pady=(0, 4))
        self.e_setup_deepseek = self._entry(row1)
        self.e_setup_deepseek.pack(side="left", fill="x", expand=True)
        self.b_setup_deepseek = self._button(row1, "测试", lambda: self._test_key("deepseek", "setup"), width=7)
        self.b_setup_deepseek.pack(side="left", padx=(8, 0))
        self.l_setup_deepseek = self._label(card1, "未测试", self.f_small, FG_DIM)
        self.l_setup_deepseek.pack(anchor="w", padx=14, pady=(0, 12))

        # —— 主识别 Key ——
        card2 = self._card(f)
        card2.pack(fill="x", **pad, pady=(0, 8))
        self._label(card2, "② 语音识别 Key（推荐阿里云，国内直连）", self.f_h2).pack(
            anchor="w", padx=14, pady=(12, 2)
        )
        self._label(
            card2,
            "阿里云百炼：bailian.console.aliyun.com 开通后创建 API Key（有免费额度）",
            self.f_small,
            FG_DIM,
        ).pack(anchor="w", padx=14, pady=(0, 8))
        row2 = tk.Frame(card2, bg=CARD)
        row2.pack(fill="x", padx=14, pady=(0, 4))
        self.e_setup_dashscope = self._entry(row2)
        self.e_setup_dashscope.pack(side="left", fill="x", expand=True)
        self.b_setup_dashscope = self._button(
            row2, "测试", lambda: self._test_key("dashscope", "setup"), width=7
        )
        self.b_setup_dashscope.pack(side="left", padx=(8, 0))
        self.l_setup_dashscope = self._label(card2, "未测试", self.f_small, FG_DIM)
        self.l_setup_dashscope.pack(anchor="w", padx=14, pady=(0, 8))

        # —— 备用识别 Key（可留空，但强烈建议配一个）——
        self._label(
            card2,
            "③ 备用识别 Key（可留空。云端服务会宕机，多配一家就不会中断）",
            self.f_h2,
        ).pack(anchor="w", padx=14, pady=(4, 2))
        self._label(
            card2,
            "Deepgram：console.deepgram.com 注册送额度（注意国内网络可能连不上）",
            self.f_small,
            FG_DIM,
        ).pack(anchor="w", padx=14, pady=(0, 8))
        row3 = tk.Frame(card2, bg=CARD)
        row3.pack(fill="x", padx=14, pady=(0, 4))
        self.e_setup_deepgram = self._entry(row3)
        self.e_setup_deepgram.pack(side="left", fill="x", expand=True)
        self.b_setup_deepgram = self._button(row3, "测试", lambda: self._test_key("deepgram", "setup"), width=7)
        self.b_setup_deepgram.pack(side="left", padx=(8, 0))
        self.l_setup_deepgram = self._label(card2, "未测试（留空表示不用它）", self.f_small, FG_DIM)
        self.l_setup_deepgram.pack(anchor="w", padx=14, pady=(0, 8))

        # —— 语言 ——
        lang_row = tk.Frame(card2, bg=CARD)
        lang_row.pack(fill="x", padx=14, pady=(0, 6))
        self._label(lang_row, "这场直播主要是：", self.f_small, FG_DIM, CARD).pack(side="left")
        self.cb_setup_source = ttk.Combobox(
            lang_row, values=[f"{n}" for _c, n in LANGUAGES], state="readonly", width=10
        )
        self.cb_setup_source.set("日语")
        self.cb_setup_source.pack(side="left", padx=(6, 12))
        self._label(lang_row, "翻译成：", self.f_small, FG_DIM, CARD).pack(side="left")
        self.cb_setup_target = ttk.Combobox(lang_row, values=TARGETS, state="readonly", width=10)
        self.cb_setup_target.set("简体中文")
        self.cb_setup_target.pack(side="left", padx=(6, 0))
        self._label(
            card2,
            "选好语言后，软件会提前告诉翻译 AI「主要语言是什么」，识别与翻译都更准。",
            self.f_small,
            FG_DIM,
        ).pack(anchor="w", padx=14, pady=(0, 6))

        self.v_local = tk.BooleanVar(value=False)
        tk.Checkbutton(
            card2,
            text="一个 Key 都不想填：改用本地识别（免费，但慢约 5 倍、准确率较低）",
            variable=self.v_local,
            font=self.f_small,
            fg=FG_DIM,
            bg=CARD,
            selectcolor=CARD,
            activebackground=CARD,
            activeforeground=FG,
            command=self._on_local_toggle,
        ).pack(anchor="w", padx=10, pady=(0, 12))

        # —— 底部操作 ——
        bottom = tk.Frame(f, bg=BG)
        bottom.pack(fill="x", **pad, pady=(4, 18))
        self.l_setup_hint = self._label(bottom, "填好后点「验证并保存」", self.f_small, FG_DIM)
        self.l_setup_hint.pack(anchor="w", pady=(0, 8))
        self.b_setup_save = self._button(bottom, "验证并保存", self._save_from_setup, kind="primary")
        self.b_setup_save.pack(anchor="w")
        return f

    # ==================================================================
    # 界面：主控
    # ==================================================================
    def _build_main_screen(self) -> tk.Frame:
        f = tk.Frame(self.container, bg=BG)
        pad = {"padx": 22}

        head = tk.Frame(f, bg=BG)
        head.pack(fill="x", **pad, pady=(18, 4))
        self._label(head, "酸菜鱼翻译器", self.f_title).pack(side="left")
        self._button(head, "设置", self.show_settings, width=7).pack(side="right")

        self.l_state = self._label(f, "未启动", self.f_h2, FG_DIM, **pad)
        self.l_state.pack(anchor="w")

        # 状态卡
        card = self._card(f)
        card.pack(fill="x", **pad, pady=(12, 10))
        self.l_last_src = self._label(card, "—", self.f_body, FG, CARD, anchor="w", justify="left")
        self.l_last_src.pack(fill="x", padx=14, pady=(12, 2))
        self.l_last_dst = self._label(card, "", self.f_h2, FG, CARD, anchor="w", justify="left")
        self.l_last_dst.pack(fill="x", padx=14, pady=(0, 12))

        # 路由信息
        info = self._card(f)
        info.pack(fill="x", **pad, pady=(0, 10))
        self.l_route = self._label(info, "", self.f_small, FG_DIM, CARD, anchor="w", justify="left")
        self.l_route.pack(fill="x", padx=14, pady=12)

        # 启动/停止
        btns = tk.Frame(f, bg=BG)
        btns.pack(fill="x", **pad, pady=(0, 6))
        self.b_start = self._button(btns, "▶  启动翻译", self.start_translation, kind="primary")
        self.b_start.pack(side="left", fill="x", expand=True, ipady=6)
        self.b_stop = self._button(btns, "■  停止翻译", self.stop_translation, kind="danger")
        self.b_stop.pack(side="left", fill="x", expand=True, padx=(10, 0), ipady=6)
        self.b_stop.configure(state="disabled")

        self.l_hint = self._label(f, "提示：字幕窗默认鼠标穿透（不挡直播操作）。F1 切换穿透 / F2 暂停 / 拖动移动。", self.f_small, FG_DIM, **pad, justify="left")
        self.l_hint.pack(anchor="w", pady=(8, 0))

        logrow = tk.Frame(f, bg=BG)
        logrow.pack(fill="x", **pad, pady=(10, 18))
        self._button(logrow, "打开配置文件夹", self._open_data_dir, width=16).pack(side="left")
        self._button(logrow, "查看字幕日志", self._open_logs, width=14).pack(side="left", padx=(8, 0))
        return f

    # ==================================================================
    # 界面：设置
    # ==================================================================
    def _build_settings_screen(self) -> tk.Frame:
        """设置页：可滚动（provider 变多，一屏放不下）。"""
        outer = tk.Frame(self.container, bg=BG)

        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x", padx=22, pady=(16, 8))
        self._label(head, "设置", self.f_title).pack(side="left")
        self._button(head, "返回", self.show_main, width=7).pack(side="right")
        self._label(
            head, "可多家都填，宕机自动切换", self.f_small, FG_DIM
        ).pack(side="right", padx=(0, 12))

        # —— 可滚动区域 ——
        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, bg=BG, highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        # 鼠标滚轮（Windows）
        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )
        self._settings_canvas = canvas
        f = inner
        pad = {"padx": 18}

        # ---- 首选引擎 ----
        card0 = self._card(f)
        card0.pack(fill="x", **pad, pady=(0, 10))
        self._label(card0, "首选引擎", self.f_h2, FG, CARD).pack(anchor="w", padx=14, pady=(12, 6))
        r_eng = tk.Frame(card0, bg=CARD)
        r_eng.pack(fill="x", padx=14, pady=(0, 4))
        self._label(r_eng, "语音识别", self.f_small, FG_DIM, CARD, width=12, anchor="w").pack(side="left")
        self.cb_engine = ttk.Combobox(
            r_eng, values=[label for _p, label in ENGINE_CHOICES], state="readonly", width=34
        )
        self.cb_engine.pack(side="left")
        r_tr = tk.Frame(card0, bg=CARD)
        r_tr.pack(fill="x", padx=14, pady=(0, 10))
        self._label(r_tr, "翻译", self.f_small, FG_DIM, CARD, width=12, anchor="w").pack(side="left")
        self.cb_translate = ttk.Combobox(
            r_tr, values=[label for _p, label in TRANSLATE_CHOICES], state="readonly", width=34
        )
        self.cb_translate.pack(side="left")

        # ---- 识别 Key ----
        card1 = self._card(f)
        card1.pack(fill="x", **pad, pady=(0, 10))
        self._label(card1, "语音识别 Key", self.f_h2, FG, CARD).pack(
            anchor="w", padx=14, pady=(12, 4)
        )
        self._label(
            card1,
            "填了哪几家就能用哪几家；首选连不上会自动降级。本地识别不需要 Key。",
            self.f_small,
            FG_DIM,
            CARD,
        ).pack(anchor="w", padx=14, pady=(0, 4))
        for pid, _label in ENGINE_CHOICES:
            conf = ASR_BY_ID.get(pid)
            if not conf or not conf["key"]:
                continue
            self._key_row(card1, conf["key"], f"{conf['label'].split(' · ')[0]}", conf["apply"])

        # ---- 翻译 Key ----
        card2 = self._card(f)
        card2.pack(fill="x", **pad, pady=(0, 10))
        self._label(card2, "翻译 Key", self.f_h2, FG, CARD).pack(anchor="w", padx=14, pady=(12, 4))
        self._label(
            card2,
            "同样是填了哪家就能用哪家。留空 = 不修改已保存的。",
            self.f_small,
            FG_DIM,
            CARD,
        ).pack(anchor="w", padx=14, pady=(0, 4))
        for pid, _label in TRANSLATE_CHOICES:
            conf = TRANSLATE_BY_ID.get(pid)
            if not conf:
                continue
            self._key_row(card2, conf["key"], conf["label"].split(" · ")[0], conf["apply"])

        self.l_set_hint = self._label(f, "未测试", self.f_small, FG_DIM, justify="left")
        self.l_set_hint.pack(anchor="w", padx=18, pady=(2, 8))

        # ---- 语言 ----
        card3 = self._card(f)
        card3.pack(fill="x", **pad, pady=(0, 10))
        self._label(card3, "翻译方向", self.f_h2, FG, CARD).pack(anchor="w", padx=14, pady=(12, 6))
        r1 = tk.Frame(card3, bg=CARD)
        r1.pack(fill="x", padx=14, pady=(0, 6))
        self._label(r1, "源语言", self.f_small, FG_DIM, CARD, width=12, anchor="w").pack(side="left")
        self.cb_source = ttk.Combobox(
            r1, values=[n for _c, n in LANGUAGES], state="readonly", width=34
        )
        self.cb_source.pack(side="left")
        r2 = tk.Frame(card3, bg=CARD)
        r2.pack(fill="x", padx=14, pady=(0, 6))
        self._label(r2, "目标语言", self.f_small, FG_DIM, CARD, width=12, anchor="w").pack(side="left")
        self.cb_target = ttk.Combobox(r2, values=TARGETS, state="readonly", width=34)
        self.cb_target.pack(side="left")
        self.v_hint = tk.BooleanVar(value=self.cfg.translate.send_language_hint)
        tk.Checkbutton(
            card3,
            text="提前告诉翻译 AI「主要语言」（减少外来语/专有名词被误判）",
            variable=self.v_hint,
            font=self.f_small,
            fg=FG_DIM,
            bg=CARD,
            selectcolor=CARD,
            activebackground=CARD,
            activeforeground=FG,
        ).pack(anchor="w", padx=10, pady=(0, 10))

        # ---- 字幕窗 ----
        card4 = self._card(f)
        card4.pack(fill="x", **pad, pady=(0, 10))
        self._label(card4, "字幕窗", self.f_h2, FG, CARD).pack(anchor="w", padx=14, pady=(12, 6))
        self.v_panel = tk.BooleanVar(value=self.cfg.overlay.background_panel)
        tk.Checkbutton(
            card4, text="字幕后面加一层深色底板（更清晰，但会遮一点画面）",
            variable=self.v_panel, font=self.f_small, fg=FG_DIM, bg=CARD, selectcolor=CARD,
            activebackground=CARD, activeforeground=FG,
        ).pack(anchor="w", padx=10)
        self.v_click = tk.BooleanVar(value=self.cfg.overlay.click_through)
        tk.Checkbutton(
            card4, text="鼠标穿透（开启后不挡直播界面操作）",
            variable=self.v_click, font=self.f_small, fg=FG_DIM, bg=CARD, selectcolor=CARD,
            activebackground=CARD, activeforeground=FG,
        ).pack(anchor="w", padx=10, pady=(0, 12))

        save = self._button(f, "保存设置", self._save_from_settings, kind="primary")
        save.pack(anchor="w", padx=18, pady=(0, 18))

        # 脱敏显示当前已保存的 Key（放在最下面，滚动到底可见）
        self.l_key_status = self._label(f, "", self.f_small, FG_DIM, justify="left")
        self.l_key_status.pack(anchor="w", padx=18, pady=(0, 16))

        self._sync_settings_fields()
        return outer

    def _key_row(self, parent: tk.Frame, key_name: str, label: str, apply_url: str = "") -> None:
        """一行 Key 输入 + 测试按钮（自动按 key_name 生成属性名）。"""
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", padx=14, pady=(6, 0))
        text = f"{label}"
        self._label(row, text, self.f_small, FG_DIM, CARD, width=22, anchor="w").pack(side="left")
        entry = self._entry(row)
        entry.pack(side="left", fill="x", expand=True)
        btn = self._button(row, "测试", lambda n=key_name: self._test_key(n, "settings"), width=6)
        btn.pack(side="left", padx=(8, 0))
        setattr(self, f"e_set_{key_name}", entry)
        setattr(self, f"b_set_{key_name}", btn)
        status = self._label(row, "", self.f_small, FG_DIM, CARD, anchor="w", width=10)
        status.pack(side="left", padx=(8, 0))
        setattr(self, f"l_set_{key_name}", status)
        if apply_url:
            self._label(parent, f"    申请：{apply_url}", self.f_small, "#6B7280", CARD).pack(
                anchor="w", padx=14
            )

    # ==================================================================
    # 界面切换
    # ==================================================================
    def _show(self, frame: tk.Frame) -> None:
        for child in (self.screen_setup, self.screen_main, self.screen_settings):
            child.pack_forget()
        frame.pack(fill="both", expand=True)
        self._autosize()

    def show_setup(self) -> None:
        self._show(self.screen_setup)
        # 已有的 Key 预填进去（重装/改配置时不用重新找）
        for name in ("deepseek", "dashscope", "deepgram"):
            entry = getattr(self, f"e_setup_{name}")
            entry.delete(0, "end")
            existing = keystore.get_key(name)
            if existing:
                entry.insert(0, existing)
                getattr(self, f"l_setup_{name}").configure(text="已保存（可重新点测试确认）", fg=FG_DIM)
        src = next((n for c, n in LANGUAGES if c == self.cfg.asr.language), "日语")
        self.cb_setup_source.set(src)
        self.cb_setup_target.set(self.cfg.translate.target_language)
        self._autosize()

    def show_main(self) -> None:
        self._show(self.screen_main)
        self._refresh_route()
        self._autosize()

    def show_settings(self) -> None:
        self._sync_settings_fields()
        self._show(self.screen_settings)
        self._autosize()

    def _autosize(self) -> None:
        """按内容收缩窗口，并居中。

        设置页内容较长（多个 provider），这里给足高度上限，超出部分靠滚动条。
        """
        self.root.update_idletasks()
        w = max(600, self.root.winfo_reqwidth())
        h = self.root.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        h = min(h + 8, int(sh * 0.88))
        x, y = (sw - w) // 2, max(20, (sh - h) // 4)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ==================================================================
    # 校验
    # ==================================================================
    def _test_key(self, name: str, where: str) -> None:
        entry = self._entry_for(name, where)
        label = self._label_for(name, where)
        value = entry.get().strip()
        fmt = keystore.check_key_format(name, value)
        if fmt:
            label.configure(text=f"✗ {fmt}", fg=ERR)
            return
        label.configure(text="验证中…", fg=WARN)
        btn = self._button_for(name, where)
        if btn:
            btn.configure(state="disabled")

        def work() -> None:
            from .validate import validate_key

            try:
                result = validate_key(name, value, self.cfg)
            except Exception as exc:  # 校验过程本身崩了也不能让界面卡在"验证中…"
                from .validate import ValidationResult

                result = ValidationResult(False, f"校验异常：{type(exc).__name__}", detail=str(exc)[:200])
            self._ui_queue.put(("validate", (name, where, result, value)))

        threading.Thread(target=work, daemon=True, name=f"validate-{name}").start()

    def _entry_for(self, name: str, where: str) -> tk.Entry:
        if where == "setup":
            return getattr(self, f"e_setup_{name}")
        return getattr(self, f"e_set_{name}")

    def _label_for(self, name: str, where: str) -> tk.Label:
        """取该 Key 的状态标签；属性不存在时退回统一提示行。"""
        if where == "setup":
            return getattr(self, f"l_setup_{name}", self.l_setup_hint)
        return getattr(self, f"l_set_{name}", self.l_set_hint)

    def _button_for(self, name: str, where: str):
        prefix = "b_setup_" if where == "setup" else "b_set_"
        return getattr(self, f"{prefix}{name}", None)

    def _on_validate_done(self, name: str, where: str, result, value: str) -> None:
        label = self._label_for(name, where)
        btn = self._button_for(name, where)
        if btn:
            btn.configure(state="normal")
        if result.ok:
            label.configure(
                text=f"✓ {result.message}（{result.elapsed_ms:.0f}ms）"
                + (f"  ⚠ {result.warning}" if result.warning else ""),
                fg=OK,
            )
        else:
            text = f"✗ {result.message}"
            if result.detail:
                text += f" — {result.detail}"
            label.configure(text=text[:160], fg=ERR)
        self._validation[(name, where)] = result

    # ==================================================================
    # 保存
    # ==================================================================
    def _save_from_setup(self) -> None:
        """向导保存：至少要有 1 个翻译 Key + 1 个识别引擎（Key 或本地）。"""
        values: dict[str, str] = {}
        for key_name, _label in ALL_KEY_FIELDS:
            entry = getattr(self, f"e_setup_{key_name}", None)
            values[key_name] = entry.get().strip() if entry else ""
        use_local = self.v_local.get()

        tr_ready = [
            c["key"]
            for c in TRANSLATE_BY_ID.values()
            if values.get(c["key"]) and self._is_validated(c["key"], "setup")
        ]
        asr_ready = [
            c["key"]
            for c in ASR_BY_ID.values()
            if c["key"] and values.get(c["key"]) and self._is_validated(c["key"], "setup")
        ]

        problems = []
        if not tr_ready:
            filled = [c["key"] for c in TRANSLATE_BY_ID.values() if values.get(c["key"])]
            problems.append(
                "翻译 Key：填了但还没验证通过（点右边的「测试」）"
                if filled
                else "翻译 Key：至少要填一家（推荐 DeepSeek / 通义千问 / Kimi），并点「测试」验证"
            )
        if not use_local and not asr_ready:
            filled = [c["key"] for c in ASR_BY_ID.values() if c["key"] and values.get(c["key"])]
            problems.append(
                "识别 Key：填了但还没验证通过（点右边的「测试」）"
                if filled
                else "识别 Key：至少要填一家（推荐阿里云 Paraformer），或勾选「改用本地识别」"
            )
        if problems:
            self.l_setup_hint.configure(text="· " + "\n· ".join(problems), fg=ERR)
            return

        # 只写"填了且验证通过"的 Key
        updates = {k: values[k] for k in set(tr_ready) | set(asr_ready)}
        keystore.set_keys(updates)

        if use_local:
            self.cfg.asr.provider = "local"
        else:
            for pref, pid in (("dashscope", "paraformer"), ("deepgram", "deepgram"), ("siliconflow", "sensevoice")):
                if pref in asr_ready:
                    self.cfg.asr.provider = pid
                    break
        for pref in ("deepseek", "dashscope", "moonshot", "siliconflow", "zhipu", "openai"):
            if pref in tr_ready:
                conf = next((c for c in TRANSLATE_BY_ID.values() if c["key"] == pref), None)
                if conf:
                    self.cfg.translate.provider = conf["id"]
                break

        self._apply_language_from(self.cb_setup_source, self.cb_setup_target)
        keystore.save_config(self.cfg)

        asr_names = [c["label"].split(" · ")[0] for c in ASR_BY_ID.values() if c["key"] in asr_ready]
        tr_names = [c["label"].split(" · ")[0] for c in TRANSLATE_BY_ID.values() if c["key"] in tr_ready]
        self.l_setup_hint.configure(
            text=(
                f"✓ 已保存到本地（识别：{'、'.join(asr_names) or '本地'}；"
                f"翻译：{'、'.join(tr_names)}），下次启动自动读取"
            ),
            fg=OK,
        )
        self._refresh_route()
        self.root.after(900, self.show_main)

    def _apply_language_from(self, src_box, target_box) -> None:
        """把界面上的语言选择写进配置。"""
        src_label = src_box.get()
        code = next((c for c, n in LANGUAGES if n == src_label), None)
        if code is None:  # 兼容旧的 "日语 · ja" 形式
            code = src_label.split("·")[0].strip() if "·" in src_label else "ja"
        self.cfg.asr.language = code
        self.cfg.translate.target_language = target_box.get() or "简体中文"

    def _save_from_settings(self) -> None:
        updates: dict[str, str] = {}
        for key_name, _label in ALL_KEY_FIELDS:
            entry = getattr(self, f"e_set_{key_name}", None)
            if entry is None:
                continue
            value = entry.get().strip()
            if not value:
                continue  # 留空 = 不修改
            if keystore.check_key_format(key_name, value):
                self.l_set_hint.configure(text=f"✗ {key_name} 格式不对，请先点「测试」", fg=ERR)
                return
            if not self._is_validated(key_name, "settings"):
                self.l_set_hint.configure(text=f"✗ {key_name} 还没验证通过，请先点「测试」", fg=ERR)
                return
            updates[key_name] = value
        if updates:
            keystore.set_keys(updates)

        eng = next((p for p, label in ENGINE_CHOICES if label == self.cb_engine.get()), None)
        if eng:
            self.cfg.asr.provider = eng
        tr = next((p for p, label in TRANSLATE_CHOICES if label == self.cb_translate.get()), None)
        if tr:
            self.cfg.translate.provider = tr

        self._apply_language_from(self.cb_source, self.cb_target)
        self.cfg.translate.send_language_hint = self.v_hint.get()
        self.cfg.overlay.background_panel = self.v_panel.get()
        self.cfg.overlay.click_through = self.v_click.get()
        keystore.save_config(self.cfg)

        for key_name, _label in ALL_KEY_FIELDS:
            entry = getattr(self, f"e_set_{key_name}", None)
            if entry is not None:
                entry.delete(0, "end")
        self.l_set_hint.configure(text="✓ 已保存", fg=OK)
        self._sync_settings_fields()
        self._refresh_route()
        self.root.after(600, self.show_main)

    def _is_validated(self, name: str, where: str) -> bool:
        result = self._validation.get((name, where))
        return bool(result is not None and result.ok)

    def _on_local_toggle(self) -> None:
        """勾选"使用本地识别"后，两家云端 Key 输入都可以不填。"""
        state = "disabled" if self.v_local.get() else "normal"
        for name in ("dashscope", "deepgram"):
            getattr(self, f"e_setup_{name}").configure(state=state)
            getattr(self, f"b_setup_{name}").configure(state=state)

    def _sync_settings_fields(self) -> None:
        lines = []
        for key_name, label in ALL_KEY_FIELDS:
            value = keystore.get_key(key_name)
            if value:
                lines.append(f"{label}：{keystore.mask_key(value)}")
        self.l_key_status.configure(
            text="已保存的 Key（脱敏）\n" + ("\n".join(lines) if lines else "（还没有保存任何 Key）")
        )

        src_name = next((n for c, n in LANGUAGES if c == self.cfg.asr.language), LANGUAGES[0][1])
        self.cb_source.set(src_name)
        self.cb_target.set(self.cfg.translate.target_language)
        eng_label = next((lb for p, lb in ENGINE_CHOICES if p == (self.cfg.asr.provider or "").lower()), None)
        self.cb_engine.set(eng_label or ENGINE_CHOICES[0][1])
        tr_label = next((lb for p, lb in TRANSLATE_CHOICES if p == (self.cfg.translate.provider or "").lower()), None)
        self.cb_translate.set(tr_label or TRANSLATE_CHOICES[0][1])
        self.v_hint.set(self.cfg.translate.send_language_hint)
        self.v_panel.set(self.cfg.overlay.background_panel)
        self.v_click.set(self.cfg.overlay.click_through)

        for key_name, _label in ALL_KEY_FIELDS:
            status = getattr(self, f"l_set_{key_name}", None)
            if status is not None:
                existing = keystore.get_key(key_name)
                status.configure(text="已保存" if existing else "未配置", fg=FG_DIM if existing else "#6B7280")

    def _refresh_route(self) -> None:
        asr_labels = [ASR_BY_ID.get(p, {}).get("label", p).split(" · ")[0] for p in self.cfg.asr_chain()]
        tr_labels = [TRANSLATE_BY_ID.get(p, {}).get("label", p).split(" · ")[0] for p in self.cfg.translate_chain()]
        src = next((n for c, n in LANGUAGES if c == self.cfg.asr.language), self.cfg.asr.language)
        tgt = self.cfg.translate.target
        hint = "（已提前告知 AI 主要语言）" if self.cfg.translate.send_language_hint else ""
        self.l_route.configure(
            text=(
                f"识别：{' → '.join(asr_labels)}\n"
                f"翻译：{' → '.join(tr_labels)}  ({tgt.model})\n"
                f"语言：{src} → {self.cfg.translate.target_language}{hint}"
            )
        )

    # ==================================================================
    # 启动 / 停止
    # ==================================================================
    def start_translation(self) -> None:
        if self.running:
            return
        missing = []
        if not keystore.get_key("deepseek"):
            missing.append("翻译 Key(DeepSeek)")
        key_type = keystore.key_for_asr_provider(self.cfg.asr.provider)
        if key_type and not keystore.get_key(key_type):
            missing.append(f"识别 Key({key_type})")
        if missing:
            self.l_state.configure(text=f"缺少{'、'.join(missing)}，请到「设置」里补上", fg=ERR)
            self.show_settings()
            return

        # 重新读取配置，避免设置页改完没生效
        self.cfg = load_config()
        self.state = SubtitleState(max_lines=self.cfg.overlay.max_lines)
        self.stats = LatencyStats()

        from .pipeline import Pipeline, RunControl

        self.control = RunControl()
        self.pipeline = Pipeline(self.cfg, self.state, self.control, self.stats)
        self.worker = threading.Thread(target=self._run_pipeline, name="pipeline", daemon=True)
        self.worker.start()

        # 悬浮窗必须在主线程创建
        self.root.after(80, self._start_overlay)
        self.running = True
        self.b_start.configure(state="disabled")
        self.b_stop.configure(state="normal")
        self.l_state.configure(text="正在启动…", fg=WARN)

    def _run_pipeline(self) -> None:
        import asyncio

        try:
            asyncio.run(self.pipeline.run())
        except Exception as exc:
            self._ui_queue.put(("pipeline_error", exc))
        finally:
            self._ui_queue.put(("pipeline_stopped", None))

    def _start_overlay(self) -> None:
        from .overlay import SubtitleOverlay

        try:
            self.overlay = SubtitleOverlay(
                self.cfg, self.state, root=self.root, on_quit=self._overlay_closed
            )
            self.overlay.start()
        except Exception as exc:
            self.l_state.configure(text=f"字幕窗创建失败：{exc}", fg=ERR)
            self.stop_translation()

    def _overlay_closed(self) -> None:
        """悬浮窗自己要求关闭（不该连带退出程序）。"""
        self.overlay = None

    def stop_translation(self) -> None:
        if not self.running:
            return
        if self.control is not None:
            self.control.stop.set()
        if self.overlay is not None:
            try:
                self.overlay.stop()
            except Exception:
                pass
            self.overlay = None
        self.running = False
        self.b_start.configure(state="normal")
        self.b_stop.configure(state="disabled")
        self.l_state.configure(text="已停止", fg=FG_DIM)
        if self.worker is not None:
            self.worker.join(timeout=4.0)
            self.worker = None
        self.l_last_src.configure(text="—")
        self.l_last_dst.configure(text="")

    # ==================================================================
    # 状态刷新
    # ==================================================================
    def _pump_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "validate":
                    self._on_validate_done(*payload)  # type: ignore[misc]
                elif kind == "pipeline_error":
                    self.l_state.configure(text=f"启动失败：{payload}", fg=ERR)
                elif kind == "pipeline_stopped":
                    if self.running:
                        # 管线自己退出了（比如识别异常），把界面同步过来
                        self.l_state.configure(text="管线已停止（可在终端查看原因）", fg=WARN)
        except queue.Empty:
            pass
        self.root.after(120, self._pump_ui_queue)

    def _refresh_status(self) -> None:
        try:
            lines, status = self.state.snapshot()
            if self.running:
                if status.state:
                    self.l_state.configure(text=status.state, fg=OK if status.errors == 0 else ERR)
                if lines:
                    newest = lines[-1]
                    if newest.source:
                        self.l_last_src.configure(text=newest.source)
                    if newest.translated:
                        self.l_last_dst.configure(text=newest.translated)
            if self.running and self.stats.partial_ms:
                s = self.stats.summary()
                self.l_hint.configure(
                    text=f"端到端 {s['端到端(中位)']:.0f}ms（P90 {s['端到端(p90)']:.0f}ms）  "
                    f"翻译 {s['翻译耗时(中位)']:.0f}ms  样本 {self.stats.translate_calls}  "
                    f"| F1 穿透 / F2 暂停 / 拖动移动",
                    fg=FG_DIM,
                )
        except Exception:
            pass
        self.root.after(400, self._refresh_status)

    # ==================================================================
    # 杂项
    # ==================================================================
    def _open_data_dir(self) -> None:
        path = keystore.data_dir()
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            self.l_hint.configure(text=f"打开失败：{exc}", fg=ERR)

    def _open_logs(self) -> None:
        logs = keystore.data_dir() / "logs"
        logs.mkdir(exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(logs)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(logs)])
        except Exception as exc:
            self.l_hint.configure(text=f"打开失败：{exc}", fg=ERR)

    def _on_close(self) -> None:
        if self.running:
            self.stop_translation()
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    panel = AppPanel()
    panel.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
