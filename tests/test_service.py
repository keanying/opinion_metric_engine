# -*- coding: utf-8 -*-
"""指标计算服务（engin_cli/service）：存储、定时、执行器、HTTP 接口。

执行器用真实子进程跑 engin_cli（离线 CSV、不写库、不推送）；
数据库用假的（按 SQL 返回 DataFrame），不连任何真实服务。
"""

import base64
import datetime as dt
import http.cookiejar
import json
import os
import re
import secrets
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
from engin_cli.service.auth import Auth, hash_password
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


# 测试用的假密码 / 假令牌：运行时随机生成，不在代码里写死（免得被密钥扫描当成泄露）
FAKE_DB_PW = "db-" + secrets.token_hex(6)
FAKE_TOKEN = "tok-" + secrets.token_hex(8)
FAKE_HOOK = "hook-" + secrets.token_hex(8)
FAKE_SVC_PW = "svc-" + secrets.token_hex(6)


def _sqlite_settings():
    st = EtlSettings()
    st.service_store = "sqlite"
    return st


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
    with pytest.raises(ValueError, match="日期"):          # 补推从库里读，日期必填
        R.build_push_argv("S1", "drill")


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

    def submit(self, kind, argv, title, trigger="manual", task=None, then=None):
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
    store._exec(f"update {store.t_task} set update_time='2026-11-01 00:00:00'")
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
            "--date", "20261102", "--no-progress"]
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
    st.write_db = False
    st.lookback_days = 40
    return run_pipeline(st, ["20261031", "20261101", "20261102"], comments_df=_rows()).tables[TABLE_MACRO]


@pytest.fixture
def server(tmp_path, macro):
    holder = {"macro": macro.copy()}
    st = EtlSettings()
    st.service_store = "sqlite"
    st.notify_feishu_webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/" + FAKE_HOOK
    st.db.password = FAKE_DB_PW
    st.push_headers = {"Authorization": "Bearer " + FAKE_TOKEN}
    app = App(st, data_dir=str(tmp_path / "svc"), scheduler_enabled=False,
              db_factory=lambda: FakeDB(holder["macro"]), auth=Auth(FakeUsers()))
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    _JAR.clear()                                          # 每个用例重新登录（会话 Cookie 放在 _JAR 里）
    assert _call(base, "/api/login", "POST", {"username": "op", "password": FAKE_SVC_PW})[0] == 200
    yield base, app, holder
    srv.shutdown()
    srv.server_close()
    app.stop()


_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(_JAR))


class FakeUsers:
    """假的用户 / 会话表（同 auth.UserRepository 的接口），字段同 SMC 的 src_opinion_sys_user。"""
    table = "`src`.`src_opinion_sys_user`"

    def __init__(self):
        self.rows = {r["username"]: r for r in [
            {"id": 1, "username": "op", "display_name": "运维小王", "role": "operator", "enabled": 1},
            {"id": 2, "username": "boss", "display_name": "", "role": "admin", "enabled": 1},
            {"id": 3, "username": "look", "display_name": "只读同学", "role": "viewer", "enabled": 1},
            {"id": 4, "username": "gone", "display_name": "离职", "role": "operator", "enabled": 0}]}
        for r in self.rows.values():
            r.update(password_hash=hash_password(FAKE_SVC_PW, iterations=1000), failed_count=0,
                     locked_until=None, last_login_at=None, last_login_ip="")
        self.sessions = {}

    def _by_id(self, uid):
        return next(r for r in self.rows.values() if r["id"] == uid)

    def get_with_hash(self, username):
        r = self.rows.get(username)
        if not r:
            return None
        return dict(r, is_locked=int(bool(r["locked_until"] and r["locked_until"] > dt.datetime.now())))

    def record_failure(self, uid, max_failed, lock_minutes):
        r = self._by_id(uid)
        r["failed_count"] += 1
        if r["failed_count"] >= max_failed:
            r["locked_until"] = dt.datetime.now() + dt.timedelta(minutes=lock_minutes)
        return bool(r["locked_until"] and r["locked_until"] > dt.datetime.now())

    def record_success(self, uid, ip):
        self._by_id(uid).update(failed_count=0, locked_until=None, last_login_at=dt.datetime.now(),
                                last_login_ip=ip)

    def create_session(self, uid, hours, ip, ua):
        token = secrets.token_urlsafe(16)
        self.sessions[token] = uid
        return token

    def session_user(self, token):
        r = self._by_id(self.sessions[token]) if token in self.sessions else None
        return dict(r) if r and r["enabled"] else None

    def touch_session(self, token, hours):
        pass

    def delete_session(self, token):
        self.sessions.pop(token, None)


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


def test_static_cache_gzip_and_keep_alive(server):
    """静态文件：ETag + no-cache（没改回 304）、gzip；长连接下没读的请求体不会串到下一个请求。"""
    import gzip as _gz
    import http.client
    base, _, _ = server
    host, port = base.replace("http://", "").split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    c.request("GET", "/js/app.js", headers={"Accept-Encoding": "gzip"})
    r = c.getresponse(); body = r.read()
    assert r.status == 200 and r.getheader("Content-Encoding") == "gzip" and r.getheader("Cache-Control") == "no-cache"
    assert b"createApp" in _gz.decompress(body)
    etag = r.getheader("ETag")
    c.request("GET", "/js/app.js", headers={"If-None-Match": etag})            # 同一个连接（长连接）
    r = c.getresponse(); assert r.status == 304 and r.read() == b""
    c.request("GET", "/vendor/vue.esm-browser.prod.js")
    r = c.getresponse(); r.read()
    assert "max-age" in r.getheader("Cache-Control") and r.getheader("Content-Encoding") is None   # 没说支持 gzip 不压
    # 缺 X-Requested-With 被拒的写请求：请求体也读掉了，下一个请求照常
    c.request("POST", "/api/tasks", body=b'{"name": "x"}', headers={"Content-Type": "application/json"})
    r = c.getresponse(); assert r.status == 403; r.read()
    c.request("GET", "/api/task-defaults", headers={"Accept-Encoding": "gzip"})
    r = c.getresponse(); assert r.status in (200, 401); r.read()
    c.close()


