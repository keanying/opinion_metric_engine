# -*- coding: utf-8 -*-
"""推送方式之二：写进另一个 MySQL 库（「写入数据源」），代替 HTTP 推送。

数据源在服务页面「推送 → 写入数据源」里添加（地址、账号、库名、表名对应），选它作为推送方式后，
跑批 / 补推 / 复制给其他景区 原本要 POST 给下游的表，改成写进这个库：

    连接数据源 → 每张表：删除「本次景区 × 本次日期」的旧行 → 写入新行（同一事务，失败回滚）→ 断开

几个决定
--------
· **删除范围和本地写库完全一样**（loader.delete_scope：景区 + publish_time，下钻表按 travel_date）：
  删的是「本次跑的景区 × 本次跑的日期」，不是「新数据里出现过的」，重算后没有的行也会清掉。
· **只写目标表有的列**：先查目标表的列，引擎有、目标表没有的列不写（报告里提示）；目标表多出来的列
  （自增 id、created_time …）不管，用它自己的默认值。删除用到的景区 / 日期列目标表必须有，没有就报错不写。
· **表名可以不一样**：数据源里配 {本地表名: 目标表名}，不配 = 同名。
· **结果用 PushResult / PushReport 返回**，和 HTTP 推送同一套：跑批报告、执行记录、推送记录页都照常显示；
  每张表在推送记录文件里记一行（result = ok / db_error / dry_run），不记密码。
· **连不上就停**：第一张表连接失败，后面的表不再试（同 HTTP 推送「地址连不上」），可之后补推。
· **连接、SSH 隧道、先删后写都用 dbutils 组件**（engin_cli/dbutils/）：这里只管「写哪些表、删什么范围、
  写哪些列、结果怎么报」。数据源开了 SSH 隧道（ssh_enabled）就经跳板机连，私钥是页面上传的内容（不落文件）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .dbutils import ConnectError, Database, DataSourceConfig
from .loader import delete_scope, scenic_key
from .pusher import PushReport, PushResult, write_push_log

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10


def open_db(ds: Dict[str, Any]) -> Database:
    """数据源（页面上存的 dict）→ dbutils.Database（还没真正连；开了 SSH 隧道就经跳板机连）。"""
    return Database(DataSourceConfig.from_dict(ds, connect_timeout=CONNECT_TIMEOUT))


def describe(ds: Dict[str, Any]) -> str:
    """「名称（host:port/库，经 SSH user@跳板机:端口）」，日志 / 推送记录里用，不含密码和密钥。"""
    return DataSourceConfig.from_dict(ds).describe()


def target_table(ds: Dict[str, Any], table: str) -> str:
    return str((ds.get("table_names") or {}).get(table) or "").strip() or table


def _delete_cols(table: str) -> List[str]:
    return [scenic_key(table), "travel_date" if table == "ads_trf_social_opinion_drill_analysis_di"
            else "publish_time"]


def check(ds: Dict[str, Any], tables: Dict[str, List[str]], opener=None) -> Dict[str, Any]:
    """测试连接（页面「测试」按钮）：连得上吗，每张表在目标库里有没有、缺哪些列。

    tables：{本地表名: 引擎会写的列}；opener(数据源) → 连接对象（不传 = open_db，测试里传假的）。
    出参：{"ok", "error", "via", "tables": [{"table", "target", "exists", "missing", "writable", "message"}]}
    """
    db = (opener or open_db)(ds)
    out: Dict[str, Any] = {"ok": False, "error": "", "via": "", "tables": []}
    try:
        try:
            db.connect()
        except ConnectError as e:
            out["error"] = str(e)
            return out
        except Exception as e:                       # noqa: BLE001 —— 连接失败原样告诉页面
            out["error"] = f"连不上：{_short(e)}"
            return out
        out["via"] = "经 SSH 隧道" if ds.get("ssh_enabled") else "直连"
        for table, cols in tables.items():
            tgt = target_table(ds, table)
            have = db.columns(tgt)
            item = {"table": table, "target": tgt, "exists": bool(have), "missing": [], "writable": False,
                    "message": ""}
            if not have:
                item["message"] = f"目标库里没有表 {tgt}"
            else:
                need = [c for c in _delete_cols(table) if c not in have]
                item["missing"] = [c for c in cols if c not in have]
                item["writable"] = not need
                item["message"] = (f"缺少删除用的列 {', '.join(need)}，不能写" if need else
                                   (f"缺 {len(item['missing'])} 列，这几列不写" if item["missing"] else "可以写入"))
            out["tables"].append(item)
        out["ok"] = True
        return out
    finally:
        try:
            db.close()
        except Exception:                            # noqa: BLE001
            pass


def _short(e: Exception) -> str:
    """pymysql 的异常 args 是 (错误码, 文字)，只留文字。"""
    a = getattr(e, "args", ())
    if len(a) >= 2 and isinstance(a[0], int):
        return f"{a[1]}（{a[0]}）"
    return str(e) or type(e).__name__


def _scope(table: str, df: pd.DataFrame, scope: Optional[Tuple[Sequence[str], Sequence[str]]]):
    """删除范围：调用方给了（本次景区 × 本次日期）就用它；没给就从数据里取。"""
    if scope and scope[0] and scope[1]:
        return [str(x) for x in scope[0]], [str(x) for x in scope[1]]
    key = scenic_key(table)
    sc = df[key].dropna().astype(str).unique().tolist() if key in df.columns else []
    ds = df["travel_date"].dropna().astype(int).astype(str).unique().tolist() \
        if "travel_date" in df.columns else []
    return sorted(sc), sorted(ds)


def write_table(db, ds: Dict[str, Any], table: str, df: pd.DataFrame, *, settings,
                scope=None) -> PushResult:
    """一张表：查目标表的列 → 删本次范围 → 写入（同一事务）。"""
    t0 = time.time()
    tgt = target_table(ds, table)
    r = PushResult(table=table, via="数据源")
    rec: Dict[str, Any] = {"table": table, "push_table": tgt, "url": f"mysql://{describe(ds)}",
                           "batch": 1, "batches": 1, "rows": int(len(df)), "attempt": 1}
    scenics, dates = _scope(table, df, scope)
    rec.update({"scenic": scenics, "travel_dates": dates})
    if not scenics or not dates:
        r.skipped = True
        r.elapsed = time.time() - t0
        return r
    try:
        have = db.columns(tgt)
        if not have:
            raise LookupError(f"目标库 {ds.get('database', '')} 里没有表 {tgt}")
        need = [c for c in _delete_cols(table) if c not in have]
        if need:
            raise LookupError(f"目标表 {tgt} 缺少删除用的列 {', '.join(need)}")
        cols = [c for c in df.columns if c in have]
        missing = [c for c in df.columns if c not in have]
        if missing:
            r.warning = f"目标表 {tgt} 没有这 {len(missing)} 列，不写：{', '.join(missing)}"
        where, params = delete_scope(table, scenics, dates,
                                     getattr(settings, "publish_time_format", "%Y%m%d"))
        size = max(int(getattr(settings, "push_batch_size", 500) or 500), 1)
        if getattr(settings, "push_dry_run", False):
            rec.update({"result": "dry_run", "response": f"试推：不写库，将删除 {len(scenics)} 个景区 × "
                                                         f"{len(dates)} 天的旧行，再写入 {len(df):,} 行"})
            r.rows, r.batches = len(df), (len(df) + size - 1) // size
        else:
            deleted, rows = db.replace_rows(tgt, df[cols], (where, params), batch_size=size)
            r.rows, r.batches = rows, (rows + size - 1) // size if rows else 0
            rec.update({"result": "ok", "rows": rows, "deleted": deleted,
                        "response": f"删除 {deleted:,} 行，写入 {rows:,} 行"})
    except Exception as e:                           # noqa: BLE001 —— 一张表写失败不影响其他表
        r.error = str(e) if isinstance(e, ConnectError) else _short(e)
        r.failed_batches = 1
        r.unreachable = isinstance(e, ConnectError)  # 连不上（含 SSH 隧道）：后面的表不再试
        rec.update({"result": "db_error", "error": r.error, "rows": 0})
    r.elapsed = time.time() - t0
    rec["elapsed"] = round(r.elapsed, 3)
    r.log_path = write_push_log(settings, rec)
    return r


def write_all(tables: Dict[str, pd.DataFrame], *, settings, scope=None,
              progress_factory=None, opener=None) -> PushReport:
    """推送方式 = 写入数据源时 push_all 走这里。出参同 push_all（PushReport）。"""
    rep = PushReport()
    ds = dict(getattr(settings, "push_db", None) or {})
    only = getattr(settings, "push_tables", None)
    only = set(only) if only else None
    todo = [(t, df) for t, df in tables.items() if only is None or t in only]
    db = (opener or open_db)(ds)
    try:
        for k, (table, df) in enumerate(todo):
            if df is None:
                continue
            r = write_table(db, ds, table, df, settings=settings, scope=scope)
            rep.results.append(r)
            log.info("写入数据源 %s · %s：%s", describe(ds), table,
                     "成功 %d 行" % r.rows if r.ok else r.error)
            if r.unreachable:
                for t2, _ in todo[k + 1:]:
                    rep.results.append(PushResult(table=t2, skipped=True, via="数据源",
                                                  error="未写入：数据源连不上，已停止（见上一张表的报错）"))
                break
    finally:
        try:
            db.close()
        except Exception:                            # noqa: BLE001
            pass
    return rep
