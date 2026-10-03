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


# ══════════════════════════════════════════════════════════════════
# 先补明细，再滚动（domain §1.8）
# ══════════════════════════════════════════════════════════════════
def test_fill_source_index_stops_at_the_nearest_day_and_respects_lookback():
    import numpy as np
    from engin_cli.windows import fill_source_index
    present = np.array([[True], [False], [False], [False], [True], [False]])
    assert fill_source_index(present, 2)[:, 0].tolist() == [0, 0, 0, -1, 4, 4]
    assert fill_source_index(present, 0)[:, 0].tolist() == [0, -1, -1, -1, 4, -1]


def test_rolling_windows_fills_each_day_then_sums():
    """快手 9/10 有 3 条、9/11~9/13 没有 → 每天复制 9/10；近 7 日 = 3 + 3×3。"""
    import pandas as pd
    from engin_cli.windows import rolling_windows
    keys = ["scenic_spot_code", "platform_code"]
    daily = pd.DataFrame({"scenic_spot_code": ["S1"], "platform_code": ["kuaishou"],
                          "travel_date": ["20260910"], "n": [3]})
    out = rolling_windows(daily, keys, "travel_date", ["n"], windows=[1, 7],
                          calendar=("20260901", "20260913"),
                          fill_lookback={1: 5, 7: 10}, fill_by=keys, presence_col="n")
    out = out.set_index("travel_date")
    assert out.loc[["20260911", "20260912", "20260913"], "n_1d"].tolist() == [3, 3, 3]
    assert out.loc["20260913", "n_7d"] == 12
    assert out.loc["20260913", "raw_n_7d"] == 3          # 行存在规则看补齐前的真实值
    assert out.loc["20260912", "n_prev_1d"] == 3         # 上一周期也来自补齐后的明细
    assert out.loc["20260909", "n_1d"] == 0              # 9/10 之前没有来源，补不出来


def test_rolling_windows_fill_uses_presence_not_own_value():
    """渠道当天有评论但这个词没出现 → 词就是 0，不去复制前一天的词。"""
    import pandas as pd
    from engin_cli.windows import rolling_windows
    keys = ["scenic_spot_code", "platform_code", "word"]
    by = ["scenic_spot_code", "platform_code"]
    daily = pd.DataFrame({"scenic_spot_code": ["S1"], "platform_code": ["douyin"],
                          "word": ["排队"], "travel_date": ["20260910"], "c": [2]})
    presence = pd.DataFrame({"scenic_spot_code": ["S1", "S1"],
                             "platform_code": ["douyin", "douyin"],
                             "travel_date": ["20260910", "20260911"], "present_cnt": [5, 4]})
    out = rolling_windows(daily, keys, "travel_date", ["c"], windows=[1],
                          calendar=("20260910", "20260912"), fill_lookback={1: 5},
                          fill_by=by, presence=presence, with_prev=False)
    got = dict(zip(out.travel_date, out.c_1d))
    assert got == {"20260910": 2, "20260911": 0, "20260912": 0}