def test_db_pool_reuses_and_rolls_back():
    from engin_cli.service.server import DbPool

    class Conn:
        def __init__(self): self.rollbacks = 0; self.pings = 0
        def rollback(self): self.rollbacks += 1
        def ping(self, reconnect=True): self.pings += 1

    class DB:
        made = 0
        def __init__(self): DB.made += 1; self._conn = Conn(); self.closed = False
        def close(self): self.closed = True
        def query_df(self, *a): return "rows"

    pool = DbPool(DB)
    a = pool.get(); assert a.query_df("select 1") == "rows"; a.close(); a.close()          # 重复 close 只还一次
    b = pool.get(); assert DB.made == 1 and b._conn.rollbacks == 1 and b._conn.pings == 1   # 复用 + 还回时 rollback
    c = pool.get(); assert DB.made == 2                                                    # 池里没了就新建
    b.close(); c.close()


def test_run_list_counts_follow_status_changes(server):
    """执行列表不取 summary，景区成败数走缓存：执行结束更新后要跟着变。"""
    base, app, _ = server
    rid = app.store.create_run("run", ["run"], "t", "manual")
    app.store.update_run(rid, status="running", summary={"scenics": [{"ok": True}]})
    item = app.store.list_runs(limit=1)["items"][0]
    assert (item["scenic_total"], item["scenic_failed"]) == (1, 0) and "summary" not in item
    app.store.update_run(rid, status="blocked", summary={"scenics": [{"ok": False, "quality_blocked": True}, {"ok": True}]})
    item = app.store.list_runs(limit=1)["items"][0]
    assert (item["scenic_total"], item["scenic_failed"], item["quality_blocked"], item["scenic_ok"]) == (2, 1, 1, 1)
    assert app.store.list_runs(limit=1)["items"][0]["scenic_total"] == 2                    # 缓存命中
    full = app.store.list_runs(limit=1, full=True)["items"][0]
    assert full["summary"]["scenics"] and full["scenic_total"] == 2


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
    # 跑完日志存一份到库里：本机日志文件没了（被删 / 在另一台服务器上）照样能看
    rid = r["run_id"]
    assert _wait(lambda: app.store.get_run_log(rid))
    saved = app.store.get_run_log(rid)
    assert "engin_cli.cli run" in saved["text"] and saved["size"] == lg["size"] and saved["host"]
    import os as _os
    _os.remove(app.store.get_run(rid)["log_path"])
    code, lg3 = _call(base, f"/api/runs/{rid}/log?offset=0")
    assert lg3["source"] == "db" and lg3["text"] == saved["text"] and lg3["offset"] == lg3["size"]
    # 文件和库里都没有：说清楚原因，不只是「暂无输出」
    app.store._exec(f"delete from {app.store.t_run_log} where run_id=?", (rid,))
    code, lg4 = _call(base, f"/api/runs/{rid}/log?offset=0")
    assert lg4["source"] == "none" and "另一台" in lg4["missing"] and lg4["text"] == ""
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
    base, app, holder = server
    body = {"scenic": "S1", "start": "20261031", "end": "20261102"}
    code, r = _call(base, "/api/quality/check", "POST", body)
    assert code == 200 and r["errors"] == [] and r["rows"] > 0
    m = holder["macro"]
    hit = (m.channel == "douyin") & (m.travel_date == 20261101) & (m.time_granularity == "td")
    m.loc[hit, "comment_total"] += 1
    code, r = _call(base, "/api/quality/check", "POST", body)
    assert any("区间相同的行指标不一致" in e for e in r["errors"])
    # 按天列出哪天不对：td@20261101 和 latest_1d@20261102 对不上，两天都要标出来
    assert [d["date"] for d in r["days"]] == ["20261101", "20261102"]
    assert r["days"][0]["same_range"] >= 1 and "douyin" in r["days"][0]["channels"]
    assert any("comment_total" in x for x in r["days"][0]["samples"])
    # 修复：按这两天重算（写库 + 推送大盘 / 下钻）
    assert r["push_ready"] is False                                     # 测试配置里没有推送地址
    code, fx = _call(base, "/api/quality/repair", "POST", {"scenic": "S1", "dates": ["20261102", "20261101"]})
    assert fx["push"] is False and "--no-push" in app.store.get_run(fx["run_id"])["command"]
    app.settings.push_url = "https://push.example/sync"                 # 配了地址才推
    code, fx = _call(base, "/api/quality/repair", "POST", {"scenic": "S1", "dates": ["20261102", "20261101"]})
    assert code == 200 and (fx["start"], fx["end"], fx["push"]) == ("20261101", "20261102", True)
    run = app.store.get_run(fx["run_id"])
    assert run["title"].startswith("数据修复") and run["command"][:5] == ["run", "--start-date", "20261101",
                                                                       "--end-date", "20261102"]
    assert "--scenic" in run["command"] and "--push" in run["command"] and "--rerun-days" in run["command"]
    assert "数据质量修复" in app.store.list_events()[0]["text"]
    code, fx = _call(base, "/api/quality/repair", "POST", {"scenic": "S1", "dates": ["20261101"], "push": False})
    assert "--no-push" in app.store.get_run(fx["run_id"])["command"]
    # 页面上的修复：把检测的整段一次重算（不是只重算有问题的那几天）
    code, fx = _call(base, "/api/quality/repair", "POST", {"scenic": "S1", "start": "20261031",
                                                           "end": "20261102", "bad_days": 2})
    assert code == 200 and (fx["start"], fx["end"]) == ("20261031", "20261102")
    assert app.store.get_run(fx["run_id"])["command"][:5] == ["run", "--start-date", "20261031",
                                                             "--end-date", "20261102"]
    assert "整段重算 20261031~20261102（2 天有问题）" in app.store.list_events()[0]["text"]
    assert _call(base, "/api/quality/repair", "POST", {"scenic": "S1", "dates": ["bad"]})[0] == 400
    assert _call(base, "/api/quality/repair", "POST", {"scenic": "S1", "start": "20261102",
                                                       "end": "20261031"})[0] == 400
    assert _call(base, "/api/quality/check", "POST", {"scenic": "S1"})[0] == 400


