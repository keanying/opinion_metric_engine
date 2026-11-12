# -*- coding: utf-8 -*-
"""评论内容为空（NULL / 空串 / 只有空白，只发了图片、视频）的评论不参与任何统计。"""

import json

import pandas as pd

from engin_cli.loader import TABLE_CORE, TABLE_DRILL_ANALYSIS, TABLE_MACRO
from engin_cli.normalize import build_comment_facts, empty_content_mask
from engin_cli.pipeline import run
from engin_cli.settings import EtlSettings


def _c(cid, content, senti=1):
    return {
        "scenic_id": "S1", "scenic_name": "景区1", "channel": "douyin", "work_id": "w1",
        "comment_id": cid, "comment_level": "level_1", "root_comment_id": "", "commenter_name": "u",
        "content": content, "likes": 0, "sentiment_label": {1: "正向", -1: "负向"}[senti],
        "sentiment_score": senti,
        "dimension_tags": json.dumps([{"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光",
                                       "sentiment": senti}], ensure_ascii=False),
        "entity_tags": "[]", "location": "广东", "keyword_tags": json.dumps(["好玩"], ensure_ascii=False),
        "publish_time": "2026-11-12 10:00:00",
    }


ROWS = [_c("ok1", "很好玩"), _c("ok2", "好玩"),
        _c("img", None, -1), _c("vid", "", -1), _c("blank", "  ", -1)]


def test_mask_and_facts():
    df = pd.DataFrame(ROWS)
    assert empty_content_mask(df).tolist() == [False, False, True, True, True]
    assert sorted(build_comment_facts(df)["comment_id"]) == ["ok1", "ok2"]
    assert not empty_content_mask(pd.DataFrame(ROWS).drop(columns="content")).any()   # 没有 content 列：不剔除


def test_pipeline_excludes_empty_content():
    st = EtlSettings()
    st.write_db = False
    st.lookback_days = 30
    res = run(st, ["20261112"], comments_df=pd.DataFrame(ROWS))
    assert not res.errors, res.errors
    assert any("评论内容为空" in n and "3 条不参与统计" in n for n in res.notes)
    core = res.tables[TABLE_CORE]
    day = core[core.travel_date.astype(str) == "20261112"].iloc[0]
    assert day.comment_count == 2 and day.negative_count == 0
    m = res.tables[TABLE_MACRO]
    td = m[(m.time_granularity == "td") & (m.channel == "all")].iloc[0]
    assert td.comment_total == 2 and td.negative_comment_cnt == 0
    assert set(res.tables[TABLE_DRILL_ANALYSIS]["comment_id"]) == {"ok1", "ok2"}
