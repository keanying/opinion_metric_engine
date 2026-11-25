# -*- coding: utf-8 -*-
"""dbutils 组件：SQL 拼装、值清洗、CRUD、先删后写、事务、连接错误、SSH 私钥。

默认用假的 pymysql 连接（记下执行了什么 SQL），不连任何真实库。
设了环境变量 DBUTILS_TEST_MYSQL（JSON：{"host","port","user","password","database"}，可带 "ssh": {...}）
时，另跑一遍真实库的增删改查往返。
"""

import datetime as dt
import io
import json
import os

import pytest

from engin_cli.dbutils import (ConnectError, Database, DataSourceConfig, SSHConfig, TunnelError,
                               build_where, quote_ident)
from engin_cli.dbutils.sql import clean_value, insert_sql, rows_and_columns


# ══════════════════════════════════════════════════════════════════
# 假连接
# ══════════════════════════════════════════════════════════════════
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql, params=()):
        self.conn.log.append(("execute", sql, list(params)))
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("boom")
        res = self.conn.results.pop(0) if self.conn.results else ([], [])
        cols, self._rows = res
        self.description = [(c,) for c in cols] if cols else None
        return self.conn.rowcount

    def executemany(self, sql, seq):
        seq = list(seq)
        self.conn.log.append(("executemany", sql, seq))
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("boom")
        return len(seq)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeConn:
    def __init__(self):
        self.log, self.results, self.commits, self.rollbacks = [], [], 0, 0
        self.rowcount, self.fail_on, self.closed = 3, "", False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


CFG = DataSourceConfig(host="db.internal", port=3306, user="u", password="p", database="dw", name="数仓")


def _db():
    conn = FakeConn()
    seen = {}

    def connect(**kw):
        seen.update(kw)
        return conn
    return Database(CFG, connect=connect), conn, seen


def _sqls(conn):
    return [x[1] for x in conn.log]


# ══════════════════════════════════════════════════════════════════
# SQL 拼装
# ══════════════════════════════════════════════════════════════════
def test_quote_ident():
    assert quote_ident("t_user") == "`t_user`" and quote_ident("dw.t_user") == "`dw`.`t_user`"
    assert quote_ident("景区") == "`景区`"
    for bad in ("a b", "x;drop table y", "a`b", "a.b.c", "", "t--"):
        with pytest.raises(ValueError):
            quote_ident(bad)


def test_build_where():
    w, p = build_where({"scenic_id": ["S1", "S2"], "travel_date": 20261101, "deleted_at": None, "x": []})
    assert w == "`scenic_id` IN (%s,%s) AND `travel_date` = %s AND `deleted_at` IS NULL AND 1=0"
    assert p == ["S1", "S2", 20261101]
    assert build_where("a = %s", [1]) == ("a = %s", [1])
    assert build_where(("a = %s", (1,))) == ("a = %s", [1])
    assert build_where(None) == ("", []) and build_where({}) == ("", [])
    with pytest.raises(TypeError):
        build_where(123)


def test_clean_value_and_rows():
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    assert clean_value(float("nan")) is None and clean_value(np.float64("nan")) is None
    assert clean_value(np.int64(3)) == 3 and type(clean_value(np.int64(3))) is int
    assert clean_value(pd.NaT) is None and clean_value(pd.NA) is None
    assert clean_value(pd.Timestamp("2026-11-01 08:00")) == dt.datetime(2026, 11, 1, 8, 0)
    df = pd.DataFrame({"a": np.array([1, 2], dtype="int64"), "b": [1.5, float("nan")], "c": ["x", None]})
    assert rows_and_columns(df) == (["a", "b", "c"], [(1, 1.5, "x"), (2, None, None)])
    assert rows_and_columns(df, ["c", "a"]) == (["c", "a"], [("x", 1), (None, 2)])
    assert rows_and_columns([{"a": 1}, {"a": 2, "b": 3}]) == (["a", "b"], [(1, None), (2, 3)])
    assert rows_and_columns([(1, 2)], ["a", "b"]) == (["a", "b"], [(1, 2)])
    with pytest.raises(ValueError):
        rows_and_columns([(1, 2)])
    with pytest.raises(ValueError):
        rows_and_columns([(1, 2)], ["a"])


def test_insert_sql():
    assert insert_sql("t", ["a", "b"]) == "INSERT INTO `t` (`a`,`b`) VALUES (%s,%s)"
    assert insert_sql("t", ["a"], "ignore").startswith("INSERT IGNORE INTO `t`")
    assert insert_sql("t", ["a", "b"], "update", ["b"]).endswith("ON DUPLICATE KEY UPDATE `b`=VALUES(`b`)")
    with pytest.raises(ValueError):
        insert_sql("t", ["a"], "replace")


