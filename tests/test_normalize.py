# -*- coding: utf-8 -*-
"""标准化单测：脏数据兜底 + 拆解口径。"""

import json

import pandas as pd
import pytest

from engin_cli.normalize import (build_comment_facts, build_dimension_facts,
                                   build_keyword_facts, normalize_sentiment,
                                   parse_json_array)


def test_parse_json_array_handles_dirty_input():
    assert parse_json_array(None) == []
    assert parse_json_array("") == []
    assert parse_json_array("[]") == []
    assert parse_json_array('["太差"]') == ["太差"]
    assert parse_json_array("['太差','宰客']") == ["太差", "宰客"]      # 单引号
    assert parse_json_array('{"dim1":"游玩体验"}') == [{"dim1": "游玩体验"}]


def test_normalize_sentiment_prefers_score():
    assert normalize_sentiment(1, "负向") == 1        # score 优先于 label
    assert normalize_sentiment(-1, None) == -1
    assert normalize_sentiment(0, None) == 0
    assert normalize_sentiment(None, "正向") == 1     # score 缺失才看 label
    assert normalize_sentiment(None, None) == 0       # 都没有 → 中性，不丢数据


def _comments():
    return pd.DataFrame([
        {"scenic_id": "S1", "scenic_name": "景区1", "channel": "weibo",
         "work_id": "w1", "comment_id": "c1", "comment_level": "level_1",
         "root_comment_id": "c1", "commenter_name": "u", "content": "好",
         "likes": 1, "sentiment_label": "正向", "sentiment_score": 1,
         "dimension_tags": json.dumps([{"dim1": "游玩体验", "dim2": "景色观赏",
                                        "dim3": "自然风光", "sentiment": 1},
                                       {"dim1": "交通接驳", "dim2": "外部交通",
                                        "dim3": "停车场", "sentiment": -1}],
                                      ensure_ascii=False),
         "entity_tags": "[]",
         "keyword_tags": json.dumps(["鬼斧神工", "鬼斧神工", "凉爽"], ensure_ascii=False),
         "publish_time": "2026-09-01 10:00:00"},
        {"scenic_id": "S1", "scenic_name": "景区1", "channel": "ctrip",
         "work_id": "w2", "comment_id": "c2", "comment_level": "level_2",
         "root_comment_id": "c1", "commenter_name": "u2", "content": "差",
         "likes": 0, "sentiment_label": "负向", "sentiment_score": -1,
         "dimension_tags": "[]", "entity_tags": "[]", "keyword_tags": '["太差"]',
         "publish_time": "2026-09-01 11:00:00"},
        {"scenic_id": "S1", "scenic_name": "景区1", "channel": "weibo",
         "work_id": "w1", "comment_id": "c3", "comment_level": "level_1",
         "root_comment_id": "c3", "commenter_name": "u3", "content": "无时间",
         "likes": 0, "sentiment_label": "正向", "sentiment_score": 1,
         "dimension_tags": "[]", "entity_tags": "[]", "keyword_tags": "[]",
         "publish_time": None},
    ])


def test_sub_comment_counted_as_independent_row():
    """规则文档：主子评论均作为独立个体计数。level_2 不能被折叠掉。"""
    cf = build_comment_facts(_comments())
    assert len(cf) == 2                     # 第三条无 publish_time 被丢弃
    assert set(cf.comment_id) == {"c1", "c2"}
    assert cf.is_positive.sum() == 1 and cf.is_negative.sum() == 1


def test_multi_dimension_comment_explodes():
    """规则文档：多维度评论各维度独立计数。一条评论命中 2 个维度 → 2 行。"""
    cf = build_comment_facts(_comments())
    df = build_dimension_facts(cf)
    assert len(df) == 2
    # 维度情感取维度自己的，不跟随整条评论
    parking = df[df.dimension_level1 == "交通接驳"].iloc[0]
    assert parking.dim_sentiment == -1 and parking.is_negative == 1


def test_keyword_dedup_within_one_comment():
    """同一条评论里重复出现的词只算一次，否则词频会被单条评论刷爆。"""
    cf = build_comment_facts(_comments())
    kf = build_keyword_facts(cf)
    assert len(kf[kf.emotion_word == "鬼斧神工"]) == 1
    assert kf[kf.emotion_word == "太差"].iloc[0].emotion_type == "negative"
    assert kf[kf.emotion_word == "凉爽"].iloc[0].emotion_type == "positive"


def test_platform_name_mapped():
    cf = build_comment_facts(_comments())
    assert set(cf.platform_name) == {"微博", "携程"}
