# -*- coding: utf-8 -*-
"""下钻明细 content_snippet 掩码（domain 第 9 章）。

业务方原话：

    content_snippet = 我是体力一般，来回两个半小时左右
    emotion_word   = 体力一般,来回两个半小时
    最终
    content_snippet = **体力一般**来回两个半小时**
    就除关键字外的其他内容，隐藏掉

这个文件把这条规则钉死，连同它的边界情况。
"""

import json

import pandas as pd
import pytest

import engin_cli.metric_calc_domain as D
from engin_cli.metric_calc_domain import mask_content
from engin_cli.pipeline import run
from engin_cli.settings import EtlSettings

DRILL = "ads_trf_social_opinion_drill_analysis_di"


# ══════════════════════════════════════════════════════════════════
# 业务方给的那个例子，一比一还原
# ══════════════════════════════════════════════════════════════════
def test_the_exact_example():
    got, hit = mask_content("我是体力一般，来回两个半小时左右",
                            ["体力一般", "来回两个半小时"])
    assert got == "**体力一般**来回两个半小时**"
    assert hit == 2


def test_hidden_runs_become_a_fixed_token_not_one_star_per_char():
    """每段隐藏内容换成**一个固定掩码串**，不是按字数出星号。

    按字数出星号会泄露原文长度 —— 拼上关键词的位置，能还原出不少信息。
    例子里「我是」2 字、「，」1 字、「左右」2 字，掩码后都是同样的 `**`。
    """
    got, _ = mask_content("我是体力一般，来回两个半小时左右",
                          ["体力一般", "来回两个半小时"])
    assert got.count("*") == 6                  # 3 段 × 2 个星号，与原文字数无关
    assert "我是" not in got and "左右" not in got and "，" not in got


def test_char_mode_shows_length():
    """mode="char" 时每个隐藏字符出一个掩码字符（要长度信息时才用）。"""
    got, _ = mask_content("我是体力一般，来回两个半小时左右",
                          ["体力一般", "来回两个半小时"], mode=D.DRILL_MASK_MODE_CHAR)
    assert got == "**体力一般*来回两个半小时**"      # 「，」只有 1 个字符 → 1 个星号


# ══════════════════════════════════════════════════════════════════
# 边界
# ══════════════════════════════════════════════════════════════════
def test_keyword_at_both_ends_leaves_no_stray_mask():
    """关键词顶到头尾时，前后不该凭空多出掩码段。"""
    assert mask_content("体力一般来回", ["体力一般"])[0] == "体力一般**"
    assert mask_content("我是体力一般", ["体力一般"])[0] == "**体力一般"
    assert mask_content("体力一般", ["体力一般"])[0] == "体力一般"


def test_adjacent_keywords_do_not_get_a_mask_between_them():
    """两个关键词紧挨着，中间没有内容可隐藏，就不该插掩码。"""
    assert mask_content("体力一般来回两个半小时", ["体力一般", "来回两个半小时"])[0] \
        == "体力一般来回两个半小时"


def test_repeated_keyword_keeps_every_occurrence():
    """同一个词出现多次，每一次都保留 —— 只留第一次会让读的人以为只提过一次。"""
    got, hit = mask_content("人很多啊真的人很多", ["人很多"])
    assert got == "人很多**人很多"
    assert hit == 2


def test_overlapping_keywords_are_merged():
    """词互相包含（「体力」「体力一般」）时区间要合并，否则会切出空片段。"""
    got, hit = mask_content("我是体力一般啊", ["体力", "体力一般"])
    assert got == "**体力一般**"
    assert hit == 1                              # 合并成一个区间


def test_crossing_keywords_are_merged():
    """交叉重叠也要合并：「一般来」和「来回」共享「来」。"""
    got, _ = mask_content("我是一般来回啊", ["一般来", "来回"])
    assert got == "**一般来回**"


def test_ignore_case_matches_but_output_keeps_original_case():
    got, hit = mask_content("去了Happy Valley玩", ["happy valley"])
    assert got == "**Happy Valley**"             # 输出保留原文的大小写
    assert hit == 1
    assert mask_content("去了Happy Valley玩", ["happy valley"],
                        ignore_case=False)[0] == "**"   # 关掉就匹配不上


def test_no_keyword_matches_hides_everything_by_default():
    """实测约 2% 会这样：大模型把词抽象过了，原文里根本没有这个词。"""
    got, hit = mask_content("每次感受都不错", ["感受不错"])
    assert got == "**"
    assert hit == 0


def test_no_match_can_keep_raw_instead():
    """一条纯 `**` 没有任何信息量，需要的话可以让这类评论保留原文。"""
    got, hit = mask_content("每次感受都不错", ["感受不错"],
                            on_no_match=D.DRILL_NO_MATCH_KEEP_RAW)
    assert got == "每次感受都不错"
    assert hit == 0


