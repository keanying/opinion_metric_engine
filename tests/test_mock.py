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
    st.write_db = st.write_csv = False
    st.lookback_days = 30
    res = run(st, ["20260910", "20260911", "20260912"], comments_df=pd.DataFrame(rows))
    return res.tables


class FakeDB:
    """只认 mock.py 发的两种 SQL：查列名、按景区 + 日期区间取数。"""

    class cfg:                                   # noqa: N801
        @staticmethod
        def ads_db():
            return "ads"

    EXTRA = ["id", "handle_flag", "created_time"]

    def __init__(self, tables):
        self.tables = {t: df.assign(id=range(len(df)), handle_flag=0,
                                    created_time=datetime(2026, 10, 1))
                       for t, df in tables.items()}
        self.queries = []

    def query_df(self, sql, params=()):
        self.queries.append((sql, params))
        if "information_schema" in sql:
            t = params[1]
            cols = list(self.tables[t].columns) if t in self.tables else []
            return pd.DataFrame({"column_name": cols})
        t = re.search(r"from `ads`\.`(\w+)`", sql).group(1)
        cols = re.findall(r"`(\w+)`", sql.split(" from ")[0])
        df = self.tables[t]
        sc, lo, hi = params
        df = df[(df[scenic_key(t)] == sc) & df["travel_date"].between(lo, hi)]
        return df[cols].reset_index(drop=True)

    def close(self):
        pass


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
    assert any(p == (SRC, 20260911, 20260911) for _, p in db.queries)


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