def test_quality_repair_follows_the_task_tables(server):
    """检测关联任务：修复按任务里计算 / 推送的表重算、推送；点名景区的任务优先于全部景区的任务。"""
    base, app, holder = server
    app.settings.push_url = "https://push.example/sync"
    body = {"scenic": "S1", "start": "20261031", "end": "20261102"}
    code, r = _call(base, "/api/quality/check", "POST", body)
    assert r["task"] is None                                           # 还没有任务
    code, fx = _call(base, "/api/quality/repair", "POST", body)
    cmd = app.store.get_run(fx["run_id"])["command"]
    assert "--tables" not in cmd and cmd[cmd.index("--push") + 1] == "drill,macro"     # 没任务：全部表，推下钻 + 大盘
    app.store.create_task("全部景区", {"scenics": "", "tables": "", "push": "macro"}, {"times": ["08:10"]})
    tid = app.store.create_task("S1 每日", {"scenics": "S1,S2", "tables": "drill,macro", "push": "drill,macro"},
                                {"times": ["08:10"]})
    code, r = _call(base, "/api/quality/check", "POST", body)
    assert r["task"]["id"] == tid and r["task"]["push"] == "drill,macro"
    assert _call(base, "/api/quality/task?scenic=S1")[1]["task"]["id"] == tid          # 选景区时就显示关联任务
    assert _call(base, "/api/quality/task?scenic=S9")[1]["task"]["name"] == "全部景区"
    assert app.store.get_check(r["check_id"])["task_id"] == tid
    code, fx = _call(base, "/api/quality/repair", "POST", body)
    run = app.store.get_run(fx["run_id"])
    cmd = run["command"]
    assert cmd[cmd.index("--tables") + 1] == "drill,macro" and cmd[cmd.index("--push") + 1] == "drill,macro"
    assert cmd[cmd.index("--scenic") + 1] == "S1"                     # 只修这个景区
    assert run["task_id"] == tid and run["title"].startswith("数据修复 · S1 每日")
    # 任务只算下钻表：修复也要把大盘表算上（检测的就是它）；任务不推送：修复也不推
    app.store.update_task(tid, "S1 每日", {"scenics": "S1", "tables": "drill", "push": ""}, {"times": ["08:10"]}, True)
    code, fx = _call(base, "/api/quality/repair", "POST", body)
    cmd = app.store.get_run(fx["run_id"])["command"]
    assert cmd[cmd.index("--tables") + 1] == "drill,macro" and "--no-push" in cmd and fx["push"] is False


def test_config_view_masks_secrets(server):
    base, _, _ = server
    code, cfg = _call(base, "/api/config")
    text = json.dumps(cfg, ensure_ascii=False)
    assert FAKE_DB_PW not in text and FAKE_TOKEN not in text and FAKE_HOOK not in text
    assert cfg["数据库"]["password"] == "已配置"


def test_maintenance_actions(server, tmp_path):
    base, app, _ = server
    code, r = _call(base, "/api/maint/push", "POST", {"tables": "drill", "start": "bad"})
    assert code == 400
    code, r = _call(base, "/api/maint/push", "POST", {"tables": "drill"})
    assert code == 400 and "日期" in r["error"]
    code, r = _call(base, "/api/maint/notify-test", "POST", {})
    assert code == 200 and _done(app.runner, r["run_id"])["kind"] == "notify"
    code, r = _call(base, "/api/maint/run", "POST", {"config": {"date_mode": "range",
                                                                "start_date": "20261101",
                                                                "end_date": "20261102", "push": ""}})
    assert code == 200
    assert app.store.get_run(r["run_id"])["title"].startswith("临时")
    code, c = _call(base, "/api/maint/cleanup", "POST", {"days": 1})
    assert code == 200 and c["deleted_runs"] == 0                       # 7 天下限，刚跑的不删


def test_maint_rerun_existing_task_for_a_date_range(server):
    """重跑一段日期：选已有任务，按它的配置跑选的这段日期（任务本身不改）。"""
    base, app, _ = server
    tid = app.store.create_task("九寨沟每日", {"scenics": "S1", "date_mode": "yesterday", "tables": "drill,macro",
                                             "push": "macro"}, {"times": ["08:10"]})
    code, r = _call(base, "/api/maint/run", "POST", {"task_id": tid, "start": "20261027", "end": "20261103"})
    assert code == 200
    run = app.store.get_run(r["run_id"])
    assert run["title"] == "重跑 · 九寨沟每日 · 20261027~20261103" and run["task_id"] == tid
    argv = " ".join(run["command"])
    assert "--start-date 20261027 --end-date 20261103" in argv and "--scenic S1" in argv
    assert "--tables drill,macro" in argv and "--push macro" in argv
    assert app.store.get_task(tid)["config"]["date_mode"] == "yesterday"   # 任务本身不改
    assert _call(base, "/api/maint/run", "POST", {"task_id": tid, "start": "20261105", "end": "20261101"})[0] == 400
    assert _call(base, "/api/maint/run", "POST", {"task_id": 9999, "start": "20261101"})[0] == 404


def test_notify_settings_api(server):
    """飞书通知：页面上设置的优先；地址里带令牌，接口只回打码后的；只有管理员能改。"""
    base, app, _ = server
    hook = "https://open.feishu.cn/open-apis/bot/v2/hook/TESTTOKEN0123456789"
    code, v = _call(base, "/api/notify/settings")
    assert code == 200 and not v["page_set"]
    assert _call(base, "/api/notify/settings", "PUT", {"webhook": hook})[0] == 403   # 操作员不能改
    _login_as(base, "boss")
    assert _call(base, "/api/notify/settings", "PUT", {"webhook": "http://x"})[0] == 400
    code, v = _call(base, "/api/notify/settings", "PUT", {"webhook": hook, "secret": "sec"})
    assert code == 200 and v["source"] == "page" and v["secret"] and "TESTTOKEN" not in json.dumps(v)
    code, v = _call(base, "/api/notify/settings", "PUT", {"webhook": ""})              # 留空 = 不改
    assert v["page_set"] and v["secret"] and app.runner.notify_settings()["feishu_webhook"] == hook
    code, cfg = _call(base, "/api/config")
    assert cfg["数据质量"]["notify_feishu_from"] == "服务页面" and "TESTTOKEN" not in json.dumps(cfg)
    code, v = _call(base, "/api/notify/settings", "PUT", {"clear": True})
    assert not v["page_set"] and app.runner.notify_settings()["feishu_webhook"] == ""


