# -*- coding: utf-8 -*-
"""mock：把一个景区的结果复制给新景区编号并推送。

库用一个假的 MySQL 代替：表数据来自一次真实跑批，按 SQL 里的景区 / 日期过滤，
外加客户表上才有的列（id / handle_flag），检查它们不会被推出去。
"""

import decimal
import json
import re
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd
import pytest

import engin_cli.cli as cli
from engin_cli import mock as M
from engin_cli.loader import TABLE_DRILL_ANALYSIS, TABLE_MACRO, scenic_key
from engin_cli.pipeline import run
from engin_cli.pusher import _jsonable
from engin_cli.settings import EtlSettings
from fake_ads import FakeAdsDB

SRC, DST, NAME = "S1", "S_MOCK", "演示景区"


def _comment(cid, content, words, ds, platform="douyin"):
    return {
        "scenic_id": SRC, "scenic_name": "景区1", "channel": platform,
        "work_id": "w1", "comment_id": cid, "comment_level": "level_1",
        "root_comment_id": "", "commenter_name": "u", "content": content, "likes": 0,
        "sentiment_label": "正向", "sentiment_score": 1,
        "dimension_tags": json.dumps([{"dim1": "游玩体验", "dim2": "景色观赏",
                                       "dim3": "自然风光", "sentiment": 1}], ensure_ascii=False),
        "entity_tags": "[]", "location": "广东",
        "keyword_tags": json.dumps(words, ensure_ascii=False),
        "publish_time": f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 10:00:00",
    }


@pytest.fixture(scope="module")
def produced():
    rows = []
    for i, d in enumerate(pd.date_range("2026-09-01", "2026-09-12")):
        ds = d.strftime("%Y%m%d")
        rows.append(_comment(f"a{ds}", "景色很好玩", ["好玩"], ds))
        if i % 3:
            rows.append(_comment(f"b{ds}", "排队太久了", ["排队"], ds, "xiaohongshu"))
    st = EtlSettings()
    st.write_db = False
    st.lookback_days = 30
    res = run(st, ["20260910", "20260911", "20260912"], comments_df=pd.DataFrame(rows))
    return res.tables


class FakeDB(FakeAdsDB):
    """假 ADS 库（见 tests/fake_ads.py）：表数据来自一次真实跑批，外加客户表上才有的列。"""


# ══════════════════════════════════════════════════════════════════
# 换景区
# ══════════════════════════════════════════════════════════════════
def test_every_table_moves_to_the_new_scenic(produced):
    db = FakeDB(produced)
    out, notes = M.build_mock(db, SRC, DST, NAME, "20260910", "20260912", list(produced))
    assert set(out) == set(produced)
    for t, df in out.items():
        src = produced[t]
        assert len(df) == len(src), t
        assert (df[scenic_key(t)] == DST).all(), t
        assert (df[M.scenic_name_col(t)] == NAME).all(), t
        # 只推引擎自己的列，客户表上的 id / handle_flag / created_time 不推
        assert list(df.columns) == [c for c in M.TABLE_COLUMNS[t] if c in src.columns], t
        # 指标原样
        keep = [c for c in df.columns
                if c not in (scenic_key(t), M.scenic_name_col(t), "etl_time", "detail_uk")]
        pd.testing.assert_frame_equal(
            df[keep].reset_index(drop=True),
            src.sort_values("travel_date", kind="stable")[keep].reset_index(drop=True),
            check_dtype=False, obj=t)


def test_drill_detail_uk_is_recomputed_for_the_new_scenic(produced):
    out, _ = M.build_mock(FakeDB(produced), SRC, DST, NAME, "20260910", "20260912",
                          [TABLE_DRILL_ANALYSIS])
    d, src = out[TABLE_DRILL_ANALYSIS], produced[TABLE_DRILL_ANALYSIS]
    assert d["detail_uk"].is_unique
    assert not set(d["detail_uk"]) & set(src["detail_uk"]), "pkId 跟原景区撞了，下游会覆盖原景区"
    # 公式跟跑批一样：把新景区数据交给跑批的公式算，结果相同
    assert d["detail_uk"].tolist() == M.drill_detail_uk(d)
    assert M.drill_detail_uk(src) == src["detail_uk"].tolist()


def test_date_range_is_respected(produced):
    db = FakeDB(produced)
    out, _ = M.build_mock(db, SRC, DST, NAME, "20260911", "20260911", [TABLE_MACRO])
    assert set(out[TABLE_MACRO]["travel_date"]) == {20260911}
    assert any(list(p or []) == [SRC, 20260911, 20260911] for _, p in db.queries)


def test_same_scenic_id_is_refused(produced):
    with pytest.raises(ValueError, match="覆盖真实景区"):
        M.build_mock(FakeDB(produced), SRC, SRC, NAME, "20260910", "20260912", [TABLE_MACRO])


