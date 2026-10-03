# -*- coding: utf-8 -*-
"""跑批上下文：一次运行里所有 builder 共享的东西。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

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

    @property
    def score_weights(self) -> tuple:
        """本次跑批的得分权重 (好, 中, 差)，来自 settings.score_weights（domain §1.1）。"""
        from . import metric_calc_domain as D
        return D.resolve_score_weights(getattr(self.settings, "score_weights", None))

    @property
    def macro_granularities(self) -> list:
        """macro 表的周期粒度清单（domain §1.10），已校验。"""
        from . import metric_calc_domain as D
        return D.macro_granularities(getattr(self.settings, "macro_granularities", None))

    def backfill_lookback(self, windows=None) -> dict:
        """本次跑批的明细补齐上限：{窗口天数: 最多往前找几天}（domain §1.8）。

        口径默认值在 domain.BACKFILL_LOOKBACK，settings.backfill_lookback 可逐档覆盖。
        backfill_mode = "off" 时返回空字典 = 不补。windows=None 返回全部档位。
        """
        from . import metric_calc_domain as D
        return D.resolve_backfill_lookback(
            getattr(self.settings, "backfill_mode", D.DEFAULT_BACKFILL_MODE),
            getattr(self.settings, "backfill_lookback", None), windows)

    @property
    def fill_start(self) -> str:
        """补齐明细最早要用到哪一天：load_start 再往前「最大上限」天。

        这一段只当补齐的来源，不进任何窗口合计 —— 否则取数区间第一天如果恰好没评论，
        它就补不出来，和逐日跑的结果对不上。
        """
        lb = max(self.backfill_lookback().values(), default=0)
        if lb <= 0:
            return self.load_start
        return (pd.to_datetime(self.load_start, format="%Y%m%d")
                - pd.Timedelta(days=int(lb))).strftime("%Y%m%d")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
