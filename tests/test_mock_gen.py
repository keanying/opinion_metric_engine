# -*- coding: utf-8 -*-
"""mock-gen：演示景区（PFTSCM00000001 A级景区）按日期造评论，走正常计算出各表。"""

import json
from datetime import date

import pandas as pd
import pytest

import engin_cli.cli as cli
from engin_cli import mock_gen as G
from engin_cli.loader import TABLE_CORE, TABLE_DRILL_ANALYSIS, TABLE_MACRO, TABLE_PLATFORM
from engin_cli.pipeline import run
from engin_cli.service import runner as R
from engin_cli.settings import EtlSettings
from engin_cli.source import COMMENT_COLS


def _st():
    st = EtlSettings()
    st.write_db = False
    st.push_enabled = False
    st.plugins = []
    st.lookback_days = 40
    return st


def _dates(a, b):
    return [d.strftime("%Y%m%d") for d in pd.date_range(a, b)]


@pytest.fixture(scope="module")
def produced():
    st = _st()
    dates = _dates("2026-11-15", "2026-11-22")
    c, w, _ = G.build_source(st, dates)
    return run(st, dates, scenic_codes=[G.MOCK_SCENIC], comments_df=c, works_df=w)


# ══════════════════════════════════════════════════════════════════
# 造评论
# ══════════════════════════════════════════════════════════════════
def test_same_day_is_always_the_same():
    a = G.gen_day(G.MOCK_SCENIC, G.MOCK_NAME, date(2026, 11, 20))
    b = G.gen_day(G.MOCK_SCENIC, G.MOCK_NAME, date(2026, 11, 20))
    assert a == b and len(a) >= 8
    # 跟别的日期一起生成，这一天也不变（分段生成、重复生成结果一致）
    df = G.gen_comments(G.MOCK_SCENIC, G.MOCK_NAME, [("20261118", "20261121"), ("20261120", "20261122")])
    one = df[df["publish_time"].str.startswith("2026-11-20")].to_dict("records")
    assert one == a
    assert df["comment_id"].is_unique


def test_days_differ():
    days = [G.gen_day(G.MOCK_SCENIC, G.MOCK_NAME, date(2026, 11, d)) for d in range(1, 15)]
    assert len({len(x) for x in days}) > 5                                    # 评论量每天不同
    pos = [sum(r["sentiment_score"] == 1 for r in x) / len(x) for x in days]
    assert len({round(p, 3) for p in pos}) > 5                                # 好评率每天不同


def test_comment_fields_match_source_table():
    rows = G.gen_day(G.MOCK_SCENIC, G.MOCK_NAME, date(2026, 11, 20))
    assert set(COMMENT_COLS) <= set(rows[0])
    for r in rows:
        assert r["scenic_id"] == G.MOCK_SCENIC and r["scenic_name"] == G.MOCK_NAME
        assert r["content"].strip()
        for w in json.loads(r["keyword_tags"]):
            assert w in r["content"]                         # 下钻表 content_snippet 只保留关键词
        assert r["publish_time"].startswith("2026-11-20")


def test_only_the_ranges_the_pipeline_reads_are_generated():
    st = _st()
    dates = _dates("2026-11-20", "2026-11-22")
    c, _, ranges = G.build_source(st, dates)
    assert ranges == G.source_ranges(st, dates)
    td = pd.to_datetime(c["publish_time"]).dt.strftime("%Y%m%d")
    assert td.max() == "20261122"
    assert all(any(a <= d <= b for a, b in ranges) for d in td.unique())
    assert any(a < "20251201" for a, _ in ranges)            # 大盘表同比要去年同期


# ══════════════════════════════════════════════════════════════════
# 走正常计算
# ══════════════════════════════════════════════════════════════════
def test_pipeline_passes_quality_checks(produced):
    assert produced.ok, produced.errors
    core = produced.tables[TABLE_CORE]
    assert (core["scenic_spot_code"] == G.MOCK_SCENIC).all()
    assert (core["scenic_spot_name"] == G.MOCK_NAME).all()
    assert sorted(core["travel_date"].astype(str)) == _dates("2026-11-15", "2026-11-22")
    assert core["comment_count"].nunique() > 3                                # 每天不一样
    assert len(produced.tables[TABLE_PLATFORM]) == 8 * 6
    assert (produced.tables[TABLE_MACRO]["scenic_name"] == G.MOCK_NAME).all()
    assert not produced.tables[TABLE_DRILL_ANALYSIS].empty


def test_segments():
    assert G.date_segments(["20261101", "20261102", "20261103", "20261110", 20261111]) == [
        {"start": "20261101", "end": "20261103", "days": 3},
        {"start": "20261110", "end": "20261111", "days": 2}]
    assert G.date_segments([]) == []
    assert G.missing_segments(["20261102", "20261103"], "20261101", "20261105") == [
        {"start": "20261101", "end": "20261101", "days": 1},
        {"start": "20261104", "end": "20261105", "days": 2}]


