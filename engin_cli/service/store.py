# -*- coding: utf-8 -*-
"""指标服务的系统表存储：任务、服务级设置、执行记录、调度日志。

默认存 MySQL（settings.service_store = "mysql"），和计算指标用同一个库（db.database），
表名沿用数仓命名规范，见 sql/sys_trf_social_opinion_service_ddl.sql：

    dim_trf_social_opinion_task_f              任务配置
    dim_trf_social_opinion_service_setting_f   服务级设置（定时总开关、推送设置、通知设置）
    dwd_trf_social_opinion_task_run_di         任务执行记录（travel_date 分区）
    dwd_trf_social_opinion_schedule_log_di     调度日志（travel_date 分区）

SQL 里一律写「库.表」。service_store = "sqlite" 只给单测和没有库时本机试用，表名、字段名与 MySQL 相同。
对外的字典键（id / name / status / created_at …）保持不变，页面和接口不用跟着改。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

TABLE_TASK = "dim_trf_social_opinion_task_f"
TABLE_SETTING = "dim_trf_social_opinion_service_setting_f"
TABLE_RUN = "dwd_trf_social_opinion_task_run_di"
TABLE_SCHEDULE_LOG = "dwd_trf_social_opinion_schedule_log_di"
TABLE_QUALITY = "dwd_trf_social_opinion_quality_check_di"
SYSTEM_TABLES = (TABLE_TASK, TABLE_SETTING, TABLE_RUN, TABLE_SCHEDULE_LOG, TABLE_QUALITY)

DDL_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "sql", "sys_trf_social_opinion_service_ddl.sql")

RUN_FINAL = ("success", "failed", "blocked", "cancelled")

# 执行记录：对外键 → 表字段
RUN_COLS = {
    "id": "run_id", "task_id": "task_id", "task_name": "task_name", "kind": "run_type",
    "trigger": "trigger_type", "status": "run_status", "command": "command_text", "title": "run_title",
    "created_at": "create_time", "started_at": "start_time", "finished_at": "finish_time",
    "exit_code": "exit_code", "log_path": "log_path", "summary": "summary_json", "message": "run_message",
}

# SQLite 版（单测 / 本机试用）：字段与 MySQL 一致，只是没有注释和库名
SQLITE_DDL = f"""
create table if not exists {TABLE_TASK} (
    task_id integer primary key autoincrement, task_name text not null default '',
    task_status integer not null default 1, task_config text, schedule_config text, last_fire_config text,
    create_time text not null, update_time text not null);
create table if not exists {TABLE_SETTING} (
    setting_code text primary key, setting_value text, update_time text not null);
create table if not exists {TABLE_RUN} (
    run_id integer primary key autoincrement, travel_date integer not null, task_id integer,
    task_name text not null default '', run_type text not null default 'run',
    trigger_type text not null default 'manual', run_status text not null default 'queued',
    command_text text, run_title text not null default '', create_time text not null,
    start_time text, finish_time text, exit_code integer, log_path text not null default '',
    summary_json text, run_message text not null default '');
create index if not exists idx_task_run_date on {TABLE_RUN} (travel_date);
create index if not exists idx_task_run_task on {TABLE_RUN} (task_id, run_id);
create table if not exists {TABLE_SCHEDULE_LOG} (
    log_id integer primary key autoincrement, travel_date integer not null, log_time text not null,
    task_id integer, task_name text not null default '', log_level text not null default 'info',
    log_content text not null default '');
create index if not exists idx_schedule_log_date on {TABLE_SCHEDULE_LOG} (travel_date);
create table if not exists {TABLE_QUALITY} (
    check_id integer primary key autoincrement, travel_date integer not null,
    check_type text not null default 'run', trigger_type text not null default '', run_id integer,
    task_id integer, task_name text not null default '', scenic_id text not null default '',
    start_date integer, end_date integer, check_status text not null default 'pass',
    attempt_cnt integer not null default 1, item_cnt integer not null default 0, issue_cnt integer not null default 0,
    check_summary text not null default '', check_detail text, operator text not null default '',
    create_time text not null);
