# -*- coding: utf-8 -*-
"""口径自检。

每次跑批都跑，靠数字而不是靠感觉。任何一项不通过都说明**计算逻辑**有问题，
不是数据问题 —— 这些断言全部只依赖恒等关系，与具体数值无关。

退出码约定：0 全通过 / 1 有未通过项。可直接接 CI。
"""

from __future__ import annotations

import json
from typing import List

import numpy as np
import pandas as pd

from .metric_calc_domain import (BACKFILL_MODE_FILL, CORE_SUFFIX, CORE_WINDOWS,
                                 DEFAULT_SCORE_FORMULA, MACRO_CHANNEL_ALL, MACRO_KEYS,
                                 MACRO_SENTIMENT_PREFIX, PLATFORM_WINDOWS, PLAT_POS,
                                 PLAT_TOT_POS, SCORE_FORMULA_V1, SCORE_MAX, SCORE_MIN,
                                 V1_SCORE_MAX, sentiment_score_from_counts)


def _fail(errs: List[str], cond: bool, msg: str):
    if not cond:
        errs.append(msg)


def validate_core(core: pd.DataFrame, formula: str = DEFAULT_SCORE_FORMULA,
                  weights=None) -> List[str]:
    errs: List[str] = []
    if core.empty:
        return ["core 表为空"]

    bad = core[core.positive_count + core.neutral_count + core.negative_count
               != core.comment_count]
    _fail(errs, bad.empty, f"core: 好+中+差 != 总数，{len(bad)} 行")

    for w in CORE_WINDOWS:
        sfx = CORE_SUFFIX[w]      # 字段后缀查 domain 的注册表，别在这里手写一份
        s = core[f"emotional_score{sfx}"]
        _fail(errs, bool(((s >= SCORE_MIN) & (s <= SCORE_MAX)).all()),
              f"core: emotional_score{sfx} 越界 [{SCORE_MIN:g},{SCORE_MAX:g}]")
        rs = core[f"positive_rate{sfx}"] + core[f"neutral_rate{sfx}"] + core[f"negative_rate{sfx}"]
        near1 = ((rs - 1.0).abs() <= 0.002) | (rs == 0)
        _fail(errs, bool(near1.all()),
              f"core: 好评率+中评率+差评率 != 1（{sfx or '1d'}），{int((~near1).sum())} 行")
        for c in (f"positive_rate{sfx}", f"neutral_rate{sfx}", f"negative_rate{sfx}"):
            v = core[c]
            _fail(errs, bool(((v >= 0) & (v <= 1)).all()), f"core: {c} 越界 [0,1]")

    _fail(errs, bool(core["daily_rating"].between(1, 5).all()),
          "core: daily_rating 越界 [1,5]")

    # 1 日档得分必须**严格等于**把表里存的计数代进公式的结果。
    # 补齐明细之后计数本身也是补齐后的值，分子分母同源，所以这里可以逐行对拍，
    # 不再需要宽松容差 —— 对不上就是公式接错或字段错位。
    calc = sentiment_score_from_counts(core.positive_count, core.neutral_count,
                                       core.negative_count, core.comment_count,
                                       formula=formula, weights=weights)
    diff = np.abs(core["emotional_score"].to_numpy() - calc)
    _fail(errs, bool(np.nanmax(diff) <= 0.001),
          f"core: emotional_score != 由表内计数算出的值（最大偏差 {np.nanmax(diff):.4f}）")

    dup = core.duplicated(subset=["scenic_spot_code", "travel_date"]).sum()
    _fail(errs, dup == 0, f"core: (景区,日期) 重复 {dup} 行")
    return errs


