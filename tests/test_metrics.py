# -*- coding: utf-8 -*-
"""公式层单测：直接拿规则文档的算式打靶。"""

import numpy as np
import pytest

from opinion_etl.metrics import (confidence, growth_rate, official_score,
                                 official_score_from_counts, safe_div, star_rating)


def test_score_neutral_when_balanced():
    # 好评率 == 差评率 → 净值 0 → 得分 5
    assert official_score_from_counts(50, 50, 200) == pytest.approx(5.0)


def test_score_full_positive():
    # 全好评且样本量 >= T → 5 + 1*5*1 = 10
    assert official_score_from_counts(200, 0, 200) == pytest.approx(10.0)


def test_score_full_negative():
    assert official_score_from_counts(0, 200, 200) == pytest.approx(0.0)


def test_confidence_weight_pulls_to_neutral():
    """样本量不足 T=100 时得分向中性收敛，这是规则文档要求的行为。"""
    # 全好评但只有 10 条：5 + 1 × 5 × 0.1 = 5.5
    assert official_score_from_counts(10, 0, 10) == pytest.approx(5.5)
    assert confidence(10) == pytest.approx(0.1)
    assert confidence(500) == pytest.approx(1.0)


def test_score_matches_spec_expansion():
    """5 + [(正面总分/总数)+(负面总分/总数)]×5×min(1,总数/T)，负面总分 = -1×负面数"""
    pos, neg, total = 620, 180, 1000
    spec = 5 + ((pos / total) + (-1 * neg / total)) * 5 * min(1, total / 100)
    assert official_score_from_counts(pos, neg, total) == pytest.approx(spec, abs=1e-4)


def test_score_empty_is_neutral_not_zero():
    """没有评论 != 评价很差。返回 0 会让空数据景区在榜单垫底。"""
    assert official_score_from_counts(0, 0, 0) == pytest.approx(5.0)


def test_score_bounded():
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
