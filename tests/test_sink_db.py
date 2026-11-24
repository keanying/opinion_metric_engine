# -*- coding: utf-8 -*-
"""推送方式之二：写入数据源（sink_db.py）+ 服务页面上的数据源管理。

目标库用假的：记下每次「删什么、写什么」，不连任何真实 MySQL。
"""

import json
import threading

import pandas as pd
import pytest

import engin_cli.cli as cli
from engin_cli import sink_db as S
from engin_cli.loader import (TABLE_CONTENT, TABLE_CORE, TABLE_DRILL_ANALYSIS, TABLE_MACRO,
                              TABLE_PLATFORM)
from engin_cli.mock import TABLE_COLUMNS
from engin_cli.pipeline import run
from engin_cli.pusher import push_all
from engin_cli.service import runner as R
from engin_cli.settings import EtlSettings, apply_overrides, push_dest, push_not_ready

DS = {"name": "客户数仓", "host": "10.0.0.8", "port": 3307, "user": "w", "password": "pw-secret",
      "database": "dw", "table_names": {TABLE_MACRO: "kpi_macro"}}


class TargetDB:
    """假的目标库：tables = {目标表名: 列}；记下 replace_rows 的调用。"""
    instances = []

    def __init__(self, cfg, tables=None, fail_connect=None):
        self.cfg = cfg
        self.tables = tables if tables is not None else {
            **{t: list(c) + ["id", "created_time"] for t, c in TABLE_COLUMNS.items() if t != TABLE_MACRO},
            "kpi_macro": list(TABLE_COLUMNS[TABLE_MACRO])}
        self.fail_connect = fail_connect
        self.writes = []
        self.closed = False
        TargetDB.instances.append(self)

    def connect(self):
        if self.fail_connect:
            raise self.fail_connect

    def query_df(self, sql, params=()):
        self.connect()
        assert "information_schema.columns" in sql and params[0] == "dw"
        return pd.DataFrame({"column_name": self.tables.get(params[1], [])})

    def replace_rows(self, table, df, where, params, batch_size=2000, database=None):
        self.writes.append({"table": table, "cols": list(df.columns), "rows": len(df), "where": where,
                            "params": list(params), "database": database, "batch": batch_size})
        return 7, len(df)

    def close(self):
        self.closed = True


class OpErr(Exception):
    """模拟 pymysql.err.OperationalError（类名要叫这个）。"""


OpErr.__name__ = "OperationalError"


def _comment(cid, ds, word="景色好", platform="douyin"):
    return {"scenic_id": "S1", "scenic_name": "景区1", "channel": platform, "work_id": "w1",
            "comment_id": cid, "comment_level": "level_1", "root_comment_id": "", "commenter_name": "u",
            "content": word, "likes": 0, "sentiment_label": "正向", "sentiment_score": 1,
            "dimension_tags": json.dumps([{"dim1": "游玩体验", "dim2": "景色观赏", "dim3": "自然风光",
                                           "sentiment": 1}], ensure_ascii=False),
            "entity_tags": "[]", "location": "广东", "keyword_tags": json.dumps([word], ensure_ascii=False),
            "publish_time": f"{ds[:4]}-{ds[4:6]}-{ds[6:]} 10:00:00"}


@pytest.fixture(scope="module")
def produced():
    rows = []
    for d in pd.date_range("2026-10-01", "2026-11-02"):
        ds = d.strftime("%Y%m%d")
        rows += [_comment(f"a{ds}", ds), _comment(f"b{ds}", ds, "人不多", "xiaohongshu")]
    st = EtlSettings()
    st.write_db = False
    st.lookback_days = 40
    return run(st, ["20261101", "20261102"], comments_df=pd.DataFrame(rows)).tables


def _st(**kw):
    st = EtlSettings()
    st.push_enabled, st.push_api, st.push_db = True, False, dict(DS)
    for k, v in kw.items():
        setattr(st, k, v)
    return st


@pytest.fixture(autouse=True)
def _fake_mysql(monkeypatch):
    TargetDB.instances = []
    monkeypatch.setattr(S, "MySQL", TargetDB)


