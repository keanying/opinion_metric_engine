# -*- coding: utf-8 -*-
"""多窗口滚动聚合。

四张表一共有近百个 `_7d / _30d / _365d` 字段，如果每张表各写一套循环，
口径迟早会漂。这里提供**唯一一份**滚动实现（含「先补明细再滚动」的缺数回补，
domain §1.8），各 builder 全部复用。

核心约定
--------
1. 窗口 N 的含义是 **[锚点日 - N + 1, 锚点日]** 的闭区间，含锚点当天。
   即 `_7d` = 近 7 天（含今天），不是「今天之前的 7 天」。
2. 上一周期是**再往前推 N 天**：[锚点 - 2N + 1, 锚点 - N]。
   环比 =（本期 - 上期）/ 上期，与规则文档一致。
3. 滚动之前必须把日期补成**连续日历**并填 0。
   直接对稀疏行做 `rolling(7)` 得到的是「最近 7 条记录」而不是「最近 7 天」，
   在有缺采日的真实数据上会算出偏大的窗口值 —— 这是这类 ETL 最常见的错。
"""

from __future__ import annotations

from typing import Iterable, List, Sequence

import numpy as np
import pandas as pd

_KID = "_kid"
_DT = "_dt"


def to_dt(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series.astype(str), format="%Y%m%d")


def to_ds(series: pd.Series) -> pd.Series:
    return series.dt.strftime("%Y%m%d")


