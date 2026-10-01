# -*- coding: utf-8 -*-
"""指标口径域单测：直接拿规则文档的算式打靶。

口径全部落在 engin_cli/metric_calc_domain.py，所以断言也只对着那一个文件写。
"""

import numpy as np
import pytest

from engin_cli.metric_calc_domain import (SCORE_EMPTY, SCORE_MAX, confidence,
                                          growth_rate, judge_sentiment, official_score,
                                          official_score_from_counts, ratio, safe_div,
                                          sentiment_score_from_counts, star_rating,
                                          weighted_score_from_counts, word_surge)


def test_v1_score_neutral_when_balanced():
    # 好评率 == 差评率 → 净值 0 → 得分 5
    assert official_score_from_counts(50, 50, 200) == pytest.approx(5.0)


def test_v1_score_full_positive():
    # 全好评且样本量 >= T → 5 + 1*5*1 = 10
    assert official_score_from_counts(200, 0, 200) == pytest.approx(10.0)


def test_v1_score_full_negative():
    assert official_score_from_counts(0, 200, 200) == pytest.approx(0.0)


def test_v1_confidence_weight_pulls_to_neutral():
    """样本量不足 T=100 时得分向中性收敛，这是规则文档要求的行为。"""
    # 全好评但只有 10 条：5 + 1 × 5 × 0.1 = 5.5
    assert official_score_from_counts(10, 0, 10) == pytest.approx(5.5)
    assert confidence(10) == pytest.approx(0.1)
    assert confidence(500) == pytest.approx(1.0)


def test_v1_score_matches_spec_expansion():
    """5 + [(正面总分/总数)+(负面总分/总数)]×5×min(1,总数/T)，负面总分 = -1×负面数"""
    pos, neg, total = 620, 180, 1000
    spec = 5 + ((pos / total) + (-1 * neg / total)) * 5 * min(1, total / 100)
    assert official_score_from_counts(pos, neg, total) == pytest.approx(spec, abs=1e-4)


def test_v1_score_empty_is_neutral_not_zero():
    """没有评论 != 评价很差。返回 0 会让空数据景区在榜单垫底。"""
    assert official_score_from_counts(0, 0, 0) == pytest.approx(5.0)


def test_v1_score_bounded():
    v = official_score(np.array([-3.0, 3.0]), np.array([1000, 1000]))
    assert v.min() >= 0 and v.max() <= 10


def test_growth_rate():
    assert growth_rate(120, 100) == pytest.approx(0.2)
    assert growth_rate(80, 100) == pytest.approx(-0.2)


def test_growth_rate_zero_base_returns_default_not_inf():
    """上期为 0 时不能返回 inf —— decimal(10,4) 存不下，也没有业务含义。"""
    v = growth_rate(50, 0)
    assert np.isfinite(v) and v == 0.0


def test_safe_div():
    assert safe_div(1, 0, default=0.0) == 0.0
    assert safe_div(10, 4) == pytest.approx(2.5)


def test_star_rating_range():
    assert star_rating(1, 0, 0, 1) == pytest.approx(5.0)
    assert star_rating(0, 0, 1, 1) == pytest.approx(1.0)
    assert star_rating(1, 1, 1, 3) == pytest.approx(3.0)


def test_ratio_zero_denominator():
    """占比的分母为 0 时取 default，不产生 NaN/inf 写进 decimal 列。"""
    assert ratio(3, 0) == pytest.approx(0.0)
    assert ratio(3, 12) == pytest.approx(0.25)


def test_judge_sentiment_is_single_source_of_truth():
    """好评/中评/差评的判定只有这一处口径。"""
    assert judge_sentiment(1, "负向") == 1        # score 优先于 label
    assert judge_sentiment(None, "差评") == -1     # score 缺失才看 label
    assert judge_sentiment(None, "偏正向") == 1    # 包含匹配兜底
    assert judge_sentiment(None, None) == 0        # 都拿不到 → 中性，不丢数据


