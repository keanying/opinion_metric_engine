# -*- coding: utf-8 -*-
"""公式层：得分、环比、占比。

单独成模块的原因：这些是产品规则文档里逐字写死的东西，
四张表都要用，改一个地方要能全链路生效，也方便单测直接打靶。
"""

from __future__ import annotations

from typing import Optional, Union

import numpy as np
import pandas as pd

from .constants import (
    CONFIDENCE_T, RATE_DECIMALS, SCORE_DECIMALS, SCORE_MAX, SCORE_MIN,
    SCORE_NEUTRAL, SCORE_SPAN,
)

Num = Union[int, float, None]


# ============================================================
# 基础工具
# ============================================================
def safe_div(num, den, default=None):
    """除法兜底。分母为 0 / 空 时返回 default，而不是 inf / NaN。"""
    num = np.asarray(num, dtype="float64")
    den = np.asarray(den, dtype="float64")
    out = np.full(num.shape, np.nan, dtype="float64")
    mask = np.isfinite(den) & (den != 0)
    np.divide(num, den, out=out, where=mask)
    if default is not None:
        out = np.where(mask, out, float(default))
    return out


def growth_rate(cur, prev, default=0.0, decimals: int = RATE_DECIMALS):
    """环比增长率 =（本期 - 上期）/ 上期。

    上期为 0 时数学上没有定义。这里返回 default（默认 0）而不是 inf ——
    decimal(10,4) 存不下 inf，看板上「从 0 涨到 100」的正确表达是「新增」，
    应由展示层根据 上期=0 且 本期>0 自行标注，不能靠一个爆炸的数字。
    """
    cur = np.asarray(cur, dtype="float64")
    prev = np.asarray(prev, dtype="float64")
    out = np.full(cur.shape, float(default), dtype="float64")
    mask = np.isfinite(prev) & (prev != 0)
    np.divide(cur - prev, prev, out=out, where=mask)
    return np.round(out, decimals)


def confidence(total, T: int = CONFIDENCE_T):
    """置信度权重 min(1, 总数/T)。总评论数不足 T 时得分向中性收敛。"""
    total = np.asarray(total, dtype="float64")
    return np.clip(np.nan_to_num(total, nan=0.0) / float(T), 0.0, 1.0)


# ============================================================
# 官方得分公式
# ============================================================
def official_score(net, total, T: int = CONFIDENCE_T, decimals: int = SCORE_DECIMALS):
    """得分 = 5 + net × 5 × min(1, 总数/T)，net = 好评率 - 差评率。"""
    net = np.asarray(net, dtype="float64")
    score = SCORE_NEUTRAL + net * SCORE_SPAN * confidence(total, T)
    score = np.clip(np.nan_to_num(score, nan=SCORE_NEUTRAL), SCORE_MIN, SCORE_MAX)
    return np.round(score, decimals)


def official_score_from_counts(pos, neg, total, T: int = CONFIDENCE_T,
                               decimals: int = SCORE_DECIMALS):
    """按正/负/总数直接算得分。

    规则文档原式：
        周期得分 = 5 + [(正面总分/总评论数) + (负面总分/总评论数)] × 5 × min(1, 总评论数/T)
        其中 负面总分 = -1 × 负面评价数量
    展开即 5 + (好评率 - 差评率) × 5 × conf。

    总数为 0 时返回中性 5 —— 「没有评论」不等于「评价很差」，
    返回 0 分会让空数据的景区在看板上排到最后一名，是错的。
    """
    pos = np.asarray(pos, dtype="float64")
    neg = np.asarray(neg, dtype="float64")
    total = np.asarray(total, dtype="float64")
    net = np.zeros(np.broadcast(pos, neg, total).shape, dtype="float64")
    mask = np.isfinite(total) & (total > 0)
    np.divide(pos - neg, total, out=net, where=mask)
    score = official_score(net, total, T, decimals)
    return np.where(mask, score, round(SCORE_NEUTRAL, decimals))


def star_rating(pos, neu, neg, total, decimals: int = SCORE_DECIMALS):
    """daily_rating（统计时刻均分）：正=5 星、中=3 星、负=1 星的加权均分，值域 [1, 5]。

    它跟 emotional_score 是两个刻度：前者是「几星」，后者是 0~10 的情感得分。
    看板上「均分 4.3」和「情感得分 8.6」并排出现时不要互相校验。
    """
    from .constants import STAR_BY_SENTIMENT as S
    pos = np.asarray(pos, dtype="float64")
    neu = np.asarray(neu, dtype="float64")
    neg = np.asarray(neg, dtype="float64")
    total = np.asarray(total, dtype="float64")
    num = pos * S[1] + neu * S[0] + neg * S[-1]
    out = np.full(np.asarray(total).shape, np.nan, dtype="float64")
    mask = np.isfinite(total) & (total > 0)
    np.divide(num, total, out=out, where=mask)
    return np.round(np.where(mask, out, 3.0), decimals)


# ============================================================
# Series 版本（builders 里直接对列用）
# ============================================================
def s_growth(cur: pd.Series, prev: pd.Series, default=0.0) -> pd.Series:
    return pd.Series(growth_rate(cur.to_numpy(), prev.to_numpy(), default=default),
                     index=cur.index)


def s_rate(num: pd.Series, den: pd.Series, decimals: int = RATE_DECIMALS,
           default=0.0) -> pd.Series:
    v = safe_div(num.to_numpy(), den.to_numpy(), default=default)
    return pd.Series(np.round(v, decimals), index=num.index)


def s_score(pos: pd.Series, neg: pd.Series, total: pd.Series,
            T: int = CONFIDENCE_T) -> pd.Series:
    return pd.Series(
        official_score_from_counts(pos.to_numpy(), neg.to_numpy(), total.to_numpy(), T=T),
        index=pos.index)
