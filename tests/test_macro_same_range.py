# -*- coding: utf-8 -*-
"""本期区间相同的两行，词云必须一样（客户对账）。

例：td@20261101 和 latest_1d@20261102 本期都是 20261101，统计的是同一批评论；
同理周一的 wtd 与当天 td、1 号的 mtd 与当天 td、1 号季度首日的 qtd 与当天 td。
以前词频并列时用「近期热度」排先后，热度窗口按 travel_date 截止，两行截止日不同 →
并列词的先后、谁进前 N 就不一样。现在热度窗口截止到本期最后一天，只跟本期区间有关。
"""

import json

import pandas as pd

from engin_cli import metric_calc_domain as D
from engin_cli.loader import TABLE_MACRO
from engin_cli.pipeline import run
from engin_cli.settings import EtlSettings


def _comment(cid, ds, word, platform="douyin"):
    return {
        "scenic_id": "S1", "scenic_name": "景区1", "channel": platform,
        "work_id": "w1", "comment_id": cid, "comment_level": "level_1",
        "root_comment_id": "", "commenter_name": "u", "content": word, "likes": 0,
        "sentiment_label": "正向", "sentiment_score": 1,
        "dimension_tags": json.dumps([{"dim1": "游玩体验", "dim2": "景色观赏",
                                       "dim3": "自然风光", "sentiment": 1}], ensure_ascii=False),
        "entity_tags": "[]", "location": "广东",
        "keyword_tags": json.dumps([word], ensure_ascii=False),
        "publish_time": f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 10:00:00",
    }


def _rows():
    rows = []
    for d in pd.date_range("2026-10-01", "2026-11-02"):
        ds = d.strftime("%Y%m%d")
        for p in ("douyin", "xiaohongshu"):
            rows.append(_comment(f"bg{p}{ds}", ds, "景色好", p))      # 每天都有，撑住日历
    # 11/01：三个词各 1 次（并列）；历史上「空气好」略多
    for i, w in enumerate(["环境好", "空气好", "人不多"]):
        rows.append(_comment(f"d1_{i}", "20261101", w))
    rows.append(_comment("h1", "20261020", "空气好"))
    # 11/02：「人不多」一下出现很多次 → 截止到 11/02 的热度里它最高
    for i in range(5):
        rows.append(_comment(f"d2_{i}", "20261102", "人不多"))
    return pd.DataFrame(rows)


def _macro(top_n=2):
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 60
    st.macro_wordcloud_top_n = top_n
    return run(st, ["20261101", "20261102"], comments_df=_rows()).tables[TABLE_MACRO]


def _cell(m, ch, t, g):
    r = m[(m["channel"] == ch) & (m["travel_date"] == int(t)) & (m["time_granularity"] == g)]
    assert len(r) == 1, (ch, t, g)
    return json.loads(r["wordcloud_map"].iloc[0])


def test_td_yesterday_equals_latest_1d_today_for_every_channel():
    m = _macro()
    for ch in m["channel"].unique():
        assert _cell(m, ch, "20261101", "td") == _cell(m, ch, "20261102", "latest_1d"), ch


def test_any_two_rows_with_the_same_period_have_the_same_wordcloud():
    """一般规律：同景区 × 渠道，本期区间相同的任意两行（不同日期 / 周期粒度）
    词云、热力图、评论数都一致。"""
    m = _macro()
    specs = {g["name"]: g for g in D.macro_granularities(None)}
    cols = ["wordcloud_map", "period_comment_heatmap", "comment_total",
            "positive_comment_cnt", "neutral_comment_cnt", "negative_comment_cnt"]
    seen, pairs = {}, 0
    for r in m.to_dict("records"):
        lo, hi = D.macro_period_ranges(specs[r["time_granularity"]], str(r["travel_date"]))["cur"]
        key = (r["scenic_id"], r["channel"], lo, hi)
        val = {c: r[c] for c in cols}
        if key in seen:
            pairs += 1
            assert seen[key][1] == val, (key, seen[key][0], (r["travel_date"], r["time_granularity"]))
        else:
            seen[key] = ((r["travel_date"], r["time_granularity"]), val)
    assert pairs > 0                                     # 确实有区间相同的行被比对过


def test_ties_are_still_broken_by_recent_heat_within_the_period():
    """并列时仍按近期热度排：截止到 11/01，「空气好」历史更多，排在并列词前面。"""
    wc = _cell(_macro(top_n=10), "douyin", "20261101", "td")
    words = [e["word"] for e in wc["positiveWord"]]
    tied = [w for w in words if w in ("环境好", "空气好", "人不多")]
    assert tied[0] == "空气好"