def rolling_windows(
    daily: pd.DataFrame,
    key_cols: Sequence[str],
    date_col: str,
    value_cols: Sequence[str],
    windows: Iterable[int],
    with_prev: bool = True,
    output_dates: Sequence[str] | None = None,
    calendar: tuple[str, str] | None = None,
    fill_lookback: dict | None = None,
    fill_by: Sequence[str] | None = None,
    presence: pd.DataFrame | None = None,
    fill_from: str | None = None,
    presence_col: str | None = None,
) -> pd.DataFrame:
    """把日粒度事实滚成多窗口累计值（可先把每日明细补齐，再滚）。

    参数
    ----
    daily : 日粒度事实，每个 (key, date) 一行
    key_cols : 分组键，如 ["scenic_spot_code"] 或 ["scenic_spot_code", "platform_code"]
    date_col : 日期列，字符串 yyyyMMdd
    value_cols : 要滚动求和的度量列
    windows : 窗口天数，如 (1, 7, 30, 60, 90, 365)
    with_prev : 是否同时产出上一周期累计（列名 `<col>_prev_<N>d`），用于环比
    output_dates : 只保留这些日期的结果；None 表示全部
    calendar : **权威**日历范围 (start, end)。不传就取数据自己的最小/最大日期 ——
               但那样「最后几天一条数据都没有」时，这几天根本不会出现在结果里。
               需要为空白日期也产出行时（见 domain §1.7 行存在规则），必须显式传。
               传了就以它为准：区间外的数据会被丢弃，**不会**把面板撑大 ——
               源表里混进一条 2013 年的评论就能让日历从 400 天变成 4700 天，
               词表一大，面板直接爆掉。
    fill_lookback : 明细补齐（domain §1.8）。{窗口天数: 最多往前找几天}。
               窗口 N 先用「最多往前找 L_N 天」补齐每日明细，再逐日相加；
               上一周期同样来自这份补齐后的明细。传 None / {} = 不补。
    fill_by : 补齐的判断粒度（domain §1.9：渠道），必须是 key_cols 的子集，
               如 ["scenic_spot_code", "platform_code"]。开启补齐时必填。
    presence : 「这个渠道当天有没有评论」：fill_by + [date_col] + present_cnt
               （domain.daily_channel_presence 的产物）。不传就用 presence_col 在
               fill_by 粒度上的合计判断（core / platform 本身就是评论计数，两者等价）。
    fill_from : 补齐时可以往前取明细的最早日期（早于 calendar 起点的那一段只当「来源」，
               不进窗口合计）。不传 = calendar 起点。
    presence_col : 行存在规则看的计数列。开启补齐时额外产出 `raw_<presence_col>_<N>d`
               （**补齐之前**的真实窗口值，见 domain.has_window_data）。

    返回
    ----
    key_cols + [date_col] + `<col>_<N>d` (+ `<col>_prev_<N>d`)
    开启补齐且给了 presence_col 时额外返回 `raw_<presence_col>_<N>d`。
    """
    key_cols = list(key_cols)
    value_cols = list(value_cols)
    windows = sorted({int(w) for w in windows})
    lookback = {w: int((fill_lookback or {}).get(w, 0)) for w in windows}
    filling = any(v > 0 for v in lookback.values())
    if filling:
        fill_by = list(fill_by or [])
        if not fill_by or not set(fill_by) <= set(key_cols):
            raise ValueError("开启明细补齐时 fill_by 必须是 key_cols 的非空子集")

    def _empty() -> pd.DataFrame:
        cols = key_cols + [date_col]
        for w in windows:
            for c in value_cols:
                cols.append(f"{c}_{w}d")
                if with_prev:
                    cols.append(f"{c}_prev_{w}d")
            if filling and presence_col:
                cols.append(f"raw_{presence_col}_{w}d")
        return pd.DataFrame(columns=cols)

    if daily.empty:
        return _empty()

    df = daily[key_cols + [date_col] + value_cols].copy()
    df[_DT] = to_dt(df[date_col])
    if calendar:
        lo = pd.to_datetime(str(calendar[0]), format="%Y%m%d")
        hi = pd.to_datetime(str(calendar[1]), format="%Y%m%d")
    else:
        lo, hi = df[_DT].min(), df[_DT].max()
    # 补齐要用到日历起点之前的明细（只当来源）：面板从 fill_from 开始铺
    src_lo = lo
    if filling and fill_from:
        src_lo = min(lo, pd.to_datetime(str(fill_from), format="%Y%m%d"))
    # 区间外的行直接丢掉：日历由 calendar 说了算，不能被离群日期撑大
    df = df[(df[_DT] >= src_lo) & (df[_DT] <= hi)]
    if df.empty:
        return _empty()

    keys = df[key_cols].drop_duplicates().reset_index(drop=True)
    keys[_KID] = np.arange(len(keys), dtype="int64")
    df = df.merge(keys, on=key_cols, how="left")

    for c in value_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0).astype("float64")

    wide = df.pivot_table(index=_DT, columns=_KID, values=value_cols,
                          aggfunc="sum", fill_value=0.0)
    # 单个度量列时 pivot_table 会丢掉最外层 → 统一补回 MultiIndex
    if not isinstance(wide.columns, pd.MultiIndex):
        wide.columns = pd.MultiIndex.from_product([[value_cols[0]], wide.columns],
                                                  names=[None, _KID])

    # 补齐连续日历：缺采日必须以 0 参与窗口，否则 rolling 会跨过它多取历史
    full = pd.date_range(src_lo, hi, freq="D")
    wide = wide.reindex(full, fill_value=0.0).sort_index()
    in_cal = np.asarray(full >= lo)
    cal_idx = full[in_cal]

    # 只要输出这几天，就在**展开成长表之前**先把行裁掉。
    # 否则「11000 个词 × 4700 天 × 6 个窗口」会先物化成几千万行再丢掉 99.99%。
    keep = None
    if output_dates is not None:
        keep = cal_idx.isin(pd.to_datetime(sorted(set(output_dates)), format="%Y%m%d"))

    base = wide[in_cal]
    src_by_lb = {}
    if filling:
        src_by_lb = _fill_sources(wide, keys, fill_by, date_col, presence, presence_col,
                                  sorted({v for v in lookback.values() if v > 0}), in_cal)

    parts: List[pd.DataFrame] = []
    filled_cache = {}
    for w in windows:
        lb = lookback[w]
        if lb > 0:
            if lb not in filled_cache:
                filled_cache[lb] = _gather_filled(wide, src_by_lb[lb], in_cal)
            data = filled_cache[lb]
        else:
            data = base
        roll = data.rolling(w, min_periods=1).sum()
        prev = roll.shift(w).fillna(0.0) if with_prev else None
        if filling and presence_col:
            raw = base[[presence_col]].rolling(w, min_periods=1).sum()
            if keep is not None:
                raw = raw[keep]
            parts.append(_to_long(raw, [presence_col], suffix=f"_{w}d",
                                  prefix="raw_"))
        if keep is not None:
            roll = roll[keep]
            if prev is not None:
                prev = prev[keep]
        parts.append(_to_long(roll, value_cols, suffix=f"_{w}d"))
        if prev is not None:
            parts.append(_to_long(prev, value_cols, suffix=f"_prev_{w}d"))

    out = pd.concat(parts, axis=1).reset_index()
    out = out.merge(keys, on=_KID, how="left")
    out[date_col] = out[_DT].dt.strftime("%Y%m%d")
    out = out.drop(columns=[_DT, _KID])

    if output_dates is not None:
        out = out[out[date_col].isin(set(output_dates))]

    ordered = key_cols + [date_col] + [c for c in out.columns
                                       if c not in key_cols and c != date_col]
    return out[ordered].reset_index(drop=True)


