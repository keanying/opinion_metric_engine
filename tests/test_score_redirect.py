# -*- coding: utf-8 -*-
"""插件：得分重定向 score_redirect（规则见 engin_cli/plugins/score_redirect.py）＋插件机制本身。"""

import json

import pandas as pd
import pytest

import engin_cli.cli as cli
from engin_cli.loader import TABLE_MACRO
from engin_cli.pipeline import run
from engin_cli.plugins import describe, parse_names, registry
from engin_cli.plugins.score_redirect import apply_macro, derive_total, process
from engin_cli.service.runner import build_run_argv, normalize_config
from engin_cli.settings import EtlSettings
from engin_cli import metric_calc_domain as D

# MD5("2026-11-12") 偶数 → 4.8；MD5("2026-11-10") 奇数 → 4.9
EVEN, ODD = "20261112", "20261110"


def test_derive_total_is_stable_md5_parity():
    assert derive_total(EVEN) == 4.8 and derive_total(ODD) == 4.9
    assert derive_total(pd.Timestamp("2026-11-12")) == 4.8          # 日期类型不影响结果
    assert {derive_total(EVEN) for _ in range(5)} == {4.8}


@pytest.mark.parametrize("total, scores, day, want_total, want_scores", [
    # 总分已 >= 4.8：不处理
    (4.8, [3.0, 4.0, 4.5, 4.6, 4.7, 5.0], EVEN, 4.8, [3.0, 4.0, 4.5, 4.6, 4.7, 5.0]),
    # 维度分全相等：全部 = 新总分
    (4.5, [4.5] * 6, ODD, 4.9, [4.9] * 6),
    # 最高维度分 > 新总分：只改总分
    (4.2, [5.0, 2.5, 4.0, 4.1, 4.3, 4.4], EVEN, 4.8, [5.0, 2.5, 4.0, 4.1, 4.3, 4.4]),
    # 最高维度分 <= 新总分：全体 + (4.8 - 4.5 + 0.1) = +0.4
    (4.1, [4.5, 4.2, 3.0, 4.0, 4.4, 4.1], EVEN, 4.8, [4.9, 4.6, 3.4, 4.4, 4.8, 4.5]),
    # 平移超过 5 的按 5：4.9 - 4.85 + 0.1 = +0.15
    (3.0, [4.85, 4.8, 3.0, 2.5, 2.5, 2.5], ODD, 4.9, [5.0, 4.95, 3.15, 2.65, 2.65, 2.65]),
])
def test_process_rules(total, scores, day, want_total, want_scores):
    t, s = process(total, scores, day)
    assert t == want_total and s == want_scores          # 6 位小数，没有 4.8999999 这种


def _comment(cid, ds, senti, platform="douyin"):
    lab = {1: "正向", 0: "中性", -1: "负向"}[senti]
    return {
        "scenic_id": "S1", "scenic_name": "景区1", "channel": platform,
        "work_id": "w1", "comment_id": cid, "comment_level": "level_1",
        "root_comment_id": "", "commenter_name": "u", "content": "内容", "likes": 0,
        "sentiment_label": lab, "sentiment_score": senti,
        "dimension_tags": json.dumps([{"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光",
                                       "sentiment": senti}], ensure_ascii=False),
        "entity_tags": "[]", "location": "广东", "keyword_tags": "[]",
        "publish_time": f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 10:00:00",
    }


def _rows():
    rows = []
    for i, d in enumerate(pd.date_range("2026-10-01", "2026-11-12")):
        ds = d.strftime("%Y%m%d")
        rows.append(_comment(f"p{ds}", ds, 1))
        rows.append(_comment(f"n{ds}", ds, -1, platform="xiaohongshu" if i % 2 else "douyin"))
    return pd.DataFrame(rows)


