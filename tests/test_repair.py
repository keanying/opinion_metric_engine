# -*- coding: utf-8 -*-
"""历史分数修正的纯计算部分（不连库）。

重点验证两件事：
1. core 的历史分数**能只靠 core 表自己的计数列**重算出来 —— 这是它不受源表
   保留期限制的前提；
2. 只产出得分字段，不产出别的列 —— 修正动作不能顺手改到评论数/环比。
"""

import json

import pandas as pd
import pytest

import engin_cli.metric_calc_domain as D
from engin_cli.normalize import build_comment_facts, build_dimension_facts
from engin_cli.repair import (CORE_KEYS, CORE_SCORE_FIELDS, DIM_KEYS, DIM_SCORE_FIELDS,
                              diff_scores, recompute_core_scores,
                              recompute_dimension_scores)


def _core_history(days=10, start="20260801"):
    """造一段 core 表历史：每天 100 条，好/中/差 = 60/30/10。"""
    dates = pd.date_range(pd.to_datetime(start, format="%Y%m%d"), periods=days, freq="D")
    return pd.DataFrame({
        "scenic_spot_code": ["S1"] * days,
        "travel_date": [int(d.strftime("%Y%m%d")) for d in dates],
        "comment_count": [100] * days,
        "positive_count": [60] * days,
        "neutral_count": [30] * days,
        "negative_count": [10] * days,
    })


def test_core_scores_recomputed_from_counts_only():
    """只给计数列就能算出新分数，不需要源表。"""
    hist = _core_history()
    out = recompute_core_scores(hist)
    assert list(out.columns) == CORE_KEYS + CORE_SCORE_FIELDS
    expect = 5 * (0.6 * 1.0 + 0.3 * 0.9 + 0.1 * 0.5)     # = 4.60
    assert out["emotional_score"].iloc[-1] == pytest.approx(expect, abs=1e-4)
    assert out["emotional_score_7d"].iloc[-1] == pytest.approx(expect, abs=1e-4)


def test_core_repair_outputs_only_score_fields():
    """产出里不能出现 comment_count / positive_rate 之类 —— 修正只碰分数。"""
    out = recompute_core_scores(_core_history())
    extra = [c for c in out.columns if c not in CORE_KEYS + CORE_SCORE_FIELDS]
    assert extra == []


def test_core_window_needs_warmup_history():
    """7 日窗口要真的把前几天算进去：只有 3 天历史时 7d 只能覆盖这 3 天。"""
    hist = _core_history(days=3)
    hist.loc[hist.index[-1], ["positive_count", "neutral_count", "negative_count"]] = [0, 0, 100]
    out = recompute_core_scores(hist)
    last = out.iloc[-1]
    # 当日全差评 → 2.5；7 日窗口混合了前两天的好评 → 明显高于 2.5
    assert last["emotional_score"] == pytest.approx(2.5, abs=1e-4)
    assert last["emotional_score_7d"] > 3.0


def test_core_scores_respect_output_dates():
    hist = _core_history(days=10)
    out = recompute_core_scores(hist, output_dates=["20260810"])
    assert out["travel_date"].astype(str).tolist() == ["20260810"]


def _dim_comments(days=3, start="20260801"):
    rows = []
    dates = pd.date_range(pd.to_datetime(start, format="%Y%m%d"), periods=days, freq="D")
    for d in dates:
        for i in range(10):
            s = 1 if i < 6 else (0 if i < 9 else -1)
            rows.append({
                "scenic_id": "S1", "scenic_name": "景区1", "channel": "weibo",
                "work_id": "w1", "comment_id": f"c{d:%Y%m%d}_{i}",
                "comment_level": "level_1", "root_comment_id": "", "commenter_name": "u",
                "content": "x", "likes": 0,
                "sentiment_label": "正向", "sentiment_score": s,
                "dimension_tags": json.dumps(
                    [{"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光",
                      "sentiment": s}], ensure_ascii=False),
                "entity_tags": "[]", "keyword_tags": "[]",
                "publish_time": f"{d:%Y-%m-%d} 10:00:00",
            })
    return pd.DataFrame(rows)


def test_dimension_scores_recomputed_from_source():
    dim_facts = build_dimension_facts(build_comment_facts(_dim_comments()))
    out = recompute_dimension_scores(dim_facts, ["20260803"])
    assert list(out.columns) == DIM_KEYS + DIM_SCORE_FIELDS
    expect = 5 * (0.6 * 1.0 + 0.3 * 0.9 + 0.1 * 0.5)
    for lvl in (1, 2, 3):
        assert out[f"dimension_{lvl}_score"].iloc[0] == pytest.approx(expect, abs=1e-4)