def fill_source_index(present: np.ndarray, lookback: int) -> np.ndarray:
    """明细补齐的「来源日」（domain §1.8）。

    入参：present (天数 × 渠道) 布尔矩阵，True = 这个渠道当天有评论；lookback 最多往前找几天
    出参：同形状的整数矩阵：当天有数据 → 自己的行号；没有 → 往前 lookback 天内
          最近一个有数据的行号；找不到 → -1（这一天就是空的）
    """
    n = present.shape[0]
    rows = np.arange(n)[:, None]
    last = np.maximum.accumulate(np.where(present, rows, -1), axis=0)
    ok = (last >= 0) & (rows - last <= int(lookback))
    return np.where(ok, last, -1)


def _fill_sources(wide, keys, fill_by, date_col, presence, presence_col, lookbacks, in_cal):
    """每个上限 L → (日历内天数 × 实体) 的来源行号矩阵（-1 = 补不到）。"""
    groups = keys[fill_by].drop_duplicates().reset_index(drop=True)
    groups["_gid"] = np.arange(len(groups), dtype="int64")
    kid_gid = keys.merge(groups, on=fill_by, how="left").sort_values(_KID)["_gid"].to_numpy()
    full = wide.index
    pres = np.zeros((len(full), len(groups)), dtype=bool)

    # 「当天有评论」：优先用传进来的评论口径；再并上数据自己的计数，防止两边口径不齐时丢数
    if presence is not None and not presence.empty:
        p = presence[fill_by + [date_col]].copy()
        p["_n"] = pd.to_numeric(presence.get("present_cnt", 1), errors="coerce").fillna(0)
        p = p[p["_n"] > 0].merge(groups, on=fill_by, how="inner")
        if not p.empty:
            pos = full.get_indexer(to_dt(p[date_col]))
            ok = pos >= 0
            pres[pos[ok], p["_gid"].to_numpy()[ok]] = True
    col = presence_col if presence_col in wide.columns.get_level_values(0) \
        else wide.columns.get_level_values(0)[0]
    own = wide[col].to_numpy() > 0                                  # (天数 × 实体)
    if own.size:
        hit = np.zeros_like(pres)
        r, k = np.nonzero(own)
        kids = wide[col].columns.to_numpy()
        hit[r, kid_gid[kids[k]]] = True
        pres |= hit

    out = {}
    for lb in lookbacks:
        src = fill_source_index(pres, lb)[in_cal]                    # (日历天数 × 渠道)
        out[lb] = src[:, kid_gid]                                    # (日历天数 × 实体)
    return out


def _gather_filled(wide, src_kid, in_cal):
    """按来源行号把每个实体的整列明细搬过来：补齐后的日历内面板。"""
    kids = wide.columns.get_level_values(_KID).to_numpy()
    idx = src_kid[:, kids]                                           # (日历天数 × 列)
    arr = wide.to_numpy()
    out = np.take_along_axis(arr, np.maximum(idx, 0), axis=0)
    out[idx < 0] = 0.0
    return pd.DataFrame(out, index=wide.index[in_cal], columns=wide.columns)


def fill_source_dates(presence: pd.DataFrame, by: Sequence[str], date_col: str,
                      target_dates: Sequence[str], lookback: int) -> pd.DataFrame:
    """事实明细层面的补齐映射（下钻表 / 跑批报告用，口径同 rolling_windows 的补齐）。

    入参：presence 有评论的 (by…, date_col) 行（domain.daily_channel_presence）；
          target_dates 要看的日期；lookback 最多往前找几天
    出参：by + [date_col, "src_date"]，只列「当天没评论、往前找到了」的组合；
          另带 found 列：False = 往前 lookback 天内也没有（这一天就是空的）
    """
    by = list(by)
    cols = by + [date_col, "src_date", "found"]
    if presence.empty or not target_dates or int(lookback) <= 0:
        return pd.DataFrame(columns=cols)
    p = presence[by + [date_col]].drop_duplicates().copy()
    p[date_col] = p[date_col].astype(str)
    have = set(map(tuple, p[by + [date_col]].to_numpy().tolist()))
    groups = p[by].drop_duplicates()
    t = groups.merge(pd.DataFrame({date_col: sorted(set(map(str, target_dates)))}),
                     how="cross")
    t = t[[tuple(r) not in have for r in t[by + [date_col]].to_numpy().tolist()]]
    if t.empty:
        return pd.DataFrame(columns=cols)
    left = t.assign(_dt=to_dt(t[date_col])).sort_values("_dt")
    right = p.assign(_dt=to_dt(p[date_col]), src_date=p[date_col])[by + ["_dt", "src_date"]]
    right = right.sort_values("_dt")
    m = pd.merge_asof(left, right, on="_dt", by=by, direction="backward",
                      allow_exact_matches=False,
                      tolerance=pd.Timedelta(days=int(lookback)))
    m["found"] = m["src_date"].notna()
    return m[cols].reset_index(drop=True)