def validate_platform(platform: pd.DataFrame, core: pd.DataFrame,
                      codes: List[str] | None = None,
                      backfill_mode: str = BACKFILL_MODE_FILL) -> List[str]:
    errs: List[str] = []
    if platform.empty:
        return ["platform 表为空"]

    # 平台合计 == core 总数。**开不开回补都必须恒成立。**
    # 因为回补的原子粒度是渠道（domain §1.9）：core 吃的就是渠道粒度的事实，
    # 在渠道上挪完再按景区相加，两边同源。这条一旦破了，说明 core 和 platform
    # 用了不同的渠道范围、不同的日历或不同的补齐上限 —— 同一天两个总数，对不上账。
    agg = platform.groupby(["scenic_spot_code", "travel_date"], as_index=False)[
        "comment_cnt"].sum()
    m = agg.merge(core[["scenic_spot_code", "travel_date", "comment_count"]],
                  on=["scenic_spot_code", "travel_date"], how="inner")
    bad = m[m.comment_cnt != m.comment_count]
    _fail(errs, bad.empty,
          f"platform: 各平台评论数之和 != core 总数，{len(bad)} 行"
          + (f"（例如 {bad.iloc[0].scenic_spot_code}/{bad.iloc[0].travel_date}："
             f"平台合计 {int(bad.iloc[0].comment_cnt)} vs core "
             f"{int(bad.iloc[0].comment_count)}）" if not bad.empty else ""))

    # 平台占比合计 = 1。分母用的是「各渠道回补后的值之和」，所以这条依然硬成立；
    # 例外只有「该景区当期各渠道全是 0」的行（分母为 0，占比全 0）。
    denom = platform.groupby(["scenic_spot_code", "travel_date"])["comment_cnt"].sum()
    live = denom[denom > 0].index
    rate = (platform.set_index(["scenic_spot_code", "travel_date"]).loc[live]
            .groupby(level=[0, 1])["comment_rate"].sum())
    off = rate[(rate - 1.0).abs() > 0.01]
    _fail(errs, off.empty, f"platform: 平台评论占比合计 != 1，{len(off)} 天")

    for c in [c for c in platform.columns if "good_rate" in c and "wow" not in c] + \
             ["comment_rate", "positive_rate"]:
        v = platform[c]
        _fail(errs, bool(((v >= 0) & (v <= 1)).all()), f"platform: {c} 越界 [0,1]")

    dup = platform.duplicated(subset=["scenic_spot_code", "platform_code",
                                      "travel_date"]).sum()
    _fail(errs, dup == 0, f"platform: (景区,平台,日期) 重复 {dup} 行")

    # 「全平台」口径字段（total_ 前缀 + positive_*_count）在同一 (景区,日期) 内
    # 必须**只有一个值**，且等于各平台相加。
    # 这两条一起才能证明它算的确实是全平台，而不是某个平台的数被错抄成了全平台。
    for w in PLATFORM_WINDOWS:
        tot, plat = PLAT_TOT_POS[w], PLAT_POS[w]
        g = platform.groupby(["scenic_spot_code", "travel_date"])
        _fail(errs, bool((g[tot].nunique() <= 1).all()),
              f"platform: {tot} 是全平台口径，同一天各渠道行却出现了多个值")
        agg = g[plat].sum().round().astype("int64")
        ref = g[tot].first().round().astype("int64")
        off = int((agg != ref).sum())
        _fail(errs, off == 0,
              f"platform: {tot} != 各平台 {plat} 之和，{off} 个 (景区,日期) 对不上")

    # 行存在规则：配置的渠道在每个 (景区,日期) 上都必须有行，一个都不能少。
    # 「当天没评论就没有这个渠道的记录」正是这条断言要挡住的回归 ——
    # 看板查的是近 7/30/90 日指标，行没了就查不到。
    expected = set(codes or [])
    if expected:
        missing = []
        for (scenic, ds), g in platform.groupby(["scenic_spot_code", "travel_date"]):
            gap = expected - set(g["platform_code"])
            if gap:
                missing.append(f"{scenic}/{ds} 缺 {sorted(gap)}")
        _fail(errs, not missing,
              f"platform: {len(missing)} 个 (景区,日期) 缺配置渠道的行，"
              f"例如 {missing[0] if missing else ''}")
    return errs


