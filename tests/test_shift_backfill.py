# -*- coding: utf-8 -*-
"""缺数回补 = 整窗前移，取到为止（domain §1.8）。

业务方原话：

    2026-09-11 没有 kuaishou 这个渠道，那么就往前取，在 2026-09-10 有一条，
    那么 2026-09-11 也是一条。1 日的往前最大 5 日，取到为止，
    **记住这个不是 5 日的累计**，是在那一日取到了就使用这一日的数据。
    最大向前：1日→5  7日→10  14日→15  30日→20  60日→30  90日→60
    以上向前跨度最好能进行配置化。
    总数需要对数据补齐后再计算，其他指标也一样，每一张表都是。

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


def test_kuaishou_takes_previous_days_value_not_a_sum(kuaishou_case):
    """09-11 没有快手 → 前移到 09-10，**就是那一天的 1 条**。"""
    p = kuaishou_case.tables[PLATFORM]
    ks = p[(p.travel_date == 20260911) & (p.platform_code == "kuaishou")]
    assert len(ks) == 1
    assert ks.comment_cnt.iloc[0] == 1          # 不是 0（没回补），也不是 2（累计）


def test_douyin_with_data_today_does_not_shift(kuaishou_case):
    """当期有数据的渠道一天都不挪 —— 回补只在没数据时才发生。"""
    p = kuaishou_case.tables[PLATFORM]
    dy = p[(p.travel_date == 20260911) & (p.platform_code == "douyin")].iloc[0]
    assert dy.comment_cnt == 20                 # 09-11 真实就是 20 条


def test_shift_is_not_cumulative_across_windows(kuaishou_case):
    """7 日档同理：整窗挪到 [09-04, 09-10]，里面只有 09-10 那 1 条。

    如果实现成「累计」，7 日档会把前移路径上的天数一起加进来，值就 > 1。
    """
    p = kuaishou_case.tables[PLATFORM]
    ks = p[(p.travel_date == 20260911) & (p.platform_code == "kuaishou")].iloc[0]
    for n in (7, 14, 30, 60, 90):
        assert ks[f"comment_cnt_{n}d"] == 1, f"{n} 日档应该只有 09-10 那 1 条"


# ══════════════════════════════════════════════════════════════════
# 前移上限：挪满了还没数据就取 0，不再往前找
# ══════════════════════════════════════════════════════════════════
def test_lookback_limit_is_respected():
    """1 日档最多往前 5 天：第 6 天之外的数据借不到。"""
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

    # 09-07 距离 09-01 是 6 天 → 超过 1 日档上限 5，取 0
    r = run(_settings(), ["20260907"], comments_df=df)
    p = r.tables[PLATFORM]
    assert p[(p.travel_date == 20260907)
             & (p.platform_code == "kuaishou")].comment_cnt.iloc[0] == 0


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
    assert ks.comment_cnt_7d == 1          # 7 日档没被覆盖，仍用默认 10 天，照样借得到


def test_backfill_mode_off_disables_shifting():
    """backfill_mode="off" → 当期没数据就是 0，一天都不挪。"""
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


def test_validation_passes_under_shift(gap_day, kuaishou_case):
    assert gap_day.errors == []
    assert kuaishou_case.errors == []


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
    """有数据的渠道一天都不挪，只有缺的那个往前借。"""
    p = multi_channel_case.tables[PLATFORM]
    day = p[p.travel_date == 20260911].set_index("platform_code")["comment_cnt"]
    assert day["douyin"] == 10           # 原样
    assert day["xiaohongshu"] == 20      # 原样
    assert day["weibo"] == 5             # 原样
    assert day["ctrip"] == 3             # 原样
    assert day["kuaishou"] == 3          # 09-11 没有 → 前移 1 天补 09-10 的 3
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

    # 没触发回补的那天两边本来就相等
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
