# -*- coding: utf-8 -*-
"""命令行：按景区跑批 + 整体推送，以及「区间重跑 == 逐日跑」。

    python -m engin_cli.cli run --push all --scenic PFTSCA01002434 --start-date 20260101 --end-date 20260916
    python -m engin_cli.cli run --push all --start-date 20260101 --end-date 20260916      # 全部景区，逐个跑
    python -m engin_cli.cli run --push all --start-date 20260916 --end-date 20260916      # 只跑/只推这一天
"""

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd
import pytest

from engin_cli import cli
from engin_cli.pipeline import run
from engin_cli.settings import EtlSettings

ROOT = Path(__file__).resolve().parents[1]
CORE = "ads_trf_social_opinion_comment_core_di"
PLATFORM = "ads_trf_social_opinion_comment_platform_di"
CONTENT = "ads_trf_social_opinion_comment_content_di"
DIMENSION = "ads_trf_social_opinion_comment_dimension_score_di"
DRILL = "ads_trf_social_opinion_drill_analysis_di"


# ══════════════════════════════════════════════════════════════════
# 模拟网关：pkId = 更新/查重键，按键 upsert
# ══════════════════════════════════════════════════════════════════
class _Gateway(BaseHTTPRequestHandler):
    store = {}
    calls = []

    def do_POST(self):                                           # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        t, pk = body["tableName"], body["pkId"]
        _Gateway.calls.append((t, len(body["data"]),
                               sorted({r.get("scenic_spot_code") for r in body["data"]})))
        s = _Gateway.store.setdefault(t, {})
        for row in body["data"]:
            s[tuple(row[k] for k in pk)] = row
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": true, "code": 200, "msg": "success"}')

    def log_message(self, *a):
        pass


@pytest.fixture
def gateway(monkeypatch):
    _Gateway.store, _Gateway.calls = {}, []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("OPINION_PUSH_URL", f"http://127.0.0.1:{srv.server_address[1]}/sync")
    monkeypatch.setenv("OPINION_PUSH_TOKEN", "pfk_test")
    monkeypatch.setenv("OPINION_PUSH_TOKEN_HEADER", "xpftkey")
    monkeypatch.setenv("OPINION_PUSH_TOKEN_PREFIX", "")
    yield _Gateway
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    out = tmp_path_factory.mktemp("sample")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample_data.py"),
                    "--days", "60", "--end-date", "20260911", "--out", str(out)],
                   check=True, cwd=str(ROOT), capture_output=True)
    return out


def _run(sample, out, *extra):
    return cli.main(["run", "--comments-csv", str(sample / "comments.csv"),
                     "--works-csv", str(sample / "works.csv"), "--skip-db",
                     "--output-dir", str(out), "--lookback-days", "40", *extra])


