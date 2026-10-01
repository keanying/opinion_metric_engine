# -*- coding: utf-8 -*-
"""看板取数参考实现。

规则文档里每个看板组件，从四张 ADS 表怎么取——这里写一遍，BI 侧照抄即可。
它同时是跑批后的「肉眼验收」入口：`python -m engin_cli.cli report` 会打印一份。

所有函数只读 ADS 表，不碰源表 —— 这是刻意的：如果某个组件必须回源表才能算，
说明 ADS 表的口径缺了东西，应该改 builder 而不是在 BI 里补算。
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import metric_calc_domain as D
from .metric_calc_domain import PERIODS, TOP_DIM_N, TREND_SPAN, growth_rate, word_surge
from .windows import prev_window_dates, window_dates

# 看板周期 → 各表的窗口字段名。
# 这里不手写字段名，一律从 domain 的注册表按「周期对应的天数」查出来，
# 改字段命名只需要改 domain 一处。
_CORE_SFX = {p: D.CORE_SUFFIX[n] for p, n in PERIODS.items()}
_DIM_SFX = {p: D.DIM_SUFFIX[n] for p, n in PERIODS.items()}
_PLAT_CNT = {p: D.PLAT_CNT[n] for p, n in PERIODS.items()}
_PLAT_RATE = {p: D.PLAT_RATE[n] for p, n in PERIODS.items()}
_PLAT_WOW = {p: D.PLAT_WOW[n] for p, n in PERIODS.items()}
_CNT_GROWTH = {p: D.CORE_CNT_GROWTH[n] for p, n in PERIODS.items()}
_CONTENT_VAL = {p: D.CONTENT_VALUE[n] for p, n in PERIODS.items()}


def _pick(df: pd.DataFrame, scenic: str, anchor: str) -> pd.DataFrame:
    return df[(df.scenic_spot_code == scenic) & (df.travel_date.astype(str) == str(anchor))]


# ---------- §2 核心数值卡 ----------
def kpi_cards(core: pd.DataFrame, scenic: str, anchor: str, period: str = "today") -> Dict:
    """评论总数 / 好评率 / 差评率 + 各自环比。

    比率和环比直接读锚点行上算好的窗口字段；**评论总数要自己按周期求和** ——
    core 表的 comment_count 是日粒度事实，没有 comment_count_7d 这种字段
    （DDL 里就没有），周期总量由日粒度累加得到，与窗口字段口径一致。
    """
    sfx = _CORE_SFX[period]
    row = _pick(core, scenic, anchor)
    if row.empty:
        return {}
    r = row.iloc[0]
    days = set(window_dates(anchor, PERIODS[period]))
    total = int(core[(core.scenic_spot_code == scenic)
                     & core.travel_date.astype(str).isin(days)]["comment_count"].sum())
    return {
        "评论总数": {"值": total, "环比": float(r[_CNT_GROWTH[period]])},
        "好评率": {"值": float(r[f"positive_rate{sfx}"]),
                   "环比": float(r[f"positive_growth_rate{sfx}"])},
        "差评率": {"值": float(r[f"negative_rate{sfx}"]),
                   "环比": float(r[f"negative_growth_rate{sfx}"])},
    }


# ---------- §3 评论量趋势图 ----------
def trend_stacked(core: pd.DataFrame, scenic: str, anchor: str,
                  period: str = "today") -> pd.DataFrame:
    days = set(window_dates(anchor, TREND_SPAN[period]))
    sub = core[(core.scenic_spot_code == scenic) & core.travel_date.astype(str).isin(days)]
    return (sub.sort_values("travel_date")[
        ["travel_date", "positive_count", "neutral_count", "negative_count"]]
        .rename(columns={"positive_count": "正面", "neutral_count": "中性",
                         "negative_count": "负面"}))


# ---------- §4 游客总体评分 ----------
def overall_score(core: pd.DataFrame, scenic: str, anchor: str,
                  period: str = "today") -> Dict:
    sfx = _CORE_SFX[period]
    row = _pick(core, scenic, anchor)
    if row.empty:
        return {}
    cur = float(row.iloc[0][f"emotional_score{sfx}"])
    n = PERIODS[period]
    prev_anchor = (pd.to_datetime(str(anchor), format="%Y%m%d")
                   - pd.Timedelta(days=n)).strftime("%Y%m%d")
    prow = _pick(core, scenic, prev_anchor)
    prev = float(prow.iloc[0][f"emotional_score{sfx}"]) if not prow.empty else None
    return {"周期得分": round(cur, 1), "周期得分_原始": cur,
            "上期得分": None if prev is None else round(prev, 1),
            "环比": None if prev in (None, 0) else float(growth_rate(cur, prev))}


def top_dimensions(dim: pd.DataFrame, scenic: str, anchor: str,
                   period: str = "today", n: int = TOP_DIM_N) -> List:
    """维度好评 TOP3（一级维度）。"""
    sfx = _DIM_SFX[period]
    sub = _pick(dim, scenic, anchor)
    if sub.empty:
        return []
    g = (sub.groupby("dimension_level1")[f"dimension_1_score{sfx}"].first()
         .sort_values(ascending=False).head(n))
    return [(k, round(float(v), 4)) for k, v in g.items()]


# ---------- §5 维度评分变化趋势图 ----------
def dimension_trend(dim: pd.DataFrame, scenic: str, anchor: str, period: str = "today",
                    dims: Optional[List[str]] = None) -> pd.DataFrame:
    sfx = _DIM_SFX[period]
    days = set(window_dates(anchor, TREND_SPAN[period]))
    sub = dim[(dim.scenic_spot_code == scenic) & dim.travel_date.astype(str).isin(days)]
    if dims:
        sub = sub[sub.dimension_level1.isin(dims)]
    if sub.empty:
        return pd.DataFrame()
    return (sub.groupby(["travel_date", "dimension_level1"])[f"dimension_1_score{sfx}"]
            .first().unstack().sort_index())


# ---------- §6 平台评论分布 ----------
def platform_distribution(plat: pd.DataFrame, scenic: str, anchor: str,
                          period: str = "today") -> pd.DataFrame:
    sub = _pick(plat, scenic, anchor)
    if sub.empty:
        return pd.DataFrame()
    cnt, rate, wow = _PLAT_CNT[period], _PLAT_RATE[period], _PLAT_WOW[period]
    out = sub[["platform_code", "platform_name", cnt, rate, wow]].copy()
    out.columns = ["platform_code", "platform_name", "评论总量", "好评占比", "好评环比"]
    total = out["评论总量"].sum()
    out["评论占比"] = (out["评论总量"] / total).round(4) if total else 0.0
    return out.sort_values("评论总量", ascending=False).reset_index(drop=True)


# ---------- §7 关键词云 ----------
def wordcloud(content: pd.DataFrame, scenic: str, anchor: str, period: str = "today",
              emotion: str = "negative", top: int = 20) -> pd.DataFrame:
    val = _CONTENT_VAL[period]
    sub = _pick(content, scenic, anchor)
    sub = sub[sub.emotion_type == emotion]
    if sub.empty:
        return pd.DataFrame()
    return (sub.sort_values(val, ascending=False).head(top)[["emotion_word", val]]
            .rename(columns={val: "出现次数"}).reset_index(drop=True))


# ---------- §8 负面突增词 ----------
def negative_surge(content: pd.DataFrame, scenic: str, anchor: str,
                   period: str = "today", top: int = D.SURGE_TOP_N) -> pd.DataFrame:
    """突增量 = 本周期出现次数 - 上一周期出现次数。

    内容表存的是「近 N 日累计出现次数」，上一周期的值就在 N 天前那一行的同一个字段上，
    所以两期一减即可，不需要回源表。
    """
    val = _CONTENT_VAL[period]
    n = PERIODS[period]
    prev_anchor = (pd.to_datetime(str(anchor), format="%Y%m%d")
                   - pd.Timedelta(days=n)).strftime("%Y%m%d")

    cur = _pick(content, scenic, anchor)
    cur = cur[cur.emotion_type == "negative"][["emotion_word", val]].rename(
        columns={val: "本期"})
    prev = _pick(content, scenic, prev_anchor)
    prev = prev[prev.emotion_type == "negative"][["emotion_word", val]].rename(
        columns={val: "上期"})
    if cur.empty:
        return pd.DataFrame()

    df = cur.merge(prev, on="emotion_word", how="left").fillna({"上期": 0})
    df["突增量"], df["突增率"] = word_surge(df["本期"], df["上期"])   # 口径见 domain
    return df.sort_values("突增量", ascending=False).head(top).reset_index(drop=True)