# ══════════════════════════════════════════════════════════════════
# CRUD
# ══════════════════════════════════════════════════════════════════
def test_connect_is_lazy_and_closes():
    db, conn, seen = _db()
    assert not conn.log and not seen                 # 没用到就不连
    with db:
        db.execute("select 1")
    assert seen["host"] == "db.internal" and seen["database"] == "dw" and seen["autocommit"] is False
    assert conn.closed


def test_query_select_count_exists():
    db, conn, _ = _db()
    conn.results = [(["id", "name"], [(1, "a"), (2, "b")])]
    assert db.query("select id, name from t") == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    conn.results = [(["id"], [(1,)])]
    db.select("t_user", ["id"], where={"city": ["杭州"]}, order_by="id desc, name", limit=10, offset=20)
    assert conn.log[-1][1] == ("SELECT `id` FROM `t_user` WHERE `city` IN (%s) ORDER BY `id` DESC,`name` "
                               "LIMIT 10 OFFSET 20") and conn.log[-1][2] == ["杭州"]
    conn.results = [(["n"], [(5,)])]
    assert db.count("t_user", where={"x": None}) == 5
    assert conn.log[-1][1] == "SELECT COUNT(*) FROM `t_user` WHERE `x` IS NULL"
    conn.results = [(["1"], [])]
    assert db.exists("t_user", where={"id": 9}) is False
    with pytest.raises(ValueError):
        db.select("t", order_by="id; drop table t")


def test_insert_upsert_batches_and_commits():
    db, conn, _ = _db()
    n = db.insert("t", [{"a": i} for i in range(5)], batch_size=2)
    many = [x for x in conn.log if x[0] == "executemany"]
    assert n == 5 and [len(x[2]) for x in many] == [2, 2, 1] and conn.commits == 1
    db.upsert("t", [{"a": 1, "b": 2}], update_columns=["b"])
    assert conn.log[-1][1].endswith("ON DUPLICATE KEY UPDATE `b`=VALUES(`b`)")
    assert db.insert("t", []) == 0


def test_update_delete_require_where():
    db, conn, _ = _db()
    assert db.update("t", {"status": 0, "note": None}, where={"id": [1, 2]}) == 3
    assert conn.log[-1][1] == "UPDATE `t` SET `status`=%s,`note`=%s WHERE `id` IN (%s,%s)"
    assert conn.log[-1][2] == [0, None, 1, 2]
    assert db.delete("t", where={"id": 1}) == 3
    for fn in (lambda: db.update("t", {"a": 1}, where=None), lambda: db.delete("t", where={}),
               lambda: db.replace_rows("t", [], where=None)):
        with pytest.raises(ValueError, match="必须带 where"):
            fn()


def test_replace_rows_is_one_transaction():
    db, conn, _ = _db()
    conn.rowcount = 7
    deleted, written = db.replace_rows("kpi", [{"scenic_id": "S1", "v": 1}, {"scenic_id": "S1", "v": 2}],
                                       where={"scenic_id": ["S1"], "travel_date": [20261101]})
    assert (deleted, written) == (7, 2)
    assert _sqls(conn)[0] == "DELETE FROM `kpi` WHERE `scenic_id` IN (%s) AND `travel_date` IN (%s)"
    assert _sqls(conn)[1].startswith("INSERT INTO `kpi`") and conn.commits == 1 and conn.rollbacks == 0
    # 写失败：删除一起回滚
    db2, conn2, _ = _db()
    conn2.fail_on = "INSERT"
    with pytest.raises(RuntimeError):
        db2.replace_rows("kpi", [{"v": 1}], where=("travel_date = %s", [20261101]))
    assert conn2.commits == 0 and conn2.rollbacks == 1
    # 没有新行：只删
    db3, conn3, _ = _db()
    assert db3.replace_rows("kpi", [], where={"v": 1}) == (3, 0) and len(conn3.log) == 1


def test_transaction_commits_once_and_rolls_back():
    db, conn, _ = _db()
    with db.transaction():
        db.insert("t", [{"a": 1}])
        with db.transaction():                        # 嵌套：只有最外层提交
            db.delete("t", where={"a": 2})
        db.update("t", {"a": 3}, where={"a": 1})
    assert conn.commits == 1 and conn.rollbacks == 0
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.insert("t", [{"a": 1}])
            raise RuntimeError("x")
    assert conn.commits == 1 and conn.rollbacks == 1


def test_connect_errors_are_explained():
    class E(Exception):
        pass

    def fail(code, text):
        def connect(**kw):
            raise E(code, text)
        return connect
    with pytest.raises(ConnectError, match="用户名或密码不对"):
        Database(CFG, connect=fail(1045, "Access denied")).connect()
    with pytest.raises(ConnectError, match="库 dw 不存在"):
        Database(CFG, connect=fail(1049, "Unknown database")).connect()
    with pytest.raises(ConnectError, match="Can't connect"):
        Database(CFG, connect=fail(2003, "Can't connect")).connect()
    with pytest.raises(E):                            # 不是连接类错误：原样抛
        Database(CFG, connect=fail(1064, "syntax")).connect()


