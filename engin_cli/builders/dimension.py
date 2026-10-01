# -*- coding: utf-8 -*-
"""ads_trf_social_opinion_comment_dimension_score_di —— 维度标签分数表。

只做机械搬运：三个层级各自分组 → 铺网格 → 滚动(含整窗前移回补) → 调 domain 算得分。
三层语义与公式见 `metric_calc_domain.dimension_metrics`。

行存在规则（domain §1.7）
------------------------
维度路径在最长窗口（365 日）内有过提及，目标区间里每天都出一行 ——
当天没人提到「停车场」不代表它近 30 日的分数没有意义，
折线图也不该因为某天没有提及就断掉。维度路径数量有限（几十条），全量铺开没有压力。

缺数回补（domain §1.8）
----------------------
每条维度路径、每个窗口各自独立往前挪，挪到有提及的那一个窗口为止。
"""

from __future__ import annotations

import pandas as pd

from .. import metric_calc_domain as D
from ..context import RunContext
from ..windows import dense_grid, rolling_windows

COLUMNS = D.DIMENSION_COLUMNS
_VALS = D.DIM_VALUE_FIELDS


def build_dimension(dim_facts: pd.DataFrame, ctx: RunContext) -> pd.DataFrame:
    if dim_facts.empty:
        return pd.DataFrame(columns=COLUMNS)

    name_map = (dim_facts.groupby("scenic_spot_code", as_index=False)
                .agg(scenic_spot_name=("scenic_spot_name", "first")))

    frames = {}
    for lvl, keys in D.DIM_LEVEL_KEYS.items():
        daily = D.daily_dimension_facts(dim_facts, keys)
        # 铺网格要在回补之前：回补需要看到「这一天是 0」才会往前挪
        daily = dense_grid(daily, keys, "travel_date", _VALS,
                           start=ctx.load_start, end=ctx.load_end)
        win = rolling_windows(daily, keys, "travel_date", _VALS,
                              windows=D.DIMENSION_WINDOWS, with_prev=False,
                              output_dates=ctx.output_dates,
                              calendar=(ctx.load_start, ctx.load_end),
                              shift_lookback=ctx.backfill_lookback(D.DIMENSION_WINDOWS),
                              presence_col="mention_cnt")
        if lvl == 3:
            ctx.note_shift("dimension", win)
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

    out = D.dimension_metrics(df, formula=ctx.settings.score_formula)
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
