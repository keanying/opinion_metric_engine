# -*- coding: utf-8 -*-
"""数据质量检测 → 重算 → 回退 → 飞书通知。

· 区间相同的行指标必须一致（td@T-1 = latest_1d@T …），不一致算检测不通过；
· 检测不通过：重新取数重算（quality_retries 次）；还不通过就回退 —— 不写库、不推送，发飞书；
· 指标计算抛异常：也发飞书。
飞书用本机假服务器，绝不发到真实群（conftest 也清掉了 OPINION_FEISHU_* 环境变量）。
"""

import base64
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd
import pytest

import engin_cli.cli as cli
import engin_cli.pipeline as P
from engin_cli import metric_calc_domain as D
from engin_cli.loader import TABLE_CORE, TABLE_MACRO
from engin_cli.notify import build_text, send_feishu
from engin_cli.pipeline import run
from engin_cli.settings import EtlSettings
from engin_cli.validate import validate_macro_same_range


def _comment(cid, ds, word="景色好", platform="douyin"):
    return {
        "scenic_id": "S1", "scenic_name": "景区1", "channel": platform,
        "work_id": "w1", "comment_id": cid, "comment_level": "level_1",
        "root_comment_id": "", "commenter_name": "u", "content": word, "likes": 0,
        "sentiment_label": "正向", "sentiment_score": 1,
        "dimension_tags": json.dumps([{"dim1": "游玩体验", "dim2": "景色观赏",
                                       "dim3": "自然风光", "sentiment": 1}], ensure_ascii=False),
        "entity_tags": "[]", "location": "广东",
        "keyword_tags": json.dumps([word], ensure_ascii=False),
        "publish_time": f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 10:00:00",
    }


def _rows():
    rows = []
    for d in pd.date_range("2026-10-01", "2026-11-02"):
        ds = d.strftime("%Y%m%d")
        rows.append(_comment(f"a{ds}", ds))
        rows.append(_comment(f"b{ds}", ds, "人不多", "xiaohongshu"))
    return pd.DataFrame(rows)


class _Feishu(BaseHTTPRequestHandler):
    bodies = []
    reply = '{"code":0,"msg":"success"}'

    def do_POST(self):                                   # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        _Feishu.bodies.append(json.loads(self.rfile.read(n).decode("utf-8")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(_Feishu.reply.encode("utf-8"))

    def log_message(self, *a):
        pass


@pytest.fixture
def feishu():
    _Feishu.bodies, _Feishu.reply = [], '{"code":0,"msg":"success"}'
    srv = HTTPServer(("127.0.0.1", 0), _Feishu)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/open-apis/bot/v2/hook/test", _Feishu
    srv.shutdown()
    srv.server_close()


def _settings(tmp_path, webhook="", **kw):
    st = EtlSettings()
    st.write_db = False
    st.write_csv = True
    st.output_dir = str(tmp_path / "out")
    st.lookback_days = 40
    st.notify_feishu_webhook = webhook
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _fail_validate(monkeypatch, times):
    """让数据质量检测前 times 次不通过（times=None 一直不通过）。"""
    calls = {"n": 0}
    real = P.validate_all

    def fake(checks):
        calls["n"] += 1
        if times is None or calls["n"] <= times:
            return ["macro: 区间相同的行指标不一致，1 处（测试注入）"]
        return real(checks)

    monkeypatch.setattr(P, "validate_all", fake)
    return calls


# ══════════════════════════════════════════════════════════════════
# 新检测项：区间相同的行指标一致
# ══════════════════════════════════════════════════════════════════
def _macro(tmp_path):
    st = _settings(tmp_path, write_csv=False)
    return run(st, ["20261101", "20261102"], comments_df=_rows())


def test_real_output_passes_the_same_range_check(tmp_path):
    res = _macro(tmp_path)
    assert res.ok, res.errors


