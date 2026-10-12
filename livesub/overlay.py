"""悬浮字幕窗：置顶 + 半透明 + 鼠标穿透，随时挂在直播画面前面。

Windows 实现要点
----------------
* **置顶**：tkinter `-topmost`。
* **半透明**：`-alpha`。用 Toplevel 做覆盖层，避免 alpha 影响主窗。
* **真·悬空文字**：`-transparentcolor` 把哨兵色（#010203）整块变成完全透明
  且不接收鼠标事件——字幕区因此只有文字，不挡直播画面。
* **无边框**：`overrideredirect(True)`，并用 `WS_EX_TOOLWINDOW` 让它不出现在
  任务栏/Alt-Tab 里。
* **全局热键**：`RegisterHotKey` + `PeekMessage`。刻意不用 WH_KEYBOARD_LL 钩子
  ——ctypes 回调跨 FFI 边界有崩溃风险，热键方式安全得多。

线程约定：本模块所有代码都在主线程（tkinter 要求）。识别/翻译在别的线程，
两边只通过 `SubtitleState` 通信。
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont

from .config import Config
from .state import SubtitleLine, SubtitleState

IS_WINDOWS = sys.platform == "win32"

SENTINEL = "#010203"  # 哨兵色：整块变透明

# 关于"滚轮翻历史"的取舍（踩过的坑，别再走回头路）
# ---------------------------------------------------------------
# 面板默认是**鼠标穿透**的（`WS_EX_TRANSPARENT`），该状态下窗口收不到任何
# 鼠标消息，滚轮自然收不到；而且实测**即使关掉穿透也滚不动** ——
# `overrideredirect(True)` 的无边框窗口没有焦点，滚轮消息不会路由到画布。
#
# 试过用 WH_MOUSE_LL 全局鼠标钩子解决，但**性能代价太大**：
# 低级钩子会让**每一次鼠标移动**都回调到本进程，整机鼠标响应都会变钝
# （用户实测反馈卡顿）。所以放弃。
#
# 最终方案：**注册键盘热键翻历史**（RegisterHotKey 零开销，系统层面转发，
# 不参与鼠标消息链），鼠标滚轮则作为"手动关掉穿透时"的补充路径。
# 键盘热键见 _register_hotkeys 里的 PageUp/PageDown。

COLOR_OUTLINE = "#000000"  # 文字描边，保证在亮画面上也读得清
COLOR_SOURCE = "#E8EAED"
COLOR_SOURCE_DIM = "#9AA0A6"
COLOR_TARGET = "#FFFFFF"
COLOR_TARGET_LIVE = "#FFE08A"
COLOR_TARGET_FAIL = "#FF8A80"
COLOR_STATUS = "#8A94A0"
COLOR_STATUS_OK = "#6FD08C"
COLOR_STATUS_ERR = "#FF6B6B"

# ---- Win32 ----
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

VK_F1, VK_F2, VK_F3, VK_F4 = 0x70, 0x71, 0x72, 0x73
#: PageUp / PageDown（用来翻看历史，零开销，替代全局鼠标钩子）
VK_PRIOR, VK_NEXT = 0x21, 0x22

user32 = ctypes.windll.user32 if IS_WINDOWS else None


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt_x", wintypes.LONG),
        ("pt_y", wintypes.LONG),
    ]


def _parse_hotkey(spec: str) -> tuple[int, int]:
    """把 "ctrl+shift+u" 解析成 (modifiers, vk)。解析不出来返回 (0, 0)。"""
    parts = [p.strip().lower() for p in spec.replace("+", " ").split() if p.strip()]
    mods = MOD_NOREPEAT
    vk = 0
    for p in parts:
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p == "alt":
            mods |= MOD_ALT
        elif p in ("win", "super", "cmd"):
            mods |= MOD_WIN
        elif p in ("f1", "f2", "f3", "f4"):
            vk = {"f1": VK_F1, "f2": VK_F2, "f3": VK_F3, "f4": VK_F4}[p]
        elif len(p) == 1:
            vk = ord(p.upper())
    if vk == 0:
        return 0, 0
    return mods, vk


class SubtitleOverlay:
    """悬浮字幕窗。

    生命周期：主控面板可以反复 `start()` / `stop()` 它，而**不退出整个程序**。
    所以这里不自己创建 Tk 根窗、也不自己跑 mainloop——根窗与事件循环由主控
    面板持有（通过 `root` 传入）。
    """

    def __init__(
        self,
        cfg: Config,
        state: SubtitleState,
        root: "tk.Tk | None" = None,
        on_quit=None,
    ):
        self.cfg = cfg
        self.state = state
        self.root = root if root is not None else tk.Tk()
        self._own_root = root is None
        self.on_quit = on_quit
        self._actions: queue.Queue[str] = queue.Queue()
        self._click_through = cfg.overlay.click_through
        self._paused = False
        self._hotkeys: list[int] = []
        self._hotkey_map: dict[int, str] = {}
        self._hwnd: int | None = None
        self._last_key: tuple = ()
        self._dragging = False
        self._drag_origin = (0, 0)
        self._bottom_offset = 0
        self._hint_job = None
        self._tick_job = None
        self._closed = False
        self._sentinel_ok = True
        self._width = 0
        self._height = 0
        self.win: "tk.Toplevel | None" = None
        self.rows: list[dict] = []

        # ---- 面板模式状态（像桌面歌词那样，可拉伸的固定面板）----
        self._panel_mode = bool(getattr(cfg.overlay, "panel_mode", True))
        self.transcript = None          # panel.TranscriptView
        self._resizing = False
        self._resize_origin = (0, 0, 0, 0)
        self._cursor_resize = False
        self._grip = None
        self._grip_ids: list[int] = []
        #: 用户用 F1 手动切换过穿透 → 之后不再让"悬停自动切换"插手
        self._hover_override = False
        #: 首帧是否已经强制刷过状态栏（见 _tick）
        self._first_render_done = False
        #: 面板屏幕矩形（悬停判断用，每 200ms 刷新一次）
        self._panel_rect = (0, 0, 0, 0)
        #: 当前是否悬停在面板上（由钩子驱动，不能用 tk 的 <Enter>）
        self._hover_state = False
        #: tick 计数（用于周期性刷新面板矩形）
        self._tick_count = 0

    # ================= 对外接口 =================
    def request_action(self, action: str) -> None:
        """可从任意线程调用。"""
        self._actions.put(action)

    def start(self) -> None:
        """创建窗口并开始渲染。必须在主线程调用。"""
        if self._closed:
            raise RuntimeError("这个悬浮窗实例已经停止，请重新创建实例")
        self._build()
        self._register_hotkeys()
        self._tick_job = self.root.after(40, self._tick)
        self._flash("字幕已开启：F1 穿透 / F2 暂停 / F3 行数 / 拖动移动", 5000)

    def stop(self) -> None:
        """销毁窗口但保留根窗，便于下次 start() 重建。"""
        if self._closed:
            return
        self._closed = True
        self._unregister_hotkeys()
        for job in (self._tick_job, self._hint_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self._tick_job = self._hint_job = None
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None
        self.rows.clear()
        # 独立运行模式下还要退出 mainloop。
        # 注意：这里只停字幕窗，根窗可能由主控面板持有（那种情况绝不能 quit，
        # 否则会把整个面板一起关掉），所以只在 _own_root 时退。
        if self._own_root:
            try:
                self.root.quit()
            except Exception:
                pass

    def run(self) -> None:
        """自带根窗的独立运行模式（命令行单独调试悬浮窗时用）。"""
        self.start()
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
            # 必须显式退出 mainloop：stop() 只销毁窗口，而 _flash 排的定时器
            # 还会占着事件循环，导致 "窗口没了但进程不退"（实测会一直挂住）。
            if self._own_root:
                try:
                    self.root.quit()
                except Exception:
                    pass
                try:
                    self.root.destroy()
                except Exception:
                    pass

    # ================= 构建 =================
    def _build(self) -> None:
        ov = self.cfg.overlay
        # 根窗由外部持有（主控面板），这里只创建覆盖层，绝不能动根窗的显示状态
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", SENTINEL)
            self.win.configure(bg=SENTINEL)
            self._sentinel_ok = True
        except tk.TclError:
            self.win.configure(bg="#101418")
            self._sentinel_ok = False

        # alpha 只用于"没有透明色支持"的降级路径；有 sentinel 时窗口 alpha 保持
        # 1.0，背景整块透明、文字完全不透明——否则 -alpha 会把文字也一起变淡，
        # 在明亮的直播画面上很难读。
        if self._sentinel_ok:
            self.win.attributes("-alpha", 1.0)
            # 字幕区是否要一层深色底板（更好读，但会遮住一点画面）
            self._panel = bool(getattr(ov, "background_panel", False))
            self._area_bg = "#12161B" if self._panel else SENTINEL
            self._bar_bg = "#0E1116"
        else:
            self.win.attributes("-alpha", ov.opacity)
            self._panel = True
            self._area_bg = "#12161B"
            self._bar_bg = "#1A1E23"

        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        if self._panel_mode:
            # 面板模式：尺寸固定，用户自己拖。0 表示按屏幕比例给个初值。
            pw = int(getattr(ov, "panel_width", 0) or 0) or max(420, int(sw * ov.width_ratio))
            ph = int(getattr(ov, "panel_height", 0) or 0) or max(140, int(sh * ov.height_ratio))
            self._width = max(240, pw)
            self._height = max(90, ph)
        else:
            self._width = max(360, int(sw * ov.width_ratio))
            # 初始高度是"上限"，真正显示的窗口会按内容自适应缩小（见 _fit_window）
            self._height = max(90, int(sh * ov.height_ratio))
        self._bottom_offset = int(sh * ov.bottom_ratio)
        x = (sw - self._width) // 2
        y = sh - self._height - self._bottom_offset
        self.win.geometry(f"{self._width}x{self._height}+{x}+{y}")

        available = set(tkfont.families())
        family = next(
            (f for f in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Arial") if f in available),
            "TkDefaultFont",
        )
        self.f_source = tkfont.Font(family=family, size=ov.original_font_size)
        self.f_target = tkfont.Font(family=family, size=ov.translated_font_size, weight="bold")
        self.f_status = tkfont.Font(family=family, size=9)

        self.status_bar = tk.Frame(self.win, bg=self._bar_bg)
        self.status_bar.pack(side="top", fill="x")
        self.lbl_status = tk.Label(
            self.status_bar,
            # 初始文案不要写"启动中"：真启动完成前它不会被刷新，看起来就像卡住了。
            # 用一个中性且准确的默认值，并且首帧会强制刷新一次（见 _tick）。
            text="等待开始…",
            font=self.f_status,
            fg=COLOR_STATUS,
            bg=self._bar_bg,
            anchor="w",
            padx=10,
            pady=3,
        )
        self.lbl_status.pack(side="left", fill="x", expand=True)

        self.body = tk.Frame(self.win, bg=self._area_bg)
        self.body.pack(side="top", fill="both", expand=True)
        self.rows: list[dict] = []

        # ---- 面板模式：一个可滚动的固定面板，替代"自适应高度的滚动字幕" ----
        if self._panel_mode:
            from .panel import TranscriptView

            self._panel_bg = str(getattr(ov, "panel_bg", "#000000"))
            self._panel_alpha = float(getattr(ov, "panel_opacity", 0.7) or 0.7)
            self._panel = True
            self._area_bg = self._panel_bg
            self._bar_bg = self._panel_bg
            # 整窗用 -alpha 做半透明（桌面歌词的做法）：底板纯黑、透过它能看到
            # 下面的直播画面。代价是文字也一起变淡，但 0.7 下仍然清楚。
            # 之前的点阵 stipple 方案文字是全不透明的，但黑色底板会变成
            # 50% 的点阵，看起来是"灰"的而不是"黑"的——用户反馈不好看。
            if self._sentinel_ok:
                self.win.attributes("-alpha", self._panel_alpha)
            self.win.configure(bg=self._panel_bg)
            self.status_bar.configure(bg=self._bar_bg)
            self.body.configure(bg=self._panel_bg)
            self.transcript = TranscriptView(
                self.body,
                self.f_target,
                self.f_source,
                show_panel=True,
                opaque_bg=True,
                bg=self._panel_bg,
                bg_live=str(getattr(ov, "panel_bg_live", "#101010")),
                auto_scroll=bool(getattr(ov, "auto_scroll", True)),
                # 光标移到面板上时临时关掉穿透（这样能拖动/改大小/滚轮）
                on_hover_change=self._on_panel_hover,
            )
            if getattr(ov, "show_scrollbar", True):
                self.transcript.attach_scrollbar()
            self.transcript.set_message("等待识别结果…（启动后这里会显示中文字幕）")

        self.hint = tk.Label(
            self.win,
            text="",
            font=self.f_status,
            fg=COLOR_STATUS_OK,
            bg=self._bar_bg,
            anchor="w",
            padx=10,
            pady=3,
        )

        self._apply_click_through(self._click_through)
        self._bind_events()
        if self._panel_mode:
            self._flash(
                "面板：PageUp 回看历史 / PageDown 回到最新 / 拖右下角改大小 / F1 穿透", 7000
            )
        else:
            self._flash("就绪：F1 穿透 / F2 暂停 / F3 行数 / F4 重连 / 双击穿透 / 拖动移动")

    def _bind_events(self) -> None:
        for widget in (self.win, self.body, self.status_bar, self.lbl_status):
            widget.bind("<Button-1>", self._on_press)
            widget.bind("<B1-Motion>", self._on_drag)
            widget.bind("<ButtonRelease-1>", self._on_release)
            widget.bind("<Double-Button-1>", self._on_double)
        if self._panel_mode:
            # 右下角手柄 + Ctrl 拖动，两种改大小的方式都支持
            win = self.win
            win.bind("<Motion>", self._on_motion)
            win.bind("<Control-Button-1>", self._on_ctrl_press)
            win.bind("<Control-B1-Motion>", self._on_resize_drag)
            win.bind("<Control-ButtonRelease-1>", self._on_resize_release)
            self._create_grip()

    # ================= 改大小（面板模式）=================
    #
    # 两种方式都支持：
    #   1) 右下角一个小三角手柄（不会误触，鼠标移上去变斜向箭头）
    #   2) 按住 Ctrl 拖窗口任意位置
    # 改大小**只改变能看到多少内容，字号不变**。
    _GRIP = 16

    def _create_grip(self) -> None:
        """右下角手柄：用几条斜线画个小三角，视觉上提示"这里能拖"。"""
        # 手柄直接画在 win 上，用一个独立的小 Canvas 保证不挡布局
        self._grip = tk.Canvas(
            self.win,
            width=self._GRIP,
            height=self._GRIP,
            bg=self._bar_bg,
            highlightthickness=0,
            bd=0,
        )
        self._grip.place(relx=1.0, rely=1.0, anchor="se")
        for i, off in enumerate((3, 7, 11)):
            self._grip.create_line(
                self._GRIP - 2, off, off, self._GRIP - 2,
                fill="#7A828C" if i < 2 else "#565D66", width=1,
            )
        self._grip.bind("<Button-1>", self._on_ctrl_press)
        self._grip.bind("<B1-Motion>", self._on_resize_drag)
        self._grip.bind("<ButtonRelease-1>", self._on_resize_release)
        self._grip.bind("<Enter>", lambda e: self._set_cursor("size_nw_se"))
        self._grip.bind("<Leave>", lambda e: self._set_cursor(""))

    def _on_panel_hover(self, hovering: bool) -> None:
        """按光标位置切换"鼠标穿透"：移上去可交互，移开恢复不挡直播。

        注意**不能靠 tkinter 的 `<Enter>/<Leave>`**：穿透时窗口收不到鼠标
        消息，那两个事件不会触发（死循环）。所以由 `_tick` 里每 200ms 用
        `winfo_pointerxy()` 主动判断光标坐标。
        """
        if not IS_WINDOWS or self.win is None:
            return
        # 用户用 F1 手动关过穿透时就不折腾了
        if self._hover_override:
            return
        if hovering == self._hover_state:
            return
        self._hover_state = hovering
        try:
            self._apply_click_through(not hovering)
        except Exception:
            pass

    def _set_cursor(self, name: str) -> None:
        try:
            self.win.configure(cursor=name or "")
        except Exception:
            pass

    def _on_motion(self, event) -> None:
        """靠右下角时把光标变成斜向箭头，提示可拖。"""
        if self._resizing:
            return
        w = self.win.winfo_width()
        h = self.win.winfo_height()
        near = event.x >= w - self._GRIP - 4 and event.y >= h - self._GRIP - 4
        if near != self._cursor_resize:
            self._cursor_resize = near
            self._set_cursor("size_nw_se" if near else "")

    def _on_ctrl_press(self, event) -> None:
        self._resizing = True
        self._dragging = False          # 不要同时触发"移动"
        self._resize_origin = (
            event.x_root,
            event.y_root,
            self.win.winfo_width(),
            self.win.winfo_height(),
        )

    def _on_resize_drag(self, event) -> None:
        if not self._resizing:
            return
        x0, y0, w0, h0 = self._resize_origin
        w = max(240, w0 + (event.x_root - x0))
        h = max(90, h0 + (event.y_root - y0))
        self._apply_size(w, h)

    def _on_resize_release(self, event) -> None:
        if not self._resizing:
            return
        self._resizing = False
        self._remember_size()

    def _apply_size(self, w: int, h: int) -> None:
        """改窗口大小并让内容重新折行（字号不变）。"""
        x, y = self.win.winfo_x(), self.win.winfo_y()
        self._width, self._height = w, h
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        if self.transcript is not None:
            self.win.update_idletasks()
            self.transcript.resize(w, h - (self.status_bar.winfo_height() or 27))

    def _remember_size(self) -> None:
        """把尺寸记进配置，下次启动沿用。"""
        try:
            self.cfg.overlay.panel_width = int(self.win.winfo_width())
            self.cfg.overlay.panel_height = int(self.win.winfo_height())
            from . import keystore

            keystore.save_config(self.cfg)
        except Exception:
            pass

    # ================= Win32 =================
    def _apply_click_through(self, enable: bool) -> None:
        self._click_through = enable
        if not IS_WINDOWS:
            return
        self.win.update_idletasks()
        hwnd = self.win.winfo_id()
        parent = user32.GetParent(hwnd)
        self._hwnd = parent or hwnd
        style = user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if enable:
            style |= WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, style)

    def _register_hotkeys(self) -> None:
        """注册全局热键。

        只用 F1~F6 与 Ctrl+Alt+方向键，避免和输入法/直播软件抢
        Ctrl+Shift+字母这类常见组合。

        翻历史用的是 **RegisterHotKey**（系统层面转发，零开销），
        刻意不用全局鼠标钩子 —— 低级钩子会让每次鼠标移动都回调到本进程，
        整机鼠标响应都会变钝（实测用户反馈卡顿）。
        """
        if not IS_WINDOWS:
            return
        wanted = [
            ((MOD_NOREPEAT, VK_F1), "toggle_click_through"),
            ((MOD_NOREPEAT, VK_F2), "toggle_pause"),
            ((MOD_NOREPEAT, VK_F3), "toggle_history"),
            ((MOD_NOREPEAT, VK_F4), "reconnect"),
            # 翻历史：PageUp 往回看，PageDown 回到最新
            ((MOD_NOREPEAT, VK_PRIOR), "scroll_back"),
            ((MOD_NOREPEAT, VK_NEXT), "scroll_latest"),
        ]
        for idx, ((mods, vk), action) in enumerate(wanted, start=1):
            if user32.RegisterHotKey(None, idx, mods, vk):
                self._hotkeys.append(idx)
                self._hotkey_map[idx] = action
            else:
                print(f"[overlay] 热键 F{idx} 注册失败（可能被别的程序占用）", file=sys.stderr)

        mods, vk = _parse_hotkey(self.cfg.hotkeys.toggle_click_through)
        if vk and (mods & (MOD_CONTROL | MOD_SHIFT | MOD_ALT)):
            idx = 90
            if user32.RegisterHotKey(None, idx, mods, vk):
                self._hotkeys.append(idx)
                self._hotkey_map[idx] = "toggle_click_through"

    def _unregister_hotkeys(self) -> None:
        if not IS_WINDOWS:
            return
        for idx in self._hotkeys:
            try:
                user32.UnregisterHotKey(None, idx)
            except Exception:
                pass
        self._hotkeys.clear()

    def _pump_hotkeys(self) -> None:
        if not IS_WINDOWS:
            return
        msg = MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
            action = self._hotkey_map.get(msg.wParam)
            if action:
                self._handle_action(action)

    # ================= 交互 =================
    def _on_press(self, event) -> None:
        if self._resizing:
            return
        self._dragging = True
        self._drag_origin = (event.x_root - self.win.winfo_x(), event.y_root - self.win.winfo_y())

    def _on_drag(self, event) -> None:
        if not self._dragging or self._resizing:
            return
        x = event.x_root - self._drag_origin[0]
        y = event.y_root - self._drag_origin[1]
        self.win.geometry(f"+{x}+{y}")

    def _on_release(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        sh = self.win.winfo_screenheight()
        h = self.win.winfo_height() if self._panel_mode else self._height
        self._bottom_offset = max(0, sh - self.win.winfo_y() - h)

    def _on_double(self, event) -> None:
        self._handle_action("toggle_click_through")

    def _handle_action(self, action: str) -> None:
        if action == "toggle_click_through":
            # 用户手动切换过 → 关掉"悬停自动切换"，尊重用户的选择
            self._hover_override = True
            self._apply_click_through(not self._click_through)
            tail = "（现在可以点直播界面了）" if self._click_through else "（现在可以拖动/滚动本窗口）"
            self._flash(f"鼠标穿透：{'开' if self._click_through else '关'}{tail}")
        elif action == "toggle_pause":
            self._paused = not self._paused
            self.state.set_status(paused=self._paused)
            self._flash(f"字幕：{'已暂停' if self._paused else '继续'}")
        elif action == "toggle_history":
            if self._panel_mode and self.transcript is not None:
                # 面板模式下 F3 改成"显示/隐藏深色底板"——
                # 面板能显示多少由窗口大小决定，不再由行数决定
                self.transcript.show_panel = not self.transcript.show_panel
                self.transcript.update_rows(self.state.snapshot()[0])
                self._flash(f"深色底板：{'开' if self.transcript.show_panel else '关'}")
            else:
                ov = self.cfg.overlay
                self.state.max_lines = 1 if self.state.max_lines > 1 else max(2, ov.max_lines)
                self._flash(f"显示行数：{self.state.max_lines}")
        elif action == "scroll_back":
            self._scroll_history(1)
        elif action == "scroll_latest":
            tv = self.transcript
            if tv is not None:
                tv.scroll_to_bottom()
            self._flash("已回到最新")
        elif action == "reconnect":
            self._flash("正在重连…")
        elif action == "quit":
            self.close()

    # ================= 翻历史（键盘热键，零开销）=================
    def _scroll_history(self, direction: int) -> None:
        """翻看历史。direction>0 = 往回看（更早的内容）。

        刻意用键盘热键而不是全局鼠标钩子：低级钩子会让**每一次鼠标移动**
        都回调到本进程，整机鼠标响应都会变钝（用户实测反馈卡顿）。
        RegisterHotKey 是系统层面转发，不参与鼠标消息链，零开销。
        """
        tv = self.transcript
        if tv is None:
            return
        tv.scroll_by(-direction * 3)
        first, last = tv.canvas.yview()
        if last >= 0.999:
            self._flash("已回到最新")
        else:
            self._flash("回看历史中…（PageDown 回到最新）")

    # ================= 悬停判断 =================
    def _refresh_panel_rect(self) -> None:
        """缓存窗口的屏幕矩形，供悬停判断使用。"""
        if self.win is None:
            return
        try:
            self.win.update_idletasks()
            x, y = self.win.winfo_rootx(), self.win.winfo_rooty()
            w, h = self.win.winfo_width(), self.win.winfo_height()
            self._panel_rect = (x, y, x + w, y + h)
        except Exception:
            pass

    def _point_in_panel(self, x: int, y: int) -> bool:
        """屏幕坐标 (x, y) 是否落在面板矩形内。"""
        left, top, right, bottom = self._panel_rect
        return left <= x <= right and top <= y <= bottom

    def _flash(self, text: str, ms: int = 3500) -> None:
        self.hint.configure(text=text)
        if not self.hint.winfo_ismapped():
            self.hint.pack(side="bottom", fill="x")
        if self._hint_job is not None:
            try:
                self.root.after_cancel(self._hint_job)
            except Exception:
                pass
        self._hint_job = self.root.after(ms, self._hide_hint)

    def _hide_hint(self) -> None:
        try:
            self.hint.pack_forget()
        except Exception:
            pass
        self._hint_job = None

    def close(self) -> None:
        """用户要求关闭悬浮窗（热键/双击等触发）。

        注意：这里只停掉字幕窗本身，**不退出主控面板**——是否需要同时停止
        翻译管线由 `on_quit` 回调决定。
        """
        if self.on_quit is not None:
            try:
                self.on_quit()
            except Exception:
                pass
        self.stop()

    # ================= 渲染循环 =================
    def _tick(self) -> None:
        if self._closed:
            return
        try:
            self._pump_hotkeys()
        except Exception:
            pass
        try:
            while True:
                self._handle_action(self._actions.get_nowait())
        except queue.Empty:
            pass
        if self.state.take_dirty():
            try:
                self._render()
            except Exception as exc:
                print(f"[overlay] 渲染异常: {exc}", file=sys.stderr)
        elif not self._first_render_done:
            # 首帧强制刷一次状态栏。
            # _render 只在"状态有变化"时才更新界面，而状态栏的初始文案是
            # 构造时写死的 —— 万一启动后状态一直没变（例如还停在"启动中"），
            # 状态栏就会一直显示那个初始值，看起来像卡住了。
            try:
                _lines, status = self.state.snapshot()
                self._render_status(status)
                self._first_render_done = True
            except Exception:
                pass

        # 每 ~200ms 刷新一次面板矩形并同步悬停状态。
        #
        # 为什么不靠 tk 的 <Enter>/<Leave>：面板穿透时窗口收不到鼠标消息，
        # 那两个事件根本不会触发。所以主动用光标坐标判断。
        self._tick_count += 1
        if self._panel_mode and self._tick_count % 5 == 0:
            self._refresh_panel_rect()
            self._sync_hover_by_pointer()

        self.root.after(40, self._tick)

    def _sync_hover_by_pointer(self) -> None:
        """按光标位置更新悬停状态（穿透时 <Enter> 不可用，只能这样）。"""
        if self.win is None or self._hover_override:
            return
        try:
            x, y = self.win.winfo_pointerxy()
        except Exception:
            return
        self._on_panel_hover(self._point_in_panel(x, y))
    def _render(self) -> None:
        lines, status = self.state.snapshot()
        if self._panel_mode:
            # 面板模式：把**全部历史**交给面板，由它决定能显示多少
            # （窗口拉大就自动显示更多，这正是"改大小不改字号"的落点）
            key = (
                tuple((l.sid, l.source, l.translated, l.final, l.failed) for l in lines),
                status.state,
                status.detail,
                status.errors,
                status.paused,
                status.e2e_ms,
            )
            if key == self._last_key:
                return
            self._last_key = key
            if self.transcript is not None:
                self.transcript.update_rows(lines)
            self._render_status(status)
            return

        rows = lines[-max(1, self.state.max_lines) :]
        key = (
            tuple((l.sid, l.source, l.translated, l.final, l.failed) for l in rows),
            status.state,
            status.detail,
            status.errors,
            status.paused,
            status.e2e_ms,
        )
        if key == self._last_key:
            return
        self._last_key = key
        self._rebuild_rows(rows)
        self._render_status(status)
        self._fit_window()

    def _fit_window(self) -> None:
        """让窗口高度严格贴住内容，同时保持底边不动。

        仅用于旧的"滚动字幕"模式。面板模式尺寸固定，不做自适应——
        自适应正是"内容跳动太快"的来源之一。
        """
        if self._panel_mode:
            return
        self.win.update_idletasks()
        bar_h = self.status_bar.winfo_height() or 27
        need = bar_h + getattr(self, "_content_height", 0) + 4
        new_h = max(70, min(need, int(self.win.winfo_screenheight() * 0.6)))
        if abs(new_h - self._height) < 3:
            return
        self._height = new_h
        sh = self.win.winfo_screenheight()
        sw = self.win.winfo_screenwidth()
        x = self.win.winfo_x()
        if x <= 0:
            x = (sw - self._width) // 2
        y = max(0, sh - new_h - self._bottom_offset)
        self.win.geometry(f"{self._width}x{new_h}+{x}+{y}")

    def _rebuild_rows(self, rows: list[SubtitleLine]) -> None:
        for row in self.rows:
            row["frame"].destroy()
        self.rows.clear()

        last_sid = rows[-1].sid if rows else -1
        wrap_width = self._width - 24
        content_height = 0
        for line in rows:
            is_live = (line.sid == last_sid) and not line.final

            if line.failed:
                target_color = COLOR_TARGET_FAIL
            elif is_live:
                target_color = COLOR_TARGET_LIVE
            else:
                target_color = COLOR_TARGET
            target_text = line.translated or ("…" if is_live else "")
            if not line.translated and not is_live:
                target_color = COLOR_STATUS

            blocks: list[tuple[str, tkfont.Font, str]] = []
            if target_text:
                blocks.append((target_text, self.f_target, target_color))
            if line.source:
                blocks.append(
                    (line.source, self.f_source, COLOR_SOURCE_DIM if is_live else COLOR_SOURCE)
                )
            if not blocks:
                continue

            # 用 Canvas 手绘"带黑色描边"的文字：无论底下是亮画面还是白字幕都能
            # 读清，纯 Label 做不到描边。高度自己算，才能让窗口严格贴住内容。
            height = 6
            wrapped: list[tuple[list[str], tkfont.Font, str]] = []
            for text, font, color in blocks:
                lines = self._wrap(text, font, wrap_width)
                wrapped.append((lines, font, color))
                height += len(lines) * (font.metrics("linespace") + 2) + 5

            canvas = tk.Canvas(
                self.body,
                height=height,
                width=wrap_width,
                bg=self._area_bg,
                highlightthickness=0,
                bd=0,
            )
            canvas.pack(side="bottom", fill="x", padx=6, pady=1)
            content_height += height + 2  # +2 是 canvas 的 pady

            y = 3
            for lines, font, color in wrapped:
                step = font.metrics("linespace") + 2
                for text in lines:
                    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
                        canvas.create_text(
                            12 + dx, y + dy, text=text, font=font, fill=COLOR_OUTLINE, anchor="nw"
                        )
                    canvas.create_text(12, y, text=text, font=font, fill=color, anchor="nw")
                    y += step
                y += 5

            self.rows.append({"frame": canvas, "sid": line.sid, "live": is_live})

        self._content_height = content_height

    def _wrap(self, text: str, font: tkfont.Font, width: int) -> list[str]:
        """按像素宽度折行。中文按字符断，英文尽量按词断。"""
        if font.measure(text) <= width:
            return [text]
        out: list[str] = []
        for raw_line in text.split("\n"):
            cur = ""
            for ch in raw_line:
                if font.measure(cur + ch) <= width:
                    cur += ch
                    continue
                if cur:
                    out.append(cur)
                cur = ch
            if cur:
                out.append(cur)
        return out

    def _render_status(self, status) -> None:
        bits = []
        color = COLOR_STATUS_ERR if status.errors else COLOR_STATUS_OK
        if status.errors:
            bits.append(f"错误×{status.errors}")
        bits.append(status.state or "…")
        if status.paused:
            bits.append("已暂停")
        if status.e2e_ms:
            bits.append(f"{status.e2e_ms:.0f}ms")
        if status.detail:
            bits.append(status.detail)
        bits.append("F1穿透 F2暂停 F3行数 F4重连")
        self.lbl_status.configure(text="  ·  ".join(b for b in bits if b), fg=color)
