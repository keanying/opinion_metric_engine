# -*- coding: utf-8 -*-
"""ads_trf_social_opinion_comment_content_di —— 内容分析表（关键词云 + 负面突增词）。

只做机械搬运：词的日频次与归类 → 滚动 → 接 core 的分母 → 调 domain 算指标 → 排名。
词口径、突增量为何不落表，见 `metric_calc_domain` 第 3 章与 `content_metrics`。

行存在规则（domain §1.7）
------------------------
词在**近 CONTENT_ROW_WINDOW（默认 30）日**内出现过，目标区间里每天都出一行，
当天没出现就是 `emotion_value = 0`，但 `emotion_value_30d` 照旧 ——
否则「昨天刷屏的词今天没人提」会让它从今天的近 30 日词云里整个消失。

只铺 30 天而不是 365 天：词的数量没有上限，「一年内所有词 × 每天一行」
会让这张表比其他三张加起来还大几个量级。30 天正好覆盖看板最长的周期（本月）。

这里不需要显式铺网格：`rolling_windows` 内部本来就会把面板补成
(词 × 连续日历)，滚完的结果天然包含「当天没出现」的行，过滤规则在最后一步生效。

缺数回补（domain §1.8）
----------------------
每个词、每个窗口各自独立往前挪。分母（景区总评论数）也走同一套回补，
否则分子挪了分母没挪，热度占比会算歪。
"""

from __future__ import annotations

import pandas as pd

from .. import metric_calc_domain as D
from ..context import RunContext
from ..windows import dense_grid, rolling_windows

COLUMNS = D.CONTENT_COLUMNS
_KEY = ["scenic_spot_code", "emotion_word"]


def build_content(keyword_facts: pd.DataFrame, core_daily: pd.DataFrame,
                  ctx: RunContext) -> pd.DataFrame:
    """keyword_facts（词粒度明细） + core_daily（景区日评论数） → 内容表。"""
    if keyword_facts.empty:
        return pd.DataFrame(columns=COLUMNS)

    daily = D.daily_keyword_facts(keyword_facts)
    name_map = (keyword_facts.groupby("scenic_spot_code", as_index=False)
                .agg(scenic_spot_name=("scenic_spot_name", "first")))

    # calendar 必须显式给：目标区间最后几天一个词都没出现时，
    # 不给日历范围的话这几天根本不会进面板，补零行也就无从谈起。
    win = rolling_windows(daily, _KEY, "travel_date", ["word_cnt"],
                          windows=D.CONTENT_WINDOWS, with_prev=False,
                          output_dates=ctx.output_dates,
                          calendar=(ctx.load_start, ctx.load_end),
                          shift_lookback=ctx.backfill_lookback(D.CONTENT_WINDOWS),
                          presence_col="word_cnt")
    ctx.note_shift("content", win)

    # 行存在规则先过滤，再去补类型和分母 —— 先把量降下来，后面两步都便宜
    row_window = int(getattr(ctx.settings, "content_row_window", D.CONTENT_ROW_WINDOW))
    df = win[D.has_window_data(win, "word_cnt", row_window)].copy()
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)

    # 情感归属：当天出现过就用当天的归类，没出现就沿用最近一次出现时的归类
    df["emotion_type"] = D.resolve_word_emotion_type(df, daily, _KEY, "travel_date")

    # 分母：core 表口径的窗口总评论数（两表分母必须同源，否则热度占比对不上账）。
    # 分母要先铺网格：景区当天一条评论都没有时 core_daily 里没有这一行，
    # 不铺的话分母取到 NaN，热度占比被兜底成 0 —— 那正是这次要修的毛病。
    denom_daily = dense_grid(core_daily, ["scenic_spot_code"], "travel_date",
                             ["comment_count"], start=ctx.load_start, end=ctx.load_end)
    denom = rolling_windows(denom_daily, ["scenic_spot_code"], "travel_date",
                            ["comment_count"], windows=D.CONTENT_WINDOWS,
                            with_prev=False, output_dates=ctx.output_dates,
                            calendar=(ctx.load_start, ctx.load_end),
                            shift_lookback=ctx.backfill_lookback(D.CONTENT_WINDOWS),
                            presence_col="comment_count")
    df = (df.merge(denom, on=["scenic_spot_code", "travel_date"], how="left")
          .merge(name_map, on="scenic_spot_code", how="left"))

    out = D.content_metrics(df)
    out["scenic_spot_code"] = df["scenic_spot_code"]
    out["scenic_spot_name"] = df["scenic_spot_name"]
    out["emotion_type"] = df["emotion_type"]
    out["emotion_word"] = df["emotion_word"]
    out["emotion_rank"] = D.content_rank(
        out[D.CONTENT_VALUE[1]],
        [df["scenic_spot_code"], df["travel_date"], df["emotion_type"]])
    out["travel_date"] = df["travel_date"].astype(int)
    out["publish_time"] = ctx.publish_time(df["travel_date"])
    out["etl_time"] = ctx.etl_time

    top_n = int(getattr(ctx.settings, "content_top_words", 0) or 0)
    if top_n > 0:
        out = out[out["emotion_rank"] <= top_n]

    return out[COLUMNS].sort_values(
        ["scenic_spot_code", "travel_date", "emotion_type", "emotion_rank"]
    ).reset_index(drop=True)
