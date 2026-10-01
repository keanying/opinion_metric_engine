# -*- coding: utf-8 -*-
"""推送进度条（只用标准库，不引 tqdm —— 这套引擎跑在客户机器上，少一个依赖少一份装不上的风险）。

终端里：同一行原地刷新

    core_di           [##########..............]  12/30 批  40.0%   6,000 行  8s 剩余约 12s

不是终端（定时任务把输出重定向进日志文件）：不刷新，每推完 10% 打一行，
否则日志里会塞满几百行进度，或者因为 \\r 变成一行巨长的乱码。

重试、失败这些事件**单独起一行**打印出来，不会被进度条盖掉 ——
「卡住了」和「在重试」在屏幕上必须看得出区别。
"""

from __future__ import annotations

import sys
import time
from typing import Optional, TextIO

_PREFIX = "ads_trf_social_opinion_"


def short_table_name(table: str) -> str:
    """ads_trf_social_opinion_comment_core_di → comment_core_di，进度条里放不下全名。"""
    return table[len(_PREFIX):] if table.startswith(_PREFIX) else table


class PushProgress:
    """一张表的推送进度。pusher 在每批推完 / 每次重试时回调它。"""

    WIDTH = 24              # 进度条格数
    REFRESH = 0.1           # 终端刷新间隔（秒），批次很快时别把终端刷爆

    def __init__(self, table: str, total_batches: int, total_rows: int,
                 stream: Optional[TextIO] = None):
        self.label = short_table_name(table)
        self.total = max(int(total_batches), 1)
        self.total_rows = int(total_rows)
        self.stream = stream or sys.stderr
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.done = 0
        self.rows = 0
        self.failed = 0
        self.t0 = time.time()
        self._last_draw = 0.0
        self._last_decile = 0
        self._last_len = 0      # 上一次画的行宽，用空格覆盖掉旧内容（不用 ANSI 控制符，旧版 Windows 控制台不认）
        self._wait_note = ""    # 正在等对方响应时的读秒，显示在进度条末尾
        self._last_wait_line = 0.0

    # ---- pusher 回调 ----
    def start(self) -> None:
        if self.tty:
            self._draw(force=True)
        else:
            self._line(f"  推送 {self.label}：{self.total_rows:,} 行，{self.total} 批 …")

    def waiting(self, batch_no: int, attempt: int, secs: float, timeout: float) -> None:
        """请求已发出、还在等对方响应（每秒回调一次）。

        终端里在进度条末尾读秒；日志文件里每 30 秒打一行，证明程序还活着、是在等对方。
        """
        tag = f"（第 {attempt} 次）" if attempt > 1 else ""
        self._wait_note = f"  第 {batch_no} 批等待响应 {secs:.0f}s/{timeout:g}s{tag}"
        if self.tty:
            self._draw(force=True)
        elif secs - self._last_wait_line >= 30 or secs < self._last_wait_line:
            self._last_wait_line = secs
            self._line(f"    第 {batch_no} 批已等待 {secs:.0f}s，对方还没有响应（超时 {timeout:g}s）{tag}")

    def batch_done(self, rows: int, ok: bool) -> None:
        self._wait_note = ""
        self._last_wait_line = 0.0
        self.done += 1
        if ok:
            self.rows += int(rows)
        else:
            self.failed += 1
        if self.tty:
            self._draw()
        else:
            decile = int(self.done * 10 / self.total)
            if decile > self._last_decile and self.done < self.total:
                self._last_decile = decile
                self._line("  " + self._text())

    def retry(self, batch_no: int, attempt: int, retries: int, reason: str,
              wait: float) -> None:
        self._wait_note = ""
        self._last_wait_line = 0.0
        self._event(f"    第 {batch_no} 批失败：{reason}；{wait:g}s 后第 {attempt}/{retries} 次重试")

    def batch_failed(self, batch_no: int, reason: str) -> None:
        self._wait_note = ""
        self._event(f"    ✗ 第 {batch_no} 批放弃：{reason}")

    def finish(self, note: str = "") -> None:
        text = self._text() + (f"  {note}" if note else "")
        if self.tty:
            self._write(self._overwrite(text) + "\n")
            self._last_len = 0
        else:
            self._line("  " + text)

    # ---- 渲染 ----
    def _text(self) -> str:
        frac = min(self.done / self.total, 1.0)
        fill = int(round(frac * self.WIDTH))
        bar = "#" * fill + "." * (self.WIDTH - fill)
        el = time.time() - self.t0
        s = (f"{self.label:<34} [{bar}] {self.done:>4}/{self.total} 批 {frac:6.1%}"
             f" {self.rows:>9,} 行 {el:5.0f}s")
        if 0 < self.done < self.total:
            s += f" 剩余约 {el / self.done * (self.total - self.done):.0f}s"
        if self.failed:
            s += f"  失败 {self.failed} 批"
        return s + self._wait_note

    def _draw(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_draw < self.REFRESH and self.done < self.total:
            return
        self._last_draw = now
        self._write(self._overwrite(self._text()))

    def _event(self, msg: str) -> None:
        if self.tty:
            self._write(self._overwrite(msg) + "\n")
            self._last_len = 0
            self._draw(force=True)
        else:
            self._line(msg)

    def _overwrite(self, text: str) -> str:
        """回到行首写 text；比上一行短的部分用空格盖掉。中文按 2 格宽算。"""
        width = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
        pad = max(self._last_len - width, 0)
        self._last_len = width
        return "\r" + text + " " * pad

    def _line(self, msg: str) -> None:
        self._write(msg + "\n")

    def _write(self, s: str) -> None:
        try:
            self.stream.write(s)
            self.stream.flush()
        except (UnicodeEncodeError, ValueError, OSError):
            pass                    # 进度条写不出来不能把推送本身拖垮


def push_progress_factory(stream: Optional[TextIO] = None):
    """给 push_all 用的工厂：每张表开推时造一个进度条。"""
    def make(table: str, total_batches: int, total_rows: int) -> PushProgress:
        return PushProgress(table, total_batches, total_rows, stream=stream)
    return make
