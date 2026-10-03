# -*- coding: utf-8 -*-
"""ads_trf_social_opinion_comment_dimension_score_di —— 维度标签分数表。

只做机械搬运：三个层级各自分组 → 补齐每日明细并滚动 → 调 domain 算得分。
三层语义与公式见 `metric_calc_domain.dimension_metrics`。

行存在规则（domain §1.7）
------------------------
维度路径在最长窗口（365 日）内有过提及，目标区间里每天都出一行 ——
当天没人提到「停车场」不代表它近 30 日的分数没有意义，
折线图也不该因为某天没有提及就断掉。维度路径数量有限（几十条），全量铺开没有压力。

缺数回补（domain §1.8 / §1.9）：先补明细，再算指标
-------------------------------------------------
补齐看的是**渠道当天有没有评论**：某渠道当天没评论 → 复制往前最近一天那个渠道的
全部维度提及（窗口 N 最多找 L_N 天）；当天有评论但没提到某个维度，就是真的没有。
所以这里按「维度路径 × 渠道」补齐、滚动，再把渠道加掉。
"""

from __future__ import annotations

import pandas as pd

from .. import metric_calc_domain as D
from ..context import RunContext
from ..windows import rolling_windows

COLUMNS = D.DIMENSION_COLUMNS
_VALS = D.DIM_VALUE_FIELDS
_FILL_BY = ["scenic_spot_code", "platform_code"]


def build_dimension(dim_facts: pd.DataFrame, ctx: RunContext,
                    presence: pd.DataFrame | None = None) -> pd.DataFrame:
    """维度明细 → 维度分数表。

    入参：dim_facts 维度粒度明细（含 platform_code）；
          presence 渠道当天有没有评论（domain.daily_channel_presence），
          不传就用维度提及本身判断（只在测试里这么用）
    """
    if dim_facts.empty:
        return pd.DataFrame(columns=COLUMNS)

    name_map = (dim_facts.groupby("scenic_spot_code", as_index=False)
                .agg(scenic_spot_name=("scenic_spot_name", "first")))
    lookback = ctx.backfill_lookback(D.DIMENSION_WINDOWS)
    frames = {}
    for lvl, keys in D.DIM_LEVEL_KEYS.items():
        win = dimension_windows(dim_facts, keys, ctx.output_dates,
                                (ctx.load_start, ctx.load_end), lookback,
                                ctx.fill_start, presence)
        if lvl == 3:   # 行存在规则只在最细粒度上判一次，二级/一级跟着三级走
            win = win[D.has_window_data(win, "mention_cnt", D.DIMENSION_ROW_WINDOW)]
        frames[lvl] = win.rename(columns={c: f"L{lvl}_{c}" for c in win.columns
                                          if c not in keys + ["travel_date"]})

    df = (frames[3]
          .merge(frames[2], on=D.DIM_KEYS_L2 + ["travel_date"], how="left")
          .merge(frames[1], on=D.DIM_KEYS_L1 + ["travel_date"], how="left")
          .merge(name_map, on="scenic_spot_code", how="left"))
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)

    out = D.dimension_metrics(df, formula=ctx.settings.score_formula,
                              weights=ctx.score_weights)
    out["scenic_spot_code"] = df["scenic_spot_code"]
    out["scenic_spot_name"] = df["scenic_spot_name"]
    out["dimension_level1"] = df["dimension_level1"]
    out["dimension_level2"] = df["dimension_level2"]
    out["dimension_level3"] = df["dimension_level3"]
    out["travel_date"] = df["travel_date"].astype(int)
    out["publish_time"] = ctx.publish_time(df["travel_date"])
    out["etl_time"] = ctx.etl_time
    return out[COLUMNS].sort_values(
        ["scenic_spot_code", "travel_date", "dimension_level1",
         "dimension_level2", "dimension_level3"]).reset_index(drop=True)


def dimension_windows(dim_facts: pd.DataFrame, keys, output_dates, calendar,
                      lookback: dict, fill_from: str,
                      presence: pd.DataFrame | None = None) -> pd.DataFrame:
    """某一层维度键的多窗口计数（补齐明细后滚动，再把渠道加掉）。repair 也用它，保证同源。

    入参：keys 该层的分组键；calendar (起, 止)；lookback {窗口: 最多往前找几天}（空 = 不补）；
          fill_from 补齐来源最早日期；presence 渠道当天有没有评论
    出参：keys + travel_date + mention_*_<N>d（+ 补齐时 raw_mention_cnt_<N>d）
    """
    by_ch = list(keys) + ["platform_code"]
    facts = dim_facts if "platform_code" in dim_facts.columns \
        else dim_facts.assign(platform_code="")
    daily = D.daily_dimension_facts(facts, by_ch)
    win = rolling_windows(daily, by_ch, "travel_date", _VALS,
                          windows=D.DIMENSION_WINDOWS, with_prev=False,
                          output_dates=output_dates, calendar=calendar,
                          fill_lookback=lookback, fill_by=_FILL_BY,
                          presence=presence, fill_from=fill_from,
                          presence_col="mention_cnt")
    num = [c for c in win.columns if c not in by_ch + ["travel_date"]]
    return win.groupby(list(keys) + ["travel_date"], as_index=False)[num].sum()