create index if not exists idx_quality_check_date on {TABLE_QUALITY} (travel_date);
"""

_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _ymd(ts: str) -> int:
    return int(ts[:10].replace("-", ""))


def _loads(text: Optional[str], default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _ts(v) -> Optional[str]:
    """MySQL 返回 datetime，SQLite 返回字符串 → 统一成 'YYYY-MM-DD HH:MM:SS'。"""
    if v is None:
        return None
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


def render_mysql_ddl(database: str) -> List[str]:
    """读 DDL 文件，表名前加上库名（「库.表」），拆成一条条语句。"""
    with open(DDL_FILE, encoding="utf-8") as f:
        text = f.read()
    text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("--"))
    text = re.sub(r"(create table if not exists|\bon)\s+((?:dim|dwd)_trf_social_opinion_\w+)",
                  rf"\1 `{database}`.\2", text)
    return [s.strip() for s in text.split(";") if s.strip()]


class StoreError(RuntimeError):
    pass


class Store:
    """系统表读写。用 Store.sqlite(path) / Store.mysql(db_cfg) 创建。"""

    def __init__(self, path: Optional[str] = None, *, mysql_cfg=None, database: str = "",
                 auto_create: bool = True):
        self._lock = threading.RLock()
        if mysql_cfg is None:
            self.kind = "sqlite"
            self.path = path or ":memory:"
            if self.path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.executescript(SQLITE_DDL)
            self._db.commit()
            q = lambda t: t                                        # noqa: E731
        else:
            self.kind = "mysql"
            self._cfg = mysql_cfg
            db = database or mysql_cfg.database
            if not db or not _IDENT.match(db):
                raise StoreError(f"系统表的库名不合法或没配：{db!r}（settings_local.py 里 DB 的 database）")
            self.database = db
            self._db = None
            self._connect()
            q = lambda t: f"`{db}`.`{t}`"                         # noqa: E731
            if auto_create:
                self._ensure_tables()
        self.t_task, self.t_setting = q(TABLE_TASK), q(TABLE_SETTING)
        self.t_run, self.t_log = q(TABLE_RUN), q(TABLE_SCHEDULE_LOG)
        self.t_quality = q(TABLE_QUALITY)

    @classmethod
    def sqlite(cls, path: str) -> "Store":
        return cls(path)

    @classmethod
    def mysql(cls, db_cfg, database: str = "", auto_create: bool = True) -> "Store":
        return cls(mysql_cfg=db_cfg, database=database, auto_create=auto_create)

    def describe(self) -> str:
        if self.kind == "sqlite":
            return f"SQLite {self.path}"
        c = self._cfg
        return f"MySQL {c.host}:{c.port} 库 {self.database}（{TABLE_TASK} 等 {len(SYSTEM_TABLES)} 张系统表）"

    def describe_parts(self) -> tuple:
        """系统状态面板用：(主行：在哪, 副行：几张什么表)。"""
        names = "任务、设置、执行记录、调度日志、质量检测记录"
        if self.kind == "sqlite":
            return f"SQLite {os.path.basename(self.path)}", f"{len(SYSTEM_TABLES)} 张系统表：{names}"
        c = self._cfg
        return f"MySQL {c.host}:{c.port} · 库 {self.database}", f"{len(SYSTEM_TABLES)} 张系统表：{names}"

    # ------------------------------------------------------------ 连接
    def _connect(self):
        import pymysql
        import pymysql.cursors
        c = self._cfg
        try:
            self._db = pymysql.connect(host=c.host, port=int(c.port), user=c.user, password=c.password,
                                       database=c.database or None, charset=c.charset, autocommit=True,
                                       connect_timeout=c.connect_timeout, read_timeout=60, write_timeout=60,
                                       cursorclass=pymysql.cursors.DictCursor)
        except Exception as e:                       # noqa: BLE001
            raise StoreError(f"连不上 MySQL {c.host}:{c.port}：{e}") from e

    def _ensure_tables(self):
        rows = self._all("select table_name as t from information_schema.tables where table_schema=?",
                         (self.database,))
        have = {r["t"] for r in rows}
        missing = [f"{self.database}.{t}" for t in SYSTEM_TABLES if t not in have]
        if not missing:
            return
        log.warning("系统表不存在，自动建表：%s", "、".join(missing))
        for stmt in render_mysql_ddl(self.database):
            try:
                self._exec(stmt)
            except Exception as e:                   # noqa: BLE001
                if getattr(e, "args", [None])[0] == 1061:       # 索引已存在（表之前建过一半）
                    continue
                raise StoreError(f"自动建系统表失败：{e}\n账号没有建表权限的话，请 DBA 在库 {self.database} "
                                 f"里执行 {DDL_FILE}") from e

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except Exception:                        # noqa: BLE001
                pass

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.kind == "mysql" else sql

    def _cursor(self):
        if self.kind == "mysql":
            try:
                self._db.ping(reconnect=True)        # 服务常驻，连接会被 wait_timeout 断掉
            except Exception:                        # noqa: BLE001
                self._connect()
        return self._db.cursor()

    def _exec(self, sql: str, params=()):
        with self._lock:
            cur = self._cursor()
            # 没参数时传 None：pymysql 拿到参数（哪怕是空元组）会对 SQL 做 % 格式化
            if self.kind == "mysql" and not params:
                cur.execute(sql)
            else:
                cur.execute(self._sql(sql), tuple(params))
            if self.kind == "sqlite":
                self._db.commit()
            return cur

    def _all(self, sql: str, params=()) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._cursor()
            cur.execute(self._sql(sql), tuple(params))
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------ 任务
    @staticmethod
    def _task(r: dict) -> Dict[str, Any]:
        return {"id": int(r["task_id"]), "name": r["task_name"], "enabled": bool(r["task_status"]),
                "config": _loads(r["task_config"], {}), "schedule": _loads(r["schedule_config"], {}),
                "last_fired": _loads(r["last_fire_config"], {}),
                "created_at": _ts(r["create_time"]), "updated_at": _ts(r["update_time"])}

    def list_tasks(self) -> List[Dict[str, Any]]:
        return [self._task(r) for r in self._all(f"select * from {self.t_task} order by task_id")]

    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        rows = self._all(f"select * from {self.t_task} where task_id=?", (int(task_id),))
        return self._task(rows[0]) if rows else None

    def create_task(self, name: str, config: dict, schedule: dict, enabled: bool = True) -> int:
        t = now_str()
        cur = self._exec(f"insert into {self.t_task} (task_name, task_status, task_config, schedule_config,"
                         f" last_fire_config, create_time, update_time) values (?,?,?,?,?,?,?)",
                         (name, int(bool(enabled)), json.dumps(config, ensure_ascii=False),
                          json.dumps(schedule, ensure_ascii=False), "{}", t, t))
        return int(cur.lastrowid)

    def update_task(self, task_id: int, name: str, config: dict, schedule: dict,
                    enabled: bool) -> None:
        self._exec(f"update {self.t_task} set task_name=?, task_status=?, task_config=?, schedule_config=?,"
                   f" update_time=? where task_id=?",
                   (name, int(bool(enabled)), json.dumps(config, ensure_ascii=False),
                    json.dumps(schedule, ensure_ascii=False), now_str(), int(task_id)))

    def set_task_enabled(self, task_id: int, enabled: bool) -> None:
        self._exec(f"update {self.t_task} set task_status=?, update_time=? where task_id=?",
                   (int(bool(enabled)), now_str(), int(task_id)))

    def set_last_fired(self, task_id: int, last_fired: dict) -> None:
        self._exec(f"update {self.t_task} set last_fire_config=? where task_id=?",
                   (json.dumps(last_fired), int(task_id)))

    def delete_task(self, task_id: int) -> None:
        self._exec(f"delete from {self.t_task} where task_id=?", (int(task_id),))

    # ------------------------------------------------------------ 服务级设置
    def get_kv(self, key: str, default=None):
        rows = self._all(f"select setting_value from {self.t_setting} where setting_code=?", (key,))
        return _loads(rows[0]["setting_value"], default) if rows else default

    def set_kv(self, key: str, value) -> None:
        v = json.dumps(value, ensure_ascii=False)
        if self.kind == "mysql":
            self._exec(f"insert into {self.t_setting} (setting_code, setting_value, update_time) values (?,?,?)"
                       f" on duplicate key update setting_value=values(setting_value),"
                       f" update_time=values(update_time)", (key, v, now_str()))
        else:
            self._exec(f"insert into {self.t_setting} (setting_code, setting_value, update_time) values (?,?,?)"
                       f" on conflict(setting_code) do update set setting_value=excluded.setting_value,"
                       f" update_time=excluded.update_time", (key, v, now_str()))

    # ------------------------------------------------------------ 调度日志
    def add_event(self, text: str, task: Optional[dict] = None, level: str = "info") -> None:
        t = now_str()
        self._exec(f"insert into {self.t_log} (travel_date, log_time, task_id, task_name, log_level, log_content)"
                   f" values (?,?,?,?,?,?)",
                   (_ymd(t), t, task["id"] if task else None, task["name"] if task else "", level, text[:1000]))

    @staticmethod
    def _event(r: dict) -> Dict[str, Any]:
        return {"id": int(r["log_id"]), "ts": _ts(r["log_time"]), "task_id": r["task_id"],
                "task_name": r["task_name"], "level": r["log_level"], "text": r["log_content"]}

    def list_events(self, task_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if task_id:
            rows = self._all(f"select * from {self.t_log} where task_id=? or task_id is null "
                             f"order by log_id desc limit ?", (int(task_id), int(limit)))
        else:
            rows = self._all(f"select * from {self.t_log} order by log_id desc limit ?", (int(limit),))
        return [self._event(r) for r in rows]

    def delete_events_before(self, before: str) -> int:
        return self._exec(f"delete from {self.t_log} where log_time<?", (before,)).rowcount

    # ------------------------------------------------------------ 数据质量检测记录
    def add_check(self, *, check_type: str, trigger: str, scenic: str, start: str, end: str, status: str,
                  attempts: int, items: int, issues: int, summary: str, detail: dict,
                  run_id: Optional[int] = None, task: Optional[dict] = None, operator: str = "") -> int:
        t = now_str()
        cur = self._exec(
            f"insert into {self.t_quality} (travel_date, check_type, trigger_type, run_id, task_id, task_name,"
            f" scenic_id, start_date, end_date, check_status, attempt_cnt, item_cnt, issue_cnt, check_summary,"
            f" check_detail, operator, create_time) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_ymd(t), check_type, trigger, run_id, task["id"] if task else None, task["name"] if task else "",
             scenic, int(start) if start else None, int(end) if end else None, status, int(attempts),
             int(items), int(issues), summary[:1000], json.dumps(detail, ensure_ascii=False, default=str),
             operator[:64], t))
        return int(cur.lastrowid)

    @staticmethod
    def _check(r: dict, full: bool = False) -> Dict[str, Any]:
        d = {"id": int(r["check_id"]), "type": r["check_type"], "trigger": r["trigger_type"], "run_id": r["run_id"],
             "task_id": r["task_id"], "task_name": r["task_name"], "scenic": r["scenic_id"],
             "start": str(r["start_date"] or ""), "end": str(r["end_date"] or ""), "status": r["check_status"],
             "attempts": int(r["attempt_cnt"] or 0), "items": int(r["item_cnt"] or 0),
             "issues": int(r["issue_cnt"] or 0), "summary": r["check_summary"], "operator": r["operator"],
             "created_at": _ts(r["create_time"])}
        if full:
            d["detail"] = _loads(r["check_detail"], {})
        return d

    def list_checks(self, status: str = "", check_type: str = "", scenic: str = "", since: str = "",
                    kw: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        where, params = ["1=1"], []
        for col, v in (("check_status", status), ("check_type", check_type), ("scenic_id", scenic)):
            if v:
                where.append(f"{col}=?")
                params.append(v)
        if since:
            where.append("travel_date>=?")
            params.append(_ymd(since))
        if kw:
            where.append("(scenic_id like ? or task_name like ? or check_summary like ?)")
            params += [f"%{kw}%"] * 3
        w = " and ".join(where)
        cols = ("check_id, check_type, trigger_type, run_id, task_id, task_name, scenic_id, start_date, end_date,"
                " check_status, attempt_cnt, item_cnt, issue_cnt, check_summary, operator, create_time")
        total = self._all(f"select count(*) as n from {self.t_quality} where {w}", params)[0]["n"]
        rows = self._all(f"select {cols} from {self.t_quality} where {w} order by check_id desc limit ? offset ?",
                         params + [int(limit), int(offset)])
        counts = {r["check_status"]: int(r["n"]) for r in self._all(
            f"select check_status, count(*) as n from {self.t_quality} where {w} group by check_status", params)}
        return {"total": int(total), "counts": counts, "items": [self._check(r) for r in rows]}

    def get_check(self, check_id: int) -> Optional[Dict[str, Any]]:
        rows = self._all(f"select * from {self.t_quality} where check_id=?", (int(check_id),))
        return self._check(rows[0], full=True) if rows else None

    def delete_checks_before(self, before: str) -> int:
        return self._exec(f"delete from {self.t_quality} where create_time<?", (before,)).rowcount

    # ------------------------------------------------------------ 执行记录
    @staticmethod
    def _run(r: dict, full: bool = False) -> Dict[str, Any]:
        d = {"id": int(r["run_id"]), "task_id": r["task_id"], "task_name": r["task_name"],
             "kind": r["run_type"], "trigger": r["trigger_type"], "status": r["run_status"],
             "title": r["run_title"], "created_at": _ts(r["create_time"]),
             "started_at": _ts(r["start_time"]), "finished_at": _ts(r["finish_time"]),
             "exit_code": r["exit_code"], "message": r["run_message"]}
        summary = _loads(r["summary_json"], {})
        scenics = summary.get("scenics") or []
        d["scenic_total"] = len(scenics)
        d["scenic_failed"] = sum(1 for s in scenics if not s.get("ok"))
        d["quality_blocked"] = sum(1 for s in scenics if s.get("quality_blocked"))
        if full:
            d["command"] = _loads(r["command_text"], [])
            d["summary"] = summary
            d["log_path"] = r["log_path"]
        return d

    def create_run(self, kind: str, command: List[str], title: str, trigger: str,
                   task: Optional[dict] = None) -> int:
        t = now_str()
        cur = self._exec(
            f"insert into {self.t_run} (travel_date, task_id, task_name, run_type, trigger_type, run_status,"
            f" command_text, run_title, create_time, log_path, summary_json, run_message)"
            f" values (?,?,?,?,?,'queued',?,?,?,'','{{}}','')",
            (_ymd(t), task["id"] if task else None, task["name"] if task else "", kind, trigger,
             json.dumps(command, ensure_ascii=False), title[:512], t))
        return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields) -> None:
        if not fields:
            return
        if "summary" in fields and not isinstance(fields["summary"], str):
            fields["summary"] = json.dumps(fields["summary"], ensure_ascii=False, default=str)
        if "message" in fields:
            fields["message"] = str(fields["message"] or "")[:1000]
        bad = [k for k in fields if k not in RUN_COLS or k == "id"]
        if bad:
            raise KeyError(f"执行记录没有这些字段：{bad}")
        cols = ", ".join(f"{RUN_COLS[k]}=?" for k in fields)
        self._exec(f"update {self.t_run} set {cols} where run_id=?", tuple(fields.values()) + (int(run_id),))

    def get_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        rows = self._all(f"select * from {self.t_run} where run_id=?", (int(run_id),))
        return self._run(rows[0], full=True) if rows else None

    def list_runs(self, status: str = "", task_id: Optional[int] = None, kind: str = "",
                  since: str = "", limit: int = 50, offset: int = 0,
                  full: bool = False) -> Dict[str, Any]:
        where, params = ["1=1"], []
        if status:
            where.append("run_status=?")
            params.append(status)
        if task_id:
            where.append("task_id=?")
            params.append(int(task_id))
        if kind:
            where.append("run_type=?")
            params.append(kind)
        if since:
            where.append("travel_date>=?")
            params.append(_ymd(since))
        w = " and ".join(where)
        total = self._all(f"select count(*) as n from {self.t_run} where {w}", params)[0]["n"]
        rows = self._all(f"select * from {self.t_run} where {w} order by run_id desc limit ? offset ?",
                         params + [int(limit), int(offset)])
        return {"total": int(total), "items": [self._run(r, full=full) for r in rows]}

    def runs_between(self, since: str) -> List[Dict[str, Any]]:
        """since（'YYYY-MM-DD…'）当天及之后的执行记录，按分区字段 travel_date 过滤。"""
        return [self._run(r, full=True) for r in
                self._all(f"select * from {self.t_run} where travel_date>=? order by run_id", (_ymd(since),))]

    def last_run_of(self, task_id: int) -> Optional[Dict[str, Any]]:
        rows = self._all(f"select * from {self.t_run} where task_id=? order by run_id desc limit 1",
                         (int(task_id),))
        return self._run(rows[0]) if rows else None

    def mark_interrupted(self) -> int:
        """服务重启：上次没跑完的记录标成失败。"""
        return self._exec(f"update {self.t_run} set run_status='failed', finish_time=?, "
                          f"run_message='服务重启，执行被中断' where run_status in ('queued','running')",
                          (now_str(),)).rowcount

    def delete_runs_before(self, before: str) -> List[str]:
        """删掉 before 之前已结束的执行记录，返回它们的日志路径（由调用方删文件）。"""
        cond = (f"create_time<? and run_status in ('success','failed','blocked','cancelled')")
        rows = self._all(f"select log_path from {self.t_run} where {cond}", (before,))
        self._exec(f"delete from {self.t_run} where {cond}", (before,))
        return [r["log_path"] for r in rows if r["log_path"]]