def test_word_surge():
    """突增量 = 本期 - 上期；上期为 0 记为「新增」(inf)，由展示层标注。"""
    delta, rate = word_surge(np.array([30.0, 12.0]), np.array([20.0, 0.0]))
    assert delta.tolist() == [10.0, 12.0]
    assert rate[0] == pytest.approx(0.5)
    assert np.isinf(rate[1])


# ============================================================
# v2 现行口径：S = 5 × (好评率×1.0 + 中评率×0.9 + 差评率×0.5)
# ============================================================
def test_v2_all_positive_is_five():
    """全好评 = 5×1.0 = 5，就是满分。"""
    assert weighted_score_from_counts(100, 0, 0, 100) == pytest.approx(5.0)


def test_v2_all_neutral():
    """全中评 = 5×0.9 = 4.5。"""
    assert weighted_score_from_counts(0, 100, 0, 100) == pytest.approx(4.5)


def test_v2_all_negative_is_floor_not_zero():
    """全差评 = 5×0.5 = 2.5，是底分不是 0 —— 差评权重给底分就是为了不被击穿。"""
    assert weighted_score_from_counts(0, 0, 100, 100) == pytest.approx(2.5)


def test_v2_matches_formula():
    """逐字对着公式打靶：S = 5 × (好评率×1.0 + 中评率×0.9 + 差评率×0.5)。"""
    pos, neu, neg = 620, 180, 200
    total = pos + neu + neg
    spec = 5 * (pos / total * 1.0 + neu / total * 0.9 + neg / total * 0.5)
    assert weighted_score_from_counts(pos, neu, neg, total) == pytest.approx(spec, abs=1e-4)


def test_v2_never_exceeds_five():
    """不会超过 5。即使计数脏了（分子大于分母）也截断在 5。"""
    assert weighted_score_from_counts(200, 0, 0, 100) == pytest.approx(SCORE_MAX)
    assert SCORE_MAX == 5.0


def test_v2_range_is_bounded_by_weights():
    """三个占比之和恒为 1，所以得分天然落在 [2.5, 5]。"""
    for pos, neu, neg in [(1, 0, 0), (0, 1, 0), (0, 0, 1), (3, 5, 2), (7, 1, 2)]:
        total = pos + neu + neg
        v = weighted_score_from_counts(pos, neu, neg, total)
        assert 2.5 - 1e-9 <= v <= 5.0 + 1e-9


def test_v2_no_confidence_weight():
    """v2 没有置信度：10 条全好评和 1000 条全好评都是 5 分。

    小样本的问题交给缺数回补，不再在公式里对样本量打折。
    """
    assert weighted_score_from_counts(10, 0, 0, 10) == pytest.approx(5.0)
    assert weighted_score_from_counts(1000, 0, 0, 1000) == pytest.approx(5.0)


def test_v2_empty_is_neutral_score():
    """没有评论 → 4.5（等价于全部按中评计），不是 0。"""
    assert weighted_score_from_counts(0, 0, 0, 0) == pytest.approx(SCORE_EMPTY)
    assert SCORE_EMPTY == pytest.approx(4.5)


def test_dispatcher_selects_formula():
    """分发器：默认走 v2，显式指定才回落 v1。"""
    v2 = sentiment_score_from_counts(80, 10, 10, 100)
    v1 = sentiment_score_from_counts(80, 10, 10, 100, formula="confidence_v1")
    assert v2 == pytest.approx(5 * (0.8 + 0.1 * 0.9 + 0.1 * 0.5))
    assert v1 == pytest.approx(5 + (0.8 - 0.1) * 5)          # v1 值域 [0,10]
    with pytest.raises(ValueError):
        sentiment_score_from_counts(1, 1, 1, 3, formula="不存在的口径")


def test_dimension_and_overall_share_one_formula():
    """维度得分与综合得分是同一个函数，只是样本范围不同。"""
    overall = sentiment_score_from_counts(60, 30, 10, 100)     # 全部评论
    dim = sentiment_score_from_counts(6, 3, 1, 10)             # 某维度下的提及
    assert overall == pytest.approx(dim)                       # 占比相同 → 得分相同