def validate_dimension(dim: pd.DataFrame) -> List[str]:
    errs: List[str] = []
    if dim.empty:
        return ["dimension 表为空"]

    for c in [c for c in dim.columns if c.startswith("dimension_") and "score" in c]:
        v = dim[c]
        _fail(errs, bool(((v >= SCORE_MIN) & (v <= SCORE_MAX)).all()),
              f"dimension: {c} 越界 [{SCORE_MIN:g},{SCORE_MAX:g}]")

    # 层级口径自洽：同一 (景区,日期,一级维度) 下 dimension_1_score 必须唯一
    g = dim.groupby(["scenic_spot_code", "travel_date", "dimension_level1"]
                    )["dimension_1_score"].nunique()
    _fail(errs, bool((g <= 1).all()),
          f"dimension: 同一一级维度当日出现多个 dimension_1_score，{int((g > 1).sum())} 组")
    g2 = dim.groupby(["scenic_spot_code", "travel_date", "dimension_level1",
                      "dimension_level2"])["dimension_2_score"].nunique()
    _fail(errs, bool((g2 <= 1).all()),
          f"dimension: 同一二级维度当日出现多个 dimension_2_score，{int((g2 > 1).sum())} 组")

    dup = dim.duplicated(subset=["scenic_spot_code", "travel_date", "dimension_level1",
                                 "dimension_level2", "dimension_level3"]).sum()
    _fail(errs, dup == 0, f"dimension: 维度路径重复 {dup} 行")
    return errs


def validate_content(content: pd.DataFrame, core: pd.DataFrame) -> List[str]:
    errs: List[str] = []
    if content.empty:
        return ["content 表为空"]

    dup = content.duplicated(subset=["scenic_spot_code", "travel_date",
                                     "emotion_word"]).sum()
    _fail(errs, dup == 0,
          f"content: (景区,日期,词) 重复 {dup} 行 —— 会撞 DDL 唯一索引")

    _fail(errs, bool(content["emotion_type"].isin(["positive", "neutral", "negative"]).all()),
          "content: emotion_type 出现非法值")

    for c in [c for c in content.columns if c.startswith("emotion_rate")]:
        v = content[c]
        _fail(errs, bool((v >= 0).all()), f"content: {c} 出现负值")

    # 词频不该离谱地超过评论数（词和评论数同一份补齐后的明细，
    # 但一条评论可以命中多个词，所以放宽到 3 倍）：抓量级错误（比如同一条评论
    # 里同词被重复计数），而不是卡个位数的口径差。
    m = content.merge(core[["scenic_spot_code", "travel_date", "comment_count"]],
                      on=["scenic_spot_code", "travel_date"], how="left")
    bad = m[m.emotion_value > m.comment_count.fillna(0) * 3 + 3]
    _fail(errs, bad.empty,
          f"content: emotion_value 远超当日评论总数，{len(bad)} 行（同一条评论里同词重复计数？）")

    rank = content.groupby(["scenic_spot_code", "travel_date", "emotion_type"]
                           )["emotion_rank"].min()
    _fail(errs, bool((rank == 1).all()), "content: 存在缺少 rank=1 的分组")
    return errs


def validate_all(tables: dict) -> List[str]:
    """tables 额外可带 "platform_codes"：本次跑批配置的渠道全集，用于行完整性断言。"""
    core = tables.get("core", pd.DataFrame())
    formula = tables.get("score_formula") or DEFAULT_SCORE_FORMULA
    errs: List[str] = []
    errs += validate_core(core, formula, tables.get("score_weights"))
    errs += validate_platform(tables.get("platform", pd.DataFrame()), core,
                              tables.get("platform_codes"),
                              tables.get("backfill_mode", BACKFILL_MODE_FILL))
    errs += validate_dimension(tables.get("dimension", pd.DataFrame()))
    errs += validate_content(tables.get("content", pd.DataFrame()), core)
    if "macro" in tables:
        errs += validate_macro(tables["macro"], core, tables.get("platform_codes"),
                               tables.get("macro_granularities"), formula)
    return errs


