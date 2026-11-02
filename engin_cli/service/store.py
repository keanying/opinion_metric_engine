# -*- coding: utf-8 -*-
"""指标服务的本地存储（SQLite，标准库）：任务、执行记录。

只存服务自己的东西（任务配置、每次执行的状态 / 命令 / 结果摘要）；
指标数据本身仍在 MySQL 的 ADS 表里，日志在 <data_dir>/runs/<id>.log。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

SCHEMA = """
create table if not exists tasks (
    id          integer primary key autoincrement,
    name        text    not null,
    enabled     integer not null default 1,
    config      text    not null default '{}',   -- 跑批参数（见 runner.TASK_DEFAULTS）
    schedule    text    not null default '{}',   -- {"times": ["08:10", "20:10"]}
    last_fired  text    not null default '{}',   -- {"08:10": "2026-11-02"} 防止同一时刻重复触发
    created_at  text    not null,
    updated_at  text    not null
);
create table if not exists runs (
    id          integer primary key autoincrement,
    task_id     integer,
    task_name   text    not null default '',
    kind        text    not null default 'run',  -- run / push / notify
    trigger     text    not null default 'manual',  -- schedule / manual
    status      text    not null default 'queued',  -- queued/running/success/failed/blocked/cancelled
    command     text    not null default '[]',
    title       text    not null default '',     -- 一句话说明（景区 + 日期）
    created_at  text    not null,
    started_at  text,
    finished_at text,
    exit_code   integer,
    log_path    text    not null default '',
    summary     text    not null default '{}',   -- run --summary-json 的内容
    message     text    not null default ''      -- 失败原因 / 备注
);
create table if not exists kv (                -- 服务级设置：定时总开关、推送设置 …
    key   text primary key,
    value text not null
);
create table if not exists events (            -- 调度日志：触发了谁、跳过了谁、为什么
    id        integer primary key autoincrement,
    ts        text    not null,
    task_id   integer,
    task_name text    not null default '',
    level     text    not null default 'info', -- info / warn / error
    text      text    not null
);
create index if not exists idx_events_ts on events (ts);
create index if not exists idx_runs_created on runs (created_at);
create index if not exists idx_runs_task on runs (task_id, created_at);
"""

RUN_FINAL = ("success", "failed", "blocked", "cancelled")


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _loads(text: Optional[str], default):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _all(self, sql: str, params=()) -> List[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # ------------------------------------------------------------ 任务
    @staticmethod
    def _task(r: sqlite3.Row) -> Dict[str, Any]:
        return {"id": r["id"], "name": r["name"], "enabled": bool(r["enabled"]),
                "config": _loads(r["config"], {}), "schedule": _loads(r["schedule"], {}),
                "last_fired": _loads(r["last_fired"], {}),
                "created_at": r["created_at"], "updated_at": r["updated_at"]}

    def list_tasks(self) -> List[Dict[str, Any]]:
        return [self._task(r) for r in self._all("select * from tasks order by id")]

    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        rows = self._all("select * from tasks where id=?", (int(task_id),))
        return self._task(rows[0]) if rows else None

    def create_task(self, name: str, config: dict, schedule: dict, enabled: bool = True) -> int:
        t = now_str()
        cur = self._exec("insert into tasks (name, enabled, config, schedule, created_at, updated_at)"
                         " values (?,?,?,?,?,?)",
                         (name, int(bool(enabled)), json.dumps(config, ensure_ascii=False),
                          json.dumps(schedule, ensure_ascii=False), t, t))
        return int(cur.lastrowid)

    def update_task(self, task_id: int, name: str, config: dict, schedule: dict,
                    enabled: bool) -> None:
        self._exec("update tasks set name=?, enabled=?, config=?, schedule=?, updated_at=? where id=?",
                   (name, int(bool(enabled)), json.dumps(config, ensure_ascii=False),
                    json.dumps(schedule, ensure_ascii=False), now_str(), int(task_id)))

    def set_task_enabled(self, task_id: int, enabled: bool) -> None:
        self._exec("update tasks set enabled=?, updated_at=? where id=?",
                   (int(bool(enabled)), now_str(), int(task_id)))

    def set_last_fired(self, task_id: int, last_fired: dict) -> None:
        self._exec("update tasks set last_fired=? where id=?",
                   (json.dumps(last_fired), int(task_id)))

    def delete_task(self, task_id: int) -> None:
        self._exec("delete from tasks where id=?", (int(task_id),))

    # ------------------------------------------------------------ 服务级设置
    def get_kv(self, key: str, default=None):
        rows = self._all("select value from kv where key=?", (key,))
        return _loads(rows[0]["value"], default) if rows else default

    def set_kv(self, key: str, value) -> None:
        self._exec("insert into kv (key, value) values (?, ?) "
                   "on conflict(key) do update set value=excluded.value",
                   (key, json.dumps(value, ensure_ascii=False)))

    # ------------------------------------------------------------ 调度日志
    def add_event(self, text: str, task: Optional[dict] = None, level: str = "info") -> None:
        self._exec("insert into events (ts, task_id, task_name, level, text) values (?,?,?,?,?)",
                   (now_str(), task["id"] if task else None, task["name"] if task else "",
                    level, text))

    def list_events(self, task_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if task_id:
            rows = self._all("select * from events where task_id=? or task_id is null "
                             "order by id desc limit ?", (int(task_id), int(limit)))
        else:
            rows = self._all("select * from events order by id desc limit ?", (int(limit),))
        return [dict(r) for r in rows]

    def delete_events_before(self, before: str) -> int:
        return self._exec("delete from events where ts<?", (before,)).rowcount

    # ------------------------------------------------------------ 执行记录
    @staticmethod
    def _run(r: sqlite3.Row, full: bool = False) -> Dict[str, Any]:
        d = {k: r[k] for k in ("id", "task_id", "task_name", "kind", "trigger", "status",
                               "title", "created_at", "started_at", "finished_at",
                               "exit_code", "message")}
        summary = _loads(r["summary"], {})
        scenics = summary.get("scenics") or []
        d["scenic_total"] = len(scenics)
        d["scenic_failed"] = sum(1 for s in scenics if not s.get("ok"))
        d["quality_blocked"] = sum(1 for s in scenics if s.get("quality_blocked"))
        if full:
            d["command"] = _loads(r["command"], [])
            d["summary"] = summary
            d["log_path"] = r["log_path"]
        return d

    def create_run(self, kind: str, command: List[str], title: str, trigger: str,
                   task: Optional[dict] = None) -> int:
        cur = self._exec(
            "insert into runs (task_id, task_name, kind, trigger, status, command, title, created_at)"
            " values (?,?,?,?, 'queued', ?,?,?)",
            (task["id"] if task else None, task["name"] if task else "", kind, trigger,
             json.dumps(command, ensure_ascii=False), title, now_str()))
        return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields) -> None:
        if not fields:
            return
        if "summary" in fields and not isinstance(fields["summary"], str):
            fields["summary"] = json.dumps(fields["summary"], ensure_ascii=False, default=str)
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"update runs set {cols} where id=?", tuple(fields.values()) + (int(run_id),))

    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        rows = self._all("select * from runs where id=?", (int(run_id),))
        return self._run(rows[0], full=True) if rows else None

    def list_runs(self, status: str = "", task_id: Optional[int] = None, kind: str = "",
                  since: str = "", limit: int = 50, offset: int = 0,
                  full: bool = False) -> Dict[str, Any]:
        where, params = ["1=1"], []
        if status:
            where.append("status=?")
            params.append(status)
        if task_id:
            where.append("task_id=?")
            params.append(int(task_id))
        if kind:
            where.append("kind=?")
            params.append(kind)
        if since:
            where.append("created_at>=?")
            params.append(since)
        w = " and ".join(where)
        total = self._all(f"select count(*) n from runs where {w}", params)[0]["n"]
        rows = self._all(f"select * from runs where {w} order by id desc limit ? offset ?",
                         params + [int(limit), int(offset)])
        return {"total": total, "items": [self._run(r, full=full) for r in rows]}

    def runs_between(self, since: str) -> List[Dict[str, Any]]:
        return [self._run(r, full=True)
                for r in self._all("select * from runs where created_at>=? order by id", (since,))]

    def last_run_of(self, task_id: int) -> Optional[Dict[str, Any]]:
        rows = self._all("select * from runs where task_id=? order by id desc limit 1", (int(task_id),))
        return self._run(rows[0]) if rows else None

    def mark_interrupted(self) -> int:
        """服务重启：上次没跑完的记录标成失败。"""
        cur = self._exec("update runs set status='failed', finished_at=?, "
                         "message='服务重启，执行被中断' where status in ('queued','running')",
                         (now_str(),))
        return cur.rowcount

    def delete_runs_before(self, before: str) -> List[str]:
        """删掉 before 之前已结束的执行记录，返回它们的日志路径（由调用方删文件）。"""
        rows = self._all("select log_path from runs where created_at<? and status in "
                         "('success','failed','blocked','cancelled')", (before,))
        self._exec("delete from runs where created_at<? and status in "
                   "('success','failed','blocked','cancelled')", (before,))
        return [r["log_path"] for r in rows if r["log_path"]]
