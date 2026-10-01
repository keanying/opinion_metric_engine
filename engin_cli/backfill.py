# -*- coding: utf-8 -*-
"""缺数回补（需求 六.1）。

> 「如果当日平台数据少或没有数据，要主动往前获取数据进行补充计算」

做法：给每个 (景区[, 平台], 日期) 求一个**有效窗口跨度 span**——
当天评论数够（≥ min_count）就用当天（span=1）；不够就一天一天往前顺延，
直到累计够数或触达 max_span 上限。

口径边界（很重要，别搞混）
--------------------------
- `comment_count` / `positive_count` 这类**计数事实**默认**不回补**。
  它们是「当天真的收到多少条」，把 5 天的量塞进当天会让评论总数凭空膨胀，
  趋势图上出现假峰值，环比也跟着错。
- 回补只作用于**比率与得分**（好评率、差评率、情感得分、维度分数）。
  这些指标在样本量很小时本来就没有意义，用近几天的样本代替是统计上正当的平滑。
- 需要连计数一起回补时把 `backfill_apply_to_counts` 打开，
  但那之后「评论总数」的含义变成「近 span 天累计」，看板要同步说明。

回补跨度会写进跑批报告（`backfill_span > 1` 的行数与分布），
不要让它悄悄发生 —— 悄悄回补的数据比缺数更难排查。
"""

from __future__ import annotations

import logging
from typing import List, Sequence, Tuple

import numpy as np
import pandas as pd

from .windows import to_dt

log = logging.getLogger(__name__)

SPAN_COL = "backfill_span"


def resolve_backfill(
    daily: pd.DataFrame,
    key_cols: Sequence[str],
    date_col: str,
    count_col: str,
    value_cols: Sequence[str],
    min_count: int,
    max_span: int,
    enabled: bool = True,
) -> pd.DataFrame:
    """为每行求有效跨度，并给出该跨度下的累计值。

    返回：原 daily 的全部列 + `backfill_span` + 每个 value_col 的 `bf_<col>`。
    `bf_<col>` 是「近 span 天累计」，span=1 时与原值相等。
    """
    key_cols, value_cols = list(key_cols), list(value_cols)
    out = daily.copy()
    if out.empty:
        out[SPAN_COL] = pd.Series(dtype="int64")
        for c in value_cols:
            out[f"bf_{c}"] = pd.Series(dtype="float64")
        return out

    if not enabled or max_span <= 1:
        out[SPAN_COL] = 1
        for c in value_cols:
            out[f"bf_{c}"] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
        return out

    max_span = int(max_span)
    cube, index = _rolling_cube(out, key_cols, date_col, value_cols, max_span)
    # cube: (span, row, metric)；index 与 out 的行一一对应
    cnt_pos = value_cols.index(count_col)
    counts = cube[:, :, cnt_pos]                       # (span, row)

    ok = counts >= float(min_count)
    # 第一个满足阈值的 span；一个都不满足就用最大跨度（已经尽力了）
    first = np.where(ok.any(axis=0), ok.argmax(axis=0), max_span - 1)
    span = (first + 1).astype("int64")

    picked = np.take_along_axis(cube, first[None, :, None], axis=0)[0]   # (row, metric)

    out = out.reset_index(drop=True)
    out[SPAN_COL] = span[index]
    for i, c in enumerate(value_cols):
        out[f"bf_{c}"] = picked[index, i]

    n_bf = int((out[SPAN_COL] > 1).sum())
    if n_bf:
        log.info("缺数回补：%d/%d 行使用了向前顺延（最大跨度 %d 天，阈值 %d 条）",
                 n_bf, len(out), int(out[SPAN_COL].max()), min_count)
    return out


