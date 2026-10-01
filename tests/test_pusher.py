# -*- coding: utf-8 -*-
"""数据推送模块。

用**真的起一个 HTTP 服务**来测，不用 mock —— 报文长什么样、头带没带、
分批对不对、重试会不会重复推，这些只有真发一次才说得清。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd
import pytest

from engin_cli.pusher import (DEFAULT_PUSH_PK, build_payload, push_all,
                              push_table)
from engin_cli.settings import EtlSettings

CORE = "ads_trf_social_opinion_comment_core_di"


class _Recorder(BaseHTTPRequestHandler):
    """把收到的请求原样记下来。status_plan 控制每次返回什么状态码。"""

    requests = []
    status_plan = []

    def do_POST(self):                                   # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8")
        _Recorder.requests.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": json.loads(raw),
            "raw": raw,
        })
        status = _Recorder.status_plan.pop(0) if _Recorder.status_plan else 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):                           # 别把测试输出刷满
        pass


@pytest.fixture
def server():
    _Recorder.requests = []
    _Recorder.status_plan = []
    srv = HTTPServer(("127.0.0.1", 0), _Recorder)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, _Recorder
    srv.shutdown()
    srv.server_close()


def _settings(srv, **kw):
    st = EtlSettings()
    st.push_enabled = True
    st.push_url = f"http://127.0.0.1:{srv.server_address[1]}/data/sync"
    st.push_headers = {"Authorization": "Bearer sk_deduehdueh"}
    st.push_retries = 0
    st.push_retry_backoff = 0.01
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _core_df(n=3):
    return pd.DataFrame([{
        "scenic_spot_code": "S1", "scenic_spot_name": "景区1",
        "travel_date": 20260911, "publish_time": "20260911",
        "comment_count": 10 + i, "positive_count": 5,
        "emotional_score": 4.5,
    } for i in range(n)])


# ══════════════════════════════════════════════════════════════════
# 报文格式 —— 就是需求里给的那个样子
# ══════════════════════════════════════════════════════════════════
def test_payload_shape_matches_the_spec():
    df = pd.DataFrame([{"order_id": 121212, "order_name": "测试订单"}])
    body = build_payload("order_table", df, ["order_id"])
    assert body == {
        "tableName": "order_table",
        "pkId": ["order_id"],
        "data": [{"order_id": 121212, "order_name": "测试订单"}],
    }


def test_body_field_names_are_configurable():
    df = pd.DataFrame([{"a": 1}])
    body = build_payload("t", df, ["a"],
                         body_fields={"table": "table", "pk": "keys", "data": "rows"})
    assert set(body) == {"table", "keys", "rows"}
    assert body["table"] == "t" and body["keys"] == ["a"]


def test_extra_top_level_fields_are_carried():
    body = build_payload("t", pd.DataFrame([{"a": 1}]), ["a"],
                         extra={"source": "opinion_metric_engine", "batchNo": 7})
    assert body["source"] == "opinion_metric_engine" and body["batchNo"] == 7


def test_numpy_and_nan_are_serialized_safely():
    """np.int64 / NaN 直接 json.dumps 会炸或产出非法 JSON，必须先转干净。"""
    df = pd.DataFrame([{"a": 1, "b": 1.5, "c": None, "d": "x"}])
    df["a"] = df["a"].astype("int64")
    df.loc[0, "b"] = float("nan")
    body = build_payload("t", df, ["a"])
    raw = json.dumps(body, allow_nan=False)          # 不抛就说明干净了
    assert json.loads(raw)["data"][0] == {"a": 1, "b": None, "c": None, "d": "x"}


# ══════════════════════════════════════════════════════════════════
# 真发一次
# ══════════════════════════════════════════════════════════════════
def test_push_sends_url_headers_and_body(server):
    srv, rec = server
    st = _settings(srv)
    res = push_table(CORE, _core_df(2), settings=st)

    assert res.ok and res.rows == 2 and res.batches == 1
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req["path"] == "/data/sync"
    assert req["headers"]["authorization"] == "Bearer sk_deduehdueh"
    assert req["headers"]["content-type"].startswith("application/json")
    assert req["body"]["tableName"] == CORE
    assert req["body"]["pkId"] == DEFAULT_PUSH_PK[CORE]
    assert len(req["body"]["data"]) == 2
    assert req["body"]["data"][0]["scenic_spot_code"] == "S1"


def test_chinese_is_not_escaped(server):
    """ensure_ascii=False —— 报文里应该是「景区1」而不是 \\u666f\\u533a1。"""
    srv, rec = server
    push_table(CORE, _core_df(1), settings=_settings(srv))
    assert "景区1" in rec.requests[0]["raw"]


def test_batching(server):
    srv, rec = server
    res = push_table(CORE, _core_df(7), settings=_settings(srv, push_batch_size=3))
    assert res.batches == 3 and res.rows == 7
    assert [len(r["body"]["data"]) for r in rec.requests] == [3, 3, 1]


def test_table_name_and_pk_are_overridable(server):
    srv, rec = server
    st = _settings(srv,
                   push_table_names={CORE: "opinion_core"},
                   push_pk={CORE: ["scenic_spot_code"]})
    push_table(CORE, _core_df(1), settings=st)
    assert rec.requests[0]["body"]["tableName"] == "opinion_core"
    assert rec.requests[0]["body"]["pkId"] == ["scenic_spot_code"]


# ══════════════════════════════════════════════════════════════════
# 开关与跳过
# ══════════════════════════════════════════════════════════════════
def test_disabled_by_default_sends_nothing(server):
    srv, rec = server
    st = _settings(srv, push_enabled=False)
    rep = push_all({CORE: _core_df(2)}, settings=st)
    assert rep.results == [] and rec.requests == []


def test_missing_url_is_reported_not_silently_skipped(server):
    srv, _ = server
    rep = push_all({CORE: _core_df(1)}, settings=_settings(srv, push_url=""))
    assert not rep.ok and "push_url" in rep.errors[0]


def test_empty_table_is_skipped(server):
    srv, rec = server
    res = push_table(CORE, pd.DataFrame(), settings=_settings(srv))
    assert res.skipped and rec.requests == []


def test_push_tables_whitelist(server):
    srv, rec = server
    st = _settings(srv, push_tables=[CORE])
    push_all({CORE: _core_df(1), "other_table": _core_df(1)}, settings=st)
    assert [r["body"]["tableName"] for r in rec.requests] == [CORE]


def test_dry_run_builds_but_does_not_send(server):
    srv, rec = server
    res = push_table(CORE, _core_df(4), settings=_settings(srv, push_dry_run=True,
                                                           push_batch_size=2))
    assert res.ok and res.batches == 2 and res.rows == 4
    assert rec.requests == []          # 一个请求都没真发出去


def test_bad_pk_config_is_caught_before_sending(server):
    srv, rec = server
    st = _settings(srv, push_pk={CORE: ["not_a_column"]})
    res = push_table(CORE, _core_df(1), settings=st)
    assert not res.ok and "not_a_column" in res.error
    assert rec.requests == []


# ══════════════════════════════════════════════════════════════════
# 失败与重试
# ══════════════════════════════════════════════════════════════════
def test_5xx_is_retried(server):
    srv, rec = server
    rec.status_plan = [500, 200]
    res = push_table(CORE, _core_df(1), settings=_settings(srv, push_retries=2))
    assert res.ok and res.rows == 1
    assert len(rec.requests) == 2          # 重试了一次就成功


def test_4xx_is_not_retried(server):
    """报文格式错/鉴权失败重试多少次都是错，别浪费时间也别打下游。"""
    srv, rec = server
    rec.status_plan = [400, 400, 400]
    res = push_table(CORE, _core_df(1), settings=_settings(srv, push_retries=3))
    assert not res.ok and "400" in res.error
    assert len(rec.requests) == 1          # 只发了一次


def test_one_bad_batch_does_not_stop_the_rest(server):
    srv, rec = server
    rec.status_plan = [200, 400, 200]
    res = push_table(CORE, _core_df(3), settings=_settings(srv, push_batch_size=1))
    assert res.failed_batches == 1
    assert res.rows == 2                   # 另外两批照推
    assert len(rec.requests) == 3


def test_push_failure_does_not_fail_the_run(server):
    """数据已经落库了，推不出去是下游的事 —— 默认不影响跑批成败。"""
    from engin_cli.pipeline import run

    srv, rec = server
    rec.status_plan = [400] * 20
    st = _settings(srv)
    st.write_db = st.write_csv = False
    st.lookback_days = 30
    st.enable_drill_analysis = False
    r = run(st, ["20260911"], comments_df=_sample_comments())
    assert r.errors == []                          # 跑批仍然算成功
    assert r.push is not None and not r.push.ok
    assert any("推送" in n for n in r.notes)        # 但报告里必须看得见


def test_push_strict_makes_the_run_fail(server):
    from engin_cli.pipeline import run

    srv, rec = server
    rec.status_plan = [400] * 20
    st = _settings(srv, push_strict=True)
    st.write_db = st.write_csv = False
    st.lookback_days = 30
    st.enable_drill_analysis = False
    r = run(st, ["20260911"], comments_df=_sample_comments())
    assert r.errors and any("推送" in e for e in r.errors)


def _sample_comments():
    rows = []
    for d in pd.date_range("2026-09-01", "2026-09-11"):
        ds = d.strftime("%Y%m%d")
        for i in range(5):
            rows.append({
                "scenic_id": "S1", "scenic_name": "景区1", "channel": "douyin",
                "work_id": "w1", "comment_id": f"c{ds}{i}", "comment_level": "level_1",
                "root_comment_id": "", "commenter_name": "u", "content": "x", "likes": 0,
                "sentiment_label": "正向", "sentiment_score": 1,
                "dimension_tags": json.dumps([{"dim1": "游玩体验", "dim2": "景色观赏",
                                               "dim3": "自然风光", "sentiment": 1}],
                                             ensure_ascii=False),
                "entity_tags": "[]",
                "keyword_tags": json.dumps(["好玩"], ensure_ascii=False),
                "publish_time": f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 10:00:00",
            })
    return pd.DataFrame(rows)


def test_all_produced_tables_have_a_default_pk():
    """四张表 + 下钻表都要有默认 pkId，漏一张就会在推送时才炸。"""
    from engin_cli import loader
    for t in (loader.TABLE_CORE, loader.TABLE_PLATFORM, loader.TABLE_DIMENSION,
              loader.TABLE_CONTENT, loader.TABLE_DRILL_ANALYSIS):
        assert DEFAULT_PUSH_PK.get(t), f"{t} 没配默认 pkId"


# ══════════════════════════════════════════════════════════════════
# 命令行：--push 既能当开关用，也能指定推哪几张表
# ══════════════════════════════════════════════════════════════════
def test_push_flag_without_value_means_all():
    """`--push`（不带值）= 推全部表。这是最常用的写法。"""
    from engin_cli.cli import build_parser, _apply_push_args, _resolve_tables
    from engin_cli.settings import EtlSettings

    a = build_parser().parse_args(["run", "--date", "20260924", "--push"])
    assert a.push == "all"
    st = EtlSettings()
    _apply_push_args(st, a)
    assert st.push_enabled is True
    assert st.push_tables is None              # None = 不限制 = 全推
    assert _resolve_tables(a.push) is None


def test_push_all_is_also_accepted():
    """`--push all` 写起来很自然，不该报 unrecognized arguments。"""
    from engin_cli.cli import build_parser, _apply_push_args
    from engin_cli.settings import EtlSettings

    a = build_parser().parse_args(["run", "--date", "20260924", "--push", "all"])
    st = EtlSettings()
    _apply_push_args(st, a)
    assert st.push_enabled is True and st.push_tables is None


def test_push_accepts_a_table_list_with_aliases():
    from engin_cli.cli import build_parser, _apply_push_args
    from engin_cli.settings import EtlSettings
    from engin_cli import loader as L

    a = build_parser().parse_args(
        ["run", "--date", "20260924", "--push", "core,platform"])
    st = EtlSettings()
    _apply_push_args(st, a)
    assert st.push_tables == [L.TABLE_CORE, L.TABLE_PLATFORM]


def test_push_flag_does_not_swallow_the_next_option():
    """`--push --scenic X`：--push 后面跟的是另一个选项，不该被当成它的值。"""
    from engin_cli.cli import build_parser
    a = build_parser().parse_args(
        ["run", "--push", "--scenic", "S1", "--start-date", "20260924",
         "--end-date", "20260924"])
    assert a.push == "all" and a.scenic == "S1"


def test_resolve_tables_accepts_full_names_and_dedupes():
    from engin_cli.cli import _resolve_tables
    from engin_cli import loader as L
    assert _resolve_tables(L.TABLE_CORE) == [L.TABLE_CORE]
    assert _resolve_tables("core,core,dim,dimension") == [L.TABLE_CORE, L.TABLE_DIMENSION]
    assert _resolve_tables("drill") == [L.TABLE_DRILL_ANALYSIS]


def test_unknown_table_name_fails_loudly_with_the_valid_list():
    """打错表名要立刻报错并告诉我有哪些可选，不能静默推 0 张表。"""
    from engin_cli.cli import _resolve_tables
    with pytest.raises(SystemExit) as e:
        _resolve_tables("core,platfrom")        # 拼错
    msg = str(e.value)
    assert "platfrom" in msg and "core" in msg


def test_no_push_still_wins_over_push():
    from engin_cli.cli import build_parser, _apply_push_args
    from engin_cli.settings import EtlSettings
    a = build_parser().parse_args(
        ["run", "--date", "20260924", "--push", "--no-push"])
    st = EtlSettings()
    _apply_push_args(st, a)
    assert st.push_enabled is False
