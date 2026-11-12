"""插件 score_redirect：大盘表得分重定向（任务页「插件」里打开，命令行 --plugins score_redirect）。

作用对象：大盘表每一行的
  总分   overall_sentiment_score      + 6 个一级维度分 dimension_breakdown[].dimension1Score
  上期   pre_overall_sentiment_score  + 6 个一级维度分 dimension_breakdown[].preDimension1Score
本期、上期各自按下面的规则处理一次。

规则
----
  deriveTotal(日期) = MD5("yyyy-MM-dd") 转整数，偶数 → 4.8，奇数 → 4.9（同一天结果固定）

  process(总分, 维度分, 日期)：
    总分 >= 4.8                → 不处理
    新总分 = deriveTotal(日期)
    维度分全部相等             → 总分、维度分全部 = 新总分
    最高维度分 > 新总分         → 总分 = 新总分，维度分不动
    否则                        → 总分 = 新总分，
                                   维度分全体 + (新总分 - 最高维度分 + 0.1)，超过 5 的按 5

  日期取「该周期区间的最后一天」：今日 / 本周 / 本月 / 本季 就是 travel_date；
  近 N 日（往前错一天）是 travel_date 前一天；上期取上期区间的最后一天。
  这样区间相同的行重定向后仍然一样（如 今日@11-11 = 近1日@11-12），
  上期分 = 上期那一天那一行的本期分，环比对得上。

在数据质量检测**之后**做：检测核对的是真实计算结果；重定向后的分数和表里的好/中/差计数
不再满足得分公式（score_explain.py 核对也会对不上），这是预期的。
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Sequence, Tuple

import pandas as pd

from .. import metric_calc_domain as D
from ..loader import TABLE_MACRO
from . import Plugin

MAX_SCORE = 5.0
STEP = 0.1
BASE_TOTAL = 4.8
HIGH_TOTAL = 4.9
DECIMALS = D.MACRO_SCORE_DECIMALS          # 和大盘表得分一样保留 6 位，免得出现 4.8999999


def derive_total(day) -> float:
    """日期（yyyyMMdd / Timestamp / date）→ 4.8 或 4.9，用 MD5（稳定哈希），同一天结果固定。"""
    t = pd.Timestamp(day) if not isinstance(day, str) else pd.to_datetime(day, format="%Y%m%d")
    key = f"{t.year:04d}-{t.month:02d}-{t.day:02d}"
    h = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16)
    return BASE_TOTAL if h % 2 == 0 else HIGH_TOTAL


def process(total: float, scores: Sequence[float], day) -> Tuple[float, List[float]]:
    """一组「总分 + 维度分」按规则重定向，返回 (新总分, 新维度分)。"""
    total = round(float(total), DECIMALS)
    scores = [round(float(s), DECIMALS) for s in scores]
    if total >= BASE_TOTAL:
        return total, scores
    new_total = derive_total(day)
    if not scores:
        return new_total, scores
    if max(scores) == min(scores):
        return new_total, [new_total] * len(scores)
    y_max = max(scores)
    if y_max > new_total:
        return new_total, scores
    delta = new_total - y_max
    return new_total, [round(min(MAX_SCORE, s + delta + STEP), DECIMALS) for s in scores]


def apply_macro(macro: pd.DataFrame, specs: Sequence[dict]) -> Tuple[pd.DataFrame, int]:
    """大盘表逐行重定向本期 / 上期得分。返回 (新表, 改动的行数)。"""
    if macro is None or macro.empty:
        return macro, 0
    by_name: Dict[str, dict] = {s["name"]: s for s in specs}
    ends: Dict[tuple, tuple] = {}

    def _ends(gran, day):
        k = (gran, day)
        if k not in ends:
            r = D.macro_period_ranges(by_name[gran], day)
            ends[k] = (r["cur"][1], r["prev"][1])
        return ends[k]

    out = macro.copy()
    totals, pres, breakdowns = [], [], []
    changed = 0
    for gran, day, tot, pre, bd in zip(out["time_granularity"], out["travel_date"],
                                       out["overall_sentiment_score"],
                                       out["pre_overall_sentiment_score"],
                                       out["dimension_breakdown"]):
        cur_end, prev_end = _ends(str(gran), str(day))
        dims = json.loads(bd) if isinstance(bd, str) and bd.strip() else []
        t2, s2 = process(tot, [d["dimension1Score"] for d in dims], cur_end)
        p2, ps2 = process(pre, [d["preDimension1Score"] for d in dims], prev_end)
        for d, a, b in zip(dims, s2, ps2):
            d["dimension1Score"], d["preDimension1Score"] = a, b
        if t2 != round(float(tot), DECIMALS) or p2 != round(float(pre), DECIMALS):
            changed += 1
        totals.append(t2)
        pres.append(p2)
        breakdowns.append(json.dumps(dims, ensure_ascii=False, separators=(",", ":")) if dims else bd)
    out["overall_sentiment_score"] = totals
    out["pre_overall_sentiment_score"] = pres
    out["dimension_breakdown"] = breakdowns
    return out, changed


def _apply(tables, ctx):
    if TABLE_MACRO not in tables:
        return "本次没有大盘表，未处理"
    tables[TABLE_MACRO], n = apply_macro(tables[TABLE_MACRO], ctx.macro_granularities)
    return f"大盘表 {n:,} 行的总分低于 4.8，已改为 4.8 / 4.9（维度分随之调整）"


PLUGIN = Plugin(
    name="score_redirect", label="得分重定向",
    description="大盘表总分低于 4.8 的，按日期改成 4.8 或 4.9，6 个一级维度分跟着调整（本期、上期都处理）",
    apply=_apply)