def test_login_with_user_table(server):
    """必须登录：用 SMC 的用户表；输错 5 次锁定；停用不能登录；角色同 SMC（只读 / 操作员不能改系统设置）。"""
    base, app, _ = server
    users = app.auth.users
    code, me = _call(base, "/api/me")
    assert code == 200 and me["user"]["display_name"] == "运维小王" and me["user"]["role_text"] == "操作员"
    assert users.rows["op"]["last_login_ip"] == "127.0.0.1" and users.rows["op"]["last_login_at"]
    assert _call(base, "/", raw=True)[0] == 200                       # 页面文件不用登录（登录页要加载）
    code, r = _call(base, "/api/scheduler", "PUT", {"enabled": True})
    assert code == 403 and "管理员" in r["error"]                     # 操作员不能改系统设置
    assert _call(base, "/api/tasks", "POST", {"name": ""})[0] == 400   # 日常操作可以（这里是参数不对）

    _call(base, "/api/logout", "POST")
    code, r = _call(base, "/api/tasks")
    assert code == 401 and r["login"] is True                         # 退出后接口要求登录
    assert _call(base, "/api/login", "POST", {"username": "op"})[0] == 400
    code, r = _call(base, "/api/login", "POST", {"username": "nobody", "password": "x"})
    assert code == 401 and r["error"] == "用户名或密码不对"           # 不提示账号存不存在

    for _ in range(4):
        code, r = _call(base, "/api/login", "POST", {"username": "boss", "password": "wrong"})
        assert code == 401 and r["error"] == "用户名或密码不对"
    code, r = _call(base, "/api/login", "POST", {"username": "boss", "password": "wrong"})
    assert code == 423 and "锁定 10 分钟" in r["error"]               # 第 5 次锁定
    code, r = _call(base, "/api/login", "POST", {"username": "boss", "password": FAKE_SVC_PW})
    assert code == 423 and "已锁定" in r["error"]                     # 锁定期间密码对了也不行
    users.rows["boss"]["locked_until"] = None
    code, r = _call(base, "/api/login", "POST", {"username": "boss", "password": FAKE_SVC_PW})
    assert code == 200 and r["user"]["display_name"] == "boss"        # 没有显示名就用登录名
    assert users.rows["boss"]["failed_count"] == 0
    assert _call(base, "/api/scheduler", "PUT", {"enabled": True})[0] in (200, 400)   # 管理员可以

    code, r = _call(base, "/api/login", "POST", {"username": "gone", "password": FAKE_SVC_PW})
    assert code == 403 and "停用" in r["error"]

    code, r = _call(base, "/api/login", "POST", {"username": "look", "password": FAKE_SVC_PW})
    assert code == 200 and r["user"]["readonly"] is True
    assert _call(base, "/api/tasks")[0] == 200                        # 只读：能看
    code, r = _call(base, "/api/tasks", "POST", {"name": "x"})
    assert code == 403 and "只读" in r["error"]                       # 不能改
    users.rows["look"]["enabled"] = 0                                 # 在 SMC 里停用 → 这边登录也失效
    app.auth._cache.clear()
    assert _call(base, "/api/tasks")[0] == 401


def test_health_without_login_only_from_local(server):
    base, app, _ = server
    _call(base, "/api/logout", "POST")
    assert _call(base, "/api/health")[0] == 200                       # 本机直连（scripts/service.sh）不用登录
    assert _call(base, "/api/health", headers={"X-Forwarded-For": "10.0.0.8"})[0] == 401   # 经过代理要登录


def test_password_hash_same_as_smc():
    """跟 SMC passwords.py 同一个格式：pbkdf2_sha256$迭代$盐(十六进制串，按文本参与计算)$十六进制哈希。"""
    import hashlib
    from engin_cli.service.auth import verify_password
    salt = "0123456789abcdef0123456789abcdef"
    stored = f"pbkdf2_sha256$240000${salt}$" + hashlib.pbkdf2_hmac(
        "sha256", "Pass1234".encode(), salt.encode(), 240000).hex()
    assert verify_password("Pass1234", stored) and not verify_password("Pass12345", stored)
    assert verify_password("秘密 123", hash_password("秘密 123", iterations=1000))
    for bad in ("", "md5$1$a$b", "pbkdf2_sha256$x$a$b", "pbkdf2_sha256$1000$abc", None):
        assert not verify_password("Pass1234", bad)


def test_run_summary_json_from_cli(tmp_path):
    src = tmp_path / "comments.csv"
    _rows().to_csv(src, index=False, encoding="utf-8-sig")
    out = tmp_path / "sum.json"
    rc = cli.main(["run", "--comments-csv", str(src), "--skip-db", "--no-push", "--rerun-days", "0",
                   "--date", "20261102", "--no-progress", "--summary-json", str(out)])
    assert rc == 0
    s = json.loads(out.read_text(encoding="utf-8"))
    assert s["exit_code"] == 0 and s["dates"] == ["20261102"]
    assert s["scenics"][0]["scenic"] == "S1" and s["scenics"][0]["ok"]


def test_run_tables_only_outputs_selected(tmp_path):
    from engin_cli.loader import TABLE_DRILL_ANALYSIS
    src = tmp_path / "comments.csv"
    _rows().to_csv(src, index=False, encoding="utf-8-sig")
    rc = cli.main(["run", "--comments-csv", str(src), "--skip-db", "--no-push", "--rerun-days", "0",
                   "--date", "20261102", "--no-progress",
                   "--tables", "macro", "--summary-json", str(tmp_path / "s.json")])
    assert rc == 0
    s = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["scenics"][0]
    assert list(s["tables"]) == [TABLE_MACRO]                     # 下钻表没算、基础四表没输出
    assert not list(tmp_path.rglob(TABLE_MACRO + ".csv"))         # 结果只写库，不出本地 CSV
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
    # 页面上设置的飞书地址传给子进程；日志头不写地址（带令牌）
    hook = "https://open.feishu.cn/open-apis/bot/v2/hook/TESTTOKEN0123456789"
    runner.store.set_kv("notify", {"feishu_webhook": hook, "feishu_secret": ""})
    rid = runner.submit("notify", ["notify-test"], "t")
    text = open(_done(runner, rid)["log_path"], encoding="utf-8").read()
    head, out = text.split("ARGS=", 1)
    assert "通知地址来自服务页面" in head and "TESTTOKEN" not in head
    assert f'"notify_feishu_webhook": "{hook}"' in out


def _login_as(base, username):
    _JAR.clear()
    assert _call(base, "/api/login", "POST", {"username": username, "password": FAKE_SVC_PW})[0] == 200


