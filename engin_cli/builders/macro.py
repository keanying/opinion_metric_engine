# -*- coding: utf-8 -*-
"""ads_trf_social_opinion_macro_gran_metric_di —— 舆情大盘不同周期粒度 KPI 指标表（需求 2.0）。

一行 = 景区 × 日期 × 周期粒度 × 渠道（配置的渠道 + all）。看板「总览」页的
周期页签 + 平台下拉选中的就是这一行，所以每个组合**恰好一行**，没数据也出（全 0）。

本文件只做搬运：周期 → 日期区间（domain §1.10 / 第 10 章）→ 区间求和 → 调 domain 算指标。

为什么不用 rolling_windows 直接滚
--------------------------------
这张表的周期不全是「近 N 日」：wtd / mtd / qtd 是日历周期，上期是「上周期的同样几天」，
同比要去年同期 —— 都不是固定宽度的滑窗。所以这里把每个周期翻译成**日期区间**，
再对日粒度事实做区间求和（按日期排好序，searchsorted 切片 + bincount）。

缺数回补（domain §1.8 / §1.10）：先补明细，再算指标
--------------------------------------------------
每个渠道每一天：当天有评论就用当天的明细；没有 → 用往前最近一个有评论那天的明细
（这个周期的档位最多往前找 L 天，找不到就是空的）。本期、上期、去年同期三段区间
都在这份补齐后的每日明细上求和，区间本身不挪。all 行 = 各渠道补齐后相加。

实现上不真的复制明细：区间里每一天先找到它的「来源日」，数一数每个来源日被用了几次，
再按次数加权求和（_FillPlan + _RangeSum.weighted）。评论 / 维度 / 词 / 地域四份
求和器用同一份来源日，所以同一行的数字来自同一份补齐后的明细。

与 core / platform 同一套补齐规则、同一个上限表，所以：

    macro latest_7d 某渠道 comment_total == platform.comment_cnt_7d
    macro td 的 all 行 comment_total   == core.comment_count
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

from .. import metric_calc_domain as D
from ..context import RunContext
from ..windows import to_dt
from .platform import platform_scope

COLUMNS = D.MACRO_COLUMNS
_PKEY = ["scenic_spot_code", "platform_code"]
# 计数向量的列序：[好, 中, 差]
_SENT_ORDER = [D.POSITIVE, D.NEUTRAL, D.NEGATIVE]
_SENT_IDX = {s: i for i, s in enumerate(_SENT_ORDER)}
_CNT_KEYS = ["pos", "neu", "neg"]


# ══════════════════════════════════════════════════════════════════════
# 取数范围：pipeline 在抽数之前调用，保证本表用到的每一段日期都取到了
# ══════════════════════════════════════════════════════════════════════
def macro_data_ranges(ctx: RunContext) -> List[Tuple[str, str]]:
    """本表需要的源数据日期段（yyyyMMdd 闭区间，已合并），含 [fill_start, load_end]。

    上期可能早于 load_start（如 qtd 的上期），同比要去年同期 —— 这两段都要额外取；
    每段再往前多取「这个周期最多往前找几天」，区间第一天没评论时才补得出来。
    """
    spans = [(pd.to_datetime(ctx.fill_start, format=D.DATE_FMT),
              pd.to_datetime(ctx.load_end, format=D.DATE_FMT))]
    for spec in ctx.macro_granularities:
        lb = pd.Timedelta(days=_lookback(ctx, spec))
        for t in ctx.output_dates:
            r = D.macro_period_ranges(spec, t)
            for lo, hi in r.values():
                spans.append((lo - lb, hi))
    spans.sort()
    merged = [list(spans[0])]
    for lo, hi in spans[1:]:
        if lo <= merged[-1][1] + pd.Timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo.strftime(D.DATE_FMT), hi.strftime(D.DATE_FMT)) for lo, hi in merged]


def _lookback(ctx: RunContext, spec: dict) -> int:
    """这个周期最多往前找几天（domain §1.10：按 macro_backfill_window 查 §1.8 的上限表）。"""
    w = D.macro_backfill_window(spec)
    return int(ctx.backfill_lookback([w]).get(w, 0))


# ══════════════════════════════════════════════════════════════════════
# 区间求和
# ══════════════════════════════════════════════════════════════════════
class _RangeSum:
    """(景区, 渠道) → 按日排好序的 (日序号, 类别, 计数)。

    sum(scenic, parts) 把若干 (渠道, 起, 止) 区间里的计数按类别加总，
    返回长度为 ncat 的向量。all 行就是把各渠道的区间一起传进来。
    """

    def __init__(self, df: pd.DataFrame, cat_col: str, ncat: int):
        self.ncat = int(ncat)
        self.groups: Dict[tuple, tuple] = {}
        self.cum: Dict[tuple, np.ndarray] = {}
        if df.empty:
            return
        df = df.sort_values(["scenic_spot_code", "platform_code", "_day"])
        for key, g in df.groupby(["scenic_spot_code", "platform_code"], sort=False):
            self.groups[key] = (g["_day"].to_numpy(dtype="int64"),
                                g[cat_col].to_numpy(dtype="int64"),
                                g["_n"].to_numpy(dtype="float64"))
            self.cum[key] = np.concatenate([[0.0], np.cumsum(self.groups[key][2])])

    def total(self, scenic: str, ch: str, lo: int, hi: int) -> float:
        """某渠道 [lo, hi] 里的计数合计（不分类别）。判断「窗口有没有数据」用，前缀和 O(log n)。"""
        g = self.groups.get((scenic, ch))
        if g is None:
            return 0.0
        a = np.searchsorted(g[0], lo, side="left")
        b = np.searchsorted(g[0], hi, side="right")
        c = self.cum[(scenic, ch)]
        return float(c[b] - c[a])

    def weighted(self, scenic: str, ch: str, src_days: np.ndarray,
                 mult: np.ndarray) -> np.ndarray:
        """补齐后的区间合计：来源日 src_days（升序）各自的明细 × 被用的次数 mult。"""
        out = np.zeros(self.ncat, dtype="float64")
        g = self.groups.get((scenic, ch))
        if g is None or len(src_days) == 0:
            return out
        days, cats, n = g
        a = np.searchsorted(days, src_days[0], side="left")
        b = np.searchsorted(days, src_days[-1], side="right")
        if b <= a:
            return out
        d = days[a:b]
        pos = np.clip(np.searchsorted(src_days, d), 0, len(src_days) - 1)
        w = np.where(src_days[pos] == d, mult[pos], 0.0)
        return np.bincount(cats[a:b], weights=n[a:b] * w, minlength=self.ncat)

    def sum(self, scenic: str, parts: Sequence[tuple]) -> np.ndarray:
        out = np.zeros(self.ncat, dtype="float64")
        for ch, lo, hi in parts:
            g = self.groups.get((scenic, ch))
            if g is None:
                continue
            days, cats, n = g
            a = np.searchsorted(days, lo, side="left")
            b = np.searchsorted(days, hi, side="right")
            if b > a:
                out += np.bincount(cats[a:b], weights=n[a:b], minlength=self.ncat)
        return out


def _daily(df: pd.DataFrame, cat_col: str, base: pd.Timestamp) -> pd.DataFrame:
    """事实明细 → (景区, 渠道, 日序号, 类别) 计数。"""
    if df.empty:
        return pd.DataFrame(columns=_PKEY + ["_day", cat_col, "_n"])
    g = (df.groupby(_PKEY + ["travel_date", cat_col], as_index=False)
         .size().rename(columns={"size": "_n"}))
    g["_day"] = (to_dt(g["travel_date"]) - base).dt.days.astype("int64")
    return g


# ══════════════════════════════════════════════════════════════════════
# 明细补齐：区间里每一天 → 来源日（domain §1.8）
# ══════════════════════════════════════════════════════════════════════
class _FillPlan:
    """(景区, 渠道) 有评论的日子 → 给定区间与上限，算出每个来源日被用了几次。"""

    def __init__(self, comments: "_RangeSum"):
        # 「渠道当天有评论」= 评论求和器里这一天有行（每条评论都计数）
        self.present = {k: np.unique(g[0]) for k, g in comments.groups.items()}
        self._cache: Dict[tuple, tuple] = {}

    def sources(self, scenic: str, ch: str, lo: int, hi: int, lookback: int) -> tuple:
        """[lo, hi] 补齐后用到的 (来源日升序数组, 每个来源日的次数)。"""
        key = (scenic, ch, lo, hi, lookback)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        p = self.present.get((scenic, ch))
        if p is None or hi < lo:
            out = (np.array([], dtype="int64"), np.array([], dtype="float64"))
        else:
            days = np.arange(lo, hi + 1, dtype="int64")
            i = np.searchsorted(p, days, side="right") - 1        # 最近一个 <= 当天的有数据日
            src = np.where(i >= 0, p[np.clip(i, 0, None)], -1)
            ok = (src >= 0) & (days - src <= int(lookback))
            u, cnt = np.unique(src[ok], return_counts=True)
            out = (u, cnt.astype("float64"))
        self._cache[key] = out
        return out


def _emit_dates(load_cf: pd.DataFrame, ctx: RunContext) -> Dict[str, List[str]]:
    """行存在规则：与 core 一致 —— 景区在近 365 日（取数区间内）有过评论，当天就出行。"""
    out: Dict[str, List[str]] = {}
    if load_cf.empty:
        return out
    days = load_cf.groupby("scenic_spot_code")["travel_date"].agg(lambda s: sorted(set(s)))
    for sc, ds in days.items():
        arr = np.array(ds)
        keep = []
        for t in ctx.output_dates:
            lo = max(ctx.load_start, (pd.to_datetime(t, format=D.DATE_FMT)
                                      - pd.Timedelta(days=D.CORE_ROW_WINDOW - 1)
                                      ).strftime(D.DATE_FMT))
            if ((arr >= lo) & (arr <= t)).any():
                keep.append(t)
        if keep:
            out[sc] = keep
    return out


# ══════════════════════════════════════════════════════════════════════
def build_macro(comment_facts: pd.DataFrame, dim_facts: pd.DataFrame,
                keyword_facts: pd.DataFrame, ctx: RunContext) -> pd.DataFrame:
    """评论 / 维度 / 关键词明细 → macro 大盘表。

    入参的三份明细要覆盖 macro_data_ranges(ctx) 的全部日期（含上期与去年同期），
    pipeline 负责按这个范围抽数。
    """
    if comment_facts.empty:
        return pd.DataFrame(columns=COLUMNS)

    specs = ctx.macro_granularities
    formula = ctx.settings.score_formula
    weights = ctx.score_weights
    top_n = int(getattr(ctx.settings, "macro_wordcloud_top_n", D.MACRO_WORDCLOUD_TOP_N))

    cf = comment_facts
    load_cf = cf[cf["travel_date"].between(ctx.load_start, ctx.load_end)]
    codes = platform_scope(ctx, cf["platform_code"].drop_duplicates().tolist())
    emit = _emit_dates(load_cf, ctx)
    if not emit:
        return pd.DataFrame(columns=COLUMNS)
    base = to_dt(cf["travel_date"]).min()

    # ---- 四份区间求和器：评论情感 / 地域 / 维度 / 词 ----
    c = cf.assign(_sent=cf["sentiment"].map(_SENT_IDX).astype("int64"))
    comments = _RangeSum(_daily(c, "_sent", base), "_sent", len(_SENT_ORDER))

    if "region" not in cf.columns:
        cf = cf.assign(region=D.REGION_UNKNOWN)
    # 没地域的评论也要算进热力图（记「未知」），heat 合计才等于 comment_total
    cf = cf.assign(region=cf["region"].fillna("").replace("", D.REGION_UNKNOWN))
    regions = sorted(cf["region"].unique())
    r_idx = {r: i for i, r in enumerate(regions)}
    rc = cf[cf["region"].isin(r_idx)]
    rc = rc.assign(_reg=rc["region"].map(r_idx))
    region_sum = _RangeSum(_daily(rc, "_reg", base), "_reg", len(regions))

    extra = sorted(set(dim_facts["dimension_level1"]) - set(D.L1_DIMENSIONS)) \
        if not dim_facts.empty else []
    dims = list(D.L1_DIMENSIONS) + extra
    d_idx = {d: i for i, d in enumerate(dims)}
    dc = dim_facts.assign(_dc=dim_facts["dimension_level1"].map(d_idx) * len(_SENT_ORDER)
                          + dim_facts["dim_sentiment"].map(_SENT_IDX)) \
        if not dim_facts.empty else dim_facts.assign(_dc=[])
    dim_sum = _RangeSum(_daily(dc, "_dc", base), "_dc", len(dims) * len(_SENT_ORDER))

    kw = keyword_facts
    if not kw.empty:
        pairs = kw[["emotion_word", "sentiment"]].drop_duplicates().reset_index(drop=True)
        w_words = pairs["emotion_word"].to_numpy()
        w_sents = pairs["sentiment"].to_numpy(dtype="int64")
        pairs["_wid"] = np.arange(len(pairs))
        kw = kw.merge(pairs, on=["emotion_word", "sentiment"], how="left")
    else:
        w_words, w_sents = np.array([], dtype=object), np.array([], dtype="int64")
        kw = kw.assign(_wid=[])
    word_sum = _RangeSum(_daily(kw, "_wid", base), "_wid", len(w_words))
    # 近期热度按「词」算（不分情感）：同一个词的各情感项加在一起
    w_code = pd.factorize(pd.Series(w_words, dtype=object))[0] if len(w_words) else \
        np.array([], dtype="int64")
    heat_days = int(D.MACRO_WORDCLOUD_HEAT_DAYS)

    names = (cf[cf["scenic_spot_name"].astype(str).str.len() > 0]
             .groupby("scenic_spot_code")["scenic_spot_name"].first())

    def _day(ts: pd.Timestamp) -> int:
        return int((ts - base).days)

    range_cache: Dict[tuple, dict] = {}

    def _ranges(spec_i: int, t: str) -> dict:
        key = (spec_i, t)
        if key not in range_cache:
            r = D.macro_period_ranges(specs[spec_i], t)
            range_cache[key] = {k: (_day(lo), _day(hi)) for k, (lo, hi) in r.items()}
        return range_cache[key]

    plan = _FillPlan(comments)
    lookbacks = [_lookback(ctx, spec) for spec in specs]

    rows: List[dict] = []
    for sc, dates in emit.items():
        for t in dates:
            # 词云并列排序用的近期热度：该景区近 heat_days 天的真实词频（不补齐、全渠道）
            hi_t = _day(pd.to_datetime(t, format=D.DATE_FMT))
            raw = word_sum.sum(sc, [(ch, hi_t - heat_days + 1, hi_t) for ch in codes])
            heat = (np.bincount(w_code, weights=raw)[w_code] if len(w_code)
                    else raw)
            for si, spec in enumerate(specs):
                r = _ranges(si, t)
                lb = lookbacks[si]
                per_ch = {}
                for ch in codes:
                    # 本期 / 上期 / 去年同期：都在补齐后的每日明细上求和（区间不挪）
                    src = {k: plan.sources(sc, ch, lo, hi, lb) for k, (lo, hi) in r.items()}
                    per_ch[ch] = {
                        "cur": comments.weighted(sc, ch, *src["cur"]),
                        "prev": comments.weighted(sc, ch, *src["prev"]),
                        "yoy": comments.weighted(sc, ch, *src["yoy"]),
                        "dim_cur": dim_sum.weighted(sc, ch, *src["cur"]),
                        "dim_prev": dim_sum.weighted(sc, ch, *src["prev"]),
                        "words": word_sum.weighted(sc, ch, *src["cur"]),
                        "regions": region_sum.weighted(sc, ch, *src["cur"]),
                    }
                # all 行 = 各渠道（各自补齐后的）区间结果相加
                total = {k: sum(v[k] for v in per_ch.values()) for k in per_ch[codes[0]]}
                for ch, agg in [(D.MACRO_CHANNEL_ALL, total)] + list(per_ch.items()):
                    row = {"scenic_id": sc, "scenic_name": names.get(sc, ""),
                           "channel": ch,
                           "channel_name": (D.MACRO_CHANNEL_ALL_NAME
                                            if ch == D.MACRO_CHANNEL_ALL
                                            else D.CHANNEL_NAME.get(ch, ch)),
                           "time_granularity": spec["name"], "travel_date": t,
                           "is_all": ch == D.MACRO_CHANNEL_ALL,
                           "all_total": float(total["cur"].sum())}
                    for period in ("cur", "prev", "yoy"):
                        for k, v in zip(_CNT_KEYS, agg[period]):
                            row[f"{period}_{k}"] = float(v)
                    row["dimension_breakdown"] = D.macro_dimension_breakdown(
                        dims, agg["dim_cur"], agg["dim_prev"], formula=formula, weights=weights)
                    row["wordcloud_map"] = D.macro_wordcloud(
                        w_words, w_sents, agg["words"], top_n=top_n, heat=heat)
                    row["period_comment_heatmap"] = D.macro_heatmap(regions, agg["regions"])
                    rows.append(row)

    df = pd.DataFrame(rows)
    out = D.macro_metrics(df, formula=formula, weights=weights)
    for col in ("scenic_id", "scenic_name", "channel", "channel_name", "time_granularity",
                "dimension_breakdown", "wordcloud_map", "period_comment_heatmap"):
        out[col] = df[col]
    out["travel_date"] = df["travel_date"].astype(int)
    out["publish_time"] = ctx.publish_time(df["travel_date"])
    out["etl_time"] = ctx.etl_time
    return out[COLUMNS].reset_index(drop=True)
