"""字幕状态：连接管线线程与 UI 线程的唯一桥梁。

tkinter 必须跑在主线程，而音频/网络跑在别的处，因此这里用一个带锁的小对象
保存"当前显示什么"，UI 线程按固定频率（25Hz）拉取。比满屏 event 更好调试，
也天然做了"UI 卡顿不拖慢识别"的隔离。

行模型
------
统一用一个 `list[SubtitleLine]`，最后一行是"正在生成的那句"（final=False），
之前的都是已定稿的。每行有唯一 `sid`（sentence id），翻译是异步回来的，
提交时必须带上 sid，回来时校验 sid 是否还匹配——否则快速说话时会出现
"上一句的译文贴到下一句上"的错位。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class SubtitleLine:
    sid: int
    source: str = ""
    translated: str = ""
    final: bool = False
    created_at: float = field(default_factory=time.monotonic)
    e2e_ms: float = 0.0
    failed: bool = False


@dataclass
class Status:
    state: str = "初始化中"
    detail: str = ""
    asr_events: int = 0
    translations: int = 0
    errors: int = 0
    paused: bool = False
    e2e_ms: float = 0.0


class SubtitleState:
    """线程安全的字幕与状态容器。"""

    def __init__(self, max_lines: int = 2, history_lines: int = 200) -> None:
        self._lock = threading.Lock()
        self.max_lines = max(1, int(max_lines))
        # 历史保留条数。悬浮窗现在是一个"固定大小的面板"（类似桌面歌词），
        # 用户把窗口拉大就该看到更多历史，所以不能只留几行。
        self.history_lines = max(self.max_lines + 2, int(history_lines))
        self._lines: list[SubtitleLine] = []
        self._status = Status()
        self._sid = 0
        self._dirty = True
        self._closed = False

    # ---------- 句子生命周期（管线线程调用）----------
    def begin_sentence(self) -> int:
        """开一句新的：上一句若还挂着就定稿它，返回新行的 sid。"""
        with self._lock:
            if self._lines and not self._lines[-1].final:
                self._lines[-1].final = True
            self._sid += 1
            self._lines.append(SubtitleLine(sid=self._sid))
            self._trim()
            self._dirty = True
            return self._sid

    def set_source(self, sid: int, source: str, final: bool = False) -> None:
        with self._lock:
            line = self._find(sid)
            if line is None:
                return
            line.source = source
            if final:
                line.final = True
            self._dirty = True

    def patch_translation(
        self, sid: int, translated: str, final: bool, e2e_ms: float = 0.0, failed: bool = False
    ) -> bool:
        """把译文写到指定行。返回是否命中（sid 不匹配说明这句已经被顶掉了）。"""
        with self._lock:
            line = self._find(sid)
            if line is None:
                return False
            line.translated = translated
            line.failed = failed
            if e2e_ms:
                line.e2e_ms = e2e_ms
            if final:
                line.final = True
            self._dirty = True
            return True

    def commit(self, sid: int) -> None:
        with self._lock:
            line = self._find(sid)
            if line is not None:
                line.final = True
                self._dirty = True

    def close_current(self) -> None:
        with self._lock:
            if self._lines:
                self._lines[-1].final = True
                self._dirty = True

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._dirty = True

    def _find(self, sid: int) -> SubtitleLine | None:
        for line in reversed(self._lines):
            if line.sid == sid:
                return line
        return None

    def _trim(self) -> None:
        # 保留较多历史：悬浮窗是可拉伸的固定面板，拉大就能看到更多内容。
        # 上限 history_lines，避免长时间运行内存无限增长。
        if len(self._lines) > self.history_lines:
            del self._lines[: len(self._lines) - self.history_lines]

    # ---------- 状态（管线线程调用）----------
    def set_status(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._status, k):
                    setattr(self._status, k, v)
            self._dirty = True

    def bump(self, **counters: int) -> None:
        with self._lock:
            for k, delta in counters.items():
                if hasattr(self._status, k):
                    setattr(self._status, k, getattr(self._status, k) + delta)
            self._dirty = True

    # ---------- 读取（UI 线程调用）----------
    def snapshot(self) -> tuple[list[SubtitleLine], Status]:
        with self._lock:
            return list(self._lines), Status(**vars(self._status))

    def take_dirty(self) -> bool:
        with self._lock:
            changed = self._dirty
            self._dirty = False
            return changed
