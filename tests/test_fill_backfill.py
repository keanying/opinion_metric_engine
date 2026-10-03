# -*- coding: utf-8 -*-
"""缺数回补 = 先补明细，再算指标（domain §1.8 / §1.9）。

业务方原话（2.0 版）：

    每日的社媒评论内容，应该是先补数据 …… 按 {1: 5, 7: 10, 14: 15, 30: 20, 60: 30,
    90: 60, 365: 90} 进行补数 …… 然后再进行计算各种指标，包括同比 / 环比 / 率等，
    这个时候已经是补数据的明细了，这些指标计算如果有就有，没有就没有了。

    快手 9/10 有 3 条，9/11 ~ 9/13 都没有 → 9/11、9/12、9/13 各复制 9/10 的明细。
    唯一区别是：当日、近 1 日最大向前找 5 日，到那一天找到截止，其他类似。

这个文件把这段话逐条钉成断言。
"""

import json

import pandas as pd
import pytest

import engin_cli.metric_calc_domain as D
from engin_cli.pipeline import run
from engin_cli.settings import EtlSettings

CORE = "ads_trf_social_opinion_comment_core_di"
PLATFORM = "ads_trf_social_opinion_comment_platform_di"
DIMENSION = "ads_trf_social_opinion_comment_dimension_score_di"
CONTENT = "ads_trf_social_opinion_comment_content_di"
DRILL = "ads_trf_social_opinion_drill_analysis_di"


def _comment(ds, channel, cid, sentiment=1, word="好玩"):
    return {
        "scenic_id": "S1", "scenic_name": "景区1", "channel": channel, "work_id": "w1",
        "comment_id": cid, "comment_level": "level_1", "root_comment_id": "",
        "commenter_name": "u", "content": "x", "likes": 0,
        "sentiment_label": "正向" if sentiment > 0 else "负向",
        "sentiment_score": sentiment,
        "dimension_tags": json.dumps(
            [{"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光",
              "sentiment": sentiment}], ensure_ascii=False),
        "entity_tags": "[]",
        "keyword_tags": json.dumps([word], ensure_ascii=False),
        "publish_time": f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 10:00:00",
    }


def _settings(**kw):
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 60
    for k, v in kw.items():
        setattr(st, k, v)
    return st


# ══════════════════════════════════════════════════════════════════
# 业务方给的那个例子，一比一还原
# ══════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def kuaishou_case():
    """抖音天天有；快手只在 09-10 有 **1 条**，09-11 一条都没有。"""
    rows = []
    for d in pd.date_range("2026-08-20", "2026-09-11"):
        ds = d.strftime("%Y%m%d")
        for i in range(20):
            rows.append(_comment(ds, "douyin", f"dy{ds}{i}"))
    rows.append(_comment("20260910", "kuaishou", "ks1"))
    return run(_settings(), ["20260911"], comments_df=pd.DataFrame(rows))


def test_kuaishou_copies_previous_days_detail(kuaishou_case):
    """09-11 没有快手 → 复制 09-10 的明细，**就是那一天的 1 条**。"""
    p = kuaishou_case.tables[PLATFORM]
    ks = p[(p.travel_date == 20260911) & (p.platform_code == "kuaishou")]
    assert len(ks) == 1
    assert ks.comment_cnt.iloc[0] == 1          # 不是 0（没补），也不是 2（累计）


def test_douyin_with_data_today_is_not_filled(kuaishou_case):
    """当天有数据的渠道不补 —— 补齐只在渠道当天没评论时才发生。"""
    p = kuaishou_case.tables[PLATFORM]
    dy = p[(p.travel_date == 20260911) & (p.platform_code == "douyin")].iloc[0]
    assert dy.comment_cnt == 20                 # 09-11 真实就是 20 条
    assert dy.comment_cnt_7d == 140


