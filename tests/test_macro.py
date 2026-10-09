# -*- coding: utf-8 -*-
"""需求 2.0 大盘表 ads_trf_social_opinion_macro_gran_metric_di。

盯三件事：
1. 周期 → 日期区间的口径（本周/本月/本季度的上期、同比、今日/近一日）；
2. 数字跟独立重算的结果、跟 core/platform 表对得上（含「先补明细」的缺数回补）；
3. 可配置项（得分权重、周期粒度）真的生效。
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engin_cli import metric_calc_domain as D
from engin_cli.builders.macro import macro_data_ranges
from engin_cli.context import RunContext
from engin_cli.normalize import build_comment_facts, build_keyword_facts
from engin_cli.pipeline import run
from engin_cli.settings import EtlSettings

ROOT = Path(__file__).resolve().parents[1]
MACRO = "ads_trf_social_opinion_macro_gran_metric_di"
CORE = "ads_trf_social_opinion_comment_core_di"
PLATFORM = "ads_trf_social_opinion_comment_platform_di"
DRILL = "ads_trf_social_opinion_drill_analysis_di"

END = "20260917"
DAYS = 420                      # 够算「近90日」的去年同期
# 样例数据每 37 天造一个「当天只有 2 条评论」的日子 —— 多数渠道当天为空，会触发补齐明细
START = pd.Timestamp(END) - pd.Timedelta(days=DAYS - 1)
SPARSE_DAY = (START + pd.Timedelta(days=37 * ((DAYS - 1) // 37))).strftime("%Y%m%d")
DATES = sorted({SPARSE_DAY, "20260915", "20260916", END})


def _settings(**kw):
    st = EtlSettings()
    st.write_db = False
    st.write_csv = False
    for k, v in kw.items():
        setattr(st, k, v)
    return st


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    out = tmp_path_factory.mktemp("macro_sample")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample_data.py"),
                    "--days", str(DAYS), "--end-date", END, "--out", str(out)],
                   check=True, cwd=str(ROOT), capture_output=True)
    return pd.read_csv(out / "comments.csv"), pd.read_csv(out / "works.csv")


@pytest.fixture(scope="module")
def result(sample):
    comments, works = sample
    return run(_settings(), DATES, comments_df=comments, works_df=works)


@pytest.fixture(scope="module")
def macro(result):
    return result.tables[MACRO]


@pytest.fixture(scope="module")
def facts(sample):
    """独立重算用的评论明细（不经过 builder）。"""
    return build_comment_facts(sample[0])


def _row(macro, scenic, date, gran, channel="all"):
    r = macro[(macro.scenic_id == scenic) & (macro.travel_date == int(date))
              & (macro.time_granularity == gran) & (macro.channel == channel)]
    assert len(r) == 1, (scenic, date, gran, channel)
    return r.iloc[0]


def _count(cf, scenic, lo, hi, channel=None):
    m = (cf.scenic_spot_code == scenic) & cf.travel_date.between(lo, hi)
    if channel:
        m &= cf.platform_code == channel
    s = cf[m]
    return int(s.is_positive.sum()), int(s.is_neutral.sum()), int(s.is_negative.sum())


def _filled(df, cf, scenic, lo, hi, lookback, channel=None):
    """独立实现的「先补明细」（domain §1.8），不经过 builder：

    区间里每一天、每个渠道：当天有评论就用当天的明细，没有就往前 lookback 天内
    找最近一个有评论的日子，用那一天的明细。df 可以是评论 / 维度 / 关键词明细。
    """
    pres = {ch: set(g) for ch, g in
            cf[cf.scenic_spot_code == scenic].groupby("platform_code")["travel_date"]}
    parts = []
    for d in pd.date_range(pd.Timestamp(lo), pd.Timestamp(hi)):
        for ch, days in pres.items():
            if channel and ch != channel:
                continue
            for k in range(lookback + 1):
                src = (d - pd.Timedelta(days=k)).strftime("%Y%m%d")
                if src in days:
                    parts.append(df[(df.scenic_spot_code == scenic)
                                    & (df.platform_code == ch) & (df.travel_date == src)])
                    break
    return pd.concat(parts) if parts else df.iloc[:0]


def _fcount(cf, scenic, lo, hi, lookback, channel=None):
    """补齐后的 (好, 中, 差)。"""
    s = _filled(cf, cf, scenic, lo, hi, lookback, channel)
    return int(s.is_positive.sum()), int(s.is_neutral.sum()), int(s.is_negative.sum())


# ══════════════════════════════════════════════════════════════════
# 周期 → 日期区间
# ══════════════════════════════════════════════════════════════════
def _spec(name):
    return next(s for s in D.macro_granularities() if s["name"] == name)


def _fmt(r):
    return {k: (lo.strftime("%Y%m%d"), hi.strftime("%Y%m%d")) for k, (lo, hi) in r.items()}


def test_default_granularities_match_the_requirement():
    """time_granularity 落表的是英文编码，中文名只放在 label 里（业务指定的取值）。"""
    specs = D.macro_granularities()
    assert [(s["name"], s["label"]) for s in specs] == [
        ("td", "今日"), ("latest_1d", "近一日"), ("latest_7d", "近7日"),
        ("wtd", "本周"), ("latest_30d", "近30日"), ("mtd", "本月"),
        ("latest_60d", "近60日"), ("latest_90d", "近90日"), ("qtd", "本季度")]


def test_label_defaults_to_name():
    spec = D.macro_granularities([{"name": "latest_14d", "type": "rolling", "days": 14}])[0]
    assert spec["label"] == "latest_14d"


def test_today_vs_last_day():
    """今日 = travel_date 当天；近一日 = 前一天（业务确认）。"""
    assert _fmt(D.macro_period_ranges(_spec("td"), "20260917")) == {
        "cur": ("20260917", "20260917"), "prev": ("20260916", "20260916"),
        "yoy": ("20250917", "20250917")}
    assert _fmt(D.macro_period_ranges(_spec("latest_1d"), "20260917"))["cur"] == (
        "20260916", "20260916")


def test_rolling_window_and_previous_period():
    r = _fmt(D.macro_period_ranges(_spec("latest_7d"), "20260917"))
    assert r["cur"] == ("20260911", "20260917")
    assert r["prev"] == ("20260904", "20260910")
    assert r["yoy"] == ("20250911", "20250917")


def test_week_is_to_date_and_previous_week_is_the_same_days():
    """2026-09-17 是周四：本周 = 周一~周四，上期 = 上周一~上周四。"""
    r = _fmt(D.macro_period_ranges(_spec("wtd"), "20260917"))
    assert r["cur"] == ("20260914", "20260917")
    assert r["prev"] == ("20260907", "20260910")


def test_month_previous_is_clipped_to_a_shorter_month():
    r = _fmt(D.macro_period_ranges(_spec("mtd"), "20260331"))
    assert r["cur"] == ("20260301", "20260331")
    assert r["prev"] == ("20260201", "20260228")      # 2 月没有 29~31 号
    assert _fmt(D.macro_period_ranges(_spec("mtd"), "20260917"))["prev"] == (
        "20260801", "20260817")


def test_quarter_to_date():
    r = _fmt(D.macro_period_ranges(_spec("qtd"), "20260917"))
    assert r["cur"] == ("20260701", "20260917")
    assert r["prev"] == ("20260401", "20260618")      # 本季度第 79 天 → 上季度第 79 天
    assert r["yoy"] == ("20250701", "20250917")


def test_yoy_on_leap_day():
    r = _fmt(D.macro_period_ranges(_spec("td"), "20280229"))
    assert r["yoy"] == ("20270228", "20270228")


def test_bad_granularity_config_fails_loudly():
    with pytest.raises(ValueError):
        D.macro_granularities([{"name": "近N日", "type": "rolling"}])          # 缺 days
    with pytest.raises(ValueError):
        D.macro_granularities([{"name": "x", "type": "fortnight"}])
    with pytest.raises(ValueError):
        D.macro_granularities([{"name": "x", "type": "week"}, {"name": "x", "type": "month"}])


def test_fetch_ranges_cover_previous_periods_and_last_year():
    st = _settings()
    ctx = RunContext(settings=st, output_dates=[END], load_start="20260322", load_end=END)
    ranges = macro_data_ranges(ctx)
    assert ranges[-1][1] == END
    # 去年同期：近90日的去年同期从 2025-06-20 起，再往前多取补齐上限 60 天 → 2025-04-21
    assert ranges[0][0] <= "20250421"
    assert any(lo <= "20250917" <= hi for lo, hi in ranges)
    # 去年同期（到 2025-09-17）与今年上期最早用到的日期（本季度上期再往前 60 天，约 2026-01）
    # 之间那段不需要，不该整段多取
    assert not any(lo <= "20251101" <= hi for lo, hi in ranges)


# ══════════════════════════════════════════════════════════════════
# 原子公式与地域
# ══════════════════════════════════════════════════════════════════
def test_percent_formulas_and_zero_denominators():
    assert D.pct_ratio(1, 3) == pytest.approx(33.333333)
    assert D.pct_ratio(5, 0) == 0
    assert D.pct_growth(120, 100) == pytest.approx(20.0)
    assert D.pct_growth(5, 0) == 0


@pytest.mark.parametrize("raw,want", [
    ("IP属地：广东", "广东"), ("IP属地:湖北", "湖北"), ("广东省深圳市", "广东"),
    ("四川成都", "四川"), ("北京市", "北京"), ("黑龙江哈尔滨", "黑龙江"),
    ("广西壮族自治区", "广西"), ("中国 上海", "上海"), ("美国", "美国"),
    ("", ""), (None, ""), ("未知", ""), (float("nan"), ""),
])
def test_normalize_region(raw, want):
    assert D.normalize_region(raw) == want


def test_score_weights_validation():
    assert D.resolve_score_weights(None) == (1.0, 0.9, 0.5)
    assert D.resolve_score_weights({"neutral": 0.8}) == (1.0, 0.8, 0.5)
    with pytest.raises(ValueError):
        D.resolve_score_weights({"neutral": 1.5})
    with pytest.raises(ValueError):
        D.resolve_score_weights({"good": 1.0})


# ══════════════════════════════════════════════════════════════════
# 端到端
# ══════════════════════════════════════════════════════════════════
def test_run_is_clean(result):
    assert result.ok, result.errors


def test_one_row_per_scenic_date_granularity_channel(macro, result):
    n_scenic = macro.scenic_id.nunique()
    n_channel = len(D.PLATFORM_CODES) + 1
    assert len(macro) == n_scenic * len(DATES) * 9 * n_channel
    assert not macro.duplicated(D.MACRO_KEYS).any()
    assert list(macro.columns) == D.MACRO_COLUMNS
    assert set(macro.channel) == set(D.PLATFORM_CODES) | {"all"}
    assert set(macro[macro.channel == "all"].channel_name) == {"整体"}


def test_backfill_actually_happened(result):
    """样例里那个稀疏日要真的触发补齐明细，否则下面的对账测不到回补。"""
    note = next(n for n in result.notes if n.startswith("输出日期上有"))
    assert "已复制往前最近一天的明细" in note and not note.startswith("输出日期上有 0 ")


@pytest.mark.parametrize("gran,days", [("td", 1), ("latest_7d", 7), ("latest_30d", 30),
                                       ("latest_60d", 60), ("latest_90d", 90)])
def test_rolling_channel_rows_equal_platform_table(macro, result, gran, days):
    """近 N 日的渠道行 == platform 表同渠道的 N 日计数（补齐后），逐行对账。"""
    plat = result.tables[PLATFORM]
    m = macro[(macro.time_granularity == gran) & (macro.channel != "all")]
    j = m.merge(plat, left_on=["scenic_id", "travel_date", "channel"],
                right_on=["scenic_spot_code", "travel_date", "platform_code"])
    assert len(j) == len(m)
    assert (j["comment_total"] == j[D.PLAT_CNT[days]]).all()
    assert (j["positive_comment_cnt"] == j[D.PLAT_POS[days]]).all()


def test_today_all_row_equals_core_comment_count(macro, result):
    core = result.tables[CORE]
    m = macro[(macro.time_granularity == "td") & (macro.channel == "all")]
    j = m.merge(core, left_on=["scenic_id", "travel_date"],
                right_on=["scenic_spot_code", "travel_date"])
    assert len(j) == len(m)
    assert (j["comment_total"] == j["comment_count"]).all()
    assert (j["positive_comment_cnt"] == j["positive_count"]).all()
    assert (j["negative_comment_cnt"] == j["negative_count"]).all()


def test_all_row_is_the_sum_of_channels(macro):
    keys = ["scenic_id", "travel_date", "time_granularity"]
    cnt = ["comment_total", "positive_comment_cnt", "neutral_comment_cnt", "negative_comment_cnt"]
    s = macro[macro.channel != "all"].groupby(keys)[cnt].sum()
    a = macro[macro.channel == "all"].set_index(keys)[cnt]
    pd.testing.assert_frame_equal(a.sort_index(), s.sort_index(), check_dtype=False)


def test_comment_rate(macro):
    assert (macro[macro.channel == "all"].comment_rate == 0).all()
    s = macro[macro.channel != "all"].groupby(
        ["scenic_id", "travel_date", "time_granularity"]).comment_rate.sum()
    assert np.allclose(s, 100.0, atol=1e-4)


def test_calendar_month_against_independent_count(macro, facts):
    """本月：本期 = 9/1~9/17，上期 = 8/1~8/17，同比 = 2025-09-01~09-17，
    三段都在补齐后的明细上求和（本月 → 30 日档，最多往前找 20 天）。"""
    sc = "PFTSCA01002434"
    r = _row(macro, sc, END, "mtd")
    cur = _fcount(facts, sc, "20260901", END, 20)
    prev = _fcount(facts, sc, "20260801", "20260817", 20)
    yoy = _fcount(facts, sc, "20250901", "20250917", 20)
    assert (r.positive_comment_cnt, r.neutral_comment_cnt, r.negative_comment_cnt) == cur
    assert r.comment_total_mom == pytest.approx(
        float(D.pct_growth(sum(cur), sum(prev))), abs=1e-6)
    assert r.comment_total_yoy == pytest.approx(
        float(D.pct_growth(sum(cur), sum(yoy))), abs=1e-6)
    assert r.positive_comment_rate == pytest.approx(cur[0] * 100 / sum(cur), abs=1e-6)
    assert r.overall_sentiment_score == pytest.approx(
        float(D.weighted_score_from_counts(*cur, sum(cur), decimals=6)), abs=1e-6)
    assert r.pre_overall_sentiment_score == pytest.approx(
        float(D.weighted_score_from_counts(*prev, sum(prev), decimals=6)), abs=1e-6)


def test_channel_yoy_against_independent_count(macro, facts):
    sc, ch = "PFT_S_00001", "douyin"
    r = _row(macro, sc, END, "wtd", ch)
    cur = _fcount(facts, sc, "20260914", END, 10, ch)
    yoy = _fcount(facts, sc, "20250914", "20250917", 10, ch)
    assert r.comment_total == sum(cur)
    assert r.negative_comment_yoy == pytest.approx(
        float(D.pct_growth(cur[2], yoy[2])), abs=1e-6)


def test_dimension_breakdown(macro, sample):
    sc = "PFTSCA01002434"
    r = _row(macro, sc, END, "mtd")
    items = json.loads(r.dimension_breakdown)
    assert [i["dimension1"] for i in items] == D.L1_DIMENSIONS
    assert all(i["dimension1"] == i["preDimension1"] for i in items)
    assert set(items[0]) == {"dimension1", "dimension1Score", "preDimension1",
                             "preDimension1Score"}
    # 独立重算一个维度（补齐后的维度明细，本月 → 最多往前找 20 天）
    from engin_cli.normalize import build_dimension_facts
    cf = build_comment_facts(sample[0])
    df = build_dimension_facts(cf, "keep")
    d = _filled(df, cf, sc, "20260901", END, 20)
    d = d[d.dimension_level1 == "交通接驳"]
    want = D.weighted_score_from_counts(d.is_positive.sum(), d.is_neutral.sum(),
                                        d.is_negative.sum(), len(d), decimals=6)
    got = next(i for i in items if i["dimension1"] == "交通接驳")["dimension1Score"]
    assert got == pytest.approx(float(want), abs=1e-6)


def test_wordcloud_ties_are_broken_by_recent_heat():
    """本期次数并列时按近期热度排先后（决定谁进前 N），不是按字符顺序截断。"""
    words = ["啊词", "环境优美", "空气清新", "冷门说法"]
    sents = [D.POSITIVE] * 4
    counts = [1, 1, 1, 1]                      # 本期都只出现 1 次
    heat = [1, 40, 25, 1]                      # 近 90 天：环境优美、空气清新常见
    wc = json.loads(D.macro_wordcloud(words, sents, counts, top_n=2, heat=heat))
    assert [x["word"] for x in wc["positiveWord"]] == ["环境优美", "空气清新"]
    assert all(x["rate"] == 25.0 for x in wc["positiveWord"])      # 占比不受影响
    # 本期次数仍然优先于热度
    wc = json.loads(D.macro_wordcloud(words, sents, [2, 1, 1, 1], top_n=2, heat=heat))
    assert [x["word"] for x in wc["positiveWord"]] == ["啊词", "环境优美"]
    # 不给热度时退回按词排序（结果可复现）
    wc = json.loads(D.macro_wordcloud(words, sents, counts, top_n=2))
    assert [x["word"] for x in wc["positiveWord"]] == sorted(words)[:2]


def test_wordcloud(macro, facts):
    sc = "PFTSCA01002434"
    r = _row(macro, sc, END, "latest_30d")
    wc = json.loads(r.wordcloud_map)
    assert list(wc) == ["positiveWord", "neutralWord", "negativeWord"]
    for group in wc.values():
        assert len(group) <= 30
        rates = [x["rate"] for x in group]
        assert rates == sorted(rates, reverse=True)
    # 占比 = 该词次数 × 100 / 周期内全部关键词次数（补齐后的关键词明细，近30日 → 最多找 20 天）
    kw = build_keyword_facts(facts)
    k = _filled(kw, facts, sc, "20260819", END, 20)
    top = wc["positiveWord"][0]
    n = ((k.emotion_word == top["word"]) & (k.sentiment == D.POSITIVE)).sum()
    assert top["rate"] == pytest.approx(n * 100 / len(k), abs=1e-6)


def test_heatmap_uses_normalized_regions(macro, facts):
    sc = "PFTSCA01002434"
    r = _row(macro, sc, END, "latest_7d")
    hm = json.loads(r.period_comment_heatmap)
    regions = [x["region"] for x in hm]
    assert "广东" in regions and not any("IP属地" in x or "省" in x for x in regions)
    assert "" not in regions
    heats = [x["heat"] for x in hm]
    assert heats == sorted(heats, reverse=True)
    f = _filled(facts, facts, sc, "20260911", END, 10)
    assert dict((x["region"], x["heat"]) for x in hm)["广东"] == int((f.region == "广东").sum())


# ══════════════════════════════════════════════════════════════════
# 可配置项
# ══════════════════════════════════════════════════════════════════
def test_score_weights_apply_to_macro_and_core(sample):
    comments, works = sample
    res = run(_settings(score_weights={"neutral": 0.6}, enable_drill_analysis=False),
              [END], comments_df=comments, works_df=works)
    assert res.ok, res.errors
    m = res.tables[MACRO]
    r = m[(m.scenic_id == "PFTSCA01002434") & (m.time_granularity == "td")
          & (m.channel == "all")].iloc[0]
    want = D.weighted_score_from_counts(r.positive_comment_cnt, r.neutral_comment_cnt,
                                        r.negative_comment_cnt, r.comment_total,
                                        decimals=6, weights=(1.0, 0.6, 0.5))
    assert r.overall_sentiment_score == pytest.approx(float(want), abs=1e-6)
    core = res.tables[CORE]
    c = core[core.scenic_spot_code == "PFTSCA01002434"].iloc[0]
    assert c.emotional_score == pytest.approx(float(D.weighted_score_from_counts(
        c.positive_count, c.neutral_count, c.negative_count, c.comment_count,
        weights=(1.0, 0.6, 0.5))), abs=1e-4)


def test_granularities_are_configurable(sample):
    comments, works = sample
    grans = [{"name": "latest_14d", "type": "rolling", "days": 14},
             {"name": "this_year", "type": "year"}]
    res = run(_settings(macro_granularities=grans, enable_drill_analysis=False),
              [END], comments_df=comments, works_df=works)
    assert res.ok, res.errors
    m = res.tables[MACRO]
    assert set(m.time_granularity) == {"latest_14d", "this_year"}
    # 近14日 同样跟 platform 表对账
    plat = res.tables[PLATFORM]
    j = m[(m.time_granularity == "latest_14d") & (m.channel != "all")].merge(
        plat, left_on=["scenic_id", "travel_date", "channel"],
        right_on=["scenic_spot_code", "travel_date", "platform_code"])
    assert (j.comment_total == j[D.PLAT_CNT[14]]).all()


def test_macro_can_be_turned_off(sample):
    comments, works = sample
    res = run(_settings(enable_macro_metric=False, enable_drill_analysis=False),
              [END], comments_df=comments, works_df=works)
    assert res.ok and MACRO not in res.tables


def test_existing_tables_unchanged_by_macro(sample):
    """macro 多取了去年同期的数据，但其余表只能用 [load_start, load_end] —— 开关前后逐值相同。"""
    comments, works = sample
    on = run(_settings(enable_drill_analysis=False), [END], comments_df=comments,
             works_df=works)
    off = run(_settings(enable_drill_analysis=False, enable_macro_metric=False), [END],
              comments_df=comments, works_df=works)
    for t in off.tables:
        a = on.tables[t].drop(columns=["etl_time"])
        b = off.tables[t].drop(columns=["etl_time"])
        pd.testing.assert_frame_equal(a, b)


# ══════════════════════════════════════════════════════════════════
# 下钻表新增列
# ══════════════════════════════════════════════════════════════════
def test_drill_has_region_and_entity_tags(result):
    drill = result.tables[DRILL]
    assert {"region", "entity_tags"} <= set(drill.columns)
    assert "广东" in set(drill.region)
    assert not drill.region.str.contains("IP属地").any()
    parsed = drill.entity_tags.map(json.loads)
    assert parsed.map(lambda v: isinstance(v, list)).all()
    assert parsed.map(len).gt(0).any()


# ══════════════════════════════════════════════════════════════════
# 所有指标都在补齐明细之后再算（业务确认）：本期、上期、去年同期都用补齐后的明细
# ══════════════════════════════════════════════════════════════════
GAP_SCENIC = "PFTSCA01002434"


@pytest.fixture(scope="module")
def gap_comments(sample):
    """造缺数：同程 2026-09-14 起一条都没有；抖音 2025-09-14~09-17（去年同期）一条都没有。"""
    c = sample[0].copy()
    d = pd.to_datetime(c.publish_time, errors="coerce").dt.strftime("%Y%m%d")
    drop = ((c.scenic_id == GAP_SCENIC) & (c.channel == "tongcheng") & (d >= "20260914"))
    drop |= ((c.scenic_id == GAP_SCENIC) & (c.channel == "douyin")
             & d.between("20250914", "20250917"))
    return c[~drop]


@pytest.fixture(scope="module")
def gap_run(gap_comments, sample):
    res = run(_settings(enable_drill_analysis=False), [END], comments_df=gap_comments,
              works_df=sample[1])
    assert res.ok, res.errors
    return res


@pytest.fixture(scope="module")
def gap_macro(gap_run, gap_comments):
    return gap_run.tables[MACRO], build_comment_facts(gap_comments)


def test_calendar_period_is_filled_day_by_day(gap_macro):
    """本周（9/14 周一 ~ 9/17）同程一条都没有 → 每一天各自复制 9/13 的明细（本周 → 最多找 10 天）。"""
    macro, cf = gap_macro
    r = _row(macro, GAP_SCENIC, END, "wtd", "tongcheng")
    day = _count(cf, GAP_SCENIC, "20260913", "20260913", "tongcheng")
    assert sum(day) > 0
    assert (r.positive_comment_cnt, r.neutral_comment_cnt, r.negative_comment_cnt) == \
        tuple(4 * x for x in day)
    # 上期 9/7~9/10 不挪，同样在补齐后的明细上算
    prev = _fcount(cf, GAP_SCENIC, "20260907", "20260910", 10, "tongcheng")
    assert r.comment_total_mom == pytest.approx(
        float(D.pct_growth(4 * sum(day), sum(prev))), abs=1e-6)


def test_year_ago_period_uses_filled_detail(gap_macro):
    """抖音去年 9/14~9/17 没数据 → 去年同期每天复制 2025-09-13 的明细，同比不再是 0 / -100。"""
    macro, cf = gap_macro
    r = _row(macro, GAP_SCENIC, END, "wtd", "douyin")
    cur = _fcount(cf, GAP_SCENIC, "20260914", END, 10, "douyin")
    day = _count(cf, GAP_SCENIC, "20250913", "20250913", "douyin")
    assert sum(day) > 0
    assert _fcount(cf, GAP_SCENIC, "20250914", "20250917", 10, "douyin") == \
        tuple(4 * x for x in day)
    assert r.comment_total_yoy == pytest.approx(
        float(D.pct_growth(sum(cur), 4 * sum(day))), abs=1e-6)


def test_previous_period_uses_filled_detail(gap_comments, sample):
    """上期没数据 → 上期每天复制往前最近一天的明细，环比不因上期缺数而变成 0。"""
    c = gap_comments
    d = pd.to_datetime(c.publish_time, errors="coerce").dt.strftime("%Y%m%d")
    # 抖音去掉 8/1~8/17（本月的上期）：8/1~8/17 都复制 7/31（8/17 距 7/31 是 17 天 ≤ 20）
    c = c[~((c.scenic_id == GAP_SCENIC) & (c.channel == "douyin") & d.between("20260801", "20260817"))]
    res = run(_settings(enable_drill_analysis=False), [END], comments_df=c, works_df=sample[1])
    assert res.ok, res.errors
    r = _row(res.tables[MACRO], GAP_SCENIC, END, "mtd", "douyin")
    cf = build_comment_facts(c)
    cur = _fcount(cf, GAP_SCENIC, "20260901", END, 20, "douyin")
    day = sum(_count(cf, GAP_SCENIC, "20260731", "20260731", "douyin"))
    assert day > 0
    assert r.comment_total_mom == pytest.approx(
        float(D.pct_growth(sum(cur), 17 * day)), abs=1e-6)


def test_backfill_off_keeps_raw_counts(gap_comments, sample):
    res = run(_settings(enable_drill_analysis=False, backfill_mode="off"), [END],
              comments_df=gap_comments, works_df=sample[1])
    r = _row(res.tables[MACRO], GAP_SCENIC, END, "wtd", "tongcheng")
    assert r.comment_total == 0


def test_fill_report_counts_the_gap(gap_run):
    """同程 9/17 没评论 → 报告里算进「已复制」的组合。"""
    note = next(n for n in gap_run.notes if n.startswith("输出日期上有"))
    assert "已复制往前最近一天的明细" in note