# ══════════════════════════════════════════════════════════════════
# 写入：先删本次景区 × 日期，再写目标表有的列
# ══════════════════════════════════════════════════════════════════
def test_push_all_writes_to_datasource(produced):
    rep = push_all(produced, settings=_st(), scope=(["S1"], ["20261101", "20261102"]))
    assert rep.ok, rep.errors
    db = TargetDB.instances[0]
    assert db.cfg.host == "10.0.0.8" and db.cfg.port == 3307 and db.cfg.database == "dw" and db.closed
    w = {x["table"]: x for x in db.writes}
    assert set(w) == {t for t in produced if t != TABLE_MACRO} | {"kpi_macro"}         # 表名映射
    assert all(x["database"] == "dw" for x in db.writes)
    # 删除范围 = 本次景区 × 本次日期，和本地写库同一个规则（下钻表按 travel_date）
    assert w[TABLE_CORE]["where"] == "`scenic_spot_code` IN (%s) AND `publish_time` IN (%s,%s)"
    assert w[TABLE_CORE]["params"] == ["S1", "20261101", "20261102"]
    assert w["kpi_macro"]["where"].startswith("`scenic_id` IN")
    assert "`travel_date` IN" in w[TABLE_DRILL_ANALYSIS]["where"]
    assert w[TABLE_DRILL_ANALYSIS]["params"][1:] == [20261101, 20261102]
    # 只写引擎的列（目标表多出来的 id / created_time 不管）
    assert w[TABLE_CORE]["cols"] == list(produced[TABLE_CORE].columns)
    assert sum(r.rows for r in rep.results) == sum(len(df) for df in produced.values())


def test_missing_columns_are_skipped_and_reported(produced):
    tables = {TABLE_CORE: [c for c in TABLE_COLUMNS[TABLE_CORE] if c != "etl_time"],
              TABLE_PLATFORM: ["platform_code"]}                             # 平台表缺删除用的列
    S.MySQL = lambda cfg: TargetDB(cfg, tables)
    rep = push_all({t: produced[t] for t in (TABLE_CORE, TABLE_PLATFORM, TABLE_CONTENT)},
                   settings=_st(), scope=(["S1"], ["20261101"]))
    by = {r.table: r for r in rep.results}
    assert by[TABLE_CORE].ok and "etl_time" in by[TABLE_CORE].warning
    assert "etl_time" not in TargetDB.instances[-1].writes[0]["cols"]
    assert not by[TABLE_PLATFORM].ok and "缺少删除用的列" in by[TABLE_PLATFORM].error
    assert not by[TABLE_CONTENT].ok and "没有表" in by[TABLE_CONTENT].error   # 一张表失败不影响其他表


def test_unreachable_stops_and_dry_run(produced, tmp_path, monkeypatch):
    monkeypatch.setenv("OPINION_PUSH_LOG_DIR", str(tmp_path))
    S.MySQL = lambda cfg: TargetDB(cfg, fail_connect=OpErr(2003, "Can't connect to MySQL server"))
    rep = push_all(produced, settings=_st(), scope=(["S1"], ["20261101"]))
    assert not rep.ok and rep.results[0].unreachable and "Can't connect" in rep.results[0].error
    assert all("已停止" in r.error for r in rep.results[1:])
    S.MySQL = TargetDB
    rep = push_all(produced, settings=_st(push_dry_run=True), scope=(["S1"], ["20261101"]))
    assert rep.ok and not TargetDB.instances[-1].writes                        # 试推：不写
    recs = [json.loads(l) for f in tmp_path.iterdir() for l in f.read_text(encoding="utf-8").splitlines()]
    assert {r["result"] for r in recs} == {"db_error", "dry_run"}
    assert all("pw-secret" not in json.dumps(r, ensure_ascii=False) for r in recs)   # 推送记录不记密码
    assert recs[0]["url"] == "mysql://客户数仓（10.0.0.8:3307/dw）"