def test_window_sums_the_filled_daily_detail(kuaishou_case):
    """窗口 = 补齐后的每日明细逐日相加：09-10 真实 1 条 + 09-11 复制 1 条 = 2。

    （旧口径「整窗前移」这里是 1 —— 那是把整个窗口挪到 [09-04, 09-10]。）
    """
    p = kuaishou_case.tables[PLATFORM]
    ks = p[(p.travel_date == 20260911) & (p.platform_code == "kuaishou")].iloc[0]
    for n in (7, 14, 30, 60, 90):
        assert ks[f"comment_cnt_{n}d"] == 2, f"{n} 日档应该是 09-10 + 09-11（复制）= 2"


# ══════════════════════════════════════════════════════════════════
# 业务方的例子：快手 9/10 有 3 条，9/11 ~ 9/13 都没有
# ══════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def three_day_gap():
    rows = []
    for d in pd.date_range("2026-08-20", "2026-09-13"):
        ds = d.strftime("%Y%m%d")
        for i in range(5):
            rows.append(_comment(ds, "douyin", f"dy{ds}{i}"))
    rows += [_comment("20260910", "kuaishou", f"ks{i}", 1 if i else -1, "排队")
             for i in range(3)]
    return run(_settings(), ["20260911", "20260912", "20260913"],
               comments_df=pd.DataFrame(rows))


def test_each_gap_day_copies_the_nearest_day(three_day_gap):
    """9/11、9/12、9/13 每天都是 9/10 的 3 条（每天各自往前找，找到 9/10 截止）。"""
    p = three_day_gap.tables[PLATFORM]
    ks = p[p.platform_code == "kuaishou"].set_index("travel_date")
    assert ks.loc[[20260911, 20260912, 20260913], "comment_cnt"].tolist() == [3, 3, 3]
    # 近 7 日（截至 9/13）= 9/10 真实 3 + 9/11~9/13 复制 3×3
    assert ks.loc[20260913, "comment_cnt_7d"] == 12
    assert ks.loc[20260912, "comment_cnt_7d"] == 9


def test_growth_uses_filled_detail(three_day_gap):
    """环比也在补齐后的明细上算：9/12 的 1 日环比 = (3 - 3) / 3 = 0。"""
    core = three_day_gap.tables[CORE].set_index("travel_date")
    # 抖音 5 + 快手 3（复制）
    assert core.loc[[20260911, 20260912, 20260913], "comment_count"].tolist() == [8, 8, 8]
    assert core.loc[20260912, "comment_count_dod"] == 0


def test_copied_detail_carries_sentiment_and_words(three_day_gap):
    """复制的是整条明细：好/中/差、关键词都跟着来（2 好 1 差）。"""
    p = three_day_gap.tables[PLATFORM]
    ks = p[(p.platform_code == "kuaishou") & (p.travel_date == 20260912)].iloc[0]
    assert ks.plat_positive_count == 2
    ct = three_day_gap.tables[CONTENT]
    day = ct[(ct.travel_date == 20260912) & (ct.emotion_word == "排队")]
    assert int(day.emotion_value.iloc[0]) == 3


def test_drill_includes_copied_comments(three_day_gap):
    """下钻表也包含复制来的评论（业务确认「包含」）：publish_time 挪到这一天，主键不撞。"""
    dr = three_day_gap.tables[DRILL]
    ks = dr[dr.platform_code == "kuaishou"]
    assert sorted(ks.travel_date.unique()) == [20260911, 20260912, 20260913]
    assert (ks.groupby("travel_date").size() == 3).all()
    day = ks[ks.travel_date == 20260912]
    assert pd.to_datetime(day.publish_time).dt.strftime("%Y%m%d").eq("20260912").all()
    assert ks.detail_uk.is_unique
    assert set(ks.comment_id) == {"ks0", "ks1", "ks2"}