def test_missing_table_and_empty_range_are_reported(produced):
    db = FakeDB({TABLE_MACRO: produced[TABLE_MACRO]})
    out, notes = M.build_mock(db, SRC, DST, NAME, "20250101", "20250102",
                              [TABLE_MACRO, TABLE_DRILL_ANALYSIS])
    assert out == {}
    assert any("没有这张表" in n for n in notes)
    assert any("没有数据" in n for n in notes)


def test_db_values_are_json_safe():
    """从库里读出来的 DECIMAL / datetime / date 推送时要能序列化。"""
    assert _jsonable(decimal.Decimal("4.500000")) == 4.5
    assert _jsonable(datetime(2026, 10, 19, 8, 11, 56)) == "2026-10-19 08:11:56"
    assert _jsonable(date(2026, 10, 19)) == "2026-10-19"


# ══════════════════════════════════════════════════════════════════
# 命令行：mock → 推送
# ══════════════════════════════════════════════════════════════════
class _Gateway(BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):                                   # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        _Gateway.bodies.append(json.loads(self.rfile.read(n).decode("utf-8")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": true, "code": 200, "msg": "ok"}')

    def log_message(self, *a):
        pass


@pytest.fixture
def gateway():
    _Gateway.bodies = []
    srv = HTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/data/sync", _Gateway.bodies
    srv.shutdown()
    srv.server_close()


def _cli(produced, monkeypatch, *argv):
    db = FakeDB(produced)
    monkeypatch.setattr(cli, "MySQL", lambda cfg: db)
    return cli.main(["mock", "--from-scenic", SRC, "--no-progress", *argv])


def test_mock_command_pushes_the_new_scenic(produced, monkeypatch, gateway, capsys):
    url, bodies = gateway
    rc = _cli(produced, monkeypatch, "--to-scenic", DST, "--to-name", NAME,
              "--start-date", "20260910", "--end-date", "20260912",
              "--tables", "drill,macro", "--push-url", url)
    assert rc == 0
    pushed = {b["tableName"]: [] for b in bodies}
    for b in bodies:
        pushed[b["tableName"]].extend(b["data"])
    assert set(pushed) == {TABLE_DRILL_ANALYSIS, TABLE_MACRO}
    for t, rows in pushed.items():
        assert len(rows) == len(produced[t])
        assert {r["scenic_id"] for r in rows} == {DST}
        assert {r["scenic_name"] for r in rows} == {NAME}
        assert "handle_flag" not in rows[0] and "id" not in rows[0]
    out = capsys.readouterr().out
    assert "推送完成" in out and "推送响应记录：" in out


def test_mock_command_requires_dates(produced, monkeypatch, capsys):
    rc = _cli(produced, monkeypatch, "--to-scenic", DST, "--to-name", NAME, "--preview")
    assert rc == 2 and "要指定日期" in capsys.readouterr().out


def test_mock_command_refuses_same_scenic(produced, monkeypatch, gateway, capsys):
    url, bodies = gateway
    rc = _cli(produced, monkeypatch, "--to-scenic", SRC, "--to-name", NAME,
              "--date", "20260911", "--push-url", url)
    assert rc == 2 and not bodies
    assert "覆盖真实景区" in capsys.readouterr().out


def test_mock_preview_does_not_send(produced, monkeypatch, gateway, capsys):
    url, bodies = gateway
    rc = _cli(produced, monkeypatch, "--to-scenic", DST, "--to-name", NAME,
              "--date", "20260911", "--tables", "macro", "--push-url", url, "--preview")
    assert rc == 0 and not bodies
    out = capsys.readouterr().out
    assert "报文预览" in out and DST in out


# ══════════════════════════════════════════════════════════════════
# 关键字替换：源景区名 → 新景区名（长白山好看 → A级景区好看）
# ══════════════════════════════════════════════════════════════════
CBS, CBS_NAME, A_ID, A_NAME = "S_CBS", "长白山景区", "PFTSCM00000001", "A级景区"


@pytest.fixture(scope="module")
def cbs():
    """景区名「长白山景区」，评论关键词里有「长白山好看」「长白山景区真美」，也有和景区名无关的词。"""
    rows = []
    for i, d in enumerate(pd.date_range("2026-09-01", "2026-09-12")):
        ds = d.strftime("%Y%m%d")
        for cid, text, words, s in ((f"a{ds}", "长白山好看，天池很美", ["长白山好看", "天池"], 1),
                                    (f"b{ds}", "长白山景区真美", ["长白山景区真美"], 1),
                                    (f"c{ds}", "排队太久了", ["排队"], -1)):
            r = _comment(cid, text, words, ds)
            r.update(scenic_id=CBS, scenic_name=CBS_NAME, sentiment_score=s,
                     sentiment_label="正向" if s > 0 else "负向")
            rows.append(r)
    st = EtlSettings()
    st.write_db = False
    st.lookback_days = 30
    res = run(st, ["20260911", "20260912"], comments_df=pd.DataFrame(rows))
    assert res.ok, res.errors
    return res.tables