def test_pipeline_passes_run_scope(produced, monkeypatch):
    rows = []
    for d in pd.date_range("2026-10-01", "2026-11-02"):
        ds = d.strftime("%Y%m%d")
        rows.append(_comment(f"a{ds}", ds))
    st = _st(write_db=False, lookback_days=40)
    res = run(st, ["20261102"], comments_df=pd.DataFrame(rows))
    assert res.push is not None and res.push.ok, res.push.errors if res.push else None
    core = next(w for w in TargetDB.instances[0].writes if w["table"] == TABLE_CORE)
    assert core["params"] == ["S1", "20261102"]


def test_check_connection():
    tables = {TABLE_CORE: list(TABLE_COLUMNS[TABLE_CORE]),
              "kpi_macro": [c for c in TABLE_COLUMNS[TABLE_MACRO] if c != "wordcloud_map"]}
    r = S.check(DS, {TABLE_CORE: TABLE_COLUMNS[TABLE_CORE], TABLE_MACRO: TABLE_COLUMNS[TABLE_MACRO],
                     TABLE_CONTENT: TABLE_COLUMNS[TABLE_CONTENT]},
                connect=lambda cfg: TargetDB(cfg, tables))
    by = {t["table"]: t for t in r["tables"]}
    assert r["ok"] and by[TABLE_CORE]["writable"] and not by[TABLE_CORE]["missing"]
    assert by[TABLE_MACRO]["target"] == "kpi_macro" and by[TABLE_MACRO]["missing"] == ["wordcloud_map"]
    assert not by[TABLE_CONTENT]["exists"]
    r = S.check(DS, {TABLE_CORE: []}, connect=lambda cfg: TargetDB(cfg, fail_connect=OpErr(1045, "Access denied")))
    assert not r["ok"] and "Access denied" in r["error"]


def test_settings_helpers():
    st = _st()
    assert push_dest(st) == "数据源 客户数仓（10.0.0.8:3307/dw）" and push_not_ready(st) == ""
    st.push_api, st.push_url = True, "https://x.example/sync"
    assert push_dest(st) == "API https://x.example/sync + 数据源 客户数仓（10.0.0.8:3307/dw）"
    st.push_db = {"host": "h"}
    assert "缺 user、database" in push_not_ready(st)
    st.push_api, st.push_db = False, None
    assert "都关着" in push_not_ready(st)
    api = EtlSettings()
    assert "未配置推送地址" in push_not_ready(api)
    apply_overrides(api, json.dumps({"push_api": False, "push_db": DS}))     # 服务传给子进程的方式
    assert api.push_api is False and api.push_db["host"] == "10.0.0.8"


def test_cli_push_url_flag_means_api():
    st = _st()

    class A:
        push = "all"
        no_push = push_dry_run = push_strict = False
        push_url = "https://x.example/sync"
    cli._apply_push_args(st, A())
    assert st.push_api is True and st.push_db is None                         # 命令行给了地址：这次只按 API 推


# ══════════════════════════════════════════════════════════════════
# 服务：数据源管理、推送方式、交给子进程
# ══════════════════════════════════════════════════════════════════
def test_normalize_datasource():
    d = R.normalize_datasource({"name": " 数仓 ", "host": "h", "user": "u", "database": "dw", "port": "3307",
                                "table_names": {TABLE_CORE: "core_x", TABLE_MACRO: "", TABLE_CONTENT: TABLE_CONTENT}})
    assert d["name"] == "数仓" and d["port"] == 3307 and d["table_names"] == {TABLE_CORE: "core_x"}
    assert R.normalize_datasource({**d, "password": ""}, {"password": "old"})["password"] == "old"  # 留空不改
    for bad, why in (({**d, "host": ""}, "地址"), ({**d, "port": "x"}, "数字"), ({**d, "database": "a-b"}, "库名"),
                     ({**d, "table_names": {TABLE_CORE: "x;drop"}}, "目标表名")):
        with pytest.raises(ValueError, match=why):
            R.normalize_datasource(bad)
    v = R.datasource_view({**d, "password": "pw"})
    assert "password" not in v and v["has_password"]