def test_push_settings_and_logs_api(server, tmp_path, monkeypatch):
    base, app, _ = server
    _login_as(base, "boss")                                           # 推送设置是系统设置：管理员
    code, v = _call(base, "/api/push/settings")
    assert v["settings"]["enabled"] and FAKE_TOKEN not in json.dumps(v)
    code, e = _call(base, "/api/push/settings", "PUT", {"push_url": "ftp://x"})
    assert code == 400
    code, v = _call(base, "/api/push/settings", "PUT", {"enabled": False, "push_batch_size": "100"})
    assert not v["settings"]["enabled"] and v["effective"]["push_batch_size"] == 100
    assert "推送总开关已关闭" in app.store.list_events()[0]["text"]
    code, v = _call(base, "/api/push/settings", "PUT", {"push_url": "https://p.example/sync"})
    code, v = _call(base, "/api/push/settings", "PUT", {"enabled": True})          # 只改总开关
    assert v["settings"]["push_url"] == "https://p.example/sync" and v["settings"]["push_batch_size"] == 100
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
    assert [x["result"] for x in lg["items"]] == ["ok"] and lg["total"] == 1
    code, lg = _call(base, "/api/push/logs?limit=1")                  # 分页：新的在前
    assert lg["total"] == 2 and [x["result"] for x in lg["items"]] == ["http_error"]
    code, lg = _call(base, "/api/push/logs?limit=1&offset=1")
    assert lg["total"] == 2 and [x["result"] for x in lg["items"]] == ["ok"] and lg["counts"]["ok"] == 1
    assert _call(base, "/api/push/logs?date=bad")[0] == 400


def test_scheduler_api_and_task_preview(server):
    base, app, _ = server
    _login_as(base, "boss")                                           # 定时总开关：管理员
    code, sv = _call(base, "/api/scheduler")
    assert sv["available"] is False                                    # 测试里没启动定时
    assert _call(base, "/api/scheduler", "PUT", {"enabled": False})[0] == 400
    code, pv = _call(base, "/api/tasks/preview", "POST", {"config": {"tables": "drill", "push": "drill,macro"},
                                                         "schedule": {"times": ["08:10", "20:10"]}})
    assert pv["ok"] and pv["tables"] == ["drill"] and pv["push"] == ["drill"] and len(pv["next_fires"]) == 3
    assert "--tables drill" in pv["command"]
    assert pv["push_no_url"] is True                                    # 测试配置没有推送地址
    code, pv2 = _call(base, "/api/tasks/preview", "POST", {"config": {"push": ""}})
    assert pv2["push_no_url"] is False
    code, pv = _call(base, "/api/tasks/preview", "POST", {"config": {"date_mode": "x"}})
    assert not pv["ok"] and "日期方式" in pv["error"]
    code, ov = _call(base, "/api/overview")
    assert "durations" in ov and len(ov["durations"]) == 14 and "today_slots" in ov


# ══════════════════════════════════════════════════════════════════
# 系统表存 MySQL（数仓规范的表名）。设 OPINION_TEST_MYSQL=host:port:user:password 才跑，
# 账号要能建库：会建 omx_t_db 临时库（模拟计算指标用的那个库），跑完删掉
# ══════════════════════════════════════════════════════════════════
def _mysql_cfg():
    spec = os.environ.get("OPINION_TEST_MYSQL")
    if not spec:
        pytest.skip("没设 OPINION_TEST_MYSQL，跳过 MySQL 系统表测试")
    from engin_cli.settings import DBConfig
    host, port, user, pw = spec.split(":", 3)
    return DBConfig(host=host, port=int(port), user=user, password=pw, database="omx_t_db")


@pytest.fixture
def mysql_store():
    import pymysql
    cfg = _mysql_cfg()
    conn = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password, autocommit=True)
    with conn.cursor() as c:
        c.execute("drop database if exists omx_t_db")
        c.execute("create database omx_t_db default charset utf8mb4")
    st = Store.mysql(cfg)
    yield st, conn
    st.close()
    with conn.cursor() as c:
        c.execute("drop database if exists omx_t_db")
    conn.close()


def test_mysql_tables_follow_naming_rules(mysql_store):
    st, conn = mysql_store
    from engin_cli.service.store import (TABLE_QUALITY, TABLE_RUN, TABLE_RUN_LOG, TABLE_SCHEDULE_LOG, TABLE_SETTING,
                                         TABLE_TASK)
    with conn.cursor() as c:
        c.execute("select table_schema, table_name, table_comment from information_schema.tables "
                  "where table_schema='omx_t_db' order by table_name")
        tables = c.fetchall()
        c.execute("select table_name, column_name, column_comment from information_schema.columns "
                  "where table_schema='omx_t_db'")
        cols = c.fetchall()
    assert {t[1] for t in tables} == {TABLE_TASK, TABLE_SETTING, TABLE_RUN, TABLE_SCHEDULE_LOG, TABLE_QUALITY,
                                     TABLE_RUN_LOG}                       # 同一个库
    assert all(t[2] for t in tables)                                     # 表注释
    assert [c for c in cols if not c[2]] == []                           # 每个字段都有注释
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", c[1]) for c in cols)     # snake_case
    names = {c[0]: set() for c in cols}
    for t, c, _ in cols:
        names[t].add(c)
    assert all("travel_date" in names[t] for t in (TABLE_RUN, TABLE_SCHEDULE_LOG, TABLE_QUALITY, TABLE_RUN_LOG))
    status = [c for c in cols if c[1].endswith("_status")]
    assert status and all("-" in c[2] for c in status)                    # 状态字段注释写清每个取值