def _words(tables):
    from engin_cli.loader import TABLE_CONTENT
    out = set(tables[TABLE_CONTENT]["emotion_word"]) | set(tables[TABLE_DRILL_ANALYSIS]["emotion_word"])
    for v in tables[TABLE_MACRO]["wordcloud_map"]:
        for items in json.loads(v).values():
            out |= {it["word"] for it in items}
    return out


def test_default_replace_words():
    assert M.default_replace_words(["长白山景区"]) == ["长白山景区", "长白山"]
    assert M.default_replace_words(["泰宁九龙潭生态旅游区", "故宫"]) == ["泰宁九龙潭生态旅游区", "泰宁九龙潭", "故宫"]
    assert M.default_replace_words(["山景区", "", None]) == ["山景区"]          # 去后缀剩 1 个字：不拆



def test_parse_words():
    assert M.parse_words(" 长白山，长白山景区, ,长白山 ,天池") == ["长白山", "长白山景区", "天池"]
    assert M.parse_words(["天池", " 长白山 ", ""]) == ["天池", "长白山"]
    assert M.parse_words(None) == [] and M.parse_words("") == []


def test_replacer_longest_first_and_single_pass():
    to = "A级景区"
    rep = M.make_replacer(["长白山", "长白山景区"], to)
    assert rep("长白山景区真美") == "A级景区真美"                                # 不会变成 A级景区景区真美
    assert rep("长白山好看") == "A级景区好看" and rep("天池") == "天池" and rep(None) is None
    # 新名称里含旧词：只换一次
    assert M.make_replacer(["长白山"], "长白山演示")("长白山好看") == "长白山演示好看"
    assert M.make_replacer(["A级景区"], to) is None


def test_replacer_many_words_in_one_text():
    rep = M.make_replacer(["长白山景区", "长白山", "天池", "二道白河"], "A级景区")
    # 一段文字里有好几个、好几种，全部一起换成新景区名称
    assert rep("长白山天池好看，长白山景区门口的二道白河也不错，下次还来长白山") == \
        "A级景区A级景区好看，A级景区门口的A级景区也不错，下次还来A级景区"
    # 一个词包含另一个：从左往右、长词优先，不会拆坏
    rep = M.make_replacer(["长白山", "白山天池"], "A级景区")
    assert rep("长白山天池") == "A级景区天池"


def test_wordcloud_words_replaced_and_merged():
    rep = M.make_replacer(["长白山景区", "长白山"], "A级景区")
    v = json.dumps({"positiveWord": [{"word": "长白山好看", "rate": 30.5}, {"word": "天池", "rate": 20.0},
                                     {"word": "长白山景区好看", "rate": 10.25}],
                    "negativeWord": []}, ensure_ascii=False, separators=(",", ":"))
    out = json.loads(M._replace_wordcloud(v, rep))
    assert out["positiveWord"] == [{"word": "A级景区好看", "rate": 40.75}, {"word": "天池", "rate": 20.0}]
    same = json.dumps({"positiveWord": [{"word": "天池", "rate": 1.0}]})
    assert M._replace_wordcloud(same, rep) == same                              # 没有要换的：原样
    assert M._replace_wordcloud(None, rep) is None


def test_copy_replaces_source_name_in_keywords(cbs):
    out, notes = M.build_mock(FakeDB(cbs), CBS, A_ID, A_NAME, "20260911", "20260912", list(cbs))
    before, after = _words(cbs), _words(out)
    assert {"长白山好看", "长白山景区真美"} <= before
    assert {"A级景区好看", "A级景区真美", "天池", "排队"} <= after
    assert not any("长白山" in w for w in after)
    drill = out[TABLE_DRILL_ANALYSIS]
    assert not drill["content_snippet"].str.contains("长白山").any()
    assert drill["content_snippet"].str.contains("A级景区").any()
    # detail_uk 按替换后的词重算：跟跑批同一个公式，且不重复
    assert drill["detail_uk"].tolist() == M.drill_detail_uk(drill) and drill["detail_uk"].is_unique
    # 指标数值不动：除了景区、名称、关键字、etl_time、detail_uk，其余列与原表一致
    from engin_cli.loader import TABLE_CONTENT
    c0, c1 = cbs[TABLE_CONTENT].sort_values("travel_date", kind="stable").reset_index(drop=True), \
        out[TABLE_CONTENT].reset_index(drop=True)
    keep = [c for c in c1.columns if c not in ("scenic_spot_code", "scenic_spot_name", "emotion_word", "etl_time")]
    pd.testing.assert_frame_equal(c1[keep], c0[keep], check_dtype=False)
    assert any(n.startswith("关键字替换：长白山景区、长白山 → A级景区") for n in notes)