# ══════════════════════════════════════════════════════════════════
# 命令行
# ══════════════════════════════════════════════════════════════════
def test_cli_default_tables_and_push_dry_run(tmp_path, capsys):
    out = tmp_path / "s.json"
    rc = cli.main(["mock-gen", "--start-date", "20261121", "--end-date", "20261122", "--skip-db",
                   "--push-dry-run", "--push-url", "https://push.invalid/sync", "--no-progress", "--summary-json", str(out)])
    assert rc == 0, capsys.readouterr().out
    s = json.loads(out.read_text(encoding="utf-8"))
    assert s["command"] == "mock-gen" and s["exit_code"] == 0
    sc = s["scenics"][0]
    assert sc["scenic"] == G.MOCK_SCENIC and sc["ok"]
    # 默认 5 张：四张指标表 + 大盘表，不出下钻表
    assert set(sc["tables"]) == {cli.TABLE_ALIAS[k] for k in G.DEFAULT_TABLES}
    assert {p["table"] for p in sc["push"]} == set(sc["tables"])
    assert not sc["loads"]                                                     # --skip-db


def test_cli_refuses_scenic_with_real_comments(monkeypatch, capsys):
    class DB:
        def __init__(self, cfg):
            self.cfg = cfg

        def query_df(self, sql, params=None):
            assert "count(*)" in sql and params[0] == "PFTSCA01016184"
            return pd.DataFrame([{"n": 12}])

        def close(self):
            pass

    monkeypatch.setattr(cli, "MySQL", DB)
    rc = cli.main(["mock-gen", "--scenic", "PFTSCA01016184", "--name", "真景区", "--date", "20261122",
                   "--no-push"])
    assert rc == 2 and "真实评论" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════
# 服务：页面提交
# ══════════════════════════════════════════════════════════════════
def test_build_mockgen_argv():
    argv, title = R.build_mockgen_argv({"start": "20261101", "end": "20261122",
                                        "push": "core,macro,drill"})
    assert argv[:9] == ["mock-gen", "--scenic", G.MOCK_SCENIC, "--name", G.MOCK_NAME,
                        "--start-date", "20261101", "--end-date", "20261122"]
    assert argv[argv.index("--tables") + 1] == "macro,core,platform,dimension,content"
    assert argv[argv.index("--push") + 1] == "macro,core"                     # 没生成的下钻表推不了
    assert "--skip-db" not in argv and G.MOCK_NAME in title
    argv, _ = R.build_mockgen_argv({"start": "20261122", "tables": "drill", "skip_db": True, "push": "drill"})
    assert argv[argv.index("--end-date") + 1] == "20261122" and "--skip-db" in argv
    for bad, why in (({"start": "2026"}, "日期"), ({"start": "20261122", "end": "20261101"}, "晚于"),
                     ({"start": "20240101", "end": "20261122"}, "最多"),
                     ({"start": "20261122", "skip_db": True}, "不写库也不推送"),
                     ({"start": "20261122", "tables": "nope"}, "不认识")):
        with pytest.raises(ValueError, match=why):
            R.build_mockgen_argv(bad)


def test_push_switch_off_turns_mockgen_push_off():
    argv, _ = R.build_mockgen_argv({"start": "20261122", "push": "core"})
    off = R.apply_push_switch(argv, enabled=False)
    assert "--push" not in off and "core" not in off[off.index("--tables") + 2:] and off[-1] == "--no-push"
    assert R.apply_push_switch(argv, enabled=True) == argv


def test_coverage_endpoint(tmp_path):
    from engin_cli.service.server import App
    seen = []

    class DB:
        def query_df(self, sql, params=()):
            seen.append(sql)
            if "drill_analysis" in sql:
                raise RuntimeError("Table doesn't exist")
            return pd.DataFrame({"d": [20261101, 20261102, 20261105]})

        def close(self):
            pass

    st = EtlSettings()
    st.service_store = "sqlite"
    app = App(st, data_dir=str(tmp_path), scheduler_enabled=False, db_factory=DB, auth=object())
    r = app.mockgen_coverage("", "20261101", "20261106")
    assert r["scenic"] == G.MOCK_SCENIC and r["default_name"] == G.MOCK_NAME and not r["db_error"]
    by = {t["key"]: t for t in r["tables"]}
    assert by["core"]["days"] == 3 and by["core"]["last"] == "20261105"
    assert by["core"]["segments"] == [{"start": "20261101", "end": "20261102", "days": 2},
                                      {"start": "20261105", "end": "20261105", "days": 1}]
    assert by["core"]["missing"] == [{"start": "20261103", "end": "20261104", "days": 2},
                                     {"start": "20261106", "end": "20261106", "days": 1}]
    assert by["drill"]["error"] and by["drill"]["days"] == 0
    assert any("`scenic_spot_code`=%s" in s for s in seen) and any("`scenic_id`=%s" in s for s in seen)
