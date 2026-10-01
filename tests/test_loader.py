# -*- coding: utf-8 -*-
"""写库策略：每次运行按「景区 + publish_time」先删除、再写入（六张表一律如此）。

用一个只记录调用的假数据库来测，不需要真的 MySQL。
真库上的端到端验证（预置旧行 → 跑两次 → 旧行删净、范围外保留、写入失败整体回滚）
在开发时用 MariaDB 跑过，见提交说明。
"""

import pandas as pd
import pytest

from engin_cli import loader as L
from engin_cli.pipeline import run
from engin_cli.settings import EtlSettings

SC = "PFTSCE01001721"


class _FakeDB:
    def __init__(self):
        self.calls = []

    def replace_rows(self, table, df, where_sql, params, batch_size=2000):
        self.calls.append({"table": table, "rows": len(df), "where": where_sql,
                           "params": list(params)})
        return 7, len(df)


def _settings(**kw):
    st = EtlSettings()
    st.write_csv = False
    st.write_db = True
    for k, v in kw.items():
        setattr(st, k, v)
    return st


@pytest.mark.parametrize("table,key", [
    (L.TABLE_CORE, "scenic_spot_code"), (L.TABLE_PLATFORM, "scenic_spot_code"),
    (L.TABLE_DIMENSION, "scenic_spot_code"), (L.TABLE_CONTENT, "scenic_spot_code"),
    (L.TABLE_MACRO, "scenic_id"),
])
def test_varchar_publish_time_tables_delete_by_scenic_and_publish_time(table, key):
    where, params = L.delete_scope(table, [SC], ["20260909", "20260910"])
    assert where == f"`{key}` IN (%s) AND `publish_time` IN (%s,%s)"
    assert params == [SC, "20260909", "20260910"]


def test_drill_deletes_whole_days_because_publish_time_is_a_datetime():
    """下钻表的 publish_time 是评论自己的发布时间，按值相等删不干净，按「落在那几天内」删。"""
    where, params = L.delete_scope(L.TABLE_DRILL_ANALYSIS, [SC],
                                   ["20260909", "20260910", "20260912"])
    assert where.startswith("`scenic_id` IN (%s) AND (")
    assert where.count("`publish_time` >= %s AND `publish_time` < %s") == 2   # 9/9~9/10 合并
    assert params == [SC, "2026-09-09 00:00:00", "2026-09-11 00:00:00",
                      "2026-09-12 00:00:00", "2026-09-13 00:00:00"]


def test_publish_time_format_is_respected():
    where, params = L.delete_scope(L.TABLE_CORE, [SC], ["20260909"],
                                   publish_time_format="%Y-%m-%d %H:%M:%S")
    assert params == [SC, "2026-09-09 00:00:00"]


def test_empty_table_still_deletes_old_rows():
    """这次某张表一行都没算出来，也要删掉旧行，不能残留。"""
    db = _FakeDB()
    res = L.load_all(db, {L.TABLE_CONTENT: pd.DataFrame(columns=["scenic_spot_code"])},
                     settings=_settings(), scenics=[SC], dates=["20260909"])
    assert len(db.calls) == 1 and db.calls[0]["rows"] == 0
    assert res[0].strategy == "delete+insert" and res[0].deleted == 7


def test_dry_run_touches_nothing():
    db = _FakeDB()
    res = L.load_all(db, {L.TABLE_CORE: pd.DataFrame({"scenic_spot_code": [SC]})},
                     settings=_settings(dry_run=True), scenics=[SC], dates=["20260909"])
    assert db.calls == [] and res[0].strategy == "dry-run"


def test_csv_only_does_not_touch_the_db():
    db = _FakeDB()
    L.load_all(db, {L.TABLE_CORE: pd.DataFrame({"scenic_spot_code": [SC]})},
               settings=_settings(write_db=False), scenics=[SC], dates=["20260909"])
    assert db.calls == []


def test_pipeline_deletes_by_this_runs_scenic_and_dates(tmp_path):
    """端到端：pipeline 把「本次景区 × 本次日期」传给写库，六张表都是先删后插。"""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, str(root / "scripts" / "make_sample_data.py"),
                    "--days", "40", "--end-date", "20260917", "--out", str(tmp_path)],
                   check=True, capture_output=True)
    comments = pd.read_csv(tmp_path / "comments.csv")
    works = pd.read_csv(tmp_path / "works.csv")
    db = _FakeDB()
    res = run(_settings(lookback_days=30), ["20260916", "20260917"],
              scenic_codes=["PFT_S_00001"], db=db,
              comments_df=comments[comments.scenic_id == "PFT_S_00001"], works_df=works)
    assert res.ok, res.errors
    assert {c["table"] for c in db.calls} == set(res.tables)
    for c in db.calls:
        assert c["params"][0] == "PFT_S_00001"
        if c["table"] == L.TABLE_DRILL_ANALYSIS:
            assert c["params"][1:] == ["2026-09-16 00:00:00", "2026-09-18 00:00:00"]
        else:
            assert c["params"][1:] == ["20260916", "20260917"]
        assert c["rows"] == len(res.tables[c["table"]])