# ══════════════════════════════════════════════════════════════════
# 往前找的上限按窗口分档：找满了还没有，这一天就是空的
# ══════════════════════════════════════════════════════════════════
def test_lookback_limit_is_respected():
    """当日档最多往前 5 天：第 6 天之外的数据借不到；7 日档（10 天）借得到。"""
    rows = [_comment("20260901", "kuaishou", "ks1")]
    for d in pd.date_range("2026-08-20", "2026-09-11"):    # 抖音撑住日历
        ds = d.strftime("%Y%m%d")
        rows.append(_comment(ds, "douyin", f"dy{ds}"))
    df = pd.DataFrame(rows)

    # 09-06 距离 09-01 是 5 天 → 够得着
    r = run(_settings(), ["20260906"], comments_df=df)
    p = r.tables[PLATFORM]
    assert p[(p.travel_date == 20260906)
             & (p.platform_code == "kuaishou")].comment_cnt.iloc[0] == 1

    # 09-07 距离 09-01 是 6 天 → 超过当日档上限 5，当日为 0；
    # 但 7 日档最多找 10 天：09-01 真实 1 + 09-02~09-07 各复制 1 = 7
    r = run(_settings(), ["20260907"], comments_df=df)
    p = r.tables[PLATFORM]
    ks = p[(p.travel_date == 20260907) & (p.platform_code == "kuaishou")].iloc[0]
    assert ks.comment_cnt == 0
    assert ks.comment_cnt_7d == 7


def test_fill_can_reach_before_the_load_range():
    """取数区间第一天没评论，也能从区间之前补（多取的那一段只当来源，不进窗口）。"""
    rows = [_comment("20260910", "kuaishou", "ks1")]
    for d in pd.date_range("2026-08-20", "2026-09-11"):
        ds = d.strftime("%Y%m%d")
        rows.append(_comment(ds, "douyin", f"dy{ds}"))
    r = run(_settings(lookback_days=1), ["20260911"], comments_df=pd.DataFrame(rows))
    assert r.errors == []
    p = r.tables[PLATFORM]
    ks = p[(p.travel_date == 20260911) & (p.platform_code == "kuaishou")].iloc[0]
    assert ks.comment_cnt == 1
    assert ks.comment_cnt_7d == 1             # 09-10 在取数区间外，只当来源，不进窗口


def test_lookback_table_matches_the_spec():
    """口径表就是业务方给的那几个数字，别在代码里各写一份。"""
    assert D.BACKFILL_LOOKBACK[1] == 5
    assert D.BACKFILL_LOOKBACK[7] == 10
    assert D.BACKFILL_LOOKBACK[14] == 15
    assert D.BACKFILL_LOOKBACK[30] == 20
    assert D.BACKFILL_LOOKBACK[60] == 30
    assert D.BACKFILL_LOOKBACK[90] == 60


# ══════════════════════════════════════════════════════════════════
# 可配置
# ══════════════════════════════════════════════════════════════════
def test_lookback_is_configurable_per_window():
    """settings.backfill_lookback 只覆盖写到的档位，其余仍用默认表。"""
    rows = [_comment("20260901", "kuaishou", "ks1")]
    for d in pd.date_range("2026-08-20", "2026-09-11"):
        ds = d.strftime("%Y%m%d")
        rows.append(_comment(ds, "douyin", f"dy{ds}"))
    df = pd.DataFrame(rows)

    # 把 1 日档的上限压到 2 天 → 09-06 就借不到了（默认 5 天时是借得到的）
    r = run(_settings(backfill_lookback={1: 2}), ["20260906"], comments_df=df)
    p = r.tables[PLATFORM]
    ks = p[(p.travel_date == 20260906) & (p.platform_code == "kuaishou")].iloc[0]
    assert ks.comment_cnt == 0
    # 7 日档没被覆盖，仍用默认 10 天：09-01 真实 1 + 09-02~09-06 各复制 1
    assert ks.comment_cnt_7d == 6


def test_backfill_mode_off_disables_fill():
    """backfill_mode="off" → 渠道当天没评论就是 0，不补。"""
    rows = [_comment("20260910", "kuaishou", "ks1")]
    for d in pd.date_range("2026-08-20", "2026-09-11"):
        ds = d.strftime("%Y%m%d")
        rows.append(_comment(ds, "douyin", f"dy{ds}"))
    r = run(_settings(backfill_mode=D.BACKFILL_MODE_OFF), ["20260911"],
            comments_df=pd.DataFrame(rows))
    p = r.tables[PLATFORM]
    assert p[(p.travel_date == 20260911)
             & (p.platform_code == "kuaishou")].comment_cnt.iloc[0] == 0