def test_config_from_datasource_dict_and_describe():
    c = DataSourceConfig.from_dict({"name": "数仓", "host": "rm-x", "port": "3306", "user": "u", "password": "pw",
                                    "database": "dw", "ssh_enabled": True, "ssh_host": "10.1.1.8",
                                    "ssh_port": 12301, "ssh_user": "mysql_connector", "ssh_key": "KEY"})
    assert c.ssh.port == 12301 and c.ssh.private_key == "KEY"
    assert c.describe() == "数仓（rm-x:3306/dw，经 SSH mysql_connector@10.1.1.8:12301）"
    assert "pw" not in c.describe() and "KEY" not in c.describe()
    assert DataSourceConfig.from_dict({"host": "h", "user": "u", "ssh_host": "x"}).ssh is None   # 没开就不走隧道


# ══════════════════════════════════════════════════════════════════
# SSH 私钥
# ══════════════════════════════════════════════════════════════════
def _rsa_pem(password=None):
    paramiko = pytest.importorskip("paramiko")
    k = paramiko.RSAKey.generate(1024)
    buf = io.StringIO()
    k.write_private_key(buf, password=password)
    return buf.getvalue()


def test_private_key_loading_messages():
    from engin_cli.dbutils import key_fingerprint, load_private_key
    plain, locked = _rsa_pem(), _rsa_pem("pw")
    assert load_private_key(plain).get_name() == "ssh-rsa"
    assert load_private_key(locked, "pw").get_bits() == 1024
    assert key_fingerprint(plain.replace("\n", "\r\n")).startswith("RSA 1024 · SHA256:")   # Windows 换行也认
    for text, pw, why in ((locked, "", "有密码保护"), (locked, "bad", "密钥密码不对"),
                          ("ssh-ed25519 AAAA… user@host", "", "公钥"),
                          ("PuTTY-User-Key-File-3: ssh-rsa", "", "PuTTYgen"), ("", "", "没有私钥")):
        with pytest.raises(TunnelError, match=why):
            load_private_key(text, pw)


def test_tunnel_unreachable_ssh_port_fails_fast():
    pytest.importorskip("paramiko")
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                         # 这个端口现在没人听
    cfg = DataSourceConfig(host="db", user="u", ssh=SSHConfig(host="127.0.0.1", port=port, user="x",
                                                              private_key=_rsa_pem(), timeout=3))
    with pytest.raises(TunnelError, match="连不上 SSH 服务器"):
        Database(cfg).connect()
    with pytest.raises(TunnelError, match="至少给一个"):
        Database(DataSourceConfig(host="db", user="u", ssh=SSHConfig(host="h", user="x"))).connect()


# ══════════════════════════════════════════════════════════════════
# 真实库（可选）
# ══════════════════════════════════════════════════════════════════
@pytest.mark.skipif(not os.environ.get("DBUTILS_TEST_MYSQL"), reason="没设 DBUTILS_TEST_MYSQL，跳过真实库测试")
def test_real_database_round_trip():
    raw = json.loads(os.environ["DBUTILS_TEST_MYSQL"])
    ssh = raw.pop("ssh", None)
    cfg = DataSourceConfig(**raw, ssh=SSHConfig(**ssh) if ssh else None)
    with Database(cfg) as db:
        assert db.ping()["database"] == cfg.database
        db.execute("drop table if exists dbutils_test")
        db.execute("create table dbutils_test (id int primary key, name varchar(20), d int) charset=utf8mb4")
        try:
            assert db.insert("dbutils_test", [{"id": 1, "name": "甲", "d": 1}, {"id": 2, "name": "乙", "d": 1}]) == 2
            db.upsert("dbutils_test", [{"id": 2, "name": "乙改", "d": 1}])
            assert db.update("dbutils_test", {"name": "甲改"}, where={"id": 1}) == 1
            assert db.replace_rows("dbutils_test", [{"id": 3, "name": "丙", "d": 1}], where={"d": 1}) == (2, 1)
            assert [r["name"] for r in db.select("dbutils_test", order_by="id")] == ["丙"]
            with pytest.raises(Exception):
                db.replace_rows("dbutils_test", [{"id": 4, "nope": 1}], where={"d": 1})
            assert db.count("dbutils_test") == 1      # 写失败，删除也回滚了
            assert db.columns("dbutils_test") == ["id", "name", "d"]
        finally:
            db.execute("drop table if exists dbutils_test")
