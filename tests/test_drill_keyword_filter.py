# -*- coding: utf-8 -*-
"""下钻表采集关键字过滤（--drill-keyword-filter）。

开启：只有原文包含本景区采集关键字（src_opinion_scenic_keyword ∪ src_opinion_scenic_filter_word）
的评论写入下钻表；关闭（默认）：原逻辑，评论全收。其余各表不受影响。
"""

import json

import pandas as pd
import pytest

import engin_cli.cli as cli
from engin_cli.loader import TABLE_DRILL_ANALYSIS, TABLE_MACRO
from engin_cli.pipeline import run
from engin_cli.settings import EtlSettings
from engin_cli.source import fetch_scenic_keywords, fetch_scenic_keywords_csv

DRILL = TABLE_DRILL_ANALYSIS
KW = {"S1": ["九嶷山", "舜帝陵"]}


def _comment(cid, content, ds, words=("好玩",), platform="douyin", scenic="S1"):
    return {
        "scenic_id": scenic, "scenic_name": "景区1", "channel": platform,
        "work_id": "w1", "comment_id": cid, "comment_level": "level_1",
        "root_comment_id": "", "commenter_name": "u", "content": content, "likes": 0,
        "sentiment_label": "正向", "sentiment_score": 1,
        "dimension_tags": json.dumps([{"dim1": "游玩体验", "dim2": "景色观赏",
                                       "dim3": "自然风光", "sentiment": 1}], ensure_ascii=False),
        "entity_tags": "[]", "location": "广东",
        "keyword_tags": json.dumps(list(words), ensure_ascii=False),
        "publish_time": f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 10:00:00",
    }


def _rows():
    rows = []
    for d in pd.date_range("2026-09-01", "2026-09-12"):
        ds = d.strftime("%Y%m%d")
        rows.append(_comment(f"hit{ds}", "去了九嶷山，景色很好玩", ds))
        rows.append(_comment(f"miss{ds}", "苏州平江路很好玩", ds))
        rows.append(_comment(f"case{ds}", "舜帝陵 yyds", ds, words=()))
    # 9/12 小红书只有一条不含关键字的评论 → 这个渠道当天有数据，不该被当成「缺数」去补
    rows.append(_comment("xhs_miss", "无关内容", "20260912", platform="xiaohongshu"))
    return pd.DataFrame(rows)


def _run(filter_on, keywords=None, **kw):
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 30
    st.drill_keyword_filter = filter_on
    for k, v in kw.items():
        setattr(st, k, v)
    return run(st, ["20260911", "20260912"], comments_df=_rows(), drill_keywords=keywords)


def test_off_by_default_keeps_every_comment():
    assert EtlSettings().drill_keyword_filter is False
    d = _run(False).tables[DRILL]
    assert {c[:3] for c in d["comment_id"]} >= {"hit", "mis", "cas"}


def test_on_only_keeps_comments_containing_a_collection_keyword():
    res = _run(True, KW)
    d = res.tables[DRILL]
    ids = set(d["comment_id"])
    assert ids, "含关键字的评论应该写入"
    assert all(i.startswith(("hit", "case")) for i in ids), ids
    assert not any(i.startswith("miss") or i == "xhs_miss" for i in ids)
    # 其余表不受影响：大盘表仍按全部评论算
    off = _run(False)
    pd.testing.assert_frame_equal(
        res.tables[TABLE_MACRO].drop(columns=["etl_time"]).reset_index(drop=True),
        off.tables[TABLE_MACRO].drop(columns=["etl_time"]).reset_index(drop=True))
    assert not res.errors, res.errors          # 下钻 ↔ 大盘对账在过滤开启时不做
    assert any("采集关键字过滤" in n and "不写入" in n for n in res.notes)


def test_matching_is_case_insensitive_and_on_full_content():
    rows = _rows()
    long = "开头" + "填" * 300 + "Jiuyi Mountain"         # 关键字在 200 字摘要之外也算
    rows.loc[len(rows)] = _comment("long", long, "20260912", words=())
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 30
    st.drill_keyword_filter = True
    d = run(st, ["20260912"], comments_df=rows,
            drill_keywords={"S1": ["jiuyi mountain"]}).tables[DRILL]
    assert set(d["comment_id"]) == {"long"}