# ══════════════════════════════════════════════════════════════════
# 参数解析
# ══════════════════════════════════════════════════════════════════
def _ns(**kw):
    base = dict(date=None, start_date=None, end_date=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_dates_single_day_forms():
    assert cli._dates(_ns(start_date="20260916", end_date="20260916")) == ["20260916"]
    assert cli._dates(_ns(start_date="20260916")) == ["20260916"]
    assert cli._dates(_ns(end_date="20260916")) == ["20260916"]
    assert cli._dates(_ns(date="20260916")) == ["20260916"]
    assert len(cli._dates(_ns(start_date="20260101", end_date="20260916"))) == 259
    with pytest.raises(SystemExit):
        cli._dates(_ns(start_date="20260917", end_date="20260916"))


def test_push_tables_parsing():
    assert cli._parse_tables("all") is None
    assert cli._parse_tables("core,drill") == [CORE, DRILL]
    assert cli._parse_tables(PLATFORM) == [PLATFORM]
    with pytest.raises(SystemExit):
        cli._parse_tables("nope")
    args = cli.build_parser().parse_args(["run", "--push", "--scenic", "S1"])
    assert args.push == "all" and args.scenic == "S1"
    args = cli.build_parser().parse_args(["run", "--push", "all", "--scenic", "S1"])
    assert args.push == "all"
    args = cli.build_parser().parse_args(["run", "--scenic", "S1"])
    assert args.push is None


def test_scenic_parsing():
    assert cli._parse_scenic(None) is None
    assert cli._parse_scenic("all") is None
    assert cli._parse_scenic("A, B,A") == ["A", "B"]


# ══════════════════════════════════════════════════════════════════
# 按景区跑 + 整体推送
# ══════════════════════════════════════════════════════════════════
def test_run_one_scenic_and_push_all(sample, tmp_path, gateway):
    rc = _run(sample, tmp_path, "--push", "all", "--scenic", "PFTSCA01002434",
              "--start-date", "20260901", "--end-date", "20260911")
    assert rc == 0
    # 只算、只推这一个景区；五张表都推了
    assert set(gateway.store) == {CORE, PLATFORM, DIMENSION, CONTENT, DRILL}
    assert {s for _, _, ss in gateway.calls for s in ss} == {"PFTSCA01002434"}
    assert len(gateway.store[CORE]) == 11
    # 产物写在景区子目录
    assert (tmp_path / "PFTSCA01002434" / f"{CORE}.csv").exists()
    assert not (tmp_path / "PFT_S_00001").exists()


def test_run_all_scenics_one_by_one(sample, tmp_path, gateway):
    """不给 --scenic：源表里的每个景区逐个跑、逐个推。"""
    rc = _run(sample, tmp_path, "--push", "all",
              "--start-date", "20260910", "--end-date", "20260911")
    assert rc == 0
    assert sorted(os.listdir(tmp_path)) == ["PFTSCA01002434", "PFT_S_00001"]
    # 每个请求只含一个景区 —— 按景区整体推，不混推
    assert all(len(ss) == 1 for _, _, ss in gateway.calls)
    assert len(gateway.store[CORE]) == 4                 # 2 景区 × 2 天


def test_single_day_pushes_only_that_day(sample, tmp_path, gateway):
    rc = _run(sample, tmp_path, "--push", "all",
              "--start-date", "20260911", "--end-date", "20260911")
    assert rc == 0
    for t in (CORE, PLATFORM, DIMENSION, CONTENT, DRILL):
        assert {k[[i for i, _ in enumerate(k)][0]] for k in gateway.store[t]}  # 非空
        days = {row["travel_date"] for row in gateway.store[t].values()}
        assert days == {20260911}, t


def test_push_subset_of_tables(sample, tmp_path, gateway):
    rc = _run(sample, tmp_path, "--push", "core,platform", "--scenic", "PFT_S_00001",
              "--date", "20260911")
    assert rc == 0
    assert set(gateway.store) == {CORE, PLATFORM}


def test_unknown_scenic_fails_but_others_continue(sample, tmp_path, gateway):
    rc = _run(sample, tmp_path, "--push", "--scenic", "NOPE,PFT_S_00001",
              "--date", "20260911")
    assert rc == 1                                        # 有景区失败 → 退出码 1
    assert len(gateway.store[CORE]) == 1                  # 另一个景区照常算、照常推


def test_fail_fast_stops(sample, tmp_path, gateway):
    rc = _run(sample, tmp_path, "--push", "--scenic", "NOPE,PFT_S_00001",
              "--date", "20260911", "--fail-fast")
    assert rc == 1 and gateway.store == {}


def test_push_subcommand_by_scenic_and_date(sample, tmp_path, gateway):
    """补推：按景区目录 + 日期过滤。与跑批直推的键完全一致（不产生重复行）。"""
    assert _run(sample, tmp_path, "--start-date", "20260909", "--end-date", "20260911") == 0
    assert gateway.store == {}                            # 没 --push 就不推

    rc = cli.main(["push", "--output-dir", str(tmp_path), "--scenic", "PFT_S_00001",
                   "--date", "20260911"])
    assert rc == 0
    assert {r["travel_date"] for r in gateway.store[CORE].values()} == {20260911}
    assert {r["scenic_spot_code"] for r in gateway.store[CORE].values()} == {"PFT_S_00001"}

    rc = cli.main(["push", "--output-dir", str(tmp_path)])   # 全部景区、全部日期
    assert rc == 0
    assert len(gateway.store[CORE]) == 6

    # 跑批直推一遍同样的数据：键一致 → 行数不变
    n = {t: len(v) for t, v in gateway.store.items()}
    assert _run(sample, tmp_path, "--push", "--start-date", "20260909",
                "--end-date", "20260911") == 0
    assert {t: len(v) for t, v in gateway.store.items()} == n


# ══════════════════════════════════════════════════════════════════
# 区间重跑 == 逐日跑（每个日期只看自己往回 lookback_days 天）
# ══════════════════════════════════════════════════════════════════
KEYS = {CORE: ["travel_date"], PLATFORM: ["travel_date", "platform_code"],
        DIMENSION: ["travel_date", "dimension_level1", "dimension_level2", "dimension_level3"],
        CONTENT: ["travel_date", "emotion_word"]}


@pytest.mark.parametrize("mode", [None, "window_shift"])
def test_range_run_equals_daily_run(sample, mode):
    comments = pd.read_csv(sample / "comments.csv")
    comments = comments[comments.scenic_id == "PFTSCA01002434"]

    def go(dates):
        st = EtlSettings()
        st.write_db = st.write_csv = False
        st.enable_drill_analysis = False
        st.lookback_days = 20                     # 故意比区间短，逼出「视野」差异
        st.backfill_window_mode = mode
        r = run(st, dates, comments_df=comments)
        assert r.errors == []
        return r.tables

    rng = [d.strftime("%Y%m%d") for d in pd.date_range("2026-08-01", "2026-09-11")]
    whole = go(rng)
    for day in ("20260811", "20260911"):
        one = go([day])
        for t, keys in KEYS.items():
            a = whole[t][whole[t].travel_date == int(day)].drop(columns=["etl_time"])
            b = one[t].drop(columns=["etl_time"])
            a = a.set_index(keys).sort_index()
            b = b.set_index(keys).sort_index()
            assert a.index.equals(b.index), (t, day)
            pd.testing.assert_frame_equal(a, b, check_dtype=False, atol=1e-9,
                                          obj=f"{t} {day}")


def test_content_rank_is_deterministic():
    import engin_cli.metric_calc_domain as D
    v = pd.Series([3, 3, 1, 3])
    g = [pd.Series(["s"] * 4), pd.Series([1] * 4)]
    r1 = D.content_rank(v, g, tiebreak=[pd.Series([5, 9, 1, 5]), pd.Series(["b", "a", "c", "a"])])
    assert r1.tolist() == [3, 1, 4, 2]     # 同为 3 次：近 7 日 9 的最前；再按词排
    # 行顺序打乱，名次跟着词走，不跟着行号走
    idx = [3, 2, 1, 0]
    r2 = D.content_rank(v[idx], [x[idx] for x in g],
                        tiebreak=[pd.Series([5, 9, 1, 5])[idx], pd.Series(["b", "a", "c", "a"])[idx]])
    assert r2.sort_index().tolist() == r1.tolist()


def test_push_without_url_stops_early(sample, tmp_path, monkeypatch):
    monkeypatch.delenv("OPINION_PUSH_URL", raising=False)
    rc = _run(sample, tmp_path, "--push", "--date", "20260911")
    assert rc == 2
    assert not tmp_path.joinpath("PFT_S_00001").exists()     # 一个景区都没跑