def test_context_merges_defaults_with_overrides():
    """RunContext.backfill_lookback：默认表 + 局部覆盖，不是整表替换。"""
    from engin_cli.context import RunContext
    ctx = RunContext(settings=_settings(backfill_lookback={7: 3}),
                     output_dates=["20260911"],
                     load_start="20260801", load_end="20260911")
    lb = ctx.backfill_lookback([1, 7, 30])
    assert lb == {1: 5, 7: 3, 30: 20}

    off = RunContext(settings=_settings(backfill_mode=D.BACKFILL_MODE_OFF),
                     output_dates=["20260911"],
                     load_start="20260801", load_end="20260911")
    assert off.backfill_lookback([1, 7, 30]) == {}
    # 往前多取的来源段 = 最大上限（365 日档 90 天）
    assert ctx.fill_start == "20260503"
    assert off.fill_start == "20260801"


def test_legacy_shift_mode_means_fill():
    """老配置写的 backfill_mode="shift" 等同 fill，不报错也不退回旧口径。"""
    assert D.backfill_enabled("shift") and D.backfill_enabled("fill")
    assert not D.backfill_enabled("off")


def test_cli_parses_lookback_overrides():
    from engin_cli.cli import _parse_lookback
    assert _parse_lookback("1:5,7:10,30:20") == {1: 5, 7: 10, 30: 20}
    assert _parse_lookback(" 1:3 , 7:7 ") == {1: 3, 7: 7}
    with pytest.raises(SystemExit):
        _parse_lookback("7")


# ══════════════════════════════════════════════════════════════════
# 「总数也要用补齐后的数据算」—— 四张表一视同仁
# ══════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def gap_day():
    """整个景区在 09-11 一条评论都没有，09-10 有 30 条。"""
    rows = []
    for d in pd.date_range("2026-08-20", "2026-09-10"):
        ds = d.strftime("%Y%m%d")
        for i in range(30):
            rows.append(_comment(ds, "douyin", f"dy{ds}{i}",
                                 1 if i % 3 else -1, "好玩" if i % 3 else "太差"))
    return run(_settings(), ["20260911"], comments_df=pd.DataFrame(rows))


def test_core_total_uses_backfilled_data(gap_day):
    """core：09-11 没数据 → 评论总数用 09-10 的 30 条，不是 0。"""
    core = gap_day.tables[CORE]
    row = core[core.travel_date == 20260911].iloc[0]
    assert row.comment_count == 30
    assert row.positive_count + row.neutral_count + row.negative_count == 30
    assert row.emotional_score > 0


def test_platform_total_uses_backfilled_data(gap_day):
    """platform：全平台口径也是回补后的，且 total == Σ 各渠道。"""
    p = gap_day.tables[PLATFORM]
    day = p[p.travel_date == 20260911]
    dy = day[day.platform_code == "douyin"].iloc[0]
    assert dy.comment_cnt == 30
    assert int(day["positive_count"].iloc[0]) == int(day["plat_positive_count"].sum())
    assert abs(day["comment_rate"].sum() - 1.0) < 0.01


def test_dimension_scores_use_backfilled_data(gap_day):
    """dimension：09-11 的维度分数来自回补后的样本，不是空的。"""
    dim = gap_day.tables[DIMENSION]
    day = dim[dim.travel_date == 20260911]
    assert not day.empty
    assert (day["dimension_1_score"] > 0).all()


def test_content_word_counts_use_backfilled_data(gap_day):
    """content：词频也是回补后的 —— 09-11 的「好玩」用 09-10 那天的次数。"""
    ct = gap_day.tables[CONTENT]
    day = ct[ct.travel_date == 20260911]
    assert set(day.emotion_word) == {"好玩", "太差"}
    assert int(day[day.emotion_word == "好玩"].emotion_value.iloc[0]) == 20
    assert int(day[day.emotion_word == "太差"].emotion_value.iloc[0]) == 10