def test_push_overrides_carry_datasource_but_log_hides_password():
    over = R.push_overrides({**R.PUSH_DEFAULTS, "api_enabled": False, "db_enabled": True, "datasource": "ds_1"},
                            {**DS, "id": "ds_1"})
    assert over["push_api"] is False and over["push_db"]["password"] == "pw-secret"
    assert "id" not in over["push_db"]
    plain = R.push_overrides(dict(R.PUSH_DEFAULTS))
    assert "push_api" not in plain and "push_db" not in plain                 # 默认：API 开、数据源关，不用传


def test_old_single_choice_setting_is_migrated():
    """早先存的「推送方式」二选一（target）→ 两个开关。"""
    ps = R.normalize_push_settings({"enabled": True, "target": "ds_1"})
    assert (ps["api_enabled"], ps["db_enabled"], ps["datasource"]) == (False, True, "ds_1") and "target" not in ps
    ps = R.normalize_push_settings({"target": "api"})
    assert (ps["api_enabled"], ps["db_enabled"], ps["datasource"]) == (True, False, "")
    ps = R.normalize_push_settings({})
    assert (ps["api_enabled"], ps["db_enabled"]) == (True, False)


def test_both_ways_on_push_api_then_write_datasource(produced, monkeypatch):
    import engin_cli.pusher as P
    sent = []

    def fake_push_table(table, df, settings, progress=None):
        sent.append(table)
        return P.PushResult(table=table, rows=len(df), batches=1)
    monkeypatch.setattr(P, "push_table", fake_push_table)
    st = _st(push_api=True, push_url="https://x.example/sync")
    rep = push_all({TABLE_CORE: produced[TABLE_CORE]}, settings=st, scope=(["S1"], ["20261101"]))
    assert rep.ok and sent == [TABLE_CORE] and TargetDB.instances[0].writes[0]["table"] == TABLE_CORE
    assert [r.via for r in rep.results] == ["", "数据源"]
    assert any(n.startswith("推送 ads_trf_social_opinion_comment_core_di") for n in rep.notes())
    assert any(n.startswith("写入数据源 ads_trf_social_opinion_comment_core_di") for n in rep.notes())
    # 只开 API：不碰数据源
    TargetDB.instances = []
    rep = push_all({TABLE_CORE: produced[TABLE_CORE]}, settings=_st(push_api=True, push_url="https://x", push_db=None))
    assert rep.ok and not TargetDB.instances
    # 都关：不推，说清楚
    rep = push_all({TABLE_CORE: produced[TABLE_CORE]}, settings=_st(push_api=False, push_db=None))
    assert "都关着" in rep.results[0].error


def _app(tmp_path):
    from engin_cli.service.server import App
    st = EtlSettings()
    st.service_store = "sqlite"
    app = App(st, data_dir=str(tmp_path), scheduler_enabled=False, db_factory=lambda: None, auth=object())
    app._ds_connect = lambda cfg: TargetDB(cfg)
    return app