def test_empty_inputs():
    assert mask_content("", ["词"]) == ("", 0)
    assert mask_content(None, ["词"]) == ("", 0)
    assert mask_content("有内容没关键词", []) == ("**", 0)
    assert mask_content("有内容空关键词", ["", "  ", None]) == ("**", 0)


def test_custom_token():
    got, _ = mask_content("我是体力一般啊", ["体力一般"], token="***")
    assert got == "***体力一般***"


# ══════════════════════════════════════════════════════════════════
# 接到下钻表里
# ══════════════════════════════════════════════════════════════════
def _comment(cid, content, words, ds="20260911"):
    return {
        "scenic_id": "S1", "scenic_name": "景区1", "channel": "douyin",
        "work_id": "w1", "comment_id": cid, "comment_level": "level_1",
        "root_comment_id": "", "commenter_name": "u", "content": content, "likes": 0,
        "sentiment_label": "正向", "sentiment_score": 1,
        "dimension_tags": json.dumps([{"dim1": "游玩体验", "dim2": "景色观赏",
                                       "dim3": "自然风光", "sentiment": 1}],
                                     ensure_ascii=False),
        "entity_tags": "[]",
        "keyword_tags": json.dumps(words, ensure_ascii=False),
        "publish_time": f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 10:00:00",
    }


def _run(**kw):
    rows = [_comment("c1", "我是体力一般，来回两个半小时左右",
                     ["体力一般", "来回两个半小时"]),
            _comment("c2", "每次感受都不错", ["感受不错"])]
    for d in pd.date_range("2026-09-01", "2026-09-11"):      # 撑住日历
        ds = d.strftime("%Y%m%d")
        rows.append(_comment(f"bg{ds}", "景色很好玩", ["好玩"], ds))
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 30
    for k, v in kw.items():
        setattr(st, k, v)
    return run(st, ["20260911"], comments_df=pd.DataFrame(rows))


@pytest.fixture(scope="module")
def drill():
    return _run()


def test_drill_table_stores_the_masked_snippet(drill):
    d = drill.tables[DRILL]
    snips = d[d.comment_id == "c1"]["content_snippet"].unique()
    assert list(snips) == ["**体力一般**来回两个半小时**"]


def test_masking_is_on_by_default(drill):
    """业务方要的就是这个行为，所以默认开着。"""
    assert EtlSettings().drill_mask_content is True
    d = drill.tables[DRILL]
    assert not d["content_snippet"].str.contains("我是").any()
    assert not d["content_snippet"].str.contains("左右").any()


def test_same_comment_looks_the_same_on_every_row(drill):
    """scope="comment"（默认）：一条评论的两行长得一样。

    点「体力一般」进来和点「来回两个半小时」进来看到的应该是同一段文本，
    不然同一条评论会忽长忽短，读的人以为是两条不同的评论。
    """
    d = drill.tables[DRILL]
    rows = d[d.comment_id == "c1"]
    assert len(rows) == 2                       # 两个词 → 两行
    assert rows["content_snippet"].nunique() == 1


def test_scope_word_highlights_only_that_row_s_word():
    d = _run(drill_mask_scope=D.DRILL_MASK_SCOPE_WORD).tables[DRILL]
    rows = d[d.comment_id == "c1"].set_index("emotion_word")["content_snippet"]
    assert rows["体力一般"] == "**体力一般**"
    assert rows["来回两个半小时"] == "**来回两个半小时**"


def test_unmatched_keyword_row_is_fully_masked(drill):
    d = drill.tables[DRILL]
    assert d[d.comment_id == "c2"]["content_snippet"].iloc[0] == "**"


def test_can_be_turned_off():
    d = _run(drill_mask_content=False).tables[DRILL]
    assert d[d.comment_id == "c1"]["content_snippet"].iloc[0] \
        == "我是体力一般，来回两个半小时左右"


def test_run_report_says_how_many_were_masked(drill):
    """掩码是看不见的手，不报出来没人知道有多少条被整条隐藏了。"""
    notes = " ".join(drill.notes)
    assert "掩码" in notes
    assert "一个都没找到" in notes               # c2 那条


def test_truncation_happens_before_masking():
    """先截断再掩码：被切掉一半的关键词匹配不上，跟着进掩码段，不会露出半个词。"""
    long_tail = "开头" + "填" * 300 + "体力一般"
    d = _run(drill_content_limit=10).tables[DRILL]
    assert (d["content_snippet"].str.len() <= 10 + 8).all()
    got, hit = mask_content(long_tail[:10], ["体力一般"])
    assert got == "**" and hit == 0             # 关键词在 10 字之外，整条隐藏
