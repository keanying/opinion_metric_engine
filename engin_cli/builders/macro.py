# -*- coding: utf-8 -*-
"""ads_trf_social_opinion_macro_gran_metric_di —— 舆情大盘不同周期粒度 KPI 指标表（需求 2.0）。

一行 = 景区 × 日期 × 周期粒度 × 渠道（配置的渠道 + all）。看板「总览」页的
周期页签 + 平台下拉选中的就是这一行，所以每个组合**恰好一行**，没数据也出（全 0）。

本文件只做搬运：周期 → 日期区间（domain §1.10 / 第 10 章）→ 区间求和 → 调 domain 算指标。

为什么不用 rolling_windows 直接滚
--------------------------------
这张表的周期不全是「近 N 日」：this_week / this_month / this_quarter 是日历周期，上期是「上周期的同样几天」，
同比要去年同期 —— 都不是固定宽度的滑窗。所以这里把每个周期翻译成**日期区间**，
再对日粒度事实做区间求和（按日期排好序，searchsorted 切片 + bincount）。

缺数回补（domain §1.10，与 core/platform 一致）
---------------------------------------------
rolling 周期在渠道粒度上整窗前移。挪几天**不在这里另算**：直接调 windows.rolling_windows
（全引擎唯一的回补实现），在与 core/platform 相同的网格、日历、上限上拿 `shift_<N>d`，
再把本期/上期区间一起往前挪那么多天。于是：

    macro latest_7d 某渠道 comment_total == platform.comment_cnt_7d
    macro today 的 all 行 comment_total   == core.comment_count

挪了窗口时，词云 / 维度 / 热力也取挪后的那个窗口 —— 同一行的数字来自同一段日期。
all 行 = 各渠道（各自挪过的）区间结果相加。日历周期与同比不回补。
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

from .. import metric_calc_domain as D
from ..context import RunContext
from ..windows import dense_grid, rolling_windows, to_dt
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
    """本表需要的源数据日期段（yyyyMMdd 闭区间，已合并），含 [load_start, load_end]。

    上期可能早于 load_start（如 this_quarter 的上期），同比要去年同期 —— 这两段都要额外取。
    rolling 周期按最大前移天数放宽，挪到上限也不会取到没抽的日期。
    """
    spans = [(pd.to_datetime(ctx.load_start, format=D.DATE_FMT),
              pd.to_datetime(ctx.load_end, format=D.DATE_FMT))]
    for spec in ctx.macro_granularities:
        lb = 0
        if spec["type"] == D.MACRO_PERIOD_ROLLING:
            lb = ctx.backfill_lookback([spec["days"]]).get(spec["days"], 0)
        for t in ctx.output_dates:
            near = D.macro_period_ranges(spec, t, shift=0)
            far = D.macro_period_ranges(spec, t, shift=lb)
            spans.append((min(far["prev"][0], far["cur"][0]), near["cur"][1]))
            spans.append((far["yoy"][0], near["yoy"][1]))
    spans.sort()
    merged = [list(spans[0])]
    for lo, hi in spans[1:]:
        if lo <= merged[-1][1] + pd.Timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo.strftime(D.DATE_FMT), hi.strftime(D.DATE_FMT)) for lo, hi in merged]


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
        if df.empty:
            return
        df = df.sort_values(["scenic_spot_code", "platform_code", "_day"])
        for key, g in df.groupby(["scenic_spot_code", "platform_code"], sort=False):
            self.groups[key] = (g["_day"].to_numpy(dtype="int64"),
                                g[cat_col].to_numpy(dtype="int64"),
                                g["_n"].to_numpy(dtype="float64"))

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
# rolling 周期的整窗前移天数：复用 windows.rolling_windows（与 core/platform 同源）
# ══════════════════════════════════════════════════════════════════════
def _rolling_shifts(load_cf: pd.DataFrame, codes: List[str], specs: List[dict],
                    ctx: RunContext) -> Dict[tuple, int]:
    """{(景区, 渠道, 锚点 yyyyMMdd, 窗口天数): 前移天数}，没回补的不在字典里。"""
    days = sorted({s["days"] for s in specs if s["type"] == D.MACRO_PERIOD_ROLLING})
    lb = ctx.backfill_lookback(days)
    if not lb or load_cf.empty:
        return {}
    anchors = set()
    for s in specs:
        if s["type"] != D.MACRO_PERIOD_ROLLING:
            continue
        for t in ctx.output_dates:
            a = (pd.to_datetime(t, format=D.DATE_FMT)
                 - pd.Timedelta(days=s["offset"])).strftime(D.DATE_FMT)
            if ctx.load_start <= a <= ctx.load_end:
                anchors.add(a)
    if not anchors:
        return {}

    # 网格、日历、presence 列与 core 完全一致 —— 挪几天才能跟 core/platform 对上账
    daily = D.daily_core_facts_by_platform(load_cf)
    keys = pd.MultiIndex.from_product(
        [daily["scenic_spot_code"].drop_duplicates(), codes], names=_PKEY).to_frame(index=False)
    daily = dense_grid(daily, _PKEY, "travel_date", ["comment_count"], keys=keys,
                       start=ctx.load_start, end=ctx.load_end)
    win = rolling_windows(daily, _PKEY, "travel_date", ["comment_count"],
                          windows=list(lb), with_prev=False,
                          output_dates=sorted(anchors),
                          calendar=(ctx.load_start, ctx.load_end),
                          shift_lookback=lb, presence_col="comment_count")
    ctx.note_shift("macro", win)

    out: Dict[tuple, int] = {}
    for n in lb:
        col = f"shift_{n}d"
        if col not in win.columns:
            continue
        moved = win[pd.to_numeric(win[col], errors="coerce").fillna(0) > 0]
        for sc, ch, d, s in zip(moved["scenic_spot_code"], moved["platform_code"],
                                moved["travel_date"], moved[col]):
            out[(sc, ch, d, n)] = int(s)
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
    shifts = _rolling_shifts(load_cf, codes, specs, ctx)
    base = to_dt(cf["travel_date"]).min()

    # ---- 四份区间求和器：评论情感 / 地域 / 维度 / 词 ----
    c = cf.assign(_sent=cf["sentiment"].map(_SENT_IDX).astype("int64"))
    comments = _RangeSum(_daily(c, "_sent", base), "_sent", len(_SENT_ORDER))

    if "region" not in cf.columns:
        cf = cf.assign(region="")
    regions = sorted(r for r in cf["region"].dropna().unique() if r)
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

    names = (cf[cf["scenic_spot_name"].astype(str).str.len() > 0]
             .groupby("scenic_spot_code")["scenic_spot_name"].first())

    def _day(ts: pd.Timestamp) -> int:
        return int((ts - base).days)

    range_cache: Dict[tuple, dict] = {}

    def _ranges(spec_i: int, t: str, s: int) -> dict:
        key = (spec_i, t, s)
        if key not in range_cache:
            r = D.macro_period_ranges(specs[spec_i], t, shift=s)
            range_cache[key] = {k: (_day(lo), _day(hi)) for k, (lo, hi) in r.items()}
        return range_cache[key]

    rows: List[dict] = []
    for sc, dates in emit.items():
        for t in dates:
            for si, spec in enumerate(specs):
                anchor = None
                if spec["type"] == D.MACRO_PERIOD_ROLLING:
                    anchor = (pd.to_datetime(t, format=D.DATE_FMT)
                              - pd.Timedelta(days=spec["offset"])).strftime(D.DATE_FMT)
                per_ch = {}
                for ch in codes:
                    s = shifts.get((sc, ch, anchor, spec["days"]), 0) if anchor else 0
                    r = _ranges(si, t, s)
                    part = {k: [(ch, lo, hi)] for k, (lo, hi) in r.items()}
                    per_ch[ch] = {
                        "cur": comments.sum(sc, part["cur"]),
                        "prev": comments.sum(sc, part["prev"]),
                        "yoy": comments.sum(sc, part["yoy"]),
                        "dim_cur": dim_sum.sum(sc, part["cur"]),
                        "dim_prev": dim_sum.sum(sc, part["prev"]),
                        "words": word_sum.sum(sc, part["cur"]),
                        "regions": region_sum.sum(sc, part["cur"]),
                    }
                # all 行 = 各渠道（各自挪过的）区间结果相加
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
                        w_words, w_sents, agg["words"], top_n=top_n)
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
