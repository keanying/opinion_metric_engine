# -*- coding: utf-8 -*-
"""多窗口滚动聚合。

四张表一共有近百个 `_7d / _30d / _365d` 字段，如果每张表各写一套循环，
口径迟早会漂。这里提供**唯一一份**滚动实现，四个 builder 全部复用。

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
    shift_lookback: dict | None = None,
    presence_col: str | None = None,
) -> pd.DataFrame:
    """把日粒度事实表滚成多窗口累计值。

    参数
    ----
    daily : 日粒度事实，每个 (key, date) 一行
    key_cols : 分组键，如 ["scenic_spot_code"] 或 ["scenic_spot_code", "platform_code"]
    date_col : 日期列，字符串 yyyyMMdd
    value_cols : 要滚动求和的度量列
    windows : 窗口天数，如 (1, 7, 30, 60, 90, 365)
    with_prev : 是否同时产出上一周期累计（列名 `<col>_prev_<N>d`），用于环比
    output_dates : 只保留这些日期的结果；None 表示全部
    shift_lookback : 缺数回补（整窗前移）。{窗口天数: 最大前移天数}。
               某个窗口当期没数据时，把整个窗口整体往前挪，挪到有数据为止，
               用**那一个窗口**的值（不是累计）。挪满上限还没有就取 0。
               口径与上限见 domain §1.8。传 None = 不回补。
    presence_col : 判断「这个窗口有没有数据」看哪一列（> 0 即有）。
               开启 shift_lookback 时必填。
    calendar : **权威**日历范围 (start, end)。不传就取数据自己的最小/最大日期 ——
               但那样「最后几天一条数据都没有」时，这几天根本不会出现在结果里。
               需要为空白日期也产出行时（见 domain §1.7 行存在规则），必须显式传。
               传了就以它为准：区间外的数据会被丢弃，**不会**把面板撑大 ——
               源表里混进一条 2013 年的评论就能让日历从 400 天变成 4700 天，
               词表一大，面板直接爆掉。

    返回
    ----
    key_cols + [date_col] + `<col>_<N>d` (+ `<col>_prev_<N>d`)
    开启回补时额外返回 `shift_<N>d`：该窗口实际往前挪了几天（0 = 没挪）。
    """
    if shift_lookback and not presence_col:
        raise ValueError("开启 shift_lookback 时必须指定 presence_col")
    key_cols = list(key_cols)
    value_cols = list(value_cols)
    windows = sorted({int(w) for w in windows})
    if daily.empty:
        cols = key_cols + [date_col]
        for w in windows:
            for c in value_cols:
                cols.append(f"{c}_{w}d")
                if with_prev:
                    cols.append(f"{c}_prev_{w}d")
        return pd.DataFrame(columns=cols)

    df = daily[key_cols + [date_col] + value_cols].copy()
    df[_DT] = to_dt(df[date_col])
    if calendar:
        # 区间外的行直接丢掉：日历由 calendar 说了算，不能被离群日期撑大
        lo_c = pd.to_datetime(str(calendar[0]), format="%Y%m%d")
        hi_c = pd.to_datetime(str(calendar[1]), format="%Y%m%d")
        df = df[(df[_DT] >= lo_c) & (df[_DT] <= hi_c)]
        if df.empty:
            cols = key_cols + [date_col]
            for w in windows:
                for c in value_cols:
                    cols.append(f"{c}_{w}d")
                    if with_prev:
                        cols.append(f"{c}_prev_{w}d")
            return pd.DataFrame(columns=cols)

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
    if calendar:
        lo = pd.to_datetime(str(calendar[0]), format="%Y%m%d")
        hi = pd.to_datetime(str(calendar[1]), format="%Y%m%d")
    else:
        lo, hi = wide.index.min(), wide.index.max()
    full = pd.date_range(lo, hi, freq="D")
    wide = wide.reindex(full, fill_value=0.0).sort_index()

    # 只要输出这几天，就在**展开成长表之前**先把行裁掉。
    # 否则「11000 个词 × 4700 天 × 6 个窗口」会先物化成几千万行再丢掉 99.99%。
    keep = None
    if output_dates is not None:
        keep = full.isin(pd.to_datetime(sorted(set(output_dates)), format="%Y%m%d"))

    parts: List[pd.DataFrame] = []
    for w in windows:
        roll = wide.rolling(w, min_periods=1).sum()
        prev = roll.shift(w).fillna(0.0) if with_prev else None

        lb = int((shift_lookback or {}).get(w, 0))
        if lb > 0:
            # 整窗前移：为每个 (输出日期, 实体) 找到最近一个「有数据」的窗口
            roll, prev, shift_days = _shift_to_nearest_data(
                roll, prev, w, presence_col, lb, keep, full)
            parts.append(shift_days)
        elif keep is not None:
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


def _shift_to_nearest_data(roll, prev, window, presence_col, lookback, keep, full):
    """把每个 (输出日期, 实体) 的窗口整体往前挪到「有数据」的那一天。

    做法：对每个输出日期行，按 s = 0, 1, 2 … 逐步往前试，第一个满足
    `presence_col > 0` 的 s 就是答案；挪满 lookback 还没有就留在原位（值全 0）。

    **不是累计**：挪到 s 天前就只用那一个窗口的值，不把中间几天加进来 ——
    这是业务方定的口径（domain §1.8）。

    只对**输出日期**做这件事：输出一天时就是 (lookback+1) × 实体数 次布尔运算；
    对着整段历史做就是几千万次，没有必要。

    返回 (挪好的 roll, 挪好的 prev, 附加列的长表)

    附加列：
      shift_<N>d          实际往前挪了几天（0 = 没挪）
      raw_<presence>_<N>d **没挪之前**的真实窗口值。行存在规则要看它 ——
                          看挪完的值会让「近 30 日出现过的词」被回补悄悄放大成
                          「近 50 日出现过的词」，内容表会跟着膨胀。
    """
    kids = roll.columns.get_level_values(_KID).to_numpy()
    n_keys = int(kids.max()) + 1 if len(kids) else 0

    pres = roll[presence_col].to_numpy() > 0                  # (n_dates, n_keys)
    rows = np.arange(len(full)) if keep is None else np.where(keep)[0]

    src = np.full((len(rows), n_keys), -1, dtype="int64")
    todo = np.ones((len(rows), n_keys), dtype=bool)
    for s in range(lookback + 1):
        cand = rows - s
        ok = cand >= 0
        hit = todo & ok[:, None] & pres[np.clip(cand, 0, None)]
        src = np.where(hit, cand[:, None], src)
        todo &= ~hit
        if not todo.any():
            break
    src = np.where(src >= 0, src, rows[:, None])              # 找不到 → 原位（全 0）

    def _gather(df, offset=0):
        """按 src 逐列取值。列的第二层就是 kid，直接拿它当索引。"""
        if df is None:
            return None
        idx = np.clip(src - offset, 0, len(full) - 1)
        arr = df.to_numpy()
        out = np.empty((len(rows), arr.shape[1]), dtype="float64")
        for j, k in enumerate(kids):
            out[:, j] = arr[idx[:, int(k)], j]
        return pd.DataFrame(out, index=full[rows], columns=df.columns)

    roll2 = _gather(roll)
    # 上一周期跟着一起挪：比的是「实际用的那个窗口」与它之前一个周期，口径才自洽。
    # prev 本身已经是 roll.shift(window)，所以这里用同一个 src 取即可。
    prev2 = _gather(prev)

    days = (rows[:, None] - src).astype("int64")
    sd = pd.DataFrame(days, index=full[rows],
                      columns=pd.Index(np.arange(n_keys), name=_KID))
    sd = sd.stack().to_frame(f"shift_{window}d")
    sd.index = sd.index.set_names([_DT, _KID])

    raw = pd.DataFrame(roll[presence_col].to_numpy()[rows], index=full[rows],
                       columns=pd.Index(np.arange(n_keys), name=_KID))
    raw = raw.stack().to_frame(f"raw_{presence_col}_{window}d")
    raw.index = raw.index.set_names([_DT, _KID])
    return roll2, prev2, pd.concat([sd, raw], axis=1)


def _to_long(wide: pd.DataFrame, value_cols: Sequence[str], suffix: str) -> pd.DataFrame:
    """(date × [metric, kid]) 宽表 → (date, kid) 索引的长表，列名加窗口后缀。"""
    long = wide.stack(level=_KID)
    long.index = long.index.set_names([_DT, _KID])
    long = long[list(value_cols)]
    long.columns = [f"{c}{suffix}" for c in value_cols]
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