def _macro(on):
    st = EtlSettings()
    st.write_db = False
    st.lookback_days = 60
    st.plugins = ["score_redirect"] if on else []
    res = run(st, ["20261110", "20261111", "20261112"], comments_df=_rows())
    assert not res.errors, res.errors
    return res, res.tables[TABLE_MACRO]


def test_pipeline_redirects_macro_after_quality_check():
    raw_res, raw = _macro(False)
    res, red = _macro(True)
    assert (raw.overall_sentiment_score < 4.8).any()                 # 原始数据有低于 4.8 的
    assert (red.overall_sentiment_score >= 4.8).all()
    assert (red.pre_overall_sentiment_score >= 4.8).all()
    assert any(n.startswith("插件「得分重定向」：大盘表") for n in res.notes)
    assert not any("得分重定向" in n for n in raw_res.notes)
    # 其余列不动
    other = [c for c in raw.columns if c not in (
        "overall_sentiment_score", "pre_overall_sentiment_score", "dimension_breakdown", "etl_time")]
    pd.testing.assert_frame_equal(raw[other].reset_index(drop=True), red[other].reset_index(drop=True))
    # 每一行都等于逐行按规则算的结果；日期取周期区间最后一天
    specs = {s["name"]: s for s in D.macro_granularities(None)}
    for r0, r1 in zip(raw.itertuples(), red.itertuples()):
        rg = D.macro_period_ranges(specs[r0.time_granularity], str(r0.travel_date))
        d0, d1 = json.loads(r0.dimension_breakdown), json.loads(r1.dimension_breakdown)
        t, s = process(r0.overall_sentiment_score, [d["dimension1Score"] for d in d0], rg["cur"][1])
        p, ps = process(r0.pre_overall_sentiment_score, [d["preDimension1Score"] for d in d0], rg["prev"][1])
        assert r1.overall_sentiment_score == t and [d["dimension1Score"] for d in d1] == s
        assert r1.pre_overall_sentiment_score == p and [d["preDimension1Score"] for d in d1] == ps


def test_same_range_rows_stay_equal():
    """区间相同的行重定向后仍然一样：今日@11-11 = 近1日@11-12；上期分 = 上期那天的本期分。"""
    _, m = _macro(True)
    k = lambda d, g, c="all": m[(m.travel_date.astype(str) == d) & (m.time_granularity == g)
                                & (m.channel == c)].iloc[0]
    for c in m.channel.unique():
        a, b = k("20261111", "td", c), k("20261112", "latest_1d", c)
        assert a.overall_sentiment_score == b.overall_sentiment_score
        assert a.dimension_breakdown == b.dimension_breakdown
        assert k("20261112", "td", c).pre_overall_sentiment_score == a.overall_sentiment_score


def test_apply_macro_empty():
    assert apply_macro(pd.DataFrame(), [])[1] == 0


def test_plugin_registry_and_names():
    assert "score_redirect" in registry()
    assert {"name": "score_redirect", "label": "得分重定向"}.items() <= describe()[0].items()
    assert parse_names("score_redirect, score_redirect,") == ["score_redirect"]
    assert parse_names(["score_redirect"]) == ["score_redirect"] and parse_names(None) == []
    with pytest.raises(ValueError, match="没有这个插件：nope"):
        parse_names("nope")


def test_cli_flag_and_task_argv():
    p = cli.build_parser()
    assert p.parse_args(["run", "--plugins", "score_redirect"]).plugins == ["score_redirect"]
    assert p.parse_args(["run", "--no-plugins"]).no_plugins
    with pytest.raises(SystemExit):
        p.parse_args(["run", "--plugins", "nope"])
    assert EtlSettings().plugins == []
    assert normalize_config({})["plugins"] == []
    with pytest.raises(ValueError):
        normalize_config({"plugins": ["nope"]})
    on = build_run_argv({"plugins": ["score_redirect"]})[0]
    assert on[on.index("--plugins") + 1] == "score_redirect"
    assert "--plugins" not in build_run_argv({})[0]