def test_fill_still_judged_on_all_comments():
    """小红书 9/12 有评论（只是不含关键字）→ 不触发补数，下钻表也不该出现 9/11 复制来的小红书评论。"""
    d = _run(True, KW).tables[DRILL]
    xhs = d[(d["channel"] == "xiaohongshu")]
    assert xhs.empty


def test_scenic_without_keywords_gets_no_drill_rows():
    res = _run(True, {})
    assert res.tables[DRILL].empty
    assert any("没有采集关键字" in n for n in res.notes)


def test_on_without_db_or_keywords_fails_clearly():
    res = None
    with pytest.raises(ValueError, match="drill-keywords-csv"):
        res = _run(True, None)
    assert res is None


# ══════════════════════════════════════════════════════════════════
# 取关键字：两张表按 scenic_id 取并集
# ══════════════════════════════════════════════════════════════════
class _FakeDB:
    class cfg:                                   # noqa: N801
        @staticmethod
        def src_db():
            return "src"

    def __init__(self, df):
        self.df, self.sql = df, []

    def query_df(self, sql, params=()):
        self.sql.append((sql, list(params)))
        return self.df


def test_fetch_scenic_keywords_unions_both_tables_by_scenic():
    db = _FakeDB(pd.DataFrame({"scenic_id": ["S1", "S1", "S1", "S2"],
                               "word": ["九嶷山", " 舜帝陵 ", "九嶷山", "平江路"]}))
    got = fetch_scenic_keywords(db, ["S1", "S2"])
    assert got == {"S1": ["九嶷山", "舜帝陵"], "S2": ["平江路"]}
    sql, params = db.sql[0]
    assert "`src`.`src_opinion_scenic_keyword`" in sql and "keyword AS word" in sql
    assert "`src`.`src_opinion_scenic_filter_word`" in sql and "UNION" in sql
    assert params == ["S1", "S2", "S1", "S2"]
    assert fetch_scenic_keywords(db, []) == {}


def test_keywords_csv(tmp_path):
    p = tmp_path / "kw.csv"
    p.write_text("scenic_id,keyword\nS1,九嶷山\nS1,舜帝陵\n", encoding="utf-8-sig")
    assert fetch_scenic_keywords_csv(str(p)) == {"S1": ["九嶷山", "舜帝陵"]}


# ══════════════════════════════════════════════════════════════════
# 命令行开关
# ══════════════════════════════════════════════════════════════════
def _cli(tmp_path, *argv):
    src = tmp_path / "comments.csv"
    _rows().to_csv(src, index=False, encoding="utf-8-sig")
    out = tmp_path / "out"
    rc = cli.main(["run", "--comments-csv", str(src), "--skip-db", "--no-push",
                   "--rerun-days", "0", "--output-dir", str(out),
                   "--start-date", "20260911", "--end-date", "20260912", *argv])
    path = out / "S1" / f"{DRILL}.csv"
    return rc, (pd.read_csv(path, dtype=str) if path.exists() else None)


def test_cli_switch_on_with_keywords_csv(tmp_path):
    kw = tmp_path / "kw.csv"
    kw.write_text("scenic_id,word\nS1,九嶷山\n", encoding="utf-8-sig")
    rc, d = _cli(tmp_path, "--drill-keyword-filter", "--drill-keywords-csv", str(kw))
    assert rc == 0
    assert set(d["comment_id"].str[:3]) == {"hit"}


def test_cli_switch_off_is_original_logic(tmp_path):
    rc, d = _cli(tmp_path, "--no-drill-keyword-filter")
    assert rc == 0
    assert {"hit", "mis", "cas"} <= set(d["comment_id"].str[:3])


def test_cli_on_without_db_or_csv_is_refused(tmp_path, capsys):
    rc, _ = _cli(tmp_path, "--drill-keyword-filter")
    assert rc == 2
    assert "--drill-keywords-csv" in capsys.readouterr().out