def test_same_range_check_catches_a_mismatch(tmp_path):
    m = _macro(tmp_path).tables[TABLE_MACRO].copy()
    specs = D.macro_granularities(None)
    assert validate_macro_same_range(m, specs) == []
    hit = (m["channel"] == "all") & (m["travel_date"] == 20261102) \
        & (m["time_granularity"] == "latest_1d")
    m.loc[hit, "wordcloud_map"] = '{"positiveWord":[],"neutralWord":[],"negativeWord":[]}'
    errs = validate_macro_same_range(m, specs)
    assert len(errs) == 1 and "区间相同的行指标不一致" in errs[0]
    assert "latest_1d@20261102" in errs[0] and "wordcloud_map" in errs[0]


def test_same_range_check_ignores_periods_with_different_lookback(tmp_path):
    """补数上限分档时，上限不同的周期补出来的明细本来就可能不同，不比。"""
    m = _macro(tmp_path).tables[TABLE_MACRO].copy()
    specs = D.macro_granularities(None)
    hit = (m["travel_date"] == 20261102) & (m["time_granularity"] == "latest_1d")
    m.loc[hit, "comment_total"] = m.loc[hit, "comment_total"] + 1
    lb = {g["name"]: 5 for g in specs}
    assert validate_macro_same_range(m, specs, lb)
    lb["latest_1d"] = 10
    assert validate_macro_same_range(m, specs, lb) == []


# ══════════════════════════════════════════════════════════════════
# 不通过 → 重算 → 回退 → 通知
# ══════════════════════════════════════════════════════════════════
def test_passing_on_retry_writes_normally(tmp_path, monkeypatch, feishu):
    url, fs = feishu
    calls = _fail_validate(monkeypatch, times=1)
    res = run(_settings(tmp_path, url), ["20261102"], comments_df=_rows())
    assert calls["n"] == 2 and res.quality_attempts == 2
    assert res.ok and not res.quality_blocked
    assert (tmp_path / "out" / f"{TABLE_CORE}.csv").exists()
    assert any("重算后通过" in n for n in res.notes)
    assert not fs.bodies                                  # 修好了，不发通知


def test_failing_twice_rolls_back_and_notifies(tmp_path, monkeypatch, feishu):
    url, fs = feishu
    calls = _fail_validate(monkeypatch, times=None)
    st = _settings(tmp_path, url, push_enabled=True, push_url=url.replace("/open-apis", "/push"))
    res = run(st, ["20261101", "20261102"], comments_df=_rows(), scenic_codes=["S1"])
    assert calls["n"] == 2                               # 算 1 次 + 重算 1 次
    assert res.quality_blocked and not res.ok
    assert not (tmp_path / "out").exists()               # 回退：一个文件都没写
    assert res.load_results == [] and res.push is None   # 没写库、没推送
    assert len(fs.bodies) == 1                           # 只有飞书通知，没有推送请求
    text = fs.bodies[0]["content"]["text"]
    assert fs.bodies[0]["msg_type"] == "text"
    assert "数据质量检测不通过，已回退" in text and "S1" in text
    assert "20261101 ~ 20261102" in text and "测试注入" in text and "计算次数：2" in text


def test_retries_are_configurable(tmp_path, monkeypatch):
    calls = _fail_validate(monkeypatch, times=None)
    run(_settings(tmp_path, quality_retries=3), ["20261102"], comments_df=_rows())
    assert calls["n"] == 4
    calls = _fail_validate(monkeypatch, times=None)
    run(_settings(tmp_path, quality_retries=0), ["20261102"], comments_df=_rows())
    assert calls["n"] == 1


def test_gate_off_still_writes_but_notifies(tmp_path, monkeypatch, feishu):
    url, fs = feishu
    _fail_validate(monkeypatch, times=None)
    res = run(_settings(tmp_path, url, quality_gate=False), ["20261102"], comments_df=_rows())
    assert not res.quality_blocked and not res.ok
    assert (tmp_path / "out" / f"{TABLE_CORE}.csv").exists()
    assert "照常写库" in fs.bodies[0]["content"]["text"]