def test_copy_with_extra_words_and_switch_off(cbs):
    # 源景区名照常换；另外给的词（逗号分隔）也都换成新景区名称
    out, notes = M.build_mock(FakeDB(cbs), CBS, A_ID, A_NAME, "20260911", "20260912", list(cbs),
                              replace_words="天池,排队")
    words = _words(out)
    assert {"A级景区好看", "A级景区真美", "A级景区"} <= words
    assert not words & {"天池", "排队"} and not any("长白山" in w for w in words)
    snip = out[TABLE_DRILL_ANALYSIS]["content_snippet"]
    assert not snip.str.contains("长白山|天池").any()
    assert any("关键字替换：长白山景区、长白山、天池、排队 → A级景区" in n for n in notes)
    out, notes = M.build_mock(FakeDB(cbs), CBS, A_ID, A_NAME, "20260911", "20260912", [TABLE_MACRO],
                              replace=False)
    assert out[TABLE_MACRO]["wordcloud_map"].tolist() == \
        cbs[TABLE_MACRO].sort_values("travel_date", kind="stable")["wordcloud_map"].tolist()
    assert any("已关闭" in n for n in notes)


def test_collision_after_replace_is_reported(cbs):
    # 「长白山好看」「长白山景区真美」都换成「A级景区」→ 同一个词，内容表主键撞车，要提示
    out, notes = M.build_mock(FakeDB(cbs), CBS, A_ID, A_NAME, "20260911", "20260912", list(cbs),
                              replace_words=["长白山好看", "长白山景区真美"])
    assert any("主键重复" in n for n in notes)


def test_mock_command_replace_words(cbs, monkeypatch, gateway, capsys):
    url, bodies = gateway
    monkeypatch.setattr(cli, "MySQL", lambda cfg: FakeDB(cbs))
    rc = cli.main(["mock", "--from-scenic", CBS, "--to-scenic", A_ID, "--to-name", A_NAME, "--no-progress",
                   "--date", "20260912", "--tables", "content", "--push-url", url])
    assert rc == 0
    words = {r["emotion_word"] for b in bodies for r in b["data"]}
    assert "A级景区好看" in words and not any("长白山" in w for w in words)
    assert "关键字替换：长白山景区、长白山 → A级景区" in capsys.readouterr().out
    bodies.clear()
    rc = cli.main(["mock", "--from-scenic", CBS, "--to-scenic", A_ID, "--to-name", A_NAME, "--no-progress",
                   "--date", "20260912", "--tables", "content", "--push-url", url, "--no-replace-words"])
    assert rc == 0 and "长白山好看" in {r["emotion_word"] for b in bodies for r in b["data"]}
    bodies.clear()
    rc = cli.main(["mock", "--from-scenic", CBS, "--to-scenic", A_ID, "--to-name", A_NAME, "--no-progress",
                   "--date", "20260912", "--tables", "content", "--push-url", url,
                   "--replace-words", "天池,排队"])
    words = {r["emotion_word"] for b in bodies for r in b["data"]}
    assert rc == 0 and "A级景区好看" in words and not words & {"天池", "排队"}


def test_user_words_in_wordcloud_any_shape():
    """客户给的词（逗号分隔、有重复）；词云从库里读出来的形态不一定规整：转义、编码两次、bytes 都要换到。"""
    rep = M.make_replacer(M.parse_words("九嶷,三分石,九嶷山,宁远,舜帝道德,九嶷,舜文化,舜帝陵"), "A级景区")
    assert rep("舜帝道德文化的鼻祖") == "A级景区文化的鼻祖"
    assert rep("九嶷山三分石") == "A级景区A级景区"
    obj = {"positiveWord": [{"word": "舜帝道德文化的鼻祖", "rate": 3.2}],
           "neutralWord": [{"word": "宁远县城", "rate": 2.0}], "negativeWord": [{"word": "舜帝陵排队", "rate": 1.0}]}
    want = {"positiveWord": [{"word": "A级景区文化的鼻祖", "rate": 3.2}],
            "neutralWord": [{"word": "A级景区县城", "rate": 2.0}], "negativeWord": [{"word": "A级景区排队", "rate": 1.0}]}
    plain = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    for v in (plain, json.dumps(obj), json.dumps(obj, ensure_ascii=False, indent=1), plain.encode("utf-8")):
        assert json.loads(M._replace_wordcloud(v, rep)) == want
    twice = M._replace_wordcloud(json.dumps(plain, ensure_ascii=False), rep)
    assert json.loads(json.loads(twice)) == want                              # 编码两次的，换完照原样包回去
    assert M._replace_wordcloud("舜帝陵 不是 JSON", rep) == "A级景区 不是 JSON"
