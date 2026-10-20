# -*- coding: utf-8 -*-
"""造演示数据：把一个景区在库里的结果整份复制给另一个景区编号，再推给下游。

用途：下游要一个新景区的样子看效果，又还没有它的评论。拿现成景区（如九嶷山 PFTSCA01016184）
一段日期的结果，景区编号 / 名称换成新的，其余原样，走正常的推送接口推过去。

    python -m engin_cli.cli mock --from-scenic PFTSCA01016184 \\
        --to-scenic PFTSCA09999999 --to-name 演示景区 \\
        --start-date 20260701 --end-date 20261019 --tables drill,macro

几个决定
--------
· **只读本地 ADS 库、只推送，不写库**：演示数据不混进本地结果表，删起来也只用找下游。
· **只取引擎自己产出的列**（各表 COLUMNS 与库表列的交集）：客户在表上加的列
  （handle_flag / created_time / 自增 id …）不推，跟跑批直推的报文一致。
· **下钻表的 detail_uk 按新景区重算**：它是 md5(景区+词+渠道+作品+评论+日期)，
  不重算的话两个景区的行 pkId 相同，下游 upsert 会把原景区的数据改成新景区。
· **新旧编号相同直接拒绝**：那等于拿演示名称覆盖真实景区的数据。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Sequence

import pandas as pd

from . import metric_calc_domain as D
from .drill_analysis import COLUMNS as DRILL_COLUMNS, _uk
from .loader import (TABLE_CONTENT, TABLE_CORE, TABLE_DIMENSION,
                     TABLE_DRILL_ANALYSIS, TABLE_MACRO, TABLE_PLATFORM, scenic_key)

log = logging.getLogger(__name__)

# 每张表推送哪些列 —— 与跑批直推的 DataFrame 一致
TABLE_COLUMNS: Dict[str, List[str]] = {
    TABLE_CORE: list(D.CORE_COLUMNS),
    TABLE_PLATFORM: list(D.PLATFORM_COLUMNS),
    TABLE_DIMENSION: list(D.DIMENSION_COLUMNS),
    TABLE_CONTENT: list(D.CONTENT_COLUMNS),
    TABLE_DRILL_ANALYSIS: list(DRILL_COLUMNS),
    TABLE_MACRO: list(D.MACRO_COLUMNS),
}

# 景区名称列：跟景区编号列一样，下钻表 / 大盘表叫 scenic_name，其余四张叫 scenic_spot_name
SCENIC_NAME_COL = {TABLE_DRILL_ANALYSIS: "scenic_name", TABLE_MACRO: "scenic_name"}


def scenic_name_col(table: str) -> str:
    return SCENIC_NAME_COL.get(table, "scenic_spot_name")


def drill_detail_uk(df: pd.DataFrame) -> List[str]:
    """下钻表 detail_uk，与 drill_analysis._expand 同一个公式。"""
    return [_uk(*k) for k in zip(df["scenic_id"], df["emotion_word"], df["channel"],
                                  df["work_id"], df["comment_id"], df["travel_date"])]


def retarget(table: str, df: pd.DataFrame, to_scenic: str, to_name: str,
             etl_time: Optional[datetime] = None) -> pd.DataFrame:
    """把一张表的行改到新景区名下：编号、名称换掉，下钻表重算 detail_uk，etl_time 记成现在。"""
    out = df.copy()
    out[scenic_key(table)] = to_scenic
    name_col = scenic_name_col(table)
    if name_col in out.columns:
        out[name_col] = to_name
    if table == TABLE_DRILL_ANALYSIS and "detail_uk" in out.columns:
        out["detail_uk"] = drill_detail_uk(out)
    if "etl_time" in out.columns:
        out["etl_time"] = pd.Timestamp(etl_time or datetime.now()).floor("s")
    return out


def _table_columns(db, table: str) -> List[str]:
    """库里这张表实际有哪些列（库表比代码旧时少几列，照样能推）。"""
    df = db.query_df(
        "select column_name from information_schema.columns "
        "where table_schema=%s and table_name=%s", (db.cfg.ads_db(), table))
    return [str(c) for c in df.iloc[:, 0].tolist()] if not df.empty else []


def fetch_scenic_tables(db, from_scenic: str, start: str, end: str,
                        tables: Sequence[str]) -> tuple:
    """从本地 ADS 库读一个景区一段日期的各表数据。

    出参：({表: DataFrame}, [说明])；库里没有的表、没数据的表不在结果里。
    """
    out: Dict[str, pd.DataFrame] = {}
    notes: List[str] = []
    ads = db.cfg.ads_db()
    for table in tables:
        have = _table_columns(db, table)
        if not have:
            notes.append(f"{table}：库里没有这张表，跳过")
            continue
        want = TABLE_COLUMNS[table]
        cols = [c for c in want if c in have]
        missing = [c for c in want if c not in have]
        if missing:
            notes.append(f"{table}：库表缺 {len(missing)} 列，不推这几列：{', '.join(missing)}")
        full = f"`{ads}`.`{table}`" if ads else f"`{table}`"
        sql = (f"select {', '.join(f'`{c}`' for c in cols)} from {full} "
               f"where `{scenic_key(table)}` = %s and `travel_date` between %s and %s")
        df = db.query_df(sql, (from_scenic, int(start), int(end)))
        if df.empty:
            notes.append(f"{table}：{from_scenic} 在 {start}~{end} 没有数据")
            continue
        df["travel_date"] = pd.to_numeric(df["travel_date"]).astype("int64")
        out[table] = df.sort_values("travel_date", kind="stable").reset_index(drop=True)
    return out, notes


def build_mock(db, from_scenic: str, to_scenic: str, to_name: str,
               start: str, end: str, tables: Sequence[str]) -> tuple:
    """读出来 + 换到新景区。出参：({表: DataFrame}, [说明])。"""
    if str(from_scenic).strip() == str(to_scenic).strip():
        raise ValueError(f"新景区编号和原景区一样（{to_scenic}）：这样会用演示数据覆盖真实景区，"
                         "请换一个新编号")
    src, notes = fetch_scenic_tables(db, from_scenic, start, end, tables)
    now = datetime.now()
    return {t: retarget(t, df, to_scenic, to_name, now) for t, df in src.items()}, notes
