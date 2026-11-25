# -*- coding: utf-8 -*-
"""SQL 小工具：标识符转义、WHERE 拼装、值清洗。值一律走 %s 参数，不拼进 SQL。"""

from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

_IDENT = re.compile(r"^[A-Za-z0-9_$一-鿿]{1,64}$")

Where = Union[None, Dict[str, Any], str, Tuple[str, Sequence[Any]]]


def quote_ident(name: str) -> str:
    """表名 / 列名 → `name`；「库.表」→ `库`.`表`。只认字母数字下划线（和中文），别的直接拒绝。"""
    parts = str(name).strip().split(".")
    if not parts or len(parts) > 2 or not all(_IDENT.match(p) for p in parts):
        raise ValueError(f"不合法的表名 / 列名：{name!r}")
    return ".".join(f"`{p}`" for p in parts)


def build_where(where: Where, params: Optional[Sequence[Any]] = None) -> Tuple[str, List[Any]]:
    """WHERE 条件 → (不含 WHERE 关键字的 SQL, 参数)。

    · dict：{列: 值} 用 AND 连起来；值是 list / tuple / set → IN (...)（空列表 → 恒假 1=0）；
      值是 None → IS NULL
    · str：原样当条件，参数用 params（或 (sql, params) 元组）
    · None / 空 → ("", [])
    """
    if where is None or where == "" or where == {}:
        return "", []
    if isinstance(where, tuple):
        sql, ps = where
        return str(sql), list(ps or [])
    if isinstance(where, str):
        return where, list(params or [])
    if not isinstance(where, dict):
        raise TypeError("where 只能是 dict、SQL 字符串或 (SQL, 参数) 元组")
    parts, out = [], []
    for col, v in where.items():
        c = quote_ident(col)
        if v is None:
            parts.append(f"{c} IS NULL")
        elif isinstance(v, (list, tuple, set, frozenset)):
            vs = [clean_value(x) for x in v]
            if not vs:
                parts.append("1=0")
            else:
                parts.append(f"{c} IN ({','.join(['%s'] * len(vs))})")
                out += vs
        else:
            parts.append(f"{c} = %s")
            out.append(clean_value(v))
    return " AND ".join(parts), out


def clean_value(v: Any) -> Any:
    """写库前把值变成 pymysql 认的原生类型：NaN / NaT / pd.NA → None，numpy 标量 → Python 标量，
    pandas Timestamp → datetime。dict / list 不在这里处理（要存 JSON 就先自己 dumps）。"""
    if v is None:
        return None
    if isinstance(v, float):
        return None if math.isnan(v) else v
    t = type(v).__module__
    if t.startswith("numpy"):
        if hasattr(v, "item"):
            v = v.item()
            return None if isinstance(v, float) and math.isnan(v) else v
    if t.startswith("pandas"):
        name = type(v).__name__
        if name in ("NaTType", "NAType"):
            return None
        if name == "Timestamp":
            return v.to_pydatetime()
        if name == "Timedelta":
            return v.to_pytimedelta()
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time, _dt.timedelta, str, bytes, int, bool)):
        return v
    return v


def rows_and_columns(rows: Any, columns: Optional[Sequence[str]] = None) -> Tuple[List[str], List[tuple]]:
    """要写的行 → (列名, 元组列表)。认：DataFrame、[dict]、[tuple/list]（要给 columns）。"""
    if rows is None:
        return list(columns or []), []
    if hasattr(rows, "itertuples") and hasattr(rows, "columns"):          # DataFrame（不 import pandas）
        cols = [str(c) for c in (columns or rows.columns)]
        df = rows[cols]
        data = [tuple(clean_value(x) for x in r) for r in df.itertuples(index=False, name=None)]
        return cols, data
    rows = list(rows)
    if not rows:
        return list(columns or []), []
    if isinstance(rows[0], dict):
        cols = list(columns) if columns else _union_keys(rows)
        return cols, [tuple(clean_value(r.get(c)) for c in cols) for r in rows]
    if not columns:
        raise ValueError("传元组 / 列表时要给 columns")
    cols = list(columns)
    for r in rows:
        if len(r) != len(cols):
            raise ValueError(f"有一行 {len(r)} 个值，但 columns 有 {len(cols)} 个")
    return cols, [tuple(clean_value(x) for x in r) for r in rows]


def _union_keys(rows: Iterable[dict]) -> List[str]:
    out: List[str] = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                out.append(k)
    return out


def insert_sql(table: str, columns: Sequence[str], on_duplicate: Optional[str] = None,
               update_columns: Optional[Sequence[str]] = None) -> str:
    """INSERT 语句（一行的占位符，配 executemany 用）。

    on_duplicate：None = 撞唯一键就报错；"ignore" = INSERT IGNORE；"update" = ON DUPLICATE KEY UPDATE
    （update_columns 不给 = 全部列）。
    """
    if not columns:
        raise ValueError("没有要写的列")
    cols = ",".join(quote_ident(c) for c in columns)
    ph = ",".join(["%s"] * len(columns))
    head = "INSERT IGNORE INTO" if on_duplicate == "ignore" else "INSERT INTO"
    sql = f"{head} {quote_ident(table)} ({cols}) VALUES ({ph})"
    if on_duplicate == "update":
        upd = list(update_columns) if update_columns else list(columns)
        sql += " ON DUPLICATE KEY UPDATE " + ",".join(f"{quote_ident(c)}=VALUES({quote_ident(c)})" for c in upd)
    elif on_duplicate not in (None, "ignore"):
        raise ValueError("on_duplicate 只能是 None / 'ignore' / 'update'")
    return sql
