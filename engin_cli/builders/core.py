# -*- coding: utf-8 -*-
"""ads_trf_social_opinion_comment_core_di —— 社媒评论核心指标表。

本文件只做机械的事：**铺网格 → 滚动(含整窗前移回补) → 按景区汇总 → 调 domain 算指标**。
所有公式、字段命名、口径判断都在 `metric_calc_domain.py`，这里不重复定义。

行存在规则（domain §1.7）
------------------------
景区在最长窗口（365 日）内有过评论，目标区间里的**每一天都出一行** ——
否则「今天还没采到数据」会让整块看板空掉，哪怕近 30 日的数据好好地躺在那里。

缺数回补（domain §1.8 + §1.9）
-----------------------------
**回补的原子粒度是渠道，不是景区。** 缺的是「快手」这一个渠道，不是整个景区，
所以本文件吃的是渠道粒度的日事实，在渠道粒度上整窗前移，再按景区把各渠道相加。

    09-11  抖音10 小红书20 微博5 携程3 （无快手）
           ↓ 只有快手往前挪一天，补 09-10 的 3 条
    core.comment_count = 各渠道相加 = 41（不是 38）

如果在景区粒度上判断「09-11 有 38 条，不算缺数」，就会出现 core=38
而各渠道之和=41 的劈叉 —— 同一天两个总数，对不上账。按渠道汇总之后
`core.comment_count == Σ 各渠道 comment_cnt` 是恒等式，不是巧合。

因此 `comment_count` 的含义是「**各渠道各自补齐后的 1 日值之和**」，
不是「当天真实收到多少条」。这是业务方明确要求的口径。
"""

from __future__ import annotations

import pandas as pd

from .. import metric_calc_domain as D
from ..context import RunContext
from ..windows import dense_grid, rolling_windows
from .platform import platform_scope

COLUMNS = D.CORE_COLUMNS
_PKEY = ["scenic_spot_code", "platform_code"]
_VALUES = D.CORE_COUNT_FIELDS


def build_core(comment_facts: pd.DataFrame, ctx: RunContext) -> pd.DataFrame:
    # 渠道粒度的日事实 —— 回补要在这个粒度上做（domain §1.9）
    daily = D.daily_core_facts_by_platform(comment_facts)
    if daily.empty:
        return pd.DataFrame(columns=COLUMNS)

    scenics = daily["scenic_spot_code"].drop_duplicates()
    codes = platform_scope(ctx, daily["platform_code"].drop_duplicates().tolist())

    # 铺成 (景区 × 渠道 × 连续日历) 的网格，缺的填 0。
    # 必须在回补之前铺：回补要能看到「这一天是 0」才会往前挪。
    # 渠道口径与 platform 表**完全一致**（同一个 platform_scope），
    # 两张表的总数才对得上 —— 少一个渠道就是一处对不上账。
    keys = pd.MultiIndex.from_product([scenics, codes], names=_PKEY).to_frame(index=False)
    daily = dense_grid(daily, _PKEY, "travel_date", _VALUES, keys=keys,
                       start=ctx.load_start, end=ctx.load_end,
                       carry_cols=["scenic_spot_name"])

    win = rolling_windows(daily, _PKEY, "travel_date", _VALUES,
                          windows=D.CORE_WINDOWS, with_prev=True,
                          output_dates=ctx.output_dates,
                          calendar=(ctx.load_start, ctx.load_end),
                          shift_lookback=ctx.backfill_lookback(D.CORE_WINDOWS),
                          presence_col="comment_count")
    ctx.note_shift("core", win)

    # 按景区汇总各渠道**补齐后**的值。
    # raw_ 前缀（未回补的真实计数）也要一起加：行存在规则要看真实数据，
    # 否则「景区接入之前」的日期会因为回补而凭空出行。
    num = [c for c in win.columns
           if c not in _PKEY + ["travel_date"] and not str(c).startswith("shift_")]
    df = win.groupby(["scenic_spot_code", "travel_date"], as_index=False)[num].sum()

    names = daily[daily["scenic_spot_name"].astype(str).str.len() > 0].groupby(
        "scenic_spot_code", as_index=False).agg(scenic_spot_name=("scenic_spot_name", "first"))
    df = df.merge(names, on="scenic_spot_code", how="left")
    df["scenic_spot_name"] = df["scenic_spot_name"].fillna("")

    # 行存在规则：最长窗口内有过数据才出行 —— 景区接入之前的日期不会凭空多出空行
    df = df[D.has_window_data(df, "comment_count", D.CORE_ROW_WINDOW)]
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)

    out = D.core_metrics(df, formula=ctx.settings.score_formula,
                         weights=ctx.score_weights)
    out["scenic_spot_name"] = df["scenic_spot_name"]
    out["scenic_spot_code"] = df["scenic_spot_code"]
    out["travel_date"] = df["travel_date"].astype(int)
    out["publish_time"] = ctx.publish_time(df["travel_date"])
    out["etl_time"] = ctx.etl_time
    return out[COLUMNS].sort_values(["scenic_spot_code", "travel_date"]).reset_index(drop=True)