def test_dimension_repair_outputs_only_score_fields():
    dim_facts = build_dimension_facts(build_comment_facts(_dim_comments()))
    out = recompute_dimension_scores(dim_facts, ["20260803"])
    extra = [c for c in out.columns if c not in DIM_KEYS + DIM_SCORE_FIELDS]
    assert extra == []


def test_diff_only_returns_changed_rows():
    hist = _core_history(days=5)
    new = recompute_core_scores(hist)

    old = hist[CORE_KEYS].copy()
    for f in CORE_SCORE_FIELDS:                     # 假装历史里存的是 v1 的分数
        old[f] = 7.0
    changed = diff_scores(old, new, CORE_KEYS, CORE_SCORE_FIELDS)
    assert len(changed) == 5

    same = new.copy()                               # 完全一致时应该一行都不返回
    assert diff_scores(same, new, CORE_KEYS, CORE_SCORE_FIELDS).empty


def test_repair_field_lists_match_requested_columns():
    """字段清单必须正好是需求点名的那些列，不多不少。"""
    assert CORE_SCORE_FIELDS == [
        "emotional_score", "emotional_score_7d", "emotional_score_30d",
        "emotional_score_60d", "emotional_score_90d", "emotional_score_365d"]
    assert len(DIM_SCORE_FIELDS) == 21
    for w in ("", "_7d", "_14d", "_30d", "_60d", "_90d", "_365d"):
        for lvl in (1, 2, 3):
            assert f"dimension_{lvl}_score{w}" in DIM_SCORE_FIELDS


# ============================================================
# 关键不变量：跑批 → 修正，结果必须一致（0 行变化）
# 两者不一致的话，repair 和 run 会互相把对方的值改回去，来回打架。
# ============================================================
@pytest.fixture(scope="module")
def pipeline_out():
    import subprocess, sys
    from pathlib import Path
    from engin_cli.pipeline import compute_load_range, run
    from engin_cli.settings import EtlSettings
    root = Path(__file__).resolve().parents[1]
    import tempfile
    out = Path(tempfile.mkdtemp())
    subprocess.run([sys.executable, str(root / "scripts" / "make_sample_data.py"),
                    "--days", "50", "--end-date", "20260901", "--out", str(out)],
                   check=True, cwd=str(root))
    comments = pd.read_csv(out / "comments.csv")
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 50
    # 目标日期铺满整段历史：线上 core 表本来就是每天跑批攒出来的，
    # repair 读的也是这张有完整历史的表，测试要还原这个前提
    all_dates = [d.strftime("%Y%m%d")
                 for d in pd.date_range("2026-07-14", "2026-09-01")]
    r = run(st, all_dates, comments_df=comments)
    # 跑批的日历 = (load_start, load_end)，repair 必须用同一段才对得上
    cal = compute_load_range(all_dates, st)
    return st, comments, r, all_dates[-3:], cal


def _raw_daily(comments):
    """源表 → 渠道粒度的当天真实日计数（未经补齐），与 repair_core 喂的入参一致。"""
    from engin_cli.normalize import build_comment_facts
    import engin_cli.metric_calc_domain as D
    return D.daily_core_facts_by_platform(build_comment_facts(comments))


def test_repair_after_run_changes_nothing_core(pipeline_out):
    """core：跑批写的分数 == 修正重算的分数，一行都不该变。

    补齐明细口径下重算必须回源表拿**当天真实计数** —— core 表里存的
    comment_count 已经是补齐后的值，再滚一次窗口口径就错了。
    """
    from engin_cli.repair import fill_start
    st, comments, r, targets, cal = pipeline_out
    core = r.tables["ads_trf_social_opinion_comment_core_di"]
    new = recompute_core_scores(_raw_daily(comments), output_dates=targets,
                                formula=st.score_formula, settings=st, calendar=cal,
                                fill_from=fill_start(st, cal[0]))
    changed = diff_scores(core, new, CORE_KEYS, CORE_SCORE_FIELDS)
    assert changed.empty, f"repair 与 run 不一致，{len(changed)} 行有差异"


