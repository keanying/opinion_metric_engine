# -*- coding: utf-8 -*-
"""写入 ADS 表。

写库策略（业务确认）：**每次运行按「景区 + publish_time」先删除、再写入**，六张表一律如此：

    表                                   删除条件
    ───────────────────────────────────  ──────────────────────────────────────────
    core / platform / dimension / content  scenic_spot_code IN (本次景区) AND publish_time IN (本次日期)
    macro 大盘表                           scenic_id       IN (本次景区) AND publish_time IN (本次日期)
    drill 下钻表                           scenic_id       IN (本次景区) AND travel_date  IN (本次日期)

· 删除范围是「本次跑的景区 × 本次跑的日期」，不是「新数据里出现过的 publish_time」——
  重算后某天某张表没有行了（比如某个词不再出现），旧行也要删掉，不能残留。
  所以即使这次某张表一行都没算出来，也会执行删除。
· 下钻表的 publish_time 是**评论自己的发布时间**（datetime，带时分秒），按值相等删不干净，
  所以按 travel_date（int yyyyMMdd，这一行属于哪一天）删。
  其余五张表的 publish_time 是 yyyyMMdd 字符串（settings.publish_time_format），与日期一一对应。
· 删除与写入在**同一个事务**里（db.replace_rows），写入失败整体回滚，不会出现删了没写的空窗。
· 不再依赖唯一索引（以前没有唯一索引的维度表靠「先删后插」、其余表靠 upsert），
  settings.dimension_upsert 已不再生效。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import pandas as pd

from .db import MySQL

log = logging.getLogger(__name__)

TABLE_CORE = "ads_trf_social_opinion_comment_core_di"
TABLE_PLATFORM = "ads_trf_social_opinion_comment_platform_di"
TABLE_DIMENSION = "ads_trf_social_opinion_comment_dimension_score_di"
TABLE_CONTENT = "ads_trf_social_opinion_comment_content_di"
TABLE_DRILL_ANALYSIS = "ads_trf_social_opinion_drill_analysis_di"
TABLE_MACRO = "ads_trf_social_opinion_macro_gran_metric_di"

# 各表的景区列名。**下钻表跟另外四张不一样**：它用 scenic_id（与源表同名），
# 其余四张用 scenic_spot_code。delete+insert 的 WHERE 条件要按表取，
# 写死一个名字的话，一旦某张表退化到 delete+insert 就会报 Unknown column。
# macro 大盘表同样用 scenic_id（需求 2.0 原文）。
SCENIC_KEY = {TABLE_DRILL_ANALYSIS: "scenic_id", TABLE_MACRO: "scenic_id"}
DEFAULT_SCENIC_KEY = "scenic_spot_code"


# 按 travel_date 删的表：下钻表的 publish_time 是评论发布时间（带时分秒），按值相等删不干净
DELETE_BY_TRAVEL_DATE = {TABLE_DRILL_ANALYSIS}


def scenic_key(table: str) -> str:
    return SCENIC_KEY.get(table, DEFAULT_SCENIC_KEY)


@dataclass
class LoadResult:
    table: str
    rows: int
    strategy: str
    deleted: int = 0


def write_csv(df: pd.DataFrame, out_dir: str, table: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{table}.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    log.info("导出 %s（%d 行）", path, len(df))
    return path


def _publish_values(dates: Sequence[str], fmt: str) -> List[str]:
    """目标日期 yyyyMMdd → 表里 publish_time 的写法（与 RunContext.publish_time 同一个格式）。"""
    if fmt == "%Y%m%d":
        return [str(d) for d in dates]
    return [pd.to_datetime(str(d), format="%Y%m%d").strftime(fmt) for d in dates]


def delete_scope(table: str, scenics: Sequence[str], dates: Sequence[str],
                 publish_time_format: str = "%Y%m%d") -> tuple:
    """这张表本次要先删掉哪些行：返回 (WHERE 条件, 参数)。规则见模块说明。"""
    key = scenic_key(table)
    scenics = [str(x) for x in scenics]
    where = f"`{key}` IN ({','.join(['%s'] * len(scenics))})"
    params: List = list(scenics)
    if table in DELETE_BY_TRAVEL_DATE:
        days = sorted({int(d) for d in dates})
        where += f" AND `travel_date` IN ({','.join(['%s'] * len(days))})"
        params += days
    else:
        vals = _publish_values(dates, publish_time_format)
        where += f" AND `publish_time` IN ({','.join(['%s'] * len(vals))})"
        params += vals
    return where, params


def load_table(db: MySQL, table: str, df: pd.DataFrame, *,
               scenics: Sequence[str], dates: Sequence[str],
               publish_time_format: str = "%Y%m%d",
               batch_size: int = 2000, dry_run: bool = False) -> LoadResult:
    """按「景区 + publish_time」（下钻表是 travel_date）先删除、再写入（同一事务）。df 为空也会执行删除。"""
    scenics = sorted({str(x) for x in scenics})
    if not scenics or not dates:
        return LoadResult(table, 0, "skip")
    where, params = delete_scope(table, scenics, dates, publish_time_format)
    if dry_run:
        log.info("[dry-run] %s：将删除 %s（%d 个景区 × %d 天），再写入 %d 行",
                 table, where.split(" AND ")[-1][:60], len(scenics), len(dates), len(df))
        return LoadResult(table, len(df), "dry-run")
    deleted, rows = db.replace_rows(table, df, where, params, batch_size=batch_size)
    return LoadResult(table, rows, "delete+insert", deleted)


def load_all(db: Optional[MySQL], tables: Dict[str, pd.DataFrame], *,
             settings, only: Optional[Sequence[str]] = None,
             scenics: Optional[Sequence[str]] = None,
             dates: Optional[Sequence[str]] = None) -> List[LoadResult]:
    """把 builder 的产物按需落 CSV / 落库。

    scenics / dates：本次跑的景区与目标日期（yyyyMMdd），决定写库前删哪些行。
    不传就从表里的数据推断 —— 但那样「这次一行都没算出来」的表就删不到旧行，
    所以 pipeline 一定会传。
    """
    results: List[LoadResult] = []
    fmt = getattr(settings, "publish_time_format", "%Y%m%d")
    for table, df in tables.items():
        if only and table not in set(only):
            continue
        if settings.write_csv:
            write_csv(df, settings.output_dir, table)
        if not settings.write_db:
            results.append(LoadResult(table, len(df), "csv-only"))
            continue
        sc = scenics
        if sc is None:
            key = scenic_key(table)
            sc = df[key].astype(str).unique().tolist() if key in df.columns else []
        ds = dates
        if ds is None:
            ds = df["travel_date"].astype(str).unique().tolist() if "travel_date" in df.columns else []
        results.append(load_table(db, table, df, scenics=sc, dates=ds,
                                  publish_time_format=fmt,
                                  batch_size=settings.batch_size, dry_run=settings.dry_run))
    return results