def test_datasource_api(tmp_path):
    from engin_cli.service.server import ApiError
    app = _app(tmp_path)
    d = app.save_datasource({**DS, "table_names": {TABLE_MACRO: "kpi_macro"}})
    assert d["id"].startswith("ds_") and "password" not in d and d["has_password"]
    with pytest.raises(ApiError, match="已经有叫"):
        app.save_datasource(DS)
    # 修改：密码留空不改
    app.save_datasource({**DS, "password": "", "host": "10.0.0.9"}, d["id"])
    saved = app.runner.datasources()[0]
    assert saved["host"] == "10.0.0.9" and saved["password"] == "pw-secret"
    # 测试连接：已保存的 / 表单（密码留空用已保存的）
    r = app.test_datasource({"id": d["id"]})
    assert r["ok"] and {t["table"] for t in r["tables"]} == set(TABLE_COLUMNS)
    assert TargetDB.instances[-1].cfg.password == "pw-secret"
    # 推送方式选它
    with pytest.raises(ApiError, match="要选一个数据源"):
        app.save_push_settings({"db_enabled": True, "datasource": "ds_nope"})
    v = app.save_push_settings({"db_enabled": True, "datasource": d["id"]})
    assert v["target_datasource"]["name"] == "客户数仓" and app.push_ready() is True
    assert v["ways"] == ["API 接口", "数据源「客户数仓」"]                      # 两个都开着：两个都推
    assert app.config_view()["推送"]["push_target"] == "API 接口 + 写入数据源 客户数仓（10.0.0.9:3307/dw）"
    v = app.save_push_settings({"api_enabled": False})
    assert v["ways"] == ["数据源「客户数仓」"]
    assert "pw-secret" not in json.dumps(v, ensure_ascii=False)
    assert "pw-secret" not in json.dumps(app.config_view(), ensure_ascii=False, default=str)
    # 正在用的不能删；改回 API 再删
    with pytest.raises(ApiError, match="正在写入"):
        app.delete_datasource(d["id"])
    v = app.save_push_settings({"db_enabled": False})
    assert v["ways"] == [] and app.push_ready() is False                       # 两个都关：不推
    assert app.delete_datasource(d["id"])["ok"] and app.runner.datasources() == []
    assert app.runner.push_settings()["datasource"] == ""                      # 选中的被删了：清掉
    texts = [e["text"] for e in app.store.list_events()]
    assert any("写入数据源已开启：「客户数仓」" in t for t in texts)
    assert any("API 接口推送已关闭" in t for t in texts) and any("写入数据源已关闭" in t for t in texts)


def test_datasource_routes_are_admin_only():
    from engin_cli.service.auth import denied_reason
    for m in ("POST", "PUT", "DELETE"):
        assert "管理员" in denied_reason("operator", m, "/api/datasources/ds_1")
    assert denied_reason("operator", "GET", "/api/datasources") == ""
    assert denied_reason("admin", "POST", "/api/datasources") == ""


def test_runner_hands_datasource_to_subprocess(tmp_path):
    """执行时：选了数据源 → 子进程拿到 push_target / push_db；日志里只写名称地址，不写密码。"""
    import os
    import sys
    app = _app(tmp_path)
    d = app.save_datasource(DS)
    app.save_push_settings({"api_enabled": False, "db_enabled": True, "datasource": d["id"]})
    seen = {}
    fake = tmp_path / "fake_cli.py"
    fake.write_text("import os\n"
                    "open(os.environ['OUT'], 'w').write(os.environ.get('OPINION_SETTINGS_JSON', ''))\n",
                    encoding="utf-8")
    runner = app.runner
    runner.python = sys.executable
    out = tmp_path / "env.json"
    os.environ["OUT"] = str(out)
    try:
        import subprocess
        real = subprocess.Popen

        def popen(cmd, **kw):
            seen["cmd"] = cmd
            return real([sys.executable, str(fake)], **kw)
        subprocess.Popen = popen
        rid = runner.submit("push", ["push", "--date", "20261101"], "补推")
        runner._execute(rid)
    finally:
        subprocess.Popen = real
        os.environ.pop("OUT", None)
    env = json.loads(out.read_text(encoding="utf-8"))
    assert env["push_api"] is False and env["push_db"]["password"] == "pw-secret"
    log = open(app.store.get_run(rid)["log_path"], encoding="utf-8").read()
    assert "（推送方式：写入数据源 客户数仓（10.0.0.8:3307/dw）" in log and "pw-secret" not in log
    # 两种方式都关：跑批改成 --no-push（只算不推），不会因为「没有推送方式」失败
    app.save_push_settings({"db_enabled": False})
    try:
        subprocess.Popen = popen
        os.environ["OUT"] = str(out)
        rid = runner.submit("run", ["run", "--date", "20261101", "--push", "drill,macro"], "跑批")
        runner._execute(rid)
    finally:
        subprocess.Popen = real
        os.environ.pop("OUT", None)
    assert "--no-push" in seen["cmd"] and "--push" not in seen["cmd"]
    log = open(app.store.get_run(rid)["log_path"], encoding="utf-8").read()
    assert "API 推送和写入数据源都没在用：本次只算不推" in log
