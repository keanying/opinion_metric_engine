# -*- coding: utf-8 -*-
"""MySQL 访问层。

只依赖 pymysql，不引 ORM —— 这个链路的 SQL 全是「大范围顺序读」和
「批量 upsert」，ORM 只会挡在中间。

连接懒建立：--dry-run / 只读 CSV 的场景不应该因为没配库就跑不起来。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from .settings import DBConfig

log = logging.getLogger(__name__)


class MySQL:
    def __init__(self, cfg: DBConfig):
        self.cfg = cfg
        self._conn = None

    # ---------- 连接 ----------
    def connect(self):
        if self._conn is not None:
            return self._conn
        try:
            import pymysql
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("缺少依赖 pymysql，请先 pip install pymysql") from e
        self._conn = pymysql.connect(
            host=self.cfg.host, port=int(self.cfg.port), user=self.cfg.user,
            password=self.cfg.password, database=self.cfg.database or None,
            charset=self.cfg.charset, autocommit=False,
            connect_timeout=self.cfg.connect_timeout,
            read_timeout=self.cfg.read_timeout,
            write_timeout=self.cfg.write_timeout,
        )
        log.info("已连接 MySQL %s:%s/%s", self.cfg.host, self.cfg.port, self.cfg.database)
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @contextmanager
    def cursor(self):
        conn = self.connect()
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    # ---------- 读 ----------
    def query_df(self, sql: str, params: Optional[Sequence[Any]] = None,
                 chunk_size: int = 20000) -> pd.DataFrame:
        """执行查询并返回 DataFrame。

        用 fetchmany 分批取，避免半年评论一次性 fetchall 把内存打满。
        """
        import pymysql.cursors  # noqa: F401
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            cols = [d[0] for d in cur.description]
            chunks: List[pd.DataFrame] = []
            total = 0
            while True:
                rows = cur.fetchmany(chunk_size)
                if not rows:
                    break
                total += len(rows)
                chunks.append(pd.DataFrame(list(rows), columns=cols))
            log.info("读取 %d 行 | %s", total, sql.split("\n")[0][:90])
        if not chunks:
            return pd.DataFrame(columns=cols)
        return pd.concat(chunks, ignore_index=True)

    def scalar(self, sql: str, params: Optional[Sequence[Any]] = None):
        with self.cursor() as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
        return row[0] if row else None

    def table_exists(self, table: str, database: Optional[str] = None) -> bool:
        db = database or self.cfg.database
        n = self.scalar(
            "select count(*) from information_schema.tables "
            "where table_schema=%s and table_name=%s", (db, table))
        return bool(n)

    def has_unique_key(self, table: str, database: Optional[str] = None) -> bool:
        """除主键外是否存在唯一索引 —— 决定用 upsert 还是 先删后插。"""
        db = database or self.cfg.database
        n = self.scalar(
            "select count(*) from information_schema.statistics "
            "where table_schema=%s and table_name=%s and non_unique=0 "
            "and index_name<>'PRIMARY'", (db, table))
        return bool(n)

    # ---------- 写 ----------
    def upsert_df(self, table: str, df: pd.DataFrame, batch_size: int = 2000,
                  update_cols: Optional[Sequence[str]] = None,
                  database: Optional[str] = None) -> int:
        """INSERT ... ON DUPLICATE KEY UPDATE，按 batch_size 分批提交。"""
        if df.empty:
            return 0
        cols = list(df.columns)
        upd = list(update_cols) if update_cols else cols
        full = f"`{database or self.cfg.ads_db()}`.`{table}`" if (database or self.cfg.ads_db()) \
            else f"`{table}`"
        placeholders = ",".join(["%s"] * len(cols))
        col_sql = ",".join(f"`{c}`" for c in cols)
        upd_sql = ",".join(f"`{c}`=VALUES(`{c}`)" for c in upd)
        sql = f"INSERT INTO {full} ({col_sql}) VALUES ({placeholders}) " \
              f"ON DUPLICATE KEY UPDATE {upd_sql}"

        data = _to_rows(df)
        conn = self.connect()
        written = 0
        with conn.cursor() as cur:
            for i in range(0, len(data), batch_size):
                cur.executemany(sql, data[i:i + batch_size])
                written += len(data[i:i + batch_size])
        conn.commit()
        log.info("写入 %s：%d 行（upsert）", table, written)
        return written

    def replace_rows(self, table: str, df: pd.DataFrame, where_sql: str,
                     params: Sequence[Any], batch_size: int = 2000,
                     database: Optional[str] = None) -> tuple:
        """先删后插，**在同一个事务里**：DELETE ... WHERE <where_sql>，再写入 df。

        任何一步失败都回滚 —— 不会出现「旧数据删了、新数据没写进去」的空窗。
        df 为空时只删不插（这次重算后某天确实没有行了，旧行也要清掉）。

        入参：where_sql 删除条件（不含 WHERE 关键字，参数用 %s 占位）；params 对应参数
        出参：(删除行数, 写入行数)
        """
        full = f"`{database or self.cfg.ads_db()}`.`{table}`" if (database or self.cfg.ads_db()) \
            else f"`{table}`"
        conn = self.connect()
        written = 0
        try:
            with conn.cursor() as cur:
                deleted = cur.execute(f"DELETE FROM {full} WHERE {where_sql}", list(params))
                if not df.empty:
                    cols = list(df.columns)
                    col_sql = ",".join(f"`{c}`" for c in cols)
                    placeholders = ",".join(["%s"] * len(cols))
                    # 删完再插，正常不会撞唯一键；ON DUPLICATE 只是兜底同一批里的重复行
                    upd_sql = ",".join(f"`{c}`=VALUES(`{c}`)" for c in cols)
                    sql = (f"INSERT INTO {full} ({col_sql}) VALUES ({placeholders}) "
                           f"ON DUPLICATE KEY UPDATE {upd_sql}")
                    data = _to_rows(df)
                    for i in range(0, len(data), batch_size):
                        cur.executemany(sql, data[i:i + batch_size])
                        written += len(data[i:i + batch_size])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        log.info("写入 %s：删除 %d 行，写入 %d 行（先删后插）", table, deleted, written)
        return deleted, written

    def delete_range(self, table: str, date_col: str, start: int, end: int,
                     key_col: Optional[str] = None,
                     keys: Optional[Iterable[str]] = None,
                     database: Optional[str] = None) -> int:
        full = f"`{database or self.cfg.ads_db()}`.`{table}`"
        sql = f"DELETE FROM {full} WHERE `{date_col}` BETWEEN %s AND %s"
        params: List[Any] = [int(start), int(end)]
        keys = list(keys or [])
        if key_col and keys:
            sql += f" AND `{key_col}` IN ({','.join(['%s'] * len(keys))})"
            params += keys
        conn = self.connect()
        with conn.cursor() as cur:
            n = cur.execute(sql, params)
        conn.commit()
        log.info("清理 %s：%d 行（%s ~ %s）", table, n, start, end)
        return n


def _to_rows(df: pd.DataFrame) -> List[tuple]:
    """DataFrame → executemany 用的元组列表。

    NaN/NaT 必须转成 None，否则 pymysql 会把 nan 当成字符串 'nan' 写进
    decimal 列，MySQL 严格模式下直接报错、非严格模式下静默写 0。
    """
    obj = df.astype(object).where(pd.notna(df), None)
    return [tuple(r) for r in obj.itertuples(index=False, name=None)]
