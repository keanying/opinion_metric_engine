# -*- coding: utf-8 -*-
"""定时：每个任务配若干个「每天 HH:MM」，到点就把任务交给执行器。

· 用服务器本地时间（客户机器是 CST）；
· 每个时刻每天只触发一次（tasks.last_fired 记着），服务重启也不会重复；
· 服务停机错过的时刻：GRACE 内（默认 30 分钟）补触发，更早的不补，免得一开机连跑一串。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
from typing import List, Optional

from .runner import Runner, build_task_argv
from .store import Store

log = logging.getLogger(__name__)

GRACE = dt.timedelta(minutes=30)
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def normalize_times(times) -> List[str]:
    """["8:10", " 20:10 ", "08:10"] → ["08:10", "20:10"]。格式错抛 ValueError。"""
    if isinstance(times, str):
        times = times.replace("，", ",").split(",")
    out = []
    for t in times or []:
        t = str(t).strip()
        if not t:
            continue
        m = _HHMM.match(t)
        if not m:
            raise ValueError(f"定时时间格式是 HH:MM（24 小时制），收到 {t!r}")
        t = f"{int(m.group(1)):02d}:{m.group(2)}"
        if t not in out:
            out.append(t)
    return sorted(out)


def due_slots(times: List[str], last_fired: dict, now: dt.datetime) -> List[str]:
    """now 这一刻该触发的时刻（到点了、在补触发窗口内、今天还没触发过）。"""
    today = now.strftime("%Y-%m-%d")
    out = []
    for t in times:
        h, m = map(int, t.split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if slot <= now < slot + GRACE and last_fired.get(t) != today:
            out.append(t)
    return out


def missed_slots(times: List[str], last_fired: dict, now: dt.datetime) -> List[str]:
    """今天已经过了补触发窗口、却没触发过的时刻（服务停着 / 定时被暂停）。记一笔日志，不补跑。"""
    today = now.strftime("%Y-%m-%d")
    out = []
    for t in times:
        h, m = map(int, t.split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now >= slot + GRACE and last_fired.get(t) != today:
            out.append(t)
    return out


def next_fire(times: List[str], now: dt.datetime) -> Optional[dt.datetime]:
    """下一次触发时间（页面展示用）。"""
    cands = []
    for t in times:
        h, m = map(int, t.split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        cands.append(slot if slot > now else slot + dt.timedelta(days=1))
    return min(cands) if cands else None


class Scheduler:
    def __init__(self, store: Store, runner: Runner, interval: float = 20.0):
        self.store = store
        self.runner = runner
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    @property
    def enabled(self) -> bool:
        """定时总开关（任务页顶上那个）。关掉后所有任务都不定时触发，只能手动执行。"""
        return bool((self.store.get_kv("scheduler", {}) or {}).get("enabled", True))

    def set_enabled(self, on: bool) -> None:
        self.store.set_kv("scheduler", {"enabled": bool(on)})
        self.store.add_event("定时总开关已" + ("打开" if on else "关闭（到点不再自动执行）"),
                             level="info" if on else "warn")

    def tick(self, now: Optional[dt.datetime] = None) -> List[int]:
        """检查一遍，触发到点的任务。返回新建的执行记录 id。"""
        now = now or dt.datetime.now()
        today = now.strftime("%Y-%m-%d")
        on = self.enabled
        fired = []
        for task in self.store.list_tasks():
            if not task["enabled"]:
                continue
            try:
                times = normalize_times((task["schedule"] or {}).get("times"))
            except ValueError:
                continue
            last = dict(task["last_fired"] or {})
            slots = due_slots(times, last, now) if on else []
            missed = missed_slots(times, last, now)
            # 定时暂停期间到点的不触发；等它过了补触发窗口，由 missed 记一笔「跳过」
            if not (slots or missed):
                continue
            for t in slots + missed:
                last[t] = today
            self.store.set_last_fired(task["id"], last)     # 先记下，再触发：出错也不会反复触发
            if missed:
                why = "定时总开关关着" if not on else "服务当时没在运行"
                # 新建 / 刚改的任务，今天早就过了的时刻不算「错过」
                if task["updated_at"][:10] != today or task["updated_at"][11:16] < min(missed):
                    self.store.add_event(f"跳过 {'、'.join(missed)}：{why}，"
                                         f"超过 {int(GRACE.total_seconds() // 60)} 分钟不补跑",
                                         task, "warn")
            if not slots:
                continue
            try:
                kind, argv, title = build_task_argv(task["config"], now.date())
            except ValueError as e:
                log.error("任务 %s 配置有误，跳过定时：%s", task["name"], e)
                self.store.add_event(f"{'、'.join(slots)} 到点，但任务配置有误，没有执行：{e}",
                                     task, "error")
                continue
            busy = self.runner.current is not None or self.runner.queued() > 0
            run_id = self.runner.submit(kind, argv, title, trigger="schedule", task=task)
            fired.append(run_id)
            self.store.add_event(f"{'、'.join(slots)} 到点，已触发执行 #{run_id}（{title}）"
                                 + ("，前面还有执行没跑完，先排队" if busy else ""), task)
            log.info("定时触发任务「%s」（%s）", task["name"], "、".join(slots))
        return fired

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:                        # noqa: BLE001 —— 调度线程不能死
                log.exception("定时检查出错")
