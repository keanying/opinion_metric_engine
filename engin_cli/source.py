# -*- coding: utf-8 -*-
"""源数据抽取。

两张源表：
  src_opinion_social_work_comment_di   评论（指标主数据源）
  src_opinion_social_work_di           作品（只为 5.2 下钻取 work_url/标题/作者）

需求六.2：评论**按 publish_time 拉取**，不是 crawl_time / create_time。
publish_time 才是「这条评论属于哪一天」的口径，用采集时间会让补采的历史评论
全都堆到补采当天，趋势图直接失真。

大区间读取用服务端游标（SSCursor）逐批吐，半年千万级评论也不会把内存打满。
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Sequence

import pandas as pd

from .db import MySQL

log = logging.getLogger(__name__)

TABLE_COMMENT = "src_opinion_social_work_comment_di"
TABLE_WORK = "src_opinion_social_work_di"

COMMENT_COLS = [
    "scenic_id", "scenic_name", "channel", "work_id",
    "comment_level", "comment_parent_id", "comment_id", "root_comment_id",
    "commenter_name", "content", "likes",
    "sentiment_label", "sentiment_score",
    "dimension_tags", "entity_tags", "keyword_tags",
    "publish_time",
]

WORK_COLS = ["scenic_id", "scenic_name", "channel", "work_id", "work_url",
             "title", "author_name", "publish_time"]


def _date_bounds(start: str, end: str):
    """yyyyMMdd → [start 00:00:00, end+1 00:00:00)。

    用半开区间而不是 BETWEEN '... 23:59:59'，否则 23:59:59.5 的评论会漏。
    """
    s = pd.to_datetime(str(start), format="%Y%m%d")
    e = pd.to_datetime(str(end), format="%Y%m%d") + pd.Timedelta(days=1)
    return s.strftime("%Y-%m-%d %H:%M:%S"), e.strftime("%Y-%m-%d %H:%M:%S")


def fetch_comments(db: MySQL, start: str, end: str,
                   scenic_codes: Optional[Sequence[str]] = None,
                   table: str = TABLE_COMMENT) -> pd.DataFrame:
    lo, hi = _date_bounds(start, end)
    cols = ",".join(f"`{c}`" for c in COMMENT_COLS)
    sql = (f"SELECT {cols} FROM `{db.cfg.src_db()}`.`{table}`\n"
           f" WHERE publish_time >= %s AND publish_time < %s")
    params: List = [lo, hi]
    if scenic_codes:
        sql += f" AND scenic_id IN ({','.join(['%s'] * len(scenic_codes))})"
        params += list(scenic_codes)
    df = db.query_df(sql, params)
    log.info("评论源数据 %s ~ %s：%d 行", start, end, len(df))
    return df


def fetch_works(db: MySQL, start: str, end: str,
                scenic_codes: Optional[Sequence[str]] = None,
                work_ids: Optional[Sequence[str]] = None,
                table: str = TABLE_WORK) -> pd.DataFrame:
    """拉作品。

    作品的发布时间通常**早于**评论，按评论区间去卡 work.publish_time 会漏掉
    大量老作品的新评论 —— 所以这里默认不按时间过滤，只按 (景区, work_id) 精确捞，
    work_id 由评论侧提供。work_id 集合过大时分批 IN。
    """
    cols = ",".join(f"`{c}`" for c in WORK_COLS)
    base = f"SELECT {cols} FROM `{db.cfg.src_db()}`.`{table}` WHERE 1=1"
    params: List = []
    if scenic_codes:
        base += f" AND scenic_id IN ({','.join(['%s'] * len(scenic_codes))})"
        params += list(scenic_codes)

    if not work_ids:
        return db.query_df(base, params)

    ids = sorted({str(w) for w in work_ids if str(w or "").strip()})
    frames = []
    step = 1000
    for i in range(0, len(ids), step):
        chunk = ids[i:i + step]
        sql = base + f" AND work_id IN ({','.join(['%s'] * len(chunk))})"
        frames.append(db.query_df(sql, params + chunk))
    if not frames:
        return pd.DataFrame(columns=WORK_COLS)
    return pd.concat(frames, ignore_index=True)


def list_scenic_codes(db: MySQL, start: str, end: str,
                      table: str = TABLE_COMMENT) -> List[str]:
    lo, hi = _date_bounds(start, end)
    df = db.query_df(
        f"SELECT DISTINCT scenic_id FROM `{db.cfg.src_db()}`.`{table}` "
        f"WHERE publish_time >= %s AND publish_time < %s", [lo, hi])
    return [str(x) for x in df["scenic_id"].tolist()] if not df.empty else []


# ============================================================
# CSV 数据源：本地跑通/回归测试用，与 MySQL 源同构
# ============================================================
_EXCEL_SAFE = re.compile(r"^\'?=?\"?(.*?)\"?\'?$", re.S)


def _unquote_excel_safe(v):
    """兼容「Excel 安全导出」把每个值包成 \'="..."\' 的格式。

    很多客户端（Navicat/DBeaver 的 CSV 导出选项）会这么写，防止长数字被 Excel
    转成科学计数法。不剥掉的话 publish_time 全部解析失败，整批数据被当成脏数据丢弃。
    """
    if not isinstance(v, str):
        return v
    s = v.strip()
    if len(s) >= 2 and s[0] == "\'" and s[-1] == "\'":
        s = s[1:-1]
    if s.startswith('="') and s.endswith('"'):
        s = s[2:-1]
    return s


def _read_src_csv(path: str) -> pd.DataFrame:
    # utf-8-sig：导出文件常带 BOM，不处理的话第一列列名会变成 \ufeffid
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""],
                     encoding="utf-8-sig")
    df.columns = [str(c).strip().lstrip("\ufeff") for c in df.columns]
    sample = df.head(50).astype(str)
    quoted = sample.map(lambda v: isinstance(v, str) and len(v) >= 2
                        and v.startswith(("'", '="')) and v.endswith(("'", '"')))
    if bool(quoted.to_numpy().any()):
        log.info("检测到 Excel 安全导出格式，正在剥离 \'=\"...\" 包裹")
        for c in df.columns:
            df[c] = df[c].map(_unquote_excel_safe)
    return df


def fetch_comments_csv(path: str) -> pd.DataFrame:
    df = _read_src_csv(path)
    for c in ("likes", "sentiment_score"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def fetch_works_csv(path: str) -> pd.DataFrame:
    return _read_src_csv(path)