def backfill_with_span(
    daily: pd.DataFrame,
    key_cols: Sequence[str],
    date_col: str,
    value_cols: Sequence[str],
    span_map: pd.DataFrame,
    span_join_cols: Sequence[str],
) -> pd.DataFrame:
    """按**外部给定**的跨度做回补。

    维度表/内容表不自己决定回补跨度，而是沿用 core 表在 (景区, 日期) 上算出的 span：
    同一天的综合得分和维度得分必须建立在同一批样本上，各算各的会让
    「综合得分落在各一级维度得分之间」这条硬性口径关系直接破掉。
    """
    key_cols, value_cols = list(key_cols), list(value_cols)
    out = daily.copy().reset_index(drop=True)
    if out.empty:
        out[SPAN_COL] = pd.Series(dtype="int64")
        for c in value_cols:
            out[f"bf_{c}"] = pd.Series(dtype="float64")
        return out

    span = (out[list(span_join_cols)]
            .merge(span_map[list(span_join_cols) + [SPAN_COL]],
                   on=list(span_join_cols), how="left")[SPAN_COL]
            .fillna(1).astype("int64").clip(lower=1))
    max_span = int(span.max())
    if max_span <= 1:
        out[SPAN_COL] = 1
        for c in value_cols:
            out[f"bf_{c}"] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
        return out

    cube, index = _rolling_cube(out, key_cols, date_col, value_cols, max_span)
    # cube 的第 1 维是「补齐后的 (key × date) 网格」，长度与 out 的行数不同，
    # 必须先把逐行 span 铺回网格位置，再做 take_along_axis。
    pick = np.zeros(cube.shape[1], dtype="int64")
    pick[index] = span.to_numpy() - 1
    picked = np.take_along_axis(cube, pick[None, :, None], axis=0)[0]
    out[SPAN_COL] = span.to_numpy()
    for i, c in enumerate(value_cols):
        out[f"bf_{c}"] = picked[index, i]
    return out


def _rolling_cube(daily: pd.DataFrame, key_cols: Sequence[str], date_col: str,
                  value_cols: Sequence[str], max_span: int) -> Tuple[np.ndarray, np.ndarray]:
    """构造 (span, row, metric) 的滚动累计立方体。

    先把 (key, date) 补成连续日历再滚动 —— 缺采日必须以 0 参与，
    否则「往前顺延 3 天」会实际跨过 10 个自然日，回补出来的比率张冠李戴。
    """
    df = daily[key_cols + [date_col] + list(value_cols)].copy()
    df["_dt"] = to_dt(df[date_col])
    for c in value_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0).astype("float64")

    keys = df[key_cols].drop_duplicates().reset_index(drop=True)
    keys["_kid"] = np.arange(len(keys), dtype="int64")
    df = df.merge(keys, on=key_cols, how="left")

    full_dates = pd.date_range(df["_dt"].min(), df["_dt"].max(), freq="D")
    grid = pd.MultiIndex.from_product([keys["_kid"], full_dates], names=["_kid", "_dt"])

    panel = (df.groupby(["_kid", "_dt"])[list(value_cols)].sum()
             .reindex(grid, fill_value=0.0).sort_index())

    n_key, n_date, n_metric = len(keys), len(full_dates), len(value_cols)
    arr = panel.to_numpy(dtype="float64").reshape(n_key, n_date, n_metric)

    cube = np.empty((max_span, n_key, n_date, n_metric), dtype="float64")
    acc = np.zeros_like(arr)
    for s in range(max_span):
        shifted = np.zeros_like(arr)
        if s == 0:
            shifted = arr
        else:
            shifted[:, s:, :] = arr[:, :-s, :]
        acc = acc + shifted
        cube[s] = acc

    # 把立方体压回 (span, row, metric)，row 用 (kid, date) 定位
    date_pos = {d: i for i, d in enumerate(full_dates)}
    row_kid = df["_kid"].to_numpy()
    row_date = df["_dt"].map(date_pos).to_numpy()
    flat = cube.reshape(max_span, n_key * n_date, n_metric)
    row_index = row_kid * n_date + row_date
    return flat, row_index


def backfill_report(df: pd.DataFrame, label: str) -> List[str]:
    """把回补情况转成人读的几行，供跑批日志/报告使用。"""
    if df.empty or SPAN_COL not in df.columns:
        return []
    vc = df[SPAN_COL].value_counts().sort_index()
    if len(vc) <= 1 and int(vc.index[0]) == 1:
        return [f"{label}: 无回补"]
    parts = [f"span={int(k)}天×{int(v)}行" for k, v in vc.items() if int(k) > 1]
    return [f"{label}: 回补 {int((df[SPAN_COL] > 1).sum())}/{len(df)} 行  " + "  ".join(parts)]
