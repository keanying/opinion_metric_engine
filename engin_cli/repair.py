# -*- coding: utf-8 -*-
"""历史数据修正 —— 只重算并回写「得分」字段。

用途：得分口径从 v1（净值+置信度，值域 [0,10]）换成 v2
（加权占比，值域 [2.5,5]）之后，把历史数据里的分数按新口径重刷一遍。

**只动这些字段，别的一列不碰**：

  ads_trf_social_opinion_comment_core_di
      emotional_score, emotional_score_7d/30d/60d/90d/365d           （6 列）
  ads_trf_social_opinion_comment_dimension_score_di
      dimension_{1,2,3}_score{,_7d,_14d,_30d,_60d,_90d,_365d}        （21 列）

用的是 UPDATE 而不是整行 upsert：评论数、好评率、环比、词频这些都不受口径变更影响，
重刷整行既慢又会把 etl_time 冲掉，出问题时也分不清是口径改的还是重算错的。

**不变量：修正的结果 == 用新口径重跑一遍的结果。**
所以修正时同样要铺网格、同样要做整窗前移回补，动作与 builders 逐步对齐。
这条不变量破了的话，repair 和 run 会互相把对方的值改回去，来回打架
（`tests/test_repair.py` 里 `test_repair_after_run_changes_nothing_*` 盯着它）。

数据从哪里读（跟 backfill_mode 有关，这是个坑）
--------------------------------------------
backfill_mode = "shift"（默认）
    **两张表都必须回源表重算。**
    原因：整窗前移之后 core 表里存的 comment_count 已经**不是当天真实条数**了，
    而是「最近一个有数据的 1 日窗口的条数」（domain §1.8）。拿它再滚一次窗口，
    等于把回补叠加第二遍 —— 09-11 借了 09-10 的 1 条，再重算时 09-11 自己
    「有数据」了，窗口就不再前移，分母分子全错位。
    所以可修正范围受源表保留期限制，改不到源表已经清掉的日期。

backfill_mode = "off"
    core 表存的就是当天真实计数，可以**直接从 core 表自己的计数列重算**，
    不需要源表 —— 即使源表只留了半年，也能把一年前的 emotional_score_365d 修对。
    dimension 仍必须回源表（维度提及数根本没落表，表里只有分数）。

用法
----
    # 先看会改成什么样，不落库
    python -m engin_cli.cli repair --start-date 20260101 --end-date 20260901 --dry-run

    # 确认后执行
    python -m engin_cli.cli repair --start-date 20260101 --end-date 20260901

    # 只修 core（backfill_mode=off 时不需要源表，最快）
    python -m engin_cli.cli repair --tables core --start-date 20260101 --end-date 20260901
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from . import metric_calc_domain as D
from .db import MySQL
from .loader import TABLE_CORE, TABLE_DIMENSION
from .normalize import build_comment_facts, build_dimension_facts
from .settings import EtlSettings
from .source import fetch_comments
from .windows import dense_grid, rolling_windows

log = logging.getLogger(__name__)

# ---- 要修正的字段（严格按需求给的清单，多一个都不改）----
CORE_SCORE_FIELDS: List[str] = [f"emotional_score{D.CORE_SUFFIX[w]}"
                                for w in D.CORE_WINDOWS]
DIM_SCORE_FIELDS: List[str] = [f"dimension_{lvl}_score{D.DIM_SUFFIX[w]}"
                               for w in D.DIMENSION_WINDOWS for lvl in (1, 2, 3)]

CORE_KEYS = ["scenic_spot_code", "travel_date"]
DIM_KEYS = ["scenic_spot_code", "travel_date", "dimension_level1",
            "dimension_level2", "dimension_level3"]


@dataclass
class RepairResult:
    table: str
    scanned: int = 0            # 读到的历史行数
    changed: int = 0            # 分数确实变了的行数
    updated: int = 0            # 实际写回的行数（dry-run 时为 0）
    max_delta: float = 0.0      # 单个字段的最大变化幅度
    preview: pd.DataFrame = field(default_factory=pd.DataFrame)
    keys: List[str] = field(default_factory=list)     # 该表的定位键（渲染对比用）
    fields: List[str] = field(default_factory=list)   # 被修正的字段清单
    note: str = ""


# ══════════════════════════════════════════════════════════════════════
# core：shift 口径回源表重算；off 口径可直接用 core 表自身的计数列
# ══════════════════════════════════════════════════════════════════════
def read_core_counts(db: MySQL, start: str, end: str,
                     scenic_codes: Optional[Sequence[str]] = None,
                     warmup_days: int = 365) -> pd.DataFrame:
    """读 core 表的计数列（含窗口预热区间）。

    ⚠ 只在 backfill_mode="off" 时可用作重算输入：shift 口径下这些计数
    本身就是回补后的值，再滚一次窗口会把回补叠加两遍（见模块注释）。
    """
    warmup = (pd.to_datetime(start, format="%Y%m%d")
              - pd.Timedelta(days=max(int(warmup_days), 1) - 1)).strftime("%Y%m%d")
    cols = ",".join(f"`{c}`" for c in CORE_KEYS + D.CORE_COUNT_FIELDS)
    sql = (f"SELECT {cols} FROM `{db.cfg.ads_db()}`.`{TABLE_CORE}` "
           f"WHERE travel_date BETWEEN %s AND %s")
    params: List = [int(warmup), int(end)]
    if scenic_codes:
        sql += f" AND scenic_spot_code IN ({','.join(['%s'] * len(scenic_codes))})"
        params += list(scenic_codes)
    return db.query_df(sql, params)


def _lookback(settings: Optional[EtlSettings], windows) -> dict:
    """本次修正用的「整窗前移」上限，与跑批同一套（domain §1.8）。"""
    mode = getattr(settings, "backfill_mode", D.DEFAULT_BACKFILL_MODE) if settings \
        else D.BACKFILL_MODE_OFF
    if mode != D.BACKFILL_MODE_SHIFT:
        return {}
    table = dict(D.BACKFILL_LOOKBACK)
    table.update(getattr(settings, "backfill_lookback", None) or {})
    return {int(w): int(table[int(w)]) for w in windows if int(w) in table}


def _weights(settings: Optional[EtlSettings]):
    """修正用的得分权重，与跑批同一套（settings.score_weights，domain §1.1）。"""
    return getattr(settings, "score_weights", None) if settings else None


def recompute_core_scores(core_rows: pd.DataFrame,
                          output_dates: Optional[Sequence[str]] = None,
                          formula: str = D.DEFAULT_SCORE_FORMULA,
                          settings: Optional[EtlSettings] = None,
                          calendar: Optional[tuple] = None) -> pd.DataFrame:
    """**当天真实**的日计数 → 新口径的 6 个 emotional_score。

    入参：core_rows 至少含 scenic_spot_code / travel_date / comment_count /
          positive_count / neutral_count / negative_count。
          ⚠ 这里要的是**未经回补的原始日计数**。带 platform_code 列时按渠道粒度
          回补再汇总（与跑批一致，domain §1.9）；不带则退化成景区粒度，
          只在 backfill_mode="off" 下等价。喂回补后的值进来会叠加两遍；
          output_dates 只输出这些日期（更早的行只作为窗口历史参与累计）；
          settings 提供回补口径与上限，不传则不回补
    出参：CORE_KEYS + CORE_SCORE_FIELDS

    **动作与 builders/core.py 逐步一致**：铺网格 → 滚动(含整窗前移回补)。
    这样「修正历史」和「用新口径重跑一遍」结果相同，
    否则 repair 和 run 会互相把对方的值改回去，来回打架。

    注意：窗口累计必须把**更早的历史**也读进来，否则 365d 会算成「只有这几天」。
    调用方负责多读一段（见 repair_core 的 warmup）。
    """
    if core_rows.empty:
        return pd.DataFrame(columns=CORE_KEYS + CORE_SCORE_FIELDS)

    # 入参带 platform_code → 按渠道粒度回补再汇总，与 builders/core.py 完全一致
    # （domain §1.9：回补的原子粒度是渠道）。不带就退化成景区粒度 ——
    # 这只在 backfill_mode="off" 下等价，因为不回补时「先滚后加」与「先加后滚」结果相同。
    by_plat = "platform_code" in core_rows.columns
    keys = ["scenic_spot_code", "platform_code"] if by_plat else ["scenic_spot_code"]

    daily = core_rows[keys + ["travel_date"] + D.CORE_COUNT_FIELDS].copy()
    daily["travel_date"] = daily["travel_date"].astype(str)
    have = daily[CORE_KEYS].drop_duplicates()
    cal = calendar or (daily["travel_date"].min(), daily["travel_date"].max())

    grid = None
    if by_plat:
        # 渠道全集必须与跑批一致，少一个渠道就少一份计数
        codes = sorted(daily["platform_code"].dropna().unique())
        cfg = getattr(settings, "platform_codes", None) or D.PLATFORM_CODES
        codes = list(dict.fromkeys([str(c).strip().lower() for c in cfg
                                    if str(c).strip()] + list(codes)))
        grid = pd.MultiIndex.from_product(
            [daily["scenic_spot_code"].drop_duplicates(), codes],
            names=keys).to_frame(index=False)

    daily = dense_grid(daily, keys, "travel_date", D.CORE_COUNT_FIELDS,
                       keys=grid, start=cal[0], end=cal[1])
    win = rolling_windows(daily, keys, "travel_date",
                          D.CORE_COUNT_FIELDS, windows=D.CORE_WINDOWS,
                          with_prev=False, output_dates=output_dates, calendar=cal,
                          shift_lookback=_lookback(settings, D.CORE_WINDOWS),
                          presence_col="comment_count")
    if by_plat:
        num = [c for c in win.columns
               if c not in keys + ["travel_date"] and not str(c).startswith("shift_")]
        win = win.groupby(CORE_KEYS, as_index=False)[num].sum()
    # 只保留库里真有那一行的记录：修正动作不新增行，只改分数
    df = win.merge(have, on=CORE_KEYS, how="inner")
    if df.empty:
        return pd.DataFrame(columns=CORE_KEYS + CORE_SCORE_FIELDS)

    out = df[CORE_KEYS].copy()
    for w in D.CORE_WINDOWS:
        out[f"emotional_score{D.CORE_SUFFIX[w]}"] = D.sentiment_score_from_counts(
            df[f"positive_count_{w}d"], df[f"neutral_count_{w}d"],
            df[f"negative_count_{w}d"], df[f"comment_count_{w}d"], formula=formula,
            weights=_weights(settings))
    return out


# ══════════════════════════════════════════════════════════════════════
# dimension：回源表重算维度提及，再重算 21 个分数
# ══════════════════════════════════════════════════════════════════════
def recompute_dimension_scores(dim_facts: pd.DataFrame,
                               output_dates: Sequence[str],
                               formula: str = D.DEFAULT_SCORE_FORMULA,
                               settings: Optional[EtlSettings] = None,
                               calendar: Optional[tuple] = None) -> pd.DataFrame:
    """维度明细 → 新口径的 21 个 dimension_N_score。

    入参：dim_facts（normalize.build_dimension_facts 的产物，需覆盖窗口所需历史）；
          output_dates 要修正的日期
    出参：DIM_KEYS + DIM_SCORE_FIELDS

    settings 传入时按与跑批相同的口径做整窗前移回补，保证「修正历史」与
    「用新口径重跑一遍」结果一致；不传则不回补。
    """
    if dim_facts.empty:
        return pd.DataFrame(columns=DIM_KEYS + DIM_SCORE_FIELDS)

    cal = calendar or (dim_facts["travel_date"].min(), dim_facts["travel_date"].max())
    frames = {}
    for lvl, keys in D.DIM_LEVEL_KEYS.items():
        daily = D.daily_dimension_facts(dim_facts, keys)
        daily = dense_grid(daily, keys, "travel_date", D.DIM_VALUE_FIELDS,
                           start=cal[0], end=cal[1])
        win = rolling_windows(daily, keys, "travel_date", D.DIM_VALUE_FIELDS,
                              windows=D.DIMENSION_WINDOWS, with_prev=False,
                              output_dates=output_dates, calendar=cal,
                              shift_lookback=_lookback(settings, D.DIMENSION_WINDOWS),
                              presence_col="mention_cnt")
        frames[lvl] = win.rename(columns={c: f"L{lvl}_{c}" for c in win.columns
                                          if c not in keys + ["travel_date"]})

    df = (frames[3]
          .merge(frames[2], on=D.DIM_KEYS_L2 + ["travel_date"], how="left")
          .merge(frames[1], on=D.DIM_KEYS_L1 + ["travel_date"], how="left"))
    if df.empty:
        return pd.DataFrame(columns=DIM_KEYS + DIM_SCORE_FIELDS)

    out = df[DIM_KEYS].copy()
    for w in D.DIMENSION_WINDOWS:
        for lvl in (1, 2, 3):
            out[f"dimension_{lvl}_score{D.DIM_SUFFIX[w]}"] = D.sentiment_score_from_counts(
                df[f"L{lvl}_mention_pos_{w}d"], df[f"L{lvl}_mention_neu_{w}d"],
                df[f"L{lvl}_mention_neg_{w}d"], df[f"L{lvl}_mention_cnt_{w}d"],
                formula=formula, weights=_weights(settings))
    return out


# ══════════════════════════════════════════════════════════════════════
# 对比 + 回写
# ══════════════════════════════════════════════════════════════════════
def diff_scores(old: pd.DataFrame, new: pd.DataFrame, keys: List[str],
                fields: List[str]) -> pd.DataFrame:
    """新旧分数对比，返回带 <字段>_old / <字段>_new 的宽表（只留有变化的行）。"""
    o = old[keys + fields].copy()
    o["travel_date"] = o["travel_date"].astype(str)
    n = new.copy()
    n["travel_date"] = n["travel_date"].astype(str)
    m = o.merge(n, on=keys, how="inner", suffixes=("_old", "_new"))
    if m.empty:
        return m
    delta = np.zeros(len(m), dtype=bool)
    for f in fields:
        delta |= (m[f"{f}_old"].astype("float64")
                  - m[f"{f}_new"].astype("float64")).abs() > 1e-6
    return m[delta]


def _max_delta(d: pd.DataFrame, fields: List[str]) -> float:
    """新旧分数的最大变化幅度，用来一眼看出这次口径变更把分数挪了多远。"""
    if d.empty:
        return 0.0
    return float(max((d[f"{f}_old"].astype("float64")
                      - d[f"{f}_new"].astype("float64")).abs().max()
                     for f in fields))


def _update_rows(db: MySQL, table: str, rows: pd.DataFrame, keys: List[str],
                 fields: List[str], batch_size: int = 1000) -> int:
    """按主键 UPDATE 指定字段。整行不动，只 SET 这几列。"""
    if rows.empty:
        return 0
    set_sql = ",".join(f"`{f}`=%s" for f in fields)
    where_sql = " AND ".join(f"`{k}`=%s" for k in keys)
    sql = f"UPDATE `{db.cfg.ads_db()}`.`{table}` SET {set_sql} WHERE {where_sql}"

    payload = rows[fields + keys].astype(object).where(pd.notna(rows[fields + keys]), None)
    data = [tuple(r) for r in payload.itertuples(index=False, name=None)]
    conn = db.connect()
    n = 0
    with conn.cursor() as cur:
        for i in range(0, len(data), batch_size):
            cur.executemany(sql, data[i:i + batch_size])
            n += len(data[i:i + batch_size])
    conn.commit()
    log.info("修正 %s：UPDATE %d 行 × %d 列", table, n, len(fields))
    return n


def repair_core(db: MySQL, start: str, end: str,
                scenic_codes: Optional[Sequence[str]] = None,
                formula: str = D.DEFAULT_SCORE_FORMULA,
                dry_run: bool = True, batch_size: int = 1000,
                settings: Optional[EtlSettings] = None,
                comments_df: Optional[pd.DataFrame] = None) -> RepairResult:
    """修正 core 表的 6 个 emotional_score 字段。

    计数从哪来取决于 backfill_mode（见模块注释）：
      shift → 回源表拿**当天真实计数**（core 表里存的已经是回补后的值，
              拿它再滚一次窗口会把回补叠加两遍）
      off   → 直接用 core 表自己的计数列，不需要源表
    """
    res = RepairResult(table=TABLE_CORE, keys=CORE_KEYS, fields=CORE_SCORE_FIELDS)
    # 预热长度必须与跑批的 lookback_days 一致，否则「修正结果 == 重跑结果」这条
    # 不变量会在长窗口上破掉：跑批只看了 180 天，修正却看了 365 天，
    # emotional_score_365d 自然对不上，然后两边来回改。
    # 想让 365d 真的是一年，把 lookback_days 调大，两边一起生效。
    lookback = settings.lookback_days if settings is not None else max(D.CORE_WINDOWS)
    warmup = (pd.to_datetime(start, format="%Y%m%d")
              - pd.Timedelta(days=lookback - 1)).strftime("%Y%m%d")
    shift = bool(_lookback(settings, D.CORE_WINDOWS))
    res.note = (f"窗口预热从 {warmup} 开始读（{lookback} 天，与跑批的 lookback_days 一致；"
                f"不修改预热区间的行）；计数来源="
                + ("源表（整窗前移口径下 core 表的计数已是回补值，不能二次滚动）"
                   if shift else "core 表自身计数列"))

    # 旧分数：只读目标区间，这是要被对比和回写的行
    cols = ",".join(f"`{c}`" for c in CORE_KEYS + D.CORE_COUNT_FIELDS + CORE_SCORE_FIELDS)
    sql = (f"SELECT {cols} FROM `{db.cfg.ads_db()}`.`{TABLE_CORE}` "
           f"WHERE travel_date BETWEEN %s AND %s")
    params: List = [int(warmup), int(end)]
    if scenic_codes:
        sql += f" AND scenic_spot_code IN ({','.join(['%s'] * len(scenic_codes))})"
        params += list(scenic_codes)
    rows = db.query_df(sql, params)
    if rows.empty:
        res.note = "core 表在该区间没有数据"
        return res

    target = [d.strftime("%Y%m%d") for d in
              pd.date_range(pd.to_datetime(start, format="%Y%m%d"),
                            pd.to_datetime(end, format="%Y%m%d"), freq="D")]

    if shift:
        if comments_df is None:
            comments_df = fetch_comments(db, warmup, end, scenic_codes)
        if comments_df is None or comments_df.empty:
            res.note = "源表在该区间没有评论数据，core 分数无法按整窗前移口径修正"
            return res
        # 渠道粒度：回补在渠道上做，再按景区汇总（domain §1.9），与跑批一致
        daily = D.daily_core_facts_by_platform(build_comment_facts(comments_df))
        daily = daily[["scenic_spot_code", "platform_code", "travel_date"]
                      + D.CORE_COUNT_FIELDS]
        daily["travel_date"] = daily["travel_date"].astype(str)
    else:
        daily = rows

    new = recompute_core_scores(daily, output_dates=target, formula=formula,
                                settings=settings, calendar=(warmup, end))
    old = rows[rows["travel_date"].astype(str).isin(set(target))]
    res.scanned = len(old)

    d = diff_scores(old, new, CORE_KEYS, CORE_SCORE_FIELDS)
    res.changed = len(d)
    if not d.empty:
        res.max_delta = _max_delta(d, CORE_SCORE_FIELDS)
        res.preview = d.head(20)
    if not dry_run and not d.empty:
        upd = d[CORE_KEYS].copy()
        for f in CORE_SCORE_FIELDS:
            upd[f] = d[f"{f}_new"]
        res.updated = _update_rows(db, TABLE_CORE, upd, CORE_KEYS,
                                   CORE_SCORE_FIELDS, batch_size)
    return res


def repair_dimension(db: MySQL, start: str, end: str,
                     scenic_codes: Optional[Sequence[str]] = None,
                     formula: str = D.DEFAULT_SCORE_FORMULA,
                     dry_run: bool = True, batch_size: int = 1000,
                     comments_df: Optional[pd.DataFrame] = None,
                     lookback_days: int = 365,
                     unknown_dimension_policy: str = "keep",
                     settings: Optional[EtlSettings] = None) -> RepairResult:
    """修正 dimension_score 表的 21 个分数字段（需回源表重算维度提及）。"""
    res = RepairResult(table=TABLE_DIMENSION, keys=DIM_KEYS, fields=DIM_SCORE_FIELDS)
    warmup = (pd.to_datetime(start, format="%Y%m%d")
              - pd.Timedelta(days=lookback_days - 1)).strftime("%Y%m%d")
    res.note = (f"回源表重算，窗口预热从 {warmup} 开始；"
                f"源表已清理的日期无法修正")

    if comments_df is None:
        comments_df = fetch_comments(db, warmup, end, scenic_codes)
    if comments_df is None or comments_df.empty:
        res.note = "源表在该区间没有评论数据，维度分数无法修正"
        return res

    cf = build_comment_facts(comments_df)
    dim_facts = build_dimension_facts(cf, unknown_dimension_policy)
    target = [d.strftime("%Y%m%d") for d in
              pd.date_range(pd.to_datetime(start, format="%Y%m%d"),
                            pd.to_datetime(end, format="%Y%m%d"), freq="D")]
    new = recompute_dimension_scores(dim_facts, target, formula=formula,
                                     settings=settings, calendar=(warmup, end))
    if new.empty:
        res.note = "重算结果为空，检查源表维度标签"
        return res

    cols = ",".join(f"`{c}`" for c in DIM_KEYS + DIM_SCORE_FIELDS)
    sql = (f"SELECT {cols} FROM `{db.cfg.ads_db()}`.`{TABLE_DIMENSION}` "
           f"WHERE travel_date BETWEEN %s AND %s")
    params: List = [int(start), int(end)]
    if scenic_codes:
        sql += f" AND scenic_spot_code IN ({','.join(['%s'] * len(scenic_codes))})"
        params += list(scenic_codes)
    old = db.query_df(sql, params)
    res.scanned = len(old)
    if old.empty:
        res.note = "维度表在该区间没有数据"
        return res

    d = diff_scores(old, new, DIM_KEYS, DIM_SCORE_FIELDS)
    res.changed = len(d)
    if not d.empty:
        res.max_delta = _max_delta(d, DIM_SCORE_FIELDS)
        res.preview = d.head(20)
    if not dry_run and not d.empty:
        upd = d[DIM_KEYS].copy()
        for f in DIM_SCORE_FIELDS:
            upd[f] = d[f"{f}_new"]
        res.updated = _update_rows(db, TABLE_DIMENSION, upd, DIM_KEYS,
                                   DIM_SCORE_FIELDS, batch_size)
    return res