def test_validation_passes_under_fill(gap_day, kuaishou_case, three_day_gap):
    assert gap_day.errors == []
    assert kuaishou_case.errors == []
    assert three_day_gap.errors == []


# ══════════════════════════════════════════════════════════════════
# 业务方给的第二个案例（多渠道明细）：core 总数 = 各渠道补齐后之和
# ══════════════════════════════════════════════════════════════════
RAW_CASE = {
    "20260910": {"douyin": 15, "xiaohongshu": 22, "weibo": 2, "ctrip": 4, "kuaishou": 3},
    "20260911": {"douyin": 10, "xiaohongshu": 20, "weibo": 5, "ctrip": 3},   # 没有快手
}


@pytest.fixture(scope="module")
def multi_channel_case():
    rows = [_comment(ds, ch, f"{ch}{ds}{i}")
            for ds, m in RAW_CASE.items() for ch, n in m.items() for i in range(n)]
    return run(_settings(lookback_days=30), ["20260910", "20260911"],
               comments_df=pd.DataFrame(rows))


def test_only_the_missing_channel_is_backfilled(multi_channel_case):
    """有数据的渠道不补，只有缺的那个复制前一天的明细。"""
    p = multi_channel_case.tables[PLATFORM]
    day = p[p.travel_date == 20260911].set_index("platform_code")["comment_cnt"]
    assert day["douyin"] == 10           # 原样
    assert day["xiaohongshu"] == 20      # 原样
    assert day["weibo"] == 5             # 原样
    assert day["ctrip"] == 3             # 原样
    assert day["kuaishou"] == 3          # 09-11 没有 → 复制 09-10 的 3 条
    assert day["tongcheng"] == 0         # 从来没有过数据 → 全 0 行


def test_core_total_equals_sum_of_backfilled_channels(multi_channel_case):
    """core.comment_count(09-11) = 41（补齐后各渠道之和），**不是** 38（原始之和）。

    这是业务方明确要求的：补了数，总数就要按补齐后的算。
    如果 core 自己在景区粒度上判断「09-11 有 38 条不算缺数」，
    就会出现 core=38 而渠道之和=41 的劈叉。
    """
    core = multi_channel_case.tables[CORE]
    p = multi_channel_case.tables[PLATFORM]
    got = int(core[core.travel_date == 20260911].comment_count.iloc[0])
    assert got == 41
    assert got != sum(RAW_CASE["20260911"].values())        # 原始 38
    assert got == int(p[p.travel_date == 20260911].comment_cnt.sum())

    # 没触发补齐的那天两边本来就相等
    d10 = int(core[core.travel_date == 20260910].comment_count.iloc[0])
    assert d10 == 46 == int(p[p.travel_date == 20260910].comment_cnt.sum())


def test_core_equals_platform_sum_is_a_hard_identity(multi_channel_case, gap_day,
                                                     kuaishou_case):
    """「core 总数 == Σ 各渠道」在每一个用例上都必须成立，不是碰巧。"""
    for res in (multi_channel_case, gap_day, kuaishou_case):
        core, p = res.tables[CORE], res.tables[PLATFORM]
        agg = p.groupby(["scenic_spot_code", "travel_date"])["comment_cnt"].sum()
        ref = core.set_index(["scenic_spot_code", "travel_date"])["comment_count"]
        assert (agg.sort_index() == ref.sort_index()).all()
        assert res.errors == []


def test_core_sentiment_split_follows_the_backfilled_channel(multi_channel_case):
    """好/中/差的拆分也跟着渠道走：补进来的样本带的是那一天那个渠道的情感分布。"""
    core = multi_channel_case.tables[CORE]
    row = core[core.travel_date == 20260911].iloc[0]
    assert row.positive_count + row.neutral_count + row.negative_count == row.comment_count
