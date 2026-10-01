# -*- coding: utf-8 -*-
"""窗口聚合单测：重点盯「缺采日」这个最容易错的地方。"""

import pandas as pd
import pytest

from engin_cli.windows import prev_window_dates, rolling_windows, window_dates


def _daily():
    # 20260105 缺采（源表里根本没有这一行）
    return pd.DataFrame({
        "k": ["a"] * 5,
        "ds": ["20260101", "20260102", "20260103", "20260104", "20260106"],
        "v": [10, 20, 30, 40, 60],
    })


def test_window_includes_anchor_day():
    """近 7 天 = [锚点-6, 锚点]，含锚点当天。"""
    d = window_dates("20260107", 7)
    assert d[0] == "20260101" and d[-1] == "20260107" and len(d) == 7


def test_prev_window_is_shifted_by_n():
    """上一周期 = 再往前推 N 天，与本期不重叠，也不留空档。"""
    cur, prev = window_dates("20260107", 7), prev_window_dates("20260107", 7)
    assert prev == window_dates("20251231", 7)
    assert prev[-1] == "20251231" and cur[0] == "20260101"
    assert not set(cur) & set(prev)


def test_missing_day_counts_as_zero_not_skipped():
    """缺采日必须以 0 参与窗口。

    如果直接对稀疏行做 rolling(3)，20260106 的 3 日窗口会取到
    0103+0104+0106 = 130；正确答案是把 0105 当 0，即 0104+0105+0106 = 100。
    """
    out = rolling_windows(_daily(), ["k"], "ds", ["v"], [3], with_prev=False)
    v = out[out.ds == "20260106"]["v_3d"].iloc[0]
    assert v == pytest.approx(100.0)


def test_prev_window_alignment():
    out = rolling_windows(_daily(), ["k"], "ds", ["v"], [2], with_prev=True)
    row = out[out.ds == "20260104"].iloc[0]
    assert row["v_2d"] == pytest.approx(70.0)        # 0103+0104
    assert row["v_prev_2d"] == pytest.approx(30.0)   # 0101+0102


def test_one_day_window_equals_daily_value():
    out = rolling_windows(_daily(), ["k"], "ds", ["v"], [1], with_prev=False)
    m = dict(zip(out.ds, out.v_1d))
    assert m["20260102"] == pytest.approx(20.0)
    assert m["20260105"] == pytest.approx(0.0)       # 缺采日补 0


def test_multiple_keys_do_not_bleed():
    df = pd.DataFrame({"k": ["a", "b", "a", "b"],
                       "ds": ["20260101", "20260101", "20260102", "20260102"],
                       "v": [1, 100, 2, 200]})
    out = rolling_windows(df, ["k"], "ds", ["v"], [7], with_prev=False)
    a = out[(out.k == "a") & (out.ds == "20260102")]["v_7d"].iloc[0]
    assert a == pytest.approx(3.0)


def test_calendar_clamps_instead_of_expanding():
    """离群日期不能把日历撑大。

    真实源表里混进一条 2013 年的评论，就能让日历从 400 天变成 4700 天；
    词表一大（上万个词），滚动面板直接爆内存。calendar 传了就以它为准。
    """
    df = pd.DataFrame({"k": ["a", "a"], "ds": ["20130101", "20260901"], "v": [999, 5]})
    out = rolling_windows(df, ["k"], "ds", ["v"], [7], with_prev=False,
                          calendar=("20260801", "20260901"))
    assert out.ds.min() == "20260801" and out.ds.max() == "20260901"
    assert len(out) == 32                       # 只有 8 月这一段，不是十几年
    assert out[out.ds == "20260901"]["v_7d"].iloc[0] == pytest.approx(5.0)  # 2013 那条被丢掉


def test_output_dates_filter_does_not_change_values():
    """先裁行再展开是纯性能优化，值必须与全量展开后再过滤完全一致。"""
    df = pd.DataFrame({"k": ["a"] * 10, "ds": [f"202608{d:02d}" for d in range(1, 11)],
                       "v": range(1, 11)})
    full = rolling_windows(df, ["k"], "ds", ["v"], [3, 7], with_prev=True)
    part = rolling_windows(df, ["k"], "ds", ["v"], [3, 7], with_prev=True,
                           output_dates=["20260810"])
    ref = full[full.ds == "20260810"].reset_index(drop=True)
    pd.testing.assert_frame_equal(part.reset_index(drop=True), ref)