def validate_macro(macro: pd.DataFrame, core: pd.DataFrame,
                   codes: List[str] | None = None,
                   granularities: List[str] | None = None,
                   formula: str = DEFAULT_SCORE_FORMULA) -> List[str]:
    """macro 大盘表（需求 2.0）的恒等关系。

    · 景区 × 日期 × 周期 × 渠道 唯一（撞唯一索引就写不进去）
    · 好 + 中 + 差 = 总评数；占比在 [0,100]；得分在值域内
    · all 行的各项计数 == 各渠道相加（与 core/platform 同一条规则，domain §1.9）
    · 各渠道 comment_rate 合计 = 100（all 行有评论时）
    · 行完整性：core 出了行的每个 (景区, 日期)，每个周期都要有 all + 配置的全部渠道
    · 三个 JSON 列都能解析
    """
    errs: List[str] = []
    if macro.empty:
        return ["macro 表为空"] if not core.empty else []

    dup = int(macro.duplicated(subset=MACRO_KEYS).sum())
    _fail(errs, dup == 0, f"macro: (景区,日期,周期,渠道) 重复 {dup} 行")

    cnt = [f"{p}_comment_cnt" for p in MACRO_SENTIMENT_PREFIX.values()]
    bad = macro[macro[cnt].sum(axis=1) != macro["comment_total"]]
    _fail(errs, bad.empty, f"macro: 好+中+差 != comment_total，{len(bad)} 行")

    rate_cols = ["comment_rate"] + [f"{p}_comment_rate" for p in MACRO_SENTIMENT_PREFIX.values()]
    for c in rate_cols:
        v = macro[c]
        _fail(errs, bool(((v >= 0) & (v <= 100 + 1e-6)).all()), f"macro: {c} 越界 [0,100]")
    hi = V1_SCORE_MAX if formula == SCORE_FORMULA_V1 else SCORE_MAX
    for c in ("overall_sentiment_score", "pre_overall_sentiment_score"):
        v = macro[c]
        _fail(errs, bool(((v >= SCORE_MIN) & (v <= hi)).all()),
              f"macro: {c} 越界 [{SCORE_MIN:g},{hi:g}]")

    grp = ["scenic_id", "travel_date", "time_granularity"]
    is_all = macro["channel"] == MACRO_CHANNEL_ALL
    sums = macro[~is_all].groupby(grp)[["comment_total"] + cnt].sum()
    alls = macro[is_all].set_index(grp)[["comment_total"] + cnt]
    j = alls.join(sums, rsuffix="_sum", how="left").fillna(0)
    off = pd.Series(False, index=j.index)
    for c in ["comment_total"] + cnt:
        off |= (j[c] - j[f"{c}_sum"]).abs() > 0
    _fail(errs, not bool(off.any()),
          f"macro: all 行计数 != 各渠道之和，{int(off.sum())} 组")

    rs = macro[~is_all].groupby(grp)["comment_rate"].sum()
    has = alls["comment_total"] > 0
    rs = rs.reindex(alls.index).fillna(0)[has]
    near = (rs - 100.0).abs() <= 0.001 * max(len(codes or []), 1) + 1e-6
    _fail(errs, bool(near.all()),
          f"macro: 各渠道 comment_rate 合计 != 100，{int((~near).sum())} 组")

    if not core.empty:
        want = core[["scenic_spot_code", "travel_date"]].drop_duplicates()
        got = macro[["scenic_id", "travel_date"]].drop_duplicates()
        miss = want.merge(got, left_on=["scenic_spot_code", "travel_date"],
                          right_on=["scenic_id", "travel_date"], how="left")
        _fail(errs, bool(miss["scenic_id"].notna().all()),
              f"macro: core 有行而 macro 缺行的 (景区,日期) {int(miss['scenic_id'].isna().sum())} 个")
    need_ch = set([MACRO_CHANNEL_ALL] + list(codes or []))
    have = macro.groupby(grp)["channel"].agg(set)
    lack = have[have.map(lambda s: not need_ch.issubset(s))]
    _fail(errs, lack.empty, f"macro: 有 {len(lack)} 组 (景区,日期,周期) 缺渠道行")
    if granularities:
        g = macro.groupby(["scenic_id", "travel_date"])["time_granularity"].agg(set)
        lack = g[g.map(lambda s: not set(granularities).issubset(s))]
        _fail(errs, lack.empty, f"macro: 有 {len(lack)} 个 (景区,日期) 缺周期行")

    for c in ("dimension_breakdown", "wordcloud_map", "period_comment_heatmap"):
        try:
            macro[c].map(json.loads)
        except Exception as e:                      # noqa: BLE001
            errs.append(f"macro: {c} 不是合法 JSON（{e}）")
    return errs
