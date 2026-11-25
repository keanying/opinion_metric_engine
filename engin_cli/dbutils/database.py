# -*- coding: utf-8 -*-
"""Database：一个 MySQL 连接（可经 SSH 隧道）+ 常用的增删改查。

· 懒连接：第一次用到时才建隧道、连库；close() 断库、关隧道。推荐 with Database(cfg) as db: … 。
· 默认不自动提交：每个写操作（insert / update / delete / replace_rows / execute）自己提交，失败回滚；
  在 with db.transaction(): 里则一起提交 / 一起回滚。
· 值一律走参数（%s），表名 / 列名经 quote_ident 校验转义。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import DataSourceConfig
from .errors import ConnectError, TunnelError
from .sql import Where, build_where, insert_sql, quote_ident, rows_and_columns
from .tunnel import SSHTunnel

log = logging.getLogger(__name__)

# pymysql 连接阶段的错误码：连不上 / 断开 / 账号密码 / 库不存在 / 主机不允许
_CONNECT_CODES = {1044, 1045, 1049, 1130, 2003, 2005, 2006, 2013}


class Database:
    def __init__(self, cfg: DataSourceConfig, *, connect=None):
        """connect：替换 pymysql.connect（测试用）。"""
        self.cfg = cfg
        self._connect_fn = connect
        self._conn = None
        self._tunnel: Optional[SSHTunnel] = None
        self._tx_depth = 0

    # ================================================================ 连接
    def connect(self):
        """连上（已连就直接返回连接）。连不上抛 ConnectError / TunnelError，说人话。"""
        if self._conn is not None:
            return self._conn
        cfg = self.cfg
        host, port = cfg.host, int(cfg.port)
        if cfg.ssh is not None:
            self._tunnel = SSHTunnel(cfg.ssh, cfg.host, cfg.port)
            try:
                port, host = self._tunnel.start(), "127.0.0.1"
            except BaseException:
                self._tunnel.stop()
                self._tunnel = None
                raise
        fn = self._connect_fn
        if fn is None:
            try:
                import pymysql
            except ImportError as e:                 # pragma: no cover
                self._close_tunnel()
                raise ConnectError("缺少依赖 pymysql：pip install pymysql") from e
            fn = pymysql.connect
        try:
            self._conn = fn(host=host, port=port, user=cfg.user, password=cfg.password,
                            database=cfg.database or None, charset=cfg.charset, autocommit=False,
                            connect_timeout=cfg.connect_timeout, read_timeout=cfg.read_timeout,
                            write_timeout=cfg.write_timeout, **(cfg.extra or {}))
        except Exception as e:                       # noqa: BLE001
            self._close_tunnel()
            raise self._connect_error(e) from e
        log.info("已连接 %s", cfg.describe())
        return self._conn

    def _connect_error(self, e: Exception) -> Exception:
        a = getattr(e, "args", ())
        code = a[0] if a and isinstance(a[0], int) else None
        text = a[1] if len(a) > 1 else str(e)
        if code not in _CONNECT_CODES:
            return e
        cfg = self.cfg
        if cfg.ssh is not None and code in (2003, 2013):
            return TunnelError(f"SSH 隧道通了，但从跳板机 {cfg.ssh.host} 连不到数据库 {cfg.host}:{cfg.port}："
                               "数据库地址 / 端口要填从跳板机上看到的（比如云数据库的内网地址），确认填对、跳板机能访问它")
        hint = {1045: "用户名或密码不对", 1044: "这个用户没有这个库的权限", 1049: f"库 {cfg.database} 不存在",
                1130: "数据库不允许从这台机器连（要在数据库上给这个来源 IP 授权）"}.get(code, "")
        return ConnectError(f"连不上数据库 {cfg.host}:{cfg.port}：{hint + '，' if hint else ''}{text}（{code}）")

    def close(self):
        try:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:                    # noqa: BLE001
                    pass
        finally:
            self._conn = None
            self._close_tunnel()

    def _close_tunnel(self):
        if self._tunnel is not None:
            self._tunnel.stop()
            self._tunnel = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def ping(self) -> Dict[str, Any]:
        """测一下：连上，返回版本、当前库、连法（直连 / 经 SSH）。"""
        row = self.query_one("select version() v, database() d")
        return {"version": row["v"], "database": row["d"],
                "via": f"SSH {self.cfg.ssh.describe()}" if self.cfg.ssh else "直连"}

    # ================================================================ 事务
    @contextmanager
    def transaction(self):
        """with db.transaction(): …  里面的写操作一起提交、出错一起回滚。可以嵌套（只有最外层提交）。"""
        conn = self.connect()
        self._tx_depth += 1
        try:
            yield self
            if self._tx_depth == 1:
                conn.commit()
        except BaseException:
            if self._tx_depth == 1:
                conn.rollback()
            raise
        finally:
            self._tx_depth -= 1

    @contextmanager
    def _write(self):
        """单个写操作：不在事务里就自己提交 / 回滚。"""
        if self._tx_depth:
            yield self.connect()
            return
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # ================================================================ 查
    def query(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        """执行查询，返回 [{列: 值}]。"""
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, list(params or []))
            cols = [d[0] for d in (cur.description or [])]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def query_one(self, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, list(params or []))
            r = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], r)) if r is not None else None

    def scalar(self, sql: str, params: Optional[Sequence[Any]] = None) -> Any:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, list(params or []))
            r = cur.fetchone()
            return r[0] if r else None

    def query_df(self, sql: str, params: Optional[Sequence[Any]] = None, chunk_size: int = 20000):
        """查询结果 → pandas DataFrame（分批取，大结果不一次性 fetchall）。"""
        import pandas as pd
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, list(params or []))
            cols = [d[0] for d in (cur.description or [])]
            chunks = []
            while True:
                rows = cur.fetchmany(chunk_size)
                if not rows:
                    break
                chunks.append(pd.DataFrame(list(rows), columns=cols))
        return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(columns=cols)

    def select(self, table: str, columns: Sequence[str] = ("*",), where: Where = None,
               params: Optional[Sequence[Any]] = None, order_by: str = "", limit: Optional[int] = None,
               offset: int = 0) -> List[Dict[str, Any]]:
        """select 列 from 表 [where] [order by] [limit]。order_by 写「列 [asc|desc], …」。"""
        cols = "*" if list(columns) == ["*"] else ",".join(quote_ident(c) for c in columns)
        sql = f"SELECT {cols} FROM {quote_ident(table)}"
        w, ps = build_where(where, params)
        if w:
            sql += f" WHERE {w}"
        if order_by:
            sql += " ORDER BY " + ",".join(_order_item(x) for x in order_by.split(","))
        if limit is not None:
            sql += f" LIMIT {int(limit)}" + (f" OFFSET {int(offset)}" if offset else "")
        return self.query(sql, ps)

    def count(self, table: str, where: Where = None, params: Optional[Sequence[Any]] = None) -> int:
        w, ps = build_where(where, params)
        return int(self.scalar(f"SELECT COUNT(*) FROM {quote_ident(table)}" + (f" WHERE {w}" if w else ""), ps) or 0)

    def exists(self, table: str, where: Where = None, params: Optional[Sequence[Any]] = None) -> bool:
        w, ps = build_where(where, params)
        return self.scalar(f"SELECT 1 FROM {quote_ident(table)}" + (f" WHERE {w}" if w else "") + " LIMIT 1",
                           ps) is not None

    # ================================================================ 元数据
    def tables(self) -> List[str]:
        return [r["t"] for r in self.query(
            "select table_name t from information_schema.tables where table_schema=database() order by 1")]

    def table_exists(self, table: str) -> bool:
        return bool(self.columns(table))

    def columns(self, table: str) -> List[str]:
        """表有哪些列（按表里的顺序）；表不存在返回 []。「库.表」也认。"""
        db, name = _split(table)
        return [r["c"] for r in self.query(
            "select column_name c from information_schema.columns where table_schema=coalesce(%s, database()) "
            "and table_name=%s order by ordinal_position", [db, name])]

    # ================================================================ 写
    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> int:
        """执行一条改动语句，返回影响行数。"""
        with self._write() as conn, conn.cursor() as cur:
            return cur.execute(sql, list(params or []))

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]], batch_size: int = 1000) -> int:
        """同一条语句、多组参数，按 batch_size 分批，返回影响行数合计。"""
        seq = list(seq)
        n = 0
        with self._write() as conn, conn.cursor() as cur:
            for i in range(0, len(seq), max(int(batch_size), 1)):
                n += cur.executemany(sql, seq[i:i + batch_size]) or 0
        return n

    def insert(self, table: str, rows: Any, *, columns: Optional[Sequence[str]] = None,
               on_duplicate: Optional[str] = None, update_columns: Optional[Sequence[str]] = None,
               batch_size: int = 1000) -> int:
        """写入多行，返回提交的行数（on_duplicate="ignore" 时被跳过的也算在内）。

        rows：DataFrame / [dict] / [tuple]（tuple 要给 columns）；columns 也可以用来只写其中几列。
        on_duplicate：None 撞唯一键报错；"ignore" 跳过；"update" 覆盖（update_columns 不给 = 全部列）。
        """
        cols, data = rows_and_columns(rows, columns)
        if not data:
            return 0
        sql = insert_sql(table, cols, on_duplicate, update_columns)
        with self._write() as conn, conn.cursor() as cur:
            for i in range(0, len(data), max(int(batch_size), 1)):
                cur.executemany(sql, data[i:i + batch_size])
        log.info("写入 %s：%d 行", table, len(data))
        return len(data)

    def upsert(self, table: str, rows: Any, *, columns: Optional[Sequence[str]] = None,
               update_columns: Optional[Sequence[str]] = None, batch_size: int = 1000) -> int:
        """写入，撞唯一键就更新（INSERT … ON DUPLICATE KEY UPDATE）。"""
        return self.insert(table, rows, columns=columns, on_duplicate="update",
                           update_columns=update_columns, batch_size=batch_size)

    def update(self, table: str, values: Dict[str, Any], where: Where,
               params: Optional[Sequence[Any]] = None) -> int:
        """update 表 set 列=值 … where …，返回影响行数。不给 where 直接报错（防手滑改全表）。"""
        if not values:
            return 0
        w, ps = build_where(where, params)
        if not w:
            raise ValueError("update 必须带 where 条件（真要改全表请用 execute 自己写 SQL）")
        _, vals = rows_and_columns([values])
        sets = ",".join(f"{quote_ident(c)}=%s" for c in values)
        return self.execute(f"UPDATE {quote_ident(table)} SET {sets} WHERE {w}", list(vals[0]) + ps)

    def delete(self, table: str, where: Where, params: Optional[Sequence[Any]] = None) -> int:
        """delete from 表 where …，返回删除行数。不给 where 直接报错（清空表请用 truncate）。"""
        w, ps = build_where(where, params)
        if not w:
            raise ValueError("delete 必须带 where 条件（真要清空表请用 truncate）")
        return self.execute(f"DELETE FROM {quote_ident(table)} WHERE {w}", ps)

    def truncate(self, table: str) -> None:
        """清空整张表（不可回滚）。"""
        self.execute(f"TRUNCATE TABLE {quote_ident(table)}")

    def replace_rows(self, table: str, rows: Any, where: Where, params: Optional[Sequence[Any]] = None, *,
                     columns: Optional[Sequence[str]] = None, batch_size: int = 1000,
                     on_duplicate: Optional[str] = "update") -> Tuple[int, int]:
        """先清理再写入：删掉 where 范围内的旧行，再写入 rows，**同一个事务**，任何一步失败整体回滚
        （不会出现删了没写的空窗）。rows 为空也会删（这次确实没有行了，旧的也要清掉）。

        on_duplicate 默认 "update"：删完再插正常不会撞唯一键，只兜底同一批里的重复行。
        返回 (删除行数, 写入行数)。
        """
        w, ps = build_where(where, params)
        if not w:
            raise ValueError("replace_rows 必须带 where 条件：先删哪些行要说清楚")
        cols, data = rows_and_columns(rows, columns)
        with self.transaction():
            conn = self.connect()
            with conn.cursor() as cur:
                deleted = cur.execute(f"DELETE FROM {quote_ident(table)} WHERE {w}", ps)
                if data:
                    sql = insert_sql(table, cols, on_duplicate)
                    for i in range(0, len(data), max(int(batch_size), 1)):
                        cur.executemany(sql, data[i:i + batch_size])
        log.info("写入 %s：删除 %d 行，写入 %d 行（先删后写）", table, deleted, len(data))
        return int(deleted or 0), len(data)


def _split(table: str) -> Tuple[Optional[str], str]:
    quote_ident(table)                               # 校验
    parts = str(table).strip().split(".")
    return (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])


def _order_item(x: str) -> str:
    bits = x.strip().split()
    if not bits or len(bits) > 2 or (len(bits) == 2 and bits[1].lower() not in ("asc", "desc")):
        raise ValueError(f"order_by 写法不对：{x!r}（应为「列 [asc|desc]」）")
    return quote_ident(bits[0]) + (f" {bits[1].upper()}" if len(bits) == 2 else "")
