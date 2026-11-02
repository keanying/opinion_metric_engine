# -*- coding: utf-8 -*-
"""指标计算服务（engin_cli/service）：存储、定时、执行器、HTTP 接口。

执行器用真实子进程跑 engin_cli（离线 CSV、不写库、不推送）；
数据库用假的（按 SQL 返回 DataFrame），不连任何真实服务。
"""

import base64
import datetime as dt
import json
import os
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.request

import pandas as pd
import pytest

import engin_cli.cli as cli
from engin_cli.loader import TABLE_MACRO
from engin_cli.pipeline import run as run_pipeline
from engin_cli.service import runner as R
from engin_cli.service import scheduler as SC
from engin_cli.service.server import App, make_server
from engin_cli.service.store import Store
from engin_cli.settings import EtlSettings


def _comment(cid, ds, word="景色好", platform="douyin", scenic="S1"):
    return {
        "scenic_id": scenic, "scenic_name": "景区1", "channel": platform,
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


def _wait(fn, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError("等待超时")


# ══════════════════════════════════════════════════════════════════
# 任务配置 → 命令
# ══════════════════════════════════════════════════════════════════
def test_build_run_argv_from_task_config():
    argv, title = R.build_run_argv(
        {"scenics": "A， B", "date_mode": "recent", "recent_days": 3, "push": "drill,macro",
         "rerun_days": 0, "drill_keyword_filter": True, "quality_gate": False,
         "quality_retries": 2, "verbose": False}, dt.date(2026, 11, 2))
    assert argv == ["run", "--start-date", "20261031", "--end-date", "20261102", "--no-progress",
                    "--scenic", "A,B", "--rerun-days", "0", "--push", "drill,macro",
                    "--drill-keyword-filter", "--no-quality-gate", "--quality-retries", "2"]
    assert title == "A,B · 20261031~20261102"


@pytest.mark.parametrize("mode, want", [("today", ("20261102", "20261102")),
                                         ("yesterday", ("20261101", "20261101"))])
def test_task_dates(mode, want):
    assert R.task_dates(R.normalize_config({"date_mode": mode}), dt.date(2026, 11, 2)) == want


def test_defaults_and_no_push():
    argv, _ = R.build_run_argv({"push": "", "skip_db": True}, dt.date(2026, 11, 2))
    assert "--no-push" in argv and "--skip-db" in argv and "-v" in argv
    assert "--scenic" not in argv and "--rerun-days" not in argv


@pytest.mark.parametrize("cfg, msg", [({"date_mode": "x"}, "日期方式"),
                                      ({"date_mode": "range", "start_date": "2026"}, "固定区间"),
                                      ({"date_mode": "range", "start_date": "20261102",
                                        "end_date": "20261101"}, "晚于")])
def test_bad_config_is_rejected(cfg, msg):
    with pytest.raises(ValueError, match=msg):
        R.normalize_config(cfg)


def test_build_push_argv():
    argv, title = R.build_push_argv("S1", "drill,macro", "20261101", "")
    assert argv == ["push", "--no-progress", "--tables", "drill,macro", "--scenic", "S1",
                    "--start-date", "20261101", "--end-date", "20261101"]
    with pytest.raises(ValueError):
        R.build_push_argv(start="2026-11-01")


def test_final_status():
    assert R.final_status(0, {}) == "success"
    assert R.final_status(1, {"scenics": [{"quality_blocked": True}]}) == "blocked"
    assert R.final_status(1, {"scenics": [{"ok": False}]}) == "failed"


# ══════════════════════════════════════════════════════════════════
# 定时
# ══════════════════════════════════════════════════════════════════
def test_normalize_times():
    assert SC.normalize_times("20:10，8:05, 08:05") == ["08:05", "20:10"]
    with pytest.raises(ValueError):
        SC.normalize_times(["24:00"])


def test_due_slots_grace_and_once_a_day():
    now = dt.datetime(2026, 11, 2, 8, 20)
    assert SC.due_slots(["08:10"], {}, now) == ["08:10"]
    assert SC.due_slots(["08:10"], {"08:10": "2026-11-02"}, now) == []      # 今天触发过了
    assert SC.due_slots(["08:10"], {"08:10": "2026-11-01"}, now) == ["08:10"]
    assert SC.due_slots(["07:00"], {}, now) == []                             # 错过太久，不补
    assert SC.due_slots(["09:00"], {}, now) == []                             # 还没到点


def test_next_fire():
    now = dt.datetime(2026, 11, 2, 8, 20)
    assert SC.next_fire(["08:10", "20:10"], now) == dt.datetime(2026, 11, 2, 20, 10)
    assert SC.next_fire(["08:10"], now) == dt.datetime(2026, 11, 3, 8, 10)
    assert SC.next_fire([], now) is None


class _FakeRunner:
    current = None

    def __init__(self):
        self.submitted = []

    def queued(self):
        return 0

    def submit(self, kind, argv, title, trigger="manual", task=None):
        self.submitted.append((kind, argv, title, trigger, task["id"] if task else None))
        return len(self.submitted)


def test_scheduler_tick_fires_once(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    on = store.create_task("每日", {"scenics": "S1"}, {"times": ["08:10"]})
    store.create_task("停用", {}, {"times": ["08:10"]}, enabled=False)
    store.create_task("仅手动", {}, {"times": []})
    fr = _FakeRunner()
    s = SC.Scheduler(store, fr)
    now = dt.datetime(2026, 11, 2, 8, 11)
    assert len(s.tick(now)) == 1
    assert fr.submitted[0][3] == "schedule" and fr.submitted[0][4] == on
    assert "--start-date" in fr.submitted[0][1] and "20261102" in fr.submitted[0][1]
    assert s.tick(now + dt.timedelta(minutes=1)) == []                         # 不重复
    assert store.get_task(on)["last_fired"] == {"08:10": "2026-11-02"}
    assert len(s.tick(now + dt.timedelta(days=1))) == 1                        # 第二天照常
    ev = store.list_events(on)
    assert "到点，已触发执行" in ev[0]["text"] and ev[0]["task_name"] == "每日"


def test_scheduler_switch_and_missed_slots_are_logged(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    tid = store.create_task("每日", {}, {"times": ["08:10"]})
    store._exec("update tasks set updated_at='2026-11-01 00:00:00'")
    fr = _FakeRunner()
    s = SC.Scheduler(store, fr)
    s.set_enabled(False)
    assert s.tick(dt.datetime(2026, 11, 2, 8, 11)) == []                       # 总开关关着不触发
    assert s.tick(dt.datetime(2026, 11, 2, 8, 50)) == []
    texts = [e["text"] for e in store.list_events(tid)]
    assert any("跳过 08:10：定时总开关关着" in t for t in texts)
    assert any("定时总开关已关闭" in t for t in texts)
    assert s.tick(dt.datetime(2026, 11, 2, 9, 30)) == []                       # 只记一次
    assert sum("跳过" in e["text"] for e in store.list_events(tid)) == 1
    s.set_enabled(True)
    assert len(s.tick(dt.datetime(2026, 11, 3, 8, 12))) == 1
    # 服务停机错过的
    s.tick(dt.datetime(2026, 11, 4, 12, 0))
    assert "服务当时没在运行" in store.list_events(tid)[0]["text"]


def test_task_tables_selection_and_push_switch():
    argv, _ = R.build_run_argv({"tables": "macro,drill", "push": "drill,core"}, dt.date(2026, 11, 2))
    assert argv[argv.index("--tables") + 1] == "drill,macro"
    assert argv[argv.index("--push") + 1] == "drill"                          # 没算的表推不了
    with pytest.raises(ValueError, match="不认识"):
        R.normalize_config({"tables": "foo"})
    off = R.apply_push_switch(argv, False)
    assert "--push" not in off and off[-1] == "--no-push" and "drill,macro" in off   # --tables 的值保留
    assert R.apply_push_switch(["push", "--tables", "drill"], False) == ["push", "--tables", "drill"]


def test_push_settings_normalize_and_overrides():
    ps = R.normalize_push_settings({"push_url": " https://x/sync ", "push_batch_size": "200",
                                    "push_timeout": "", "push_dry_run": 1})
    assert ps["push_url"] == "https://x/sync" and ps["push_batch_size"] == 200
    assert R.push_overrides(ps) == {"push_url": "https://x/sync", "push_batch_size": 200,
                                    "push_dry_run": True}
    with pytest.raises(ValueError, match="http"):
        R.normalize_push_settings({"push_url": "ftp://x"})


def test_settings_json_overrides(monkeypatch):
    from engin_cli.settings import load_settings
    monkeypatch.setenv("OPINION_IGNORE_LOCAL_SETTINGS", "1")
    monkeypatch.setenv("OPINION_SETTINGS_JSON", json.dumps(
        {"push_url": "https://p/sync", "push_batch_size": "50", "push_dry_run": True,
         "db": {"host": "evil"}, "push_headers": {"a": "b"}}))
    st = load_settings()
    assert st.push_url == "https://p/sync" and st.push_batch_size == 50 and st.push_dry_run
    assert st.db.host != "evil" and not st.push_headers                       # 只认推送参数


# ══════════════════════════════════════════════════════════════════
# 存储
# ══════════════════════════════════════════════════════════════════
def test_store_runs_and_cleanup(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    tid = st.create_task("t", {}, {})
    rid = st.create_run("run", ["run"], "说明", "manual", st.get_task(tid))
    st.update_run(rid, status="running", log_path=str(tmp_path / "1.log"))
    assert st.mark_interrupted() == 1
    r = st.get_run(rid)
    assert r["status"] == "failed" and "重启" in r["message"] and r["task_name"] == "t"
    st.update_run(rid, created_at="2020-01-01 00:00:00",
                  summary={"scenics": [{"ok": False, "quality_blocked": True}]})
    row = st.list_runs()["items"][0]
    assert row["scenic_total"] == 1 and row["quality_blocked"] == 1
    assert st.delete_runs_before("2021-01-01") == [str(tmp_path / "1.log")]
    assert st.list_runs()["total"] == 0


# ══════════════════════════════════════════════════════════════════
# 执行器：真实子进程
# ══════════════════════════════════════════════════════════════════
@pytest.fixture
def runner(tmp_path):
    store = Store(str(tmp_path / "svc" / "s.db"))
    r = R.Runner(store, str(tmp_path / "svc"))
    r.start()
    yield r
    r.stop()


def _done(runner, rid):
    return _wait(lambda: (lambda x: x if x["status"] not in ("queued", "running") else None)(
        runner.store.get_run(rid)), timeout=120)


def test_runner_runs_engin_cli_and_reads_the_summary(runner, tmp_path):
    src = tmp_path / "comments.csv"
    _rows().to_csv(src, index=False, encoding="utf-8-sig")
    argv = ["run", "--comments-csv", str(src), "--skip-db", "--no-push", "--rerun-days", "0",
            "--output-dir", str(tmp_path / "out"), "--date", "20261102", "--no-progress"]
    rid = runner.submit("run", argv, "测试")
    run = _done(runner, rid)
    assert run["status"] == "success" and run["exit_code"] == 0
    s = run["summary"]["scenics"]
    assert [x["scenic"] for x in s] == ["S1"] and s[0]["ok"] and s[0]["quality_attempts"] == 1
    assert s[0]["tables"][TABLE_MACRO] > 0
    with open(run["log_path"], encoding="utf-8") as f:
        log = f.read()
    assert "engin_cli.cli run" in log and "校验通过" in log


def test_runner_failure_and_cancel(runner, tmp_path):
    rid = runner.submit("notify", ["notify-test"], "通知测试")
    run = _done(runner, rid)
    assert run["status"] == "failed" and run["exit_code"] == 2            # 没配 webhook
    # 取消：换一个会一直跑的「python」
    fake = tmp_path / "slow.sh"
    fake.write_text("#!/bin/sh\necho started\nsleep 30\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    runner.python = str(fake)
    rid1 = runner.submit("run", ["run"], "慢")
    rid2 = runner.submit("run", ["run"], "排队中")
    _wait(lambda: runner.store.get_run(rid1)["status"] == "running")
    assert runner.cancel(rid2)                                             # 排队中的直接取消
    assert runner.store.get_run(rid2)["status"] == "cancelled"
    t0 = time.time()
    assert runner.cancel(rid1)
    assert _done(runner, rid1)["status"] == "cancelled"
    assert time.time() - t0 < 10
    assert not runner.cancel(rid1)                                         # 已结束的不能再取消


# ══════════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════════
class FakeDB:
    """按 SQL 返回大盘表数据（来自一次真实跑批）。"""

    class cfg:                                   # noqa: N801
        @staticmethod
        def ads_db():
            return "ads"

    def __init__(self, macro):
        self.macro = macro

    def query_df(self, sql, params=()):
        m = self.macro
        if "group by scenic_id" in sql:
            return pd.DataFrame([{"scenic_id": "S1", "scenic_name": "景区1",
                                  "first_date": int(m.travel_date.min()), "last_date": int(m.travel_date.max())}])
        if "max(travel_date) d" in sql:
            return pd.DataFrame([{"d": int(m.travel_date.max())}])
        if "time_granularity=%s order by channel" in sql:
            sc, d, g = params
            return m[(m.travel_date == d) & (m.time_granularity == g)].sort_values("channel")
        if "channel=%s" in sql:
            sc, ch, g = params[:3]
            r = m[(m.channel == ch) & (m.time_granularity == g)].sort_values("travel_date", ascending=False)
            return r[["travel_date", "comment_total", "overall_sentiment_score", "positive_comment_cnt",
                      "neutral_comment_cnt", "negative_comment_cnt"]]
        if "between" in sql:
            sc, lo, hi = params
            return m[m.travel_date.between(lo, hi)]
        raise AssertionError(sql)

    def close(self):
        pass


@pytest.fixture(scope="module")
def macro():
    st = EtlSettings()
    st.write_db = st.write_csv = False
    st.lookback_days = 40
    return run_pipeline(st, ["20261031", "20261101", "20261102"], comments_df=_rows()).tables[TABLE_MACRO]


@pytest.fixture
def server(tmp_path, macro):
    holder = {"macro": macro.copy()}
    st = EtlSettings()
    st.notify_feishu_webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token-xyz"
    st.db.password = "dbpass"
    st.push_headers = {"Authorization": "Bearer sk_supersecret"}
    app = App(st, data_dir=str(tmp_path / "svc"), scheduler_enabled=False,
              db_factory=lambda: FakeDB(holder["macro"]))
    srv = make_server(app, "127.0.0.1", 0, password="")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", app, holder
    srv.shutdown()
    srv.server_close()
    app.stop()


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _call(base, path, method="GET", body=None, headers=None, raw=False):
    h = {"X-Requested-With": "t"} if method != "GET" else {}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        with _OPENER.open(req, timeout=30) as r:
            content = r.read()
            return r.status, (content if raw else json.loads(content))
    except urllib.error.HTTPError as e:
        content = e.read()
        try:
            return e.code, json.loads(content)
        except ValueError:
            return e.code, content


def test_static_page_and_vendor(server):
    base, _, _ = server
    code, html = _call(base, "/", raw=True)
    assert code == 200 and b'id="app"' in html and b"vue.esm-browser.prod.js" in html
    code, js = _call(base, "/vendor/vue.esm-browser.prod.js", raw=True)
    assert code == 200 and b"vue v3" in js[:200]
    # 路径穿越：拿不到 static 以外的文件，回首页
    code, body = _call(base, "/../../settings.py", raw=True)
    assert code == 200 and b'id="app"' in body


def test_task_crud_and_manual_run(server):
    base, app, _ = server
    code, t = _call(base, "/api/tasks", "POST", {"name": "每日", "schedule": {"times": ["8:10"]},
                                                 "config": {"scenics": "S1", "push": ""}})
    assert code == 200 and t["schedule"]["times"] == ["08:10"]
    assert "--no-push" in t["command_preview"] and t["next_fire"]
    code, e = _call(base, "/api/tasks", "POST", {"name": "", "config": {}})
    assert code == 400 and "名称" in e["error"]
    code, t2 = _call(base, f"/api/tasks/{t['id']}", "PUT", {"name": "改名", "enabled": False,
                                                           "config": {"date_mode": "yesterday"}, "schedule": {}})
    assert t2["name"] == "改名" and not t2["enabled"] and t2["next_fire"] is None
    app.runner.python = sys.executable
    code, r = _call(base, f"/api/tasks/{t['id']}/run", "POST", {})
    assert code == 200 and r["run_id"]
    code, runs = _call(base, "/api/runs")
    assert runs["total"] == 1 and runs["items"][0]["task_name"] == "改名"
    code, detail = _call(base, f"/api/runs/{r['run_id']}")
    assert detail["command"][0] == "run" and "log_path" not in detail
    _done(app.runner, r["run_id"])
    code, lg = _call(base, f"/api/runs/{r['run_id']}/log?offset=0")
    assert lg["done"] and "engin_cli.cli run" in lg["text"]
    code, lg2 = _call(base, f"/api/runs/{r['run_id']}/log?offset={lg['offset']}")
    assert lg2["text"] == ""                                     # 增量拉取
    assert _call(base, f"/api/tasks/{t['id']}", "DELETE")[0] == 200
    assert _call(base, "/api/tasks")[1] == []
    assert _call(base, "/api/runs/9999")[0] == 404


def test_csrf_header_required(server):
    base, _, _ = server
    code, e = _call(base, "/api/tasks", "POST", {"name": "x"}, headers={"X-Requested-With": ""})
    assert code == 403


def test_overview_and_quality_views(server):
    base, app, _ = server
    rid = app.store.create_run("run", ["run"], "说明", "schedule")
    app.store.update_run(rid, status="blocked", summary={"scenics": [
        {"scenic": "S1", "ok": False, "quality_blocked": True, "quality_attempts": 2,
         "errors": ["macro: 区间相同的行指标不一致，1 处"]}]})
    code, ov = _call(base, "/api/overview")
    assert code == 200 and ov["week"]["blocked"] == 1 and ov["scenic_results"]["blocked"] == 1
    assert len(ov["daily"]) == 14 and ov["recent"][0]["id"] == rid
    code, q = _call(base, "/api/quality?days=7")
    assert q["items"][0]["quality_blocked"] and q["items"][0]["attempts"] == 2


def test_metrics_endpoints(server):
    base, _, _ = server
    code, sc = _call(base, "/api/metrics/scenics")
    assert sc[0]["scenic_id"] == "S1"
    code, cell = _call(base, "/api/metrics/cell?scenic=S1&granularity=latest_7d")
    assert cell["date"] == "20261102"
    all_row = [r for r in cell["rows"] if r["channel"] == "all"][0]
    assert isinstance(all_row["wordcloud_map"], dict) and isinstance(all_row["period_comment_heatmap"], list)
    code, trend = _call(base, "/api/metrics/trend?scenic=S1&channel=all")
    assert [x["travel_date"] for x in trend] == sorted(x["travel_date"] for x in trend)
    assert _call(base, "/api/metrics/cell")[0] == 400


def test_online_quality_check_on_db_data(server):
    base, _, holder = server
    body = {"scenic": "S1", "start": "20261031", "end": "20261102"}
    code, r = _call(base, "/api/quality/check", "POST", body)
    assert code == 200 and r["errors"] == [] and r["rows"] > 0
    m = holder["macro"]
    hit = (m.channel == "douyin") & (m.travel_date == 20261101) & (m.time_granularity == "td")
    m.loc[hit, "comment_total"] += 1
    code, r = _call(base, "/api/quality/check", "POST", body)
    assert any("区间相同的行指标不一致" in e for e in r["errors"])
    assert _call(base, "/api/quality/check", "POST", {"scenic": "S1"})[0] == 400


def test_config_view_masks_secrets(server):
    base, _, _ = server
    code, cfg = _call(base, "/api/config")
    text = json.dumps(cfg, ensure_ascii=False)
    assert "dbpass" not in text and "sk_supersecret" not in text and "secret-token-xyz" not in text
    assert cfg["数据库"]["password"] == "已配置"


def test_maintenance_actions(server, tmp_path):
    base, app, _ = server
    code, r = _call(base, "/api/maint/push", "POST", {"tables": "drill", "start": "bad"})
    assert code == 400
    code, r = _call(base, "/api/maint/notify-test", "POST", {})
    assert code == 200 and _done(app.runner, r["run_id"])["kind"] == "notify"
    code, r = _call(base, "/api/maint/run", "POST", {"config": {"date_mode": "range",
                                                                "start_date": "20261101",
                                                                "end_date": "20261102", "push": ""}})
    assert code == 200
    assert app.store.get_run(r["run_id"])["title"].startswith("临时")
    code, c = _call(base, "/api/maint/cleanup", "POST", {"days": 1})
    assert code == 200 and c["deleted_runs"] == 0                       # 7 天下限，刚跑的不删


def test_password_protection(tmp_path):
    app = App(EtlSettings(), data_dir=str(tmp_path / "svc"), scheduler_enabled=False)
    srv = make_server(app, "127.0.0.1", 0, password="p@ss")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert _call(base, "/api/health")[0] == 401
        assert _call(base, "/", raw=True)[0] == 401
        bad = base64.b64encode(b"admin:wrong").decode()
        assert _call(base, "/api/health", headers={"Authorization": "Basic " + bad})[0] == 401
        ok = base64.b64encode("admin:p@ss".encode()).decode()
        assert _call(base, "/api/health", headers={"Authorization": "Basic " + ok})[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_refuses_public_bind_without_password(tmp_path):
    app = App(EtlSettings(), data_dir=str(tmp_path / "svc"), scheduler_enabled=False)
    with pytest.raises(ValueError, match="访问密码"):
        make_server(app, "0.0.0.0", 0, password="")


def test_run_summary_json_from_cli(tmp_path):
    src = tmp_path / "comments.csv"
    _rows().to_csv(src, index=False, encoding="utf-8-sig")
    out = tmp_path / "sum.json"
    rc = cli.main(["run", "--comments-csv", str(src), "--skip-db", "--no-push", "--rerun-days", "0",
                   "--output-dir", str(tmp_path / "o"), "--date", "20261102", "--no-progress",
                   "--summary-json", str(out)])
    assert rc == 0
    s = json.loads(out.read_text(encoding="utf-8"))
    assert s["exit_code"] == 0 and s["dates"] == ["20261102"]
    assert s["scenics"][0]["scenic"] == "S1" and s["scenics"][0]["ok"]


def test_run_tables_only_outputs_selected(tmp_path):
    from engin_cli.loader import TABLE_DRILL_ANALYSIS
    src = tmp_path / "comments.csv"
    _rows().to_csv(src, index=False, encoding="utf-8-sig")
    out = tmp_path / "o"
    rc = cli.main(["run", "--comments-csv", str(src), "--skip-db", "--no-push", "--rerun-days", "0",
                   "--output-dir", str(out), "--date", "20261102", "--no-progress",
                   "--tables", "macro", "--summary-json", str(tmp_path / "s.json")])
    assert rc == 0
    s = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["scenics"][0]
    assert list(s["tables"]) == [TABLE_MACRO]                     # 下钻表没算、基础四表没输出
    files = sorted(p.name for p in out.rglob("*.csv"))
    assert files == [TABLE_MACRO + ".csv"]
    assert TABLE_DRILL_ANALYSIS not in s["tables"]


def test_runner_push_switch_and_overrides_reach_subprocess(runner, tmp_path):
    runner.store.set_kv("push", {"enabled": False, "push_url": "https://p.example/sync"})
    fake = tmp_path / "echo.sh"
    fake.write_text("#!/bin/sh\necho ARGS=\"$*\"\necho JSON=\"$OPINION_SETTINGS_JSON\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    runner.python = str(fake)
    rid = runner.submit("run", ["run", "--date", "20261102", "--push", "drill"], "t")
    run = _done(runner, rid)
    text = open(run["log_path"], encoding="utf-8").read()
    assert "推送总开关已关闭" in text and "--no-push" in text and "--push drill" not in text
    assert '"push_url": "https://p.example/sync"' in text
    # 总开关开着：不加那句提示
    runner.store.set_kv("push", {"enabled": True})
    rid = runner.submit("run", ["run", "--date", "20261102", "--push", "drill"], "t")
    text = open(_done(runner, rid)["log_path"], encoding="utf-8").read()
    assert "推送总开关已关闭" not in text and "--push drill" in text and "JSON=\n" in text


def test_push_settings_and_logs_api(server, tmp_path, monkeypatch):
    base, app, _ = server
    code, v = _call(base, "/api/push/settings")
    assert v["settings"]["enabled"] and "sk_supersecret" not in json.dumps(v)
    code, e = _call(base, "/api/push/settings", "PUT", {"push_url": "ftp://x"})
    assert code == 400
    code, v = _call(base, "/api/push/settings", "PUT", {"enabled": False, "push_batch_size": "100"})
    assert not v["settings"]["enabled"] and v["effective"]["push_batch_size"] == 100
    assert "推送总开关已关闭" in app.store.list_events()[0]["text"]
    d = tmp_path / "pl"
    d.mkdir()
    (d / "push_20261102.log").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"time": "2026-11-02 08:00:00", "table": "t1", "rows": 10, "result": "ok", "url": "https://secret"},
        {"time": "2026-11-02 08:00:01", "table": "t1", "rows": 5, "result": "http_error", "response": "502"},
    ]) + "\nnot json\n", encoding="utf-8")
    monkeypatch.setenv("OPINION_PUSH_LOG_DIR", str(d))
    code, lg = _call(base, "/api/push/logs")
    assert lg["date"] == "20261102" and lg["counts"] == {"ok": 1, "http_error": 1} and lg["rows_ok"] == 10
    assert lg["items"][0]["result"] == "http_error" and "url" not in lg["items"][1]
    code, lg = _call(base, "/api/push/logs?result=ok")
    assert [x["result"] for x in lg["items"]] == ["ok"]
    assert _call(base, "/api/push/logs?date=bad")[0] == 400


def test_scheduler_api_and_task_preview(server):
    base, app, _ = server
    code, sv = _call(base, "/api/scheduler")
    assert sv["available"] is False                                    # 测试里没启动定时
    assert _call(base, "/api/scheduler", "PUT", {"enabled": False})[0] == 400
    code, pv = _call(base, "/api/tasks/preview", "POST", {"config": {"tables": "drill", "push": "drill,macro"},
                                                         "schedule": {"times": ["08:10", "20:10"]}})
    assert pv["ok"] and pv["tables"] == ["drill"] and pv["push"] == ["drill"] and len(pv["next_fires"]) == 3
    assert "--tables drill" in pv["command"]
    code, pv = _call(base, "/api/tasks/preview", "POST", {"config": {"date_mode": "x"}})
    assert not pv["ok"] and "日期方式" in pv["error"]
    code, ov = _call(base, "/api/overview")
    assert "durations" in ov and len(ov["durations"]) == 14 and "today_slots" in ov