def test_mysql_store_round_trip(mysql_store):
    st, conn = mysql_store
    tid = st.create_task("每日", {"scenics": "S1"}, {"times": ["08:10"]})
    st.set_last_fired(tid, {"08:10": "2026-11-02"})
    t = st.get_task(tid)
    assert t["name"] == "每日" and t["enabled"] and t["last_fired"] == {"08:10": "2026-11-02"}
    assert len(t["created_at"]) == 19
    st.set_kv("push", {"enabled": False})
    st.set_kv("push", {"enabled": True, "push_url": "https://x"})              # upsert
    assert st.get_kv("push") == {"enabled": True, "push_url": "https://x"}
    st.add_event("08:10 到点", st.get_task(tid))
    st.add_event("定时总开关已关闭", level="warn")
    assert [e["text"] for e in st.list_events(tid)] == ["定时总开关已关闭", "08:10 到点"]
    rid = st.create_run("run", ["run", "--date", "20261102"], "S1 · 20261102", "schedule", st.get_task(tid))
    st.update_run(rid, status="running", started_at=now_str_(), log_path="/tmp/x.log")
    st.update_run(rid, status="blocked", finished_at=now_str_(), exit_code=1,
                  summary={"scenics": [{"scenic": "S1", "ok": False, "quality_blocked": True}]}, message="回退")
    r = st.get_run(rid)
    assert r["status"] == "blocked" and r["command"][0] == "run" and r["quality_blocked"] == 1
    assert st.list_runs(status="blocked")["total"] == 1 and st.last_run_of(tid)["id"] == rid
    assert [x["id"] for x in st.runs_between("2000-01-01")] == [rid]
    with conn.cursor() as c:                                              # 分区字段 = 创建当天
        c.execute("select travel_date, run_type, trigger_type from omx_t_db.dwd_trf_social_opinion_task_run_di")
        td, rt, tt = c.fetchone()
    assert td == int(time.strftime("%Y%m%d")) and (rt, tt) == ("run", "schedule")
    rid2 = st.create_run("push", ["push"], "补推", "manual")
    assert st.mark_interrupted() == 1 and st.get_run(rid2)["status"] == "failed"
    st.update_task(tid, "改名", {}, {"times": []}, False)
    assert not st.get_task(tid)["enabled"]
    assert st.delete_runs_before("2999-01-01") == ["/tmp/x.log"]
    st.delete_task(tid)
    assert st.list_tasks() == []


def test_mysql_store_reconnects(mysql_store):
    st, conn = mysql_store
    with conn.cursor() as c:                                              # 模拟 wait_timeout 把连接断了
        c.execute("kill %s", (st._db.thread_id(),))
    time.sleep(0.2)
    assert st.create_task("断线后", {}, {}) > 0
    assert st.list_tasks()[0]["name"] == "断线后"


def test_mysql_ddl_renders_database_names():
    from engin_cli.service.store import render_mysql_ddl
    stmts = render_mysql_ddl("opinion")
    assert len(stmts) == 15                                           # 6 张表 + 9 个索引
    heads = [s.split("(")[0] for s in stmts]                             # 建表 / 建索引的表名都带库名
    assert all("`opinion`." in h for h in heads)
    assert any("`opinion`.dim_trf_social_opinion_task_f" in h for h in heads)
    assert any("`opinion`.dwd_trf_social_opinion_task_run_di" in h for h in heads)
    assert any("`opinion`.dwd_trf_social_opinion_quality_check_di" in h for h in heads)
    assert "（dim_trf_social_opinion_task_f.task_id）" in "".join(stmts)   # 注释里的表名不动


def test_service_store_setting(tmp_path):
    from engin_cli.service.server import open_store
    from engin_cli.service.store import StoreError
    st = EtlSettings()
    st.service_store = "sqlite"
    assert open_store(st, str(tmp_path)).kind == "sqlite"
    st.service_store = "oracle"
    with pytest.raises(StoreError):
        open_store(st, str(tmp_path))
    st.service_store = "mysql"
    st.db.database = "bad-name;drop"
    with pytest.raises(StoreError, match="库名"):
        open_store(st, str(tmp_path))


def now_str_():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════════════════════════════════════════════════════
# 跑完后复制给其他景区（engin_cli mock 挂在正式景区的跑批后面）
# ══════════════════════════════════════════════════════════════════
COPY_CFG = {"scenics": "PFTSCA01016184", "date_mode": "today", "push": "drill,macro",
            "copies": [{"from_scenic": "", "to_scenic": "PFTSCA01018088", "to_name": "泰宁九龙潭生态旅游区"}]}


def test_copy_steps_follow_the_run():
    kind, argv, title, then = R.build_task_argv(COPY_CFG, dt.date(2026, 11, 3))
    assert kind == "run" and argv[:5] == ["run", "--start-date", "20261103", "--end-date", "20261103"]
    assert len(then) == 1 and then[0]["from_scenic"] == "PFTSCA01016184"          # 源景区默认 = 任务里唯一的景区
    assert then[0]["argv"] == ["mock", "--from-scenic", "PFTSCA01016184", "--to-scenic", "PFTSCA01018088",
                               "--to-name", "泰宁九龙潭生态旅游区", "--start-date", "20261103",
                               "--end-date", "20261103", "--no-progress"]
    # 只算了部分表：复制同样的表
    _, _, _, then2 = R.build_task_argv({**COPY_CFG, "tables": "drill,macro"}, dt.date(2026, 11, 3))
    assert then2[0]["argv"][-2:] == ["--tables", "drill,macro"]
    with pytest.raises(ValueError, match="不在这个任务要跑的景区里"):
        R.normalize_config({**COPY_CFG, "copies": [{"from_scenic": "X", "to_scenic": "Y", "to_name": "n"}]})
    with pytest.raises(ValueError, match="不能和源景区一样"):
        R.normalize_config({**COPY_CFG, "copies": [{"to_scenic": "PFTSCA01016184", "to_name": "n"}]})
    with pytest.raises(ValueError, match="只算不写库"):
        R.normalize_config({**COPY_CFG, "skip_db": True})
    # 页面上加了空行：忽略
    assert R.normalize_config({**COPY_CFG, "copies": [{"to_scenic": "", "to_name": ""}]})["copies"] == []


def test_legacy_standalone_copy_task_becomes_run_plus_copy():
    old = {"task_type": "mock", "from_scenic": "PFTSCA01016184", "to_scenic": "PFTSCA01018088",
           "to_name": "泰宁九龙潭生态旅游区", "date_mode": "today"}
    cfg = R.normalize_config(old)
    assert cfg["scenics"] == "PFTSCA01016184"                                     # 不会变成跑全部景区
    assert cfg["copies"] == [{"from_scenic": "PFTSCA01016184", "to_scenic": "PFTSCA01018088",
                              "to_name": "泰宁九龙潭生态旅游区"}]