def test_no_data_never_reaches_the_loader(tmp_path, monkeypatch):
    """取数为空时就算关掉闸门也不进入落库（不然会按景区 × 日期把库里已有数据删掉）。"""
    loaded = []
    monkeypatch.setattr(P, "load_all", lambda *a, **k: loaded.append(1) or [])
    res = run(_settings(tmp_path, quality_gate=False), ["20261102"],
              comments_df=_rows().iloc[0:0])
    assert not res.ok and not loaded


def test_no_webhook_means_no_notification(tmp_path, monkeypatch):
    _fail_validate(monkeypatch, times=None)
    res = run(_settings(tmp_path), ["20261102"], comments_df=_rows())
    assert res.quality_blocked                           # 照样回退，只是没地方通知


# ══════════════════════════════════════════════════════════════════
# 命令行：异常也通知；回退在汇总里写清楚
# ══════════════════════════════════════════════════════════════════
def _cli(tmp_path, *argv):
    src = tmp_path / "comments.csv"
    _rows().to_csv(src, index=False, encoding="utf-8-sig")
    return cli.main(["run", "--comments-csv", str(src), "--skip-db", "--no-push",
                     "--rerun-days", "0", "--output-dir", str(tmp_path / "out"),
                     "--date", "20261102", *argv])


def test_cli_exception_sends_notification(tmp_path, monkeypatch, feishu, capsys):
    url, fs = feishu
    monkeypatch.setenv("OPINION_FEISHU_WEBHOOK", url)

    def boom(*a, **k):
        raise RuntimeError("除零了")

    monkeypatch.setattr(cli, "run_pipeline", boom)
    assert _cli(tmp_path) == 1
    text = fs.bodies[0]["content"]["text"]
    assert "指标计算异常" in text and "S1" in text and "RuntimeError: 除零了" in text


def test_cli_reports_rollback(tmp_path, monkeypatch, feishu, capsys):
    url, fs = feishu
    monkeypatch.setenv("OPINION_FEISHU_WEBHOOK", url)
    _fail_validate(monkeypatch, times=None)
    assert _cli(tmp_path) == 1
    out = capsys.readouterr().out
    assert "已回退" in out and "已发飞书通知" in out
    assert len(fs.bodies) == 1


def test_cli_no_quality_gate_and_retries_flags(tmp_path, monkeypatch, feishu):
    url, fs = feishu
    calls = _fail_validate(monkeypatch, times=None)
    _cli(tmp_path, "--no-quality-gate", "--quality-retries", "0")
    assert calls["n"] == 1
    assert (tmp_path / "out" / "S1").exists()            # 闸门关了，照常写


def test_notify_test_command(feishu, capsys):
    url, fs = feishu
    assert cli.main(["notify-test", "--webhook", url]) == 0
    assert "通知测试" in fs.bodies[0]["content"]["text"]
    assert cli.main(["notify-test"]) == 2                # 没配 webhook


# ══════════════════════════════════════════════════════════════════
# 飞书报文
# ══════════════════════════════════════════════════════════════════
def test_signature_when_secret_is_set(feishu):
    url, fs = feishu
    st = EtlSettings()
    st.notify_feishu_webhook, st.notify_feishu_secret = url, "s3cr3t"
    assert send_feishu(st, "标题", ["a"]) is True
    b = fs.bodies[0]
    key = f"{b['timestamp']}\ns3cr3t".encode()
    assert b["sign"] == base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


def test_feishu_error_reply_and_unreachable_do_not_raise(feishu):
    url, fs = feishu
    st = EtlSettings()
    st.notify_feishu_webhook = url
    fs.reply = '{"code":19021,"msg":"sign match fail"}'
    assert send_feishu(st, "t") is False
    st.notify_feishu_webhook = "http://127.0.0.1:1/hook"
    assert send_feishu(st, "t") is False
    st.notify_feishu_webhook = ""
    assert send_feishu(st, "t") is None


def test_long_messages_are_truncated():
    text = build_text("T", [f"第 {i} 条" + "x" * 500 for i in range(40)])
    lines = text.split("\n")
    assert lines[0] == "T"
    assert any("另有 25 条" in x for x in lines)
    assert all(len(x) <= 302 for x in lines[1:-2])
