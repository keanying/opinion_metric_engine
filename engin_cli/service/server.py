# -*- coding: utf-8 -*-
"""指标计算服务：HTTP API + 页面（Vue 3，静态文件，无需构建）。

    python -m engin_cli.cli serve            # 默认 http://127.0.0.1:8765

只用标准库（http.server / threading）+ 已有的 pymysql，不增加依赖。
任务、设置、执行记录、调度日志存 MySQL 系统表（store.py，数仓命名规范），执行日志是文件。

安全：
· 必须登录：用 SMC 系统的用户（src_opinion_sys_user，会话在 src_opinion_sys_user_session，见 auth.py）。
  页面静态文件不用登录（登录页要加载），
  /api/* 一律要登录；只有本机（不经过代理）请求 /api/health 例外，给 scripts/service.sh 检测用；
· 角色跟 SMC 一样：admin 全部；operator 不能改推送设置 / 定时总开关；viewer 只读（auth.denied_reason）；
· 所有改动类请求（含登录）要带 X-Requested-With 头，会话 Cookie 是 SameSite=Strict，挡住跨站提交（CSRF）。
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import gzip
import hmac
import itertools
import json
import logging
import math
import mimetypes
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from .runner import (APP_ROOT, PUSH_DEFAULTS, TABLE_KEYS, Runner, TASK_DEFAULTS, apply_push_switch,
                     build_push_argv, build_run_argv, build_task_argv, normalize_config, normalize_push_settings,
                     push_overrides, task_dates, normalize_notify_settings)
from .auth import COOKIE, SESSION_HOURS, Auth, AuthError, UserRepository, denied_reason
from .scheduler import GRACE, Scheduler, next_fire, normalize_times
from .store import Store, StoreError, now_str

log = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_req = threading.local()                       # 当前请求的登录用户（处理函数里要记操作人时用）


def current_user_name() -> str:
    u = getattr(_req, "user", None) or {}
    return str(u.get("display_name") or u.get("username") or "")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _clean(v: Any) -> Any:
    """转成 json 能序列化的值（DECIMAL / 日期 / NaN / numpy）。"""
    if v is None:
        return None
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.strftime("%Y-%m-%d %H:%M:%S" if isinstance(v, dt.datetime) else "%Y-%m-%d")
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if hasattr(v, "item") and callable(getattr(v, "item")):        # numpy 标量
        try:
            return _clean(v.item())
        except (TypeError, ValueError):
            return str(v)
    if isinstance(v, (str, int, bool)):
        return v
    return str(v)


_STATIC: Dict[str, tuple] = {}
_STATIC_LOCK = threading.Lock()


def _compressible(ctype: str) -> bool:
    return ctype.startswith(("text/", "application/json", "application/javascript", "image/svg"))


# 执行日志实时流（SSE）：/api/runs/<id>/log/stream?offset=已经拿到的字节数
TABLE_SCENIC = "src_opinion_social_scenic"   # 景区下拉的来源（使用方导入的景区清单）
_LOG_STREAM = re.compile(r"^/api/runs/(\d+)/log/stream$")


def _plugins_describe() -> List[dict]:
    from ..plugins import describe
    return describe()


def _utf8_complete(data: bytes) -> int:
    """data 里完整 UTF-8 字符的长度：末尾一个多字节字符只写了一半时，把那半个去掉（下次再读）。"""
    for k in range(1, min(4, len(data)) + 1):
        b = data[-k]
        if b & 0xC0 != 0x80:                      # 找到最后一个字符的首字节
            need = 4 if b >= 0xF0 else 3 if b >= 0xE0 else 2 if b >= 0xC0 else 1
            return len(data) - k if need > k else len(data)
    return len(data)


def _static_file(full: str, compress: bool) -> tuple:
    """静态文件（内容, gzip 后的内容 / None, ETag），按修改时间缓存在内存里：不用每次读盘、每次压缩。"""
    st = os.stat(full)
    key = (st.st_size, st.st_mtime_ns)
    hit = _STATIC.get(full)
    if hit and hit[0] == key:
        return hit[1]
    with open(full, "rb") as f:
        raw = f.read()
    gz = gzip.compress(raw, 6) if compress and len(raw) > 1024 else None
    val = (raw, gz, f'"{st.st_size:x}-{st.st_mtime_ns:x}"')
    with _STATIC_LOCK:
        _STATIC[full] = (key, val)
    return val


class DbPool:
    """数据库连接池：页面每个请求都要查库（登录校验、指标、景区列表 …），每次新建连接要几十毫秒（远程库更久），
    改成用完放回池里复用。

    · 还回来时先 rollback：连接是 autocommit=False，select 也会开事务，不结束的话下次读到的还是旧快照
    · 借出去前 ping 一下，库那边断开了（wait_timeout、网络闪断）就自动重连
    · 空闲太久（IDLE 秒）的直接丢掉重建；池里最多留 SIZE 个，多出来的用完就断开
    """
    SIZE, IDLE = 6, 600

    def __init__(self, make: Callable):
        self._make, self._idle, self._lock = make, [], threading.Lock()

    def get(self) -> "_Lease":
        while True:
            with self._lock:
                item = self._idle.pop() if self._idle else None
            if item is None:
                return _Lease(self, self._make())
            db, at = item
            if time.time() - at > self.IDLE:
                _close_quiet(db)
                continue
            try:
                if db._conn is not None:
                    db._conn.ping(reconnect=True)
                return _Lease(self, db)
            except Exception:                          # noqa: BLE001 —— 坏了就换一个
                _close_quiet(db)

    def put(self, db) -> None:
        try:
            if db._conn is not None:
                db._conn.rollback()                    # 结束 select 开的事务，下次读到最新数据
        except Exception:                              # noqa: BLE001 —— 连接坏了：不放回
            _close_quiet(db)
            return
        with self._lock:
            if len(self._idle) < self.SIZE:
                self._idle.append((db, time.time()))
                return
        _close_quiet(db)


class _Lease:
    """从池里借出的连接：用法和 MySQL 一样，close() 是还回池里。"""

    def __init__(self, pool: DbPool, db):
        self._pool, self._db, self._done = pool, db, False

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        if not self._done:
            self._done = True
            self._pool.put(self._db)


def _close_quiet(db) -> None:
    try:
        db.close()
    except Exception:                                  # noqa: BLE001
        pass


def _mask(v: str, keep: int = 6) -> str:
    v = str(v or "")
    if not v:
        return ""
    return v[:keep] + "***" if len(v) > keep else "***"


def _issues_by_day(issues: List[dict]) -> List[dict]:
    """质量检测明细 → 按日期汇总：哪天不对、几处、哪些渠道、举几个例子。"""
    by: Dict[int, dict] = {}
    for it in issues:
        d = by.setdefault(int(it["date"]), {"date": str(it["date"]), "count": 0, "same_range": 0,
                                            "period_sum": 0, "channels": [], "samples": []})
        d["count"] += 1
        d[it["check"]] = d.get(it["check"], 0) + 1
        if it["channel"] not in d["channels"]:
            d["channels"].append(it["channel"])
        if len(d["samples"]) < 4:
            r, v = it.get("rows") or [], it.get("values") or []
            if it["check"] == "same_range" and len(r) > 1 and len(v) > 1:
                txt = f"{it['channel']} · {it['field']}：{r[0]} = {v[0]}，{r[1]} = {v[1]}"
            else:
                txt = (f"{it['channel']} · {r[0] if r else ''} 评论数 {v[0] if v else ''}，"
                       f"区间内每天 td 合计 {v[1] if len(v) > 1 else ''}")
            d["samples"].append(txt)
    return [by[k] for k in sorted(by)]


def open_store(settings, data_dir: str) -> Store:
    """按 settings.service_store 打开系统表存储：mysql（默认，数仓规范的系统表）/ sqlite（本机试用）。"""
    kind = str(getattr(settings, "service_store", "mysql") or "mysql").lower()
    if kind == "sqlite":
        return Store.sqlite(os.path.join(data_dir, "service.db"))
    if kind != "mysql":
        raise StoreError(f"service_store 只能是 mysql 或 sqlite，收到 {kind!r}")
    return Store.mysql(settings.db)                     # 和计算指标用同一个库（db.database）


# ══════════════════════════════════════════════════════════════════════
# 应用：持有 settings / 存储 / 执行器 / 定时，API 的业务逻辑都在这里
# ══════════════════════════════════════════════════════════════════════
class App:
    def __init__(self, settings, data_dir: Optional[str] = None, runner: Optional[Runner] = None,
                 scheduler_enabled: bool = True, db_factory: Optional[Callable] = None,
                 store: Optional[Store] = None, auth: Optional[Auth] = None):
        self.settings = settings
        d = data_dir or getattr(settings, "service_data_dir", "") or "service_data"
        # 相对路径按项目根目录算（执行器的子进程也在项目根目录跑），跟从哪启动服务无关
        self.data_dir = d if os.path.isabs(d) else os.path.join(APP_ROOT, d)
        os.makedirs(self.data_dir, exist_ok=True)
        self.store = store or open_store(settings, self.data_dir)
        self.runner = runner or Runner(self.store, self.data_dir)
        self.scheduler = Scheduler(self.store, self.runner) if scheduler_enabled else None
        self._db_factory = db_factory
        self._pool = None                         # 指标库 / 用户表连接池（第一次用时建）
        self._push_cache: Dict[str, tuple] = {}    # 推送记录文件 → ((大小, 修改时间), 解析好的记录)
        # 登录：用 SMC 系统的用户 / 会话表，在源表库（src_database，没配就是 database），见 auth.py
        self.auth = auth or Auth(UserRepository(self._db, settings.db.src_db()))
        self.started_at = now_str()
        # 系统状态：系统表库 / 指标库在后台线程里检测（连库最多要等 connect_timeout 秒，不能卡住请求）
        self._health_lock = threading.Lock()
        self._health_deps: Dict[str, dict] = {}
        self._health_at = 0.0
        self._health_busy = False

    def start(self):
        self.runner.start()
        if self.scheduler:
            self.scheduler.start()

    def stop(self):
        if self.scheduler:
            self.scheduler.stop()
        self.runner.stop()

    # ------------------------------------------------------------ 系统状态
    HEALTH_TTL = 20                                   # 秒：库的检测结果缓存这么久

    def health(self, refresh: bool = False) -> dict:
        """页面顶栏的状态标识：服务（能响应就是在线）、系统表库、指标库、定时。

        库的检测放在后台线程，这里立刻返回上一次的结果；第一次还没测完时显示「检测中」。
        """
        with self._health_lock:
            stale = refresh or time.time() - self._health_at > self.HEALTH_TTL
            if stale and not self._health_busy:
                self._health_busy = True
                threading.Thread(target=self._check_deps, name="health", daemon=True).start()
            deps = dict(self._health_deps)
            busy = self._health_busy
        checks = [{"key": "service", "label": "服务", "ok": True, "detail": f"运行中 · {self.started_at} 启动"}]
        for key, label in (("store", "系统表库"), ("db", "指标库")):
            checks.append(deps.get(key) or {"key": key, "label": label, "ok": None, "detail": "检测中…"})
        if self.scheduler is None:
            checks.append({"key": "scheduler", "label": "定时", "ok": None, "detail": "未启动（--no-scheduler），只能手动执行"})
        elif not self.scheduler.enabled:
            checks.append({"key": "scheduler", "label": "定时", "ok": None, "detail": "总开关已关闭，到点不会自动执行"})
        else:
            checks.append({"key": "scheduler", "label": "定时", "ok": True, "detail": "运行中"})
        cur, queued = self.runner.current, self.runner.queued()
        checks.append({"key": "runner", "label": "执行器", "ok": True,
                       "detail": (f"正在执行 #{cur}" if cur else "空闲") + (f"，排队 {queued} 个" if queued else "")})
        return {"ok": all(c["ok"] is not False for c in checks), "now": now_str(),
                "checked_at": deps.get("checked_at", {}).get("detail", ""), "checking": busy, "checks": checks}

    def _check_deps(self) -> None:
        out: Dict[str, dict] = {}
        try:
            self.store.get_kv("scheduler", {})
            main, sub = self.store.describe_parts()
            out["store"] = {"key": "store", "label": "系统表库", "ok": True, "detail": main, "sub": sub}
        except Exception as e:                        # noqa: BLE001
            out["store"] = {"key": "store", "label": "系统表库", "ok": False,
                            "detail": f"读写系统表失败：{str(e)[:200]}（任务、执行记录存不进去）"}
        c = self.settings.db
        where = f"MySQL {c.host}:{c.port} · 库 {c.ads_db() or c.database}"
        try:
            self._query("select 1 as ok")
            out["db"] = {"key": "db", "label": "指标库", "ok": True, "detail": where, "sub": "跑批、指标查看、数据检测都用这个库"}
        except ApiError as e:
            out["db"] = {"key": "db", "label": "指标库", "ok": False,
                         "detail": f"{where} 连不上：{str(e)[:200]}（跑批、指标查看、数据检测都用不了）"}
        out["checked_at"] = {"detail": now_str()}
        with self._health_lock:
            self._health_deps, self._health_at, self._health_busy = out, time.time(), False

    # ------------------------------------------------------------ 数据库
    def _db(self):
        """查指标库 / 用户表的连接。测试传了 db_factory 就用它；否则从连接池借（用完 close() 是还回池里，不是断开）。"""
        if self._db_factory:
            return self._db_factory()
        if self._pool is None:
            from ..db import MySQL
            self._pool = DbPool(lambda: MySQL(self.settings.db))
        return self._pool.get()

    def _query(self, sql: str, params=()) -> List[dict]:
        db = self._db()
        try:
            df = db.query_df(sql, list(params))
        except Exception as e:                        # noqa: BLE001
            raise ApiError(502, f"查询数据库失败：{e}")
        finally:
            try:
                db.close()
            except Exception:                         # noqa: BLE001
                pass
        return [{k: _clean(v) for k, v in r.items()} for r in df.to_dict("records")]

    def _ads(self, table: str) -> str:
        ads = self.settings.db.ads_db()
        return f"`{ads}`.`{table}`" if ads else f"`{table}`"

    # ------------------------------------------------------------ 任务
    def _task_view(self, t: dict, last: Optional[dict] = None) -> dict:
        """last：各任务最近一次执行（列表一次查好传进来；单个任务不传就现查）。"""
        times = (t.get("schedule") or {}).get("times") or []
        try:
            _, argv, title, _then = build_task_argv(t["config"])
            preview = "python -m engin_cli.cli " + " ".join(argv)
        except ValueError as e:
            preview, title = f"（配置有误：{e}）", ""
        nf = next_fire(times, dt.datetime.now()) if t["enabled"] and times else None
        return {**t, "config": normalize_config(t["config"]) if preview[:1] != "（" else t["config"],
                "next_fire": nf.strftime("%Y-%m-%d %H:%M") if nf else None,
                "command_preview": preview, "title": title,
                "last_run": (last if last is not None else self.store.last_runs()).get(int(t["id"]))}

    def list_tasks(self) -> List[dict]:
        last = self.store.last_runs()
        return [self._task_view(t, last) for t in self.store.list_tasks()]

    def _task_input(self, body: dict) -> Tuple[str, dict, dict, bool]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ApiError(400, "任务名称不能为空")
        try:
            cfg = normalize_config(body.get("config") or {})
            times = normalize_times((body.get("schedule") or {}).get("times") or [])
        except (ValueError, TypeError) as e:
            raise ApiError(400, str(e))
        return name, cfg, {"times": times}, bool(body.get("enabled", True))

    def create_task(self, body: dict) -> dict:
        name, cfg, sch, en = self._task_input(body)
        return self._task_view(self.store.get_task(self.store.create_task(name, cfg, sch, en)))

    def update_task(self, task_id: int, body: dict) -> dict:
        if not self.store.get_task(task_id):
            raise ApiError(404, "任务不存在")
        name, cfg, sch, en = self._task_input(body)
        self.store.update_task(task_id, name, cfg, sch, en)
        return self._task_view(self.store.get_task(task_id))

    def run_task(self, task_id: int) -> dict:
        t = self.store.get_task(task_id)
        if not t:
            raise ApiError(404, "任务不存在")
        try:
            kind, argv, title, then = build_task_argv(t["config"])
        except ValueError as e:
            raise ApiError(400, str(e))
        return {"run_id": self.runner.submit(kind, argv, title, "manual", t, then=then)}

    def preview_task(self, body: dict) -> dict:
        """任务编辑页右边的「预览：这次定时会跑什么」。"""
        try:
            cfg = normalize_config(body.get("config") or {})
            times = normalize_times((body.get("schedule") or {}).get("times") or [])
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": str(e)}
        kind, argv, title, then = build_task_argv(cfg)
        ps = self.runner.push_settings()
        argv = apply_push_switch(argv, ps["enabled"])
        start, end = task_dates(cfg)
        now = dt.datetime.now()
        fires, t = [], now
        for _ in range(3 if times else 0):
            nf = next_fire(times, t)
            fires.append(nf.strftime("%Y-%m-%d %H:%M"))
            t = nf
        return {"ok": True, "command": "python -m engin_cli.cli " + " ".join(argv), "title": title,
                "start": start, "end": end, "scenics": cfg["scenics"].split(",") if cfg["scenics"] else [],
                "tables": cfg["tables"].split(",") if cfg["tables"] else [],
                "push": cfg["push"].split(",") if cfg["push"] and ps["enabled"] else [],
                # 跑完后复制：源景区通过了才执行；复制会推送，所以也要推送地址
                "copies": [{"title": x["title"], "command": "python -m engin_cli.cli " + " ".join(x["argv"])}
                           for x in then],
                "push_off": not ps["enabled"], "next_fires": fires,
                # 要推送却没配推送地址：engin_cli 会直接拒绝启动（整次执行失败），页面上提前提示
                "push_no_url": bool((cfg["push"] or then) and ps["enabled"] and not self.push_ready()),
                "scheduler_on": self.scheduler.enabled if self.scheduler else False}

    # ------------------------------------------------------------ 定时
    def scheduler_view(self) -> dict:
        tasks = self.list_tasks()
        nxt = sorted(({"task_id": t["id"], "task_name": t["name"], "at": t["next_fire"]}
                      for t in tasks if t["next_fire"]), key=lambda x: x["at"])
        return {"available": self.scheduler is not None,
                "enabled": bool(self.scheduler and self.scheduler.enabled),
                "grace_minutes": int(GRACE.total_seconds() // 60),
                "next": nxt[0] if nxt else None, "running": self.runner.current,
                "queued": self.runner.queued(), "events": self.store.list_events(limit=30)}

    def set_scheduler(self, body: dict) -> dict:
        if not self.scheduler:
            raise ApiError(400, "服务是用 --no-scheduler 启动的，定时不可用")
        self.scheduler.set_enabled(bool(body.get("enabled")))
        return self.scheduler_view()

    # ------------------------------------------------------------ 推送
    def push_settings_view(self) -> dict:
        st = self.settings
        ps = self.runner.push_settings()
        base = {"push_url": st.push_url, "push_batch_size": st.push_batch_size,
                "push_timeout": st.push_timeout, "push_retries": st.push_retries,
                "push_dry_run": st.push_dry_run, "push_strict": st.push_strict}
        over = push_overrides(ps)
        return {"settings": ps, "base": base, "effective": {**base, **over},
                "headers": {k: _mask(v) for k, v in (st.push_headers or {}).items()},
                "table_names": st.push_table_names or {}, "log_dir": self._push_log_dir()}

    def save_push_settings(self, body: dict) -> dict:
        """只改传进来的字段（例如只关总开关），其余保持原样。"""
        try:
            ps = normalize_push_settings({**self.runner.push_settings(), **(body or {})})
        except (ValueError, TypeError) as e:
            raise ApiError(400, str(e))
        old = self.runner.push_settings()
        self.store.set_kv("push", ps)
        if old["enabled"] != ps["enabled"]:
            self.store.add_event("推送总开关已" + ("打开" if ps["enabled"] else "关闭（任务只算不推）"),
                                 level="info" if ps["enabled"] else "warn")
        return self.push_settings_view()

    def _push_records(self, path: str) -> List[dict]:
        """解析好的推送记录（新的在前）。按文件大小 + 修改时间缓存：翻页、筛选不用每次把整天的文件重读重解析一遍。"""
        if not path or not os.path.exists(path):
            return []
        st = os.stat(path)
        key = (st.st_size, st.st_mtime_ns)
        hit = self._push_cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        recs = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    r["_text"] = json.dumps(r, ensure_ascii=False)     # 关键字搜索用（解析时算一次）
                    recs.append(r)
        recs.reverse()
        if len(self._push_cache) > 8:
            self._push_cache.clear()
        self._push_cache[path] = (key, recs)
        return recs

    def _push_log_dir(self) -> str:
        env = os.environ.get("OPINION_PUSH_LOG_DIR")
        d = str(env if env is not None else (getattr(self.settings, "push_log_dir", "") or "")).strip()
        if not d:
            return ""
        return d if os.path.isabs(d) else os.path.join(APP_ROOT, d)

    def push_logs(self, date: str = "", result: str = "", keyword: str = "",
                  limit: int = 50, offset: int = 0) -> dict:
        """推送记录（pusher 每个请求一行 JSON，logs/push_yyyyMMdd.log），新的在前。
        分页：offset / limit；total 是符合筛选条件的条数，counts / rows_ok 统计当天全部。"""
        d = self._push_log_dir()
        files = sorted((f for f in (os.listdir(d) if d and os.path.isdir(d) else [])
                        if re.fullmatch(r"push_\d{8}\.log", f)), reverse=True)
        dates = [f[5:13] for f in files]
        date = date or (dates[0] if dates else "")
        if date and not re.fullmatch(r"\d{8}", date):
            raise ApiError(400, "日期格式是 yyyyMMdd")
        items, counts, rows, total = [], {}, 0, 0
        path = os.path.join(d, f"push_{date}.log") if d and date else ""
        for r in self._push_records(path):
                r = dict(r)
                k = r.get("result") or "unknown"
                counts[k] = counts.get(k, 0) + 1
                if k in ("ok", "dry_run"):
                    rows += int(r.get("rows") or 0)
                if result and k != result:
                    continue
                if keyword and keyword not in r["_text"]:
                    continue
                total += 1
                if offset < total <= offset + limit:
                    resp = r.get("response")
                    if isinstance(resp, str) and len(resp) > 2000:
                        r["response"] = resp[:2000] + "…"
                    r.pop("url", None)
                    r.pop("_text", None)
                    items.append(r)
        return {"dir": d, "dates": dates[:60], "date": date, "counts": counts,
                "rows_ok": rows, "total": total, "items": items}

    # ------------------------------------------------------------ 执行记录
    def get_run(self, run_id: int) -> dict:
        run = self.store.get_run(run_id)
        if not run:
            raise ApiError(404, "执行记录不存在")
        run.pop("log_path", None)                  # 服务器上的路径不用给页面
        return run

    def run_log(self, run_id: int, offset: int = 0) -> dict:
        """执行日志：先读本机的日志文件（执行中实时追加）；文件没有 / 是空的就读库里跑完时存的那份。
        都没有时 missing 说明原因（例如日志文件在另一台服务器上）。"""
        run = self.store.get_run(run_id)
        if not run:
            raise ApiError(404, "执行记录不存在")
        done = run["status"] not in ("queued", "running")
        out = {"status": run["status"], "done": done, "source": "file", "missing": ""}
        path = run.get("log_path") or ""
        size = os.path.getsize(path) if path and os.path.exists(path) else 0
        if size or not done:
            data = b""
            if size:
                with open(path, "rb") as f:
                    f.seek(max(0, min(int(offset), size)))
                    data = f.read(512 * 1024)
            return dict(out, text=data.decode("utf-8", "replace"), offset=int(offset) + len(data), size=size)
        saved = self.store.get_run_log(run_id)
        if saved and saved["text"]:
            data = saved["text"].encode("utf-8")
            chunk = data[max(0, min(int(offset), len(data))):][:512 * 1024]
            return dict(out, source="db", saved_at=saved["saved_at"], host=saved["host"],
                        text=chunk.decode("utf-8", "replace"), offset=int(offset) + len(chunk), size=len(data))
        if not path:
            why = "这次执行没有日志"
        elif not os.path.exists(path):
            why = (f"这台服务器上没有日志文件 {path}，库里也没存（这个版本之前跑的执行不存库）。"
                   f"可能是被清理了，或者是另一台连同一个库的服务跑的这次执行，到那台服务器上看")
        else:
            why = f"日志文件是空的（{path}），子进程没有输出"
        return dict(out, source="none", missing=why, text="", offset=0, size=0)

    def run_log_events(self, run_id: int, offset: int = 0, *, poll: float = 0.25,
                       status_every: float = 2.0, heartbeat: float = 15.0, max_seconds: float = 6 * 3600):
        """执行日志实时流（页面用 EventSource 订阅）：日志文件一有新内容就推，不用页面定时来拉。

        逐个产出 (事件, 数据)：
          log    {text, offset, size, source, missing, ...} 新内容；第一条带日志来源 / 缺失原因
          status {status}  执行状态变了（排队 → 执行中）
          ping   None      心跳，连接空闲时每 heartbeat 秒一次，免得被代理 / 浏览器当成断线
          done   {status}  执行已结束、日志已读完，随后断开
        执行状态每 status_every 秒查一次库；文件大小每 poll 秒看一次（只是 stat，不读库）。
        """
        first = self.run_log(run_id, offset)                 # 不存在在这里抛 404
        if first["source"] != "file":                        # 库里存的 / 没有：一次给完就结束
            r = first
            yield "log", r
            while r["text"] and r["offset"] < r["size"]:
                r = self.run_log(run_id, r["offset"])
                yield "log", r
            yield "done", {"status": r["status"]}
            return
        yield "log", dict(first, text="", offset=int(offset))   # 先告诉页面日志来源，内容下面从文件读
        run = self.store.get_run(run_id) or {}
        path, status = run.get("log_path") or "", first["status"]
        done = first["done"]
        offset = max(0, int(offset))
        t0 = last_check = last_send = time.time()
        while time.time() - t0 < max_seconds:
            data, size = b"", 0
            if path and os.path.exists(path):
                size = os.path.getsize(path)
                if size > offset:
                    with open(path, "rb") as f:
                        f.seek(offset)
                        data = f.read(512 * 1024)
            if data and not done:
                data = data[:_utf8_complete(data)]           # 子进程还在写：多字节字符写了一半的留到下次
            if data:
                offset += len(data)
                last_send = time.time()
                yield "log", {"text": data.decode("utf-8", "replace"), "offset": offset, "size": size,
                              "source": "file", "missing": "", "status": status, "done": False}
                continue
            if done:
                yield "done", {"status": status}
                return
            now = time.time()
            if now - last_check >= status_every:
                last_check = now
                run = self.store.get_run(run_id) or {}
                if run.get("status") != status:
                    status = run.get("status") or status
                    path = run.get("log_path") or path          # 排队时还没有日志文件
                    if status in ("queued", "running"):         # 结束了不发 status，读完日志发 done
                        yield "status", {"status": status}
                done = status not in ("queued", "running")
                if done:
                    continue                                    # 结束了：再读一遍文件，把最后的输出推完
            if now - last_send >= heartbeat:
                last_send = now
                yield "ping", None
            time.sleep(poll)

    # ------------------------------------------------------------ 概览
    def overview(self) -> dict:
        now = dt.datetime.now()
        since14 = (now - dt.timedelta(days=13)).strftime("%Y-%m-%d 00:00:00")
        runs = [r for r in self.store.runs_between(since14) if r["kind"] == "run"]
        latest = self.store.list_runs(limit=40, count=False)["items"]      # 最近执行 + 需要关注，一次查
        days = [(now - dt.timedelta(days=13 - i)).strftime("%Y-%m-%d") for i in range(14)]
        daily = {d: {"date": d, "success": 0, "failed": 0, "blocked": 0, "other": 0} for d in days}
        for r in runs:
            d = r["created_at"][:10]
            if d in daily:
                k = r["status"] if r["status"] in ("success", "failed", "blocked") else "other"
                daily[d][k] += 1
        since7 = (now - dt.timedelta(days=6)).strftime("%Y-%m-%d")
        last7 = [r for r in runs if r["created_at"][:10] >= since7]
        done7 = [r for r in last7 if r["status"] in ("success", "failed", "blocked")]
        scen = {"ok": 0, "failed": 0, "blocked": 0}
        for r in last7:
            scen["blocked"] += r["quality_blocked"]
            scen["ok"] += r["scenic_ok"]
            scen["failed"] += r["scenic_total"] - r["quality_blocked"] - r["scenic_ok"]
        tasks = self.list_tasks()
        upcoming = sorted([{"task_id": t["id"], "task_name": t["name"], "at": t["next_fire"]}
                           for t in tasks if t["next_fire"]], key=lambda x: x["at"])[:6]
        return {
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "started_at": self.started_at,
            "tasks": {"total": len(tasks), "enabled": sum(1 for t in tasks if t["enabled"])},
            "running": self.runner.current,
            "today": sum(1 for r in runs if r["created_at"][:10] == days[-1]),
            "week": {"total": len(last7), "success": sum(r["status"] == "success" for r in last7),
                     "failed": sum(r["status"] == "failed" for r in last7),
                     "blocked": sum(r["status"] == "blocked" for r in last7),
                     "success_rate": round(100.0 * sum(r["status"] == "success" for r in done7)
                                           / len(done7), 1) if done7 else None},
            "scenic_results": scen,
            "daily": list(daily.values()),
            "upcoming": upcoming,
            "recent": latest[:8],
            "durations": self._durations(runs, days),
            "attention": [r for r in latest
                          if r["status"] in ("failed", "blocked")][:4],
            "scheduler": {"available": self.scheduler is not None,
                          "enabled": bool(self.scheduler and self.scheduler.enabled)},
            "push_enabled": self.runner.push_settings()["enabled"],
            "today_slots": sorted({x for t in tasks if t["enabled"]
                                   for x in (t.get("schedule") or {}).get("times") or []}),
            "today_runs": [{"at": r["created_at"][11:16], "status": r["status"]}
                           for r in runs if r["created_at"][:10] == days[-1]],
        }

    @staticmethod
    def _durations(runs: List[dict], days: List[str]) -> List[dict]:
        """每天跑批的平均耗时（秒），概览页的曲线图用。"""
        fmt = "%Y-%m-%d %H:%M:%S"
        acc = {d: [] for d in days}
        for r in runs:
            if r.get("started_at") and r.get("finished_at") and r["created_at"][:10] in acc:
                try:
                    secs = (dt.datetime.strptime(r["finished_at"], fmt)
                            - dt.datetime.strptime(r["started_at"], fmt)).total_seconds()
                except ValueError:
                    continue
                acc[r["created_at"][:10]].append(secs)
        return [{"date": d, "avg": round(sum(v) / len(v), 1) if v else 0, "n": len(v)}
                for d, v in acc.items()]

    # ------------------------------------------------------------ 数据质量
    def quality(self, days: int = 14) -> dict:
        since = (dt.datetime.now() - dt.timedelta(days=max(int(days), 1) - 1)).strftime("%Y-%m-%d")
        items = []
        for r in reversed(self.store.runs_between(since, full=True)):
            for s in (r.get("summary") or {}).get("scenics") or []:
                if s.get("errors") or s.get("quality_attempts", 0) > 1:
                    items.append({"run_id": r["id"], "created_at": r["created_at"],
                                  "task_name": r["task_name"], "title": r["title"],
                                  "scenic": s.get("scenic"), "ok": bool(s.get("ok")),
                                  "quality_blocked": bool(s.get("quality_blocked")),
                                  "attempts": s.get("quality_attempts", 0),
                                  "errors": s.get("errors") or []})
        return {"days": days, "items": items[:300]}

    def quality_check(self, body: dict) -> dict:
        """对库里已有的大盘表数据做一次检测（区间相同的行一致、近 N 日 = 每天 td 之和）。"""
        from .. import metric_calc_domain as D
        from ..builders.macro import _lookback
        from ..context import RunContext
        from ..loader import TABLE_MACRO
        from ..validate import run_check, validate_macro_same_range, validate_period_sums
        import pandas as pd

        scenic = str(body.get("scenic") or "").strip()
        start, end = str(body.get("start") or ""), str(body.get("end") or "")
        if not scenic or not re.fullmatch(r"\d{8}", start) or not re.fullmatch(r"\d{8}", end):
            raise ApiError(400, "要填景区编号和开始、结束日期（yyyyMMdd）")
        rows = self._query(f"select * from {self._ads(TABLE_MACRO)} where scenic_id=%s "
                           f"and travel_date between %s and %s", (scenic, int(start), int(end)))
        if not rows:
            msg = "这段日期库里没有大盘表数据"
            cid = self.store.add_check(check_type="manual", trigger="page", scenic=scenic, start=start, end=end,
                                       status="fail", attempts=1, items=0, issues=0, summary=msg,
                                       operator=current_user_name(),
                                       detail={"attempts": [], "dates": [start, end], "rows": 0, "source": msg})
            return {"rows": 0, "errors": [], "checks": [], "message": msg, "check_id": cid}
        df = pd.DataFrame(rows)
        df["travel_date"] = pd.to_numeric(df["travel_date"]).astype("int64")
        st = self.settings
        specs = D.macro_granularities(getattr(st, "macro_granularities", None))
        ctx = RunContext(settings=st, output_dates=[end], load_start=start, load_end=end)
        lb = {g["name"]: _lookback(ctx, g) for g in specs}
        checks = ["区间相同的行指标一致（td@T-1 = latest_1d@T、周一 wtd = 当天 td …）"]
        issues: List[dict] = []
        trace: List[dict] = []                         # 检测过程：存进检测记录，点进去能看
        errs = run_check(trace, "same_range", len(df), validate_macro_same_range, df, specs, lb, issues=issues)
        if len(set(ctx.backfill_lookback().values())) <= 1:
            checks.append("各周期评论数 = 区间内每天 td 之和")
            errs += run_check(trace, "period_sum", len(df), validate_period_sums, df, specs, issues=issues)
        days = _issues_by_day(issues)
        message = "全部通过" if not errs else f"{len(days)} 天有问题，共 {len(issues)} 处"
        m = self.task_for_scenic(scenic)               # 关联的任务：修复时按它的表计算、推送
        check_id = self.store.add_check(
            check_type="manual", trigger="page", scenic=scenic, start=start, end=end,
            status="fail" if errs else "pass", attempts=1, items=len(trace), issues=len(issues),
            summary=message, operator=current_user_name(), task=m["task"] if m else None,
            detail={"attempts": [{"attempt": 1, "at": now_str(), "ok": not errs, "errors": errs[:30], "steps": trace}],
                    "dates": [start, end], "rows": len(df),
                    "source": f"库里已有的大盘表 {self._ads(TABLE_MACRO)}，景区 {scenic}，{start}~{end}，共 {len(df)} 行",
                    "days": days, "task": self._task_brief(m)})
        return {"scenic": scenic, "start": start, "end": end, "rows": len(df), "errors": errs,
                "checks": checks, "days": days, "push_ready": self.push_ready(), "message": message,
                "check_id": check_id, "task": self._task_brief(m)}

    def task_for_scenic(self, scenic: str) -> Optional[dict]:
        """数据检测 / 修复关联的任务：任务里点名了这个景区的优先（启用的优先），没有就用「全部景区」的任务。
        返回 {id, name, enabled, tables, push, task}（tables / push 为空 = 全部表 / 不推送），都没有返回 None。"""
        named, every = [], []
        for t in self.store.list_tasks():
            try:
                cfg = normalize_config(t["config"])
            except ValueError:
                continue
            lst = [x for x in cfg["scenics"].split(",") if x]
            if scenic in lst:
                named.append((t, cfg))
            elif not lst:
                every.append((t, cfg))
        for group in (named, every):
            group.sort(key=lambda x: (not x[0]["enabled"], x[0]["id"]))
            if group:
                t, cfg = group[0]
                return {"id": t["id"], "name": t["name"], "enabled": bool(t["enabled"]),
                        "tables": cfg["tables"], "push": cfg["push"], "all_scenics": group is every, "task": t}
        return None

    @staticmethod
    def _task_brief(m: Optional[dict]) -> Optional[dict]:
        return {k: v for k, v in m.items() if k != "task"} if m else None

    def get_check(self, check_id: int) -> dict:
        c = self.store.get_check(check_id)
        if not c:
            raise ApiError(404, "检测记录不存在")
        return c

    def push_ready(self) -> bool:
        """推送能用：总开关开着、且配了推送地址（页面设置或 settings_local）。"""
        ps = self.runner.push_settings()
        return bool(ps["enabled"] and (ps.get("push_url") or getattr(self.settings, "push_url", "")))

    def quality_repair(self, body: dict) -> dict:
        """数据修复：把检测的**整段日期**按当前源数据一次重算（写库，可选推送）。

        为什么不只重算有问题的那几天：评论会晚到，库里早些天的 td 是当时算的。周期指标（近 7 日、本周 …）
        依赖区间里每一天，td@T-1 又要等于 latest_1d@T —— 只重算一部分日期，重算的那段和没重算的相邻几天
        用的是不同时刻的源数据，边上又会出现新的不一致，要修好几次。整段一次重算，用的是同一份源数据，
        检测里比对的每一对行都一起刷新，一次就能通过。
        body：{scenic, start, end}（检测的区间）；老的 {scenic, dates} 也认，按其中最早 ~ 最晚日期重算。
        """
        scenic = str(body.get("scenic") or "").strip()
        start, end = str(body.get("start") or ""), str(body.get("end") or "")
        dates = sorted({str(d) for d in (body.get("dates") or [])})
        if not start and dates:
            start, end = dates[0], dates[-1]
        end = end or start
        if not scenic or not all(re.fullmatch(r"\d{8}", d) for d in (start, end)) or start > end:
            raise ApiError(400, "要选景区和要重算的日期区间（yyyyMMdd）")
        # 关联任务：按任务里「计算哪些表」重算（大盘表一定算，检测的就是它），按任务里「推送哪些表」推送；
        # 没有关联任务：全部表重算，推下钻表 + 大盘表
        m = self.task_for_scenic(scenic)
        tables, push = "", "drill,macro"
        if m:
            tables = m["tables"]
            if tables and "macro" not in tables.split(","):
                tables = ",".join([x for x in TABLE_KEYS if x in tables.split(",") + ["macro"]])
            push = m["push"]
        # 推送没开 / 没配地址：只重算写库，不推（否则 run 会因为没地址直接退出，什么都没修）
        if not (body.get("push", True) and self.push_ready()):
            push = ""
        try:
            argv, title = build_run_argv({"scenics": scenic, "date_mode": "range", "start_date": start,
                                          "end_date": end, "rerun_days": 0, "tables": tables, "push": push,
                                          "verbose": True})
        except ValueError as e:
            raise ApiError(400, str(e))
        run_id = self.runner.submit("run", argv, "数据修复 · " + (f"{m['name']} · " if m else "") + title,
                                    task=m["task"] if m else None)
        span = start if start == end else f"{start}~{end}"
        bad = int(body.get("bad_days") or len(dates) or 0)
        self.store.add_event(f"数据质量修复：{scenic} 整段重算 {span}"
                             + (f"（{bad} 天有问题）" if bad else "") + f"，已提交执行 #{run_id}"
                             + ("，修复后推送" if push else "，不推送"))
        return {"run_id": run_id, "start": start, "end": end, "push": bool(push), "push_tables": push,
                "tables": tables, "task": self._task_brief(m)}

    # ------------------------------------------------------------ 指标查看
    def metric_scenics(self) -> List[dict]:
        from ..loader import TABLE_MACRO
        return self._query(f"select scenic_id, max(scenic_name) scenic_name, "
                           f"min(travel_date) first_date, max(travel_date) last_date "
                           f"from {self._ads(TABLE_MACRO)} group by scenic_id order by scenic_id")

    def scenic_options(self) -> List[dict]:
        """景区下拉的选项：源库景区表 src_opinion_social_scenic 里启用的景区（enabled=1）。"""
        src = self.settings.db.src_db()
        table = f"`{src}`.`{TABLE_SCENIC}`" if src else f"`{TABLE_SCENIC}`"
        out: List[dict] = []
        db_error = ""
        try:
            out = [{"scenic_id": str(r["scenic_id"]), "scenic_name": r.get("scenic_name") or ""}
                   for r in self._query(f"select scenic_id, scenic_name from {table} "
                                        f"where enabled = 1 order by scenic_id")]
        except ApiError as e:
            db_error = str(e)
        return {"items": out, "db_error": db_error}

    def metric_cell(self, scenic: str, date: str = "", granularity: str = "td") -> dict:
        from ..loader import TABLE_MACRO
        if not scenic:
            raise ApiError(400, "要选景区")
        tbl = self._ads(TABLE_MACRO)
        if not date:
            r = self._query(f"select max(travel_date) d from {tbl} where scenic_id=%s", (scenic,))
            date = str(r[0]["d"] or "") if r else ""
            if not date:
                return {"date": "", "granularity": granularity, "rows": []}
        rows = self._query(f"select * from {tbl} where scenic_id=%s and travel_date=%s "
                           f"and time_granularity=%s order by channel", (scenic, int(date), granularity))
        for r in rows:
            for c in ("dimension_breakdown", "wordcloud_map", "period_comment_heatmap"):
                try:
                    r[c] = json.loads(r.get(c) or "null")
                except (TypeError, ValueError):
                    pass
        return {"date": date, "granularity": granularity, "rows": rows}

    def metric_trend(self, scenic: str, channel: str = "all", granularity: str = "td",
                     start: str = "", end: str = "") -> List[dict]:
        from ..loader import TABLE_MACRO
        if not scenic:
            raise ApiError(400, "要选景区")
        sql = (f"select travel_date, comment_total, overall_sentiment_score, "
               f"positive_comment_cnt, neutral_comment_cnt, negative_comment_cnt "
               f"from {self._ads(TABLE_MACRO)} where scenic_id=%s and channel=%s "
               f"and time_granularity=%s")
        params = [scenic, channel, granularity]
        if start:
            sql += " and travel_date>=%s"
            params.append(int(start))
        if end:
            sql += " and travel_date<=%s"
            params.append(int(end))
        return self._query(sql + " order by travel_date desc limit 120", params)[::-1]

    # ------------------------------------------------------------ 维护
    def maint_push(self, body: dict) -> dict:
        try:
            argv, title = build_push_argv(str(body.get("scenics") or "").strip(),
                                          str(body.get("tables") or "").strip(),
                                          str(body.get("start") or "").strip(),
                                          str(body.get("end") or "").strip())
        except ValueError as e:
            raise ApiError(400, str(e))
        return {"run_id": self.runner.submit("push", argv, title)}

    def maint_run(self, body: dict) -> dict:
        """重跑一段日期：
        · 传 task_id + start / end：按已有任务的配置（景区、表、推送、质量、跑完后复制）跑这段日期；
        · 不传 task_id：临时跑一次（不建任务），参数同任务配置（config）。"""
        if body.get("task_id"):
            t = self.store.get_task(int(body["task_id"]))
            if not t:
                raise ApiError(404, "任务不存在")
            start, end = str(body.get("start") or ""), str(body.get("end") or body.get("start") or "")
            cfg = dict(t["config"], date_mode="range", start_date=start, end_date=end)
            try:
                kind, argv, _, then = build_task_argv(cfg)
            except ValueError as e:
                raise ApiError(400, str(e))
            title = f"重跑 · {t['name']} · " + (start if start == end else f"{start}~{end}")
            return {"run_id": self.runner.submit(kind, argv, title, "manual", t, then=then)}
        try:
            argv, title = build_run_argv(body.get("config") or {})
        except ValueError as e:
            raise ApiError(400, str(e))
        return {"run_id": self.runner.submit("run", argv, "临时 · " + title)}

    # ------------------------------------------------------------ 通知设置（飞书机器人）
    def notify_settings_view(self) -> dict:
        """页面上配的飞书机器人（优先）；没配就用 settings_local / 环境变量的。地址里带令牌，只回打码后的。"""
        ns, st = self.runner.notify_settings(), self.settings
        page, base = ns["feishu_webhook"], str(st.notify_feishu_webhook or "")
        url = page or base
        return {"source": "page" if page else ("local" if base else "none"),
                "webhook": _mask(url, 40), "secret": bool(ns["feishu_secret"] if page else st.notify_feishu_secret),
                "page_set": bool(page), "local_set": bool(base)}

    def save_notify_settings(self, body: dict) -> dict:
        """webhook 留空 = 不改；clear=true 清掉页面上的设置（回到 settings_local 的）。"""
        body = body or {}
        if body.get("clear"):
            ns = normalize_notify_settings({})
        else:
            cur = self.runner.notify_settings()
            url = str(body.get("webhook") or "").strip() or cur["feishu_webhook"]
            if not url:
                raise ApiError(400, "请填飞书机器人地址")
            secret = cur["feishu_secret"] if body.get("secret") is None else str(body.get("secret") or "")
            try:
                ns = normalize_notify_settings({"feishu_webhook": url, "feishu_secret": secret})
            except ValueError as e:
                raise ApiError(400, str(e))
        self.store.set_kv("notify", ns)
        self.store.add_event("通知设置已" + ("清除（改用配置文件的）" if body.get("clear") else "更新"))
        return self.notify_settings_view()

    def maint_notify(self) -> dict:
        return {"run_id": self.runner.submit("notify", ["notify-test"], "通知测试")}

    def maint_cleanup(self, body: dict) -> dict:
        days = max(int(body.get("days") or 90), 7)
        before = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        paths = self.store.delete_runs_before(before)
        self.store.delete_events_before(before)
        self.store.delete_checks_before(before)
        removed = 0
        for p in paths:
            for f in (p, p.replace(".log", ".summary.json")):
                try:
                    os.remove(f)
                    removed += 1
                except OSError:
                    pass
        return {"deleted_runs": len(paths), "deleted_files": removed, "before": before}

    def config_view(self) -> dict:
        st = self.settings
        db = st.db
        nv = self.notify_settings_view()
        return {
            "数据库": {"host": db.host, "port": db.port, "user": db.user,
                    "database": db.database, "ads_database": db.ads_db(), "src_database": db.src_db(),
                    "password": "已配置" if db.password else "未配置"},
            "推送": {"push_url": st.push_url, "push_headers": {k: _mask(v) for k, v in
                                                            (st.push_headers or {}).items()},
                   "push_batch_size": st.push_batch_size, "push_timeout": st.push_timeout,
                   "push_retries": st.push_retries, "push_log_dir": st.push_log_dir},
            "计算": {"rerun_days": st.rerun_days, "backfill_lookback": st.backfill_lookback,
                   "lookback_days": st.lookback_days, "drill_keyword_filter": st.drill_keyword_filter,
                   "plugins": st.plugins, "score_formula": st.score_formula},
            "数据质量": {"quality_gate": st.quality_gate, "quality_retries": st.quality_retries,
                     "notify_feishu_webhook": nv["webhook"],
                     "notify_feishu_from": {"page": "服务页面", "local": "配置文件", "none": "未配置"}[nv["source"]],
                     "notify_feishu_secret": "已配置" if nv["secret"] else "未配置"},
            "服务": {"store": self.store.describe(), "data_dir": self.data_dir, "push_settings_from_page": push_overrides(
                self.runner.push_settings()), "app_root": APP_ROOT,
                   "started_at": self.started_at},
        }


# ══════════════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════════════
def _route_table(app: App):
    """[(方法, 路径正则, 处理函数(match, query, body) → 结果)]"""
    I = int                                       # noqa: E741

    def q1(q, k, default=""):
        return (q.get(k) or [default])[0]

    return [
        ("GET", r"/api/health", lambda m, q, b: app.health(refresh=q1(q, "refresh") == "1")),
        ("GET", r"/api/overview", lambda m, q, b: app.overview()),
        ("GET", r"/api/config", lambda m, q, b: app.config_view()),
        ("GET", r"/api/task-defaults", lambda m, q, b: TASK_DEFAULTS),
        ("GET", r"/api/tasks", lambda m, q, b: app.list_tasks()),
        ("POST", r"/api/tasks/preview", lambda m, q, b: app.preview_task(b)),
        ("GET", r"/api/tasks/(\d+)/events",
         lambda m, q, b: app.store.list_events(I(m[1]), limit=min(I(q1(q, "limit", "50")), 200))),
        ("GET", r"/api/scheduler", lambda m, q, b: app.scheduler_view()),
        ("PUT", r"/api/scheduler", lambda m, q, b: app.set_scheduler(b)),
        ("GET", r"/api/push/settings", lambda m, q, b: app.push_settings_view()),
        ("PUT", r"/api/push/settings", lambda m, q, b: app.save_push_settings(b)),
        ("GET", r"/api/push/logs", lambda m, q, b: app.push_logs(
            q1(q, "date"), q1(q, "result"), q1(q, "q"), min(I(q1(q, "limit", "50")), 1000),
            max(I(q1(q, "offset", "0")), 0))),
        ("POST", r"/api/tasks", lambda m, q, b: app.create_task(b)),
        ("PUT", r"/api/tasks/(\d+)", lambda m, q, b: app.update_task(I(m[1]), b)),
        ("DELETE", r"/api/tasks/(\d+)", lambda m, q, b: app.store.delete_task(I(m[1])) or {"ok": True}),
        ("POST", r"/api/tasks/(\d+)/run", lambda m, q, b: app.run_task(I(m[1]))),
        ("POST", r"/api/tasks/(\d+)/enabled",
         lambda m, q, b: app.store.set_task_enabled(I(m[1]), bool(b.get("enabled"))) or {"ok": True}),
        ("GET", r"/api/runs", lambda m, q, b: app.store.list_runs(
            status=q1(q, "status"), task_id=I(q1(q, "task_id", "0") or 0), kind=q1(q, "kind"),
            limit=min(I(q1(q, "limit", "50")), 200), offset=I(q1(q, "offset", "0")))),
        ("GET", r"/api/runs/(\d+)", lambda m, q, b: app.get_run(I(m[1]))),
        ("GET", r"/api/runs/(\d+)/log", lambda m, q, b: app.run_log(I(m[1]), I(q1(q, "offset", "0")))),
        ("POST", r"/api/runs/(\d+)/cancel", lambda m, q, b: {"ok": app.runner.cancel(I(m[1]))}),
        ("GET", r"/api/quality", lambda m, q, b: app.quality(I(q1(q, "days", "14")))),
        ("POST", r"/api/quality/check", lambda m, q, b: app.quality_check(b)),
        ("GET", r"/api/quality/task", lambda m, q, b: {"task": app._task_brief(app.task_for_scenic(q1(q, "scenic")))}),
        ("GET", r"/api/quality/checks", lambda m, q, b: app.store.list_checks(
            status=q1(q, "status"), check_type=q1(q, "type"), scenic=q1(q, "scenic"), kw=q1(q, "q"),
            since=(dt.datetime.now() - dt.timedelta(days=max(I(q1(q, "days", "30") or 30), 1) - 1)).strftime("%Y-%m-%d"),
            limit=min(I(q1(q, "limit", "30")), 200), offset=I(q1(q, "offset", "0")))),
        ("GET", r"/api/quality/checks/(\d+)", lambda m, q, b: app.get_check(I(m[1]))),
        ("POST", r"/api/quality/repair", lambda m, q, b: app.quality_repair(b)),
        ("GET", r"/api/metrics/scenics", lambda m, q, b: app.metric_scenics()),
        ("GET", r"/api/scenics", lambda m, q, b: app.scenic_options()),
        ("GET", r"/api/plugins", lambda m, q, b: _plugins_describe()),
        ("GET", r"/api/metrics/cell", lambda m, q, b: app.metric_cell(
            q1(q, "scenic"), q1(q, "date"), q1(q, "granularity", "td"))),
        ("GET", r"/api/metrics/trend", lambda m, q, b: app.metric_trend(
            q1(q, "scenic"), q1(q, "channel", "all"), q1(q, "granularity", "td"),
            q1(q, "start"), q1(q, "end"))),
        ("POST", r"/api/maint/push", lambda m, q, b: app.maint_push(b)),
        ("POST", r"/api/maint/run", lambda m, q, b: app.maint_run(b)),
        ("POST", r"/api/maint/notify-test", lambda m, q, b: app.maint_notify()),
        ("GET", r"/api/notify/settings", lambda m, q, b: app.notify_settings_view()),
        ("PUT", r"/api/notify/settings", lambda m, q, b: app.save_notify_settings(b)),
        ("POST", r"/api/maint/cleanup", lambda m, q, b: app.maint_cleanup(b)),
    ]


def make_handler(app: App):
    routes = [(meth, re.compile(f"^{pat}$"), fn) for meth, pat, fn in _route_table(app)]

    class Handler(BaseHTTPRequestHandler):
        server_version = "OpinionMetricService"

        def log_message(self, fmt, *args):            # 走 logging，别刷 stderr
            log.debug("%s %s", self.address_string(), fmt % args)

        # -------------------------------------------------- 登录 / 防 CSRF
        def _token(self) -> str:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
            return ""

        def _client_ip(self) -> str:
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            return fwd or self.client_address[0]

        def _is_local(self) -> bool:
            """本机直连（不经过反向代理）：scripts/service.sh 检测服务状态用。"""
            return self.client_address[0] in LOOPBACK and not self.headers.get("X-Forwarded-For")

        def _cookie(self, token: str, max_age: int) -> dict:
            return {"Set-Cookie": f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"}

        def _gzip_ok(self) -> bool:
            return "gzip" in (self.headers.get("Accept-Encoding") or "")

        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None,
                  cache: str = "no-store", gz: Optional[bytes] = None):
            """gz：已经压好的内容（静态文件缓存的）；没给且内容大、浏览器支持，就现压（接口返回的 JSON）。"""
            if gz is None and len(body) > 2048 and _compressible(ctype) and self._gzip_ok():
                gz = gzip.compress(body, 5)
            use_gz = gz is not None and self._gzip_ok()
            data = gz if use_gz else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", cache)
            if use_gz or gz is not None:
                self.send_header("Vary", "Accept-Encoding")
            if use_gz:
                self.send_header("Content-Encoding", "gzip")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, status: int, data: Any, extra: Optional[dict] = None):
            self._send(status, json.dumps(_clean(data), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra)

        def _dispatch(self):
            u = urllib.parse.urlsplit(self.path)
            path = u.path
            # 长连接（HTTP/1.1）：请求体一定先读完，提前拒绝时没读的请求体会被当成下一个请求
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if not path.startswith("/api/"):
                return self._static(path)                 # 页面文件不用登录（登录页本身要加载）
            writing = self.command in ("POST", "PUT", "DELETE")
            if writing and not self.headers.get("X-Requested-With"):
                return self._json(403, {"error": "缺少 X-Requested-With 请求头"})
            body = {}
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    return self._json(400, {"error": "请求体不是合法 JSON"})
            body = body if isinstance(body, dict) else {}
            # ---- 登录 / 退出：不需要已登录
            if path == "/api/login" and self.command == "POST":
                try:
                    token, user = app.auth.login(body.get("username"), body.get("password"), self._client_ip(),
                                                 self.headers.get("User-Agent") or "")
                except AuthError as e:
                    return self._json(e.status, {"error": str(e)})
                return self._json(200, {"user": user}, self._cookie(token, SESSION_HOURS * 3600))
            if path == "/api/logout" and self.command == "POST":
                app.auth.logout(self._token())
                return self._json(200, {"ok": True}, self._cookie("", 0))
            user = app.auth.user_for(self._token())
            if user is None and not (path == "/api/health" and self._is_local()):
                return self._json(401, {"error": "请先登录", "login": True})
            if path == "/api/me":
                return self._json(200, {"user": user})
            _req.user = user
            why = denied_reason(user["role"], self.command, path) if user else ""
            if why:
                return self._json(403, {"error": why})
            q = urllib.parse.parse_qs(u.query)
            m = _LOG_STREAM.match(path)
            if m and self.command == "GET":
                return self._log_stream(int(m[1]), int((q.get("offset") or ["0"])[0] or 0))
            for meth, pat, fn in routes:
                m = pat.match(path)
                if m and meth == self.command:
                    try:
                        return self._json(200, fn(m, q, body))
                    except ApiError as e:
                        return self._json(e.status, {"error": str(e)})
                    except (ValueError, TypeError) as e:
                        return self._json(400, {"error": str(e)})
                    except Exception as e:            # noqa: BLE001
                        log.exception("接口 %s %s 出错", self.command, path)
                        return self._json(500, {"error": f"服务内部错误：{e}"})
            return self._json(404, {"error": "接口不存在"})

        def _log_stream(self, run_id: int, offset: int):
            """执行日志实时流（Server-Sent Events）：服务端有新日志就推给页面，页面用 EventSource 接。
            单向推送用 SSE 足够，标准库就能做，浏览器断线会自己重连；一条连接一个线程，看完日志页面就断开。"""
            events = app.run_log_events(run_id, offset)
            try:
                first = next(events)                       # 执行记录不存在：在发响应头之前就报 404
            except ApiError as e:
                return self._json(e.status, {"error": str(e)})
            self.close_connection = True                   # 流没有长度，推完就断开连接
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")    # 前面有 nginx 反向代理时，别把流缓冲住
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for ev, data in itertools.chain([first], events):
                    if ev == "ping":
                        chunk = b": ping\n\n"
                    else:
                        chunk = (f"event: {ev}\ndata: "
                                 + json.dumps(_clean(data), ensure_ascii=False) + "\n\n").encode("utf-8")
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, ValueError):                  # 页面关了 / 断网：结束这个流
                pass
            finally:
                events.close()

        def _static(self, path: str):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            full = os.path.realpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
                # 前端路由（#/xxx 之外的直链）一律回首页
                full = os.path.join(STATIC_DIR, "index.html")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            if full.endswith(".js"):
                ctype = "text/javascript; charset=utf-8"
            raw, gz, etag = _static_file(full, _compressible(ctype))
            # 页面代码（html / js / css）每次都向服务确认有没有改（没改回 304，不重新下载），升级后不用强制刷新；
            # 字体、图片、vue 库一天内直接用浏览器缓存
            cache = ("public, max-age=86400" if "/vendor/" in full.replace(os.sep, "/") or ctype.startswith(("image/", "font/"))
                     else "no-cache")
            inm = self.headers.get("If-None-Match") or ""
            if etag in [x.strip() for x in inm.split(",")]:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache)
                self.end_headers()
                return
            self._send(200, raw, ctype, {"ETag": etag}, cache=cache, gz=gz)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _dispatch

    # 长连接：一个页面十几个文件 + 接口共用几个连接，不用每个请求都重新建连接；空闲 60 秒断开
    Handler.protocol_version = "HTTP/1.1"
    Handler.timeout = 60

    return Handler


def make_server(app: App, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, int(port)), make_handler(app))
    srv.daemon_threads = True
    return srv


def serve(settings, host: Optional[str] = None, port: Optional[int] = None,
          scheduler_enabled: bool = True) -> int:
    host = host or getattr(settings, "service_host", "127.0.0.1")
    port = int(port or getattr(settings, "service_port", 8765))
    try:
        app = App(settings, scheduler_enabled=scheduler_enabled)
    except StoreError as e:
        print(f"✗ 启动失败：{e}")
        print("  系统表默认建在计算指标用的同一个 MySQL 库里（settings_local.py 的 DB）；"
              "只想本机试用可以设 service_store = \"sqlite\"")
        return 2
    try:
        srv = make_server(app, host, port)
    except (ValueError, OSError) as e:
        print(f"✗ 启动失败：{e}")
        return 2
    # kill / systemctl stop / scripts/service.sh stop 发的是 SIGTERM：跟 Ctrl+C 一样正常退出
    # （停定时、结束正在跑的子进程，执行记录标成「服务重启，执行被中断」），不留孤儿进程
    import signal

    def _on_term(*_):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except ValueError:                                  # 不在主线程（测试里）就算了
        pass
    app.start()
    print("=" * 72)
    print(f"指标计算服务已启动：http://{host}:{port}/"
          + f"   （需要登录，账号在 {app.auth.users.table}）")
    print(f"系统表：{app.store.describe()}")
    print(f"执行日志：{app.data_dir}    定时：{'开启' if scheduler_enabled else '关闭'}")
    print("按 Ctrl+C 停止")
    print("=" * 72, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        srv.server_close()
        app.stop()
    return 0