def test_scheduler_passes_copy_steps(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.create_task("九嶷山每日", COPY_CFG, {"times": ["08:10"]})
    calls = []

    class FR(_FakeRunner):
        def submit(self, kind, argv, title, trigger="manual", task=None, then=None):
            calls.append((kind, then))
            return 1
    SC.Scheduler(store, FR()).tick(dt.datetime(2026, 11, 3, 8, 11))
    assert calls[0][0] == "run" and calls[0][1][0]["argv"][0] == "mock"


def _fake_engine(tmp_path, ok):
    """假的 engin_cli：run 时按 --summary-json 写结果，mock 时打印一行。"""
    f = tmp_path / "fake.sh"
    f.write_text("#!/bin/sh\n"
                 "shift 2\n"                                     # 去掉 -m engin_cli.cli
                 'if [ "$1" = mock ]; then echo "MOCK $*"; exit 0; fi\n'
                 'while [ $# -gt 0 ]; do if [ "$1" = --summary-json ]; then out="$2"; fi; shift; done\n'
                 f'echo \'{{"scenics":[{{"scenic":"PFTSCA01016184","ok":{"true" if ok else "false"}}}]}}\' > "$out"\n'
                 f'exit {0 if ok else 1}\n')
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return str(f)


def test_runner_runs_copy_after_successful_run(runner, tmp_path):
    runner.python = _fake_engine(tmp_path, ok=True)
    kind, argv, title, then = R.build_task_argv(COPY_CFG, dt.date(2026, 11, 3))
    rid = runner.submit(kind, argv, title, "schedule", None, then=then)
    assert _done(runner, rid)["status"] == "success"
    mock = _wait(lambda: next((r for r in runner.store.list_runs(kind="mock")["items"]), None))
    m = _done(runner, mock["id"])
    assert m["status"] == "success" and m["trigger"] == "schedule" and m["command"][0] == "mock"
    assert "已提交 #" in open(runner.store.get_run(rid)["log_path"], encoding="utf-8").read()


def test_runner_skips_copy_when_source_failed(runner, tmp_path):
    runner.python = _fake_engine(tmp_path, ok=False)
    kind, argv, title, then = R.build_task_argv(COPY_CFG, dt.date(2026, 11, 3))
    rid = runner.submit(kind, argv, title, "manual", None, then=then)
    assert _done(runner, rid)["status"] == "failed"
    time.sleep(0.5)
    assert runner.store.list_runs(kind="mock")["total"] == 0
    assert "跳过" in open(runner.store.get_run(rid)["log_path"], encoding="utf-8").read()
    assert "跑完后复制跳过" in runner.store.list_events()[0]["text"]


def test_copy_preview_api(server):
    base, app, _ = server
    code, pv = _call(base, "/api/tasks/preview", "POST", {"config": COPY_CFG, "schedule": {"times": ["08:10"]}})
    assert pv["ok"] and len(pv["copies"]) == 1 and pv["copies"][0]["command"].startswith("python -m engin_cli.cli mock ")
    assert pv["push_no_url"] is True


def test_health_reports_each_part_and_never_blocks(server):
    """顶栏状态标识：服务能响应就在线；系统表库 / 指标库在后台线程检测，接口立刻返回。"""
    base, app, _ = server

    class OkDB:
        def query_df(self, sql, params=()):
            return pd.DataFrame([{"ok": 1}])

        def close(self):
            pass

    class DownDB(OkDB):
        def query_df(self, sql, params=()):
            raise OSError("Connection refused")

    app._db_factory = OkDB
    code, h = _call(base, "/api/health?refresh=1")
    assert code == 200 and h["checks"][0]["key"] == "service" and h["checks"][0]["ok"] is True
    _wait(lambda: not app._health_busy and app._health_deps)
    code, h = _call(base, "/api/health")
    by = {c["key"]: c for c in h["checks"]}
    assert h["ok"] and by["store"]["ok"] and by["db"]["ok"]
    assert by["scheduler"]["ok"] is None                   # --no-scheduler：提示，不算故障

    app._db_factory = DownDB
    _call(base, "/api/health?refresh=1")
    _wait(lambda: not app._health_busy and app._health_deps["db"]["ok"] is False)
    code, h = _call(base, "/api/health")
    by = {c["key"]: c for c in h["checks"]}
    assert code == 200 and not h["ok"] and by["db"]["ok"] is False
    assert "Connection refused" in by["db"]["detail"] and by["service"]["ok"] is True
    assert FAKE_DB_PW not in json.dumps(h, ensure_ascii=False)          # 不带密码


def test_repair_must_recompute_the_whole_checked_range(server):
    """评论晚到时：只重算「有问题的那几天」，边上会冒出新的不一致（客户要修好几次）；整段一次重算就干净。"""
    base, app, holder = server
    st = EtlSettings()
    st.write_db, st.lookback_days, st.enable_drill_analysis = False, 40, False

    def src(late):
        rows = []
        for d in pd.date_range("2026-10-10", "2026-11-02"):
            ds = d.strftime("%Y%m%d")
            rows.append(_comment(f"a{ds}", ds))
            if late and "20261024" <= ds <= "20261030":
                rows.append(_comment(f"late{ds}", ds, "排队太久"))     # 当时还没有、后来才进源表
        return pd.DataFrame(rows)

    def calc(late, a, b):
        dates = [d.strftime("%Y%m%d") for d in pd.date_range(a, b)]
        return run_pipeline(st, dates, comments_df=src(late)).tables[TABLE_MACRO]

    def write(db, new):
        return pd.concat([db[~db.travel_date.isin(set(new.travel_date))], new], ignore_index=True)

    body = {"scenic": "S1", "start": "20261020", "end": "20261102"}

    def check(db):
        holder["macro"] = db
        return _call(base, "/api/quality/check", "POST", body)[1]

    db = write(calc(False, "20261020", "20261102"), calc(True, "20261031", "20261102"))   # 日常只重跑最近几天
    r = check(db)
    assert r["days"], "晚到的评论应该让检测报出问题"
    # 旧做法：只重算有问题的第一天 ~ 最后一天 → 还是不对
    part = write(db, calc(True, r["days"][0]["date"], r["days"][-1]["date"]))
    assert check(part)["days"]
    # 现在的做法：整段一次重算 → 通过
    assert check(write(db, calc(True, body["start"], body["end"])))["days"] == []


def test_mysql_login_with_smc_user_table():
    """真库：SMC 的用户表（客户给的建表语句）+ 会话表；SQL 跟 SMC 一致（库的 NOW() 判断锁定 / 过期）。"""
    import pymysql
    from engin_cli.db import MySQL
    from engin_cli.service.auth import Auth, AuthError, UserRepository
    cfg = _mysql_cfg()
    conn = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password, autocommit=True)
    with conn.cursor() as c:
        c.execute("drop database if exists omx_t_src")
        c.execute("create database omx_t_src default charset utf8mb4")
        c.execute("""CREATE TABLE omx_t_src.`src_opinion_sys_user` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `username` varchar(64) NOT NULL COMMENT '登录名',
  `display_name` varchar(100) NOT NULL DEFAULT '' COMMENT '显示名，复核人等处显示它',
  `password_hash` varchar(255) NOT NULL COMMENT 'pbkdf2_sha256$迭代次数$盐$哈希',
  `role` varchar(20) NOT NULL DEFAULT 'operator' COMMENT 'admin 管理员 / operator 操作员 / viewer 只读',
  `enabled` tinyint NOT NULL DEFAULT '1',
  `failed_count` int NOT NULL DEFAULT '0' COMMENT '连续输错密码次数',
  `locked_until` datetime DEFAULT NULL COMMENT '输错太多次锁到什么时候',
  `last_login_at` datetime DEFAULT NULL,
  `last_login_ip` varchar(64) NOT NULL DEFAULT '',
  `password_changed_at` datetime DEFAULT NULL,
  `create_time` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_sys_user` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC COMMENT='系统用户'""")
        c.execute("insert into omx_t_src.src_opinion_sys_user (username, display_name, password_hash, role) "
                  "values (%s, %s, %s, 'admin'), ('v', '', %s, 'viewer')",
                  ["admin", "管理员甲", hash_password(FAKE_SVC_PW), hash_password(FAKE_SVC_PW, 1000)])
    import dataclasses
    cfg = dataclasses.replace(cfg, database="omx_t_src")
    auth = Auth(UserRepository(lambda: MySQL(cfg), "omx_t_src"))
    try:
        for _ in range(4):
            with pytest.raises(AuthError, match="用户名或密码不对"):
                auth.login("admin", "wrong", "10.0.0.1")
        with pytest.raises(AuthError, match="锁定 10 分钟"):
            auth.login("admin", "wrong", "10.0.0.1")
        with pytest.raises(AuthError, match="已锁定"):
            auth.login("admin", FAKE_SVC_PW, "10.0.0.1")
        with conn.cursor() as c:
            c.execute("update omx_t_src.src_opinion_sys_user set locked_until = NULL where username='admin'")
        token, user = auth.login("admin", FAKE_SVC_PW, "10.0.0.2", "pytest")
        assert user["display_name"] == "管理员甲" and user["role"] == "admin"
        with conn.cursor() as c:
            c.execute("select failed_count, last_login_ip, last_login_at is not null "
                      "from omx_t_src.src_opinion_sys_user where username='admin'")
            assert c.fetchone() == (0, "10.0.0.2", 1)
            c.execute("select count(*), max(ip), max(user_agent) from omx_t_src.src_opinion_sys_user_session")
            assert c.fetchone() == (1, "10.0.0.2", "pytest")          # 会话表照 SMC 的结构自动建
        assert auth.user_for(token)["username"] == "admin"
        assert auth.user_for("not-a-token") is None
        with conn.cursor() as c:                                      # 在 SMC 里停用 → 这边也失效
            c.execute("update omx_t_src.src_opinion_sys_user set enabled = 0 where username='admin'")
        auth._cache.clear()
        assert auth.user_for(token) is None
        t2, u2 = auth.login("v", FAKE_SVC_PW)
        assert u2["readonly"] and u2["display_name"] == "v"
        auth.logout(t2)
        auth._cache.clear()
        assert auth.user_for(t2) is None
    finally:
        with conn.cursor() as c:
            c.execute("drop database if exists omx_t_src")
        conn.close()


