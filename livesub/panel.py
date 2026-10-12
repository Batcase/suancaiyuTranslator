"""固定尺寸的转录面板视图（像网易云桌面歌词那种，但可拉伸）。

为什么单独做这个
----------------
原来的悬浮窗是"自适应高度的滚动字幕"：窗口高度严格贴住内容，只显示最新 1~2 行。
快语速主播一两秒就换一批文字，于是整块内容不停被替换 —— **眼睛根本跟不上**。

面板模式反过来：
  * 窗口尺寸**固定**（用户自己拖），更新时不会整体跳动
  * 字号**不变**，窗口拉大只是**能显示更多历史**
  * 新内容从底部进入、旧的往上移（tail -f），视线可以稳定盯着底部
  * 鼠标滚轮往上翻可以看历史，滚回底部就恢复自动跟随

实现要点
--------
* 用一个 Canvas 承载所有文字，**复用 item**（不每帧销毁重建）：
  这样滚动位置稳定、性能也不随历史增长而下降。
  文字用 create_text 画多遍做描边——Label 做不到描边，而在亮画面上
  没描边的白字根本读不清。
* 底板用 stipple 点阵做半透明：tkinter 的 `-alpha` 是**整窗**属性，
  用了它文字也会一起变淡。stipple 只让底板"虚"掉，文字保持完全不透明。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

from .state import SubtitleLine

OUTLINE = "#000000"
COLOR_TARGET = "#FFFFFF"
COLOR_TARGET_LIVE = "#FFE08A"
COLOR_TARGET_FAIL = "#FF8A80"
COLOR_SOURCE = "#C8CDD4"
COLOR_SOURCE_DIM = "#8A94A0"
COLOR_PANEL = "#0D1117"
COLOR_PANEL_LIVE = "#141A22"
COLOR_TEXT_DIM = "#6B7280"

#: 描边偏移（8 个方向）
_OUTLINE_OFFSETS = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1))


class TranscriptView:
    """可滚动的转录面板。"""

    def __init__(
        self,
        parent: tk.Widget,
        f_target: tkfont.Font,
        f_source: tkfont.Font,
        *,
        show_panel: bool = True,
        opaque_bg: bool = False,
        bg: str = COLOR_PANEL,
        bg_live: str = COLOR_PANEL_LIVE,
        auto_scroll: bool = True,
        on_hover_change=None,
        **kwargs,
    ) -> None:
        # 吃掉未识别的关键字参数：调用方（overlay）可能传入不同版本命名的回调，
        # 少一个参数也不该让整个悬浮窗起不来。
        if on_hover_change is None:
            on_hover_change = kwargs.get("on_click_through_change")
        self.f_target = f_target
        self.f_source = f_source
        self.show_panel = show_panel
        #: True = 用不透明的纯色底板（此时由窗口 alpha 统一做半透明，
        #: 效果是"整块半透明"——像桌面歌词）；False = 用点阵 stipple 做半透明
        self.opaque_bg = opaque_bg
        self.bg = bg
        self.bg_live = bg_live
        self.auto_scroll = auto_scroll
        #: 鼠标悬停状态变化时回调（用来临时关掉鼠标穿透，否则滚轮收不到）
        self.on_hover_change = on_hover_change
        self._hovering = False

        # 底板完全不透明时画布也必须是同一个底色，否则会露出窗口底色
        self.canvas = tk.Canvas(
            parent,
            bg=(bg if opaque_bg else OUTLINE),
            highlightthickness=0,
            bd=0,
            takefocus=0,
        )
        self.canvas.pack(side="left", fill="both", expand=True)

        self.sb = tk.Scrollbar(parent, orient="vertical", command=self.canvas.yview, width=10)
        self.canvas.configure(yscrollcommand=self._on_scroll)

        #: 每个 sid 对应的渲染结果 {"top","height","items":[...]}
        self._rendered: dict[int, dict] = {}
        self._order: list[int] = []
        self._content_h = 0
        self._width_seen = 0
        self._user_scrolled = False
        self._total_h = 0
        self._viewport_h = 0
        #: 宽度还不可信时暂存的行，等窗口布局完成后再画
        self._pending_rows: list[SubtitleLine] | None = None
        #: "等待声音…"占位文字的 canvas item（有内容时要删掉）
        self._placeholder_ids: list[int] = []

        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._scroll_units(-3))
        self.canvas.bind("<Button-5>", lambda e: self._scroll_units(3))

        # 鼠标进入/离开时切换"鼠标穿透"。
        #
        # 为什么必须这么做：面板默认是**鼠标穿透**的（不然会挡住直播软件的点击），
        # 而穿透在 Win32 里就是给窗口加 WS_EX_TRANSPARENT —— 那种状态下窗口
        # **完全不接收任何鼠标事件**，滚轮自然也收不到，"滚轮回看历史"就是失效的。
        #
        # 折中方案：鼠标悬停在面板上时临时关掉穿透（这样能滚动），
        # 移开后再恢复（继续不挡直播）。对用户来说就是"鼠标移上去就能滚"。
        self.on_hover_change = on_hover_change
        self.canvas.bind("<Enter>", lambda e: self._set_hover(True), add="+")
        self.canvas.bind("<Leave>", lambda e: self._set_hover(False), add="+")

    def _set_hover(self, hovering: bool) -> None:
        self._hovering = hovering
        if self.on_hover_change is not None:
            try:
                self.on_hover_change(hovering)
            except Exception:
                pass

    @property
    def hovering(self) -> bool:
        return self._hovering

    # ------------------------------------------------------------------
    # 滚动
    # ------------------------------------------------------------------
    def attach_scrollbar(self) -> None:
        self.sb.pack(side="right", fill="y")

    def _on_scroll(self, first: str, last: str) -> None:
        self.sb.set(first, last)

    def _on_wheel(self, event) -> None:
        # 向上滚 = 想回看历史 → 停止自动跟随；滚到底部恢复
        self._scroll_units(-1 * int(event.delta / 120) * 3)

    def _scroll_units(self, units: int) -> None:
        self.canvas.yview_scroll(units, "units")
        self._sync_follow()

    def scroll_by(self, units: int) -> None:
        """供外部（全局鼠标钩子）调用：滚轮翻页。"""
        self._scroll_units(units)

    def _sync_follow(self) -> None:
        if not self.auto_scroll:
            return
        first, last = self.canvas.yview()
        at_bottom = last >= 0.999
        if at_bottom:
            if self._user_scrolled:
                self._user_scrolled = False
        else:
            self._user_scrolled = True

    def scroll_to_bottom(self) -> None:
        self.canvas.yview_moveto(1.0)
        self._user_scrolled = False

    def resize(self, width: int, height: int) -> None:
        """窗口尺寸变了：重新折行并重排（字号不变）。"""
        self.canvas.configure(width=width, height=height)
        self._width_seen = 0        # 强制重排

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def update_rows(self, rows: list[SubtitleLine]) -> None:
        """用最新的状态刷新面板。

        ★ 关键：**先全量测量高度，再按最终位置一次性绘制**。

        为什么不能"边测边画、只重画变化的那块"：
        canvas 的 item 是"后创建的在上面"。译文是流式增长的，某一行的字变多
        → 这一行要重画 → **新画的底板矩形会压住上面那些还没重画的行**，
        视觉上就是"下一行叠在上一行上面"。实测确认过这个现象。

        全量重绘的代价很小（几十个 item），换来的是永远不会叠字。
        """
        # 窗口还没真正布局出来时 winfo_width() 会返回 1。
        # 这时**先不画**，等下一帧再画——否则会把内容按 80px 折行，
        # 折出来的排版是错的。
        raw_w = self.canvas.winfo_width()
        if raw_w <= 8:
            self.canvas.update_idletasks()
            raw_w = self.canvas.winfo_width()
            if raw_w <= 8:
                self._pending_rows = rows
                return
        self._pending_rows = None

        cw = max(80, raw_w)
        pad = 10
        inner = max(60, cw - pad * 2 - 8)

        # ---- 第一遍：算出每一块的内容与高度 ----
        blocks: list[tuple[float, str, list[str], str, list[str], bool, int]] = []
        live_sid = rows[-1].sid if rows else -1
        y = 0
        for line in rows:
            is_live = (line.sid == live_sid) and not line.final
            if line.failed:
                tcolor = COLOR_TARGET_FAIL
            elif is_live:
                tcolor = COLOR_TARGET_LIVE
            else:
                tcolor = COLOR_TARGET

            translated = line.translated or ("…" if is_live else "")
            source = line.source
            if not translated and not source:
                continue

            t_rows = self._wrap(translated, self.f_target, inner) if translated else []
            s_rows = self._wrap(source, self.f_source, inner) if source else []
            h = self._measure(t_rows, s_rows)
            blocks.append((y, translated, t_rows, tcolor, s_rows, is_live, h))
            y = int(y + h)

        total = max(y, 1)

        # ---- 第二遍：全量重绘 ----
        self.canvas.delete("all")
        self._rendered.clear()
        self._order.clear()
        self._placeholder_ids.clear()

        if not blocks:
            # 一行内容都没有时给个提示，不然用户看到黑框会以为坏了
            self._content_h = 1
            self.canvas.configure(scrollregion=(0, 0, cw, 40))
            self._placeholder_ids.append(
                self.canvas.create_text(
                    14, 12,
                    text="等待声音…（请确认直播正在播放，且有说话声）",
                    font=self.f_source,
                    fill=COLOR_TEXT_DIM,
                    anchor="nw",
                )
            )
            return

        for top, translated, t_rows, tcolor, s_rows, is_live, h in blocks:
            self._paint_block(int(top), t_rows, tcolor, s_rows, is_live, cw, inner)

        self._content_h = total
        self.canvas.configure(scrollregion=(0, 0, cw, total + 8))

        if self.auto_scroll and not self._user_scrolled:
            self.scroll_to_bottom()

    # ------------------------------------------------------------------
    def _measure(self, t_rows: list[str], s_rows: list[str]) -> float:
        """算一块（译文若干行 + 原文若干行）需要多高。"""
        h = 4.0
        if t_rows:
            h += len(t_rows) * (self.f_target.metrics("linespace") + 2)
        if s_rows:
            h += 2 + len(s_rows) * (self.f_source.metrics("linespace") + 2)
        return h + 6

    def _paint_block(
        self,
        top: int,
        t_rows: list[str],
        tcolor: str,
        s_rows: list[str],
        is_live: bool,
        width: int,
        inner: int,
    ) -> None:
        """按算好的行内容绘制一块。"""
        pad = 10
        y = top + 4

        if self.show_panel:
            bg = self.bg_live if is_live else self.bg
            self.canvas.create_rectangle(
                pad, top + 1, width - pad, top + self._measure(t_rows, s_rows) - 2,
                fill=bg, outline="",
                stipple="" if self.opaque_bg else "gray50",
            )

        for row in t_rows:
            self._outlined_text(pad + 6, y, row, self.f_target, tcolor)
            y += self.f_target.metrics("linespace") + 2

        if s_rows:
            scolor = COLOR_SOURCE_DIM if is_live else COLOR_SOURCE
            y += 2
            for row in s_rows:
                self._outlined_text(pad + 6, y, row, self.f_source, scolor)
                y += self.f_source.metrics("linespace") + 2

    def _outlined_text(self, x: int, y: int, text: str, font: tkfont.Font, color: str) -> None:
        """画一遍描边、再画一遍填充。

        描边是 8 个方向的黑色副本：直播画面明暗不定，没有描边的白字经常读不清。
        """
        for dx, dy in _OUTLINE_OFFSETS:
            self.canvas.create_text(
                x + dx, y + dy, text=text, font=font, fill=OUTLINE, anchor="nw"
            )
        self.canvas.create_text(x, y, text=text, font=font, fill=color, anchor="nw")

    # ------------------------------------------------------------------
    def _wrap(self, text: str, font: tkfont.Font, width: int) -> list[str]:
        """按像素宽度折行。中文按字符断，英文尽量按词断。"""
        out: list[str] = []
        for raw in (text or "").split("\n"):
            if font.measure(raw) <= width:
                out.append(raw)
                continue
            cur = ""
            last_space = -1
            for ch in raw:
                if font.measure(cur + ch) <= width:
                    cur += ch
                    if ch == " ":
                        last_space = len(cur) - 1
                    continue
                if last_space > 0 and len(cur) - last_space < 14:
                    out.append(cur[:last_space])
                    cur = cur[last_space + 1 :] + ch
                else:
                    out.append(cur)
                    cur = ch
                last_space = -1
            if cur:
                out.append(cur)
        return out or [""]

    def _clear(self) -> None:
        self.canvas.delete("all")
        self._rendered.clear()
        self._order.clear()
        self._content_h = 0

    def set_message(self, text: str, color: str = COLOR_TEXT_DIM) -> None:
        """面板里还没有内容时显示一句提示。"""
        self._clear()
        self.canvas.create_text(
            14, 12, text=text, font=self.f_source, fill=color, anchor="nw"
        )