def _to_long(wide: pd.DataFrame, value_cols: Sequence[str], suffix: str,
             prefix: str = "") -> pd.DataFrame:
    """(date × [metric, kid]) 宽表 → (date, kid) 索引的长表，列名加窗口后缀。"""
    long = wide.stack(level=_KID)
    long.index = long.index.set_names([_DT, _KID])
    long = long[list(value_cols)]
    long.columns = [f"{prefix}{c}{suffix}" for c in value_cols]
    return long


def dense_grid(
    daily: pd.DataFrame,
    key_cols: Sequence[str],
    date_col: str,
    value_cols: Sequence[str],
    keys: pd.DataFrame | None = None,
    start: str | None = None,
    end: str | None = None,
    carry_cols: Sequence[str] = (),
) -> pd.DataFrame:
    """把日粒度事实补成 (实体 × 连续日历) 的完整网格，没有数据的格子填 0。

    为什么需要它
    ------------
    源表里「某平台当天一条评论都没有」表现为**这一行根本不存在**。
    如果直接拿这种稀疏表往下走：

      · 缺数回补拿不到这一天，算不出「往前顺延几天」；
      · 最终产出里这个平台当天没有行，可看板要展示的是它的 30/60/90 日指标 ——
        窗口里明明有值，行却没了。

    补成网格之后，「当天没有数据」变成一条 0 值记录，回补和滚动都能正常处理，
    要不要把它写进 ADS 由 builder 的行存在规则决定（见 domain 的 §1.7）。

    参数
    ----
    keys       实体全集。不传就用 daily 里出现过的实体；
               传了则以它为准（平台表用配置的渠道清单，实现「配置了但从没数据 → 全 0」）
    start/end  日历范围，默认取 daily 的最小/最大日期
    carry_cols 随实体走的描述列（如 platform_name），按实体取第一个非空值填满

    返回
    ----
    key_cols + [date_col] + value_cols(+carry_cols)，行数 = 实体数 × 天数
    """
    key_cols, value_cols = list(key_cols), list(value_cols)
    carry_cols = [c for c in carry_cols if c in daily.columns]

    if keys is None:
        keys = daily[key_cols].drop_duplicates() if not daily.empty \
            else pd.DataFrame(columns=key_cols)
    keys = keys[key_cols].drop_duplicates().reset_index(drop=True)
    if keys.empty:
        return daily.copy()

    if start is None or end is None:
        if daily.empty:
            return daily.copy()
        lo = daily[date_col].astype(str).min() if start is None else start
        hi = daily[date_col].astype(str).max() if end is None else end
    else:
        lo, hi = start, end
    cal = pd.DataFrame({date_col: [d.strftime("%Y%m%d") for d in pd.date_range(
        pd.to_datetime(str(lo), format="%Y%m%d"),
        pd.to_datetime(str(hi), format="%Y%m%d"), freq="D")]})

    grid = keys.merge(cal, how="cross")
    src = daily.copy()
    if not src.empty:
        src[date_col] = src[date_col].astype(str)
    out = grid.merge(src, on=key_cols + [date_col], how="left")

    for c in value_cols:
        out[c] = pd.to_numeric(out.get(c), errors="coerce").fillna(0.0)
    for c in carry_cols:
        # 描述列随实体走：用该实体已知的值填满全部日期
        if not src.empty:
            fill = (src.dropna(subset=[c]).groupby(key_cols, as_index=False)[c]
                    .first().rename(columns={c: "_fill"}))
            out = out.merge(fill, on=key_cols, how="left")
            out[c] = out[c].fillna(out["_fill"]).fillna("")
            out = out.drop(columns=["_fill"])
        else:
            out[c] = ""
    return out


def window_dates(anchor: str, days: int) -> List[str]:
    """[anchor-days+1, anchor] 的日期字符串列表（升序）。"""
    end = pd.to_datetime(str(anchor), format="%Y%m%d")
    start = end - pd.Timedelta(days=days - 1)
    return [d.strftime("%Y%m%d") for d in pd.date_range(start, end, freq="D")]


def prev_window_dates(anchor: str, days: int) -> List[str]:
    """上一周期 [anchor-2*days+1, anchor-days] 的日期字符串列表。"""
    end = pd.to_datetime(str(anchor), format="%Y%m%d") - pd.Timedelta(days=days)
    start = end - pd.Timedelta(days=days - 1)
    return [d.strftime("%Y%m%d") for d in pd.date_range(start, end, freq="D")]
