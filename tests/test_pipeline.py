# -*- coding: utf-8 -*-
"""端到端：构造源数据 → 跑全链路 → 校验四张表的恒等关系。"""

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from engin_cli.pipeline import run
from engin_cli.settings import EtlSettings
from engin_cli.validate import validate_all

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    out = tmp_path_factory.mktemp("sample")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample_data.py"),
                    "--days", "60", "--end-date", "20260901", "--out", str(out)],
                   check=True, cwd=str(ROOT))
    return (pd.read_csv(out / "comments.csv"), pd.read_csv(out / "works.csv"))


@pytest.fixture(scope="module")
def result(sample, tmp_path_factory):
    comments, works = sample
    st = EtlSettings()
    st.write_db = False
    st.write_csv = False
    st.lookback_days = 60
    return run(st, ["20260830", "20260831", "20260901"],
               comments_df=comments, works_df=works)


def test_pipeline_runs_clean(result):
    assert result.ok, result.errors
    assert not result.errors


def test_all_four_tables_produced(result):
    for t, df in result.tables.items():
        assert not df.empty, f"{t} 为空"


def test_validation_assertions(result):
    errs = validate_all({
        "core": result.tables["ads_trf_social_opinion_comment_core_di"],
        "platform": result.tables["ads_trf_social_opinion_comment_platform_di"],
        "dimension": result.tables["ads_trf_social_opinion_comment_dimension_score_di"],
        "content": result.tables["ads_trf_social_opinion_comment_content_di"],
    })
    assert errs == []


def test_core_counts_add_up(result):
    core = result.tables["ads_trf_social_opinion_comment_core_di"]
    assert (core.positive_count + core.neutral_count + core.negative_count
            == core.comment_count).all()


def test_platform_sums_to_core(result):
    """平台之和 == core 总数。这条挂了说明分组或去重错了。"""
    core = result.tables["ads_trf_social_opinion_comment_core_di"]
    plat = result.tables["ads_trf_social_opinion_comment_platform_di"]
    agg = plat.groupby(["scenic_spot_code", "travel_date"])["comment_cnt"].sum()
    ref = core.set_index(["scenic_spot_code", "travel_date"])["comment_count"]
    assert (agg.sort_index() == ref.sort_index()).all()


def test_dimension_hierarchy_consistent(result):
    """同一 (景区,日期,一级维度) 下 dimension_1_score 必须唯一 —— BI 下钻要对得上账。"""
    dim = result.tables["ads_trf_social_opinion_comment_dimension_score_di"]
    g = dim.groupby(["scenic_spot_code", "travel_date", "dimension_level1"])[
        "dimension_1_score"].nunique()
    assert (g == 1).all()


def test_content_unique_key(result):
    """(travel_date, scenic_spot_code, emotion_word) 必须唯一，否则撞库里的唯一索引。"""
    c = result.tables["ads_trf_social_opinion_comment_content_di"]
    assert not c.duplicated(subset=["scenic_spot_code", "travel_date",
                                    "emotion_word"]).any()


def test_content_rate_matches_value_over_comments(result):
    """热度占比 = 近 N 日词频 / 近 N 日总评论数，两表分母必须同源。"""
    core = result.tables["ads_trf_social_opinion_comment_core_di"]
    c = result.tables["ads_trf_social_opinion_comment_content_di"]
    m = c.merge(core[["scenic_spot_code", "travel_date", "comment_count"]],
                on=["scenic_spot_code", "travel_date"])
    calc = m.emotion_value / m.comment_count
    assert (calc - m.emotion_rate_1d).abs().max() < 1e-5


def test_drill_analysis_links_back_to_work(result):
    """5.2：每行都要能跳转，work_url 缺失率不能高。"""
    d = result.tables["ads_trf_social_opinion_drill_analysis_di"]
    assert (d.work_url.astype(str).str.len() > 0).mean() > 0.95
    assert not d.duplicated(subset=["detail_uk"]).any()


def test_drill_analysis_words_exist_in_content_table(result):
    """下钻表的词必须都能在内容表里找到，否则点了词查不到明细。

    注意两张表的景区列名不一样：下钻表是 scenic_id，内容表是 scenic_spot_code，
    所以关联时要先对齐（这正是 test_schema 里那两条断言盯着的差异）。
    """
    c = result.tables["ads_trf_social_opinion_comment_content_di"]
    d = result.tables["ads_trf_social_opinion_drill_analysis_di"]
    # 没有关键词的评论那一行（空词，word_source=none）只为对账评论数，不是词
    d = d[d.word_source == "keyword"]
    left = (d[["scenic_id", "travel_date", "emotion_word"]]
            .rename(columns={"scenic_id": "scenic_spot_code"}).drop_duplicates())
    right = c[["scenic_spot_code", "travel_date", "emotion_word"]].drop_duplicates()
    missing = left.merge(right, how="left", indicator=True)
    assert (missing._merge == "both").all()


def test_scores_within_bounds(result):
    """v2 口径下得分值域是 [2.5, 5]，且**不会超过 5**。"""
    core = result.tables["ads_trf_social_opinion_comment_core_di"]
    dim = result.tables["ads_trf_social_opinion_comment_dimension_score_di"]
    for c in [x for x in core.columns if x.startswith("emotional_score")]:
        assert core[c].between(2.5, 5.0).all(), f"{c} 越界"
    for c in [x for x in dim.columns if "score" in x]:
        assert dim[c].between(2.5, 5.0).all(), f"{c} 越界"


def test_core_score_matches_new_formula(result):
    """core 的 7 日得分逐行对拍新公式（7 日档不走回补，可以严格相等）。"""
    import engin_cli.metric_calc_domain as D
    core = result.tables["ads_trf_social_opinion_comment_core_di"]
    calc = D.weighted_score_from_counts(
        core.positive_rate_7d, core.neutral_rate_7d, core.negative_rate_7d, 1.0)
    assert (core.emotional_score_7d - calc).abs().max() < 0.01


def test_dimension_score_uses_same_formula_as_overall(result):
    """维度得分与综合得分同一个公式：占比相同的样本必须得到相同的分。"""
    import engin_cli.metric_calc_domain as D
    a = D.sentiment_score_from_counts(60, 30, 10, 100)
    b = D.sentiment_score_from_counts(600, 300, 100, 1000)
    assert a == b


def test_backfill_kicks_in_on_sparse_day(sample):
    """需求六.1：当日数据极少时应向前顺延取数，而不是算出一个 0/100% 的极端比率。"""
    comments, works = sample
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 60
    st.min_daily_comments = 10 ** 6      # 强制所有行都触发回补
    st.max_backfill_days = 7
    r = run(st, ["20260901"], comments_df=comments, works_df=works)
    core = r.tables["ads_trf_social_opinion_comment_core_di"]
    # 计数事实不受回补影响（默认 backfill_apply_to_counts=False）
    st2 = EtlSettings()
    st2.write_db = st2.write_csv = False
    st2.lookback_days = 60
    st2.enable_backfill = False
    r2 = run(st2, ["20260901"], comments_df=comments, works_df=works)
    core2 = r2.tables["ads_trf_social_opinion_comment_core_di"]
    assert (core.sort_values("scenic_spot_code").comment_count.tolist()
            == core2.sort_values("scenic_spot_code").comment_count.tolist())
