# -*- coding: utf-8 -*-
"""写入 ADS 表。

幂等策略按表的唯一索引情况自动选择（查 INFORMATION_SCHEMA，不写死）：

    有唯一索引  → INSERT ... ON DUPLICATE KEY UPDATE
    没有唯一索引 → 先按 (景区, 日期区间) DELETE 再 INSERT

维度表建表语句里只有 PRIMARY KEY(id)，直接 upsert 等同纯 INSERT，
同一天重跑一次就多一份重复行 —— 这是这套表最容易踩的坑。
执行 `sql/alter_dimension_uniquekey.sql` 补上唯一索引后会自动切回 upsert。
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


def load_table(db: MySQL, table: str, df: pd.DataFrame, *,
               date_col: str = "travel_date",
               key_col: str = "scenic_spot_code",
               batch_size: int = 2000,
               force_delete_insert: Optional[bool] = None,
               dry_run: bool = False) -> LoadResult:
    if df.empty:
        return LoadResult(table, 0, "skip")
    if dry_run:
        log.info("[dry-run] 将写入 %s：%d 行", table, len(df))
        return LoadResult(table, len(df), "dry-run")

    if force_delete_insert is None:
        force_delete_insert = not db.has_unique_key(table, db.cfg.ads_db())

    deleted = 0
    if force_delete_insert:
        start, end = int(df[date_col].min()), int(df[date_col].max())
        keys = sorted(df[key_col].astype(str).unique().tolist())
        deleted = db.delete_range(table, date_col, start, end, key_col, keys)
        strategy = "delete+insert"
    else:
        strategy = "upsert"

    rows = db.upsert_df(table, df, batch_size=batch_size)
    return LoadResult(table, rows, strategy, deleted)


def load_all(db: Optional[MySQL], tables: Dict[str, pd.DataFrame], *,
             settings, only: Optional[Sequence[str]] = None) -> List[LoadResult]:
    """把 builder 的产物按需落 CSV / 落库。"""
    results: List[LoadResult] = []
    for table, df in tables.items():
        if only and table not in set(only):
            continue
        if settings.write_csv:
            write_csv(df, settings.output_dir, table)
        if not settings.write_db:
            results.append(LoadResult(table, len(df), "csv-only"))
            continue
        force = True if (table == TABLE_DIMENSION and not settings.dimension_upsert) else None
        results.append(load_table(db, table, df, batch_size=settings.batch_size,
                                  key_col=scenic_key(table),
                                  force_delete_insert=force, dry_run=settings.dry_run))
    return results
