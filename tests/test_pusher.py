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

from engin_cli.pusher import (DEFAULT_PUSH_PK, PushReport, build_payload, push_all,
                              push_table)
from engin_cli.settings import EtlSettings

CORE = "ads_trf_social_opinion_comment_core_di"


class _Recorder(BaseHTTPRequestHandler):
    """把收到的请求原样记下来。status_plan 控制每次返回什么状态码。"""

    requests = []
    status_plan = []
    body_plan = []          # 每次返回的响应体；空了就回 {"ok":true}

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
        body = _Recorder.body_plan.pop(0) if _Recorder.body_plan else '{"ok":true}'
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *a):                           # 别把测试输出刷满
        pass


@pytest.fixture
def server():
    _Recorder.requests = []
    _Recorder.status_plan = []
    _Recorder.body_plan = []
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
    # pkId 有问题时会把请求 JSON 写到 output_dir/push_debug/，别写进项目目录
    import tempfile
    st.output_dir = tempfile.mkdtemp()
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _core_df(n=3):
    # 每行一天：core 表的 pkId（景区 + 日期 + publish_time）必须唯一，否则推送前校验会拒发
    return pd.DataFrame([{
        "scenic_spot_code": "S1", "scenic_spot_name": "景区1",
        "travel_date": 20260901 + i, "publish_time": str(20260901 + i),
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


# ══════════════════════════════════════════════════════════════════
# HTTP 200 + 响应体 status=false —— 网关的业务失败
# ══════════════════════════════════════════════════════════════════
def test_http_200_with_status_false_is_a_failure(server):
    """网关 HTTP 恒为 200，失败写在响应体里。以前这种情况会被当成推送成功。"""
    srv, rec = server
    rec.body_plan = ['{"status": false, "code": 5011, "msg": "主键字段 x 不在允许字段内",'
                     ' "trace_id": "abc123"}']
    res = push_table(CORE, _core_df(2), settings=_settings(srv, push_retries=3))
    assert not res.ok
    assert res.failed_batches == 1 and res.rows == 0
    assert "code=5011" in res.error and "trace_id=abc123" in res.error
    assert "主键字段 x 不在允许字段内" in res.error
    assert len(rec.requests) == 1, "业务失败是报文/配置问题，重试没有意义"


def test_http_200_with_status_true_is_a_success(server):
    srv, rec = server
    rec.body_plan = ['{"status": true, "code": 200, "msg": "ok"}']
    res = push_table(CORE, _core_df(2), settings=_settings(srv))
    assert res.ok and res.rows == 2


@pytest.mark.parametrize("body", ['{"ok":true}', "OK", "", '[1,2]', '{"status": "success"}'])
def test_responses_without_a_false_status_are_successes(server, body):
    """不是 JSON、或没有 status 字段 → 只看 HTTP 状态码，保持原来的行为。"""
    srv, rec = server
    rec.body_plan = [body]
    assert push_table(CORE, _core_df(1), settings=_settings(srv)).ok


@pytest.mark.parametrize("value", ["false", 0, "0", "FAIL"])
def test_falsy_status_spellings(server, value):
    srv, rec = server
    rec.body_plan = [json.dumps({"status": value, "msg": "x"})]
    assert not push_table(CORE, _core_df(1), settings=_settings(srv)).ok


def test_success_field_names_are_configurable(server):
    srv, rec = server
    rec.body_plan = ['{"success": false, "errCode": "E1", "errMsg": "bad", "rid": "r9"}']
    st = _settings(srv, push_success_field="success", push_code_field="errCode",
                   push_message_field="errMsg", push_trace_field="rid")
    res = push_table(CORE, _core_df(1), settings=st)
    assert not res.ok and "code=E1" in res.error and "trace_id=r9" in res.error


def test_success_check_can_be_turned_off(server):
    srv, rec = server
    rec.body_plan = ['{"status": false}']
    assert push_table(CORE, _core_df(1), settings=_settings(srv, push_success_field="")).ok


def test_business_failure_in_one_batch_does_not_stop_the_rest(server):
    srv, rec = server
    rec.body_plan = ['{"status": true}', '{"status": false, "msg": "boom"}', '{"status": true}']
    res = push_table(CORE, _core_df(3), settings=_settings(srv, push_batch_size=1))
    assert res.batches == 2 and res.failed_batches == 1 and res.rows == 2


def test_macro_table_default_pk_matches_its_unique_key():
    from engin_cli.loader import TABLE_MACRO
    assert DEFAULT_PUSH_PK[TABLE_MACRO] == ["scenic_id", "travel_date",
                                            "time_granularity", "channel"]


def test_macro_alias_resolves():
    from engin_cli.cli import _resolve_tables
    from engin_cli.loader import TABLE_MACRO
    assert _resolve_tables("macro") == [TABLE_MACRO]
    assert _resolve_tables("core,kpi") == [CORE, TABLE_MACRO]


# ══════════════════════════════════════════════════════════════════
# 推送地址连不上：立即停止，不要一批一批地白等重试（以前表现为「卡住」）
# ══════════════════════════════════════════════════════════════════
def _closed_port_url():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                  # 端口空出来，没有服务在监听
    return f"http://127.0.0.1:{port}/sync"


def test_unreachable_url_stops_the_whole_push_quickly():
    import time
    st = EtlSettings()
    st.push_enabled = True
    st.push_url = _closed_port_url()
    st.push_retries = 2
    st.push_retry_backoff = 0.01
    st.push_batch_size = 1
    t0 = time.time()
    rep = push_all({CORE: _core_df(50), "ads_trf_social_opinion_comment_platform_di": _core_df(5)},
                   settings=st)
    assert time.time() - t0 < 5, "连不上时应该第一批就停，而不是 50 批挨个重试"
    first, second = rep.results
    assert first.unreachable and first.batches == 0 and first.failed_batches == 50
    assert "连不上" in first.error and "连接被拒绝" in first.error
    assert second.skipped and "未推送" in second.error       # 后面的表不再推
    assert not rep.ok


def test_progress_bar_reports_batches_and_retries(server):
    import io
    from engin_cli.progress import push_progress_factory
    srv, rec = server
    rec.status_plan = [200, 503, 200, 200]                  # 第 2 批第一次 503，重试后成功
    out = io.StringIO()                                     # 不是终端 → 每 10% 一行
    st = _settings(srv, push_batch_size=1, push_retries=1, push_retry_backoff=0.01)
    rep = push_all({CORE: _core_df(3)}, settings=st, progress_factory=push_progress_factory(out))
    assert rep.ok
    text = out.getvalue()
    assert "推送 comment_core_di：3 行，3 批" in text
    assert "第 2 批失败：HTTP 503" in text and "第 1/1 次重试" in text
    assert "3/3 批 100.0%" in text and "✓" in text
    assert "\r" not in text                                 # 日志文件里不能有回车刷新


def test_progress_bar_in_a_terminal_redraws_in_place(server):
    import io
    from engin_cli.progress import PushProgress

    class _Tty(io.StringIO):
        def isatty(self):
            return True

    srv, _ = server
    out = _Tty()
    push_table(CORE, _core_df(4), settings=_settings(srv, push_batch_size=1),
               progress=PushProgress(CORE, 4, 4, stream=out))
    text = out.getvalue()
    assert text.count("\n") == 1                            # 只在结束时换一次行
    assert "\r" in text and "4/4 批 100.0%" in text
    assert "\033" not in text                               # 不用 ANSI 控制符，旧版 Windows 控制台不认


# ══════════════════════════════════════════════════════════════════
# 对方连得上但一直不响应：超时只重试 1 次，然后停止整次推送；等待期间进度条读秒
# ══════════════════════════════════════════════════════════════════
@pytest.fixture
def silent_server():
    """接受连接但永远不回的服务（模拟卡住的下游）。"""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(50)
    conns = []
    stop = threading.Event()

    def _accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conns.append(srv.accept()[0])
            except OSError:
                pass

    threading.Thread(target=_accept, daemon=True).start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}/sync", conns
    stop.set()
    for c in conns:
        c.close()
    srv.close()


def test_unresponsive_server_stops_after_one_timeout_retry(silent_server):
    import time
    url, conns = silent_server
    st = EtlSettings()
    st.push_enabled, st.push_url = True, url
    st.push_timeout, st.push_retries, st.push_retry_backoff = 1, 3, 0.01
    st.push_batch_size = 1
    t0 = time.time()
    rep = push_all({CORE: _core_df(20), "ads_trf_social_opinion_comment_platform_di": _core_df(2)},
                   settings=st)
    assert time.time() - t0 < 6, "超时只重试 1 次，不能按 push_retries 重试到底、更不能 20 批挨个等"
    first, second = rep.results
    assert first.unreachable and "没有响应" in first.error
    assert len(conns) == 2                       # 第 1 次 + 重试 1 次
    assert second.skipped and "未推送" in second.error


def test_progress_bar_counts_seconds_while_waiting(silent_server):
    import io
    from engin_cli.progress import PushProgress

    class _Tty(io.StringIO):
        def isatty(self):
            return True

    url, _ = silent_server
    st = EtlSettings()
    st.push_enabled, st.push_url = True, url
    st.push_timeout, st.push_retries, st.push_retry_backoff = 2.5, 0, 0.01
    out = _Tty()
    push_table(CORE, _core_df(1), settings=st, progress=PushProgress(CORE, 1, 1, stream=out))
    text = out.getvalue()
    assert "第 1 批等待响应 1s/2.5s" in text and "第 1 批等待响应 2s/2.5s" in text


def test_push_from_scenic_dir_does_not_repush_the_same_scenic_from_flat_dir(tmp_path, server):
    """新老两份 CSV 都在时（output/<景区>/ 和老的平铺 output/），同一个景区只推一次。"""
    from engin_cli import cli
    srv, rec = server
    df = _core_df(1).assign(scenic_spot_code="S1", travel_date=20260911)
    (tmp_path / "S1").mkdir()
    df.to_csv(tmp_path / "S1" / f"{CORE}.csv", index=False)
    pd.concat([df, df.assign(scenic_spot_code="S2")]).to_csv(tmp_path / f"{CORE}.csv", index=False)
    url = f"http://127.0.0.1:{srv.server_address[1]}/data/sync"
    assert cli.main(["push", "--output-dir", str(tmp_path), "--push-url", url,
                     "--scenic", "S1", "--no-progress"]) == 0
    assert len(rec.requests) == 1                # 只从景区目录推了一次
    rec.requests.clear()
    assert cli.main(["push", "--output-dir", str(tmp_path), "--push-url", url,
                     "--no-progress"]) == 0
    sent = sorted(r["scenic_spot_code"] for q in rec.requests for r in q["body"]["data"])
    assert sent == ["S1", "S2"]                  # S1 来自子目录，S2 来自平铺目录，各一次


# ══════════════════════════════════════════════════════════════════
# curl 能通、engin_cli 超时：本机地址不走系统代理；push --diagnose 能指出原因
# ══════════════════════════════════════════════════════════════════
@pytest.fixture
def blackhole_proxy(silent_server, monkeypatch):
    """系统代理指向一个只收不回的地址（模拟 Windows 上配了 Clash/公司代理，但没排除 localhost）。"""
    url, _ = silent_server
    monkeypatch.setenv("http_proxy", url.rsplit("/", 1)[0])
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    return url


def test_local_push_ignores_system_proxy(server, blackhole_proxy):
    from engin_cli.pusher import proxy_for
    srv, rec = server
    url = f"http://localhost:{srv.server_address[1]}/data/sync"
    assert proxy_for(url) is None
    st = _settings(srv, push_url=url, push_timeout=3)
    assert push_table(CORE, _core_df(1), settings=st).ok
    assert len(rec.requests) == 1


def test_remote_push_still_uses_system_proxy(blackhole_proxy):
    from engin_cli.pusher import proxy_for
    assert proxy_for("http://data.example.com/sync") == blackhole_proxy.rsplit("/", 1)[0]


def test_content_type_matches_curl(server):
    srv, rec = server
    push_table(CORE, _core_df(1), settings=_settings(srv))
    assert rec.requests[0]["headers"]["content-type"] == "application/json"


def test_diagnose_points_at_integer_travel_date(silent_server):
    """服务收到整数 travel_date 就卡住、字符串才正常 → 诊断结论指向报文。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from engin_cli.pusher import diagnose

    class _Picky(BaseHTTPRequestHandler):
        def do_POST(self):                                    # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if any(isinstance(r.get("travel_date"), int) for r in body["data"]):
                import time
                time.sleep(3)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": true}')

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Picky)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    lines = []
    body = build_payload(CORE, _core_df(1), DEFAULT_PUSH_PK[CORE])
    res = diagnose(f"http://127.0.0.1:{srv.server_address[1]}/sync", {}, body,
                   timeout=1, out=lines.append)
    srv.shutdown()
    assert res["actual"] is False and res["variant"] is True
    assert "travel_date 用字符串能通" in lines[-1]


def test_diagnose_ok_path(server):
    from engin_cli.pusher import diagnose
    srv, _ = server
    lines = []
    res = diagnose(f"http://localhost:{srv.server_address[1]}/sync", {},
                   build_payload(CORE, _core_df(1), DEFAULT_PUSH_PK[CORE]),
                   timeout=2, out=lines.append)
    assert res["actual"] is True and "能通" in lines[-1]


# ══════════════════════════════════════════════════════════════════
# 发送前检查 pkId：只提醒、照常发送（下游按 pkId 自己查），并留下问题行的请求 JSON
# ══════════════════════════════════════════════════════════════════
PLATFORM = "ads_trf_social_opinion_comment_platform_di"


def _platform_df():
    return pd.DataFrame([{"scenic_spot_code": "S1", "travel_date": 20260909,
                          "publish_time": "20260909", "platform_code": ch, "comment_cnt": i}
                         for i, ch in enumerate(["douyin", "weibo", "ctrip"])])


def test_duplicate_pk_is_sent_with_a_warning_and_request_json(server, tmp_path):
    srv, rec = server
    st = _settings(srv, push_pk={PLATFORM: ["scenic_spot_code", "publish_time", "travel_date"]},
                   output_dir=str(tmp_path))
    res = push_table(PLATFORM, _platform_df(), settings=st)
    assert res.ok and res.rows == 3 and len(rec.requests) == 1     # 照常发送
    assert "不能唯一定位一行" in res.warning and "已照常发送" in res.warning
    assert "platform_code" in res.warning                          # 给出默认 pkId 作为参考
    # 示例请求：一组 pkId 相同的行，格式与真实请求一样
    sample = json.loads(res.pk_sample)
    assert sample["tableName"] == PLATFORM
    assert sample["pkId"] == ["scenic_spot_code", "publish_time", "travel_date"]
    assert len(sample["data"]) == 3
    # 完整文件写在 output_dir/push_debug/ 下
    dump = json.loads(open(res.pk_dump, encoding="utf-8").read())
    assert res.pk_dump.startswith(str(tmp_path)) and len(dump["data"]) == 3
    assert any("pkId" in n for n in PushReport(results=[res]).notes())


def test_default_pk_passes_without_warning(server):
    srv, rec = server
    res = push_table(PLATFORM, _platform_df(), settings=_settings(srv))
    assert res.ok and not res.warning and not res.pk_sample
    assert len(rec.requests) == 1


def test_null_pk_is_sent_with_a_warning_but_empty_string_is_fine(server, tmp_path):
    srv, rec = server
    dim = "ads_trf_social_opinion_comment_dimension_score_di"
    df = pd.DataFrame([{"scenic_spot_code": "S1", "travel_date": 20260909,
                        "dimension_level1": "游玩体验", "dimension_level2": "排队时长",
                        "dimension_level3": ""}])
    res = push_table(dim, df, settings=_settings(srv, output_dir=str(tmp_path)))
    assert res.ok and not res.warning                              # 空串是合法取值
    df.loc[0, "dimension_level3"] = None
    res = push_table(dim, df, settings=_settings(srv, output_dir=str(tmp_path)))
    assert res.ok and "为空" in res.warning and len(rec.requests) == 2
