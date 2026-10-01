# -*- coding: utf-8 -*-
"""跑批上下文：一次运行里所有 builder 共享的东西。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from .settings import EtlSettings


@dataclass
class RunContext:
    settings: EtlSettings
    # 需要产出的目标日期（yyyyMMdd，升序）。窗口计算会用到更早的历史，
    # 但只有这些日期会被写库 —— 否则每天跑批都在重写半年的历史行。
    output_dates: List[str]
    # 实际参与计算的取数区间（含为了算窗口/环比而多取的历史）
    load_start: str
    load_end: str
    scenic_codes: Optional[List[str]] = None
    etl_time: datetime = field(default_factory=datetime.now)
    # 跑批过程中累积的告警，最后统一打印，不要散在各处 print
    warnings: List[str] = field(default_factory=list)
    # 整窗前移回补的实际发生情况：{表名: {窗口: (挪了几行, 总行数, 最大前移天数)}}
    shift_stats: Dict[str, dict] = field(default_factory=dict)

    @property
    def publish_time_format(self) -> str:
        return getattr(self.settings, "publish_time_format", "%Y%m%d")

    def publish_time(self, travel_date: pd.Series) -> pd.Series:
        """travel_date(yyyyMMdd) → publish_time 字符串。

        DDL 注释写的是 'YYYY-MM-DD HH:mm:ss'，但 publish_time 进了三张表的
        唯一索引，格式一旦变更就会跟历史数据产生重复行。默认沿用 yyyyMMdd，
        要改成 datetime 口径就把 settings.publish_time_format 设成
        '%Y-%m-%d %H:%M:%S'，并同步清洗历史数据。
        """
        fmt = self.publish_time_format
        if fmt == "%Y%m%d":
            return travel_date.astype(str)
        return pd.to_datetime(travel_date.astype(str), format="%Y%m%d").dt.strftime(fmt)

    def backfill_lookback(self, windows) -> dict:
        """本次跑批用的「整窗前移」上限：{窗口天数: 最大前移天数}。

        口径默认值在 domain §1.8，settings.backfill_lookback 可逐档覆盖。
        backfill_mode != "shift" 时返回空字典 = 不做前移回补。
        """
        from . import metric_calc_domain as D
        mode = getattr(self.settings, "backfill_mode", D.DEFAULT_BACKFILL_MODE)
        if mode != D.BACKFILL_MODE_SHIFT:
            return {}
        table = dict(D.BACKFILL_LOOKBACK)
        table.update(getattr(self.settings, "backfill_lookback", None) or {})
        return {int(w): int(table[int(w)]) for w in windows if int(w) in table}

    def note_shift(self, label: str, win: pd.DataFrame) -> None:
        """记录某张表的整窗前移实际发生了多少 —— 回补是「看不见的手」，
        不统计出来就没人知道今天有多少指标其实是借来的。

        入参：label 表名（报告里显示）；win rolling_windows 的产出
              （带 shift_<N>d 列，没有这些列就什么都不记）
        """
        stat = {}
        for c in win.columns:
            if not (isinstance(c, str) and c.startswith("shift_") and c.endswith("d")):
                continue
            v = pd.to_numeric(win[c], errors="coerce").fillna(0)
            moved = v > 0
            if not moved.any():
                continue
            stat[c[len("shift_"):-1]] = (int(moved.sum()), len(v), int(v.max()))
        if stat:
            self.shift_stats[label] = stat

    def shift_report(self) -> List[str]:
        """把 note_shift 攒下来的统计渲染成报告行。"""
        out = []
        for label, stat in self.shift_stats.items():
            parts = [f"{w}日 {n:,}/{tot:,} 行(最多前移 {mx} 天)"
                     for w, (n, tot, mx) in sorted(stat.items(), key=lambda x: int(x[0]))]
            out.append(f"{label} 触发整窗前移：" + "，".join(parts))
        return out

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