def test_repair_after_run_changes_nothing_dimension(pipeline_out):
    """dimension：同上，按渠道补齐维度明细再滚动，不依赖 core。"""
    import engin_cli.metric_calc_domain as D
    from engin_cli.normalize import build_comment_facts, build_dimension_facts
    from engin_cli.repair import fill_start
    st, comments, r, targets, cal = pipeline_out
    dim = r.tables["ads_trf_social_opinion_comment_dimension_score_di"]
    dim = dim[dim.travel_date.astype(str).isin(targets)]

    cf = build_comment_facts(comments)
    dim_facts = build_dimension_facts(cf, st.unknown_dimension_policy)
    new = recompute_dimension_scores(dim_facts, targets, formula=st.score_formula,
                                     settings=st, calendar=cal,
                                     presence=D.daily_channel_presence(cf),
                                     fill_from=fill_start(st, cal[0]))
    changed = diff_scores(dim, new, DIM_KEYS, DIM_SCORE_FIELDS)
    assert changed.empty, f"repair 与 run 不一致，{len(changed)} 行有差异"


def test_feeding_backfilled_counts_back_in_is_wrong():
    """反面用例：把**补齐后**的计数当输入喂回去，结果就错了。

    这条测试存在的意义是把这个坑钉死：重算 core 必须回源表拿当天真实计数，
    不能图省事直接读 core 自己的计数列（模块注释里写的就是这件事）。

    造一段刻意稀疏的历史：8/01 有 10 条，之后一直到 8/16 一条都没有。
      · 真实明细下，7 日档（最多找 10 天）在 8/16 的窗口 [8/10, 8/16] 里
        只有 8/10、8/11 能找到 8/01 → 20 条
      · 喂 core 表存的值进去：1 日档已经把 8/02~8/06 补成了 10 条，
        这几天自己就「有数据」了 → 8/10~8/16 全都能找到 8/06 → 70 条，口径全错
    """
    from engin_cli.settings import EtlSettings
    import engin_cli.metric_calc_domain as D

    st = EtlSettings()
    st.lookback_days = 16
    cal = ("20260801", "20260816")
    days = [d.strftime("%Y%m%d") for d in pd.date_range("2026-08-01", "2026-08-16")]
    raw = pd.DataFrame([{"scenic_spot_code": "S1", "platform_code": "douyin",
                         "travel_date": t, "comment_count": 10 if t == "20260801" else 0,
                         "positive_count": 6 if t == "20260801" else 0,
                         "neutral_count": 2 if t == "20260801" else 0,
                         "negative_count": 2 if t == "20260801" else 0} for t in days])
    targets = ["20260814", "20260815", "20260816"]

    right = recompute_core_scores(raw, output_dates=targets,
                                  formula=st.score_formula, settings=st, calendar=cal)
    assert len(right) == 3

    # 模拟「core 表存的是补齐后的值」：1 日档把 8/02~8/06 补成了 10 条
    backfilled = raw.copy()
    m = backfilled.travel_date.between("20260802", "20260806")
    backfilled.loc[m, D.CORE_COUNT_FIELDS] = [10, 6, 2, 2]

    w = _window_counts(raw, backfilled, targets, st, cal)
    assert w["raw_7d"] == 20, w
    assert w["bf_7d"] == 70, ("喂补齐后的计数进去，7 日窗口的样本量应该跟真实口径不同", w)


def _window_counts(raw, backfilled, targets, st, cal):
    """把两种输入下 8/16 的 7 日窗口评论数捞出来对比（给上面那条反面用例用）。"""
    import engin_cli.metric_calc_domain as D
    from engin_cli.repair import _lookback
    from engin_cli.windows import rolling_windows

    keys = ["scenic_spot_code", "platform_code"]

    def cnt(daily):
        d = daily.copy()
        d["travel_date"] = d["travel_date"].astype(str)
        w = rolling_windows(d, keys, "travel_date",
                            D.CORE_COUNT_FIELDS, windows=[7], with_prev=False,
                            output_dates=[targets[-1]], calendar=cal,
                            fill_lookback=_lookback(st, [7]), fill_by=keys,
                            presence_col="comment_count")
        return int(w["comment_count_7d"].sum())

    return {"raw_7d": cnt(raw), "bf_7d": cnt(backfilled)}


def test_repair_warmup_follows_lookback_days():
    """修正的预热长度跟着 lookback_days 走，不能自作主张拉到 365 天 ——
    否则长窗口对不上跑批结果，repair 和 run 会来回改。"""
    import inspect
    from engin_cli import repair as R
    src = inspect.getsource(R.repair_core)
    assert "settings.lookback_days" in src
