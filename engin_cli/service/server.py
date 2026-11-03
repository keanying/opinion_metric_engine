# -*- coding: utf-8 -*-
"""指标计算服务：HTTP API + 页面（Vue 3，静态文件，无需构建）。

    python -m engin_cli.cli serve            # 默认 http://127.0.0.1:8765

只用标准库（http.server / threading）+ 已有的 pymysql，不增加依赖。
任务、设置、执行记录、调度日志存 MySQL 系统表（store.py，数仓命名规范），执行日志是文件。

安全：
· 默认只监听本机。监听其他地址（如 0.0.0.0）必须配 service_password，否则拒绝启动 ——
  这个服务能触发跑批、推送，能看库里的指标，裸奔在内网上不行；
· 配了密码就走 HTTP Basic 认证（用户名随意，密码对就行）；
· 所有改动类请求（POST/PUT/DELETE）要带 X-Requested-With 头，挡住跨站表单提交（CSRF）。
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import hmac
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

from .runner import (APP_ROOT, PUSH_DEFAULTS, Runner, TASK_DEFAULTS, apply_push_switch,
                     build_push_argv, build_run_argv, normalize_config, normalize_push_settings,
                     push_overrides, task_dates)
from .scheduler import GRACE, Scheduler, next_fire, normalize_times
from .store import Store, StoreError, now_str

log = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


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
                 store: Optional[Store] = None):
        self.settings = settings
        d = data_dir or getattr(settings, "service_data_dir", "") or "service_data"
        # 相对路径按项目根目录算（执行器的子进程也在项目根目录跑），跟从哪启动服务无关
        self.data_dir = d if os.path.isabs(d) else os.path.join(APP_ROOT, d)
        os.makedirs(self.data_dir, exist_ok=True)
        self.store = store or open_store(settings, self.data_dir)
        self.runner = runner or Runner(self.store, self.data_dir)
        self.scheduler = Scheduler(self.store, self.runner) if scheduler_enabled else None
        self._db_factory = db_factory
        self.started_at = now_str()

    def start(self):
        self.runner.start()
        if self.scheduler:
            self.scheduler.start()

    def stop(self):
        if self.scheduler:
            self.scheduler.stop()
        self.runner.stop()

    # ------------------------------------------------------------ 数据库
    def _db(self):
        if self._db_factory:
            return self._db_factory()
        from ..db import MySQL
        return MySQL(self.settings.db)

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
    def _task_view(self, t: dict) -> dict:
        times = (t.get("schedule") or {}).get("times") or []
        try:
            argv, title = build_run_argv(t["config"])
            preview = "python -m engin_cli.cli " + " ".join(argv)
        except ValueError as e:
            preview, title = f"（配置有误：{e}）", ""
        nf = next_fire(times, dt.datetime.now()) if t["enabled"] and times else None
        return {**t, "config": normalize_config(t["config"]) if preview[:1] != "（" else t["config"],
                "next_fire": nf.strftime("%Y-%m-%d %H:%M") if nf else None,
                "command_preview": preview, "title": title,
                "last_run": self.store.last_run_of(t["id"])}

    def list_tasks(self) -> List[dict]:
        return [self._task_view(t) for t in self.store.list_tasks()]

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
            argv, title = build_run_argv(t["config"])
        except ValueError as e:
            raise ApiError(400, str(e))
        return {"run_id": self.runner.submit("run", argv, title, "manual", t)}

    def preview_task(self, body: dict) -> dict:
        """任务编辑页右边的「预览：这次定时会跑什么」。"""
        try:
            cfg = normalize_config(body.get("config") or {})
            times = normalize_times((body.get("schedule") or {}).get("times") or [])
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": str(e)}
        argv, title = build_run_argv(cfg)
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
                "push_off": not ps["enabled"], "next_fires": fires,
                # 要推送却没配推送地址：engin_cli 会直接拒绝启动（整次执行失败），页面上提前提示
                "push_no_url": bool(cfg["push"] and ps["enabled"] and not self.push_ready()),
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
                "push_dry_run": st.push_dry_run, "push_strict": st.push_strict,
                "push_cleanup_local": st.push_cleanup_local}
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

    def _push_log_dir(self) -> str:
        env = os.environ.get("OPINION_PUSH_LOG_DIR")
        d = str(env if env is not None else (getattr(self.settings, "push_log_dir", "") or "")).strip()
        if not d:
            return ""
        return d if os.path.isabs(d) else os.path.join(APP_ROOT, d)

    def push_logs(self, date: str = "", result: str = "", keyword: str = "",
                  limit: int = 200) -> dict:
        """推送记录（pusher 每个请求一行 JSON，logs/push_yyyyMMdd.log）。"""
        d = self._push_log_dir()
        files = sorted((f for f in (os.listdir(d) if d and os.path.isdir(d) else [])
                        if re.fullmatch(r"push_\d{8}\.log", f)), reverse=True)
        dates = [f[5:13] for f in files]
        date = date or (dates[0] if dates else "")
        if date and not re.fullmatch(r"\d{8}", date):
            raise ApiError(400, "日期格式是 yyyyMMdd")
        items, counts, rows = [], {}, 0
        path = os.path.join(d, f"push_{date}.log") if d and date else ""
        if path and os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line in reversed(lines):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                k = r.get("result") or "unknown"
                counts[k] = counts.get(k, 0) + 1
                if k in ("ok", "dry_run"):
                    rows += int(r.get("rows") or 0)
                if result and k != result:
                    continue
                if keyword and keyword not in json.dumps(r, ensure_ascii=False):
                    continue
                if len(items) < limit:
                    resp = r.get("response")
                    if isinstance(resp, str) and len(resp) > 2000:
                        r["response"] = resp[:2000] + "…"
                    r.pop("url", None)
                    items.append(r)
        return {"dir": d, "dates": dates[:60], "date": date, "counts": counts,
                "rows_ok": rows, "items": items}

    # ------------------------------------------------------------ 执行记录
    def get_run(self, run_id: int) -> dict:
        run = self.store.get_run(run_id)
        if not run:
            raise ApiError(404, "执行记录不存在")
        run.pop("log_path", None)                  # 服务器上的路径不用给页面
        return run

    def run_log(self, run_id: int, offset: int = 0) -> dict:
        run = self.store.get_run(run_id)
        if not run:
            raise ApiError(404, "执行记录不存在")
        text, size = "", 0
        path = run.get("log_path") or ""
        if path and os.path.exists(path):
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(0, min(int(offset), size)))
                text = f.read(512 * 1024).decode("utf-8", "replace")
        return {"text": text, "offset": int(offset) + len(text.encode("utf-8")),
                "size": size, "status": run["status"],
                "done": run["status"] not in ("queued", "running")}

    # ------------------------------------------------------------ 概览
    def overview(self) -> dict:
        now = dt.datetime.now()
        since14 = (now - dt.timedelta(days=13)).strftime("%Y-%m-%d 00:00:00")
        runs = [r for r in self.store.runs_between(since14) if r["kind"] == "run"]
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
            for s in (r.get("summary") or {}).get("scenics") or []:
                scen["blocked" if s.get("quality_blocked") else "ok" if s.get("ok") else "failed"] += 1
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
            "recent": self.store.list_runs(limit=8)["items"],
            "durations": self._durations(runs, days),
            "attention": [r for r in self.store.list_runs(limit=40)["items"]
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
        for r in reversed(self.store.runs_between(since)):
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
        from ..validate import validate_macro_same_range, validate_period_sums
        import pandas as pd

        scenic = str(body.get("scenic") or "").strip()
        start, end = str(body.get("start") or ""), str(body.get("end") or "")
        if not scenic or not re.fullmatch(r"\d{8}", start) or not re.fullmatch(r"\d{8}", end):
            raise ApiError(400, "要填景区编号和开始、结束日期（yyyyMMdd）")
        rows = self._query(f"select * from {self._ads(TABLE_MACRO)} where scenic_id=%s "
                           f"and travel_date between %s and %s", (scenic, int(start), int(end)))
        if not rows:
            return {"rows": 0, "errors": [], "checks": [], "message": "这段日期库里没有大盘表数据"}
        df = pd.DataFrame(rows)
        df["travel_date"] = pd.to_numeric(df["travel_date"]).astype("int64")
        st = self.settings
        specs = D.macro_granularities(getattr(st, "macro_granularities", None))
        ctx = RunContext(settings=st, output_dates=[end], load_start=start, load_end=end)
        lb = {g["name"]: _lookback(ctx, g) for g in specs}
        checks = ["区间相同的行指标一致（td@T-1 = latest_1d@T、周一 wtd = 当天 td …）"]
        issues: List[dict] = []
        errs = validate_macro_same_range(df, specs, lb, issues=issues)
        if len(set(ctx.backfill_lookback().values())) <= 1:
            checks.append("各周期评论数 = 区间内每天 td 之和")
            errs += validate_period_sums(df, specs, issues=issues)
        days = _issues_by_day(issues)
        return {"scenic": scenic, "start": start, "end": end, "rows": len(df), "errors": errs,
                "checks": checks, "days": days, "push_ready": self.push_ready(),
                "message": "全部通过" if not errs else f"{len(days)} 天有问题，共 {len(issues)} 处"}

    def push_ready(self) -> bool:
        """推送能用：总开关开着、且配了推送地址（页面设置或 settings_local）。"""
        ps = self.runner.push_settings()
        return bool(ps["enabled"] and (ps.get("push_url") or getattr(self.settings, "push_url", "")))

    def quality_repair(self, body: dict) -> dict:
        """数据修复：把有问题的日期按当前源数据重算一遍（写库，可选推送）。"""
        scenic = str(body.get("scenic") or "").strip()
        dates = sorted({str(d) for d in (body.get("dates") or [])})
        if not scenic or not dates or not all(re.fullmatch(r"\d{8}", d) for d in dates):
            raise ApiError(400, "要选景区和要修复的日期（yyyyMMdd）")
        # 推送没开 / 没配地址：只重算写库，不推（否则 run 会因为没地址直接退出，什么都没修）
        push = ",".join(t for t in str(body.get("push_tables") or "drill,macro").split(",") if t) \
            if body.get("push", True) and self.push_ready() else ""
        try:
            argv, title = build_run_argv({"scenics": scenic, "date_mode": "range", "start_date": dates[0],
                                          "end_date": dates[-1], "rerun_days": 0, "push": push,
                                          "verbose": True})
        except ValueError as e:
            raise ApiError(400, str(e))
        run_id = self.runner.submit("run", argv, "数据修复 · " + title)
        span = dates[0] if dates[0] == dates[-1] else f"{dates[0]}~{dates[-1]}"
        self.store.add_event(f"数据质量修复：{scenic} {span}（{len(dates)} 天有问题）已提交执行 #{run_id}"
                             + ("，修复后推送" if push else "，不推送"))
        return {"run_id": run_id, "start": dates[0], "end": dates[-1], "push": bool(push)}

    # ------------------------------------------------------------ 指标查看
    def metric_scenics(self) -> List[dict]:
        from ..loader import TABLE_MACRO
        return self._query(f"select scenic_id, max(scenic_name) scenic_name, "
                           f"min(travel_date) first_date, max(travel_date) last_date "
                           f"from {self._ads(TABLE_MACRO)} group by scenic_id order by scenic_id")

    def scenic_options(self) -> List[dict]:
        """景区下拉的选项：库里大盘表有数据的景区 + 本地有待补推 CSV 的景区（库连不上也能列本地的）。"""
        from ..loader import TABLE_DRILL_ANALYSIS, TABLE_MACRO, TABLE_CORE, TABLE_PLATFORM, \
            TABLE_DIMENSION, TABLE_CONTENT
        out: Dict[str, dict] = {}
        db_error = ""
        try:
            for r in self.metric_scenics():
                out[str(r["scenic_id"])] = {"scenic_id": str(r["scenic_id"]), "scenic_name": r.get("scenic_name") or "",
                                            "last_date": r.get("last_date"), "has_csv": False}
        except ApiError as e:
            db_error = str(e)
        base = getattr(self.settings, "output_dir", "") or "output"
        base = base if os.path.isabs(base) else os.path.join(APP_ROOT, base)
        tables = {f"{t}.csv" for t in (TABLE_CORE, TABLE_PLATFORM, TABLE_DIMENSION, TABLE_CONTENT,
                                       TABLE_DRILL_ANALYSIS, TABLE_MACRO)}
        if os.path.isdir(base):
            for d in sorted(os.listdir(base)):
                full = os.path.join(base, d)
                if os.path.isdir(full) and tables & set(os.listdir(full)):
                    out.setdefault(d, {"scenic_id": d, "scenic_name": "", "last_date": None, "has_csv": False})
                    out[d]["has_csv"] = True
        return {"items": sorted(out.values(), key=lambda x: x["scenic_id"]), "db_error": db_error}

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
        """临时跑一次（不建任务）：参数同任务配置。"""
        try:
            argv, title = build_run_argv(body.get("config") or {})
        except ValueError as e:
            raise ApiError(400, str(e))
        return {"run_id": self.runner.submit("run", argv, "临时 · " + title)}

    def maint_notify(self) -> dict:
        return {"run_id": self.runner.submit("notify", ["notify-test"], "飞书通知测试")}

    def maint_cleanup(self, body: dict) -> dict:
        days = max(int(body.get("days") or 90), 7)
        before = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        paths = self.store.delete_runs_before(before)
        self.store.delete_events_before(before)
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
                   "score_formula": st.score_formula},
            "数据质量": {"quality_gate": st.quality_gate, "quality_retries": st.quality_retries,
                     "notify_feishu_webhook": _mask(st.notify_feishu_webhook, 40),
                     "notify_feishu_secret": "已配置" if st.notify_feishu_secret else "未配置"},
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
        ("GET", r"/api/health", lambda m, q, b: {"ok": True, "now": now_str()}),
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
            q1(q, "date"), q1(q, "result"), q1(q, "q"), min(I(q1(q, "limit", "200")), 1000))),
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
        ("POST", r"/api/quality/repair", lambda m, q, b: app.quality_repair(b)),
        ("GET", r"/api/metrics/scenics", lambda m, q, b: app.metric_scenics()),
        ("GET", r"/api/scenics", lambda m, q, b: app.scenic_options()),
        ("GET", r"/api/metrics/cell", lambda m, q, b: app.metric_cell(
            q1(q, "scenic"), q1(q, "date"), q1(q, "granularity", "td"))),
        ("GET", r"/api/metrics/trend", lambda m, q, b: app.metric_trend(
            q1(q, "scenic"), q1(q, "channel", "all"), q1(q, "granularity", "td"),
            q1(q, "start"), q1(q, "end"))),
        ("POST", r"/api/maint/push", lambda m, q, b: app.maint_push(b)),
        ("POST", r"/api/maint/run", lambda m, q, b: app.maint_run(b)),
        ("POST", r"/api/maint/notify-test", lambda m, q, b: app.maint_notify()),
        ("POST", r"/api/maint/cleanup", lambda m, q, b: app.maint_cleanup(b)),
    ]


def make_handler(app: App, password: str = ""):
    routes = [(meth, re.compile(f"^{pat}$"), fn) for meth, pat, fn in _route_table(app)]

    class Handler(BaseHTTPRequestHandler):
        server_version = "OpinionMetricService"

        def log_message(self, fmt, *args):            # 走 logging，别刷 stderr
            log.debug("%s %s", self.address_string(), fmt % args)

        # -------------------------------------------------- 认证 / 防 CSRF
        def _authorized(self) -> bool:
            if not password:
                return True
            h = self.headers.get("Authorization") or ""
            if not h.startswith("Basic "):
                return False
            try:
                _, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            return hmac.compare_digest(pw.encode("utf-8"), password.encode("utf-8"))

        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data: Any):
            self._send(status, json.dumps(_clean(data), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _dispatch(self):
            if not self._authorized():
                self._send(401, "需要登录".encode("utf-8"), "text/plain; charset=utf-8",
                           {"WWW-Authenticate": 'Basic realm="opinion-metric", charset="UTF-8"'})
                return
            u = urllib.parse.urlsplit(self.path)
            path = u.path
            if not path.startswith("/api/"):
                return self._static(path)
            if self.command in ("POST", "PUT", "DELETE") and not self.headers.get("X-Requested-With"):
                return self._json(403, {"error": "缺少 X-Requested-With 请求头"})
            body = {}
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except ValueError:
                    return self._json(400, {"error": "请求体不是合法 JSON"})
            q = urllib.parse.parse_qs(u.query)
            for meth, pat, fn in routes:
                m = pat.match(path)
                if m and meth == self.command:
                    try:
                        return self._json(200, fn(m, q, body if isinstance(body, dict) else {}))
                    except ApiError as e:
                        return self._json(e.status, {"error": str(e)})
                    except (ValueError, TypeError) as e:
                        return self._json(400, {"error": str(e)})
                    except Exception as e:            # noqa: BLE001
                        log.exception("接口 %s %s 出错", self.command, path)
                        return self._json(500, {"error": f"服务内部错误：{e}"})
            return self._json(404, {"error": "接口不存在"})

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
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _dispatch

    return Handler


def make_server(app: App, host: str = "127.0.0.1", port: int = 8765,
                password: str = "") -> ThreadingHTTPServer:
    if host not in LOCAL_HOSTS and not password:
        raise ValueError(f"监听 {host} 会让其他机器访问到这个服务，必须先配置访问密码"
                         "（settings_local.py 的 service_password，或环境变量 OPINION_SERVICE_PASSWORD）")
    srv = ThreadingHTTPServer((host, int(port)), make_handler(app, password))
    srv.daemon_threads = True
    return srv


def serve(settings, host: Optional[str] = None, port: Optional[int] = None,
          scheduler_enabled: bool = True) -> int:
    host = host or getattr(settings, "service_host", "127.0.0.1")
    port = int(port or getattr(settings, "service_port", 8765))
    password = str(getattr(settings, "service_password", "") or "")
    try:
        app = App(settings, scheduler_enabled=scheduler_enabled)
    except StoreError as e:
        print(f"✗ 启动失败：{e}")
        print("  系统表默认建在计算指标用的同一个 MySQL 库里（settings_local.py 的 DB）；"
              "只想本机试用可以设 service_store = \"sqlite\"")
        return 2
    try:
        srv = make_server(app, host, port, password)
    except (ValueError, OSError) as e:
        print(f"✗ 启动失败：{e}")
        return 2
    app.start()
    print("=" * 72)
    print(f"指标计算服务已启动：http://{host}:{port}/"
          + ("   （已开启密码访问）" if password else "   （仅本机可访问）"))
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