def test_every_run_records_a_quality_check_with_its_process(runner, tmp_path):
    """跑批每个景区都记一条检测记录：查了哪些项（规则）、核对多少行、断言几次、结果；执行摘要里不再留一份。"""
    src = tmp_path / "comments.csv"
    _rows().to_csv(src, index=False, encoding="utf-8-sig")
    argv = ["run", "--comments-csv", str(src), "--skip-db", "--no-push", "--rerun-days", "0",
            "--date", "20261102", "--no-progress"]
    rid = runner.submit("run", argv, "测试", trigger="schedule", task={"id": 7, "name": "九寨沟 · 每日跑批"})
    run = _done(runner, rid)
    assert run["status"] == "success"
    assert "quality_trace" not in run["summary"]["scenics"][0]            # 过程挪到检测记录表了
    lst = runner.store.list_checks(since="2000-01-01")
    assert lst["total"] == 1 and lst["counts"] == {"pass": 1}
    c = runner.store.get_check(lst["items"][0]["id"])
    assert (c["type"], c["trigger"], c["run_id"], c["task_name"], c["scenic"]) == \
        ("run", "schedule", rid, "九寨沟 · 每日跑批", "S1")
    assert c["start"] == c["end"] == "20261102" and c["status"] == "pass" and c["summary"].startswith("全部通过")
    att = c["detail"]["attempts"]
    assert len(att) == 1 and att[0]["ok"] and att[0]["at"]
    steps = {s["key"]: s for s in att[0]["steps"]}
    assert {"core", "platform", "dimension", "content", "same_range", "macro"} <= set(steps)
    assert c["items"] == len(att[0]["steps"])
    for s in steps.values():                                             # 每一项：规则、行数、断言次数、结果
        assert s["rules"] and s["rows"] > 0 and s["asserts"] >= 1 and s["ok"] and s["errors"] == []
    assert steps["same_range"]["compared"] > 0                           # 区间一致：比了多少对


def test_manual_quality_check_is_recorded_with_operator(server):
    base, app, holder = server
    m = holder["macro"]
    m.loc[(m.channel == "douyin") & (m.travel_date == 20261101) & (m.time_granularity == "td"), "comment_total"] += 1
    code, r = _call(base, "/api/quality/check", "POST", {"scenic": "S1", "start": "20261031", "end": "20261102"})
    assert code == 200 and r["check_id"]
    code, lst = _call(base, "/api/quality/checks?days=1")
    assert code == 200 and lst["items"][0]["id"] == r["check_id"]
    assert (lst["items"][0]["type"], lst["items"][0]["trigger"], lst["items"][0]["operator"]) == ("manual", "page", "运维小王")
    code, c = _call(base, f"/api/quality/checks/{r['check_id']}")
    assert c["status"] == "fail" and c["issues"] >= 1 and "库里已有的大盘表" in c["detail"]["source"]
    sr = c["detail"]["attempts"][0]["steps"][0]
    assert sr["key"] == "same_range" and not sr["ok"] and sr["issue_total"] >= 1
    it = sr["issues"][0]                                                 # 对比的是哪两行、各是多少
    assert it["field"] and len(it["rows"]) >= 2 and len(it["values"]) >= 2
    assert _call(base, "/api/quality/checks/99999")[0] == 404
