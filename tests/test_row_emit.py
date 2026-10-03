# -*- coding: utf-8 -*-
"""行存在规则（domain §1.7）：当天没有数据，行也要在。

源表里「某平台/某维度/某个词当天一条都没有」= 这一行根本不存在。
如果产出跟着一起消失，看板查今天的近 30/60/90 日指标就会扑空 ——
窗口里明明有值，行没了。这个文件盯的就是这件事。
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


@pytest.fixture(scope="module")
def sparse():
    """一段**故意稀疏**的数据：

    · 微博 / 抖音 8/20~8/30 每天都有
    · 小红书只有 8/20 一条，之后再没有
    · 携程只有 8/25 一条
    · 快手 / 同程 **从头到尾一条都没有**
    · 8/31 整个景区一条评论都没有
    """
    rows = []
    for d in range(20, 31):
        ds = f"202608{d:02d}"
        for i in range(30):
            rows.append(_comment(ds, "weibo", f"wb{ds}{i}",
                                 1 if i % 3 else -1, "好玩" if i % 3 else "太差"))
        for i in range(10):
            rows.append(_comment(ds, "douyin", f"dy{ds}{i}"))
    rows.append(_comment("20260820", "xiaohongshu", "xhs1"))
    rows.append(_comment("20260825", "ctrip", "ct1"))
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def result(sparse):
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 60
    st.backfill_mode = "off"            # 关掉明细补齐，好判断值到底来自哪
    return run(st, ["20260830", "20260831"], comments_df=sparse)


def test_validation_passes_with_zero_rows(result):
    assert result.errors == []


# ---------- platform ----------
def test_all_configured_channels_get_a_row_every_day(result):
    """配置的 6 个渠道每天都要有行，一个都不能少。"""
    p = result.tables[PLATFORM]
    for ds in (20260830, 20260831):
        got = set(p[p.travel_date == ds]["platform_code"])
        assert set(D.PLATFORM_CODES) <= got, f"{ds} 缺渠道：{set(D.PLATFORM_CODES) - got}"


def test_channel_with_no_data_today_keeps_window_values(result):
    """小红书当天 0 条，但近 30 日有 1 条 —— 行要在，窗口值要保住。

    这就是行存在规则的起因：当日没数据就没有渠道记录，可 30/60/90 日是有值的。
    本用例跑在 backfill_mode="off" 下，看的是「不回补时值到底来自哪」。
    """
    p = result.tables[PLATFORM]
    xhs = p[(p.travel_date == 20260830) & (p.platform_code == "xiaohongshu")]
    assert len(xhs) == 1
    assert xhs.comment_cnt.iloc[0] == 0            # 当日确实是 0
    assert xhs.comment_cnt_30d.iloc[0] == 1        # 近 30 日有 1 条
    assert xhs.comment_cnt_7d.iloc[0] == 0         # 近 7 日确实没有


def test_channel_with_no_data_ever_is_all_zero(result):
    """配置了但从来没有数据的渠道 → 全 0 行，而不是没有行。"""
    p = result.tables[PLATFORM]
    for code in ("kuaishou", "tongcheng"):
        row = p[(p.travel_date == 20260830) & (p.platform_code == code)]
        assert len(row) == 1, f"{code} 应该出一条全 0 行"
        assert row.comment_cnt.iloc[0] == 0
        assert row.comment_cnt_365d.iloc[0] == 0
        assert row.comment_rate.iloc[0] == 0
        assert row.platform_name.iloc[0] == D.CHANNEL_NAME[code]   # 名称从字典补齐


def test_platform_sum_still_equals_core(result):
    """补零行不能把「平台之和 = core 总数」这条恒等式破坏掉。

    开不开回补都成立：回补的原子粒度是渠道（domain §1.9），core 吃的就是渠道
    粒度的事实，在渠道上挪完再按景区相加，两边同源。
    开着回补的版本见 test_fill_backfill.py。
    """
    core, p = result.tables[CORE], result.tables[PLATFORM]
    agg = p.groupby(["scenic_spot_code", "travel_date"])["comment_cnt"].sum()
    ref = core.set_index(["scenic_spot_code", "travel_date"])["comment_count"]
    assert (agg.sort_index() == ref.sort_index()).all()


def test_unconfigured_channel_in_data_is_not_dropped(sparse):
    """数据里有、配置里没写的渠道要保留 —— 采集侧新接平台不该因为配置忘改而丢数。"""
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 60
    st.platform_codes = ["weibo"]           # 配置里只留微博
    r = run(st, ["20260830"], comments_df=sparse)
    codes = set(r.tables[PLATFORM]["platform_code"])
    assert "weibo" in codes and "douyin" in codes      # 抖音没配置，但数据里有
    assert "kuaishou" not in codes                     # 没配置也没数据 → 不出行


# ---------- core ----------
def test_core_emits_row_on_zero_comment_day(result):
    """8/31 整天没有评论，行还是要在，窗口指标照旧。"""
    core = result.tables[CORE]
    row = core[core.travel_date == 20260831]
    assert len(row) == 1
    assert row.comment_count.iloc[0] == 0
    assert row.comment_count_dod.iloc[0] == -1.0       # 从 40 掉到 0 = -100%
    assert row.positive_rate_30d.iloc[0] > 0           # 近 30 日的比率还在
    assert row.emotional_score_30d.iloc[0] > 0


def test_core_does_not_emit_before_first_data(sparse):
    """景区接入之前的日期不该凭空多出空行。"""
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 60
    r = run(st, ["20260810", "20260830"], comments_df=sparse)   # 8/10 早于首条数据
    dates = set(r.tables[CORE]["travel_date"])
    assert 20260810 not in dates and 20260830 in dates


# ---------- dimension ----------
def test_dimension_path_survives_a_day_without_mentions(result):
    """维度路径当天没人提，行也要在 —— 否则折线图会断掉。"""
    dim = result.tables[DIMENSION]
    for ds in (20260830, 20260831):
        assert len(dim[dim.travel_date == ds]) > 0
    row = dim[dim.travel_date == 20260831].iloc[0]
    assert row.dimension_1_score_30d > 0


# ---------- content ----------
def test_word_survives_a_day_without_occurrence(result):
    """词当天没出现，但近 30 日出现过 → 行要在，30 日热度占比不能被抹成 0。

    这一条同时盯住分母：core 当天没有行时，分母不铺网格就会取到 NaN，
    热度占比被兜底成 0，30 日词云直接失真。
    """
    ct = result.tables[CONTENT]
    today = ct[ct.travel_date == 20260831]
    assert set(today.emotion_word) == {"好玩", "太差"}
    for _, r in today.iterrows():
        assert r.emotion_value == 0                    # 当日确实没出现
        assert r.emotion_value_30d > 0                 # 近 30 日出现过
        assert r.emotion_rate_30d > 0                  # 分母正确 → 占比不为 0


def test_word_emotion_type_carried_forward(result):
    """补零行的情感归属沿用该词最近一次出现时的归类，不能留空。"""
    ct = result.tables[CONTENT]
    today = ct[ct.travel_date == 20260831]
    assert today[today.emotion_word == "太差"].emotion_type.iloc[0] == "negative"
    assert today[today.emotion_word == "好玩"].emotion_type.iloc[0] == "positive"


def test_content_row_window_bounds_the_table(sparse):
    """content_row_window 控制铺行范围：调小到 1 天，就只剩当天真的出现过的词。"""
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 60
    st.content_row_window = 1
    st.backfill_mode = "off"
    r = run(st, ["20260831"], comments_df=sparse)
    assert r.tables[CONTENT].empty          # 8/31 一个词都没出现


# ============================================================
# 线上实测回归：八大处公园 20260909 / 20260910
#   微博 0909 有 1 条、0910 归零（但近 30 日 10010 条）
#   快手 0909 归零（但近 90 日 2600 条）、0910 有 1 条
#   同程 全程 0 条
# 这三种情况在旧版里都会让整条渠道记录消失。
# ============================================================
def _bdc_comments():
    import random
    random.seed(7)
    rows = []
    for d in pd.date_range("2026-06-13", "2026-09-10"):
        ds = d.strftime("%Y%m%d")
        if ds != "20260910":                       # 微博 0910 一条都没有
            n = 1 if ds == "20260909" else random.randint(80, 200)
            for i in range(n):
                rows.append(_comment(ds, "weibo", f"wb{ds}{i}",
                                     random.choice([1, 0, -1, -1])))
        for i in range(random.randint(100, 300)):
            rows.append(_comment(ds, "douyin", f"dy{ds}{i}", random.choice([1, 0, -1])))
        for i in range(random.randint(150, 400)):
            rows.append(_comment(ds, "xiaohongshu", f"xhs{ds}{i}", random.choice([1, 0, -1])))
        for i in range(random.randint(5, 20)):
            rows.append(_comment(ds, "ctrip", f"ct{ds}{i}", random.choice([1, 1, 0])))
        if ds != "20260909":                       # 快手 0909 一条都没有
            for i in range(random.randint(1, 30)):
                rows.append(_comment(ds, "kuaishou", f"ks{ds}{i}", random.choice([1, 0, -1])))
        # 同程：全程 0 条
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def bdc():
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 90
    return run(st, ["20260909", "20260910"], comments_df=_bdc_comments())


def test_bdc_every_channel_present_both_days(bdc):
    """六个渠道 × 两天 = 12 行，一行不少。"""
    assert bdc.errors == []
    p = bdc.tables[PLATFORM]
    assert len(p) == len(D.PLATFORM_CODES) * 2
    for ds in (20260909, 20260910):
        assert set(p[p.travel_date == ds]["platform_code"]) == set(D.PLATFORM_CODES)


def test_bdc_weibo_zero_day_backfills_from_previous_day(bdc):
    """微博 0910 当日 0 条 → 复制 0909 那一天的明细，就是 1 条。

    `bdc` 用的是默认口径（先补明细）。所以 comment_cnt 不再是「当天真实条数」，
    而是「补齐当天明细后的条数」—— 0909 正好只有 1 条。
    """
    p = bdc.tables[PLATFORM]
    w = p[(p.travel_date == 20260910) & (p.platform_code == "weibo")].iloc[0]
    assert w.comment_cnt == 1              # 不是 0，也不是「0909+0910 累计」
    assert w.comment_cnt_7d > 0 and w.comment_cnt_30d > 0 and w.comment_cnt_90d > 0


def test_bdc_kuaishou_zero_day_backfills_from_previous_day(bdc):
    """快手 0909 当日 0 条 → 复制 0908 的明细（1~30 条），不是 0 也不是累计。"""
    p = bdc.tables[PLATFORM]
    k = p[(p.travel_date == 20260909) & (p.platform_code == "kuaishou")].iloc[0]
    raw = _bdc_comments()
    d0908 = int(((raw.channel == "kuaishou")
                 & (raw.publish_time.str.startswith("2026-09-08"))).sum())
    assert d0908 > 0
    assert k.comment_cnt == d0908          # 就是 0908 那一天的数，一条不多
    assert k.comment_cnt_90d > 0


def test_bdc_total_fields_are_identical_across_channels(bdc):
    """`total_` 前缀 = 全平台口径，同一天每个渠道行携带同一份，这是设计不是 bug。

    对照组：不带 total_ 的同名字段是该渠道自己的，各行必须不同。
    """
    p = bdc.tables[PLATFORM]
    day = p[p.travel_date == 20260910]
    for col in ("total_good_rate_wow_1d", "week_total_good_rate_wow",
                "total_good_rate_wow_14d", "mon_total_good_rate_wow",
                "total_good_rate_1d", "week_total_good_rate"):
        assert day[col].nunique() == 1, f"{col} 是全平台口径，各行应相同"
    assert day["good_rate_wow_1d"].nunique() > 1      # 平台口径，各行不同
    assert day["week_good_rate"].nunique() > 1


def test_total_prefixed_fields_are_whole_platform(bdc):
    """`total_` 前缀 + `positive_*_count` 是**全平台**口径：

    · 同一天各渠道行上必须是同一个值（这是设计，不是重复写错）
    · 且必须等于各平台相加（这条才能证明它算的确实是全平台）

    命名是个坑：`positive_count` 没有 total_ 前缀，但 DDL 注释写的是
    「近1日(全平台)好评数」，它的平台版叫 `plat_positive_count`。
    """
    p = bdc.tables[PLATFORM]
    for ds in (20260909, 20260910):
        day = p[p.travel_date == ds]
        for w in D.PLATFORM_WINDOWS:
            tot, plat = D.PLAT_TOT_POS[w], D.PLAT_POS[w]
            assert day[tot].nunique() == 1, f"{tot} 应是全平台口径，各行相同"
            assert int(day[plat].sum()) == int(day[tot].iloc[0]), \
                f"{tot} 必须等于各平台 {plat} 之和"
        for f in (D.PLAT_TOT_RATE[w] for w in D.PLATFORM_WINDOWS):
            assert day[f].nunique() == 1, f"{f} 应是全平台口径，各行相同"
