# -*- coding: utf-8 -*-
"""ads_trf_social_opinion_comment_platform_di —— 平台评论分布及排行表。

只做机械搬运：铺网格 → 补齐每日明细并滚动 → 汇总全平台 → 调 domain 算指标。
公式与字段命名见 `metric_calc_domain.platform_metrics`。

行存在规则（domain §1.7）
------------------------
**配置的渠道每天都出一行**，当天没有评论也出。

缺数回补（domain §1.8）：先补明细，再算指标
------------------------------------------
每个渠道各自判断：当天没评论 → 复制往前最近一个有评论那天的明细（窗口 N 最多找 L_N 天）。
快手 09-10 有 3 条、09-11~09-13 没有 → 这三天各算 3 条；抖音当天有数据就不补。

「全平台」口径（total_ 前缀）= 各渠道补齐后的值相加，
所以 total = Σ plat、占比合计 = 1、core 总数 = Σ 各渠道 这几条恒等式都硬成立。
"""

from __future__ import annotations

from typing import List, Sequence

import pandas as pd

from .. import metric_calc_domain as D
from ..context import RunContext
from ..windows import dense_grid, rolling_windows

COLUMNS = D.PLATFORM_COLUMNS
_PKEY = ["scenic_spot_code", "platform_code"]
_SKEY = ["scenic_spot_code"]
_PVALS = ["plat_cnt", "plat_pos"]        # 平台口径


def platform_scope(ctx: RunContext, seen: Sequence[str]) -> List[str]:
    """本次跑批要出行的渠道全集 = 配置的渠道 ∪ 数据里实际出现的渠道。

    配置里没写但数据里有的渠道也保留 —— 采集侧新接一个平台时，
    不应该因为配置忘了改就把真实数据丢掉。
    """
    configured = getattr(ctx.settings, "platform_codes", None) or D.PLATFORM_CODES
    out = list(dict.fromkeys([str(c).strip().lower() for c in configured if str(c).strip()]))
    for c in seen:
        if c not in out:
            out.append(c)
    return out


def build_platform(comment_facts: pd.DataFrame, ctx: RunContext) -> pd.DataFrame:
    pdaily = D.daily_platform_facts(comment_facts)
    if pdaily.empty:
        return pd.DataFrame(columns=COLUMNS)

    scenics = pdaily["scenic_spot_code"].drop_duplicates()
    codes = platform_scope(ctx, pdaily["platform_code"].drop_duplicates().tolist())

    # 铺成 (景区 × 渠道 × 连续日历) 的网格，缺的填 0。
    # 从 fill_start 开始铺：取数区间之前那一段只当补齐的来源。
    keys = pd.MultiIndex.from_product([scenics, codes], names=_PKEY).to_frame(index=False)
    pdaily = dense_grid(pdaily, _PKEY, "travel_date", _PVALS, keys=keys,
                        start=ctx.fill_start, end=ctx.load_end,
                        carry_cols=["scenic_spot_name", "platform_name"])
    # 配置了但从来没有数据的渠道，名称从渠道字典补，别留空
    blank = pdaily["platform_name"].astype(str).str.len() == 0
    pdaily.loc[blank, "platform_name"] = pdaily.loc[blank, "platform_code"].map(
        D.CHANNEL_NAME).fillna(pdaily.loc[blank, "platform_code"])
    sname = (pdaily[pdaily["scenic_spot_name"].astype(str).str.len() > 0]
             .groupby("scenic_spot_code", as_index=False)
             .agg(_sname=("scenic_spot_name", "first")))

    win = rolling_windows(pdaily, _PKEY, "travel_date", _PVALS,
                          windows=D.PLATFORM_WINDOWS, with_prev=True,
                          output_dates=ctx.output_dates,
                          calendar=(ctx.load_start, ctx.load_end),
                          fill_lookback=ctx.backfill_lookback(D.PLATFORM_WINDOWS),
                          fill_by=_PKEY, fill_from=ctx.fill_start,
                          presence_col="plat_cnt")

    # 「全平台」= 各渠道**补齐后**的值相加。
    # 不能用景区粒度另算一份：各渠道各自补过，分母跟分子不同源的话，
    # total 就不等于 Σ plat，占比合计也不再是 1。
    agg_cols = {}
    for n in D.PLATFORM_WINDOWS:
        agg_cols[f"total_cnt_{n}d"] = (f"plat_cnt_{n}d", "sum")
        agg_cols[f"total_pos_{n}d"] = (f"plat_pos_{n}d", "sum")
        agg_cols[f"total_pos_prev_{n}d"] = (f"plat_pos_prev_{n}d", "sum")
        if f"raw_plat_cnt_{n}d" in win.columns:
            # 补齐前的真实计数：行存在规则看它，与 core 同一口径
            agg_cols[f"raw_total_cnt_{n}d"] = (f"raw_plat_cnt_{n}d", "sum")
    totals = win.groupby(_SKEY + ["travel_date"], as_index=False).agg(**agg_cols)
    totals["day_total_cnt"] = totals["total_cnt_1d"]

    df = (win.merge(totals, on=_SKEY + ["travel_date"], how="left")
          .merge(pdaily[_PKEY + ["platform_name"]].drop_duplicates(_PKEY),
                 on=_PKEY, how="left")
          .merge(sname, on="scenic_spot_code", how="left"))
    df["scenic_spot_name"] = df["_sname"].fillna("")

    # 行存在规则：景区在最长窗口内有数据的日期，配置的渠道全部出行（含全 0 行）
    df = df[D.has_window_data(df, "total_cnt", D.CORE_ROW_WINDOW)]
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)

    out = D.platform_metrics(df)
    out["scenic_spot_code"] = df["scenic_spot_code"]
    out["scenic_spot_name"] = df["scenic_spot_name"]
    out["platform_name"] = df["platform_name"]
    out["platform_code"] = df["platform_code"]
    out["travel_date"] = df["travel_date"].astype(int)
    out["publish_time"] = ctx.publish_time(df["travel_date"])
    out["etl_time"] = ctx.etl_time
    return out[COLUMNS].sort_values(
        ["scenic_spot_code", "travel_date", "platform_code"]).reset_index(drop=True)
